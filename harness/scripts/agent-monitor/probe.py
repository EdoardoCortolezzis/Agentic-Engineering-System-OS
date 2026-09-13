"""Level 2: semantic verdict for a level-1 signal.

Invoked only after the deterministic detector finds something, never on an
interval: level 1 costs nothing and filters, while level 2 costs quota and
only decides whether repetition is a problem or legitimate work.

Runs on the managed Anthropic subscription through the headless `claude` CLI.
`ANTHROPIC_API_KEY` is deliberately removed from the environment: if present,
the CLI would bill usage instead of using the subscription.

`disableAllHooks` is not an optimization but a necessity: without it, the
nested call would rerun global hooks—including this monitor recursively—and
the current configuration blocked it on a plugin hook after 81 seconds.
Disabling them returns a response in about 6 seconds. For the same reason MCP
servers are not loaded: the monitor reads a trace and needs no tools.

Any problem (missing CLI, timeout, exhausted quota, malformed JSON) returns
None. The monitor must never be the reason a task stops: ADR 0011 documents
what happens when a misconfigured guardrail blocks valid work.
"""

from __future__ import annotations

import json
import os
import subprocess

MODEL = "claude-haiku-4-5-20251001"
TIMEOUT_S = 25
MAX_ACTIONS = 12

PROMPT = """You observe a coding agent trace. A deterministic detector flagged: {detail}

Latest agent actions (oldest to newest):
{actions}

Is the agent making progress or stuck? Reply ONLY with one JSON object on one \
line, with no surrounding text:
{{"verdict": "converging"|"looping"|"drifting", "reason": "<max 20 words>", \
"suggestion": "<max 25 words, what to try differently>"}}

"converging" if actions show real progress (even slowly) or legitimate job \
waiting. "looping" if it repeats the same path without changing hypotheses. \
"drifting" if it moved away from the task objective."""


def build_prompt(detail: str, events: list) -> str:
    actions = "\n".join(
        f"- {e.text.replace(chr(10), ' ')[:160]}" for e in events[-MAX_ACTIONS:]
    )
    return PROMPT.format(detail=detail, actions=actions or "- (none)")


def ask(detail: str, events: list) -> dict | None:
    """Ask Haiku for a verdict. Return None for any unexpected condition."""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["AES_MONITOR_ACTIVE"] = "1"  # disable the hook inside this call
    try:
        completed = subprocess.run(
            [
                "claude", "-p", build_prompt(detail, events),
                "--model", MODEL,
                "--settings", '{"disableAllHooks":true}',
                "--mcp-config", '{"mcpServers":{}}',
                "--strict-mcp-config",
                "--output-format", "json",
                "--disallowed-tools", "Bash", "Edit", "Write", "Read",
                "Glob", "Grep", "WebSearch", "WebFetch", "Task",
            ],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return _parse(completed.stdout)


def _parse(stdout: str) -> dict | None:
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    text = envelope.get("result") if isinstance(envelope, dict) else None
    if not isinstance(text, str):
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        verdict = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if verdict.get("verdict") not in ("converging", "looping", "drifting"):
        return None
    return verdict
