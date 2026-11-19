"""Scanner (bot 1): waiting room, polling, kills, pass rules. PAPER ONLY.

Decision logic (`decide`, `holders_growth`, `is_copycat`, `blocking_warns`, `top10_share`) is pure.
`Scanner` is the thin IO shell around it; sources are injected.
"""
import re
import time

from .sources import KNOWN_PROGRAM_OWNERS, total_holders

BLOCKING_EXT = ("nontransferable", "pausable", "transferhook", "permanentdelegate", "defaultaccountstate")


def fmt_k(v):
    v = float(v or 0)
    return f"${v / 1000:.1f}K" if v >= 1000 else f"${v:.0f}"


# ---------------- pure helpers ----------------
def normalize(s):
    """lowercase, drop spaces, digits, emoji and any non-alphanumeric character."""
    return re.sub(r"[^a-z]", "", (s or "").lower())


def _inside(a, b):
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def is_copycat(mint, name, symbol, runners):
    """Exact or contained match of normalized name or symbol against today's runners (self excluded).
    runners: iterable of {mint, name, symbol}. Returns the runner label or None."""
    n, s = normalize(name), normalize(symbol)
    for r in runners or []:
        if r.get("mint") == mint:
            continue
        rn, rs = normalize(r.get("name")), normalize(r.get("symbol"))
        if (n and (_inside(n, rn) or _inside(n, rs))) or (s and (_inside(s, rs) or _inside(s, rn))):
            return r.get("symbol") or r.get("name") or r.get("mint")
    return None


def _ext_names(report):
    ext = (report or {}).get("token_extensions")
    if ext is None:
        ext = (report or {}).get("tokenExtensions")
    names = []
    if isinstance(ext, dict):
        names = [str(k) for k in ext]
    elif isinstance(ext, list):
        for e in ext:
            if not isinstance(e, dict):
                names.append(str(e))
                continue
            n = str(e.get("extension") or e.get("type") or e.get("name") or "")
            if re.sub(r"[^a-z]", "", n.lower()) == "defaultaccountstate":
                st = e.get("state")
                st = st.get("accountState") if isinstance(st, dict) else st
                if str(st).lower() != "frozen":
                    continue  # only a frozen default state blocks selling
            names.append(n)
    return names


def blocking_warns(report):
    """RugCheck danger risks / Token-2022 extensions that can block selling -> text or None."""
    out = []
    for r in (report or {}).get("risks") or []:
        if isinstance(r, dict) and str(r.get("level", "")).lower() == "danger":
            out.append(str(r.get("name") or r.get("description") or "danger"))
    for n in _ext_names(report):
        key = re.sub(r"[^a-z]", "", n.lower())
        if any(b in key for b in BLOCKING_EXT):
            out.append(n)
    return ", ".join(dict.fromkeys(out)) or None


def top10_share(holders, supply, exclude):
    """% of supply held by the 10 biggest holders after dropping excluded owners/addresses.
    holders: [{address, owner, amount}]. None if supply unknown."""
    if not supply:
        return None
    ex = set(x for x in exclude if x)
    rows = [h for h in holders if h.get("owner") not in ex and h.get("address") not in ex]
    top = sorted((h.get("amount") or 0 for h in rows), reverse=True)[:10]
    return round(sum(top) / supply * 100, 2)


def holders_growth_info(samples, now, min_age_min=8):
    """(growth %, minutes since the base sample) or (None, None). Base = newest sample at least
    min_age_min old (younger samples only -> wait). samples: [(ts, holders)] oldest first, last = now."""
    if not samples or samples[-1][1] is None:
        return None, None
    old = [(ts, h) for ts, h in samples if now - ts >= min_age_min * 60 and h]
    if not old:
        return None, None
    ts, base = old[-1]
    return (samples[-1][1] / base - 1) * 100, round((now - ts) / 60)


def holders_growth(samples, now, min_age_min=8):
    return holders_growth_info(samples, now, min_age_min)[0]


H1_WINDOW_S = 3600


def measured_h1(prices, now, min_liq=0):
    """% change of the PumpSwap price over the last hour from our own stored readings, or None.
    prices: [(ts, price, liq)] oldest first. The baseline is the first stored reading whose pool holds at
    least min_liq, never DexScreener's own first tick of a brand-new pair (dust-priced, which makes
    priceChange.h1 read +100000% on young pools) and never a stub read of a pool that is not indexed yet."""
    pts = [(t, p) for t, p, q in prices or [] if p and p > 0 and (q or 0) >= min_liq and now - t <= H1_WINDOW_S]
    if len(pts) < 2:
        return None
    return (pts[-1][1] / pts[0][1] - 1) * 100


