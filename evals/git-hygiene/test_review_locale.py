"""Deterministic eval for local review fallback.

Fake binaries separately record received arguments and stdin, and simulate
success, exhausted quota, or ordinary errors. The eval verifies that a real
error is not masked by switching providers and that no false green passes
without an executed review.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Callable


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "harness" / "scripts" / "review-locale.sh"


class TestReviewLocale(unittest.TestCase):
    """Verify reviewer selection and error propagation."""

    def _fixture(self, temporary: Path) -> tuple[Path, Path, Path]:
        repo = temporary / "repo"
        remote = temporary / "origin.git"
        bin_dir = temporary / "bin"
        log_dir = temporary / "log"
        (repo / "harness" / "scripts").mkdir(parents=True)
        (repo / "harness" / "config").mkdir()
        (repo / "docs").mkdir()
        bin_dir.mkdir()
        log_dir.mkdir()
        target = repo / "harness" / "scripts" / "review-locale.sh"
        target.write_bytes(SCRIPT.read_bytes())
        target.chmod(0o755)
        (repo / "docs" / "review-criteria.md").write_text(
            "1. Test criterion\n", encoding="utf-8"
        )
        (repo / "changed.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                       capture_output=True)
        subprocess.run(["git", "init", "-b", "develop", str(repo)], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                        str(remote)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email",
                        "eval@example.test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name",
                        "Review eval"], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "push", "origin", "develop"],
                       check=True, capture_output=True)
        (repo / "changed.txt").write_text("change\n", encoding="utf-8")
        return repo, bin_dir, log_dir

    def _install_stub(self, bin_dir: Path, name: str) -> None:
        script = bin_dir / name
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -u\n"
            "has_base=false\n"
            "has_prompt=false\n"
            "expects_value=false\n"
            "for argument in \"$@\"; do\n"
            "    if [ \"$expects_value\" = true ]; then\n"
            "        expects_value=false\n"
            "        continue\n"
            "    fi\n"
            "    case \"$argument\" in\n"
            "        --base) has_base=true; expects_value=true ;;\n"
            "        --base=*) has_base=true ;;\n"
            "        -m|--model) expects_value=true ;;\n"
            "        exec|review|-*) ;;\n"
            "        *) has_prompt=true ;;\n"
            "    esac\n"
            "done\n"
            "{ printf '%q ' \"$@\"; printf '\\n'; } > \"$STUB_LOG_DIR/"
            f"{name}.args\"\n"
            f"cat > \"$STUB_LOG_DIR/{name}.stdin\"\n"
            "if [ \"$has_base\" = true ] && [ \"$has_prompt\" = true ]; then\n"
            "    printf '%s\\n' \"error: the argument '--base <BRANCH>' cannot be used with '[PROMPT]'\" >&2\n"
            "    exit 2\n"
            "fi\n"
            f"printf '%s\\n' \"${{{name.upper()}_OUTPUT-}}\" >&2\n"
            f"exit \"${{{name.upper()}_STATUS:-0}}\"\n",
            encoding="utf-8",
        )
        script.chmod(0o755)

    def _run(
        self,
        *arguments: str,
        prepare: Callable[[Path], None] | None = None,
        **settings: str,
    ) -> tuple[subprocess.CompletedProcess, Path]:
        with tempfile.TemporaryDirectory(prefix="review-locale-") as directory:
            temporary = Path(directory)
            repo, bin_dir, log_dir = self._fixture(temporary)
            if prepare is not None:
                prepare(repo)
            self._install_stub(bin_dir, "codex")
            self._install_stub(bin_dir, "claude")
            environment = os.environ.copy()
            environment.update(settings)
            environment["PATH"] = os.pathsep.join(
                [str(bin_dir), environment.get("PATH", "")]
            )
            environment["STUB_LOG_DIR"] = str(log_dir)
            result = subprocess.run(
                ["bash", str(repo / "harness/scripts/review-locale.sh"), *arguments],
                cwd=repo, capture_output=True, text=True, env=environment,
            )
            # Preserve artifacts for assertions before the temporary directory is removed.
            result._review_log = {name: (log_dir / f"{name}.args").read_text()
                                  if (log_dir / f"{name}.args").exists() else ""
                                  for name in ("codex", "claude")}
            result._review_prompt = {name: (log_dir / f"{name}.prompt").read_text()
                                     if (log_dir / f"{name}.prompt").exists() else ""
                                     for name in ("codex", "claude")}
            result._review_stdin = {name: (log_dir / f"{name}.stdin").read_text()
                                    if (log_dir / f"{name}.stdin").exists() else ""
                                    for name in ("codex", "claude")}
            return result, repo

    def test_codex_success_does_not_invoke_claude(self):
        result, _ = self._run(CODEX_STATUS="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result._review_log["codex"])
        self.assertEqual(result._review_log["claude"], "")

    def test_reviewers_receive_prompt_on_stdin_without_positional_prompt(self):
        result, _ = self._run(CODEX_STATUS="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        codex_arguments = result._review_log["codex"]
        self.assertNotIn("--base", codex_arguments)
        self.assertIn("-", codex_arguments)
        self.assertNotIn("Criterion", codex_arguments)
        self.assertIn("Test criterion", result._review_stdin["codex"])

    def test_large_prompt_is_passed_without_argument_size_failure(self):
        def make_large_change(repo: Path) -> None:
            (repo / "changed.txt").write_text(
                "x" * (128 * 1024 + 1), encoding="utf-8"
            )

        result, _ = self._run(
            prepare=make_large_change,
            CODEX_STATUS="1",
            CODEX_OUTPUT="You've hit your usage limit",
            CLAUDE_STATUS="0",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(len(result._review_stdin["claude"]), 128 * 1024)
        self.assertNotIn("E2BIG", result.stderr)

    def test_reviewer_routing_is_configurable(self):
        result, _ = self._run(
            PRIMARY_REVIEWER="claude",
            PRIMARY_REVIEWER_MODEL="primary-test-model",
            FALLBACK_REVIEWER="codex",
            FALLBACK_REVIEWER_MODEL="fallback-test-model",
            CLAUDE_STATUS="1",
            CLAUDE_OUTPUT="You've hit your usage limit",
            CODEX_STATUS="0",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-p --model primary-test-model", result._review_log["claude"])
        self.assertIn("exec review -m fallback-test-model", result._review_log["codex"])

    def test_harness_config_is_used_when_environment_is_unset(self):
        def configure(repo: Path) -> None:
            (repo / "harness/config/harness.conf").write_text(
                'PRIMARY_REVIEWER="claude"\n'
                'PRIMARY_REVIEWER_MODEL="config-primary"\n'
                'FALLBACK_REVIEWER="codex"\n'
                'FALLBACK_REVIEWER_MODEL="config-fallback"\n',
                encoding="utf-8",
            )

        result, _ = self._run(
            prepare=configure,
            CLAUDE_STATUS="1",
            CLAUDE_OUTPUT="You've hit your usage limit",
            CODEX_STATUS="0",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-p --model config-primary", result._review_log["claude"])
        self.assertIn("exec review -m config-fallback", result._review_log["codex"])

    def test_environment_overrides_harness_config(self):
        def configure(repo: Path) -> None:
            (repo / "harness/config/harness.conf").write_text(
                'PRIMARY_REVIEWER="claude"\nPRIMARY_REVIEWER_MODEL="config-model"\n',
                encoding="utf-8",
            )

        result, _ = self._run(
            prepare=configure,
            PRIMARY_REVIEWER="codex",
            PRIMARY_REVIEWER_MODEL="environment-model",
            CODEX_STATUS="0",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("exec review -m environment-model", result._review_log["codex"])
        self.assertEqual(result._review_log["claude"], "")

    def test_unknown_reviewer_adapter_fails_explicitly(self):
        result, _ = self._run(PRIMARY_REVIEWER="unknown")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrecognized reviewer adapter", result.stderr)

    def test_quota_failure_falls_back_to_claude(self):
        result, _ = self._run(
            CODEX_STATUS="1", CODEX_OUTPUT="You've hit your usage limit; retry later",
            CLAUDE_STATUS="0",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result._review_log["claude"])

    def test_quota_failure_message_is_configurable(self):
        result, _ = self._run(
            QUOTA_FAILURE_MESSAGE="custom quota exhausted",
            CODEX_STATUS="1",
            CODEX_OUTPUT="custom quota exhausted; retry later",
            CLAUDE_STATUS="0",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result._review_log["claude"])

    def test_non_quota_failure_does_not_fall_back(self):
        result, _ = self._run(CODEX_STATUS="7", CODEX_OUTPUT="network failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result._review_log["claude"], "")

    def test_both_providers_failing_is_failure(self):
        result, _ = self._run(
            CODEX_STATUS="1", CODEX_OUTPUT="You've hit your usage limit",
            CLAUDE_STATUS="9", CLAUDE_OUTPUT="claude crashed",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no review", (result.stdout + result.stderr).lower())

    def test_second_opinion_invokes_both_and_labels_results(self):
        result, _ = self._run("--second-opinion")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result._review_log["codex"])
        self.assertTrue(result._review_log["claude"])
        self.assertIn("Primary review", result.stdout)
        self.assertIn("Fallback review", result.stdout)

    def test_second_opinion_reports_primary_failure_and_fallback_success(self):
        result, _ = self._run(
            "--second-opinion", CODEX_STATUS="7", CODEX_OUTPUT="primary bad",
            CLAUDE_STATUS="0",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("primary review failed", (result.stdout + result.stderr).lower())
        self.assertIn("fallback review succeeded", (result.stdout + result.stderr).lower())

    def test_second_opinion_reports_primary_success_and_fallback_failure(self):
        result, _ = self._run(
            "--second-opinion", CODEX_STATUS="0",
            CLAUDE_STATUS="8", CLAUDE_OUTPUT="fallback bad",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("primary review succeeded", (result.stdout + result.stderr).lower())
        self.assertIn("fallback review failed", (result.stdout + result.stderr).lower())

    def test_review_uses_configured_integration_branch(self):
        def configure(repo: Path) -> None:
            (repo / "harness/config/harness.conf").write_text(
                'INTEGRATION_BRANCH="main"\n', encoding="utf-8"
            )
            subprocess.run(["git", "-C", str(repo), "branch", "main"], check=True)
            subprocess.run(["git", "-C", str(repo), "push", "origin", "main"], check=True,
                            capture_output=True)

        result, _ = self._run(prepare=configure, CODEX_STATUS="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("origin/main", result._review_stdin["codex"])

    def test_no_success_when_reviewers_are_not_successful(self):
        result, _ = self._run(
            "--second-opinion", CODEX_STATUS="7", CODEX_OUTPUT="bad",
            CLAUDE_STATUS="8", CLAUDE_OUTPUT="bad",
        )
        self.assertNotEqual(result.returncode, 0)

    def test_fallback_excludes_untracked_file_contents(self):
        def add_untracked_file(repo: Path) -> None:
            (repo / "credenziali-non-ignorate.txt").write_text(
                "SECRET_CANARY_UNTRACKED\n", encoding="utf-8"
            )

        result, _ = self._run(
            prepare=add_untracked_file,
            CODEX_STATUS="1",
            CODEX_OUTPUT="You've hit your usage limit",
            CLAUDE_STATUS="0",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("SECRET_CANARY_UNTRACKED", result._review_stdin["claude"])

    def test_fallback_includes_staged_new_file(self):
        def stage_new_file(repo: Path) -> None:
            (repo / "staged.txt").write_text(
                "STAGED_CANARY\n", encoding="utf-8"
            )
            subprocess.run(["git", "-C", str(repo), "add", "staged.txt"], check=True)

        result, _ = self._run(
            prepare=stage_new_file,
            CODEX_STATUS="1",
            CODEX_OUTPUT="You've hit your usage limit",
            CLAUDE_STATUS="0",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("STAGED_CANARY", result._review_stdin["claude"])


if __name__ == "__main__":
    unittest.main()


class TestHarnessScriptsAreExecutable(unittest.TestCase):
    """Gli script dell'harness vanno invocati direttamente, non con `bash`.

    `AGENTS.md` e `policies/git-workflow.md` documentano invocazioni come
    `harness/scripts/review-locale.sh <branch>`: uno script che perde il bit
    di esecuzione fallisce con «permission denied» solo al primo uso reale, e
    dopo la propagazione lo farebbe in ogni repo consumer. E' successo
    davvero, riscrivendo review-locale.sh (2026-09-06).
    """

    def test_every_harness_script_keeps_its_executable_bit(self):
        scripts_dir = REPO / "harness" / "scripts"
        not_executable = sorted(
            str(path.relative_to(REPO))
            for path in scripts_dir.rglob("*.sh")
            if not os.access(path, os.X_OK)
        )
        self.assertFalse(
            not_executable,
            "Questi script dell'harness non sono eseguibili "
            f"({', '.join(not_executable)}): la documentazione li invoca per "
            "path diretto, quindi fallirebbero con «permission denied» al "
            "primo uso reale e in ogni repo consumer dopo la propagazione. "
            "Ripristina il bit con `chmod +x`.",
        )
