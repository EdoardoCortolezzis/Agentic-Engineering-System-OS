"""Eval dello sblocco del portachiavi.

Lo script maneggia la password che protegge le credenziali dei provider: i
rami che contano sono il rifiuto di una password vuota — con cui il file
cifrato, leggibile dal processo del modello, sarebbe apribile da chiunque — e i
due modi in cui lo sblocco può fallire restando silenzioso.

`runuser`, `busctl` e `grep` non vengono eseguiti davvero: sono sostituiti su
PATH, e il socket del bus è un vero socket Unix creato nella directory del
test. Lo script opera quindi sull'account corrente, senza root e senza toccare
il portachiavi della macchina.

Esecuzione: `python3 -m unittest evals/ops-hetzner/test_keyring_unlock.py`
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hooks_harness import REPO, _fake_bin

UNLOCK = REPO / "ops" / "hetzner" / "aes-keyring-unlock"
HA_RUNUSER_SOSTITUIBILE = os.name == "posix"


@unittest.skipUnless(HA_RUNUSER_SOSTITUIBILE, "serve una shell POSIX")
class TestSbloccoPortachiavi(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aes-keyring-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.runtime = self.tmp / "runtime"
        self.runtime.mkdir()
        # Un socket vero: lo script verifica `-S`, non l'esistenza del file.
        self.bus = socket.socket(socket.AF_UNIX)
        self.bus.bind(str(self.runtime / "bus"))
        self.addCleanup(self.bus.close)
        self.tracce = self.tmp / "invocazioni"
        # `runuser` finto: registra l'invocazione e poi esegue davvero cio' che
        # segue `--`, così il resto della catena (env, daemon, busctl) resta
        # quello vero dello script.
        _fake_bin(self.bin, "runuser",
                  f'echo "$@" >> "{self.tracce}"\n'
                  'while [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"')
        _fake_bin(self.bin, "gnome-keyring-daemon", 'cat > /dev/null; exit 0')
        _fake_bin(self.bin, "busctl", 'echo org.freedesktop.secrets')

    def esegui(self, password: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["AES_KEYRING_USER"] = os.environ.get("USER") or os.environ.get("LOGNAME") or "root"
        env["AES_KEYRING_RUNTIME_DIR"] = str(self.runtime)
        env["AES_KEYRING_HOME"] = str(self.tmp)
        return subprocess.run(
            ["bash", str(UNLOCK)],
            input=password, capture_output=True, text=True, env=env,
        )

    def test_rifiuta_una_password_vuota_senza_avviare_il_daemon(self):
        """Una password vuota non protegge il file cifrato del portachiavi."""
        result = self.esegui("\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("empty password", result.stderr)
        self.assertFalse(self.tracce.exists(),
                         "il daemon è stato invocato con una password vuota")

    def test_segnala_il_daemon_che_non_parte(self):
        """`set -e` farebbe uscire lo script muto: password errata e daemon
        assente sarebbero indistinguibili da un successo silenzioso."""
        _fake_bin(self.bin, "gnome-keyring-daemon", 'cat > /dev/null; exit 1')
        result = self.esegui("segreta\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("keyring daemon did not start", result.stderr)

    def test_segnala_il_servizio_non_acquisito(self):
        _fake_bin(self.bin, "busctl", 'echo org.freedesktop.qualcosaltro')
        result = self.esegui("segreta\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Secret Service is not active", result.stderr)

    def test_conferma_lo_sblocco_riuscito(self):
        result = self.esegui("segreta\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Keyring unlocked", result.stdout)
        self.assertNotIn("segreta", result.stdout + result.stderr)

    def test_non_sblocca_senza_sessione_utente(self):
        self.bus.close()
        (self.runtime / "bus").unlink()
        result = self.esegui("segreta\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("user session is absent", result.stderr)


if __name__ == "__main__":
    unittest.main()
