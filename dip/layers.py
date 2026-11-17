"""Analyst (bot 2): 8 layers in order. First red stops; the rest are "not checked". No data = red.

`data` is any object with the methods LiveData (sources.py) implements, so tests pass a fake:
mint_info, report, graph, creator_balance, prior_mints, is_rug, top_holders, token_accounts.
Each method returns None when the source gave no data.
"""
from desk.sources import bundle_share_from_rugcheck

from .scanner import KNOWN_PROGRAM_OWNERS, top10_share

NAMES = ["mint authority", "freeze authority", "dev", "top 10", "liquidity", "pool age", "bundles", "delegations"]


def _res(n, status, value=None, source=None, reason=None, **extra):
    d = {"n": n, "name": NAMES[n - 1], "status": status, "value": value, "source": source, "reason": reason}
    d.update(extra)
    return d


def _ok(n, value, source, **x):
    return _res(n, "ok", value, source, None, **x)


def _red(n, reason, source=None, value=None, **x):
    return _res(n, "red", value, source, reason, **x)


def _short(a):
    return f"{a[:4]}...{a[-3:]}" if a and len(a) > 9 else str(a)


def layer1(ctx, data, cfg):
    mi = ctx.setdefault("_mint", data.mint_info(ctx["mint"]))
    if mi is None:
        return _red(1, "no data: mint", "rpc")
    if mi.get("mintAuthority") is None:
        return _ok(1, "Revoked", "rpc")
    return _red(1, "mint not revoked", "rpc", value=mi.get("mintAuthority"))


def layer2(ctx, data, cfg):
    mi = ctx.get("_mint")
    if mi is None:
        return _red(2, "no data: mint", "rpc")
    if mi.get("freezeAuthority") is None:
        return _ok(2, "Revoked", "rpc")
    return _red(2, "freeze not revoked", "rpc", value=mi.get("freezeAuthority"))


def layer3(ctx, data, cfg):
    a = cfg["analyst"]
    rep = data.report(ctx["mint"])
    creator = ctx.get("creator") or (rep or {}).get("creator")
    if not creator:
        return _red(3, "no data: dev", "room/rugcheck")
    supply = (ctx.get("_mint") or {}).get("supply")
    bal = data.creator_balance(creator, ctx["mint"])
    if bal is None and rep and isinstance(rep.get("creatorBalance"), (int, float)):
        bal = float(rep["creatorBalance"])
    if bal is None or not supply:
        return _red(3, "no data: dev", "rpc")
    pct = bal / supply * 100
    room_bal = ctx.get("creator_balance")
    if room_bal is not None and bal < room_bal:
        return _red(3, "dev sold", "rpc", value=f"{pct:.1f}%")
    if pct >= a["max_dev_pct"]:
        return _red(3, f"dev holds {pct:.0f}%", "rpc", value=f"{pct:.1f}%")
    ctx["creator_balance_now"] = bal
    priors = data.prior_mints(creator, ctx["mint"]) or []
    rugged = [m for m in priors if data.is_rug(m)]
    if rugged:
        return _red(3, f"dev rugged {len(rugged)} before", "rugs", value=f"{pct:.1f}%")
    return _ok(3, f"{pct:.1f}%" + (" (fresh dev)" if not priors else ""), "rpc+rugcheck")


def layer4(ctx, data, cfg):
    a = cfg["analyst"]
    rows = data.top_holders(ctx["mint"])
    supply = (ctx.get("_mint") or {}).get("supply")
    if rows is None or not supply:
        return _red(4, "no data: top10", "rpc")
    ctx["_holders"] = rows
    pct = top10_share(rows, supply, KNOWN_PROGRAM_OWNERS | {ctx.get("pool"), ctx.get("curve")} | set(ctx.get("exclude") or []))
    if pct >= a["max_top10_pct"]:
        return _red(4, f"top 10 hold {pct:.0f}%", "rpc", value=pct)
    return _ok(4, f"{pct:.1f}%", "rpc")


def layer5(ctx, data, cfg):
    a = cfg["analyst"]
    rep = data.report(ctx["mint"])
    if not rep:
        return _red(5, "no data: liquidity", "rugcheck")
    pool = ctx.get("pool")
    mk = None
    for m in rep.get("markets") or []:
        if not isinstance(m, dict):
            continue
        if (pool and m.get("pubkey") == pool) or (not pool and "pump" in str(m.get("marketType", "")).lower()):
            mk = m
            break
    if mk is None:
        return _red(5, "no data: liquidity", "rugcheck")
    locked = (mk.get("lp") or {}).get("lpLockedPct")
    if locked is None:
        return _red(5, "no data: liquidity", "rugcheck")
    if locked < a["min_lp_locked_pct"]:
        return _red(5, "liquidity unlocked", "rugcheck", value=f"{locked:.0f}% locked")
    s_liq, now_liq = ctx.get("liq_scanner"), ctx.get("liq_now")
    if s_liq and now_liq is not None:
        drop = (1 - now_liq / s_liq) * 100
        if drop > a["max_liq_drop_pct"]:
            return _red(5, f"liquidity down {drop:.0f}%", "dexscreener", value=f"{drop:.0f}%")
    return _ok(5, f"{locked:.0f}% burned", "rugcheck")


