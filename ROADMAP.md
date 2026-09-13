# Roadmap

This roadmap describes the incremental development of the Agentic Engineering
System. `STATE.md` records measured progress; this file records intended
direction and acceptance criteria.

## v0.1.0 — public baseline

The public baseline includes the six-layer contract: specifications and evals,
portable skills, provider-neutral reach, opt-in orchestration, fail-closed
guardrails, and deterministic verification. It also includes Git session
hygiene, consumer synchronization, issue task contracts, local monitoring, CI
checks, and documented experimental queue configuration.

Acceptance is executable: the OSS release contract, Git hygiene, Git workflow,
harness synchronization, and focused runtime evals pass on a clean checkout.

## Next increments

### Contracts

Write an eval before every new feature. Keep specifications concise and include
Given/When/Then scenarios that exercise the failure path as well as the happy
path. Add trajectory evaluation only when output-only assertions cannot capture
the safety property.

### Know-how

Give each portable skill an authority tier—read-only, draft-only, or
action-allowed—and maintain positive and negative trigger examples. Review
third-party skills before adoption.

### External reach

Keep one least-privilege integration per project, read-only by default, with
credentials supplied only through environment or CI secret stores. Re-run the
MCP safety scan whenever integration configuration changes.

### Orchestration

Keep the default workflow monolithic and explicit. Use the marker-based
orchestration helper only where bounded parallel work has a measurable benefit;
do not introduce another agent framework without an ADR and eval.

### Guardrails

Continue evaluating coverage for file edits, hooks, MCP calls, and generated
commands. Add checkpointing or a circuit breaker only when a concrete failure
mode and recovery rule are specified.

### Verification and observability

Prefer deterministic tests and local review. If telemetry is enabled, preserve
the default of no prompt, response, or tool-content export. Add budget caps and
error-focused sampling before increasing remote execution.

## Experimental work

Remote workers and self-hosted queues remain experimental. They require an
operator-owned host, explicit data-flow review, credential isolation, resource
limits, and a recovery design for interrupted work. The local harness remains
the supported baseline until those criteria are independently demonstrated.

## Human decisions

Product scope, provider accounts, CI permissions, privacy policy, and merge
approval are human-in-the-loop decisions. Record blockers in `STATE.md` instead
of guessing or silently changing the plan.
