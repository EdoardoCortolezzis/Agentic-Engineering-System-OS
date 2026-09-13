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

adopt_mode=0
if [ "$#" -eq 2 ] && [ "$1" = "--adopt" ] && [ -n "$2" ]; then
    adopt_mode=1
    feature_name="$2"
elif [ "$#" -eq 1 ] && [ -n "$1" ]; then
    feature_name="$1"
else
    printf 'Usage: %s [--adopt] <name>\n' "${BASH_SOURCE[0]}" >&2
    exit 2
fi

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$ROOT" ]; then
    printf 'Error: cannot determine the Git repository root.\n' >&2
    exit 1
fi

# Use the physical repository root for path construction and symlink targets.
ROOT="$(CDPATH= cd -- "$ROOT" 2>/dev/null && pwd -P)"
if [ -z "$ROOT" ]; then
    printf 'Error: cannot resolve the Git repository root.\n' >&2
    exit 1
fi

# BRANCH_PREFIXES is a space-separated list; the first entry is the prefix
# used for a newly started feature.
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

# A stale origin/develop would defeat the purpose of starting from the
# current integration branch, so fetch failures are fatal here.  The queue
# workflow deliberately uses checkout ``persist-credentials: false``: when a
# trusted caller provides GH_TOKEN, use it only through an ephemeral
# askpass helper.  The token never appears in argv, Git config, or the
# worktree inherited by the model child.  Disabling credential.helper also
# prevents an inherited checkout helper from silently changing the auth path.
trusted_fetch() {
    local fetch_status=0
    local previous_exit_trap=''
    local previous_int_trap=''
    local previous_term_trap=''

    # Keep the helper out of the worktree even when fetch is interrupted or
    # the shell exits from an unexpected error.  Capture and restore traps so
    # this helper remains composable when called by a larger harness script.
    previous_exit_trap="$(trap -p EXIT)"
    previous_int_trap="$(trap -p INT)"
    previous_term_trap="$(trap -p TERM)"
    _aes_fetch_askpass=''

    _aes_cleanup_askpass() {
        local status=$?
        if [ -n "${_aes_fetch_askpass:-}" ]; then
            rm -f -- "$_aes_fetch_askpass" || true
            _aes_fetch_askpass=''
        fi
        return "$status"
    }

    _aes_restore_fetch_traps() {
        if [ -n "$previous_exit_trap" ]; then
            eval "$previous_exit_trap"
        else
            trap - EXIT
        fi
        if [ -n "$previous_int_trap" ]; then
            eval "$previous_int_trap"
        else
            trap - INT
        fi
        if [ -n "$previous_term_trap" ]; then
            eval "$previous_term_trap"
        else
            trap - TERM
        fi
    }

    trap '_aes_cleanup_askpass' EXIT INT TERM

    if [ -n "${GH_TOKEN:-}" ]; then
        if ! _aes_fetch_askpass="$(mktemp "${TMPDIR:-/tmp}/aes-git-askpass.XXXXXX")"; then
            printf 'Error: cannot create temporary credential helper.\n' >&2
            _aes_restore_fetch_traps
            return 1
        fi
        chmod 700 "$_aes_fetch_askpass" || {
            _aes_cleanup_askpass
            printf 'Error: cannot secure temporary credential helper.\n' >&2
            _aes_restore_fetch_traps
            return 1
        }
        # Keep the secret in the trusted process environment.  The helper is
        # deleted before this function returns and is never copied into a
        # worktree or passed to the agent adapter.
        if ! printf '%s\n' \
            '#!/usr/bin/env bash' \
            'case "${1:-}" in' \
            '  *Username*) printf "%s\\n" "x-access-token" ;;' \
            '  *Password*) printf "%s\\n" "${GH_TOKEN:-}" ;;' \
            '  *) exit 1 ;;' \
            'esac' > "$_aes_fetch_askpass"; then
            _aes_cleanup_askpass
            printf 'Error: cannot write temporary credential helper.\n' >&2
            _aes_restore_fetch_traps
            return 1
        fi
        GIT_ASKPASS="$_aes_fetch_askpass" GIT_TERMINAL_PROMPT=0 \
            git -c credential.helper= fetch --prune
        fetch_status=$?
        _aes_cleanup_askpass
        _aes_restore_fetch_traps
        return "$fetch_status"
    fi

    git fetch --prune
    fetch_status=$?
    _aes_restore_fetch_traps
    return "$fetch_status"
}

if ! trusted_fetch; then
    printf 'Error: git fetch --prune failed.\n' >&2
    exit 1
fi

# Check refs before creating any directory or worktree. Adoption requires the
# remote ref created by the atomic claim and still rejects a local collision.
if [ "$adopt_mode" -eq 1 ]; then
    if git show-ref --verify --quiet "refs/heads/$branch"; then
        printf 'Error: branch already exists locally: %s\n' "$branch" >&2
        exit 1
    fi
    if ! git show-ref --verify --quiet "refs/remotes/origin/$branch"; then
        printf 'Error: remote branch to adopt does not exist: %s\n' "$branch" >&2
        exit 1
    fi