def decide(coin, row, report, holders_now, samples, top10, runners, cfg, now, prices=None):
    """One poll for one waiting coin. Returns ("kill", reason) | ("drop", reason) | ("pass", None) | ("wait", why).

    coin: room row dict; row: DexScreener row or None; report: RugCheck report or None;
    holders_now: int|None; samples: [(ts, holders)] incl. fresh pass-time sample; top10: % or None."""
    s = cfg["scanner"]
    age_room = (now - (coin.get("migrated_at") or now)) / 60
    if row is None:
        # DexScreener doesn't know it: never pass, never kill on missing data; time it out.
        if age_room >= s["max_wait_min"]:
            return "drop", "timed out"
        return "wait", "no dexscreener pair"
    created = (row.get("pair_created_ms") or 0) / 1000 or coin.get("migrated_at") or now
    pool_age = (now - created) / 60
    young = pool_age < s["min_pool_age_min"]
    # A pool younger than min_pool_age_min is only judged on a reading we have seen twice: the very first
    # DexScreener read of a new pair can be a stub (pool not indexed yet, dust liquidity, dust first tick).
    # prices=None (pure callers) means "no history available": old pools are judged on the row alone.
    n_reads = None if prices is None else sum(1 for t, p, q in prices if now - t <= H1_WINDOW_S)
    seen_twice = not young or (n_reads or 0) >= 2
    liq = row.get("liq_usd")
    if liq is None:
        # no liquidity figure = DexScreener has no real PumpSwap pool data yet: never pass, never kill
        if age_room >= s["max_wait_min"]:
            return "drop", "timed out"
        return "wait", "no pumpswap liquidity data"
    if liq < s["min_liquidity_usd"]:
        if seen_twice:
            return "kill", f"liq {fmt_k(liq)}"
        return "wait", "first read, confirming liquidity"
    if top10 is not None and top10 > s["max_top10_pct"]:
        return "kill", f"top 10 hold {top10:.0f}%"
    h1 = measured_h1(prices, now, s["min_liquidity_usd"]) if prices is not None and pool_age < H1_WINDOW_S / 60 else None
    if h1 is None and (not young or pool_age >= H1_WINDOW_S / 60):
        # no usable history of our own, but the pool is old enough (>= min_pool_age_min) that DexScreener's
        # h1 is a real PumpSwap reading; a full-hour-old pair always uses it.
        h1 = row.get("chg_h1")
    if h1 is not None and h1 > s["max_h1_change_pct"]:
        return "kill", f"+{h1:.0f}% in 1h"
    cc = is_copycat(coin["mint"], coin.get("name") or row.get("name"), coin.get("symbol") or row.get("symbol"), runners)
    if cc:
        return "kill", f"copycat of {cc}"
    warn = blocking_warns(report)
    if warn:
        return "kill", f"warns: {warn}"
    if not young:
        if holders_now is None:
            return "kill", "no data: holders"
        g, g_min = holders_growth_info(samples, now, s["holders_growth_min_age_min"])
        if g is None:
            if age_room >= s["max_wait_min"]:
                return "drop", "timed out"
            return "wait", "holders sample too young"
        if g < s["min_holders_growth_pct"]:
            return "kill", f"holders leaving {g:.0f}% in {g_min}m"
        if top10 is None:
            if age_room >= s["max_wait_min"]:
                return "drop", "timed out"
            return "wait", "top10 unknown"
        return "pass", None
    if age_room >= s["max_wait_min"]:
        return "drop", "timed out"
    return "wait", "pool too young"


def curve_minutes(migrated_at, new_seen_at=None, detected_at=None, pf_created=None):
    """Minutes on the curve: first available start among message seen, RugCheck detectedAt, pump.fun created."""
    for start in (new_seen_at, detected_at, pf_created):
        if start:
            return round((migrated_at - start) / 60, 2)
    return None


