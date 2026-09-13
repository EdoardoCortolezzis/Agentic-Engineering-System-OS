#!/usr/bin/env bash

set -uo pipefail

# Resolve configuration from this script's location, so the script can be
# invoked from any directory inside the target repository.
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/../config/harness.conf"

if [ ! -r "$CONFIG_FILE" ]; then
    printf 'Error: configuration is unreadable: %s\n' "$CONFIG_FILE" >&2
    exit 2
fi

# shellcheck source=/dev/null
source "$CONFIG_FILE"

check_mode=0
if [ "${1:-}" = "--check" ]; then
    check_mode=1
    shift
fi

if [ "$#" -ne 0 ]; then
    printf 'Usage: %s [--check]\n' "${BASH_SOURCE[0]}" >&2
    exit 2
fi

current_branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null)"
if [ -z "$current_branch" ]; then
    current_branch='(detached)'
fi

current_sha="$(git rev-parse --short HEAD 2>/dev/null)"
if [ -z "$current_sha" ]; then
    current_sha='unknown'
fi

# Fetch is deliberately the only network operation. Failure is non-fatal:
# the remaining checks use whatever refs are available locally.
if git fetch --prune >/dev/null 2>&1; then
    fetch_ok=1
else
    fetch_ok=0
    printf 'Warning: git fetch --prune failed; using local refs.\n' >&2
fi

integration_ref="origin/${INTEGRATION_BRANCH}"

behind=0
ahead=0
divergence="$(git rev-list --left-right --count "${integration_ref}...HEAD" 2>/dev/null)"
if [ -n "$divergence" ]; then
    # With integration_ref...HEAD, the left count is behind and the right
    # count is ahead.
    read -r behind ahead <<EOF
$divergence
EOF
fi

commits_ahead="$(git rev-list --count "${integration_ref}..HEAD" 2>/dev/null)"
if [ -z "$commits_ahead" ]; then
    commits_ahead=0
fi

# A branch can have a different SHA from the integration branch while
# carrying exactly the same patch (for example after squash/rebase or a
# cherry-pick). `git cherry` compares patch identity rather than ancestry.
content_merged='unknown'
cherry_output="$(git cherry "$integration_ref" HEAD 2>/dev/null)"
cherry_status=$?
if [ "$cherry_status" -eq 0 ]; then
    has_unmatched=0
    while IFS= read -r cherry_line; do
        case "$cherry_line" in
            +*) has_unmatched=1 ;;
        esac
    done <<< "$cherry_output"

    if [ "$has_unmatched" -eq 0 ]; then
        content_merged='yes'
    else
        content_merged='no'
    fi
fi

# Read the branch configuration directly so a configured-but-pruned
# upstream (`gone`) is distinguishable from a branch with no upstream.
upstream_remote=''
upstream_merge=''
if [ "$current_branch" != '(detached)' ]; then
    upstream_remote="$(git config --get "branch.${current_branch}.remote" 2>/dev/null)"
    upstream_merge="$(git config --get "branch.${current_branch}.merge" 2>/dev/null)"
fi

upstream_status='missing'
if [ -n "$upstream_remote" ] && [ -n "$upstream_merge" ]; then
    merge_branch="${upstream_merge#refs/heads/}"
    if [ "$upstream_remote" = '.' ]; then
        upstream_ref="$merge_branch"
    else
        upstream_ref="${upstream_remote}/${merge_branch}"
    fi

    if git rev-parse --verify --quiet "${upstream_ref}^{commit}" >/dev/null 2>&1; then
        upstream_status='ok'
    else
        upstream_status='gone'
    fi
fi

working_tree_status="$(git status --porcelain 2>/dev/null)"
working_tree_state='dirty'
if [ -z "$working_tree_status" ] && git status --porcelain >/dev/null 2>&1; then
    working_tree_state='clean'
fi

worktree_output="$(git worktree list --porcelain 2>/dev/null)"
worktree_paths=''
while IFS= read -r worktree_line; do
    case "$worktree_line" in
        'worktree '*)
            worktree_path="${worktree_line#worktree }"
            if [ -n "$worktree_paths" ]; then
                worktree_paths="${worktree_paths}|${worktree_path}"
            else
                worktree_paths="$worktree_path"
            fi
            ;;
    esac
done <<< "$worktree_output"
if [ -z "$worktree_paths" ]; then
    worktree_paths='none'
fi

if [ "$current_branch" = "$INTEGRATION_BRANCH" ] || \
   [ "$current_branch" = "$PRODUCTION_BRANCH" ]; then
    verdict='PROTECTED'
elif [ "$upstream_status" = 'gone' ] || \
     { [ "$content_merged" = 'yes' ] && \
       [ "$commits_ahead" -gt 0 ]; }; then
    verdict='DEAD'
elif [ "$behind" -gt 0 ]; then
    verdict='STALE'
else
    verdict='OK'
fi

printf 'branch=%s sha=%s\n' "$current_branch" "$current_sha"
printf 'ahead=%s behind=%s vs=%s\n' "$ahead" "$behind" "$integration_ref"
printf 'upstream=%s\n' "$upstream_status"
printf 'content_on_%s=%s\n' "$integration_ref" "$content_merged"
printf 'working_tree=%s\n' "$working_tree_state"
printf 'worktrees=%s\n' "$worktree_paths"
printf 'VERDICT: %s\n' "$verdict"

if [ "$check_mode" -eq 1 ] && \
   { [ "$verdict" = 'STALE' ] || [ "$verdict" = 'DEAD' ]; }; then
    exit 1
fi

exit 0
