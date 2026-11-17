# Paper Memecoin Desk (Solana)

Paper only. There is no wallet, no signing, no live order, and no switch that turns those on.
The desk reads public market data and records simulated fills in `data/desk.db`.

Stages, in order: Scout, Filter, Analyst, Executor, Risk, Logger (`desk/engine.py`).
A personal GMGN key is optional and not stored here. With no key, bundle and creator-rug
gates use RugCheck, pump.fun when it is reachable, and cached reports. The smart-wallet
score is dropped and its weight is spread across the other factors. DexScreener,
GeckoTerminal, RugCheck, the public Solana RPC, and Jupiter quotes are the free sources.

## State

`data/desk.db` is the ledger (cash, positions, trades, kills). `reports/dashboard.json`
is the snapshot the dashboard reads. Both are committed so a new machine can continue
the same paper book. Do not commit `secrets/`, `.env`, or anything under `logs/`.

Trading rules live in `config.json`. `config.medium.json` and `config.strict.json` are
unused backups. Nothing in the Actions runner changes gates, scores, size, or stops.

New entries take the hard stop from `risk.hard_stop_net_pct` (net of slippage, swap
fees, price impact, and the network fee). A new hard-stop fill is labeled
`stop_{int(pct)}pct`. A stop already stored on a position is never lowered, so open
positions keep the stop they were given at entry.

## Run one pass

```bash
python3 run.py manage    # open positions only: fresh prices, same stop and trail
python3 run.py tick      # full scan, then refresh reports/dashboard.json
python3 tests/test_free_sources.py
python3 tests/test_manage_fastpath.py
python3 tests/e2e_mock.py
```

`manage` does not scout. It re-marks open positions and exits on the existing
stop, the trailing stop, or a liquidity drop (`liq_drop`). It does not lower a
stop. With no open positions it does not write a run.

The Analyst can add up to `x_watchlist.max_boost` points from an X watchlist posted as a
JSON comment on GitHub issue 8. Only comments by `sneezekd` with association OWNER
are read, cached for 5 minutes, and ignored after `ttl_hours`. Watchlist mints are
scouted even if DexScreener discovery missed them, then they face every filter gate.
The boost cannot turn a below-minimum score into a pass. `x_score` is stored on the
trade and shown on the dashboard.

New entries also skip a 5-minute chase above `entry.max_chase_5m_pct`, a dump-then-bounce
inside `entry.flush_window_minutes` (missing 5m candles fail closed), a paper fill more
than `entry.max_fill_worse_than_mark_pct` worse than the mark, and a pre-buy round trip
over the existing `gates.max_round_trip_loss_pct`. New entries pause for the rest of the
America/Edmonton day when day P&L is at or below `brake.max_day_loss_pct` of start-of-day
equity, or a single close is at or below `brake.max_single_loss_pct`. Open positions
keep being managed. The dashboard shows `entry_pause.paused`.

## GitHub Actions relay

Hosted cron cannot run faster than every 5 minutes and often starts late, which is
how a sleeping computer missed stops. `.github/workflows/paper-desk.yml` instead
holds one runner for about 5 hours 45 minutes (under the 6 hour job cap):

- about every 15 seconds, `run.py manage` checks stops, trails, and liquidity.
  A public repo does not spend Actions minutes, and each check is one DexScreener
  batch for the open book (Jupiter only when an exit fires). A flat book does not
  call the network, so 15s stays inside the free rate limits.
- about every 3 minutes, `run.py tick` runs the full scout, filter, and score pass
- when the run finishes, including after a failed step, it dispatches the next
  **Paper desk** run on `main` with `GITHUB_TOKEN`. It skips that dispatch if another
  run is already queued or in progress, so waiters do not pile up
- `concurrency: paper-desk` keeps one running job and one waiting job, so the
  dispatched run does not overlap the current one
- cron `*/30 * * * *` is only a backup if a handoff is missed. **Run workflow**
  starts a job immediately

State is pushed to the `state` branch with the built-in `GITHUB_TOKEN` (`contents: write`).
Each push is a single snapshot commit whose message ends in `[skip ci]`, rewritten with
`--force-with-lease` so the binary database does not accumulate a commit per scan.
The push is retried if the remote moved. Do not protect `state` against force
pushes, or the snapshot cannot move. The runner publishes after every full scan,
immediately when a stop fires or the trail ratchets, and again before the job exits.

The relay checks out `main` for code, then overlays `data/desk.db` and
`reports/dashboard.json` from `state` when that branch exists. The first run uses
the copies on `main`.

## Copy desk (separate paper book)

