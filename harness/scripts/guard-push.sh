#!/usr/bin/env bash
# PreToolUse guard: decide whether a Bash command pushes to a protected branch.
# It lives in a script, not a regex inside a JSON string, because three quoting
# levels made the previous version wrong in both directions.
#
# Split the command at separators: `develop` in one segment must not implicate
# `git push` in another (`git push origin feature/x && gh pr create --base
# develop` is valid).
#
# Core rule: **when in doubt, block**. This applies to a push without a refspec
# (the branch configuration chooses its destination), an unresolved `HEAD`,
# and every argument the shell would expand—quotes, `$`, backticks, and
# substitutions. Comparing that text literally would allow `git push origin
# 'develop'`, which is natural syntax, not an attack.
#
# Declared cost: because metacharacters attached to a command are stripped, a
# `git push` inside a string—`echo "git push origin develop"`—is blocked as a
# push. The same tokenizer makes `bash -c "git push origin develop"`
# recognizable; the two forms are identical at that level, so we choose the
# blocking false positive.
#
# Declared limit: a push hidden behind true indirection (a runtime-built
# variable or encoding) remains invisible. Stopping it would require recursive
# interpretation of arbitrary shell; this limitation is explicit.
#
# Exit: 0 allows, 2 blocks. Any other value means the guard did not decide;
# callers treat it as a block.

set -uo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P)"
CONFIG_FILE="${SCRIPT_DIR}/../config/harness.conf"

# These four names are the floor and cannot be removed; configuration may only
# add the consumer's integration branch. Extract the value with sed instead of
# `source`: this runs on every tool call, and executing configuration code is a
# poor tradeoff.
PROTECTED_BRANCHES="main master develop production"
# The harness release tag determines which code consumers execute in CI
# (ADR 0023): moving it is publishing. It is not configurable, for the same
# reason the four branches are a floor.
PROTECTED_TAGS="harness-release"
if [ -r "$CONFIG_FILE" ]; then
    for config_key in INTEGRATION_BRANCH PRODUCTION_BRANCH; do
        # `INTEGRATION_BRANCH="staging"  # comment` is natural syntax: without
        # removing the comment, the value becomes `staging" # comment`, the
        # loop splits it into words, and none matches the branch. The consumer
        # would silently remain pushable precisely when configuration matters.
        config_value="$(sed -n "s/^[[:space:]]*${config_key}[[:space:]]*=[[:space:]]*//p" "$CONFIG_FILE" \
            | tail -n 1 \
            | sed -e 's/[[:space:]]*#.*$//' -e 's/[[:space:]]*$//' \
                  -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'\$//")"
        case "$config_value" in
            "") ;;
            *[!A-Za-z0-9._/-]*)
                printf 'guard-push: %s in harness.conf is not a usable branch name (%s); only the default list remains protected.\n' \
                    "$config_key" "$config_value" >&2
                ;;
            *) PROTECTED_BRANCHES="$PROTECTED_BRANCHES $config_value" ;;
        esac
    done
fi

payload="$(cat)"
case "$payload" in
    *[![:space:]]*) ;;
    *)
        printf 'BLOCKED: no PreToolUse payload received; the guard could evaluate nothing.\n' >&2
        exit 2
        ;;
esac
if ! command_line="$(printf '%s' "$payload" | jq -r '.tool_input.command // empty' 2>/dev/null)"; then
    printf 'BLOCKED: PreToolUse payload cannot be decoded; blocking when in doubt.\n' >&2
    exit 2
fi
[ -n "$command_line" ] || exit 0

# A shell-expanded argument cannot be compared as text. Globs (*, ?, [) are
# free to block: `git check-ref-format` rejects them, so real refs never cause
# a false positive. Braces differ: `refs/heads/feature/{a}` is a legal name,
# so blocking them can hit a valid ref. Keep blocking because the shell expands
# them—`harness-relea{se,se}` is harmless comparison text and a release tag to
# the shell—and braces in branch names are pathological while the bypass is real.
is_opaque() {
    case "$1" in
        *[\'\"\$\`\(\)\&\\*\?\[\{\}]*) return 0 ;;
        *) return 1 ;;
    esac
}

