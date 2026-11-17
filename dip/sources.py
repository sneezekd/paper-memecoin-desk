"""Data adapters for the dip desk. Read-only; nothing here signs or sends a transaction.

Every network call takes an injectable callable (`get`, `rpc`, `client_factory`, `sleep`) so tests
run offline. Failures are "no data" (None), never a pass. Secrets are never printed or logged.
"""
import json
import time

from copy.rpc import WsClient, rpc_call, rpc_endpoints
from desk.sources import (_ds_pair_row, bundle_share_from_rugcheck, f, http_json,  # noqa: F401
                          jupiter_quote, prior_tokens_from_rugcheck, pumpfun_coin, pumpfun_created_coins)

PORTAL_WS = "wss://pumpportal.fun/api/data"
DS_TOKENS = "https://api.dexscreener.com/tokens/v1/solana/"
RUGCHECK = "https://api.rugcheck.xyz"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EUFhXkTFiRf6sBXwtkXH"
PUMPSWAP_DEX = ("pumpswap", "pumpfun-amm")
# Known AMM / bonding curve program accounts that hold supply but are not real holders.
KNOWN_PROGRAM_OWNERS = {
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # pump.fun AMM program
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # pump.fun bonding curve program
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium AMM authority
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",  # pump.fun migration account
    "1nc1nerator11111111111111111111111111111111",
}


def retry(fn, tries=3, gap_s=2.0, sleep=time.sleep):
    """Call fn() up to `tries` times. Returns (ok, value). A raised error counts as a failed try."""
    for i in range(tries):
        try:
            return True, fn()
        except Exception:
            if i < tries - 1:
                sleep(gap_s)
    return False, None


# ---------------- DexScreener ----------------
class RateLimited(Exception):
    pass


def pair_row(p):
    r = _ds_pair_row(p)
    r["name"] = (p.get("baseToken") or {}).get("name")
    return r


def pick_pairs(pairs, mints):
    """Pure. Best pumpswap pair per mint by liquidity; anything else = DexScreener doesn't know it."""
    wanted = set(mints)
    best = {}
    for p in pairs or []:
        if not isinstance(p, dict) or p.get("chainId") != "solana" or p.get("dexId") not in PUMPSWAP_DEX:
            continue
        r = pair_row(p)
        a = r["address"]
        if a not in wanted:
            continue
        # prefer dexId 'pumpswap' over 'pumpfun-amm', then the most liquid
        key = (r["dex"] == "pumpswap", r["liq_usd"] or 0)
        if a not in best or key > best[a][0]:
            best[a] = (key, r)
    return {a: v[1] for a, v in best.items()}


class DexScreener:
    """Batched reader (<=30 mints per call) that stays under ds_max_per_min requests per minute."""

    def __init__(self, get=http_json, sleep=time.sleep, clock=time.time, max_per_min=250):
        self.get, self.sleep, self.clock, self.max_per_min = get, sleep, clock, max_per_min
        self.stamps = []

    def _throttle(self):
        now = self.clock()
        self.stamps = [t for t in self.stamps if now - t < 60]
        if len(self.stamps) >= self.max_per_min:
            self.sleep(max(0.0, 60 - (now - self.stamps[0])))
        self.stamps.append(self.clock())

    def batch(self, mints):
        """Returns (rows by mint, errors). Raises RateLimited on 429 so the caller waits and keeps the room."""
        rows, errors = {}, []
        mints = list(dict.fromkeys(m for m in mints if m))
        for i in range(0, len(mints), 30):
            chunk = mints[i:i + 30]
            self._throttle()
            try:
                pairs = self.get(DS_TOKENS + ",".join(chunk)) or []
            except Exception as e:
                if "429" in str(e):
                    raise RateLimited(str(e)[:80])
                errors.append(str(e)[:120])
                continue
            rows.update(pick_pairs(pairs, chunk))
        return rows, errors

    def top_boosts_runners(self, pct):
        """Solana boosted tokens with h24 >= pct, as [{mint,name,symbol,chg_h24}]. Failure -> []."""
        try:
            items = self.get("https://api.dexscreener.com/token-boosts/top/v1") or []
            mints = [i["tokenAddress"] for i in items if i.get("chainId") == "solana" and i.get("tokenAddress")]
            if not mints:
                return []
            out = []
            for i in range(0, len(mints), 30):
                for p in self.get(DS_TOKENS + ",".join(mints[i:i + 30])) or []:
                    r = pair_row(p)
                    if r["chg_h24"] is not None and r["chg_h24"] >= pct and r["address"]:
                        out.append({"mint": r["address"], "name": r["name"], "symbol": r["symbol"], "chg_h24": r["chg_h24"]})
            return out
        except Exception:
            return []


