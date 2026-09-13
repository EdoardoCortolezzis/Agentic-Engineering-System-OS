#!/usr/bin/env bash

set -uo pipefail

# Resolve configuration from this script's location so the command can be
# invoked from the consumer root or another directory.
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$BASH_SOURCE")" 2>/dev/null && pwd -P)"
CONFIG_FILE="$SCRIPT_DIR/../config/harness.conf"

if [ ! -r "$CONFIG_FILE" ]; then
    printf 'Error: configuration is unreadable: %s\n' "$CONFIG_FILE" >&2
    exit 2
fi

# shellcheck source=/dev/null
source "$CONFIG_FILE"

check_mode=0
check_upstream_mode=0
source_argument=''

usage() {
    printf 'Usage: %s [--check] [--check-upstream] [--source <AES-repo-path>]\n' "$BASH_SOURCE" >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --check)
            check_mode=1
            shift
            ;;
        --check-upstream)
            check_upstream_mode=1
            shift
            ;;
        --source)
            if [ "$#" -lt 2 ] || [ -z "$2" ]; then
                printf 'Error: --source requires the AES repository path.\n' >&2
                usage
                exit 2
            fi
            source_argument="$2"
            shift 2
            ;;
        *)
            usage
            exit 2
            ;;
    esac
done

consumer_root="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$consumer_root" ]; then
    printf 'Error: cannot determine the consumer repository root.\n' >&2
    exit 1
fi
consumer_root="$(CDPATH= cd -- "$consumer_root" 2>/dev/null && pwd -P)"
if [ -z "$consumer_root" ]; then
    printf 'Error: cannot resolve the consumer repository root.\n' >&2
    exit 1
fi

version_path="$consumer_root/.harness-version"
if [ "$check_upstream_mode" -eq 1 ] && [ "$check_mode" -eq 0 ] && [ ! -f "$version_path" ]; then
    exit 0
fi

if [ -n "$source_argument" ]; then
    if [ -d "$source_argument" ]; then
        source_root="$(CDPATH= cd -- "$source_argument" 2>/dev/null && pwd -P)"
    else
        source_root=''
    fi
else
    source_root="$(CDPATH= cd -- "$consumer_root/../AES" 2>/dev/null && pwd -P)"
    if [ -z "$source_root" ] || [ ! -f "$source_root/harness/manifest.txt" ]; then
        # Also support a checkout in a directory with the extended name.
        source_root="$(CDPATH= cd -- "$consumer_root/../Agentic-Engineering-System" 2>/dev/null && pwd -P)"
    fi
fi

source_display="$source_argument"
if [ -z "$source_display" ]; then
    source_display="$consumer_root/../AES"
fi
if [ -z "$source_root" ] || [ ! -d "$source_root" ]; then
    printf 'Error: AES source repository not found: %s\n' "$source_display" >&2
    exit 1
fi

manifest="$source_root/harness/manifest.txt"
if [ ! -r "$manifest" ]; then
    printf 'Error: manifest is unreadable: %s\n' "$manifest" >&2
    exit 1
fi

if ! command -v realpath >/dev/null 2>&1; then
    printf 'Error: realpath is required to validate manifest boundaries.\n' >&2
    exit 1
fi

source_root_real="$(realpath "$source_root")" || {
    printf 'Error: AES root cannot be resolved: %s\n' "$source_root" >&2
    exit 1
}
consumer_root_real="$(realpath "$consumer_root")" || {
    printf 'Error: consumer root cannot be resolved: %s\n' "$consumer_root" >&2
    exit 1
}

