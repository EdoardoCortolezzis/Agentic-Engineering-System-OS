# Specification: issue task flow

## Goal

Use GitHub Issues as the task control plane while keeping ownership in an
atomic Git ref and preserving explicit human gates.

## BDD scenarios

Scenario: a valid issue is dispatchable
Given an open issue with `aes:ready`, a valid contract, and closed dependencies
When `tasks.py next` runs
Then the issue is listed once.

Scenario: a missing contract is rejected
Given an issue without the required contract block
When readiness is evaluated
Then it is not dispatchable and a reason is recorded.

Scenario: the first claim wins
Given two workers claim the same issue concurrently
When both create the remote claim ref
Then exactly one succeeds and the loser moves to another task.

Scenario: a dependency cycle is diagnosed
Given tasks whose dependencies form a cycle
When the task graph is evaluated
Then `find_cycle` reports the cycle and no task is hidden as healthy.

Scenario: a blocked task requires human action
Given a technical or product blocker
When the worker reports it
Then the issue becomes `aes:blocked` or `aes:needs-human` with a precise reason.

Scenario: a pull request is opened
Given a claimed task with passing checks
When the worker links its pull request
Then the issue becomes `aes:pr-open` and the worker stops.

## Invariants

Labels are status only. The claim ref is authoritative and must never be
deleted or replaced by a worker that lost it. Workers are restricted to the
issue-declared repository and paths. Merge, publication, credential grants,
and product decisions remain human actions.
