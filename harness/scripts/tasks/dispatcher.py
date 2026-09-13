"""Repo-local queue planner and dispatcher for the AES issue protocol.

The pure selection functions are deliberately independent from GitHub. The
CLI only coordinates ``gh`` and emits JSON for a worker matrix; issue bodies
are never interpolated into a shell command. The durable ownership lock is
the remote feature ref created atomically by this planner before the worker
matrix, never local runner state (a matrix can land on another self-hosted
runner).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import sys
import uuid

import claim as task_claim
from authorization import author_allowed, author_gate_reason
from dispatch import validate
import provider_github
from readiness import Task, is_ready


class QueueConfigError(RuntimeError):
    """Queue configuration is missing or unsafe."""


@dataclass(frozen=True)
class QueueConfig:
    enabled: bool
    max_concurrency: int
    max_concurrency_per_repo: int
    budget: int

    @classmethod
    def from_env(cls) -> "QueueConfig":
        raw_enabled = os.environ.get("AES_QUEUE_ENABLED", "").strip().lower()
        if raw_enabled != "true":
            return cls(False, 0, 0, 0)

        def positive(name: str) -> int:
            raw = os.environ.get(name, "")
            try:
                value = int(raw)
            except (TypeError, ValueError) as error:
                raise QueueConfigError(f"{name} must be a positive integer") from error
            if value <= 0:
                raise QueueConfigError(f"{name} must be a positive integer")
            return value

        raw_budget = os.environ.get("AES_QUEUE_BUDGET", "").strip()
        if raw_budget in ("", "0"):
            return cls(False, 0, 0, 0)
        return cls(
            True,
            positive("AES_QUEUE_MAX_CONCURRENCY"),
            positive("AES_QUEUE_MAX_CONCURRENCY_PER_REPO"),
            positive("AES_QUEUE_BUDGET"),
        )


def select_tasks(
    tasks: list[Task],
    config: QueueConfig,
    active_global: int,
    active_repo: int,
    budget_used: int,
    now: datetime | None = None,
    dep_states: dict[str, str] | None = None,
    allowed_authors: str | None = None,
) -> tuple[list[Task], list[tuple[Task, str]]]:
    """Select dispatchable tasks without partially reserving budget.

    The second result contains diagnostics.  Invalid metadata is returned to
    the caller so the state transition to ``aes:needs-human`` remains an
    explicit side effect of the CLI, while cap/future/budget skips stay ready.
    """

    if not config.enabled:
        return [], []
    now = now or datetime.now(timezone.utc)
    dep_states = dep_states or {}
    candidates = sorted(
        (task for task in tasks if "aes:ready" in task.labels),
        key=lambda task: (
            task.dispatch.priority_rank if task.dispatch else 99,
            task.created_at or datetime.max.replace(tzinfo=timezone.utc),
            task.number,
        ),
    )
    selected: list[Task] = []
    skipped: list[tuple[Task, str]] = []
    reserved = budget_used
    configured_authors = (
        allowed_authors
        if allowed_authors is not None
        else os.environ.get("AES_QUEUE_ALLOWED_AUTHORS", "")
    )
    for task in candidates:
        author_reason = author_gate_reason(task.author, configured_authors)
        if author_reason:
            # Authorization is exclusion-only. An untrusted issue must never
            # be able to create a PM mention by supplying malformed metadata.
            skipped.append((task, author_reason))
            continue
        if task.dispatch_error:
            skipped.append((task, task.dispatch_error))
            continue
        if task.dispatch is None:
            skipped.append((task, "dispatch block not found"))
            continue
        if task.dispatch.dispatch != "auto":
            continue
        reasons = list(validate(task.dispatch, task.labels))
        ready, ready_reason = is_ready(task, dep_states)
        if not ready:
            reasons.append(ready_reason)
        if reasons:
            skipped.append((task, "; ".join(dict.fromkeys(reasons))))
            continue
        if task.dispatch.not_before and task.dispatch.not_before > now:
            skipped.append((task, "not_before is in the future"))
            continue
        if active_global + len(selected) >= config.max_concurrency:
            skipped.append((task, "global concurrency cap exhausted"))
            continue
        if active_repo + len(selected) >= config.max_concurrency_per_repo:
            skipped.append((task, "repository concurrency cap exhausted"))
            continue
        assert task.dispatch.budget is not None
        if reserved + task.dispatch.budget > config.budget:
            skipped.append((task, "budget unavailable; no partial reservation"))
            continue
        selected.append(task)
        reserved += task.dispatch.budget
    return selected, skipped


def command_check_author(args: argparse.Namespace) -> int:
    if not author_allowed(args.author):
        print(f"error: issue author {args.author!r} is not allowed", file=sys.stderr)
        return 2
    return 0


def command_plan(args: argparse.Namespace) -> int:
    config = QueueConfig.from_env()
    if not config.enabled:
        print(json.dumps({"tasks": [], "disabled": True}))
        return 0
    repo = args.repo or provider_github.current_repo()
    tasks = provider_github.list_tasks(repo, ("aes:ready",))
    waiting_tasks = provider_github.list_tasks(repo, ("aes:waiting-provider",))
    active = provider_github.list_tasks(repo, ("aes:claimed",))
    configured_authors = os.environ.get("AES_QUEUE_ALLOWED_AUTHORS", "")
    valid_active = [
        task for task in active if _valid_active_claim(task, configured_authors)
    ]
    invalid_active = [
        task for task in active if not _valid_active_claim(task, configured_authors)
    ]
    active_budget = sum(
        task.dispatch.budget
        for task in valid_active
        if task.dispatch is not None and task.dispatch.budget is not None
    )
    dep_keys = sorted(
        dependency
        for task in tasks
        if task.contract
        for dependency in task.contract.depends_on
    )
    dep_states = provider_github.get_states(repo, dep_keys)
    jobs: list[dict[str, object]] = []
    skipped: list[tuple[Task, str]] = []

    # A waiting execution is resumed before selecting new work.  It owns the
    # same budget/capacity unit as a fresh execution, so a queue containing one
    # waiter and one ready task with cap=1 emits exactly one job.  The counters
    # below are deliberately local reservations: a failed live revalidation or
    # a lost remote resume lease does not consume capacity for this plan.
    reserved_global = len(valid_active)
    reserved_repo = len(valid_active)
    reserved_budget = active_budget
    waiting_jobs: list[dict[str, object]] = []
    for task in _ordered_waiting_tasks(waiting_tasks):
        if not args.apply:
            skipped.append((task, "waiting-provider resume requires --apply to acquire a lease"))
            continue
        reason = _waiting_metadata_reason(task, configured_authors)
        if reason:
            skipped.append((task, reason))
            continue
        assert task.dispatch is not None and task.dispatch.budget is not None
        if reserved_global >= config.max_concurrency:
            skipped.append((task, "global concurrency cap exhausted"))
            continue
        if reserved_repo >= config.max_concurrency_per_repo:
            skipped.append((task, "repository concurrency cap exhausted"))
            continue
        if reserved_budget + task.dispatch.budget > config.budget:
            skipped.append((task, "budget unavailable; no partial reservation"))
            continue
        try:
            execution_id, lease = _resumable_execution(
                repo,
                task,
                acquire_lease=args.apply,
            )
        except (task_claim.ClaimError, provider_github.GitHubError, ValueError) as error:
            skipped.append((task, f"waiting-provider resume rejected: {error}"))
            continue
        job: dict[str, object] = {
            "repo": task.repo_slug,
            "issue": task.number,
            "execution_id": execution_id,
            "resume": True,
        }
        if lease is None:
            # ``_resumable_execution`` rejects this in production.  Keep the
            # guard at the matrix boundary as well: a worker must never be
            # emitted with a partial or discoverable lease identity.
            raise RuntimeError("dispatcher acquired a resume without a complete lease")
        job["lease"] = {
            "owner": lease.owner,
            "ref": lease.ref,
            "expected_sha": lease.expected_sha,
        }
        job["lease_owner"] = lease.owner
        job["lease_ref"] = lease.ref
        job["lease_expected_sha"] = lease.expected_sha
        waiting_jobs.append(job)
        reserved_global += 1
        reserved_repo += 1
        reserved_budget += task.dispatch.budget

    # Only capacity left after accepted waiters may be used by new ready work.
    selected, ready_skipped = select_tasks(
        tasks,
        config,
        active_global=reserved_global,
        active_repo=reserved_repo,
        budget_used=reserved_budget,
        dep_states=dep_states,
        allowed_authors=configured_authors,
    )
    skipped.extend(ready_skipped)
    jobs.extend(waiting_jobs)
    if args.apply:
        from tasks import notify_needs_human

        for task in invalid_active:
            if task.state == "open" and _requires_human_transition(task):
                reason = task.dispatch_error or "task has no valid dispatch contract"
                notify_needs_human(
                    repo,
                    task.number,
                    f"validation-{repo.replace('/', '-')}-{task.number}",
                    reason,
                    "Restore valid aes:contract and aes:dispatch blocks before dispatch.",
                )
        for task, reason in skipped:
            if _requires_human_transition(task):
                notify_needs_human(
                    repo,
                    task.number,
                    f"validation-{repo.replace('/', '-')}-{task.number}",
                    reason,
                    "Validate the dispatch contract.",
                )
        base_branch = os.environ.get("AES_INTEGRATION_BRANCH", "develop")
        for task in selected:
            execution_id = str(uuid.uuid4())
            try:
                result = task_claim.claim_remote(
                    repo,
                    task.number,
                    base_branch,
                    execution_id=execution_id,
                )
            except (task_claim.ClaimError, provider_github.GitHubError) as error:
                reason = f"remote claim failed: {error}"
                # A failure may happen after the ref was created but before
                # the issue mirrors were updated.  Keep that failure visible
                # and remove the task from the ready queue when possible;
                # otherwise an orphaned claim would be silent until doctor.
                try:
                    notify_needs_human(
                        repo,
                        task.number,
                        execution_id,
                        reason,
                        "Inspect the remote claim and resume only after the claim is reconciled.",
                    )
                except (provider_github.GitHubError, RuntimeError) as notify_error:
                    print(
                        f"error: {reason}; unable to emit aes:needs-human: {notify_error}",
                        file=sys.stderr,
                    )
                skipped.append((task, reason))
                continue
            if result is task_claim.LOST:
                skipped.append((task, "LOST: remote claim already exists"))
                continue
            jobs.append(
                {
                    "repo": task.repo_slug,
                    "issue": task.number,
                    "execution_id": execution_id,
                    "resume": False,
                }
            )
    else:
        jobs.extend(
            [
                {
                    "repo": task.repo_slug,
                    "issue": task.number,
                    "execution_id": str(uuid.uuid4()),
                    "resume": False,
                }
                for task in selected
            ]
        )
    output = {
        "tasks": jobs,
        "skipped": [{"issue": task.number, "reason": reason} for task, reason in skipped],
    }
    print(json.dumps(output, separators=(",", ":")))
    return 0


def _valid_active_claim(task: Task, allowed_authors: str | None = None) -> bool:
    """Count only open claimed tasks with complete, valid queue metadata."""

    if task.state != "open" or task.contract is None:
        return False
    # A waiting provider execution keeps its claim/ref but does not consume a
    # live worker slot while quota is unavailable.
    if "aes:waiting-provider" in task.labels:
        return False
    if not author_allowed(task.author, allowed_authors):
        return False
    if task.dispatch is None or task.dispatch_error:
        return False
    if task.dispatch.dispatch != "auto" or task.dispatch.budget is None:
        return False
    return not validate(task.dispatch, task.labels)


def _ordered_waiting_tasks(tasks: list[Task]) -> list[Task]:
    """Order waiters deterministically before reserving resume capacity."""

    return sorted(
        tasks,
        key=lambda task: (
            task.dispatch.priority_rank if task.dispatch else 99,
            task.created_at or datetime.max.replace(tzinfo=timezone.utc),
            task.number,
        ),
    )


def _waiting_metadata_reason(task: Task, configured_authors: str) -> str | None:
    """Return a non-capacity reason for a malformed waiting execution."""

    if task.state != "open":
        return "task is not open"
    if "aes:waiting-provider" not in task.labels:
        return "task is no longer waiting for a provider"
    if "aes:claimed" not in task.labels:
        return "waiting task has no active claim"
    author_reason = author_gate_reason(task.author, configured_authors)
    if author_reason:
        return author_reason
    if task.contract is None:
        return "waiting task has no valid contract/dispatch metadata"
    if task.dispatch_error or task.dispatch is None:
        return task.dispatch_error or "waiting task has no valid contract/dispatch metadata"
    reasons = validate(task.dispatch, task.labels)
    if task.dispatch.dispatch != "auto" or reasons:
        return "; ".join(reasons) or "waiting task is not configured for autonomous dispatch"
    return None


def _resumable_execution(
    repo: str,
    task: Task,
    *,
    acquire_lease: bool = False,
) -> tuple[str, provider_github.ResumeLease]:
    """Revalidate a waiting claim and return its original execution id.

    Waiting issues are mutable remote state.  The dispatcher therefore reads
    the issue and claim comments again immediately before emitting a worker
    job, rather than trusting the label scan that found the candidate.
    """

    live = provider_github.get_task(repo, task.number)
    if live.contract is None or live.dispatch_error or live.dispatch is None:
        raise ValueError("waiting task has no valid contract/dispatch metadata")
    if live.state != "open" or "aes:waiting-provider" not in live.labels:
        raise task_claim.ClaimError("waiting task changed while being resumed")
    if "aes:claimed" not in live.labels:
        raise task_claim.ClaimError("waiting task has no active claim")
    configured_authors = os.environ.get("AES_QUEUE_ALLOWED_AUTHORS", "")
    author_reason = author_gate_reason(live.author, configured_authors)
    if author_reason:
        raise task_claim.ClaimError(author_reason)
    if validate(live.dispatch, live.labels):
        raise ValueError("waiting task dispatch metadata or labels are invalid")

    comments = provider_github.list_issue_comments(repo, task.number)
    execution_id: str | None = None
    branch: str | None = None
    candidate_errors: list[str] = []
    for comment in reversed(comments):
        marker_ids = task_claim.extract_execution_ids(comment.body)
        for candidate in reversed(marker_ids):
            try:
                branch = task_claim.validate_claim(
                    repo,
                    task.number,
                    candidate,
                    enforce_author=True,
                )
            except task_claim.ClaimError as error:
                candidate_errors.append(str(error))
                continue
            execution_id = candidate
            break
        if execution_id:
            break
    if not execution_id:
        detail = candidate_errors[-1] if candidate_errors else "no structured marker found"
        raise ValueError(f"waiting task has no valid execution_id claim marker: {detail}")
    # The claim/ref/comment is the ownership proof.  This also verifies that
    # the execution id belongs to the sole feature ref and that the author
    # gate still holds at the exact resume boundary.
    assert branch is not None
    if not acquire_lease:
        raise ValueError("waiting task resume requires an acquired lease")
    acquired = task_claim.acquire_resume_lease(
        repo,
        task.number,
        execution_id,
        branch,
    )
    if not isinstance(acquired, provider_github.ResumeLease):
        raise task_claim.ClaimError("dispatcher did not receive a complete resume lease")
    return execution_id, acquired


def _requires_human_transition(task: Task) -> bool:
    """Return whether a skipped task has a malformed contract/dispatch gate."""

    # Author authorization is an exclusion gate, never a HITL transition.
    if author_gate_reason(task.author) is not None:
        return False
    if task.contract is None or task.dispatch_error or task.dispatch is None:
        return True
    return bool(validate(task.dispatch, task.labels))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-author", dest="author", help=argparse.SUPPRESS)
    parser.add_argument("--repo")
    parser.add_argument("--apply", action="store_true")
    parser.set_defaults(handler=command_plan)
    return parser


if __name__ == "__main__":
    try:
        parsed = build_parser().parse_args()
        if parsed.author is not None:
            raise SystemExit(command_check_author(parsed))
        raise SystemExit(parsed.handler(parsed))
    except (QueueConfigError, provider_github.GitHubError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
