"""Logica deterministica del protocollo di orchestrazione Codex (spec 001).

Nessuna chiamata a `codex exec` qui dentro: questo modulo prende in input
task/history/output grezzo e decide prompt e transizioni di stato in modo
puro, cosi da essere testabile senza invocare il CLI reale (vedi
evals/codex-orchestrate/test_orchestrate.py). `run.sh` e' l'unico posto che
invoca `codex exec` davvero e usa la CLI qui sotto (`build-prompt`,
`process-output`) per non dover fare parsing di testo libero in bash.

Protocollo a turni via marker (vedi ADR in docs/decisions/): Codex stampa
`QUESTION: ...` quando e' bloccato su un'ambiguita', `PHASE: ...` per ogni
fase completata, `DONE` quando il task e' concluso. Nessun driver TUI live.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field, replace

MAX_ITER = 8

PROTOCOL_HEADER = """Stai eseguendo un task in modalita' orchestrata da un altro agente (Claude).
Segui questo protocollo di output, una riga per marker:

- `PHASE: <descrizione>` per ogni fase significativa completata.
- `QUESTION: <domanda>` se sei bloccato su un'ambiguita' che non puoi
  risolvere da solo (decisione di prodotto/architettura o dettaglio
  implementativo mancante). Dopo aver stampato QUESTION, fermati: non
  indovinare, non procedere oltre finche' non ricevi una risposta.
- `DONE` (da sola su una riga) quando il task e' completato.

Non inventare marker diversi da questi tre."""


@dataclass(frozen=True)
class ParsedOutput:
    question: str | None
    phases: list[str]
    done: bool


def parse_markers(output: str) -> ParsedOutput:
    """Estrae question/phases/done dalle righe marker dell'output di Codex.

    Testo libero senza marker riconosciuti non produce ne' domanda ne' DONE
    (scenario 5): non si inventa uno stato da un output ambiguo.
    """
    question: str | None = None
    phases: list[str] = []
    done = False
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("QUESTION:"):
            question = stripped[len("QUESTION:"):].strip()
        elif stripped.startswith("PHASE:"):
            phases.append(stripped[len("PHASE:"):].strip())
        elif stripped == "DONE":
            done = True
    return ParsedOutput(question=question, phases=phases, done=done)


def build_prompt(task: str, history: list[tuple[str, str]]) -> str:
    """Antepone il protocollo fisso al task e appende la history Q/A risolta."""
    parts = [PROTOCOL_HEADER, "", f"Task: {task}"]
    if history:
        parts.append("")
        parts.append("Domande gia' risolte in questa sessione:")
        for question, answer in history:
            parts.append(f"Q: {question}")
            parts.append(f"A: {answer}")
    return "\n".join(parts)


@dataclass
class SessionState:
    task: str
    history: list[tuple[str, str]] = field(default_factory=list)
    iteration: int = 0
    status: str = "in_progress"
    last_question: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "task": self.task,
                "history": [[q, a] for q, a in self.history],
                "iteration": self.iteration,
                "status": self.status,
                "last_question": self.last_question,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> "SessionState":
        data = json.loads(raw)
        return cls(
            task=data["task"],
            history=[tuple(pair) for pair in data.get("history", [])],
            iteration=data.get("iteration", 0),
            status=data.get("status", "in_progress"),
            last_question=data.get("last_question"),
        )


def process_output(state: SessionState, raw_output: str) -> SessionState:
    """Aggiorna lo stato dopo un round di `codex exec` (conta come un'iterazione)."""
    parsed = parse_markers(raw_output)
    if parsed.question:
        status = "awaiting_answer"
        last_question = parsed.question
    elif parsed.done:
        status = "done"
        last_question = None
    else:
        status = "in_progress"
        last_question = None
    return replace(
        state,
        iteration=state.iteration + 1,
        status=status,
        last_question=last_question,
    )


def resume(state: SessionState, answer: str) -> SessionState:
    """Applica la risposta dell'utente/Claude a una domanda bloccante.

    Appende (domanda, risposta) alla history e riporta lo stato a
    `in_progress`, pronto per un nuovo `build_prompt`. L'iterazione aumenta
    di uno una volta completato il round successivo (`process_output`).
    """
    new_history = list(state.history) + [(state.last_question, answer)]
    return replace(
        state,
        history=new_history,
        status="in_progress",
        last_question=None,
    )


def next_action(state: SessionState) -> str:
    """Decide il prossimo passo senza mutare lo stato.

    Valori: `done`, `await_answer`, `cap_reached`, `run_codex`.
    """
    if state.status == "done":
        return "done"
    if state.status == "cap_reached":
        return "cap_reached"
    if state.iteration >= MAX_ITER:
        return "cap_reached"
    if state.status == "awaiting_answer":
        return "await_answer"
    return "run_codex"


def _cli_build_prompt(payload: dict) -> dict:
    state = SessionState.from_json(json.dumps(payload["state"]))
    answer = payload.get("answer")
    if answer is not None:
        state = resume(state, answer)
    prompt = build_prompt(state.task, state.history)
    return {"prompt": prompt, "state": json.loads(state.to_json())}


def _cli_process_output(payload: dict) -> dict:
    state = SessionState.from_json(json.dumps(payload["state"]))
    state = process_output(state, payload["output"])
    action = next_action(state)
    if action == "cap_reached":
        state = replace(state, status="cap_reached")
    return {"state": json.loads(state.to_json()), "action": action}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("build-prompt", "process-output"):
        print("usage: orchestrate.py {build-prompt|process-output} < input.json", file=sys.stderr)
        return 2
    payload = json.loads(sys.stdin.read())
    if argv[1] == "build-prompt":
        result = _cli_build_prompt(payload)
    else:
        result = _cli_process_output(payload)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
