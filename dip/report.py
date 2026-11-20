"""Dashboard JSON (step 4 fleshes this out). reports/dip_dashboard.json, no secrets."""
import json
import os
import time
from pathlib import Path

from desk import outcomes as O
from .book import _kill_bucket
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


def _recent_rejections(book, limit=50):
    out = []
    for r in book.db.execute("SELECT * FROM room WHERE status IN ('killed','dropped') "
                             "ORDER BY COALESCE(updated_at,0) DESC LIMIT ?", (limit,)):
        p = book.db.execute("SELECT price, mcap, liq, chg_h1 FROM prices WHERE mint=? ORDER BY ts LIMIT 1",
                            (r["mint"],)).fetchone()
        out.append({"mint": r["mint"], "symbol": r["symbol"] or r["name"], "status": r["status"],
                    "gate": _kill_bucket(r["reason"]), "reason": r["reason"], "ts": r["updated_at"],
                    "price": p["price"] if p else None, "mcap": p["mcap"] if p else None,
                    "liq": p["liq"] if p else None, "chg_h1": p["chg_h1"] if p else None})
    return out


def _room_counts(book):
    out = {"waiting": 0, "killed": 0, "dropped": 0, "passed": 0}
    for r in book.db.execute("SELECT status, COUNT(*) c FROM room GROUP BY status"):
        out[r["status"] or "unknown"] = r["c"]
    return out


def _rules(cfg):
    sc, an, br, ex, rk = cfg["scanner"], cfg["analyst"], cfg["brain"], cfg["executor"], cfg["risk"]
    return {"min_liquidity_usd": sc["min_liquidity_usd"], "max_h1_change_pct": sc["max_h1_change_pct"],
            "max_top10_pct": sc["max_top10_pct"], "min_pool_age_min": sc["min_pool_age_min"],
            "max_wait_min": sc["max_wait_min"], "stop_pct": rk["stop_pct"], "trail_pct": ex["trail_pct"],
            "dip_entry_pct": ex["dip_pct"], "tp_x": ex["tp_x"], "min_score": an["min_score"],
            "risk_per_trade_pct": br["risk_per_trade_pct"], "always_on": bool(cfg["session"].get("always_on"))}


def build(book, cfg, now=None, refresh=False):
    """refresh=True also updates post-rejection quotes from DexScreener (throttled); write() turns it on."""
    now = now or time.time()
    start = float(book.meta_get("start_cash", cfg["brain"]["paper_start_cash"]))
    eq = equity(book)
    opens = []
    for p in book.open_positions():
        opens.append({"mint": p["mint"], "symbol": p["symbol"], "entry": p["entry_price"], "mark": p["last_price"],
                      "peak": p["peak"], "stop_line": p["stop_line"], "tp_done": bool(p["tp_done"]),
                      "age_min": round((now - p["opened_at"]) / 60, 1), "qty": p["qty"], "cost_usd": p["cost_usd"]})
    closed = [{"mint": r["mint"], "symbol": r["symbol"], "entry": r["entry_price"], "size_usd": r["cost_usd"],
               "pnl_usd": r["pnl_usd"], "pnl_pct": r["pnl_pct"], "exit_reason": r["exit_reason"],
               "opened_at": r["opened_at"], "closed_at": r["closed_at"]}
              for r in book.db.execute("SELECT * FROM positions WHERE status='closed' ORDER BY closed_at DESC LIMIT 60")]
    waiting = []
    for o in book.orders_waiting():
        last = book.last_price(o["mint"])
        h10 = book.high_since(o["mint"], now - cfg["executor"]["dip_window_min"] * 60)
        cur = last["price"] if last else None
        waiting.append({"mint": o["mint"], "symbol": o["symbol"], "size_usd": o["size_usd"], "high10": h10, "current": cur,
                        "depth_pct": round((1 - cur / h10) * 100, 2) if cur and h10 else None,
                        "minutes_left": round(max(0, o["expires_at"] - now) / 60, 1)})
    if refresh:
        oc = O.update_and_export(book.db, "dip", O.seed_dip, now=now)
    else:
        try:
            O.seed_dip(book.db, now=now)
            oc = O.export(book.db, "dip", now=now)
        except Exception as e:
            oc = {"missed_runners": [], "dodged_rugs": [], "gate_scorecard": [], "outcome_index": {},
                  "outcomes_meta": {"tracked": 0, "last_refresh_ts": None, "window_hours": O.WINDOW_H, "error": str(e)[:150]}}
    health = O.health_from_sources({"dexscreener_outcomes": oc["outcomes_meta"].get("error") or "ok",
                                    "telegram": "on" if telegram_enabled() else "off"},
                                   [oc["outcomes_meta"].get("error")])
    return {
        "mode": "PAPER ONLY", "updated_at": now, "generated_ts": now,
        "closed_trades": closed, "wins": sum(1 for c in closed if (c["pnl_usd"] or 0) > 0),
        "losses": sum(1 for c in closed if (c["pnl_usd"] or 0) <= 0),
        "recent_rejections": _recent_rejections(book), "room_counts": _room_counts(book), "rules": _rules(cfg),
        "health": health, **oc,
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
    tmp.write_text(json.dumps(build(book, cfg, now, refresh=True), indent=1, default=str) + "\n")
    os.replace(tmp, out)
    return out
