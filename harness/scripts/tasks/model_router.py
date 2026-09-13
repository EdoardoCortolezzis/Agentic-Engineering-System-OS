"""Provider-neutral model routing for the autonomous issue queue.

This module deliberately owns only the *invocation seam*.  Queue state,
GitHub publication, and quota accounting stay in their respective modules.
The router accepts an availability callback instead of importing ``quota`` so
that quota policy can evolve without coupling the worker to one provider
implementation.

The public API is safe to use from a worker process:

* prompts are supplied as stdin to the provider command, never as argv;
* the child environment is copied and GitHub credentials are removed;
* Claude fallback is disabled unless the runner attests a verified managed
  native sandbox; when enabled, Claude receives a per-invocation settings
  file with no network and only explicitly declared path writes;
* ``subprocess.run`` is replaced by a small callable in tests;
* returned metadata never contains stdout, stderr, argv, or environment.

The command line interface follows the same contract.  It reads a prompt from
``--prompt-file`` (or stdin when the value is ``-``), writes model output to a
file only when requested, and emits redacted JSON metadata on stdout.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
import json
import math
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Protocol, Sequence, TextIO

from orchestration import OrchestrationError, parse_plan, parse_verdict, validate_execution_id
from claude_sandbox import (
    SandboxError,
    build_argv as build_sandbox_argv,
    managed_sandbox_ready,
    write_settings as write_claude_settings,
)


ProviderName = str
RoleName = str
Effort = str

CONTROLLER = "controller"
WORKER = "worker"
OPENAI = "openai"
ANTHROPIC = "anthropic"
QUOTA_PROVIDER = {OPENAI: "codex", ANTHROPIC: "claude"}

DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_MAX_PROMPT_CHARS = 1_000_000
DEFAULT_MAX_OUTPUT_CHARS = 2_000_000

CODEX_CREDENTIAL_ATTESTATION = "AES_CODEX_MANAGED_CREDENTIALS"
CODEX_CREDENTIAL_ATTESTED_VALUE = "1"
CODEX_CREDENTIAL_STORE = "keyring"
CODEX_AUTH_FILENAME = "auth.json"

HIGH_EFFORTS = frozenset({"high", "xhigh"})
ALLOWED_FALLBACK_FAILURES = frozenset({"quota", "policy", "transient"})
# A failed Codex quota refresh is retryable, but it is not evidence of
# exhausted quota and must never authorize either a stale Codex attempt or a
# Claude fallback.
REFRESH_FAILED = "refresh_failed"
# A provider policy reserve (for example Claude's work-hours reserve) is a
# temporary dispatch stop, just like an exhausted quota or transient outage.
# Authentication, configuration, and output failures remain human actions.
WAITABLE_FAILURES = frozenset({"quota", "policy", "transient", REFRESH_FAILED})
FAILURE_CLASSES = frozenset(
    {
        "quota",
        "policy",
        "transient",
        "auth",
        "config",
        "invalid_output",
        "unknown",
        REFRESH_FAILED,
    }
)
_AVAILABILITY_ALIASES = {
    "quota_exhausted": "quota",
    "policy_blocked": "policy",
    "auth_error": "auth",
    "authentication": "auth",
    "configuration": "config",
    "unknown": "config",
}


class RouterConfigError(ValueError):
    """The router cannot construct a safe invocation from its configuration."""


class CommandRunner(Protocol):
    """Small injectable process seam used by :class:`ModelRouter`."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: str,
        env: Mapping[str, str],
        cwd: str | None,
        timeout: float | None,
    ) -> "ProcessResult":
        ...


class AvailabilityCallback(Protocol):
    """Quota/policy adapter seam; no import of a concrete quota module."""

    def __call__(self, provider: str, role: str) -> "Availability | str | bool":
        ...


@dataclass(frozen=True)
class ProcessResult:
    """Provider process data needed for routing.

    ``stderr`` is consumed only for classification and is never copied into a
    router result or emitted by the CLI.
    """

    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class Availability:
    """Availability result returned by a quota/policy adapter.

    ``status=available`` permits an invocation.  Other values are treated as
    explicit provider classifications; only quota, policy, and transient
    permit moving to the fallback provider.
    """

    available: bool
    status: str = "available"
    detail: str | None = None

    def __post_init__(self) -> None:
        normalized = self.status.strip().lower()
        normalized = _AVAILABILITY_ALIASES.get(normalized, normalized)
        if self.available:
            normalized = "available"
        if normalized not in FAILURE_CLASSES | {"available"}:
            raise RouterConfigError(f"unknown availability status: {self.status!r}")
        object.__setattr__(self, "status", normalized)


