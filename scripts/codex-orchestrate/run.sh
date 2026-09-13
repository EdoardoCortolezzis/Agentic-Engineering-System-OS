#!/usr/bin/env bash
# Wrapper sottile attorno a orchestrate.py + `codex exec` (spec 001-codex-orchestrate).
#
# Uso:
#   run.sh <session-id> <task...>            # nuova sessione
#   run.sh resume <session-id> <risposta...> # riprende una sessione awaiting_answer
#   run.sh continue <session-id>              # riprende una sessione in_progress
#
# Nessun testo passato dall'utente/output di Codex viene mai interpolato
# direttamente in una stringa di comando shell: task/risposta/output vengono
# passati come argomenti separati (bash) o serializzati in JSON via
# `jq --arg` prima di raggiungere orchestrate.py / codex.
set -euo pipefail

# Deroga consapevole alla regola model-agnostic di AGENTS.md, autorizzata da
# Edo: il default resta override-abile per limitarne la portata.
DEFAULT_CODEX_MODEL="gpt-5.6-luna"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ORCHESTRATE="$SCRIPT_DIR/orchestrate.py"

usage() {
  echo "Usage:" >&2
  echo "  run.sh <session-id> <task...>" >&2
  echo "  run.sh resume <session-id> <risposta...>" >&2
  echo "  run.sh continue <session-id>" >&2
  exit 1
}

[ "$#" -ge 2 ] || usage

if [ "$1" = "resume" ]; then
  MODE="resume"
  SESSION_ID="$2"
  shift 2
  ANSWER="$*"
  [ -n "$ANSWER" ] || { echo "resume richiede una risposta non vuota: run.sh resume <session-id> <risposta...>" >&2; exit 1; }
elif [ "$1" = "continue" ]; then
  [ "$#" -eq 2 ] || usage
  MODE="continue"
  SESSION_ID="$2"
  shift 2
else
  MODE="new"
  SESSION_ID="$1"
  shift 1
  TASK="$*"
fi

