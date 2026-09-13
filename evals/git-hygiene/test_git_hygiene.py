"""Eval deterministico per l'igiene della sessione Git.

Verifica gli script `harness/scripts/git-orient.sh` e
`harness/scripts/feature-start.sh` eseguendoli su repository Git temporanei
sintetici, ciascuno con un bare repository locale come `origin`. I test non
usano la rete né lo stato del repository reale; copiano anche la configurazione
harness nel repository temporaneo, così gli script vengono verificati nel modo
in cui saranno distribuiti.

Lo script non è ancora presente durante la prima fase di sviluppo: in quel
caso i test devono fallire, senza errori di import o di sintassi.

Esecuzione: `python3 -m unittest evals/git-hygiene/test_git_hygiene.py -v`
"""

from contextlib import contextmanager
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SCRIPT_SOURCE = REPO / "harness" / "scripts" / "git-orient.sh"
FEATURE_START_SOURCE = REPO / "harness" / "scripts" / "feature-start.sh"
FEATURE_DONE_SOURCE = REPO / "harness" / "scripts" / "feature-done.sh"
REVIEW_LOCALE_SOURCE = REPO / "harness" / "scripts" / "review-locale.sh"
WAIT_CHECKS_SOURCE = REPO / "harness" / "scripts" / "wait-checks.sh"
CONFIG_SOURCE = REPO / "harness" / "config" / "harness.conf"


