"""Paper copy book. Fills come from Jupiter quotes. Nothing here signs or sends a transaction."""
import json
import sqlite3
import time
from pathlib import Path

from desk.sources import ds_best_pairs, jupiter_quote, rpc_mint_info, sol_price_usd

from .parse import WSOL

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "copy.db"
EVENTS = ROOT / "logs" / "copy-events.jsonl"
DASH = ROOT / "reports" / "copy_dashboard.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS seen(signature TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY,
  mint TEXT UNIQUE,
  symbol TEXT,
  leader TEXT,
  leader_label TEXT,
  status TEXT,
  opened_at REAL,
  closed_at REAL,
  qty REAL,
  qty_initial REAL,
  decimals INT,
  size_usd REAL,
  cost_usd REAL,
  entry_mark REAL,
  entry_fill REAL,
  leader_price REAL,
  slippage_vs_leader_pct REAL,
  detection_latency_s REAL,
  entry_liq REAL,
  exit_cost_frac REAL,
  stop_mark REAL,
  peak_mark REAL,
  last_mark REAL,
  last_check REAL,
  half_sold INT DEFAULT 0,
  exit_reason TEXT,
  pnl_usd REAL,
  pnl_pct REAL
);
CREATE TABLE IF NOT EXISTS trades(
  id INTEGER PRIMARY KEY,
  position_id INT,
  ts REAL,
  side TEXT,
  reason TEXT,
  qty REAL,
  usd REAL,
  fill REAL,
  pnl_usd REAL
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY,
  ts REAL,
  wallet TEXT,
  mint TEXT,
  side TEXT,
  signature TEXT,
  latency_s REAL,
  slippage_vs_leader_pct REAL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS mint_cache(address TEXT PRIMARY KEY, ts REAL, revoked INT, decimals INT);
"""


def load_config():
    return json.loads((Path(__file__).resolve().parent / "config.json").read_text())


def load_wallets(path=None):
    p = Path(path) if path else Path(__file__).resolve().parent / "wallets.json"
    data = json.loads(p.read_text())
    rows = data.get("wallets") if isinstance(data, dict) else data
    out = []
    for w in rows or []:
        if not isinstance(w, dict) or not w.get("address"):
            continue
        if w.get("enabled") is False:
            continue
        out.append({"address": w["address"], "label": w.get("label") or w["address"][:6]})
    return out


def initial_stop(qty, cost, k, hard_stop_net_pct, network_fee):
    """Mark where selling the position nets hard_stop_net_pct after exit costs and the network fee."""
    target = cost * (1 + hard_stop_net_pct / 100.0)
    return (target + network_fee) / (qty * (1 - k))


def net_value(qty, mark, k, network_fee):
    return qty * mark * (1 - k) - network_fee


def net_pct(qty, mark, k, network_fee, cost):
    if not cost:
        return 0.0
    return (net_value(qty, mark, k, network_fee) / cost - 1) * 100.0


def backoff_seconds(attempt, cap):
    return min(cap, float(2 ** max(0, attempt - 1)))


class CopyBook:
    def __init__(self, db_path=None, events_path=None, cfg=None):
        self.cfg = cfg or load_config()
        self.db_path = Path(db_path) if db_path else DB
        self.events_path = Path(events_path) if events_path else EVENTS
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.db_path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if self.meta("cash") is None:
            start = float(self.cfg["bankroll"]["starting_balance_usd"])
            self.set_meta("cash", start)
            self.set_meta("starting_balance", start)
            self.event_log("ledger_init", note="PAPER ONLY starting balance")

    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return default if row is None else row["value"]

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))
        self.db.commit()

    @property
    def cash(self):
        return float(self.meta("cash") or 0)

    def add_cash(self, delta):
        self.set_meta("cash", self.cash + delta)

    def event_log(self, kind, **kw):
        line = {"ts": time.time(), "event": kind, **kw}
        with self.events_path.open("a") as fh:
            fh.write(json.dumps(line, default=str) + "\n")
        self.db.execute(
            "INSERT INTO events(ts,wallet,mint,side,signature,latency_s,slippage_vs_leader_pct,note) VALUES(?,?,?,?,?,?,?,?)",
            (line["ts"], kw.get("wallet"), kw.get("mint"), kw.get("side"), kw.get("signature"),
             kw.get("latency_s"), kw.get("slippage_vs_leader_pct"), kind if not kw.get("note") else kw.get("note")),
        )
        self.db.commit()

    def seen(self, signature):
        if not signature:
            return False
        return self.db.execute("SELECT 1 FROM seen WHERE signature=?", (signature,)).fetchone() is not None

    def mark_seen(self, signature):
        if signature:
            self.db.execute("INSERT OR IGNORE INTO seen VALUES(?,?)", (signature, time.time()))
            self.db.commit()

    def open_positions(self):
        return self.db.execute("SELECT * FROM positions WHERE status='open'").fetchall()

    def equity(self, marks=None):
        fee = float(self.cfg["costs"]["network_fee_usd_per_tx"])
        total = self.cash
        for p in self.open_positions():
            mark = ((marks or {}).get(p["mint"]) or {}).get("price_usd") or p["last_mark"] or p["entry_mark"]
            total += max(0.0, net_value(p["qty"], mark, p["exit_cost_frac"] or 0.03, fee))
        return total

    def ever_traded(self, mint):
        return self.db.execute("SELECT 1 FROM positions WHERE mint=?", (mint,)).fetchone() is not None

    def position_for(self, mint, leader=None):
        if leader:
            return self.db.execute(
                "SELECT * FROM positions WHERE mint=? AND leader=? AND status='open'", (mint, leader)).fetchone()
        return self.db.execute(
            "SELECT * FROM positions WHERE mint=? AND status='open'", (mint,)).fetchone()

    def try_entry(self, swap, label="", check=None, now_ts=None):
        """Paper-buy the leader's first buy. `check` returns a quote dict or raises/returns ok False."""
        if swap.get("side") != "buy" or not swap.get("first_buy"):
            return None
        mint = swap["mint"]
        if self.ever_traded(mint):
            self.event_log("skip_reentry", wallet=swap.get("wallet"), mint=mint, signature=swap.get("signature"),
                           note="one entry per coin")
            return None
        leader_px = swap.get("leader_price_usd")
        if not leader_px or leader_px <= 0:
            self.event_log("skip_no_leader_price", wallet=swap.get("wallet"), mint=mint, note="fail closed")
            return None
        size = self.equity() * float(self.cfg["bankroll"]["max_trade_pct_of_equity"]) / 100.0
        size = min(size, self.cash)
        if size < float(self.cfg["bankroll"]["min_trade_usd"]):
            self.event_log("skip_size", mint=mint, note="size below minimum")
            return None
        if check is None:
            check = live_safety_check
        try:
            quote = check(mint, leader_px, size, self.cfg)
        except Exception as e:
            self.event_log("skip_check", mint=mint, wallet=swap.get("wallet"), note="fail closed: " + str(e)[:160])
            return None
        if not quote or not quote.get("ok"):
            self.event_log("skip_check", mint=mint, wallet=swap.get("wallet"),
                           note=(quote or {}).get("reason") or "fail closed")
            return None
        slip = (quote["fill"] / leader_px - 1) * 100.0
        cap = float(self.cfg["safety"]["max_worse_than_leader_pct"])
        if slip > cap:
            self.event_log("skip_chase", mint=mint, wallet=swap.get("wallet"), slippage_vs_leader_pct=round(slip, 2),
                           note=f"fill {slip:.1f}% worse than leader (cap {cap:.0f}%)")
            return None
        fee = float(self.cfg["costs"]["network_fee_usd_per_tx"])
        k = quote["exit_cost_frac"]
        qty = quote["qty"]
        cost = size
        stop = initial_stop(qty, cost, k, float(self.cfg["risk"]["hard_stop_net_pct"]), fee)
        ts = now_ts if now_ts is not None else time.time()
        cur = self.db.execute(
            "INSERT INTO positions(mint,symbol,leader,leader_label,status,opened_at,qty,qty_initial,decimals,size_usd,"
            "cost_usd,entry_mark,entry_fill,leader_price,slippage_vs_leader_pct,detection_latency_s,entry_liq,"
            "exit_cost_frac,stop_mark,peak_mark,last_mark,last_check,half_sold) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (mint, quote.get("symbol") or mint[:4], swap.get("wallet"), label, "open", ts, qty, qty,
             int(quote.get("decimals") or 6), cost, cost, quote.get("mark") or leader_px, quote["fill"], leader_px,
             slip, swap.get("latency_s"), quote.get("liq_usd"), k, stop, quote.get("mark") or leader_px,
             quote.get("mark") or leader_px, ts),
        )
        self.add_cash(-cost)
        self.db.execute(
            "INSERT INTO trades(position_id,ts,side,reason,qty,usd,fill,pnl_usd) VALUES(?,?,?,?,?,?,?,?)",
            (cur.lastrowid, ts, "BUY", "leader_buy", qty, cost, quote["fill"], 0.0))
        self.db.commit()
        self.event_log("paper_buy", wallet=swap.get("wallet"), mint=mint, side="buy", signature=swap.get("signature"),
                       latency_s=swap.get("latency_s"), slippage_vs_leader_pct=round(slip, 2),
                       note=f"paper buy ${cost:.2f} slip {slip:.1f}%")
        return cur.lastrowid

    def on_leader_sell(self, swap, now_ts=None):
        """Sell the whole paper position when the leader sells at least the configured fraction."""
        if swap.get("side") != "sell":
            return False
        frac = swap.get("sold_fraction")
        need = float(self.cfg["risk"]["leader_sell_fraction"])
        if frac is None or frac + 1e-9 < need:
            self.event_log("leader_sell_ignored", wallet=swap.get("wallet"), mint=swap.get("mint"),
                           note=f"sold fraction {frac} < {need}")
            return False
        p = self.position_for(swap["mint"], swap.get("wallet"))
        if p is None:
            return False
        mark = p["last_mark"] or p["entry_mark"]
        self._sell_all(p, mark, p["entry_liq"], "leader_sell",
                       f"leader sold {frac:.0%} of {swap['mint'][:6]}", now_ts)
        return True

    def manage(self, marks, now_ts=None):
        """Re-mark open paper positions. Hard stop never moves down. Returns exit count."""
        ts = now_ts if now_ts is not None else time.time()
        fee = float(self.cfg["costs"]["network_fee_usd_per_tx"])
        risk = self.cfg["risk"]
        n = 0
        for p in list(self.open_positions()):
            q = (marks or {}).get(p["mint"]) or {}
            mark = q.get("price_usd") or p["last_mark"] or p["entry_mark"]
            liq = q.get("liq_usd")
            peak = max(p["peak_mark"] or mark, mark)
            self.db.execute("UPDATE positions SET last_mark=?, peak_mark=?, last_check=? WHERE id=?",
                            (mark, peak, ts, p["id"]))
            self.db.commit()
            p = self.db.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
            reason = None
            if liq is not None and p["entry_liq"]:
                drop = (1 - liq / p["entry_liq"]) * 100.0
                floor = float(self.cfg["safety"]["min_liquidity_usd"])
                if round(drop, 4) > float(risk["liq_drop_from_entry_pct"]) or liq < floor:
                    reason = "liq_drop"
            if reason is None and ts - p["opened_at"] >= float(risk["max_hold_seconds"]):
                reason = "max_hold"
            pct = net_pct(p["qty"], mark, p["exit_cost_frac"] or 0.03, fee, p["cost_usd"])
            if reason is None and mark <= p["stop_mark"]:
                reason = "stop"
            if reason:
                self._sell_all(p, mark, liq if liq is not None else p["entry_liq"], reason, reason, ts)
                n += 1
                continue
            if not p["half_sold"] and round(pct, 4) >= float(risk["take_profit_net_pct"]):
                self._sell_fraction(p, mark, float(risk["take_profit_fraction"]), "take_profit", ts)
                p = self.db.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
            if p["half_sold"] and p["status"] == "open":
                trail = peak * (1 - float(risk["trail_distance_from_peak_pct"]) / 100.0)
                # The hard stop stays where it was set. The trail can only exit above that stop.
                if mark <= max(p["stop_mark"], trail):
                    self._sell_all(p, mark, liq if liq is not None else p["entry_liq"], "trail",
                                   f"trail {risk['trail_distance_from_peak_pct']}% under peak", ts)
                    n += 1
        return n

    def _sell_fraction(self, p, mark, fraction, reason, ts):
        fraction = min(max(fraction, 0.0), 1.0)
        if fraction <= 0 or p["qty"] <= 0:
            return
        qty = p["qty"] * fraction
        fee = float(self.cfg["costs"]["network_fee_usd_per_tx"])
        proceeds = max(0.0, net_value(qty, mark, p["exit_cost_frac"] or 0.03, fee))
        cost = p["cost_usd"] * fraction
        pnl = proceeds - cost
        self.add_cash(proceeds)
        self.db.execute(
            "UPDATE positions SET qty=?, cost_usd=?, size_usd=?, half_sold=1 WHERE id=?",
            (p["qty"] - qty, p["cost_usd"] - cost, p["size_usd"] * (1 - fraction), p["id"]))
        self.db.execute(
            "INSERT INTO trades(position_id,ts,side,reason,qty,usd,fill,pnl_usd) VALUES(?,?,?,?,?,?,?,?)",
            (p["id"], ts, "SELL", reason, qty, proceeds, mark, pnl))
        self.db.commit()
        self.event_log("paper_sell", mint=p["mint"], wallet=p["leader"], side="sell", note=reason)

    def _sell_all(self, p, mark, liq, reason, note, ts):
        if p["status"] != "open":
            return
        fee = float(self.cfg["costs"]["network_fee_usd_per_tx"])
        proceeds = max(0.0, net_value(p["qty"], mark, p["exit_cost_frac"] or 0.03, fee))
        pnl = proceeds - p["cost_usd"]
        pnl_pct = pnl / p["size_usd"] * 100.0 if p["size_usd"] else 0.0
        self.add_cash(proceeds)
        self.db.execute(
            "UPDATE positions SET status='closed', closed_at=?, qty=0, cost_usd=0, exit_reason=?, pnl_usd=?, pnl_pct=? WHERE id=?",
            (ts, reason, pnl, pnl_pct, p["id"]))
        self.db.execute(
            "INSERT INTO trades(position_id,ts,side,reason,qty,usd,fill,pnl_usd) VALUES(?,?,?,?,?,?,?,?)",
            (p["id"], ts, "SELL", reason, p["qty"], proceeds, mark, pnl))
        self.db.commit()
        self.event_log("paper_sell", mint=p["mint"], wallet=p["leader"], side="sell", note=note or reason)

    def dashboard(self):
        from .report import write_dashboard
        return write_dashboard(self, path=getattr(self, "dash_path", None))


