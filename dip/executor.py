"""Executor (bot 4): dip entry, re-check, paper fill, take profit. PAPER ONLY: every fill is a SQLite row.

Pure decisions: `dip_decision`, `recheck`, `paper_fill`, `tp_step`. `Executor` is the IO shell; the
price rows (one batched DexScreener read, same as open positions), RugCheck warns and creator balance
are injected callables. Stops/time/pulled/dev exits belong to risk.py.
"""
from .fees import fee_usd


def dip_decision(order, price, row, high10, now, cfg):
    """("buy"|"wait"|"skip", reason). price/row None = no price this poll.
    Order of rules: crash, dip + sellers calm, 30 min timeout, else keep waiting."""
    e = cfg["executor"]
    if price is not None and high10:
        under = (1 - price / high10) * 100
        if price < high10 * (1 - e["crash_pct"] / 100):
            return "skip", f"crash -{under:.0f}%"
        buys, sells = (row or {}).get("buys_m5"), (row or {}).get("sells_m5")
        calm = buys is not None and sells is not None and sells <= e["flush_sell_ratio"] * buys
        if price <= high10 * (1 - e["dip_pct"] / 100) and calm:
            return "buy", None
    if now >= order["expires_at"]:
        return "skip", "no dip"
    return "wait", "dipping" if price is not None and high10 and price < high10 else "no dip yet"


def recheck(order, row, available_cash, warns, creator_balance_now, cfg):
    """Right before the paper buy. Returns a skip reason or None.
    available_cash: cash minus money reserved by OTHER waiting orders."""
    e = cfg["executor"]
    liq, price = row.get("liq_usd"), row.get("price_usd")
    s_liq = order.get("scanner_liq")
    if s_liq and liq is not None and (1 - liq / s_liq) * 100 > e["recheck_liq_drop_pct"]:
        return "liquidity dropping"
    a_price = order.get("analyst_price")
    if a_price and price is not None and (price / a_price - 1) * 100 > e["recheck_price_up_pct"]:
        return "ran before the click"
    if available_cash < order["size_usd"]:
        return "no free cash"
    if warns:
        return f"warns: {warns}"
    ref = order.get("creator_balance")
    if ref is not None:
        if creator_balance_now is None:
            return "no data: dev"
        if creator_balance_now < ref:
            return "dev sold while we waited"
    return None


def paper_fill(size_usd, price, cfg):
    """Buy fills at the DexScreener price: qty = (size - fee) / price. Returns (qty, fee, stop_line)."""
    fee = fee_usd(size_usd, cfg["fees"])
    return (size_usd - fee) / price, fee, price * (1 - cfg["brain"]["stop_pct"] / 100)


def tp_step(pos, price, cfg):
    """Take profit then trail, per poll. Returns (peak, action) with action None or
    {"sell_pct": 50|100, "reason": "take profit"|"trail", "tp_done": bool}."""
    e = cfg["executor"]
    peak = max(pos.get("peak") or 0.0, price)
    if not pos.get("tp_done"):
        if price >= pos["entry_price"] * e["tp_x"]:
            return peak, {"sell_pct": e["tp_sell_pct"], "reason": "take profit", "tp_done": True}
        return peak, None
    if price <= peak * (1 - e["trail_pct"] / 100):
        return peak, {"sell_pct": 100, "reason": "trail", "tp_done": True}
    return peak, None


