"""Eval deterministico del detector di loop (livello 1 del monitor).

Nessuna sessione reale e nessuna chiamata a modelli: gli eventi sono
sintetici e coprono anche un errore ricorrente osservato in un transcript,
perché è il caso che il monitor esiste per intercettare.

Il test piu' importante e' `test_sessione_sana_non_produce_segnali`: un
monitor che avvisa su lavoro legittimo verrebbe ignorato entro un giorno, e
ADR 0011 documenta gia' cosa succede quando un guard-rail mal tarato blocca
lavoro valido.

Esecuzione: `python3 -m unittest evals/agent-monitor/test_detector.py`
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "harness" / "scripts" / "agent-monitor"))

from codex_log import parse_codex_text  # noqa: E402
from detector import (  # noqa: E402
    is_user_rejection,
    PING_PONG_CYCLES,
    REPEAT_ACTION_THRESHOLD,
    REPEAT_ERROR_THRESHOLD,
    detect,
    fingerprint,
    make_event,
    normalize,
)

UNIQUE_ERR = (
    "sqlite3.IntegrityError: UNIQUE constraint failed: articles.urn_base, "
    "articles.article, articles.in_force_from"
)


def ev(payload, session="s1", kind="bash", is_error=False, outcome=""):
    return make_event(
        session, "claude", kind, payload, outcome=outcome, is_error=is_error
    )


def run(events):
    """Rigioca una lista di eventi restituendo il primo segnale prodotto."""
    seen = []
    for event in events:
        seen.append(event)
        signal = detect(seen, event)
        if signal:
            return signal
    return None


class TestNormalizzazione(unittest.TestCase):
    def test_prefisso_macchina_collassa_ma_il_file_resta(self):
        # Del path si azzera solo la parte specifica della macchina: la coda
        # identifica il file, e senza di essa quattro Write su file diversi
        # diventavano un'unica azione ripetuta.
        self.assertEqual(
            normalize("pytest /srv/workspace/proj/x.py"),
            normalize("pytest /srv/ci/proj/x.py"),
        )
        self.assertNotEqual(
            normalize("pytest /srv/workspace/proj/x.py"),
            normalize("pytest /srv/workspace/proj/y.py"),
        )

    def test_write_su_file_diversi_non_sono_la_stessa_azione(self):
        # Regressione del falso positivo dominante trovato col replay sui
        # transcript reali (114 segnali su 5279 eventi, quasi tutti qui).
        events = [
            ev(f"Write /srv/workspace/src/mod_{n}.py",
               outcome="File created successfully")
            for n in ("urn", "client", "parser", "store", "index")
        ]
        self.assertIsNone(run(events))

    def test_numeri_preservati_nelle_azioni(self):
        # Nelle azioni il numero e' spesso il soggetto del lavoro: se
        # collassasse, venti commit distinti sembrerebbero un loop.
        self.assertNotEqual(
            fingerprint("bash", "git commit -m 'step 3'"),
            fingerprint("bash", "git commit -m 'step 4'"),
        )

    def test_timestamp_collassano(self):
        a = normalize("ingest avviato 2026-08-30 10:03:11")
        b = normalize("ingest avviato 2026-09-01 22:59:04")
        self.assertEqual(a, b)

    def test_azioni_diverse_restano_diverse(self):
        self.assertNotEqual(
            fingerprint("bash", "pytest evals/corpus"),
            fingerprint("bash", "ruff check src"),
        )

    def test_recurring_error_stable_across_years(self):
        # Lo stesso vincolo violato su anni diversi deve avere una firma sola,
        # altrimenti R4 non vedrebbe mai il difetto ricorrente.
        f1942 = fingerprint("error", f"1942: {UNIQUE_ERR}")
        f1943 = fingerprint("error", f"1943: {UNIQUE_ERR}")
        self.assertEqual(f1942, f1943)


class TestRegole(unittest.TestCase):
    def test_r1_stessa_azione_stesso_esito(self):
        events = [
            ev("pytest evals/corpus", outcome="1 failed, 3 passed")
        ] * REPEAT_ACTION_THRESHOLD
        signal = run(events)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.rule, "repeat_action")

    def test_r2_stesso_errore_ripetuto_in_sessione(self):
        signal = run([ev(UNIQUE_ERR, is_error=True)] * REPEAT_ERROR_THRESHOLD)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.rule, "repeat_error")

    def test_r3_alternanza_tra_due_azioni(self):
        events = []
        for _ in range(PING_PONG_CYCLES):
            events.append(ev("edit config a"))
            events.append(ev("edit config b"))
        signal = run(events)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.rule, "ping_pong")

    def test_r4_stesso_difetto_in_sessioni_distinte(self):
        # Il pattern corpus-t2 -> t3 -> t4: un errore per sessione, mai
        # abbastanza per far scattare R2, ma la diagnosi resta sbagliata.
        events = [
            ev(UNIQUE_ERR, session="corpus-t2", is_error=True),
            ev(UNIQUE_ERR, session="corpus-t3", is_error=True),
            ev(UNIQUE_ERR, session="corpus-t4", is_error=True),
        ]
        signal = run(events)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.rule, "recurring_defect")
        self.assertEqual(signal.count, 3)

    def test_r4_ha_priorita_su_r2(self):
        # Se un difetto e' sia ripetuto in sessione sia ricorrente tra
        # sessioni, il segnale utile e' il secondo: dice che la causa non e'
        # stata capita, non solo che il comando e' stato rilanciato.
        events = [
            ev(UNIQUE_ERR, session="corpus-t2", is_error=True),
            ev(UNIQUE_ERR, session="corpus-t3", is_error=True),
        ]
        events += [ev(UNIQUE_ERR, session="corpus-t4", is_error=True)] * 3
        signal = run(events)
        self.assertEqual(signal.rule, "recurring_defect")


class TestFalsiPositivi(unittest.TestCase):
    def test_sessione_sana_non_produce_segnali(self):
        events = [ev(f"git commit -m 'step {i}'") for i in range(20)]
        self.assertIsNone(run(events))

    def test_stessa_azione_con_esiti_diversi_e_progresso(self):
        # Rilanciare i test mentre il numero di fallimenti cala non e' un
        # loop: e' esattamente il lavoro che il monitor non deve disturbare.
        events = [
            ev("pytest evals/corpus", outcome=f"{n} failed")
            for n in (9, 6, 3, 1, 0)
        ]
        self.assertIsNone(run(events))

    def test_sotto_soglia_non_segnala(self):
        events = [ev("pytest evals/corpus", outcome="1 failed")] * (
            REPEAT_ACTION_THRESHOLD - 1
        )
        self.assertIsNone(run(events))

    def test_errore_singolo_in_molte_sessioni_diverse_non_segnala(self):
        # Errori diversi in sessioni diverse sono lavoro normale.
        events = [
            ev(f"errore numero {i} tipo Foo{i}Error", session=f"s{i}", is_error=True)
            for i in range(6)
        ]
        self.assertIsNone(run(events))

    def test_rifiuto_utente_non_e_un_difetto(self):
        # Era la voce piu' frequente fra i difetti "ricorrenti" nel replay.
        self.assertTrue(
            is_user_rejection(
                "The user doesn't want to proceed with this tool use. "
                "The tool use was rejected"
            )
        )
        self.assertFalse(is_user_rejection("sqlite3.IntegrityError: UNIQUE"))

    def test_segnale_agganciato_all_evento_corrente(self):
        # Dopo un loop chiuso, un'azione nuova non deve riemettere il segnale
        # solo perche' la finestra contiene ancora le ripetizioni.
        events = [
            ev("pytest evals/corpus", outcome="1 failed")
        ] * REPEAT_ACTION_THRESHOLD
        nuovo = ev("ruff check src")
        events.append(nuovo)
        self.assertIsNone(detect(events, nuovo))


class TestParserCodex(unittest.TestCase):
    def test_output_non_sconfina_nel_blocco_successivo(self):
        # Regressione: senza confini di blocco l'ultimo comando catturava
        # anche i marker del round, e due errori identici finivano con firme
        # diverse — cioe' la ripetizione diventava invisibile.
        blocco = (
            "exec\n"
            "/bin/zsh -lc 'pytest -q' in /repo\n"
            " exited 1 in 2515ms:\n"
            "sqlite3.IntegrityError: UNIQUE constraint failed\n"
        )
        text = "PHASE: analisi\n" + blocco * 3 + "DONE\n"
        events = parse_codex_text(text, "codex-test")
        errors = [e for e in events if e.is_error]
        self.assertEqual(len(errors), 3)
        self.assertEqual(len({e.fingerprint for e in errors}), 1)


if __name__ == "__main__":
    unittest.main()
