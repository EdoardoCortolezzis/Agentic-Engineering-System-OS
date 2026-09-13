"""Eval deterministico per il git workflow (ADR 0005).

Verifica che l'enforcement del workflow sia reale e non solo documentato:

1. Il hook PreToolUse versionato in `.claude/settings.json` blocca il push
   diretto verso i branch protetti (`main`, `master`, `production`,
   `develop`) e lascia passare i branch di lavoro (`feature/*`, `fix/*`,
   `hotfix/*`, `release/*`).
2. Gli artefatti del workflow esistono: ADR, policy, riferimento in
   `AGENTS.md`.

Il test (1) esegue il comando hook reale con payload sintetici — lo stesso
metodo usato in Fase 0 Step 3 per verificare il blocco dei segreti — così
non c'è drift tra il hook e il suo test: il hook è la fonte di verità.

Esecuzione: `python3 -m unittest evals/git-workflow/test_git_workflow.py`
"""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

PROTECTED_BRANCHES = ("main", "master", "production", "develop")


def load_push_hook_command() -> str:
    """Estrae il comando del hook di protezione push dal settings versionato.

    Fallisce (AssertionError) se il hook non è presente: l'eval deve
    rompersi, non passare silenziosamente, se l'enforcement viene rimosso.
    """
    settings = json.loads((REPO / ".claude" / "settings.json").read_text())
    for entry in settings.get("hooks", {}).get("PreToolUse", []):
        for hook in entry.get("hooks", []):
            cmd = hook.get("command", "")
            if "guard-push.sh" in cmd:
                return cmd
    raise AssertionError(
        "push-protection hook not found in .claude/settings.json"
    )


PUSH_HOOK = load_push_hook_command()


def run_hook(git_command: str) -> subprocess.CompletedProcess:
    """Esegue il hook reale con un payload PreToolUse sintetico."""
    payload = json.dumps({"tool_input": {"command": git_command}})
    return subprocess.run(
        PUSH_HOOK,
        shell=True,
        input=payload,
        capture_output=True,
        text=True,
    )


