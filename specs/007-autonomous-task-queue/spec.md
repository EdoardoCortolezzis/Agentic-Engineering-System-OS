# Specification: autonomous issue queue

## Goal

Dispatch bounded GitHub Issue work from a repo-local queue while preserving
human accountability, explicit budget and concurrency limits, and fail-closed
provider handling.

## Contract

An issue is eligible only when it has `aes:ready`, valid `aes:contract` and
`aes:dispatch` blocks, approved `role:*` and `autonomy:*` labels, closed
dependencies, and a usable budget. The dispatcher creates one atomic claim ref
before starting a worker. A local ledger is an audit artifact, not ownership.

The controller emits a provider-neutral plan with safe relative paths, unique
IDs, acyclic dependencies, explicit effort, and no hidden defaults. Global and
per-repository concurrency caps are independent. `auto-merge` is never a worker
capability: a pull request reaches `aes:pr-open` and waits for a human.

## BDD scenarios

Scenario: a valid task is selected
Given a repo-local issue is open with `aes:ready` and valid dispatch metadata
When the dispatcher scans the queue
Then it selects the issue only when budget and concurrency are available.

Scenario: invalid metadata is stopped
Given an `aes:ready` issue has malformed dispatch metadata
When the dispatcher validates it
Then the issue transitions to `aes:needs-human` with a reason.

Scenario: concurrency is enforced
Given the global or per-repository concurrency cap is reached
When the dispatcher scans another eligible issue
Then it leaves that issue unclaimed.

Scenario: a future task remains queued
Given `not_before` is in the future
When the dispatcher scans the issue
Then it does not claim or execute it.

Scenario: cross-repository dependencies are checked
Given an issue depends on an open issue in another repository
When readiness is evaluated
Then the dependent issue is not dispatchable.

Scenario: a provider is unavailable
Given the quota refresh fails or provider state is stale
When a worker starts
Then it records `aes:waiting-provider` and does not use stale credentials.

Scenario: a worker needs human input
Given a task requires a product decision or permission
When the worker reaches that boundary
Then it records `aes:needs-human` and stops.

Scenario: a pull request is ready for review
Given a worker completes its scoped change and checks pass
When it opens a linked pull request
Then the issue becomes `aes:pr-open` and no auto-merge occurs.

## Limits

Quota state is stored atomically outside the checkout. Notifications are
best-effort and deduplicated by execution ID, but cannot change state. Remote
workers and self-hosted queues are experimental; operators own their
credentials, network boundary, data retention, and resource caps.
