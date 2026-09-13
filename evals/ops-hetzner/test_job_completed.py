"""Eval della pulizia di fine job: lock, ledger, worktree.

Esecuzione: `python3 -m unittest evals/ops-hetzner/test_job_completed.py`
"""

import os
import subprocess
import threading
import time
import unittest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hooks_harness import HAS_GNU_TOOLS, HookTestCase, JOB_STARTED, JOB_COMPLETED, REPO, _fake_bin


@unittest.skipUnless(HAS_GNU_TOOLS, "servono flock e df GNU, come sul server")
class TestJobCompleted(HookTestCase):
    def setUp(self):
        super().setUp()
        self.fake_df(100)
        self.worktrees = self.tmp / "worktrees"

    def _git(self, *args, cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"},
        )

    def _repo_con_worktree(self, name: str) -> tuple:
        repo = self.tmp / "repo"
        if not repo.exists():
            repo.mkdir()
            self._git("init", "-b", "main", cwd=repo)
            (repo / "file.txt").write_text("uno\n")
            (repo / ".gitignore").write_text("artefatti/\n")
            self._git("add", "-A", cwd=repo)
            self._git("commit", "-m", "iniziale", cwd=repo)
        # Due livelli sotto la root: e' la profondita' che l'hook cerca.
        wt = self.worktrees / "progetto" / name
        wt.parent.mkdir(parents=True, exist_ok=True)
        added = self._git("worktree", "add", "-b", name, str(wt), cwd=repo)
        self.assertEqual(added.returncode, 0, added.stderr)
        # L'hook guarda solo i worktree piu' vecchi della soglia.
        os.utime(wt, (0, 0))
        return repo, wt

    def _registrati(self, repo: Path) -> str:
        return self._git("worktree", "list", cwd=repo).stdout

    def test_libera_il_segnaposto_del_proprio_job(self):
        started = self.run_started(GITHUB_RUN_ID="7", GITHUB_JOB="worker",
                                   GITHUB_WORKSPACE=self.tmp / "ws-a")
        self.assertEqual(started.returncode, 0, started.stderr)
        self.run_completed(GITHUB_RUN_ID="7", GITHUB_JOB="worker",
                           GITHUB_WORKSPACE=self.tmp / "ws-a")
        self.assertEqual(self.stamps(), [])

    def test_non_tocca_il_lock_di_un_altro_worker_della_stessa_matrice(self):
        """Due worker della stessa matrice condividono run id e nome del job.

        Con un glob sul run id, il primo che finisce toglierebbe anche il
        lock dell'altro, ancora in corso: chi parte dopo conterebbe meno job
        di quanti ne girano e supererebbe il tetto.
        """
        comune = {"GITHUB_RUN_ID": "9", "GITHUB_JOB": "worker",
                  "AES_HOST_MAX_JOBS": 2}
        primo = self.run_started(GITHUB_WORKSPACE=self.tmp / "ws-a", **comune)
        secondo = self.run_started(GITHUB_WORKSPACE=self.tmp / "ws-b", **comune)
        self.assertEqual(primo.returncode, 0, primo.stderr)
        self.assertEqual(secondo.returncode, 0, secondo.stderr)
        self.assertEqual(len(self.stamps()), 2)

        self.run_completed(GITHUB_WORKSPACE=self.tmp / "ws-a", **comune)
        rimasti = self.stamps()
        self.assertEqual(len(rimasti), 1,
                         "la chiusura di un worker ha liberato il lock dell'altro")
        self.assertIn("ws_b", rimasti[0].name)

    def test_rifiuta_un_archivio_non_assoluto(self):
        """Un archivio relativo scriverebbe nel checkout che sta per sparire."""
        result = self.run_completed(AES_ARCHIVE_DIR="archivio-relativo")
        self.assertEqual(result.returncode, 0, "la pulizia non deve far fallire il job")
        self.assertIn("ARCHIVE is not an absolute path", result.stderr)

    def test_segnala_il_lock_non_rilasciato(self):
        """Un posto perso in silenzio ferma la coda senza dirlo.

        Se il segnaposto non si riesce a togliere, i job successivi restano
        in attesa di qualcosa che nessuno libererà: l'hook non fa fallire il
        job per questo, ma deve lasciarne traccia.
        """
        avvio = {"GITHUB_RUN_ID": "5", "GITHUB_JOB": "worker",
                 "GITHUB_WORKSPACE": self.tmp / "ws-a"}
        self.assertEqual(self.run_started(**avvio).returncode, 0)
        stamp = self.stamps()[0]
        # Una directory al posto del file: `rm -f` fallisce, come farebbe un
        # filesystem in sola lettura, ma in modo riproducibile anche da root.
        stamp.unlink()
        stamp.mkdir()
        (stamp / "dentro").write_text("x")
        result = self.run_completed(**avvio)
        self.assertEqual(result.returncode, 0, "la pulizia non deve far fallire il job")
        self.assertIn("job lock not released", result.stderr)

    def test_archivia_il_ledger_fuori_dal_checkout(self):
        workspace = self.tmp / "workspace"
        artifacts = workspace / ".agent" / "tasks" / "artifacts"
        artifacts.mkdir(parents=True)
        (artifacts / "ledger.json").write_text('{"stato": "in-corso"}')
        self.run_completed(GITHUB_WORKSPACE=workspace, GITHUB_RUN_ID="42")
        copie = list(self.archive.rglob("ledger.json"))
        self.assertEqual(len(copie), 1, "il ledger non e' stato archiviato")
        self.assertIn("42", str(copie[0]))

    def test_segnala_l_archiviazione_fallita(self):
        """Se l'unica copia non viene scritta, l'operatore deve saperlo.

        Il checkout successivo cancella l'originale: un fallimento taciuto
        qui diventa un ledger perso che nessuno ha visto sparire.
        """
        workspace = self.tmp / "workspace"
        artifacts = workspace / ".agent" / "tasks" / "artifacts"
        artifacts.mkdir(parents=True)
        (artifacts / "ledger.json").write_text("{}")
        # Un file al posto della directory di archivio: mkdir -p fallisce.
        self.archive.parent.mkdir(parents=True, exist_ok=True)
        self.archive.write_text("non sono una directory\n")
        result = self.run_completed(GITHUB_WORKSPACE=workspace, GITHUB_RUN_ID="3")
        self.assertEqual(result.returncode, 0, "la pulizia non deve far fallire il job")
        self.assertIn("ledger not archived", result.stderr)

    def _archivio_con(self, giorni: int, nome: str) -> Path:
        vecchio = self.archive / nome
        vecchio.mkdir(parents=True)
        (vecchio / "state.json").write_text("{}")
        quando = time.time() - giorni * 86400
        os.utime(vecchio, (quando, quando))
        return vecchio

    def test_per_default_non_cancella_nessun_ledger(self):
        """Dopo la pulizia del checkout l'archivio è l'unica copia.

        Cancellarla è una decisione di chi amministra la macchina, non un
        effetto collaterale della fine di un job.
        """
        antico = self._archivio_con(60, "20250101")
        result = self.run_completed()
        self.assertTrue((antico / "state.json").exists(),
                        "un ledger è stato cancellato senza che nessuno lo chiedesse")
        self.assertIn("pruning is a human decision", result.stdout)

    def test_pota_solo_oltre_la_soglia_quando_e_configurata(self):
        """Una scadenza scritta nel file di root è un atto deliberato, ma
        deve togliere solo ciò che ha superato la soglia."""
        vecchio = self._archivio_con(30, "20250102")
        recente = self._archivio_con(1, "20250103")
        result = self.run_completed(AES_LEDGER_RETENTION_DAYS=14)
        self.assertFalse(vecchio.exists(), "il ledger scaduto non è stato rimosso")
        self.assertTrue((recente / "state.json").exists(),
                        "è stato rimosso un ledger ancora entro la soglia")
        self.assertIn("older than 14 days", result.stdout)

    def test_rimuove_il_worktree_pulito_e_la_sua_registrazione(self):
        """Cancellare la directory non basta.

        Il worktree resta registrato in `.git/worktrees` e il branch resta
        occupato: il job successivo che prova a ricrearlo fallisce.
        """
        repo, wt = self._repo_con_worktree("pulito")
        self.run_completed(AES_WORKTREE_MAX_AGE_DAYS=1)
        self.assertFalse(wt.exists(), "la directory del worktree e' rimasta")
        self.assertNotIn("pulito", self._registrati(repo))

    def test_conserva_il_worktree_con_modifiche_non_committate(self):
        repo, wt = self._repo_con_worktree("sporco")
        (wt / "in-corso.txt").write_text("lavoro non finito\n")
        os.utime(wt, (0, 0))
        self.run_completed(AES_WORKTREE_MAX_AGE_DAYS=1)
        self.assertTrue((wt / "in-corso.txt").exists())

    def test_conserva_il_worktree_con_soli_file_ignorati(self):
        """Un file ignorato non e' recuperabile dal branch.

        Un `.env`, una venv o un artefatto di build non compaiono in uno
        status normale: il worktree sembra pulito e `git worktree remove` li
        porta via senza dire nulla. Non e' una cancellazione che un hook
        possa decidere da solo.
        """
        repo, wt = self._repo_con_worktree("ignorati")
        (wt / "artefatti").mkdir()
        (wt / "artefatti" / "credenziali.env").write_text("VALORE=x\n")
        os.utime(wt, (0, 0))
        result = self.run_completed(AES_WORKTREE_MAX_AGE_DAYS=1)
        self.assertTrue((wt / "artefatti" / "credenziali.env").exists(),
                        "un file ignorato e' stato cancellato dalla pulizia")
        self.assertIn("ignored files; kept", result.stdout)

    def test_conserva_il_worktree_se_git_status_fallisce(self):
        """Uno `status` che fallisce non e' un worktree pulito.

        L'output vuoto di un comando andato male — Git assente, repository
        corrotto, permessi — sembra identico a un worktree senza modifiche.
        Trattarlo come pulito significa cancellare lavoro non committato.
        """
        repo, wt = self._repo_con_worktree("illeggibile")
        canarino = wt / "da-non-perdere.txt"
        canarino.write_text("lavoro non committato\n")
        os.utime(wt, (0, 0))
        _fake_bin(self.bin, "git", 'echo "fatale" >&2; exit 128')
        result = self.run_completed(AES_WORKTREE_MAX_AGE_DAYS=1)
        self.assertTrue(canarino.exists(),
                        "un errore di Git ha portato a cancellare il worktree")
        self.assertIn("cannot be verified", result.stdout)

    def test_non_fallisce_mai_il_job(self):
        """La pulizia che va storta non deve cancellare un esito verde."""
        _fake_bin(self.bin, "git", 'exit 128')
        result = self.run_completed(GITHUB_WORKSPACE=self.tmp / "assente",
                                    AES_WORKTREE_MAX_AGE_DAYS=1)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