validate_relative_manifest_path() {
    local value="$1" label="$2" component
    case "$value" in
        ''|/*|*'//'*)
            printf 'Error: manifest %s is not a normalized relative path: %s\n' "$label" "$value" >&2
            return 1
            ;;
    esac
    local old_ifs="$IFS"
    IFS='/'
    read -r -a components <<< "$value"
    IFS="$old_ifs"
    for component in "${components[@]}"; do
        component_lower="$(printf '%s' "$component" | tr '[:upper:]' '[:lower:]')"
        case "$component_lower" in
            ''|.|..|.git)
                printf 'Error: manifest %s contains a forbidden component: %s\n' "$label" "$value" >&2
                return 1
                ;;
        esac
    done
}

path_inside_root() {
    local candidate="$1" root="$2" resolved
    resolved="$(realpath "$candidate" 2>/dev/null)" || return 1
    case "$resolved/" in
        "$root/"*) return 0 ;;
        *) return 1 ;;
    esac
}

validate_manifest_destination() {
    local destination="$1" existing parent
    existing="$destination"
    while [ ! -e "$existing" ] && [ ! -L "$existing" ]; do
        parent="$(dirname -- "$existing")"
        [ "$parent" != "$existing" ] || return 1
        existing="$parent"
    done
    path_inside_root "$existing" "$consumer_root_real"
}

path_inside_root "$manifest" "$source_root_real" || {
    printf 'Error: manifest escapes the AES root through a symlink.\n' >&2
    exit 1
}

# Validate every manifest path before any copy or directory creation. The
# lexical check blocks traversal and Git metadata; realpath checks also block
# symlink escapes through an existing source or destination component.
manifest_line=0
while IFS= read -r manifest_entry || [ -n "$manifest_entry" ]; do
    manifest_line=$((manifest_line + 1))
    trimmed="$(printf '%s\n' "$manifest_entry" | sed 's/^[[:space:]]*//')"
    case "$trimmed" in
        ''|\#*) continue ;;
    esac
    read -r _manifest_mode manifest_source manifest_destination _manifest_keys _manifest_extra <<< "$trimmed"
    if [ -z "${_manifest_mode:-}" ] || [ -z "${manifest_source:-}" ] || [ -z "${manifest_destination:-}" ]; then
        printf 'Error: invalid manifest line %s.\n' "$manifest_line" >&2
        exit 1
    fi
    validate_relative_manifest_path "$manifest_source" "source on line $manifest_line" || exit 1
    validate_relative_manifest_path "$manifest_destination" "destination on line $manifest_line" || exit 1
    manifest_source_path="$source_root/$manifest_source"
    manifest_destination_path="$consumer_root/$manifest_destination"
    if [ -e "$manifest_source_path" ] || [ -L "$manifest_source_path" ]; then
        path_inside_root "$manifest_source_path" "$source_root_real" || {
            printf 'Error: source on line %s escapes the AES root through a symlink.\n' "$manifest_line" >&2
            exit 1
        }
    fi
    validate_manifest_destination "$manifest_destination_path" || {
        printf 'Error: destination on line %s escapes the consumer root through a symlink.\n' "$manifest_line" >&2
        exit 1
    }
done < "$manifest"

if ! command -v python3 >/dev/null 2>&1; then
    printf 'Error: python3 is required for JSON synchronization.\n' >&2
    exit 1
fi

source_commit="$(git -C "$source_root" rev-parse HEAD 2>/dev/null)"
if [ -z "$source_commit" ]; then
    printf 'Error: cannot determine the AES repository HEAD commit: %s\n' "$source_root" >&2
    exit 1
fi

upstream_status=0
if [ "$check_upstream_mode" -eq 1 ] && [ -f "$version_path" ]; then
    if ! registered_commit="$(python3 - "$version_path" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as version_file:
    source_commit = json.load(version_file).get("source_commit")
if not isinstance(source_commit, str) or not source_commit:
    raise SystemExit(1)
print(source_commit)
PY
    )"; then
        printf 'Error: source_commit is missing or invalid in .harness-version.\n' >&2
        upstream_status=1
    elif ! git -C "$source_root" cat-file -e "${registered_commit}^{commit}" 2>/dev/null; then
        printf 'Error: registered AES commit %s does not exist in the local source repository; fetch the AES repository and retry.\n' "$registered_commit" >&2
        upstream_status=1
    elif [ "$registered_commit" != "$source_commit" ]; then
        if git -C "$source_root" merge-base --is-ancestor "$registered_commit" "$source_commit" 2>/dev/null; then
            behind_count="$(git -C "$source_root" rev-list --count "${registered_commit}..${source_commit}" 2>/dev/null)"
            if [ -z "$behind_count" ]; then
                printf 'Error: cannot calculate how far behind the AES repository is.\n' >&2
                upstream_status=1
            else
                printf 'Harness is %s commits behind AES. Update with: %s/harness/scripts/aes-sync.sh --source %s\n' \
                    "$behind_count" "$source_root" "$source_root" >&2
                upstream_status=1
            fi
        else
            printf 'Error: registered AES commit %s is not an ancestor of source HEAD %s.\n' \
                "$registered_commit" "$source_commit" >&2
            upstream_status=1
        fi
    fi
