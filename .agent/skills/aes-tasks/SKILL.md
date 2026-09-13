---
name: aes-tasks
description: >
  Work on AES tasks tracked as GitHub Issues: inspect the backlog, claim one
  atomically, implement it, open a pull request, and report blockers. This
  skill also documents the planner, dispatcher, and worker queue protocol.
  Do not use it for untracked ad-hoc work.
---

# Issue task workflow

GitHub Issues are the control plane. A label records status, the issue body
contains the execution contract, and an atomic remote Git ref is the claim
lock. The local marker is audit metadata only.

## Roles

- The planner turns a bounded request into an issue with `aes:contract`,
  `aes:dispatch`, acceptance criteria, and `aes:ready` only when verification
  is possible.
- The dispatcher checks dependencies, `not_before`, concurrency, budget, and
  author authorization before creating one claim per task.
- The worker adopts the remote claim, follows the repository workflow, runs
  tests, and opens a pull request. It never merges, moves a task across
  repositories, or bypasses a lost claim.

The dispatch block contains only `dispatch`, `priority`, `not_before`, and
`budget`. `role:*` and `autonomy:*` are separate canonical labels. Unknown
keys and values fail closed.

## Cycle

### Find work

```sh
harness/scripts/tasks/tasks.py next --role backend
harness/scripts/tasks/tasks.py show 42
```

Only open tasks with `aes:ready`, clear dependencies, and valid dispatch gates
are returned.

### Claim

```sh
harness/scripts/tasks/tasks.py claim 42
```

The first atomic ref creator wins. A `LOST` result means move to another task;
never delete or force another actor's ref. After a successful claim the issue
becomes `aes:claimed`, receives an execution ID comment, and the worker creates
its worktree only after adopting that ref.

### Implement and block safely

Follow the normal repository rules: write the eval before code, keep changes
small, and update documentation with behavior. If an unresolvable blocker
exists:

```sh
harness/scripts/tasks/tasks.py block 42 "precise blocker and required decision"
```

This records `aes:blocked` and stops. A product, permission, or policy question
must use `aes:needs-human` instead of an invented assumption.

### Open and stop

Link the pull request with `Closes #42` and:

```sh
harness/scripts/tasks/tasks.py link-pr 42
```

The issue becomes `aes:pr-open`. A human reviews and merges it; a green CI run
is not authorization to merge. `done` is the result of issue closure, not a
label the worker applies manually.

## Queue contract

The optional dispatcher and worker enforce `aes:ready`, `aes:claimed`,
`aes:waiting-provider`, `aes:blocked`, `aes:needs-human`, and `aes:pr-open`
transitions. They require a provider-neutral plan with bounded paths, acyclic
dependencies, unique IDs, non-empty prompts, and explicit budget and effort.
The route and quota rules are in `policies/autonomous-task-queue.md`.

Events include `repo`, `issue`, `execution_id`, and a stable deduplication key.
Notifications do not change status or authorize a merge. Provider quota
failures preserve the same ledger for a later retry; ambiguous credentials,
network state, or external actions stop with a human request.

## Diagnosis

```sh
harness/scripts/tasks/tasks.py doctor
```

The doctor reports missing labels, orphaned claims, and stale leases. It does
not release a stale lease automatically. The authoritative ownership signal is
the remote ref, not the label; discrepancies must be escalated.
