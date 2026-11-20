"""SQLite paper ledger for the dip desk (data/dip.db). PAPER ONLY: rows, never real orders."""
import json
import os
import sqlite3
import time
from pathlib import Path

from desk import outcomes as O

ROOT = Path(__file__).resolve().parent.parent
DB = Path(os.environ.get("PAPER_DIP_DB") or ROOT / "data" / "dip.db")
CONFIG = Path(__file__).resolve().parent / "config.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS room(
  mint TEXT PRIMARY KEY, pool TEXT, creator TEXT, name TEXT, symbol TEXT,
  migrated_at REAL, new_seen_at REAL, creator_balance REAL, curve_min REAL,
  liq_scanner REAL, status TEXT DEFAULT 'waiting', reason TEXT, updated_at REAL, passed_day TEXT, curve TEXT
);
CREATE TABLE IF NOT EXISTS prices(
  id INTEGER PRIMARY KEY, mint TEXT, ts REAL, price REAL, liq REAL, mcap REAL,
  vol_m5 REAL, vol_h1 REAL, buys_m5 INT, sells_m5 INT, buys_h1 INT, sells_h1 INT,
  chg_h1 REAL, chg_h24 REAL, liq_mcap REAL, accel REAL
);
CREATE INDEX IF NOT EXISTS prices_mint_ts ON prices(mint, ts);
CREATE TABLE IF NOT EXISTS holders_samples(id INTEGER PRIMARY KEY, mint TEXT, ts REAL, holders INT);
CREATE INDEX IF NOT EXISTS holders_mint_ts ON holders_samples(mint, ts);
CREATE TABLE IF NOT EXISTS rugs(mint TEXT PRIMARY KEY, creator TEXT, name TEXT, seen_at REAL);
CREATE TABLE IF NOT EXISTS audits(
  id INTEGER PRIMARY KEY, mint TEXT, ts REAL, layers TEXT, parts TEXT, score INT,
  verdict TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY, mint TEXT, symbol TEXT, pool TEXT, status TEXT, created_at REAL,
  expires_at REAL, size_usd REAL, reserved_usd REAL, score INT, analyst_price REAL,
  analyst_liq REAL, scanner_liq REAL, creator_balance REAL, closed_at REAL, reason TEXT
);
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY, mint TEXT, symbol TEXT, pool TEXT, status TEXT, night TEXT,
  opened_at REAL, closed_at REAL, entry_price REAL, qty REAL, qty_initial REAL, size_usd REAL,
  cost_usd REAL, buy_fee REAL, peak REAL, stop_line REAL, tp_done INT DEFAULT 0,
  creator_balance REAL, scanner_liq REAL, no_price_polls INT DEFAULT 0, last_price REAL,
  last_liq REAL, proceeds_usd REAL DEFAULT 0, fees_usd REAL DEFAULT 0, exit_reason TEXT,
  pnl_usd REAL, pnl_pct REAL
);
CREATE TABLE IF NOT EXISTS trades(
  id INTEGER PRIMARY KEY, position_id INT, mint TEXT, side TEXT, ts REAL, price REAL, qty REAL,
  usd REAL, fee_usd REAL, reason TEXT
);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, kind TEXT, mint TEXT, note TEXT, data TEXT);
"""


def load_config(path=None):
    return json.loads(Path(path or CONFIG).read_text())


class DipBook:
    def __init__(self, path=None, cfg=None):
        self.cfg = cfg or load_config()
        p = Path(path or DB)
        if str(path) != ":memory:":
            p.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path) if path == ":memory:" else str(p))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA + O.SCHEMA)
        if "curve" not in [r["name"] for r in self.db.execute("PRAGMA table_info(room)")]:
            self.db.execute("ALTER TABLE room ADD COLUMN curve TEXT")
        if self.meta_get("cash") is None:
            self.meta_set("cash", float(self.cfg["brain"]["paper_start_cash"]))
            self.meta_set("start_cash", float(self.cfg["brain"]["paper_start_cash"]))
        self.db.commit()

    # ---- meta / cash / night counters ----
    def meta_get(self, key, default=None):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def meta_set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, json.dumps(value)))
        self.db.commit()

    def cash(self):
        return float(self.meta_get("cash", 0.0))

    def add_cash(self, delta):
        self.meta_set("cash", round(self.cash() + delta, 6))

    def reserved(self):
        r = self.db.execute("SELECT COALESCE(SUM(reserved_usd),0) s FROM orders WHERE status='waiting'").fetchone()
        return float(r["s"])

    def free_cash(self):
        """Cash minus money reserved by waiting dip orders (open positions are already spent)."""
        return round(self.cash() - self.reserved(), 6)

    def night(self):
        return self.meta_get("night", {"id": None, "trades": 0, "start_equity": None, "realized": 0.0, "locked": False})

    def night_set(self, n):
        self.meta_set("night", n)

    def night_reset(self, night_id, equity):
        n = {"id": night_id, "trades": 0, "start_equity": equity, "realized": 0.0, "locked": False}
        self.night_set(n)
        return n

    def night_bump_trades(self):
        n = self.night()
        n["trades"] = int(n.get("trades") or 0) + 1
        self.night_set(n)

    # ---- room ----
    def room_add(self, mint, pool=None, creator=None, name=None, symbol=None, migrated_at=None,
                 new_seen_at=None, creator_balance=None, curve_min=None, curve=None):
        if self.db.execute("SELECT 1 FROM room WHERE mint=?", (mint,)).fetchone():
            return False
        self.db.execute(
            "INSERT INTO room(mint,pool,creator,name,symbol,migrated_at,new_seen_at,creator_balance,curve_min,curve,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (mint, pool, creator, name, symbol, migrated_at, new_seen_at, creator_balance, curve_min, curve, time.time()))
        self.db.commit()
        return True

    def room_update(self, mint, **fields):
        if not fields:
            return
        cols = ",".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE room SET {cols}, updated_at=? WHERE mint=?", (*fields.values(), time.time(), mint))
        self.db.commit()

    def room_get(self, mint):
        r = self.db.execute("SELECT * FROM room WHERE mint=?", (mint,)).fetchone()
        return dict(r) if r else None

    def room_waiting(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM room WHERE status='waiting'")]

    def room_close(self, mint, status, reason):
        self.room_update(mint, status=status, reason=reason)

    def passed_today(self, mint, day):
        r = self.db.execute("SELECT 1 FROM room WHERE mint=? AND passed_day=?", (mint, day)).fetchone()
        return bool(r)

    def kill_counts(self):
        out = {}
        for r in self.db.execute("SELECT status, reason FROM room WHERE status IN ('killed','dropped')"):
            key = _kill_bucket(r["reason"])
            out[key] = out.get(key, 0) + 1
        return out

    # ---- prices ----
    def add_price(self, mint, row, ts=None):
        liq, mcap = row.get("liq_usd"), row.get("mcap")
        vol_m5, vol_h1 = row.get("vol_m5"), row.get("vol_h1")
        liq_mcap = liq / mcap if liq is not None and mcap else None
        accel = vol_m5 * 12 / vol_h1 if vol_m5 is not None and vol_h1 else None
        self.db.execute(
            "INSERT INTO prices(mint,ts,price,liq,mcap,vol_m5,vol_h1,buys_m5,sells_m5,buys_h1,sells_h1,chg_h1,chg_h24,liq_mcap,accel)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mint, ts or time.time(), row.get("price_usd"), liq, mcap, vol_m5, vol_h1, row.get("buys_m5"),
             row.get("sells_m5"), row.get("buys_h1"), row.get("sells_h1"), row.get("chg_h1"), row.get("chg_h24"),
             liq_mcap, accel))
        self.db.commit()

    def last_price(self, mint):
        r = self.db.execute("SELECT * FROM prices WHERE mint=? ORDER BY ts DESC, id DESC LIMIT 1", (mint,)).fetchone()
        return dict(r) if r else None

    def price_history(self, mint, since_ts):
        """[(ts, price, liq)] oldest first: stored PumpSwap readings with a usable price, since since_ts."""
        return [(r["ts"], r["price"], r["liq"]) for r in self.db.execute(
            "SELECT ts, price, liq FROM prices WHERE mint=? AND ts>=? AND price IS NOT NULL AND price>0 ORDER BY ts, id",
            (mint, since_ts))]

    def high_since(self, mint, since_ts):
        r = self.db.execute("SELECT MAX(price) p FROM prices WHERE mint=? AND ts>=? AND price IS NOT NULL",
                            (mint, since_ts)).fetchone()
        return r["p"]

    def runner_rows(self, pct):
        """Latest row per mint in `prices` with h24 >= pct (today's runners)."""
        rows = self.db.execute(
            "SELECT p.mint, p.chg_h24, r.name, r.symbol FROM prices p LEFT JOIN room r ON r.mint=p.mint "
            "WHERE p.id IN (SELECT MAX(id) FROM prices GROUP BY mint) AND p.chg_h24 >= ?", (pct,))
        return [dict(r) for r in rows]

    # ---- holders ----
    def add_holders(self, mint, holders, ts=None):
        self.db.execute("INSERT INTO holders_samples(mint,ts,holders) VALUES(?,?,?)", (mint, ts or time.time(), holders))
        self.db.commit()

    def holders_samples(self, mint):
        return [(r["ts"], r["holders"]) for r in
                self.db.execute("SELECT ts, holders FROM holders_samples WHERE mint=? ORDER BY ts", (mint,))]

    # ---- rugs ----
    def rugs_replace(self, rows, now=None):
        now = now or time.time()
        for r in rows:
            if r.get("mint"):
                self.db.execute("INSERT OR REPLACE INTO rugs(mint,creator,name,seen_at) VALUES(?,?,?,?)",
                                (r["mint"], r.get("creator"), r.get("name"), now))
        self.db.commit()

    def is_rug(self, mint):
        return bool(self.db.execute("SELECT 1 FROM rugs WHERE mint=?", (mint,)).fetchone())

    def rug_creators(self):
        return {r["creator"] for r in self.db.execute("SELECT creator FROM rugs WHERE creator IS NOT NULL")}

    # ---- audits ----
    def save_audit(self, mint, layers, parts, score, verdict, reason, ts=None):
        self.db.execute("INSERT INTO audits(mint,ts,layers,parts,score,verdict,reason) VALUES(?,?,?,?,?,?,?)",
                        (mint, ts or time.time(), json.dumps(layers), json.dumps(parts), score, verdict, reason))
        self.db.commit()

    def recent_audits(self, limit=50):
        out = []
        for r in self.db.execute("SELECT * FROM audits ORDER BY id DESC LIMIT ?", (limit,)):
            d = dict(r)
            d["layers"] = json.loads(d["layers"] or "[]")
            d["parts"] = json.loads(d["parts"] or "{}")
            out.append(d)
        return out

    # ---- orders (waiting dip orders reserve cash) ----
    def order_create(self, mint, size_usd, score, symbol=None, pool=None, analyst_price=None, analyst_liq=None,
                     scanner_liq=None, creator_balance=None, wait_min=30, now=None):
        now = now or time.time()
        if self.db.execute("SELECT 1 FROM orders WHERE mint=? AND status='waiting'", (mint,)).fetchone():
            return None
        cur = self.db.execute(
            "INSERT INTO orders(mint,symbol,pool,status,created_at,expires_at,size_usd,reserved_usd,score,analyst_price,"
            "analyst_liq,scanner_liq,creator_balance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mint, symbol, pool, "waiting", now, now + wait_min * 60, size_usd, size_usd, score, analyst_price,
             analyst_liq, scanner_liq, creator_balance))
        self.db.commit()
        return cur.lastrowid

    def orders_waiting(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM orders WHERE status='waiting' ORDER BY id")]

    def order_close(self, order_id, status, reason, now=None):
        """status: filled | skipped | cancelled. Releases the reservation."""
        self.db.execute("UPDATE orders SET status=?, reason=?, reserved_usd=0, closed_at=? WHERE id=?",
                        (status, reason, now or time.time(), order_id))
        self.db.commit()

    def skip_counts(self):
        out = {}
        for r in self.db.execute("SELECT reason FROM orders WHERE status IN ('skipped','cancelled')"):
            k = _kill_bucket(r["reason"])
            out[k] = out.get(k, 0) + 1
        return out

    # ---- positions / trades ----
    def open_positions(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM positions WHERE status='open' ORDER BY id")]

    def has_position(self, mint, night=None):
        q, a = "SELECT 1 FROM positions WHERE mint=? AND (status='open'", [mint]
        if night is not None:
            q += " OR night=?"
            a.append(night)
        return bool(self.db.execute(q + ")", a).fetchone())

    def open_position(self, mint, price, qty, size_usd, fee, stop_line, night=None, symbol=None, pool=None,
                      creator_balance=None, scanner_liq=None, now=None):
        """Paper buy row. Cash leaves the book now: size_usd (qty already net of the fee)."""
        now = now or time.time()
        cur = self.db.execute(
            "INSERT INTO positions(mint,symbol,pool,status,night,opened_at,entry_price,qty,qty_initial,size_usd,cost_usd,"
            "buy_fee,peak,stop_line,tp_done,creator_balance,scanner_liq,last_price,fees_usd) "
            "VALUES(?,?,?,'open',?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)",
            (mint, symbol, pool, night, now, price, qty, qty, size_usd, size_usd, fee, price, stop_line,
             creator_balance, scanner_liq, price, fee))
        pid = cur.lastrowid
        self.db.execute("INSERT INTO trades(position_id,mint,side,ts,price,qty,usd,fee_usd,reason) VALUES(?,?,?,?,?,?,?,?,?)",
                        (pid, mint, "buy", now, price, qty, size_usd, fee, "dip"))
        self.db.commit()
        self.add_cash(-size_usd)
        return pid

    def position_update(self, pid, **fields):
        cols = ",".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE positions SET {cols} WHERE id=?", (*fields.values(), pid))
        self.db.commit()

    def record_sell(self, pid, price, qty, fee, reason, now=None, close=False, proceeds=None):
        """Paper sell. proceeds defaults to qty*price - fee (0 for 'no route' is passed explicitly)."""
        now = now or time.time()
        p = dict(self.db.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        qty = min(qty, p["qty"])
        gross = qty * (price or 0)
        if proceeds is None:
            proceeds = max(0.0, gross - fee)
        self.db.execute("INSERT INTO trades(position_id,mint,side,ts,price,qty,usd,fee_usd,reason) VALUES(?,?,?,?,?,?,?,?,?)",
                        (pid, p["mint"], "sell", now, price, qty, gross, fee, reason))
        new_qty = p["qty"] - qty
        tot_proceeds = (p["proceeds_usd"] or 0) + proceeds
        fields = {"qty": new_qty, "proceeds_usd": tot_proceeds, "fees_usd": (p["fees_usd"] or 0) + fee}
        if close or new_qty <= 1e-12:
            fields.update(status="closed", closed_at=now, exit_reason=reason, qty=0.0,
                          pnl_usd=tot_proceeds - p["cost_usd"],
                          pnl_pct=(tot_proceeds - p["cost_usd"]) / p["cost_usd"] * 100 if p["cost_usd"] else None)
            n = self.night()
            n["realized"] = float(n.get("realized") or 0) + (tot_proceeds - p["cost_usd"])
            self.night_set(n)
        self.position_update(pid, **fields)
        self.add_cash(proceeds)
        return proceeds

    # ---- events ----
    def event(self, kind, mint=None, note=None, **data):
        self.db.execute("INSERT INTO events(ts,kind,mint,note,data) VALUES(?,?,?,?,?)",
                        (time.time(), kind, mint, note, json.dumps(data, default=str) if data else None))
        self.db.commit()

    def recent_events(self, limit=50):
        return [dict(r) for r in self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    def close(self):
        self.db.close()


def _kill_bucket(reason):
    """Group reasons like 'liq $14.2K' / 'top 10 hold 38%' into stable bucket names."""
    r = (reason or "unknown").lower()
    for key, name in (("liq", "liq"), ("top 10", "top10"), ("copycat", "copycat"), ("warns", "warns"),
                      ("holders leaving", "holders leaving"), ("no data", "no data"), ("timed out", "timed out"),
                      ("crash", "crash"), ("no dip", "no dip"), ("ran before", "ran before the click"),
                      ("dev sold", "dev sold")):
        if r.startswith(key):
            return name
    if "in 1h" in r:
        return "h1 pump"
    return r.split(" ")[0]
