"""X watchlist: trusted comments only, ttl, and a score nudge that cannot pass a miss.

No network and no production database.
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import desk.engine as E
import desk.sources as S
from desk.xwatch import apply_x_boost, pick_watchlist

MINT = "MintWatch111111111111111111111111111111111"


def ok(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("ok:", msg)


def comment(body, login="sneezekd", assoc="OWNER", when="2026-11-16T20:00:00Z", cid=1):
    return {"id": cid, "body": body, "updated_at": when, "created_at": when,
            "author_association": assoc, "user": {"login": login}}


def payload(score=80, generated="2026-11-16T20:00:00Z", ttl=2, mint=MINT):
    return ("```json\n"
            '{"generated_at":"%s","ttl_hours":%s,"coins":[{"mint":"%s","symbol":"XW","x_score":%s,"mentions":4,"distinct_accounts":3,"notes":"test"}]}\n'
            "```") % (generated, ttl, mint, score)


NOW = time.mktime(time.strptime("2026-11-16T20:30:00Z", "%Y-%m-%dT%H:%M:%SZ"))
# mktime is local. Use calendar from the parser instead by passing an absolute now.
# 2026-11-16T20:30:00Z is 30 minutes after 20:00Z. Compute via the same parser.
from desk.xwatch import _parse_time
NOW = _parse_time("2026-11-16T20:30:00Z")

coins, meta = pick_watchlist([
    comment("not json", when="2026-11-16T20:10:00Z", cid=1),
    comment(payload(80), when="2026-11-16T20:00:00Z", cid=2),
    comment(payload(10), login="someoneelse", when="2026-11-16T20:20:00Z", cid=3),
], NOW)
ok(list(coins) == [MINT] and coins[MINT]["x_score"] == 80, f"owner comment wins over a stranger, got {coins}")
ok(meta["ignored"] == ["bad json"], f"bad json is skipped, got {meta}")

coins, meta = pick_watchlist([
    comment(payload(99), assoc="CONTRIBUTOR", when="2026-11-16T20:25:00Z", cid=4),
    comment(payload(40), when="2026-11-16T20:00:00Z", cid=5),
], NOW)
ok(coins[MINT]["x_score"] == 40, "sneezekd without OWNER is ignored")

old = pick_watchlist([comment(payload(90, generated="2026-11-16T10:00:00Z", ttl=2), when="2026-11-16T10:00:00Z")], NOW)
ok(old[0] == {}, f"expired payload is ignored, got {old}")

shown, boost, passed = apply_x_boost(60, 100, 8, 65, True)
ok(shown == 68 and boost == 8 and passed is False, f"boost cannot clear min score, got {shown, boost, passed}")
shown, boost, passed = apply_x_boost(70, 50, 8, 65, True)
ok(shown == 74 and boost == 4 and passed is True, f"half score nudges a passer, got {shown, boost, passed}")
shown, boost, passed = apply_x_boost(90, 100, 8, 65, False)
ok(passed is False and shown == 98, "low coverage still fails")
shown, boost, passed = apply_x_boost(80, None, 8, 65, True)
ok(boost == 0 and shown == 80 and passed, "no x_score leaves the score alone")

# Scout adds a missed mint, and still drops one outside the mcap window.
tmp = Path(tempfile.mkdtemp())
E.DB = tmp / "t.db"
E.EVENTS = tmp / "e.jsonl"
desk = E.Desk(verbose=False)
now_ms = int(time.time() * 1000) - 60 * 60 * 1000
low = "MintLow111111111111111111111111111111111111"

def pair(mcap):
    return {"pair": "pair", "dex": "raydium", "price_usd": 1.0, "mcap": mcap, "liq_usd": 40000,
            "pair_created_ms": now_ms, "fetched_at": time.time(), "symbol": "XW", "vol_h1": 1000,
            "chg_h1": 1, "chg_m5": 1}

S.ds_discovery = lambda terms, allowed: ({}, {"mocked": "discovery"})
S.gecko_discovery = lambda: ({}, {"mocked": "gecko"})
S.ds_best_pairs = lambda addrs, allowed: ({m: pair(500000 if m == MINT else 10) for m in addrs}, [])
E.load_x_watchlist = lambda log, cfg, now_ts=None, fetch=None: {
    MINT: {"mint": MINT, "symbol": "XW", "x_score": 80},
    low: {"mint": low, "symbol": "LOW", "x_score": 90},
}
cands, counts = E.Scout(desk.cfg, desk.log).run({})
got = {c["address"]: c for c in cands}
ok(MINT in got and got[MINT]["x_watch"]["x_score"] == 80, "watchlist mint is scouted when discovery missed it")
ok(low not in got and counts["mcap_out_of_window"] >= 1, "watchlist mint outside the mcap window is still dropped")
print("X WATCHLIST TESTS PASSED")
