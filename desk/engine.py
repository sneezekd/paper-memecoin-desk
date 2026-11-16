"""Six-stage PAPER desk: Scout -> Filter -> Analyst -> Executor -> Risk -> Logger.

PAPER ONLY. Nothing here holds a wallet, signs, or sends a transaction, and there is no live switch.
Each stage is a separate class; a stage only receives what the previous stage handed off.
"""
import json, sqlite3, time, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo
from . import sources as S
from .sources import f

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "desk.db"
EVENTS = ROOT / "logs" / "events.jsonl"
SOL = S.SOL_MINT
BURN = S.BURN_OWNERS


def now():
    return time.time()


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ") if ts else ""


def load_config():
    return json.loads((ROOT / "config.json").read_text())


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def trading_day(now_ts=None, cfg=None):
    """America/Edmonton calendar day by default. Returns (iso_date, start_ts, end_ts)."""
    tzname = ((cfg or {}).get("brake") or {}).get("timezone") or "America/Edmonton"
    t = dt.datetime.fromtimestamp(now() if now_ts is None else now_ts, ZoneInfo(tzname))
    start = t.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1)
    return start.date().isoformat(), start.timestamp(), end.timestamp()


def _row_val(row, key, idx=0):
    if row is None:
        return None
    if hasattr(row, "keys"):
        return row[key]
    return row[idx]


def start_of_day_equity(db, day_start, cfg):
    row = db.execute("SELECT equity FROM equity WHERE ts < ? ORDER BY ts DESC LIMIT 1", (day_start,)).fetchone()
    val = _row_val(row, "equity")
    if val is not None:
        return float(val)
    meta = db.execute("SELECT value FROM meta WHERE key='starting_balance'").fetchone()
    val = _row_val(meta, "value")
    if val is not None:
        return float(val)
    return float(cfg["bankroll"]["starting_balance_usd"])


def entry_pause_view(db, cfg, equity, now_ts=None, meta_day=None, meta_reason=None):
    """Read-only daily brake. Stays on for the rest of the local day once tripped."""
    brake = cfg["brake"]
    day, day_start, _end = trading_day(now_ts, cfg)
    sod = start_of_day_equity(db, day_start, cfg)
    pnl = (equity / sod - 1) * 100 if sod else 0.0
    tzname = brake.get("timezone") or "America/Edmonton"
    if meta_day == day and meta_reason:
        return {"paused": True, "reason": meta_reason, "day": day, "day_pnl_pct": round(pnl, 2),
                "start_of_day_equity_usd": round(sod, 4), "timezone": tzname}
    reason = None
    if pnl <= float(brake["max_day_loss_pct"]):
        reason = (f"day P&L {pnl:.1f}% <= {brake['max_day_loss_pct']}% of start-of-day equity ${sod:.2f}")
    worst = db.execute(
        "SELECT symbol, pnl_pct FROM positions WHERE status='closed' AND closed_at>=? AND pnl_pct<=? "
        "ORDER BY pnl_pct ASC LIMIT 1",
        (day_start, float(brake["max_single_loss_pct"]))).fetchone()
    if worst is not None:
        bit = (f"{_row_val(worst, 'symbol')} closed {float(_row_val(worst, 'pnl_pct')):.1f}% "
               f"(worse than {brake['max_single_loss_pct']}%)")
        reason = f"{reason}; {bit}" if reason else bit
    return {"paused": reason is not None, "reason": reason, "day": day, "day_pnl_pct": round(pnl, 2),
            "start_of_day_equity_usd": round(sod, 4), "timezone": tzname}


def sync_entry_pause(log, ledger, cfg, marks=None, now_ts=None):
    """Persist the brake when it first trips today. Later calls do not re-log it."""
    view = entry_pause_view(log.db, cfg, ledger.equity(marks), now_ts=now_ts,
                            meta_day=log.meta("entry_pause_day"), meta_reason=log.meta("entry_pause_reason"))
    if view["paused"] and not (log.meta("entry_pause_day") == view["day"] and log.meta("entry_pause_reason")):
        log.set_meta("entry_pause_day", view["day"])
        log.set_meta("entry_pause_reason", view["reason"])
        log.event("entry_paused", reason=view["reason"], day=view["day"], day_pnl_pct=view["day_pnl_pct"],
                  start_of_day_equity_usd=view["start_of_day_equity_usd"])
    return view


def path_has_dump_then_bounce(prices, dump_pct, bounce_pct):
    """True when a later trough is dump_pct under an earlier peak, then a later price bounces bounce_pct off that trough."""
    n = len(prices)
    for i in range(n):
        peak = prices[i]
        if not peak or peak <= 0:
            continue
        trough, trough_at = peak, i
        for j in range(i + 1, n):
            if prices[j] < trough:
                trough, trough_at = prices[j], j
            if trough <= 0 or j <= trough_at:
                continue
            dump = (peak - trough) / peak * 100
            bounce = (prices[j] / trough - 1) * 100
            if dump > dump_pct and bounce > bounce_pct:
                return True
    return False


def chase_flush_verdict(chg_m5, candles, now_ts, entry_cfg):
    """None if the coin may continue. Otherwise (gate, reason, unverifiable). Missing 5m path fails closed."""
    window = float(entry_cfg["flush_window_minutes"]) * 60
    bar = 300.0
    usable = []
    for c in candles or []:
        ts = c.get("ts")
        o, h, l, cl = c.get("open"), c.get("high"), c.get("low"), c.get("close")
        if ts is None or None in (o, h, l, cl):
            continue
        if min(o, h, l, cl) <= 0:
            continue
        if ts + bar >= now_ts - window and ts <= now_ts + 120:
            usable.append(c)
    usable.sort(key=lambda c: c["ts"])
    chg = chg_m5
    if chg is None and usable and usable[-1]["open"]:
        chg = (usable[-1]["close"] / usable[-1]["open"] - 1) * 100
    if chg is None:
        return ("chase_5m", "5m price change missing", True)
    cap = float(entry_cfg["max_chase_5m_pct"])
    if chg > cap:
        return ("chase_5m", f"up {chg:.1f}% in 5m (> {cap:.0f}%)", False)
    if len(usable) < 2:
        return ("flush_bounce", "5m candles missing for the 10m flush check", True)
    dump, bounce = float(entry_cfg["flush_dump_pct"]), float(entry_cfg["flush_bounce_pct"])
    paths = []
    for order in (("high", "low"), ("low", "high")):
        path = []
        for c in usable:
            path.extend([c["open"], c[order[0]], c[order[1]], c["close"]])
        paths.append(path)
    for path in paths:
        if path_has_dump_then_bounce(path, dump, bounce):
            return ("flush_bounce",
                    f"dumped >{dump:.0f}% then bounced >{bounce:.0f}% within {entry_cfg['flush_window_minutes']}m",
                    False)
    return None


def fill_vs_mark_pct(fill, mark):
    """Positive when the paper fill is worse (higher) than the mark."""
    if fill is None or not mark or mark <= 0:
        return None
    return (fill / mark - 1) * 100


def round_trip_loss_pct(amount_in, amount_out):
    if not amount_in:
        return None
    return (1 - amount_out / amount_in) * 100


