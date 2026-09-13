#!/usr/bin/env python3
"""Fail-closed quota snapshots and provider selection for the AES queue.

The queue never guesses consumption. This module accepts only the small,
provider-specific quota fragments exposed by Codex app-server and Claude
Code's statusline, normalizes them into a secret-free cache, and applies the
routing policy at decision time. Raw provider payloads are intentionally not
written to disk: a cache contains percentages, reset timestamps and the time
at which those values were observed.

The public surface is deliberately usable without a provider SDK:

normalize_codex_snapshot / normalize_claude_statusline
    Parse one payload received by a runner.
QuotaCache
    Atomically persists normalized snapshots with mode 0600.
select_provider / QuotaCache.inspect
    Prefer Codex, and use Claude only when the Codex quota is exhausted.

The command line interface reads JSON from stdin for ingest and emits a JSON
decision for inspect. It is suitable for a trusted statusline or app-server
adapter and does not read API keys.

Deployments set ``AES_QUOTA_CACHE_ROOT`` and ``AES_QUOTA_CACHE_PATH`` to one
shared persistent location outside the checkout. ``AES_QUOTA_CACHE_FILE`` is
accepted only as a compatibility alias for older local integrations.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import secrets
import stat
import sys
import time as time_module
from typing import Any, Iterator, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


CODEX = "codex"
CLAUDE = "claude"
PROVIDER_ORDER = (CODEX, CLAUDE)

AVAILABLE = "available"
QUOTA_EXHAUSTED = "quota_exhausted"
POLICY_BLOCKED = "policy_blocked"
UNKNOWN = "unknown"
AUTH_ERROR = "auth_error"
STATES = frozenset({AVAILABLE, QUOTA_EXHAUSTED, POLICY_BLOCKED, UNKNOWN, AUTH_ERROR})

DEFAULT_TTL_SECONDS = 300
# The legacy default is retained for library/test callers that explicitly
# construct ``QuotaCache`` without deployment configuration.  Queue workers
# must provide AES_QUOTA_CACHE_ROOT/PATH so the shared snapshot survives a
# fresh checkout and is never coupled to one execution's artifacts.
DEFAULT_CACHE_PATH = Path(".agent") / "tasks" / "quota.json"
DEFAULT_CACHE_ROOT = DEFAULT_CACHE_PATH.parent
DEFAULT_TIMEZONE = "Europe/Berlin"
CLAUDE_WORKDAY_START = time(8, 0)
CLAUDE_WORKDAY_END = time(17, 0)
CLAUDE_WORKDAY_LIMIT = 70.0
SCHEMA = "aes.quota-cache.v1"


class QuotaError(RuntimeError):
    """Base error for invalid quota configuration or cache data."""


class QuotaConfigError(QuotaError):
    """The quota cache policy is configured unsafely."""


class QuotaPayloadError(QuotaError):
    """A provider payload cannot be normalized without guessing."""


class QuotaCacheError(QuotaError):
    """A cache cannot be read or written safely."""


def _canonical_provider(provider: str) -> str:
    aliases = {
        "codex": CODEX,
        "openai": CODEX,
        "claude": CLAUDE,
        "anthropic": CLAUDE,
    }
    try:
        return aliases[provider.strip().lower()]
    except (AttributeError, KeyError) as error:
        raise QuotaPayloadError(f"unsupported quota provider: {provider!r}") from error


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise QuotaPayloadError("timestamps must include an explicit timezone")
    return value.astimezone(timezone.utc)


def _now(value: datetime | None) -> datetime:
    return _aware(value) if value is not None else datetime.now(timezone.utc)


def _parse_timestamp(value: Any, *, field: str) -> datetime | None:
    """Parse an RFC3339 string or Unix seconds without accepting ambiguity."""

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise QuotaPayloadError(f"{field} is not a timestamp")
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise QuotaPayloadError(f"{field} is not a finite timestamp")
        numeric = float(value)
        # App-server payloads have appeared with both seconds and millis.
        if abs(numeric) >= 1_000_000_000_000:
            numeric /= 1000.0
        try:
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as error:
            raise QuotaPayloadError(f"{field} is outside the supported range") from error
    if not isinstance(value, str):
        raise QuotaPayloadError(f"{field} is not a timestamp")
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return _aware(datetime.fromisoformat(raw))
    except ValueError as error:
        raise QuotaPayloadError(f"{field} is not RFC3339") from error


def _parse_percent(value: Any, *, field: str = "usedPercent") -> float:
    if isinstance(value, bool):
        raise QuotaPayloadError(f"{field} is not a percentage")
    if isinstance(value, str):
        value = value.strip().removesuffix("%")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise QuotaPayloadError(f"{field} is not a percentage") from error
    if not math.isfinite(parsed) or parsed < 0 or parsed > 100:
        raise QuotaPayloadError(f"{field} must be between 0 and 100")
    return parsed


def _observed_at(payload: Mapping[str, Any], observed_at: datetime | None) -> datetime:
    if observed_at is not None:
        return _aware(observed_at)
    for key in ("observed_at", "observedAt", "timestamp"):
        if key in payload and payload[key] not in (None, ""):
            parsed = _parse_timestamp(payload[key], field=key)
            if parsed is not None:
                return parsed
    return datetime.now(timezone.utc)


def _auth_error(payload: Mapping[str, Any]) -> bool:
    """Recognize auth failures without retaining provider error text."""

    candidates: list[Any] = []
    for key in ("status", "type", "code", "error"):
        if key in payload:
            candidates.append(payload[key])
    for value in candidates:
        if isinstance(value, Mapping):
            candidates.extend(value.get(key) for key in ("status", "type", "code", "message"))
    text = " ".join(str(value).lower() for value in candidates if value is not None)
    return any(
        marker in text
        for marker in (
            "unauthor",
            "forbidden",
            "authentication",
            "invalid_api_key",
            "invalid api key",
            "401",
            "403",
            "auth_error",
        )
    )


def _mapping_at(payload: Mapping[str, Any], *keys: str) -> Mapping[str, Any]:
    current: Any = payload
    for key in keys:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(key)
    return current if isinstance(current, Mapping) else {}


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


@dataclass(frozen=True)
class QuotaSnapshot:
    """Normalized, secret-free quota observation."""

    provider: str
    used_percent: float | None
    reset_at: datetime | None
    observed_at: datetime
    auth_error: bool = False
    valid: bool = True

    def __post_init__(self) -> None:
        canonical = _canonical_provider(self.provider)
        object.__setattr__(self, "provider", canonical)
        object.__setattr__(self, "observed_at", _aware(self.observed_at))
        if self.reset_at is not None:
            object.__setattr__(self, "reset_at", _aware(self.reset_at))
        if self.used_percent is not None:
            if not math.isfinite(float(self.used_percent)) or not 0 <= self.used_percent <= 100:
                raise QuotaPayloadError("used_percent must be between 0 and 100")

    def to_cache(self) -> dict[str, Any]:
        """Return only fields that are safe to persist."""

        if not self.valid:
            raise QuotaPayloadError("invalid snapshots cannot be cached")
        return {
            "provider": self.provider,
            "used_percent": self.used_percent,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
            "observed_at": self.observed_at.isoformat(),
            "auth_error": self.auth_error,
        }

    @classmethod
    def from_cache(cls, value: Mapping[str, Any]) -> "QuotaSnapshot":
        if not isinstance(value, Mapping):
            raise QuotaCacheError("quota snapshot must be an object")
        provider = _canonical_provider(str(value.get("provider", "")))
        used = value.get("used_percent")
        if used is not None:
            used = _parse_percent(used, field="used_percent")
        observed = _parse_timestamp(value.get("observed_at"), field="observed_at")
        if observed is None:
            raise QuotaCacheError("quota snapshot has no observed_at")
        reset = _parse_timestamp(value.get("reset_at"), field="reset_at")
        auth_error = value.get("auth_error", False)
        if not isinstance(auth_error, bool):
            raise QuotaCacheError("auth_error must be boolean")
        if used is None and not auth_error:
            raise QuotaCacheError("quota snapshot has no usage value")
        return cls(provider, used, reset, observed, auth_error=auth_error)


def _unknown_snapshot(provider: str, observed_at: datetime) -> QuotaSnapshot:
    return QuotaSnapshot(
        provider,
        used_percent=None,
        reset_at=None,
        observed_at=observed_at,
        valid=False,
    )


def _normalize(
    provider: str,
    payload: Mapping[str, Any],
    *,
    observed_at: datetime | None,
    used: Any,
    reset: Any,
) -> QuotaSnapshot:
    canonical = _canonical_provider(provider)
    if not isinstance(payload, Mapping):
        raise QuotaPayloadError("quota payload must be a JSON object")
    # An invalid provider timestamp must not make the queue guess a value. It
    # becomes an unknown observation and is rejected by ingest. In particular,
    # do not continue with a ``now`` timestamp: doing so could turn an
    # explicitly malformed payload into a fresh, valid observation.
    if observed_at is not None:
        seen_at = _aware(observed_at)
    else:
        try:
            seen_at = _observed_at(payload, None)
        except QuotaPayloadError:
            return _unknown_snapshot(canonical, datetime.now(timezone.utc))
    if _auth_error(payload):
        return QuotaSnapshot(canonical, None, None, seen_at, auth_error=True)
    if used is None:
        return _unknown_snapshot(canonical, seen_at)
    try:
        parsed_used = _parse_percent(used)
        parsed_reset = _parse_timestamp(reset, field="reset")
    except QuotaPayloadError:
        return _unknown_snapshot(canonical, seen_at)
    return QuotaSnapshot(canonical, parsed_used, parsed_reset, seen_at)


def normalize_codex_snapshot(
    payload: Mapping[str, Any], *, observed_at: datetime | None = None
) -> QuotaSnapshot:
    """Normalize a Codex app-server snapshot using usedPercent/reset."""

    if not isinstance(payload, Mapping):
        raise QuotaPayloadError("Codex quota payload must be a JSON object")
    source = payload
    for candidate in (
        _mapping_at(payload, "rateLimits", "primary"),
        _mapping_at(payload, "rateLimits", "secondary"),
        _mapping_at(payload, "rate_limits", "primary"),
        _mapping_at(payload, "rate_limits", "secondary"),
        _mapping_at(payload, "account", "rateLimits", "primary"),
        _mapping_at(payload, "account", "rate_limits", "primary"),
        _mapping_at(payload, "rateLimits"),
        _mapping_at(payload, "rate_limits"),
        _mapping_at(payload, "account", "rateLimits"),
        _mapping_at(payload, "account", "rate_limits"),
        payload,
    ):
        if _first(candidate, "usedPercent", "used_percent", "usedPercentage") is not None:
            source = candidate
            break
    return _normalize(
        CODEX,
        payload,
        observed_at=observed_at,
        used=_first(source, "usedPercent", "used_percent", "usedPercentage"),
        reset=_first(source, "reset", "resetsAt", "resetAt", "reset_at"),
    )


def normalize_claude_statusline(
    payload: Mapping[str, Any], *, observed_at: datetime | None = None
) -> QuotaSnapshot:
    """Normalize Claude Code's rate_limits.five_hour statusline payload."""

    if not isinstance(payload, Mapping):
        raise QuotaPayloadError("Claude quota payload must be a JSON object")
    source = _mapping_at(payload, "rate_limits", "five_hour")
    if not source:
        source = _mapping_at(payload, "rateLimits", "fiveHour")
    return _normalize(
        CLAUDE,
        payload,
        observed_at=observed_at,
        used=_first(source, "used_percentage", "usedPercent", "used_percent"),
        reset=_first(source, "resets_at", "resetsAt", "reset_at"),
    )


