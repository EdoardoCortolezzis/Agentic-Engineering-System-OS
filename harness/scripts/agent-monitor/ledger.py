"""Persist level-2 event ledger and budget.

Separate from detector.py to keep that module pure: all I/O lives here. The
ledger is per repository (`<repo>/.agent/monitor/events.jsonl`), not per
session, because R4 compares different sessions.

Appends are single write() calls of one line in append mode: on POSIX this is
enough to avoid interleaved lines from concurrent hooks without a lock. A lost
or truncated line can at worst reduce monitor sensitivity, never agent work.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from detector import Event

MAX_LEDGER = 2000
MAX_BYTES = 2 * 1024 * 1024
# The read tail matches ledger retention: R4 says "in N distinct sessions of
# this repository", which would be false at full retention with a shorter
# window than MAX_LEDGER. R1 and R3 still inspect only the last WINDOW events.
TAIL = MAX_LEDGER


def monitor_dir(repo_root: str | Path) -> Path:
    return Path(repo_root) / ".agent" / "monitor"


def _ensure_dir(path: Path) -> None:
    """Create the monitor directory and hide it from Git.

    The local .gitignore avoids modifying every consumer repository's ignore
    file: the ledger is local working state and must never appear as untracked
    in `git status`, where it could become noise an agent eventually commits.
    """
    path.mkdir(parents=True, exist_ok=True)
    marker = path / ".gitignore"
    if not marker.exists():
        marker.write_text("*\n", encoding="utf-8")


def _events_path(repo_root: str | Path) -> Path:
    return monitor_dir(repo_root) / "events.jsonl"


def append(repo_root: str | Path, event: Event) -> None:
    path = _events_path(repo_root)
    _ensure_dir(path.parent)
    row = {
        "ts": time.time(),
        "session_id": event.session_id,
        "source": event.source,
        "kind": event.kind,
        "fingerprint": event.fingerprint,
        "cycle": event.cycle,
        "is_error": event.is_error,
        "text": event.text,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    if path.stat().st_size > MAX_BYTES:
        _rotate(path)


def _rotate(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()[-MAX_LEDGER:]
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def tail(repo_root: str | Path, limit: int = TAIL) -> list[Event]:
    """Return the repository's latest events, oldest to newest."""
    path = _events_path(repo_root)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # skip a line truncated by a concurrent write
        events.append(
            Event(
                session_id=row["session_id"],
                source=row["source"],
                kind=row["kind"],
                fingerprint=row["fingerprint"],
                cycle=row.get("cycle", row["fingerprint"]),
                is_error=row["is_error"],
                text=row.get("text", ""),
            )
        )
    return events


# --- level-2 budget --------------------------------------------------------
# When using a subscription, quota—not dollars—is scarce: a monitor that
# consumes it is worse than the problem it solves (ADR 0013).

PROBE_COOLDOWN_S = 600
PROBE_MAX_PER_SESSION = 5
# The budget is indexed by session and would grow forever without pruning; it
# is read on every signal, so keep the file short.
BUDGET_MAX_SESSIONS = 50


def _budget_path(repo_root: str | Path) -> Path:
    return monitor_dir(repo_root) / "probe-budget.json"


def _load_budget(repo_root: str | Path) -> dict:
    path = _budget_path(repo_root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def may_probe(repo_root: str | Path, session_id: str, signal_fp: str) -> bool:
    """Return whether level 2 may be invoked now for this signal."""
    entry = _load_budget(repo_root).get(session_id)
    if not entry:
        return True
    if entry.get("calls", 0) >= PROBE_MAX_PER_SESSION:
        return False
    if signal_fp in entry.get("seen", []):
        return False  # already reported; repeating it adds nothing
    return (time.time() - entry.get("last", 0)) >= PROBE_COOLDOWN_S


def record_probe(repo_root: str | Path, session_id: str, signal_fp: str) -> None:
    path = _budget_path(repo_root)
    _ensure_dir(path.parent)
    budget = _load_budget(repo_root)
    entry = budget.setdefault(session_id, {"calls": 0, "seen": [], "last": 0})
    entry["calls"] = entry.get("calls", 0) + 1
    entry["last"] = time.time()
    seen = entry.setdefault("seen", [])
    if signal_fp not in seen:
        seen.append(signal_fp)
    if len(budget) > BUDGET_MAX_SESSIONS:
        recent = sorted(budget.items(), key=lambda kv: kv[1].get("last", 0))
        for stale, _ in recent[: len(budget) - BUDGET_MAX_SESSIONS]:
            del budget[stale]
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(budget), encoding="utf-8")
    os.replace(tmp, path)
