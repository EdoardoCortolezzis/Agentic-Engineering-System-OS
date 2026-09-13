#!/usr/bin/env bash

set -uo pipefail

# Resolve configuration from this script's location, so the script can be
# invoked from any directory inside the target repository.
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P)"
CONFIG_FILE="${SCRIPT_DIR}/../config/harness.conf"

if [ ! -r "$CONFIG_FILE" ]; then
    printf 'Error: configuration is unreadable: %s\n' "$CONFIG_FILE" >&2
    exit 2
fi

# shellcheck source=/dev/null
source "$CONFIG_FILE"

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
    printf 'Usage: %s <name>\n' "${BASH_SOURCE[0]}" >&2
    exit 2
fi

feature_name="$1"

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$ROOT" ]; then
    printf 'Error: cannot determine the Git repository root.\n' >&2
    exit 1
fi

# Use the physical repository root for path construction.
ROOT="$(CDPATH= cd -- "$ROOT" 2>/dev/null && pwd -P)"
if [ -z "$ROOT" ]; then
    printf 'Error: cannot resolve the Git repository root.\n' >&2
    exit 1
fi

# BRANCH_PREFIXES is a space-separated list; the first entry is the prefix
# used for a feature branch.
read -r branch_prefix _ <<< "$BRANCH_PREFIXES"
if [ -z "${branch_prefix:-}" ]; then
    printf 'Error: BRANCH_PREFIXES contains no prefix.\n' >&2
    exit 1
fi

branch="${branch_prefix}/${feature_name}"
if ! git check-ref-format --branch "$branch" >/dev/null 2>&1; then
    printf 'Error: invalid feature name: %s\n' "$feature_name" >&2
    exit 2
fi