class TestHarnessConfiguration(unittest.TestCase):
    def test_settings_has_no_duplicate_keys(self):
        """Una chiave JSON ripetuta non e' un errore: e' una perdita silenziosa.

        Ogni parser tiene l'ultima occorrenza e scarta le precedenti senza
        dire nulla. `.claude/settings.json` conteneva due volte
        `hooks.PostToolUse`, e quella scartata era la variante con il
        fallback su CLAUDE_PROJECT_DIR e il timeout: `aes-sync` propagava
        quindi ai consumer la versione peggiore. `json.load` normale non lo
        vede, ed e' esattamente per questo che il difetto e' passato.
        """
        def reject_duplicates(pairs):
            seen = set()
            for key, _ in pairs:
                if key in seen:
                    self.fail(f"chiave duplicata in .claude/settings.json: {key!r}")
                seen.add(key)
            return dict(pairs)

        json.loads(
            (REPO / ".claude" / "settings.json").read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
        )

    def test_posttooluse_hook_resolves_root_without_relying_on_cwd(self):
        """Il monitor deve trovare la root anche con la CWD fuori dal repo."""
        settings = json.loads(
            (REPO / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
        for entry in settings.get("hooks", {}).get("PostToolUse", []):
            for hook in entry.get("hooks", []):
                if "posttooluse.sh" in hook.get("command", ""):
                    self.assertIn("CLAUDE_PROJECT_DIR", hook["command"])
                    self.assertIn("timeout", hook)
                    return
        self.fail("hook PostToolUse del monitor assente da .claude/settings.json")

    def test_session_start_hook_present(self):
        settings = json.loads(
            (REPO / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
        for entry in settings.get("hooks", {}).get("SessionStart", []):
            for hook in entry.get("hooks", []):
                if "git-orient.sh" in hook.get("command", ""):
                    return
        self.fail(
            "SessionStart hook referencing git-orient.sh not found in "
            ".claude/settings.json"
        )


class TestGitOrient(unittest.TestCase):
    @staticmethod
    def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
        """Esegue un comando Git nel repository temporaneo."""
        git_args = list(args)
        if git_args[:1] == ["cherry-pick"]:
            # Evita il fast-forward implicito: il fixture deve avere un commit
            # distinto ma con una patch già contenuta in origin/develop.
            commit = git_args[-1]
            diff = subprocess.run(
                ["git", "diff", f"{commit}^", commit],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "apply", "--index"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
                input=diff.stdout,
            )
            return subprocess.run(
                ["git", "commit", "-m", "merged fixture"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
        return subprocess.run(
            ["git", *git_args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )

    def _commit(self, repo: Path, filename: str, content: str, message: str) -> None:
        (repo / filename).write_text(content, encoding="utf-8")
        self._git(repo, "add", filename)
        self._git(repo, "commit", "-m", message)

    def _stage_harness(self, repo: Path) -> Path:
        """Copia config e script nel layout atteso dal repository target."""
        config = repo / "harness" / "config" / "harness.conf"
        config.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CONFIG_SOURCE, config)

        script = repo / "harness" / "scripts" / "git-orient.sh"
        if SCRIPT_SOURCE.exists():
            script.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SCRIPT_SOURCE, script)

        feature_script = repo / "harness" / "scripts" / "feature-start.sh"
        if FEATURE_START_SOURCE.exists():
            feature_script.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(FEATURE_START_SOURCE, feature_script)

        feature_done_script = repo / "harness" / "scripts" / "feature-done.sh"
        if FEATURE_DONE_SOURCE.exists():
            feature_done_script.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(FEATURE_DONE_SOURCE, feature_done_script)

        review_script = repo / "harness" / "scripts" / "review-locale.sh"
        if REVIEW_LOCALE_SOURCE.exists():
            review_script.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REVIEW_LOCALE_SOURCE, review_script)

        wait_checks_script = repo / "harness" / "scripts" / "wait-checks.sh"
        if WAIT_CHECKS_SOURCE.exists():
            wait_checks_script.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(WAIT_CHECKS_SOURCE, wait_checks_script)
        return script

    @contextmanager
    def _temporary_repo(self):
        """Crea un repo di lavoro e un bare remote locale indipendenti."""
        with tempfile.TemporaryDirectory(prefix="git-hygiene-") as directory:
            root = Path(directory)
            remote = root / "origin.git"
            repo = root / "repo"

            subprocess.run(
                ["git", "init", "--bare", str(remote)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "init", "-b", "develop", str(repo)],
                check=True,
                capture_output=True,
                text=True,
            )
            self._git(repo, "config", "user.email", "git-hygiene@example.test")
            self._git(repo, "config", "user.name", "Git Hygiene Eval")
            self._git(repo, "remote", "add", "origin", str(remote))
            self._commit(repo, "README", "base\n", "base commit")
            self._git(repo, "push", "--set-upstream", "origin", "develop")
            script = self._stage_harness(repo)

            yield root, repo, remote, script

    def _run_orient(
        self, repo: Path, script: Path, *arguments: str
    ) -> subprocess.CompletedProcess:
        """Esegue lo script staged; bash rende l'assenza attesa un FAIL pulito."""
        return subprocess.run(
            ["bash", str(script), *arguments],
            cwd=repo,
            capture_output=True,
            text=True,
        )

    def _assert_verdict(
        self,
        process: subprocess.CompletedProcess,
        verdict: str,
        expected_returncode: int = 0,
    ) -> str:
        output = process.stdout + process.stderr
        self.assertEqual(
            process.returncode,
            expected_returncode,
            f"unexpected exit code {process.returncode}: {output}",
        )
        self.assertIn(f"VERDICT: {verdict}", output)
        return output

    def _create_stale_branch(self, repo: Path, commits: int) -> None:
        self._git(repo, "checkout", "-b", "feature/stale", "origin/develop")
        self._git(repo, "checkout", "develop")
        for index in range(commits):
            self._commit(
                repo,
                f"develop-{index}.txt",
                f"commit {index}\n",
                f"develop commit {index}",
            )
        self._git(repo, "push", "origin", "develop")
        self._git(repo, "checkout", "feature/stale")

    def test_orient_fresh_branch(self):
        with self._temporary_repo() as (_, repo, _, script):
            self._git(repo, "checkout", "-b", "feature/fresh", "origin/develop")

            process = self._run_orient(repo, script)

            self._assert_verdict(process, "OK")

    def test_orient_stale_branch(self):
        with self._temporary_repo() as (_, repo, _, script):
            self._create_stale_branch(repo, commits=3)

            process = self._run_orient(repo, script)
            output = self._assert_verdict(process, "STALE")

            self.assertIn("behind=3", output)

    def test_orient_merged_branch(self):
        with self._temporary_repo() as (_, repo, _, script):
            self._git(repo, "checkout", "-b", "feature/merged", "origin/develop")
            self._commit(repo, "merged.txt", "already merged\n", "feature change")
            feature_commit = self._git(repo, "rev-parse", "HEAD").stdout.strip()

            self._git(repo, "checkout", "develop")
            self._git(repo, "cherry-pick", feature_commit)
            self._git(repo, "push", "origin", "develop")
            self._git(repo, "checkout", "feature/merged")

            process = self._run_orient(repo, script)

            self._assert_verdict(process, "DEAD")

    def test_orient_gone_upstream(self):
        with self._temporary_repo() as (_, repo, _, script):
            self._git(repo, "checkout", "-b", "feature/gone", "origin/develop")
            self._git(repo, "push", "--set-upstream", "origin", "feature/gone")
            self._git(repo, "push", "origin", "--delete", "feature/gone")

            process = self._run_orient(repo, script)

            self._assert_verdict(process, "DEAD")

    def test_orient_protected_branch(self):
        with self._temporary_repo() as (_, repo, _, script):
            process = self._run_orient(repo, script)

            self._assert_verdict(process, "PROTECTED")

    def test_orient_offline_is_non_fatal(self):
        with self._temporary_repo() as (root, repo, _, script):
            self._create_stale_branch(repo, commits=2)
            unreachable_remote = root / "remote-that-does-not-exist.git"
            self._git(repo, "remote", "set-url", "origin", str(unreachable_remote))

            process = self._run_orient(repo, script)
            output = self._assert_verdict(process, "STALE")

            self.assertEqual(process.returncode, 0)
            lowered = output.lower()
            self.assertIn("warning", lowered)
            self.assertIn("fetch", lowered)

    def test_orient_check_mode_fails_on_stale(self):
        with self._temporary_repo() as (_, repo, _, script):
            self._create_stale_branch(repo, commits=3)

            process = self._run_orient(repo, script, "--check")
            output = process.stdout + process.stderr

            self.assertNotEqual(process.returncode, 0)
            self.assertIn("VERDICT: STALE", output)
            self.assertIn("behind=3", output)


class TestFeatureStart(unittest.TestCase):
    """Verifica la creazione di worktree per nuove feature."""

    _git = staticmethod(TestGitOrient._git)
    _commit = TestGitOrient._commit
    _stage_harness = TestGitOrient._stage_harness
    _temporary_repo = TestGitOrient._temporary_repo

    @staticmethod
    def _worktree_path(root: Path, repo: Path, feature_name: str) -> Path:
        """Restituisce il path atteso da WORKTREE_ROOT relativo."""
        return root / ".worktrees" / repo.name / feature_name

    def _run_feature_start(
        self,
        repo: Path,
        feature_name: str,
        *arguments: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        """Esegue lo script staged e fallisce chiaramente se non esiste ancora."""
        script = repo / "harness" / "scripts" / "feature-start.sh"
        self.assertTrue(script.is_file(), "feature-start.sh non è presente")
        env = os.environ.copy()
        if environment:
            env.update(environment)
        return subprocess.run(
            ["bash", str(script), *arguments, feature_name],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
        )

    def _worktree_count(self, repo: Path) -> int:
        listing = self._git(repo, "worktree", "list", "--porcelain").stdout
        return sum(line.startswith("worktree ") for line in listing.splitlines())

    def test_feature_start_fetch_uses_trusted_ephemeral_askpass(self):
        """The persist-credentials=false path must not put GH_TOKEN in argv."""
        source = FEATURE_START_SOURCE.read_text(encoding="utf-8")

        self.assertIn('GIT_ASKPASS="$_aes_fetch_askpass"', source)
        self.assertIn("git -c credential.helper= fetch --prune", source)
        self.assertIn('GH_TOKEN:-', source)
        self.assertIn("trap '_aes_cleanup_askpass' EXIT INT TERM", source)
        self.assertNotRegex(source, r"git fetch[^\n]*GH_TOKEN")

    def test_feature_start_removes_askpass_when_fetch_fails(self):
        with self._temporary_repo() as (root, repo, _, _):
            unreachable_remote = root / "missing-origin.git"
            self._git(repo, "remote", "set-url", "origin", str(unreachable_remote))
            temp_dir = root / "askpass-tmp"
            temp_dir.mkdir()

            process = self._run_feature_start(
                repo,
                "fetch-fails",
                environment={
                    "GH_TOKEN": "secret-that-must-not-leak",
                    "TMPDIR": str(temp_dir),
                },
            )

            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(
                list(temp_dir.glob("aes-git-askpass.*")),
                [],
                "credential helper temporaneo lasciato dopo fetch failure",
            )

    def test_feature_start_creates_worktree(self):
        with self._temporary_repo() as (root, repo, _, _):
            process = self._run_feature_start(repo, "esempio")
            output = process.stdout + process.stderr
            self.assertEqual(process.returncode, 0, output)

            worktree = self._worktree_path(root, repo, "esempio")
            self.assertTrue(worktree.is_dir())
            self.assertNotIn(repo.resolve(), worktree.resolve().parents)

            branch_commit = self._git(
                repo, "rev-parse", "refs/heads/feature/esempio"
            ).stdout.strip()
            origin_commit = self._git(repo, "rev-parse", "origin/develop").stdout.strip()
            self.assertEqual(branch_commit, origin_commit)

    def test_feature_start_symlinks_linked_files(self):
        with self._temporary_repo() as (root, repo, _, _):
            content = "secret-for-feature-start\n"
            env_file = repo / ".env"
            env_file.write_text(content, encoding="utf-8")

            process = self._run_feature_start(repo, "esempio")
            output = process.stdout + process.stderr
            self.assertEqual(process.returncode, 0, output)

            worktree_env = self._worktree_path(root, repo, "esempio") / ".env"
            self.assertTrue(worktree_env.is_symlink())
            link_target = worktree_env.readlink()
            self.assertTrue(link_target.is_absolute())
            self.assertEqual(link_target, env_file.resolve())
            self.assertEqual(worktree_env.read_text(encoding="utf-8"), content)

    def test_feature_start_excludes_linked_files(self):
        with self._temporary_repo() as (root, repo, _, _):
            (repo / ".gitignore").write_text("shared/\n", encoding="utf-8")
            self._git(repo, "add", ".gitignore")
            self._git(repo, "commit", "-m", "ignore shared directory")
            self._git(repo, "push", "origin", "develop")
            (repo / "shared").mkdir()

            config = repo / "harness" / "config" / "harness.conf"
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    'LINKED_FILES=".env"', 'LINKED_FILES="shared"'
                ),
                encoding="utf-8",
            )

            process = self._run_feature_start(repo, "esempio")
            output = process.stdout + process.stderr
            self.assertEqual(process.returncode, 0, output)

            worktree = self._worktree_path(root, repo, "esempio")
            status = self._git(worktree, "status", "--porcelain").stdout
            self.assertEqual(status, "", status)

    def test_feature_start_skips_missing_linked_files(self):
        with self._temporary_repo() as (root, repo, _, _):
            self.assertFalse((repo / ".env").exists())

            process = self._run_feature_start(repo, "esempio")
            output = process.stdout + process.stderr
            self.assertEqual(process.returncode, 0, output)

            worktree = self._worktree_path(root, repo, "esempio")
            self.assertTrue(worktree.is_dir())
            self.assertFalse((worktree / ".env").exists())

    def test_feature_start_rejects_existing_branch(self):
        with self._temporary_repo() as (root, repo, _, _):
            self._git(repo, "branch", "feature/esempio", "origin/develop")
            worktree = self._worktree_path(root, repo, "esempio")
            before = self._worktree_count(repo)

            process = self._run_feature_start(repo, "esempio")
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(self._worktree_count(repo), before)
            self.assertFalse(worktree.exists())

    def test_feature_start_adopts_existing_remote_branch(self):
        with self._temporary_repo() as (root, repo, _, _):
            branch = "feature/42-esempio"
            self._git(
                repo,
                "push",
                "origin",
                f"origin/develop:refs/heads/{branch}",
            )

            process = self._run_feature_start(repo, "42-esempio", "--adopt")
            output = process.stdout + process.stderr

            self.assertEqual(process.returncode, 0, output)
            worktree = self._worktree_path(root, repo, "42-esempio")
            self.assertTrue(worktree.is_dir())
            self.assertEqual(
                self._git(worktree, "rev-parse", "HEAD").stdout.strip(),
                self._git(repo, "rev-parse", f"origin/{branch}").stdout.strip(),
            )
            self.assertEqual(
                self._git(worktree, "rev-parse", "--abbrev-ref", "@{upstream}")
                .stdout.strip(),
                f"origin/{branch}",
            )

    def test_feature_start_adopt_rejects_missing_remote_branch(self):
        with self._temporary_repo() as (root, repo, _, _):
            worktree = self._worktree_path(root, repo, "42-assente")
            before = self._worktree_count(repo)

            process = self._run_feature_start(repo, "42-assente", "--adopt")

            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(self._worktree_count(repo), before)
            self.assertFalse(worktree.exists())

    def test_feature_start_without_adopt_rejects_remote_branch(self):
        with self._temporary_repo() as (root, repo, _, _):
            branch = "feature/42-esistente"
            self._git(
                repo,
                "push",
                "origin",
                f"origin/develop:refs/heads/{branch}",
            )
            worktree = self._worktree_path(root, repo, "42-esistente")
            before = self._worktree_count(repo)

            process = self._run_feature_start(repo, "42-esistente")

            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(self._worktree_count(repo), before)
            self.assertFalse(worktree.exists())


class TestFeatureDone(unittest.TestCase):
    """Verifica la chiusura sicura di worktree per feature."""

    _git = staticmethod(TestGitOrient._git)
    _commit = TestGitOrient._commit
    _stage_harness = TestGitOrient._stage_harness
    _temporary_repo = TestGitOrient._temporary_repo
    _worktree_path = staticmethod(TestFeatureStart._worktree_path)
    _worktree_count = TestFeatureStart._worktree_count

    def _run_feature_done(
        self, root: Path, repo: Path, feature_name: str, pr_state: str
    ) -> subprocess.CompletedProcess:
        """Esegue feature-done con un gh stub locale e deterministico."""
        script = repo / "harness" / "scripts" / "feature-done.sh"
        self.assertTrue(script.is_file(), "feature-done.sh non è presente")

        pr_json = {
            "MERGED": '{"state":"MERGED","mergedAt":"2026-01-01T00:00:00Z"}',
            "OPEN": '{"state":"OPEN","mergedAt":null}',
        }[pr_state]
        stub_dir = root / "gh-stub"
        stub_dir.mkdir()
        gh_stub = stub_dir / "gh"
        gh_stub.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' '{pr_json}'\n",
            encoding="utf-8",
        )
        gh_stub.chmod(0o755)

        environment = os.environ.copy()
        environment["PATH"] = os.pathsep.join(
            [str(stub_dir), environment.get("PATH", "")]
        )
        return subprocess.run(
            ["bash", str(script), feature_name],
            cwd=repo,
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_feature_done_rejects_unmerged(self):
        with self._temporary_repo() as (root, repo, _, _):
            worktree = self._worktree_path(root, repo, "esempio")
            self._git(
                repo,
                "worktree",
                "add",
                str(worktree),
                "-b",
                "feature/esempio",
                "origin/develop",
            )
            before = self._worktree_count(repo)

            process = self._run_feature_done(root, repo, "esempio", "OPEN")

            self.assertNotEqual(process.returncode, 0)
            self.assertTrue(worktree.is_dir())
            self.assertEqual(self._worktree_count(repo), before)
            self.assertTrue(
                self._git(repo, "branch", "--list", "feature/esempio").stdout.strip()
            )

    def test_feature_done_removes_merged(self):
        with self._temporary_repo() as (root, repo, _, _):
            worktree = self._worktree_path(root, repo, "esempio")
            self._git(
                repo,
                "worktree",
                "add",
                str(worktree),
                "-b",
                "feature/esempio",
                "origin/develop",
            )
            self._commit(
                worktree,
                "merged.txt",
                "already merged\n",
                "feature change",
            )
            self._git(repo, "checkout", "develop")
            self._git(
                repo,
                "merge",
                "--no-ff",
                "feature/esempio",
                "-m",
                "merge feature/esempio",
            )
            self._git(repo, "push", "origin", "develop")
            before = self._worktree_count(repo)

            process = self._run_feature_done(root, repo, "esempio", "MERGED")
            output = process.stdout + process.stderr

            self.assertEqual(process.returncode, 0, output)
            self.assertFalse(worktree.exists())
            self.assertEqual(self._worktree_count(repo), before - 1)
            self.assertFalse(
                self._git(repo, "branch", "--list", "feature/esempio").stdout.strip()
            )

    def test_feature_done_without_worktree(self):
        with self._temporary_repo() as (root, repo, _, _):
            self._git(repo, "branch", "feature/esempio")

            process = self._run_feature_done(root, repo, "esempio", "MERGED")
            output = process.stdout + process.stderr

            self.assertEqual(process.returncode, 0, output)
            self.assertFalse(
                self._git(repo, "branch", "--list", "feature/esempio").stdout.strip()
            )

    def test_feature_done_removes_squash_merged_branch(self):
        """Squash vero: piu' commit fusi in uno solo su develop.

        Il fixture usa `git merge --squash` e non un cherry-pick perche' e'
        proprio la fusione di N commit in uno a rompere il confronto
        per-commit: nessuno dei patch-id originali sopravvive, e uno
        `git cherry` per commit segnalerebbe tutto come non integrato.
        """
        with self._temporary_repo() as (root, repo, _, _):
            self._git(repo, "checkout", "-b", "feature/esempio", "origin/develop")
            for index in (1, 2, 3):
                self._commit(
                    repo, f"squash{index}.txt", f"c{index}\n", f"commit {index}"
                )
            self._git(repo, "checkout", "develop")
            self._git(repo, "merge", "--squash", "feature/esempio")
            self._git(repo, "commit", "-m", "feat: tutto in uno (#42)")
            self._git(repo, "push", "origin", "develop")

            process = self._run_feature_done(root, repo, "esempio", "MERGED")
            output = process.stdout + process.stderr

            self.assertEqual(process.returncode, 0, output)
            self.assertFalse(
                self._git(repo, "branch", "--list", "feature/esempio").stdout.strip()
            )
            self.assertIn("squash", output.lower())

    def test_feature_done_preserves_commit_added_after_the_squash(self):
        """Una PR MERGED non garantisce che il branch locale non sia avanzato."""
        with self._temporary_repo() as (root, repo, _, _):
            self._git(repo, "checkout", "-b", "feature/esempio", "origin/develop")
            self._commit(repo, "integrato.txt", "integrato\n", "change integrato")
            self._git(repo, "checkout", "develop")
            self._git(repo, "merge", "--squash", "feature/esempio")
            self._git(repo, "commit", "-m", "feat: squashato (#43)")
            self._git(repo, "push", "origin", "develop")
            self._git(repo, "checkout", "feature/esempio")
            self._commit(repo, "dopo.txt", "aggiunto dopo\n", "commit successivo")
            self._git(repo, "checkout", "develop")

            process = self._run_feature_done(root, repo, "esempio", "MERGED")
            output = process.stdout + process.stderr

            self.assertNotEqual(process.returncode, 0, output)
            self.assertTrue(
                self._git(repo, "branch", "--list", "feature/esempio").stdout.strip()
            )
            self.assertIn("not integrated", output.lower())


class TestReviewLocale(unittest.TestCase):
    """Verify local review without invoking the real runtime."""

    _git = staticmethod(TestGitOrient._git)
    _commit = TestGitOrient._commit
    _stage_harness = TestGitOrient._stage_harness
    _temporary_repo = TestGitOrient._temporary_repo

    def _run_review(
        self, root: Path, repo: Path, *arguments: str
    ) -> tuple[subprocess.CompletedProcess, Path]:
        script = repo / "harness" / "scripts" / "review-locale.sh"
        self.assertTrue(script.is_file(), "review-locale.sh is missing")
        stub_dir = root / "codex-stub"
        stub_dir.mkdir()
        prompt_file = root / "review-prompt.txt"
        args_file = root / "review-args.txt"
        codex_stub = stub_dir / "codex"
        codex_stub.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%q ' \"$@\" > '{args_file}'\n"
            f"cat > '{prompt_file}'\n"
            "printf 'review simulata\\n'\n",
            encoding="utf-8",
        )
        codex_stub.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = os.pathsep.join(
            [str(stub_dir), environment.get("PATH", "")]
        )
        return (
            subprocess.run(
                ["bash", str(script), *arguments],
                cwd=repo,
                capture_output=True,
                text=True,
                env=environment,
            ),
            prompt_file,
        )

    def test_review_passes_criteria_as_dedicated_prompt(self):
        with self._temporary_repo() as (root, repo, _, _):
            criteria = repo / "docs" / "review-criteria.md"
            criteria.parent.mkdir()
            criteria.write_text("Testable criterion\n", encoding="utf-8")
            self._git(repo, "checkout", "-b", "feature/review", "origin/develop")
            self._commit(repo, "review.txt", "diff da rivedere\n", "review diff")
            before = self._git(repo, "status", "--porcelain").stdout

            process, prompt_file = self._run_review(root, repo)

            output = process.stdout + process.stderr
            self.assertEqual(process.returncode, 0, output)
            self.assertEqual(before, self._git(repo, "status", "--porcelain").stdout)
            prompt = prompt_file.read_text(encoding="utf-8")
            self.assertIn("Testable criterion", prompt)
            self.assertIn("BLOCKING", prompt)
            self.assertNotIn("criterion", (root / "review-args.txt").read_text(encoding="utf-8").lower())

    def test_review_rejects_missing_criteria(self):
        with self._temporary_repo() as (root, repo, _, _):
            self._git(repo, "checkout", "-b", "feature/review", "origin/develop")

            process, _ = self._run_review(root, repo)
            output = process.stdout + process.stderr

            self.assertNotEqual(process.returncode, 0)
            self.assertIn("Error: review criteria are unreadable:", output)
            self.assertIn("docs/review-criteria.md", output)

    def test_review_rejects_more_than_one_argument(self):
        with self._temporary_repo() as (root, repo, _, _):
            process, _ = self._run_review(root, repo, "develop", "main")
            output = process.stdout + process.stderr

            self.assertNotEqual(process.returncode, 0)
            self.assertIn("Usage:", output)

    def test_review_succeeds_when_there_is_nothing_to_review(self):
        with self._temporary_repo() as (root, repo, _, _):
            criteria = repo / "docs" / "review-criteria.md"
            criteria.parent.mkdir()
            criteria.write_text("Testable criterion\n", encoding="utf-8")
            self._git(repo, "add", "docs/review-criteria.md", "harness")
            self._git(repo, "commit", "-m", "add review criteria and harness")
            self._git(repo, "push", "origin", "develop")
            self._git(repo, "checkout", "-b", "feature/review", "origin/develop")

            process, _ = self._run_review(root, repo)
            output = process.stdout + process.stderr

            self.assertEqual(process.returncode, 0, output)
            self.assertIn("local review is unnecessary", output)

    def test_review_does_not_change_git_state(self):
        with self._temporary_repo() as (root, repo, _, _):
            criteria = repo / "docs" / "review-criteria.md"
            criteria.parent.mkdir()
            criteria.write_text("Testable criterion\n", encoding="utf-8")
            self._git(repo, "add", "docs/review-criteria.md", "harness")
            self._git(repo, "commit", "-m", "add review criteria and harness")
            self._git(repo, "push", "origin", "develop")
            self._git(repo, "checkout", "-b", "feature/review", "origin/develop")
            (repo / "README").write_text("modifica non committata\n", encoding="utf-8")

            before = self._git_state(repo)
            process, _ = self._run_review(root, repo)
            output = process.stdout + process.stderr

            self.assertEqual(process.returncode, 0, output)
            self.assertEqual(before, self._git_state(repo))

    def _git_state(self, repo: Path) -> tuple[str, str, str, str]:
        return (
            self._git(repo, "rev-parse", "HEAD").stdout,
            self._git(repo, "status", "--porcelain=v1").stdout,
            self._git(repo, "diff", "--cached", "--binary").stdout,
            self._git(repo, "ls-files", "--stage").stdout,
        )


class TestWaitChecks(unittest.TestCase):
    """Verifica gli invarianti del comando di attesa della CI."""

    _git = staticmethod(TestGitOrient._git)
    _commit = TestGitOrient._commit
    _stage_harness = TestGitOrient._stage_harness
    _temporary_repo = TestGitOrient._temporary_repo

    def test_wait_checks_uses_runs_and_failed_logs(self):
        self.assertTrue(WAIT_CHECKS_SOURCE.is_file(), "wait-checks.sh non è presente")
        source = WAIT_CHECKS_SOURCE.read_text(encoding="utf-8")
        self.assertIn('gh run list --branch "$branch"', source)
        self.assertIn("--log-failed", source)
        self.assertNotIn("gh pr checks", source)
        self.assertTrue(WAIT_CHECKS_SOURCE.stat().st_mode & 0o111)

    def test_wait_checks_marks_comments_written_on_an_older_commit(self):
        """I commenti restano sulla PR dopo un push: vanno attribuiti al commit.

        Il campo da guardare e' `original_commit_id`, non `commit_id`: GitHub
        rimappa il secondo sul nuovo head quando la riga commentata esiste
        ancora, quindi un rilievo gia' risolto comparirebbe come se
        riguardasse il codice attuale. Il primo commento del fixture e'
        esattamente quel caso.
        """
        with self._temporary_repo() as (root, repo, _, _):
            script = repo / "harness" / "scripts" / "wait-checks.sh"
            head = self._git(repo, "rev-parse", "develop").stdout.strip()
            stub_dir = root / "gh-stub"
            stub_dir.mkdir()
            comments = json.dumps([
                {"user": {"login": "bot"}, "path": "a.sh", "body": "rilievo vecchio",
                 "original_commit_id": "0" * 40, "commit_id": head},
                {"user": {"login": "bot"}, "path": "a.sh", "body": "rilievo attuale",
                 "original_commit_id": head, "commit_id": head},
            ])
            runs = json.dumps([{"headSha": head, "name": "Tests",
                                "status": "completed", "conclusion": "success",
                                "databaseId": 1}])
            gh_stub = stub_dir / "gh"
            gh_stub.write_text(
                "#!/usr/bin/env bash\n"
                f"case \"$1 $2\" in\n"
                f"  'run list') cat <<'JSON'\n{runs}\nJSON\n    ;;\n"
                f"  'pr list') printf '7\\n' ;;\n"
                f"  'api --paginate')\n"
                f"    case \"$3\" in\n"
                f"      *reviews) printf '[]\\n' ;;\n"
                f"      *comments) cat <<'JSON'\n{comments}\nJSON\n        ;;\n"
                f"    esac ;;\n"
                f"esac\n",
                encoding="utf-8",
            )
            gh_stub.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = os.pathsep.join(
                [str(stub_dir), environment.get("PATH", "")]
            )

            process = subprocess.run(
                ["bash", str(script), "develop"], cwd=repo,
                capture_output=True, text=True, env=environment,
            )
            output = process.stdout + process.stderr

            self.assertEqual(process.returncode, 0, output)
            self.assertIn("rilievo vecchio", output)
            self.assertIn("rilievo attuale", output)
            self.assertEqual(output.count("[ON A PREVIOUS COMMIT]"), 1, output)
            marked = next(
                line for line in output.splitlines() if "[ON A PREVIOUS COMMIT]" in line
            )
            self.assertIn("rilievo vecchio", marked)

    def test_wait_checks_rejects_missing_branch_without_changing_git_state(self):
        with self._temporary_repo() as (_, repo, _, _):
            script = repo / "harness" / "scripts" / "wait-checks.sh"
            before = self._git_state(repo)

            process = subprocess.run(
                ["bash", str(script)], cwd=repo, capture_output=True, text=True
            )
            output = process.stdout + process.stderr

            self.assertNotEqual(process.returncode, 0)
            self.assertIn("Usage:", output)
            self.assertEqual(before, self._git_state(repo))

    def test_wait_checks_does_not_change_git_state_while_waiting(self):
        with self._temporary_repo() as (root, repo, _, _):
            script = repo / "harness" / "scripts" / "wait-checks.sh"
            stub_dir = root / "gh-stub"
            stub_dir.mkdir()
            gh_stub = stub_dir / "gh"
            gh_stub.write_text("#!/usr/bin/env bash\nprintf '[]\\n'\n", encoding="utf-8")
            gh_stub.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = os.pathsep.join(
                [str(stub_dir), environment.get("PATH", "")]
            )
            environment["WAIT_CHECKS_TIMEOUT_SECONDS"] = "1"
            environment["WAIT_CHECKS_POLL_SECONDS"] = "1"
            before = self._git_state(repo)

            process = subprocess.run(
                ["bash", str(script), "develop"],
                cwd=repo,
                capture_output=True,
                text=True,
                env=environment,
            )
            output = process.stdout + process.stderr

            self.assertNotEqual(process.returncode, 0)
            self.assertIn("Timeout:", output)
            self.assertEqual(before, self._git_state(repo))

    def _git_state(self, repo: Path) -> tuple[str, str, str, str]:
        return (
            self._git(repo, "rev-parse", "HEAD").stdout,
            self._git(repo, "status", "--porcelain=v1").stdout,
            self._git(repo, "diff", "--cached", "--binary").stdout,
            self._git(repo, "ls-files", "--stage").stdout,
        )

if __name__ == "__main__":
    unittest.main()
