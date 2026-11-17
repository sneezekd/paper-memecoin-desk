"""Dashboard JSON (step 4 fleshes this out). reports/dip_dashboard.json, no secrets."""
import json
import os
import time
from pathlib import Path

from .notify import enabled as telegram_enabled, fmt_summary
from .risk import equity, night_pnl, window_state

OUT = Path(__file__).resolve().parent.parent / "reports" / "dip_dashboard.json"


def summary_data(book, cfg, now=None):
    """Numbers for the daily summary (last 24 h of closed trades)."""
    now = now or time.time()
    since = now - 86400
    closed = [dict(r) for r in book.db.execute(
        "SELECT pnl_usd FROM positions WHERE status='closed' AND closed_at>=?", (since,))]
    buys = book.db.execute("SELECT COUNT(*) c FROM trades WHERE side='buy' AND ts>=?", (since,)).fetchone()["c"]
    top = lambda d, n: sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:n]  # noqa: E731
    return {"cash": book.cash(), "equity": equity(book), "night_pnl": night_pnl(book), "trades": buys,
            "wins": sum(1 for c in closed if (c["pnl_usd"] or 0) > 0),
            "losses": sum(1 for c in closed if (c["pnl_usd"] or 0) <= 0),
            "top_skips": top(book.skip_counts(), 5), "kills": top(book.kill_counts(), 5)}


def summary_text(book, cfg, now=None):
    return fmt_summary(summary_data(book, cfg, now))


def _event(e):
    return {"ts": e["ts"], "kind": e["kind"], "mint": e["mint"], "note": e["note"]}


def _audit(book, a):
    room = book.room_get(a["mint"]) or {}
    return {**a, "symbol": room.get("symbol") or room.get("name")}


def build(book, cfg, now=None):
    now = now or time.time()
    start = float(book.meta_get("start_cash", cfg["brain"]["paper_start_cash"]))
    eq = equity(book)
    opens = []
    for p in book.open_positions():
        opens.append({"mint": p["mint"], "symbol": p["symbol"], "entry": p["entry_price"], "mark": p["last_price"],
                      "peak": p["peak"], "stop_line": p["stop_line"], "tp_done": bool(p["tp_done"]),
                      "age_min": round((now - p["opened_at"]) / 60, 1), "qty": p["qty"], "cost_usd": p["cost_usd"]})
    waiting = []
    for o in book.orders_waiting():
        last = book.last_price(o["mint"])
        h10 = book.high_since(o["mint"], now - cfg["executor"]["dip_window_min"] * 60)
        cur = last["price"] if last else None
        waiting.append({"mint": o["mint"], "symbol": o["symbol"], "size_usd": o["size_usd"], "high10": h10, "current": cur,
                        "depth_pct": round((1 - cur / h10) * 100, 2) if cur and h10 else None,
                        "minutes_left": round(max(0, o["expires_at"] - now) / 60, 1)})
    return {
        "mode": "PAPER ONLY", "updated_at": now,
        "cash": round(book.cash(), 4), "reserved": round(book.reserved(), 4), "equity": round(eq, 4),
        "start_cash": start, "pnl_usd": round(eq - start, 4), "pnl_pct": round((eq - start) / start * 100, 3) if start else 0.0,
        "open_positions": opens, "waiting_orders": waiting, "recent_events": [_event(e) for e in book.recent_events(30)],
        "audits": [_audit(book, a) for a in book.recent_audits(50)], "scanner_kills": book.kill_counts(), "skip_reasons": book.skip_counts(),
        "night": {**book.night(), "pnl_usd": round(night_pnl(book), 4),
                  "loss_stop_pct": cfg["risk"]["night_loss_stop_pct"], "max_trades": cfg["brain"]["max_trades_night"]},
        "session": {"tz": cfg["session"]["tz"], "night_start": cfg["session"]["night_start"],
                    "lock_time": cfg["session"]["lock_time"], "report_time": cfg["session"]["report_time"],
                    "always_on": bool(cfg["session"].get("always_on")), "in_window": window_state(now, cfg)[0]},
        "telegram": "on" if telegram_enabled() else "off", "social": "off",
    }


def write(book, cfg, path=None, now=None):
    out = Path(path or OUT)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(build(book, cfg, now), indent=1, default=str) + "\n")
    os.replace(tmp, out)
    return out
