# Autonomous task queue policy

The queue is an opt-in dispatcher for GitHub Issue contracts. It may prepare
and open pull requests, but it does not modify secrets or protected branches,
perform external publication, or merge code. It must stop with
`aes:needs-human` when a human decision or authorization is required.

## Explicit activation

The minimum configuration is:

```text
AES_QUEUE_ENABLED=false
AES_QUEUE_MAX_CONCURRENCY=1
AES_QUEUE_MAX_CONCURRENCY_PER_REPO=1
AES_QUEUE_BUDGET=0
```

Missing, unknown, or malformed values disable dispatch. `AES_QUEUE_BUDGET` is
an abstract per-cycle cap, not a provider or model quota. Global and per-repo
concurrency caps are independent; the effective limit is the lower value. The
dispatcher requires an explicit `AES_QUEUE_ALLOWED_AUTHORS` allow-list and the
worker requires `AES_TRUSTED_ACTOR`, verified with `gh api user`; it never
substitutes the event author for the credential identity.

## Plan and claim

The controller produces a provider-neutral JSON plan before workers start. The
plan has unique task IDs, non-empty prompts, safe relative paths, acyclic
dependencies, and an explicit `reasoning_effort` of `high` or `xhigh` for each
worker. The configured routes are controller `gpt-5.6-sol` with fallback
`claude-opus-5`, and worker `gpt-5.6-luna` with fallback `claude-sonnet-5`.
Claude effort is mapped explicitly to the supported CLI effort. Provider quota
may suspend a route but cannot alter the plan.

The durable claim is an atomic remote Git ref. A label is status, not a lock;
the local ledger is only an audit marker. A lost claim is abandoned, never
deleted or forcibly replaced.

## Quota and credentials

Quota state is an atomic JSON cache outside the checkout, configured with
`AES_QUOTA_CACHE_ROOT`, `AES_QUOTA_CACHE_PATH`, and
`AES_QUOTA_CACHE_TTL_SECONDS`. Missing, stale, unsafe, or in-checkout paths are
rejected. The Codex route requires an explicitly managed credential directory;
the Claude fallback is optional and is attempted only after Codex quota
exhaustion is confirmed. During 08:00–17:00 Europe/Berlin on weekdays, a
missing/stale Claude observation or usage at least 70% blocks the fallback.
Provider refresh failures persist `aes:waiting-provider` and retry the same
execution rather than using stale data.

## State transitions

| From | To | Condition |
|---|---|---|
| draft | `aes:ready` | Contract and dispatch metadata are valid. |
| `aes:ready` | `aes:needs-human` | Metadata is invalid or incomplete. |
| `aes:ready` | `aes:claimed` | Budget and atomic claim ref are secured. |
| `aes:claimed` | `aes:waiting-provider` | Provider or quota is unavailable. |
| `aes:claimed` | `aes:blocked` | A reproducible technical blocker exists. |
| `aes:claimed` | `aes:needs-human` | Product, permission, or policy input is needed. |
| `aes:claimed` | `aes:pr-open` | Checks pass and a linked pull request is open. |
| `aes:pr-open` | closed | A human reviews and merges explicitly. |

The dispatcher must not skip a gate or clear `aes:blocked` or
`aes:needs-human` merely to continue.

## Fail-closed boundaries

Stop without executing code when the contract or dispatch block is missing,
duplicated, unknown, or malformed; labels are invalid; dependencies are open
or cyclic; `not_before` is in the future; budget, lease, credentials, branch,
worktree, or source version cannot be verified; or a provider response is
ambiguous. Record a reason and notify the configured product manager. A
notification is best-effort and cannot change state or approve a merge.

Workers are restricted to the repository and paths declared by the issue.
Trusted publication uses validated arguments only after revalidating the claim
and contract. The worker does not merge code, and it emits `aes:needs-human`
with a precise question when an operation falls outside its contract.

## Audit

Record repository, issue, execution ID, claim ref, skip/claim reason, budget,
state transition, and pull-request URL. Logs must not contain credentials or
raw payloads. Queue execution is subject to operator resource limits and
external service availability; remote execution remains experimental.
