"""Read-only snapshot of the copy book for the public dashboard."""
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASH = ROOT / "reports" / "copy_dashboard.json"


def write_dashboard(book, path=None):
    db = book.db
    start = float(book.meta("starting_balance") or book.cfg["bankroll"]["starting_balance_usd"])
    equity = book.equity()
    opens = []
    for p in db.execute("SELECT * FROM positions WHERE status='open' ORDER BY opened_at"):
        mark = p["last_mark"] or p["entry_mark"]
        fee = float(book.cfg["costs"]["network_fee_usd_per_tx"])
        from .engine import net_value
        mv = net_value(p["qty"], mark, p["exit_cost_frac"] or 0.03, fee)
        opens.append({
            "symbol": p["symbol"], "mint": p["mint"], "leader": p["leader"], "leader_label": p["leader_label"],
            "size_usd": round(p["size_usd"], 4), "entry_fill": p["entry_fill"], "leader_price": p["leader_price"],
            "slippage_vs_leader_pct": p["slippage_vs_leader_pct"], "last_mark": mark, "stop_mark": p["stop_mark"],
            "half_sold": bool(p["half_sold"]), "unrealized_pnl_usd": round(mv - p["cost_usd"], 4),
            "detection_latency_s": p["detection_latency_s"], "opened_at": p["opened_at"],
        })
    closed = []
    for p in db.execute("SELECT * FROM positions WHERE status='closed' ORDER BY closed_at DESC LIMIT 40"):
        closed.append({
            "symbol": p["symbol"], "mint": p["mint"], "leader": p["leader"], "leader_label": p["leader_label"],
            "size_usd": p["size_usd"], "entry_fill": p["entry_fill"], "leader_price": p["leader_price"],
            "slippage_vs_leader_pct": p["slippage_vs_leader_pct"], "exit_reason": p["exit_reason"],
            "pnl_usd": p["pnl_usd"], "pnl_pct": p["pnl_pct"], "detection_latency_s": p["detection_latency_s"],
            "opened_at": p["opened_at"], "closed_at": p["closed_at"],
        })
    recent = []
    for e in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 30"):
        recent.append({
            "ts": e["ts"], "wallet": e["wallet"], "mint": e["mint"], "side": e["side"],
            "latency_s": e["latency_s"], "slippage_vs_leader_pct": e["slippage_vs_leader_pct"], "note": e["note"],
        })
    wins = sum(1 for p in closed if (p["pnl_usd"] or 0) > 0)
    losses = sum(1 for p in closed if (p["pnl_usd"] or 0) <= 0)
    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime()),
        "generated_ts": time.time(),
        "mode": "PAPER ONLY",
        "balance": {
            "starting_usd": start, "cash_usd": round(book.cash, 4), "equity_usd": round(equity, 4),
            "total_pnl_usd": round(equity - start, 4),
            "total_pnl_pct": round((equity / start - 1) * 100, 2) if start else 0,
        },
        "open_positions": opens,
        "closed_trades": closed,
        "wins": wins,
        "losses": losses,
        "recent_events": recent,
        "rules": {
            "stop_net_pct": book.cfg["risk"]["hard_stop_net_pct"],
            "take_profit_net_pct": book.cfg["risk"]["take_profit_net_pct"],
            "take_profit_fraction": book.cfg["risk"]["take_profit_fraction"],
            "trail_distance_from_peak_pct": book.cfg["risk"]["trail_distance_from_peak_pct"],
            "leader_sell_fraction": book.cfg["risk"]["leader_sell_fraction"],
        },
    }
    dest = Path(path) if path else DASH
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=1, default=str))
    tmp.replace(dest)
    return out
