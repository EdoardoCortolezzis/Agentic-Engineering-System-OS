"""Eval deterministico per il wrapper scripts/codex-orchestrate/run.sh.

Esegue il wrapper in repository temporanei con un eseguibile ``codex`` finto
in testa a ``PATH``. In questo modo l'eval verifica il confine shell senza
chiamare il servizio reale e controlla sia gli argomenti sia l'ambiente che il
wrapper consegna a Codex.

Esecuzione: ``python3 -m unittest evals/codex-orchestrate/test_run_sh.py``
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
ORCHESTRATE_DIR = REPO / "scripts" / "codex-orchestrate"


class TestRunWrapper(unittest.TestCase):
    """Verifica la selezione del modello e l'isolamento del file .env."""

    def _run_wrapper(
        self,
        *,
        model: str | None = None,
        dotenv: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], list[str], dict[str, str]]:
        with tempfile.TemporaryDirectory(prefix="codex-orchestrate-") as directory:
            root = Path(directory)
            script_dir = root / "scripts" / "codex-orchestrate"
            script_dir.mkdir(parents=True)
            for filename in ("run.sh", "orchestrate.py"):
                shutil.copy2(ORCHESTRATE_DIR / filename, script_dir / filename)

            if dotenv is not None:
                (root / ".env").write_text(dotenv, encoding="utf-8")

            stub_dir = root / "stub-bin"
            stub_dir.mkdir()
            args_file = root / "codex-args.json"
            env_file = root / "codex-env.json"
            codex_stub = stub_dir / "codex"
            codex_stub.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "import os\n"
                "import sys\n"
                f"Path = {str(args_file)!r}\n"
                f"EnvPath = {str(env_file)!r}\n"
                "with open(Path, 'w', encoding='utf-8') as file:\n"
                "    json.dump(sys.argv[1:], file)\n"
                "with open(EnvPath, 'w', encoding='utf-8') as file:\n"
                "    json.dump(dict(os.environ), file)\n"
                "print('DONE')\n",
                encoding="utf-8",
            )
            codex_stub.chmod(0o755)

            environment = os.environ.copy()
            environment.pop("CODEX_MODEL", None)
            if model is not None:
                environment["CODEX_MODEL"] = model
            environment["PATH"] = (
                f"{stub_dir}{os.pathsep}{environment['PATH']}"
            )

            completed = subprocess.run(
                [str(script_dir / "run.sh"), "model-selection", "task di prova"],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("DONE", completed.stdout)
            return (
                completed,
                json.loads(args_file.read_text(encoding="utf-8")),
                json.loads(env_file.read_text(encoding="utf-8")),
            )

    @staticmethod
    def _model_argument(args: list[str]) -> str | None:
        if "-m" not in args:
            return None
        index = args.index("-m")
        if index + 1 >= len(args):
            raise AssertionError(f"-m has no value: {args!r}")
        return args[index + 1]

    def test_environment_model_is_passed_to_codex(self):
        _, args, _ = self._run_wrapper(model="model-from-environment")

        self.assertEqual(self._model_argument(args), "model-from-environment")

    def test_without_model_uses_hardcoded_default(self):
        _, args, _ = self._run_wrapper()

        self.assertEqual(self._model_argument(args), "gpt-5.6-luna")

    def test_dotenv_model_is_selected_without_exporting_other_secrets(self):
        _, args, received_environment = self._run_wrapper(
            dotenv=(
                "CODEX_MODEL=model-from-dotenv\n"
                "SECRET_CANARY=non-deve-arrivare-a-codex\n"
            ),
        )

        self.assertEqual(self._model_argument(args), "model-from-dotenv")
        self.assertNotIn("SECRET_CANARY", received_environment)

    def test_environment_model_takes_precedence_over_dotenv(self):
        _, args, _ = self._run_wrapper(
            model="model-from-environment",
            dotenv="CODEX_MODEL=model-from-dotenv\n",
        )

        self.assertEqual(self._model_argument(args), "model-from-environment")

    def test_continue_runs_in_progress_session_and_reuses_history(self):
        with tempfile.TemporaryDirectory(prefix="codex-orchestrate-continue-") as directory:
            root = Path(directory)
            script_dir = root / "scripts" / "codex-orchestrate"
            script_dir.mkdir(parents=True)
            for filename in ("run.sh", "orchestrate.py"):
                shutil.copy2(ORCHESTRATE_DIR / filename, script_dir / filename)

            stub_dir = root / "stub-bin"
            stub_dir.mkdir()
            prompt_file = root / "prompt.txt"
            (stub_dir / "codex").write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                f"open({str(prompt_file)!r}, 'w', encoding='utf-8').write(sys.stdin.read())\n"
                "print('DONE')\n",
                encoding="utf-8",
            )
            (stub_dir / "codex").chmod(0o755)
            session_dir = root / ".agent" / "orchestration" / "continue-me"
            session_dir.mkdir(parents=True)
            (session_dir / "state.json").write_text(
                json.dumps(
                    {
                        "task": "riprendi task",
                        "history": [["domanda precedente", "risposta precedente"]],
                        "iteration": 2,
                        "status": "in_progress",
                        "last_question": None,
                    }
                ),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PATH"] = f"{stub_dir}{os.pathsep}{environment['PATH']}"

            completed = subprocess.run(
                [str(script_dir / "run.sh"), "continue", "continue-me"],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("DONE", completed.stdout)
            self.assertIn("domanda precedente", prompt_file.read_text(encoding="utf-8"))
            final_state = json.loads((session_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(final_state["status"], "done")
            self.assertEqual(final_state["iteration"], 3)

    def test_continue_rejects_missing_or_non_in_progress_sessions(self):
        for status in ("awaiting_answer", "done"):
            with self.subTest(status=status), tempfile.TemporaryDirectory(
                prefix="codex-orchestrate-continue-reject-"
            ) as directory:
                root = Path(directory)
                script_dir = root / "scripts" / "codex-orchestrate"
                script_dir.mkdir(parents=True)
                for filename in ("run.sh", "orchestrate.py"):
                    shutil.copy2(ORCHESTRATE_DIR / filename, script_dir / filename)
                session_dir = root / ".agent" / "orchestration" / "blocked"
                session_dir.mkdir(parents=True)
                (session_dir / "state.json").write_text(
                    json.dumps({"task": "task", "status": status}),
                    encoding="utf-8",
                )
                completed = subprocess.run(
                    [str(script_dir / "run.sh"), "continue", "blocked"],
                    cwd=root,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("in_progress", completed.stderr)

        with tempfile.TemporaryDirectory(prefix="codex-orchestrate-continue-missing-") as directory:
            root = Path(directory)
            script_dir = root / "scripts" / "codex-orchestrate"
            script_dir.mkdir(parents=True)
            for filename in ("run.sh", "orchestrate.py"):
                shutil.copy2(ORCHESTRATE_DIR / filename, script_dir / filename)
            completed = subprocess.run(
                [str(script_dir / "run.sh"), "continue", "missing"],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("non trovata", completed.stderr)


if __name__ == "__main__":
    unittest.main()