fi

if [ "$check_upstream_mode" -eq 1 ] && [ "$check_mode" -eq 0 ]; then
    exit "$upstream_status"
fi

# `cp -p` writes into the existing destination inode. When the destination is
# the currently running script (aes-sync.sh bumping itself), bash keeps reading
# that inode while its content changes: the instruction stream becomes
# misaligned and produces a mid-token syntax error. This was reproduced during
# a real propagation. Writing elsewhere and then renaming is atomic on POSIX
# and leaves the old inode intact while a process holds it open, so the running
# process reads one coherent version to completion.
# If the process is interrupted between `mktemp` and `mv` (kill or crash—the
# scenario that exposed the defect), the temporary file would otherwise remain
# untracked, invisible to `--check`, and accumulate. One variable is enough
# because atomic_copy calls are sequential here, never concurrent.
_atomic_copy_pending_temp=""
trap '[ -n "$_atomic_copy_pending_temp" ] && rm -f "$_atomic_copy_pending_temp"' EXIT

atomic_copy() {
    local source="$1" destination="$2"
    _atomic_copy_pending_temp="$(mktemp "${destination}.tmp.XXXXXX")" || return 1
    if ! cp -p "$source" "$_atomic_copy_pending_temp"; then
        rm -f "$_atomic_copy_pending_temp"
        _atomic_copy_pending_temp=""
        return 1
    fi
    if ! mv -f "$_atomic_copy_pending_temp" "$destination"; then
        rm -f "$_atomic_copy_pending_temp"
        _atomic_copy_pending_temp=""
        return 1
    fi
    _atomic_copy_pending_temp=""
}

# Declared divergences are read before applying the manifest: a file listed
# here belongs to the consumer, and that must remain true when synchronization
# runs. While `--check` exempted it from drift but copying still overwrote it,
# declaring an override preserved nothing; the first bump deleted it.
declared_overrides=''
if [ -r "$consumer_root/.harness-overrides" ]; then
    # Inline comments and a leading `./` are natural syntax. A non-matching
    # override fails in the worst way: silently, while overwriting the file it
    # was meant to protect.
    declared_overrides="$(sed -e 's/[[:space:]]*#.*$//' -e 's/^[[:space:]]*//' \
        -e 's/[[:space:]]*$//' -e 's|^\./||' \
        "$consumer_root/.harness-overrides" | grep -v '^$')"
fi

is_declared_override() {
    [ -n "$declared_overrides" ] || return 1
    printf '%s\n' "$declared_overrides" | grep -Fxq -- "$1"
}

