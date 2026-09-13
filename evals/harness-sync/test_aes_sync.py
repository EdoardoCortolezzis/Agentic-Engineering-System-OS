"""Eval deterministico per ``harness/scripts/aes-sync.sh``.

Verifica la propagazione degli asset dichiarati nel manifest AES verso un repo
consumer sintetico: copy, seed, merge-json, pinning della provenienza,
idempotenza e rilevamento del drift dichiarato o non dichiarato. Ogni test
crea repo Git temporanei indipendenti, senza copiare gli asset reali del repo
e senza usare la rete.

Gli eval includono anche regressioni di sicurezza sui path del manifest:
devono fallire chiuso prima di creare directory o copiare asset, inclusi
traversal, `.git` e symlink fuori dalle root.

Esecuzione: ``python3 -m unittest evals/harness-sync/test_aes_sync.py -v``
"""

from contextlib import contextmanager
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SCRIPT_SOURCE = REPO / "harness" / "scripts" / "aes-sync.sh"

MANIFEST = """\
# Sintetico e intenzionalmente indipendente dagli asset reali di AES.
copy        harness/scripts/git-orient.sh     harness/scripts/git-orient.sh
copy        harness/scripts/feature-start.sh  harness/scripts/feature-start.sh
copy        harness/scripts/feature-done.sh   harness/scripts/feature-done.sh
copy        policies/git-workflow.md          policies/git-workflow.md
seed        harness/config/harness.conf       harness/config/harness.conf
merge-json  .claude/settings.json             .claude/settings.json             hooks.PreToolUse,hooks.SessionStart
"""

COPY_DESTINATIONS = (
    "harness/scripts/git-orient.sh",
    "harness/scripts/feature-start.sh",
    "harness/scripts/feature-done.sh",
    "policies/git-workflow.md",
)
SEED_DESTINATION = "harness/config/harness.conf"
JSON_DESTINATION = ".claude/settings.json"
ALL_DESTINATIONS = COPY_DESTINATIONS + (SEED_DESTINATION, JSON_DESTINATION)


