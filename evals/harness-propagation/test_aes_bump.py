"""Eval deterministico per la propagazione automatica dell'harness.

Questi test fissano il contratto del workflow e del wrapper. I test runtime
usano repository Git temporanei (incluso un bare remote per il consumer) e un
``gh`` finto che registra ogni chiamata senza accedere alla rete.

Esecuzione: ``python3 -m unittest discover -s evals/harness-propagation
-p 'test_*.py' -v``
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
import json


REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "aes-harness-bump.yml"
SCRIPT = REPO / "harness" / "scripts" / "aes-bump.sh"
MANIFEST = REPO / "harness" / "manifest.txt"
STABLE_BRANCH = "automation/aes-harness-bump"


class TestAesBumpContract(unittest.TestCase):
    """Verifica il contratto osservabile senza GitHub o runner reali."""

    def _workflow_source(self) -> str:
        self.assertTrue(
            WORKFLOW.is_file(),
            "Manca .github/workflows/aes-harness-bump.yml: il workflow di "
            "propagazione non è ancora stato implementato.",
        )
        return WORKFLOW.read_text(encoding="utf-8")

    def _script_source(self) -> str:
        self.assertTrue(
            SCRIPT.is_file(),
            "Manca harness/scripts/aes-bump.sh: il wrapper di propagazione "
            "non è ancora stato implementato.",
        )
        return SCRIPT.read_text(encoding="utf-8")

    def _manifest_entries(self) -> set[tuple[str, str, str]]:
        entries: set[tuple[str, str, str]] = set()
        for line in MANIFEST.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            fields = line.split()
            self.assertGreaterEqual(
                len(fields), 3, f"Riga manifest non valida: {line!r}"
            )
            entries.add((fields[0], fields[1], fields[2]))
        return entries

    def test_manifest_propagates_workflow_and_wrapper(self) -> None:
        entries = self._manifest_entries()
        required = {
            ("copy", "harness/scripts/aes-bump.sh", "harness/scripts/aes-bump.sh"),
            (
                "copy",
                ".github/workflows/aes-harness-bump.yml",
                ".github/workflows/aes-harness-bump.yml",
            ),
        }
        self.assertTrue(
            required <= entries,
            "Il manifest deve propagare workflow e wrapper ai consumer; "
            f"mancano: {sorted(required - entries)}",
        )

    def test_workflow_has_manual_and_automatic_triggers(self) -> None:
        source = self._workflow_source()
        self.assertRegex(
            source,
            r"(?m)^\s{2}workflow_dispatch:\s*$",
            "Il workflow deve poter essere avviato manualmente con "
            "workflow_dispatch.",
        )
        self.assertRegex(
            source,
            r"(?m)^\s{2}schedule:\s*$",
            "Il workflow deve avere un trigger automatico schedule.",
        )
        self.assertRegex(
            source,
            r"(?m)^\s{4}-\s+cron:\s*['\"][^'\"]+['\"]",
            "Il trigger schedule deve dichiarare un cron deterministico.",
        )
        self.assertRegex(
            source,
            r"(?m)^\s+ref:\s*develop\s*$",
            "Il consumer va sincronizzato sul suo develop, non sul branch "
            "del run.",
        )

    def test_checkout_uses_optional_configured_credential_not_literal(self) -> None:
        source = self._workflow_source() + "\n" + self._script_source()
        self.assertNotRegex(
            source,
            r"(?m)^\s*if:.*AES_SYNC_TOKEN",
            "La presenza del token non deve essere un prerequisito del job: "
            "i repository OSS pubblici devono poter eseguire il workflow senza secret.",
        )
        self.assertRegex(
            source,
            r"secrets\.AES_SYNC_TOKEN",
            "Quando configurato, il token deve poter essere usato per i repository privati.",
        )
        for pattern, label in (
            (r"ghp_[A-Za-z0-9_]+", "classic personal access token"),
            (r"github_pat_[A-Za-z0-9_]+", "fine-grained personal access token"),
            (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key"),
        ):
            self.assertIsNone(
                re.search(pattern, source),
                f"Il codice non deve contenere una credenziale letterale ({label}).",
            )

    def test_checkout_aes_uses_sync_token_and_gh_token_is_explicit(self) -> None:
        source = self._workflow_source()
        self.assertRegex(
            source,
            r"(?ms)uses:\s*actions/checkout@[^\n]+.*?repository:.*?\n.*?token:\s*\$\{\{\s*secrets\.AES_SYNC_TOKEN\s*\}\}",
            "Il checkout del repository AES deve usare AES_SYNC_TOKEN quando "
            "il consumer lo configura.",
        )
        self.assertRegex(
            source,
            r"(?m)GH_TOKEN:\s*\$\{\{\s*secrets\.AES_SYNC_TOKEN\s*\}\}",
            "gh deve ricevere esplicitamente lo stesso token per push e PR.",
        )

    def test_both_checkouts_use_full_history_and_sync_token(self) -> None:
        source = self._workflow_source()
        checkout_blocks = re.findall(
            r"(?ms)^\s*- name: Check out .*?\n(.*?)(?=^\s*- name:|\Z)",
            source,
        )
        self.assertEqual(len(checkout_blocks), 2)
        for block in checkout_blocks:
            self.assertIn(
                "fetch-depth: 0",
                block,
                "Entrambi i checkout devono avere storia completa: il wrapper "
                "deve poter verificare ancestry e fast-forward.",
            )
            self.assertIn("secrets.AES_SYNC_TOKEN", block)
        self.assertRegex(
            source,
            r"(?m)^\s*GH_TOKEN:\s*\$\{\{\s*secrets\.AES_SYNC_TOKEN\s*\}\}",
        )

    def test_bump_workflow_has_non_mutating_permissions_and_concurrency(self) -> None:
        source = self._workflow_source()
        self.assertRegex(
            source,
            r"(?m)^permissions:\s*\{\}\s*$",
            "Il GITHUB_TOKEN predefinito non deve avere permessi di scrittura: "
            "le mutazioni usano AES_SYNC_TOKEN.",
        )
        self.assertRegex(source, r"(?m)^concurrency:\s*$")
        self.assertRegex(source, r"(?m)^\s+cancel-in-progress:\s+false\s*$")

    def test_aes_checkout_is_pinned_to_the_release_tag(self) -> None:
        """Cosa viene eseguito nella CI del consumer lo decide un rilascio.

        `.aes-source` e' il codice che gira nel consumer. Puntarlo a
        `develop` significa eseguire ogni commit del sorgente prima che sia
        stato deliberatamente pubblicato; puntarlo al tag di rilascio
        restringe la finestra a cio' che Edo ha promosso (ADR 0023).
        """
        source = self._workflow_source()
        aes_block = re.search(
            r"(?ms)^\s*- name: Check out [^\n]*\n(?:(?!^\s*- name:).)*?"
            r"repository:\s*\$\{\{\s*vars\.AES_SOURCE_REPOSITORY\s*\}\}.*?"
            r"(?=^\s*- name:|\Z)",
            source,
        )
        self.assertIsNotNone(
            aes_block, "Manca il checkout del repository AES nel workflow."
        )
        self.assertRegex(
            aes_block.group(0),
            r"(?m)^\s+ref:\s*\$\{\{\s*vars\.AES_SOURCE_REF\s*\}\}\s*$",
            "Il checkout di AES deve usare il ref configurato dal consumer.",
        )
        self.assertNotRegex(
            aes_block.group(0),
            r"(?m)^\s+ref:\s*develop\s*$",
            "Il consumer non deve eseguire implicitamente develop.",
        )
        self.assertRegex(
            aes_block.group(0),
            r"(?m)^\s+repository:\s*\$\{\{\s*vars\.AES_SOURCE_REPOSITORY\s*\}\}\s*$",
        )

    def test_workflow_runs_the_bump_script_from_the_aes_checkout(self) -> None:
        """L'autoriparazione: il consumer non esegue la propria copia.

        Un consumer esegue il wrapper che ha mergiato per ultimo, quindi un
        bug nel wrapper andrebbe portato a mano in ogni consumer prima che
        l'automazione possa ripararsi. Prendendolo da `.aes-source`, una
        correzione su AES/develop e' attiva ovunque al run successivo.
        """
        source = self._workflow_source()
        self.assertRegex(
            source,
            r"(?m)^\s+run:\s*\.aes-source/harness/scripts/aes-bump\.sh\b",
            "Il workflow deve eseguire il wrapper del checkout AES.",
        )
        self.assertNotRegex(
            source,
            r"(?m)^\s+run:\s*(?:\./)?harness/scripts/aes-bump\.sh\b",
            "Eseguire la copia del consumer riporta il bootstrap manuale.",
        )

    def test_bump_job_does_not_run_on_the_aes_source_repository(self) -> None:
        """AES propaga questo workflow, ma non ne e' un consumer.

        Sincronizzare AES con se stesso non ha significato: non ha un
        `.harness-version` da verificare e non ha il secret AES_SYNC_TOKEN,
        quindi senza guardia il workflow fallisce ogni giorno anche nel
        repository sorgente.
        """
        source = self._workflow_source()
        self.assertIn("vars.AES_SOURCE_REPOSITORY != ''", source)
        self.assertIn("vars.AES_SOURCE_REF != ''", source)
        self.assertIn("vars.AES_SOURCE_REPOSITORY != github.repository", source)
        self.assertNotIn("EdoardoCortolezzis/Agentic-Engineering-System", source)

    def test_all_actions_are_pinned_to_full_commit_sha(self) -> None:
        source = self._workflow_source()
        references = re.findall(r"(?m)^\s*(?:-\s*)?uses:\s*([^\s#]+)", source)
        self.assertTrue(
            references,
            "Il workflow deve usare almeno una action per il checkout e il "
            "runner; nessun riferimento uses trovato.",
        )
        for reference in references:
            self.assertRegex(
                reference,
                r"^[^@\s]+@[0-9a-fA-F]{40}$",
                f"Action non pinnata a SHA completo: {reference}",
            )

    def test_bump_uses_stable_branch_and_one_pr(self) -> None:
        source = self._script_source()
        self.assertIn(
            STABLE_BRANCH,
            source,
            "Il branch della PR deve essere stabile e condiviso fra i run.",
        )
        self.assertRegex(
            source,
            r"gh\s+pr\s+list",
            "Prima di creare una PR il wrapper deve cercare quella già esistente.",
        )
        self.assertRegex(source, r"--head\s+.*automation/aes-harness-bump")
        self.assertRegex(source, r"--base\s+develop")
        self.assertNotRegex(
            source,
            r"(?:run_id|run_number|github\.sha).*?(?:branch|head)|(?:branch|head).*?(?:run_id|run_number|github\.sha)",
        )

    def test_workflow_never_merges_or_pushes_develop(self) -> None:
        source = self._workflow_source() + "\n" + self._script_source()
        self.assertNotRegex(
            source,
            r"(?im)\bgh\s+pr\s+merge\b|\bauto-merge\b",
            "La propagazione apre una PR ma non può mergiarla.",
        )
        self.assertNotRegex(
            source,
            r"(?im)\bgit\s+push\b[^\n#]*(?:^|[ /])develop(?:\s|$)",
            "Nessun git push deve puntare direttamente a develop.",
        )
        self.assertNotRegex(
            source,
            r"(?im)\bgit\s+push\b[^\n#]*(?:-f|--force)",
            "Il branch che può contenere lavoro umano non va sovrascritto con force push.",
        )
        self.assertRegex(
            source,
            r"git(?:\s+-c\s+core\.hooksPath=/dev/null)?\s+push\s+origin\s+HEAD:refs/heads/automation/aes-harness-bump",
            "Il push deve essere indirizzato esplicitamente al branch stabile.",
        )
        self.assertNotRegex(
            source,
            r"git\s+push\s+origin\s+(?:HEAD|\$[A-Za-z_][A-Za-z0-9_]*)(?:\s|$)",
            "Un push indiretto potrebbe pubblicare accidentalmente develop.",
        )

    @staticmethod
    def _git(*args: str, cwd: Path) -> None:
        subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        )

    def _repo_fixture(self, root: Path, name: str) -> Path:
        repo = root / name
        repo.mkdir()
        self._git("init", "-b", "develop", cwd=repo)
        self._git("config", "user.email", "eval@example.invalid", cwd=repo)
        self._git("config", "user.name", "AES eval", cwd=repo)
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "tracked.txt", cwd=repo)
        self._git("commit", "-m", "fixture", cwd=repo)
        return repo

    def _consumer_fixture(self, root: Path) -> Path:
        """Crea un clone consumer con un bare remote realistico."""
        seed = self._repo_fixture(root, "consumer-seed")
        bare = root / "consumer.git"
        self._git("init", "--bare", str(bare), cwd=root)
        self._git("remote", "add", "origin", str(bare), cwd=seed)
        self._git("push", "-u", "origin", "develop", cwd=seed)
        consumer = root / "consumer"
        self._git("clone", "--branch", "develop", str(bare), str(consumer), cwd=root)
        self._git("config", "user.email", "eval@example.invalid", cwd=consumer)
        self._git("config", "user.name", "AES eval", cwd=consumer)
        return consumer

    def _install_target_and_fake_dependencies(
        self, consumer: Path, source: Path, fake_mode: str = "ok"
    ) -> tuple[Path, Path]:
        """Prepara il contratto di esecuzione del wrapper per i test runtime.

        `aes-sync.sh` viene installato nella sorgente, non nel consumer: il
        wrapper lo prende da li', perche' e' lo strumento che legge il
        manifest della sorgente.
        """
        self.assertTrue(
            SCRIPT.is_file(),
            "Manca harness/scripts/aes-bump.sh: impossibile eseguire l'eval "
            "runtime prima dell'implementazione.",
        )
        scripts = consumer / "harness" / "scripts"
        scripts.mkdir(parents=True)
        target = scripts / "aes-bump.sh"
        shutil.copy2(SCRIPT, target)
        target.chmod(0o755)

        source_scripts = source / "harness" / "scripts"
        source_scripts.mkdir(parents=True, exist_ok=True)
        sync = source_scripts / "aes-sync.sh"
        sync.write_text(
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            "mode=\"${1:-}\"\n"
            "source_path=''\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = --source ]; then source_path=\"$2\"; shift 2; else shift; fi\n"
            "done\n"
            f"if [ \"$mode\" = --check-upstream ]; then [ \"${{FAKE_UPSTREAM:-aligned}}\" = behind ] && exit 1; exit 0; fi\n"
            f"if [ \"$mode\" = --check ]; then [ \"${{FAKE_SYNC_MODE:-{fake_mode}}}\" = drift ] && exit 1; exit 0; fi\n"
            "mkdir -p harness; printf 'synced\\n' > harness/managed.txt; "
            "printf '{\\\"source_commit\\\":\\\"%s\\\",\\\"files\\\":{}}\\n' \"$(git -C \"$source_path\" rev-parse HEAD)\" > .harness-version\n"
            "exit 0\n",
            encoding="utf-8",
        )
        sync.chmod(0o755)

        bin_dir = consumer.parent / "fake-bin"
        bin_dir.mkdir(exist_ok=True)
        gh = bin_dir / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            ": \"${GH_EVAL_LOG:?GH_EVAL_LOG must be set}\"\n"
            ": \"${GH_TOKEN:?GH_TOKEN must be set}\"\n"
            "printf '%s\\n' \"$*\" >> \"$GH_EVAL_LOG\"\n"
            "case \"$*\" in\n"
            "  *'pr list'*) case \"${GH_EVAL_PR_MODE:-state}\" in "
            "malformed) printf 'not-json\\n';; duplicate) printf '[{\"number\":1},{\"number\":2}]\\n';; "
            "*) if [ -f \"${GH_EVAL_PR_STATE}\" ]; then printf '[{\"number\":1}]\\n'; else printf '[]\\n'; fi;; esac ;;\n"
            "  *'pr create'*) : > \"${GH_EVAL_PR_STATE}\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)
        git_wrapper = bin_dir / "git"
        git_wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            "if [ \"${GIT_EVAL_FETCH_MODE:-}\" = auth ] && [ \"${1:-}\" = fetch ] && "
            "printf '%s' \"$*\" | grep -Fq 'automation/aes-harness-bump'; then\n"
            "  printf '%s\\n' 'fatal: authentication failed while fetching stable ref' >&2\n"
            "  exit 128\n"
            "fi\n"
            "exec /usr/bin/git \"$@\"\n",
            encoding="utf-8",
        )
        git_wrapper.chmod(0o755)
        return target, bin_dir

    def _commit_fixture_state(self, consumer: Path, source_commit: str) -> None:
        (consumer / ".harness-version").write_text(
            json.dumps({"source_commit": source_commit, "files": {}}) + "\n",
            encoding="utf-8",
        )
        self._git("add", "-A", cwd=consumer)
        self._git("commit", "-m", "consumer harness fixture", cwd=consumer)
        if subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=consumer,
            capture_output=True, text=True
        ).returncode == 0:
            self._git("push", "origin", "HEAD:develop", cwd=consumer)

    def _run_wrapper(
        self, consumer: Path, source: Path, bin_dir: Path, log: Path,
        script: Path | None = None, **extra_env: str
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        # Un runner GitHub non esporta un'identita' Git. Se la lasciassimo
        # passare dall'ambiente di chi lancia l'eval, ogni test runtime
        # commiterebbe con l'identita' dello sviluppatore e il caso reale
        # resterebbe scoperto.
        for inherited in (
            "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL",
            "GIT_COMMITTER_NAME",
            "GIT_COMMITTER_EMAIL",
        ):
            env.pop(inherited, None)
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "GH_TOKEN": "test-token-from-environment",
                "GH_EVAL_LOG": str(log),
                "GH_EVAL_PR_STATE": str(log.with_suffix(".pr")),
                **extra_env,
            }
        )
        return subprocess.run(
            [str(script or consumer / "harness" / "scripts" / "aes-bump.sh"),
             "--source", str(source)],
            cwd=consumer,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_aligned_consumer_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aes-bump-aligned-") as temp:
            root = Path(temp)
            source = self._repo_fixture(root, "aes")
            consumer = self._repo_fixture(root, "consumer")
            source_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=source,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            log = root / "gh.log"
            _, bin_dir = self._install_target_and_fake_dependencies(consumer, source)
            self._commit_fixture_state(consumer, source_commit)
            before = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=consumer,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            result = self._run_wrapper(consumer, source, bin_dir, log)
            self.assertEqual(result.returncode, 0, result.stderr)
            after = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=consumer,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(before, after)
            self.assertFalse(log.exists(), "Un no-op non deve interrogare GitHub.")

    def test_replay_reuses_existing_pr_without_empty_commit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aes-bump-idempotent-") as temp:
            root = Path(temp)
            source = self._repo_fixture(root, "aes")
            consumer = self._consumer_fixture(root)
            old_source_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=source,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            _, bin_dir = self._install_target_and_fake_dependencies(consumer, source)
            self._commit_fixture_state(consumer, old_source_commit)
            (source / "tracked.txt").write_text("new AES harness\n", encoding="utf-8")
            self._git("add", "tracked.txt", cwd=source)
            self._git("commit", "-m", "advance harness", cwd=source)
            log = root / "gh.log"
            result = self._run_wrapper(
                consumer, source, bin_dir, log, FAKE_UPSTREAM="behind"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self._git("switch", "develop", cwd=consumer)
            result = self._run_wrapper(
                consumer, source, bin_dir, log, FAKE_UPSTREAM="behind"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            commits = subprocess.run(
                ["git", "rev-list", "--all", "--count"],
                cwd=consumer,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(
                commits,
                "3",
                "Un secondo run sullo stesso HEAD non deve produrre commit vuoti.",
            )
            self.assertEqual(
                sum("pr create" in line for line in log.read_text().splitlines()),
                1,
                "La PR stabile va creata una sola volta.",
            )

            remote_branch = subprocess.run(
                ["git", "ls-remote", "--heads", "origin", STABLE_BRANCH],
                cwd=consumer,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn(STABLE_BRANCH, remote_branch)

    def test_local_drift_aborts_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aes-bump-drift-") as temp:
            root = Path(temp)
            source = self._repo_fixture(root, "aes")
            consumer = self._consumer_fixture(root)
            source_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=source, check=True,
                capture_output=True, text=True
            ).stdout.strip()
            log = root / "gh.log"
            target, bin_dir = self._install_target_and_fake_dependencies(
                consumer, source, fake_mode="drift"
            )
            self._commit_fixture_state(consumer, source_commit)
            marker = consumer / "human-change.txt"
            marker.write_text("keep me\n", encoding="utf-8")
            before = marker.read_text(encoding="utf-8")
            result = self._run_wrapper(consumer, source, bin_dir, log)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), before)
            self.assertFalse(
                log.exists(),
                f"Il drift deve bloccare prima di chiamare gh ({target}).",
            )

    def test_human_branch_work_is_not_force_overwritten(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aes-bump-human-branch-") as temp:
            root = Path(temp)
            source = self._repo_fixture(root, "aes")
            consumer = self._consumer_fixture(root)
            old_source_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=source, check=True,
                capture_output=True, text=True
            ).stdout.strip()
            log = root / "gh.log"
            _, bin_dir = self._install_target_and_fake_dependencies(consumer, source)
            self._commit_fixture_state(consumer, old_source_commit)
            (source / "tracked.txt").write_text("new AES harness\n", encoding="utf-8")
            self._git("add", "tracked.txt", cwd=source)
            self._git("commit", "-m", "advance harness", cwd=source)
            first = self._run_wrapper(consumer, source, bin_dir, log, FAKE_UPSTREAM="behind")
            self.assertEqual(first.returncode, 0, first.stderr)
            (consumer / "human.txt").write_text("keep human work\n", encoding="utf-8")
            self._git("add", "human.txt", cwd=consumer)
            self._git("commit", "-m", "human work", cwd=consumer)
            before = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=consumer, check=True,
                capture_output=True, text=True
            ).stdout.strip()
            (source / "tracked.txt").write_text("second AES harness\n", encoding="utf-8")
            self._git("add", "tracked.txt", cwd=source)
            self._git("commit", "-m", "advance harness again", cwd=source)
            result = self._run_wrapper(consumer, source, bin_dir, log, FAKE_UPSTREAM="behind")
            self.assertNotEqual(result.returncode, 0)
            after = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=consumer, check=True,
                capture_output=True, text=True
            ).stdout.strip()
            self.assertEqual(before, after)
            self.assertEqual((consumer / "human.txt").read_text(encoding="utf-8"), "keep human work\n")

    def test_prose_mention_of_marker_is_not_automation_trailer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aes-bump-marker-prose-") as temp:
            root = Path(temp)
            source = self._repo_fixture(root, "aes")
            consumer = self._consumer_fixture(root)
            old_source_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=source, check=True,
                capture_output=True, text=True
            ).stdout.strip()
            log = root / "gh.log"
            _, bin_dir = self._install_target_and_fake_dependencies(consumer, source)
            self._commit_fixture_state(consumer, old_source_commit)
            (source / "tracked.txt").write_text("new AES harness\n", encoding="utf-8")
            self._git("add", "tracked.txt", cwd=source)
            self._git("commit", "-m", "advance harness", cwd=source)
            first = self._run_wrapper(consumer, source, bin_dir, log, FAKE_UPSTREAM="behind")
            self.assertEqual(first.returncode, 0, first.stderr)
            (consumer / "human-prose.txt").write_text("keep human work\n", encoding="utf-8")
            self._git("add", "human-prose.txt", cwd=consumer)
            self._git(
                "commit", "-m",
                "human note AES-Automation: aes-harness-bump is only documentation",
                cwd=consumer,
            )
            self._git("push", "origin", "HEAD:refs/heads/" + STABLE_BRANCH, cwd=consumer)
            before = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=consumer, check=True,
                capture_output=True, text=True
            ).stdout.strip()
            (source / "tracked.txt").write_text("second AES harness\n", encoding="utf-8")
            self._git("add", "tracked.txt", cwd=source)
            self._git("commit", "-m", "advance harness again", cwd=source)
            result = self._run_wrapper(consumer, source, bin_dir, log, FAKE_UPSTREAM="behind")
            self.assertNotEqual(result.returncode, 0)
            after = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=consumer, check=True,
                capture_output=True, text=True
            ).stdout.strip()
            self.assertEqual(before, after)

    def test_non_not_found_stable_fetch_error_aborts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aes-bump-fetch-error-") as temp:
            root = Path(temp)
            source = self._repo_fixture(root, "aes")
            consumer = self._consumer_fixture(root)
            old_source_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=source, check=True,
                capture_output=True, text=True
            ).stdout.strip()
            log = root / "gh.log"
            _, bin_dir = self._install_target_and_fake_dependencies(consumer, source)
            self._commit_fixture_state(consumer, old_source_commit)
            (source / "tracked.txt").write_text("new AES harness\n", encoding="utf-8")
            self._git("add", "tracked.txt", cwd=source)
            self._git("commit", "-m", "advance harness", cwd=source)
            result = self._run_wrapper(
                consumer, source, bin_dir, log,
                FAKE_UPSTREAM="behind", GIT_EVAL_FETCH_MODE="auth",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot fetch stable ref", result.stderr)
            self.assertFalse(log.exists())

    def test_trailer_marked_divergent_remote_history_aborts_before_push(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aes-bump-divergent-") as temp:
            root = Path(temp)
            source = self._repo_fixture(root, "aes")
            consumer = self._consumer_fixture(root)
            old_source_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=source, check=True,
                capture_output=True, text=True
            ).stdout.strip()
            log = root / "gh.log"
            _, bin_dir = self._install_target_and_fake_dependencies(consumer, source)
            self._commit_fixture_state(consumer, old_source_commit)
            # Create develop D1, then a stable branch from the old root with
            # the automation trailer. The trailer alone must not bless a
            # history that is not a descendant of current develop.
            self._git("switch", "-c", "divergent", cwd=consumer)
            (consumer / "divergent.txt").write_text("side history\n", encoding="utf-8")
            self._git("add", "divergent.txt", cwd=consumer)
            self._git(
                "commit", "-m", "side automation",
                "-m", "AES-Automation: aes-harness-bump", cwd=consumer,
            )
            divergent_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=consumer, check=True,
                capture_output=True, text=True
            ).stdout.strip()
            self._git("switch", "develop", cwd=consumer)
            (consumer / "develop.txt").write_text("develop history\n", encoding="utf-8")
            self._git("add", "develop.txt", cwd=consumer)
            self._git("commit", "-m", "advance consumer develop", cwd=consumer)
            self._git("push", "origin", "HEAD:develop", cwd=consumer)
            self._git("push", "origin", f"{divergent_sha}:refs/heads/{STABLE_BRANCH}", cwd=consumer)
            self._git("fetch", "origin", STABLE_BRANCH, cwd=consumer)
            self._git("switch", "develop", cwd=consumer)
            (source / "tracked.txt").write_text("new AES harness\n", encoding="utf-8")
            self._git("add", "tracked.txt", cwd=source)
            self._git("commit", "-m", "advance harness", cwd=source)
            result = self._run_wrapper(consumer, source, bin_dir, log, FAKE_UPSTREAM="behind")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("stable branch", result.stderr)
            self.assertEqual(
                subprocess.run(
                    ["git", "rev-parse", f"refs/remotes/origin/{STABLE_BRANCH}"],
                    cwd=consumer, check=True, capture_output=True, text=True,
                ).stdout.strip(),
                divergent_sha,
            )

    def test_real_bump_and_replay_use_real_aes_sync(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aes-bump-real-sync-") as temp:
            root = Path(temp)
            source = self._repo_fixture(root, "aes")
            scripts = source / "harness" / "scripts"
            scripts.mkdir(parents=True)
            (source / "harness" / "config").mkdir(parents=True)
            shutil.copy2(REPO / "harness" / "scripts" / "aes-sync.sh", scripts / "aes-sync.sh")
            shutil.copy2(SCRIPT, scripts / "aes-bump.sh")
            (source / "harness" / "config" / "harness.conf").write_text(
                "# fixture\nLINKED_FILES=\n", encoding="utf-8"
            )
            (source / "managed.txt").write_text("v1\n", encoding="utf-8")
            (source / "harness" / "manifest.txt").write_text(
                "copy harness/scripts/aes-sync.sh harness/scripts/aes-sync.sh\n"
                "copy harness/scripts/aes-bump.sh harness/scripts/aes-bump.sh\n"
                "copy managed.txt managed.txt\n"
                "seed harness/config/harness.conf harness/config/harness.conf\n",
                encoding="utf-8",
            )
            self._git("add", "-A", cwd=source)
            self._git("commit", "-m", "fixture AES harness", cwd=source)
            consumer = self._consumer_fixture(root)
            initial_sync = subprocess.run(
                [str(scripts / "aes-sync.sh"), "--source", str(source)],
                cwd=consumer, check=False, capture_output=True, text=True,
            )
            self.assertEqual(initial_sync.returncode, 0, initial_sync.stderr)
            self._git("add", "-A", cwd=consumer)
            self._git("commit", "-m", "install fixture harness", cwd=consumer)
            self._git("push", "origin", "HEAD:develop", cwd=consumer)
            (source / "managed.txt").write_text("v2\n", encoding="utf-8")
            self._git("add", "managed.txt", cwd=source)
            self._git("commit", "-m", "advance fixture harness", cwd=source)
            bin_dir = consumer.parent / "fake-bin"
            bin_dir.mkdir()
            gh = bin_dir / "gh"
            gh.write_text(
                "#!/usr/bin/env bash\nset -eu\n"
                ": \"${GH_EVAL_LOG:?}\"; : \"${GH_TOKEN:?}\"\n"
                "printf '%s\\n' \"$*\" >> \"$GH_EVAL_LOG\"\n"
                "case \"$*\" in\n"
                "*'pr list'*) if [ -f \"${GH_EVAL_PR_STATE}\" ]; then printf '[{\"number\":1}]\\n'; else printf '[]\\n'; fi;;\n"
                "*'pr create'*) : > \"${GH_EVAL_PR_STATE}\";;\n"
                "esac\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)
            log = root / "gh.log"
            first = self._run_wrapper(consumer, source, bin_dir, log, FAKE_UPSTREAM="behind")
            self.assertEqual(first.returncode, 0, first.stderr)
            self._git("switch", "develop", cwd=consumer)
            second = self._run_wrapper(consumer, source, bin_dir, log, FAKE_UPSTREAM="behind")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(sum("pr create" in line for line in log.read_text().splitlines()), 1)

    def test_consumer_with_a_broken_wrapper_still_heals_itself(self) -> None:
        """Il caso che l'autoriparazione esiste per risolvere.

        Il consumer ha mergiato un `aes-bump.sh` rotto, e quella copia e'
        quella registrata in `.harness-version`: e' quindi allineata, il
        controllo di drift non ha nulla da segnalare e senza autoriparazione
        il bump continuerebbe a fallire finche' qualcuno non porta il fix a
        mano nel consumer. Eseguendo la copia di AES, il run che porta la
        correzione e' anche quello che la applica.
        """
        with tempfile.TemporaryDirectory(prefix="aes-bump-self-heal-") as temp:
            root = Path(temp)
            source = self._repo_fixture(root, "aes")
            scripts = source / "harness" / "scripts"
            scripts.mkdir(parents=True)
            (source / "harness" / "config").mkdir(parents=True)
            real_sync = REPO / "harness" / "scripts" / "aes-sync.sh"
            sync_marker = root / "consumer-sync-ran"
            # Delega al sync reale, ma lascia una traccia: cosi' il test
            # distingue quale delle due copie il wrapper ha invocato.
            (scripts / "aes-sync.sh").write_text(
                "#!/usr/bin/env bash\n"
                f"printf 'x' >> {sync_marker}\n"
                f'exec {real_sync} "$@"\n',
                encoding="utf-8",
            )
            (scripts / "aes-sync.sh").chmod(0o755)
            broken = (
                "#!/usr/bin/env bash\n"
                "printf 'BROKEN CONSUMER WRAPPER RAN\\n' >&2\n"
                "exit 97\n"
            )
            (scripts / "aes-bump.sh").write_text(broken, encoding="utf-8")
            (scripts / "aes-bump.sh").chmod(0o755)
            (source / "harness" / "config" / "harness.conf").write_text(
                "# fixture\nLINKED_FILES=\n", encoding="utf-8"
            )
            (source / "managed.txt").write_text("v1\n", encoding="utf-8")
            (source / "harness" / "manifest.txt").write_text(
                "copy harness/scripts/aes-sync.sh harness/scripts/aes-sync.sh\n"
                "copy harness/scripts/aes-bump.sh harness/scripts/aes-bump.sh\n"
                "copy managed.txt managed.txt\n"
                "seed harness/config/harness.conf harness/config/harness.conf\n",
                encoding="utf-8",
            )
            self._git("add", "-A", cwd=source)
            self._git("commit", "-m", "fixture AES harness (wrapper rotto)", cwd=source)

            # Il consumer sincronizza e merge: si porta in casa il wrapper
            # rotto, e ne registra l'hash. Nessun drift da segnalare.
            consumer = self._consumer_fixture(root)
            subprocess.run(
                [str(scripts / "aes-sync.sh"), "--source", str(source)],
                cwd=consumer, check=True, capture_output=True, text=True,
            )
            self._git("add", "-A", cwd=consumer)
            self._git("commit", "-m", "install fixture harness", cwd=consumer)
            self._git("push", "origin", "HEAD:develop", cwd=consumer)

            # AES corregge entrambi gli strumenti e avanza.
            shutil.copy2(SCRIPT, scripts / "aes-bump.sh")
            (scripts / "aes-bump.sh").chmod(0o755)
            shutil.copy2(real_sync, scripts / "aes-sync.sh")
            (scripts / "aes-sync.sh").chmod(0o755)
            (source / "managed.txt").write_text("v2\n", encoding="utf-8")
            self._git("add", "-A", cwd=source)
            self._git("commit", "-m", "fix del wrapper", cwd=source)

            bin_dir = root / "fake-bin"
            bin_dir.mkdir()
            gh = bin_dir / "gh"
            gh.write_text(
                "#!/usr/bin/env bash\nset -eu\n"
                ': "${GH_EVAL_LOG:?}"; : "${GH_TOKEN:?}"\n'
                "printf '%s\\n' \"$*\" >> \"$GH_EVAL_LOG\"\n"
                "case \"$*\" in\n"
                "*'pr list'*) if [ -f \"$GH_EVAL_PR_STATE\" ]; then printf '[{\"number\":1}]\\n'; "
                "else printf '[]\\n'; fi;;\n"
                "*'pr create'*) : > \"$GH_EVAL_PR_STATE\";;\n"
                "esac\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)
            log = root / "gh.log"
            sync_marker.unlink(missing_ok=True)

            result = self._run_wrapper(
                consumer, source, bin_dir, log,
                script=scripts / "aes-bump.sh",
            )
            self.assertEqual(
                result.returncode, 0,
                "Il wrapper di AES deve completare il bump nonostante la "
                f"copia rotta nel consumer: {result.stderr}",
            )
            self.assertNotIn(
                "BROKEN CONSUMER WRAPPER RAN",
                result.stderr,
                "La copia del consumer non deve essere eseguita.",
            )
            self.assertFalse(
                sync_marker.exists(),
                "Anche aes-sync deve venire dalla sorgente: legge il manifest "
                "della sorgente, e la copia del consumer e' quella vecchia.",
            )
            self.assertIn("pr create", log.read_text(encoding="utf-8"))
            healed = subprocess.run(
                ["git", "show",
                 f"refs/remotes/origin/{STABLE_BRANCH}:harness/scripts/aes-bump.sh"],
                cwd=consumer, check=True, capture_output=True, text=True,
            ).stdout
            self.assertNotIn(
                "BROKEN CONSUMER WRAPPER RAN",
                healed,
                "Il run che porta la correzione deve anche applicarla.",
            )

    def test_repo_hooks_do_not_run_during_automation_mutations(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aes-bump-hooks-") as temp:
            root = Path(temp)
            source = self._repo_fixture(root, "aes")
            consumer = self._consumer_fixture(root)
            old_source_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=source, check=True,
                capture_output=True, text=True
            ).stdout.strip()
            log = root / "gh.log"
            _, bin_dir = self._install_target_and_fake_dependencies(consumer, source)
            self._commit_fixture_state(consumer, old_source_commit)
            hook_marker = root / "hook-ran"
            hooks = consumer / ".git" / "hooks"
            for hook_name in ("post-checkout", "pre-commit", "pre-push"):
                hook = hooks / hook_name
                hook.write_text(f"#!/usr/bin/env bash\ntouch '{hook_marker}'\n", encoding="utf-8")
                hook.chmod(0o755)
            (source / "tracked.txt").write_text("new AES harness\n", encoding="utf-8")
            self._git("add", "tracked.txt", cwd=source)
            self._git("commit", "-m", "advance harness", cwd=source)
            result = self._run_wrapper(consumer, source, bin_dir, log, FAKE_UPSTREAM="behind")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(hook_marker.exists(), "Git hooks must be disabled for automation mutations")

    def test_local_stable_branch_is_checked_when_remote_ref_is_missing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aes-bump-local-stable-") as temp:
            root = Path(temp)
            source = self._repo_fixture(root, "aes")
            consumer = self._consumer_fixture(root)
            old_source_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=source, check=True,
                capture_output=True, text=True
            ).stdout.strip()
            log = root / "gh.log"
            _, bin_dir = self._install_target_and_fake_dependencies(consumer, source)
            self._commit_fixture_state(consumer, old_source_commit)
            (source / "tracked.txt").write_text("new AES harness\n", encoding="utf-8")
            self._git("add", "tracked.txt", cwd=source)
            self._git("commit", "-m", "advance harness", cwd=source)
            first = self._run_wrapper(consumer, source, bin_dir, log, FAKE_UPSTREAM="behind")
            self.assertEqual(first.returncode, 0, first.stderr)
            self._git("push", "origin", "--delete", STABLE_BRANCH, cwd=consumer)
            self._git("update-ref", "-d", f"refs/remotes/origin/{STABLE_BRANCH}", cwd=consumer)
            (consumer / "human-local.txt").write_text("keep local work\n", encoding="utf-8")
            self._git("add", "human-local.txt", cwd=consumer)
            self._git("commit", "-m", "human local work", cwd=consumer)
            before = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=consumer, check=True,
                capture_output=True, text=True
            ).stdout.strip()
            (source / "tracked.txt").write_text("second AES harness\n", encoding="utf-8")
            self._git("add", "tracked.txt", cwd=source)
            self._git("commit", "-m", "advance harness again", cwd=source)
            result = self._run_wrapper(consumer, source, bin_dir, log, FAKE_UPSTREAM="behind")
            self.assertNotEqual(result.returncode, 0)
            after = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=consumer, check=True,
                capture_output=True, text=True
            ).stdout.strip()
            self.assertEqual(before, after)

    def test_malformed_or_duplicate_pr_aborts_before_push(self) -> None:
        for pr_mode in ("malformed", "duplicate"):
            with self.subTest(pr_mode=pr_mode), tempfile.TemporaryDirectory(
                prefix="aes-bump-pr-state-"
            ) as temp:
                root = Path(temp)
                source = self._repo_fixture(root, "aes")
                consumer = self._consumer_fixture(root)
                old_source_commit = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=source, check=True,
                    capture_output=True, text=True
                ).stdout.strip()
                log = root / "gh.log"
                _, bin_dir = self._install_target_and_fake_dependencies(consumer, source)
                self._commit_fixture_state(consumer, old_source_commit)
                (source / "tracked.txt").write_text("new AES harness\n", encoding="utf-8")
                self._git("add", "tracked.txt", cwd=source)
                self._git("commit", "-m", "advance harness", cwd=source)
                result = self._run_wrapper(
                    consumer, source, bin_dir, log,
                    FAKE_UPSTREAM="behind", GH_EVAL_PR_MODE=pr_mode,
                )
                self.assertNotEqual(result.returncode, 0, result.stderr)
                remote_branch = subprocess.run(
                    ["git", "ls-remote", "--heads", "origin", STABLE_BRANCH],
                    cwd=consumer, check=True, capture_output=True, text=True,
                ).stdout
                self.assertNotIn(STABLE_BRANCH, remote_branch)

    def test_source_checkout_nested_in_the_consumer_is_never_committed(self) -> None:
        """Il layout che il workflow produce davvero.

        `actions/checkout` mette AES in `<consumer>/.aes-source`, dentro
        l'albero di lavoro del consumer. E' l'unico caso che esercita i
        pathspec `:(exclude)$source_relative`: senza di essi il checkout di
        AES conta come albero sporco, o finisce dentro il commit di bump.
        Tutte le altre fixture tengono la sorgente fuori dal consumer, quindi
        questo ramo non era coperto.
        """
        with tempfile.TemporaryDirectory(prefix="aes-bump-nested-") as temp:
            root = Path(temp)
            consumer = self._consumer_fixture(root)
            source = self._repo_fixture(consumer, ".aes-source")
            _, bin_dir = self._install_target_and_fake_dependencies(consumer, source)
            old_source_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=source, check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            # Come fa il wrapper: la sorgente annidata resta fuori dall'indice.
            (consumer / ".harness-version").write_text(
                json.dumps({"source_commit": old_source_commit, "files": {}}) + "\n",
                encoding="utf-8",
            )
            self._git("add", "-A", "--", ".", ":(exclude).aes-source", cwd=consumer)
            self._git("commit", "-m", "consumer harness fixture", cwd=consumer)
            self._git("push", "origin", "HEAD:develop", cwd=consumer)

            (source / "tracked.txt").write_text("new AES harness\n", encoding="utf-8")
            self._git("add", "tracked.txt", cwd=source)
            self._git("commit", "-m", "advance harness", cwd=source)

            log = root / "gh.log"
            result = self._run_wrapper(
                consumer, source, bin_dir, log, FAKE_UPSTREAM="behind"
            )
            self.assertEqual(
                result.returncode, 0,
                "Il checkout di AES annidato non deve far fallire il bump: "
                f"{result.stderr}",
            )
            tracked = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only",
                 f"refs/remotes/origin/{STABLE_BRANCH}"],
                cwd=consumer, check=True, capture_output=True, text=True,
            ).stdout.splitlines()
            self.assertFalse(
                [path for path in tracked if path.startswith(".aes-source")],
                "Il checkout di AES non deve finire nel commit del consumer.",
            )

    def test_bump_commits_without_a_configured_git_identity(self) -> None:
        """Il caso del runner: nessuna identita' Git, da nessuna parte.

        `actions/checkout` non configura `user.name`/`user.email`, e il
        runner non ha config globale o di sistema. Le altre fixture runtime
        configurano l'identita' locale nel consumer, quindi non vedono
        questo caso: senza questo test il wrapper torna a fallire in CI con
        "Author identity unknown" pur avendo la suite verde.
        """
        with tempfile.TemporaryDirectory(prefix="aes-bump-no-identity-") as temp:
            root = Path(temp)
            source = self._repo_fixture(root, "aes")
            consumer = self._consumer_fixture(root)
            old_source_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=source, check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            log = root / "gh.log"
            _, bin_dir = self._install_target_and_fake_dependencies(consumer, source)
            self._commit_fixture_state(consumer, old_source_commit)
            (source / "tracked.txt").write_text("new AES harness\n", encoding="utf-8")
            self._git("add", "tracked.txt", cwd=source)
            self._git("commit", "-m", "advance harness", cwd=source)

            # Da qui in poi il consumer e' indistinguibile da un checkout
            # appena fatto su un runner: nessuna identita' locale, e nessuna
            # config globale o di sistema da cui ereditarne una.
            self._git("config", "--unset", "user.email", cwd=consumer)
            self._git("config", "--unset", "user.name", cwd=consumer)

            result = self._run_wrapper(
                consumer, source, bin_dir, log,
                FAKE_UPSTREAM="behind",
                GIT_CONFIG_GLOBAL=os.devnull,
                GIT_CONFIG_SYSTEM=os.devnull,
            )
            self.assertEqual(
                result.returncode, 0,
                "Il bump deve completare senza identita' Git configurata: "
                f"{result.stderr}",
            )
            self.assertIn(
                "pr create",
                log.read_text(encoding="utf-8"),
                "Il bump deve arrivare fino all'apertura della PR, non "
                "fermarsi al commit.",
            )
            pushed = subprocess.run(
                ["git", "log", "-1", "--format=%an <%ae>%n%B",
                 f"refs/remotes/origin/{STABLE_BRANCH}"],
                cwd=consumer, check=True, capture_output=True, text=True,
            ).stdout
            self.assertIn(
                "AES harness bump <aes-harness-bump@users.noreply.github.com>",
                pushed,
                "Il commit di automazione deve essere attribuito "
                "all'automazione, non a un'identita' ambientale.",
            )
            self.assertIn(
                "AES-Automation: aes-harness-bump",
                pushed,
                "L'identita' non deve sostituire il trailer che marca il "
                "commit come automazione.",
            )


if __name__ == "__main__":
    unittest.main()