def ttl_seconds(value: int | float | str | None = None) -> float:
    """Resolve a positive cache TTL, failing closed on bad configuration."""

    raw: Any = os.environ.get("AES_QUOTA_CACHE_TTL_SECONDS") if value is None else value
    if raw in (None, ""):
        return float(DEFAULT_TTL_SECONDS)
    try:
        parsed = float(raw)
    except (TypeError, ValueError) as error:
        raise QuotaConfigError("AES_QUOTA_CACHE_TTL_SECONDS must be positive") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise QuotaConfigError("AES_QUOTA_CACHE_TTL_SECONDS must be positive")
    return parsed


def cache_path(value: str | Path | None = None) -> Path:
    if value is not None:
        return Path(value)
    configured = os.environ.get("AES_QUOTA_CACHE_PATH")
    if configured:
        return Path(configured)
    # AES_QUOTA_CACHE_FILE is kept as a compatibility alias for older local
    # integrations; new deployments must use the explicit PATH/ROOT pair.
    return Path(os.environ.get("AES_QUOTA_CACHE_FILE", str(DEFAULT_CACHE_PATH)))


def cache_root(value: str | Path | None = None) -> Path:
    """Return the explicitly approved directory for quota cache files.

    A caller supplying a custom cache file without a root is treated as
    approving that file's *literal parent* for backwards compatibility.  The
    parent is still checked component-by-component for symlinks and ``..`` is
    rejected.  Deployments that accept paths from configuration should set
    ``AES_QUOTA_CACHE_ROOT`` (or pass ``root=`` to :class:`QuotaCache`) to pin
    the cache inside one directory.
    """

    if value is not None:
        return Path(value)
    configured = os.environ.get("AES_QUOTA_CACHE_ROOT")
    if configured:
        return Path(configured)
    return DEFAULT_CACHE_ROOT


