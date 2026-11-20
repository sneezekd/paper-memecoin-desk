"""Logger read-side: report, morning recap, reject audit, dashboard JSON. Read-only over the DB."""
import json, time, datetime as dt
from zoneinfo import ZoneInfo
from .engine import Logger, Ledger, load_config, ROOT, now, sync_entry_pause
from . import sources as S
from . import outcomes as O

TZ = ZoneInfo("America/Edmonton")


def lt(ts):
    return dt.datetime.fromtimestamp(ts, TZ).strftime("%b %d %I:%M %p MT") if ts else ""


def _ctx():
    log = Logger()
    return log, Ledger(log, load_config()), log.db


def _rows(db, q, *a):
    return [dict(r) for r in db.execute(q, a).fetchall()]


def funnel_since(db, since):
    runs = _rows(db, "SELECT * FROM runs WHERE kind='scan' AND ts>=? AND summary IS NOT NULL", since)
    scouted_events = sum((json.loads(r["summary"]).get("scouted") or 0) for r in runs)
    uniq = db.execute("SELECT COUNT(DISTINCT address) FROM scouted WHERE ts>=?", (since,)).fetchone()[0]
    kills = _rows(db, "SELECT gate, COUNT(*) n, COUNT(DISTINCT address) coins, SUM(unverifiable) unverif FROM kills "
                      "WHERE ts>=? GROUP BY gate ORDER BY n DESC", since)
    passed = db.execute("SELECT COUNT(*) FROM gate_passes WHERE ts>=?", (since,)).fetchone()[0]
    scored = db.execute("SELECT COUNT(*) FROM scores WHERE ts>=?", (since,)).fetchone()[0]
    s70 = db.execute("SELECT COUNT(*) FROM scores WHERE ts>=? AND passed=1", (since,)).fetchone()[0]
    buys = db.execute("SELECT COUNT(*) FROM trades WHERE side='BUY' AND ts>=?", (since,)).fetchone()[0]
    return {"scans": len(runs), "scouted_pair_checks": scouted_events, "scouted_unique_coins": uniq,
            "kills_by_gate": kills, "filter_passed": passed, "scored": scored, "scored_70plus": s70, "bought": buys}


def _last_scan_ticker(db, limit=40):
    """Coins from the latest scan, for the public dashboard ticker. Read-only."""
    last = db.execute("SELECT id FROM runs WHERE kind='scan' ORDER BY id DESC LIMIT 1").fetchone()
    if not last:
        return []
    rows = _rows(db, "SELECT symbol, address, price, mcap, liq, dex, url FROM scouted "
                     "WHERE run_id=? ORDER BY id LIMIT ?", last["id"], limit)
    out = []
    for c in rows:
        ch = None
        for r in db.execute("SELECT evidence FROM kills WHERE address=? AND run_id=? ORDER BY id DESC LIMIT 4",
                            (c["address"], last["id"])):
            try:
                ev = json.loads(r["evidence"] or "{}")
            except Exception:
                continue
            if ev.get("chg_h1") is not None:
                try:
                    ch = float(ev["chg_h1"])
                    break
                except (TypeError, ValueError):
                    pass
        out.append({"symbol": c["symbol"], "address": c["address"], "price": c["price"], "chg_h1": ch,
                    "mcap": c["mcap"], "liq": c["liq"], "dex": c["dex"], "url": c["url"]})
    return out


def _recent_rejects(db, limit):
    out = []
    for r in _rows(db, "SELECT ts, symbol, address, stage, gate, reason, unverifiable, price, liq, mcap, evidence "
                       "FROM kills ORDER BY id DESC LIMIT ?", limit):
        try:
            ch = float(json.loads(r.pop("evidence") or "{}").get("chg_h1"))
        except (TypeError, ValueError, AttributeError):
            ch = None
        out.append(r | {"at": lt(r["ts"]), "chg_h1": ch})
    return out


