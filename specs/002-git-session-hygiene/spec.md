# Specification: Git session hygiene

## Goal

Ensure an agent starts from a usable checkout and works in an isolated,
short-lived worktree without changing protected branches.

## BDD scenarios

Scenario: a current integration checkout is accepted
Given the integration branch exists and the remote is reachable
When `git-orient.sh` runs
Then it reports `OK`.

Scenario: a stale branch is rejected
Given the current branch is behind its integration remote
When orientation runs in check mode
Then it reports `STALE` and exits non-zero.

Scenario: a feature worktree is outside the repository
Given a clean integration checkout
When `feature-start.sh` creates a feature
Then the worktree is outside the repository tree and linked local files are
ignored safely.

Scenario: a protected branch cannot be pushed
Given a shell command that pushes a protected branch
When `guard-push.sh` evaluates it
Then it exits with a blocking status.

Scenario: an integrated branch is removed safely
Given a feature patch is integrated by content
When `feature-done.sh` runs
Then it uses safe deletion and never force deletes the branch.

## Invariants

Production and integration refs are configured, not hardcoded. Worktrees
isolate tracked code; shared data and external services remain operator-owned
boundaries. A missing hook or malformed configuration fails closed.