@dataclass(frozen=True)
class RoutingPolicy:
    """Models, effort mapping, commands, and bounded retry settings."""

    controller_openai_model: str = "gpt-5.6-sol"
    controller_anthropic_model: str = "claude-opus-5"
    worker_openai_model: str = "gpt-5.6-luna"
    worker_anthropic_model: str = "claude-sonnet-5"
    controller_effort: str = "medium"
    anthropic_controller_effort: str = "medium"
    anthropic_worker_high_effort: str = "high"
    anthropic_worker_xhigh_effort: str = "high"
    codex_command: tuple[str, ...] = ("codex",)
    claude_command: tuple[str, ...] = ("claude",)
    transient_retries: int = 2
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS

    def __post_init__(self) -> None:
        for name in (
            "controller_openai_model",
            "controller_anthropic_model",
            "worker_openai_model",
            "worker_anthropic_model",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise RouterConfigError(f"{name} cannot be empty")
        if self.transient_retries < 0 or self.transient_retries > 5:
            raise RouterConfigError("transient_retries must be between 0 and 5")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
        ):
            raise RouterConfigError("timeout_seconds must be a finite positive number")
        for name in ("max_prompt_chars", "max_output_chars"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise RouterConfigError(f"{name} must be a positive integer")
        if not self.codex_command or not self.claude_command:
            raise RouterConfigError("provider command cannot be empty")
        for effort_name in (
            "controller_effort",
            "anthropic_controller_effort",
            "anthropic_worker_high_effort",
            "anthropic_worker_xhigh_effort",
        ):
            value = getattr(self, effort_name)
            if not isinstance(value, str) or not value.strip():
                raise RouterConfigError(f"{effort_name} cannot be empty")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "RoutingPolicy":
        """Load operational knobs without sourcing a dotenv file or logging it."""

        env = os.environ if environ is None else environ

        def text(name: str, default: str) -> str:
            value = env.get(name, default).strip()
            if not value:
                raise RouterConfigError(f"{name} cannot be empty")
            return value

        def command(name: str, default: str) -> tuple[str, ...]:
            try:
                result = tuple(shlex.split(env.get(name, default)))
            except ValueError as error:
                raise RouterConfigError(f"{name} is not valid shell-like argv") from error
            if not result:
                raise RouterConfigError(f"{name} cannot be empty")
            return result

        raw_retries = env.get("AES_MODEL_TRANSIENT_RETRIES", "2").strip()
        try:
            retries = int(raw_retries)
        except ValueError as error:
            raise RouterConfigError("AES_MODEL_TRANSIENT_RETRIES must be an integer") from error
        raw_timeout = env.get("AES_MODEL_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)).strip()
        if raw_timeout:
            try:
                timeout = float(raw_timeout)
            except ValueError as error:
                raise RouterConfigError("AES_MODEL_TIMEOUT_SECONDS must be numeric") from error
        else:
            raise RouterConfigError("AES_MODEL_TIMEOUT_SECONDS cannot be empty")

        def positive_int(name: str, default: int) -> int:
            raw = env.get(name, str(default)).strip()
            try:
                value = int(raw)
            except ValueError as error:
                raise RouterConfigError(f"{name} must be an integer") from error
            if value <= 0:
                raise RouterConfigError(f"{name} must be positive")
            return value

        return cls(
            controller_openai_model=text(
                "AES_CONTROLLER_OPENAI_MODEL", cls.controller_openai_model
            ),
            controller_anthropic_model=text(
                "AES_CONTROLLER_ANTHROPIC_MODEL", cls.controller_anthropic_model
            ),
            worker_openai_model=text("AES_WORKER_OPENAI_MODEL", cls.worker_openai_model),
            worker_anthropic_model=text("AES_WORKER_ANTHROPIC_MODEL", cls.worker_anthropic_model),
            controller_effort=text("AES_CONTROLLER_OPENAI_EFFORT", cls.controller_effort),
            anthropic_controller_effort=text(
                "AES_CONTROLLER_ANTHROPIC_EFFORT", cls.anthropic_controller_effort
            ),
            anthropic_worker_high_effort=text(
                "AES_WORKER_ANTHROPIC_EFFORT_HIGH", cls.anthropic_worker_high_effort
            ),
            anthropic_worker_xhigh_effort=text(
                "AES_WORKER_ANTHROPIC_EFFORT_XHIGH", cls.anthropic_worker_xhigh_effort
            ),
            codex_command=command("AES_CODEX_COMMAND", "codex"),
            claude_command=command("AES_CLAUDE_COMMAND", "claude"),
            transient_retries=retries,
            timeout_seconds=timeout,
            max_prompt_chars=positive_int(
                "AES_MODEL_MAX_PROMPT_CHARS", DEFAULT_MAX_PROMPT_CHARS
            ),
            max_output_chars=positive_int(
                "AES_MODEL_MAX_OUTPUT_CHARS", DEFAULT_MAX_OUTPUT_CHARS
            ),
        )


def _path_overlaps(left: Path, right: Path) -> bool:
    """Return whether either path contains the other."""

    return left == right or left in right.parents or right in left.parents


def _codex_home(environ: Mapping[str, str] | None, *, cwd: str | None = None) -> Path:
    """Validate the runner's non-file Codex credential boundary.

    Codex itself must read the OS keyring, but the model-generated shell must
    not receive a file-backed ``auth.json``.  ``workspace-write`` does not
    make a host directory unreadable merely because it is not writable, so the
    queue requires an operator-attested, dedicated directory and rejects any
    file or symlink named ``auth.json`` before starting the provider.
    """

    source = dict(os.environ if environ is None else environ)

    def env_text(name: str) -> str:
        value = source.get(name, "")
        if not isinstance(value, str):
            raise RouterConfigError(f"{name} must be text")
        return value.strip()

    if env_text(CODEX_CREDENTIAL_ATTESTATION) != CODEX_CREDENTIAL_ATTESTED_VALUE:
        raise RouterConfigError(
            f"{CODEX_CREDENTIAL_ATTESTATION}=1 is required for autonomous Codex"
        )

    raw_home = env_text("CODEX_HOME")
    if not raw_home:
        raise RouterConfigError("CODEX_HOME must be a dedicated absolute directory")
    home = Path(raw_home)
    if not home.is_absolute() or home == Path(home.anchor):
        raise RouterConfigError("CODEX_HOME must be a dedicated absolute directory")

    # Reject symlink components so a later provider or filesystem race cannot
    # redirect the credential root into the user's normal Codex directory.
    try:
        for ancestor in home.parents:
            if ancestor == ancestor.parent:
                continue
            if ancestor.is_symlink():
                raise RouterConfigError("CODEX_HOME may not contain symlink components")
        if home.is_symlink():
            raise RouterConfigError("CODEX_HOME may not be a symlink")
        if not home.is_dir():
            raise RouterConfigError("CODEX_HOME must be an existing directory")
        resolved_home = home.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise RouterConfigError("CODEX_HOME is not an accessible directory") from error

    # A dedicated home must not be the user's normal Codex home.  The
    # comparison is intentionally conservative and only rejects the exact
    # conventional location; broader overlap with the worktree is handled
    # below because that would expose the keyring setup to model writes.
    user_home = env_text("HOME")
    if user_home:
        try:
            normal_codex_home = (Path(user_home).expanduser() / ".codex").resolve(strict=False)
        except (OSError, RuntimeError):
            normal_codex_home = None
        if normal_codex_home is not None and resolved_home == normal_codex_home:
            raise RouterConfigError("CODEX_HOME must be separate from the user's ~/.codex")

    if cwd:
        try:
            resolved_cwd = Path(cwd).resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise RouterConfigError("worker cwd is not accessible") from error
        if _path_overlaps(resolved_home, resolved_cwd):
            raise RouterConfigError("CODEX_HOME must not overlap the worker worktree")

    # os.path.lexists catches dangling symlinks, unlike Path.exists().  Walk
    # without following symlink directories so a stale or malicious link can
    # never hide an auth.json outside the dedicated root.
    try:
        def walk_error(error: OSError) -> None:
            raise error

        for root, directories, files in os.walk(
            resolved_home,
            followlinks=False,
            onerror=walk_error,
        ):
            for name in (*directories, *files):
                candidate = Path(root) / name
                if name == CODEX_AUTH_FILENAME or (
                    os.path.lexists(candidate) and candidate.is_symlink()
                ):
                    raise RouterConfigError(
                        "CODEX_HOME must not contain auth.json or symlink entries"
                    )
    except (OSError, RuntimeError) as error:
        raise RouterConfigError("CODEX_HOME could not be inspected safely") from error
    return resolved_home


def _validated_absolute_paths(
    paths: Sequence[str | os.PathLike[str]],
    field_name: str,
) -> tuple[Path, ...]:
    """Validate absolute paths without following symlink components."""

    if isinstance(paths, (str, bytes, bytearray)) or not isinstance(paths, Sequence):
        raise RouterConfigError(f"{field_name} must be a sequence of paths")
    normalized: list[Path] = []
    for index, raw in enumerate(paths):
        if not isinstance(raw, (str, os.PathLike)):
            raise RouterConfigError(f"{field_name}[{index}] must be a path")
        path = Path(raw)
        if not path.is_absolute():
            raise RouterConfigError(f"{field_name}[{index}] must be absolute")
        for ancestor in reversed(path.parents):
            if ancestor == ancestor.parent:
                break
            if ancestor.is_symlink():
                raise RouterConfigError(
                    f"{field_name}[{index}] may not contain symlinks"
                )
        if path.is_symlink():
            raise RouterConfigError(f"{field_name}[{index}] may not be a symlink")
        try:
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise RouterConfigError(f"{field_name}[{index}] is not accessible") from error
        if resolved == Path(resolved.anchor):
            raise RouterConfigError(f"{field_name}[{index}] may not be a filesystem root")
        if resolved in normalized:
            raise RouterConfigError(f"{field_name} contains duplicate paths")
        normalized.append(resolved)
    return tuple(normalized)


def _validate_writable_scope(
    writable_paths: Sequence[str | os.PathLike[str]],
    contract_paths: Sequence[str | os.PathLike[str]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return canonical path strings after binding writes to the contract."""

    contracts = _validated_absolute_paths(contract_paths, "contract_paths")
    writable = _validated_absolute_paths(writable_paths, "writable_paths")
    if writable and not contracts:
        raise RouterConfigError("writable_paths require contract_paths")
    for path in writable:
        if not any(path == scope or scope in path.parents for scope in contracts):
            raise RouterConfigError("writable_paths must be a subset of contract_paths")
    return tuple(str(path) for path in writable), tuple(str(path) for path in contracts)


@dataclass(frozen=True)
class ModelRequest:
    """A provider invocation request.

    Worker requests must carry a plan-selected effort.  The router never
    chooses worker effort heuristically: the controller has already estimated
    subtask difficulty and must pass exactly ``high`` or ``xhigh``.
    """

    role: str
    prompt: str
    effort: str | None = None
    expected_execution_id: str | None = None
    cwd: str | None = None
    env: Mapping[str, str] | None = None
    output_validator: Callable[[str], bool] | None = None
    timeout_seconds: float | None = None
    # Absolute paths are supplied by the trusted orchestrator after binding a
    # relative plan scope to the adopted worktree.  A controller may have no
    # writable paths; workers get exactly their subtask roots.
    writable_paths: Sequence[str | os.PathLike[str]] = ()
    contract_paths: Sequence[str | os.PathLike[str]] = ()
    # Controller output has two strict contracts: ``plan`` before workers and
    # ``verdict`` after workers.  ``mode`` is a compatibility alias accepted
    # by callers that use the shorter name; worker requests must leave both
    # unset.
    controller_mode: str = "plan"
    mode: str | None = None

    def validate(self) -> None:
        if self.role not in {CONTROLLER, WORKER}:
            raise RouterConfigError(f"unknown router role: {self.role!r}")
        if not isinstance(self.prompt, str):
            raise RouterConfigError("prompt must be text supplied through stdin")
        if self.role == WORKER and self.effort not in HIGH_EFFORTS:
            raise RouterConfigError("worker effort must be exactly 'high' or 'xhigh'")
        if self.role == CONTROLLER and self.effort not in (None, "medium"):
            raise RouterConfigError("controller effort is fixed at medium")
        selected_mode = self.mode if self.mode is not None else self.controller_mode
        if self.role == CONTROLLER and selected_mode not in {"plan", "verdict"}:
            raise RouterConfigError("controller mode must be 'plan' or 'verdict'")
        if self.role == WORKER and (
            self.mode is not None or self.controller_mode != "plan"
        ):
            raise RouterConfigError("controller mode is only valid for controllers")
        if self.role == CONTROLLER:
            if self.expected_execution_id is None:
                raise RouterConfigError("controller requests require expected_execution_id")
            try:
                validate_execution_id(self.expected_execution_id)
            except (OrchestrationError, TypeError) as error:
                raise RouterConfigError("expected_execution_id is not safe") from error
        elif self.expected_execution_id is not None:
            raise RouterConfigError("expected_execution_id is only valid for controllers")
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
        ):
            raise RouterConfigError("timeout_seconds must be a finite positive number")
        if self.role == CONTROLLER and self.writable_paths:
            raise RouterConfigError("controller requests must have zero writable paths")
        _validate_writable_scope(self.writable_paths, self.contract_paths)


@dataclass(frozen=True)
class ModelResult:
    """Safe routing metadata returned to the trusted parent worker."""

    status: str
    provider: str | None
    model: str | None
    effort: str | None
    exit_code: int | None
    attempts: int
    duration_ms: int
    classification: str | None = None
    fallback_used: bool = False
    output: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.status not in {"success", "needs_human", "waiting_provider"}:
            raise RouterConfigError(f"unknown model result status: {self.status!r}")
        if self.attempts < 0:
            raise RouterConfigError("attempts cannot be negative")

    def as_dict(self) -> dict[str, object]:
        """Return metadata safe for logs and machine-readable CLI output.

        Model output is intentionally absent.  Callers that need it can use
        the in-memory ``output`` field or ask the CLI to write an output file.
        Provider stderr and command argv are never retained here.
        """

        return {
            "status": self.status,
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
            "exit_code": self.exit_code,
            "attempts": self.attempts,
            "duration_ms": self.duration_ms,
            "classification": self.classification,
            "fallback_used": self.fallback_used,
        }


@dataclass(frozen=True)
class _ProviderSpec:
    name: str
    model: str
    effort: str


def classify_failure(returncode: int, stderr: str = "") -> str:
    """Classify process failures using explicit, provider-neutral signals.

    The classifier is intentionally conservative.  Unknown failures do not
    trigger a provider fallback because doing so can hide authentication,
    configuration, or prompt/output bugs.
    """

    text = stderr.lower()
    if returncode == 0:
        return "unknown"
    if returncode in {401, 403} or any(
        marker in text
        for marker in (
            "authentication",
            "unauthorized",
            "invalid api key",
            "api key is invalid",
            "credential",
            "permission denied",
            "login required",
        )
    ):
        return "auth"
    if returncode in {429, 529} or any(
        marker in text
        for marker in (
            "quota exceeded",
            "rate limit",
            "rate_limit",
            "usage limit",
            "too many requests",
            "credits exhausted",
        )
    ):
        return "quota"
    if returncode in {408, 500, 502, 503, 504, 522, 524, 75} or any(
        marker in text
        for marker in (
            "timed out",
            "timeout",
            "temporarily unavailable",
            "temporary failure",
            "connection reset",
            "connection refused",
            "service unavailable",
            "overloaded",
            "try again",
        )
    ):
        return "transient"
    if any(marker in text for marker in ("policy", "safety refusal", "policy_blocked")):
        return "policy"
    if any(
        marker in text
        for marker in (
            "command not found",
            "no such file or directory",
            "invalid option",
            "unknown option",
            "configuration",
            "config error",
        )
    ):
        return "config"
    return "unknown"


def _process_group_id(process: Any) -> int | None:
    """Return a safe, distinct process-group id for a POSIX child."""

    if os.name == "nt":
        return None
    pid = getattr(process, "pid", None)
    if pid is None:
        return None
    try:
        group_id = os.getpgid(pid)
        # A group id equal to our own would make a cleanup bug catastrophic.
        if group_id > 0 and group_id != os.getpgrp():
            return group_id
    except (OSError, ProcessLookupError, TypeError, ValueError):
        pass
    return None


def _terminate_process_group(process: Any, *, group_id: int | None) -> bool:
    """Terminate a timed-out provider and reap its direct child.

    Provider commands may launch descendants (for example a shell wrapper).
    A timeout on ``Popen.communicate`` only stops waiting; signaling the
    isolated group before retrying prevents those descendants from surviving
    beside the next attempt.  Returning ``False`` makes callers fail closed
    instead of retrying when cleanup cannot be confirmed.
    """

    cleanup_ok = True
    group_force_killed = False
    if group_id is not None:
        try:
            os.killpg(group_id, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            cleanup_ok = False
    else:
        try:
            process.terminate()
        except (OSError, AttributeError):
            cleanup_ok = False

    try:
        process.wait(timeout=0.25)
    except subprocess.TimeoutExpired:
        if group_id is not None:
            try:
                os.killpg(group_id, signal.SIGKILL)
                group_force_killed = True
            except ProcessLookupError:
                group_force_killed = True
                pass
            except OSError:
                cleanup_ok = False
        try:
            process.kill()
        except (OSError, AttributeError):
            cleanup_ok = False
        try:
            process.wait(timeout=0.25)
        except (subprocess.TimeoutExpired, OSError, AttributeError):
            cleanup_ok = False
    except (OSError, AttributeError):
        cleanup_ok = False

    # The direct child can exit on SIGTERM while a descendant ignores it and
    # keeps the process group (and our pipes) alive.  Re-check the group after
    # reaping the child and force-kill any remaining members.
    if group_id is not None and not group_force_killed:
        try:
            os.killpg(group_id, signal.SIGKILL)
            group_force_killed = True
        except ProcessLookupError:
            group_force_killed = True
            pass
        except OSError:
            cleanup_ok = False

    try:
        if process.poll() is None:
            cleanup_ok = False
    except (OSError, AttributeError):
        cleanup_ok = False
    return cleanup_ok


def _default_runner(
    argv: Sequence[str],
    *,
    stdin: str,
    env: Mapping[str, str],
    cwd: str | None,
    timeout: float | None,
) -> ProcessResult:
    popen_kwargs: dict[str, object] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": dict(env),
        "cwd": cwd,
        "text": True,
    }
    # ``start_new_session`` is the portable POSIX equivalent of placing the
    # provider in a fresh process group.  It is not a Windows Popen option.
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True

    process = subprocess.Popen(list(argv), **popen_kwargs)
    group_id = _process_group_id(process)
    try:
        stdout, stderr = process.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name != "nt" and group_id is None:
            # A POSIX invocation was expected to have an isolated process
            # group.  Without its id we cannot prove descendants are gone.
            _terminate_process_group(process, group_id=None)
            return ProcessResult(125, "", "configuration error")
        if not _terminate_process_group(process, group_id=group_id):
            # A retry while an untracked descendant may still be running can
            # create concurrent provider executions.  Stop fail-closed.
            return ProcessResult(125, "", "configuration error")
        try:
            # The process is reaped above; this drains any buffered pipe data
            # without allowing a timed-out descendant to hold the retry open.
            process.communicate(timeout=0.25)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return ProcessResult(125, "", "configuration error")
        return ProcessResult(408, "", "provider process timed out")
    except BaseException:
        # Do not leak a provider process when the caller is interrupted or a
        # pipe fails unexpectedly.  Re-raise after best-effort cleanup.
        _terminate_process_group(process, group_id=group_id)
        raise
    return ProcessResult(process.returncode, stdout or "", stderr or "")


_GITHUB_SECRET_NAMES = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "ACTIONS_RUNTIME_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        "GIT_ASKPASS",
        "SSH_AUTH_SOCK",
    }
)


_SAFE_ENV_NAMES = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PWD",
        "SHELL",
        "TERM",
        "TMPDIR",
        "USER",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "CI",
        "NO_COLOR",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
    }
)
_PROVIDER_ENV_NAMES = {
    # Authentication is deliberately subscription-first: the CLIs use their
    # local account/configuration state (CODEX_HOME / CLAUDE_CONFIG_DIR), not
    # ambient API or OAuth credentials inherited from the worker.
    OPENAI: frozenset({"OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID"}),
    ANTHROPIC: frozenset({"ANTHROPIC_BASE_URL"}),
}
_SUBSCRIPTION_CREDENTIAL_NAMES = frozenset(
    {
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    }
)
_AUTHORITY_PREFIXES = ("GIT_", "SSH_", "GITHUB_", "GH_", "ACTIONS_")
_AUTHORITY_NAMES = frozenset(
    {
        *_GITHUB_SECRET_NAMES,
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_CREDENTIAL_HELPER",
        "GIT_TERMINAL_PROMPT",
    }
)


def child_environment(
    base: Mapping[str, str] | None = None, *, provider: str | None = None
) -> dict[str, str]:
    """Build a minimal provider environment without repository authority.

    ``provider`` is required by the router and selects only that provider's
    credentials.  The provider-neutral form is retained for callers that only
    need the authority scrubber (and intentionally keeps no API credentials).
    """

    if provider is not None and provider not in {OPENAI, ANTHROPIC}:
        raise RouterConfigError(f"unknown provider environment: {provider!r}")
    source = dict(os.environ if base is None else base)
    allowed = set(_SAFE_ENV_NAMES)
    if provider is not None:
        allowed.update(_PROVIDER_ENV_NAMES[provider])
    result: dict[str, str] = {}
    for name, value in source.items():
        upper = name.upper()
        if name in _SUBSCRIPTION_CREDENTIAL_NAMES:
            continue
        if name not in allowed:
            continue
        if name in _AUTHORITY_NAMES or upper.startswith(_AUTHORITY_PREFIXES):
            continue
        if provider == OPENAI and (upper.startswith("ANTHROPIC_") or upper.startswith("CLAUDE_")):
            continue
        if provider == ANTHROPIC and (
            upper.startswith("OPENAI_") or upper.startswith("CODEX_")
        ):
            continue
        if provider == OPENAI and name == "CLAUDE_CONFIG_DIR":
            continue
        if provider == ANTHROPIC and name == "CODEX_HOME":
            continue
        result[name] = value
    result["GIT_TERMINAL_PROMPT"] = "0"
    # Do not let a checkout-level credential helper or system Git config turn
    # a model-generated git command into repository authority.  The sandbox
    # also denies network access, but this keeps the child fail-closed on
    # platforms where a Git subprocess is still available.
    result["GIT_CONFIG_NOSYSTEM"] = "1"
    result["GIT_CONFIG_GLOBAL"] = os.devnull
    return result


def _availability(value: Availability | str | bool | None) -> Availability:
    if value is None:
        return Availability(True)
    if isinstance(value, Availability):
        return value
    if isinstance(value, bool):
        return Availability(value, "available" if value else "config")
    normalized = str(value).strip().lower()
    if normalized == "available":
        return Availability(True)
    return Availability(False, normalized)


def _controller_output_is_valid(
    output: str,
    expected_execution_id: str,
    mode: str = "plan",
) -> bool:
    """Accept one strict controller object, optionally wrapped in a JSON fence.

    Provider prose is intentionally not searched for a JSON substring: doing
    so makes a valid-looking plan inside an explanation ambiguous.  The
    Codex CLI supports output schemas, but the router has no schema file to
    ship in this module's invocation contract, so validation stays local and
    deterministic here.
    """

    text = output.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) < 3 or lines[-1].strip() != "```":
            return False
        language = lines[0].strip().lower()
        if language not in {"```", "```json"}:
            return False
        text = "\n".join(lines[1:-1]).strip()
    if not text.startswith("{") or not text.endswith("}"):
        return False

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    try:
        payload = json.loads(text, object_pairs_hook=reject_duplicate_keys)
        if not isinstance(payload, dict):
            return False
        if mode == "plan":
            parsed = parse_plan(payload)
        elif mode == "verdict":
            parsed = parse_verdict(payload)
        else:
            return False
    except (json.JSONDecodeError, OrchestrationError, TypeError, ValueError):
        return False
    return parsed.execution_id == expected_execution_id


class ModelRouter:
    """Route one controller or worker invocation with fail-closed fallback."""

    def __init__(
        self,
        policy: RoutingPolicy | None = None,
        *,
        runner: CommandRunner | None = None,
        availability: AvailabilityCallback | None = None,
        clock: Callable[[], float] | None = None,
        # Kept as a source-compatible keyword for callers from the previous
        # wrapper implementation.  Native Claude Code owns the sandbox now;
        # no caller-supplied executable is ever invoked.
        sandbox_backend: object | None = None,
    ) -> None:
        self.policy = policy or RoutingPolicy.from_env()
        self.runner = runner or _default_runner
        self.availability = availability
        self.clock = clock or time.monotonic
        # The old ``sandbox_backend`` was a dangerous process wrapper.  It is
        # deliberately ignored rather than trusted: native Claude Code must
        # receive the complete per-invocation settings document below.
        del sandbox_backend

    def _specs(self, request: ModelRequest) -> tuple[_ProviderSpec, _ProviderSpec]:
        effort = request.effort or self.policy.controller_effort
        if request.role == CONTROLLER:
            return (
                _ProviderSpec(OPENAI, self.policy.controller_openai_model, "medium"),
                _ProviderSpec(
                    ANTHROPIC,
                    self.policy.controller_anthropic_model,
                    self.policy.anthropic_controller_effort,
                ),
            )
        assert request.effort in HIGH_EFFORTS
        anthropic_effort = (
            self.policy.anthropic_worker_high_effort
            if request.effort == "high"
            else self.policy.anthropic_worker_xhigh_effort
        )
        return (
            _ProviderSpec(OPENAI, self.policy.worker_openai_model, effort),
            _ProviderSpec(ANTHROPIC, self.policy.worker_anthropic_model, anthropic_effort),
        )

    def _argv(
        self,
        spec: _ProviderSpec,
        request: ModelRequest,
        *,
        settings_path: str | None = None,
    ) -> tuple[str, ...]:
        if spec.name == OPENAI:
            # ``-`` makes the prompt an explicit stdin stream.  It can never
            # be confused with an issue body accidentally placed in argv.
            argv = self.policy.codex_command + (
                "exec",
                "--sandbox",
                "workspace-write",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--strict-config",
            )
            for path in request.writable_paths:
                argv += ("--add-dir", str(path))
            return argv + (
                "--model",
                spec.model,
                "-c",
                f"model_reasoning_effort={spec.effort}",
                "-c",
                f'cli_auth_credentials_store="{CODEX_CREDENTIAL_STORE}"',
                # Codex needs HOME/CODEX_HOME itself to resolve the account;
                # model-generated shell children do not.  Excluding both
                # prevents a child command from rediscovering credential
                # paths through its inherited environment.  This is defense
                # in depth, not filesystem isolation.
                "-c",
                'shell_environment_policy.filters.HOME="exclude"',
                "-c",
                'shell_environment_policy.filters.CODEX_HOME="exclude"',
                "-",
            )
        if request.cwd is None or settings_path is None:
            raise SandboxError("Claude native sandbox requires a worktree and settings")
        command = self.policy.claude_command + (
            "-p",
            "--model",
            spec.model,
            "--effort",
            spec.effort,
        )
        try:
            return build_sandbox_argv(
                command,
                settings_path=settings_path,
            )
        except (AttributeError, TypeError, SandboxError, OSError, ValueError) as error:
            raise SandboxError("Claude native sandbox cannot be configured") from error

    def _check_availability(self, spec: _ProviderSpec, request: ModelRequest) -> Availability:
        if self.availability is None:
            # A missing quota/policy adapter must never silently authorize a
            # provider invocation.  This is especially important for the CLI,
            # where an omitted cache must be an explicit human-visible stop.
            return Availability(False, "config")
        try:
            quota_provider = QUOTA_PROVIDER[spec.name]
            return _availability(self.availability(quota_provider, request.role))
        except Exception:
            # An unavailable quota adapter is configuration failure, not proof
            # that a fallback provider is safe to use.
            return Availability(False, "config")

    def _invoke_provider(
        self,
        spec: _ProviderSpec,
        request: ModelRequest,
    ) -> tuple[ProcessResult, int, str | None]:
        # OpenAI keeps the empty control cwd.  Claude Code must start in the
        # adopted worktree so its built-in Read/Edit tools resolve relative
        # paths there; its native settings document supplies the OS and tool
        # boundaries.  The settings file itself lives in a private temporary
        # directory and is deleted after every attempt.
        try:
            with tempfile.TemporaryDirectory(prefix="aes-control-") as control_dir:
                effective_request = request
                settings_path: str | None = None
                if spec.name == ANTHROPIC:
                    # Native settings are constructed below, but the local
                    # CLI cannot attest that the managed policy needed for
                    # subscription isolation is actually loaded.  Keep the
                    # fallback disabled until an operator has verified that
                    # policy on this runner; request data cannot enable it.
                    if not managed_sandbox_ready():
                        return ProcessResult(126, "", "configuration error"), 0, "config"
                    if request.cwd is None:
                        return ProcessResult(126, "", "configuration error"), 0, "config"
                    try:
                        settings_path = str(
                            write_claude_settings(
                                Path(control_dir) / "claude-settings.json",
                                worktree=request.cwd,
                                writable_paths=request.writable_paths,
                                environ=request.env,
                            )
                        )
                    except (OSError, SandboxError, TypeError, ValueError):
                        return ProcessResult(126, "", "configuration error"), 0, "config"
                else:
                    effective_request = replace(request, cwd=control_dir)
                try:
                    argv = self._argv(
                        spec,
                        effective_request,
                        settings_path=settings_path,
                    )
                except SandboxError:
                    # Missing sandbox is a configuration stop, never a
                    # provider failure eligible for fallback or retry.
                    return ProcessResult(126, "", "configuration error"), 0, "config"
                attempts = 0
                last: ProcessResult | None = None
                limit = self.policy.transient_retries + 1
                for _ in range(limit):
                    attempts += 1
                    try:
                        environment = child_environment(
                            request.env,
                            provider=spec.name,
                        )
                        environment["PWD"] = str(effective_request.cwd or control_dir)
                        if spec.name == ANTHROPIC:
                            # Defense in depth for versions that do not yet
                            # implement --no-session-persistence for every
                            # print-mode code path.
                            environment["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] = "1"
                        result = self.runner(
                            argv,
                            stdin=request.prompt,
                            env=environment,
                            cwd=effective_request.cwd or control_dir,
                            timeout=(
                                request.timeout_seconds
                                if request.timeout_seconds is not None
                                else self.policy.timeout_seconds
                            ),
                        )
                    except (OSError, ValueError):
                        # Do not carry the exception text into a result: it may
                        # contain a path, token-bearing command, or provider
                        # response.
                        return (
                            ProcessResult(127, "", "configuration error"),
                            attempts,
                            "config",
                        )
                    last = result
                    if result.returncode == 0:
                        return result, attempts, None
                    classification = classify_failure(result.returncode, result.stderr)
                    if classification != "transient":
                        return result, attempts, classification
                assert last is not None
                return last, attempts, "transient"
        except OSError:
            return ProcessResult(127, "", "configuration error"), 0, "config"

    def invoke(self, request: ModelRequest) -> ModelResult:
        """Invoke OpenAI first and use Claude only on explicit allowed failures."""

        try:
            request.validate()
        except RouterConfigError:
            # Invalid plan/configuration is a human decision, not a provider
            # problem and therefore must never trigger a fallback.
            return ModelResult(
                "needs_human",
                None,
                None,
                request.effort,
                None,
                0,
                0,
                "config",
            )

        if len(request.prompt) > self.policy.max_prompt_chars:
            return ModelResult(
                "needs_human",
                None,
                None,
                request.effort,
                None,
                0,
                0,
                "config",
            )

        started = self.clock()
        primary, fallback = self._specs(request)
        try:
            _codex_home(request.env, cwd=request.cwd)
        except (RouterConfigError, TypeError, ValueError):
            # The primary Codex route must be explicitly provisioned with a
            # dedicated keyring-backed home.  Do this before quota checks so a
            # malformed/missing credential boundary cannot be masked as a
            # provider wait or trigger Claude fallback.
            return self._result(
                "needs_human",
                primary,
                None,
                0,
                started,
                "config",
                False,
                "",
            )
        total_attempts = 0
        first_failure: str | None = None
        last_failure: str | None = None
        last_exit: int | None = None

        for index, spec in enumerate((primary, fallback)):
            availability = self._check_availability(spec, request)
            if not availability.available:
                classification = availability.status
                if index == 0:
                    first_failure = classification
                    last_failure = classification
                    if classification not in ALLOWED_FALLBACK_FAILURES:
                        if classification in WAITABLE_FAILURES:
                            return self._result(
                                "waiting_provider",
                                spec,
                                None,
                                total_attempts,
                                started,
                                classification,
                                False,
                                "",
                            )
                        return self._result(
                            "needs_human",
                            spec,
                            None,
                            total_attempts,
                            started,
                            classification,
                            False,
                            "",
                        )
                    continue
                return self._result(
                    "waiting_provider" if classification in WAITABLE_FAILURES else "needs_human",
                    spec,
                    None,
                    total_attempts,
                    started,
                    classification,
                    True,
                    "",
                )

            process, attempts, classification = self._invoke_provider(spec, request)
            total_attempts += attempts
            last_exit = process.returncode
            if process.returncode == 0:
                if len(process.stdout) > self.policy.max_output_chars:
                    return self._result(
                        "needs_human",
                        spec,
                        last_exit,
                        total_attempts,
                        started,
                        "invalid_output",
                        index > 0,
                        "",
                    )
                if request.role == CONTROLLER:
                    selected_mode = (
                        request.mode
                        if request.mode is not None
                        else request.controller_mode
                    )
                    valid = _controller_output_is_valid(
                        process.stdout,
                        request.expected_execution_id,  # type: ignore[arg-type]
                        selected_mode,
                    )
                    if valid and request.output_validator is not None:
                        try:
                            valid = bool(request.output_validator(process.stdout))
                        except Exception:
                            valid = False
                elif request.output_validator is not None:
                    try:
                        valid = bool(request.output_validator(process.stdout))
                    except Exception:
                        valid = False
                else:
                    valid = bool(process.stdout.strip())
                if not valid:
                    return self._result(
                        "needs_human",
                        spec,
                        last_exit,
                        total_attempts,
                        started,
                        "invalid_output",
                        index > 0,
                        "",
                    )
                return self._result(
                    "success",
                    spec,
                    last_exit,
                    total_attempts,
                    started,
                    None,
                    index > 0,
                    process.stdout,
                )

            first_failure = first_failure or classification
            last_failure = classification
            last_exit = process.returncode
            if classification not in ALLOWED_FALLBACK_FAILURES:
                return self._result(
                    "needs_human",
                    spec,
                    last_exit,
                    total_attempts,
                    started,
                    classification,
                    index > 0,
                    "",
                )
            # Explicit quota/policy/transient: move to the next provider.

        # Both providers were unavailable with a fallback-eligible reason.
        return self._result(
            "waiting_provider" if last_failure in WAITABLE_FAILURES else "needs_human",
            fallback,
            last_exit,
            total_attempts,
            started,
            last_failure,
            True,
            "",
        )

    def _result(
        self,
        status: str,
        spec: _ProviderSpec,
        exit_code: int | None,
        attempts: int,
        started: float,
        classification: str | None,
        fallback_used: bool,
        output: str,
    ) -> ModelResult:
        elapsed = max(0.0, self.clock() - started)
        return ModelResult(
            status,
            spec.name,
            spec.model,
            spec.effort,
            exit_code,
            attempts,
            int(elapsed * 1000),
            classification,
            fallback_used,
            output,
        )


def _confined_path(raw: str, root: Path, *, output: bool = False) -> Path:
    """Resolve a CLI path below ``root`` and reject symlink components."""

    if not isinstance(raw, str) or not raw.strip():
        raise RouterConfigError("CLI path cannot be empty")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise RouterConfigError("approved root must be a directory")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise RouterConfigError("CLI path is outside the approved root") from error
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise RouterConfigError("CLI paths may not contain symlinks")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise RouterConfigError("CLI path resolves outside the approved root") from error
    if output and resolved.exists() and not resolved.is_file():
        raise RouterConfigError("output path must be a regular file")
    return resolved


def _read_prompt(path: str, stdin: TextIO, *, max_chars: int, root: Path) -> str:
    if path == "-":
        prompt = stdin.read(max_chars + 1)
    else:
        prompt_path = _confined_path(path, root)
        with prompt_path.open("r", encoding="utf-8") as handle:
            prompt = handle.read(max_chars + 1)
    if len(prompt) > max_chars:
        raise RouterConfigError("prompt exceeds the configured size limit")
    return prompt


def _write_output(path: Path, content: str) -> None:
    """Write output without following a final symlink."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags | nofollow, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(content)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _quota_cache_availability(path: Path) -> AvailabilityCallback:
    """Adapt the normalized quota cache to the router's provider names."""

    try:
        from quota import QuotaCache, provider_availability
    except ImportError as error:  # pragma: no cover - packaging failure
        raise RouterConfigError("quota adapter is unavailable") from error
    cache = QuotaCache(path)

    def availability(provider: str, role: str) -> Availability:
        del role
        # The router callback deliberately speaks the quota module's
        # canonical names (codex/claude).  Do not translate them back through
        # the model-provider names (openai/anthropic).
        if provider not in {"codex", "claude"}:
            return Availability(False, "config")
        state = provider_availability(provider, cache=cache)
        return Availability(state.status == "available", state.status, state.reason)

    return availability


def _cli(argv: Sequence[str], *, stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=(CONTROLLER, WORKER), required=True)
    parser.add_argument(
        "--mode",
        choices=("plan", "verdict"),
        default="plan",
        help="strict controller output contract (ignored for workers)",
    )
    parser.add_argument(
        "--prompt-file", required=True, help="file containing prompt, or '-' for stdin"
    )
    parser.add_argument("--effort", choices=tuple(sorted(HIGH_EFFORTS)), default=None)
    parser.add_argument(
        "--execution-id",
        default=None,
        help="expected orchestration execution id for controller output",
    )
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--output-file", default=None)
    parser.add_argument(
        "--root",
        default=None,
        help="approved root for prompt, cwd, output, and quota-cache paths",
    )
    parser.add_argument(
        "--quota-cache",
        default=None,
        help="normalized quota cache; omitted means no provider invocation",
    )
    parser.add_argument(
        "--writable-path",
        action="append",
        default=[],
        help="absolute/approved path made writable for this invocation (repeatable)",
    )
    parser.add_argument(
        "--contract-path",
        action="append",
        default=[],
        help="approved contract scope binding writable paths (repeatable)",
    )
    parser.add_argument("--expect-json", action="store_true")
    args = parser.parse_args(list(argv))
    try:
        root = Path(args.root or os.environ.get("AES_APPROVED_ROOT", os.getcwd())).resolve(
            strict=True
        )
        policy = RoutingPolicy.from_env()
        prompt = _read_prompt(
            args.prompt_file,
            stdin,
            max_chars=policy.max_prompt_chars,
            root=root,
        )
        cwd = _confined_path(args.cwd, root) if args.cwd else root
        output_file = _confined_path(args.output_file, root, output=True) if args.output_file else None
        writable_paths = tuple(
            str(_confined_path(path, root)) for path in args.writable_path
        )
        contract_paths = tuple(
            str(_confined_path(path, root)) for path in args.contract_path
        )
        quota_availability = (
            _quota_cache_availability(_confined_path(args.quota_cache, root))
            if args.quota_cache
            else None
        )
        if args.role == CONTROLLER and not args.execution_id:
            raise RouterConfigError("controller CLI requests require --execution-id")
        result = ModelRouter(policy, availability=quota_availability).invoke(
            ModelRequest(
                role=args.role,
                prompt=prompt,
                effort=args.effort,
                cwd=str(cwd),
                expected_execution_id=(
                    args.execution_id if args.role == CONTROLLER else None
                ),
                output_validator=None,
                controller_mode=args.mode,
                writable_paths=writable_paths,
                contract_paths=contract_paths,
            )
        )
        if output_file and result.output:
            _write_output(output_file, result.output)
        stdout.write(json.dumps(result.as_dict(), sort_keys=True) + "\n")
        return 0 if result.status == "success" else 1
    except (OSError, RouterConfigError) as error:
        # Keep provider paths/errors out of the machine-readable stream.
        stderr.write(f"model router configuration error: {type(error).__name__}\n")
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    return _cli(
        sys.argv[1:] if argv is None else argv,
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
