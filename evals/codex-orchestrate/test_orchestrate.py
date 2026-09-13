"""Eval deterministico per il protocollo codex-orchestrate (spec 001).

Verifica le 5 transizioni di stato descritte negli scenari BDD della spec
(specs/001-codex-orchestrate/spec.md) usando solo `orchestrate.py`: nessuna
chiamata reale a `codex exec` (costerebbe credito e richiede auth). L'output
di Codex e' simulato come stringa passata a `process_output`/`parse_markers`,
esattamente come previsto dalla sezione "Eval" della spec.

`run.sh` (thin wrapper I/O attorno a questo modulo) non e' coperto qui: va
verificato manualmente end-to-end con un task giocattolo reale.

Esecuzione: `python3 -m unittest evals/codex-orchestrate/test_orchestrate.py`
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "codex-orchestrate"))

from orchestrate import (  # noqa: E402
    MAX_ITER,
    SessionState,
    build_prompt,
    next_action,
    process_output,
    resume,
)


class TestCodexOrchestrate(unittest.TestCase):
    def test_task_without_ambiguity_completes_in_one_round(self):
        state = SessionState(task="aggiungi un endpoint /health")
        output = "PHASE: implementazione\nDONE"

        result = process_output(state, output)

        self.assertEqual(result.status, "done")
        self.assertEqual(result.iteration, 1)
        self.assertIsNone(result.last_question)

    def test_question_marker_stops_session_awaiting_answer(self):
        state = SessionState(task="migra il db a postgres")
        output = "PHASE: analisi schema\nQUESTION: quale versione di postgres?"

        result = process_output(state, output)

        self.assertEqual(result.status, "awaiting_answer")
        self.assertEqual(result.last_question, "quale versione di postgres?")
        # il giro si ferma: non si rilancia Codex automaticamente
        self.assertEqual(next_action(result), "await_answer")

    def test_resume_includes_history_and_increments_iteration(self):
        state = SessionState(
            task="migra il db a postgres",
            iteration=1,
            status="awaiting_answer",
            last_question="quale versione di postgres?",
        )

        resumed = resume(state, "postgres 16")
        prompt = build_prompt(resumed.task, resumed.history)

        self.assertIn("quale versione di postgres?", prompt)
        self.assertIn("postgres 16", prompt)

        final = process_output(resumed, "PHASE: migrazione\nDONE")
        self.assertEqual(final.iteration, state.iteration + 1)

    def test_cap_reached_after_max_iterations(self):
        state = SessionState(
            task="task ambiguo che continua a fare domande",
            iteration=MAX_ITER,
            status="in_progress",
        )

        action = next_action(state)

        self.assertEqual(action, "cap_reached")

    def test_output_without_markers_stays_in_progress(self):
        state = SessionState(task="refactor del modulo auth")
        output = "sto pensando a come strutturare il refactor...\nancora testo libero"

        result = process_output(state, output)

        self.assertEqual(result.status, "in_progress")
        self.assertIsNone(result.last_question)

    def test_run_handles_output_larger_than_arg_max(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_repo = Path(temp_dir)
            fake_script_dir = fake_repo / "scripts" / "codex-orchestrate"
            fake_script_dir.mkdir(parents=True)
            real_script_dir = REPO / "scripts" / "codex-orchestrate"
            for filename in ("run.sh", "orchestrate.py"):
                shutil.copy2(real_script_dir / filename, fake_script_dir / filename)

            stub_dir = fake_repo / "stub-bin"
            stub_dir.mkdir()
            arg_max = os.sysconf("SC_ARG_MAX")
            target_size = max(1_200_000, arg_max + 256 * 1024)
            repeated_line = "codex-output-line\n"
            done_line = "DONE\n"
            repetitions = target_size // len(repeated_line) + 1
            codex_stub = stub_dir / "codex"
            codex_stub.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                f"sys.stdout.write({repeated_line!r} * {repetitions})\n"
                f"sys.stdout.write({done_line!r})\n",
                encoding="utf-8",
            )
            codex_stub.chmod(0o755)

            environment = os.environ.copy()
            environment["PATH"] = f"{stub_dir}{os.pathsep}{environment['PATH']}"
            run_script = fake_script_dir / "run.sh"
            completed = subprocess.run(
                [str(run_script), "large-output", "task qualsiasi"],
                cwd=fake_repo,
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary_start = completed.stdout.rfind("\n{") + 1
            summary = json.loads(completed.stdout[summary_start:])
            self.assertEqual(summary["status"], "done")

            state_file = fake_repo / ".agent" / "orchestration" / "large-output" / "state.json"
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "done")


if __name__ == "__main__":
    unittest.main()
