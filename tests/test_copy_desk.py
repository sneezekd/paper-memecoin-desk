"""Paper copy desk: swap parsing, stop math, exits, and a mock buy-to-leader-sell run.

No network and no production database.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from copy.engine import CopyBook, backoff_seconds, initial_stop, net_pct, net_value
from copy.parse import detection_latency, parse_swap
from copy.rpc import encode_frame, try_decode_frame

WALLET = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
MINT = "Mint111111111111111111111111111111111111111"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def ok(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("ok:", msg)


buy = json.loads((FIXTURES / "copy_buy.json").read_text())
sell = json.loads((FIXTURES / "copy_sell.json").read_text())
parsed = parse_swap(buy, WALLET, sol_price_usd=150.0)
ok(parsed and parsed["side"] == "buy" and parsed["first_buy"], f"buy parse {parsed}")
ok(abs(parsed["token_amount"] - 1000) < 1e-6, "buy token amount")
ok(parsed["leader_price_usd"] and parsed["leader_price_usd"] > 0, "leader price in usd")
sold = parse_swap(sell, WALLET, sol_price_usd=150.0)
ok(sold and sold["side"] == "sell" and sold["sold_fraction"] >= 0.25, f"sell parse {sold}")
ok(abs(sold["sold_fraction"] - 0.6) < 1e-9, "sold 60% of the leader position")
ok(parse_swap(buy, "not-the-wallet") is None, "other wallet is ignored")
ok(detection_latency(100, 103.5) == 3.5, "latency is detect time minus block time")
ok(detection_latency(None, 1) is None, "missing block time has no latency")

fee = 0.02
qty, cost, k = 1000.0, 10.0, 0.03
stop = initial_stop(qty, cost, k, -8.0, fee)
ok(abs(net_pct(qty, stop, k, fee, cost) - (-8.0)) < 1e-6, "stop mark nets exactly -8%")
# A higher mark must not be used to move this stop down. The function is pure.
ok(initial_stop(qty, cost, k, -8.0, fee) == stop, "stop math is stable")

frame = encode_frame(1, b'{"ok":1}', mask_key=b"abcd")
# Server frames are unmasked. Rebuild the payload check by decoding the masked client frame.
decoded = try_decode_frame(frame)
ok(decoded is not None and decoded[0] and decoded[1] == 1, "client frame decodes")
ok(decoded[2] == b'{"ok":1}', "masked payload round-trips")
ok(backoff_seconds(1, 30) == 1 and backoff_seconds(3, 30) == 4 and backoff_seconds(10, 30) == 30, "reconnect backoff")

tmp = Path(tempfile.mkdtemp())
book = CopyBook(db_path=tmp / "copy.db", events_path=tmp / "e.jsonl")
book.dash_path = tmp / "dash.json"
swap = dict(parsed)
swap["signature"] = "buySig111"
swap["latency_s"] = 1.2
leader_px = swap["leader_price_usd"]

def good_check(mint, leader_price, size, cfg):
    ok_fill = leader_price * 1.02  # 2% worse than the leader, inside the 15% cap
    return {"ok": True, "fill": ok_fill, "qty": size / ok_fill, "decimals": 6, "symbol": "COPY",
            "liq_usd": 80000, "mark": leader_price, "exit_cost_frac": 0.03}

pid = book.try_entry(swap, label="leader-a", check=good_check)
ok(pid is not None, "paper entry recorded")
pos = book.position_for(MINT)
ok(pos["leader"] == WALLET and abs(pos["slippage_vs_leader_pct"] - 2) < 0.2, "slippage vs leader stored")
ok(pos["detection_latency_s"] == 1.2, "detection latency stored")
stop_at_entry = pos["stop_mark"]
ok(book.try_entry(swap, label="leader-a", check=good_check) is None, "no second entry and no average-down")

# Price through +40% net sells half and does not lower the stop.
mark_tp = (pos["cost_usd"] * 1.40 + fee) / (pos["qty"] * (1 - 0.03))
n = book.manage({MINT: {"price_usd": mark_tp, "liq_usd": 80000}})
pos = book.position_for(MINT)
ok(pos and pos["half_sold"] == 1 and pos["status"] == "open", "sold half at +40% net")
ok(abs(pos["stop_mark"] - stop_at_entry) < 1e-9, "hard stop was not lowered after the partial")
ok(n == 0, "the partial is not a full exit")

# A 10% leader sale does not flatten us.
small = dict(sold)
small["sold_fraction"] = 0.10
small["mint"] = MINT
small["wallet"] = WALLET
ok(book.on_leader_sell(small) is False, "a 10% leader sale is not enough")
ok(book.position_for(MINT)["status"] == "open", "position still open")

# A 60% leader sale closes the rest.
big = dict(sold)
big["mint"] = MINT
big["wallet"] = WALLET
ok(book.on_leader_sell(big) is True, "leader sold 60%, we sell everything")
closed = book.db.execute("SELECT * FROM positions WHERE mint=?", (MINT,)).fetchone()
ok(closed["status"] == "closed" and closed["exit_reason"] == "leader_sell", "exit reason is leader_sell")

# Fresh book: liquidity drop and max-hold and hard stop.
book2 = CopyBook(db_path=tmp / "copy2.db", events_path=tmp / "e2.jsonl")
book2.dash_path = tmp / "dash2.json"
swap2 = dict(swap)
swap2["mint"] = "Mint222222222222222222222222222222222222222"
swap2["signature"] = "buy2"

def check2(mint, leader_price, size, cfg):
    return good_check(mint, leader_price, size, cfg)

# patch mint on the swap used by ever_traded
pid = book2.try_entry(swap2, label="leader-a", check=check2)
ok(pid, "second book entry")
p = book2.position_for(swap2["mint"])
stop2 = p["stop_mark"]
book2.manage({swap2["mint"]: {"price_usd": p["entry_mark"], "liq_usd": 10000}})
p = book2.db.execute("SELECT * FROM positions WHERE mint=?", (swap2["mint"],)).fetchone()
ok(p["status"] == "closed" and p["exit_reason"] == "liq_drop", "liquidity under the floor exits")
ok(abs(p["stop_mark"] - stop2) < 1e-9, "liq drop did not change the stop")

book3 = CopyBook(db_path=tmp / "copy3.db", events_path=tmp / "e3.jsonl")
swap3 = dict(swap)
swap3["mint"] = "Mint333333333333333333333333333333333333333"
swap3["signature"] = "buy3"
book3.try_entry(swap3, label="leader-a", check=check2)
p = book3.position_for(swap3["mint"])
book3.manage({swap3["mint"]: {"price_usd": p["stop_mark"] * 0.99, "liq_usd": 80000}})
p = book3.db.execute("SELECT * FROM positions WHERE mint=?", (swap3["mint"],)).fetchone()
ok(p["status"] == "closed" and p["exit_reason"] == "stop", "hard stop exits at the entry stop")

book4 = CopyBook(db_path=tmp / "copy4.db", events_path=tmp / "e4.jsonl")
swap4 = dict(swap)
swap4["mint"] = "Mint444444444444444444444444444444444444444"
swap4["signature"] = "buy4"
book4.try_entry(swap4, label="leader-a", check=check2, now_ts=1_000)
p = book4.position_for(swap4["mint"])
book4.manage({swap4["mint"]: {"price_usd": p["entry_mark"], "liq_usd": 80000}}, now_ts=1_000 + 86400)
p = book4.db.execute("SELECT * FROM positions WHERE mint=?", (swap4["mint"],)).fetchone()
ok(p["status"] == "closed" and p["exit_reason"] == "max_hold", "24h max hold exits")

dash = book.dashboard()
ok(dash["mode"] == "PAPER ONLY" and dash["closed_trades"], "dashboard lists the closed copy trade")
ok(dash["closed_trades"][0]["leader"] == WALLET, "dashboard keeps the leader wallet")
print("COPY DESK TESTS PASSED")