# ---------------- RugCheck ----------------
class RugCheck:
    def __init__(self, get=http_json, sleep=time.sleep, tries=3, gap_s=2.0):
        self.get, self.sleep, self.tries, self.gap_s = get, sleep, tries, gap_s

    def _try(self, url):
        return retry(lambda: self.get(url), self.tries, self.gap_s, self.sleep)

    def report(self, mint):
        ok, d = self._try(f"{RUGCHECK}/v1/tokens/{mint}/report")
        return d if ok and isinstance(d, dict) and d.get("mint") else None

    def graph(self, mint):
        """List/dict from the insiders graph. An empty list is a valid answer ('no edges'); failure is None."""
        ok, d = self._try(f"{RUGCHECK}/v1/tokens/{mint}/insiders/graph")
        return d if ok and isinstance(d, (list, dict)) else None

    def rugs_ticker(self, limit=50):
        """Rug list as [{mint, creator, name}] or None when the request failed."""
        ok, d = self._try(f"{RUGCHECK}/v1/stats/rugs/ticker?limit={int(limit)}")
        if not ok:
            return None
        items = d if isinstance(d, list) else (d.get("items") or d.get("data") or []) if isinstance(d, dict) else []
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            m = it.get("mint") or it.get("token") or it.get("address")
            if m:
                out.append({"mint": m, "creator": it.get("creator") or it.get("deployer"),
                            "name": it.get("name") or it.get("symbol")})
        return out


def total_holders(report):
    n = (report or {}).get("totalHolders")
    return int(n) if isinstance(n, (int, float)) else None


def creator_balance_from_report(report):
    v = (report or {}).get("creatorBalance")
    return f(v) if v is not None else None


