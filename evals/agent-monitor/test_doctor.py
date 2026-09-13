"""Eval della diagnosi di aggancio del monitor.

Il monitor e' fail-open e muto: quando non trova nulla non stampa nulla, e
quando e' rotto stampa anch'esso nulla. I due stati sono indistinguibili
dall'esterno, ed e' esattamente cosi' che l'hook precedente e' rimasto
inerte per giorni dopo che il repo si era spostato.

Questi test fissano il contratto opposto: rotto = parla, sano = tace. Il
caso `test_repo_senza_hook_registrato` e' quello che conta di piu', perche'
descrive un consumer che ha ricevuto i moduli ma non l'aggancio — il modo
di fallire che la propagazione harness introduce.

Esecuzione: `python3 -m pytest evals/agent-monitor`
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MONITOR_DIR = REPO / "harness" / "scripts" / "agent-monitor"
sys.path.insert(0, str(MONITOR_DIR))

import doctor  # noqa: E402
import ledger  # noqa: E402
from detector import make_event  # noqa: E402


def _fake_repo(root: Path, *, modules=True, hook=True) -> Path:
    """Repo sintetico con i soli pezzi che il doctor guarda."""
    monitor = root / "harness" / "scripts" / "agent-monitor"
    monitor.mkdir(parents=True)
    if modules:
        for name in doctor.MODULES:
            (monitor / name).write_text("", encoding="utf-8")
    settings = root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    hooks = {"PreToolUse": []}
    if hook:
        hooks["PostToolUse"] = [
            {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": "bash harness/scripts/agent-monitor/posttooluse.sh",
                    }
                ],
            }
        ]
    settings.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    return root


class TestDoctor(unittest.TestCase):
    def test_repo_sano_non_ha_problemi(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_repo(Path(tmp))
            self.assertEqual(doctor.problems(root), [])

    def test_repo_senza_hook_registrato(self):
        """Moduli presenti, nessun aggancio: installato ma inerte."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_repo(Path(tmp), hook=False)
            found = doctor.problems(root)
            self.assertEqual(len(found), 1)
            self.assertIn("PostToolUse", found[0])
            self.assertIn("aes-sync", found[0])

    def test_repo_senza_moduli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_repo(Path(tmp), modules=False)
            found = doctor.problems(root)
            self.assertTrue(any("missing modules" in problem for problem in found))
            self.assertIn("hook.py", " ".join(found))

    def test_repo_senza_punto_d_ingresso(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_repo(Path(tmp))
            (root / "harness" / "scripts" / "agent-monitor" / "posttooluse.sh").unlink()
            found = doctor.problems(root)
            self.assertTrue(any("posttooluse.sh" in problem for problem in found))

    def test_settings_illeggibile_e_un_problema_non_un_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_repo(Path(tmp))
            (root / ".claude" / "settings.json").write_text("{ rotto", encoding="utf-8")
            self.assertTrue(doctor.problems(root))

    def test_il_repo_reale_e_agganciato(self):
        """Non un mock: e' AES stesso a dover risultare monitorato."""
        self.assertEqual(doctor.problems(REPO), [])

    def test_summary_riporta_il_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_repo(Path(tmp))
            self.assertIn("ledger is still empty", doctor.summary(root))
            ledger.append(root, make_event("s1", "claude", "bash", "ls"))
            summary = doctor.summary(root)
            self.assertIn("1 events", summary)
            self.assertIn(time.strftime("%Y-%m-%d"), summary)


class TestLedgerSiAutoIgnora(unittest.TestCase):
    def test_la_directory_del_ledger_nasce_gitignorata(self):
        """Il ledger non deve comparire come untracked in nessun consumer.

        Senza questo, propagare il monitor a un repo consumer significa
        lasciargli in `git status` un file di stato locale che prima o poi
        qualcuno committa.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger.append(root, make_event("s1", "claude", "bash", "ls"))
            marker = ledger.monitor_dir(root) / ".gitignore"
            self.assertTrue(marker.is_file())
            self.assertEqual(marker.read_text(encoding="utf-8").strip(), "*")

    def test_la_coda_letta_copre_la_ritenzione(self):
        """R4 dice "in N sessioni distinte": vero solo se TAIL == MAX_LEDGER."""
        self.assertEqual(ledger.TAIL, ledger.MAX_LEDGER)

    def test_il_budget_non_cresce_all_infinito(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(ledger.BUDGET_MAX_SESSIONS + 10):
                ledger.record_probe(root, f"sessione-{index}", f"fp-{index}")
            budget = json.loads(
                (ledger.monitor_dir(root) / "probe-budget.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(len(budget), ledger.BUDGET_MAX_SESSIONS)
            self.assertIn(
                f"sessione-{ledger.BUDGET_MAX_SESSIONS + 9}",
                budget,
                "la potatura deve togliere le sessioni piu' vecchie, non le ultime",
            )


if __name__ == "__main__":
    unittest.main()
