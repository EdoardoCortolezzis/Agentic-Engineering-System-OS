"""Parsing and validation of the strict ``aes:dispatch`` issue block."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import sys
from typing import Iterable


OPEN_MARKER = "<!-- aes:dispatch -->"
CLOSE_MARKER = "<!-- /aes:dispatch -->"
KNOWN_KEYS = frozenset({"dispatch", "priority", "not_before", "budget"})
DISPATCH_VALUES = frozenset({"auto", "manual", "disabled"})
PRIORITIES = {"critical": 0, "high": 1, "normal": 2, "low": 3}
_UTC_RE = re.compile(r"(?:Z|\+00:00)$")
_MAX_BUDGET_DIGITS = 4300


class DispatchError(ValueError):
    """Indicates malformed or ambiguous queue metadata."""


@dataclass(frozen=True)
class DispatchMetadata:
    """Queue metadata independent from GitHub or a worker provider."""

    dispatch: str
    priority: str
    not_before: datetime | None
    budget: int | None

    @property
    def priority_rank(self) -> int:
        return PRIORITIES[self.priority]


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    if not _UTC_RE.search(value):
        raise DispatchError("not_before must be an RFC 3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise DispatchError("not_before is not a valid RFC 3339 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise DispatchError("not_before must use UTC")
    return parsed.astimezone(timezone.utc)


def _parse_budget(raw_budget: str) -> int | None:
    """Parse a budget without allowing integer-conversion errors to escape."""

    if not raw_budget:
        return None
    if not raw_budget.isdigit():
        raise DispatchError("budget must be a positive integer")
    runtime_limit = getattr(sys, "get_int_max_str_digits", lambda: 0)()
    digit_limit = min(_MAX_BUDGET_DIGITS, runtime_limit) if runtime_limit else _MAX_BUDGET_DIGITS
    if len(raw_budget) > digit_limit:
        raise DispatchError("budget exceeds the supported digit limit")
    try:
        budget = int(raw_budget)
    except (OverflowError, ValueError) as error:
        raise DispatchError("budget must be a positive integer") from error
    if budget <= 0:
        raise DispatchError("budget must be a positive integer")
    return budget


def parse(body: str) -> DispatchMetadata:
    """Parse exactly one flat dispatch block and reject unknown/duplicate keys."""

    openings = body.count(OPEN_MARKER)
    closings = body.count(CLOSE_MARKER)
    if openings != 1 or closings != 1:
        raise DispatchError("dispatch block must occur exactly once")
    start = body.index(OPEN_MARKER) + len(OPEN_MARKER)
    try:
        end = body.index(CLOSE_MARKER, start)
    except ValueError as error:
        raise DispatchError("dispatch closing marker must follow opening marker") from error
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(body[start:end].splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        if ":" not in line:
            raise DispatchError(f"invalid dispatch line {line_number}: missing ':'")
        key, value = (part.strip() for part in line.split(":", 1))
        if key not in KNOWN_KEYS:
            raise DispatchError(f"unknown dispatch key: {key}")
        if key in values:
            raise DispatchError(f"duplicate dispatch key: {key}")
        values[key] = value
    missing = sorted(KNOWN_KEYS - values.keys())
    if missing:
        raise DispatchError(f"missing dispatch keys: {', '.join(missing)}")
    dispatch = values["dispatch"].lower()
    priority = values["priority"].lower()
    if dispatch not in DISPATCH_VALUES:
        raise DispatchError(f"unknown dispatch value: {dispatch}")
    if priority not in PRIORITIES:
        raise DispatchError(f"unknown priority value: {priority}")
    not_before = _parse_timestamp(values["not_before"])
    budget = _parse_budget(values["budget"])
    return DispatchMetadata(dispatch, priority, not_before, budget)


def validate(metadata: DispatchMetadata, labels: Iterable[str]) -> tuple[str, ...]:
    """Return all reasons why metadata cannot enter the autonomous queue."""

    label_list = tuple(labels)
    reasons: list[str] = []
    if metadata.dispatch == "auto":
        if metadata.budget is None:
            reasons.append("budget is required for dispatch: auto")
        role_labels = [label for label in label_list if label.startswith("role:")]
        if len(role_labels) != 1 or not role_labels[0].removeprefix("role:"):
            reasons.append("exactly one role:* label is required")
        autonomy_labels = [label for label in label_list if label.startswith("autonomy:")]
        if autonomy_labels != ["autonomy:worker"]:
            reasons.append("exactly one autonomy:worker label is required")
    return tuple(reasons)


def parse_and_validate(body: str, labels: Iterable[str]) -> tuple[DispatchMetadata, tuple[str, ...]]:
    """Convenience function used by dispatcher adapters."""

    metadata = parse(body)
    return metadata, validate(metadata, labels)