# ---------------- Solana RPC ----------------
class Rpc:
    """JSON-RPC wrappers returning None on failure (no data). `call(method, params)` is injectable."""

    def __init__(self, call=None, tries=3, gap_s=2.0, sleep=time.sleep):
        if call is None:
            url = rpc_endpoints()["http"]
            call = lambda method, params: rpc_call(url, method, params)  # noqa: E731
        self.call, self.tries, self.gap_s, self.sleep = call, tries, gap_s, sleep

    def _do(self, method, params):
        ok, v = retry(lambda: self.call(method, params), self.tries, self.gap_s, self.sleep)
        return (True, v) if ok else (False, None)

    def account_info(self, address):
        ok, v = self._do("getAccountInfo", [address, {"encoding": "jsonParsed"}])
        return (v or {}).get("value") if ok else None

    def mint_info(self, mint):
        v = self.account_info(mint)
        parsed = ((v or {}).get("data") or {}).get("parsed") or {}
        if parsed.get("type") != "mint":
            return None
        i = parsed.get("info") or {}
        return {"mintAuthority": i.get("mintAuthority"), "freezeAuthority": i.get("freezeAuthority"),
                "supply": f(i.get("supply")), "decimals": i.get("decimals"), "extensions": i.get("extensions") or [],
                "program": v.get("owner")}

    def token_supply(self, mint):
        ok, v = self._do("getTokenSupply", [mint])
        val = (v or {}).get("value") if ok else None
        return f((val or {}).get("amount")) if val else None

    def largest_accounts(self, mint):
        ok, v = self._do("getTokenLargestAccounts", [mint])
        if not ok or not v:
            return None
        return [{"address": a.get("address"), "amount": f(a.get("amount"), 0.0)} for a in v.get("value") or []]

    def token_account_owner(self, token_account):
        v = self.account_info(token_account)
        info = (((v or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        return info.get("owner")

    def owner_balance(self, owner, mint):
        """Sum of the owner's raw token balance for the mint, or None if RPC fails."""
        ok, v = self._do("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
        if not ok or v is None:
            return None
        total = 0.0
        for a in v.get("value") or []:
            info = (((a.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            total += f((info.get("tokenAmount") or {}).get("amount"), 0.0)
        return total

    def token_accounts_for_mint(self, mint):
        """All token accounts of the mint on Token and Token-2022 (jsonParsed). None if the RPC refuses."""
        out = []
        for program in (TOKEN_PROGRAM, TOKEN_2022):
            ok, v = self._do("getProgramAccounts", [program, {
                "encoding": "jsonParsed", "filters": [{"memcmp": {"offset": 0, "bytes": mint}}, {"dataSize": 165}]}])
            if not ok:
                if program == TOKEN_PROGRAM:
                    return None
                continue
            for a in v or []:
                info = (((a.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
                out.append({"address": a.get("pubkey"), "owner": info.get("owner"),
                            "amount": f((info.get("tokenAmount") or {}).get("amount"), 0.0),
                            "delegate": info.get("delegate"), "delegated": f((info.get("delegatedAmount") or {}).get("amount"), 0.0)})
        return out

    def signature_count(self, address, limit=20):
        ok, v = self._do("getSignaturesForAddress", [address, {"limit": limit}])
        return len(v) if ok and isinstance(v, list) else None


# ---------------- PumpPortal (one websocket, migrations + new tokens only) ----------------
def parse_portal_message(msg, now=None):
    """Pure. Returns {kind: 'new'|'migration', mint, creator, pool, name, symbol, seen_at} or None."""
    if not isinstance(msg, dict):
        return None
    mint = msg.get("mint")
    if not mint:
        return None
    tx = (msg.get("txType") or "").lower()
    if tx == "migrate" or (not tx and msg.get("signature") and not msg.get("name")):
        kind = "migration"
    elif tx == "create" or msg.get("name") or msg.get("symbol"):
        kind = "new"
    else:
        return None
    new = kind == "new"
    pool = msg.get("pool") if not new else None
    # only a real address counts; labels like "pump" / "pump-amm" are not pool addresses
    pool = pool if isinstance(pool, str) and len(pool) >= 32 else None
    # On migrate messages traderPublicKey is the migration tx signer, NOT the creator.
    creator = (msg.get("traderPublicKey") or msg.get("creator")) if new else msg.get("creator")
    return {"kind": kind, "mint": mint, "creator": creator, "pool": pool,
            "curve": msg.get("bondingCurveKey") if new else None,
            "name": msg.get("name"), "symbol": msg.get("symbol"), "seen_at": now or time.time()}


def backoff_seconds(attempt):
    """1, 2, 4 ... capped at 60."""
    return min(60, 2 ** max(0, attempt))


class PortalFeed:
    """One connection. Subscribes to migrations and new tokens only; reconnects after 120 s of silence."""

    def __init__(self, client_factory=None, url=PORTAL_WS, silence_s=120, clock=time.time, sleep=time.sleep):
        self.factory = client_factory or (lambda: WsClient(url))
        self.silence_s, self.clock, self.sleep = silence_s, clock, sleep
        self.client = None
        self.attempt = 0
        self.next_connect = 0.0  # earliest time of the next (re)connect try; never slept on
        self.last_msg = clock()

    def _connect(self):
        c = self.factory()
        c.connect()
        c.send_json({"method": "subscribeMigration"})
        c.send_json({"method": "subscribeNewToken"})
        self.client, self.last_msg, self.attempt = c, self.clock(), 0

    def poll(self, timeout=1.0):
        """Returns a list of parsed events (maybe empty). Handles reconnect with backoff internally."""
        try:
            if self.client is None:
                if self.clock() < self.next_connect:
                    return []  # backing off: the caller's loop keeps running (risk polls etc.)
                self._connect()
            if self.clock() - self.last_msg > self.silence_s:
                raise RuntimeError("silent")
            msg = self.client.recv_json(timeout)
        except Exception:
            self.close()
            self.next_connect = self.clock() + backoff_seconds(self.attempt)
            self.attempt += 1
            return []
        if msg is None:
            return []
        self.last_msg = self.clock()
        ev = parse_portal_message(msg, self.clock())
        return [ev] if ev else []

    def close(self):
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
        self.client = None


# ---------------- Composite provider used by the Analyst ----------------
class LiveData:
    """Implements the data interface layers.py expects, on top of the adapters above."""

    def __init__(self, rpc=None, rugcheck=None, book=None, pumpfun_created=pumpfun_created_coins):
        self.rpc = rpc or Rpc()
        self.rc = rugcheck or RugCheck()
        self.book = book
        self.pumpfun_created = pumpfun_created
        self._report = {}

    def mint_info(self, mint):
        return self.rpc.mint_info(mint)

    def report(self, mint):
        if mint not in self._report:
            self._report[mint] = self.rc.report(mint)
        return self._report[mint]

    def graph(self, mint):
        return self.rc.graph(mint)

    def creator_balance(self, creator, mint):
        return self.rpc.owner_balance(creator, mint)

    def prior_mints(self, creator, mint):
        found = set()
        rep = self.report(mint)
        if rep:
            found.update(t["mint"] for t in prior_tokens_from_rugcheck(rep, mint))
        try:
            for c in self.pumpfun_created(creator) or []:
                m = c.get("mint") if isinstance(c, dict) else None
                if m and m != mint:
                    found.add(m)
        except Exception:
            pass
        return found

    def is_rug(self, mint):
        return bool(self.book and self.book.is_rug(mint))

    def top_holders(self, mint):
        """Largest accounts with resolved owners: [{address, owner, amount}] or None."""
        rows = self.rpc.largest_accounts(mint)
        if rows is None:
            return None
        for r in rows:
            r["owner"] = self.rpc.token_account_owner(r["address"])
        return rows

    def token_accounts(self, mint):
        return self.rpc.token_accounts_for_mint(mint)

    def signature_count(self, owner):
        return self.rpc.signature_count(owner, 20)
