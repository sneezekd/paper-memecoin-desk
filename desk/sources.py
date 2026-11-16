"""Read-only market data sources. Nothing here can sign or send a transaction.

GMGN is queried only through a whitelist of read-only gmgn-cli commands, and
GMGN_PRIVATE_KEY is always stripped from the subprocess environment.
"""
import json, os, shutil, subprocess, time, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GMGN_DEMO_KEY = "gmgn_solbscbaseethmonadtron"  # public read-only test key published in the GMGN README
UA = {"User-Agent": "paper-desk/1.0 (read-only research)", "Accept": "application/json"}
READ_ONLY_GMGN = {("market", "trending"), ("market", "trenches"), ("track", "smartmoney"),
                  ("token", "info"), ("token", "security")}


def f(x, default=None):
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def http_json(url, timeout=15, retries=1, headers=None):
    last = None
    hdrs = dict(UA)
    if headers:
        hdrs.update(headers)
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode(errors="replace")
                if "just a moment" in raw[:200].lower() or raw.lstrip()[:1] not in "{[":
                    # Cloudflare HTML or non-JSON — don't keep retrying as if it were JSON
                    if raw.lstrip()[:1] not in "{[":
                        last = "non-JSON response"
                        if "just a moment" in raw[:400].lower() or "cloudflare" in raw[:800].lower():
                            last = "HTTP 403 Cloudflare challenge"
                        raise RuntimeError(f"{url[:80]}: {last}")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code == 429:
                time.sleep(2 + 2 * i)
            elif e.code in (401, 403, 404):
                break
        except RuntimeError as e:
            last = str(e)[:120]
            break
        except Exception as e:  # network / json
            last = str(e)[:120]
        time.sleep(0.5)
    raise RuntimeError(f"{url[:80]}: {last}")


