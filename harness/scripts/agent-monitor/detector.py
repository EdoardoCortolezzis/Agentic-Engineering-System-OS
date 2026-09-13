"""Deterministic core of the reasoning monitor (level 1).

No I/O, network, or model calls: this module takes a list of normalized events
and decides whether the agent is stuck in a loop, making it testable without
real sessions (see evals/agent-monitor/test_detector.py). This is the same
purity choice used by scripts/codex-orchestrate/orchestrate.py: I/O belongs in
ledger.py and in the shims that invoke this module.

Four rules, all anchored to the event just recorded (`current`): a rule does
not fire because the window contains old repetitions, only when the agent is
repeating *now*. Without this anchor the signal would repeat on every later
tool call until the window emptied.

R4 is the only cross-session rule, which is why the ledger is per repository
rather than per session: repeated defects across independent sessions matter
more than a single-session loop.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass

# Window and thresholds. Tune them with replay.py and recorded fixtures.
WINDOW = 30
REPEAT_ACTION_THRESHOLD = 4
REPEAT_ERROR_THRESHOLD = 3
PING_PONG_CYCLES = 3
RECURRING_DEFECT_SESSIONS = 3

MAX_TEXT = 400

# Replacement order matters: timestamps and hex values come before bare
# numbers, otherwise "2026-09-01" would become "<N>-<N>-<N>" and distinct
# dates would still collapse, while a hexadecimal hash would be split.
_TS = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?")
# Only remove the machine-specific prefix: preserve the path suffix because it
# distinguishes one Write from another. Replacing the entire path collapsed
# four Writes to different files into one repeated action (a false positive
# measured by replaying real transcripts).
_HOME_PREFIX = re.compile(r"/(?:Users|home)/[^/\s]+/")
# CI runners commonly mount the checkout below a job-specific directory such
# as `/srv/workspace` or `/srv/ci`. Keep the project-relative suffix (it is
# what distinguishes two files) while removing that volatile component.
_CI_PREFIX = re.compile(r"/srv/[^/\s]+/")
_HEX = re.compile(r"\b[0-9a-fA-F]{7,}\b")
_NUM = re.compile(r"\b\d+\b")
# Durations vary on every run and say nothing about progress ("in 3.42s").
_DUR = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:ms|s|sec|seconds|min|m)\b")
_WS = re.compile(r"\s+")

# A typical Python/SQLite exception: "sqlite3.IntegrityError", "ValueError".
_EXC = re.compile(r"\b(?:[a-z_][\w.]*\.)?([A-Z]\w*(?:Error|Exception))\b")

# A user rejection arrives as a tool_result with is_error=True, but it is not a
# defect: it is a decision. Counting it made "the user said no" the most
# frequent recurring defect in the ledger.
_REJECTION = re.compile(
    r"user doesn't want|tool use was rejected|user rejected|"
    r"requested permissions? .* but you haven't granted",
    re.IGNORECASE,
)


def is_user_rejection(text: str) -> bool:
    """Return whether the outcome is a user rejection, not an agent error."""
    return bool(_REJECTION.search(text))


# Waiting is not being stuck. It is the class of false positives that caused
# OpenHands to kill agents waiting on long jobs (issue #5355): pure wait
# primitives do not enter this ledger, while polling that observes unchanged
# output remains reportable because "the job has not advanced in four checks"
# is true information.
_NOOP_WAIT = re.compile(r"^\s*(?::|true|sleep\s+[\d.]+)\s*;?\s*$")


def is_noop_wait(payload: str) -> bool:
    """Return whether the action is a pure wait with no information."""
    return bool(_NOOP_WAIT.match(payload))


@dataclass(frozen=True)
class Event:
    """An observed action already reduced to a comparable form."""

    session_id: str
    source: str  # "claude" | "codex"
    kind: str  # "bash" | "edit" | "phase" | "question" | "error"
    fingerprint: str  # identity of the action (or error signature)
    cycle: str  # identity of the pair (action, observed outcome)
    is_error: bool
    text: str


@dataclass(frozen=True)
class Signal:
    rule: str
    detail: str
    count: int
    fingerprint: str


def normalize(
    text: str, repo_root: str | None = None, collapse_numbers: bool = False
) -> str:
    """Reduce text to a stable form across different executions.

    Absolute paths, timestamps, and hashes vary on every run while describing
    the same action, so they are always removed.

    Numbers are not removed unless requested: in an error message the year
    1942 is incidental and should collapse because the defect is the same, but
    in `git commit -m 'step 7'` the number *is* the work, and collapsing it
    makes twenty distinct commits look like one repeated command (the false
    positive caught by `test_sessione_sana_non_produce_segnali`).
    """
    s = text.strip()
    if repo_root:
        s = s.replace(repo_root, ".")
    s = _TS.sub("<TS>", s)
    s = _DUR.sub("<DUR>", s)
    s = _HOME_PREFIX.sub("~/", s)
    s = _CI_PREFIX.sub("/srv/", s)
    s = _HEX.sub("<HEX>", s)
    if collapse_numbers:
        s = _NUM.sub("<N>", s)
    return _WS.sub(" ", s).strip()


def error_signature(text: str, repo_root: str | None = None) -> str:
    """Error skeleton: exception type plus the start of the message.

    Truncation keeps the signature stable when the tail of the message
    contains variable details (for example, UNIQUE constraint columns listed
    in a different order).
    """
    norm = normalize(text, repo_root, collapse_numbers=True)
    match = _EXC.search(norm)
    head = match.group(1) if match else ""
    return f"{head}|{norm[:120]}"


def fingerprint(kind: str, payload: str, repo_root: str | None = None) -> str:
    """Comparable identity of an action or an error."""
    basis = (
        error_signature(payload, repo_root)
        if kind == "error"
        else normalize(payload, repo_root)
    )
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]
    return f"{kind}:{digest}"


def make_event(
    session_id: str,
    source: str,
    kind: str,
    payload: str,
    outcome: str = "",
    is_error: bool = False,
    repo_root: str | None = None,
) -> Event:
    """Build a comparable event.

    `outcome` is the observed outcome (truncated stdout/stderr or the error
    message): it distinguishes "I reran the command and got a different
    result" from "I reran the command and it failed the same way".

    Numbers are preserved in the outcome because they often measure progress
    ("9 failed" -> "3 failed" -> "0 failed"): collapsing them would make a
    recovering suite indistinguishable from a stalled one.
    """
    fp_kind = "error" if is_error else kind
    action_fp = fingerprint(fp_kind, payload, repo_root)
    outcome_norm = normalize(outcome, repo_root)
    cycle_basis = f"{action_fp}|{outcome_norm[:200]}"
    cycle = hashlib.sha256(cycle_basis.encode("utf-8")).hexdigest()[:12]
    return Event(
        session_id=session_id,
        source=source,
        kind=kind,
        fingerprint=action_fp,
        cycle=cycle,
        is_error=is_error,
        text=(payload if not outcome else f"{payload}\n-> {outcome}")[:MAX_TEXT],
    )


def payload_for(tool_name: str, tool_input: dict) -> str:
    """Identify a tool call by its command or the file it touched.

    Preserve the file path: it distinguishes one Write from another (see the
    note on _HOME_PREFIX).
    """
    if tool_name == "Bash":
        return tool_input.get("command", "")
    if tool_name in ("Edit", "Write", "Read", "NotebookEdit"):
        target = tool_input.get("file_path", "")
        old = str(tool_input.get("old_string", ""))[:80]
        return f"{tool_name} {target} {old}".strip()
    return f"{tool_name} {json.dumps(tool_input, ensure_ascii=False, sort_keys=True)[:120]}"


def _ping_pong(window: list[Event]) -> bool:
    """Recognize an A/B/A/B/A/B alternation ending at the current event."""
    needed = PING_PONG_CYCLES * 2
    if len(window) < needed:
        return False
    tail = [e.fingerprint for e in window[-needed:]]
    even = set(tail[0::2])
    odd = set(tail[1::2])
    return len(even) == 1 and len(odd) == 1 and even != odd


def detect(events: list[Event], current: Event) -> Signal | None:
    """Decide whether `current` closes a stall pattern.

    `events` is the repository ledger tail (longer than the window because R4
    looks across sessions) and must include `current` as its last element.
    """
    window = events[-WINDOW:]

    # R4 first: it is the most costly to miss. A defect recurring across
    # sessions is not an iteration; it is a wrong diagnosis.
    if current.is_error:
        sessions = {
            e.session_id for e in events if e.fingerprint == current.fingerprint
        }
        if len(sessions) >= RECURRING_DEFECT_SESSIONS:
            return Signal(
                rule="recurring_defect",
                detail=(
                    f"the same error already appeared in {len(sessions)} "
                    "distinct sessions of this repository: previous fixes "
                    "did not resolve the cause"
                ),
                count=len(sessions),
                fingerprint=current.fingerprint,
            )

        errors = sum(
            1 for e in window if e.is_error and e.fingerprint == current.fingerprint
        )
        if errors >= REPEAT_ERROR_THRESHOLD:
            return Signal(
                rule="repeat_error",
                detail=f"same error repeated {errors} times in this session",
                count=errors,
                fingerprint=current.fingerprint,
            )

    if _ping_pong(window) and window[-1].fingerprint == current.fingerprint:
        return Signal(
            rule="ping_pong",
            detail=(
                f"alternation between two actions for {PING_PONG_CYCLES} cycles: "
                "each change appears to undo the previous one"
            ),
            count=PING_PONG_CYCLES,
            fingerprint=current.fingerprint,
        )

    # Use the (action, outcome) pair, not the action alone: rerunning a command
    # that now produces different output is progress, not a loop.
    repeats = Counter(e.cycle for e in window)[current.cycle]
    if repeats >= REPEAT_ACTION_THRESHOLD:
        return Signal(
            rule="repeat_action",
            detail=f"same action with the same outcome {repeats} times",
            count=repeats,
            fingerprint=current.fingerprint,
        )

    return None
