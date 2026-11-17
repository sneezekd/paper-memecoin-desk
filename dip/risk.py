"""Risk (bot 5): exits and session handling. PAPER ONLY: every exit is a SQLite row.

Pure helpers: `tightened_stop`, `window_state`, `equity`, `night_pnl`. `Risk.poll` is the per-poll
exit pass (spec order: stop, dead money, pulled, dev, warns, no price); creator balance, RugCheck
warns, the Jupiter quote and the clock are injected. `Session` owns the night window and counters.
"""
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .brain import night_loss_pct
from .fees import fee_usd
from .notify import fmt_exit, fmt_night_loss, fmt_night_over

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PUMP_DECIMALS = 6  # pump.fun tokens all mint with 6 decimals


def tightened_stop(pos, cfg):
    """Stop line = entry * (1 - stop_pct). It only ever tightens: a looser setting never lowers it."""
    line = pos["entry_price"] * (1 - cfg["risk"]["stop_pct"] / 100)
    return max(pos.get("stop_line") or 0.0, line)


def mark(pos):
    return pos.get("last_price") or pos.get("entry_price") or 0.0


def equity(book, prices=None):
    """cash + open positions marked at the latest price (waiting orders still count as cash)."""
    prices = prices or {}
    return book.cash() + sum(p["qty"] * (prices.get(p["mint"]) or mark(p)) for p in book.open_positions())


def night_pnl(book, prices=None):
    """Night P&L = equity now - night-start equity (realized + partial proceeds + open marked)."""
    n = book.night()
    if n.get("start_equity") is None:
        return 0.0
    return equity(book, prices) - float(n["start_equity"])


def jupiter_sell_quote_usd(mint, qty, quote=None):
    """USD out for selling qty via Jupiter, or None if it does not route. Never raises."""
    try:
        if quote is None:
            from desk.sources import jupiter_quote as quote
        d = quote(mint, USDC, int(qty * 10 ** PUMP_DECIMALS))
        out = float(d.get("outAmount") or 0) / 1e6
        return out if out > 0 else None
    except Exception:
        return None


class Risk:
    def __init__(self, book, cfg, creator_balance_fn=None, warns_fn=None, quote_fn=None, clock=None, notify=None):
        """creator_balance_fn(mint) -> float|None; warns_fn(mint) -> text|None;
        quote_fn(mint, qty) -> USD out | None; notify(text) is an optional hook."""
        self.book, self.cfg = book, cfg
        self.creator_balance_fn = creator_balance_fn or (lambda m: None)
        self.warns_fn = warns_fn or (lambda m: None)
        self.quote_fn = quote_fn or jupiter_sell_quote_usd
        self.clock = clock or time.time
        self.notify = notify or (lambda text: None)
        self._dev_at, self._warn_at = {}, {}

    def sell_all(self, p, price, reason, now):
        """Paper exit of the whole remaining qty at `price`, minus the fee. Returns proceeds."""
        gross = p["qty"] * price
        proceeds = self.book.record_sell(p["id"], price, p["qty"], fee_usd(gross, self.cfg["fees"]), reason,
                                         now=now, close=True)
        self.book.event("paper_sell", p["mint"], reason, position=p["id"])
        pos = self.book.db.execute("SELECT pnl_usd, pnl_pct FROM positions WHERE id=?", (p["id"],)).fetchone()
        self.notify(fmt_exit(p.get("symbol"), p["mint"], reason, pos["pnl_usd"] or 0.0, pos["pnl_pct"] or 0.0))
        return proceeds

    def _no_price_exit(self, p, now):
        usd = self.quote_fn(p["mint"], p["qty"])
        if usd:
            fee = fee_usd(usd, self.cfg["fees"])
            self.book.record_sell(p["id"], usd / p["qty"], p["qty"], fee, "no price", now=now, close=True)
            note = "jupiter quote"
        else:
            self.book.record_sell(p["id"], 0.0, p["qty"], 0.0, "no price", now=now, close=True, proceeds=0.0)
            note = "no route"
        self.book.event("paper_sell", p["mint"], "no price", position=p["id"], detail=note)
        pos = self.book.db.execute("SELECT pnl_usd, pnl_pct FROM positions WHERE id=?", (p["id"],)).fetchone()
        self.notify(fmt_exit(p.get("symbol"), p["mint"], f"no price, {note}", pos["pnl_usd"] or 0.0, pos["pnl_pct"] or 0.0))
        return note

    def poll(self, rows, now=None):
        """One risk pass over every open position. rows: {mint: DexScreener row}.
        Returns [(mint, reason)] for exits fired."""
        now = now or self.clock()
        r = self.cfg["risk"]
        out = []
        for p in self.book.open_positions():
            row = rows.get(p["mint"]) or {}
            price, liq = row.get("price_usd"), row.get("liq_usd")
            if not price:
                n = int(p.get("no_price_polls") or 0) + 1
                if n >= r["no_price_polls"]:
                    self._no_price_exit(p, now)
                    out.append((p["mint"], "no price"))
                else:
                    self.book.position_update(p["id"], no_price_polls=n)
                continue
            fields = {"no_price_polls": 0, "last_price": price}
            if liq is not None:
                fields["last_liq"] = liq
            line = tightened_stop(p, self.cfg)
            if line != p.get("stop_line"):
                fields["stop_line"] = line
            reason = None
            if price <= line:
                reason = "stop"
            elif now - p["opened_at"] >= r["timeout_h"] * 3600 and price < p["entry_price"] * (1 + r["timeout_min_gain_pct"] / 100):
                reason = "dead money"
            elif liq is not None and (liq <= 0 or (p.get("last_liq") and liq < p["last_liq"] * (1 - r["pulled_drop_pct"] / 100))):
                reason = "pulled"
            elif now - self._dev_at.get(p["id"], 0) >= r["dev_check_s"]:
                self._dev_at[p["id"]] = now
                ref, bal = p.get("creator_balance"), self.creator_balance_fn(p["mint"])
                if ref and bal is not None and bal < ref * (1 - r["dev_dump_pct"] / 100):
                    reason = "dev dumping"
            if reason is None and now - self._warn_at.get(p["id"], 0) >= r["warn_check_s"]:
                self._warn_at[p["id"]] = now
                if self.warns_fn(p["mint"]):
                    reason = "warns"
            self.book.position_update(p["id"], **fields)
            if reason:
                self.sell_all({**p, **fields}, price, reason, now)
                out.append((p["mint"], reason))
        return out


