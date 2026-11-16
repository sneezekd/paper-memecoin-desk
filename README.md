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

To turn the desk off, add a repository variable named `DESK_ENABLED` with the
value `false` (**Settings → Secrets and variables → Actions → Variables**).
The shift already running finishes and does not dispatch the next one. Cron
runs that start while it is `false` skip trading. Delete the variable, or set
it to anything other than `false`, to allow runs again. Disabling the
**Paper desk** workflow in the Actions tab stops it as well.

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
