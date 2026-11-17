# Dip desk spec (third paper desk)

PAPER ONLY. No wallets, keys, seed phrases, signing, live orders, browser automation or any
trading-app login. Every "buy" and "sell" is a row in a local SQLite file. Never log or print
secrets (HELIUS_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).

The dip desk is a third, independent desk next to the main desk (`desk/`, paper-desk.yml) and
the copy desk (`copy/`, copy-desk.yml). Do not change the behavior of either. Reusing their
helpers by import (desk/sources.py, copy/rpc.py) is fine; if a shared helper must change, keep
its old behavior identical and keep every existing test green. Python 3.12 standard library
only (the other desks install nothing; copy/rpc.py has a stdlib WebSocket client).

## Layout
- `dip/` package: `config.json`, `sources.py` (data adapters), `scanner.py`, `layers.py`
  (Analyst 8 layers), `score.py`, `brain.py` (sizing), `executor.py` (dip entry, re-check,
  paper fill, take profit), `risk.py` (exits), `book.py` (SQLite ledger), `notify.py`
  (Telegram), `report.py` (dashboard JSON), `run.py` (CLI: `listen`, `dashboard`, `summary`).
- State: `data/dip.db`, `reports/dip_dashboard.json`, both published to the `state` branch.
- Workflow: `.github/workflows/dip-desk.yml`. Tests: `tests/test_dip_desk.py` (offline).
- All numbers below live in `dip/config.json`, never hard-coded in logic.

## Settings (dip/config.json defaults, from the source strategy)
scanner: min_liquidity_usd 20000, max_top10_pct 25, max_h1_change_pct 200, poll_s 60,
  min_pool_age_min 10, max_wait_min 60, runner_h24_pct 300, min_holders_growth_pct 0,
  holders_sample_min 5
analyst: max_dev_pct 5, max_top10_pct 25, min_lp_locked_pct 99, max_liq_drop_pct 50,
  max_bundled 5, max_insiders_pct 15, max_delegated_accounts 10, retries 3, retry_gap_s 2,
  min_score 70, alive_volume_h1 25000, organic_unique_h1 80, organic_buys_h1 150,
  bundled_penalty 0.5, bundled_penalty_min 3, fast_curve_min 10, max_fresh_pct 50,
  fresh_top_n 20, fresh_sig_min 10, flush_sell_ratio 1.5, chase_h1_pct 200
social: enabled false, social_weight 0.5, shill_cut 0.5
brain: paper_start_cash 100, risk_per_trade_pct 3, stop_pct 25, full_size_score 80,
  below_full_pct 50, max_pool_pct 2, min_ticket_usd 2, max_ticket_usd 50, max_trades_night 30
executor: dip_pct 15, dip_window_min 10, dip_wait_min 30, crash_pct 35, flush_sell_ratio 1.5,
  recheck_liq_drop_pct 20, recheck_price_up_pct 15, tp_x 2.0, tp_sell_pct 50, trail_pct 40
risk: poll_s 5, stop_pct 25, timeout_h 3, timeout_min_gain_pct 5, pulled_drop_pct 80,
  dev_check_s 60, dev_dump_pct 50, warn_check_s 60, no_price_polls 12, night_loss_stop_pct 30
session: tz "America/Edmonton", night_start "22:00", lock_time "06:30", report_time "08:00",
  always_on false  (always_on true = trade 24/7, lock_time and night counters reset daily at
  night_start instead)
fees (source strategy's paper fee model, each side, on the trade's USD size): under $5 -> $0.10
  flat; $5 to $47.50 -> 2%; $47.50 to $190 -> $0.95 flat; above $190 -> fee_pct 0.45%. No
  separate gas/network fee. Config: fees.small_usd 5, small_flat 0.10, mid_top 47.50, mid_pct 2,
  big_top 190, big_flat 0.95, fee_pct 0.45. Implement as dip/fees.py fee_usd(size) with unit tests
  at the boundaries.

## Data sources (free, no login)
- New coins: PumpPortal WebSocket `wss://pumpportal.fun/api/data`, ONE connection; on open send
  `{"method":"subscribeMigration"}` and `{"method":"subscribeNewToken"}` only (never trade
  streams). Record mint, creator (if present), pool (if present), time seen. Read fields
  defensively (missing key -> null). Silent 120 s -> reconnect with backoff 1,2,4..60 s.