def _zone() -> ZoneInfo:
    try:
        return ZoneInfo(DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError as error:  # pragma: no cover - platform packaging
        raise QuotaConfigError(f"timezone {DEFAULT_TIMEZONE} is unavailable") from error


def in_claude_work_window(now: datetime | None = None) -> bool:
    local = _now(now).astimezone(_zone())
    return local.weekday() < 5 and CLAUDE_WORKDAY_START <= local.time() < CLAUDE_WORKDAY_END


@dataclass(frozen=True)
class ProviderStatus:
    provider: str
    status: str
    used_percent: float | None
    reset_at: datetime | None
    observed_at: datetime | None
    fresh: bool
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _canonical_provider(self.provider))
        if self.status not in STATES:
            raise QuotaError(f"unsupported provider status: {self.status}")

    @property
    def available(self) -> bool:
        """Whether this provider may be selected for a new invocation."""

        return self.status == AVAILABLE

    def to_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "used_percent": self.used_percent,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "fresh": self.fresh,
            "reason": self.reason,
        }


def _age_fresh(snapshot: QuotaSnapshot | None, now: datetime, ttl: float) -> bool:
    if snapshot is None:
        return False
    age = (now - snapshot.observed_at).total_seconds()
    return 0 <= age <= ttl


def evaluate_provider(
    provider: str,
    snapshot: QuotaSnapshot | None,
    *,
    now: datetime | None = None,
    ttl: int | float | str | None = None,
) -> ProviderStatus:
    """Evaluate one snapshot at the current time under the fail-closed policy."""

    canonical = _canonical_provider(provider)
    current = _now(now)
    freshness_ttl = ttl_seconds(ttl)
    fresh = _age_fresh(snapshot, current, freshness_ttl)
    if snapshot is None or not snapshot.valid:
        if canonical == CLAUDE and in_claude_work_window(current):
            return ProviderStatus(
                canonical,
                POLICY_BLOCKED,
                None,
                None,
                None,
                False,
                "Claude quota snapshot is missing or malformed during work hours",
            )
        return ProviderStatus(
            canonical,
            UNKNOWN,
            None,
            None,
            None,
            False,
            "quota snapshot is missing or malformed",
        )
    if snapshot.auth_error:
        return ProviderStatus(
            canonical,
            AUTH_ERROR,
            None,
            None,
            snapshot.observed_at,
            fresh,
            "provider authentication failed",
        )
    if not fresh:
        if canonical == CLAUDE and in_claude_work_window(current):
            return ProviderStatus(
                canonical,
                POLICY_BLOCKED,
                snapshot.used_percent,
                snapshot.reset_at,
                snapshot.observed_at,
                False,
                "Claude quota snapshot is stale during work hours",
            )
        return ProviderStatus(
            canonical,
            UNKNOWN,
            snapshot.used_percent,
            snapshot.reset_at,
            snapshot.observed_at,
            False,
            "quota snapshot is stale",
        )
    assert snapshot.used_percent is not None
    if (
        canonical == CLAUDE
        and in_claude_work_window(current)
        and snapshot.used_percent >= CLAUDE_WORKDAY_LIMIT
    ):
        return ProviderStatus(
            canonical,
            POLICY_BLOCKED,
            snapshot.used_percent,
            snapshot.reset_at,
            snapshot.observed_at,
            True,
            "Claude five-hour usage is at or above the 70% work-hours reserve",
        )
    if snapshot.used_percent >= 100:
        return ProviderStatus(
            canonical,
            QUOTA_EXHAUSTED,
            snapshot.used_percent,
            snapshot.reset_at,
            snapshot.observed_at,
            True,
            "provider quota is exhausted",
        )
    return ProviderStatus(
        canonical,
        AVAILABLE,
        snapshot.used_percent,
        snapshot.reset_at,
        snapshot.observed_at,
        True,
        "quota snapshot is fresh and below its policy limit",
    )


