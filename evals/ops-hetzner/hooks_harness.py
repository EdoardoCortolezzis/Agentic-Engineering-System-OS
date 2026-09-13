"""Impalcatura condivisa degli eval degli hook di job.

Gli hook girano in directory temporanee, con `df` — e dove serve `git` —
sostituiti da binari finti su PATH, così il risultato non dipende dallo spazio
libero della macchina. La configurazione non passa mai dall'ambiente: gli hook
la leggono da un file, esattamente come sul server, e i test lo passano con
`--config`.

Gli hook usano `flock` e `df --output`: sono strumenti GNU, presenti sul
server e sui runner CI. Dove mancano — tipicamente su macOS — i test si
saltano invece di fingere di passare, tranne in CI dove uno skip sarebbe un
controllo spento in silenzio.
"""

import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
JOB_STARTED = REPO / "ops" / "hetzner" / "job-started.sh"
JOB_COMPLETED = REPO / "ops" / "hetzner" / "job-completed.sh"

HAS_GNU_TOOLS = shutil.which("flock") is not None and subprocess.run(
    ["df", "-BG", "--output=avail", "."],
    capture_output=True,
).returncode == 0

if os.environ.get("CI") and not HAS_GNU_TOOLS:
    # Uno skip in CI e' un controllo che si spegne da solo senza far rosso
    # nulla: esattamente la forma vietata da ADR 0010. Su una macchina di
    # sviluppo saltare e' onesto; in CI e' un falso verde.
    raise RuntimeError(
        "in CI gli eval degli hook devono girare: flock e df GNU non sono disponibili"
    )


def _fake_bin(directory: Path, name: str, body: str) -> None:
    """Crea un eseguibile finto in una directory da anteporre a PATH."""
    path = directory / name
    path.write_text("#!/usr/bin/env bash\n" + body + "\n")
    path.chmod(0o755)


class HookTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aes-hooks-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lock_dir = self.tmp / "locks"
        self.archive = self.tmp / "archive"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.free_gb = 100
        self._config_n = 0

    def env(self, **extra) -> dict:
        """Ambiente del job: contiene solo cio' che GitHub Actions imposta.

        I parametri del gate non stanno qui di proposito — l'hook non li
        legge dall'ambiente, perche' il `.env` da cui arrivano e' scrivibile
        dall'account che esegue i job.
        """
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def config(self, **valori) -> str:
        """Scrive il file di configurazione e ne torna il percorso.

        Sul server e' `/etc/aes/limits.conf`, di proprieta' di root; qui e' un
        file temporaneo passato con `--config`, che solo chi invoca l'hook
        puo' impostare.
        """
        base = {
            "AES_LOCK_DIR": self.lock_dir,
            "AES_ARCHIVE_DIR": self.archive,
            "AES_DISK_CHECK_PATH": self.tmp,
            "AES_WORKTREE_ROOTS": self.tmp / "worktrees",
        }
        base.update(valori)
        # Un file per chiamata: piu' avvii in parallelo scriverebbero lo stesso
        # percorso e leggerebbero una configurazione troncata a meta'.
        self._config_n += 1
        percorso = self.tmp / f"limits-{self._config_n}.conf"
        percorso.write_text(
            "".join(f"{k}={v}\n" for k, v in base.items())
        )
        return str(percorso)

    def fake_df(self, free_gb: int) -> None:
        _fake_bin(self.bin, "df", f'echo "Avail"; echo "{free_gb}G"')

    def _dividi(self, extra: dict) -> tuple:
        """Separa cio' che va nel file di configurazione da cio' che va
        nell'ambiente del job: i due canali hanno fiducia diversa."""
        di_config, di_ambiente = {}, {}
        for chiave, valore in extra.items():
            if chiave.startswith("AES_"):
                di_config[chiave] = valore
            else:
                di_ambiente[chiave] = valore
        return di_config, di_ambiente

    def run_started(self, config_path=None, env_extra=None,
                    **extra) -> subprocess.CompletedProcess:
        di_config, di_ambiente = self._dividi(extra)
        di_ambiente.update(env_extra or {})
        percorso = config_path or self.config(**di_config)
        return subprocess.run(
            ["bash", str(JOB_STARTED), "--config", percorso],
            capture_output=True, text=True, env=self.env(**di_ambiente),
        )

    def run_completed(self, config_path=None, **extra) -> subprocess.CompletedProcess:
        di_config, di_ambiente = self._dividi(extra)
        percorso = config_path or self.config(**di_config)
        return subprocess.run(
            ["bash", str(JOB_COMPLETED), "--config", percorso],
            capture_output=True, text=True, env=self.env(**di_ambiente),
        )

    def stamps(self) -> list:
        if not self.lock_dir.exists():
            return []
        return [p for p in self.lock_dir.iterdir() if not p.name.startswith(".")]
