"""Replay the detector on recorded logs to tune thresholds without model calls.

Logs under `.agent/orchestration/<session>/log.txt` can be replayed as a
dataset with a known outcome. Replay answers how many signals would be
produced (noise) and whether the recorded cases are detected (effectiveness).

It reads two formats:
  - Codex round logs (`<directory>/<session>/log.txt`);
  - Claude Code transcripts (`<directory>/<project>/*.jsonl`).

Usage:
    python3 harness/scripts/agent-monitor/replay.py <log-directory> [...]
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_log import parse_codex_text  # noqa: E402
from detector import (  # noqa: E402
    detect,
    is_noop_wait,
    is_user_rejection,
    make_event,
    payload_for,
)

# "exec" / command / " succeeded in 12ms:" or " exited 1 in 0ms:"
def _natural(path: Path) -> tuple:
    parts = re.split(r"(\d+)", path.name)
    return tuple(int(p) if p.isdigit() else p for p in parts)


def parse_log(path: Path, session_id: str) -> list:
    """Events from one Codex session's log.txt."""
    return parse_codex_text(
        path.read_text(encoding="utf-8", errors="replace"), session_id
    )


def parse_transcript(path: Path, session_id: str) -> list:
    """Extract events from a Claude Code JSONL transcript."""
    pending: dict[str, tuple[str, dict]] = {}
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (record.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                pending[block.get("id", "")] = (
                    block.get("name", "?"),
                    block.get("input") or {},
                )
            elif block.get("type") == "tool_result":
                name, tool_input = pending.pop(
                    block.get("tool_use_id", ""), (None, None)
                )
                if name is None:
                    continue
                raw = block.get("content")
                outcome = raw if isinstance(raw, str) else json.dumps(raw)[:600]
                failed = bool(block.get("is_error"))
                payload = payload_for(name, tool_input)
                if is_noop_wait(payload):
                    continue
                events.append(
                    make_event(
                        session_id,
                        "claude",
                        name.lower(),
                        payload,
                        outcome=outcome[:600],
                    )
                )
                if failed and not is_user_rejection(outcome):
                    events.append(
                        make_event(
                            session_id, "claude", "error", outcome[:600], is_error=True
                        )
                    )
    return events


def collect(root: str) -> list[tuple[str, Path, str]]:
    """Find sessions to replay and the parser appropriate for each."""
    base = Path(root)
    codex = sorted(base.glob("*/log.txt"), key=lambda p: _natural(p.parent))
    if codex:
        return [(log.parent.name, log, "codex") for log in codex]
    transcripts = sorted(base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return [(t.stem[:8], t, "claude") for t in transcripts]


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    sessions = []
    for root in argv[1:]:
        sessions.extend(collect(root))

    ledger: list = []
    signals: list[tuple[str, object]] = []
    for session_id, log, kind in sessions:
        events = (
            parse_log(log, session_id)
            if kind == "codex"
            else parse_transcript(log, session_id)
        )
        if not events:
            continue
        session_signals = []
        for event in events:
            ledger.append(event)
            signal = detect(ledger, event)
            if signal:
                session_signals.append(signal)
                signals.append((session_id, signal))
        print(
            f"{session_id:<14} events={len(events):>4}  signals={len(session_signals)}"
            + (
                "  [" + ", ".join(sorted({s.rule for s in session_signals})) + "]"
                if session_signals
                else ""
            )
        )

    print(f"\ntotal events: {len(ledger)}   total signals: {len(signals)}")
    for rule, count in Counter(sg.rule for _, sg in signals).most_common():
        share = 100.0 * count / max(len(ledger), 1)
        print(f"  {rule:<18} {count:>4}  ({share:.2f}% of events)")

    recurring = [(s, sg) for s, sg in signals if sg.rule == "recurring_defect"]
    print(f"\nrecurring cross-session defects: {len(recurring)}")
    for session_id, signal in recurring[:10]:
        print(f"  {session_id:<14} {signal.count} sessioni  {signal.fingerprint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
