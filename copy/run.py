#!/usr/bin/env python3
"""Paper copy desk runner. No wallet, no signing, no live order.

  python3 copy/run.py listen     # WebSocket loop used by GitHub Actions
  python3 copy/run.py discover   # one-shot wallet ranking, not the hot loop
  python3 copy/run.py dashboard  # refresh reports/copy_dashboard.json
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from copy.engine import CopyBook, backoff_seconds, load_config, load_wallets
from copy.parse import detection_latency, parse_swap
from copy.rpc import WsClient, rpc_call, rpc_endpoints
from desk.sources import ds_best_pairs, sol_price_usd


def _sol_price():
    now = time.time()
    cached = getattr(_sol_price, "v", None)
    if cached and now - cached[0] < 30:
        return cached[1]
    px = sol_price_usd()
    _sol_price.v = (now, px)
    return px


def _labels():
    return {w["address"]: w.get("label") or "" for w in load_wallets()}


def _apply_tx(book, tx, detected_at, labels):
    acted = False
    for wallet, label in labels.items():
        swap = parse_swap(tx, wallet, sol_price_usd=_sol_price())
        if not swap:
            continue
        swap["latency_s"] = detection_latency(swap.get("block_time"), detected_at)
        book.event_log(
            "detect", wallet=wallet, mint=swap["mint"], side=swap["side"], signature=swap.get("signature"),
            latency_s=swap["latency_s"], note=f"latency {swap['latency_s']}s" if swap["latency_s"] is not None else "latency unknown",
        )
        print(f"detect {swap['side']} {swap['mint'][:6]} latency={swap['latency_s']}s", flush=True)
        if swap["side"] == "buy":
            if book.try_entry(swap, label=label):
                acted = True
        elif book.on_leader_sell(swap):
            acted = True
    return acted


def _fetch_tx(http, signature):
    return rpc_call(http, "getTransaction", [signature, {
        "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed",
    }])


def _remark(book, cfg):
    mints = [p["mint"] for p in book.open_positions()]
    marks = {}
    if mints:
        try:
            pairs, _errs = ds_best_pairs(mints, set(cfg["safety"]["allowed_dex_ids"]))
            for mint, row in pairs.items():
                marks[mint] = {"price_usd": row.get("price_usd"), "liq_usd": row.get("liq_usd")}
        except Exception as e:
            print("remark failed", str(e)[:160], flush=True)
    n = book.manage(marks)
    book.dashboard()
    return n


def _poll_new(book, http, labels, cursors):
    """Signatures newer than the cursor. Used only while the socket is down."""
    acted = False
    for wallet in labels:
        try:
            sigs = rpc_call(http, "getSignaturesForAddress", [wallet, {"limit": 8, "commitment": "confirmed"}]) or []
        except Exception as e:
            print("poll failed", wallet[:6], str(e)[:120], flush=True)
            continue
        fresh = []
        for row in sigs:
            sig = row.get("signature")
            if not sig or sig == cursors.get(wallet) or book.seen(sig):
                break
            fresh.append(sig)
        for sig in reversed(fresh):
            book.mark_seen(sig)
            try:
                tx = _fetch_tx(http, sig)
            except Exception as e:
                print("getTransaction failed", str(e)[:120], flush=True)
                continue
            if tx:
                tx.setdefault("transaction", {}).setdefault("signatures", [sig])
                if _apply_tx(book, tx, time.time(), {wallet: labels[wallet]}):
                    acted = True
        if sigs:
            cursors[wallet] = sigs[0]["signature"]
    return acted


def _publish():
    if os.environ.get("COPY_PUSH_STATE") != "1":
        return
    import subprocess
    subprocess.call(["bash", str(ROOT / "scheduler.sh"), "publish-copy"])


def listen():
    eps = rpc_endpoints()
    if eps["name"] == "helius":
        os.environ["SOLANA_RPC_URL"] = eps["http"]
    cfg = load_config()
    book = CopyBook()
    book.dashboard()
    labels = _labels()
    print(f"copy listen via {eps['name']} wallets={len(labels)} PAPER ONLY", flush=True)
    http = eps["http"]
    cursors = {}
    for wallet in labels:
        try:
            sigs = rpc_call(http, "getSignaturesForAddress", [wallet, {"limit": 1}]) or []
        except Exception:
            sigs = []
        if sigs and sigs[0].get("signature"):
            cursors[wallet] = sigs[0]["signature"]
            book.mark_seen(sigs[0]["signature"])
    deadline = time.time() + float(os.environ.get("COPY_MAX_SECONDS") or 20700)
    remark_s = float(cfg["loop"]["remark_seconds"])
    cap = float(cfg["loop"]["reconnect_max_seconds"])
    ws = None
    attempt = 0
    next_remark = time.time()
    next_publish = time.time() + 180
    while time.time() < deadline:
        if not labels:
            if time.time() >= next_remark:
                book.dashboard()
                next_remark = time.time() + remark_s
            time.sleep(remark_s)
            continue
        if ws is None:
            try:
                client = WsClient(eps["ws"], timeout=20)
                client.connect()
                for i, wallet in enumerate(labels, start=1):
                    client.send_json({"jsonrpc": "2.0", "id": i, "method": "logsSubscribe",
                                       "params": [{"mentions": [wallet]}, {"commitment": "confirmed"}]})
                ws = client
                attempt = 0
                print("copy socket connected", flush=True)
            except Exception as e:
                attempt += 1
                wait = backoff_seconds(attempt, cap)
                print(f"copy socket down ({e}); poll fallback, backoff {wait:.0f}s", flush=True)
                if _poll_new(book, http, labels, cursors):
                    _publish()
                _remark(book, cfg)
                time.sleep(min(wait, float(cfg["loop"]["fallback_poll_seconds"])))
                continue
        try:
            msg = ws.recv_json(remark_s)
        except Exception as e:
            print("copy socket lost", e, flush=True)
            try:
                ws.close()
            except Exception:
                pass
            ws = None
            continue
        if isinstance(msg, dict) and msg.get("method") == "logsNotification":
            value = ((msg.get("params") or {}).get("result") or {}).get("value") or {}
            sig = value.get("signature")
            if sig and not value.get("err") and not book.seen(sig):
                book.mark_seen(sig)
                try:
                    tx = _fetch_tx(http, sig)
                except Exception as e:
                    print("getTransaction failed", str(e)[:160], flush=True)
                    tx = None
                if tx:
                    tx.setdefault("transaction", {}).setdefault("signatures", [sig])
                    if _apply_tx(book, tx, time.time(), labels):
                        _publish()
        now = time.time()
        if now >= next_remark:
            if _remark(book, cfg):
                _publish()
            next_remark = now + remark_s
        if now >= next_publish:
            _publish()
            next_publish = now + 180
    book.dashboard()
    print("copy listen finished", flush=True)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "dashboard"
    if cmd == "listen":
        listen()
    elif cmd == "discover":
        from copy.discover import main as discover_main
        raise SystemExit(discover_main(sys.argv[2:]))
    elif cmd == "dashboard":
        CopyBook().dashboard()
        print("copy dashboard updated", flush=True)
    else:
        print("usage: copy/run.py listen|discover|dashboard", flush=True)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