# ---------------- IO shell ----------------
class Scanner:
    def __init__(self, book, ds, rugcheck, rpc, cfg, clock=time.time, day_of=None, pumpfun=None):
        self.book, self.ds, self.rc, self.rpc, self.cfg, self.clock = book, ds, rugcheck, rpc, cfg, clock
        self.day_of = day_of or (lambda t: time.strftime("%Y-%m-%d", time.gmtime(t)))
        self.pumpfun = pumpfun
        self.new_seen = {}  # mint -> (seen_at, creator)
        self.report_cache = {}  # mint -> (ts, report)
        self.boost_runners, self.boost_at = [], 0.0
        self.rugs_at = 0.0

    def on_event(self, ev):
        """Feed a parsed PumpPortal event."""
        if ev["kind"] == "new":
            self.new_seen[ev["mint"]] = (ev["seen_at"], ev.get("creator"), ev.get("name"), ev.get("symbol"), ev.get("curve"))
            return
        seen = self.new_seen.get(ev["mint"])
        migrated = ev["seen_at"]
        # creator: the earlier create message, else RugCheck (a migrate message's trader is the tx signer)
        creator = seen[1] if seen else None
        curve = seen[4] if seen else None
        curve_m = curve_minutes(migrated, seen[0]) if seen else None
        bal = self.rpc.owner_balance(creator, ev["mint"]) if creator else None
        rep = None
        if bal is None or curve_m is None or not creator:
            rep = self.rc.report(ev["mint"])
            if rep:
                creator = creator or rep.get("creator")
                if bal is None:
                    bal = rep.get("creatorBalance") if isinstance(rep.get("creatorBalance"), (int, float)) else None
                if curve_m is None and rep.get("detectedAt"):
                    curve_m = _detected_min(rep["detectedAt"], migrated)
        if curve_m is None and self.pumpfun:
            try:
                pf = self.pumpfun(ev["mint"])
                ts = (pf or {}).get("created_timestamp")
                if ts:
                    curve_m = curve_minutes(migrated, ts / 1000 if ts > 1e11 else ts)
            except Exception:
                pass
        self.book.room_add(ev["mint"], pool=ev.get("pool"), creator=creator,
                           name=ev.get("name") or (seen[2] if seen else None),
                           symbol=ev.get("symbol") or (seen[3] if seen else None),
                           migrated_at=migrated, new_seen_at=seen[0] if seen else None,
                           creator_balance=bal, curve_min=curve_m, curve=curve)

    def refresh_rugs(self):
        now = self.clock()
        if now - self.rugs_at < self.cfg["scanner"]["rugs_refresh_min"] * 60:
            return
        self.rugs_at = now
        rows = self.rc.rugs_ticker(50)
        if rows:
            self.book.rugs_replace(rows, now)

    def _runners(self, now):
        s = self.cfg["scanner"]
        if now - self.boost_at > 600:
            self.boost_runners, self.boost_at = self.ds.top_boosts_runners(s["runner_h24_pct"]), now
        return self.book.runner_rows(s["runner_h24_pct"]) + self.boost_runners

    def _report_for(self, mint, now, force=False):
        """RugCheck report, cached holders_sample_min; each fetch also stores a holders sample.
        force=True takes a fresh sample (used at pass time)."""
        ttl = self.cfg["scanner"]["holders_sample_min"] * 60
        hit = self.report_cache.get(mint)
        if hit and not force and now - hit[0] < ttl:
            return hit[1]
        rep = self.rc.report(mint)
        if rep is None:
            return hit[1] if hit else None
        self.report_cache[mint] = (now, rep)
        n = total_holders(rep)
        if n is not None:
            self.book.add_holders(mint, n, now)
        return rep

    def tick(self):
        """One poll: price read for the room, then a decision per coin. Returns [(mint, action, reason)]."""
        now = self.clock()
        s = self.cfg["scanner"]
        self.refresh_rugs()
        room = self.book.room_waiting()
        if not room:
            return []
        rows, _errs = self.ds.batch([c["mint"] for c in room])  # RateLimited propagates: caller waits, room kept
        runners = self._runners(now)
        out = []
        for c in room:
            row = rows.get(c["mint"])
            if row:
                self.book.add_price(c["mint"], row, now)
                upd = {}
                if row.get("pair") and not c.get("pool"):
                    upd["pool"] = row["pair"]  # DexScreener pairAddress of the chosen pumpswap pair
                if c.get("liq_scanner") is None and (row.get("liq_usd") or 0) >= s["min_liquidity_usd"]:
                    upd["liq_scanner"] = row.get("liq_usd")  # first real reading, not a stub first read
                if upd:
                    self.book.room_update(c["mint"], **upd)
                c = {**c, **upd}
            report = self._report_for(c["mint"], now) if row else None
            top10 = None
            if row:
                hs = self.rpc.largest_accounts(c["mint"])
                supply = self.rpc.token_supply(c["mint"])
                if hs is not None:
                    for h in hs:
                        h["owner"] = self.rpc.token_account_owner(h["address"])
                    top10 = top10_share(hs, supply, KNOWN_PROGRAM_OWNERS | {c.get("pool"), c.get("curve")})
            pass_time = row and ((now - ((row.get("pair_created_ms") or 0) / 1000 or c.get("migrated_at") or now)) / 60
                                 >= s["min_pool_age_min"])
            if pass_time:
                report = self._report_for(c["mint"], now, force=True)
            samples = self.book.holders_samples(c["mint"])
            holders_now = samples[-1][1] if samples else total_holders(report)
            prices = self.book.price_history(c["mint"], now - H1_WINDOW_S) if row else None
            act, why = decide(c, row, report, holders_now, samples, top10, runners, self.cfg, now, prices)
            day = self.day_of(now)
            if act == "pass" and self.book.passed_today(c["mint"], day):
                act, why = "drop", "already passed today"
            if act == "kill":
                self.book.room_close(c["mint"], "killed", why)
            elif act == "drop":
                self.book.room_close(c["mint"], "dropped", why)
            elif act == "pass":
                self.book.room_update(c["mint"], status="passed", passed_day=day)
            if act != "wait":
                self.book.event("scanner_" + act, c["mint"], why)
                out.append((c["mint"], act, why))
        return out


def _detected_min(detected_at, migrated):
    """RugCheck detectedAt is an ISO string; minutes from it to migration, or None."""
    try:
        from datetime import datetime
        ts = datetime.fromisoformat(str(detected_at).replace("Z", "+00:00")).timestamp()
        return round((migrated - ts) / 60, 2)
    except Exception:
        return None
