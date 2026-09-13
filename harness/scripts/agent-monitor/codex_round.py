"""Monitor one `codex exec` round, invoked by run.sh.

Reads the raw round output from stdin and writes a warning to stdout only when
the detector finds a signal. It shares the repository ledger with the Claude
Code hook so the cross-session rule can detect repeated defects.

Usage: codex_round.py <session-id> <repo-root> < round-output
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ledger  # noqa: E402
import probe  # noqa: E402
from codex_log import parse_codex_text  # noqa: E402
from detector import detect  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        return 0
    session_id, root = argv[1], argv[2]
    events = parse_codex_text(sys.stdin.read(), session_id, repo_root=root)
    if not events:
        return 0

    # Read the tail once and extend it in memory: rereading the ledger for
    # every event in the round is quadratic and does not change the verdict.
    history = ledger.tail(root)
    signal = None
    for event in events:
        ledger.append(root, event)
        history.append(event)
        signal = detect(history, event) or signal
    if signal is None:
        return 0

    line = f"[monitor] {signal.detail}"
    if ledger.may_probe(root, session_id, signal.fingerprint):
        ledger.record_probe(root, session_id, signal.fingerprint)
        verdict = probe.ask(signal.detail, history)
        if verdict:
            if verdict["verdict"] == "converging":
                return 0
            line += (
                f" | {verdict['verdict']}: {verdict.get('reason', '')}"
                f" | try: {verdict.get('suggestion', '')}"
            )
    print(line)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except SystemExit:
        raise
    except BaseException:
        raise SystemExit(0)  # fail-open, like the hook
