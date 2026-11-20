"""Post-rejection outcome tracking shared by the Main and Dip desks. PAPER / read-only market data only.

For every coin a desk rejected we remember the price at the *first* kill, then keep refreshing the current
price (DexScreener, throttled) and the running peak/min. From that we can say which rules blocked runners
("missed") and which saved us from crashes ("dodged"). Pure functions take plain dicts so tests need no network.
"""
import json
import time

from . import sources as S

WINDOW_H = 48
REFRESH_S = 12 * 60          # throttle between network refreshes per desk
MAX_MINTS = 90
BATCH = 30
DEAD_LIQ = 1000.0            # below this a checked coin counts as dead
MIN_PICK_LIQ = 500.0         # ignore pairs with less liquidity than this when a better one exists
OUTLIER_X = 5000.0           # price this many x above the median candidate is treated as bogus
GOOD_QUOTES = {"SOL", "WSOL", "USDC", "USDT", "USD1", "PYUSD"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS rejected_outcomes(
  address TEXT, desk TEXT, symbol TEXT, pair TEXT, url TEXT,
  first_kill_ts REAL, last_kill_ts REAL, kill_gate TEXT, kill_reason TEXT,
  kill_price REAL, kill_mcap REAL, kill_liq REAL, kill_count INT DEFAULT 1,
  last_checked_ts REAL, current_price REAL, current_mcap REAL, current_liq REAL,
  peak_price REAL, peak_mcap REAL, peak_ts REAL, min_price REAL, min_mcap REAL,
  peak_multiple REAL, current_multiple REAL, dead INT DEFAULT 0,
  PRIMARY KEY(address, desk)
);
CREATE TABLE IF NOT EXISTS outcomes_state(desk TEXT PRIMARY KEY, last_refresh_ts REAL, error TEXT);
"""

COLS = ["address", "desk", "symbol", "pair", "url", "first_kill_ts", "last_kill_ts", "kill_gate", "kill_reason",
        "kill_price", "kill_mcap", "kill_liq", "kill_count", "last_checked_ts", "current_price", "current_mcap",
        "current_liq", "peak_price", "peak_mcap", "peak_ts", "min_price", "min_mcap", "peak_multiple",
        "current_multiple", "dead"]


def ensure_schema(db):
    db.executescript(SCHEMA)
    db.commit()


def _rows(db, q, *a):
    cur = db.execute(q, a)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def kill_bucket(reason):
    """Same grouping as dip.book._kill_bucket (duplicated to avoid an import cycle)."""
    r = (reason or "unknown").lower()
    for key, name in (("liq", "liq"), ("top 10", "top10"), ("copycat", "copycat"), ("warns", "warns"),
                      ("holders leaving", "holders leaving"), ("no data", "no data"), ("timed out", "timed out"),
                      ("crash", "crash"), ("no dip", "no dip"), ("ran before", "ran before the click"),
                      ("dev sold", "dev sold")):
        if r.startswith(key):
            return name
    if "in 1h" in r:
        return "h1 pump"
    return r.split(" ")[0]


# ---------------- seed (no network) ----------------
def _upsert(db, row):
    """Insert a new outcome row, or only bump last_kill_ts / kill_count / symbol on an existing one."""
    ex = db.execute("SELECT 1 FROM rejected_outcomes WHERE address=? AND desk=?", (row["address"], row["desk"])).fetchone()
    if ex:
        db.execute("UPDATE rejected_outcomes SET last_kill_ts=MAX(COALESCE(last_kill_ts,0),?), kill_count=?, "
                   "symbol=COALESCE(?,symbol), pair=COALESCE(pair,?), url=COALESCE(url,?) WHERE address=? AND desk=?",
                   (row["last_kill_ts"] or 0, row["kill_count"], row.get("symbol"), row.get("pair"), row.get("url"),
                    row["address"], row["desk"]))
        return 0
    row = {c: row.get(c) for c in COLS}
    db.execute(f"INSERT INTO rejected_outcomes({','.join(COLS)}) VALUES({','.join('?' * len(COLS))})",
               [row[c] for c in COLS])
    return 1


def seed_main(db, now=None, window_h=None):
    """One row per killed address from `kills`, using the earliest kill (preferring one that has a price)."""
    now = now or time.time()
    ensure_schema(db)
    since = now - window_h * 3600 if window_h else 0
    per = {}
    for k in _rows(db, "SELECT * FROM kills WHERE ts>=? AND address IS NOT NULL ORDER BY ts, id", since):
        per.setdefault(k["address"], []).append(k)
    sc = {r["address"]: r for r in _rows(db, "SELECT address, symbol, pair, url FROM scouted GROUP BY address")}
    n = 0
    for a, ks in per.items():
        first = next((k for k in ks if (k["price"] or 0) > 0), ks[0])
        s = sc.get(a) or {}
        n += _upsert(db, {
            "address": a, "desk": "main", "symbol": ks[-1]["symbol"] or s.get("symbol"), "pair": s.get("pair"),
            "url": s.get("url") or f"https://dexscreener.com/solana/{s.get('pair') or a}",
            "first_kill_ts": first["ts"], "last_kill_ts": ks[-1]["ts"], "kill_gate": first["gate"],
            "kill_reason": first["reason"], "kill_price": first["price"], "kill_mcap": first["mcap"],
            "kill_liq": first["liq"], "kill_count": len(ks)})
    db.commit()
    return n


def seed_dip(db, now=None, window_h=None):
    """One row per killed/dropped room mint. Kill price/mcap/liq come from the earliest `prices` row."""
    now = now or time.time()
    ensure_schema(db)
    n = 0
    for r in _rows(db, "SELECT * FROM room WHERE status IN ('killed','dropped')"):
        ts = r.get("updated_at") or r.get("migrated_at") or 0
        if window_h and ts < now - window_h * 3600:
            continue
        p = db.execute("SELECT ts, price, mcap, liq FROM prices WHERE mint=? AND price>0 ORDER BY ts LIMIT 1",
                       (r["mint"],)).fetchone()
        first_ts = p[0] if p else (r.get("migrated_at") or ts)
        pool = r.get("pool")
        n += _upsert(db, {
            "address": r["mint"], "desk": "dip", "symbol": r.get("symbol") or r.get("name"), "pair": pool,
            "url": f"https://dexscreener.com/solana/{pool or r['mint']}",
            "first_kill_ts": first_ts, "last_kill_ts": ts, "kill_gate": kill_bucket(r.get("reason")),
            "kill_reason": r.get("reason"), "kill_price": p[1] if p else None, "kill_mcap": p[2] if p else None,
            "kill_liq": p[3] if p else None, "kill_count": 1})
    db.commit()
    return n


# ---------------- quote selection ----------------
def pick_outcome_pair(pairs, mint):
    """Best current quote for `mint` from raw DexScreener pairs, or None.

    Only pairs where baseToken is the mint count (a pair where it is the quote token gives bogus prices).
    Prefer real quotes (SOL/USDC/...), drop dust-liquidity and extreme-outlier prices, take the deepest pool.
    """
    cands = []
    for p in pairs or []:
        if (p.get("baseToken") or {}).get("address") != mint or p.get("chainId", "solana") != "solana":
            continue
        row = S._ds_pair_row(p)
        if not row["price_usd"] or row["price_usd"] <= 0:
            continue
        row["liq_usd"] = row["liq_usd"] or 0.0
        cands.append(row)
    if not cands:
        return None
    real = [c for c in cands if (c.get("quote") or "").upper() in GOOD_QUOTES]
    cands = real or cands
    deep = [c for c in cands if c["liq_usd"] >= MIN_PICK_LIQ]
    cands = deep or cands
    if len(cands) >= 3:
        prices = sorted(c["price_usd"] for c in cands)
        med = prices[len(prices) // 2]
        sane = [c for c in cands if med / OUTLIER_X <= c["price_usd"] <= med * OUTLIER_X]
        cands = sane or cands
    return max(cands, key=lambda c: c["liq_usd"])


def fetch_quotes(mints, fetch=None, sleep=time.sleep):
    """{mint: quote_row | None} for each requested mint, batched 30/request. Returns (quotes, error).

    A mint the API answered for but had no usable pair maps to None (gone). If a batch errors, its mints are
    absent from the result so existing rows are kept. A 429 aborts the remaining batches.
    """
    fetch = fetch or S.http_json
    quotes, err = {}, None
    mints = list(dict.fromkeys(m for m in mints if m))
    for i in range(0, len(mints), BATCH):
        chunk = mints[i:i + BATCH]
        try:
            pairs = fetch("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(chunk)) or []
        except Exception as e:
            err = str(e)[:150]
            if "429" in err:
                err = "rate_limited: " + err
                break
            continue
        for m in chunk:
            quotes[m] = pick_outcome_pair(pairs, m)
        sleep(0.25)
    return quotes, err


# ---------------- apply quotes ----------------
def apply_quotes(rows, quotes, now=None):
    """Pure: new row dicts with current/peak/min/multiples updated from `quotes` ({mint: quote|None})."""
    now = now or time.time()
    out = []
    for r in rows:
        if r["address"] not in quotes:
            out.append(r)
            continue
        r = dict(r)
        q = quotes[r["address"]]
        r["last_checked_ts"] = now
        if q is None or (q.get("liq_usd") or 0) < DEAD_LIQ:
            r["dead"] = 1
        else:
            r["dead"] = 0
        if q is None:
            r["current_price"], r["current_mcap"], r["current_liq"] = 0.0, None, None
        else:
            r["current_price"], r["current_mcap"], r["current_liq"] = q["price_usd"], q.get("mcap"), q.get("liq_usd")
            r["pair"] = q.get("pair") or r.get("pair")
            r["url"] = q.get("url") or r.get("url")
            r["symbol"] = r.get("symbol") or q.get("symbol")
            cp = q["price_usd"]
            if r.get("peak_price") is None and (r.get("kill_price") or 0) > 0:
                # price at the kill was itself observed, so the peak since the kill can't be lower
                r["peak_price"], r["peak_mcap"], r["peak_ts"] = r["kill_price"], r.get("kill_mcap"), r.get("first_kill_ts")
            if not r["dead"]:       # a dust-liquidity quote is not a trustworthy peak
                if r.get("peak_price") is None or cp > r["peak_price"]:
                    r["peak_price"], r["peak_mcap"], r["peak_ts"] = cp, q.get("mcap"), now
                if r.get("min_price") is None or cp < r["min_price"]:
                    r["min_price"], r["min_mcap"] = cp, q.get("mcap")
        kp = r.get("kill_price")
        if kp and kp > 0:
            r["peak_multiple"] = (r["peak_price"] / kp) if r.get("peak_price") else None
            r["current_multiple"] = (r["current_price"] or 0) / kp
        out.append(r)
    return out


def _save(db, rows):
    for r in rows:
        db.execute("UPDATE rejected_outcomes SET symbol=?, pair=?, url=?, last_checked_ts=?, current_price=?, current_mcap=?, "
                   "current_liq=?, peak_price=?, peak_mcap=?, peak_ts=?, min_price=?, min_mcap=?, peak_multiple=?, "
                   "current_multiple=?, dead=? WHERE address=? AND desk=?",
                   (r["symbol"], r["pair"], r["url"], r["last_checked_ts"], r["current_price"], r["current_mcap"],
                    r["current_liq"], r["peak_price"], r["peak_mcap"], r["peak_ts"], r["min_price"], r["min_mcap"],
                    r["peak_multiple"], r["current_multiple"], r["dead"], r["address"], r["desk"]))
    db.commit()


def _priority(r, now):
    """Meaningful kills (mcap >= $10k or liq >= $5k) before dust; within each, least recently checked first
    (never-checked rows lead), so a 90-mint cap still rotates through every tracked coin."""
    meaningful = (r["kill_mcap"] or 0) >= 10_000 or (r["kill_liq"] or 0) >= 5_000
    return (not meaningful, r["last_checked_ts"] or 0, -(r["last_kill_ts"] or 0))


def refresh(db, desk, now=None, fetch=None, sleep=time.sleep, force=False, interval=REFRESH_S):
    """Throttled network refresh. Never raises. Returns {'refreshed': n, 'error': str|None, 'skipped': bool}."""
    now = now or time.time()
    try:
        ensure_schema(db)
        st = db.execute("SELECT last_refresh_ts FROM outcomes_state WHERE desk=?", (desk,)).fetchone()
        if st and st[0] and now - st[0] < interval and not force:
            return {"refreshed": 0, "error": None, "skipped": True}
        rows = _rows(db, "SELECT * FROM rejected_outcomes WHERE desk=? AND last_kill_ts>=?", desk, now - WINDOW_H * 3600)
        rows.sort(key=lambda r: _priority(r, now))
        rows = rows[:MAX_MINTS]
        quotes, err = fetch_quotes([r["address"] for r in rows], fetch=fetch, sleep=sleep)
        _save(db, apply_quotes(rows, quotes, now))
        db.execute("INSERT OR REPLACE INTO outcomes_state VALUES(?,?,?)", (desk, now, err))
        db.commit()
        return {"refreshed": len(quotes), "error": err, "skipped": False}
    except Exception as e:  # never break report generation
        return {"refreshed": 0, "error": str(e)[:150], "skipped": False}


# ---------------- scorecard + export ----------------
def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def _tracked(r):
    return bool((r.get("kill_price") or 0) > 0 and r.get("last_checked_ts"))


def _crashed(r):
    if not _tracked(r):
        return False
    cm = r.get("current_multiple")
    return bool(r.get("dead")) or (cm is not None and cm <= 0.5)


def gate_scorecard(rows):
    by = {}
    for r in rows:
        by.setdefault(r.get("kill_gate") or "unknown", []).append(r)
    out = []
    for gate, rs in by.items():
        tr = [r for r in rs if _tracked(r)]
        pm = [r["peak_multiple"] for r in tr if r.get("peak_multiple") is not None]
        best = max((r for r in tr if r.get("peak_multiple")), key=lambda r: r["peak_multiple"], default=None)
        out.append({
            "gate": gate, "desk": rs[0].get("desk"), "unique_coins_killed": len(rs), "tracked_coins": len(tr),
            "runners_2x": sum(1 for x in pm if x >= 2), "runners_5x": sum(1 for x in pm if x >= 5),
            "crashed_50pct": sum(1 for r in tr if _crashed(r)),
            "median_peak_x": _median(pm), "median_current_x": _median([r.get("current_multiple") for r in tr]),
            "best_runner": {"symbol": best["symbol"], "peak_x": best["peak_multiple"], "kill_mcap": best["kill_mcap"],
                            "peak_mcap": best["peak_mcap"], "url": best["url"], "address": best["address"]} if best else None})
    out.sort(key=lambda g: (-g["runners_5x"], -g["runners_2x"], -g["unique_coins_killed"]))
    return out


def _view(r):
    keep = ("address", "desk", "symbol", "url", "kill_gate", "kill_reason", "kill_price", "kill_mcap", "kill_liq",
            "first_kill_ts", "last_kill_ts", "kill_count", "peak_price", "peak_mcap", "peak_ts", "peak_multiple",
            "current_price", "current_mcap", "current_liq", "current_multiple", "min_price", "last_checked_ts", "dead")
    return {k: r.get(k) for k in keep}


def export(db, desk, now=None, refresh_result=None):
    """Dashboard JSON fragment for one desk. Read-only."""
    now = now or time.time()
    ensure_schema(db)
    rows = _rows(db, "SELECT * FROM rejected_outcomes WHERE desk=?", desk)
    tr = [r for r in rows if _tracked(r)]
    runners = sorted((r for r in tr if (r.get("peak_multiple") or 0) >= 2), key=lambda r: -r["peak_multiple"])[:40]
    dodged = sorted((r for r in tr if _crashed(r)), key=lambda r: (r.get("current_multiple") is not None,
                                                                  r.get("current_multiple") or 0))[:40]
    st = db.execute("SELECT last_refresh_ts, error FROM outcomes_state WHERE desk=?", (desk,)).fetchone()
    err = (refresh_result or {}).get("error") or (st[1] if st else None)
    return {
        "missed_runners": [_view(r) for r in runners],
        "dodged_rugs": [_view(r) for r in dodged],
        "gate_scorecard": gate_scorecard(rows),
        "outcome_index": {r["address"]: {"p": round(r["peak_multiple"], 3) if r.get("peak_multiple") is not None else None,
                                         "c": round(r["current_multiple"], 3) if r.get("current_multiple") is not None else None}
                          for r in sorted(tr, key=lambda r: -(r.get("last_kill_ts") or 0))[:300]},
        "outcomes_meta": {"tracked": len(tr), "seeded": len(rows), "last_refresh_ts": st[0] if st else None,
                          "window_hours": WINDOW_H, "error": err},
    }


def update_and_export(db, desk, seed, now=None, **kw):
    """Seed (no network) + throttled refresh + export. Never raises: a failure yields an empty, flagged fragment."""
    try:
        seed(db, now=now, window_h=None)
        res = refresh(db, desk, now=now, **kw)
        return export(db, desk, now=now, refresh_result=res)
    except Exception as e:
        return {"missed_runners": [], "dodged_rugs": [], "gate_scorecard": [], "outcome_index": {},
                "outcomes_meta": {"tracked": 0, "last_refresh_ts": None, "window_hours": WINDOW_H, "error": str(e)[:150]}}


def health_from_sources(sources, extra_errors=()):
    """{'status','errors','sources'} from a name->status-string dict."""
    errors, limited = [], False
    for k, v in (sources or {}).items():
        s = str(v)
        low = s.lower()
        if "429" in low or "rate_limited" in low:
            limited = True
            errors.append(f"{k}: {s}"[:160])
        elif low.startswith("error") or "error:" in low:
            errors.append(f"{k}: {s}"[:160])
    for e in extra_errors:
        if e:
            errors.append(str(e)[:160])
            if "429" in str(e) or "rate_limited" in str(e):
                limited = True
    status = "rate_limited" if limited else ("errors" if errors else "ok")
    return {"status": status, "errors": errors[:12], "sources": sources or {}}