class TestAesSync(unittest.TestCase):
    """Esegue aes-sync su una sorgente AES e un consumer sintetici."""

    def test_manifest_propagates_sync_tooling(self):
        manifest_path = REPO / "harness" / "manifest.txt"
        destinations = {
            fields[2]
            for line in manifest_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
            for fields in [line.split()]
            if len(fields) >= 3
        }
        required = {
            "harness/scripts/aes-sync.sh",
            "harness/scripts/review-locale.sh",
            "harness/scripts/guard-push.sh",
            "harness/scripts/wait-checks.sh",
            "harness/manifest.txt",
            ".github/workflows/claude-review.yml",
            ".github/workflows/claude-security-review.yml",
            ".github/workflows/security-freshness.yml",
            ".github/workflows/tests.yml",
            "policies/documentation.md",
        }
        missing = sorted(required - destinations)
        self.assertFalse(
            missing,
            "The real harness manifest must propagate the sync tooling, the "
            "PR CI workflows and the shared policies (missing: "
            f"{', '.join(missing)}): without the tooling a consumer repo "
            "cannot verify its own drift with 'aes-sync --check', and "
            "without the workflows its pull requests get no CI checks at all.",
        )

    def test_manifest_propagates_the_agent_monitor_and_its_hook(self):
        """Il monitor e' inerte se arrivano i moduli ma non l'aggancio.

        Sono due meta' della stessa cosa e vanno verificate insieme: il
        consumer che riceve i .py senza la voce `hooks.PostToolUse` ha un
        monitor installato che non scatta mai, cioe' esattamente il modo di
        fallire che ADR 0014 esiste per rendere impossibile.
        """
        sys.path.insert(0, str(REPO / "harness" / "scripts" / "agent-monitor"))
        import doctor  # noqa: PLC0415 — importato qui per non toccare i path globali

        manifest_lines = [
            line.split()
            for line in (REPO / "harness" / "manifest.txt")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        destinations = {f[2] for f in manifest_lines if len(f) >= 3}
        required = {
            f"harness/scripts/agent-monitor/{name}"
            for name in (*doctor.MODULES, "doctor.py", "posttooluse.sh")
        }
        missing = sorted(required - destinations)
        self.assertFalse(
            missing,
            "Il manifest deve propagare ogni modulo che doctor.py dichiara "
            f"necessario (mancanti: {', '.join(missing)}): un consumer con i "
            "moduli incompleti ha un monitor che esce in silenzio.",
        )

        managed = {
            key
            for fields in manifest_lines
            if len(fields) == 4 and fields[2] == ".claude/settings.json"
            for key in fields[3].split(",")
        }
        self.assertIn(
            "hooks.PostToolUse",
            managed,
            "Senza hooks.PostToolUse fra le chiavi gestite il consumer riceve "
            "i moduli del monitor ma nessun hook che li invochi.",
        )

    def test_manifest_propagates_the_github_task_flow_and_its_hook(self):
        """Il flusso task funziona solo se asset, protocollo e hook arrivano insieme."""
        manifest_lines = [
            line.split()
            for line in (REPO / "harness" / "manifest.txt")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        destinations = {fields[2] for fields in manifest_lines if len(fields) >= 3}
        tasks_dir = REPO / "harness" / "scripts" / "tasks"
        required = {
            str(path.relative_to(REPO))
            for path in tasks_dir.iterdir()
            if path.is_file() and path.suffix == ".py"
        }
        required.update(
            {
                ".agent/skills/aes-tasks/SKILL.md",
                ".agent/skills/aes-harness/SKILL.md",
                ".github/ISSUE_TEMPLATE/aes-task.md",
            }
        )
        missing = sorted(required - destinations)
        self.assertFalse(
            missing,
            "Il manifest deve propagare l'intero flusso task di ADR 0015 "
            f"(mancanti: {', '.join(missing)}): un consumer che riceve "
            "tasks.py ma non le skill ha il comando e non il protocollo; "
            "uno che riceve le skill ma non tutti i moduli ha istruzioni che "
            "rimandano a un comando inesistente.",
        )

        settings_entries = [
            fields
            for fields in manifest_lines
            if len(fields) == 4 and fields[2] == ".claude/settings.json"
        ]
        self.assertTrue(
            any(
                fields[0] == "merge-json"
                and "hooks.SessionStart" in fields[3].split(",")
                for fields in settings_entries
            ),
            ".claude/settings.json deve restare gestito in merge-json con "
            "hooks.SessionStart: senza questa chiave il consumer riceve gli "
            "script ma non l'aggancio che esegue il controllo --check-upstream.",
        )

    def test_workflows_do_not_gate_on_secret_presence(self):
        """I workflow non devono diventare verde saltando uno step CI."""
        workflow_dir = REPO / ".github" / "workflows"
        workflow_paths = sorted(workflow_dir.glob("*.yml"))
        workflow_paths.extend(sorted(workflow_dir.glob("*.yaml")))
        self.assertTrue(
            workflow_paths,
            "La directory dei workflow reali deve contenere almeno un workflow.",
        )

        secret_variables_by_workflow = {}
        assignment_pattern = re.compile(
            r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*"
            r"\$\{\{\s*secrets\.[A-Za-z_][A-Za-z0-9_-]*\s*\}\}"
        )
        for workflow_path in workflow_paths:
            secret_variables_by_workflow[workflow_path] = {
                match.group(1)
                for line in workflow_path.read_text(encoding="utf-8").splitlines()
                if (match := assignment_pattern.match(line))
            }

        empty_comparison_pattern = re.compile(
            r"(?:==|!=)\s*['\"]{2}|['\"]{2}\s*(?:==|!=)"
        )
        violations = []
        for workflow_path in workflow_paths:
            secret_variables = secret_variables_by_workflow[workflow_path]
            for line_number, line in enumerate(
                workflow_path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if_match = re.match(r"^\s*if:\s*(.*)$", line)
                if not if_match:
                    continue
                condition = if_match.group(1)
                references_secret = "secrets." in condition
                references_secret_variable = any(
                    f"env.{variable}" in condition
                    for variable in secret_variables
                )
                if (
                    (references_secret or references_secret_variable)
                    and empty_comparison_pattern.search(condition)
                ):
                    violations.append(
                        f"{workflow_path.relative_to(REPO)}:{line_number}: {line.strip()}"
                    )

        self.assertFalse(
            violations,
            "È vietato usare un gating condizionale sulla presenza di un secret "
            "(`if: ... == ''` o `if: ... != ''`): se il secret manca lo step "
            "viene saltato e la PR può risultare falsamente verde. Vedi ADR 0010. "
            f"Violazioni: {', '.join(violations)}",
        )

    def test_review_workflows_keep_their_cost_invariants(self):
        """Gli invarianti di costo dell'ADR 0017 non regrediscono."""
        review_paths = [
            REPO / ".github" / "workflows" / "claude-review.yml",
            REPO / ".github" / "workflows" / "claude-security-review.yml",
        ]
        sources = {}
        for path in review_paths:
            self.assertTrue(path.exists(), f"Workflow di review assente: {path}")
            sources[path] = path.read_text(encoding="utf-8")

        # 1. I due gruppi di concurrency devono restare distinti: con lo stesso
        # gruppo le due review si annullerebbero a vicenda e una PR resterebbe
        # con una sola delle due, senza che niente diventi rosso.
        group_pattern = re.compile(r"^\s*group:\s*(.+)$", re.MULTILINE)
        groups = {}
        for path, source in sources.items():
            found = group_pattern.findall(source)
            self.assertEqual(
                len(found),
                1,
                f"{path.name} deve dichiarare esattamente un concurrency.group.",
            )
            self.assertIn(
                "cancel-in-progress: true",
                source,
                f"{path.name} deve annullare il run precedente: senza, ogni push "
                "paga per intero una review già obsoleta (ADR 0011).",
            )
            groups[path.name] = found[0].strip()
        self.assertEqual(
            len(set(groups.values())),
            len(groups),
            "I concurrency.group dei due workflow di review devono essere "
            f"distinti, altrimenti si annullano a vicenda: {groups}",
        )

        # 2. Nessun push rilancia una review pagante: è l'invariante centrale
        # di costo dell'ADR 0017, che rivede il comportamento dell'ADR 0013.
        for path, source in sources.items():
            types_match = re.search(r"^\s*types:\s*\[(.+)\]\s*$", source, re.MULTILINE)
            self.assertIsNotNone(
                types_match, f"{path.name} deve dichiarare i tipi di evento."
            )
            event_types = {value.strip() for value in types_match.group(1).split(",")}
            self.assertNotIn(
                "synchronize",
                event_types,
                f"{path.name} non deve reagire a 'synchronize': un push non deve "
                "ripagare una review completa (ADR 0017).",
            )

            condition_match = re.search(
                r"^\s{4}if:\s*\|\s*$\n((?:^\s{6,}.*(?:\n|$))*)",
                source,
                re.MULTILINE,
            )
            self.assertIsNotNone(
                condition_match,
                f"{path.name} deve usare una condizione multilinea sul job.",
            )
            condition = condition_match.group(1)
            self.assertIn(
                "github.event.pull_request.draft == false",
                condition,
                f"{path.name} deve mantenere il filtro draft nel job: una review "
                "sul lavoro in corso paga iterazioni che devono restare locali.",
            )
            self.assertNotIn(
                "paths-ignore",
                source,
                f"{path.name} non deve ignorare i Markdown: una credenziale "
                "in chiaro nel diff resta un rilievo di sicurezza applicabile.",
            )

        review_source = sources[review_paths[0]]
        review_types = {
            value.strip()
            for value in re.search(
                r"^\s*types:\s*\[(.+)\]\s*$", review_source, re.MULTILINE
            ).group(1).split(",")
        }
        self.assertEqual(
            review_types,
            {"labeled", "ready_for_review"},
            "claude-review.yml deve essere un'escalation attivata solo da "
            "labeled/ready_for_review (ADR 0017).",
        )
        self.assertIn(
            "contains(github.event.pull_request.labels.*.name, 'review:deep')",
            review_source,
            "claude-review.yml deve richiedere la label 'review:deep', non "
            "la vecchia 'review' (ADR 0017).",
        )
        self.assertNotIn(
            "contains(github.event.pull_request.labels.*.name, 'review')",
            review_source.replace("'review:deep'", ""),
            "claude-review.yml non deve reintrodurre il gate sulla vecchia "
            "label 'review' (ADR 0017).",
        )

        security_source = sources[review_paths[1]]
        security_types = {
            value.strip()
            for value in re.search(
                r"^\s*types:\s*\[(.+)\]\s*$", security_source, re.MULTILINE
            ).group(1).split(",")
        }
        self.assertIn(
            "opened",
            security_types,
            "claude-security-review.yml deve includere 'opened': senza questo "
            "una PR aperta già pronta non viene analizzata e può risultare "
            "falsamente verde (ADR 0010, aggiornato da ADR 0017).",
        )
        self.assertIn(
            "ready_for_review",
            security_types,
            "claude-security-review.yml deve includere 'ready_for_review' per "
            "le PR che diventano pronte da draft (ADR 0017).",
        )
        self.assertEqual(
            security_types,
            {"labeled", "opened", "reopened", "ready_for_review"},
            "claude-security-review.yml deve eseguire il controllo indipendente "
            "una volta per PR con opened/reopened/ready_for_review e permettere "
            "il rescan esplicito (ADR 0017).",
        )
        self.assertIn(
            "github.event.action != 'labeled'",
            security_source,
            "claude-security-review.yml deve distinguere l'evento labeled dagli "
            "altri trigger prima di applicare il filtro di rescan.",
        )
        self.assertIn(
            "github.event.label.name == 'security:rescan'",
            security_source,
            "claude-security-review.yml deve rilanciare la scan solo per la "
            "label security:rescan.",
        )

        freshness_path = REPO / ".github" / "workflows" / "security-freshness.yml"
        self.assertTrue(
            freshness_path.exists(),
            f"Workflow di freschezza assente: {freshness_path}",
        )
        freshness_source = freshness_path.read_text(encoding="utf-8")
        freshness_types_match = re.search(
            r"^\s*types:\s*\[(.+)\]\s*$", freshness_source, re.MULTILINE
        )
        self.assertIsNotNone(
            freshness_types_match,
            "security-freshness.yml deve dichiarare i tipi di evento.",
        )
        self.assertEqual(
            {value.strip() for value in freshness_types_match.group(1).split(",")},
            {"opened", "reopened", "synchronize", "labeled", "ready_for_review"},
            "security-freshness.yml deve verificare ogni cambio di commit della PR "
            "e deve ri-eseguirsi dopo la label security:rescan: senza 'labeled' "
            "il gate resta rosso per sempre dopo una scansione riuscita.",
        )
        self.assertNotIn(
            "continue-on-error",
            freshness_source,
            "security-freshness.yml non deve trasformare un errore API in falso verde.",
        )
        self.assertIn(
            "actions: read",
            freshness_source,
            "security-freshness.yml deve avere il permesso minimo di lettura delle run.",
        )
        self.assertIn(
            "head_sha",
            freshness_source,
            "security-freshness.yml deve confrontare la run con l'head SHA corrente.",
        )
        self.assertIn(
            '.conclusion != "skipped"',
            freshness_source,
            "Il predicato deve escludere le run skipped: una label qualsiasi, "
            "come review:deep, può creare una run senza avviare la scansione e "
            "rendere verde il gate sul nuovo commit.",
        )
        self.assertIn(
            '.conclusion != "cancelled"',
            freshness_source,
            "Il predicato deve escludere le run cancelled: una run annullata "
            "senza una scansione effettiva non dimostra che il commit sia stato "
            "analizzato.",
        )
        self.assertIn(
            ".conclusion == null",
            freshness_source,
            "Le run ancora in corso o in coda devono continuare a contare come "
            "esecuzioni della security review.",
        )
        self.assertIn(
            "security:rescan",
            freshness_source,
            "Il fallimento deve spiegare che security:rescan rilancia la scansione.",
        )

        # 3. Gli invarianti che non cambiano restano espliciti: token, pin,
        # concurrency e assenza di fallback verde o gating su secret.
        for path, source in sources.items():
            self.assertIn(
                "github_token: ${{ secrets.GITHUB_TOKEN }}",
                source,
                f"{path.name} deve mantenere github_token: senza, la review "
                "può essere skippata uscendo verde (ADR 0010).",
            )
            self.assertIn(
                "anthropics/claude-code-action@v1.0.189",
                source,
                f"{path.name} deve mantenere il pin dell'action a v1.0.189.",
            )
            self.assertNotIn(
                "continue-on-error",
                source,
                f"{path.name} non deve degradare un errore a falso verde.",
            )
            self.assertFalse(
                re.search(r"^\s+id-token:\s", source, re.MULTILINE),
                f"{path.name} non deve introdurre id-token: write.",
            )

        # 4. Il modello resta quello economico e pinnato: un ritorno a Pro va
        # deciso in un ADR, non introdotto da una modifica di passaggio.
        for path, source in sources.items():
            self.assertNotIn(
                "deepseek-v4-pro",
                source,
                f"{path.name} è tornato al modello Pro senza un ADR che lo "
                "motivi: l'ADR 0011 prevede il ritorno solo per la security "
                "review e solo se Flash smette di trovare i bloccanti veri.",
            )
            self.assertIn(
                "--model deepseek/deepseek-v4-flash-0731",
                source,
                f"{path.name} deve pinnare il modello a un tag datato, non "
                "mobile, come per il pin dell'action.",
            )
            self.assertIn(
                "Bash(git diff:*)",
                source,
                f"{path.name} deve permettere di restringere il diff senza "
                "sprecare turni in tool denial.",
            )
            for read_tool in ("Read", "Grep", "Glob"):
                self.assertIn(
                    read_tool,
                    source,
                    f"{path.name} deve consentire il tool read-only {read_tool}.",
                )

        self.assertIn(
            "--max-turns 80",
            sources[REPO / ".github" / "workflows" / "claude-review.yml"],
            "L'auto-review deve mantenere il tetto di 80 turni dell'ADR 0011.",
        )
        self.assertIn(
            "--max-turns 100",
            sources[REPO / ".github" / "workflows" / "claude-security-review.yml"],
            "La security review richiede i 100 turni misurati nell'ADR 0011.",
        )

    @staticmethod
    def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *arguments],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )

    @classmethod
    def _init_git_repo(cls, repo: Path, name: str) -> None:
        cls._git(repo, "init", "-b", "main")
        cls._git(repo, "config", "user.email", f"{name}@example.test")
        cls._git(repo, "config", "user.name", f"AES Sync {name}")
        (repo / "README.fixture").write_text(
            f"fixture repo: {name}\n", encoding="utf-8"
        )
        cls._git(repo, "add", "README.fixture")
        cls._git(repo, "commit", "-m", "fixture baseline")

    @classmethod
    def _build_aes_source(cls, root: Path) -> tuple[Path, str]:
        """Crea una sorgente AES breve, riconoscibile e già versionata."""
        source = root / "aes-source"
        source.mkdir()
        for directory in (
            source / "harness" / "scripts",
            source / "harness" / "config",
            source / "policies",
            source / ".claude",
        ):
            directory.mkdir(parents=True)

        (source / "harness" / "manifest.txt").write_text(
            MANIFEST, encoding="utf-8"
        )
        for filename, marker in (
            ("git-orient.sh", "AES FIXTURE git-orient"),
            ("feature-start.sh", "AES FIXTURE feature-start"),
            ("feature-done.sh", "AES FIXTURE feature-done"),
        ):
            script = source / "harness" / "scripts" / filename
            script.write_text(
                f"#!/usr/bin/env bash\nprintf '%s\\n' '{marker}'\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
        (source / "harness" / "config" / "harness.conf").write_text(
            "# AES FIXTURE CONFIG\nLINKED_FILES=.env\n", encoding="utf-8"
        )
        (source / "policies" / "git-workflow.md").write_text(
            "# AES FIXTURE POLICY\nUse feature branches only.\n",
            encoding="utf-8",
        )
        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "printf 'AES FIXTURE pre\\n'",
                            }
                        ],
                    }
                ],
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "printf 'AES FIXTURE session\\n'",
                            }
                        ]
                    }
                ],
            },
            "source_only": "must-not-be-merged",
        }
        (source / ".claude" / "settings.json").write_text(
            json.dumps(settings, indent=2) + "\n", encoding="utf-8"
        )

        cls._init_git_repo(source, "source")
        cls._git(source, "add", ".")
        cls._git(source, "commit", "-m", "synthetic AES source")
        source_commit = cls._git(source, "rev-parse", "HEAD").stdout.strip()
        return source, source_commit

    @classmethod
    def _build_consumer(cls, root: Path) -> Path:
        consumer = root / "consumer"
        consumer.mkdir()
        cls._init_git_repo(consumer, "consumer")
        return consumer

    @contextmanager
    def _temporary_repositories(self):
        """Crea una coppia AES/consumer isolata per il singolo test."""
        with tempfile.TemporaryDirectory(prefix="aes-sync-") as directory:
            root = Path(directory)
            source, source_commit = self._build_aes_source(root)
            consumer = self._build_consumer(root)
            yield source, source_commit, consumer

    def _run_sync(
        self, consumer: Path, source: Path, *arguments: str
    ) -> subprocess.CompletedProcess:
        """Invoca lo script reale dalla root del consumer."""
        return subprocess.run(
            ["bash", str(SCRIPT_SOURCE), *arguments, "--source", str(source)],
            cwd=consumer,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _output(process: subprocess.CompletedProcess) -> str:
        return process.stdout + process.stderr

    # Deve coincidere con DIGEST_LENGTH in aes-sync.sh: un digest completo
    # (64 caratteri hex) è indistinguibile da una chiave privata per gli
    # scanner di segreti, e .harness-version cambia a ogni bump dell'harness.
    DIGEST_LENGTH = 16

    @classmethod
    def _sha256(cls, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()[: cls.DIGEST_LENGTH]

    @classmethod
    def _managed_json_sha256(cls, source: Path) -> str:
        """Hash canonico delle sole chiavi dichiarate dal manifest."""
        settings = json.loads(
            (source / JSON_DESTINATION).read_text(encoding="utf-8")
        )
        managed = {"hooks": settings["hooks"]}
        canonical = json.dumps(
            managed, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()[: cls.DIGEST_LENGTH]

    @staticmethod
    def _read_json(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json(path: Path, value: dict) -> None:
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    def _assert_sync_succeeded(
        self, process: subprocess.CompletedProcess
    ) -> None:
        self.assertEqual(process.returncode, 0, self._output(process))

    def _assert_initial_sync(
        self, source: Path, source_commit: str, consumer: Path
    ) -> dict:
        process = self._run_sync(consumer, source)
        self._assert_sync_succeeded(process)
        version_path = consumer / ".harness-version"
        self.assertTrue(version_path.is_file())
        version = self._read_json(version_path)
        self.assertEqual(version["source_commit"], source_commit)
        return version

    def test_first_sync_copies_and_pins(self):
        with self._temporary_repositories() as (source, source_commit, consumer):
            version = self._assert_initial_sync(source, source_commit, consumer)

            for relative_path in ALL_DESTINATIONS:
                self.assertTrue(
                    (consumer / relative_path).is_file(), relative_path
                )
            for relative_path in COPY_DESTINATIONS:
                self.assertEqual(
                    (consumer / relative_path).read_bytes(),
                    (source / relative_path).read_bytes(),
                )

            # La spec non registra hash per seed: tutti gli asset tracciati
            # (copy e sottostruttura merge-json) devono invece essere pinnati.
            expected_files = set(COPY_DESTINATIONS) | {JSON_DESTINATION}
            self.assertEqual(set(version["files"]), expected_files)
            for relative_path in expected_files:
                if relative_path == JSON_DESTINATION:
                    self.assertEqual(
                        version["files"][relative_path],
                        self._managed_json_sha256(source),
                    )
                else:
                    self.assertEqual(
                        version["files"][relative_path]["sha256"],
                        self._sha256(consumer / relative_path),
                    )
                    self.assertEqual(
                        version["files"][relative_path]["executable"],
                        bool(
                            (source / relative_path).stat().st_mode & 0o111
                        ),
                    )

    def test_sync_is_idempotent(self):
        with self._temporary_repositories() as (source, source_commit, consumer):
            first_version = self._assert_initial_sync(
                source, source_commit, consumer
            )
            before = {
                relative_path: (consumer / relative_path).read_bytes()
                for relative_path in ALL_DESTINATIONS
            }

            second = self._run_sync(consumer, source)
            self._assert_sync_succeeded(second)
            after = {
                relative_path: (consumer / relative_path).read_bytes()
                for relative_path in ALL_DESTINATIONS
            }
            second_version = self._read_json(consumer / ".harness-version")

            self.assertEqual(after, before)
            self.assertEqual(second_version["files"], first_version["files"])

    def test_check_detects_undeclared_drift(self):
        with self._temporary_repositories() as (source, source_commit, consumer):
            self._assert_initial_sync(source, source_commit, consumer)
            drifted = consumer / "policies" / "git-workflow.md"
            drifted.write_text("# LOCAL UNDECLARED DRIFT\n", encoding="utf-8")

            process = self._run_sync(consumer, source, "--check")
            output = self._output(process)

            self.assertNotEqual(process.returncode, 0, output)
            self.assertIn("policies/git-workflow.md", output)

    def test_check_accepts_declared_override(self):
        with self._temporary_repositories() as (source, source_commit, consumer):
            self._assert_initial_sync(source, source_commit, consumer)
            drifted = consumer / "policies" / "git-workflow.md"
            drifted.write_text("# LOCAL DECLARED OVERRIDE\n", encoding="utf-8")
            (consumer / ".harness-overrides").write_text(
                "# policy intentionally customized\n"
                "policies/git-workflow.md\n",
                encoding="utf-8",
            )

            process = self._run_sync(consumer, source, "--check")

            self.assertEqual(process.returncode, 0, self._output(process))

    def test_sync_does_not_overwrite_a_declared_override(self):
        """Un override dichiarato deve sopravvivere alla sincronizzazione.

        Finche' `--check` lo esentava dal drift ma la copia lo sovrascriveva
        lo stesso, dichiarare un override non conservava nulla: il primo
        `aes-sync` lo cancellava. Con il bump automatico funzionante quel
        `aes-sync` gira ogni notte, quindi ogni PR di bump proponeva di
        distruggere la divergenza voluta del consumer.
        """
        with self._temporary_repositories() as (source, source_commit, consumer):
            self._assert_initial_sync(source, source_commit, consumer)
            overridden = consumer / "policies" / "git-workflow.md"
            wanted = "# DIVERGENZA VOLUTA DEL CONSUMER\n"
            overridden.write_text(wanted, encoding="utf-8")
            (consumer / ".harness-overrides").write_text(
                "# policy adattata a questo repo\n"
                "policies/git-workflow.md\n",
                encoding="utf-8",
            )

            process = self._run_sync(consumer, source)
            output = self._output(process)
            self._assert_sync_succeeded(process)

            self.assertEqual(
                overridden.read_text(encoding="utf-8"), wanted,
                "La sincronizzazione non deve sovrascrivere un file dichiarato "
                f"in .harness-overrides. Output: {output}",
            )
            self.assertIn(
                "policies/git-workflow.md", output,
                "Un file gestito che non viene aggiornato deve essere visibile: "
                "altrimenti l'override diventa un modo silenzioso di restare "
                "indietro.",
            )
            # Il contratto di --check non cambia: l'override resta dichiarato
            # e il repo resta verde.
            check = self._run_sync(consumer, source, "--check")
            self.assertEqual(check.returncode, 0, self._output(check))

    def test_override_entry_tolerates_natural_spelling(self):
        """Un override che non combacia fallisce nel modo peggiore.

        Commento in linea e `./` iniziale sono grafie naturali. Se non
        vengono normalizzate la voce non corrisponde a nulla, e il modo in
        cui l'override fallisce e' in silenzio: al sync successivo il file
        che doveva proteggere viene sovrascritto.
        """
        for spelling in (
            "policies/git-workflow.md  # adattata a questo repo",
            "./policies/git-workflow.md",
            "  policies/git-workflow.md  ",
        ):
            with self.subTest(spelling=spelling):
                with self._temporary_repositories() as (source, source_commit, consumer):
                    self._assert_initial_sync(source, source_commit, consumer)
                    overridden = consumer / "policies" / "git-workflow.md"
                    wanted = "# DIVERGENZA VOLUTA\n"
                    overridden.write_text(wanted, encoding="utf-8")
                    (consumer / ".harness-overrides").write_text(
                        spelling + "\n", encoding="utf-8"
                    )

                    process = self._run_sync(consumer, source)
                    self._assert_sync_succeeded(process)
                    self.assertEqual(
                        overridden.read_text(encoding="utf-8"), wanted,
                        f"La grafia {spelling!r} deve valere come override.",
                    )

    def test_override_that_matches_nothing_is_reported(self):
        """Un refuso non protegge niente e deve dirlo.

        Senza avviso, una voce sbagliata sembra una protezione e non lo e':
        ci si accorge dell'errore quando il file e' gia' stato sovrascritto.
        """
        with self._temporary_repositories() as (source, source_commit, consumer):
            self._assert_initial_sync(source, source_commit, consumer)
            (consumer / ".harness-overrides").write_text(
                "policies/git-worfklow.md\n", encoding="utf-8"
            )

            process = self._run_sync(consumer, source)
            output = self._output(process)

            self._assert_sync_succeeded(process)
            self.assertIn("policies/git-worfklow.md", output)
            self.assertIn("it protects nothing", output)

    def test_sync_does_not_merge_into_a_declared_override(self):
        """La regola vale per il file, non per la modalita' del manifest.

        `merge-json` scrive solo le chiavi gestite, ma se il consumer ha
        dichiarato quel file come divergenza voluta anche quelle chiavi sono
        sue: un override che vale per `copy` e non per `merge-json` sarebbe
        una semantica a meta'.
        """
        with self._temporary_repositories() as (source, source_commit, consumer):
            self._assert_initial_sync(source, source_commit, consumer)
            settings = consumer / JSON_DESTINATION
            mine = '{\n  "hooks": {\n    "PreToolUse": "solo mio"\n  }\n}\n'
            settings.write_text(mine, encoding="utf-8")
            (consumer / ".harness-overrides").write_text(
                f"# hook gestiti a mano in questo repo\n{JSON_DESTINATION}\n",
                encoding="utf-8",
            )

            process = self._run_sync(consumer, source)
            self._assert_sync_succeeded(process)

            self.assertEqual(
                settings.read_text(encoding="utf-8"), mine,
                "Nemmeno le chiavi gestite vanno riscritte in un file "
                "dichiarato come override.",
            )

    def test_check_passes_on_clean_repo(self):
        with self._temporary_repositories() as (source, source_commit, consumer):
            self._assert_initial_sync(source, source_commit, consumer)

            process = self._run_sync(consumer, source, "--check")

            self.assertEqual(process.returncode, 0, self._output(process))

    def test_check_reports_obsolete_version_format_without_suggesting_an_override(self):
        """Un .harness-version del formato precedente non e' un drift.

        Registrava una stringa per gli asset copy, quindi nessuna voce puo'
        essere confrontata col bit di esecuzione. Il messaggio deve dire di
        risincronizzare: il consiglio di dichiarare un override manderebbe a
        formalizzare una divergenza che non esiste.
        """
        with self._temporary_repositories() as (source, source_commit, consumer):
            self._assert_initial_sync(source, source_commit, consumer)
            version_path = consumer / ".harness-version"
            version = json.loads(version_path.read_text(encoding="utf-8"))
            obsolete = "harness/scripts/feature-start.sh"
            version["files"][obsolete] = version["files"][obsolete]["sha256"]
            version_path.write_text(
                json.dumps(version, indent=2) + "\n", encoding="utf-8"
            )

            process = self._run_sync(consumer, source, "--check")
            output = self._output(process)

            self.assertNotEqual(process.returncode, 0, output)
            self.assertIn("Obsolete .harness-version format", output)
            self.assertIn(obsolete, output)
            self.assertNotIn(".harness-overrides", output)

    def test_copy_mode_replaces_destination_via_atomic_rename(self):
        """Un asset `copy` va sostituito con un rename, non riscritto in-place.

        `cp -p` in-place scrive nell'inode di destinazione esistente. Quando
        la destinazione e' lo script correntemente in esecuzione — il bump
        di aes-sync.sh su se stesso, riprodotto durante una propagazione
        reale — un processo che lo sta ancora leggendo vede contenuto
        vecchio e nuovo mescolati a meta' lettura: il sintomo osservato era
        un errore di sintassi a meta' parola. Un rename atomico lascia
        intatto il vecchio inode finche' qualcuno lo tiene aperto, quindi un
        secondo apply deve produrre un inode diverso per lo stesso path.
        """
        with self._temporary_repositories() as (source, source_commit, consumer):
            self._assert_initial_sync(source, source_commit, consumer)
            target = consumer / "harness" / "scripts" / "git-orient.sh"
            inode_before = target.stat().st_ino

            second = self._run_sync(consumer, source)

            self.assertEqual(second.returncode, 0, self._output(second))
            inode_after = target.stat().st_ino
            self.assertNotEqual(
                inode_before, inode_after,
                "il file ha lo stesso inode di prima: e' stato riscritto "
                "in-place, non sostituito con un rename atomico",
            )

    def test_check_reports_full_length_digest_as_obsolete_format(self):
        """Un digest sha256 completo (64 caratteri) e' un formato precedente.

        Prima del troncamento .harness-version registrava il digest intero:
        una stringa esadecimale di 64 caratteri e' indistinguibile da una
        chiave privata per gli scanner di segreti (incluso quello che
        intercetta il commit di questo stesso file a ogni bump). Come per il
        formato a stringa (D6), il messaggio deve dire di risincronizzare,
        non suggerire un override.
        """
        with self._temporary_repositories() as (source, source_commit, consumer):
            self._assert_initial_sync(source, source_commit, consumer)
            version_path = consumer / ".harness-version"
            version = json.loads(version_path.read_text(encoding="utf-8"))
            obsolete = "harness/scripts/feature-start.sh"
            entry = version["files"][obsolete]
            full_digest = hashlib.sha256(
                (consumer / obsolete).read_bytes()
            ).hexdigest()
            self.assertEqual(len(full_digest), 64)
            version["files"][obsolete] = {**entry, "sha256": full_digest}
            version_path.write_text(
                json.dumps(version, indent=2) + "\n", encoding="utf-8"
            )

            process = self._run_sync(consumer, source, "--check")
            output = self._output(process)

            self.assertNotEqual(process.returncode, 0, output)
            self.assertIn("Obsolete .harness-version format", output)
            self.assertIn(obsolete, output)
            self.assertNotIn(".harness-overrides", output)

    def test_registered_digests_never_reach_sha256_length(self):
        """Nessun digest in .harness-version deve poter far scattare uno
        scanner di segreti generico: 64 caratteri hex e' il pattern con cui
        si riconoscono le chiavi private, e questo file cambia a ogni bump
        dell'harness in ogni repo consumer.
        """
        with self._temporary_repositories() as (source, source_commit, consumer):
            self._assert_initial_sync(source, source_commit, consumer)
            version = json.loads(
                (consumer / ".harness-version").read_text(encoding="utf-8")
            )
            for destination, expected in version["files"].items():
                digest = expected if isinstance(expected, str) else expected["sha256"]
                self.assertNotEqual(
                    len(digest), 64,
                    f"{destination}: digest lungo 64 caratteri, "
                    "indistinguibile da una chiave privata",
                )

    def test_check_detects_executable_bit_drift(self):
        with self._temporary_repositories() as (source, source_commit, consumer):
            self._assert_initial_sync(source, source_commit, consumer)
            drifted = consumer / "harness/scripts/feature-start.sh"
            drifted.chmod(drifted.stat().st_mode & ~0o111)

            process = self._run_sync(consumer, source, "--check")
            output = self._output(process)

            self.assertNotEqual(process.returncode, 0, output)
            self.assertIn("harness/scripts/feature-start.sh", output)

    def test_check_rejects_legacy_string_file_metadata_without_mass_drift(self):
        with self._temporary_repositories() as (source, source_commit, consumer):
            self._assert_initial_sync(source, source_commit, consumer)
            version_path = consumer / ".harness-version"
            version = self._read_json(version_path)
            for destination, metadata in list(version["files"].items()):
                if isinstance(metadata, dict):
                    version["files"][destination] = metadata["sha256"]
            self._write_json(version_path, version)

            process = self._run_sync(consumer, source, "--check")
            output = self._output(process)

            self.assertNotEqual(process.returncode, 0, output)
            self.assertIn("Obsolete .harness-version format", output)
            self.assertNotIn("Drift: harness/scripts/feature-start.sh", output)

    def test_check_upstream_detects_consumer_behind(self):
        with self._temporary_repositories() as (source, source_commit, consumer):
            self._assert_initial_sync(source, source_commit, consumer)
            for index in range(3):
                filename = f"upstream-{index}.txt"
                (source / filename).write_text(f"commit {index}\n", encoding="utf-8")
                self._git(source, "add", filename)
                self._git(source, "commit", "-m", f"upstream commit {index}")

            process = self._run_sync(consumer, source, "--check-upstream")
            output = self._output(process)

            self.assertNotEqual(process.returncode, 0, output)
            self.assertIn("3", output)
            self.assertIn("Update with:", output)

    def test_check_upstream_accepts_aligned_consumer(self):
        with self._temporary_repositories() as (source, source_commit, consumer):
            self._assert_initial_sync(source, source_commit, consumer)

            process = self._run_sync(consumer, source, "--check-upstream")

            self.assertEqual(process.returncode, 0, self._output(process))
            self.assertEqual(self._output(process), "")

    def test_check_upstream_ignores_repo_without_version_file(self):
        with self._temporary_repositories() as (source, _, consumer):
            process = self._run_sync(consumer, source, "--check-upstream")

            self.assertEqual(process.returncode, 0, self._output(process))
            self.assertEqual(self._output(process), "")

    def test_check_upstream_rejects_unknown_registered_commit(self):
        with self._temporary_repositories() as (source, source_commit, consumer):
            self._assert_initial_sync(source, source_commit, consumer)
            version_path = consumer / ".harness-version"
            version = self._read_json(version_path)
            version["source_commit"] = "0" * 40
            self._write_json(version_path, version)

            process = self._run_sync(consumer, source, "--check-upstream")

            output = self._output(process)
            self.assertNotEqual(process.returncode, 0, output)
            self.assertIn("does not exist", output)

    def test_check_and_check_upstream_run_together(self):
        with self._temporary_repositories() as (source, source_commit, consumer):
            self._assert_initial_sync(source, source_commit, consumer)

            process = self._run_sync(
                consumer, source, "--check", "--check-upstream"
            )

            self.assertEqual(process.returncode, 0, self._output(process))

    def test_seed_creates_missing_config(self):
        with self._temporary_repositories() as (source, source_commit, consumer):
            self.assertFalse((consumer / SEED_DESTINATION).exists())

            process = self._run_sync(consumer, source)

            self._assert_sync_succeeded(process)
            seeded = consumer / SEED_DESTINATION
            self.assertTrue(seeded.is_file())
            self.assertEqual(
                seeded.read_bytes(),
                (source / SEED_DESTINATION).read_bytes(),
            )

    def test_seed_does_not_overwrite_existing_config(self):
        with self._temporary_repositories() as (source, source_commit, consumer):
            config = consumer / SEED_DESTINATION
            config.parent.mkdir(parents=True)
            config.write_text(
                "# CONSUMER CONFIGURATION\nLINKED_FILES=data/;reports/\n",
                encoding="utf-8",
            )
            before = config.read_bytes()

            process = self._run_sync(consumer, source)

            self._assert_sync_succeeded(process)
            self.assertEqual(config.read_bytes(), before)

    def test_merge_json_preserves_consumer_keys(self):
        with self._temporary_repositories() as (source, source_commit, consumer):
            settings_path = consumer / JSON_DESTINATION
            settings_path.parent.mkdir(parents=True)
            self._write_json(settings_path, {"sandbox": {"enabled": True}})

            self._assert_initial_sync(source, source_commit, consumer)
            merged = self._read_json(settings_path)
            aes_settings = self._read_json(source / JSON_DESTINATION)

            self.assertEqual(
                merged["hooks"]["PreToolUse"],
                aes_settings["hooks"]["PreToolUse"],
            )
            self.assertEqual(
                merged["hooks"]["SessionStart"],
                aes_settings["hooks"]["SessionStart"],
            )
            self.assertEqual(merged["sandbox"], {"enabled": True})

    def test_check_detects_drift_in_managed_json_key(self):
        with self._temporary_repositories() as (source, source_commit, consumer):
            settings_path = consumer / JSON_DESTINATION
            settings_path.parent.mkdir(parents=True)
            self._write_json(settings_path, {"sandbox": {"enabled": True}})
            self._assert_initial_sync(source, source_commit, consumer)

            managed_drift = self._read_json(settings_path)
            managed_drift["hooks"]["PreToolUse"] = [{"local": "drift"}]
            self._write_json(settings_path, managed_drift)
            drift_process = self._run_sync(consumer, source, "--check")
            drift_output = self._output(drift_process)

            self.assertNotEqual(drift_process.returncode, 0, drift_output)
            self.assertIn(JSON_DESTINATION, drift_output)

            # Ripristina le sole chiavi gestite e cambia solo quella che AES
            # non possiede: sandbox non deve essere considerata drift.
            self._assert_sync_succeeded(self._run_sync(consumer, source))
            consumer_only_change = self._read_json(settings_path)
            consumer_only_change["sandbox"] = {"enabled": False}
            self._write_json(settings_path, consumer_only_change)
            clean_process = self._run_sync(consumer, source, "--check")

            self.assertEqual(clean_process.returncode, 0, self._output(clean_process))

    def test_manifest_rejects_absolute_parent_and_git_paths_before_mutation(self):
        with self._temporary_repositories() as (source, _source_commit, consumer):
            (source / "harness" / "manifest.txt").write_text(
                "copy harness/scripts/git-orient.sh .git/hooks/pre-push\n"
                "copy harness/scripts/git-orient.sh ../outside\n",
                encoding="utf-8",
            )
            process = self._run_sync(consumer, source)
            output = self._output(process)
            self.assertNotEqual(process.returncode, 0, output)
            self.assertIn("manifest", output.lower())
            self.assertFalse((consumer / ".git" / "hooks" / "pre-push").exists())
            self.assertFalse((consumer.parent / "outside").exists())

    def test_manifest_rejects_source_and_destination_symlink_escape(self):
        with self._temporary_repositories() as (source, _source_commit, consumer):
            outside = source.parent / "outside-source"
            outside.mkdir()
            (outside / "secret.txt").write_text("must stay outside\n", encoding="utf-8")
            source_link = source / "harness" / "scripts" / "escape.sh"
            source_link.symlink_to(outside / "secret.txt")
            (source / "harness" / "manifest.txt").write_text(
                "copy harness/scripts/escape.sh harness/scripts/escape.sh\n",
                encoding="utf-8",
            )
            source_escape = self._run_sync(consumer, source)
            self.assertNotEqual(source_escape.returncode, 0, self._output(source_escape))
            self.assertFalse((consumer / "harness" / "scripts" / "escape.sh").exists())

            destination_target = consumer.parent / "outside-destination.txt"
            destination_target.write_text("must stay outside\n", encoding="utf-8")
            destination = consumer / "policies" / "escape.md"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(destination_target)
            (source / "harness" / "manifest.txt").write_text(
                "copy policies/git-workflow.md policies/escape.md\n",
                encoding="utf-8",
            )
            destination_escape = self._run_sync(consumer, source)
            self.assertNotEqual(destination_escape.returncode, 0, self._output(destination_escape))
            self.assertEqual(destination_target.read_text(encoding="utf-8"), "must stay outside\n")


if __name__ == "__main__":
    unittest.main()
