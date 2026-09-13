# Git workflow policy

## Branches and worktrees

`main` is production and `develop` is the integration branch. Work starts from
a freshly fetched integration branch and uses a short-lived `feature/*`,
`fix/*`, or `hotfix/*` branch in a worktree outside the repository tree. Run
`harness/scripts/git-orient.sh` before work and use the feature scripts to create
and finish the worktree.

## Protected refs

Agents must not push directly to `main`, `develop`, protected release tags, or
any other configured protected ref. `guard-push.sh` fails closed for direct,
force, delete, refspec, and shell-expansion forms of a protected push. A work
branch may be pushed and reviewed through a pull request.

## Pull requests and merge

The agent may prepare commits, run checks, and open a pull request. The agent
does not run `gh pr merge` on its own. The merge is performed by a human after
review; passing CI is not merge authorization. Review comments that identify a
real defect should be fixed, while non-blocking suggestions can be answered in
the pull request without unnecessary churn.

## Completion and cleanup

`feature-done.sh` checks that the branch's complete patch is integrated before
using safe branch deletion. It never uses force deletion. A branch is not reused
after merge; create a new one for later work. If a claim or remote state is
ambiguous, stop and request human review rather than deleting data.

## Synchronization and verification

`aes-sync.sh --check` detects local changes to managed assets. It does not prove
that a consumer has the latest source; use `--check-upstream` or an explicit
sync when checking freshness. Run focused tests, `git diff --check`, and the
repository's full required eval suite before opening a pull request.
