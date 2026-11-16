"""End-to-end test in a TEMP DB (does not touch data/desk.db).

Free path (default): real DexScreener/RPC/RugCheck/Jupiter; bundle + creator-rug gates use
RugCheck insiders / creatorTokens (no GMGN key). Optional --gmgn-mock still exercises the
GMGN branch with fake bundle/creator/smart-wallet data.

Then fake a price path to exercise stop never-lower + trailing exit.

  python3 tests/e2e_mock.py [mint] [--gmgn-mock]
"""
import sys, json, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import desk.engine as E, desk.sources as S

tmp = Path(tempfile.mkdtemp())
E.DB = tmp / "t.db"
E.EVENTS = tmp / "e.jsonl"

args = [a for a in sys.argv[1:] if not a.startswith("--")]
flags = {a for a in sys.argv[1:] if a.startswith("--")}
addr = args[0] if args else "4B8CdHPZ8ng1CtMP2Jk2W8wGAwJ5nJnap1deVsz2pump"  # BUTTHOLE; previously died only on GMGN fail-closed
gmgn_mock = "--gmgn-mock" in flags

d = E.Desk()
if gmgn_mock:
    class FakeG:
        available = True
        key_mode = "mock"
    S.gmgn_token_info_raw = lambda g, a: {"wallet_tags_stat": {"smart_wallets": 6}, "stat": {"dev_team_hold_rate": "0"}}
    S.gmgn_created_tokens = lambda g, w: {"tokens": [{"token_address": addr, "bundler_rate": 0.02}]}
    d.gmgn = FakeG()
    d.filter.gmgn = d.gmgn
else:
    d.gmgn = None
    d.filter.gmgn = None

print("PATH", "gmgn-mock" if gmgn_mock else "free (no GMGN)", "mint", addr)
d.log.start_run("test")
pairs, _ = S.ds_best_pairs([addr], set(d.cfg["scout"]["allowed_dex_ids"]))
if addr not in pairs:
    print("NO PAIR for", addr); sys.exit(1)
ds = pairs[addr]
ds["vol_h1"] = max(ds["vol_h1"] or 0, 60000)  # force past volume gate for test only
c = {"address": addr, "symbol": ds["symbol"], "sources": {"test"}, "ds": ds, "age_min": 100,
     "signal_price": ds["price_usd"], "signal_ts": ds["fetched_at"]}
try:
    ev = d.filter.check(c)
    print("FILTER PASS", json.dumps({k: ev[k] for k in ev if k in
          ("liq_usd", "vol_h1", "chg_h1", "lp", "top10", "dev_hold_pct", "bundled_pct",
           "bundled_source", "creator_history", "round_trip_loss_pct", "bundle_insiders")}, default=str))
    print("no_creator_history", c.get("no_creator_history"))
except E.Gate as g:
    print("FILTER KILL", g.gate, g.reason, "unverifiable" if g.unverifiable else "")
    sys.exit(0)

s = E.Analyst(d.cfg, d.log, use_smart_wallets=bool(d.gmgn)).run(c, S.gecko_pool_txns([ds["pair"]]))
print("SCORE", s)
print("weights_in_play", list(S.analyst_weights(d.cfg["analyst"]["weights"], bool(d.gmgn))))
s["passed"] = True
s["score"] = max(s["score"], 70)
orig = S.ds_best_pairs

def fresh(a, al):
    r, e = orig(a, al)
    if addr in r:
        r[addr]["vol_h1"] = max(r[addr]["vol_h1"] or 0, 60000)
    return r, e

S.ds_best_pairs = fresh
# pre-fill recheck is tight on the 10s data-age gate; give it a clean clock by refreshing signal
c["signal_price"] = c["ds"]["price_usd"]
t0 = time.time()
pid = E.Executor(d.cfg, d.log, d.L, d.filter, d.risk).run(c, s)
print("PID", pid, "cash", d.L.cash, "exec_s", round(time.time() - t0, 1))
for k in d.log.db.execute("select gate,reason from kills"):
    print("KILL", tuple(k))
if not pid:
    sys.exit(0)
p = dict(d.log.db.execute("select * from positions where id=?", (pid,)).fetchone())
print("entry", p["entry_fill"], "mark", p["entry_mark"], "stop", p["stop_mark"],
      "stop vs mark %", (p["stop_mark"] / p["entry_mark"] - 1) * 100, "half_size", p["half_size"])
e = p["entry_mark"]
for m in [e * 1.05, e * 1.20, e * 1.40, e * 1.30, e * 1.25]:
    S.ds_best_pairs = lambda a, al, m=m: ({addr: {"price_usd": m, "liq_usd": p["entry_liq"]}}, [])
    n, _ = d.risk.manage()
    q = d.log.db.execute("select status,stop_mark,trail_active,pnl_usd,exit_reason from positions where id=?",
                         (pid,)).fetchone()
    print(f"mark x{m / e:.2f} -> {tuple(q)}")
print([tuple(r) for r in d.log.db.execute("select old_stop,new_stop,why from stop_moves")])
print("cash", d.L.cash)