# ---------------- session ----------------
def _hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def window_state(now, cfg):
    """(in_window, night_id). Night id = local date the night started. always_on: always in window,
    the id rolls over daily at night_start."""
    s = cfg["session"]
    t = datetime.fromtimestamp(now, ZoneInfo(s["tz"]))
    mins, start, lock = t.hour * 60 + t.minute, _hm(s["night_start"]), _hm(s["lock_time"])
    started = mins >= start
    day = t.date() if started else t.date() - timedelta(days=1)
    if s.get("always_on"):
        return True, day.isoformat()
    if start > lock:  # window crosses midnight
        return (started or mins < lock), day.isoformat()
    return (start <= mins < lock), t.date().isoformat()


class Session:
    def __init__(self, book, risk, executor, cfg, clock=None, notify=None):
        self.book, self.risk, self.executor, self.cfg = book, risk, executor, cfg
        self.clock = clock or time.time
        self.notify = notify or (lambda text: None)
        self.in_window, self.night_id = True, None

    def step(self, rows, now=None):
        """Roll the night, lock at lock_time, apply the night loss stop. Call before the entry pass.
        Returns {"in_window", "night_id", "buys_allowed", "night_loss"}."""
        now = now or self.clock()
        prices = {m: r.get("price_usd") for m, r in rows.items() if r.get("price_usd")}
        self.in_window, self.night_id = window_state(now, self.cfg)
        n = self.book.night()
        if n.get("id") != self.night_id:
            n = self.book.night_reset(self.night_id, equity(self.book, prices))
            self.book.event("night_start", None, self.night_id)
        if not self.in_window:
            self._lock(rows, now)
        pnl = night_pnl(self.book, prices)
        loss = night_loss_pct(n.get("start_equity"), pnl)
        stopped = loss >= self.cfg["risk"]["night_loss_stop_pct"]
        if stopped and not n.get("locked"):
            n["locked"] = True
            self.book.night_set(n)
            self.executor.cancel_all("night loss stop", now=now)
            self.book.event("night_loss_stop", None, f"{loss:.1f}%")
            self.notify(fmt_night_loss(loss))
        return {"in_window": self.in_window, "night_id": self.night_id,
                "buys_allowed": self.in_window and not stopped, "night_loss": loss}

    def _lock(self, rows, now):
        """Outside the window: cancel waiting orders, sell every open position 100% "night over"."""
        cancelled = self.executor.cancel_all("night over", now=now)
        sold = 0
        for p in self.book.open_positions():
            price = (rows.get(p["mint"]) or {}).get("price_usd")
            if price:  # never invent a price; without one it is retried next poll
                self.risk.sell_all(p, price, "night over", now)
                sold += 1
        if cancelled or sold:
            n = self.book.night()
            self.book.event("night_over", None, f"cancelled {len(cancelled)}, sold {sold}")
            self.notify(fmt_night_over(len(cancelled), sold, float(n.get("realized") or 0)))
