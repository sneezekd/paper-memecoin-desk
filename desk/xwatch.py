"""X watchlist from a GitHub issue comment. PAPER ONLY. No trading authority.

Only a comment whose author login is sneezekd and whose author_association is
OWNER is trusted. A bad or expired payload is logged by the caller and ignored.
The boost cannot skip a filter gate or the Analyst minimum score.
"""
import json
import os
import re
from datetime import datetime, timezone

from .sources import http_json

TRUSTED_LOGIN = "sneezekd"
TRUSTED_ASSOCIATION = "OWNER"
_FENCE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.I | re.S)


def comment_is_trusted(comment):
    user = (comment or {}).get("user") or {}
    return user.get("login") == TRUSTED_LOGIN and (comment or {}).get("author_association") == TRUSTED_ASSOCIATION


def extract_json_block(body):
    """The first fenced JSON object, or None if the fence is missing or not JSON."""
    if not body:
        return None
    m = _FENCE.search(body)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _parse_time(value):
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def coins_from_payload(payload, now_ts):
    """Return (coins_by_mint, meta). Raises ValueError when the payload is unusable or expired."""
    if not isinstance(payload, dict):
        raise ValueError("payload is not an object")
    generated = _parse_time(payload.get("generated_at"))
    if generated is None:
        raise ValueError("generated_at missing or unparseable")
    try:
        ttl_h = float(payload.get("ttl_hours"))
    except (TypeError, ValueError):
        raise ValueError("ttl_hours missing")
    if ttl_h < 0:
        raise ValueError("ttl_hours negative")
    if now_ts - generated > ttl_h * 3600:
        raise ValueError("comment older than ttl_hours")
    coins = payload.get("coins")
    if not isinstance(coins, list):
        raise ValueError("coins missing")
    out = {}
    for coin in coins:
        if not isinstance(coin, dict):
            continue
        mint = coin.get("mint")
        if not isinstance(mint, str) or len(mint) < 32:
            continue
        try:
            score = float(coin.get("x_score"))
        except (TypeError, ValueError):
            continue
        score = max(0.0, min(100.0, score))
        out[mint] = {
            "mint": mint,
            "symbol": coin.get("symbol"),
            "x_score": score,
            "mentions": coin.get("mentions"),
            "distinct_accounts": coin.get("distinct_accounts"),
            "notes": coin.get("notes"),
        }
    return out, {"generated_at": payload.get("generated_at"), "ttl_hours": ttl_h, "n": len(out)}


def pick_watchlist(comments, now_ts):
    """Newest trusted comment that parses and is still inside its ttl.

    Returns (coins_by_mint, info). info['ignored'] lists why newer trusted comments were skipped.
    """
    ordered = sorted(comments or [], key=lambda c: (c or {}).get("updated_at") or (c or {}).get("created_at") or "", reverse=True)
    ignored = []
    for comment in ordered:
        if not comment_is_trusted(comment):
            continue
        payload = extract_json_block((comment or {}).get("body") or "")
        if payload is None:
            ignored.append("bad json")
            continue
        try:
            coins, meta = coins_from_payload(payload, now_ts)
        except ValueError as e:
            ignored.append(str(e))
            continue
        meta["comment_id"] = comment.get("id")
        meta["ignored"] = ignored
        return coins, meta
    return {}, {"ignored": ignored or ["no trusted comment"], "n": 0}


def apply_x_boost(base_score, x_score, max_boost, min_score, coverage_ok):
    """Nudge a score that already cleared min_score. Never turns a miss into a pass.

    Returns (shown_score, boost, passed).
    """
    boost = 0.0
    if x_score is not None:
        xs = max(0.0, min(100.0, float(x_score)))
        boost = round(float(max_boost) * xs / 100.0, 2)
    shown = round(min(100.0, float(base_score) + boost), 1)
    passed = bool(coverage_ok and float(base_score) >= float(min_score))
    return shown, boost, passed


def fetch_issue_comments(repo, issue, token=None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "paper-desk"}
    if token:
        headers["Authorization"] = "Bearer " + token
    out = []
    for page in range(1, 6):
        url = f"https://api.github.com/repos/{repo}/issues/{int(issue)}/comments?per_page=100&page={page}"
        batch = http_json(url, timeout=15, retries=1, headers=headers)
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
    return out


def load_x_watchlist(log, cfg, now_ts=None, fetch=None):
    """Cached watchlist for one scan. Failures return {} and are logged. Never raises into the scan."""
    xw = (cfg or {}).get("x_watchlist") or {}
    if not xw.get("enabled"):
        return {}
    cache_s = float(xw.get("cache_seconds") or 300)
    cached = log.cache_get("x_watchlist", cache_s) if log is not None else None
    if isinstance(cached, dict) and isinstance(cached.get("coins"), dict):
        return cached["coins"]
    import time
    now_ts = time.time() if now_ts is None else now_ts
    try:
        token = (os.environ.get("GITHUB_TOKEN") or "").strip() or None
        comments = (fetch or fetch_issue_comments)(xw.get("repo") or "sneezekd/paper-memecoin-desk", xw.get("issue_number") or 8, token)
        coins, meta = pick_watchlist(comments, now_ts)
    except Exception as e:
        if log is not None:
            log.event("x_watchlist_error", error=str(e)[:200])
        return {}
    if log is not None:
        log.cache_set("x_watchlist", {"coins": coins, "meta": meta})
        log.event("x_watchlist", n=meta.get("n"), generated_at=meta.get("generated_at"),
                  comment_id=meta.get("comment_id"), ignored=meta.get("ignored"))
    return coins
