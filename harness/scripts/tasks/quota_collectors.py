#!/usr/bin/env python3
"""Bounded subscription quota collectors for Codex and Claude Code.

This module is the provider-I/O edge of the quota policy in :mod:`quota`.
It deliberately keeps the protocol code separate from routing and persistence:

* ``refresh-codex`` starts ``codex app-server --stdio`` and speaks the local
  JSONL v2 protocol (``initialize`` / ``initialized`` /
  ``account/rateLimits/read``).  It waits for the matching JSON-RPC response,
  ignoring notifications and unrelated messages, and always tears the process
  down before returning.
* ``ingest-claude`` reads one Claude Code statusline JSON object from stdin,
  extracts only ``rate_limits.five_hour``, and atomically updates
  :class:`quota.QuotaCache`.

Both commands are subscription-session only.  API-key environment variables
are removed from the Codex child environment and are never accepted as CLI
arguments.  Raw provider payloads and process stderr are never persisted or
printed.  The Claude command is intentionally silent on stdout, so it can be
used as a ``statusLine`` sidecar without replacing the user's displayed line::

    {
      "statusLine": {
        "type": "command",
        "command": "AES_QUOTA_CACHE_ROOT=/var/lib/aes/quota AES_QUOTA_CACHE_PATH=quota.json python3 /opt/aes/harness/scripts/tasks/quota_collectors.py ingest-claude"
      }
    }

Claude Code invokes that command with its statusline JSON object on stdin;
``rate_limits.five_hour.used_percentage`` and ``resets_at`` are the only
provider fields consumed. ``AES_QUOTA_CACHE_ROOT`` and
``AES_QUOTA_CACHE_PATH`` point to one shared persistent file outside every
checkout; the worker rejects missing, relative-root, traversal, or
checkout-local configuration. The cache path is private (the writer enforces
mode ``0600`` and uses an atomic replace). Missing or stale Claude snapshots
are fail-closed during Europe/Berlin work hours (Monday–Friday 08:00–17:00),
and the worker refreshes Codex before each execution.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Sequence, TextIO

try:  # Direct execution (the normal CLI path).
    from quota import (
        QuotaCache,
        QuotaError,
        QuotaPayloadError,
        QuotaSnapshot,
        normalize_claude_statusline,
        normalize_codex_snapshot,
        _parse_timestamp,
    )
except ImportError:  # pragma: no cover - package import convenience
    from .quota import (  # type: ignore[no-redef]
        QuotaCache,
        QuotaError,
        QuotaPayloadError,
        QuotaSnapshot,
        normalize_claude_statusline,
        normalize_codex_snapshot,
        _parse_timestamp,
    )


CODEX = "codex"
CLAUDE = "claude"
DEFAULT_CODEX_COMMAND = ("codex", "app-server", "--stdio")
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_LINE_BYTES = 1_048_576
DEFAULT_MAX_INPUT_BYTES = 1_048_576
DEFAULT_MAX_MESSAGES = 512
_SAFE_ENVIRONMENT_NAMES = frozenset(
    {
        "CODEX_HOME",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "NO_COLOR",
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
    }
)
_SAFE_ENVIRONMENT_PREFIXES = ("LC_",)
_SECRET_ENVIRONMENT_SUFFIXES = (
    "_API_KEY",
    "_API_TOKEN",
    "_AUTH_TOKEN",
    "_PASSWORD",
    "_SECRET",
    "_TOKEN",
)
_MISSING = object()


class CollectorError(QuotaError):
    """A provider collector cannot obtain a safe, complete observation."""


class CollectorTimeoutError(CollectorError):
    """The bounded provider protocol deadline elapsed."""


class CollectorProtocolError(CollectorError):
    """The provider emitted malformed or unexpected JSON-RPC data."""


@dataclass(frozen=True)
class _ProcessSpec:
    command: tuple[str, ...]
    env: dict[str, str]


def _validate_timeout(value: float | int | str | None) -> float:
    raw: Any = (
        os.environ.get("AES_CODEX_QUOTA_TIMEOUT_SECONDS")
        if value is None
        else value
    )
    if raw in (None, ""):
        return DEFAULT_TIMEOUT_SECONDS
    try:
        parsed = float(raw)
    except (TypeError, ValueError) as error:
        raise CollectorError("collector timeout must be positive") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise CollectorError("collector timeout must be positive")
    return parsed


def _validate_limit(value: int | str | None, *, default: int, field: str) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise CollectorError(f"{field} must be positive") from error
    if parsed <= 0:
        raise CollectorError(f"{field} must be positive")
    return parsed


def _process_spec(
    command: Sequence[str] | None,
    environment: Mapping[str, str] | None,
    *,
    allow_command_override: bool = True,
) -> _ProcessSpec:
    if command is not None and not allow_command_override:
        raise CollectorError("Codex command override is unavailable in production")
    raw_command = tuple(command or DEFAULT_CODEX_COMMAND)
    if not raw_command or any(not isinstance(part, str) or not part for part in raw_command):
        raise CollectorError("Codex command must not be empty")
    # Do not let an apparently harmless command override the subscription-only
    # contract with an API key flag.  Values are not included in this error.
    for part in raw_command[1:]:
        option = part.split("=", 1)[0].strip().lower()
        if option in {"--api-key", "--api_key", "-api-key"} or any(
            marker in part.lower() for marker in ("api_key=", "api-key=")
        ):
            raise CollectorError("Codex API-key authentication is not supported")

    source = dict(os.environ if environment is None else environment)
    child_environment: dict[str, str] = {}
    # The app-server uses the persisted Codex account session.  Keep the
    # child's environment deterministic and remove GitHub/cloud/general
    # authority while deliberately preserving CODEX_HOME.
    for name, value in source.items():
        upper = name.upper()
        if name not in _SAFE_ENVIRONMENT_NAMES and not any(
            upper.startswith(prefix) for prefix in _SAFE_ENVIRONMENT_PREFIXES
        ):
            continue
        if upper.endswith(_SECRET_ENVIRONMENT_SUFFIXES):
            continue
        child_environment[name] = value
    child_environment["GIT_TERMINAL_PROMPT"] = "0"
    child_environment["GIT_CONFIG_NOSYSTEM"] = "1"
    child_environment["GIT_CONFIG_GLOBAL"] = os.devnull
    return _ProcessSpec(raw_command, child_environment)


class _JsonlReader:
    """A deadline-aware, line-bounded reader for a subprocess pipe."""

    def __init__(self, stream: Any, *, max_line_bytes: int):
        self._stream = stream
        try:
            self._fd = stream.fileno()
        except (AttributeError, OSError, ValueError) as error:
            raise CollectorProtocolError("Codex stdout is not a readable pipe") from error
        self._max_line_bytes = max_line_bytes
        self._buffer = bytearray()

    def read_message(self, deadline: float) -> Mapping[str, Any] | None:
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                return self._decode(line)
            if len(self._buffer) > self._max_line_bytes:
                raise CollectorProtocolError("Codex JSONL message exceeds the size limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CollectorTimeoutError("Codex app-server response timed out")
            try:
                ready, _, _ = select.select([self._fd], [], [], remaining)
            except (OSError, ValueError) as error:
                raise CollectorProtocolError("cannot read Codex app-server output") from error
            if not ready:
                raise CollectorTimeoutError("Codex app-server response timed out")
            try:
                chunk = os.read(self._fd, min(8192, self._max_line_bytes + 1 - len(self._buffer)))
            except OSError as error:
                raise CollectorProtocolError("cannot read Codex app-server output") from error
            if not chunk:
                if not self._buffer:
                    return None
                line = bytes(self._buffer)
                self._buffer.clear()
                return self._decode(line)
            self._buffer.extend(chunk)

    @staticmethod
    def _decode(line: bytes) -> Mapping[str, Any]:
        line = line.rstrip(b"\r")
        if not line.strip():
            # Blank lines are not JSON-RPC notifications.  Returning an empty
            # object lets the caller ignore them without exposing text.
            return {}
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CollectorProtocolError("Codex emitted malformed JSONL") from error
        if not isinstance(value, Mapping):
            raise CollectorProtocolError("Codex JSONL message must be an object")
        return value


def _send_message(stream: Any, payload: Mapping[str, Any]) -> None:
    try:
        stream.write((json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        stream.flush()
    except (OSError, BrokenPipeError, AttributeError, TypeError) as error:
        raise CollectorProtocolError("cannot write Codex app-server request") from error


def _wait_for_response(
    reader: _JsonlReader,
    expected_id: int,
    *,
    deadline: float,
    max_messages: int,
) -> Mapping[str, Any]:
    for _ in range(max_messages):
        message = reader.read_message(deadline)
        if message is None:
            raise CollectorProtocolError("Codex app-server exited before the response")
        # Notifications (including sparse account/rate-limit updates) and
        # blank lines have no id.  They are deliberately ignored until the
        # response matching this request arrives.
        if message.get("id", _MISSING) != expected_id:
            continue
        if "error" in message:
            raise CollectorProtocolError("Codex app-server rejected the request")
        result = message.get("result", _MISSING)
        if not isinstance(result, Mapping):
            raise CollectorProtocolError("Codex app-server returned no result")
        return result
    raise CollectorProtocolError("Codex app-server emitted too many messages")


def _cleanup_process(process: Any) -> None:
    """Close pipes and terminate the short-lived app-server process."""

    # Capture the process group before closing stdin.  Closing a protocol pipe
    # can make the direct child exit while descendants keep running; signaling
    # first guarantees the whole start-new-session group receives termination.
    running = getattr(process, "poll", lambda: 0)() is None
    pid = getattr(process, "pid", None)
    group_id: int | None = None
    if running and pid is not None and os.name != "nt":
        try:
            candidate = os.getpgid(pid)
            # ``start_new_session=True`` should give the child its own group.
            # Never signal our own group if a test double or a platform
            # implementation violates that assumption.
            if candidate > 0 and candidate != os.getpgrp():
                group_id = candidate
        except (OSError, ProcessLookupError, TypeError, ValueError):
            group_id = None

    if running:
        if group_id is not None:
            try:
                os.killpg(group_id, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                group_id = None
        if group_id is None:
            try:
                process.terminate()
            except (OSError, AttributeError):
                pass
        try:
            process.wait(timeout=0.25)
        except (subprocess.TimeoutExpired, OSError, AttributeError):
            if group_id is not None:
                try:
                    os.killpg(group_id, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
            try:
                process.kill()
            except (OSError, AttributeError):
                pass
            try:
                process.wait(timeout=0.25)
            except (subprocess.TimeoutExpired, OSError, AttributeError):
                pass

    stdin = getattr(process, "stdin", None)
    if stdin is not None:
        try:
            stdin.close()
        except (OSError, ValueError):
            pass

    for stream_name in ("stdout", "stderr"):
        stream = getattr(process, stream_name, None)
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def _query_codex(
    *,
    command: Sequence[str] | None,
    timeout: float | int | str | None,
    max_line_bytes: int | str | None,
    max_messages: int,
    environment: Mapping[str, str] | None,
    cwd: str | Path | None,
    popen_factory: Callable[..., Any],
    allow_command_override: bool,
) -> Mapping[str, Any]:
    spec = _process_spec(
        command,
        environment,
        allow_command_override=allow_command_override,
    )
    deadline = time.monotonic() + _validate_timeout(timeout)
    line_limit = _validate_limit(max_line_bytes, default=DEFAULT_MAX_LINE_BYTES, field="max_line_bytes")
    message_limit = _validate_limit(max_messages, default=DEFAULT_MAX_MESSAGES, field="max_messages")
    process: Any = None
    try:
        try:
            process = popen_factory(
                list(spec.command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=spec.env,
                cwd=str(cwd) if cwd is not None else None,
                bufsize=0,
                start_new_session=True,
            )
        except (OSError, TypeError) as error:
            raise CollectorError("cannot start Codex app-server") from error
        if process.stdin is None or process.stdout is None:
            raise CollectorProtocolError("Codex app-server pipes are unavailable")
        reader = _JsonlReader(process.stdout, max_line_bytes=line_limit)
        _send_message(
            process.stdin,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "aes-quota-collector",
                        "version": "1",
                    },
                    "capabilities": {"experimentalApi": False},
                },
            },
        )
        initialize_result = _wait_for_response(
            reader,
            1,
            deadline=deadline,
            max_messages=message_limit,
        )
        if not initialize_result:
            raise CollectorProtocolError("Codex initialize response is empty")
        _send_message(process.stdin, {"method": "initialized"})
        _send_message(
            process.stdin,
            {"id": 2, "method": "account/rateLimits/read", "params": None},
        )
        return _wait_for_response(
            reader,
            2,
            deadline=deadline,
            max_messages=message_limit,
        )
    finally:
        if process is not None:
            _cleanup_process(process)


def collect_codex_quota(
    *,
    cache: QuotaCache | None = None,
    cache_path: str | Path | None = None,
    cache_root: str | Path | None = None,
    command: Sequence[str] | None = None,
    timeout: float | int | str | None = None,
    max_line_bytes: int | str | None = None,
    max_messages: int = DEFAULT_MAX_MESSAGES,
    environment: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    observed_at: datetime | None = None,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    allow_command_override: bool = True,
) -> QuotaSnapshot:
    """Collect and persist one Codex subscription quota snapshot.

    ``command`` is a dependency-injection seam for deterministic tests and
    trusted embedding code.  The production CLI explicitly disables that
    seam, so an operator cannot replace the subscription app-server with an
    arbitrary command line.
    """

    result = _query_codex(
        command=command,
        timeout=timeout,
        max_line_bytes=max_line_bytes,
        max_messages=max_messages,
        environment=environment,
        cwd=cwd,
        popen_factory=popen_factory,
        allow_command_override=allow_command_override,
    )
    snapshot = normalize_codex_snapshot(
        result,
        observed_at=observed_at or datetime.now(timezone.utc),
    )
    if not snapshot.valid:
        raise QuotaPayloadError("Codex rate-limit response is missing a usable window")
    (cache or QuotaCache(cache_path, root=cache_root)).write(snapshot)
    return snapshot


def _read_stdin_json(stream: TextIO | None = None, *, max_bytes: int = DEFAULT_MAX_INPUT_BYTES) -> Mapping[str, Any]:
    source: Any = stream or sys.stdin
    try:
        raw = source.buffer.read(max_bytes + 1)
    except AttributeError:
        raw = source.read(max_bytes + 1)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > max_bytes:
        raise CollectorProtocolError("Claude statusline payload exceeds the size limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CollectorProtocolError("Claude statusline payload is not valid JSON") from error
    if not isinstance(payload, Mapping):
        raise CollectorProtocolError("Claude statusline payload must be an object")
    return payload


def ingest_claude_statusline(
    payload: Mapping[str, Any],
    *,
    cache: QuotaCache | None = None,
    cache_path: str | Path | None = None,
    cache_root: str | Path | None = None,
    observed_at: datetime | None = None,
) -> QuotaSnapshot:
    """Persist the five-hour Claude subscription window from one payload."""

    snapshot = normalize_claude_statusline(
        payload,
        observed_at=observed_at or datetime.now(timezone.utc),
    )
    if not snapshot.valid:
        raise QuotaPayloadError("Claude statusline has no usable five_hour window")
    (cache or QuotaCache(cache_path, root=cache_root)).write(snapshot)
    return snapshot


def _timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return _parse_timestamp(raw, field="observed_at")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    codex = commands.add_parser("refresh-codex", help="read Codex app-server quota into the cache")
    codex.add_argument("--cache", type=Path)
    codex.add_argument("--cache-root", type=Path)
    codex.add_argument("--timeout", type=float)
    codex.add_argument("--max-line-bytes", type=int)
    codex.add_argument("--observed-at")
    codex.add_argument("--command", nargs="+", dest="codex_command")
    claude = commands.add_parser("ingest-claude", help="ingest Claude statusline JSON from stdin")
    claude.add_argument("--cache", type=Path)
    claude.add_argument("--cache-root", type=Path)
    claude.add_argument("--observed-at")
    claude.add_argument("--max-input-bytes", type=int, default=DEFAULT_MAX_INPUT_BYTES)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "refresh-codex":
            snapshot = collect_codex_quota(
                cache_path=args.cache,
                cache_root=args.cache_root,
                command=args.codex_command,
                timeout=args.timeout,
                max_line_bytes=args.max_line_bytes,
                observed_at=_timestamp(args.observed_at),
                allow_command_override=False,
            )
            print(json.dumps(snapshot.to_cache(), sort_keys=True))
            return 0
        payload = _read_stdin_json(max_bytes=_validate_limit(args.max_input_bytes, default=DEFAULT_MAX_INPUT_BYTES, field="max_input_bytes"))
        ingest_claude_statusline(
            payload,
            cache_path=args.cache,
            cache_root=args.cache_root,
            observed_at=_timestamp(args.observed_at),
        )
        # A statusLine command's stdout becomes the displayed statusline.  A
        # sidecar must remain silent to preserve the user's existing output.
        return 0
    except (CollectorError, QuotaError, OSError, TypeError, ValueError):
        # Never print provider stderr, malformed payloads or exception text:
        # any of them may contain credentials or backend-owned content.
        print("quota collector failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