class Executor:
    def __init__(self, book, cfg, warns_fn=None, creator_balance_fn=None, clock=None):
        """warns_fn(mint) -> text|None (RugCheck blocking warns); creator_balance_fn(mint) -> float|None."""
        import time
        self.book, self.cfg = book, cfg
        self.warns_fn = warns_fn or (lambda mint: None)
        self.creator_balance_fn = creator_balance_fn or (lambda mint: None)
        self.clock = clock or time.time

    def create_order(self, mint, size_usd, score, row, scanner_liq=None, creator_balance=None, symbol=None, now=None):
        """Waiting dip order; reserves `size_usd` of cash until filled, skipped or cancelled."""
        return self.book.order_create(
            mint, size_usd, score, symbol=symbol, pool=(row or {}).get("pair"),
            analyst_price=(row or {}).get("price_usd"), analyst_liq=(row or {}).get("liq_usd"),
            scanner_liq=scanner_liq, creator_balance=creator_balance,
            wait_min=self.cfg["executor"]["dip_wait_min"], now=now)

    def cancel_all(self, reason, now=None):
        """Release every waiting order (night over)."""
        ids = []
        for o in self.book.orders_waiting():
            self.book.order_close(o["id"], "cancelled", reason, now=now)
            self.book.event("order_cancelled", o["mint"], reason)
            ids.append(o["id"])
        return ids

    def high10(self, mint, price, now):
        since = now - self.cfg["executor"]["dip_window_min"] * 60
        h = self.book.high_since(mint, since)
        return max(h or 0.0, price or 0.0) or None

    def tick(self, rows, night_id=None, now=None):
        """One poll. rows: {mint: DexScreener row}. Stores prices for waiting orders and open positions,
        then runs dip entries and take profit. Returns [(mint, action, detail)]."""
        now = now or self.clock()
        out = []
        watch = {o["mint"] for o in self.book.orders_waiting()} | {p["mint"] for p in self.book.open_positions()}
        for m in watch:
            r = rows.get(m)
            if r and r.get("price_usd"):
                self.book.add_price(m, r, now)
        for o in self.book.orders_waiting():
            out += self._order(o, rows.get(o["mint"]), night_id, now)
        for p in self.book.open_positions():
            out += self._position(p, rows.get(p["mint"]), now)
        return out

    def _order(self, o, row, night_id, now):
        price = (row or {}).get("price_usd")
        h10 = self.high10(o["mint"], price, now)
        act, why = dip_decision(o, price, row, h10, now, self.cfg)
        if act == "wait":
            return []
        if act == "skip":
            self.book.order_close(o["id"], "skipped", why, now=now)
            self.book.event("order_skipped", o["mint"], why)
            return [(o["mint"], "skip", why)]
        if self.book.has_position(o["mint"], night_id):  # one position per mint per night, never average down
            self.book.order_close(o["id"], "skipped", "already in position", now=now)
            return [(o["mint"], "skip", "already in position")]
        available = self.book.cash() - (self.book.reserved() - o["reserved_usd"])
        why = recheck(o, row, available, self.warns_fn(o["mint"]), self.creator_balance_fn(o["mint"]), self.cfg)
        if why:
            self.book.order_close(o["id"], "skipped", why, now=now)
            self.book.event("order_skipped", o["mint"], why)
            return [(o["mint"], "skip", why)]
        qty, fee, stop = paper_fill(o["size_usd"], price, self.cfg)
        self.book.order_close(o["id"], "filled", "dip", now=now)  # release the reservation, then spend the cash
        pid = self.book.open_position(o["mint"], price, qty, o["size_usd"], fee, stop, night=night_id,
                                      symbol=o.get("symbol"), pool=o.get("pool"),
                                      creator_balance=o.get("creator_balance"), scanner_liq=o.get("scanner_liq"), now=now)
        self.book.night_bump_trades()
        self.book.event("paper_buy", o["mint"], f"${o['size_usd']:.2f} @ {price}", position=pid)
        return [(o["mint"], "buy", pid)]

    def _position(self, p, row, now):
        price = (row or {}).get("price_usd")
        if not price:
            return []
        peak, act = tp_step(p, price, self.cfg)
        fields = {"peak": peak, "last_price": price}
        out = []
        if act:
            qty = p["qty"] * act["sell_pct"] / 100
            gross = qty * price
            self.book.record_sell(p["id"], price, qty, fee_usd(gross, self.cfg["fees"]), act["reason"], now=now,
                                  close=act["sell_pct"] >= 100)
            fields["tp_done"] = 1
            out.append((p["mint"], "sell", act["reason"]))
            self.book.event("paper_sell", p["mint"], act["reason"], position=p["id"])
        self.book.position_update(p["id"], **fields)
        return out