def liq_drop_reason(entry_liq, liq_now, drop_pct, floor):
    """Immediate exit text, or None. Unknown liquidity does not invent an exit. The stored stop is not moved."""
    if liq_now is None:
        return None
    if floor is not None and liq_now < float(floor):
        return f"pool liquidity ${liq_now:,.0f} below ${float(floor):,.0f} floor"
    if entry_liq and entry_liq > 0:
        drop = (1 - liq_now / entry_liq) * 100
        if round(drop, 4) > float(drop_pct):
            return (f"pool liquidity ${liq_now:,.0f} is {drop:.0f}% below entry ${entry_liq:,.0f} "
                    f"(>{float(drop_pct):.0f}%)")
    return None


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, ts REAL, kind TEXT, status TEXT, summary TEXT, secs REAL);
CREATE TABLE IF NOT EXISTS scouted(id INTEGER PRIMARY KEY, run_id INT, ts REAL, address TEXT, symbol TEXT, pair TEXT,
  dex TEXT, age_min REAL, mcap REAL, liq REAL, price REAL, sources TEXT, url TEXT);
CREATE TABLE IF NOT EXISTS kills(id INTEGER PRIMARY KEY, run_id INT, ts REAL, stage TEXT, address TEXT, symbol TEXT,
  gate TEXT, reason TEXT, unverifiable INT, price REAL, liq REAL, mcap REAL, evidence TEXT);
CREATE TABLE IF NOT EXISTS gate_passes(id INTEGER PRIMARY KEY, run_id INT, ts REAL, address TEXT, symbol TEXT, evidence TEXT);
CREATE TABLE IF NOT EXISTS scores(id INTEGER PRIMARY KEY, run_id INT, ts REAL, address TEXT, symbol TEXT, score REAL,
  coverage_pct REAL, passed INT, components TEXT, missing TEXT, price REAL, liq REAL);
CREATE TABLE IF NOT EXISTS holder_snaps(id INTEGER PRIMARY KEY, ts REAL, address TEXT, holders INT, source TEXT);
CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY, address TEXT UNIQUE, symbol TEXT, status TEXT,
  opened_at REAL, closed_at REAL, score REAL, size_usd REAL, decimals INT, qty REAL, entry_mark REAL, entry_fill REAL,
  entry_liq REAL, stop_mark REAL, initial_stop_mark REAL, trail_active INT DEFAULT 0, peak_mark REAL,
  last_mark REAL, last_check REAL, last_net_pct REAL, exit_mark REAL, exit_fill REAL, proceeds REAL,
  pnl_usd REAL, pnl_pct REAL, exit_reason TEXT, half_size INT, url TEXT, entry_rationale TEXT, exit_cost_frac REAL);
CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, position_id INT, ts REAL, side TEXT, reason TEXT,
  mark REAL, fill REAL, qty REAL, usd REAL, fees_usd REAL, pnl_usd REAL, price_source TEXT, rationale TEXT);
CREATE TABLE IF NOT EXISTS stop_moves(id INTEGER PRIMARY KEY, position_id INT, ts REAL, old_stop REAL, new_stop REAL, why TEXT);
CREATE TABLE IF NOT EXISTS equity(id INTEGER PRIMARY KEY, run_id INT, ts REAL, cash REAL, open_value REAL, equity REAL,
  n_open INT);