class TestPushProtectionHook(unittest.TestCase):
    def _assert_blocked(self, command: str) -> None:
        proc = run_hook(command)
        self.assertEqual(
            proc.returncode, 2,
            f"should be blocked but rc={proc.returncode}: {command!r}",
        )
        self.assertIn("BLOCKED", proc.stderr, command)

    def _assert_allowed(self, command: str) -> None:
        proc = run_hook(command)
        self.assertEqual(
            proc.returncode, 0,
            f"should be allowed but rc={proc.returncode} stderr={proc.stderr}: "
            f"{command!r}",
        )

    def test_push_to_protected_branches_is_blocked(self):
        for branch in PROTECTED_BRANCHES:
            with self.subTest(branch=branch):
                self._assert_blocked(f"git push origin {branch}")
                self._assert_blocked(f"git push -u origin {branch}")

    def test_implicit_push_is_blocked(self):
        self._assert_blocked("git push")
        self._assert_blocked("git push --force")
        self._assert_blocked("git push origin")

    def test_alternate_git_invocations_do_not_bypass_the_guard(self):
        """`git push` non e' sempre in testa al comando.

        `git -C <dir> push` e' la forma tipica di chi lavora con la CWD fuori
        dalla root del repo — innocente, non una manovra ostile — e pretendere
        `words[0]=="git" && words[1]=="push"` la lasciava passare per intero,
        insieme a `-c`, `sudo` ed `env`.
        """
        for command in (
            "git -C /tmp/repo push origin develop",
            "git -c user.email=a@b.c push origin develop",
            "git --git-dir=/x --work-tree=/y push origin develop",
            "sudo git push origin develop",
            "env git push origin develop",
        ):
            with self.subTest(command=command):
                self._assert_blocked(command)
        # La stessa forma verso un branch di lavoro resta lecita: e' il
        # comando che conta, non il modo in cui e' scritto.
        self._assert_allowed("git -C /tmp/repo push origin feature/x")

    def test_command_token_obscured_by_shell_metacharacters_is_blocked(self):
        """Un metacarattere attaccato a `git` non lo rende un comando diverso.

        Trovato da una sessione avversaria di test funzionali: la ricerca
        del token pretendeva la parola esatta `git`, quindi `(git`, `$(git`,
        `` `git `` e un percorso come `/usr/bin/git` non venivano mai
        riconosciuti come l'inizio di un comando git — la stessa classe di
        oscuramento che `is_opaque` blocca dopo `push`, ma che in posizione
        di comando non veniva mai raggiunta. La shell esegue tutte queste
        forme per davvero.
        """
        for command in (
            "(git push origin develop)",
            "$(git push origin develop)",
            "`git push origin develop`",
            "/usr/bin/git push origin develop",
            "sudo /usr/bin/git push origin develop",
            'bash -c "git push origin develop"',
            "sh -c 'git push origin develop'",
        ):
            with self.subTest(command=command):
                self._assert_blocked(command)

    def test_trailing_shell_comment_is_not_evaluated_as_a_command(self):
        """Cio' che segue un `#` e' un commento della shell, non un comando.

        Trovato dalla stessa sessione avversaria: `git push origin feature/x
        # git push origin develop` veniva bloccato perche' il testo dopo `#`
        era analizzato come se fosse eseguito, ma la shell non lo esegue.
        """
        self._assert_allowed(
            "git push origin feature/x # git push origin develop"
        )

    def test_arguments_the_shell_would_expand_are_blocked(self):
        """Un argomento espanso dalla shell non e' confrontabile alla lettera.

        `git push origin 'develop'` e' la grafia naturale per proteggere un
        nome, non un tentativo di aggirare: confrontando il testo grezzo il
        token era `'develop'` con gli apici, e non corrispondeva a nulla.
        """
        for command in (
            "git push origin 'develop'",
            'git push origin "develop"',
            "git push origin $BRANCH",
            "git push origin $(git branch --show-current)",
            "git push origin develop&",
        ):
            with self.subTest(command=command):
                self._assert_blocked(command)

    def test_shell_glob_metacharacters_are_blocked(self):
        """Un glob non e' confrontabile alla lettera con un nome di branch.

        Rilievo della security review: `*`, `?`, `[` mancavano dal set di
        caratteri opachi. Un nome di branch valido non puo' contenerli — git
        li vieta — quindi bloccarli non produce mai un falso positivo su un
        branch reale.
        """
        for command in (
            "git push origin refs/heads/deve*",
            "git push origin devel?p",
            "git push origin dev[e]lop",
        ):
            with self.subTest(command=command):
                self._assert_blocked(command)

    def test_unparsable_payload_is_blocked_by_the_guard_itself(self):
        """Il messaggio lo scrive lo script, non il wrapper.

        Prima l'uscita dipendeva dal codice d'errore di `jq`: il blocco
        arrivava perche' il wrapper trattava «non 0 e non 2» come dubbio. Un
        `jq` che uscisse con 2 avrebbe prodotto un blocco muto.
        """
        guard = REPO / "harness" / "scripts" / "guard-push.sh"
        process = subprocess.run(["bash", str(guard)], input="non-json",
                                 capture_output=True, text=True)
        self.assertEqual(process.returncode, 2, process.stderr)
        self.assertIn("BLOCKED", process.stderr)

    def test_payload_without_a_command_is_allowed(self):
        """JSON valido senza comando non e' un dubbio: non c'e' nulla da valutare."""
        guard = REPO / "harness" / "scripts" / "guard-push.sh"
        process = subprocess.run(["bash", str(guard)], input='{"tool_input":{}}',
                                 capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_empty_stdin_is_blocked(self):
        """Uno stdin vuoto non e' "nessun comando da valutare": e' non aver
        letto nulla. Trovato dalla sessione avversaria: la guardia
        permetteva senza aver visto alcun payload.
        """
        guard = REPO / "harness" / "scripts" / "guard-push.sh"
        process = subprocess.run(["bash", str(guard)], input="",
                                 capture_output=True, text=True)
        self.assertEqual(process.returncode, 2, process.stderr)

    def test_integration_branch_with_trailing_comment_is_still_protected(self):
        """`INTEGRATION_BRANCH="staging"  # commento` e' una grafia naturale.

        Senza togliere il commento il valore diventava `staging" # commento`,
        spezzato in parole da cui nessuna corrispondeva a `staging`: il
        branch di integrazione del consumer restava pushabile in silenzio,
        proprio nel caso in cui la configurazione serve a qualcosa.
        """
        with tempfile.TemporaryDirectory(prefix="guard-push-conf-") as directory:
            root = Path(directory)
            config_dir = root / "harness" / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "harness.conf").write_text(
                'INTEGRATION_BRANCH="staging"   # branch di integrazione\n',
                encoding="utf-8",
            )
            script_dir = root / "harness" / "scripts"
            script_dir.mkdir(parents=True)
            guard_source = (REPO / "harness" / "scripts" / "guard-push.sh").read_text(
                encoding="utf-8"
            )
            guard = script_dir / "guard-push.sh"
            guard.write_text(guard_source, encoding="utf-8")
            guard.chmod(0o755)
            payload = json.dumps(
                {"tool_input": {"command": "git push origin staging"}}
            )

            process = subprocess.run(
                ["bash", str(guard)], input=payload,
                capture_output=True, text=True,
            )

            self.assertEqual(process.returncode, 2, process.stderr)

    def test_separate_command_segments_are_evaluated_independently(self):
        self._assert_allowed(
            "git push origin feature/x && gh pr create --base develop"
        )

    def test_refspec_forms_for_protected_branches_are_blocked(self):
        for command in (
            "git push origin HEAD:develop",
            "git push origin +develop",
        ):
            with self.subTest(command=command):
                self._assert_blocked(command)

    def test_moving_the_harness_release_tag_is_blocked(self):
        """Spostare il tag di rilascio pubblica codice nella CI dei consumer.

        Da ADR 0023 il workflow di bump esegue `.aes-source` pinnato a
        `harness-release`: chi muove quel tag decide cosa gira nella CI di
        ogni consumer, con AES_SYNC_TOKEN nello scope. E' una pubblicazione,
        non un push di lavoro, e la guardia la tratta come i branch di
        integrazione. Prima questo caso passava: `is_protected_ref`
        confrontava solo nomi di branch.
        """
        for command in (
            "git push origin harness-release",
            "git push origin refs/tags/harness-release",
            "git push -f origin harness-release",
            "git push --force origin refs/tags/harness-release",
            "git push origin HEAD:refs/tags/harness-release",
            "git push origin :refs/tags/harness-release",
            "git push origin --delete harness-release",
            "git push origin +refs/tags/harness-release",
        ):
            with self.subTest(command=command):
                self._assert_blocked(command)

    def test_moving_the_release_tag_via_the_api_is_blocked(self):
        """`git push` non e' l'unico modo di spostare un ref.

        L'API REST lo fa con una chiamata sola, e `gh release create` crea il
        tag per conto suo. Coprire solo `git push` lascia scoperto proprio
        l'agente di questa postazione, che e' il modello di minaccia
        dichiarato in ADR 0023 — e che ha `gh` a portata di mano.
        """
        for command in (
            "gh api -X PATCH repos/Edo/AES/git/refs/tags/harness-release -f sha=abc",
            "gh api --method DELETE repos/o/r/git/refs/tags/harness-release",
            "curl -X PATCH https://api.github.com/repos/o/r/git/refs/tags/harness-release",
            "gh release create harness-release",
            "gh release delete harness-release",
        ):
            with self.subTest(command=command):
                self._assert_blocked(command)

    def test_quoted_or_computed_release_tag_refs_are_blocked(self):
        """Gli apici sono la grafia naturale, non una manovra ostile.

        `read -r -a` non interpreta il quoting: un ref citato non termina col
        nome del tag e passava il confronto. E' la stessa classe di errore
        che l'intestazione di `guard-push.sh` descrive per `git push origin
        'develop'`, sul ramo nuovo. Un ref costruito a runtime non e'
        confrontabile affatto, quindi si blocca.
        """
        for command in (
            "gh api -X PATCH 'repos/o/r/git/refs/tags/harness-release' -f sha=abc",
            'gh api -X PATCH "repos/o/r/git/refs/tags/harness-release"',
            "gh release create 'harness-release'",
            'gh api -X PATCH "repos/o/r/git/refs/tags/$TAG"',
            "gh api -X PATCH repos/o/r/git/refs/tags/${TAG}",
            # Anche il nome del binario si puo' citare: `${word##*[...]}` su
            # `'curl'` taglia fino all'apice finale e lascia la stringa
            # vuota, quindi il ramo API veniva saltato del tutto.
            "'curl' -X PATCH https://api.github.com/repos/o/r/git/refs/tags/harness-release",
            '"/usr/bin/gh" api -X PATCH repos/o/r/git/refs/tags/harness-release',
            "'gh' release create harness-release",
            # Il tag si crea anche nominandolo in un campo invece che nel
            # path: `-f ref=...` sull'API dei ref, `-f tag_name=...` su
            # quella delle release.
            "gh api -X POST repos/o/r/git/refs -f ref=refs/tags/harness-release -f sha=abc",
            "gh api repos/o/r/releases -f tag_name=harness-release -f target_commitish=abc",
            # Un carattere di controllo appeso al nome non deve far fallire
            # il confronto esatto: GitHub rifiuterebbe quel ref, ma la
            # guardia non deve dipendere da cosa rifiuta il server. `%0A`
            # cade da solo (la command substitution toglie i newline finali),
            # `%09` e `%0B` no: sono quelli che rendono il trim necessario.
            "gh api -X POST repos/o/r/git/refs -f ref=refs/tags/harness-release%09 -f sha=abc",
            "gh api -X POST repos/o/r/git/refs -f ref=refs/tags/harness-release%0B -f sha=abc",
            "gh api -X POST repos/o/r/git/refs -f ref=refs/tags/harness-release%0A -f sha=abc",
        ):
            with self.subTest(command=command):
                self._assert_blocked(command)

    def test_refspec_with_an_empty_destination_is_blocked(self):
        """Una destinazione vuota non dice nulla, quindi si blocca.

        Oggi git rifiuta queste grafie da solo (`fatal: invalid refspec`,
        verificato), quindi non sono un varco aperto. Ma la guardia non deve
        dipendere dalla validazione di git per non lasciar passare un ref
        protetto: `${ref##*:}` su `harness-release:` produce la stringa
        vuota, che non corrispondeva a nulla e passava.
        """
        for command in (
            "git push origin harness-release:",
            "git push origin refs/tags/harness-release:",
            "git push origin develop:",
        ):
            with self.subTest(command=command):
                self._assert_blocked(command)

    def test_bulk_tag_push_options_are_blocked(self):
        """Un push puo' pubblicare il tag senza nominarlo.

        `git push --tags origin feature/x` ha una refspec innocua — quindi
        supera sia la regola sul push senza destinazione sia il confronto coi
        ref protetti — e intanto aggiorna tutti i tag remoti, di rilascio
        compreso. Bastano due comandi: uno sposta il tag locale, l'altro lo
        pubblica senza che la guardia lo veda.
        """
        for command in (
            "git push --tags --force origin feature/x",
            "git push --tags origin feature/x",
            "git push --mirror origin feature/x",
            "git push --follow-tags origin feature/x",
            # Non e' un'opzione di `git push` sul git attuale (esce 129), ma
            # e' nell'elenco perche' costa un token e il giorno che lo
            # diventasse cancellerebbe il tag di rilascio in silenzio.
            "git push --prune-tags origin feature/x",
        ):
            with self.subTest(command=command):
                self._assert_blocked(command)

    def test_percent_encoded_release_tag_refs_are_blocked(self):
        """Lo stesso path per l'API, un testo diverso per `case`.

        `refs%2Ftags%2Fharness-release` e `harness%2Drelease` raggiungono lo
        stesso ref: confrontare la forma codificata alla lettera e' il bypass
        piu' diretto del controllo API.
        """
        for command in (
            "gh api -X PATCH repos/o/r/git/refs%2Ftags%2Fharness-release",
            "gh api -X PATCH repos/o/r/git/refs/tags/harness%2Drelease",
            # Doppia e tripla codifica: fermarsi al primo giro di decodifica
            # lascia passare `%252F`, che ridiventa `%2F` e non corrisponde
            # piu' a nulla.
            "gh api -X PATCH repos/o/r/git/refs%252Ftags%252Fharness-release",
            "gh api -X PATCH repos/o/r/git/refs%25252Ftags%25252Fharness-release",
        ):
            with self.subTest(command=command):
                self._assert_blocked(command)

    def test_api_calls_that_do_not_touch_the_release_tag_are_allowed(self):
        """Il prezzo del blocco su gh/curl e' limitato al tag protetto.

        Se ogni `gh api` diventasse sospetto la guardia verrebbe disattivata,
        che e' il modo peggiore di perdere una protezione.
        """
        for command in (
            "gh api repos/o/r/git/refs/tags/v1.2.3 -X PATCH",
            "gh pr list --base develop",
            "curl https://example.com/harness-release-notes",
            "git ls-remote origin refs/tags/harness-release",
        ):
            with self.subTest(command=command):
                self._assert_allowed(command)

    def test_guarded_tag_matches_the_ref_pinned_by_the_bump_workflow(self):
        """Due file scollegati che devono dire lo stesso nome.

        Il workflow pinna `.aes-source` a un tag; la guardia protegge un
        elenco di tag. Se qualcuno cambia il tag di rilascio in un file solo,
        la protezione si spegne in silenzio e nessun altro test se ne accorge.
        """
        workflow = (REPO / ".github" / "workflows" / "aes-harness-bump.yml").read_text(
            encoding="utf-8"
        )
        aes_block = re.search(
            r"(?ms)^\s*- name: Check out [^\n]*\n(?:(?!^\s*- name:).)*?"
            r"repository:\s*\$\{\{\s*vars\.AES_SOURCE_REPOSITORY\s*\}\}.*?"
            r"(?=^\s*- name:|\Z)",
            workflow,
        )
        self.assertIsNotNone(aes_block, "Manca il checkout di AES nel workflow.")
        pinned = re.search(r"(?m)^\s+ref:\s*(.+?)\s*$", aes_block.group(0))
        self.assertIsNotNone(pinned, "Il checkout di AES non pinna alcun ref.")
        self.assertEqual(
            pinned.group(1), "${{ vars.AES_SOURCE_REF }}",
            "Il ref deve essere configurabile.",
        )
        guard = (REPO / "harness" / "scripts" / "guard-push.sh").read_text(
            encoding="utf-8"
        )
        declared = re.search(r'(?m)^PROTECTED_TAGS="([^"]*)"', guard)
        self.assertIsNotNone(declared, "guard-push.sh non dichiara PROTECTED_TAGS.")
        self.assertIn("harness-release", declared.group(1))

    def test_brace_expansion_of_the_release_tag_is_blocked(self):
        """Le graffe sono espansione di shell come il glob.

        `harness-relea{se,se}` e' testo che non corrisponde a nulla per la
        guardia, e il nome del tag di rilascio per la shell. Mancavano dalla
        classe di caratteri opachi, quindi il bypass valeva sia per il ramo
        `git push` sia per quello API.
        """
        for command in (
            "git push origin harness-relea{se,se}",
            "gh api -X PATCH repos/o/r/git/refs/tags/harness-relea{se,se}",
        ):
            with self.subTest(command=command):
                self._assert_blocked(command)

    def test_naming_the_release_tag_in_prose_is_not_blocked(self):
        """Parlare del tag non e' spostarlo.

        La prima stesura bloccava qualunque comando `gh` che contenesse il
        nome del tag: commentare una PR o cercarne il nome diventava
        impossibile. Una guardia che impedisce di *parlare* del rilascio crea
        attrito proprio sul flusso che dovrebbe proteggere, e l'attrito e' il
        modo piu' comune di perdere una guardia.
        """
        for command in (
            "gh pr comment 49 --body il tag harness-release si sposta a mano",
            "gh pr list --search harness-release",
            "gh issue create --title bump di harness-release",
        ):
            with self.subTest(command=command):
                self._assert_allowed(command)

    def test_unrelated_tags_are_not_blocked(self):
        """La protezione e' del tag di rilascio, non dei tag in generale.

        Bloccare ogni tag renderebbe la guardia un ostacolo al versionamento
        normale, e una guardia che infastidisce viene disattivata.
        """
        for command in (
            "git push origin refs/tags/v1.2.3",
            "git push origin v1.2.3",
            "git push origin harness-release-notes",
        ):
            with self.subTest(command=command):
                self._assert_allowed(command)

    def test_missing_guard_fails_closed(self):
        command = PUSH_HOOK.replace("guard-push.sh", "guard-push.sh.missing")
        payload = json.dumps({"tool_input": {"command": "git push origin feature/x"}})
        process = subprocess.run(command, shell=True, input=payload,
                                 capture_output=True, text=True)
        self.assertEqual(process.returncode, 2)
        self.assertIn("guard-push", process.stderr)

    def _run_guard_in(self, repo: Path, command: str) -> subprocess.CompletedProcess:
        """Invoca lo script dal repo indicato: HEAD dipende dal branch corrente."""
        guard = REPO / "harness" / "scripts" / "guard-push.sh"
        payload = json.dumps({"tool_input": {"command": command}})
        return subprocess.run(
            ["bash", str(guard)], cwd=repo, input=payload,
            capture_output=True, text=True,
        )

    def test_head_refspec_depends_on_the_checked_out_branch(self):
        """`git push origin HEAD` non nomina una destinazione: la sceglie HEAD.

        Su un branch di lavoro e' lecito; su `develop` aggiorna `develop`, ed
        e' esattamente il push che la guardia esiste per fermare. Senza
        risolvere HEAD la guardia lascia passare il caso peggiore.
        """
        with tempfile.TemporaryDirectory(prefix="guard-push-head-") as directory:
            repo = Path(directory)
            def git(*args: str) -> None:
                subprocess.run(["git", "-C", str(repo), *args], check=True,
                               capture_output=True, text=True)
            git("init", "-q", "-b", "develop", ".")
            git("-c", "user.email=a@b.c", "-c", "user.name=t",
                "commit", "-q", "--allow-empty", "-m", "base")

            for command in ("git push origin HEAD", "git push origin @"):
                with self.subTest(branch="develop", command=command):
                    self.assertEqual(
                        self._run_guard_in(repo, command).returncode, 2,
                        f"{command!r} su develop deve essere bloccato",
                    )

            git("checkout", "-q", "-b", "feature/x")
            for command in ("git push origin HEAD", "git push origin @"):
                with self.subTest(branch="feature/x", command=command):
                    self.assertEqual(
                        self._run_guard_in(repo, command).returncode, 0,
                        f"{command!r} su un branch di lavoro deve passare",
                    )

    def test_guard_crash_fails_closed(self):
        """Un guard che va in errore deve bloccare, non lasciar passare.

        Un exit code diverso da 0 e 2 significa che il guard non ha deciso:
        un payload illeggibile, jq assente, lo script corrotto. Trattarlo
        come "permesso" spegne la protezione senza che nessuno lo veda, che
        e' la stessa classe di falso verde dell'ADR 0010.
        """
        process = subprocess.run(PUSH_HOOK, shell=True, input="non-json",
                                 capture_output=True, text=True)
        self.assertEqual(process.returncode, 2, process.stderr)
        self.assertIn("BLOCKED", process.stderr)

    def test_push_to_work_branches_is_allowed(self):
        for command in (
            "git push origin feature/git-workflow",
            "git push origin fix/off-by-one",
            "git push origin hotfix/login-bug",
            "git push origin release/1.0",
            "git push origin my-feature",
        ):
            with self.subTest(command=command):
                self._assert_allowed(command)

    def test_non_push_git_commands_are_allowed(self):
        for command in (
            "git push origin --delete old-branch",
            "git status",
            "git fetch origin",
        ):
            with self.subTest(command=command):
                self._assert_allowed(command)


