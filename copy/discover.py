#!/usr/bin/env python3
"""Rank candidate copy wallets. Manual / workflow_dispatch only, not the hot loop.

PAPER ONLY. Reads public swaps. Never asks for or writes a private key.
A human copies addresses into copy/wallets.json and sets enabled.
"""
import json
import time
from pathlib import Path

from desk.sources import http_json

from .rpc import rpc_call, rpc_endpoints

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "candidates.json"


def _pairs(limit):
    """Solana pairs that later ran at least 3x on a DexScreener change window."""
    found = []
    for q in ("solana meme", "pump"):
        try:
            d = http_json("https://api.dexscreener.com/latest/dex/search?q=" + q.replace(" ", "%20"))
        except Exception:
            continue
        for p in d.get("pairs") or []:
            if p.get("chainId") != "solana":
                continue
            ch = (p.get("priceChange") or {})
            best = max([float(ch.get(k) or 0) for k in ("h1", "h6", "h24")])
            if best < 200:  # 3x is +200%
                continue
            found.append(p)
            if len(found) >= limit:
                return found
        time.sleep(0.3)
    return found


def _early_buyers(http, pair, pages=1):
    """Oldest signatures in the most recent page are the earliest this sample can see."""
    sigs = rpc_call(http, "getSignaturesForAddress", [pair, {"limit": 40, "commitment": "confirmed"}]) or []
    sigs = list(reversed(sigs))  # oldest first inside this page
    buyers = []
    for row in sigs[:25]:
        sig = row.get("signature")
        if not sig or row.get("err"):
            continue
        try:
            tx = rpc_call(http, "getTransaction", [sig, {
                "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed",
            }])
        except Exception:
            continue
        if not tx or (tx.get("meta") or {}).get("err"):
            continue
        for b in (tx.get("meta") or {}).get("postTokenBalances") or []:
            owner = b.get("owner")
            pre = 0.0
            for pb in (tx.get("meta") or {}).get("preTokenBalances") or []:
                if pb.get("owner") == owner and pb.get("mint") == b.get("mint"):
                    pre = float((pb.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            post = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            if owner and post > pre:
                buyers.append(owner)
        if len(buyers) >= 8:
            break
    return buyers


def _too_fast(http, wallet):
    sigs = rpc_call(http, "getSignaturesForAddress", [wallet, {"limit": 200, "commitment": "confirmed"}]) or []
    if len(sigs) < 200:
        return False
    newest = sigs[0].get("blockTime") or 0
    oldest = sigs[-1].get("blockTime") or 0
    span = max(1, newest - oldest)
    return (len(sigs) / span) * 86400 > 200


def _score_wallet(http, wallet, sol_price):
    from .parse import parse_swap
    sigs = rpc_call(http, "getSignaturesForAddress", [wallet, {"limit": 30, "commitment": "confirmed"}]) or []
    pnl = 0.0
    wins = 0
    closed = 0
    open_cost = {}
    for row in reversed(sigs):
        sig = row.get("signature")
        if not sig:
            continue
        try:
            tx = rpc_call(http, "getTransaction", [sig, {
                "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed",
            }])
        except Exception:
            continue
        if tx:
            tx.setdefault("transaction", {}).setdefault("signatures", [sig])
        swap = parse_swap(tx, wallet, sol_price_usd=sol_price)
        if not swap:
            continue
        if swap["side"] == "buy":
            open_cost[swap["mint"]] = open_cost.get(swap["mint"], 0.0) + swap["sol_amount"]
        else:
            cost = open_cost.get(swap["mint"])
            if cost:
                closed += 1
                trade = swap["sol_amount"] - cost
                pnl += trade
                if trade > 0:
                    wins += 1
                open_cost[swap["mint"]] = 0.0
    win_rate = (wins / closed) if closed else 0.0
    return {"win_rate": round(win_rate, 3), "closed": closed, "pnl_sol": round(pnl, 4)}


def discover(max_coins=3, max_wallets=10):
    eps = rpc_endpoints()
    http = eps["http"]
    pairs = _pairs(max_coins)
    counts = {}
    for p in pairs:
        pair = p.get("pairAddress")
        if not pair:
            continue
        try:
            for owner in _early_buyers(http, pair):
                counts[owner] = counts.get(owner, 0) + 1
        except Exception:
            continue
        time.sleep(0.2)
    try:
        from desk.sources import sol_price_usd
        sol = sol_price_usd()
    except Exception:
        sol = 150.0
    ranked = []
    for wallet, n in sorted(counts.items(), key=lambda kv: -kv[1])[: max_wallets * 3]:
        try:
            if _too_fast(http, wallet):
                continue
            score = _score_wallet(http, wallet, sol)
        except Exception:
            continue
        if score["closed"] < 1:
            continue
        score.update({"address": wallet, "early_buys": n, "score": round(score["win_rate"] * 100 + score["pnl_sol"], 3)})
        ranked.append(score)
    ranked.sort(key=lambda r: -r["score"])
    out = {"generated_at": time.time(), "rpc": eps["name"], "candidates": ranked[:max_wallets],
           "note": "PAPER ONLY research. A human enables wallets in copy/wallets.json. Deployers and >200 trades/day are dropped."}
    OUT.write_text(json.dumps(out, indent=1))
    return out


def main(argv):
    max_coins = 3
    if "--max-coins" in argv:
        max_coins = int(argv[argv.index("--max-coins") + 1])
    out = discover(max_coins=max_coins)
    print(json.dumps({"candidates": len(out["candidates"]), "file": str(OUT)}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
