#!/usr/bin/env bash
set -euo pipefail

# Run a local review of the current branch diff before pushing.
# Untracked files are excluded because they are not part of the change and
# including them would send arbitrary disk content to an external service.
# Usage: harness/scripts/review-locale.sh [--second-opinion] [branch-base]
# Hardcoded defaults are a deliberate exception to the model-agnostic rule;
# the environment can still override them to limit scope (ADR 0017).
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/../config/harness.conf"

primary_reviewer_from_env="${PRIMARY_REVIEWER+x}"
primary_reviewer_value_from_env="${PRIMARY_REVIEWER-}"
primary_model_from_env="${PRIMARY_REVIEWER_MODEL+x}"
primary_model_value_from_env="${PRIMARY_REVIEWER_MODEL-}"
fallback_reviewer_from_env="${FALLBACK_REVIEWER+x}"
fallback_reviewer_value_from_env="${FALLBACK_REVIEWER-}"
fallback_model_from_env="${FALLBACK_REVIEWER_MODEL+x}"
fallback_model_value_from_env="${FALLBACK_REVIEWER_MODEL-}"
quota_message_from_env="${QUOTA_FAILURE_MESSAGE+x}"
quota_message_value_from_env="${QUOTA_FAILURE_MESSAGE-}"

PRIMARY_REVIEWER="codex"
PRIMARY_REVIEWER_MODEL="gpt-5.6-sol"
FALLBACK_REVIEWER="claude"
FALLBACK_REVIEWER_MODEL="opus"
QUOTA_FAILURE_MESSAGE="You've hit your usage limit"
if [ -r "$CONFIG_FILE" ]; then
    # Only harness.conf is configuration; never load .env or other files.
    source "$CONFIG_FILE"
fi
if [ -n "$primary_reviewer_from_env" ]; then PRIMARY_REVIEWER="$primary_reviewer_value_from_env"; fi
if [ -n "$primary_model_from_env" ]; then PRIMARY_REVIEWER_MODEL="$primary_model_value_from_env"; fi
if [ -n "$fallback_reviewer_from_env" ]; then FALLBACK_REVIEWER="$fallback_reviewer_value_from_env"; fi
if [ -n "$fallback_model_from_env" ]; then FALLBACK_REVIEWER_MODEL="$fallback_model_value_from_env"; fi
if [ -n "$quota_message_from_env" ]; then QUOTA_FAILURE_MESSAGE="$quota_message_value_from_env"; fi
second_opinion=false
base_branch="${INTEGRATION_BRANCH:-develop}"
base_argument_set=false

for argument in "$@"; do
    case "$argument" in
        --second-opinion) second_opinion=true ;;
        -*) printf 'Usage: %s [--second-opinion] [branch-base]\n' "${BASH_SOURCE[0]}" >&2; exit 2 ;;
        *)
            if [ "$base_argument_set" = true ]; then
                printf 'Usage: %s [--second-opinion] [branch-base]\n' "${BASH_SOURCE[0]}" >&2; exit 2
            fi
            base_branch="$argument"
            base_argument_set=true
            ;;
    esac
done
if [ "$#" -gt 2 ]; then
    printf 'Usage: %s [--second-opinion] [branch-base]\n' "${BASH_SOURCE[0]}" >&2; exit 2
fi

validate_reviewer() {
    case "$1" in
        codex|claude) ;;
        *)
            printf 'Error: unrecognized reviewer adapter: %s (valid values: codex, claude).\n' "$1" >&2
            exit 2
            ;;
    esac
}

validate_reviewer "$PRIMARY_REVIEWER"
validate_reviewer "$FALLBACK_REVIEWER"

repository_root="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$repository_root" ]; then
    printf 'Error: cannot determine the Git repository root.\n' >&2; exit 1
fi
criteria_file="${repository_root}/docs/review-criteria.md"
if [ ! -r "$criteria_file" ]; then
    printf 'Error: review criteria are unreadable: %s\n' "$criteria_file" >&2; exit 1