def provider_availability(
    provider: str,
    snapshot: QuotaSnapshot | None = None,
    *,
    cache: "QuotaCache | None" = None,
    now: datetime | None = None,
    ttl: int | float | str | None = None,
) -> ProviderStatus:
    """Return the canonical availability state for one provider.

    This is the deliberately small adapter seam for an orchestrator: callers
    may use ``codex``/``claude`` (or their ``openai``/``anthropic`` aliases),
    while the result always exposes canonical ``provider`` and one of
    ``available``, ``quota_exhausted``, ``policy_blocked``, ``unknown`` or
    ``auth_error``.  Freshness and the Claude 70% work-hours reserve are
    applied by :func:`evaluate_provider`; no router import is required.
    """

    canonical = _canonical_provider(provider)
    if snapshot is not None and snapshot.provider != canonical:
        raise QuotaPayloadError("snapshot provider does not match provider")
    if snapshot is None and cache is not None:
        try:
            snapshot = cache.read().get(canonical)
        except QuotaCacheError as error:
            current = _now(now)
            if canonical == CLAUDE and in_claude_work_window(current):
                status = POLICY_BLOCKED
                reason = f"quota cache unavailable during Claude work hours: {error}"
            else:
                status = UNKNOWN
                reason = f"quota cache unavailable: {error}"
            return ProviderStatus(canonical, status, None, None, None, False, reason)
    return evaluate_provider(canonical, snapshot, now=now, ttl=ttl)


