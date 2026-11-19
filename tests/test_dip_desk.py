"""Offline smoke tests for the dip desk (step 1): fees, layers, scanner kills. No network."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dip.book import DipBook, load_config
from dip.fees import fee_usd
from dip.layers import run_layers
from dip.scanner import (Scanner, blocking_warns, decide, holders_growth, is_copycat, normalize,
                         top10_share)
from dip import sources as S
from dip.brain import size_order
from dip.executor import Executor, dip_decision, paper_fill, recheck, tp_step
from dip.score import evaluate, fresh_pct, gate, score_parts

CFG = load_config()
MINT, POOL, DEV = "M" * 44, "P" * 44, "D" * 44


def ok(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("ok:", msg)


# ---- fees ----
for size, want in [(4.99, 0.10), (5, 0.10), (20, 0.40), (47.50, 0.95), (100, 0.95), (190, 0.95), (200, 0.90)]:
    ok(abs(fee_usd(size) - want) < 1e-9, f"fee {size} -> {want} (got {fee_usd(size)})")


# ---- layers ----
class Fake:
    def __init__(self, **kw):
        self.d = dict(
            mint_info={"mintAuthority": None, "freezeAuthority": None, "supply": 1_000_000.0},
            report={"mint": MINT, "creator": DEV, "creatorBalance": 10, "token": {"supply": 1_000_000},
                    "insiderNetworks": [], "topHolders": [],
                    "markets": [{"pubkey": POOL, "lp": {"lpLockedPct": 100}}]},
            graph=[], balance=10.0, priors=[], rugs=set(),
            holders=[{"address": "a%d" % i, "owner": "w%d" % i, "amount": 10_000.0} for i in range(10)],
            accounts=[{"address": "t%d" % i, "owner": "w%d" % i, "amount": 100.0, "delegate": None, "delegated": 0}
                      for i in range(25)])
        self.d.update(kw)
        self.calls = []

    def _c(self, n):
        self.calls.append(n)

    def mint_info(self, m): self._c(1); return self.d["mint_info"]
    def report(self, m): return self.d["report"]
    def graph(self, m): self._c(7); return self.d["graph"]
    def creator_balance(self, c, m): return self.d["balance"]
    def prior_mints(self, c, m): return self.d["priors"]
    def is_rug(self, m): return m in self.d["rugs"]
    def top_holders(self, m): return self.d["holders"]
    def token_accounts(self, m): self._c(8); return self.d["accounts"]


def ctx(**kw):
    c = {"mint": MINT, "pool": POOL, "creator": DEV, "creator_balance": 10.0, "liq_scanner": 30000,
         "liq_now": 29000, "pool_age_min": 12}
    c.update(kw)
    return c


def statuses(res):
    return [r["status"] for r in res]


res = run_layers(ctx(), Fake(), CFG)
ok(statuses(res) == ["ok"] * 8, "all 8 layers ok")
ok(res[7]["holder_owners"], "layer 8 stores holder owners")

res = run_layers(ctx(), Fake(mint_info={"mintAuthority": "X", "freezeAuthority": None, "supply": 1e6}), CFG)
ok(statuses(res) == ["red"] + ["not checked"] * 7 and res[0]["reason"] == "mint not revoked", "first red stops, rest not checked")
f = Fake(mint_info={"mintAuthority": None, "freezeAuthority": "F", "supply": 1e6})
res = run_layers(ctx(), f, CFG)
ok(res[1]["reason"] == "freeze not revoked" and 7 not in f.calls and 8 not in f.calls, "layer 2 red; later layers never fetched")
res = run_layers(ctx(), Fake(mint_info=None), CFG)
ok(res[0]["status"] == "red" and res[0]["reason"].startswith("no data"), "no mint data = red no data")
res = run_layers(ctx(creator=None), Fake(report={"mint": MINT, "markets": []}), CFG)
ok(res[2]["reason"] == "no data: dev", "unknown creator = red no data: dev")
res = run_layers(ctx(), Fake(balance=5.0), CFG)
ok(res[2]["reason"] == "dev sold", "creator balance lower than room entry = dev sold")
res = run_layers(ctx(), Fake(priors=["old1", "old2"], rugs={"old2"}), CFG)
ok(res[2]["reason"] == "dev rugged 1 before", "creator with a mint in rugs -> layer 3 red")
res = run_layers(ctx(), Fake(priors=[]), CFG)
ok(res[2]["status"] == "ok", "fresh dev with no history is ok")
res = run_layers(ctx(), Fake(holders=[{"address": "a0", "owner": "w0", "amount": 580_000.0}]), CFG)
ok(res[3]["reason"] == "top 10 hold 58%", "top 10 hold 58% red")
res = run_layers(ctx(), Fake(holders=[{"address": "a0", "owner": POOL, "amount": 580_000.0}]), CFG)
ok(res[3]["status"] == "ok", "pool-owned vault excluded from top 10")
res = run_layers(ctx(liq_now=10000), Fake(), CFG)
ok(res[4]["reason"] == "liquidity down 67%", "liquidity down red")
rep = Fake().d["report"]
res = run_layers(ctx(), Fake(report={**rep, "markets": []}), CFG)
ok(res[4]["reason"] == "no data: liquidity", "no market = no data: liquidity")
res = run_layers(ctx(pool_age_min=4), Fake(), CFG)
ok(res[5]["reason"] == "pool too young", "pool too young")
ws = ["w%d" % i for i in range(6)]
edges = [{"source": ws[i], "target": ws[i + 1]} for i in range(5)]
hs = [{"address": "a%d" % i, "owner": ws[i], "amount": 100.0} for i in range(6)]
res = run_layers(ctx(), Fake(graph=[{"nodes": [], "links": edges}], holders=hs), CFG)
ok(res[6]["reason"] == "6 bundled wallets", "bundled wallets red")
res = run_layers(ctx(), Fake(graph=None), CFG)
ok(res[6]["reason"] == "no data: bundles", "graph failure = no data: bundles")
res = run_layers(ctx(), Fake(graph=[]), CFG)
ok(res[6]["status"] == "ok", "empty graph list is a valid 'no edges'")
res = run_layers(ctx(), Fake(report={**rep, "insiderNetworks": [{"tokenAmount": 380_000}]}), CFG)
ok(res[6]["reason"] == "insiders hold 38%", "insiders 38% -> layer 7 red")
acc = [{"address": "t%d" % i, "owner": "w%d" % i, "amount": 1.0, "delegate": "9fQx" + "z" * 36 + "T2a", "delegated": 5}
       for i in range(11)]
res = run_layers(ctx(), Fake(accounts=acc), CFG)
ok(res[7]["status"] == "red" and res[7]["reason"].startswith("11 wallets -> 9fQx"), "delegations red")
res = run_layers(ctx(), Fake(accounts=None), CFG)
ok(res[7]["reason"] == "no data: delegations", "RPC refuses = no data: delegations")


# ---- scanner ----
NOW = 1_800_000_000.0


def row(**kw):
    r = {"liq_usd": 50000, "chg_h1": 20, "pair_created_ms": (NOW - 15 * 60) * 1000, "name": "Foo", "symbol": "FOO",
         "pair": POOL, "price_usd": 1.0}
    r.update(kw)
    return r


COIN = {"mint": MINT, "name": "Foo Coin", "symbol": "FOO", "migrated_at": NOW - 15 * 60}
SAMPLES = [(NOW - 600, 100), (NOW, 100)]


def dec(**kw):
    a = dict(coin=COIN, row=row(), report={"mint": MINT, "risks": []}, holders_now=100, samples=SAMPLES,
             top10=10.0, runners=[], cfg=CFG, now=NOW, prices=None)
    a.update(kw)
    return decide(a["coin"], a["row"], a["report"], a["holders_now"], a["samples"], a["top10"], a["runners"],
                  a["cfg"], a["now"], a["prices"])


ok(dec() == ("pass", None), "clean coin passes at pool age >= 10 min")
ok(dec(row=row(pair_created_ms=(NOW - 5 * 60) * 1000))[0] == "wait", "pool age 5 min waits")
ok(dec(row=None)[0] == "wait", "coin unknown to DexScreener never passes")
ok(dec(row=row(liq_usd=14200)) == ("kill", "liq $14.2K"), "liq kill text")
ok(dec(top10=38.0) == ("kill", "top 10 hold 38%"), "top10 kill")
ok(dec(row=row(chg_h1=240)) == ("kill", "+240% in 1h"), "h1 kill")
ok(dec(report={"risks": [{"level": "danger", "name": "Freeze Authority still enabled"}]})[1].startswith("warns: "), "danger risk warns kill")
ok(blocking_warns({"token_extensions": [{"extension": "transferHook"}]}) == "transferHook", "token-2022 blocking extension")
ok(blocking_warns({"token_extensions": [{"extension": "metadataPointer"}]}) is None, "harmless extension ok")
ok(dec(samples=[(NOW - 600, 100), (NOW, 96)]) == ("kill", "holders leaving -4% in 10m"), "holders -4% kill")
ok(dec(samples=[(NOW - 120, 100), (NOW, 90)])[0] == "wait", "only young samples -> wait")
ok(dec(holders_now=None, samples=[]) == ("kill", "no data: holders"), "holders unknown at pass time kills")
ok(holders_growth([(NOW - 600, 100), (NOW - 400, 50), (NOW, 110)], NOW) is not None, "growth uses newest old-enough sample")
ok(normalize("Pepe 2.0 🐸!") == "pepe", "normalize drops digits/emoji/non-alnum")
runners = [{"mint": "R" * 44, "name": "Pepe the Frog", "symbol": "PEPE"}]
ok(is_copycat(MINT, "Pepe", "PEP3", runners) == "PEPE", "copycat: name inside runner name")
ok(is_copycat("R" * 44, "Pepe", "PEPE", runners) is None, "copycat excludes itself")
ok(dec(runners=runners, coin={**COIN, "name": "PEPE 2", "symbol": "PEPE2"})[1].startswith("copycat of"), "copycat kill")
ok(dec(row=row(), coin={**COIN, "migrated_at": NOW - 61 * 60}, top10=None)[0] == "drop", "60 min without passing -> timed out")
ok(top10_share([{"address": "a", "owner": POOL, "amount": 900.0}, {"address": "b", "owner": "w", "amount": 100.0}], 1000, {POOL}) == 10.0,
   "top10 excludes pool")

# scanner end to end with mocked IO
book = DipBook(":memory:")
book.room_add(MINT, pool=POOL, creator=DEV, name="Foo Coin", symbol="FOO", migrated_at=NOW - 15 * 60)
book.add_holders(MINT, 100, NOW - 600)


class FakeDS:
    def batch(self, mints): return {MINT: row(liq_usd=14200)}, []
    def top_boosts_runners(self, pct): return []


class FakeRC:
    def report(self, m): return {"mint": MINT, "totalHolders": 100, "risks": []}
    def rugs_ticker(self, n): return [{"mint": "old", "creator": DEV, "name": "x"}]


class FakeRpc:
    def largest_accounts(self, m): return [{"address": "a", "amount": 10.0}]
    def token_supply(self, m): return 1000.0
    def token_account_owner(self, a): return "w"
    def owner_balance(self, o, m): return 5.0


sc = Scanner(book, FakeDS(), FakeRC(), FakeRpc(), CFG, clock=lambda: NOW)
out = sc.tick()
ok(out == [(MINT, "kill", "liq $14.2K")] and book.kill_counts() == {"liq": 1}, "scanner tick kills on low liquidity and counts it")
ok(book.is_rug("old"), "rugs ticker refreshed into the rugs table")
ok(book.last_price(MINT)["liq_mcap"] is None and book.last_price(MINT)["price"] == 1.0, "prices row stored")


# ---- scanner fixes: only real migrations, stub first reads wait, h1 measured on the PumpSwap pool ----
from dip.scanner import measured_h1  # noqa: E402
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ok(S.parse_portal_message({"mint": USDC, "signature": "sig"}, 5.0) is None, "nameless message with a signature is NOT a migration")
ok(S.parse_portal_message({"mint": USDC, "signature": "sig", "txType": "buy"}, 5.0) is None, "trade message ignored")
ok(S.parse_portal_message({"mint": USDC, "message": "Successfully subscribed"}, 5.0) is None, "subscribe ack ignored")
ok(S.parse_portal_message({"mint": "m", "txType": "migrate"}, 5.0)["kind"] == "migration", "explicit migrate still parsed")
ok(S.parse_portal_message({"mint": "m", "name": "n", "symbol": "s"}, 5.0)["kind"] == "new", "named new-token message still parsed")
sc3 = Scanner(DipBook(":memory:"), FakeDS(), FakeRC(), FakeRpc(), CFG, clock=lambda: NOW)
sc3.on_event({"kind": "new", "mint": MINT, "creator": DEV, "name": "n", "symbol": "s", "seen_at": NOW - 30, "curve": None})
ok(sc3.book.room_waiting() == [], "new (still on curve) token never enters the room")

young = row(pair_created_ms=(NOW - 80) * 1000)
ycoin = {**COIN, "migrated_at": NOW - 80}
one = [(NOW, 1.0, 50000)]
two = [(NOW - 60, 1.0, 50000), (NOW, 1.0, 50000)]
ok(dec(row=young | {"liq_usd": 4}, coin=ycoin, prices=one)[0] == "wait", "seconds-old pair, stub $4 liq on first read: wait, no kill")
ok(dec(row=young | {"liq_usd": None}, coin=ycoin, prices=one)[0] == "wait", "seconds-old pair with no liquidity figure: wait")
ok(dec(row=young | {"liq_usd": 14200}, coin=ycoin, prices=two) == ("kill", "liq $14.2K"), "real liq under $20k kills once seen twice")
ok(dec(row=young | {"chg_h1": 364679}, coin=ycoin, prices=one)[0] == "wait", "seconds-old pair, garbage DexScreener h1: wait, no kill")
ok(dec(row=young | {"chg_h1": 364679}, coin=ycoin, prices=two)[0] == "wait", "garbage h1 ignored; flat measured price: wait")
ok(dec(row=young, coin=ycoin, prices=[(NOW - 60, 1.0, 50000), (NOW, 3.5, 50000)]) == ("kill", "+250% in 1h"), "measured +250% on the pool kills")
ok(dec(row=young, coin=ycoin, prices=[(NOW - 60, 1.0, 50000), (NOW, 3.0, 50000)])[0] == "wait", "measured +200% exactly is not above the cut")
ok(abs(measured_h1([(NOW - 4000, 1.0, 9e4), (NOW - 60, 2.0, 9e4), (NOW, 2.2, 9e4)], NOW, 20000) - 10.0) < 1e-6 and measured_h1(one, NOW) is None,
   "measured_h1 uses the last hour, needs 2 readings")
ok(abs(measured_h1([(NOW - 120, 1e-9, 3), (NOW - 60, 1.0, 9e4), (NOW, 1.5, 9e4)], NOW, 20000) - 50.0) < 1e-6, "stub first read (liq $3) is not a baseline")
ok(dec(row=row(chg_h1=364679), prices=[(NOW - 600, 1.0, 5e4), (NOW, 1.0, 5e4)])[0] == "pass", "old pool with flat measured price passes despite dust-tick h1")
ok(dec(row=row(chg_h1=300), prices=None)[0] == "kill", "no history, 15-min pool: raw h1 still kills")
ok(dec(row=row(chg_h1=300, pair_created_ms=(NOW - 61 * 60) * 1000), prices=[(NOW, 1.0, 5e4)])[0] == "kill", "pair a full hour old: DexScreener h1 is a real window")
ok(dec(row=None, coin={**COIN, "migrated_at": NOW - 59 * 60})[0] == "wait" and dec(row=None, coin={**COIN, "migrated_at": NOW - 61 * 60})[0] == "drop", "unknown coin waits, then drops at 60 min")
ok(dec(row=young | {"liq_usd": None}, coin={**COIN, "migrated_at": NOW - 61 * 60}, prices=one)[0] == "drop", "no-liquidity pair still drops at 60 min")

# tick end to end: stub first read waits and is kept; real second read decides on measured h1
tb = DipBook(":memory:")
tb.room_add(MINT, pool=POOL, creator=DEV, name="Foo Coin", symbol="FOO", migrated_at=NOW - 80)
tb.add_holders(MINT, 100, NOW - 60)
seq = iter([row(liq_usd=3, price_usd=1e-9, chg_h1=5e6, pair_created_ms=(NOW - 80) * 1000),
            row(liq_usd=60000, price_usd=1.0, chg_h1=5e6, pair_created_ms=(NOW - 80) * 1000)])
class SeqDS:
    def batch(self, mints): return {MINT: next(seq)}, []
    def top_boosts_runners(self, pct): return []
t = {"now": NOW}
sc4 = Scanner(tb, SeqDS(), FakeRC(), FakeRpc(), CFG, clock=lambda: t["now"])
ok(sc4.tick() == [] and len(tb.room_waiting()) == 1, "first read stub (liq $3, h1 5e6%): coin stays in the room")
t["now"] += 60
ok(sc4.tick() == [] and len(tb.room_waiting()) == 1, "second read: stub baseline ignored, only one real reading -> still waiting (not a +1e11% kill)")

class RLDS:
    def batch(self, mints):
        raise S.RateLimited("429")
    def top_boosts_runners(self, pct): return []
tb2 = DipBook(":memory:")
tb2.room_add(MINT, migrated_at=NOW - 80)
try:
    Scanner(tb2, RLDS(), FakeRC(), FakeRpc(), CFG, clock=lambda: NOW).tick()
    raised = False
except S.RateLimited:
    raised = True
ok(raised and len(tb2.room_waiting()) == 1, "429 propagates to the caller (which waits 30s) and the room is kept")

# book: reserved cash
b2 = DipBook(":memory:")
ok(b2.cash() == 100 and b2.free_cash() == 100, "paper start cash 100")
oid = b2.order_create(MINT, 12.0, 80)
ok(b2.free_cash() == 88.0, "waiting order reserves cash")
b2.order_close(oid, "cancelled", "no dip")
ok(b2.free_cash() == 100.0, "cancel releases reserved cash")

# sources: pure pair pick + retry + portal parse
pairs = [{"chainId": "solana", "dexId": "raydium", "baseToken": {"address": "A"}, "liquidity": {"usd": 9e9}},
         {"chainId": "solana", "dexId": "pumpswap", "baseToken": {"address": "A", "name": "a", "symbol": "A"}, "liquidity": {"usd": 5}, "priceUsd": "2"},
         {"chainId": "solana", "dexId": "pumpswap", "baseToken": {"address": "A", "name": "a", "symbol": "A"}, "liquidity": {"usd": 50}, "priceUsd": "3"}]
ok(S.pick_pairs(pairs, ["A", "B"])["A"]["price_usd"] == 3.0 and "B" not in S.pick_pairs(pairs, ["A", "B"]), "pumpswap pair pick by liquidity; unknown mint absent")
n = {"c": 0}
def flaky():
    n["c"] += 1
    if n["c"] < 3:
        raise RuntimeError("x")
    return 1
ok(S.retry(flaky, 3, 2, sleep=lambda s: None) == (True, 1) and n["c"] == 3, "retry 3x")
ev = S.parse_portal_message({"txType": "migrate", "mint": "m", "pool": "pump-amm"}, 5.0)
ok(ev["kind"] == "migration" and ev["pool"] is None and ev["creator"] is None, "portal migration parsed defensively")
ev = S.parse_portal_message({"txType": "create", "mint": "m", "traderPublicKey": "c", "name": "n"}, 5.0)
ok(ev["kind"] == "new" and ev["creator"] == "c", "portal new token parsed")
ok([S.backoff_seconds(i) for i in (0, 1, 2, 6, 9)] == [1, 2, 4, 60, 60], "backoff 1,2,4..60")

# ---- portal fixes: bondingCurveKey, migrate signer is not the creator ----
ev = S.parse_portal_message({"txType": "create", "mint": "m", "traderPublicKey": "c", "bondingCurveKey": "BC", "name": "n"}, 5.0)
ok(ev["curve"] == "BC" and ev["creator"] == "c", "create message: bondingCurveKey stored as curve")
ev = S.parse_portal_message({"txType": "migrate", "mint": "m", "traderPublicKey": "signer", "pool": "pump-amm"}, 5.0)
ok(ev["creator"] is None, "migrate traderPublicKey is NOT the creator")
sc2 = Scanner(DipBook(":memory:"), FakeDS(), FakeRC(), FakeRpc(), CFG, clock=lambda: NOW)
sc2.on_event({"kind": "new", "mint": MINT, "creator": DEV, "curve": "CURVE" + "c" * 39, "name": "Foo", "symbol": "F", "seen_at": NOW - 600})
sc2.on_event({"kind": "migration", "mint": MINT, "creator": None, "pool": None, "seen_at": NOW})
rm = sc2.book.room_get(MINT)
ok(rm["creator"] == DEV and rm["curve"].startswith("CURVE") and rm["curve_min"] == 10.0, "room: creator from create msg, curve stored")
sc2.book.room_update(MINT, liq_scanner=50000)
sc2.tick()
ok(sc2.book.room_get(MINT)["pool"] == POOL, "pool from DexScreener pairAddress when message lacks one")
ok(top10_share([{"address": "cv", "owner": "x", "amount": 900.0}, {"address": "b", "owner": "w", "amount": 100.0}], 1000, {"cv"}) == 10.0,
   "curve excluded from top 10 (scanner helper)")
res = run_layers(ctx(curve="CV"), Fake(holders=[{"address": "a0", "owner": "CV", "amount": 580_000.0}]), CFG)
ok(res[3]["status"] == "ok", "layer 4 excludes the bonding curve")

# ---- score / gate ----
def mrow(**kw):  # core = 4 -> alive 1, organic 1, flush 1, chase 0
    r = {"vol_h1": 25000, "buys_h1": 150, "sells_h1": 100, "buys_m5": 10, "sells_m5": 5, "chg_h1": 0}
    r.update(kw)
    return r


p = score_parts(mrow(), 1, 20, 10, CFG)
ok(p["score"] == 100 and p["social_pts"] == 0 and p["social"] is None, "social off -> 4-part score, social_pts 0")
ok("social: off" in p["log"] and "smart: unavailable" in p["log"] and "organic: txns" in p["log"], "social: off / smart: unavailable logged")
ok(score_parts(mrow(vol_h1=25000 * 0.69), 1, 20, 10, CFG)["score"] == 92, "score parts are rounded")
# 69 vs 70: core 2.76 -> 69, core 2.8 -> 70 (alive carries the difference, organic/flush/chase fixed)
low = dict(mrow(), buys_h1=0, sells_h1=0, buys_m5=0, sells_m5=0, chg_h1=0)  # organic 0, flush 1, chase 0 -> core = alive + 2
p69 = score_parts(dict(low, vol_h1=25000 * 0.76), None, None, None, CFG)
p70 = score_parts(dict(low, vol_h1=25000 * 0.8), None, None, None, CFG)
ok((p69["score"], p70["score"]) == (69, 70), "boundary scores 69 / 70 constructed from parts")
green = [{"n": i, "status": "ok", "reason": None} for i in range(1, 9)]
ok(gate(green, 69, CFG)[0] == "reject" and gate(green, 70, CFG)[0] == "pass", "score 69 rejects, 70 passes")
red7 = green[:6] + [{"n": 7, "status": "red", "reason": "14 bundled wallets"}, {"n": 8, "status": "not checked", "reason": None}]
ok(gate(red7, 100, CFG) == ("reject", "14 bundled wallets"), "one red layer rejects regardless of score")
ok(gate(green[:7] + [{"n": 8, "status": "not checked"}], 100, CFG)[0] == "reject", "an unchecked layer never passes")
ps = score_parts(mrow(), 1, 20, 10, {**CFG, "social": {**CFG["social"], "enabled": True}}, social=1.0)
ok(ps["score"] == 100 and ps["social_pts"] == 0 and "social: off" not in ps["log"], "social on: 5-part formula, no 'social: off'")
ps = score_parts(mrow(vol_h1=0), 1, 20, 10, {**CFG, "social": {**CFG["social"], "enabled": True}}, social=1.0)
ok((ps["score"], ps["social_pts"]) == (78, 3), "social_pts = score - round(100*core/4)")

# organic multipliers: curve 4 min + fresh 60% -> x0.25
pm = score_parts(mrow(), 1, 4, 60, CFG)
ok(abs(pm["organic"] - 0.25) < 1e-9, "curve 4 min + fresh 60% -> organic x0.25")
ok(abs(score_parts(mrow(sells_h1=200), 3, 20, 10, CFG)["organic"] - 0.25) < 1e-9, "sells>buys and bundled group 3 -> x0.25")
ok(score_parts(mrow(buys_m5=10, sells_m5=15), 1, 20, 10, CFG)["flush"] == 0.5, "flush 0.5 at 1.5x")
ok(score_parts(mrow(buys_m5=10, sells_m5=16), 1, 20, 10, CFG)["flush"] == 0.0, "flush 0 above 1.5x")
ok(score_parts(mrow(chg_h1=100), 1, 20, 10, CFG)["chase"] == 0.5, "chase 100% -> 0.5")

# fresh_pct: top 20, paced, < 10 signatures
slept, owners = [], ["o%d" % i for i in range(25)]
counts = {o: (3 if i < 12 else 20) for i, o in enumerate(owners)}
pct, chk, fr = fresh_pct(owners, counts.get, CFG, sleep=slept.append)
ok(chk == 20 and fr == 12 and pct == 60.0 and len(slept) == 19 and set(slept) == {0.5}, "fresh_pct: top 20 owners, 0.5 s pacing, 60%")
ok(fresh_pct(["a"], lambda o: None, CFG, sleep=lambda s: None)[0] is None, "fresh_pct unknown without signature data")

# evaluate saves every decision with all parts, including rejects
bk = DipBook(":memory:")
lay = run_layers(ctx(), Fake(), CFG)
cx = ctx(bundle_group=1, curve_min=4)
out = evaluate(cx, mrow(), lay, lambda o: 3, CFG, book=bk, sleep=lambda s: None, now=NOW)
a = bk.recent_audits()[0]
ok(out["parts"]["fresh_pct"] == 100.0 and abs(a["parts"]["organic"] - 0.25) < 1e-9 and a["parts"]["log"] and a["score"] == out["score"],
   "audit row holds all parts (fresh 100% + curve 4 -> organic x0.25)")
lay = run_layers(ctx(), Fake(mint_info={"mintAuthority": "X", "freezeAuthority": None, "supply": 1e6}), CFG)
out = evaluate(ctx(), mrow(), lay, lambda o: 3, CFG, book=bk, sleep=lambda s: None, now=NOW)
ok(out["verdict"] == "reject" and out["reason"] == "mint not revoked" and bk.recent_audits()[0]["verdict"] == "reject",
   "red layer rejects and the reject is saved")

# ---- brain ----
ok(size_order(100, 80, 1e6, CFG)["size"] == 12.0, "cash 100, score 80 -> $12")
ok(size_order(100, 79, 1e6, CFG)["size"] == 6.0, "score 79 -> $6")
r = size_order(10, 80, 1e6, CFG)
ok(r["size"] == 0 and r["reason"] == "under $2", "$1.20 -> skip under $2")
big = {**CFG, "brain": {**CFG["brain"], "max_ticket_usd": 1000}}
r = size_order(10000, 80, 20000, big)
ok(r["size"] == 400.0 and r["capHit"] == "pool", "pool 20000 -> $400 pool cap")
r = size_order(10000, 80, 1e9, CFG)
ok(r["size"] == 50.0 and r["capHit"] == "ticket", "ticket cap $50")
ok(size_order(100, 80, 1e6, CFG)["capHit"] is None, "no cap hit when under both caps")
bb = DipBook(":memory:")
bb.order_create("X" * 44, 12.0, 80)
ok(size_order(bb.free_cash(), 80, 1e6, CFG)["size"] == round(88 * 0.12, 2), "reserved cash is not free")
ok(size_order(100, 80, 1e6, CFG, trades_night=30)["reason"] == "max trades", "max trades skip")
ok(size_order(100, 80, 1e6, CFG, night_loss=30)["reason"] == "night loss stop", "night loss stop skip")
ok(size_order(100, 80, 1e6, CFG, has_position=True)["size"] == 0, "no 2nd buy on a held mint")
ok(size_order(100, 80, 1e6, {**CFG, "social": {**CFG["social"], "enabled": True}}, shill=True)["size"] == 6.0, "shill ring halves (social on)")
ok(size_order(100, 80, 1e6, CFG, shill=True)["size"] == 12.0, "shill ignored when social off")
ok(size_order(33.333, 80, 1e6, CFG)["size"] == 4.0, "rounded to cents")

# ---- executor: dip entry ----
T0 = NOW
ORDER = {"id": 1, "mint": MINT, "size_usd": 12.0, "expires_at": T0 + 1800, "scanner_liq": 50000, "analyst_price": 1.0,
         "creator_balance": 10.0, "reserved_usd": 12.0}
calm = {"buys_m5": 10, "sells_m5": 5}
run = {"buys_m5": 5, "sells_m5": 20}
ok(dip_decision(ORDER, 1.0, calm, 1.0, T0, CFG)[0] == "wait", "at the 10 min high waits")
ok(dip_decision(ORDER, 0.85, run, 1.0, T0, CFG)[0] == "wait", "15% under with sellers running waits")
ok(dip_decision(ORDER, 0.85, calm, 1.0, T0, CFG)[0] == "buy", "15% under with buyers back buys")
ok(dip_decision(ORDER, 0.59, calm, 1.0, T0, CFG) == ("skip", "crash -41%"), '41% under -> skip "crash -41%"')
ok(dip_decision(ORDER, 0.9, calm, 1.0, T0 + 1800, CFG) == ("skip", "no dip"), '30 min with no dip -> "no dip"')
ok(recheck(ORDER, {"liq_usd": 39000, "price_usd": 0.85}, 50, None, 10.0, CFG) == "liquidity dropping", "re-check: liq down >20%")
ok(recheck(ORDER, {"liq_usd": 50000, "price_usd": 1.2}, 50, None, 10.0, CFG) == "ran before the click", "re-check: price up >15% since Analyst")
ok(recheck(ORDER, {"liq_usd": 50000, "price_usd": 0.85}, 5, None, 10.0, CFG) == "no free cash", "re-check: no free cash")
ok(recheck(ORDER, {"liq_usd": 50000, "price_usd": 0.85}, 50, "freezable", 10.0, CFG) == "warns: freezable", "re-check: warns")
ok(recheck(ORDER, {"liq_usd": 50000, "price_usd": 0.85}, 50, None, 9.0, CFG) == "dev sold while we waited", "re-check: creator balance lower")
ok(recheck(ORDER, {"liq_usd": 50000, "price_usd": 0.85}, 50, None, 10.0, CFG) is None, "re-check passes clean")
qty, fee, stop = paper_fill(12.0, 0.5, CFG)
ok(abs(fee - 0.24) < 1e-9 and abs(qty - (12 - 0.24) / 0.5) < 1e-9 and abs(stop - 0.375) < 1e-9, "paper fill per fee model, stop line -25%")


def prow(price, buys=10, sells=5, liq=50000):
    return {"price_usd": price, "liq_usd": liq, "buys_m5": buys, "sells_m5": sells, "mcap": 1e5}


eb = DipBook(":memory:")
eb.night_reset("n1", 100.0)
bal = {"v": 10.0}
ex = Executor(eb, CFG, creator_balance_fn=lambda m: bal["v"])
row0 = {**prow(1.0), "pair": POOL}
oid = ex.create_order(MINT, 12.0, 80, row0, scanner_liq=50000, creator_balance=10.0, now=T0)
ok(eb.free_cash() == 88.0, "waiting dip order reserves cash")
eb.add_price(MINT, prow(1.0), T0 - 120)  # the 10 min high
ok(ex.tick({MINT: prow(1.0)}, "n1", now=T0) == [], "executor waits at the high")
ok(ex.tick({MINT: prow(0.85, 5, 20)}, "n1", now=T0 + 5) == [] and eb.orders_waiting(), "executor waits: 15% under, sellers running")
out = ex.tick({MINT: prow(0.85)}, "n1", now=T0 + 10)
pos = eb.open_positions()
ok(out == [(MINT, "buy", pos[0]["id"])] and abs(eb.cash() - 88.0) < 1e-9 and eb.reserved() == 0, "executor buys at the dip, cash spent, reservation released")
ok(abs(pos[0]["entry_price"] - 0.85) < 1e-9 and pos[0]["tp_done"] == 0 and abs(pos[0]["stop_line"] - 0.6375) < 1e-9, "position stores entry, qty, stop line")
ok(eb.night()["trades"] == 1, "night trade counter bumped on fill")
# one position per mint per night
ex.create_order(MINT, 12.0, 80, row0, scanner_liq=50000, creator_balance=10.0, now=T0 + 20)
eb.add_price(MINT, prow(1.0), T0 + 20)
out = ex.tick({MINT: prow(0.8)}, "n1", now=T0 + 30)
ok(out[0] == (MINT, "skip", "already in position") and len(eb.open_positions()) == 1 and eb.reserved() == 0, "one position per mint per night")
ok(eb.has_position(MINT, "n1"), "book.has_position sees it")
# take profit then trail
entry = 0.85
out = ex.tick({MINT: prow(entry * 2.0)}, "n1", now=T0 + 40)
p = eb.open_positions()[0]
ok(out == [(MINT, "sell", "take profit")] and p["tp_done"] == 1 and abs(p["qty"] - pos[0]["qty"] / 2) < 1e-9, "TP 2.0x sells 50%")
ex.tick({MINT: prow(3.0)}, "n1", now=T0 + 50)
ok(abs(eb.open_positions()[0]["peak"] - 3.0) < 1e-9 and len(eb.open_positions()) == 1, "peak ratchets after TP")
out = ex.tick({MINT: prow(1.79)}, "n1", now=T0 + 60)
ok(out == [(MINT, "sell", "trail")] and not eb.open_positions(), "40% off the peak sells the rest")
ok(tp_step({"entry_price": 1, "peak": 1, "tp_done": 0}, 1.99, CFG)[1] is None, "below 2.0x no take profit")
trades = [dict(r) for r in eb.db.execute("SELECT side, reason FROM trades ORDER BY id")]
ok([t["reason"] for t in trades] == ["dip", "take profit", "trail"], "trades recorded: buy, TP, trail")

# cancel releases reserved cash; timeout -> no dip; crash skip
eb2 = DipBook(":memory:")
ex2 = Executor(eb2, CFG)
ex2.create_order(MINT, 12.0, 80, row0, scanner_liq=50000, now=T0)
eb2.add_price(MINT, prow(1.0), T0 - 60)
out = ex2.tick({MINT: prow(0.59)}, "n1", now=T0 + 5)
ok(out == [(MINT, "skip", "crash -41%")] and eb2.free_cash() == 100.0, "crash skip releases cash")
ex2.create_order(MINT, 12.0, 80, row0, scanner_liq=50000, now=T0)
ok(ex2.tick({MINT: prow(1.0)}, "n1", now=T0 + 1801) == [(MINT, "skip", "no dip")] and eb2.free_cash() == 100.0, "30 min -> no dip, cash released")
ex2.create_order(MINT, 12.0, 80, row0, scanner_liq=50000, now=T0)
ex2.cancel_all("night over")
ok(eb2.free_cash() == 100.0 and not eb2.orders_waiting(), "cancel releases reserved cash")
print("ALL OK")


# ================= step 3: risk, session =================
from dip.risk import Risk, Session, equity, night_pnl, tightened_stop, window_state  # noqa: E402
from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

R0 = 1_000_000.0


def rbook(cash_size=20.0, entry=1.0, creator_bal=100.0):
    b = DipBook(":memory:")
    b.night_reset("n1", 100.0)
    pid = b.open_position(MINT, entry, (cash_size - fee_usd(cash_size)) / entry, cash_size, fee_usd(cash_size),
                          entry * 0.75, night="n1", symbol="TST", creator_balance=creator_bal, now=R0)
    return b, pid


def rrow(price, liq=30000.0):
    return {"price_usd": price, "liq_usd": liq}


def rpos(b):
    return dict(b.db.execute("SELECT * FROM positions ORDER BY id DESC LIMIT 1").fetchone())


b, pid = rbook()
rk = Risk(b, CFG, clock=lambda: R0 + 5)
ok(rk.poll({MINT: rrow(0.751)}, R0 + 5) == [] and len(b.open_positions()) == 1, "-24.9% holds")
out = rk.poll({MINT: rrow(0.7490)}, R0 + 10)
p = rpos(b)
ok(out == [(MINT, "stop")] and p["status"] == "closed" and p["exit_reason"] == "stop", "-25.1% = stop")
gross = p["qty_initial"] * 0.749
ok(abs(p["proceeds_usd"] - (gross - fee_usd(gross))) < 1e-9, "paper exit = price that fired minus fee")

b, pid = rbook()
rk = Risk(b, CFG, clock=lambda: R0)
ok(rk.poll({MINT: rrow(1.04)}, R0 + 3 * 3600 - 1) == [], "just under 3h holds")
ok(rk.poll({MINT: rrow(1.04)}, R0 + 3 * 3600) == [(MINT, "dead money")], "3h at +4% = dead money")
b, pid = rbook()
rk = Risk(b, CFG, clock=lambda: R0)
ok(rk.poll({MINT: rrow(1.06)}, R0 + 3 * 3600) == [], "3h at +6% is not dead money")

# stop only tightens
b, pid = rbook()
loose = json.loads(json.dumps(CFG))
loose["risk"]["stop_pct"] = 40
ok(abs(tightened_stop(rpos(b), loose) - 0.75) < 1e-12, "looser setting never moves an open stop down")
Risk(b, loose).poll({MINT: rrow(0.9)}, R0 + 5)
ok(abs(rpos(b)["stop_line"] - 0.75) < 1e-12, "open stop stays at 0.75 after a looser poll")
tight = json.loads(json.dumps(CFG))
tight["risk"]["stop_pct"] = 10
ok(abs(tightened_stop(rpos(b), tight) - 0.90) < 1e-12, "a tighter setting moves the stop up")
Risk(b, tight).poll({MINT: rrow(0.95)}, R0 + 5)
ok(abs(rpos(b)["stop_line"] - 0.90) < 1e-12, "tightened stop is persisted")

# pulled
b, pid = rbook()
rk = Risk(b, CFG)
ok(rk.poll({MINT: rrow(0.95, 30000)}, R0 + 5) == [], "healthy poll stores liq")
ok(rk.poll({MINT: rrow(0.95, 5000)}, R0 + 10) == [(MINT, "pulled")], "liq down >80% in one poll = pulled")
ok(abs(rpos(b)["last_price"] - 0.95) < 1e-12 and rpos(b)["exit_reason"] == "pulled", "pulled exits at that poll's price")
b, pid = rbook()
ok(Risk(b, CFG).poll({MINT: rrow(0.95, 0)}, R0 + 5) == [(MINT, "pulled")], "liq 0 = pulled")

# dev dumping, every 60 s
b, pid = rbook(creator_bal=100.0)
bal = {"v": 60.0}
rk = Risk(b, CFG, creator_balance_fn=lambda m: bal["v"])
ok(rk.poll({MINT: rrow(1.0)}, R0 + 5) == [], "creator down 40% is fine")
bal["v"] = 40.0
ok(rk.poll({MINT: rrow(1.0)}, R0 + 30) == [], "dev check only every 60 s")
ok(rk.poll({MINT: rrow(1.0)}, R0 + 66) == [(MINT, "dev dumping")], "creator down >50% = dev dumping")

# warns, every 60 s
b, pid = rbook()
w = {"v": None}
rk = Risk(b, CFG, warns_fn=lambda m: w["v"])
rk.poll({MINT: rrow(1.0)}, R0 + 5)
w["v"] = "pausable"
ok(rk.poll({MINT: rrow(1.0)}, R0 + 20) == [], "warns checked only every 60 s")
ok(rk.poll({MINT: rrow(1.0)}, R0 + 70) == [(MINT, "warns")], "blocking warns = warns exit")

# no price: 12 polls, then Jupiter quote or proceeds 0
b, pid = rbook()
rk = Risk(b, CFG, quote_fn=lambda m, q: None)
for i in range(11):
    assert rk.poll({}, R0 + i) == []
ok(len(b.open_positions()) == 1 and rpos(b)["no_price_polls"] == 11, "11 polls without a price keep the position")
cash_before = b.cash()
ok(rk.poll({}, R0 + 12) == [(MINT, "no price")], "12th poll without a price exits")
p = rpos(b)
ok(p["proceeds_usd"] == 0 and p["exit_reason"] == "no price" and b.cash() == cash_before, "no route -> proceeds 0")
ok(any(e["note"] == "no price" and "no route" in (e["data"] or "") for e in b.recent_events()), "recorded as no route")
b, pid = rbook()
rk = Risk(b, CFG, quote_fn=lambda m, q: 10.0)
for i in range(12):
    rk.poll({}, R0 + i)
p = rpos(b)
ok(abs(p["proceeds_usd"] - (10.0 - fee_usd(10.0))) < 1e-9, "routable no-price exit uses the quote minus fee")
b, pid = rbook()
rk = Risk(b, CFG, quote_fn=lambda m, q: None)
for i in range(6):
    rk.poll({}, R0 + i)
rk.poll({MINT: rrow(1.0)}, R0 + 7)
ok(rpos(b)["no_price_polls"] == 0, "a price resets the no-price counter")
from dip.risk import jupiter_sell_quote_usd  # noqa: E402
ok(jupiter_sell_quote_usd(MINT, 10, quote=lambda *a: {"outAmount": "2500000"}) == 2.5, "jupiter quote parsed to USD")
ok(jupiter_sell_quote_usd(MINT, 10, quote=lambda *a: (_ for _ in ()).throw(RuntimeError("no route"))) is None, "no route -> None")

# night loss stop blocks buys
b = DipBook(":memory:")
exn = Executor(b, CFG)
rkn = Risk(b, CFG)
b.night_reset("n1", 100.0)
b.open_position(MINT, 1.0, 40.0, 40.0, 0.0, 0.75, night="n1", now=R0)
sess = Session(b, rkn, exn, CFG)
NIGHT = datetime(2026, 11, 17, 23, 0, tzinfo=ZoneInfo(CFG["session"]["tz"])).timestamp()
st = sess.step({MINT: rrow(1.0)}, NIGHT)
ok(st["buys_allowed"] and st["night_id"] == "2026-11-17", "buys allowed inside the window")
exn.create_order("W" * 44, 5.0, 80, {"price_usd": 1}, now=NIGHT)
st = sess.step({MINT: rrow(0.2)}, NIGHT + 5)  # 40 qty marked at 0.2 = -32 on 100
ok(not st["buys_allowed"] and st["night_loss"] >= 30 and b.night()["locked"], "night P&L <= -30% stops buys")
ok(not b.orders_waiting(), "night loss stop cancels waiting orders")
ok(size_order(b.free_cash(), 80, 50000, CFG, night_loss=st["night_loss"])["reason"] == "night loss stop", "brain skips on night loss stop")
ok(abs(night_pnl(b, {MINT: 0.2}) - (-32.0)) < 1e-9 and abs(equity(b, {MINT: 0.2}) - 68.0) < 1e-9, "night pnl = realized + open marked")

# window / always_on / night reset
# the shipped config is always_on=true; the 22:00-06:30 window rules are tested with always_on off
NCFG = json.loads(json.dumps(CFG))
NCFG["session"]["always_on"] = False
tzn = ZoneInfo(CFG["session"]["tz"])
ts = lambda d, h, m=0: datetime(2026, 11, d, h, m, tzinfo=tzn).timestamp()  # noqa: E731
ok(window_state(ts(17, 21, 59), NCFG) == (False, "2026-11-16"), "21:59 outside the window")
ok(window_state(ts(17, 22), NCFG) == (True, "2026-11-17"), "22:00 opens the window")
ok(window_state(ts(18, 6, 29), NCFG) == (True, "2026-11-17"), "06:29 still the same night")
ok(window_state(ts(18, 6, 30), NCFG) == (False, "2026-11-17"), "06:30 lock_time closes it")
ao = json.loads(json.dumps(NCFG))
ao["session"]["always_on"] = True
ok(window_state(ts(18, 12), ao) == (True, "2026-11-17") and window_state(ts(18, 22), ao) == (True, "2026-11-18"),
   "always_on trades 24/7 and rolls the night at night_start")

# lock_time sells all "night over"
b = DipBook(":memory:")
exl = Executor(b, NCFG)
rkl = Risk(b, NCFG)
sess = Session(b, rkl, exl, NCFG)
sess.step({}, ts(17, 23))
M2 = "N" * 44
b.open_position(MINT, 1.0, 10.0, 10.0, 0.2, 0.75, night="2026-11-17", now=ts(17, 23))
b.open_position(M2, 2.0, 5.0, 10.0, 0.2, 1.5, night="2026-11-17", now=ts(17, 23))
exl.create_order("W" * 44, 5.0, 80, {"price_usd": 1}, now=ts(18, 5))
sess.step({MINT: rrow(1.1), M2: rrow(2.0)}, ts(18, 6, 29))
ok(len(b.open_positions()) == 2 and len(b.orders_waiting()) == 1, "nothing sold before lock_time")
st = sess.step({MINT: rrow(1.1), M2: rrow(2.0)}, ts(18, 6, 30))
reasons = [r["exit_reason"] for r in b.db.execute("SELECT exit_reason FROM positions")]
ok(reasons == ["night over", "night over"] and not b.open_positions() and not b.orders_waiting(), "lock_time cancels orders and sells all 100% night over")
ok(not st["buys_allowed"], "no buys after lock_time")
st = sess.step({}, ts(18, 22, 1))
ok(st["buys_allowed"] and b.night()["id"] == "2026-11-18" and b.night()["trades"] == 0 and b.night()["realized"] == 0.0, "night counters reset at night_start")


# ================= step 4: reconnect schedule, telegram, summary, dashboard =================
import io
import os
from contextlib import redirect_stdout
from dip import notify, report
from dip.run import Desk

# --- PumpPortal reconnect backoff is scheduled, never slept ---
class DeadClient:
    def connect(self): raise OSError("down")
    def send_json(self, m): pass
    def close(self): pass

clk = {"t": 1000.0}
slept = []
feed = S.PortalFeed(client_factory=DeadClient, clock=lambda: clk["t"], sleep=slept.append)
t0 = time.time()
feed.poll(1.0)
ok(feed.next_connect == 1001.0 and feed.attempt == 1, "failed connect schedules the next try 1 s out")
feed.poll(1.0)
ok(feed.attempt == 1 and not slept, "poll during backoff returns at once: no reconnect attempt, no sleep")
clk["t"] = 1001.5
feed.poll(1.0)
ok(feed.attempt == 2 and feed.next_connect == 1003.5 and not slept and time.time() - t0 < 0.5,
   "next try after the deadline backs off 2 s, still never sleeps")

# --- telegram off / on ---
saved = {k: os.environ.pop(k, None) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
notify._state["off_logged"] = False
buf = io.StringIO()
with redirect_stdout(buf):
    r1 = notify.send("PAPER x", post=lambda *a: (_ for _ in ()).throw(AssertionError("must not post")))
    r2 = notify.send("PAPER y")
ok(r1 is False and r2 is False and buf.getvalue().count("telegram: off") == 1, "telegram off without env, logged once")
os.environ["TELEGRAM_BOT_TOKEN"] = "123:FAKE-TOKEN-SECRET"
ok(not notify.enabled(), "token alone does not enable telegram")
os.environ["TELEGRAM_CHAT_ID"] = "-100FAKECHAT"
ok(notify.enabled(), "both env vars enable telegram")
sent = []
notify._state["last_send"] = 0.0
ok(notify.send("PAPER hi", post=lambda tok, chat, text: sent.append((tok, chat, text)), sleep=lambda s: None)
   and sent[0][2] == "PAPER hi", "telegram posts when enabled")
def boom(*a): raise OSError("https://api.telegram.org/bot123:FAKE-TOKEN-SECRET/sendMessage failed")
buf = io.StringIO()
with redirect_stdout(buf):
    r = notify.send("PAPER z", post=boom, sleep=lambda s: None)
ok(r is False and "FAKE-TOKEN-SECRET" not in buf.getvalue(), "send failure is swallowed and never logs the token")
waits = []
notify._state["last_send"] = 100.0
notify.send("PAPER a", post=lambda *a: None, sleep=waits.append, clock=lambda: 100.3)
ok(waits and abs(waits[0] - 0.7) < 1e-9, "max ~1 message per second")

# --- message formats ---
m = notify.fmt_bought("DOGE", MINT, 12.0, 12.0, 80, 17.4)
ok(m.startswith("PAPER bought DOGE $12.00 (12.0% of cash) score 80 dip -17%") and f"https://dexscreener.com/solana/{MINT}" in m,
   "bought message: symbol, size, % cash, score, dip depth, DexScreener link")
ok(notify.fmt_exit("DOGE", MINT, "take profit", 5.5, 91.0).startswith("PAPER take profit 50% DOGE $+5.50 (+91.0%)"), "take profit message")
ok(notify.fmt_exit("DOGE", MINT, "trail", 3.0, 25.0).startswith("PAPER sold (trail)"), "trail message")
ok(notify.fmt_exit("DOGE", MINT, "stop", -3.0, -25.0).startswith("PAPER stopped DOGE $-3.00 (-25.0%)"), "stop message")
ok("reason: dead money" in notify.fmt_exit(None, MINT, "dead money", -0.5, -4.0) and
   notify.fmt_exit(None, MINT, "dead money", -0.5, -4.0).startswith("PAPER exit (dead money) MMMMMM"), "other exit with reason")
ok(notify.fmt_night_loss(31.2).startswith("PAPER night loss stop") and notify.fmt_night_over(1, 2, -3.5).startswith("PAPER night over"),
   "night messages start with PAPER")

# --- summary + dashboard ---
sb = DipBook(":memory:")
sb.room_add(MINT, symbol="DOGE")
sb.room_add("K" * 44)
sb.room_close("K" * 44, "killed", "liq $14.2K")
sb.room_add("L" * 44)
sb.room_close("L" * 44, "killed", "liq $9.0K")
sb.night_reset("2026-11-17", 100.0)
oid = sb.order_create("S" * 44, 5.0, 75, wait_min=30)
sb.order_close(oid, "skipped", "no dip")
t_now = time.time()
pid = sb.open_position(MINT, 1.0, 11.0, 12.0, 1.0, 0.75, night="2026-11-17", symbol="DOGE", now=t_now - 600)
sb.record_sell(pid, 1.5, 11.0, 0.5, "trail", now=t_now - 60, close=True)
sd = report.summary_data(sb, CFG, t_now)
ok(sd["wins"] == 1 and sd["losses"] == 0 and sd["trades"] == 1 and sd["kills"][0] == ("liq", 2) and sd["top_skips"] == [("no dip", 1)],
   "summary counts trades, wins/losses, kills, skips")
txt = report.summary_text(sb, CFG, t_now)
ok(txt.startswith("PAPER daily summary") and "cash $" in txt and "equity $" in txt and "night P&L" in txt
   and "scanner kills: liq 2" in txt and "top skip reasons:" in txt, "summary text has cash, equity, night P&L, skips, kills")

# daily summary once per day at report_time, tracked in meta
tzs = ZoneInfo(CFG["session"]["tz"])
at = lambda h, mi=0: datetime(2026, 11, 18, h, mi, tzinfo=tzs).timestamp()  # noqa: E731
msgs = []
dsk = Desk(book=sb, cfg=CFG, ds=object(), rc=object(), rpc=object(), notify_fn=msgs.append)
ok(dsk.maybe_summary(at(7, 59)) is None and not msgs, "no summary before report_time")
ok(dsk.maybe_summary(at(8, 0)) and len(msgs) == 1 and msgs[0].startswith("PAPER daily summary"), "summary sent at report_time")
dsk.maybe_summary(at(9)); dsk.maybe_summary(at(20))
ok(len(msgs) == 1, "summary sent only once per day")
dsk.maybe_summary(at(8) + 86400)
ok(len(msgs) == 2, "summary sent again the next day")
dsk.maybe_summary(force=True)
ok(len(msgs) == 3, "on-demand summary always sends")

# dashboard: contents + no secrets
os.environ["HELIUS_API_KEY"] = "FAKE-HELIUS-KEY-123"
sb.event("note", MINT, "hello")
sb.save_audit(MINT, [{"n": 1, "name": "mint authority", "status": "ok", "value": "Revoked", "source": "rpc", "reason": None}],
              {"score": 80, "log": ["social: off"]}, 80, "pass", None, ts=t_now)
dash = report.build(sb, CFG, t_now)
for key in ("cash", "equity", "start_cash", "pnl_usd", "open_positions", "waiting_orders", "recent_events", "audits",
            "scanner_kills", "skip_reasons", "night", "session", "telegram", "updated_at"):
    ok(key in dash, f"dashboard has {key}")
ok(dash["telegram"] == "on" and dash["social"] == "off" and dash["audits"][0]["symbol"] == "DOGE", "dashboard telegram on, social off, audit symbol")
ok(dash["session"]["report_time"] == "08:00" and "pnl_usd" in dash["night"], "dashboard session window and night counters")
path = report.write(sb, CFG, path=Path(__file__).resolve().parent / "_dip_dash_test.json", now=t_now)
raw = Path(path).read_text()
Path(path).unlink()
for secret in ("FAKE-TOKEN-SECRET", "-100FAKECHAT", "FAKE-HELIUS-KEY-123"):
    ok(secret not in raw, f"dashboard JSON does not contain env secret {secret[:8]}...")
json.loads(raw)
for k, v in saved.items():
    os.environ.pop(k, None)
    if v is not None:
        os.environ[k] = v
os.environ.pop("HELIUS_API_KEY", None)
print("ALL OK (step 4)")