def layer6(ctx, data, cfg):
    age = ctx.get("pool_age_min")
    if age is None:
        return _red(6, "no data: pool age", "dexscreener")
    if age < cfg["analyst"]["min_pool_age_min"]:
        return _red(6, "pool too young", "dexscreener", value=f"{age:.0f}m")
    return _ok(6, f"{age:.0f}m", "dexscreener")


class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def _edges(graph):
    """Defensive edge/node extraction from a dict or list-of-networks graph."""
    nets = graph if isinstance(graph, list) else [graph]
    edges = []
    for n in nets:
        if not isinstance(n, dict):
            continue
        for e in n.get("links") or n.get("edges") or []:
            if isinstance(e, dict):
                a, b = e.get("source") or e.get("from"), e.get("target") or e.get("to")
                if isinstance(a, dict):
                    a = a.get("id")
                if isinstance(b, dict):
                    b = b.get("id")
                if a and b:
                    edges.append((a, b))
    return edges


def biggest_group(graph, holders):
    """Union-find over graph edges between wallets that hold this mint. Returns the biggest group size (>=1)."""
    uf = _UF()
    hs = set(holders)
    for a, b in _edges(graph):
        if a in hs and b in hs:
            uf.union(a, b)
    sizes = {}
    for x in list(uf.p):
        r = uf.find(x)
        sizes[r] = sizes.get(r, 0) + 1
    return max(sizes.values()) if sizes else 1


def layer7(ctx, data, cfg):
    a = cfg["analyst"]
    graph = data.graph(ctx["mint"])
    if graph is None:
        return _red(7, "no data: bundles", "rugcheck")
    rep = data.report(ctx["mint"])
    if not rep:
        return _red(7, "no data: bundles", "rugcheck")
    holders = {h.get("owner") for h in ctx.get("_holders") or [] if h.get("owner")}
    holders |= {h.get("address") for h in rep.get("topHolders") or [] if isinstance(h, dict) and h.get("address")}
    holders |= {h.get("owner") for h in rep.get("topHolders") or [] if isinstance(h, dict) and h.get("owner")}
    holders.discard(None)
    group = biggest_group(graph, holders)
    ctx["bundle_group"] = group
    if group >= a["max_bundled"]:
        return _red(7, f"{group} bundled wallets", "rugcheck", value=group)
    try:
        pct, _ev = bundle_share_from_rugcheck(rep)
    except RuntimeError:
        return _red(7, "no data: bundles", "rugcheck")
    missing = "insiderNetworks" not in rep
    if pct >= a["max_insiders_pct"]:
        return _red(7, f"insiders hold {pct:.0f}%", "rugcheck", value=group, insiders_pct=round(pct, 2))
    return _ok(7, f"group {group}, insiders {pct:.1f}%", "rugcheck", insiders_pct=round(pct, 2),
               note="insiderNetworks missing: counted as 0 networks" if missing else None)


def layer8(ctx, data, cfg):
    a = cfg["analyst"]
    accts = data.token_accounts(ctx["mint"])
    if accts is None:
        return _red(8, "no data: delegations", "rpc")
    by = {}
    for x in accts:
        if x.get("delegate") and (x.get("delegated") or 0) > 0:
            by[x["delegate"]] = by.get(x["delegate"], 0) + 1
    ex = KNOWN_PROGRAM_OWNERS | {ctx.get("pool"), ctx.get("curve")} | set(ctx.get("exclude") or [])
    owners = [x.get("owner") for x in sorted(accts, key=lambda r: -(r.get("amount") or 0))
              if x.get("owner") and x["owner"] not in ex and x.get("address") not in ex][:a["fresh_top_n"]]
    ctx["holder_owners"] = owners
    if by:
        d, n = max(by.items(), key=lambda kv: kv[1])
        if n > a["max_delegated_accounts"]:
            return _red(8, f"{n} wallets -> {_short(d)}", "rpc", value=n, holder_owners=owners)
    return _ok(8, "None", "rpc", holder_owners=owners)


LAYERS = [layer1, layer2, layer3, layer4, layer5, layer6, layer7, layer8]


def run_layers(ctx, data, cfg):
    """ctx: mint, pool, creator, creator_balance (room entry), liq_scanner, liq_now, pool_age_min, exclude.
    Returns 8 result dicts. Stops at the first red; later ones are status "not checked".
    An exception inside a layer is "no data", never a pass."""
    out, stopped = [], False
    for i, fn in enumerate(LAYERS, 1):
        if stopped:
            out.append(_res(i, "not checked"))
            continue
        try:
            r = fn(ctx, data, cfg)
        except Exception as e:
            r = _red(i, f"no data: {NAMES[i - 1].split()[0]}", "error", value=type(e).__name__)
        out.append(r)
        if r["status"] != "ok":
            stopped = True
    return out
