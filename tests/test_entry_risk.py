"""Offline tests for chase/flush, fill cap, liquidity-drop exits, and the daily brake.

No network and no production database. Existing stop and trail thresholds stay put.
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import desk.engine as E


def ok(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("ok:", msg)


cfg = E.load_config()
entry = cfg["entry"]
assert cfg["risk"]["hard_stop_net_pct"] == -8.0
assert cfg["risk"]["trail_activate_net_pct"] == 12.0
assert cfg["risk"]["trail_distance_from_peak_pct"] == 8.0
assert cfg["gates"]["min_liquidity_usd"] == 25000
assert cfg["gates"]["max_round_trip_loss_pct"] == 10
assert entry["max_chase_5m_pct"] == 25.0
assert entry["flush_dump_pct"] == 25.0
assert entry["flush_bounce_pct"] == 25.0
assert entry["flush_window_minutes"] == 10
assert entry["max_fill_worse_than_mark_pct"] == 3.0
assert cfg["risk"]["liq_drop_from_entry_pct"] == 30.0
assert cfg["brake"]["max_day_loss_pct"] == -15.0
assert cfg["brake"]["max_single_loss_pct"] == -40.0

now_ts = 1_000_000.0
calm = [
    {"ts": now_ts - 600, "open": 1.0, "high": 1.04, "low": 0.98, "close": 1.01},
    {"ts": now_ts - 300, "open": 1.01, "high": 1.06, "low": 0.99, "close": 1.03},
]
flush = [
    {"ts": now_ts - 600, "open": 1.0, "high": 1.05, "low": 0.70, "close": 0.72},
    {"ts": now_ts - 300, "open": 0.72, "high": 1.05, "low": 0.70, "close": 1.02},
]

v = E.chase_flush_verdict(30, calm, now_ts, entry)
ok(v and v[0] == "chase_5m" and v[2] is False, f"5m chase skips, got {v}")
v = E.chase_flush_verdict(5, calm, now_ts, entry)
ok(v is None, f"calm 5m path passes, got {v}")
v = E.chase_flush_verdict(5, flush, now_ts, entry)
ok(v and v[0] == "flush_bounce" and v[2] is False, f"dump then bounce skips, got {v}")
v = E.chase_flush_verdict(None, None, now_ts, entry)
ok(v and v[0] == "chase_5m" and v[2] is True, f"missing 5m change fails closed, got {v}")
v = E.chase_flush_verdict(5, [calm[-1]], now_ts, entry)
ok(v and v[0] == "flush_bounce" and v[2] is True, f"one candle fails closed, got {v}")
ok(E.path_has_dump_then_bounce([1.0, 1.4, 0.7, 1.0], 25, 25), "in-candle V is a flush")
ok(not E.path_has_dump_then_bounce([1.0, 1.1, 1.2], 25, 25), "a grind up is not a flush")

ok(E.fill_vs_mark_pct(1.04, 1.0) > 3, "4% worse than the mark exceeds the 3% cap")
ok(E.fill_vs_mark_pct(1.02, 1.0) < 3, "2% worse than the mark is inside the cap")
ok(E.fill_vs_mark_pct(1.0, None) is None, "missing mark fails closed")
ok(E.round_trip_loss_pct(100, 88) > 10, "12% round trip exceeds the existing 10% cap")
ok(E.round_trip_loss_pct(100, 95) < 10, "5% round trip is inside the existing cap")

ok(E.liq_drop_reason(100000, 69000, 30, 25000), "31% liquidity drop exits")
ok(E.liq_drop_reason(100000, 70000, 30, 25000) is None, "exactly 30% does not exit")
ok(E.liq_drop_reason(30000, 20000, 30, 25000), "liquidity under the floor exits")
ok(E.liq_drop_reason(100000, None, 30, 25000) is None, "unknown liquidity does not invent an exit")

# manage loop: liquidity drop closes, and does not lower the stored stop
tmp = Path(tempfile.mkdtemp())
E.DB = tmp / "t.db"
E.EVENTS = tmp / "e.jsonl"
d = E.Desk(verbose=False)
addr = "MintLiqDrop"
stop = 0.009
d.log.db.execute(
    "INSERT INTO positions(address,symbol,status,opened_at,score,size_usd,decimals,qty,entry_mark,entry_fill,"
    "entry_liq,stop_mark,initial_stop_mark,trail_active,peak_mark,last_mark,exit_cost_frac) "
    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (addr, "LIQ", "open", time.time(), 70, 10.0, 6, 1000.0, 0.01, 0.0102, 100000.0,
     stop, stop, 0, 0.01, 0.01, 0.03),
)
d.log.db.commit()


def _offline(*_a, **_k):
    raise RuntimeError("offline")


E.S.ds_best_pairs = lambda addresses, allowed: ({addr: {"price_usd": 0.0105, "liq_usd": 60000.0}}, [])
E.S.sol_price_usd = _offline
E.S.jupiter_quote = _offline
n, _ = d.risk.manage()
row = d.log.db.execute("SELECT status, exit_reason, stop_mark FROM positions WHERE address=?", (addr,)).fetchone()
ok(n == 1 and row["status"] == "closed" and row["exit_reason"] == "liq_drop", f"liq drop exit, got {tuple(row)}")
ok(abs(row["stop_mark"] - stop) < 1e-12, "stored stop was not lowered")

# a modest dip in liquidity leaves the position and its stop alone
addr2 = "MintLiqOk"
d.log.db.execute(
    "INSERT INTO positions(address,symbol,status,opened_at,score,size_usd,decimals,qty,entry_mark,entry_fill,"
    "entry_liq,stop_mark,initial_stop_mark,trail_active,peak_mark,last_mark,exit_cost_frac) "
    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (addr2, "OK", "open", time.time(), 70, 10.0, 6, 1000.0, 0.01, 0.0102, 100000.0,
     stop, stop, 0, 0.01, 0.01, 0.03),
)
d.log.db.commit()
E.S.ds_best_pairs = lambda addresses, allowed: ({addr2: {"price_usd": 0.0105, "liq_usd": 80000.0}}, [])
n, _ = d.risk.manage()
row = d.log.db.execute("SELECT status, stop_mark FROM positions WHERE address=?", (addr2,)).fetchone()
ok(n == 0 and row["status"] == "open" and abs(row["stop_mark"] - stop) < 1e-12, "20% liquidity dip holds")

# daily brake: -20% vs start-of-day equity, then it stays on after a recovery
day, day_start, day_end = E.trading_day(cfg=cfg)
tmp_b = Path(tempfile.mkdtemp())
E.DB = tmp_b / "t.db"
E.EVENTS = tmp_b / "e.jsonl"
b = E.Desk(verbose=False)
b.log.db.execute("INSERT INTO equity(ts,cash,open_value,equity,n_open) VALUES(?,?,?,?,?)",
                 (day_start - 30, 100, 0, 100, 0))
b.log.set_meta("cash", "80")
first = E.sync_entry_pause(b.log, b.L, cfg)
ok(first["paused"] and first["day_pnl_pct"] <= -15, f"day drawdown pauses entries, got {first}")
b.log.set_meta("cash", "100")
second = E.sync_entry_pause(b.log, b.L, cfg)
ok(second["paused"] and second["reason"] == first["reason"], "pause lasts the rest of the local day")
events = E.EVENTS.read_text().count("entry_paused")
ok(events == 1, f"pause is logged once, got {events}")
healed = E.entry_pause_view(b.log.db, cfg, 100.0, now_ts=day_end + 60,
                            meta_day=first["day"], meta_reason=first["reason"])
ok(not healed["paused"], "the next local day can enter again")

# a single -41% close pauses even when day equity is flat
tmp2 = Path(tempfile.mkdtemp())
E.DB = tmp2 / "t.db"
E.EVENTS = tmp2 / "e.jsonl"
d2 = E.Desk(verbose=False)
d2.log.db.execute(
    "INSERT INTO positions(address,symbol,status,opened_at,closed_at,pnl_pct,size_usd) VALUES(?,?,?,?,?,?,?)",
    ("MintDump", "DUMP", "closed", day_start + 10, day_start + 20, -41.0, 4.0),
)
d2.log.db.commit()
third = E.sync_entry_pause(d2.log, d2.L, cfg, now_ts=day_start + 30)
ok(third["paused"] and "DUMP" in third["reason"], f"single -41% close pauses, got {third}")
open_stop = d.log.db.execute("SELECT stop_mark FROM positions WHERE address=?", (addr2,)).fetchone()
ok(abs(open_stop["stop_mark"] - stop) < 1e-12, "the brake does not touch an open stop")

print("ENTRY RISK TESTS PASSED")
