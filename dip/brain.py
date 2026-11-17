"""Brain (bot 3): position sizing. PAPER ONLY. `size_order` is pure; `Brain` is the thin book shell."""


def night_loss_pct(start_equity, realized, open_pnl=0.0):
    """Marked night P&L as a loss % of night-start equity (positive = losing)."""
    if not start_equity:
        return 0.0
    return -(realized + open_pnl) / start_equity * 100


def size_order(free_cash, score, pool_liq, cfg, trades_night=0, night_loss=0.0, has_position=False,
               shill=False):
    """Returns {size, capHit, reason}. size 0 with a reason = skip.
    free_cash: cash minus money reserved by waiting dip orders. night_loss: loss % tonight (>=0).
    capHit: "pool" | "ticket" | None (the cap that bound the size)."""
    b = cfg["brain"]
    skip = lambda why: {"size": 0.0, "capHit": None, "reason": why}  # noqa: E731
    if has_position:
        return skip("already in position")
    if night_loss >= cfg["risk"]["night_loss_stop_pct"]:
        return skip("night loss stop")
    if trades_night >= b["max_trades_night"]:
        return skip("max trades")
    raw = max(0.0, free_cash) * b["risk_per_trade_pct"] / b["stop_pct"]
    if score < b["full_size_score"]:
        raw *= b["below_full_pct"] / 100
    if shill and cfg["social"]["enabled"]:
        raw *= cfg["social"]["shill_cut"]
    size, cap = raw, None
    pool_cap = (pool_liq or 0) * b["max_pool_pct"] / 100
    if pool_cap and size > pool_cap:
        size, cap = pool_cap, "pool"
    if size > b["max_ticket_usd"]:
        size, cap = b["max_ticket_usd"], "ticket"
    size = round(size, 2)
    if size < b["min_ticket_usd"]:
        return skip("under $2")
    return {"size": size, "capHit": cap, "reason": None}


class Brain:
    def __init__(self, book, cfg):
        self.book, self.cfg = book, cfg

    def size(self, mint, score, pool_liq, night_id, open_pnl=0.0, shill=False):
        n = self.book.night()
        loss = night_loss_pct(n.get("start_equity"), float(n.get("realized") or 0), open_pnl)
        waiting = any(o["mint"] == mint for o in self.book.orders_waiting())
        held = waiting or self.book.has_position(mint, night_id)  # one position per mint per night
        return size_order(self.book.free_cash(), score, pool_liq, self.cfg, int(n.get("trades") or 0), loss, held, shill)
