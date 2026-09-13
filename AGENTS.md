# Agentic Engineering System

This repository is a harness, not an agent. Configuration, policy, skills,
specifications, and evals are versioned so the runtime and model provider can
be replaced without losing the engineering contract.

## Before working

1. Run `harness/scripts/git-orient.sh` and do not start from a `STALE` or
   `DEAD` checkout.
2. Read `STATE.md` for the current state and `ROADMAP.md` for the next step.
3. Read the relevant `.agent/skills/<name>/SKILL.md` before using a skill.

## Non-negotiable rules

- Keep modules small and readable. Avoid speculative abstractions.
- Keep public documentation, user-facing comments and messages, templates,
  and workflow prompts in English. Keep source identifiers in English too.
- Keep provider-specific behavior in configuration. Application logic must be
  model-agnostic.
- Stop and record an explicit human-in-the-loop dependency in `STATE.md` when
  an account, credential, approval, captcha, or product decision is required.
- If implementation deviates from `ROADMAP.md`, record the reason in an ADR in
  `docs/decisions/` before proceeding.
- Every feature starts with a deterministic or trajectory eval under `evals/`.
  Use `specs/TEMPLATE.md`, including its Given/When/Then scenarios.
- Update documentation in the same change whenever behavior makes it stale.
  Follow `policies/documentation.md`.
- Use the Git workflow in `policies/git-workflow.md`: work from `develop` in a
  short-lived `feature/`, `fix/`, or `hotfix/` branch, never push directly to
  protected branches. Merge is performed by a human.
- Task execution is issue-driven. The `aes:*` label is status, the contract is
  in the issue body, and an atomic Git ref is the claim lock. Never delete or
  replace another actor's claim.
- Do not add provider gateways, A2A, cryptographic identities, AgBOM, or new
  multi-agent frameworks without an explicit ADR and an eval.

## Orchestration

The supervisor decomposes work, supplies context, and verifies acceptance
criteria. Delegated executors receive bounded tasks and return results; they do
not make product or architecture decisions. Run the declared local review and
the relevant tests yourself. Use `harness/scripts/wait-checks.sh` for one
bounded CI wait rather than polling repeatedly.

## Repository map

- `specs/`: contracts and design scenarios.
- `evals/`: deterministic and trajectory evaluations.
- `policies/`: guardrails and operating rules.
- `.agent/skills/`: portable operational knowledge.
- `docs/decisions/`: architecture decision records.
- `harness/`: scripts and configuration copied to consumers.
- `ops/`: optional experimental operator examples, not consumer assets.
