"""PostToolUse hook: record a tool call and warn the agent when needed.

Read the Claude Code hook payload from stdin and write an `additionalContext`
object to stdout: the warning reaches the agent itself, which can correct
course, not only the user watching.

Never block (no exit 2 and no `decision: block`); every exception becomes a
clean, silent exit. A monitor that incorrectly stops work is worse than the
problem it solves: ADR 0011.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ledger  # noqa: E402
import probe  # noqa: E402
from detector import (  # noqa: E402
    detect,
    is_noop_wait,
    is_user_rejection,
    make_event,
    payload_for,
)

ADVICE = (
    "monitor: {detail}.{verdict} "
    "If you are deliberately iterating or waiting for a job, ignore this warning."
)


def repo_root(cwd: str) -> Path:
    """Repository root containing `cwd`, or `cwd` itself if not a repository."""
    path = Path(cwd)
    for candidate in [path, *path.parents]:
        if (candidate / ".git").exists():
            return candidate
    return path


def _outcome(tool_response) -> tuple[str, bool]:
    if isinstance(tool_response, str):
        return tool_response, False
    if isinstance(tool_response, dict):
        content = tool_response.get("content", tool_response)
        text = content if isinstance(content, str) else json.dumps(content)[:600]
        return text, bool(tool_response.get("is_error"))
    return json.dumps(tool_response)[:600], False


def main() -> int:
    payload = json.loads(sys.stdin.read())
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id", "?")
    root = repo_root(payload.get("cwd") or ".")

    action = payload_for(tool_name, tool_input)
    if not action or is_noop_wait(action):
        return 0

    outcome, failed = _outcome(payload.get("tool_response"))
    outcome = outcome[:600]

    event = make_event(
        session_id, "claude", tool_name.lower(), action,
        outcome=outcome, repo_root=str(root),
    )
    ledger.append(root, event)
    if failed and not is_user_rejection(outcome):
        error_event = make_event(
            session_id, "claude", "error", outcome,
            is_error=True, repo_root=str(root),
        )
        ledger.append(root, error_event)
        event = error_event

    events = ledger.tail(root)
    signal = detect(events, event)
    if signal is None:
        return 0

    verdict_text = ""
    if ledger.may_probe(root, session_id, signal.fingerprint):
        ledger.record_probe(root, session_id, signal.fingerprint)
        verdict = probe.ask(signal.detail, events)
        if verdict:
            verdict_text = (
                f" Verdict: {verdict['verdict']} — {verdict.get('reason', '')}. "
                f"Try instead: {verdict.get('suggestion', '')}."
            )
        # "converging" is a green light: level 1 raised a false alarm.
        if verdict and verdict["verdict"] == "converging":
            return 0

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": ADVICE.format(
                        detail=signal.detail, verdict=verdict_text
                    ),
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        raise SystemExit(0)  # fail-open: never block the agent's work