fi
if git fetch --prune >/dev/null 2>&1; then :; else
    printf 'Warning: git fetch --prune failed; using local refs.\n' >&2
fi
case "$base_branch" in
    origin/*) base_ref="$base_branch" ;;
    *) base_ref="origin/${base_branch}" ;;
esac
if ! git rev-parse --verify --quiet "${base_ref}^{commit}" >/dev/null; then
    printf 'Error: base branch unavailable: %s\n' "$base_ref" >&2; exit 1
fi

diff_output="$(git diff --no-ext-diff "$base_ref")"
if [ -z "$diff_output" ]; then
    printf 'No diff between the current branch and %s: local review is unnecessary.\n' "$base_ref"; exit 0
fi

newline=$'\n'
criteria_prompt="You are the local reviewer for the current repository. Respond in English.${newline}${newline}"
criteria_prompt+="Clearly distinguish BLOCKING from NON-BLOCKING findings. For each finding, cite the number and short text of the applied criterion. Do not invent style findings outside the criteria. If you find none, state that explicitly.${newline}${newline}"
criteria_prompt+="--- Review criteria ---${newline}$(<"$criteria_file")"
primary_prompt="$criteria_prompt${newline}Review the current branch changes against ${base_ref}."
review_prompt="$criteria_prompt${newline}--- Diff to review ---${newline}${diff_output}"

primary_output_file="$(mktemp)"
fallback_output_file="$(mktemp)"
trap 'rm -f "$primary_output_file" "$fallback_output_file"' EXIT
run_codex_adapter() { printf '%s' "$2" | codex exec review -m "$1" -; }
run_claude_adapter() { printf '%s' "$2" | claude -p --model "$1"; }
run_reviewer() {
    case "$1" in
        codex) run_codex_adapter "$2" "$3" ;;
        claude) run_claude_adapter "$2" "$3" ;;
        *)
            printf 'Error: unrecognized reviewer adapter: %s (valid values: codex, claude).\n' "$1" >&2
            return 2
            ;;
    esac
}
run_primary() { run_reviewer "$PRIMARY_REVIEWER" "$PRIMARY_REVIEWER_MODEL" "$primary_prompt" >"$primary_output_file" 2>&1; }
run_fallback() { run_reviewer "$FALLBACK_REVIEWER" "$FALLBACK_REVIEWER_MODEL" "$review_prompt" >"$fallback_output_file" 2>&1; }
is_quota_failure() { grep -Fqi "$QUOTA_FAILURE_MESSAGE" "$primary_output_file"; }

if "$second_opinion"; then
    primary_status=0; fallback_status=0
    run_primary || primary_status=$?
    run_fallback || fallback_status=$?
    printf '%s\n' '=== Primary review ==='; cat "$primary_output_file"
    printf '%s\n' '=== Fallback review ==='; cat "$fallback_output_file"
    if [ "$primary_status" -ne 0 ] || [ "$fallback_status" -ne 0 ]; then
        if [ "$primary_status" -eq 0 ]; then
            printf 'Error: primary review succeeded; fallback review failed.\n' >&2
        elif [ "$fallback_status" -eq 0 ]; then
            printf 'Error: primary review failed; fallback review succeeded.\n' >&2
        else
            printf 'Error: both reviews failed.\n' >&2
        fi
        exit 1
    fi
    exit 0
fi
if run_primary; then cat "$primary_output_file"; exit 0; fi
if ! is_quota_failure; then
    cat "$primary_output_file" >&2
    printf 'Error: primary reviewer failed; no fallback was run.\n' >&2; exit 1
fi
printf 'Warning: primary reviewer subscription quota exhausted; trying fallback.\n' >&2
if run_fallback; then cat "$fallback_output_file"; exit 0; fi
cat "$fallback_output_file" >&2
printf 'Error: no review completed successfully; both reviewers failed.\n' >&2
exit 1
