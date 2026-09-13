---
name: codex-orchestrate
description: >
  Opt-in supervision of a bounded task executed by Codex CLI. Use only when
  the user explicitly asks for delegation and supervision. The marker-based
  protocol pauses on product or architecture questions and never merges code.
---

# Codex orchestration

This helper runs complete `codex exec` rounds and parses `QUESTION:`, `PHASE:`,
and `DONE` markers. It is not the default implementation workflow, a live TUI
driver, or a sandbox.

## Run

```sh
scripts/codex-orchestrate/run.sh <session-id> "<bounded task>"
scripts/codex-orchestrate/run.sh resume <session-id> "<answer>"
```

Session state and logs live under the gitignored
`.agent/orchestration/<session-id>/` directory. Read the final JSON status:
`done`, `awaiting_answer`, or `cap_reached`. Do not resume a capped session
without an explicit decision.

## Questions

Resolve an implementation detail from repository context when it is unambiguous.
Ask the user when the question changes product behavior, architecture, scope,
credentials, or an external side effect. In case of doubt, stop and ask.

## Limits

The wrapper does not sandbox the CLI. Logs can contain sensitive task or model
output if the task includes it; keep credentials out of prompts and inspect
permissions before running. A response without markers remains in progress and
must be diagnosed or reformulated, not blindly relaunched. The helper never
pushes protected branches or merges a pull request.
