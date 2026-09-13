#!/usr/bin/env bash
# ACTIONS_RUNNER_HOOK_JOB_STARTED — root:root 0755, not writable by the runner.
# Fail closed: if the server lacks space or a check cannot be applied, the job
# does not start. A job failed immediately is safer than a disk filling during
# work or a concurrency cap that only appears active.
#
# **No environment configuration.** The runner's `.env` is in the home of the
# account running jobs: a threshold or path read from it would be chosen by
# the party being limited—pointing AES_LOCK_DIR elsewhere would always show
# zero active jobs. Values come from the root configuration file, and only
# `--config <file>` can change it: the runner passes that argument when it
# invokes the hook, not the job.
set -euo pipefail

MAX_JOBS=1
MIN_FREE_GB=15
LOCK_DIR=/run/aes/jobs
DISK_PATH=/
STALE_MINUTES=360
WAIT_SECONDS=1800
POLL_SECONDS=10

CONFIG=/etc/aes/limits.conf
if [ "${1:-}" = "--config" ]; then
  [ -n "${2:-}" ] || { echo "::error::--config requires a path" >&2; exit 1; }
  CONFIG="$2"
fi

if [ -f "$CONFIG" ]; then
  while IFS='=' read -r chiave valore; do
    case "$chiave" in
      AES_HOST_MAX_JOBS) MAX_JOBS="$valore" ;;
      AES_MIN_FREE_GB) MIN_FREE_GB="$valore" ;;
      AES_LOCK_DIR) LOCK_DIR="$valore" ;;
      AES_DISK_CHECK_PATH) DISK_PATH="$valore" ;;
      AES_STAMP_MAX_AGE_MINUTES) STALE_MINUTES="$valore" ;;
      AES_SLOT_WAIT_SECONDS) WAIT_SECONDS="$valore" ;;
      AES_SLOT_POLL_SECONDS) POLL_SECONDS="$valore" ;;
    esac
  done < "$CONFIG"
fi

# An unreadable limit is no limit: stop rather than compare numbers with a
# string and let a job through because of an error. Validate each value alone—
# concatenating them lets an empty value disappear between separators—and an
# empty value is not a number.
for coppia in "AES_HOST_MAX_JOBS=$MAX_JOBS" "AES_MIN_FREE_GB=$MIN_FREE_GB" \
              "AES_STAMP_MAX_AGE_MINUTES=$STALE_MINUTES" \
              "AES_SLOT_WAIT_SECONDS=$WAIT_SECONDS" \
              "AES_SLOT_POLL_SECONDS=$POLL_SECONDS"; do
  valore="${coppia#*=}"
  case "$valore" in
    ""|*[!0-9]*)
      echo "::error::${coppia%%=*} is not a number in ${CONFIG} (value: '${valore}'): job rejected" >&2
      exit 1 ;;
  esac
