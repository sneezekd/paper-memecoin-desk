"""Telegram notifier. PAPER ONLY messages. Enabled only when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are
both non-empty; otherwise a no-op that logs "telegram: off" once. The token is never logged, never put
in an exception message and never written to the dashboard. Failures never raise.
"""
import json
import os
import time
import urllib.request

TIMEOUT_S = 5
MIN_GAP_S = 1.0
_state = {"off_logged": False, "last_send": 0.0}


def enabled():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def _post(token, chat_id, text):
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    body = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=TIMEOUT_S).read()


def send(text, post=None, sleep=time.sleep, clock=time.time):
    """Best-effort message, at most ~1 per second. `post` is injectable for tests. Returns True if sent."""
    if not enabled():
        if not _state["off_logged"]:
            _state["off_logged"] = True
            print("telegram: off", flush=True)
        return False
    wait = MIN_GAP_S - (clock() - _state["last_send"])
    if 0 < wait <= MIN_GAP_S:
        sleep(wait)
    _state["last_send"] = clock()
    try:
        (post or _post)(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"], text[:3900])
        return True
    except Exception as e:  # message deliberately omitted: urllib errors can embed the URL (token)
        print("telegram: send failed (%s)" % type(e).__name__, flush=True)
        return False


# ---- message formats (all start with PAPER) ----
def _name(sym, mint):
    return sym or (mint or "?")[:6]


def fmt_bought(sym, mint, size, pct_cash, score, depth_pct, dex_url=None):
    return (f"PAPER bought {_name(sym, mint)} ${size:.2f} ({pct_cash:.1f}% of cash) score {score} "
            f"dip -{depth_pct:.0f}%\n{dex_url or 'https://dexscreener.com/solana/' + mint}")


def fmt_exit(sym, mint, reason, pnl_usd, pnl_pct):
    """take profit / trail / stop / any other exit, with reason and $/% P&L."""
    label = {"take profit": "take profit 50%", "trail": "sold (trail)", "stop": "stopped"}.get(reason)
    head = f"PAPER {label}" if label else f"PAPER exit ({reason})"
    return f"{head} {_name(sym, mint)} ${pnl_usd:+.2f} ({pnl_pct:+.1f}%) reason: {reason}"


def fmt_night_loss(loss_pct):
    return f"PAPER night loss stop hit at -{loss_pct:.1f}%: no more buys tonight"


def fmt_night_over(cancelled, sold, realized):
    return f"PAPER night over: cancelled {cancelled} orders, sold {sold} positions, realized ${realized:+.2f}"


def fmt_summary(s):
    """s: dict from report.summary_data."""
    top = ", ".join(f"{k} {v}" for k, v in s["top_skips"]) or "none"
    kills = ", ".join(f"{k} {v}" for k, v in s["kills"]) or "none"
    return (f"PAPER daily summary\ncash ${s['cash']:.2f} | equity ${s['equity']:.2f} | night P&L ${s['night_pnl']:+.2f}\n"
            f"trades {s['trades']} | wins {s['wins']} / losses {s['losses']}\n"
            f"top skip reasons: {top}\nscanner kills: {kills}")
