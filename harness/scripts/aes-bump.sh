#!/usr/bin/env bash

set -euo pipefail

stable_branch='automation/aes-harness-bump'
automation_name='AES harness bump'
automation_email='aes-harness-bump@users.noreply.github.com'
source_argument=''

usage() {
    printf 'Usage: %s --source <path-to-aes-checkout>\n' "$0" >&2
}

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --source)
            [ "$#" -ge 2 ] || { usage; fail '--source requires a path'; }
            source_argument="$2"
            shift 2
            ;;
        *)
            usage
            fail "unknown argument: $1"
            ;;
    esac
done

[ -n "$source_argument" ] || { usage; fail '--source is required'; }
[ -d "$source_argument" ] || fail "AES checkout not found: $source_argument"

consumer_root="$(git rev-parse --show-toplevel 2>/dev/null)" || fail 'not inside a Git consumer repository'
consumer_root="$(CDPATH= cd -- "$consumer_root" && pwd -P)"
source_root="$(CDPATH= cd -- "$source_argument" && pwd -P)"
# The sync tool is taken from the source, not from the consumer: it reads
# the source's manifest, so the two must come from the same tree. It also
# completes the self-healing property — otherwise a bug in aes-sync.sh would
# still need a manual bootstrap in every consumer.
sync_script="$source_root/harness/scripts/aes-sync.sh"
[ -x "$sync_script" ] || fail "sync script is not executable: $sync_script"

source_commit="$(git -C "$source_root" rev-parse HEAD 2>/dev/null)" || fail 'cannot determine AES HEAD'
version_path="$consumer_root/.harness-version"

# The managed-file check is deliberately the first external operation. A
# consumer with local drift must not create a branch, query GitHub, or push.
"$sync_script" --check --source "$source_root" || fail 'consumer has unmanaged harness drift'
source_relative=''
case "$source_root/" in
    "$consumer_root/"*) source_relative="${source_root#"$consumer_root/"}" ;;
esac
if [ -n "$source_relative" ]; then
    dirty_status="$(git status --porcelain --untracked-files=all -- . ":(exclude)$source_relative")"
else
    dirty_status="$(git status --porcelain --untracked-files=all)"
fi
if [ -n "$dirty_status" ]; then
    fail 'consumer working tree is not clean'
fi

# --check-upstream returns 1 both for a real lag and for malformed metadata;
# call it for the documented check, then classify the result locally so an
# invalid or divergent source_commit remains fail-closed.
set +e
"$sync_script" --check-upstream --source "$source_root"
upstream_status=$?
set -e
[ "$upstream_status" -eq 0 ] || [ "$upstream_status" -eq 1 ] || fail 'upstream check failed unexpectedly'

registered_commit=''
if [ -f "$version_path" ]; then
    registered_commit="$(python3 - "$version_path" <<'PY'
import json
import sys

try:
    value = json.load(open(sys.argv[1], encoding="utf-8")).get("source_commit", "")
except (OSError, ValueError):
    raise SystemExit(2)
print(value if isinstance(value, str) else "")
PY
)" || fail 'source_commit metadata is not valid'
fi

if [ -n "$registered_commit" ]; then
    git -C "$source_root" cat-file -e "${registered_commit}^{commit}" 2>/dev/null \
        || fail "registered AES commit is unavailable: $registered_commit"
    if [ "$registered_commit" = "$source_commit" ]; then
        printf 'AES harness is already up to date at %s.\n' "$source_commit"
        exit 0
    fi
    git -C "$source_root" merge-base --is-ancestor "$registered_commit" "$source_commit" \
        || fail 'registered AES commit diverges from the checked-out AES history'
fi

check_automation_history() {
    local base="$1" target="$2" commit
    git merge-base --is-ancestor "$base" "$target" \
        || fail 'stable branch is not a descendant of develop; refusing divergent history'
    while IFS= read -r commit; do
        [ -n "$commit" ] || continue
        git show -s --format=%B "$commit" \
            | git interpret-trailers --parse \
            | grep -Fxq 'AES-Automation: aes-harness-bump' \
            || fail 'stable branch contains work without the exact AES automation trailer'
    done < <(git rev-list "$base..$target")
}

# Fetch only the two refs that this workflow is allowed to inspect. No merge,
# reset, or force update is used: a remote branch that moved unexpectedly is
# treated as a conflict and stops the run. Keep fetch diagnostics visible: a
# missing stable ref is expected on the first run, while an auth/network error
# must not be mistaken for that case.
set +e
develop_fetch_output="$(git fetch --no-tags origin develop 2>&1)"
develop_fetch_status=$?
set -e
[ "$develop_fetch_status" -eq 0 ] || fail "cannot fetch origin/develop: $develop_fetch_output"
set +e
stable_fetch_output="$(git fetch --no-tags origin "$stable_branch" 2>&1)"
stable_fetch_status=$?
set -e
if [ "$stable_fetch_status" -ne 0 ]; then
    case "$stable_fetch_output" in
        *"couldn't find remote ref"*|*"remote ref does not exist"*|*"could not find remote ref"*)
            printf 'Notice: stable remote ref does not exist yet; treating this as the first run. Fetch output: %s\n' \
                "$stable_fetch_output" >&2
            ;;
        *)
            fail "cannot fetch stable ref: $stable_fetch_output"
            ;;
    esac