# In --check, perform no copy or creation operation.
if [ "$check_mode" -eq 0 ]; then
    manifest_line=0
    while IFS= read -r manifest_entry || [ -n "$manifest_entry" ]; do
        manifest_line=$((manifest_line + 1))
        trimmed="$(printf '%s\n' "$manifest_entry" | sed 's/^[[:space:]]*//')"
        case "$trimmed" in
            ''|\#*) continue ;;
        esac

        mode=''
        source_relative=''
        destination_relative=''
        managed_keys=''
        extra=''
        read -r mode source_relative destination_relative managed_keys extra <<< "$trimmed"

        if [ -z "$mode" ] || [ -z "$source_relative" ] || [ -z "$destination_relative" ]; then
            printf 'Error: invalid manifest line %s.\n' "$manifest_line" >&2
            exit 1
        fi

        source_path="$source_root/$source_relative"
        destination_path="$consumer_root/$destination_relative"

        case "$mode" in
            copy)
                if [ -n "$managed_keys" ] || [ -n "$extra" ]; then
                    printf 'Error: unexpected columns on manifest line %s.\n' "$manifest_line" >&2
                    exit 1
                fi
                if [ ! -f "$source_path" ]; then
                    printf 'Error: source not found on manifest line %s: %s\n' "$manifest_line" "$source_path" >&2
                    exit 1
                fi
                if is_declared_override "$destination_relative"; then
                    # Report it: a managed file that is not updated must be
                    # visible, or an override silently becomes a way to lag.
                    printf 'Declared override, not overwritten: %s\n' "$destination_relative"
                    continue
                fi
                if ! mkdir -p "$(dirname -- "$destination_path")"; then
                    printf 'Error: cannot create directory for: %s\n' "$destination_path" >&2
                    exit 1
                fi
                if ! atomic_copy "$source_path" "$destination_path"; then
                    printf 'Error: cannot copy %s to %s\n' "$source_path" "$destination_path" >&2
                    exit 1
                fi
                ;;
            seed)
                if [ -n "$managed_keys" ] || [ -n "$extra" ]; then
                    printf 'Error: unexpected columns on manifest line %s.\n' "$manifest_line" >&2
                    exit 1
                fi
                if [ ! -e "$destination_path" ]; then
                    if [ ! -f "$source_path" ]; then
                        printf 'Error: source not found on manifest line %s: %s\n' "$manifest_line" "$source_path" >&2
                        exit 1
                    fi
                    if ! mkdir -p "$(dirname -- "$destination_path")"; then
                        printf 'Error: cannot create directory for: %s\n' "$destination_path" >&2
                        exit 1
                    fi
                    if ! atomic_copy "$source_path" "$destination_path"; then
                        printf 'Error: cannot create seed: %s\n' "$destination_path" >&2
                        exit 1
                    fi
                fi
                ;;
            merge-json)
                if [ -z "$managed_keys" ] || [ -n "$extra" ]; then
                    printf 'Error: invalid JSON keys on manifest line %s.\n' "$manifest_line" >&2
                    exit 1
                fi
                if [ ! -f "$source_path" ]; then
                    printf 'Error: JSON source not found on manifest line %s: %s\n' "$manifest_line" "$source_path" >&2
                    exit 1
                fi
                ;;
            *)
                printf 'Error: unrecognized mode on manifest line %s: %s\n' "$manifest_line" "$mode" >&2
                exit 1
                ;;
        esac
    done < "$manifest"
fi

operation='apply'
if [ "$check_mode" -eq 1 ]; then
    operation='check'
fi

python3 - "$operation" "$consumer_root" "$source_root" "$manifest" "$source_commit" <<'PY'
import copy
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


operation = sys.argv[1]
consumer_root = Path(sys.argv[2])
source_root = Path(sys.argv[3])
manifest_path = Path(sys.argv[4])
source_commit = sys.argv[5]
version_path = consumer_root / ".harness-version"
overrides_path = consumer_root / ".harness-overrides"


