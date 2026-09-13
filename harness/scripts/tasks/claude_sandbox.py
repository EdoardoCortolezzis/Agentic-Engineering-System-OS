#!/usr/bin/env python3
"""Build a fail-closed native Claude Code sandbox invocation.

The old implementation wrapped the CLI in ``sandbox-exec``/``bwrap`` and
granted it a read-only bind of ``/``.  That made the *whole host* readable and
did not cover Claude's built-in Read/Edit tools.  This module instead supplies
a per-invocation settings document to Claude Code itself:

* Bash is isolated by Claude Code's OS sandbox with no network and no
  unsandboxed retry;
* Read is confined to the adopted worktree;
* Edit/Bash writes are reopened only for the worker's exact subtask paths;
* user, project, and local settings, MCP, plugins, slash commands, Chrome,
  and session persistence are disabled by explicit CLI arguments;

The module intentionally contains no fallback wrapper.  If the native CLI
cannot honor the settings, the caller must stop before starting Claude.
"""

from __future__ import annotations

from pathlib import Path
import json
import os
import platform
from typing import Mapping, Sequence


class SandboxError(RuntimeError):
    """The requested native Claude sandbox cannot be constructed safely."""


def managed_sandbox_ready(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether the operator attested the managed native sandbox.

    The local CLI cannot prove that an organization-managed policy is loaded,
    and the installed CLI may predate some documented sandbox settings.  The
    router therefore keeps Claude fallback disabled unless the runner marks
    the host as having deployed and verified that policy.  This is deliberately
    a strict value check; model/request data is never allowed to turn it on.
    """

    env = os.environ if environ is None else environ
    return env.get("AES_CLAUDE_MANAGED_SANDBOX", "").strip() == "1"


def _reject_symlink_chain(value: str | os.PathLike[str], field_name: str) -> None:
    path = Path(value)
    for ancestor in reversed(path.parents):
        if ancestor == ancestor.parent:
            break
        if ancestor.is_symlink():
            raise SandboxError(f"{field_name} may not contain symlinks")
    if path.is_symlink():
        raise SandboxError(f"{field_name} may not be a symlink")


def _validated_directory(value: str | os.PathLike[str], field_name: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise SandboxError(f"{field_name} must be an absolute path")
    _reject_symlink_chain(path, field_name)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise SandboxError(f"{field_name} is unavailable") from error
    if not resolved.is_dir():
        raise SandboxError(f"{field_name} must be a directory")
    return resolved


def _validated_paths(
    paths: Sequence[str | os.PathLike[str]],
    *,
    field_name: str,
    worktree: Path | None = None,
) -> tuple[Path, ...]:
    if isinstance(paths, (str, bytes, bytearray)) or not isinstance(paths, Sequence):
        raise SandboxError(f"{field_name} must be a sequence")
    normalized: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_absolute():
            raise SandboxError(f"{field_name} must contain absolute paths")
        _reject_symlink_chain(path, field_name)
        try:
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise SandboxError(f"{field_name} contains an unavailable path") from error
        if resolved == Path(resolved.anchor):
            raise SandboxError(f"{field_name} may not contain a filesystem root")
        if worktree is not None:
            try:
                resolved.relative_to(worktree)
            except ValueError as error:
                raise SandboxError(
                    f"{field_name} must stay inside the adopted worktree"
                ) from error
        if resolved in normalized:
            raise SandboxError(f"{field_name} contains duplicate paths")
        normalized.append(resolved)
    return tuple(normalized)


def _permission_path(path: Path) -> str:
    """Return Claude's absolute Read/Edit rule spelling for *path*."""

    # Claude's permission grammar uses // for an absolute filesystem path;
    # one slash would be interpreted relative to the project root.
    return "//" + str(path).lstrip("/")


def _runtime_read_paths() -> tuple[str, ...]:
    """Return only OS runtime paths needed by a sandboxed child process.

    ``denyRead: ["/"]`` is deliberate.  A shell still needs its executable,
    loader, device nodes, and standard libraries, so those narrow platform
    runtime paths are the only host paths reopened.  They are not writable and
    do not include home directories or the worktree's parent.
    """

    if platform.system().lower() == "darwin":
        return ("/bin", "/usr", "/System", "/dev")
    if platform.system().lower() == "linux":
        # Never reopen /proc on a shared self-hosted runner.  Without a
        # dedicated PID namespace, same-user process entries can expose
        # command lines and environment variables belonging to the trusted
        # dispatcher or publisher.  The fallback remains fail-closed if a
        # command cannot operate without procfs.
        return ("/bin", "/usr", "/lib", "/lib64", "/sbin", "/etc", "/dev")
    # Native Claude sandboxing is not supported on other platforms.  Keeping
    # this empty makes the settings conservative; failIfUnavailable remains
    # the final startup gate in Claude Code.
    return ()


def _sensitive_worktree_paths(root: Path) -> tuple[str, ...]:
    """Deny common root-level credential files to Bash as well as Read.

    Claude permission rules already reject these names for the built-in Read
    tool.  The OS sandbox needs the corresponding absolute paths because Bash
    otherwise inherits the broad worktree read grant.  Root-level files are
    the relevant AES case: ``feature-start.sh`` may link ``.env`` into a
    worktree.  Existing ``.env.*`` variants are included without relying on
    undocumented glob handling in native sandbox path rules.
    """

    fixed = [root / name for name in (".env", ".git-credentials", ".netrc", ".npmrc")]
    try:
        variants = sorted(
            path for path in root.iterdir() if path.name.startswith(".env.")
        )
    except OSError as error:
        raise SandboxError("sandbox worktree cannot be inspected") from error
    return tuple(str(path) for path in (*fixed, *variants))


def build_settings(
    *,
    worktree: str | os.PathLike[str],
    writable_paths: Sequence[str | os.PathLike[str]] = (),
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Build the complete per-invocation native Claude settings document."""

    root = _validated_directory(worktree, "sandbox worktree")
    writable = _validated_paths(
        writable_paths,
        field_name="sandbox writable paths",
        worktree=root,
    )
    root_permission = _permission_path(root)

    # In addition to the OS-level Bash boundary, use dontAsk + scoped allow
    # rules so built-in Read/Edit cannot prompt and escape the contract in an
    # unattended worker.  A deny rule for sensitive files remains effective
    # even if a managed policy adds a broad allow rule.
    read_allow_rules = [f"Read({root_permission})", f"Read({root_permission}/**)"]
    edit_allow_rules = [
        f"Edit({_permission_path(path)})" for path in writable
    ] + [
        f"Edit({_permission_path(path)}/**)" for path in writable
    ]
    sensitive_permission_rules = [
        "Read(//**/.env)",
        "Read(//**/.env.*)",
        "Read(//**/.git-credentials)",
        "Read(//**/.netrc)",
        "Read(//**/.npmrc)",
        "Edit(//**/.git/**)",
        "Edit(//**/.claude/**)",
        "Bash(git push *)",
        "Bash(gh *)",
    ]

    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {
                # Root denial closes the native sandbox's default host-wide
                # read policy.  The worktree and runtime paths are explicit
                # narrow re-opens; no host root is ever allowRead'd wholesale.
                "denyRead": ["/", "~/", *_sensitive_worktree_paths(root)],
                "allowRead": [str(root), *_runtime_read_paths()],
                "denyWrite": ["/", "~/"],
                "allowWrite": [str(path) for path in writable],
            },
            "network": {"allowedDomains": []},
        },
        "permissions": {
            "defaultMode": "dontAsk",
            "allow": [*read_allow_rules, *edit_allow_rules, "Bash"],
            "deny": [
                *sensitive_permission_rules,
                "Agent",
                "WebFetch",
                "Write",
                "NotebookEdit",
                "Computer",
                "Task",
                "Skill",
                "Glob",
                "Grep",
            ],
        },
        # Prevent project/user configuration from adding plugins, hooks, or
        # permissions when this document is passed via --settings.
        "enabledPlugins": {},
    }


def write_settings(
    path: str | os.PathLike[str],
    *,
    worktree: str | os.PathLike[str],
    writable_paths: Sequence[str | os.PathLike[str]] = (),
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Atomically write a private settings file for one Claude invocation."""

    target = Path(path)
    if not target.is_absolute():
        raise SandboxError("sandbox settings path must be absolute")
    _reject_symlink_chain(target, "sandbox settings path")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(
        build_settings(
            worktree=worktree,
            writable_paths=writable_paths,
            environ=environ,
        ),
        sort_keys=True,
        separators=(",", ":"),
    )
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return target


def build_argv(
    command: Sequence[str],
    *,
    settings_path: str | os.PathLike[str],
) -> tuple[str, ...]:
    """Add native Claude Code isolation flags without wrapping the process."""

    if not command or any(not isinstance(part, str) or not part for part in command):
        raise SandboxError("Claude command cannot be empty")
    settings = Path(settings_path)
    if not settings.is_absolute():
        raise SandboxError("Claude settings path must be absolute")
    _reject_symlink_chain(settings, "Claude settings path")
    if not settings.is_file():
        raise SandboxError("Claude settings file is unavailable")
    return tuple(command) + (
        "--settings",
        str(settings),
        # An empty list excludes all filesystem settings scopes.  Managed
        # settings remain enforced by Claude Code and cannot be weakened here.
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--disable-slash-commands",
        "--no-chrome",
        "--no-session-persistence",
        "--permission-mode",
        "dontAsk",
        "--tools",
        "Bash,Read,Edit",
    )


__all__ = [
    "SandboxError",
    "build_argv",
    "build_settings",
    "managed_sandbox_ready",
    "write_settings",
]
