"""Eval del gate di avvio job: configurazione, disco, concorrenza, segnaposto.

Il modulo tiene insieme i quattro gruppi perche' condividono l'impalcatura e
si leggono come un unico contratto: cosa deve rifiutare il gate e cosa deve
attendere. Se crescera' ancora, lo split naturale e' per gruppo —
`test_job_started_config.py` per validazione di limiti e percorsi,
`test_job_started_lock.py` per concorrenza e segnaposto — tenendo in
`hooks_harness.py` cio' che gia' condividono.

Esecuzione: `python3 -m unittest evals/ops-hetzner/test_job_started.py`
"""

import os
import subprocess
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hooks_harness import HAS_GNU_TOOLS, HookTestCase, JOB_STARTED, JOB_COMPLETED, REPO, _fake_bin


@unittest.skipUnless(HAS_GNU_TOOLS, "servono flock e df GNU, come sul server")
class TestJobStarted(HookTestCase):
    def test_rifiuta_il_job_quando_il_disco_e_sotto_soglia(self):
        """Un disco quasi pieno deve fermare il job prima che inizi.

        Un worker che riempie il disco a meta' lavoro lascia dietro di se'
        un checkout monco e un ledger incompleto: il rifiuto immediato e'
        l'esito meno costoso.
        """
        self.fake_df(3)
        result = self.run_started(AES_MIN_FREE_GB=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn("is below", result.stderr)
        self.assertEqual(self.stamps(), [])

    def test_accetta_il_primo_job_e_scrive_il_segnaposto(self):
        self.fake_df(100)
        result = self.run_started(AES_MIN_FREE_GB=15, GITHUB_RUN_ID="1",
                                  GITHUB_JOB="worker", GITHUB_REPOSITORY="o/r")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.stamps()), 1)

    def test_rifiuta_il_secondo_job_quando_l_attesa_scade(self):
        self.fake_df(100)
        first = self.run_started(AES_HOST_MAX_JOBS=1, GITHUB_RUN_ID="1",
                                 GITHUB_WORKSPACE=self.tmp / "ws-a")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_started(AES_HOST_MAX_JOBS=1, GITHUB_RUN_ID="2",
                                  GITHUB_WORKSPACE=self.tmp / "ws-b",
                                  AES_SLOT_WAIT_SECONDS=0)
        self.assertEqual(second.returncode, 1)
        self.assertIn("no host slot available", second.stderr)
        self.assertEqual(len(self.stamps()), 1)

    def test_non_parla_di_claim_quando_rifiuta_il_dispatcher(self):
        """Il dispatcher gira prima del claim.

        Dirgli che una issue è rimasta `aes:claimed` manderebbe a cercare a
        mano qualcosa che la schedule riprende da sola.
        """
        self.fake_df(100)
        self.run_started(GITHUB_JOB="worker", GITHUB_WORKSPACE=self.tmp / "ws-a")
        rifiutato = self.run_started(AES_SLOT_WAIT_SECONDS=0,
                                     GITHUB_JOB="dispatch",
                                     GITHUB_WORKSPACE=self.tmp / "ws-b")
        self.assertEqual(rifiutato.returncode, 1)
        self.assertNotIn("aes:claimed", rifiutato.stderr)

    def test_dice_che_la_issue_resta_claimed_quando_rifiuta(self):
        """Un rifiuto qui lascia una issue reclamata senza worker.

        Il dispatcher raccoglie `aes:ready` e `aes:waiting-provider`, non
        `aes:claimed`: se il log non dice che la issue va rimessa in coda,
        resta ferma senza che nulla lo segnali.
        """
        self.fake_df(100)
        self.run_started(AES_HOST_MAX_JOBS=1, GITHUB_JOB="worker",
                         GITHUB_WORKSPACE=self.tmp / "ws-a")
        rifiutato = self.run_started(AES_HOST_MAX_JOBS=1,
                                     GITHUB_JOB="worker",
                                     GITHUB_WORKSPACE=self.tmp / "ws-b",
                                     AES_SLOT_WAIT_SECONDS=0)
        self.assertEqual(rifiutato.returncode, 1)
        self.assertIn("aes:claimed", rifiutato.stderr)

    def test_aspetta_un_posto_invece_di_rifiutare_subito(self):
        """Il difetto che questo test tiene chiuso.

        Rifiutare immediatamente abbandonava una issue gia' reclamata. Qui il
        posto si libera dopo l'avvio dell'attesa: il job deve prenderlo.
        """
        self.fake_df(100)
        occupante = self.run_started(AES_HOST_MAX_JOBS=1, GITHUB_RUN_ID="1",
                                     GITHUB_WORKSPACE=self.tmp / "ws-a")
        self.assertEqual(occupante.returncode, 0, occupante.stderr)

        def _libera():
            time.sleep(2)
            for stamp in self.stamps():
                stamp.unlink()

        liberatore = threading.Thread(target=_libera)
        liberatore.start()
        self.addCleanup(liberatore.join)
        atteso = self.run_started(AES_HOST_MAX_JOBS=1, GITHUB_RUN_ID="2",
                                  GITHUB_WORKSPACE=self.tmp / "ws-b",
                                  AES_SLOT_WAIT_SECONDS=60,
                                  AES_SLOT_POLL_SECONDS=1)
        self.assertEqual(atteso.returncode, 0, atteso.stderr)
        self.assertIn("waiting for a slot", atteso.stdout)
        self.assertEqual(len(self.stamps()), 1)

    def test_pota_i_segnaposto_rimasti_da_job_morti(self):
        """Un job ucciso non deve togliere un posto per sempre.

        Se l'hook di fine non gira — job cancellato, runner riavviato — il
        segnaposto resta. Senza potatura il tetto scende di un posto a ogni
        incidente, fino a una coda ferma senza errori.
        """
        self.fake_df(100)
        self.lock_dir.mkdir(parents=True)
        orfano = self.lock_dir / "vecchio-worker-slot"
        morto = subprocess.Popen(["true"])
        morto.wait()
        orfano.write_text(f"pid={morto.pid}\njob morto\n")
        os.utime(orfano, (0, 0))
        result = self.run_started(AES_HOST_MAX_JOBS=1,
                                  AES_STAMP_MAX_AGE_MINUTES=60,
                                  AES_SLOT_WAIT_SECONDS=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(orfano.exists())

    def test_non_pota_il_segnaposto_di_un_job_lungo_ancora_vivo(self):
        """Un job lungo ha un segnaposto vecchio quanto lui.

        Potare per sola eta' aprirebbe un varco esattamente nel tetto che
        questo hook difende: il job successivo conterebbe zero e partirebbe.
        La potatura deve guardare se il processo e' ancora vivo.
        """
        self.fake_df(100)
        self.lock_dir.mkdir(parents=True)
        vivo = subprocess.Popen(["sleep", "30"])

        def _ferma():
            vivo.kill()
            vivo.wait()

        self.addCleanup(_ferma)
        lungo = self.lock_dir / "lungo-worker-slot"
        avvio = os.stat(f"/proc/{vivo.pid}").st_mtime_ns // 1_000_000_000
        lungo.write_text(f"pid={vivo.pid}\navvio={avvio}\njob ancora in corso\n")
        os.utime(lungo, (0, 0))
        result = self.run_started(AES_HOST_MAX_JOBS=1,
                                  AES_STAMP_MAX_AGE_MINUTES=60,
                                  AES_SLOT_WAIT_SECONDS=0)
        self.assertEqual(result.returncode, 1,
                         "un job lungo e' stato scambiato per orfano")
        self.assertTrue(lungo.exists())

    def test_pota_un_segnaposto_il_cui_pid_e_stato_riciclato(self):
        """Il sistema riusa i PID.

        Un PID riassegnato a un altro processo risponde a `kill -0` e terrebbe
        il posto occupato per sempre: l'istante di avvio registrato nel
        segnaposto è ciò che distingue i due processi.
        """
        self.fake_df(100)
        self.lock_dir.mkdir(parents=True)
        altro = subprocess.Popen(["sleep", "30"])

        def _ferma():
            altro.kill()
            altro.wait()

        self.addCleanup(_ferma)
        riciclato = self.lock_dir / "riciclato-worker-slot"
        riciclato.write_text(f"pid={altro.pid}\navvio=1\njob morto\n")
        os.utime(riciclato, (0, 0))
        result = self.run_started(AES_HOST_MAX_JOBS=1,
                                  AES_STAMP_MAX_AGE_MINUTES=60,
                                  AES_SLOT_WAIT_SECONDS=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(riciclato.exists())
        self.assertIn("riciclato", result.stdout)

    def test_non_pota_un_segnaposto_ancora_giovane(self):
        self.fake_df(100)
        self.lock_dir.mkdir(parents=True)
        (self.lock_dir / "recente-worker-slot").write_text("pid=1\njob vivo\n")
        result = self.run_started(AES_HOST_MAX_JOBS=1,
                                  AES_STAMP_MAX_AGE_MINUTES=60,
                                  AES_SLOT_WAIT_SECONDS=0)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no host slot available", result.stderr)

    def test_rimisura_il_disco_dopo_l_attesa(self):
        """Il disco puo' riempirsi mentre si aspetta un posto.

        Chi parte dopo mezz'ora d'attesa con la misura presa all'inizio fa
        passare per verde proprio il caso che il controllo deve fermare.
        """
        self.fake_df(100)
        occupante = self.run_started(AES_HOST_MAX_JOBS=1, GITHUB_RUN_ID="1",
                                     GITHUB_WORKSPACE=self.tmp / "ws-a")
        self.assertEqual(occupante.returncode, 0, occupante.stderr)

        def _riempi_il_disco_e_libera():
            time.sleep(2)
            self.fake_df(2)
            for stamp in self.stamps():
                stamp.unlink()

        scenario = threading.Thread(target=_riempi_il_disco_e_libera)
        scenario.start()
        self.addCleanup(scenario.join)
        result = self.run_started(AES_HOST_MAX_JOBS=1, GITHUB_RUN_ID="2",
                                  GITHUB_WORKSPACE=self.tmp / "ws-b",
                                  AES_MIN_FREE_GB=15,
                                  AES_SLOT_WAIT_SECONDS=60,
                                  AES_SLOT_POLL_SECONDS=1)
        self.assertEqual(result.returncode, 1,
                         "il job e' partito con una misura del disco stantia")
        self.assertIn("is below", result.stderr)

    def test_l_ambiente_del_job_non_puo_alzare_i_limiti(self):
        """Il `.env` del runner sta nella home di chi esegue i job.

        Se i limiti o i percorsi del gate arrivassero da lì, sarebbe il job a
        scegliere quanto essere limitato: bastava spostare la directory dei
        lock per vedere sempre zero job attivi.
        """
        self.fake_df(100)
        primo = self.run_started(GITHUB_WORKSPACE=self.tmp / "ws-a")
        self.assertEqual(primo.returncode, 0, primo.stderr)
        secondo = self.run_started(
            AES_SLOT_WAIT_SECONDS=0,
            GITHUB_WORKSPACE=self.tmp / "ws-b",
            env_extra={"AES_HOST_MAX_JOBS": "8",
                       "AES_LOCK_DIR": str(self.tmp / "lock-mio"),
                       "AES_MIN_FREE_GB": "0"},
        )
        self.assertEqual(secondo.returncode, 1,
                         "il job ha alzato il proprio tetto dall'ambiente")
        self.assertEqual(len(self.stamps()), 1)
        self.assertFalse((self.tmp / "lock-mio").exists(),
                         "il job ha spostato la directory dei lock")

    def test_rifiuta_un_limite_non_numerico(self):
        self.fake_df(100)
        result = self.run_started(AES_HOST_MAX_JOBS="tanti")
        self.assertEqual(result.returncode, 1)
        self.assertIn("AES_HOST_MAX_JOBS is not a number", result.stderr)

    def test_rifiuta_un_limite_vuoto(self):
        """Un valore vuoto è il caso che una validazione concatenata perde.

        Fra due separatori non lascia traccia, passa il controllo, e poi fa
        fallire il confronto numerico: il tetto non viene applicato e ogni
        job parte.
        """
        self.fake_df(100)
        result = self.run_started(AES_HOST_MAX_JOBS="")
        self.assertEqual(result.returncode, 1,
                         "un tetto vuoto ha lasciato partire il job")
        self.assertIn("AES_HOST_MAX_JOBS is not a number", result.stderr)
        self.assertEqual(self.stamps(), [])

    def test_rifiuta_il_job_se_i_segnaposto_non_sono_elencabili(self):
        """`find | wc -l` torna lo stato di `wc`.

        Un `find` che fallisce — permessi, filesystem in errore — darebbe
        zero, e zero significa «nessun job attivo»: il tetto sparirebbe
        proprio quando la macchina ha un problema.
        """
        self.fake_df(100)
        _fake_bin(self.bin, "find", 'exit 1')
        result = self.run_started(AES_HOST_MAX_JOBS=1)
        self.assertEqual(result.returncode, 1)
        self.assertIn("stamps cannot be listed", result.stderr)

    def test_rifiuta_ogni_percorso_non_assoluto(self):
        """Ogni percorso va validato da solo.

        Controllando i due concatenati, basta che il primo sia assoluto
        perché il secondo passi: un `AES_DISK_CHECK_PATH` relativo farebbe
        misurare lo spazio della directory corrente.
        """
        self.fake_df(100)
        solo_lock = self.run_started(AES_LOCK_DIR="lock-relativo")
        self.assertEqual(solo_lock.returncode, 1)
        self.assertIn("LOCK_DIR is not an absolute path", solo_lock.stderr)

        solo_disco = self.run_started(AES_DISK_CHECK_PATH=".")
        self.assertEqual(solo_disco.returncode, 1,
                         "un percorso disco relativo è passato dietro un LOCK_DIR assoluto")
        self.assertIn("DISK_PATH is not an absolute path", solo_disco.stderr)

    def test_fallisce_chiuso_se_il_lock_non_e_acquisibile(self):
        """`set -e` non vale dentro una funzione usata come condizione.

        Senza un controllo esplicito, un `flock` fallito lascerebbe proseguire
        fino a `return 0` e il job partirebbe senza serializzazione.
        """
        self.fake_df(100)
        _fake_bin(self.bin, "flock", 'exit 1')
        result = self.run_started(AES_HOST_MAX_JOBS=1)
        self.assertEqual(result.returncode, 1)
        self.assertIn("gate lock not acquired", result.stderr)
        self.assertEqual(self.stamps(), [])

    def test_due_avvii_simultanei_ne_ammettono_uno_solo(self):
        """Il difetto che questo test tiene chiuso.

        Contare i job attivi e scrivere il proprio segnaposto sono due
        operazioni distinte: senza lock, due runner che partono insieme
        contano entrambi zero e passano entrambi, con il tetto a uno. Il
        test parte da un conteggio a zero proprio per colpire quella
        finestra.
        """
        self.fake_df(100)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda i: self.run_started(
                    AES_HOST_MAX_JOBS=1, GITHUB_RUN_ID=i,
                    GITHUB_WORKSPACE=self.tmp / f"ws-{i}",
                    AES_SLOT_WAIT_SECONDS=0),
                range(8),
            ))
        accepted = [r for r in results if r.returncode == 0]
        self.assertEqual(len(accepted), 1,
                         "il lock non ha serializzato conteggio e scrittura")
        self.assertEqual(len(self.stamps()), 1)


if __name__ == "__main__":
    unittest.main()
