#!/usr/bin/env python3
"""Paper dip desk runner. PAPER ONLY: no wallet, no signing, no live order.

  python3 dip/run.py listen     # long loop used by GitHub Actions
  python3 dip/run.py summary    # print (and send to Telegram if configured) the daily summary
  python3 dip/run.py dashboard  # write reports/dip_dashboard.json once
  python3 dip/run.py tick       # one offline-safe pass (CI smoke)
"""
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dip import notify, report  # noqa: E402
from dip import sources as S  # noqa: E402
from dip.book import DipBook, load_config  # noqa: E402
from dip.brain import Brain  # noqa: E402
from dip.executor import Executor  # noqa: E402
from dip.layers import run_layers  # noqa: E402
from dip.risk import Risk, Session, night_pnl  # noqa: E402
from dip.scanner import Scanner, blocking_warns  # noqa: E402
from dip.score import evaluate  # noqa: E402


def log(msg):
    print(time.strftime("%H:%M:%S", time.gmtime()), msg, flush=True)


def safe_err(e):
    """Type + short message with any API key query value redacted. Never log secrets."""
    s = str(e)[:100]
    for k in ("HELIUS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        v = os.environ.get(k)
        if v:
            s = s.replace(v, "***")
    return f"{type(e).__name__}: {s}"


def guarded(name, fn, *a, **kw):
    """Run one phase; a source error is logged and the loop continues."""
    try:
        return fn(*a, **kw)
    except Exception as e:
        log(f"{name} failed: {safe_err(e)}")
        return None


class Desk:
    def __init__(self, book=None, cfg=None, ds=None, rc=None, rpc=None, clock=time.time, notify_fn=None):
        self.cfg = cfg or load_config()
        self.book = book or DipBook(cfg=self.cfg)
        self.clock = clock
        self.notify = notify_fn or notify.send
        self.ds = ds or S.DexScreener(max_per_min=self.cfg["scanner"]["ds_max_per_min"])
        self.rc = rc or S.RugCheck()
        self.rpc = rpc or S.Rpc()
        self.data = S.LiveData(self.rpc, self.rc, self.book)
        tz = ZoneInfo(self.cfg["session"]["tz"])
        self.scanner = Scanner(self.book, self.ds, self.rc, self.rpc, self.cfg, clock=clock,
                               day_of=lambda t: datetime.fromtimestamp(t, tz).date().isoformat())
        self.executor = Executor(self.book, self.cfg, warns_fn=self._warns, creator_balance_fn=self._creator_balance,
                                 clock=clock)
        self.risk = Risk(self.book, self.cfg, creator_balance_fn=self._creator_balance, warns_fn=self._warns,
                         clock=clock, notify=self.notify)
        self.session = Session(self.book, self.risk, self.executor, self.cfg, clock=clock, notify=self.notify)
        self.brain = Brain(self.book, self.cfg)
        self.state = {"buys_allowed": False, "night_id": None, "in_window": False}
        self.ds_block_until = 0.0

    # ---- injected IO ----
    def _warns(self, mint):
        return blocking_warns(self.rc.report(mint))

    def _creator_balance(self, mint):
        c = self.book.room_get(mint)
        creator = (c or {}).get("creator")
        return self.rpc.owner_balance(creator, mint) if creator else None

    def ds_ok(self):
        return self.clock() >= self.ds_block_until

    def ds_batch(self, mints):
        """One batched DexScreener read. 429 -> back off 30 s (room and book kept); returns {} then."""
        if not mints or not self.ds_ok():
            return {}
        try:
            rows, errs = self.ds.batch(mints)
        except S.RateLimited:
            self.ds_block_until = self.clock() + self.cfg["scanner"]["ds_429_wait_s"]
            log("dexscreener 429: waiting 30 s, room kept")
            return {}
        for e in errs:
            log(f"dexscreener error: {e[:80]}")
        return rows

    # ---- phases ----
    def cycle(self, now=None):
        """Executor + risk poll (every risk.poll_s): one batched read for waiting + open mints."""
        now = now or self.clock()
        mints = [o["mint"] for o in self.book.orders_waiting()] + [p["mint"] for p in self.book.open_positions()]
        if mints and not self.ds_ok():
            return []  # rate limited: skip this poll entirely so it does not count as "no price"
        rows = self.ds_batch(mints)
        if mints and not rows and not self.ds_ok():
            return []
        self.state = self.session.step(rows, now)
        exits = self.risk.poll(rows, now)
        acts = self.executor.tick(rows, self.state["night_id"], now=now)
        for mint, act, detail in acts:
            if act == "buy":
                self.notify_buy(mint, detail, now)
            elif act == "sell":
                self.notify_sell(mint, detail)
        for mint, why in exits:
            log(f"exit {mint[:6]} {why}")
        return exits + acts

    def _last_position(self, mint):
        r = self.book.db.execute("SELECT * FROM positions WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,)).fetchone()
        return dict(r) if r else None

    def notify_buy(self, mint, pid, now):
        p = self.book.db.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
        if not p:
            return
        p = dict(p)
        o = self.book.db.execute("SELECT score FROM orders WHERE mint=? AND status='filled' ORDER BY id DESC LIMIT 1",
                                 (mint,)).fetchone()
        cash_before = self.book.cash() + p["size_usd"]
        h10 = self.book.high_since(mint, now - self.cfg["executor"]["dip_window_min"] * 60) or p["entry_price"]
        depth = max(0.0, (1 - p["entry_price"] / h10) * 100) if h10 else 0.0
        self.notify(notify.fmt_bought(p["symbol"], mint, p["size_usd"], p["size_usd"] / cash_before * 100 if cash_before else 0,
                                      o["score"] if o else "?", depth))

    def notify_sell(self, mint, reason):
        """Executor sells: take profit (half) and trail (rest)."""
        p = self._last_position(mint)
        if not p:
            return
        if reason == "take profit":  # partial: P&L of the sold slice vs its share of cost
            frac = self.cfg["executor"]["tp_sell_pct"] / 100
            pnl = (p["proceeds_usd"] or 0) - p["cost_usd"] * frac
            pct = pnl / (p["cost_usd"] * frac) * 100 if p["cost_usd"] else 0.0
        else:
            pnl, pct = p["pnl_usd"] or 0.0, p["pnl_pct"] or 0.0
        self.notify(notify.fmt_exit(p["symbol"], mint, reason, pnl, pct))

    def maybe_summary(self, now=None, force=False):
        """Daily summary once per local day at session.report_time (tracked in meta)."""
        now = now or self.clock()
        s = self.cfg["session"]
        t = datetime.fromtimestamp(now, ZoneInfo(s["tz"]))
        h, m = s["report_time"].split(":")
        day = t.date().isoformat()
        if not force and (t.hour * 60 + t.minute < int(h) * 60 + int(m) or self.book.meta_get("last_summary_day") == day):
            return None
        self.book.meta_set("last_summary_day", day)
        text = report.summary_text(self.book, self.cfg, now)
        self.notify(text)
        return text

    def analyze(self, mint, now=None):
        """Analyst on a scanner pass: 8 layers, score, brain sizing, waiting dip order."""
        now = now or self.clock()
        c = self.book.room_get(mint) or {}
        rows = self.ds_batch([mint])
        row = rows.get(mint)
        if not row:
            self.book.event("analyst_skip", mint, "no data: price")
            return None
        created = (row.get("pair_created_ms") or 0) / 1000 or c.get("migrated_at") or now
        ctx = {"mint": mint, "pool": c.get("pool") or row.get("pair"), "creator": c.get("creator"),
               "creator_balance": c.get("creator_balance"), "liq_scanner": c.get("liq_scanner"),
               "liq_now": row.get("liq_usd"), "pool_age_min": (now - created) / 60,
               "curve": c.get("curve"), "curve_min": c.get("curve_min")}
        layers = run_layers(ctx, self.data, self.cfg)
        res = evaluate(ctx, row, layers, self.data.signature_count, self.cfg, self.book, now=now)
        if res["verdict"] != "pass":
            return res
        n = self.book.night()
        pnl = night_pnl(self.book)
        size = self.brain.size(mint, res["score"], row.get("liq_usd"), self.state.get("night_id"),
                               open_pnl=pnl - float(n.get("realized") or 0))
        why = size["reason"]
        if not why and not self.state.get("buys_allowed"):
            why = "outside night window" if not self.state.get("in_window") else "night loss stop"
        if why:
            oid = self.book.order_create(mint, 0.0, res["score"], symbol=c.get("symbol"), wait_min=0, now=now)
            if oid:
                self.book.order_close(oid, "skipped", why, now=now)
            self.book.event("brain_skip", mint, why)
            return res
        bal = ctx.get("creator_balance_now", c.get("creator_balance"))
        self.executor.create_order(mint, size["size"], res["score"], row, scanner_liq=c.get("liq_scanner"),
                                   creator_balance=bal, symbol=c.get("symbol"), now=now)
        self.book.event("order_waiting", mint, f"${size['size']:.2f} score {res['score']}")
        return res

    def scan(self):
        if not self.ds_ok():
            return []
        out = self.scanner.tick()
        for mint, act, why in out:
            if act == "pass":
                guarded("analyst", self.analyze, mint)
        return out

    def write_report(self):
        return guarded("report", report.write, self.book, self.cfg)


def publish():
    if os.environ.get("DIP_PUSH_STATE") != "1":
        return
    guarded("publish", subprocess.call, ["bash", str(ROOT / "scheduler.sh"), "publish-dip"], timeout=300)


def listen():
    desk = Desk()
    cfg = desk.cfg
    start = time.time()
    deadline = start + float(os.environ.get("DIP_MAX_SECONDS") or 20700) - 5
    feed = S.PortalFeed(silence_s=cfg["scanner"]["ws_silence_s"])
    poll_s, scan_s = cfg["risk"]["poll_s"], cfg["scanner"]["poll_s"]
    next_cycle = next_scan = start
    next_publish = start + 300
    log("dip desk listening (PAPER ONLY)")
    desk.write_report()
    try:
        while time.time() < deadline:
            for ev in guarded("portal", feed.poll, 1.0) or []:
                guarded("room", desk.scanner.on_event, ev)
            if feed.client is None:
                time.sleep(0.25)  # socket down: backoff is a schedule, not a sleep; just avoid a hot loop
            now = time.time()
            guarded("summary", desk.maybe_summary, now)
            if now >= next_cycle:
                next_cycle = now + poll_s
                guarded("risk poll", desk.cycle)
            if now >= next_scan:
                next_scan = now + scan_s
                guarded("scanner", desk.scan)
                desk.write_report()
            if now >= next_publish:
                next_publish = now + 300
                desk.write_report()
                publish()
    except KeyboardInterrupt:
        pass
    finally:
        feed.close()
        desk.write_report()
        log("dip desk exiting cleanly")


def tick():
    """One pass with no scanner/portal traffic; safe offline (network errors are logged, not raised)."""
    desk = Desk()
    out = guarded("cycle", desk.cycle) or []
    path = desk.write_report()
    log(f"tick ok: {len(out)} actions, dashboard {path}")


def summary():
    """Print the summary and send it to Telegram when enabled."""
    desk = Desk()
    text = report.summary_text(desk.book, desk.cfg)
    print(text)
    notify.send(text)


def dashboard():
    desk = Desk()
    log(f"wrote {desk.write_report()}")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    fn = {"listen": listen, "tick": tick, "dashboard": dashboard, "summary": summary}.get(cmd)
    if not fn:
        print(__doc__)
        return 2
    fn()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