if ! [[ "$SESSION_ID" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "session-id non valido: solo lettere, cifre, '-' e '_'" >&2
  exit 1
fi

SESSION_DIR="$REPO_ROOT/.agent/orchestration/$SESSION_ID"
STATE_FILE="$SESSION_DIR/state.json"
LOG_FILE="$SESSION_DIR/log.txt"

mkdir -p "$SESSION_DIR"

if [ "$MODE" = "new" ]; then
  if [ -f "$STATE_FILE" ]; then
    echo "Sessione $SESSION_ID esiste gia' (state.json presente). Usa 'resume' o un altro session-id." >&2
    exit 1
  fi
  jq -n --arg task "$TASK" \
    '{task: $task, history: [], iteration: 0, status: "in_progress", last_question: null}' \
    > "$STATE_FILE"
  BUILD_INPUT=$(jq -n --slurpfile state "$STATE_FILE" '{state: $state[0]}')
elif [ "$MODE" = "resume" ]; then
  if [ ! -f "$STATE_FILE" ]; then
    echo "Sessione $SESSION_ID non trovata (nessun state.json). Impossibile fare resume." >&2
    exit 1
  fi
  CURRENT_STATUS=$(jq -r '.status' "$STATE_FILE")
  if [ "$CURRENT_STATUS" != "awaiting_answer" ]; then
    echo "Sessione $SESSION_ID non e' in attesa di risposta (status=$CURRENT_STATUS)." >&2
    exit 1
  fi
  BUILD_INPUT=$(jq -n --slurpfile state "$STATE_FILE" --arg answer "$ANSWER" \
    '{state: $state[0], answer: $answer}')
else
  if [ ! -f "$STATE_FILE" ]; then
    echo "Sessione $SESSION_ID non trovata (nessun state.json). Impossibile fare continue." >&2
    exit 1
  fi
  CURRENT_STATUS=$(jq -r '.status' "$STATE_FILE")
  if [ "$CURRENT_STATUS" != "in_progress" ]; then
    echo "Sessione $SESSION_ID non e' in stato in_progress (status=$CURRENT_STATUS)." >&2
    exit 1
  fi
  BUILD_INPUT=$(jq -n --slurpfile state "$STATE_FILE" \
    '{state: $state[0]}')
fi

BUILD_OUTPUT=$(python3 "$ORCHESTRATE" build-prompt <<<"$BUILD_INPUT")
PROMPT=$(jq -r '.prompt' <<<"$BUILD_OUTPUT")
NEW_STATE=$(jq -c '.state' <<<"$BUILD_OUTPUT")
echo "$NEW_STATE" > "$STATE_FILE"

ITERATION=$(jq -r '.iteration' <<<"$NEW_STATE")
{
  echo ""
  echo "=== [$SESSION_ID] iterazione $ITERATION ==="
} >> "$LOG_FILE"

RAW_FILE="$SESSION_DIR/.last_output.txt"
: > "$RAW_FILE"

# CODEX_MODEL si legge dall'ambiente, altrimenti dalla riga corrispondente in
# `.env`. Si estrae la singola chiave invece di fare `source` del file: `.env`
# contiene le API key di tutti i provider, e sorgerlo le esporterebbe
# nell'ambiente del processo Codex senza che nessuno l'abbia chiesto.
if [ -z "${CODEX_MODEL:-}" ] && [ -r "$REPO_ROOT/.env" ]; then
  CODEX_MODEL="$(sed -n 's/^[[:space:]]*CODEX_MODEL[[:space:]]*=[[:space:]]*//p' "$REPO_ROOT/.env" | tail -n 1)"
  CODEX_MODEL="${CODEX_MODEL%\"}"
  CODEX_MODEL="${CODEX_MODEL#\"}"
fi
if [ -z "${CODEX_MODEL:-}" ]; then
  CODEX_MODEL="$DEFAULT_CODEX_MODEL"
fi

# Il sandbox e' esplicito e non ereditato da ~/.codex/config.toml: fuori dai
# path marcati "trusted" Codex parte read-only, e un orchestratore che delega
# un'implementazione ma non puo' scrivere fallisce in modo silenzioso e
# confuso. workspace-write limita comunque la scrittura all'albero di lavoro.
CODEX_ARGS=(exec -s workspace-write)
CODEX_ARGS+=(-m "$CODEX_MODEL")
# Tracciato nel log perche' STATE.md deve poter dire con quale modello e'
# stato scritto un change, e a posteriori non si ricava da nessun'altra parte.
echo "modello: $CODEX_MODEL" >> "$LOG_FILE"

printf '%s' "$PROMPT" | codex "${CODEX_ARGS[@]}" - 2>&1 | tee -a "$LOG_FILE" "$RAW_FILE"

PROCESS_INPUT=$(jq -n --rawfile output "$RAW_FILE" --argjson state "$NEW_STATE" \
  '{state: $state, output: $output}')

# Monitor del ragionamento: registra le azioni del round nel ledger del repo
# e avvisa se l'agente sta ripetendo se stesso. Avvisa e basta, mai blocca, e
# ogni suo errore viene ignorato (|| true): orchestrate.py resta intatto.
MONITOR="$REPO_ROOT/harness/scripts/agent-monitor/codex_round.py"
if [ -f "$MONITOR" ]; then
  python3 "$MONITOR" "$SESSION_ID" "$REPO_ROOT" < "$RAW_FILE" 2>/dev/null \
    | tee -a "$LOG_FILE" || true
fi

rm -f "$RAW_FILE"
PROCESS_OUTPUT=$(python3 "$ORCHESTRATE" process-output <<<"$PROCESS_INPUT")

FINAL_STATE=$(jq -c '.state' <<<"$PROCESS_OUTPUT")
echo "$FINAL_STATE" > "$STATE_FILE"

FINAL_STATUS=$(jq -r '.status' <<<"$FINAL_STATE")
FINAL_ITERATION=$(jq -r '.iteration' <<<"$FINAL_STATE")
FINAL_QUESTION=$(jq -r '.last_question // ""' <<<"$FINAL_STATE")

jq -n --arg status "$FINAL_STATUS" \
  --argjson iteration "$FINAL_ITERATION" \
  --arg question "$FINAL_QUESTION" \
  '{status: $status, iteration: $iteration, question: (if $question == "" then null else $question end)}'