is_protected_ref() {
    local ref="$1" protected
    ref="${ref##*:}"
    ref="${ref#+}"
    # A refspec ending in a colon leaves the destination empty, which says
    # nothing. Git currently rejects that syntax itself (`fatal: invalid
    # refspec`), so it is not an open bypass; depending on Git validation is
    # fragile, however. Block when in doubt, as everywhere here.
    [ -n "$ref" ] || return 0
    # A fully qualified ref already identifies its type: `refs/tags/x` is not
    # a branch and must not be compared with the branch list.
    case "$ref" in
        refs/tags/*)
            ref="${ref#refs/tags/}"
            for protected in $PROTECTED_TAGS; do
                [ "$ref" = "$protected" ] && return 0
            done
            return 1
            ;;
    esac
    ref="${ref#refs/heads/}"
    # `git push origin HEAD` names no destination: the current branch chooses
    # it, so on a develop checkout it updates develop.
    case "$ref" in
        HEAD|@)
            ref="$(git symbolic-ref --quiet --short HEAD 2>/dev/null)" || return 0
            [ -n "$ref" ] || return 0
            ;;
    esac
    # A bare name does not say whether it is a branch or tag: `git push origin
    # harness-release` moves the tag. Compare against both lists and block when
    # in doubt. The cost is a branch sharing a protected tag's name, which
    # should not exist.
    for protected in $PROTECTED_BRANCHES $PROTECTED_TAGS; do
        [ "$ref" = "$protected" ] && return 0
    done
    return 1
}

# `git push` is not the only way to move a ref. The REST API does it in one
# call—`gh api -X PATCH repos/o/r/git/refs/tags/harness-release`, or the same
# through curl—and `gh release create` creates the tag itself. Against the
# agent on this host, which is the threat model declared in ADR 0023, covering
# only `git push` is not protection: it is a signpost.
#
# It blocks only two forms: an argument that is the *ref path*
# (`.../refs/tags/<tag>`), and the bare tag name inside a `release`
# subcommand. Naming the tag in arbitrary text—a `--body` or `--search`—is
# not enough: the first version blocked those too, and a guard that prevents
# *mentioning* the release tag creates friction in the very workflow it should
# protect.
#
# Declared tradeoff: the two forms do not distinguish reads from writes.
# Chasing `-X`, `--method`, POST defaults for `-f`, and subcommand
# abbreviations would add more room for mistakes than value. Use
# `git ls-remote` to read that ref.
mentions_protected_tag_via_api() {
    local segment="$1" word candidate tag stripped decoded escaped rounds value
    local -a words
    read -r -a words <<< "$segment"
    local saw_client=false
    for word in "${words[@]}"; do
        # Strip quotes before, not after: `${word##*[...]}` on `'curl'`
        # cuts through the final quote and leaves an empty string—the exact
        # opposite of recognizing the command. This is the same unquoting
        # that this function applies to paths; omitting it here made the API
        # branch bypassable by quoting the binary name.
        candidate="${word//\'/}"
        candidate="${candidate//\"/}"
        candidate="${candidate##*[\`\(\{\$]}"
        candidate="${candidate##*/}"
        case "$candidate" in
            gh|curl|wget) saw_client=true; break ;;
        esac
    done
    [ "$saw_client" = true ] || return 1
    # `gh release create <tag>` creates the tag without touching the ref API,
    # so the bare name matters only inside a `release` subcommand.
    local saw_release=false
    for word in "${words[@]}"; do
        case "${word//[\'\"]/}" in
            release) saw_release=true; break ;;
        esac
    done
    for word in "${words[@]}"; do
        # Strip quotes before comparing: `read -r -a` does not interpret
        # them, so `gh api '.../tags/harness-release'` ends with a quote
        # rather than the tag name. This is natural syntax, not an attack—the
        # same reason the `git push` branch normalizes instead of comparing
        # literally.
        stripped="${word//\'/}"
        stripped="${stripped//\"/}"
        # `refs%2Ftags%2Fharness-release` is the same API path but different
        # text for `case`. Decode before comparing: this string is used only
        # for comparison, so approximate decoding is harmless, while failing
        # to decode leaves the most direct bypass open.
        # Decode until the string stops changing: `%252F` becomes `%2F` on
        # the first pass and `/` on the second; stopping after one pass would
        # let double encoding through. Limit the number of passes because
        # this runs on every tool call.
        rounds=0
        while [ "$rounds" -lt 4 ]; do
            case "$stripped" in
                *%[0-9A-Fa-f][0-9A-Fa-f]*) ;;
                *) break ;;
            esac
            escaped="${stripped//\\/\\\\}"
            decoded="$(printf '%b' "${escaped//%/\\x}" 2>/dev/null)"
            [ -n "$decoded" ] || break
            [ "$decoded" = "$stripped" ] && break
            stripped="$decoded"
            rounds=$((rounds + 1))
        done
        # A URL may append a query and fragment to the ref name.
        stripped="${stripped%%\?*}"
        stripped="${stripped%%#*}"
        stripped="${stripped%/}"
        # A ref built at runtime cannot be compared: `.../refs/tags/$T` names
        # a tag known only after expansion. Block it here, as `is_opaque` does
        # after `push`. The filter is narrow—only ref-shaped arguments—so that
        # every `gh` command containing a variable is not blocked.
        case "$stripped" in
            *refs/tags/*)
                case "$stripped" in
                    *[\$\`*?[\{\}]*) return 0 ;;
                esac
                ;;
        esac
        # `gh api ... -f ref=refs/tags/<tag>` and `-f tag_name=<tag>` create
        # the tag by naming it in a field, not as a URL path. Also inspect the
        # value after the first `=`. The `key=value` form keeps prose out:
        # `--body ... <tag> ...` remains allowed because the name is a bare
        # word there.
        value=''
        case "$stripped" in
            *=*) value="${stripped#*=}" ;;
        esac
        # A control character appended to the name (`...harness-release%0A`,
        # decoded above) would defeat an exact comparison. GitHub would reject
        # that ref name, but comparison must not depend on what the server
        # rejects.
        value="${value%%[[:cntrl:]]*}"
        value="${value%"${value##*[![:space:]]}"}"
        for tag in $PROTECTED_TAGS; do
            case "$stripped" in
                # A ref path naming the tag: the actual API route.
                refs/tags/"$tag"|*/refs/tags/"$tag") return 0 ;;
            esac
            case "$value" in
                "$tag"|refs/tags/"$tag"|*/refs/tags/"$tag") return 0 ;;
            esac
            if [ "$saw_release" = true ]; then
                case "$stripped" in
                    "$tag") return 0 ;;
                esac
            fi
        done
    done
    return 1
}