# ---------------- GMGN (gmgn-cli, read-only) ----------------
class Gmgn:
    def __init__(self, allow_demo=True):
        self.bin = shutil.which("gmgn-cli") or str(Path.home() / ".local/bin/gmgn-cli")
        self.key_mode = None
        self.env = {k: v for k, v in os.environ.items() if k != "GMGN_PRIVATE_KEY"}
        self.env["PATH"] = str(Path.home() / ".local/bin") + ":" + self.env.get("PATH", "")
        personal = self._personal_key()
        if personal:
            self.env["GMGN_API_KEY"] = personal
            self.key_mode = "personal"
        elif allow_demo:
            self.env["GMGN_API_KEY"] = GMGN_DEMO_KEY
            self.key_mode = "public-demo"
        self.errors = []

    @staticmethod
    def _personal_key():
        if os.environ.get("GMGN_API_KEY") and os.environ["GMGN_API_KEY"] != GMGN_DEMO_KEY:
            return os.environ["GMGN_API_KEY"]
        for p in (ROOT / "secrets/gmgn.env", Path.home() / ".config/gmgn/.env"):
            if p.exists():
                for line in p.read_text().splitlines():
                    if line.strip().startswith("GMGN_API_KEY="):
                        v = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if v and v != GMGN_DEMO_KEY:
                            return v
        return None

    @property
    def available(self):
        return self.key_mode is not None and Path(self.bin).exists()

    def run(self, *args):
        if tuple(args[:2]) not in READ_ONLY_GMGN:
            raise PermissionError(f"gmgn-cli {' '.join(args[:2])} is not on the read-only whitelist")
        cmd = [self.bin, *args, "--raw"]
        for attempt in range(2):
            p = subprocess.run(cmd, env=self.env, capture_output=True, text=True, timeout=45)
            out = (p.stdout or "").strip()
            if p.returncode == 0 and out:
                try:
                    d = json.loads(out.splitlines()[-1]) if not out.startswith("{") else json.loads(out)
                except json.JSONDecodeError:
                    d = None
                if d is not None:
                    if isinstance(d, dict) and "data" in d and "code" in d:
                        if d.get("code") not in (0, None):
                            raise RuntimeError(f"gmgn {args[:2]} code={d.get('code')} {d.get('message')}")
                        d = d["data"]
                    time.sleep(0.4)  # stay well under free-tier leaky bucket
                    return d
            err = (p.stderr or out)[:200]
            if "RATE_LIMIT" in err and attempt == 0:
                time.sleep(6)
                continue
            raise RuntimeError(f"gmgn {' '.join(args[:2])}: {err}")

    def trenches(self):
        d = self.run("market", "trenches", "--chain", "sol", "--type", "new_creation", "near_completion",
                     "completed", "--limit", "60")
        out = []
        for key, label in (("new_creation", "gmgn_trenches_new"), ("pump", "gmgn_trenches_near_grad"),
                           ("near_completion", "gmgn_trenches_near_grad"), ("completed", "gmgn_trenches_graduated")):
            for it in (d.get(key) or []):
                out.append((label, it))
        return out

    def trending(self, interval):
        d = self.run("market", "trending", "--chain", "sol", "--interval", interval, "--limit", "50",
                     "--order-by", "volume", "--max-created", "12h", "--min-liquidity", "5000")
        return [(f"gmgn_trending_{interval}", it) for it in (d.get("rank") or [])]

    def smartmoney_buys(self, window_min):
        d = self.run("track", "smartmoney", "--chain", "sol", "--limit", "200", "--side", "buy")
        rows = d.get("list") if isinstance(d, dict) else d
        now = time.time()
        agg = {}
        skip = {"So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
        for r in rows or []:
            if r.get("side") != "buy" or now - (r.get("timestamp") or 0) > window_min * 60:
                continue
            a = r.get("base_address")
            if not a or a in skip:
                continue
            e = agg.setdefault(a, {"address": a, "symbol": (r.get("base_token") or {}).get("symbol"),
                                   "makers": set(), "usd": 0.0})
            e["makers"].add(r.get("maker"))
            e["usd"] += f(r.get("amount_usd"), 0)
        return [{"address": a, "symbol": e["symbol"], "smart_buyers": len(e["makers"]),
                 "smart_buy_usd": round(e["usd"], 2)} for a, e in agg.items()]

    def token_detail(self, address):
        info = self.run("token", "info", "--chain", "sol", "--address", address)
        sec = {}
        try:
            sec = self.run("token", "security", "--chain", "sol", "--address", address)
        except Exception as e:
            self.errors.append(str(e))
        dev = info.get("dev") or {}
        link = info.get("link") or {}
        supply = f(info.get("total_supply"), 0) or 0
        dev_bal = f(dev.get("creator_token_balance"), None)
        return {
            "address": address, "symbol": info.get("symbol"),
            "dev_team_hold_rate": (dev_bal / supply) if (dev_bal is not None and supply) else None,
            "top_10_holder_rate": f(sec.get("top_10_holder_rate"), f(dev.get("top_10_holder_rate"))),
            "twitter_username": link.get("twitter_username"), "website": link.get("website"),
            "telegram": link.get("telegram"),
            "renounced_mint": sec.get("renounced_mint"), "renounced_freeze_account": sec.get("renounced_freeze_account"),
            "is_honeypot": sec.get("honeypot"), "creation_timestamp": info.get("creation_timestamp"),
            "liquidity": info.get("liquidity"),
        }


# ---------------- DexScreener (keyless) ----------------
def dexscreener_new_solana():
    """Recently profiled/boosted Solana tokens (keyless discovery fallback)."""
    out = []
    for url, label in (("https://api.dexscreener.com/token-profiles/latest/v1", "dexscreener_profiles"),
                       ("https://api.dexscreener.com/token-boosts/latest/v1", "dexscreener_boosts")):
        try:
            for it in http_json(url) or []:
                if it.get("chainId") == "solana" and it.get("tokenAddress"):
                    out.append((label, {"address": it["tokenAddress"], "has_profile_links": bool(it.get("links"))}))
        except Exception as e:
            out.append(("error", {"error": f"{label}: {e}"}))
    return out


def dexscreener_pairs(addresses):
    """Best (highest-liquidity) Solana pair per token, via batched keyless API (30/call)."""
    best = {}
    addrs = list(dict.fromkeys(addresses))
    for i in range(0, len(addrs), 30):
        chunk = addrs[i:i + 30]
        try:
            pairs = http_json("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(chunk)) or []
        except Exception:
            continue
        for p in pairs:
            a = (p.get("baseToken") or {}).get("address")
            if a not in chunk:
                continue
            liq = f((p.get("liquidity") or {}).get("usd"), 0)
            if a not in best or liq > best[a]["liq_usd"]:
                info = p.get("info") or {}
                best[a] = {
                    "pair": p.get("pairAddress"), "dex": p.get("dexId"), "url": p.get("url"),
                    "symbol": (p.get("baseToken") or {}).get("symbol"),
                    "price_usd": f(p.get("priceUsd")), "liq_usd": liq,
                    "vol_m5": f((p.get("volume") or {}).get("m5"), 0), "vol_h1": f((p.get("volume") or {}).get("h1"), 0),
                    "buys_m5": ((p.get("txns") or {}).get("m5") or {}).get("buys", 0),
                    "sells_m5": ((p.get("txns") or {}).get("m5") or {}).get("sells", 0),
                    "buys_h1": ((p.get("txns") or {}).get("h1") or {}).get("buys", 0),
                    "sells_h1": ((p.get("txns") or {}).get("h1") or {}).get("sells", 0),
                    "chg_m5": f((p.get("priceChange") or {}).get("m5"), 0),
                    "chg_h1": f((p.get("priceChange") or {}).get("h1"), 0),
                    "chg_h6": f((p.get("priceChange") or {}).get("h6"), 0),
                    "chg_h24": f((p.get("priceChange") or {}).get("h24"), 0),
                    "vol_h6": f((p.get("volume") or {}).get("h6"), 0),
                    "vol_h24": f((p.get("volume") or {}).get("h24"), 0),
                    "fetched_at": time.time(),
                    "mcap": f(p.get("marketCap") or p.get("fdv")),
                    "pair_created_ms": p.get("pairCreatedAt"),
                    "socials": [s.get("url") or s.get("handle") for s in info.get("socials") or []] +
                               [w.get("url") for w in info.get("websites") or []],
                }
        time.sleep(0.25)
    return best


# ---------------- RugCheck (keyless) ----------------
def rugcheck_summary(address):
    d = http_json(f"https://api.rugcheck.xyz/v1/tokens/{address}/report/summary", timeout=15)
    risks = d.get("risks") or []
    return {"score_normalised": f(d.get("score_normalised")), "score": f(d.get("score")),
            "lp_locked_pct": f(d.get("lpLockedPct")),
            "danger": [r.get("name") for r in risks if (r.get("level") or "").lower() == "danger"],
            "warn": [r.get("name") for r in risks if (r.get("level") or "").lower() == "warn"],
            "risk_names": [r.get("name") for r in risks]}


# ---------------- v2 additions: freshness-stamped fetchers ----------------
SOL_MINT = "So11111111111111111111111111111111111111112"
BURN_OWNERS = {"1nc1nerator11111111111111111111111111111111", "11111111111111111111111111111111"}
RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")


def http_post_json(url, body, timeout=12):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={**UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def rpc_mint_info(mint):
    """Solana RPC getAccountInfo (jsonParsed). Read-only; no signing."""
    d = http_post_json(RPC_URL, {"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                                 "params": [mint, {"encoding": "jsonParsed"}]})
    if "error" in d:
        raise RuntimeError(f"rpc error {d['error']}")
    v = (d.get("result") or {}).get("value")
    if not v:
        raise RuntimeError("mint account not found")
    parsed = (v.get("data") or {}).get("parsed") or {}
    if parsed.get("type") != "mint":
        raise RuntimeError("account is not a parsed mint")
    info = parsed.get("info") or {}
    return {"program": v.get("owner"), "mintAuthority": info.get("mintAuthority"),
            "freezeAuthority": info.get("freezeAuthority"), "supply": info.get("supply"),
            "decimals": info.get("decimals"), "extensions": info.get("extensions") or []}


def rugcheck_report(mint):
    d = http_json(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report", timeout=20)
    if not isinstance(d, dict) or not d.get("mint"):
        raise RuntimeError("empty RugCheck report")
    return d


def jupiter_quote(input_mint, output_mint, amount_raw, slippage_bps=300):
    url = ("https://lite-api.jup.ag/swap/v1/quote?inputMint=%s&outputMint=%s&amount=%d&slippageBps=%d"
           % (input_mint, output_mint, int(amount_raw), slippage_bps))
    last = None
    for i in range(3):
        try:
            d = http_json(url, timeout=12, retries=0)
            if d.get("outAmount"):
                return d
            last = d.get("error") or d.get("errorCode") or "no route"
            break
        except RuntimeError as e:
            last = str(e)
            if "429" in last:
                time.sleep(1.5 * (i + 1))
                continue
            break
    raise RuntimeError(f"jupiter quote failed: {last}")


def sol_price_usd():
    pairs = http_json("https://api.dexscreener.com/tokens/v1/solana/" + SOL_MINT)
    best = None
    for p in pairs or []:
        if (p.get("baseToken") or {}).get("address") == SOL_MINT and (p.get("quoteToken") or {}).get("symbol") in ("USDC", "USDT"):
            liq = f((p.get("liquidity") or {}).get("usd"), 0)
            if best is None or liq > best[0]:
                best = (liq, f(p.get("priceUsd")))
    if not best or not best[1]:
        raise RuntimeError("no SOL/USD price")
    return best[1]


def dexscreener_search(q):
    d = http_json("https://api.dexscreener.com/latest/dex/search?q=" + urllib.request.quote(q))
    return [p for p in (d.get("pairs") or []) if p.get("chainId") == "solana"]


def dexscreener_top_boosts():
    out = []
    for it in http_json("https://api.dexscreener.com/token-boosts/top/v1") or []:
        if it.get("chainId") == "solana" and it.get("tokenAddress"):
            out.append(it["tokenAddress"])
    return out


def gmgn_created_tokens(g, wallet):
    return g.run("portfolio", "created-tokens", "--chain", "sol", "--wallet", wallet)


READ_ONLY_GMGN.add(("portfolio", "created-tokens"))


# ---------------- v3 additions ----------------
def _ds_pair_row(p):
    info = p.get("info") or {}
    tx = p.get("txns") or {}
    vol = p.get("volume") or {}
    chg = p.get("priceChange") or {}
    return {
        "pair": p.get("pairAddress"), "dex": p.get("dexId"), "url": p.get("url"),
        "address": (p.get("baseToken") or {}).get("address"),
        "symbol": (p.get("baseToken") or {}).get("symbol"),
        "quote": (p.get("quoteToken") or {}).get("symbol"),
        "price_usd": f(p.get("priceUsd")), "liq_usd": f((p.get("liquidity") or {}).get("usd")),
        "vol_m5": f(vol.get("m5")), "vol_h1": f(vol.get("h1")), "vol_h6": f(vol.get("h6")),
        "vol_h24": f(vol.get("h24")),
        "buys_m5": (tx.get("m5") or {}).get("buys"), "sells_m5": (tx.get("m5") or {}).get("sells"),
        "buys_h1": (tx.get("h1") or {}).get("buys"), "sells_h1": (tx.get("h1") or {}).get("sells"),
        "chg_m5": f(chg.get("m5")), "chg_h1": f(chg.get("h1")), "chg_h6": f(chg.get("h6")),
        "chg_h24": f(chg.get("h24")),
        "mcap": f(p.get("marketCap")) or f(p.get("fdv")),
        "pair_created_ms": p.get("pairCreatedAt"),
        "socials": [s.get("url") or s.get("handle") for s in info.get("socials") or []] +
                   [w.get("url") for w in info.get("websites") or []],
        "fetched_at": time.time(),
    }


def ds_best_pairs(addresses, allowed_dex):
    """Best (highest-liquidity) Pump.fun/Raydium pair per token. Errors raise per chunk -> caller logs."""
    best, errors = {}, []
    addrs = list(dict.fromkeys(a for a in addresses if a))
    for i in range(0, len(addrs), 30):
        chunk = addrs[i:i + 30]
        try:
            pairs = http_json("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(chunk)) or []
        except Exception as e:
            errors.append(str(e)[:120]); continue
        for p in pairs:
            if p.get("chainId") != "solana" or p.get("dexId") not in allowed_dex:
                continue
            r = _ds_pair_row(p)
            a = r["address"]
            if a not in chunk:
                continue
            if a not in best or (r["liq_usd"] or 0) > (best[a]["liq_usd"] or 0):
                best[a] = r
        time.sleep(0.25)
    return best, errors


def ds_discovery(search_terms, allowed_dex):
    """DexScreener keyless discovery: latest profiles, boosts, takeovers, and dex searches."""
    addrs, status = {}, {}
    feeds = (("ds_profiles", "https://api.dexscreener.com/token-profiles/latest/v1"),
             ("ds_boosts_latest", "https://api.dexscreener.com/token-boosts/latest/v1"),
             ("ds_boosts_top", "https://api.dexscreener.com/token-boosts/top/v1"),
             ("ds_takeovers", "https://api.dexscreener.com/community-takeovers/latest/v1"))
    for label, url in feeds:
        try:
            rows = [it for it in (http_json(url) or []) if it.get("chainId") == "solana" and it.get("tokenAddress")]
            for it in rows:
                addrs.setdefault(it["tokenAddress"], set()).add(label)
            status[label] = f"ok ({len(rows)})"
        except Exception as e:
            status[label] = f"error: {e}"[:150]
    for q in search_terms:
        try:
            rows = [p for p in dexscreener_search(q) if p.get("dexId") in allowed_dex]
            for p in rows:
                a = (p.get("baseToken") or {}).get("address")
                if a and a != SOL_MINT:
                    addrs.setdefault(a, set()).add(f"ds_search:{q}")
            status[f"ds_search:{q}"] = f"ok ({len(rows)})"
        except Exception as e:
            status[f"ds_search:{q}"] = f"error: {e}"[:150]
        time.sleep(0.3)
    return addrs, status


GECKO_DEX = {"pumpswap", "pump-fun", "raydium", "raydium-clmm", "raydium-cp", "raydium-launchlab"}


def gecko_discovery():
    """Address-only discovery supplement (new + trending pools). Pair data still comes from DexScreener."""
    addrs, status = {}, {}
    for label, path in (("gt_new_pools", "new_pools"), ("gt_trending", "trending_pools")):
        n = 0
        for page in (1, 2):
            try:
                d = http_json(f"https://api.geckoterminal.com/api/v2/networks/solana/{path}?page={page}&include=dex")
            except Exception as e:
                status[label] = f"error: {e}"[:150]; break
            for it in d.get("data") or []:
                rel = it.get("relationships") or {}
                dex = ((rel.get("dex") or {}).get("data") or {}).get("id", "")
                base = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
                if dex in GECKO_DEX and base.startswith("solana_"):
                    a = base.split("_", 1)[1]
                    if a != SOL_MINT:
                        addrs.setdefault(a, set()).add(label); n += 1
            time.sleep(2.1)
        status.setdefault(label, f"ok ({n})")
    return addrs, status


def gecko_pool_txns(pair_addresses):
    """Unique buyers/sellers per pool from GeckoTerminal (keyless). {pair: {..., fetched_at}}."""
    out = {}
    pa = list(dict.fromkeys(p for p in pair_addresses if p))
    for i in range(0, len(pa), 30):
        chunk = pa[i:i + 30]
        d = http_json("https://api.geckoterminal.com/api/v2/networks/solana/pools/multi/" + ",".join(chunk))
        for it in d.get("data") or []:
            at = it.get("attributes") or {}
            out[at.get("address")] = {"tx": at.get("transactions") or {}, "fetched_at": time.time()}
        time.sleep(2.1)
    return out


def gmgn_token_info_raw(g, address):
    return g.run("token", "info", "--chain", "sol", "--address", address)


# ---------------- Free-source replacements for GMGN (bundle, creator history) ----------------
PF_HEADERS = {"User-Agent": "Mozilla/5.0 (paper-desk/1.0; read-only research)",
              "Accept": "application/json", "Origin": "https://pump.fun", "Referer": "https://pump.fun/"}
PUMPFUN_AVAILABLE = None  # tri-state cache for this process: None unknown, True/False


def pumpfun_available():
    """Pump.fun's frontend API is often behind a Cloudflare challenge from datacenter IPs."""
    global PUMPFUN_AVAILABLE
    return PUMPFUN_AVAILABLE


def _pumpfun_get(url):
    global PUMPFUN_AVAILABLE
    try:
        d = http_json(url, timeout=12, retries=0, headers=PF_HEADERS)
        PUMPFUN_AVAILABLE = True
        return d
    except Exception as e:
        if "403" in str(e) or "Cloudflare" in str(e) or "non-JSON" in str(e):
            PUMPFUN_AVAILABLE = False
            return None
        raise


def pumpfun_coin(mint):
    """Public pump.fun coin metadata, or None if the API is blocked/unavailable."""
    return _pumpfun_get("https://frontend-api-v3.pump.fun/coins/" + mint)


def pumpfun_created_coins(wallet, limit=50):
    """Creator's pump.fun launches, newest first. None if the API is blocked; [] if none."""
    d = _pumpfun_get("https://frontend-api-v3.pump.fun/coins/user-created-coins/%s?limit=%d&offset=0&includeNsfw=true"
                     % (wallet, int(limit)))
    if d is None:
        return None
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ("coins", "data", "tokens", "results"):
            if isinstance(d.get(k), list):
                return d[k]
    raise RuntimeError("unexpected pump.fun created-coins shape")


def bundle_share_from_rugcheck(rc):
    """Coordinated/insider cluster share as a bundled-launch proxy.

    Returns (pct, evidence). A successful RugCheck report always yields a number:
    missing/empty insiderNetworks means 0% (no cluster detected), not unknown.
    Uses max(tokenAmount, currentHolding) / supply so sold-down clusters still count
    at their received size, which is closer to a launch bundle than current holdings alone.
    """
    if not isinstance(rc, dict) or not rc.get("mint"):
        raise RuntimeError("invalid RugCheck report")
    supply = f((rc.get("token") or {}).get("supply"))
    if not supply:
        raise RuntimeError("RugCheck supply missing")
    nets = rc.get("insiderNetworks") or []
    if not isinstance(nets, list):
        nets = []
    amt = sum(f(n.get("tokenAmount"), 0) or 0 for n in nets if isinstance(n, dict))
    cur = sum(f(n.get("currentHolding"), 0) or 0 for n in nets if isinstance(n, dict))
    pct = max(amt, cur) / supply * 100
    ev = {"insider_networks": len(nets), "graph_insiders": rc.get("graphInsidersDetected") or 0,
          "token_amount_pct": round(amt / supply * 100, 3), "current_holding_pct": round(cur / supply * 100, 3)}
    return pct, ev


def _mint_of(tok):
    if isinstance(tok, str):
        return tok
    if not isinstance(tok, dict):
        return None
    return tok.get("mint") or tok.get("token_address") or tok.get("address") or tok.get("token")


def prior_tokens_from_rugcheck(rc, current_mint):
    """Other tokens RugCheck has attributed to this creator. [] if none detected (field null/absent)."""
    ct = rc.get("creatorTokens")
    if not ct:
        return []
    if not isinstance(ct, list):
        return []
    out = []
    for t in ct:
        m = _mint_of(t)
        if m and m != current_mint:
            row = dict(t) if isinstance(t, dict) else {"mint": t}
            row["mint"] = m
            out.append(row)
    return out


def implied_ath_mcap(ds, current):
    if current is None:
        return None
    highs = [current]
    if ds:
        for ch in (ds.get("chg_h1"), ds.get("chg_h6"), ds.get("chg_h24")):
            if ch is not None and ch > -99.999:
                highs.append(current / (1 + ch / 100.0))
    return max(highs)


def extract_ath_and_current(tok, ds=None):
    """Best-effort (ath_mcap, current_mcap) from a creator-token record + optional DexScreener pair."""
    ath = current = None
    if isinstance(tok, dict):
        ath = f(tok.get("token_ath_mc") or tok.get("ath_market_cap") or tok.get("peakMarketCapUSD")
                or tok.get("ath_mc") or tok.get("ath") or tok.get("usd_ath_market_cap"))
        current = f(tok.get("market_cap") or tok.get("usd_market_cap") or tok.get("mc")
                    or tok.get("fdv") or tok.get("usd_market_cap_now"))
    if ds:
        current = f(ds.get("mcap")) if ds.get("mcap") is not None else current
        impl = implied_ath_mcap(ds, current)
        if impl is not None:
            ath = max(ath, impl) if ath is not None else impl
    return ath, current


def is_rug_by_drawdown(ath, current, min_ath, dd_pct):
    if ath is None or current is None:
        return False
    return ath >= min_ath and current <= ath * (1 - dd_pct / 100)


def classify_prior_rugs(priors, ds_by_mint, min_ath, dd_pct):
    """priors: list of dicts with at least 'mint'. Returns (rugs_symbols_or_mints, details)."""
    rugs, details = [], []
    for t in priors:
        m = _mint_of(t)
        ds = (ds_by_mint or {}).get(m)
        ath, cur = extract_ath_and_current(t, ds)
        is_rug = is_rug_by_drawdown(ath, cur, min_ath, dd_pct)
        sym = (t.get("symbol") if isinstance(t, dict) else None) or (ds or {}).get("symbol") or (m[:6] if m else "?")
        details.append({"mint": m, "symbol": sym, "ath": ath, "current": cur, "rug": is_rug})
        if is_rug:
            rugs.append(sym)
    return rugs, details


def analyst_weights(weights, include_smart_wallets):
    """If the smart-wallet factor is dropped, redistribute its weight across the other five."""
    W = dict(weights)
    if include_smart_wallets or "smart_wallets" not in W:
        return W
    sw = W.pop("smart_wallets")
    others = sum(W.values()) or 1
    return {k: v + sw * v / others for k, v in W.items()}
