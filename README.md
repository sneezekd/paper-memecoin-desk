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

## Run one pass

```bash
python3 run.py manage    # open positions only: fresh prices, same stop and trail
python3 run.py tick      # full scan, then refresh reports/dashboard.json
python3 tests/test_free_sources.py
python3 tests/test_manage_fastpath.py
python3 tests/e2e_mock.py
```

`manage` does not scout. It re-marks open positions and exits only on the existing
stop or trailing stop. With no open positions it does not write a run.

## GitHub Actions relay

Hosted cron cannot run faster than every 5 minutes and often starts late, which is
how a sleeping computer missed stops. `.github/workflows/paper-desk.yml` instead
holds one runner for about 5 hours 45 minutes (under the 6 hour job cap):

- about every 50 seconds, `run.py manage` checks stops and trails
- about every 3 minutes, `run.py tick` runs the full scout, filter, and score pass
- `concurrency: paper-desk` allows one running job and keeps the newest waiting job
- cron `*/30 * * * *` plus **Run workflow** starts a job when none is running, so the
  waiting job takes over as the current one exits

State is pushed to the `state` branch with the built-in `GITHUB_TOKEN` (`contents: write`).
Each push is a single snapshot commit whose message ends in `[skip ci]`, rewritten with
`--force-with-lease` so the binary database does not accumulate a commit per scan.
The push is retried if the remote moved. Do not protect `state` against force
pushes, or the snapshot cannot move. The runner publishes after every full scan,
immediately when a stop fires or the trail ratchets, and again before the job exits.

The relay checks out `main` for code, then overlays `data/desk.db` and
`reports/dashboard.json` from `state` when that branch exists. The first run uses
the copies on `main`.

Cancel the in-progress run and disable the **Paper desk** workflow to pause.
Scheduled workflows also go idle after 60 days without repository activity; a
`state` update counts as activity while the relay is healthy.

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
