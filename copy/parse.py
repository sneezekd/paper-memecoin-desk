"""Turn a jsonParsed getTransaction result into a leader buy or sell.

PAPER ONLY. Balance changes are read; nothing is signed.
"""
WSOL = "So11111111111111111111111111111111111111112"


def _account_keys(tx):
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    keys = msg.get("accountKeys") or []
    out = []
    for k in keys:
        if isinstance(k, str):
            out.append(k)
        elif isinstance(k, dict):
            out.append(k.get("pubkey") or "")
        else:
            out.append("")
    return out


def _ui(balance):
    amt = (balance or {}).get("uiTokenAmount") or {}
    ui = amt.get("uiAmount")
    if ui is None and amt.get("uiAmountString") is not None:
        try:
            ui = float(amt["uiAmountString"])
        except (TypeError, ValueError):
            ui = None
    try:
        return float(ui) if ui is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def parse_swap(tx, wallet, sol_price_usd=None):
    """Return a swap dict, or None if this transaction is not a token swap for `wallet`.

    A buy increases a non-WSOL balance and spends SOL. A sell decreases that balance.
    `sold_fraction` is how much of the leader's pre-trade token balance left.
    """
    if not tx or not wallet:
        return None
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return None
    keys = _account_keys(tx)
    try:
        idx = keys.index(wallet)
    except ValueError:
        return None
    pre_bal = (meta.get("preBalances") or [None])[idx] if idx < len(meta.get("preBalances") or []) else None
    post_bal = (meta.get("postBalances") or [None])[idx] if idx < len(meta.get("postBalances") or []) else None
    if pre_bal is None or post_bal is None:
        return None
    fee = int(meta.get("fee") or 0)
    lamport_delta = int(post_bal) - int(pre_bal)  # negative when the leader spent SOL
    # The fee is already inside the signer's balance change. Add it back so the swap
    # size is the SOL that moved in the trade, not the network fee.
    if lamport_delta < 0:
        sol_amount = (abs(lamport_delta) - fee) / 1e9
    else:
        sol_amount = (lamport_delta + fee) / 1e9
    if sol_amount <= 0:
        return None

    def totals(rows):
        out = {}
        for b in rows or []:
            if (b or {}).get("owner") != wallet:
                continue
            mint = b.get("mint")
            if not mint or mint == WSOL:
                continue
            out[mint] = out.get(mint, 0.0) + _ui(b)
        return out

    pre = totals(meta.get("preTokenBalances"))
    post = totals(meta.get("postTokenBalances"))
    mints = set(pre) | set(post)
    if not mints:
        return None
    changes = []
    for mint in mints:
        delta = post.get(mint, 0.0) - pre.get(mint, 0.0)
        if abs(delta) > 0:
            changes.append((mint, pre.get(mint, 0.0), post.get(mint, 0.0), delta))
    if not changes:
        return None
    mint, pre_amt, post_amt, delta = max(changes, key=lambda r: abs(r[3]))
    if delta > 0 and lamport_delta < 0:
        side = "buy"
        token_amount = delta
        sold_fraction = None
        first_buy = pre_amt <= max(token_amount * 0.01, 1e-9)
    elif delta < 0 and lamport_delta > 0:
        side = "sell"
        token_amount = abs(delta)
        sold_fraction = (token_amount / pre_amt) if pre_amt > 0 else None
        first_buy = False
    else:
        return None
    price_sol = sol_amount / token_amount if token_amount else None
    price_usd = (price_sol * sol_price_usd) if (price_sol is not None and sol_price_usd) else None
    return {
        "wallet": wallet,
        "mint": mint,
        "side": side,
        "token_amount": token_amount,
        "pre_amount": pre_amt,
        "post_amount": post_amt,
        "sol_amount": sol_amount,
        "sold_fraction": sold_fraction,
        "first_buy": first_buy,
        "leader_price_sol": price_sol,
        "leader_price_usd": price_usd,
        "block_time": tx.get("blockTime"),
        "signature": ((tx.get("transaction") or {}).get("signatures") or [None])[0],
    }


def detection_latency(block_time, detected_at):
    """Seconds from the leader transaction's block time to when we saw it. None if unknown."""
    if block_time is None or detected_at is None:
        return None
    try:
        return max(0.0, float(detected_at) - float(block_time))
    except (TypeError, ValueError):
        return None
