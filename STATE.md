# State

Last updated: 2026-09-13

## CI assertion migration and README diagrams

The public release translated the Hetzner operator scripts and keyring helper
to English, but the Linux GitHub Actions suite still asserted their former
Italian messages. The resulting CI failures were assertion-contract drift, not
runtime failures: the scripts emitted the intended English fail-closed errors.
The affected deterministic evals now assert the English messages.

The README also includes maintained diagrams for the agentic workflow,
high-level harness architecture, and the explicitly experimental remote
execution path. The remote diagram does not change the supported local
baseline or remove human approval requirements.

## Current release work

The `v0.1.0` OSS release is being prepared on a short-lived feature branch.
Public documentation is being consolidated in English, private consumer plans
and machine metadata are excluded, and the MIT license plus a safe environment
template are present. Runtime behavior and claim semantics remain unchanged.

## Implemented harness capabilities

- Git orientation, feature worktrees, protected-branch push checks, and safe
  branch completion are implemented under `harness/scripts/`.
- `aes-sync.sh` propagates the manifest's copy, seed, and merge-json assets;
  `--check` detects local drift and `--check-upstream` separately checks source
  freshness.
- Issue task contracts use explicit `aes:*` status labels and atomic Git refs
  for claims. Readiness, authorization, dispatch, quota, and human escalation
  are fail-closed.
- The optional agent monitor stores inspectable operational metadata locally.
  It does not export prompt, response, or tool content by default.
- CI workflows and deterministic eval suites cover these contracts and the
  release surface.

## Verification for this release

Required before release completion:

```text
python3 -m pytest -q evals/oss-release-contract
python3 -m pytest -q evals/git-hygiene evals/git-workflow evals/harness-sync
git diff --check
```

The release task is complete only when all commands pass and a final audit
finds no private paths, credentials, consumer-specific plans, or machine
metadata in tracked distribution files. Focused tests for any touched runtime
file must also pass.

## Known limits and human decisions

`aes-sync --check` detects local tampering, not whether a consumer has fetched
the newest source revision. Worktrees isolate tracked code but not shared data
or external services. Remote workers and the queue are experimental and need
operator-managed credentials, resource limits, data-flow review, and recovery
behavior. Human review remains required for product decisions, credentials,
protected-branch merges, and any ambiguous authorization.

No human action is currently required to complete the documentation and
deterministic release checks in this branch. Account setup, CI permissions, and
remote deployment are intentionally outside this release change.
