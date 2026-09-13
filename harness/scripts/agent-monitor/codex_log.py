"""Read `exec` blocks from `codex exec` output.

Format produced by Codex, three lines plus output:

    exec
    <comando> in <workdir>
     succeeded in 12ms:        or         exited 1 in 0ms:
    <output...>

Used both for offline replay of historical logs and for the runtime monitor
called by run.sh, so it lives in a separate module instead of being duplicated.
"""

from __future__ import annotations

import re

from detector import make_event

_STATUS = re.compile(r"^\s*(succeeded|exited (\d+)) in \S+:")
# A command's output ends where another command starts or where Codex prints a
# protocol marker. Without these boundaries capture spilled into the next
# block and two identical errors ended up with different signatures, hiding
# exactly the repetition we need to detect.
_BOUNDARY = re.compile(r"^\s*(exec|DONE|PHASE:|QUESTION:|tokens used)")
_TRAILING_WORKDIR = re.compile(r"\s+in /\S+$")
OUTCOME_LINES = 12


def parse_codex_text(text: str, session_id: str, repo_root: str | None = None) -> list:
    """Extract events (the action, plus an error when the command failed)."""
    lines = text.splitlines()
    events = []
    i = 0
    while i < len(lines):
        if lines[i].strip() != "exec":
            i += 1
            continue
        if i + 1 >= len(lines):
            break
        command = _TRAILING_WORKDIR.sub("", lines[i + 1].strip())
        status_at, failed = None, False
        for j in range(i + 2, min(i + 6, len(lines))):
            match = _STATUS.match(lines[j])
            if match:
                status_at = j
                failed = match.group(2) is not None and match.group(2) != "0"
                break
        if status_at is None:
            i += 1
            continue
        captured = []
        for line in lines[status_at + 1 : status_at + 1 + OUTCOME_LINES]:
            if _BOUNDARY.match(line) or _STATUS.match(line):
                break
            if line.strip():
                captured.append(line)
        output = "\n".join(captured)
        events.append(
            make_event(
                session_id, "codex", "bash", command,
                outcome=output, repo_root=repo_root,
            )
        )
        if failed:
            events.append(
                make_event(
                    session_id, "codex", "error", output,
                    is_error=True, repo_root=repo_root,
                )
            )
        i = status_at + 1
    return events