`copy/` is a second paper strategy. It does not use a wallet, a private key, or the
main desk's `data/desk.db`. Leader buys and sells are watched on a Solana
`logsSubscribe` WebSocket (public mainnet RPC, or Helius when the optional
`HELIUS_API_KEY` secret is set). Our fills are Jupiter quotes. Detection latency
is the leader transaction's block time until we parse it, usually a couple of
seconds on a live socket and up to about 5 seconds while the socket is down and
the signature poll is the fallback.

Tracked wallets live in `copy/wallets.json` (`enabled: true`). `python3 copy/run.py discover`
(or the **Copy discover** workflow) writes `copy/candidates.json` and does not
turn a wallet on by itself. The copy book starts at $100 in `data/copy.db`.
The Pages dashboard has a **Copy desk** tab fed by `reports/copy_dashboard.json`.

`copy-desk.yml` is its own concurrency group and chains the next run the same
way as the main desk. Set repository variable `COPY_DESK_ENABLED` to `false` to
stop it. State pushes start from the latest `state` branch and only replace
that strategy's files, so the two relays can retry a rejected push without
wiping the other ledger.

To turn the desk off, add a repository variable named `DESK_ENABLED` with the
value `false` (**Settings → Secrets and variables → Actions → Variables**).
The shift already running finishes and does not dispatch the next one. Cron
runs that start while it is `false` skip trading. Delete the variable, or set
it to anything other than `false`, to allow runs again. Disabling the
**Paper desk** workflow in the Actions tab stops it as well.

## Dip desk (third paper book)

A separate PAPER-ONLY desk in `dip/` (spec: `docs/DIP_DESK_SPEC.md`). It watches new PumpSwap
migrations, runs a scanner and an 8-layer analyst, sizes a trade, then waits up to 30 minutes for a
15% dip before it "buys". Every buy and sell is a row in `data/dip.db`; there are no wallets, keys,
signing, or live orders. Dashboard: `reports/dip_dashboard.json` (the **Dip desk** tab on the site).

- Workflow: `.github/workflows/dip-desk.yml` (every 30 minutes). Off switch: set the repository
  variable `DIP_DESK_ENABLED=false` (Settings -> Secrets and variables -> Actions -> Variables).
- Telegram (optional): secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Both must be set or it
  stays off. Messages start with `PAPER`. `python3 dip/run.py summary` prints (and sends) the summary.
- Commands: `python3 dip/run.py listen | tick | dashboard | summary`. Tests: `python3 tests/test_dip_desk.py`.

Source-strategy rules that are approximated, because the original data needs a login or a paid feed:

- Unique buyers -> DexScreener `txns` (buys in the last hour, logged as "organic: txns").
- Holder count / growth -> RugCheck `totalHolders`, sampled every 5 minutes.
- Trading-app token warnings -> RugCheck danger `risks` plus Token-2022 extensions that can block selling.
- Smart holders: unavailable (logged "smart: unavailable", counted as 0).
- Social: off (logged "social: off"), so the score uses 4 parts.
- Paper fills: buys and exits fill at the DexScreener price that fired them, minus the source
  strategy's fee schedule (under $5 -> $0.10; $5-$47.50 -> 2%; $47.50-$190 -> $0.95; above -> 0.45%).
  No other slippage model, so real fills would be worse on thin pools.

## Dashboard

The page is the existing `web/static/index.html`, deployed by the Pages workflow.
It polls every 30 seconds. GitHub's raw file cache can still hold a snapshot for
a few minutes after a push. In the browser it tries the local `/api/state` first
(for `python3 web/server.py`), then:

- `https://raw.githubusercontent.com/sneezekd/paper-memecoin-desk/state/reports/dashboard.json`
- the same file on `main`, until the first relay snapshot exists

Public URL, once Pages is enabled:

https://sneezekd.github.io/paper-memecoin-desk/

`web/server.py` is still the optional local read-only server. The old Cloudflare /
Pinggy tunnel, TLS proxy, and askpass helper are not part of this repo.

## What Chris clicks once

The workflow file asks for `contents: write`, but a repository set to read-only
tokens ignores that. On https://github.com/sneezekd/paper-memecoin-desk :

1. **Settings → Actions → General → Actions permissions.** Choose **Allow all actions and reusable workflows**. Save if it was not already selected.
2. On that same page, **Workflow permissions**. Choose **Read and write permissions**. Save.
3. **Settings → Pages → Build and deployment → Source.** Choose **GitHub Actions**.
4. If the Pages workflow already failed, open **Actions → Pages →** the failed run **→ Re-run jobs**.
5. Merge this pull request into `main` first if it is not merged yet. Then **Actions → Paper desk → Run workflow → Run workflow** (branch `main`). The half-hour schedule will also start it, but Run workflow is the first run.

If a run sits on “Waiting for review” for the `github-pages` environment, open that run and approve it, or remove required reviewers under **Settings → Environments → github-pages**.
