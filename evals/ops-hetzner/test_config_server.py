"""Eval della configurazione del server: i file e gli hook devono concordare.

Esecuzione: `python3 -m unittest evals/ops-hetzner/test_config_server.py`
"""

import unittest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hooks_harness import JOB_STARTED, REPO


class TestConfigurazioneServer(unittest.TestCase):
    """La configurazione del server e gli hook devono parlare della stessa
    directory: se `aes.conf` creasse un percorso diverso dal default degli
    hook, il tetto di concorrenza conterebbe in una directory vuota."""

    def test_la_directory_dei_lock_coincide_con_quella_creata_a_boot(self):
        tmpfiles = (REPO / "ops" / "hetzner" / "aes.conf").read_text()
        default = None
        for riga in JOB_STARTED.read_text().splitlines():
            if riga.startswith("LOCK_DIR="):
                default = riga.split("=", 1)[1].strip().strip('"')
                break
        self.assertIsNotNone(default, "LOCK_DIR non trovato in job-started.sh")
        creati = [r.split()[1] for r in tmpfiles.splitlines()
                  if r.startswith("d ") and len(r.split()) > 1]
        self.assertIn(default, creati,
                      f"{default} non è creata da aes.conf: {creati}")


if __name__ == "__main__":
    unittest.main()