elif git show-ref --verify --quiet "refs/heads/$branch" || \
     git show-ref --verify --quiet "refs/remotes/origin/$branch"; then
        printf 'Error: branch already exists locally or remotely: %s\n' "$branch" >&2
        exit 1
fi

worktree_root_config="$WORKTREE_ROOT"
case "$worktree_root_config" in
    /*) worktree_root_path="$worktree_root_config" ;;
    *) worktree_root_path="${ROOT}/${worktree_root_config}" ;;
esac

if ! mkdir -p "$worktree_root_path"; then
    printf 'Error: cannot create worktree directory: %s\n' "$worktree_root_path" >&2
    exit 1
fi

worktree_root_path="$(CDPATH= cd -- "$worktree_root_path" 2>/dev/null && pwd -P)"
if [ -z "$worktree_root_path" ]; then
    printf 'Error: cannot resolve the worktree directory.\n' >&2
    exit 1
fi

repo_name="$(basename -- "$ROOT")"
worktree_path="${worktree_root_path}/${repo_name}/${feature_name}"

if [ "$adopt_mode" -eq 1 ]; then
    worktree_base="origin/$branch"
    worktree_arguments=(--track -b "$branch")
else
    worktree_base="origin/$INTEGRATION_BRANCH"
    worktree_arguments=(-b "$branch")
fi

if ! git worktree add "${worktree_arguments[@]}" "$worktree_path" "$worktree_base"; then
    printf 'Error: cannot create worktree: %s\n' "$worktree_path" >&2
    exit 1
fi

# Resolve an existing path to an absolute physical path. Kept in shell so the
# harness does not depend on a platform-specific realpath implementation:
# `cd -P` resolves any symlink in the parent directories, which is what the
# symlink target needs to be stable when the worktree lives outside the repo.
canonical_path() {
    local directory

    if [ -d "$1" ]; then
        (CDPATH= cd -P -- "$1" 2>/dev/null && pwd -P)
        return
    fi

    directory="$(CDPATH= cd -P -- "$(dirname -- "$1")" 2>/dev/null && pwd -P)" || return 1
    printf '%s/%s\n' "$directory" "$(basename -- "$1")"
}

# LINKED_FILES is intentionally a space-separated list of paths.
created_linked_files=()
for linked_file in $LINKED_FILES; do
    source_path="${ROOT}/${linked_file}"
    if [ ! -e "$source_path" ]; then
        continue
    fi

    resolved_source="$(canonical_path "$source_path")"
    if [ -z "$resolved_source" ]; then
        printf 'Error: cannot resolve linked file: %s\n' "$source_path" >&2
        exit 1
    fi

    worktree_link="${worktree_path}/${linked_file}"
    worktree_link_parent="$(dirname -- "$worktree_link")"
    if ! mkdir -p "$worktree_link_parent"; then
        printf 'Error: cannot prepare linked file: %s\n' "$worktree_link" >&2
        exit 1
    fi
    if ! ln -s "$resolved_source" "$worktree_link"; then
        printf 'Error: cannot create symlink: %s\n' "$worktree_link" >&2
        exit 1
    fi
    created_linked_files+=("$linked_file")
done

# A trailing-slash .gitignore pattern matches directories but not a symlink
# with the same name. Use the common git-dir because a per-worktree
# info/exclude is not read by Git for this purpose.
if [ "${#created_linked_files[@]}" -gt 0 ]; then
    if ! common_git_dir="$(git rev-parse --git-common-dir 2>/dev/null)" || \
       [ -z "$common_git_dir" ]; then
        printf 'Warning: cannot determine common git-dir; linked files are not excluded.\n' >&2
    else
        case "$common_git_dir" in
            /*) ;;
            *) common_git_dir="${ROOT}/${common_git_dir}" ;;
        esac

        info_dir="${common_git_dir}/info"
        info_exclude="${info_dir}/exclude"
        if ! mkdir -p "$info_dir"; then
            printf 'Warning: cannot create Git info directory; linked files are not excluded.\n' >&2
        else
            for linked_file in "${created_linked_files[@]}"; do
                exclude_entry="/${linked_file}"
                if grep -Fqx -- "$exclude_entry" "$info_exclude" 2>/dev/null; then
                    continue
                fi
                if ! printf '%s\n' "$exclude_entry" >> "$info_exclude"; then
                    printf 'Warning: cannot add %s to %s; continuing.\n' \
                        "$exclude_entry" "$info_exclude" >&2
                fi
            done
        fi
    fi
fi

printf 'Worktree created: %s\n' "$worktree_path"
printf 'Run: cd %s\n' "$worktree_path"
exit 0