fi
develop_ref='refs/remotes/origin/develop'
git rev-parse --verify "$develop_ref" >/dev/null 2>&1 || fail 'origin/develop is unavailable'
remote_stable=''
develop_commit="$(git rev-parse "$develop_ref")"
if git rev-parse --verify "refs/remotes/origin/$stable_branch" >/dev/null 2>&1; then
    remote_stable="$(git rev-parse "refs/remotes/origin/$stable_branch")"
    check_automation_history "$develop_commit" "$remote_stable"
fi

if git show-ref --verify --quiet "refs/heads/$stable_branch"; then
    local_stable="$(git rev-parse "$stable_branch")"
    check_automation_history "$develop_commit" "$local_stable"
    if [ -n "$remote_stable" ] && [ "$local_stable" != "$remote_stable" ]; then
        fail 'local stable branch differs from its remote; refusing to overwrite it'
    fi
    git -c core.hooksPath=/dev/null switch "$stable_branch" >/dev/null
elif [ -n "$remote_stable" ]; then
    git -c core.hooksPath=/dev/null switch --create "$stable_branch" "$remote_stable" >/dev/null
else
    git -c core.hooksPath=/dev/null switch --create "$stable_branch" "$develop_ref" >/dev/null
fi

# Check for an ambiguous PR before changing or publishing the branch. A
# single existing PR is safe to reuse; more than one is an unrecoverable
# state that must not be made worse by a push.
: "${GH_TOKEN:?GH_TOKEN is required for consumer push and pull-request operations}"
preflight_pr_json="$(gh pr list --state open --head automation/aes-harness-bump --base develop --json number)" \
    || fail 'cannot list the stable pull request'
preflight_pr_count="$(python3 - "$preflight_pr_json" <<'PY'
import json
import sys
try:
    value = json.loads(sys.argv[1])
except ValueError:
    raise SystemExit(2)
if not isinstance(value, list):
    raise SystemExit(2)
print(len(value))
PY
)" || fail 'GitHub returned invalid pull-request data'
[ "$preflight_pr_count" -le 1 ] || fail 'multiple open AES harness pull requests found for the stable branch'

# A previous automation commit may already contain this exact source. This
# path is what makes a replay idempotent without creating an empty commit.
stable_source_commit=''
if [ -f "$version_path" ]; then
    stable_source_commit="$(python3 - "$version_path" <<'PY'
import json
import sys
try:
    value = json.load(open(sys.argv[1], encoding="utf-8")).get("source_commit", "")
except (OSError, ValueError):
    value = ""
print(value if isinstance(value, str) else "")
PY
)"
fi
if [ "$stable_source_commit" = "$source_commit" ]; then
    printf 'Stable branch already contains AES %s.\n' "$source_commit"
else
    "$sync_script" --source "$source_root"
    if ! git diff --quiet || ! git diff --cached --quiet; then
        if [ -n "$source_relative" ]; then
            git add -A -- . ":(exclude)$source_relative"
        else
            git add -A
        fi
        # The identity is set on the command line, not read from the
        # configuration: a CI runner has none, and `git commit` there aborts
        # with "Author identity unknown". It is also unconditional, so the
        # commit that the trailer marks as automation is attributed to the
        # automation everywhere, not to whoever happened to run the script.
        git -c core.hooksPath=/dev/null \
            -c user.name="$automation_name" \
            -c user.email="$automation_email" \
            commit -m "Aggiorna harness AES a ${source_commit:0:12}" \
            -m 'AES-Automation: aes-harness-bump' >/dev/null
    fi
fi

if [ -n "$remote_stable" ]; then
    git merge-base --is-ancestor "$remote_stable" HEAD \
        || fail 'stable branch cannot advance by fast-forward'
fi

# This is intentionally a fully qualified, literal destination. In
# particular, never push the current branch implicitly: a checkout regression
# must not turn this workflow into a push to develop.
git -c core.hooksPath=/dev/null push origin HEAD:refs/heads/automation/aes-harness-bump

: "${GH_TOKEN:?GH_TOKEN is required for consumer push and pull-request operations}"
pr_json="$(gh pr list --state open --head automation/aes-harness-bump --base develop --json number)" \
    || fail 'cannot list the stable pull request'
pr_count="$(python3 - "$pr_json" <<'PY'
import json
import sys
try:
    value = json.loads(sys.argv[1])
except ValueError:
    raise SystemExit(2)
if not isinstance(value, list):
    raise SystemExit(2)
print(len(value))
PY
)" || fail 'GitHub returned invalid pull-request data'

case "$pr_count" in
    0)
        gh pr create \
            --base develop \
            --head automation/aes-harness-bump \
            --title "Aggiorna harness AES a ${source_commit:0:12}" \
            --body "Aggiornamento automatico degli asset dell'harness da AES/develop a ${source_commit}. Il merge resta manuale." \
            >/dev/null || fail 'cannot create the stable pull request'
        ;;
    1)
        printf 'Reusing the existing AES harness pull request.\n'
        ;;
    *)
        fail 'multiple open AES harness pull requests found for the stable branch'
        ;;
esac
