"""Offline tests: missed-runner tracker, gate scorecard, GeckoTerminal 429 limiter. No network, no production DB."""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
TMP = Path(tempfile.mkdtemp())
os.environ["PAPER_DESK_DB"] = str(TMP / "desk.db")
os.environ["PAPER_DIP_DB"] = str(TMP / "dip.db")

from desk import outcomes as O
from desk import sources as S


def ok(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("ok:", msg)


FIONA = "B34nXKzCgsJb9ChLuSHPRpXLE3WFSWyeT8Cqkzzmpump"
DUST = "D" * 44
NOW = 1_000_000.0


def rows(db, desk):
    cur = db.execute("SELECT * FROM rejected_outcomes WHERE desk=?", (desk,))
    names = [d[0] for d in cur.description]
    return {r[0]: dict(zip(names, r)) for r in cur.fetchall()}


# ---- older-schema DB migrates with CREATE TABLE IF NOT EXISTS ----
old = sqlite3.connect(":memory:")
old.executescript("""CREATE TABLE kills(id INTEGER PRIMARY KEY, run_id INT, ts REAL, stage TEXT, address TEXT, symbol TEXT,
  gate TEXT, reason TEXT, unverifiable INT, price REAL, liq REAL, mcap REAL, evidence TEXT);
  CREATE TABLE scouted(id INTEGER PRIMARY KEY, run_id INT, ts REAL, address TEXT, symbol TEXT, pair TEXT, dex TEXT,
  age_min REAL, mcap REAL, liq REAL, price REAL, sources TEXT, url TEXT);""")
O.ensure_schema(old)
ok(old.execute("SELECT COUNT(*) FROM rejected_outcomes").fetchone()[0] == 0, "ensure_schema adds table to an old DB")
# real Logger / DipBook open on a DB that only has old tables
from desk.engine import Logger
import desk.engine as E
pre = sqlite3.connect(E.DB)
pre.execute("CREATE TABLE kills(id INTEGER PRIMARY KEY, ts REAL)")
pre.commit(); pre.close()
lg = Logger()
ok(lg.db.execute("SELECT COUNT(*) FROM rejected_outcomes").fetchone()[0] == 0, "Logger creates rejected_outcomes on an old DB")
from dip.book import DipBook
book = DipBook(path=str(TMP / "dip.db"))
ok(book.db.execute("SELECT COUNT(*) FROM rejected_outcomes").fetchone()[0] == 0, "DipBook creates rejected_outcomes")

# ---- main seed ----
db = sqlite3.connect(":memory:")
db.executescript(E.SCHEMA)
O.ensure_schema(db)
K = "INSERT INTO kills(ts,address,symbol,gate,reason,price,liq,mcap) VALUES(?,?,?,?,?,?,?,?)"
db.execute(K, (NOW - 100, FIONA, "FIONA", "pumped_1h", "already +378% in 1h (>= +80%)", 0.0001515, 90000, 151355))
db.execute(K, (NOW - 50, FIONA, "FIONA", "top10_real", "top10 too high", 0.0004, 99000, 400000))
db.execute(K, (NOW - 40, DUST, "DUST", "liquidity", "liq $3", None, 3, 100))
db.execute(K, (NOW - 30, DUST, "DUST", "liquidity", "liq $3", 0.00001, 3, 120))
db.execute("INSERT INTO scouted(ts,address,symbol,pair,url) VALUES(?,?,?,?,?)", (NOW - 100, FIONA, "FIONA", "PAIRF", "https://dexscreener.com/solana/PAIRF"))
db.commit()
ok(O.seed_main(db, now=NOW) == 2, "main seed creates one row per address")
r = rows(db, "main")
f = r[FIONA]
ok(f["kill_gate"] == "pumped_1h" and f["kill_price"] == 0.0001515 and f["kill_mcap"] == 151355, "FIONA uses earliest kill (gate/price/mcap)")
ok(f["kill_count"] == 2 and f["last_kill_ts"] == NOW - 50 and f["pair"] == "PAIRF", "kill_count / last_kill_ts / pair from later kills + scouted")
ok(r[DUST]["kill_price"] == 0.00001, "first kill with no price falls back to a priced kill")

# idempotent: peaks and kill price survive a re-seed with a later kill
db.execute("UPDATE rejected_outcomes SET peak_price=0.002, peak_multiple=13.2, last_checked_ts=? WHERE address=?", (NOW, FIONA))
db.execute(K, (NOW - 10, FIONA, "FIONA2", "volume_1h", "later", 0.9, 1, 1))
db.commit()
ok(O.seed_main(db, now=NOW) == 0, "second seed inserts nothing new")
f = rows(db, "main")[FIONA]
ok(f["kill_price"] == 0.0001515 and f["kill_gate"] == "pumped_1h" and f["peak_price"] == 0.002 and f["peak_multiple"] == 13.2,
   "re-seed keeps kill price, gate and peaks")
ok(f["kill_count"] == 3 and f["last_kill_ts"] == NOW - 10, "re-seed updates kill_count and last_kill_ts")

# ---- dip seed ----
ddb = book.db
ddb.execute("INSERT INTO room(mint,pool,symbol,migrated_at,status,reason,updated_at) VALUES(?,?,?,?,?,?,?)",
            (FIONA, "POOLF", "FIONA", NOW - 3000, "killed", "liq $19.3K", NOW - 2900))
ddb.execute("INSERT INTO room(mint,pool,symbol,migrated_at,status,reason,updated_at) VALUES(?,?,?,?,?,?,?)",
            (DUST, "POOLD", "DUST", NOW - 3000, "killed", "liq $3", NOW - 2800))
ddb.execute("INSERT INTO room(mint,pool,symbol,migrated_at,status,reason,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("T" * 44, "POOLT", "TO", NOW - 3000, "dropped", "timed out waiting", NOW - 2700))
ddb.execute("INSERT INTO room(mint,pool,symbol,status,reason,updated_at) VALUES(?,?,?,?,?,?)", ("W" * 44, "PW", "WAIT", "waiting", None, NOW))
P = "INSERT INTO prices(mint,ts,price,liq,mcap) VALUES(?,?,?,?,?)"
ddb.execute(P, (FIONA, NOW - 2950, 4.906e-05, 14933, 48998))
ddb.execute(P, (FIONA, NOW - 2000, 4e-04, 90000, 400000))
ddb.commit()
ok(O.seed_dip(ddb, now=NOW) == 3, "dip seed: killed + dropped only, waiting skipped")
d = rows(ddb, "dip")
ok(d[FIONA]["kill_price"] == 4.906e-05 and d[FIONA]["kill_mcap"] == 48998 and d[FIONA]["kill_liq"] == 14933,
   "dip FIONA kill price/mcap/liq from the earliest prices row")
ok(d[FIONA]["kill_gate"] == "liq" and d[FIONA]["kill_reason"] == "liq $19.3K", "dip gate bucket via _kill_bucket")
ok(d["T" * 44]["kill_gate"] == "timed out" and d["T" * 44]["kill_price"] is None, "timed-out drop is included, no price")
from dip.book import _kill_bucket
ok(all(O.kill_bucket(x) == _kill_bucket(x) for x in ("liq $3", "top 10 hold 38%", "already +9% in 1h", "weird thing", None)),
   "shared kill_bucket matches dip.book._kill_bucket")
O.seed_dip(ddb, now=NOW)
ok(rows(ddb, "dip")[FIONA]["kill_price"] == 4.906e-05, "dip re-seed idempotent")

# ---- pick_outcome_pair ----
def pair(base, quote, price, liq, mcap=None, qaddr="So11111111111111111111111111111111111111112", pa="x"):
    return {"chainId": "solana", "dexId": "pumpswap", "pairAddress": pa, "url": "u/" + pa,
            "baseToken": {"address": base, "symbol": "B"}, "quoteToken": {"address": qaddr, "symbol": quote},
            "priceUsd": str(price), "liquidity": {"usd": liq}, "marketCap": mcap}

M = "MINT" + "1" * 40
bogus = pair("SOLX", "DCTY", 139.0, 2_000_000, 132e9, pa="bogus")
bogus["quoteToken"]["address"] = M                       # our mint is the QUOTE on a huge fake pair
real = pair(M, "SOL", 0.0012, 80_000, 1_200_000, pa="real")
dust = pair(M, "SOL", 0.5, 40, 500_000_000, pa="dust")
shallow = pair(M, "SOL", 0.0011, 9_000, 1_100_000, pa="shallow")
other_quote = pair(M, "RANDOM", 0.0013, 900_000, 1_300_000, pa="rq")
q = O.pick_outcome_pair([bogus, dust, shallow, real], M)
ok(q and q["pair"] == "real" and q["price_usd"] == 0.0012, "quote-token pair and dust outlier dropped; deepest real pair kept")
ok(O.pick_outcome_pair([bogus], M) is None, "only a quote-token pair -> no quote")
ok(O.pick_outcome_pair([other_quote, real], M)["pair"] == "real", "real quote (SOL/USDC) preferred over unknown quote")
ok(O.pick_outcome_pair([], M) is None, "empty pairs -> None")
outl = [pair(M, "SOL", 0.001, 50_000, pa="a"), pair(M, "SOL", 0.0011, 40_000, pa="b"), pair(M, "SOL", 9e6, 60_000, pa="c")]
ok(O.pick_outcome_pair(outl, M)["pair"] == "a", "extreme outlier price dropped even with decent liquidity")

# ---- apply_quotes ----
base = {"address": "A", "desk": "main", "symbol": "A", "pair": None, "url": None, "first_kill_ts": 1, "kill_price": 1.0,
        "kill_mcap": 1000, "peak_price": None, "peak_mcap": None, "min_price": None, "min_mcap": None, "dead": None}
qa = {"price_usd": 3.0, "liq_usd": 20_000, "mcap": 3000, "pair": "PA", "url": "uA", "symbol": "A"}
r1 = O.apply_quotes([base], {"A": qa}, now=10)[0]
ok(r1["peak_price"] == 3.0 and r1["peak_multiple"] == 3.0 and r1["current_multiple"] == 3.0 and r1["min_price"] == 3.0,
   "first quote sets peak/min/multiples")
r2 = O.apply_quotes([r1], {"A": dict(qa, price_usd=1.2, mcap=1200)}, now=20)[0]
ok(r2["peak_price"] == 3.0 and r2["peak_ts"] == 10 and r2["min_price"] == 1.2 and abs(r2["current_multiple"] - 1.2) < 1e-9,
   "lower price keeps peak, lowers min")
r3 = O.apply_quotes([r2], {"A": dict(qa, price_usd=6.0, mcap=6000)}, now=30)[0]
ok(r3["peak_price"] == 6.0 and r3["peak_mcap"] == 6000 and r3["peak_multiple"] == 6.0, "higher price lifts peak")
r4 = O.apply_quotes([r3], {"A": None}, now=40)[0]
ok(r4["dead"] == 1 and r4["current_price"] == 0.0 and r4["current_multiple"] == 0.0 and r4["peak_multiple"] == 6.0,
   "gone pair: current 0, dead, peak kept")
r5 = O.apply_quotes([base], {"A": dict(qa, liq_usd=200, price_usd=0.4)}, now=50)[0]
ok(r5["dead"] == 1 and r5["current_multiple"] == 0.4, "liq < $1k counts as dead")
ok(O.apply_quotes([base], {}, now=1)[0] is base, "mint not in quotes is untouched (kept on a failed batch)")

# ---- fetch_quotes: batching, 429 handling ----
calls = []
def fake_fetch(url):
    calls.append(url)
    return [pair(m, "SOL", 1.0, 5000) for m in url.rsplit("/", 1)[1].split(",") if m != "GONE"]
mints = [f"M{i:02d}" for i in range(65)] + ["GONE"]
qs, err = O.fetch_quotes(mints, fetch=fake_fetch, sleep=lambda s: None)
ok(len(calls) == 3 and err is None and qs["GONE"] is None and qs["M00"]["price_usd"] == 1.0, "30 per request; unanswered mint -> None")
def f429(url):
    raise RuntimeError("HTTP 429")
qs, err = O.fetch_quotes(mints, fetch=f429, sleep=lambda s: None)
ok(qs == {} and err.startswith("rate_limited"), "429: no quotes, error surfaced")

# refresh: throttle, keep rows on 429, never raise
cx = sqlite3.connect(":memory:")
cx.executescript(E.SCHEMA)
cx.execute(K, (NOW - 100, FIONA, "FIONA", "pumped_1h", "x", 1.0, 9000, 20000)); cx.commit()
O.seed_main(cx, now=NOW)
res = O.refresh(cx, "main", now=NOW, fetch=f429, sleep=lambda s: None)
ok(res["error"] and not res["skipped"] and rows(cx, "main")[FIONA]["kill_price"] == 1.0, "refresh on 429 keeps the row, reports error")
res = O.refresh(cx, "main", now=NOW + 60, fetch=f429, sleep=lambda s: None)
ok(res["skipped"], "refresh is throttled inside the interval")
calls.clear()
res = O.refresh(cx, "main", now=NOW + 3600, fetch=lambda u: [pair(FIONA, "SOL", 5.0, 50_000, 99_000)], sleep=lambda s: None)
ok(rows(cx, "main")[FIONA]["peak_multiple"] == 5.0 and res["error"] is None, "refresh applies a good quote after the interval")
ex = O.export(cx, "main", now=NOW + 3600)
ok(ex["missed_runners"][0]["address"] == FIONA and ex["outcomes_meta"]["tracked"] == 1, "export lists the runner")

# ---- gate scorecard math ----
def row(addr, gate, kp, peak, cur, dead=0, sym=None, checked=1):
    return {"address": addr, "desk": "main", "symbol": sym or addr, "kill_gate": gate, "kill_price": kp, "kill_mcap": 1000,
            "peak_mcap": 1000 * (peak or 1), "url": "u", "last_checked_ts": checked, "peak_multiple": peak,
            "current_multiple": cur, "dead": dead}
S_ROWS = [row("a", "g1", 1, 6.0, 5.0, sym="BIG"), row("b", "g1", 1, 2.0, 0.4), row("c", "g1", 1, 1.0, 0.9),
          row("d", "g1", 1, 1.0, 0.0, dead=1), row("e", "g1", 0, None, None, checked=None),
          row("f", "g2", 1, 1.2, 1.0), row("g", "g3", 1, 3.0, 0.5)]
sc = {g["gate"]: g for g in O.gate_scorecard(S_ROWS)}
g1 = sc["g1"]
ok(g1["unique_coins_killed"] == 5 and g1["tracked_coins"] == 4, "g1: 5 killed, 4 tracked (untracked excluded)")
ok(g1["runners_2x"] == 2 and g1["runners_5x"] == 1, "g1: 2x/5x counts")
ok(g1["crashed_50pct"] == 2, "g1: crash <=0.5x or dead")
ok(g1["median_peak_x"] == 1.5 and abs(g1["median_current_x"] - 0.65) < 1e-9, "g1: medians")
ok(g1["best_runner"]["symbol"] == "BIG" and g1["best_runner"]["peak_x"] == 6.0, "g1: best runner")
ok(sc["g3"]["crashed_50pct"] == 1 and sc["g3"]["runners_2x"] == 1, "exactly 0.5x counts as crash, 3x as 2x runner")
ok([g["gate"] for g in O.gate_scorecard(S_ROWS)] == ["g1", "g3", "g2"], "sorted by 5x, then 2x, then killed")

# ---- health ----
h = O.health_from_sources({"gecko_txns": "error: x: HTTP 429", "ds": "ok (3)"})
ok(h["status"] == "rate_limited" and h["errors"], "429 in a source -> rate_limited")
ok(O.health_from_sources({"gt": "error: boom"})["status"] == "errors", "other error -> errors")
ok(O.health_from_sources({"a": "ok (1)", "n": 3})["status"] == "ok", "clean -> ok")
ok(O.health_from_sources({"gt": "error: boom"}, ["rate_limited: HTTP 429"])["status"] == "rate_limited", "extra 429 wins")

# ---- GeckoTerminal 429 limiter ----
real_http = S.http_json
hits = []
clock = [1000.0]
def mock_429(url, timeout=15, retries=1, headers=None):
    hits.append(url)
    raise RuntimeError(f"{url[:60]}: HTTP 429")
def good(url, timeout=15, retries=1, headers=None):
    hits.append(url)
    return {"data": [{"attributes": {"address": "P1", "transactions": {"h1": {"buyers": 30}}}}]}
S.http_json = good
S._gecko_reset()
st = {}
out = S.gecko_pool_txns(["P1"], st)
ok("P1" in out and "gecko_txns" not in st, "fresh fetch fills cache, status left for caller ('ok (n)')")
S.http_json = mock_429
S._gecko["last_call"] = 0.0
out = S.gecko_pool_txns(["P1", "P2"], st)
ok("P1" in out and "P2" not in out, "429 returns last-good cache for cached pools")
ok(st["gecko_txns"].startswith("rate_limited (cooldown, using cache 1/2)") and "Traceback" not in st["gecko_txns"], f"clean status: {st['gecko_txns']}")
n = len(hits)
out = S.gecko_pool_txns(["P1"], st)
ok(len(hits) == n, "call inside the cooldown does not touch the network")
st2 = {}
S._gecko["cache"].clear()
out = S.gecko_pool_txns(["P9"], st2)
ok(out == {} and st2["gecko_txns"] == "rate_limited (skipped)" and len(hits) == n, "no cache -> 'rate_limited (skipped)', still no network")
slept = []
S._gecko_reset(); S.http_json = good
S.gecko_http("https://x/1", now=lambda: 50.0, sleep=slept.append)
S.gecko_http("https://x/2", now=lambda: 50.5, sleep=slept.append)
ok(slept and abs(slept[0] - 1.6) < 1e-6, "minimum spacing between gecko calls")
S._gecko_reset(); S.http_json = mock_429
try:
    S.gecko_http("https://x/3", now=lambda: 100.0, sleep=lambda s: None)
except RuntimeError:
    pass
ok(S._gecko["cooldown_until"] == 160.0, "429 sets a 60s cooldown")
S.http_json = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 500"))
S._gecko_reset()
try:
    S.gecko_pool_txns(["P1"], {})
    raise AssertionError("non-429 error should still raise")
except RuntimeError:
    ok(True, "non-429 errors still propagate to the caller")
S.http_json = real_http
S._gecko_reset()
print("ALL OK")

# ---- UI file sanity (static, no browser) ----
html = (Path(__file__).resolve().parent.parent / "web" / "static" / "index.html").read_text()
for f in ("dashboard.json", "dip_dashboard.json", "copy_dashboard.json"):
    ok("sneezekd/paper-memecoin-desk/" in html and f in html, f"UI still reads {f}")
ok('color-scheme" content="dark"' in html and "state/reports/" in html and "main/reports/" in html, "UI is dark and keeps state->main fallback")
ok("api.dexscreener.com" not in html, "UI never calls DexScreener from the browser")