CREATE TABLE IF NOT EXISTS rc_cache(address TEXT PRIMARY KEY, ts REAL, data TEXT);
CREATE TABLE IF NOT EXISTS kv_cache(key TEXT PRIMARY KEY, ts REAL, data TEXT);
"""


class Logger:
    """Stage 6. Append-only: every write is an INSERT (positions is the only mutable state table)."""

    def __init__(self):
        DB.parent.mkdir(exist_ok=True)
        EVENTS.parent.mkdir(exist_ok=True)
        self.db = sqlite3.connect(DB, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.run_id = None

    def event(self, kind, **kw):
        with EVENTS.open("a") as fh:
            fh.write(json.dumps({"ts": iso(now()), "run_id": self.run_id, "event": kind, **kw}, default=str) + "\n")

    def meta(self, k, default=None):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        return default if r is None else r[0]

    def set_meta(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))
        self.db.commit()

    def cache_get(self, key, ttl):
        r = self.db.execute("SELECT ts,data FROM kv_cache WHERE key=?", (key,)).fetchone()
        if r and now() - r["ts"] < ttl:
            return json.loads(r["data"])
        return None

    def cache_set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO kv_cache VALUES(?,?,?)", (key, now(), json.dumps(value, default=str)))
        self.db.commit()

    def start_run(self, kind):
        self.run_id = self.db.execute("INSERT INTO runs(ts,kind) VALUES(?,?)", (now(), kind)).lastrowid
        self.db.commit()
        return self.run_id

    def end_run(self, status, summary, t0):
        self.db.execute("UPDATE runs SET status=?, summary=?, secs=? WHERE id=?",
                        (json.dumps(status), json.dumps(summary), round(now() - t0, 1), self.run_id))
        self.db.commit()
        self.event("run_end", status=status, summary=summary, secs=round(now() - t0, 1))

    def scouted(self, c):
        d = c["ds"]
        self.db.execute("INSERT INTO scouted(run_id,ts,address,symbol,pair,dex,age_min,mcap,liq,price,sources,url) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (self.run_id, now(), c["address"], c["symbol"], d["pair"], d["dex"], c["age_min"], d["mcap"],
                         d["liq_usd"], d["price_usd"], ",".join(sorted(c["sources"])), d.get("url")))

    def kill(self, c, stage, gate, reason, unverifiable=False, evidence=None):
        d = c.get("ds") or {}
        self.db.execute("INSERT INTO kills(run_id,ts,stage,address,symbol,gate,reason,unverifiable,price,liq,mcap,evidence) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (self.run_id, now(), stage, c["address"], c.get("symbol"), gate, reason, int(unverifiable),
                         d.get("price_usd"), d.get("liq_usd"), d.get("mcap"), json.dumps(evidence or {}, default=str)))
        self.event("kill", stage=stage, symbol=c.get("symbol"), address=c["address"], gate=gate, reason=reason,
                   unverifiable=unverifiable)

    def gate_pass(self, c, evidence):
        self.db.execute("INSERT INTO gate_passes(run_id,ts,address,symbol,evidence) VALUES(?,?,?,?,?)",
                        (self.run_id, now(), c["address"], c["symbol"], json.dumps(evidence, default=str)))
        self.event("filter_pass", symbol=c["symbol"], address=c["address"])

    def score(self, c, s):
        d = c["ds"]
        self.db.execute("INSERT INTO scores(run_id,ts,address,symbol,score,coverage_pct,passed,components,missing,price,liq) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (self.run_id, now(), c["address"], c["symbol"], s["score"], s["coverage_pct"], int(s["passed"]),
                         json.dumps(s["components"]), json.dumps(s["missing"]), d["price_usd"], d["liq_usd"]))
        self.event("score", symbol=c["symbol"], address=c["address"], **s)

    def trade(self, pid, side, reason, mark, fill, qty, usd, fees, pnl, src, rationale):
        self.db.execute("INSERT INTO trades(position_id,ts,side,reason,mark,fill,qty,usd,fees_usd,pnl_usd,price_source,rationale) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (pid, now(), side, reason, mark, fill, qty, usd, fees, pnl, src, rationale))
        self.event("paper_" + side.lower(), position_id=pid, reason=reason, mark=mark, fill=fill, usd=usd, pnl_usd=pnl,
                   price_source=src, rationale=rationale)

    def equity(self, cash, open_value, n_open):
        self.db.execute("INSERT INTO equity(run_id,ts,cash,open_value,equity,n_open) VALUES(?,?,?,?,?,?)",
                        (self.run_id, now(), cash, open_value, cash + open_value, n_open))
        self.db.commit()


class Ledger:
    """Paper cash + cost model shared by Executor and Risk."""

    def __init__(self, log, cfg):
        self.log, self.cfg = log, cfg
        if log.meta("cash") is None:
            bal = cfg["bankroll"]["starting_balance_usd"]
            log.set_meta("cash", bal); log.set_meta("starting_balance", bal); log.set_meta("created_at", now())
            log.event("ledger_init", starting_balance_usd=bal, note="PAPER ONLY")

    @property
    def cash(self):
        return float(self.log.meta("cash"))

    def add_cash(self, d):
        self.log.set_meta("cash", self.cash + d)

    def open_positions(self):
        return self.log.db.execute("SELECT * FROM positions WHERE status='open' ORDER BY opened_at").fetchall()

    def model_sell_net(self, qty, mark, liq):
        c = self.cfg["costs"]
        gross_mark = qty * mark
        impact = (gross_mark / liq * 100 * c["model_price_impact_multiplier"]) if liq else 5.0
        slip = c["extra_adverse_slippage_pct"] + impact
        gross = gross_mark * (1 - slip / 100)
        return gross * (1 - c["swap_fee_pct"] / 100) - c["network_fee_usd_per_tx"]

    def pos_net(self, p, mark):
        """Net exit value of a position at `mark`, using the exit-cost fraction measured by a real Jupiter
        sell quote at entry (pool fees + impact + DexScreener/Jupiter basis) plus network fee."""
        k = p["exit_cost_frac"] if p["exit_cost_frac"] is not None else 0.03
        return p["qty"] * mark * (1 - k) - self.cfg["costs"]["network_fee_usd_per_tx"]

    def open_value(self, marks=None):
        v = 0.0
        for p in self.open_positions():
            m = (marks or {}).get(p["address"], {}).get("price_usd") or p["last_mark"] or p["entry_mark"]
            v += max(0.0, self.pos_net(p, m))
        return v

    def equity(self, marks=None):
        return self.cash + self.open_value(marks)


class Scout:
    """Stage 1. New Pump.fun / Raydium pairs via DexScreener, 30m-72h old, mcap $300k-$20M."""

    def __init__(self, cfg, log):
        self.cfg, self.log = cfg["scout"], log

    def run(self, status):
        sc = self.cfg
        allowed = set(sc["allowed_dex_ids"])
        addrs, st = S.ds_discovery(sc["dexscreener_search_terms"], allowed)
        status.update(st)
        if sc.get("geckoterminal_address_discovery"):
            ga, st2 = S.gecko_discovery()
            status.update(st2)
            for a, labels in ga.items():
                addrs.setdefault(a, set()).update(labels)
        pairs, errs = S.ds_best_pairs(list(addrs), allowed)
        if errs:
            status["ds_pairs_errors"] = errs[:3]
        counts = {"discovered": len(addrs), "no_pumpfun_raydium_pair": 0, "age_out_of_window": 0,
                  "mcap_out_of_window": 0, "missing_age_or_mcap": 0, "scouted": 0}
        out = []
        for a, labels in addrs.items():
            d = pairs.get(a)
            if not d:
                counts["no_pumpfun_raydium_pair"] += 1; continue
            if not d.get("pair_created_ms") or d.get("mcap") is None:
                counts["missing_age_or_mcap"] += 1; continue
            age = (now() - d["pair_created_ms"] / 1000) / 60
            if not (sc["min_pair_age_minutes"] <= age <= sc["max_pair_age_minutes"]):
                counts["age_out_of_window"] += 1; continue
            if not (sc["min_mcap_usd"] <= d["mcap"] <= sc["max_mcap_usd"]):
                counts["mcap_out_of_window"] += 1; continue
            c = {"address": a, "symbol": d.get("symbol") or a[:6], "sources": labels, "ds": d,
                 "age_min": round(age, 1), "signal_price": d["price_usd"], "signal_ts": d["fetched_at"]}
            self.log.scouted(c)
            out.append(c)
        counts["scouted"] = len(out)
        self.log.db.commit()
        return out, counts


class Gate(Exception):
    def __init__(self, gate, reason, unverifiable=False, evidence=None):
        super().__init__(reason)
        self.gate, self.reason, self.unverifiable, self.evidence = gate, reason, unverifiable, evidence or {}


GATE_ORDER = ["liquidity", "volume_1h", "pumped_1h", "chase_5m", "flush_bounce", "mint_freeze", "token2022_ext",
              "lp_burn_lock", "top10_real", "dev_hold", "bundled_launch", "creator_rugs", "sell_sim",
              "entry_slippage", "entry_round_trip"]


class Filter:
    """Stage 2. Binary pass/fail gates, cheapest first, FAIL CLOSED. One hit = dead. No score can override."""

    def __init__(self, cfg, log, gmgn):
        self.cfg, self.G, self.log, self.gmgn = cfg, cfg["gates"], log, gmgn

    def _rugcheck(self, mint, fresh=False):
        if not fresh:
            r = self.log.db.execute("SELECT ts,data FROM rc_cache WHERE address=?", (mint,)).fetchone()
            if r and now() - r["ts"] < self.G["rugcheck_cache_seconds"]:
                return json.loads(r["data"]), r["ts"]
        d = S.rugcheck_report(mint)
        t = now()
        self.log.db.execute("INSERT OR REPLACE INTO rc_cache VALUES(?,?,?)", (mint, t, json.dumps(d)))
        self.log.db.commit()
        time.sleep(0.3)
        return d, t

    def _use_free(self):
        return bool((self.cfg.get("sources") or {}).get("use_free_sources_when_no_gmgn", True))

    def _bundle_share(self, c, ev, ts, gm_info):
        """Return (pct, source, error_reason_or_None). Prefer GMGN bundler_rate when available."""
        if self.gmgn and self.gmgn.available:
            try:
                ct = S.gmgn_created_tokens(self.gmgn, ev.get("creator") or (c.get("rc") or {}).get("creator"))
                ts["gmgn_created"] = now()
                ev["_gmgn_created"] = ct
                toks = (ct or {}).get("tokens") or []
                me = next((t for t in toks if t.get("token_address") == c["address"]), None)
                br = f((me or {}).get("bundler_rate"))
                if br is not None:
                    br = br * 100 if br <= 1 else br
                    return br, "gmgn", None
                if not self._use_free():
                    return None, "gmgn", "bundled-launch share not reported for this token"
            except Exception as e:
                ev["gmgn_created_error"] = str(e)[:160]
                if not self._use_free():
                    return None, "gmgn", f"GMGN created-tokens error: {e}"[:160]
        if not self._use_free() and not (self.gmgn and self.gmgn.available):
            return None, None, "unverifiable: needs GMGN API key (bundled-launch share)"
        try:
            pct, evb = S.bundle_share_from_rugcheck(c.get("rc") or {})
            ev["bundle_insiders"] = evb
            ts["bundle_free"] = ts.get("rugcheck") or now()
            return pct, "rugcheck_insiders", None
        except Exception as e:
            return None, "rugcheck_insiders", f"RugCheck insider/bundle data error: {e}"[:160]

    def _priors_from_cache(self, creator, current):
        rows = self.log.db.execute("SELECT address,data FROM rc_cache").fetchall()
        out = []
        for r in rows:
            if r["address"] == current:
                continue
            try:
                d = json.loads(r["data"])
            except Exception:
                continue
            if d.get("creator") == creator:
                tm = d.get("tokenMeta") or {}
                tok = {"mint": r["address"], "symbol": tm.get("symbol"),
                       "rugged": d.get("rugged"), "detectedAt": d.get("detectedAt")}
                out.append(tok)
        return out

    def _creator_history(self, c, ev, ts, creator):
        """Return (prev_list, rug_symbols, source, error_or_None). No history is not an error."""
        G = self.G
        look = G["creator_lookback_launches"]
        ttl = (self.cfg.get("sources") or {}).get("free_source_cache_seconds", 1800)
        prev, src = None, None

        if ev.get("_gmgn_created") or (self.gmgn and self.gmgn.available and ev.get("gmgn_created_error") is None):
            ct = ev.get("_gmgn_created")
            if ct is None and self.gmgn and self.gmgn.available:
                try:
                    ct = S.gmgn_created_tokens(self.gmgn, creator); ts["gmgn_created"] = now()
                    ev["_gmgn_created"] = ct
                except Exception as e:
                    ev["gmgn_created_error"] = str(e)[:160]
                    ct = None
            if ct is not None:
                toks = (ct or {}).get("tokens") or []
                prev = sorted([t for t in toks if t.get("token_address") != c["address"]],
                              key=lambda t: -(f(t.get("create_timestamp"), 0) or 0))[:look]
                for t in prev:
                    t.setdefault("mint", t.get("token_address"))
                src = "gmgn"
                rugs = []
                for t in prev:
                    ath, mc = f(t.get("token_ath_mc")), f(t.get("market_cap"))
                    if S.is_rug_by_drawdown(ath, mc, G["rug_def_min_ath_mcap_usd"], G["rug_def_drawdown_pct"]):
                        rugs.append(t.get("symbol") or t.get("mint"))
                return prev, rugs, src, None
            if not self._use_free():
                return None, None, "gmgn", ev.get("gmgn_created_error") or "GMGN created-tokens unavailable"

        if not self._use_free():
            return None, None, None, "unverifiable: needs GMGN API key (creator history)"

        ck = "creator_hist:" + creator
        cached = self.log.cache_get(ck, ttl)
        if cached and isinstance(cached, dict) and "prev" in cached:
            return cached.get("prev") or [], cached.get("rugs") or [], cached.get("source") or "cache", None
        priors = []
        sources = []
        # pump.fun (may be Cloudflare-blocked from this host — treat block as "unavailable", not a kill)
        try:
            pf = S.pumpfun_created_coins(creator, limit=max(look + 5, 15))
            ts["pumpfun_created"] = now()
            if pf is not None:
                sources.append("pump.fun")
                for t in pf:
                    m = S._mint_of(t)
                    if m and m != c["address"]:
                        t = dict(t); t["mint"] = m
                        priors.append(t)
        except Exception as e:
            ev["pumpfun_error"] = str(e)[:160]
            # A mid-flight error after the API was reachable is fail-closed
            if S.pumpfun_available() is True:
                return None, None, "pump.fun", f"pump.fun created-coins error: {e}"[:160]

        # RugCheck creatorTokens on this report (null = none detected, not unknown)
        rc_priors = S.prior_tokens_from_rugcheck(c.get("rc") or {}, c["address"])
        if rc_priors:
            sources.append("rugcheck_creatorTokens")
            have = {S._mint_of(x) for x in priors}
            priors.extend(t for t in rc_priors if S._mint_of(t) not in have)

        # Other RugCheck reports already cached for this creator
        cached_priors = self._priors_from_cache(creator, c["address"])
        if cached_priors:
            sources.append("rc_cache")
            have = {S._mint_of(x) for x in priors}
            priors.extend(t for t in cached_priors if S._mint_of(t) not in have)

        # Dedup, keep most recently mentioned first, cap at lookback
        seen, prev = set(), []
        for t in priors:
            m = S._mint_of(t)
            if not m or m in seen:
                continue
            seen.add(m); prev.append(t)
            if len(prev) >= look:
                break
        src = "+".join(sources) if sources else "free_none_detected"

        # DexScreener (any DEX) for ATH/current on prior mints
        ds_map = {}
        if prev:
            try:
                ds_map = S.dexscreener_pairs([S._mint_of(t) for t in prev])
                ts["ds_creator_priors"] = now()
            except Exception as e:
                ev["ds_priors_error"] = str(e)[:160]
        rugs, details = S.classify_prior_rugs(prev, ds_map, G["rug_def_min_ath_mcap_usd"], G["rug_def_drawdown_pct"])
        ev["creator_prior_details"] = details
        self.log.cache_set(ck, {"prev": prev, "rugs": rugs, "source": src})
        return prev, rugs, src, None

    def check(self, c, fresh=False, trade_usd=8.0):
        """Returns evidence dict (with per-gate fetched_at) or raises Gate."""
        G = self.G
        ev, ts = {}, {}
        c["_ev"] = ev
        d = c["ds"]
        ts["dexscreener"] = d["fetched_at"]

        # 1-3: DexScreener numbers already in hand (cheapest)
        liq = d.get("liq_usd")
        if liq is None:
            raise Gate("liquidity", "liquidity unknown", True)
        if liq < G["min_liquidity_usd"]:
            raise Gate("liquidity", f"liquidity ${liq:,.0f} < ${G['min_liquidity_usd']:,}")
        ev["liq_usd"] = liq
        v1 = d.get("vol_h1")
        if v1 is None:
            raise Gate("volume_1h", "1h volume unknown", True)
        if v1 < G["min_volume_h1_usd"]:
            raise Gate("volume_1h", f"1h volume ${v1:,.0f} < ${G['min_volume_h1_usd']:,}")
        ev["vol_h1"] = v1
        ch = d.get("chg_h1")
        if ch is None:
            raise Gate("pumped_1h", "1h price change unknown", True)
        if ch >= G["max_price_change_h1_pct"]:
            raise Gate("pumped_1h", f"already +{ch:.0f}% in 1h (>= +{G['max_price_change_h1_pct']}%)")
        ev["chg_h1"] = ch

        # 4-5: Solana RPC mint account (authorities + Token-2022 extensions)
        try:
            mi = S.rpc_mint_info(c["address"])
        except Exception as e:
            raise Gate("mint_freeze", f"RPC error: {e}"[:160], True)
        ts["rpc_mint"] = now()
        if mi["mintAuthority"] or mi["freezeAuthority"]:
            raise Gate("mint_freeze", f"mint authority={mi['mintAuthority']} freeze authority={mi['freezeAuthority']} (not revoked)")
        if mi["decimals"] is None or mi["supply"] is None:
            raise Gate("mint_freeze", "mint decimals/supply unknown", True)
        c["decimals"], c["supply_raw"] = int(mi["decimals"]), int(mi["supply"])
        ev["mint_freeze"] = "both revoked"
        ev["token_program"] = mi["program"]
        bad = []
        for x in mi["extensions"]:
            name, st = x.get("extension"), x.get("state") or {}
            if name == "transferHook" and (st.get("programId") or st.get("authority")):
                bad.append(f"transferHook program={st.get('programId')} authority={st.get('authority')}")
            if name == "transferFeeConfig":
                bps = max(f((st.get("newerTransferFee") or {}).get("transferFeeBasisPoints"), 0) or 0,
                          f((st.get("olderTransferFee") or {}).get("transferFeeBasisPoints"), 0) or 0)
                if st.get("transferFeeConfigAuthority") or bps > 0:
                    bad.append(f"transferFee {bps}bps authority={st.get('transferFeeConfigAuthority')}")
            if name == "permanentDelegate" and st.get("delegate"):
                bad.append(f"permanentDelegate {st.get('delegate')}")
            if name == "pausableConfig" and st.get("authority"):
                bad.append(f"pausable authority {st.get('authority')}")
        if bad:
            raise Gate("token2022_ext", "; ".join(bad)[:200])
        ev["token2022_ext"] = [x.get("extension") for x in mi["extensions"]] or "none (classic SPL)"

        # 6-8: RugCheck full report (LP, holders, creator)
        try:
            rc, rc_ts = self._rugcheck(c["address"], fresh=fresh)
        except Exception as e:
            raise Gate("lp_burn_lock", f"RugCheck error: {e}"[:160], True)
        ts["rugcheck"] = rc_ts
        markets = rc.get("markets") or []
        mk = next((m for m in markets if m.get("pubkey") == d["pair"]), None)
        if mk is None:
            raise Gate("lp_burn_lock", f"DexScreener pair {d['pair'][:8]}.. not in RugCheck markets ({len(markets)})", True)
        lp = mk.get("lp") or {}
        lpp = f(lp.get("lpLockedPct"))
        if lpp is None:
            raise Gate("lp_burn_lock", "LP locked/burned % unknown", True)
        if lpp < G["min_lp_locked_or_burned_pct"]:
            raise Gate("lp_burn_lock", f"LP burned/locked {lpp:.1f}% < {G['min_lp_locked_or_burned_pct']}%")
        lockers = rc.get("lockers") or {}
        my_lockers = [l for l in lockers.values() if isinstance(l, dict) and
                      (l.get("tokenAccount") in (mk.get("liquidityA"), mk.get("liquidityB")) or
                       l.get("pubkey") == mk.get("pubkey") or l.get("mint") in (mk.get("mintLP"),))] or \
                     [l for l in lockers.values() if isinstance(l, dict)]
        if my_lockers:
            unlocks = [f(l.get("unlockDate")) for l in my_lockers]
            if any(u is None for u in unlocks):
                raise Gate("lp_burn_lock", "LP locker present but unlock date unknown", True)
            u = min(unlocks)
            u = u / 1000 if u > 1e12 else u
            days = (u - now()) / 86400
            if days < G["min_lock_days"]:
                raise Gate("lp_burn_lock", f"LP lock expires in {days:.1f}d (< {G['min_lock_days']}d)")
            ev["lp"] = f"{lpp:.0f}% locked, unlock in {days:.0f}d"
        else:
            ev["lp"] = f"{lpp:.0f}% burned (no time locker)"

        supply = f((rc.get("token") or {}).get("supply"))
        holders = rc.get("topHolders")
        if not supply or not holders:
            raise Gate("top10_real", "holder list or supply missing from RugCheck", True)
        known = rc.get("knownAccounts") or {}
        pool_owners = {k for k, v in known.items() if (v or {}).get("type") in ("AMM", "LOCKER")}
        pool_owners |= {m.get("pubkey") for m in markets}
        pool_accts = set()
        for m in markets:
            pool_accts |= {m.get("liquidityA"), m.get("liquidityB")}
        real = [h for h in holders if h.get("owner") not in pool_owners and h.get("owner") not in BURN
                and h.get("address") not in pool_accts]
        top10 = sum(f(h.get("pct"), 0) for h in real[:10])
        non_ins = sum(f(h.get("pct"), 0) for h in [h for h in real if not h.get("insider")][:10])
        cluster = sum(f(n.get("currentHolding"), 0) for n in rc.get("insiderNetworks") or []) / supply * 100
        eff = max(top10, non_ins + cluster)
        ev["top10"] = {"real_top10_pct": round(top10, 2), "insider_cluster_pct": round(cluster, 2),
                       "effective_pct": round(eff, 2), "excluded_pool_burn": len(holders) - len(real)}
        if eff > G["max_top10_real_pct"]:
            raise Gate("top10_real", f"real top-10 {top10:.1f}% (+same-funder clusters -> {eff:.1f}%) > {G['max_top10_real_pct']}%",
                       evidence=ev["top10"])

        creator = rc.get("creator")
        cb = rc.get("creatorBalance")
        gm_info = None
        if self.gmgn and self.gmgn.available:
            try:
                gm_info = S.gmgn_token_info_raw(self.gmgn, c["address"]); ts["gmgn_info"] = now()
            except Exception as e:
                ev["gmgn_info_error"] = str(e)[:120]
        c["gmgn_info"] = gm_info
        if gm_info:
            creator = creator or (gm_info.get("dev") or {}).get("creator_address")
        if cb is None:
            raise Gate("dev_hold", "creator balance unknown", True)
        dev_pct = f(cb, 0) / supply * 100
        if gm_info:
            dth = f((gm_info.get("stat") or {}).get("dev_team_hold_rate"))
            if dth is not None:
                dev_pct = max(dev_pct, dth * 100)
        ev["dev_hold_pct"] = round(dev_pct, 3)
        if dev_pct > G["max_dev_hold_pct"]:
            raise Gate("dev_hold", f"dev/creator holds {dev_pct:.2f}% > {G['max_dev_hold_pct']}%")
        if not creator:
            raise Gate("creator_rugs", "creator address unknown", True)
        ev["creator"] = creator
        c["rc"] = rc  # needed by free-source bundle/creator helpers before sell_sim

        # 9-10: bundled launch + creator rugs. GMGN when a personal key is present, else free sources.
        br, bsrc, berr = self._bundle_share(c, ev, ts, gm_info)
        if berr:
            raise Gate("bundled_launch", berr, True)
        ev["bundled_pct"] = round(br, 2)
        ev["bundled_source"] = bsrc
        if br > G["max_bundled_pct"]:
            raise Gate("bundled_launch", f"bundled launch {br:.1f}% > {G['max_bundled_pct']}%")

        prev, rugs, hsrc, herr = self._creator_history(c, ev, ts, creator)
        if herr:
            raise Gate("creator_rugs", herr, True)
        c["no_creator_history"] = len(prev) == 0
        ev["creator_history"] = {"prior_launches": len(prev), "rugs": rugs, "source": hsrc}
        if len(rugs) >= G["creator_rugs_to_kill"]:
            raise Gate("creator_rugs", f"creator rugged {len(rugs)} of last {len(prev)} launches: {rugs[:5]}")

        self._chase_flush_gate(c, ev, ts)

        # 11: sell simulation (Jupiter buy -> 100% sell quote)
        try:
            solp = S.sol_price_usd()
            lam = int(trade_usd / solp * 1e9)
            q1 = S.jupiter_quote(SOL, c["address"], lam, self.cfg["costs"]["jupiter_slippage_bps"])
            q2 = S.jupiter_quote(c["address"], SOL, int(q1["outAmount"]), self.cfg["costs"]["jupiter_slippage_bps"])
        except Exception as e:
            raise Gate("sell_sim", f"sell simulation failed (possible honeypot / no route): {e}"[:160], True)
        ts["jupiter"] = now()
        rt = (1 - int(q2["outAmount"]) / lam) * 100
        ev["round_trip_loss_pct"] = round(rt, 2)
        if rt > G["max_round_trip_loss_pct"]:
            raise Gate("sell_sim", f"buy->sell round trip loses {rt:.1f}% > {G['max_round_trip_loss_pct']}% (tax/honeypot)")
        ev["fetched_at"] = ts
        c["rc"] = rc
        return ev

    def _chase_flush_gate(self, c, ev, ts):
        """Skip a 5m chase or a dump-then-bounce. Missing candles or 5m change fail closed."""
        entry = self.cfg["entry"]
        pair = (c.get("ds") or {}).get("pair")
        if not pair:
            raise Gate("chase_5m", "pair address missing for 5m path", True)
        candles, err = None, None
        try:
            candles = S.gecko_ohlcv_5m(pair)
            ts["ohlcv_5m"] = now()
        except Exception as e:
            err = str(e)[:160]
            ev["ohlcv_error"] = err
        verdict = chase_flush_verdict((c.get("ds") or {}).get("chg_m5"), candles, now(), entry)
        if verdict:
            gate, reason, unver = verdict
            if err and unver:
                reason = f"{reason} ({err})"[:200]
            raise Gate(gate, reason, unver, {"chg_m5": (c.get("ds") or {}).get("chg_m5")})
        ev["chg_m5"] = (c.get("ds") or {}).get("chg_m5")
        ev["chase_flush"] = "ok"


class Analyst:
    """Stage 3. 0-100 score over available components (weights renormalised); missing components listed.
    Below min coverage or below 70 = dies."""

    def __init__(self, cfg, log, use_smart_wallets=False):
        self.A, self.log, self.use_smart_wallets = cfg["analyst"], log, use_smart_wallets

    def run(self, c, gt):
        d, comp, missing = c["ds"], {}, []
        # momentum: 1h change (below the +80% gate), 5m change, 5m volume acceleration vs 1h average
        if None not in (d.get("chg_h1"), d.get("chg_m5"), d.get("vol_m5"), d.get("vol_h1")) and d["vol_h1"] > 0:
            accel = d["vol_m5"] * 12 / d["vol_h1"]
            comp["momentum"] = round(100 * (0.4 * clamp(d["chg_h1"] / 40) + 0.3 * clamp((d["chg_m5"] + 5) / 15)
                                            + 0.3 * clamp(accel / 2)), 1)
        else:
            missing.append("momentum")
        tx = ((gt or {}).get(d["pair"]) or {}).get("tx") or {}
        h1 = tx.get("h1") or {}
        if h1.get("buyers") is not None:
            comp["unique_buyers"] = round(100 * clamp(h1["buyers"] / 400), 1)
        else:
            missing.append("unique_buyers (GeckoTerminal unavailable)")
        if d.get("buys_h1") is not None and d.get("sells_h1") is not None:
            r = d["buys_h1"] / max(d["sells_h1"], 1)
            comp["buy_sell_ratio"] = round(100 * clamp((r - 0.7) / 0.8), 1)
        else:
            missing.append("buy_sell_ratio")
        hc = (c.get("rc") or {}).get("totalHolders")
        if hc:
            self.log.db.execute("INSERT INTO holder_snaps(ts,address,holders,source) VALUES(?,?,?,?)",
                                (now(), c["address"], int(hc), "rugcheck"))
            prev = self.log.db.execute("SELECT ts,holders FROM holder_snaps WHERE address=? AND ts<? AND ts>? "
                                       "ORDER BY ts LIMIT 1", (c["address"], now() - 300, now() - 6 * 3600)).fetchone()
            if prev and prev["holders"]:
                g = (hc / prev["holders"] - 1) * 100 / ((now() - prev["ts"]) / 3600)
                comp["holder_growth"] = round(100 * clamp(g / 20), 1)
            else:
                missing.append("holder_growth (no snapshot 5m-6h old yet)")
        else:
            missing.append("holder_growth (holder count unknown)")
        if d.get("liq_usd") and d.get("mcap"):
            comp["liq_mcap"] = round(100 * clamp((d["liq_usd"] / d["mcap"] - 0.05) / 0.15), 1)
        else:
            missing.append("liq_mcap")
        W = S.analyst_weights(self.A["weights"], self.use_smart_wallets)
        if self.use_smart_wallets:
            sw = ((c.get("gmgn_info") or {}).get("wallet_tags_stat") or {}).get("smart_wallets")
            if sw is not None:
                comp["smart_wallets"] = round(100 * clamp(f(sw, 0) / 5), 1)
            else:
                missing.append("smart_wallets (GMGN field missing)")
        # else: smart-wallet factor dropped; its weight is redistributed over the other five
        wsum = sum(W[k] for k in comp if k in W)
        cov = wsum / sum(W.values()) * 100 if W else 0.0
        score = round(sum(W[k] * v for k, v in comp.items() if k in W) / wsum, 1) if wsum else 0.0
        passed = cov >= self.A["min_weight_coverage_pct"] and score >= self.A["min_score"]
        s = {"score": score, "coverage_pct": round(cov, 1), "passed": passed, "components": comp, "missing": missing}
        self.log.score(c, s)
        return s


class Executor:
    """Stage 4. One entry per coin ever; size = min(8% equity, 2% pool liq) (x0.5 if creator has no history).
    Re-runs every gate just before the fill (data <10s old), re-quotes, kills on chase/impact. Never averages down."""

    def __init__(self, cfg, log, ledger, filt, risk):
        self.cfg, self.log, self.L, self.F, self.risk = cfg, log, ledger, filt, risk

    def size(self, c):
        B = self.cfg["bankroll"]
        eq = self.L.equity()
        usd = min(eq * B["max_trade_pct_of_equity"] / 100, c["ds"]["liq_usd"] * B["max_trade_pct_of_pool_liquidity"] / 100)
        if c.get("no_creator_history"):
            usd *= B["no_creator_history_size_multiplier"]
        return round(min(usd, self.L.cash), 2), eq

    def run(self, c, s):
        G, B, C = self.cfg["gates"], self.cfg["bankroll"], self.cfg["costs"]
        if self.log.db.execute("SELECT 1 FROM positions WHERE address=?", (c["address"],)).fetchone():
            self.log.kill(c, "executor", "one_entry_per_coin", "already traded this coin once (no re-entry)")
            return None
        usd, eq = self.size(c)
        if usd < B["min_trade_usd"]:
            self.log.kill(c, "executor", "size_too_small", f"size ${usd:.2f} < ${B['min_trade_usd']} (cash ${self.L.cash:.2f})")
            return None
        # fresh pre-fill pass: refetch DexScreener, rerun every gate on fresh data
        try:
            pairs, _ = S.ds_best_pairs([c["address"]], set(self.cfg["scout"]["allowed_dex_ids"]))
            c["ds"] = pairs[c["address"]]
        except Exception as e:
            self.log.kill(c, "executor", "stale_data", f"pre-fill DexScreener refresh failed: {e}"[:160], True); return None
        try:
            ev = self.F.check(c, fresh=True, trade_usd=usd)
        except Gate as g:
            self.log.kill(c, "executor(pre-fill recheck)", g.gate, g.reason, g.unverifiable, g.evidence); return None
        try:
            solp = S.sol_price_usd()
            lam = int((usd - C["network_fee_usd_per_tx"]) / solp * 1e9)
            q = S.jupiter_quote(SOL, c["address"], lam, C["jupiter_slippage_bps"])
        except Exception as e:
            self.log.kill(c, "executor", "requote", f"re-quote failed: {e}"[:160], True); return None
        t_fill = now()
        ages = {k: round(t_fill - v, 1) for k, v in ev["fetched_at"].items()}
        if max(ages.values()) > G["max_data_age_seconds_at_fill"]:
            self.log.kill(c, "executor", "stale_data", f"gate data too old at fill: {ages}", True); return None
        impact = (f(q.get("priceImpactPct"), None))
        if impact is None:
            self.log.kill(c, "executor", "requote", "price impact missing from quote", True); return None
        impact *= 100
        qty = int(q["outAmount"]) / 10 ** c["decimals"]
        spend_after_fee = (usd - C["network_fee_usd_per_tx"])
        fill = spend_after_fee / qty * (1 + C["extra_adverse_slippage_pct"] / 100)
        qty = spend_after_fee / fill
        chase = (fill / c["signal_price"] - 1) * 100
        if impact > G["max_price_impact_pct"]:
            self.log.kill(c, "executor", "requote_impact", f"price impact {impact:.2f}% > {G['max_price_impact_pct']}%"); return None
        if chase > G["max_chase_above_signal_pct"]:
            self.log.kill(c, "executor", "requote_chase", f"fill {chase:+.1f}% vs scout signal > {G['max_chase_above_signal_pct']}%"); return None
        mark = c["ds"]["price_usd"]
        worse = fill_vs_mark_pct(fill, mark)
        slip_cap = self.cfg["entry"]["max_fill_worse_than_mark_pct"]
        if worse is None:
            self.log.kill(c, "executor", "entry_slippage", "mark missing for the fill-vs-mark cap", True); return None
        if worse > slip_cap:
            self.log.kill(c, "executor", "entry_slippage",
                          f"fill {worse:.2f}% worse than mark (cap {slip_cap}%)"); return None
        try:
            q2 = S.jupiter_quote(c["address"], SOL, int(q["outAmount"]), C["jupiter_slippage_bps"])
            sell_usd = int(q2["outAmount"]) / 1e9 * solp * (1 - C["extra_adverse_slippage_pct"] / 100)
            k = min(max(1 - sell_usd / (qty * mark), 0.0), 0.5)
            rt_now = round_trip_loss_pct(lam, int(q2["outAmount"]))
        except Exception as e:
            self.log.kill(c, "executor", "requote", f"exit-cost quote failed: {e}"[:160], True); return None
        if rt_now is None or rt_now > G["max_round_trip_loss_pct"]:
            self.log.kill(c, "executor", "entry_round_trip",
                          f"pre-buy round trip {rt_now if rt_now is not None else 'unknown'}% > {G['max_round_trip_loss_pct']}%",
                          rt_now is None)
            return None
        stop = self.risk.initial_stop(qty, usd, k)  # stop set BEFORE the order is recorded
        rationale = (f"score {s['score']} (cov {s['coverage_pct']}%, comps {s['components']}); gates: liq ${ev['liq_usd']:,.0f}, "
                     f"1h vol ${ev['vol_h1']:,.0f}, 1h chg {ev['chg_h1']:+.0f}%, LP {ev['lp']}, top10 {ev['top10']['effective_pct']}%, "
                     f"dev {ev['dev_hold_pct']}%, bundled {ev.get('bundled_pct')}%, round-trip {ev['round_trip_loss_pct']}%, "
                     f"impact {impact:.2f}%, chase {chase:+.1f}%, size ${usd:.2f} of equity ${eq:.2f}"
                     + (" (half size: creator has no history)" if c.get("no_creator_history") else ""))
        cur = self.log.db.execute(
            "INSERT INTO positions(address,symbol,status,opened_at,score,size_usd,decimals,qty,entry_mark,entry_fill,entry_liq,"
            "stop_mark,initial_stop_mark,peak_mark,last_mark,last_check,half_size,url,entry_rationale,exit_cost_frac) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c["address"], c["symbol"], "open", t_fill, s["score"], usd, c["decimals"], qty, mark, fill, c["ds"]["liq_usd"],
             stop, stop, mark, mark, t_fill, int(bool(c.get("no_creator_history"))), c["ds"].get("url"), rationale, k))
        pid = cur.lastrowid
        self.L.add_cash(-usd)
        self.log.trade(pid, "BUY", "entry", mark, fill, qty, usd, usd - qty * mark, 0.0, "jupiter_quote", rationale)
        stop_pct = self.cfg["risk"]["hard_stop_net_pct"]
        self.log.db.execute("INSERT INTO stop_moves(position_id,ts,old_stop,new_stop,why) VALUES(?,?,?,?,?)",
                            (pid, t_fill, None, stop, f"initial {int(stop_pct)}% net stop set before entry"))
        self.log.db.commit()
        return pid


def hard_stop_reason(pct):
    """exit_reason for a new hard-stop fill. int(-8) -> 'stop_-8pct'.
    Closed trades already stored keep whatever reason they were written with."""
    return f"stop_{int(pct)}pct"


class Risk:
    """Stage 5. Hard stop from risk.hard_stop_net_pct (net of slippage, swap fee,
    price impact, and network fee) is set before entry and never lowered.
    A trail can ratchet it up after trail_activate_net_pct. A liquidity drop
    can also close the position. New hard-stop fills use hard_stop_reason(pct)."""

    def __init__(self, cfg, log, ledger):
        self.cfg, self.R, self.log, self.L = cfg, cfg["risk"], log, ledger

    def initial_stop(self, qty, cost, k):
        """Mark at which selling everything nets hard_stop_net_pct of cost after exit costs (k) and network fee.
        Used for new entries only. manage() does not recompute a stop already stored on a position."""
        target = cost * (1 + self.R["hard_stop_net_pct"] / 100)
        return (target + self.cfg["costs"]["network_fee_usd_per_tx"]) / (qty * (1 - k))

    def _exit_value(self, p, mark, liq):
        """Exit proceeds: real Jupiter sell quote if available, else cost model."""
        C = self.cfg["costs"]
        try:
            solp = S.sol_price_usd()
            q = S.jupiter_quote(p["address"], SOL, int(p["qty"] * 10 ** p["decimals"]), C["jupiter_slippage_bps"])
            net = int(q["outAmount"]) / 1e9 * solp * (1 - C["extra_adverse_slippage_pct"] / 100) - C["network_fee_usd_per_tx"]
            return max(net, 0.0), "jupiter_quote"
        except Exception:
            return max(self.L.pos_net(p, mark), 0.0), "entry-measured exit cost model (no Jupiter route)"

    def _close(self, p, mark, liq, reason, why):
        proceeds, src = self._exit_value(p, mark, liq if liq else p["entry_liq"])
        pnl = proceeds - p["size_usd"]
        pnl_pct = pnl / p["size_usd"] * 100 if p["size_usd"] else 0.0
        exit_fill = proceeds / p["qty"] if p["qty"] else 0
        self.log.db.execute("UPDATE positions SET status='closed', closed_at=?, exit_mark=?, exit_fill=?, proceeds=?, "
                            "pnl_usd=?, pnl_pct=?, exit_reason=? WHERE id=?",
                            (now(), mark, exit_fill, proceeds, pnl, pnl_pct, reason, p["id"]))
        self.L.add_cash(proceeds)
        self.log.trade(p["id"], "SELL", reason, mark, exit_fill, p["qty"], proceeds, None, pnl, src, why)
        self.log.db.commit()

    def manage(self):
        ops = self.L.open_positions()
        if not ops:
            return 0, {}
        quotes, errs = S.ds_best_pairs([p["address"] for p in ops], set(self.cfg["scout"]["allowed_dex_ids"]))
        n = 0
        floor = self.cfg["gates"]["min_liquidity_usd"]
        drop_pct = self.R["liq_drop_from_entry_pct"]
        for p in ops:
            q = quotes.get(p["address"])
            if not q or not q.get("price_usd"):
                self.log.event("stale_mark", position_id=p["id"], symbol=p["symbol"], note="no fresh price; stop still armed")
                continue
            mark = q["price_usd"]
            raw_liq = q.get("liq_usd")
            liq = raw_liq if raw_liq else p["entry_liq"]
            peak = max(p["peak_mark"] or mark, mark)
            net = (self.L.pos_net(p, mark) / p["size_usd"] - 1) * 100
            drop = liq_drop_reason(p["entry_liq"], raw_liq, drop_pct, floor)
            if drop:
                self.log.db.execute("UPDATE positions SET last_mark=?, peak_mark=?, last_check=?, last_net_pct=? WHERE id=?",
                                    (mark, peak, now(), net, p["id"]))
                self.log.db.commit()
                fresh = self.log.db.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
                self._close(fresh, mark, raw_liq, "liq_drop", drop)
                n += 1
                continue
            stop, trail = p["stop_mark"], p["trail_active"]
            if net >= self.R["trail_activate_net_pct"] and not trail:
                trail = 1
                self.log.event("trail_on", position_id=p["id"], symbol=p["symbol"], net_pct=round(net, 2))
            if trail:
                cand = peak * (1 - self.R["trail_distance_from_peak_pct"] / 100)
                if cand > stop:  # ratchet only upward; never lowered
                    self.log.db.execute("INSERT INTO stop_moves(position_id,ts,old_stop,new_stop,why) VALUES(?,?,?,?,?)",
                                        (p["id"], now(), stop, cand, f"trail {self.R['trail_distance_from_peak_pct']}% under peak {peak:.10g}"))
                    stop = cand
            self.log.db.execute("UPDATE positions SET last_mark=?, peak_mark=?, last_check=?, last_net_pct=?, stop_mark=?, "
                                "trail_active=? WHERE id=?", (mark, peak, now(), net, stop, trail, p["id"]))
            self.log.db.commit()
            if mark <= stop:
                fresh = self.log.db.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
                reason = "trailing_stop" if trail else hard_stop_reason(self.R["hard_stop_net_pct"])
                why = (f"mark {mark:.10g} <= stop {stop:.10g} ({reason}); peak {peak:.10g}"
                       + (" - gapped past stop between checks" if mark < stop * 0.97 else ""))
                self._close(fresh, mark, liq, reason, why)
                n += 1
        sync_entry_pause(self.log, self.L, self.cfg, quotes)
        return n, quotes


class Desk:
    def __init__(self, verbose=True):
        self.cfg = load_config()
        self.log = Logger()
        self.L = Ledger(self.log, self.cfg)
        src = self.cfg["sources"]
        self.gmgn = S.Gmgn(allow_demo=src["gmgn_allow_public_demo_key"]) if src["gmgn_enabled"] else None
        if self.gmgn and not self.gmgn.available:
            self.gmgn = None
        self.use_free = bool(src.get("use_free_sources_when_no_gmgn", True))
        self.verbose = verbose
        self.risk = Risk(self.cfg, self.log, self.L)
        self.filter = Filter(self.cfg, self.log, self.gmgn)

    def say(self, *a):
        if self.verbose:
            print(*a, flush=True)

    def manage_only(self):
        """Positions-only fast path. Re-marks open positions and applies the same stop and trail
        rules as a full scan. No scout, filter, or score. Flat books do not write a run."""
        ops = self.L.open_positions()
        if not ops:
            return {"exits": 0, "stop_moves": 0, "open": 0}
        t0 = now()
        before = self.log.db.execute("SELECT COUNT(*) FROM stop_moves").fetchone()[0]
        self.log.start_run("manage")
        n, quotes = self.risk.manage()
        self.log.equity(self.L.cash, self.L.open_value(quotes), len(self.L.open_positions()))
        self.log.end_run({}, {"exits": n}, t0)
        after = self.log.db.execute("SELECT COUNT(*) FROM stop_moves").fetchone()[0]
        return {"exits": n, "stop_moves": after - before, "open": len(ops)}

    def scan_once(self):
        t0 = now()
        self.log.start_run("scan")
        if self.gmgn:
            gates = "gmgn (free fallback on)" if self.use_free else "gmgn only"
            gmgn = f"personal key ({self.gmgn.key_mode})"
        else:
            gates = "free (RugCheck insiders + creatorTokens/pump.fun; smart-wallets reweighted)" if self.use_free \
                else "GMGN required (fail closed)"
            gmgn = "no personal key"
        status = {"gmgn": gmgn, "gates_source": gates}
        self.say(f"[{iso(t0)}] PAPER scan (cash ${self.L.cash:.2f})")
        exits, quotes = self.risk.manage()                                   # Risk first on open positions
        pause = sync_entry_pause(self.log, self.L, self.cfg, quotes)
        status["entry_pause"] = {"paused": pause["paused"], "reason": pause.get("reason")}
        cands, funnel = Scout(self.cfg, self.log).run(status)                # Stage 1
        kills = {}
        survivors = []
        traded = {r[0] for r in self.log.db.execute("SELECT address FROM positions")}
        est_size = self.L.equity() * self.cfg["bankroll"]["max_trade_pct_of_equity"] / 100
        for c in cands:                                                      # Stage 2
            if c["address"] in traded:
                funnel["skipped_already_traded"] = funnel.get("skipped_already_traded", 0) + 1
                continue
            try:
                ev = self.filter.check(c, trade_usd=max(est_size, 1.0))
                self.log.gate_pass(c, ev); survivors.append(c)
            except Gate as g:
                kills[g.gate] = kills.get(g.gate, 0) + 1
                self.log.kill(c, "filter", g.gate, g.reason, g.unverifiable, g.evidence or c.get("_ev"))
        self.log.db.commit()
        gt = {}
        if survivors:
            try:
                gt = S.gecko_pool_txns([c["ds"]["pair"] for c in survivors])
            except Exception as e:
                status["gecko_txns"] = f"error: {e}"[:150]
        scored = []
        for c in survivors:                                                  # Stage 3
            s = self.analyst_run(c, gt)
            if s["passed"]:
                scored.append((c, s))
            else:
                g = "analyst_below_70" if s["coverage_pct"] >= self.cfg["analyst"]["min_weight_coverage_pct"] else "analyst_low_coverage"
                kills[g] = kills.get(g, 0) + 1
                self.log.kill(c, "analyst", g, f"score {s['score']} (coverage {s['coverage_pct']}%, missing {s['missing']})")
        scored.sort(key=lambda cs: -cs[1]["score"])
        bought = []
        if pause["paused"]:
            self.say("  entries paused:", pause.get("reason"))
        else:
            ex = Executor(self.cfg, self.log, self.L, self.filter, self.risk)
            for c, s in scored:                                              # Stage 4
                pid = ex.run(c, s)
                if pid:
                    bought.append(c["symbol"])
        self.log.equity(self.L.cash, self.L.open_value(quotes), len(self.L.open_positions()))  # Stage 6 snapshot
        funnel.update({"filter_kills": kills, "filter_passed": len(survivors), "scored_70plus": len(scored),
                       "bought": bought, "exits": exits,
                       "entries_paused": pause.get("reason") if pause["paused"] else None})
        self.log.end_run(status, funnel, t0)
        self.say("  sources:", json.dumps(status))
        self.say("  funnel:", json.dumps(funnel))
        self.say(f"  cash ${self.L.cash:.2f} | equity ${self.L.equity():.2f} | {now() - t0:.0f}s")
        return funnel

    def analyst_run(self, c, gt):
        return Analyst(self.cfg, self.log, use_smart_wallets=bool(self.gmgn)).run(c, gt)