worktree_root_config="$WORKTREE_ROOT"
case "$worktree_root_config" in
    /*) worktree_root_path="$worktree_root_config" ;;
    *) worktree_root_path="${ROOT}/${worktree_root_config}" ;;
esac

# Resolve an existing worktree root physically. If it does not exist, resolve
# its parent so the expected path can still be compared with Git's registry.
if [ -d "$worktree_root_path" ]; then
    worktree_root_path="$(CDPATH= cd -- "$worktree_root_path" 2>/dev/null && pwd -P)"
else
    worktree_root_parent="$(CDPATH= cd -- "$(dirname -- "$worktree_root_path")" 2>/dev/null && pwd -P)"
    if [ -z "$worktree_root_parent" ]; then
        printf 'Error: cannot resolve the worktree directory.\n' >&2
        exit 1
    fi
    worktree_root_path="${worktree_root_parent}/$(basename -- "$worktree_root_path")"
fi
if [ -z "$worktree_root_path" ]; then
    printf 'Error: cannot resolve the worktree directory.\n' >&2
    exit 1
fi

repo_name="$(basename -- "$ROOT")"
worktree_path="${worktree_root_path}/${repo_name}/${feature_name}"

if ! command -v gh >/dev/null 2>&1; then
    printf 'Error: gh command unavailable; no worktree was removed.\n' >&2
    exit 1
fi

pr_json="$(gh pr view "$branch" --json state,mergedAt 2>/dev/null)"
if [ "$?" -ne 0 ]; then
    printf 'Error: cannot query PR state for %s; no worktree was removed.\n' "$branch" >&2
    exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
    printf 'Error: jq command unavailable; no worktree was removed.\n' >&2
    exit 1
fi

pr_state="$(printf '%s\n' "$pr_json" | jq -r '.state // empty' 2>/dev/null)"
if [ "$?" -ne 0 ]; then
    printf 'Error: invalid PR JSON response; no worktree was removed.\n' >&2
    exit 1
fi

if [ "$pr_state" != "MERGED" ]; then
    printf 'Error: PR for branch %s is not MERGED (state: %s); worktree and branch were not removed.\n' \
        "$branch" "${pr_state:-unknown}" >&2
    exit 1
fi

# Deleting a checked-out branch is forbidden by Git. In the expected flow the
# main worktree is checked out on the integration branch, but guard explicitly
# against being invoked from the feature branch itself.
current_branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
if [ "$current_branch" = "$branch" ]; then
    printf 'Error: current branch is the one to delete; no worktree was removed.\n' >&2
    exit 1
fi

worktree_list="$(git worktree list --porcelain 2>/dev/null)"
if [ "$?" -ne 0 ]; then
    printf 'Error: cannot list registered worktrees.\n' >&2
    exit 1
fi

worktree_registered=false
while IFS= read -r worktree_entry; do
    case "$worktree_entry" in
        "worktree "*)
            registered_path="${worktree_entry#worktree }"
            if [ "$registered_path" = "$worktree_path" ]; then
                worktree_registered=true
                break
            fi
            ;;
    esac
done <<< "$worktree_list"

if ! git fetch --prune; then
    printf 'Error: git fetch --prune failed.\n' >&2
    exit 1
fi

integration_ref="origin/${INTEGRATION_BRANCH}"
if ! git rev-parse --verify --quiet "${integration_ref}^{commit}" >/dev/null; then
    printf 'Error: integration branch unavailable: %s\n' "$integration_ref" >&2
    exit 1
fi
# If the branch is already an ancestor of the integration branch, nothing
# else is needed: this is the `merge --no-ff` case that `git branch -d` handles.
ahead_count="$(git rev-list --count "${integration_ref}..${branch}" 2>/dev/null)"
if [ -z "$ahead_count" ]; then
    printf 'Error: cannot compare %s with %s; branch kept.\n' "$branch" "$integration_ref" >&2
    exit 1
fi

if [ "$ahead_count" -ne 0 ]; then
    # A squash merges N commits into one: the individual commit patch-ids do
    # not survive, so `git cherry "$integration_ref" "$branch"` would report
    # all of them as unintegrated (verified: 3 squashed commits produce three
    # '+' lines). What survives is the branch's COMPLETE patch: build a
    # temporary commit with the branch tree attached to the merge-base, then
    # look for that patch-id in the integration branch.
    merge_base="$(git merge-base "$integration_ref" "$branch" 2>/dev/null)"
    branch_tree="$(git rev-parse "${branch}^{tree}" 2>/dev/null)"
    probe_commit=""
    if [ -n "$merge_base" ] && [ -n "$branch_tree" ]; then
        probe_commit="$(git commit-tree "$branch_tree" -p "$merge_base" -m 'squash-probe' 2>/dev/null)"
    fi
    if [ -z "$probe_commit" ]; then
        printf 'Error: cannot compare %s with %s; branch kept.\n' "$branch" "$integration_ref" >&2
        exit 1
    fi
    if git cherry "$integration_ref" "$probe_commit" | grep -q '^+ '; then
        printf 'Error: branch %s kept; its content is not integrated into %s. Unintegrated commits:\n%s\n' \
            "$branch" "$integration_ref" "$(git log --oneline "${integration_ref}..${branch}")" >&2
        exit 1
    fi
fi

if [ "$worktree_registered" = true ]; then
    if [ -d "$worktree_path" ]; then
        if ! git worktree remove "$worktree_path"; then
            printf 'Error: cannot remove worktree: %s\n' "$worktree_path" >&2
            exit 1
        fi
    else
        # A registered but missing directory is stale metadata, not a reason
        # to block cleanup of the already merged branch.
        if ! git worktree prune --expire now; then
            printf 'Error: cannot clean missing worktree: %s\n' "$worktree_path" >&2
            exit 1
        fi
    fi
fi

if git branch -d "$branch"; then
    :
else
    if ! git branch -D "$branch"; then
        printf 'Error: cannot delete local branch: %s\n' "$branch" >&2
        exit 1
    fi
    printf 'Branch %s force-deleted: the merge was a squash.\n' "$branch"
fi

printf 'Feature completed and cleaned up: %s\n' "$branch"
exit 0
