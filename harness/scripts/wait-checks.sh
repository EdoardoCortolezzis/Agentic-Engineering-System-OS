#!/usr/bin/env bash

set -euo pipefail

# Wait once for a branch's GitHub Actions checks and summarize the result.
# Usage: harness/scripts/wait-checks.sh <branch>

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
    printf 'Usage: %s <branch>\n' "${BASH_SOURCE[0]}" >&2
    exit 2
fi

branch="$1"
timeout_seconds="${WAIT_CHECKS_TIMEOUT_SECONDS:-1800}"
poll_seconds="${WAIT_CHECKS_POLL_SECONDS:-15}"

if ! [[ "$timeout_seconds" =~ ^[1-9][0-9]*$ ]] || ! [[ "$poll_seconds" =~ ^[1-9][0-9]*$ ]]; then
    printf 'Error: WAIT_CHECKS_TIMEOUT_SECONDS and WAIT_CHECKS_POLL_SECONDS must be positive integers.\n' >&2
    exit 2
fi

if ! local_head_sha="$(git rev-parse --verify "${branch}^{commit}" 2>/dev/null)"; then
    printf 'Error: local branch %s does not point to a valid commit.\n' "$branch" >&2
    exit 2
fi

target_sha="$local_head_sha"
observed_ref="$branch"
remote_ref="refs/remotes/origin/${branch}"
if git show-ref --verify --quiet "$remote_ref"; then
    remote_head_sha="$(git rev-parse --verify "${remote_ref}^{commit}")"
    if [ "$remote_head_sha" != "$local_head_sha" ]; then
        # gh observes the branch on the remote repository, not its local ref.
        target_sha="$remote_head_sha"
        observed_ref="origin/${branch}"
    fi
fi

get_runs() {
    gh run list --branch "$branch" --limit 100 \
        --json headSha,name,status,conclusion,databaseId
}

started_at="$SECONDS"
while :; do
    all_runs_json="$(get_runs)"
    runs_json="$(printf '%s' "$all_runs_json" | jq --arg sha "$target_sha" '[.[] | select(.headSha == $sha)]')"
    run_count="$(printf '%s' "$runs_json" | jq 'length')"

    elapsed=$((SECONDS - started_at))
    if [ "$run_count" -eq 0 ]; then
        if [ "$elapsed" -ge "$timeout_seconds" ]; then
            printf 'Timeout: no run for commit %s within %s seconds.\n' \
                "$target_sha" "$timeout_seconds" >&2
            exit 1
        fi

        sleep "$poll_seconds"
        continue
    fi

    active_runs="$(printf '%s' "$runs_json" | jq '[.[] | select(.status != "completed")] | length')"

    if [ "$active_runs" -eq 0 ]; then
        break
    fi

    if [ "$elapsed" -ge "$timeout_seconds" ]; then
        printf 'Timeout: checks for commit %s did not finish within %s seconds.\n' \
            "$target_sha" "$timeout_seconds" >&2
        exit 1
    fi

    sleep "$poll_seconds"
done

printf 'Workflow result for branch %s (commit %s):\n' "$branch" "$target_sha"
if [ "$target_sha" != "$local_head_sha" ]; then
    printf 'The local branch points to %s; reporting %s, the ref observed by GitHub.\n' \
        "$local_head_sha" "$observed_ref"
fi
workflow_rows="$(printf '%s' "$runs_json" | jq -r '
    sort_by(.name, .databaseId)
    | group_by(.name)
    | map(last)
    | .[]
    | [.databaseId, .name, (.conclusion // .status)]
    | @tsv
')"

if [ -z "$workflow_rows" ]; then
    printf 'No GitHub Actions run found.\n'
else
    while IFS=$'\t' read -r run_id workflow_name conclusion; do
        printf -- '- %s: %s (run %s)\n' "$workflow_name" "$conclusion" "$run_id"
    done <<< "$workflow_rows"

    failed_run_ids="$(printf '%s' "$runs_json" | jq -r '
        sort_by(.name, .databaseId)
        | group_by(.name)
        | map(last)
        | .[]
        | select(.conclusion == "failure")
        | .databaseId
    ')"
    if [ -n "$failed_run_ids" ]; then
        printf '\nError logs from failed runs:\n'
        while IFS= read -r failed_run_id; do
            printf '\nRun %s:\n' "$failed_run_id"
            # Only lines explaining failure: a full review log can exceed one
            # thousand lines, and this script exists so it need not be read all.
            if error_lines="$(gh run view "$failed_run_id" --log-failed 2>/dev/null)"; then
                printf '%s\n' "$error_lines" \
                    | grep -iE '##\[error\]|^[^[:space:]]*[[:space:]]+Error|error:|failed|exceeding' \
                    | tail -20 \
                    || printf 'No recognizable error line in run %s: read the full log with `gh run view %s --log-failed`.\n' \
                        "$failed_run_id" "$failed_run_id"
            else
                printf 'Cannot read error lines for run %s.\n' "$failed_run_id" >&2
            fi
        done <<< "$failed_run_ids"
    fi
fi

pr_number="$(gh pr list --head "$branch" --state all --limit 1 --json number --jq '.[0].number // empty')"
if [ -z "$pr_number" ]; then
    printf '\nNo PR associated with branch %s.\n' "$branch"
    exit 0
fi

printf '\nReview comments for PR #%s:\n' "$pr_number"
# Comments accumulate on a PR and remain after every push: without saying
# which commit they belong to, a resolved finding looks new on every run.
# This happened in practice and is the same false signal these scripts exist
# to avoid.
# Use `original_commit_id`, not `commit_id`: GitHub remaps the latter to the
# new head when the commented line still exists, so a finding written on an
# old commit can appear to concern the current one. `/reviews` entries lack
# that field and use `commit_id`.
mark_stale='
    (if ((.original_commit_id // .commit_id) // "") == $sha then "" else " [ON A PREVIOUS COMMIT]" end) as $stale
'
review_comments="$(gh api --paginate "repos/{owner}/{repo}/pulls/${pr_number}/reviews" \
    | jq -r --arg sha "$target_sha" ".[] | select(.body != \"\") | ${mark_stale} | \"\(.user.login) [\(.state)]\(\$stale): \(.body)\"")"
inline_comments="$(gh api --paginate "repos/{owner}/{repo}/pulls/${pr_number}/comments" \
    | jq -r --arg sha "$target_sha" ".[] | ${mark_stale} | \"\(.user.login) [comment on \(.path)]\(\$stale): \(.body)\"")"

if [ -z "$review_comments" ] && [ -z "$inline_comments" ]; then
    printf 'No review comments.\n'
else
    if [ -n "$review_comments" ]; then
        printf '%s\n' "$review_comments"
    fi
    if [ -n "$inline_comments" ]; then
        printf '%s\n' "$inline_comments"
    fi
fi
