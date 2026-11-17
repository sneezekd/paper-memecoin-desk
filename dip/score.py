"""Score (code): alive / organic / flush / chase -> core, optional social, pass gate. PAPER ONLY.

Everything except `fresh_pct` and `evaluate` is pure. Network access (signature counts) and sleeping
are injected so tests run offline.
"""
import math
import time

SOCIAL_OFF_LOG = "social: off"
SMART_LOG = "smart: unavailable"
ORGANIC_LOG = "organic: txns"
FLUSH_LOG = "flush: txns"


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def rnd(x):
    """Round half up (Python's round() is banker's rounding; a score of 69.5 must become 70)."""
    return int(math.floor(x + 0.5))


def _n(v):
    return float(v) if isinstance(v, (int, float)) else 0.0


def fresh_pct(owners, sig_count, cfg, sleep=time.sleep, pace_s=0.5):
    """Share (%) of the top holder owners with fewer than fresh_sig_min signatures (limit 20).
    owners: layer 8 holder owners (pool/curve already excluded). sig_count(owner) -> int|None.
    Calls are spaced pace_s apart via the injected sleep. Owners with no answer are left out of the
    denominator. Returns (pct|None, checked, fresh)."""
    a = cfg["analyst"]
    checked = fresh = 0
    for i, o in enumerate((owners or [])[:a["fresh_top_n"]]):
        if i:
            sleep(pace_s)
        try:
            n = sig_count(o)
        except Exception:
            n = None
        if n is None:
            continue
        checked += 1
        if n < a["fresh_sig_min"]:
            fresh += 1
    return (round(fresh / checked * 100, 2) if checked else None), checked, fresh


def score_parts(row, bundle_group, curve_min, fresh, cfg, social=None):
    """Pure. row: DexScreener row (vol_h1, buys_h1, sells_h1, buys_m5, sells_m5, chg_h1).
    bundle_group: layer 7 biggest group (None = unknown), curve_min: minutes on the curve (None = unknown),
    fresh: fresh_pct or None, social: 0..1 or None. Social is only used when cfg social.enabled."""
    a, s = cfg["analyst"], cfg["social"]
    log = []
    alive = clamp(_n(row.get("vol_h1")) / a["alive_volume_h1"])
    buys_h1, sells_h1 = _n(row.get("buys_h1")), _n(row.get("sells_h1"))
    organic = clamp(buys_h1 / a["organic_buys_h1"])
    log.append(ORGANIC_LOG)
    mult = []
    if sells_h1 > buys_h1:
        mult.append(("sells>buys", a["bundled_penalty"]))
    if bundle_group is not None and bundle_group >= a["bundled_penalty_min"]:
        mult.append((f"bundled group {bundle_group}", a["bundled_penalty"]))
    if curve_min is not None and curve_min < a["fast_curve_min"]:
        mult.append((f"fast curve {curve_min:g}m", a["bundled_penalty"]))
    if fresh is not None and fresh >= a["max_fresh_pct"]:
        mult.append((f"fresh {fresh:g}%", a["bundled_penalty"]))
    for _name, m in mult:
        organic *= m
    b5, s5 = _n(row.get("buys_m5")), _n(row.get("sells_m5"))
    flush = 1.0 if s5 <= b5 else 0.5 if s5 <= a["flush_sell_ratio"] * b5 else 0.0
    log.append(FLUSH_LOG)
    chase = clamp(_n(row.get("chg_h1")) / a["chase_h1_pct"])
    core = alive + organic + flush + (1 - chase)
    if s["enabled"]:
        w, soc = s["social_weight"], clamp(social or 0.0)
    else:
        w, soc = 0.0, None
        log.append(SOCIAL_OFF_LOG)
    log.append(SMART_LOG)  # trading-app smart-holders leaderboard is not available; s = 0
    score = rnd(100 * (core + w * (soc or 0.0)) / (4 + w))
    return {"alive": round(alive, 4), "organic": round(organic, 4), "organic_mult": [m[0] for m in mult],
            "flush": flush, "chase": round(chase, 4), "core": round(core, 4), "social": soc,
            "social_weight": w, "fresh_pct": fresh, "bundle_group": bundle_group, "curve_min": curve_min,
            "score": score, "social_pts": score - rnd(100 * core / 4), "log": log}


def gate(layers, score, cfg):
    """Pure. ("pass"|"reject", reason). Any layer that is not ok rejects whatever the score says."""
    for r in layers:
        if r.get("status") != "ok":
            return "reject", r.get("reason") or f"layer {r.get('n')} {r.get('status')}"
    if score < cfg["analyst"]["min_score"]:
        return "reject", f"score {score} < {cfg['analyst']['min_score']}"
    return "pass", None


def evaluate(ctx, row, layers, sig_count, cfg, book=None, sleep=time.sleep, social=None, now=None):
    """Score one coin after the 8 layers and save the decision (rejects too) with all parts.
    ctx: layers' ctx (bundle_group, holder_owners, curve_min). Returns {score, verdict, reason, parts}."""
    owners = ctx.get("holder_owners")
    if owners is None and len(layers) >= 8:
        owners = layers[7].get("holder_owners")
    fresh, checked, nfresh = (None, 0, 0)
    if owners and layers and all(r.get("status") == "ok" for r in layers):
        fresh, checked, nfresh = fresh_pct(owners, sig_count, cfg, sleep)
    parts = score_parts(row, ctx.get("bundle_group"), ctx.get("curve_min"), fresh, cfg, social)
    parts["fresh_checked"] = checked
    verdict, reason = gate(layers, parts["score"], cfg)
    if book is not None:
        book.save_audit(ctx["mint"], layers, parts, parts["score"], verdict, reason, ts=now)
    return {"score": parts["score"], "verdict": verdict, "reason": reason, "parts": parts}
