"""Positions-only fast path on an empty temp database. No network, no production DB.

The fast path must not scout, and it must leave the stop / trail thresholds alone.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import desk.engine as E

tmp = Path(tempfile.mkdtemp())
E.DB = tmp / "t.db"
E.EVENTS = tmp / "e.jsonl"

d = E.Desk(verbose=False)
info = d.manage_only()
assert info == {"exits": 0, "stop_moves": 0, "open": 0}, info
assert d.log.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
risk = d.cfg["risk"]
assert risk["hard_stop_net_pct"] == -8.0
assert risk["trail_activate_net_pct"] == 12.0
assert risk["trail_distance_from_peak_pct"] == 8.0
assert d.cfg["analyst"]["min_score"] == 65
assert d.cfg["gates"]["min_liquidity_usd"] == 25000

addr = "MintFastPath111"
d.log.db.execute(
    "INSERT INTO positions(address,symbol,status,opened_at,score,size_usd,decimals,qty,entry_mark,entry_fill,"
    "entry_liq,stop_mark,initial_stop_mark,trail_active,peak_mark,last_mark,exit_cost_frac) "
    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (addr, "FAST", "open", 1, 70, 10.0, 6, 1000.0, 0.01, 0.0102, 100000.0,
     0.009, 0.009, 0, 0.01, 0.01, 0.03),
)
d.log.db.commit()


def _offline(*_a, **_k):
    raise RuntimeError("offline")


E.S.ds_best_pairs = lambda addresses, allowed: ({addr: {"price_usd": 0.008, "liq_usd": 100000.0}}, [])
E.S.sol_price_usd = _offline
E.S.jupiter_quote = _offline
fired = d.manage_only()
assert fired["exits"] == 1 and fired["open"] == 1, fired
row = d.log.db.execute("SELECT status, exit_reason FROM positions WHERE address=?", (addr,)).fetchone()
assert row["status"] == "closed" and row["exit_reason"] == E.hard_stop_reason(risk["hard_stop_net_pct"]), tuple(row)
assert row["exit_reason"] == "stop_-8pct"
assert d.log.db.execute("SELECT COUNT(*) FROM scouted").fetchone()[0] == 0
print("FAST PATH OK", info, fired)