- DexScreener `https://api.dexscreener.com/tokens/v1/solana/{m1,m2,...}` (batched, up to 30),
  pick the `pumpswap` pair with the most liquidity (fall back to the most-liquid pair whose
  dexId is pumpswap/pumpfun-amm; anything else = DexScreener doesn't know it -> no pass).
  Stay under 250 req/min; on 429 wait 30 s and keep the room.
- RugCheck public: `/v1/tokens/{mint}/report` (creator, creatorBalance, creatorTokens,
  topHolders, markets[].pubkey + lp.lpLockedPct, insiderNetworks, totalHolders, risks,
  detectedAt, token_extensions), `/v1/tokens/{mint}/insiders/graph`, `/v1/stats/rugs/ticker?limit=50`
  (rug list; refresh every 10 min into a `rugs` table — the SSE stream is optional).
  An HTTP error/timeout is "no data", but an empty JSON list from insiders/graph is a valid
  answer meaning "no edges".
- Solana RPC: `copy.rpc.rpc_endpoints()` (Helius when HELIUS_API_KEY is set, else public):
  getAccountInfo jsonParsed, getTokenLargestAccounts, getTokenSupply,
  getTokenAccountsByOwner, getProgramAccounts (Token and Token-2022, filter by mint, use
  dataSlice/jsonParsed sparingly), getSignaturesForAddress(limit 20).
- pump.fun public API via desk.sources.pumpfun_coin (created_timestamp) when not blocked.
- Paper fills (source strategy's PAPER rule): buy fills at the DexScreener price at the dip
  poll; qty = (size - fee_usd(size)) / price. Exit fills at the DexScreener price that fired it:
  proceeds = qty*price - fee_usd(qty*price). No other slippage model.

## Scanner (bot 1)
1. Every migration -> waiting room row: mint, pool, creator, migrated_at, creator balance now
   (RPC; RugCheck creatorBalance as 2nd source; null if unknown), curve_min = migrated_at - the
   time its new-token message was seen (else RugCheck detectedAt, else pump.fun created_timestamp,
   else null).
2. Every poll_s (60): one batched DexScreener read for the room -> `prices` table (price, liq,
   mcap, volume m5/h1, txns m5/h1 buys+sells, priceChange h1/h24). Holders = RugCheck
   totalHolders, sampled at most every holders_sample_min per coin (cache), plus a fresh sample
   at pass time. holders_growth = holders now / the newest sample at least 8 min old - 1
   (if only younger samples exist, wait).
3. Kill on the first hit (store reason text): liq < 20000 "liq $14.2K"; top10 (pool + curve +
   known program accounts excluded) > 25% "top 10 hold 38%"; h1 change > 200 "+240% in 1h";
   copycat of a runner today "copycat of X"; RugCheck danger risks / Token-2022 extensions that
   can block selling (non-transferable, pausable, transfer hook, permanent delegate, freezable
   default state) "warns: {text}" (this replaces the trading-app tokenWarnings check);
   at pass time holders_growth < 0 "holders leaving -4% in 10m"; holders unknown at pass time
   -> kill "no data: holders".
   Copycat: normalize name and symbol (lowercase, drop spaces, digits, emoji/non-alnum), match
   exact or one inside the other vs today's runners (any coin in `prices` or DexScreener top boosts
   with h24 >= +300%, excluding itself).
4. Pass at pool age >= 10 min with no kill, once per mint. 60 min in the room without passing -> drop
   "timed out". Never pass a coin DexScreener doesn't know. Never twice in a day.
5. Log only: liq_mcap = liq / mcap, accel = volume.m5 * 12 / volume.h1.

## Analyst (bot 2): 8 layers in order, stop at first red, the rest "not checked"
No data is never a pass (red "no data: X"). Retry a failing source 3x, 2 s apart.
1 Mint authority null -> ok "Revoked" else red "mint not revoked".
2 Freeze authority null -> ok "Revoked" else red "freeze not revoked".
3 Dev: creator holds < 5% and balance not lower than at room entry ("dev sold"); creator's
  other mints (new-token log, RugCheck creatorTokens, pump.fun created coins when reachable)
  found in `rugs` -> red "dev rugged N before". Unknown creator -> red "no data: dev". A fresh dev
  with no history is ok.
4 Top 10 via getTokenLargestAccounts minus pool/curve vaults (resolve owners with
  getAccountInfo jsonParsed; exclude the pool address and known AMM/curve program owners),
  < 25% else red "top 10 hold 58%".
5 Liquidity: RugCheck market whose pubkey == pool (or, if the pool is unknown, the pumpswap
  market): lpLockedPct >= 99 and liq not down > 50% since Scanner. ok "100% burned", red
  "liquidity unlocked" / "liquidity down 62%". No such market -> red "no data: liquidity".
6 Pool age >= 10 min else red "pool too young".
7 Bundles: insiders graph -> keep holder wallets of this mint, union-find on edges, biggest
  group size < 5 else red "14 bundled wallets"; RugCheck insiderNetworks summed holding / supply
  < 15% else red "insiders hold 38%" (key missing = 0 networks, log it). Graph request failed ->
  red "no data: bundles".
8 Delegations: getProgramAccounts for the mint on Token and Token-2022; any single delegate
  over 10 holder accounts -> red "41 wallets -> 9fQx...T2a" else ok "None". Also store holder
  owners (for fresh_pct). RPC refuses -> red "no data: delegations".

## Score (code)
alive = clamp(volume.h1 / 25000, 0, 1)
organic = clamp(buys.h1 / 150, 0, 1), logged "organic: txns" (no free unique-buyer source; this
  is the source strategy's own fallback), x0.5 if sells.h1 > buys.h1, x0.5 if layer 7 biggest
  group >= 3, x0.5 if curve_min < 10, x0.5 if fresh_pct >= 50 (fresh_pct = share of the top 20
  holder owners from layer 8, pool/curve excluded, with < 10 signatures in
  getSignaturesForAddress(owner, limit 20); spread the 20 calls over ~10 s).
flush = 1 if sells.m5 <= buys.m5, 0.5 if sells.m5 <= 1.5 x buys.m5, else 0 ("flush: txns").
chase = clamp(priceChange.h1 / 200, 0, 1)
core = alive + organic + flush + (1 - chase)
social = null when social.enabled is false -> log "social: off", w = 0. The "smart holders"
part (trading-app leaderboard) is not available: log "smart: unavailable" and treat s = 0 if
social is ever enabled.
score = round(100 * (core + w*social) / (4 + w)); social_pts = score - round(100*core/4).
Pass at score >= 70. A red layer rejects whatever social says. Save every decision (rejects too)
with all parts.

## Brain (bot 3): sizing
free cash = cash minus money reserved by orders waiting for a dip (open positions are already
spent). raw = free_cash * 3% / 25% (12% of cash). score >= 80 -> raw, else raw * 50%. shill ring
-> half again (only when social on). Cap at 2% of pool liquidity ("pool"), cap at max_ticket_usd
("ticket"). Round to cents. < $2 -> skip "under $2". max_trades_night reached -> skip
"max trades". Night loss >= night_loss_stop_pct -> skip "night loss stop". One position per mint
per night, never a 2nd buy on an open position, never average down. Size never changes after.
Expected: cash 100, score 80 -> 12.00; 79 -> 6.00; pool $20,000 caps at $400.

## Executor (bot 4): DIP ENTRY + re-check + paper fill + take profit
A sized coin becomes a waiting order (cash reserved) for up to 30 min. Every risk poll (5 s, same
batched DexScreener call as open positions): high10 = max price over the last 10 min (prices
table + polls). Buy only when price <= high10 * 0.85 AND sells.m5 <= 1.5 x buys.m5. Price < high10
* 0.65 -> skip "crash -41%". Falling with sellers running -> keep waiting. 30 min with no dip ->
skip "no dip". Cancelled orders release reserved cash.
Re-check right before the paper buy: liq down > 20% since Scanner -> skip "liquidity dropping";
price up > 15% since Analyst -> skip "ran before the click"; free cash < size -> "no free cash";
warnings again -> "warns: ..."; creator balance lower than at Analyst -> "dev sold while we waited".
Paper buy: fill per the fee model above. Store entry price, qty, cost, buy fee, peak,
stop line = entry * (1 - 25%), tp_done false.
Take profit (every poll): price >= entry * 2.0 and not tp_done -> sell 50%, tp_done. Then trail:
peak = max(peak, price); price <= peak * (1 - 40%) -> sell 100% "trail".

## Risk (bot 5): exits, every poll in this order
stop: price <= stop line (entry * 0.75; the line only ever tightens, a looser setting never moves
  an open stop down) -> sell 100% "stop".
time: open >= 3 h and price < entry * 1.05 -> "dead money".
pulled: liq -> 0 or down > 80% in one poll -> "pulled".
dev: every 60 s creator balance down > 50% since buy -> "dev dumping".
warns: every 60 s RugCheck danger risks / blocking extensions appear -> "warns".
no price: 12 polls in a row without a price -> "no price".
night: realized + open marked night P&L <= -30% of night-start equity -> no more buys tonight.
Paper exit price: the DexScreener price that fired it, minus fee_usd. For "pulled" use that
poll's price; for "no price" use a Jupiter sell quote (desk.sources.jupiter_quote) for the qty if
it routes, else proceeds 0 (recorded as "no route"). Never invent a price. Record fees per trade.
Session: buys only inside the night window (night_start -> lock_time, tz) unless always_on. At
lock_time: cancel waiting orders, sell every open position 100% "night over", summary.

## Notifications (Telegram), dip/notify.py
Enabled only when both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars are non-empty; otherwise
a no-op that logs "telegram: off". stdlib urllib POST to sendMessage. Messages start with
"PAPER": bought, sold/TP, stopped/exits with reason and $/%, night over, daily summary at
report_time. Rate-limit-safe, failures never crash the loop, token never logged.

## Dashboard
reports/dip_dashboard.json: cash, equity, start cash, P&L, open positions (entry, mark, peak, stop
line, tp_done, age), waiting dip orders (high10, current, depth, minutes left), recent activity,
last 50 audits (each coin's 8 layer results with value/source, score parts, social: off, verdict,
reason), scanner kill counts by reason, skip reasons counts, night counters, updated_at.
web/static/index.html: add a "Dip desk" section/tab in the same style as the copy desk section,
reading `https://raw.githubusercontent.com/sneezekd/paper-memecoin-desk/state/reports/dip_dashboard.json`
(fallback main). Don't break the existing sections.

## Workflow (.github/workflows/dip-desk.yml), mirror copy-desk.yml exactly
cron "*/30 * * * *" + workflow_dispatch, concurrency group dip-desk, job `if: vars.DIP_DESK_ENABLED != 'false'`,
timeout 360, checkout main, python 3.12, `./scheduler.sh restore-dip`, run `python3 dip/run.py listen`
with DIP_PUSH_STATE=1, DIP_MAX_SECONDS=20700, HELIUS_API_KEY / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
from secrets; persist step `./scheduler.sh publish-dip` (always); chain step identical to the copy
desk's but for dip-desk.yml and DIP_DESK_ENABLED. The listen loop publishes state every ~5 min
(like the copy desk) and exits cleanly before DIP_MAX_SECONDS. scheduler.sh: add restore_dip /
publish_dip (copies of the copy functions for data/dip.db + reports/dip_dashboard.json, own lock
file), add them to the case and usage line. Seed data/dip.db and reports/dip_dashboard.json in main
(empty $100 book) like the other desks. Add the dip tests to test.yml.

## Tests (tests/test_dip_desk.py, offline, mocked sources, runnable as `python3 tests/test_dip_desk.py`)
- gate: score 69 rejects, 70 passes; 1 red layer of 8 rejects regardless of score; social off ->
  4-part score and "social: off" logged; layers stop at first red, rest "not checked"; missing
  data -> red "no data".
- brain: cash 100, score 80 -> $12; 79 -> $6; under $2 -> skip; pool 20000 -> cap $400; reserved
  cash is not free; max trades / night loss stop skip.
- risk: -25.1% = stop; 2.0x = sell 50%; then 40% off the peak = sell the rest; 3 h at +4% =
  dead money; a looser stop setting never moves an open stop down; pulled; no price after 12 polls.
- dip: at the 10 min high waits; 15% under with sellers running waits; 15% under with buyers back
  buys; 41% under -> skip "crash -41%"; 30 min no dip -> "no dip"; one position per mint.
- metrics: warns -> scanner kill; holders -4% in 10m -> kill; insiders 38% -> layer 7 red; creator
  with a mint in rugs -> layer 3 red; curve 4 min + fresh 60% -> organic x0.25.
- fees: $4.99 -> 0.10, $5 -> 0.10 (2%), $20 -> 0.40, $47.50 -> 0.95, $100 -> 0.95, $200 -> 0.90.
- telegram off when env missing; secrets never in dashboard JSON.