def dashboard_data():
    log, L, db = _ctx()
    cfg = load_config()
    start = float(log.meta("starting_balance"))
    ops = _rows(db, "SELECT * FROM positions WHERE status='open' ORDER BY opened_at")
    for p in ops:
        mark = p["last_mark"] or p["entry_mark"]
        p["mark_value_usd"] = round(L.pos_net(p, mark), 4)
        p["unrealized_pnl_usd"] = round(p["mark_value_usd"] - p["size_usd"], 4)
    closed = _rows(db, "SELECT * FROM positions WHERE status='closed' ORDER BY closed_at DESC")
    eq = L.equity()
    realized = sum(p["pnl_usd"] or 0 for p in closed)
    last = db.execute("SELECT * FROM runs WHERE kind='scan' ORDER BY id DESC LIMIT 1").fetchone()
    out = {
        "generated_at": lt(now()), "generated_ts": now(), "mode": "PAPER ONLY",
        "balance": {"starting_usd": start, "cash_usd": round(L.cash, 4), "equity_usd": round(eq, 4),
                    "total_pnl_usd": round(eq - start, 4), "total_pnl_pct": round((eq / start - 1) * 100, 2),
                    "realized_pnl_usd": round(realized, 4),
                    "unrealized_pnl_usd": round(sum(p["unrealized_pnl_usd"] for p in ops), 4)},
        "open_positions": [{k: p[k] for k in ("symbol", "address", "score", "size_usd", "entry_fill", "last_mark", "stop_mark",
                                              "initial_stop_mark", "trail_active", "peak_mark", "unrealized_pnl_usd", "url")}
                           | {"opened": lt(p["opened_at"]), "opened_ts": p["opened_at"], "x_score": p.get("x_score")} for p in ops],
        "closed_trades": [{k: p[k] for k in ("symbol", "address", "score", "size_usd", "entry_fill", "exit_fill", "pnl_usd",
                                             "pnl_pct", "exit_reason", "url")} | {"opened": lt(p["opened_at"]), "closed": lt(p["closed_at"]),
                                                                                  "opened_ts": p["opened_at"], "closed_ts": p["closed_at"],
                                                                                  "x_score": p.get("x_score")}
                          for p in closed],
        "wins": sum(1 for p in closed if (p["pnl_usd"] or 0) > 0), "losses": sum(1 for p in closed if (p["pnl_usd"] or 0) <= 0),
        "funnel_24h": funnel_since(db, now() - 86400),
        "funnel_all_time": funnel_since(db, 0),
        "last_scan": {"at": lt(last["ts"]), "secs": last["secs"], "summary": json.loads(last["summary"] or "{}"),
                      "sources": json.loads(last["status"] or "{}")} if last else None,
        "recent_rejects": _recent_rejects(db, 100),
        "equity_curve": [{"at": lt(r["ts"]), "ts": r["ts"], "equity": round(r["equity"], 4)} for r in _rows(
            db, "SELECT ts, equity FROM equity ORDER BY id DESC LIMIT 300")][::-1],
        "rules": {"stop_net_pct": cfg["risk"]["hard_stop_net_pct"], "trail_after_net_pct": cfg["risk"]["trail_activate_net_pct"],
                  "trail_distance_pct": cfg["risk"]["trail_distance_from_peak_pct"],
                  "max_trade_pct_equity": cfg["bankroll"]["max_trade_pct_of_equity"],
                  "max_trade_pct_pool_liq": cfg["bankroll"]["max_trade_pct_of_pool_liquidity"],
                  "min_score": cfg["analyst"]["min_score"]},
        "ticker": _last_scan_ticker(db),
        "entry_pause": sync_entry_pause(log, L, cfg),
    }
    oc = O.update_and_export(db, "main", O.seed_main)       # never raises; DexScreener errors land in outcomes_meta
    out.update(oc)
    srcs = (out["last_scan"] or {}).get("sources") or {}
    out["health"] = O.health_from_sources(srcs, [oc["outcomes_meta"].get("error")])
    p = ROOT / "reports" / "dashboard.json"
    p.parent.mkdir(exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=1, default=str))
    tmp.replace(p)
    return out


def recap(hours=12):
    log, L, db = _ctx()
    since = now() - hours * 3600
    fn = funnel_since(db, since)
    start_eq = db.execute("SELECT equity FROM equity WHERE ts<? ORDER BY id DESC LIMIT 1", (since,)).fetchone()
    trades = _rows(db, "SELECT * FROM positions WHERE opened_at>=? OR closed_at>=? OR status='open' ORDER BY opened_at", since, since)
    closed = [t for t in trades if t["status"] == "closed" and t["closed_at"] >= since]
    w = sum(1 for t in closed if t["pnl_usd"] > 0); l_ = len(closed) - w
    best = max(closed, key=lambda t: t["pnl_usd"], default=None)
    worst = min(closed, key=lambda t: t["pnl_usd"], default=None)
    lines = [f"# Morning recap (PAPER) - last {hours:g}h, to {lt(now())}", "",
             f"- Scans run: {fn['scans']}",
             f"- Scanned: {fn['scouted_unique_coins']} unique coins in the 30m-72h / $300k-$20M window ({fn['scouted_pair_checks']} pair checks)",
             f"- Passed all gates: {fn['filter_passed']} | scored: {fn['scored']} | scored 70+: {fn['scored_70plus']}",
             f"- Trades taken: {fn['bought']}",
             f"- W/L (closed in window): {w}/{l_}",
             f"- Best: {best['symbol']} {best['pnl_usd']:+.2f} ({best['pnl_pct']:+.1f}%)" if best else "- Best: none",
             f"- Worst: {worst['symbol']} {worst['pnl_usd']:+.2f} ({worst['pnl_pct']:+.1f}%)" if worst else "- Worst: none",
             f"- Final balance: equity ${L.equity():.2f} (cash ${L.cash:.2f})"
             + (f", started window at ${start_eq[0]:.2f}" if start_eq else ""), "",
             "## Killed by each rule (kills / unique coins / of which unverifiable)"]
    lines += [f"- {k['gate']}: {k['n']} / {k['coins']} / {k['unverif']}" for k in fn["kills_by_gate"]] or ["- none"]
    lines += ["", "## Trades (entry, size, score, exit, reason)"]
    for t in trades:
        ex = (f"exit {t['exit_fill']:.10g} on {lt(t['closed_at'])}, {t['exit_reason']}, P&L {t['pnl_usd']:+.2f} ({t['pnl_pct']:+.1f}%)"
              if t["status"] == "closed" else f"OPEN, last {t['last_mark']:.10g}, stop {t['stop_mark']:.10g}"
              + (" (trailing)" if t["trail_active"] else ""))
        lines.append(f"- {t['symbol']}: entry {t['entry_fill']:.10g} on {lt(t['opened_at'])}, size ${t['size_usd']:.2f}, "
                     f"score {t['score']}, {ex}")
    if not trades:
        lines.append("- No trades. Nothing passed every gate and scored 70+, so the desk stayed flat.")
    txt = "\n".join(lines)
    (ROOT / "reports" / "recap_latest.md").write_text(txt)
    return txt