# 0 permette, 2 blocca.
check_segment() {
    local segment="$1" word remote_seen=false
    local -a words refs=()
    local index=0 count
    if mentions_protected_tag_via_api "$segment"; then
        printf 'BLOCKED: the harness release tag cannot be moved through the API. Moving it publishes code to consumer CI; Edo performs that action. See policies/git-workflow.md.\n' >&2
        return 2
    fi
    read -r -a words <<< "$segment"
    count="${#words[@]}"
    # Text after a word beginning with `#` is a shell comment. Parsing it used
    # to block `git push origin feature/x # git push origin develop`, which
    # does not push anything to develop.
    local limit=0
    while [ "$limit" -lt "$count" ]; do
        case "${words[limit]}" in
            \#*) break ;;
        esac
        limit=$((limit + 1))
    done
    count="$limit"

    # The git command may have any prefix (`sudo`, `env`, an assignment): look
    # for the `git` token instead of requiring it first. `git -C <dir> push
    # origin develop` is the normal form when CWD is outside the root, not an
    # attempt to bypass the guard.
    local candidate
    while [ "$index" -lt "$count" ]; do
        # An opening metacharacter or path attached to the command does not
        # make it a different command: `(git`, `$(git`, `` `git `` and
        # `/usr/bin/git` all execute git. Requiring the exact word let them
        # through—the same obfuscation that `is_opaque` blocks after `push`,
        # but which was never reached in command position. The declared cost
        # is some false positives on text containing a path ending in `git`
        # followed by `push`.
        candidate="${words[index]}"
        candidate="${candidate##*[\`\(\{\$\"\']}"
        candidate="${candidate##*/}"
        [ "$candidate" = "git" ] && break
        index=$((index + 1))
    done
    [ "$index" -lt "$count" ] || return 0
    index=$((index + 1))

    # Global options before the subcommand. `-C` and `-c` take a separate
    # value; skipping only one would mistake that value for the subcommand,
    # and push would no longer be recognized.
    while [ "$index" -lt "$count" ]; do
        case "${words[index]}" in
            -C|-c|--git-dir|--work-tree|--namespace|--super-prefix)
                index=$((index + 2)) ;;
            -*) index=$((index + 1)) ;;
            *) break ;;
        esac
    done
    [ "$index" -lt "$count" ] || return 0
    [ "${words[index]}" = "push" ] || return 0
    index=$((index + 1))

    while [ "$index" -lt "$count" ]; do
        word="${words[index]}"
        index=$((index + 1))
        if is_opaque "$word"; then
            printf 'BLOCKED: argomento di git push non interpretabile (%s); in dubbio si blocca.\n' "$word" >&2
            return 2
        fi
        case "$word" in
            # These options publish tags without naming one: the adjacent
            # refspec may be harmless while the release tag moves anyway.
            # They cannot be compared with the protected list, so block them.
            --tags|--mirror|--follow-tags|--prune-tags)
                printf 'BLOCKED: %s publishes unnamed tags, including the release tag. Push an explicit refspec.\n' "$word" >&2
                return 2
                ;;
            -*) continue ;;
        esac
        if [ "$remote_seen" = false ]; then
            remote_seen=true
            continue
        fi
        refs[${#refs[@]}]="$word"
    done

    if [ "$remote_seen" = false ] || [ "${#refs[@]}" -eq 0 ]; then
        printf 'BLOCKED: git push has no explicit refspec: branch configuration chooses the destination, not the command.\n' >&2
        return 2
    fi
    for word in "${refs[@]}"; do
        if is_protected_ref "$word"; then
            printf 'BLOCKED: direct push to a protected ref (%s). For a branch, use a feature branch and a PR. Moving the harness release tag publishes code to consumer CI; Edo performs that action. See policies/git-workflow.md.\n' "$word" >&2
            return 2
        fi
    done
    return 0
}

status=0
while IFS= read -r segment; do
    check_segment "$segment" || status=$?
    [ "$status" -eq 0 ] || exit 2
done <<< "$(printf '%s' "$command_line" | sed -E 's/&&|\|\||[;|&]/\n/g')"
exit 0