def parse_manifest(path):
    entries = {}
    with path.open(encoding="utf-8") as manifest_file:
        for line_number, raw_line in enumerate(manifest_file, 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            columns = line.split()
            if len(columns) not in (3, 4):
                raise ValueError(f"invalid manifest line {line_number}")
            mode, source, destination = columns[:3]
            keys = columns[3] if len(columns) == 4 else ""
            if mode not in {"copy", "seed", "merge-json"}:
                raise ValueError(f"unrecognized mode on manifest line {line_number}: {mode}")
            if mode == "merge-json" and not keys:
                raise ValueError(f"missing JSON keys on manifest line {line_number}")
            if mode != "merge-json" and keys:
                raise ValueError(f"unexpected keys on manifest line {line_number}")
            entries[destination] = (mode, source, keys)
    return entries


def get_path(value, dotted_path):
    for part in dotted_path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(dotted_path)
        value = value[part]
    return value


def set_path(value, dotted_path, leaf):
    parts = dotted_path.split(".")
    target = value
    for part in parts[:-1]:
        child = target.get(part)
        if child is None:
            child = {}
            target[part] = child
        elif not isinstance(child, dict):
            raise ValueError(f"intermediate level {part!r} is not an object")
        target = child
    target[parts[-1]] = leaf


DIGEST_LENGTH = 16
# .harness-version detects accidental drift in a fixed list of known assets,
# not an adversary: 64 bits of a SHA-256 digest are more than sufficient. A
# full digest (64 hex characters) is indistinguishable from a private key to
# the secret scanners used by most CI/pre-commit setups (including the one
# propagating this harness), and every harness bump changes this file. A
# truncated digest avoids triggering that check on a file containing no secret.


def _truncate(hex_digest):
    """Apply the common truncation to every .harness-version digest."""
    return hex_digest[:DIGEST_LENGTH]


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as binary_file:
        for block in iter(lambda: binary_file.read(1024 * 1024), b""):
            digest.update(block)
    return _truncate(digest.hexdigest())


def copy_metadata(path):
    """Record content and executable bit for copied assets."""
    return {
        "sha256": file_hash(path),
        "executable": bool(path.stat().st_mode & 0o111),
    }


def managed_hash(path, keys):
    with path.open(encoding="utf-8") as json_file:
        value = json.load(json_file)
    managed = {}
    for dotted_path in keys.split(","):
        if dotted_path:
            set_path(managed, dotted_path, get_path(value, dotted_path))
    canonical = json.dumps(
        managed, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _truncate(hashlib.sha256(canonical).hexdigest())


def merge_json(source_path, destination_path, keys):
    with source_path.open(encoding="utf-8") as source_file:
        source = json.load(source_file)

    if destination_path.exists():
        with destination_path.open(encoding="utf-8") as destination_file:
            destination = json.load(destination_file)
        if not isinstance(destination, dict):
            raise ValueError("destination JSON must contain an object")
        before = copy.deepcopy(destination)
    else:
        destination = {}
        before = None

    for dotted_path in keys.split(","):
        source_value = copy.deepcopy(get_path(source, dotted_path))
        parts = dotted_path.split(".")
        target = destination
        for part in parts[:-1]:
            current = target.get(part)
            if current is None:
                current = {}
                target[part] = current
            elif not isinstance(current, dict):
                raise ValueError(
                    f"intermediate level {part!r} is not an object"
                )
            target = current
        target[parts[-1]] = source_value

    if before != destination:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_text(
            json.dumps(destination, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


try:
    entries = parse_manifest(manifest_path)

    overrides = set()
    if overrides_path.is_file():
        with overrides_path.open(encoding="utf-8") as overrides_file:
            for raw_line in overrides_file:
                line = raw_line.split("#", 1)[0].strip()
                if line.startswith("./"):
                    line = line[2:]
                if line:
                    overrides.add(line)

    # An entry matching no manifest destination protects nothing, and failing
    # silently would be worst: the next sync would overwrite the file it was
    # meant to preserve. Warn and continue; blocking a consumer for a typo in
    # a declaration file would be disproportionate.
    for declared in sorted(overrides - set(entries)):
        print(
            f"Warning: .harness-overrides declares {declared}, which is not "
            "a manifest destination: it protects nothing.",
            file=sys.stderr,
        )

    if operation == "check":
        if not version_path.is_file():
            print("Error: .harness-version not found.", file=sys.stderr)
            raise SystemExit(1)
        with version_path.open(encoding="utf-8") as version_file:
            version = json.load(version_file)
        registered_files = version.get("files", {})
        if not isinstance(registered_files, dict):
            raise ValueError(".harness-version files key must be an object")

        # An older .harness-version format recorded a string for copy assets,
        # or a full SHA-256 digest (64 characters, before truncation). Report
        # this immediately and alone: continuing drift checks would suggest an
        # override for a format that must simply be regenerated.
        def _is_obsolete_copy_entry(expected):
            if isinstance(expected, str):
                return True
            sha256 = expected.get("sha256") if isinstance(expected, dict) else None
            return isinstance(sha256, str) and len(sha256) != DIGEST_LENGTH

        obsolete = sorted(
            destination
            for destination, expected in registered_files.items()
            if entries.get(str(destination), ("", "", ""))[0] == "copy"
            and _is_obsolete_copy_entry(expected)
        )
        if obsolete:
            print(
                "Obsolete .harness-version format. Resynchronize with "
                f"aes-sync (affected entries: {', '.join(obsolete)}).",
                file=sys.stderr,
            )
            raise SystemExit(3)

        # Overrides announce themselves: after the first sync that respects
        # them, no registered entry remains from which to infer them, and a
        # managed file AES does not update must remain visible.
        for declared in sorted(overrides):
            if declared in entries:
                print(f"Override dichiarato: {declared}")

        drift = []
        for destination, expected_hash in registered_files.items():
            relative_path = str(destination)
            path = consumer_root / relative_path
            entry = entries.get(relative_path)
            actual_hash = None
            error_detail = ""
            try:
                if not path.is_file():
                    raise OSError("file not found")
                if entry is None:
                    raise ValueError("voce assente dal manifest")
                mode, _source, keys = entry
                if mode == "merge-json":
                    # merge-json records only managed content; the executable
                    # bit is not meaningful for a JSON file.
                    actual_hash = managed_hash(path, keys)
                elif mode == "copy":
                    if (
                        not isinstance(expected_hash, dict)
                        or not isinstance(expected_hash.get("sha256"), str)
                        or not isinstance(expected_hash.get("executable"), bool)
                    ):
                        raise ValueError("invalid copy metadata")
                    actual_hash = copy_metadata(path)
                    if (
                        actual_hash["sha256"] == expected_hash["sha256"]
                        and actual_hash["executable"] != expected_hash["executable"]
                    ):
                        # Without this detail, "Drift" suggests a content
                        # difference that does not exist.
                        error_detail = (
                            "executable bit changed; content is identical"
                        )
                else:
                    raise ValueError("mode cannot be hashed")
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                actual_hash = None
                error_detail = str(error)

            if relative_path in overrides:
                # Already reported above; only its not being drift matters here.
                pass
            elif actual_hash != expected_hash:
                suffix = f" ({error_detail})" if error_detail else ""
                print(f"Drift: {relative_path}{suffix}")
                drift.append(relative_path)

        raise SystemExit(1 if drift else 0)

    if operation != "apply":
        raise ValueError(f"unrecognized operation: {operation}")

    files = {}
    for destination, (mode, source, keys) in entries.items():
        destination_path = consumer_root / destination
        # A file declared in .harness-overrides is not managed by AES: it is
        # neither written nor registered. Registering its hash would claim AES
        # knows its expected content, which is exactly what the override
        # denies. This applies to the file, not the mode: managed merge-json
        # keys also belong to the consumer, and an override may omit them.
        if destination in overrides:
            continue
        if mode == "copy":
            files[destination] = copy_metadata(destination_path)
        elif mode == "merge-json":
            merge_json(source_root / source, destination_path, keys)
            files[destination] = managed_hash(destination_path, keys)
        # seed intentionally has no entry in .harness-version.

    new_version = {
        "source_commit": source_commit,
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "files": dict(sorted(files.items())),
    }

    old_version = None
    if version_path.is_file():
        try:
            with version_path.open(encoding="utf-8") as version_file:
                old_version = json.load(version_file)
        except (OSError, json.JSONDecodeError):
            pass

    # Preserve the version file when synchronization produces no changes.
    if (
        isinstance(old_version, dict)
        and old_version.get("source_commit") == new_version["source_commit"]
        and old_version.get("files") == new_version["files"]
        and isinstance(old_version.get("synced_at"), str)
    ):
        raise SystemExit(0)

    version_path.write_text(
        json.dumps(new_version, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
    print(f"Error: AES synchronization failed: {error}", file=sys.stderr)
    raise SystemExit(1)
PY
python_status=$?
if [ "$python_status" -ne 0 ]; then
    # 3 means obsolete .harness-version format: the Python block already said
    # what to do, and adding override advice would suggest a divergence that
    # does not exist.
    if [ "$python_status" -ne 3 ]; then
        # In --check nothing was synchronized: the message must state what
        # actually happened, or it will mislead the reader.
        if [ "$check_mode" -eq 1 ]; then
            printf 'Undeclared drift detected: sync with aes-sync or declare the divergence in .harness-overrides.\n' >&2
        else
            printf 'Error: cannot complete AES synchronization.\n' >&2
        fi
    fi
    exit 1
fi

if [ "$check_mode" -eq 1 ]; then
    # --check compares with hashes registered in .harness-version: it verifies
    # that nothing was tampered with locally, NOT that the consumer is aligned
    # with AES's latest commit. Say so to avoid the conclusion "green means
    # up to date".
    printf 'No undeclared drift relative to %s. This does not check whether AES is ahead: use aes-sync to update.\n' "$source_commit"
    exit "$upstream_status"
else
    printf 'AES synchronization completed: %s\n' "$source_commit"
fi
exit 0
