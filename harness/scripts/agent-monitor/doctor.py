"""Verify that the monitor is actually connected. Silence means healthy.

It exists for a measured reason: the monitor is fail-open and quiet when it
finds nothing, so "off" and "saw nothing" produce the same output—none. The
first hook version remained inert for days after the repository moved, without
saying so. This is the same class of problem as green CI false positives (ADR
0010): a check that does not run looks like a passing check.

It runs at SessionStart and speaks only when something is broken, so keeping it
enabled adds zero noisy lines to a healthy session. `--verbose` prints status
even when everything is fine for manual inspection.

Usage: doctor.py [--verbose] [<repo-root>]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HOOK_NAME = "agent-monitor"
MODULES = ("hook.py", "detector.py", "ledger.py", "probe.py", "posttooluse.sh")
# Deliberately duplicated from ledger.monitor_dir: doctor must work when
# modules are missing, which is exactly the case it diagnoses, so it cannot
# import them.
LEDGER_RELATIVE = Path(".agent") / "monitor" / "events.jsonl"


def _monitor_dir(root: Path) -> Path:
    return root / "harness" / "scripts" / HOOK_NAME


def _registered(root: Path) -> bool:
    """Return whether .claude/settings.json connects monitor to PostToolUse."""
    settings = root / ".claude" / "settings.json"
    if not settings.is_file():
        return False
    try:
        hooks = json.loads(settings.read_text(encoding="utf-8"))["hooks"]
        entries = hooks["PostToolUse"]
    except (json.JSONDecodeError, OSError, KeyError, TypeError):
        return False
    return HOOK_NAME in json.dumps(entries)


def problems(root: Path) -> list[str]:
    """List why the monitor would not work, or return an empty list."""
    found = []
    missing = [m for m in MODULES if not (_monitor_dir(root) / m).is_file()]
    if missing:
        found.append(
            f"missing modules in harness/scripts/{HOOK_NAME}/: {', '.join(missing)}"
            " — run harness/scripts/aes-sync.sh"
        )
    if not _registered(root):
        found.append(
            "no PostToolUse hook registered in .claude/settings.json"
            " — run harness/scripts/aes-sync.sh"
        )
    return found


def summary(root: Path) -> str:
    """Return a readable status line: event/session counts and latest time."""
    ledger_path = root / LEDGER_RELATIVE
    if not ledger_path.is_file():
        return "monitor connected, ledger is still empty."
    sessions, count, last = set(), 0, 0.0
    try:
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            count += 1
            sessions.add(row.get("session_id"))
            last = max(last, float(row.get("ts", 0)))
    except OSError as error:
        return f"monitor connected, ledger unreadable: {error}"
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if last else "never"
    return (
        f"monitor connected: {count} events, {len(sessions)} sessions, "
        f"latest {when}."
    )


def main(argv: list[str]) -> int:
    verbose = "--verbose" in argv
    positional = [a for a in argv[1:] if not a.startswith("-")]
    root = Path(positional[0] if positional else ".").resolve()

    found = problems(root)
    if found:
        print("monitor NOT active: " + "; ".join(found))
    elif verbose:
        print(summary(root))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except SystemExit:
        raise
    except BaseException:
        raise SystemExit(0)  # a broken diagnosis must not block session startup
