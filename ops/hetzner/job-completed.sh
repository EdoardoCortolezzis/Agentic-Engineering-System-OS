#!/usr/bin/env bash
# ACTIONS_RUNNER_HOOK_JOB_COMPLETED — root:root 0755.
# Never fail the job: failed cleanup must not erase an outcome. Like the start
# hook, this does not read job environment configuration: paths it removes
# must not be selectable by the job.
set -uo pipefail

LOCK_DIR=/run/aes/jobs
ARCHIVE=/var/lib/aes/state/archive
WORKTREE_MAX_AGE_DAYS=7
# `feature-start.sh` creates worktrees in `<checkout>/../.worktrees`, under
# the runner's workspace. Adapt this example to the host's runner layout.
WORKTREE_ROOTS='/var/lib/aes/runners/*/_work/*/.worktrees'
# Zero means "do not delete": pruning archives is a deliberate operator action,
# not a side effect.
LEDGER_RETENTION_DAYS=0

CONFIG=/etc/aes/limits.conf
if [ "${1:-}" = "--config" ] && [ -n "${2:-}" ]; then
  CONFIG="$2"
fi

if [ -f "$CONFIG" ]; then
  while IFS='=' read -r chiave valore; do
    case "$chiave" in
      AES_LOCK_DIR) LOCK_DIR="$valore" ;;
      AES_ARCHIVE_DIR) ARCHIVE="$valore" ;;
      AES_WORKTREE_ROOTS) WORKTREE_ROOTS="$valore" ;;
      AES_WORKTREE_MAX_AGE_DAYS) WORKTREE_MAX_AGE_DAYS="$valore" ;;
      AES_LEDGER_RETENTION_DAYS) LEDGER_RETENTION_DAYS="$valore" ;;
    esac
  done < "$CONFIG"
fi

# Validate each path independently: concatenating them would let a relative
# ARCHIVE hide behind an absolute LOCK_DIR and place the ledger in the current
# directory, namely the checkout about to be deleted.
for coppia in "LOCK_DIR=$LOCK_DIR" "ARCHIVE=$ARCHIVE"; do
  case "${coppia#*=}" in
    /*) ;;
    *) echo "::error::${coppia%%=*} is not an absolute path in ${CONFIG}: cleanup skipped" >&2; exit 0 ;;
  esac
done

# Only the job's own stamp, not a glob on the run id: matrix jobs share a run
# id and job name, and a glob would remove a still-running worker's lock,
# undercounting concurrency for the next job.
slot="$(printf '%s' "${GITHUB_WORKSPACE:-${RUNNER_WORKSPACE:-nessuno}}" | tr -c 'A-Za-z0-9' '_')"
stamp="${LOCK_DIR}/${GITHUB_RUN_ID:-manual}-${GITHUB_JOB:-job}-${slot}"
# An ignored release failure is a lost slot: later jobs wait on a stamp nobody
# will remove. The hook does not fail the job for this, but it reports it.
if [ -e "$stamp" ] && ! rm -f "$stamp" 2>/dev/null; then
  echo "::error::job lock not released: $stamp remains and occupies a slot" >&2
elif [ -e "$stamp" ]; then
  echo "::error::job lock still present after removal: $stamp" >&2
fi

# The ledger lives in the checkout and the next checkout deletes it
# (`actions/checkout` runs git clean -ffdx). Keep a copy here until
# AES_STATE_ROOT moves state outside the checkout (ADR 0024).
src="${GITHUB_WORKSPACE:-}/.agent/tasks/artifacts"
if [ -n "${GITHUB_WORKSPACE:-}" ] && [ -d "$src" ]; then
  dest="${ARCHIVE}/$(date +%Y%m%d)/${GITHUB_RUN_ID:-manual}"
  # A silently failed archive is worse than no archive: the next checkout
  # deletes the original and nobody knows the only copy was never written.
  # The hook does not fail the job, but reports it.
  if ! mkdir -p "$dest"; then
    echo "::error::ledger not archived: cannot create $dest" >&2
  elif ! cp -a "$src/." "$dest/"; then
    echo "::error::ledger not archived: copy from $src failed" >&2
  else
    echo "ledger archived at $dest"
  fi
fi

# Remove a worktree only after confirming that no work is in progress. A
# failing `status` does not mean a clean worktree: empty output from a failed
# command could delete uncommitted changes.
remove_worktree() {
  local wt="$1" status rc main repo
  # `--ignored` is deliberate: a .env, venv, or build artifact does not appear
  # in normal status and cannot be recovered from the branch. Keep a worktree
  # containing any of these; cleanup remains a human decision.
  status="$(git -C "$wt" status --porcelain --ignored=matching 2>/dev/null)"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "Git status cannot be verified; worktree kept: $wt"
    return
  fi
  if [ -n "$status" ]; then
    echo "worktree has uncommitted or ignored files; kept: $wt"
    return
  fi
  # The directory is only half of the worktree: the other half is the entry in
  # the main repository's .git/worktrees, which keeps the branch occupied.
  main="$(git -C "$wt" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
  repo="${main%/.git}"
  if [ -n "$main" ] && [ -d "$repo" ] \
     && git -C "$repo" worktree remove "$wt" 2>/dev/null; then
    echo "worktree removed: $wt"
    return
  fi
# No fallback `rm -rf`: if Git refuses removal, a person evaluates why. A
# cleanup hook never forces deletion.
  echo "Git refused removal; worktree kept: $wt"
}

for root in $WORKTREE_ROOTS; do
  [ -d "$root" ] || continue
  while IFS= read -r wt; do
    [ -n "$wt" ] && remove_worktree "$wt"
  done < <(find "$root" -mindepth 2 -maxdepth 2 -type d -mtime "+${WORKTREE_MAX_AGE_DAYS}" -print 2>/dev/null)
done

# Archived ledgers are not deleted automatically. After checkout cleanup they
# are the only remaining copy, so deleting that copy is an operator decision:
# the hook only reports it. Automatic expiry requires setting
# `AES_LEDGER_RETENTION_DAYS` in the root-owned file, itself a deliberate act.
if [ "$LEDGER_RETENTION_DAYS" -gt 0 ] 2>/dev/null; then
  while IFS= read -r vecchio; do
    [ -n "$vecchio" ] || continue
    rm -rf "$vecchio" && echo "ledger older than ${LEDGER_RETENTION_DAYS} days, removed: $vecchio"
  done < <(find "$ARCHIVE" -mindepth 1 -maxdepth 1 -type d -mtime "+${LEDGER_RETENTION_DAYS}" 2>/dev/null)
else
  vecchi="$(find "$ARCHIVE" -mindepth 1 -maxdepth 1 -type d -mtime +14 2>/dev/null | wc -l | tr -d ' ')"
  if [ "${vecchi:-0}" -gt 0 ]; then
    echo "${vecchi} ledgers older than 14 days in ${ARCHIVE}: pruning is a human decision"
  fi
fi

exit 0
