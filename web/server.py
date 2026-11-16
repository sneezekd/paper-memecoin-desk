#!/usr/bin/env python3
"""Read-only live dashboard for the paper memecoin desk.

Serves one HTML page and /api/state from desk.db + events.jsonl.
PAPER ONLY. Never opens wallets, secrets, or writes to the desk DB.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "desk.db"
EVENTS = ROOT / "logs" / "events.jsonl"
SCHED_PID = ROOT / "logs" / "scheduler.pid"
STATIC = Path(__file__).resolve().parent / "static"
TZ = ZoneInfo("America/Edmonton")
TOKEN = os.environ.get("DASHBOARD_TOKEN", "").strip()
HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.environ.get("DASHBOARD_PORT", "8788"))

CHG_RE = re.compile(r"already \+(-?[\d.]+)% in 1h", re.I)


def _hard_stop_label() -> str:
    """Display label from config.json. int(-8) -> '-8%'. No stored stop is rewritten."""
    cfg = json.loads((ROOT / "config.json").read_text())
    return f"{int(cfg['risk']['hard_stop_net_pct'])}%"
GATE_LABELS = {
    "volume_1h": "1h volume",
    "top10_real": "top-10 holders",
    "bundled_launch": "bundled launch",
    "pumped_1h": "already +80% in 1h",
    "lp_burn_or_lock": "LP burn / lock",
    "mint_or_freeze": "mint or freeze authority",
    "dev_hold": "dev wallet",
    "creator_rugs": "creator rugs",
    "liquidity": "liquidity",
    "sell_sim": "sell simulation",
    "token2022_fees": "Token-2022 fees",
    "order_size": "order vs pool",
    "chase": "chase",
    "price_impact": "price impact",
    "stale_data": "stale data",
    "requote": "re-quote",
}


def _now() -> float:
    return time.time()


def lt(ts, fmt="%b %d %I:%M %p MT") -> str:
    if not ts:
        return ""
    if isinstance(ts, str):
        try:
            ts = dt.datetime.strptime(ts.replace("Z", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()
        except Exception:
            return ts
    return dt.datetime.fromtimestamp(float(ts), TZ).strftime(fmt)


def hhmm(ts) -> str:
    return lt(ts, "%H:%M")


def _open_db():
    uri = f"file:{DB}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def _rows(db, q, *a):
    return [dict(r) for r in db.execute(q, a).fetchall()]


def _meta(db, k, default=None):
    r = db.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
    return default if r is None else r[0]


def _scheduler_status(last_ts):
    running, pid = False, None
    try:
        if SCHED_PID.is_file():
            pid = int(SCHED_PID.read_text().strip())
            os.kill(pid, 0)
            running = True
    except (ValueError, OSError, ProcessLookupError):
        running, pid = False, pid
    age = (_now() - last_ts) if last_ts else None
    # Full scans are about every 3 minutes; allow a few misses before calling the board stale.
    stale = bool(age is not None and age > 12 * 60)
    if not running:
        label = "Stopped"
    elif stale:
        label = "Running · stale"
    else:
        label = "Running"
    return {"running": running, "pid": pid, "stale": stale, "label": label, "last_scan_age_s": round(age) if age is not None else None}


def _parse_event_ts(raw):
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%SZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            d = dt.datetime.strptime(s.replace("+00:00", "Z"), fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            return d.timestamp()
        except Exception:
            continue
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _tail_jsonl(path: Path, n=120, max_bytes=800_000):
    if not path.is_file():
        return []
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            fh.readline()
        raw = fh.read().decode("utf-8", "replace")
    out = []
    for line in raw.splitlines()[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _chg_from_reason(reason):
    if not reason:
        return None
    m = CHG_RE.search(reason)
    return float(m.group(1)) if m else None


def _chg_h1_for(db, address, run_id, price, ts):
    # 1) same-run kill reason / evidence (real 1h change the filter saw)
    for r in db.execute(
        "SELECT reason, evidence FROM kills WHERE address=? AND run_id=? ORDER BY id DESC LIMIT 4",
        (address, run_id),
    ):
        ev = {}
        try:
            ev = json.loads(r["evidence"] or "{}")
        except Exception:
            ev = {}
        if ev.get("chg_h1") is not None:
            try:
                return float(ev["chg_h1"])
            except (TypeError, ValueError):
                pass
        ch = _chg_from_reason(r["reason"])
        if ch is not None:
            return ch
    # 2) a stored price about an hour earlier, if we have one
    if price and ts:
        lo, hi = ts - 80 * 60, ts - 40 * 60
        prev = db.execute(
            "SELECT price, ts FROM scouted WHERE address=? AND ts BETWEEN ? AND ? AND price>0 "
            "ORDER BY ABS(ts - ?) LIMIT 1",
            (address, lo, hi, ts - 3600),
        ).fetchone()
        if prev and prev["price"]:
            return round((price / prev["price"] - 1) * 100, 2)
    return None


def _pid_symbols(db):
    return {r["id"]: r["symbol"] for r in db.execute("SELECT id, symbol FROM positions")}


def _feed_from_events(events, symbols):
    feed = []
    for ev in reversed(events):  # newest first after we iterate file order
        kind = ev.get("event")
        ts = _parse_event_ts(ev.get("ts"))
        sym = ev.get("symbol") or symbols.get(ev.get("position_id")) or ""
        item = None
        if kind == "kill":
            reason = ev.get("reason") or ev.get("gate") or "killed"
            gate = ev.get("gate") or ""
            item = {
                "agent": "FILTER",
                "kind": "kill",
                "action": f"killed {sym or 'coin'}" + (f" · {gate}" if gate else "") + f" · {reason}",
            }
        elif kind == "filter_pass":
            item = {"agent": "FILTER", "kind": "pass", "action": f"passed {sym}"}
        elif kind == "score":
            sc = ev.get("score")
            passed = ev.get("passed")
            bit = "passed 70+" if passed else "below 70"
            item = {"agent": "ANALYST", "kind": "score", "action": f"scored {sym} {sc} · {bit}"}
        elif kind == "paper_buy":
            usd = ev.get("usd")
            usd_s = f"${usd:,.2f}" if isinstance(usd, (int, float)) else ""
            item = {"agent": "EXECUTOR", "kind": "buy", "action": f"bought {sym} {usd_s} · paper".strip()}
        elif kind == "paper_sell":
            usd = ev.get("usd")
            pnl = ev.get("pnl_usd")
            reason = ev.get("reason") or "exit"
            parts = [f"sold {sym} · {reason}"]
            if isinstance(pnl, (int, float)):
                parts.append(f"P&L {pnl:+.2f}")
            elif isinstance(usd, (int, float)):
                parts.append(f"${usd:,.2f}")
            item = {"agent": "EXECUTOR", "kind": "sell", "action": " · ".join(parts)}
        elif kind == "trail_on":
            net = ev.get("net_pct")
            extra = f" · {net:+.1f}% net" if isinstance(net, (int, float)) else ""
            item = {"agent": "RISK", "kind": "risk", "action": f"trailing stop on {sym}{extra}"}
        elif kind == "stale_mark":
            item = {"agent": "RISK", "kind": "risk", "action": f"stale mark on {sym} · stop still armed"}
        elif kind == "run_end":
            sm = ev.get("summary") or {}
            if isinstance(sm, str):
                try:
                    sm = json.loads(sm)
                except Exception:
                    sm = {}
            disc = sm.get("discovered")
            scouted = sm.get("scouted")
            bought = sm.get("bought") or []
            n_buy = len(bought) if isinstance(bought, list) else (bought or 0)
            action = "scan done"
            if disc is not None:
                action += f" · {disc} discovered"
            if scouted is not None:
                action += f" · {scouted} in window"
            if n_buy:
                action += f" · {n_buy} bought"
            item = {"agent": "SCOUT", "kind": "scan", "action": action}
        elif kind == "ledger_init":
            bal = ev.get("starting_balance_usd", 100)
            item = {"agent": "LOGGER", "kind": "info", "action": f"paper ledger init · ${float(bal):.0f} starting balance"}
        else:
            continue
        if item:
            item.update({"ts": ts, "hhmm": hhmm(ts) if ts else "--:--", "symbol": sym})
            feed.append(item)
    return feed


def snapshot():
    db = _open_db()
    try:
        start = float(_meta(db, "starting_balance", 100.0))
        cash = float(_meta(db, "cash", start))
        last = db.execute("SELECT * FROM runs WHERE kind='scan' ORDER BY id DESC LIMIT 1").fetchone()
        last = dict(last) if last else None
        if last:
            try:
                last["summary"] = json.loads(last["summary"] or "{}")
            except Exception:
                last["summary"] = {}
            try:
                last["status"] = json.loads(last["status"] or "{}")
            except Exception:
                last["status"] = {}

        eq_rows = _rows(db, "SELECT ts, cash, open_value, equity, n_open FROM equity ORDER BY id")
        latest_eq = eq_rows[-1] if eq_rows else None
        ops = _rows(db, "SELECT * FROM positions WHERE status='open' ORDER BY opened_at")
        closed = _rows(db, "SELECT * FROM positions WHERE status='closed' ORDER BY closed_at DESC")
        open_value = 0.0
        open_out = []
        for p in ops:
            mark = p.get("last_mark") or p.get("entry_mark") or 0
            size = p.get("size_usd") or 0
            # Prefer the desk's own last_net_pct (net of exit costs) when present
            if p.get("last_net_pct") is not None:
                pnl_pct = p["last_net_pct"]
                pnl_usd = round(size * pnl_pct / 100.0, 4) if size else 0.0
            else:
                pnl_usd = 0.0
                pnl_pct = 0.0
            # mark-to-market value approximation for equity: size + pnl
            mv = size + pnl_usd
            open_value += mv
            open_out.append({
                "symbol": p.get("symbol"),
                "address": p.get("address"),
                "score": p.get("score"),
                "size_usd": p.get("size_usd"),
                "entry": p.get("entry_fill") or p.get("entry_mark"),
                "mark": mark,
                "stop": p.get("stop_mark"),
                "trail_active": bool(p.get("trail_active")),
                "peak": p.get("peak_mark"),
                "pnl_usd": round(pnl_usd, 4),
                "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
                "opened": lt(p.get("opened_at")),
                "url": p.get("url"),
            })
        if latest_eq and not ops:
            equity = float(latest_eq["equity"])
            cash = float(latest_eq["cash"])
            open_value = float(latest_eq["open_value"] or 0)
        elif latest_eq and ops:
            # Recompute from latest cash + open pnl so the board moves between scans
            cash = float(_meta(db, "cash", latest_eq["cash"]))
            equity = cash + open_value
        else:
            equity = cash + open_value

        closed_out = []
        for p in closed:
            closed_out.append({
                "symbol": p.get("symbol"),
                "address": p.get("address"),
                "score": p.get("score"),
                "size_usd": p.get("size_usd"),
                "entry": p.get("entry_fill") or p.get("entry_mark"),
                "exit": p.get("exit_fill") or p.get("exit_mark"),
                "reason": p.get("exit_reason"),
                "pnl_usd": p.get("pnl_usd") or 0,
                "pnl_pct": p.get("pnl_pct"),
                "opened": lt(p.get("opened_at")),
                "closed": lt(p.get("closed_at")),
                "url": p.get("url"),
            })
        wins = sum(1 for p in closed if (p.get("pnl_usd") or 0) > 0)
        losses = sum(1 for p in closed if p.get("status") == "closed" and (p.get("pnl_usd") or 0) <= 0)
        # losses already filtered to closed
        losses = sum(1 for p in closed if (p.get("pnl_usd") or 0) <= 0)

        # scanned = sum of discovered across scans, else scouted checks
        discovered_sum = 0
        have_disc = False
        for r in db.execute("SELECT summary FROM runs WHERE kind='scan' AND summary IS NOT NULL"):
            try:
                sm = json.loads(r[0] or "{}")
            except Exception:
                sm = {}
            if "discovered" in sm and sm["discovered"] is not None:
                discovered_sum += int(sm["discovered"] or 0)
                have_disc = True
        scouted_n = db.execute("SELECT COUNT(*) FROM scouted").fetchone()[0]
        scanned = discovered_sum if have_disc else scouted_n
        buys = db.execute("SELECT COUNT(*) FROM trades WHERE side='BUY'").fetchone()[0]
        scored_n = db.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
        scored_pass = db.execute("SELECT COUNT(*) FROM scores WHERE passed=1").fetchone()[0]
        kills_n = db.execute("SELECT COUNT(*) FROM kills").fetchone()[0]
        kill_rows = _rows(
            db,
            "SELECT gate, COUNT(*) n, COUNT(DISTINCT address) coins, SUM(unverifiable) unverif "
            "FROM kills GROUP BY gate ORDER BY n DESC",
        )
        for k in kill_rows:
            k["label"] = GATE_LABELS.get(k["gate"], (k["gate"] or "").replace("_", " "))

        # ticker: latest scan's scouted coins
        ticker = []
        if last:
            for c in _rows(db, "SELECT * FROM scouted WHERE run_id=? ORDER BY id", last["id"]):
                ticker.append({
                    "symbol": c["symbol"],
                    "price": c["price"],
                    "chg_h1": _chg_h1_for(db, c["address"], c["run_id"], c["price"], c["ts"]),
                    "mcap": c["mcap"],
                    "liq": c["liq"],
                    "dex": c["dex"],
                    "url": c["url"],
                    "address": c["address"],
                })

        # equity curve; if empty, flat $start from first scan (or now-1h) to now
        curve = [{"ts": r["ts"], "at": lt(r["ts"]), "equity": round(r["equity"], 4)} for r in eq_rows]
        if not curve:
            first = db.execute("SELECT MIN(ts) FROM runs WHERE kind='scan'").fetchone()[0]
            t0 = first or (_now() - 3600)
            curve = [
                {"ts": t0, "at": lt(t0), "equity": start},
                {"ts": _now(), "at": lt(_now()), "equity": start},
            ]
        else:
            # extend to now if the last point isn't current, so the line reaches the present
            if _now() - curve[-1]["ts"] > 15:
                curve.append({"ts": _now(), "at": lt(_now()), "equity": round(equity, 4)})

        symbols = _pid_symbols(db)
        # also map from recent kills / scouted in case buy events lack a join
        events = _tail_jsonl(EVENTS, n=200)
        feed = _feed_from_events(events, symbols)

        sm = (last or {}).get("summary") or {}
        last_scouted = sm.get("scouted")
        last_disc = sm.get("discovered")
        last_kills = sm.get("filter_kills") or {}
        last_killed = sum(last_kills.values()) if isinstance(last_kills, dict) else 0

        last_kill = db.execute("SELECT ts, symbol, gate, reason FROM kills ORDER BY id DESC LIMIT 1").fetchone()
        last_score = db.execute("SELECT ts, symbol, score, passed FROM scores ORDER BY id DESC LIMIT 1").fetchone()
        last_buy = db.execute(
            "SELECT t.ts, t.usd, t.side, p.symbol FROM trades t LEFT JOIN positions p ON p.id=t.position_id "
            "WHERE t.side='BUY' ORDER BY t.id DESC LIMIT 1"
        ).fetchone()
        last_sell = db.execute(
            "SELECT t.ts, t.usd, t.pnl_usd, t.reason, p.symbol FROM trades t LEFT JOIN positions p ON p.id=t.position_id "
            "WHERE t.side='SELL' ORDER BY t.id DESC LIMIT 1"
        ).fetchone()
        last_risk = db.execute("SELECT ts, why FROM stop_moves ORDER BY id DESC LIMIT 1").fetchone()
        n_stops = db.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0]
        n_trails = db.execute("SELECT COUNT(*) FROM positions WHERE status='open' AND trail_active=1").fetchone()[0]

        if last and last_disc is not None:
            scout_act = f"{last_disc} discovered this scan · {last_scouted or 0} in the 30m–72h / $300k–$20M window"
        elif scouted_n:
            scout_act = f"{scouted_n} pair checks logged"
        else:
            scout_act = "waiting for the first scan"

        if last_kill:
            filter_act = f"killed {last_kill['symbol']} · {last_kill['gate']} · {last_kill['reason']}"
        else:
            filter_act = "no kills yet"
        if last and last_scouted:
            filter_act = f"this scan: {last_killed} of {last_scouted} killed" + (
                f" · latest {last_kill['symbol']} ({last_kill['gate']})" if last_kill else ""
            )

        if last_score:
            bit = "passed 70+" if last_score["passed"] else "below 70"
            analyst_act = f"scored {last_score['symbol']} {last_score['score']} · {bit}"
        else:
            analyst_act = "waiting — nothing has cleared the filter yet"

        if last_buy:
            executor_act = f"bought {last_buy['symbol']} ${float(last_buy['usd']):.2f} paper"
        else:
            executor_act = "idle — nothing scored 70+, so no entry"

        if n_stops:
            risk_act = f"{n_stops} open · stops armed" + (f" · {n_trails} trailing" if n_trails else "")
        elif last_sell:
            risk_act = f"exited {last_sell['symbol']} · {last_sell['reason']}"
        else:
            risk_act = f"no open positions · stop {_hard_stop_label()} · trail +12%"

        logger_act = f"{kills_n} kills logged" + (f" · {buys} trades" if buys else " · no trades yet")

        kill_pct = round(100.0 * kills_n / scouted_n, 0) if scouted_n else 0
        agents = [
            {
                "id": "scout",
                "name": "Scout",
                "subtitle": "30m–72h · $300k–$20M",
                "activity": scout_act,
                "count": scanned,
                "badge": f"{scanned:,}",
            },
            {
                "id": "filter",
                "name": "Filter",
                "subtitle": "reason for all",
                "activity": filter_act,
                "count": kills_n,
                "badge": f"{kills_n:,} · {kill_pct:.0f}%",
            },
            {
                "id": "analyst",
                "name": "Analyst",
                "subtitle": "score 70+ only",
                "activity": analyst_act,
                "count": scored_n,
                "badge": f"{scored_n:,}" + (f" · {scored_pass} pass" if scored_n else ""),
            },
            {
                "id": "executor",
                "name": "Executor",
                "subtitle": "8% max size",
                "activity": executor_act,
                "count": buys,
                "badge": f"{buys:,}",
            },
            {
                "id": "risk",
                "name": "Risk",
                "subtitle": f"stop {_hard_stop_label()} · trail +12%",
                "activity": risk_act,
                "count": n_stops,
                "badge": f"{n_stops} open" + (f" · {n_trails} trail" if n_trails else ""),
            },
            {
                "id": "logger",
                "name": "Logger",
                "subtitle": "reason for all",
                "activity": logger_act,
                "count": kills_n + buys + len(closed),
                "badge": f"{kills_n + buys:,}",
            },
        ]

        best = None
        if closed_out:
            best = max(closed_out, key=lambda t: t["pnl_usd"] if t["pnl_usd"] is not None else float("-inf"))

        pnl_usd = equity - start
        pnl_pct = (equity / start - 1) * 100 if start else 0
        sched = _scheduler_status(last["ts"] if last else None)

        return {
            "generated_at": lt(_now()),
            "generated_ts": _now(),
            "mode": "PAPER ONLY",
            "scanner": {
                **sched,
                "last_scan_at": lt(last["ts"]) if last else None,
                "last_scan_ts": last["ts"] if last else None,
                "last_scan_secs": last["secs"] if last else None,
                "last_summary": sm if last else None,
            },
            "equity": {
                "starting_usd": start,
                "cash_usd": round(cash, 4),
                "equity_usd": round(equity, 4),
                "pnl_usd": round(pnl_usd, 4),
                "pnl_pct": round(pnl_pct, 2),
                "multiplier": round(equity / start, 4) if start else 1.0,
                "wins": wins,
                "losses": losses,
            },
            "equity_curve": curve,
            "stats": {"scanned": scanned, "traded": buys, "scouted_checks": scouted_n},
            "ticker": ticker,
            "agents": agents,
            "feed": feed[:120],
            "kills_total": kills_n,
            "kills_by_rule": kill_rows,
            "open_positions": open_out,
            "closed_trades": closed_out,
            "best_trade": best,
        }
    finally:
        db.close()


_LAST_OK = None


def get_state():
    global _LAST_OK
    try:
        _LAST_OK = snapshot()
        _LAST_OK["stale"] = False
        return _LAST_OK
    except Exception as e:
        if _LAST_OK:
            out = dict(_LAST_OK)
            out["stale"] = True
            out["error"] = str(e)
            return out
        raise


class Handler(BaseHTTPRequestHandler):
    server_version = "PaperDeskDashboard/1.0"

    def log_message(self, fmt, *args):
        # keep it quiet; still record errors
        if args and str(args[0]).startswith(("4", "5")):
            super().log_message(fmt, *args)

    def _token_ok(self, got):
        if not TOKEN or not got:
            return False
        import hmac
        return hmac.compare_digest(str(got), str(TOKEN))

    def _auth(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        qs = parse_qs(u.query)
        qs_tok = (qs.get("token") or [None])[0]
        path_tok = parts[0] if parts else None
        tok = path_tok if path_tok and self._token_ok(path_tok) else (qs_tok if self._token_ok(qs_tok) else None)
        if not tok:
            return None, None
        rest = "/" + "/".join(parts[1:]) if self._token_ok(path_tok) else u.path
        if rest == "":
            rest = "/"
        return tok, rest

    def _send(self, code, body, ctype="text/html; charset=utf-8", cache="no-store"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        tok, rest = self._auth()
        if not tok:
            self._send(401, "unauthorized\n", "text/plain; charset=utf-8")
            return
        if rest in ("/", "/index.html", ""):
            html = (STATIC / "index.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8", "no-cache")
            return
        if rest in ("/api/state", "/api/state/"):
            try:
                data = get_state()
                body = json.dumps(data, default=str).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:
                err = json.dumps({"error": str(e), "mode": "PAPER ONLY"}).encode()
                self._send(500, err, "application/json; charset=utf-8")
            return
        if rest in ("/api/health", "/api/health/"):
            self._send(200, b'{"ok":true,"mode":"PAPER ONLY"}', "application/json; charset=utf-8")
            return
        self._send(404, "not found\n", "text/plain; charset=utf-8")

    def do_HEAD(self):
        # health-style probes
        tok, rest = self._auth()
        if not tok:
            self.send_response(401)
        elif rest in ("/", "/index.html", "/api/state", "/api/state/", "/api/health", "/api/health/", ""):
            self.send_response(200)
        else:
            self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()


def main():
    if not TOKEN:
        raise SystemExit("DASHBOARD_TOKEN is not set")
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    print(f"paper-desk dashboard listening on http://{HOST}:{PORT}/<token>/  PAPER ONLY", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