# Keep a discoverable alias for adapters that name this operation simply
# ``availability`` while retaining the explicit public API above.
get_provider_availability = provider_availability


@dataclass(frozen=True)
class RoutingDecision:
    selected_provider: str | None
    status: str
    reason: str
    providers: dict[str, ProviderStatus]

    def to_json(self) -> dict[str, Any]:
        return {
            "selected_provider": self.selected_provider,
            "status": self.status,
            "reason": self.reason,
            "providers": {name: state.to_json() for name, state in self.providers.items()},
        }


def select_provider(
    snapshots: Mapping[str, QuotaSnapshot],
    *,
    now: datetime | None = None,
    ttl: int | float | str | None = None,
) -> RoutingDecision:
    """Select Codex first and fall back to Claude only on Codex exhaustion."""

    normalized_snapshots: dict[str, QuotaSnapshot] = {}
    for provider, snapshot in snapshots.items():
        canonical = _canonical_provider(provider)
        if snapshot.provider != canonical:
            raise QuotaPayloadError("snapshot provider does not match its mapping key")
        normalized_snapshots[canonical] = snapshot
    states = {
        provider: evaluate_provider(
            provider,
            normalized_snapshots.get(provider),
            now=now,
            ttl=ttl,
        )
        for provider in PROVIDER_ORDER
    }
    codex_state = states[CODEX]
    if codex_state.status == AVAILABLE:
        return RoutingDecision(CODEX, AVAILABLE, "Codex is available and has priority", states)
    if codex_state.status == QUOTA_EXHAUSTED:
        claude_state = states[CLAUDE]
        if claude_state.status == AVAILABLE:
            return RoutingDecision(
                CLAUDE,
                AVAILABLE,
                "Codex quota is exhausted; Claude is available",
                states,
            )
        return RoutingDecision(
            None,
            claude_state.status,
            f"Codex quota is exhausted; {claude_state.reason}",
            states,
        )
    return RoutingDecision(
        None,
        codex_state.status,
        f"Codex is not dispatchable: {codex_state.reason}",
        states,
    )