done
[ "$POLL_SECONDS" -gt 0 ] || { echo "::error::zero wait interval in ${CONFIG}: job rejected" >&2; exit 1; }
# Validate each path independently: concatenating them would check only the
# first character of one string, allowing `AES_DISK_CHECK_PATH=.` behind an
# absolute LOCK_DIR.
for coppia in "LOCK_DIR=$LOCK_DIR" "DISK_PATH=$DISK_PATH"; do
  case "${coppia#*=}" in
    /*) ;;
    *) echo "::error::${coppia%%=*} is not an absolute path in ${CONFIG}: job rejected" >&2; exit 1 ;;
  esac
done

# Name the stamp so the completion hook can remove its own stamp only. The PID
# is insufficient—the hooks are separate processes—and RUNNER_NAME is the same
# for all runners on this host. The workspace distinguishes runners, and each
# runner executes one job at a time.
slot="$(printf '%s' "${GITHUB_WORKSPACE:-${RUNNER_WORKSPACE:-nessuno}}" | tr -c 'A-Za-z0-9' '_')"
STAMP="${LOCK_DIR}/${GITHUB_RUN_ID:-manual}-${GITHUB_JOB:-job}-${slot}"

mkdir -p "$LOCK_DIR"

# Results: 0 slot acquired, 1 host at its cap (retry makes sense), 2 check
# could not be applied or disk is below threshold (retry does not help).
# `set -e` does not apply inside a function used as a condition, so every
# fallible command is checked explicitly: otherwise a failed `flock` would
# start the job without serialization.
prova_ad_occupare_un_posto() {
  local running

  # Re-measure the disk on every attempt: the job occupying the slot can fill
  # it while we wait, and starting with a half-hour-old measurement would pass
  # precisely the case this check must stop.
  if ! free_gb="$(df -BG --output=avail "$DISK_PATH" 2>/dev/null | tail -1 | tr -dc '0-9')" \
     || [ -z "$free_gb" ]; then
    echo "::error::free space cannot be measured on ${DISK_PATH}: job rejected" >&2
    return 2
  fi
  if [ "$free_gb" -lt "$MIN_FREE_GB" ]; then
    echo "::error::free space ${free_gb}G is below ${MIN_FREE_GB}G threshold: job rejected" >&2
    return 2
  fi

  # Counting active jobs and writing this stamp must be one operation: without
  # the lock, two runners starting together both count zero and both pass.
  if ! exec 9>"${LOCK_DIR}/.gate"; then
    echo "::error::gate lock cannot be opened in ${LOCK_DIR}: job rejected" >&2
    return 2
  fi
  if ! flock 9; then
    echo "::error::gate lock not acquired: job rejected" >&2
    exec 9>&-
    return 2
  fi

  # A job killed before its completion hook runs leaves its stamp: without
  # pruning, the cap would permanently lose a slot. But file age alone says
  # nothing—a long job has a stamp as old as itself, and pruning it would open
  # a hole in the cap this hook protects. Check whether the creating process is
  # still alive; age remains only a secondary condition.
  while IFS= read -r stamp; do
    [ -n "$stamp" ] || continue
    pid="$(sed -n 's/^pid=//p' "$stamp" 2>/dev/null | head -1)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      # A live PID is not enough: the system reuses PIDs, and a reassigned PID
      # would hold the slot forever. The stamp carries the creating process's
      # start time; if it differs, that PID now belongs to someone else.
      avvio_atteso="$(sed -n 's/^avvio=//p' "$stamp" 2>/dev/null | head -1)"
      avvio_reale="$(stat -c %Y "/proc/${pid}" 2>/dev/null || echo '')"
      if [ -z "$avvio_atteso" ] || [ "$avvio_atteso" = "$avvio_reale" ]; then
        continue
      fi
      echo "stamp PID ${pid} was reused by another process: $(basename "$stamp")"
    fi
    rm -f "$stamp" 2>/dev/null
    echo "orphan stamp removed: $(basename "$stamp")"
  done < <(find "$LOCK_DIR" -maxdepth 1 -type f -name '*-*-*' -mmin "+${STALE_MINUTES}" 2>/dev/null)

  # `find | wc -l` returns `wc`'s status, which succeeds even when `find`
  # cannot read the directory: the count would be zero and the cap would not
  # apply. Obtain the list first; an error rejects rather than counting zero.
  if ! elenco="$(find "$LOCK_DIR" -maxdepth 1 -type f -name '*-*-*')"; then
    echo "::error::stamps cannot be listed in ${LOCK_DIR}: job rejected" >&2
    exec 9>&-
    return 2
  fi
  running="$(printf '%s' "$elenco" | grep -c . || true)"
  if [ "$running" -ge "$MAX_JOBS" ]; then
    exec 9>&-
    return 1
  fi

  # `pid` is the process that invoked the hook—the runner worker, which lives
  # as long as the job—and makes orphan detection verifiable. Without a stamp,
  # the job would run outside the count.
  if ! printf 'pid=%s\navvio=%s\n%s %s %s\n' "$PPID" \
       "$(stat -c %Y "/proc/$PPID" 2>/dev/null || echo '')" \
       "${GITHUB_REPOSITORY:-?}" "${GITHUB_RUN_ID:-?}" "$(date -Is)" > "$STAMP"; then
    echo "::error::stamp cannot be written in ${LOCK_DIR}: job rejected" >&2
    exec 9>&-
    return 2
  fi
  exec 9>&-
  return 0
}

# A job arriving here already has an issue marked `aes:claimed` by the
# dispatcher, which does not rescan that state: rejecting immediately would
# leave it stalled until someone queues it manually. For the concurrency cap,
# wait for a slot instead—on a self-hosted runner this costs machine time only.
# Disk failures still reject because waiting cannot help, as does timeout.
scaduto=$(( $(date +%s) + WAIT_SECONDS ))
atteso=0
free_gb="?"
while true; do
  esito=0
  prova_ad_occupare_un_posto || esito=$?
  [ "$esito" -eq 0 ] && break
  [ "$esito" -eq 2 ] && exit 1
  if [ "$(date +%s)" -ge "$scaduto" ]; then
    echo "::error::no host slot available after ${atteso}s (cap ${MAX_JOBS}): job rejected." >&2
    # Only the worker arrives here with an already claimed issue. The dispatcher
    # runs before claiming: if it is rejected, issues remain `aes:ready` and
    # the next schedule picks them up automatically.
    if [ "${GITHUB_JOB:-}" = "worker" ]; then
      echo "::error::issue remains 'aes:claimed' and must be queued manually: see docs/runner-self-hosted.md" >&2
    fi
    exit 1
  fi
  [ "$atteso" -eq 0 ] && echo "host at ${MAX_JOBS}-job cap: waiting for a slot, up to ${WAIT_SECONDS}s"
  sleep "$POLL_SECONDS"
  atteso=$(( atteso + POLL_SECONDS ))
done

echo "AES job started after ${atteso}s of waiting, ${free_gb}G free"
