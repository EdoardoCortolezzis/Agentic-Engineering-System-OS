# ADR 0005: Protected integration branches

## Status

Accepted.

## Decision

Production uses `main`; integration uses `develop`; short-lived work uses
`feature/*`, `fix/*`, or `hotfix/*`. Work starts from a freshly fetched
integration branch in an external worktree. Agents may create branches, commit,
run checks, and open pull requests, but must not push directly to protected
branches.

The merge is a human action. An agent must not invoke `gh pr merge` unless the
repository owner gives an explicit instruction for that pull request. A green
CI run is evidence for review, not permission to integrate.

## Consequences

The branch boundary is visible in Git and enforced by the push guard. Branches
are disposable after merge, which keeps stale state from being reused. A human
must review and merge every pull request, including automation-generated work.