class TestWorkflowArtifacts(unittest.TestCase):
    def test_adr_numbers_are_unique(self):
        """In un registro di decisioni il numero e' l'identificatore.

        `0006` ne identificava due — la review su OpenRouter e il protocollo
        a turni di codex-orchestrate — e i riferimenti erano gia' ambigui nei
        fatti: due voci di STATE.md citavano «ADR 0006» intendendo documenti
        diversi.
        """
        numbers = {}
        for path in sorted((REPO / "docs" / "decisions").glob("*.md")):
            match = re.match(r"^(\d{4})-", path.name)
            self.assertIsNotNone(match, f"ADR senza numero: {path.name}")
            number = match.group(1)
            self.assertNotIn(
                number, numbers,
                f"numero ADR duplicato {number}: {numbers.get(number)} e {path.name}",
            )
            numbers[number] = path.name

    def test_adr_exists(self):
        self.assertTrue(
            (REPO / "docs" / "decisions" / "0005-git-workflow-main-develop-hotfix.md").exists(),
            "ADR 0005 mancante",
        )

    def test_policy_exists(self):
        self.assertTrue((REPO / "policies" / "git-workflow.md").exists())

    def test_agents_references_policy(self):
        text = (REPO / "AGENTS.md").read_text()
        self.assertIn("policies/git-workflow.md", text)

    def test_merge_is_reserved_to_the_human(self):
        """La regola nata da un merge non autorizzato deve restare scritta.

        Verifica la sostanza, non l'esistenza del file: la regola è il solo
        guardrail contro il ripetersi dell'incidente, e una riscrittura
        distratta la toglierebbe senza che niente diventi rosso.
        """
        policy = (REPO / "policies" / "git-workflow.md").read_text()
        agents = (REPO / "AGENTS.md").read_text()

        self.assertIn(
            "gh pr merge",
            policy,
            "policies/git-workflow.md deve dire esplicitamente che l'agente non "
            "esegue `gh pr merge` di propria iniziativa.",
        )
        for document, name in ((policy, "policies/git-workflow.md"), (agents, "AGENTS.md")):
            self.assertRegex(
                document,
                r"(?i)merge (is performed|remains) (by )?a human",
                f"{name} must state that a human performs the merge.",
            )


if __name__ == "__main__":
    unittest.main()