class QuotaCache:
    """Atomic 0600 JSON cache of normalized provider observations.

    Cache files are confined to one approved directory.  Every path component
    below that directory is opened with ``O_NOFOLLOW`` and updates are guarded
    by an advisory lock in the same directory.  Consequently two concurrent
    provider ingests perform a serialized read/modify/write instead of losing
    the snapshot written by the other process.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        root: str | Path | None = None,
        ttl: int | float | str | None = None,
    ):
        raw_path = cache_path(path)
        explicit_path = path is not None or any(
            name in os.environ
            for name in ("AES_QUOTA_CACHE_PATH", "AES_QUOTA_CACHE_FILE")
        )
        configured_root = root is not None or bool(os.environ.get("AES_QUOTA_CACHE_ROOT"))
        if any(part == ".." for part in raw_path.parts):
            raise QuotaConfigError("quota cache path cannot contain '..'")

        # A relative path paired with an explicit root is relative to that
        # root.  For the legacy custom-path API, its literal parent is the
        # approved root; callers that need a wider/narrower policy should pass
        # root= explicitly.
        approved_root = cache_root(root)
        if not raw_path.is_absolute() and configured_root:
            target = approved_root / raw_path
        else:
            target = raw_path
        if root is None and not os.environ.get("AES_QUOTA_CACHE_ROOT") and explicit_path:
            approved_root = target.parent
        self.root = self._absolute_clean(approved_root, "cache root")
        self.path = self._absolute_clean(target, "cache path")
        # Keep both lexical paths (used by the descriptor-relative
        # O_NOFOLLOW operations below) and their real paths.  A symlink in an
        # ancestor can otherwise make a path that looks outside a checkout
        # resolve back into it.  The worker compares these canonical values
        # before it permits a cache for an execution.
        try:
            self.real_root = self.root.resolve(strict=False)
            self.real_path = self.path.resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise QuotaConfigError("quota cache path cannot be canonicalized") from error
        try:
            relative = self.path.relative_to(self.root)
        except ValueError as error:
            raise QuotaConfigError("quota cache path must stay inside its approved root") from error
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise QuotaConfigError("quota cache path must name a file below its approved root")
        self._parent_parts = relative.parts[:-1]
        self._target_name = relative.name
        self._lock_name = f".{self._target_name}.lock"
        self.ttl = ttl_seconds(ttl)

    @staticmethod
    def _absolute_clean(value: str | Path, label: str) -> Path:
        path = Path(value)
        if any(part == ".." for part in path.parts):
            raise QuotaConfigError(f"{label} cannot contain '..'")
        if not path.is_absolute():
            path = Path.cwd() / path
        if path == Path(path.anchor):
            raise QuotaConfigError(f"{label} may not be a filesystem root")
        return path

    @staticmethod
    def _directory_flags() -> int:
        return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)

    @staticmethod
    def _file_flags() -> int:
        return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)

    def _ensure_root(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            root_stat = self.root.lstat()
            if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
                raise QuotaCacheError("quota cache root must be a real directory")
            os.chmod(self.root, 0o700)
        except QuotaCacheError:
            raise
        except OSError as error:
            raise QuotaCacheError(f"cannot prepare quota cache root: {error}") from error

    def _open_directory(self, *, create: bool) -> int | None:
        """Open the approved parent directory without following symlinks."""

        if create:
            self._ensure_root()
        try:
            directory_fd = os.open(self.root, self._directory_flags())
        except FileNotFoundError:
            return None
        try:
            for part in self._parent_parts:
                try:
                    next_fd = os.open(part, self._directory_flags(), dir_fd=directory_fd)
                except FileNotFoundError:
                    if not create:
                        os.close(directory_fd)
                        return None
                    try:
                        os.mkdir(part, 0o700, dir_fd=directory_fd)
                    except FileExistsError:
                        # Another writer may have created this component
                        # between our open and mkdir.  Re-open it with
                        # O_NOFOLLOW so a replacement symlink is rejected.
                        pass
                    next_fd = os.open(part, self._directory_flags(), dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            return directory_fd
        except (OSError, ValueError) as error:
            try:
                os.close(directory_fd)
            except OSError:
                pass
            if isinstance(error, QuotaCacheError):
                raise
            raise QuotaCacheError(f"cannot open quota cache directory: {error}") from error

    @staticmethod
    def _decode_payload(payload: Any) -> dict[str, QuotaSnapshot]:
        if not isinstance(payload, Mapping) or payload.get("schema") != SCHEMA:
            raise QuotaCacheError("unsupported or malformed quota cache schema")
        raw_snapshots = payload.get("snapshots")
        if not isinstance(raw_snapshots, Mapping):
            raise QuotaCacheError("quota cache snapshots must be an object")
        result: dict[str, QuotaSnapshot] = {}
        try:
            for provider, value in raw_snapshots.items():
                canonical = _canonical_provider(str(provider))
                snapshot = QuotaSnapshot.from_cache(value)
                if snapshot.provider != canonical:
                    raise QuotaCacheError("quota cache provider key does not match snapshot")
                result[canonical] = snapshot
        except (QuotaError, TypeError) as error:
            if isinstance(error, QuotaCacheError):
                raise
            raise QuotaCacheError(f"malformed quota snapshot: {error}") from error
        return result

    def _read_from_directory(self, directory_fd: int) -> dict[str, QuotaSnapshot]:
        try:
            fd = os.open(self._target_name, self._file_flags(), dir_fd=directory_fd)
        except FileNotFoundError:
            return {}
        except OSError as error:
            raise QuotaCacheError(f"cannot open quota cache: {error}") from error
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise QuotaCacheError("quota cache must be a regular file")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise QuotaCacheError("quota cache permissions must be 0600")
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                payload = json.load(handle)
        except QuotaCacheError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise QuotaCacheError(f"cannot read quota cache: {error}") from error
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        return self._decode_payload(payload)

    @contextmanager
    def _locked_directory(self) -> Iterator[int]:
        directory_fd = self._open_directory(create=True)
        assert directory_fd is not None
        lock_fd = -1
        try:
            # macOS can transiently report ENOENT when two processes create
            # the same relative O_CREAT entry through independent directory
            # descriptors.  Retry the create/open; once one contender has
            # created the lock, the next attempt opens that same inode.
            lock_flags = (
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            for attempt in range(20):
                try:
                    lock_fd = os.open(
                        self._lock_name,
                        lock_flags,
                        0o600,
                        dir_fd=directory_fd,
                    )
                    break
                except FileNotFoundError:
                    if attempt == 19:
                        raise
                    time_module.sleep(0.001)
            if stat.S_IMODE(os.fstat(lock_fd).st_mode) != 0o600:
                os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield directory_fd
        except OSError as error:
            raise QuotaCacheError(f"cannot lock quota cache: {error}") from error
        finally:
            if lock_fd >= 0:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(lock_fd)
            os.close(directory_fd)

    def read(self) -> dict[str, QuotaSnapshot]:
        directory_fd = self._open_directory(create=False)
        if directory_fd is None:
            return {}
        try:
            return self._read_from_directory(directory_fd)
        finally:
            os.close(directory_fd)

    def write(self, snapshot: QuotaSnapshot) -> None:
        if not snapshot.valid:
            raise QuotaPayloadError("invalid snapshots cannot be cached")
        # The lock covers both read and replace.  Without this critical
        # section two provider ingests can each read the old file and one can
        # silently discard the other's fresh snapshot.
        with self._locked_directory() as directory_fd:
            snapshots = self._read_from_directory(directory_fd)
            snapshots[snapshot.provider] = snapshot
            self._write_all_unlocked(directory_fd, snapshots)

    def _write_all(self, snapshots: Mapping[str, QuotaSnapshot]) -> None:
        with self._locked_directory() as directory_fd:
            self._write_all_unlocked(directory_fd, snapshots)

    def _write_all_unlocked(
        self, directory_fd: int, snapshots: Mapping[str, QuotaSnapshot]
    ) -> None:
        temporary_name: str | None = None
        fd = -1
        try:
            for _ in range(10):
                candidate = f".{self._target_name}.{secrets.token_hex(12)}.tmp"
                try:
                    fd = os.open(
                        candidate,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                        dir_fd=directory_fd,
                    )
                    temporary_name = candidate
                    break
                except FileExistsError:
                    continue
            if fd < 0 or temporary_name is None:
                raise QuotaCacheError("cannot allocate a temporary quota cache file")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                json.dump(
                    {
                        "schema": SCHEMA,
                        "snapshots": {
                            name: value.to_cache() for name, value in snapshots.items()
                        },
                    },
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(
                temporary_name,
                self._target_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            temporary_name = None
            os.fsync(directory_fd)
        except QuotaError:
            raise
        except OSError as error:
            raise QuotaCacheError(f"cannot write quota cache: {error}") from error
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except OSError:
                    pass

    def inspect(self, *, now: datetime | None = None) -> RoutingDecision:
        try:
            snapshots = self.read()
        except QuotaCacheError as error:
            states = {
                provider: evaluate_provider(provider, None, now=now, ttl=self.ttl)
                for provider in PROVIDER_ORDER
            }
            return RoutingDecision(None, UNKNOWN, f"quota cache unavailable: {error}", states)
        return select_provider(snapshots, now=now, ttl=self.ttl)


def _ingest(
    provider: str,
    payload: Mapping[str, Any],
    cache: QuotaCache,
    observed_at: datetime | None,
) -> QuotaSnapshot:
    canonical = _canonical_provider(provider)
    snapshot = (
        normalize_codex_snapshot(payload, observed_at=observed_at)
        if canonical == CODEX
        else normalize_claude_statusline(payload, observed_at=observed_at)
    )
    if not snapshot.valid:
        raise QuotaPayloadError("quota payload is malformed or does not expose usage")
    cache.write(snapshot)
    return snapshot


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    ingest = subcommands.add_parser("ingest", help="normalize JSON from stdin into the cache")
    ingest.add_argument(
        "--provider",
        required=True,
        choices=("codex", "openai", "claude", "anthropic"),
    )
    ingest.add_argument("--cache", type=Path)
    ingest.add_argument("--cache-root", type=Path)
    ingest.add_argument("--observed-at")
    inspect_parser = subcommands.add_parser(
        "inspect",
        help="emit the current routing decision as JSON",
    )
    inspect_parser.add_argument("--cache", type=Path)
    inspect_parser.add_argument("--cache-root", type=Path)
    inspect_parser.add_argument("--at", dest="now")
    return parser


def _cli_timestamp(raw: str | None, field: str) -> datetime | None:
    return _parse_timestamp(raw, field=field) if raw else None


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "ingest":
            payload = json.load(sys.stdin)
            snapshot = _ingest(
                args.provider,
                payload,
                QuotaCache(args.cache, root=args.cache_root),
                _cli_timestamp(args.observed_at, "observed_at"),
            )
            print(json.dumps(snapshot.to_cache(), sort_keys=True))
            return 0
        decision = QuotaCache(args.cache, root=args.cache_root).inspect(
            now=_cli_timestamp(args.now, "at")
        )
        print(json.dumps(decision.to_json(), sort_keys=True))
        return 0
    except (QuotaError, OSError, json.JSONDecodeError, TypeError) as error:
        print(json.dumps({"status": UNKNOWN, "error": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