def live_safety_check(mint, leader_price_usd, size_usd, cfg, rpc_url=None, deadline=None):
    """Fast fail-closed checks. Returns {ok: True, ...} or {ok: False, reason}."""
    safety = cfg["safety"]
    costs = cfg["costs"]
    deadline = deadline if deadline is not None else time.time() + float(safety["check_budget_seconds"])

    def left():
        return deadline - time.time()

    if left() <= 0:
        return {"ok": False, "reason": "safety budget exceeded"}
    try:
        info = rpc_mint_info(mint)
    except Exception as e:
        return {"ok": False, "reason": "mint check failed: " + str(e)[:120]}
    if info.get("mintAuthority") or info.get("freezeAuthority"):
        return {"ok": False, "reason": "mint or freeze authority still set"}
    if info.get("decimals") is None:
        return {"ok": False, "reason": "mint decimals missing"}
    if left() <= 0:
        return {"ok": False, "reason": "safety budget exceeded"}
    try:
        pairs, _errs = ds_best_pairs([mint], set(safety["allowed_dex_ids"]))
    except Exception as e:
        return {"ok": False, "reason": "liquidity lookup failed: " + str(e)[:120]}
    pair = pairs.get(mint)
    if not pair or pair.get("liq_usd") is None:
        return {"ok": False, "reason": "liquidity missing"}
    if pair["liq_usd"] < float(safety["min_liquidity_usd"]):
        return {"ok": False, "reason": f"liquidity ${pair['liq_usd']:,.0f} below floor"}
    if left() <= 0.15:
        return {"ok": False, "reason": "safety budget exceeded"}
    try:
        sol = sol_price_usd()
        fee = float(costs["network_fee_usd_per_tx"])
        lam = int(max(size_usd - fee, 0.5) / sol * 1e9)
        buy = jupiter_quote(WSOL, mint, lam, int(costs["jupiter_slippage_bps"]))
        out_raw = int(buy["outAmount"])
        if out_raw <= 0:
            return {"ok": False, "reason": "jupiter buy route empty"}
        decimals = int(info["decimals"])
        qty = out_raw / 10 ** decimals
        spend = max(size_usd - fee, 0.0)
        fill = spend / qty * (1 + float(costs["extra_adverse_slippage_pct"]) / 100.0)
        sell = jupiter_quote(mint, WSOL, out_raw, int(costs["jupiter_slippage_bps"]))
        back = int(sell["outAmount"])
        rt = (1 - back / lam) * 100.0
        if rt > float(safety["max_round_trip_loss_pct"]):
            return {"ok": False, "reason": f"round trip {rt:.1f}% over cap"}
        sell_usd = back / 1e9 * sol * (1 - float(costs["extra_adverse_slippage_pct"]) / 100.0)
        mark = pair.get("price_usd") or leader_price_usd
        k = min(max(1 - sell_usd / (qty * mark), 0.0), 0.5) if mark else 0.03
    except Exception as e:
        return {"ok": False, "reason": "jupiter quote failed: " + str(e)[:120]}
    worse = (fill / leader_price_usd - 1) * 100.0
    if worse > float(safety["max_worse_than_leader_pct"]):
        return {"ok": False, "reason": f"fill {worse:.1f}% worse than leader"}
    return {"ok": True, "fill": fill, "qty": spend / fill, "decimals": decimals, "symbol": pair.get("symbol"),
            "liq_usd": pair["liq_usd"], "mark": mark, "exit_cost_frac": k, "round_trip_loss_pct": rt}
