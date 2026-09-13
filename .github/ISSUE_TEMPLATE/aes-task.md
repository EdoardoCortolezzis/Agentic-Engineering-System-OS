---
name: Task AES
about: An AES-agent executable task with an execution contract
title: ''
labels: ''
---

## Goal

<!-- One sentence: what must be done and why. -->

## Acceptance criteria

<!-- Verifiable checklist. If it cannot be verified, the task is not ready. -->

-
-

## Execution contract

<!-- The block below is read by the harness (harness/scripts/tasks/).
     Format: one `key: value` per line, lists separated by commas.
     This is not YAML: no indentation and no dash lists.
     Allowed keys: repo, paths, constraints, depends_on, done.
     An unknown key fails parsing; it is not ignored.
     Leave irrelevant keys empty; do not delete them. -->

<!-- aes:contract -->
repo:
paths:
constraints:
depends_on:
done:
<!-- /aes:contract -->

## Queue metadata

<!-- The dispatcher reads this block only when the autonomous queue is
     enabled. Flat format: one `key: value` per line, not YAML.
     Allowed keys: dispatch, priority, not_before, budget.
     `not_before` is RFC 3339 UTC; `budget` is an integer in abstract units.
     A task with `dispatch: auto` must have every required field valid. -->

<!-- aes:dispatch -->
dispatch: manual
priority: normal
not_before:
budget:
<!-- /aes:dispatch -->

---

<!-- Before adding `aes:ready`: the contract is complete, acceptance criteria
     are verifiable, and dependencies in `depends_on` exist.
     For the autonomous queue, set `dispatch: auto` and a positive `budget`.
     Role and autonomy remain in the canonical `role:*` and `autonomy:*`
     labels, compatible with tasks.py. The lock remains the Git ref created by
     the claim, not a label or the metadata block. -->
