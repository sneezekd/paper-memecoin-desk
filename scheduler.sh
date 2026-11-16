#!/usr/bin/env bash
# Paper-only desk runner. No tunnels, no wallets, no live orders.
#
#   ./scheduler.sh manage         # one positions check (stops / trails only)
#   ./scheduler.sh once           # one full scout/filter/score tick
#   ./scheduler.sh loop           # local loop until stopped
#   ./scheduler.sh start|stop|status
#   ./scheduler.sh ci-loop        # GitHub Actions relay (time-bounded)
#   ./scheduler.sh commit-state   # publish data/desk.db + reports/dashboard.json
#
# The Actions relay checks stops about every DESK_MANAGE_SEC (default 15s) and
# runs a full scan about every DESK_SCAN_SEC (default 180s). State is committed
# to the `state` branch as a single moving snapshot ([skip ci]) so the binary
# database does not pile up in git history.
set -u
ROOT=$(cd "$(dirname "$0")" && pwd)
cd "$ROOT"
export TZ="${TZ:-America/Edmonton}"
export PYTHONUNBUFFERED=1

MANAGE_SEC="${DESK_MANAGE_SEC:-15}"
SCAN_SEC="${DESK_SCAN_SEC:-180}"
MAX_SEC="${DESK_MAX_SECONDS:-20700}" # 5h45m, under the 6h hosted-runner cap
STATE_BRANCH="${DESK_STATE_BRANCH:-state}"
LOCK_FILE="${DESK_LOCK_FILE:-/tmp/paper-desk-state.lock}"

say() { printf '%s\n' "$*"; }

cash_line() {
  python3 - <<'PY' || true
import sqlite3
c = sqlite3.connect("data/desk.db")
cash = c.execute("select value from meta where key='cash'").fetchone()
op = c.execute("select count(*) from positions where status='open'").fetchone()[0]
if cash:
    print("cash=%.2f open=%s" % (float(cash[0]), op))
else:
    print("cash=? open=%s" % op)
PY
}

restore_state() {
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { say "not a git checkout; keeping local files"; return 0; }
  git fetch --depth=1 origin "$STATE_BRANCH" >/dev/null 2>&1 || true
  if ! git rev-parse --verify "origin/${STATE_BRANCH}" >/dev/null 2>&1; then
    say "no origin/${STATE_BRANCH} yet; using the database in this checkout"
    cash_line
    return 0
  fi
  git restore --source="origin/${STATE_BRANCH}" --worktree -- data/desk.db reports/dashboard.json
  say "restored data/desk.db and reports/dashboard.json from origin/${STATE_BRANCH}"
  cash_line
}

publish_state() {
  if [ "${DESK_PUSH_STATE:-}" != "1" ]; then
    say "DESK_PUSH_STATE is not 1; not pushing state"
    return 0
  fi
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { say "not a git checkout"; return 1; }
  [ -f data/desk.db ] && [ -f reports/dashboard.json ] || { say "missing state files"; return 1; }

  (
    flock -n 9 || { say "state publish already in progress"; exit 0; }
    stat_line=$(cash_line | head -n 1)
    msg="state: ${stat_line:-snapshot} [skip ci]"
    backup=$(mktemp -d)
    python3 - "$backup" <<'PY' || exit 1
import sqlite3, shutil, sys
from pathlib import Path
dst = Path(sys.argv[1])
src = sqlite3.connect("file:data/desk.db?mode=ro", uri=True)
out = sqlite3.connect(dst / "desk.db")
src.backup(out)
out.close()
src.close()
shutil.copyfile("reports/dashboard.json", dst / "dashboard.json")
PY
    author_name="${GIT_AUTHOR_NAME:-github-actions[bot]}"
    author_email="${GIT_AUTHOR_EMAIL:-41898282+github-actions[bot]@users.noreply.github.com}"
    attempt=1
    while [ "$attempt" -le 5 ]; do
      git fetch --depth=1 origin "$STATE_BRANCH" >/dev/null 2>&1 || true
      expected=""
      if git rev-parse --verify "origin/${STATE_BRANCH}" >/dev/null 2>&1; then
        expected=$(git rev-parse "origin/${STATE_BRANCH}")
        old_db=$(git rev-parse "origin/${STATE_BRANCH}:data/desk.db" 2>/dev/null || echo missing)
        old_js=$(git rev-parse "origin/${STATE_BRANCH}:reports/dashboard.json" 2>/dev/null || echo missing)
        new_db=$(git hash-object "$backup/desk.db")
        new_js=$(git hash-object "$backup/dashboard.json")
        if [ "$old_db" = "$new_db" ] && [ "$old_js" = "$new_js" ]; then
          say "state unchanged (${stat_line:-ok})"
          exit 0
        fi
      fi
      git worktree prune >/dev/null 2>&1 || true
      wt=$(mktemp -d)
      rmdir "$wt"
      git worktree add --detach "$wt" HEAD >/dev/null
      mkdir -p "$wt/data" "$wt/reports"
      cp "$backup/desk.db" "$wt/data/desk.db"
      cp "$backup/dashboard.json" "$wt/reports/dashboard.json"
      git -C "$wt" add -A
      tree=$(git -C "$wt" write-tree)
      commit=$(
        GIT_AUTHOR_NAME="$author_name" \
        GIT_AUTHOR_EMAIL="$author_email" \
        GIT_COMMITTER_NAME="$author_name" \
        GIT_COMMITTER_EMAIL="$author_email" \
        git -C "$wt" commit-tree "$tree" -m "$msg"
      )
      git worktree remove --force "$wt" >/dev/null 2>&1 || rm -rf "$wt"
      if [ -n "$expected" ]; then
        if git push --force-with-lease="refs/heads/${STATE_BRANCH}:${expected}" origin "${commit}:refs/heads/${STATE_BRANCH}"; then
          say "published ${STATE_BRANCH} ${commit:0:12} (${stat_line:-ok})"
          exit 0
        fi
      else
        if git push origin "${commit}:refs/heads/${STATE_BRANCH}"; then
          say "published ${STATE_BRANCH} ${commit:0:12} (${stat_line:-ok})"
          exit 0
        fi
      fi
      say "state push rejected (attempt ${attempt}); retrying"
      attempt=$((attempt + 1))
      sleep $((attempt * 2))
    done
    say "failed to publish ${STATE_BRANCH}"
    exit 1
  ) 9>"$LOCK_FILE"
}