def report(hours=24):
    d = dashboard_data()
    b = d["balance"]
    lines = [f"# Paper desk report {d['generated_at']}", f"Equity ${b['equity_usd']:.2f} (cash ${b['cash_usd']:.2f}), "
             f"P&L {b['total_pnl_usd']:+.2f} ({b['total_pnl_pct']:+.2f}%), realized {b['realized_pnl_usd']:+.2f}, "
             f"unrealized {b['unrealized_pnl_usd']:+.2f}", "", "## Open"]
    lines += [f"- {p['symbol']} size ${p['size_usd']:.2f} entry {p['entry_fill']:.8g} last {p['last_mark']:.8g} "
              f"stop {p['stop_mark']:.8g}{' trailing' if p['trail_active'] else ''} uPnL {p['unrealized_pnl_usd']:+.2f}"
              for p in d["open_positions"]] or ["- none"]
    lines += ["", "## Funnel last 24h", json.dumps(funnel_since(_ctx()[2], now() - hours * 3600), indent=1, default=str),
              "", "## Recent rejects"]
    lines += [f"- {r['at']} {r['symbol']} [{r['gate']}] {r['reason']}" for r in d["recent_rejects"][:25]]
    txt = "\n".join(lines)
    (ROOT / "reports" / "report_latest.md").write_text(txt)
    return txt


def reject_audit(days=7):
    log, L, db = _ctx()
    A = load_config()["audit"]
    since = now() - days * 86400
    first_kill = _rows(db, "SELECT address, symbol, gate, MIN(ts) ts, price, liq FROM kills WHERE ts>=? AND price>0 "
                           "GROUP BY address", since)
    passed = _rows(db, "SELECT address, symbol, MIN(ts) ts, price, liq, MAX(score) score FROM scores WHERE ts>=? AND passed=1 "
                       "GROUP BY address", since)
    traded = _rows(db, "SELECT address, symbol, opened_at ts, entry_mark price, entry_liq liq FROM positions WHERE opened_at>=?", since)
    allp = {r["address"]: r for r in passed + traded}
    cur, _ = S.ds_best_pairs([r["address"] for r in first_kill] + list(allp), set(load_config()["scout"]["allowed_dex_ids"]))
    ran, by_gate = [], {}
    for r in first_kill:
        q = cur.get(r["address"])
        if q and q["price_usd"] and r["price"] and (q["price_usd"] / r["price"] - 1) * 100 >= A["ran_threshold_pct"]:
            chg = (q["price_usd"] / r["price"] - 1) * 100
            ran.append({"symbol": r["symbol"], "gate": r["gate"], "since_kill_pct": round(chg, 1)})
            by_gate.setdefault(r["gate"], []).append(r["symbol"])
    rugged = []
    for a, r in allp.items():
        q = cur.get(a)
        dd = (1 - q["price_usd"] / r["price"]) * 100 if q and q["price_usd"] and r["price"] else None
        if q is None or (dd is not None and dd >= A["rug_drawdown_pct"]) or \
                (q and r["liq"] and (q["liq_usd"] or 0) < r["liq"] * A["rug_liquidity_fraction"]):
            rugged.append({"symbol": r["symbol"], "drawdown_pct": round(dd, 1) if dd is not None else "pair gone",
                           "liq_then": r["liq"], "liq_now": q["liq_usd"] if q else None})
    kills_total = {k["gate"]: k["coins"] for k in funnel_since(db, since)["kills_by_gate"]}
    out = {"window_days": days, "rejected_coins_checked": len(first_kill),
           "rejected_then_ran_by_gate": {g: {"ran": len(v), "of_killed": kills_total.get(g), "coins": v} for g, v in by_gate.items()},
           "rejected_then_ran": sorted(ran, key=lambda x: -x["since_kill_pct"])[:30],
           "passed_coins_checked": len(allp), "passed_then_rugged": rugged,
           "note": f"ran = +{A['ran_threshold_pct']}% since first kill; rugged = -{A['rug_drawdown_pct']}% or liquidity < "
                   f"{A['rug_liquidity_fraction']:.0%} of then, or pair gone"}
    (ROOT / "reports" / "reject_audit_latest.json").write_text(json.dumps(out, indent=1))
    return out