run_bounded() {
  # $1 = epoch deadline (0 = forever)
  local deadline="$1" now next_scan sleep_for out
  next_scan=$(date +%s)
  mkdir -p logs data reports
  while true; do
    now=$(date +%s)
    if [ "$deadline" -gt 0 ] && [ "$now" -ge "$deadline" ]; then
      break
    fi
    if [ "$now" -ge "$next_scan" ]; then
      say "=== scan $(date -u '+%F %T')Z ==="
      python3 run.py tick || say "tick failed rc=$?"
      publish_state || say "publish after scan failed rc=$?"
      next_scan=$(( $(date +%s) + SCAN_SEC ))
    else
      say "=== manage $(date -u '+%F %T')Z ==="
      out=$(python3 run.py manage 2>&1) || out="${out}"$'\n'"manage failed rc=$?"
      printf '%s\n' "$out"
      if printf '%s\n' "$out" | grep -Eq 'exits: *[1-9]|stop_moves: *[1-9]'; then
        publish_state || say "publish after exit failed rc=$?"
      fi
    fi
    now=$(date +%s)
    if [ "$deadline" -gt 0 ] && [ "$now" -ge "$deadline" ]; then
      break
    fi
    sleep_for=$MANAGE_SEC
    if [ "$next_scan" -gt "$now" ] && [ $((next_scan - now)) -lt "$sleep_for" ]; then
      sleep_for=$((next_scan - now))
    fi
    if [ "$deadline" -gt 0 ] && [ $((deadline - now)) -lt "$sleep_for" ]; then
      sleep_for=$((deadline - now))
    fi
    if [ "$sleep_for" -gt 0 ]; then
      sleep "$sleep_for"
    fi
  done
}

on_ci_exit() {
  trap - EXIT TERM INT
  publish_state || true
  exit 0
}

ci_loop() {
  say "paper relay: manage every ${MANAGE_SEC}s, scan every ${SCAN_SEC}s, max ${MAX_SEC}s"
  restore_state || true
  trap on_ci_exit EXIT TERM INT
  local start deadline
  start=$(date +%s)
  deadline=$((start + MAX_SEC))
  run_bounded "$deadline"
}

local_loop() {
  mkdir -p logs
  echo $$ > logs/scheduler.pid
  trap 'rm -f logs/scheduler.pid; exit 0' TERM INT
  say "local paper loop pid $$ (manage ${MANAGE_SEC}s, scan ${SCAN_SEC}s)"
  DESK_PUSH_STATE=0 run_bounded 0
}

cmd_start() {
  mkdir -p logs
  if [ -f logs/scheduler.pid ] && kill -0 "$(cat logs/scheduler.pid)" 2>/dev/null; then
    say "already running pid $(cat logs/scheduler.pid)"
    return 0
  fi
  nohup "$0" loop >>logs/scheduler.log 2>&1 &
  sleep 0.4
  say "started pid $(cat logs/scheduler.pid 2>/dev/null || echo $?)"
}

cmd_stop() {
  if [ -f logs/scheduler.pid ]; then
    kill "$(cat logs/scheduler.pid)" 2>/dev/null || true
    rm -f logs/scheduler.pid
    say stopped
  else
    say "not running"
  fi
}

cmd_status() {
  if [ -f logs/scheduler.pid ] && kill -0 "$(cat logs/scheduler.pid)" 2>/dev/null; then
    say "running pid $(cat logs/scheduler.pid)"
  else
    say "not running"
  fi
  cash_line
}

usage() {
  say "usage: $0 manage|once|loop|start|stop|status|ci-loop|commit-state|restore"
  return 2
}

case "${1:-status}" in
  manage) python3 run.py manage ;;
  once) python3 run.py tick ;;
  loop) local_loop ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  ci-loop) ci_loop ;;
  commit-state) publish_state ;;
  restore) restore_state ;;
  *) usage ;;
esac
