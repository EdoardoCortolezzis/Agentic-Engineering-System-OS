#!/usr/bin/env python3
"""CLI for the AES task flow based on GitHub Issues."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Sequence

import claim as task_claim
from authorization import author_gate_reason, autonomous_gate_enabled
from contract import Contract, ContractError
import provider_github
from readiness import Task, find_cycle, is_ready


TASK_LABELS = {
    "aes:ready": ("1D76DB", "Task dispatchable by an agent"),
    "aes:claimed": ("FBCA04", "Task claimed by an execution"),
    "aes:waiting-provider": ("C5DEF5", "Task waiting for provider quota or availability"),
    "aes:pr-open": ("A371F7", "Task with an open pull request"),
    "aes:blocked": ("D73A4A", "Task blocked by a reported problem"),
    "aes:needs-human": ("B60205", "Task waiting for human action"),
}
REF_PATTERN = re.compile(r"^refs/heads/feature/(\d+)-.+$")
CONFIG_PATTERN = re.compile(
    r"^\s*INTEGRATION_BRANCH\s*=\s*['\"]?([^'\"\s#]+)['\"]?\s*(?:#.*)?$"
)
DEFAULT_LEASE_STALE_HOURS = 24
EXECUTION_ID_PATTERN = re.compile(
    r"(?:execution[_ -]?id\s*[:=]?\s*|`)[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}`?",
    re.IGNORECASE,
)


def _repo(explicit_repo: str | None) -> str:
    """Use the explicit repository or the current directory's repository."""

    return explicit_repo or provider_github.current_repo()


def _dependency_states(task: Task) -> dict[str, str]:
    """Load the states needed to evaluate one task."""

    dependencies = task.contract.depends_on if task.contract else ()
    return provider_github.get_states(task.repo_slug, dependencies)


def _print_contract(contract: Contract | None) -> None:
    """Print the contract in a brief-friendly format."""

    print("Contract:")
    if contract is None:
        print("  (missing)")
        return

    print(f"  repo: {contract.repo or '(not specified)'}")
    print(f"  paths: {', '.join(contract.paths) or '(none)'}")
    print(f"  constraints: {contract.constraints or '(none)'}")
    print(f"  depends_on: {', '.join(contract.depends_on) or '(none)'}")
    print(f"  done: {', '.join(contract.done) or '(none)'}")


def command_next(args: argparse.Namespace) -> int:
    """List only tasks that genuinely pass readiness."""

    repo_slug = _repo(args.repo)
    required_labels = ["aes:ready"]
    if args.role:
        required_labels.append(f"role:{args.role}")

    tasks = provider_github.list_tasks(repo_slug, required_labels)
    dependencies = {
        dependency
        for task in tasks
        if task.contract is not None
        for dependency in task.contract.depends_on
    }
    dep_states = provider_github.get_states(repo_slug, sorted(dependencies))
    ready_tasks = [task for task in tasks if is_ready(task, dep_states)[0]]
    if autonomous_gate_enabled():
        ready_tasks = [
            task for task in ready_tasks
            if author_gate_reason(task.author) is None
        ]

    if not ready_tasks:
        print("No dispatchable tasks found.")
        return 0
    for task in ready_tasks:
        print(f"#{task.number} {task.title}")
    return 0


def command_show(args: argparse.Namespace) -> int:
    """Show the task, contract, and any blocking reason."""

    repo_slug = _repo(args.repo)
    task = provider_github.get_task(repo_slug, args.number)
    ready, reason = is_ready(task, _dependency_states(task))

    print(f"Task: {repo_slug}#{task.number}")
    print(f"Title: {task.title}")
    print(f"State: {task.state}")
    print(f"Labels: {', '.join(task.labels) or '(none)'}")
    print(f"Readiness: {'ready' if ready else 'not ready'}")
    if not ready:
        print(f"Reason: {reason}")
    _print_contract(task.contract)
    return 0


def _integration_branch() -> str:
    """Read the integration branch from harness configuration."""

    config_path = Path(__file__).resolve().parents[2] / "config" / "harness.conf"
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RuntimeError(f"failed to read harness config {config_path}: {error}") from error
    for line in lines:
        match = CONFIG_PATTERN.match(line)
        if match:
            return match.group(1)
    raise RuntimeError(f"INTEGRATION_BRANCH is missing from {config_path}")


def command_claim(args: argparse.Namespace) -> int:
    """Backward-compatible claim plus local adoption."""

    repo_slug = _repo(args.repo)
    result = task_claim.claim(
        repo_slug,
        args.number,
        args.base or _integration_branch(),
        execution_id=args.execution_id,
        enforce_author=autonomous_gate_enabled(),
    )
    print(result.value)
    if result is task_claim.LOST:
        return 1

    return _adopt_claim(repo_slug, args.number, args.execution_id)


def _adopt_claim(
    repo_slug: str,
    number: int,
    execution_id: str | None,
    *,
    enforce_author: bool = False,
) -> int:
    """Adopt an already claimed remote ref and distinguish failures from LOST."""

    # Read the branch name from the ref instead of recalculating it from the
    # title: the title may change between reading it and creating the ref, in
    # which case `--adopt` would receive a slug matching no branch. The ref is
    # the source of truth for ownership (ADR 0015).
    try:
        branch = task_claim.claimed_branch(
            repo_slug,
            number,
            execution_id,
            enforce_author=enforce_author,
        )
    except task_claim.ClaimError as error:
        print(f"Claim is valid (acquired), but adoption was rejected: {error}", file=sys.stderr)
        return 1
    feature = branch.removeprefix("feature/")
    script = Path(__file__).resolve().parents[1] / "feature-start.sh"
    try:
        adopted = subprocess.run(
            (str(script), "--adopt", feature),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        print(
            "Claim is valid (acquired), but the worktree could not be created. "
            "Create it manually with feature-start.sh --adopt " + feature,
            file=sys.stderr,
        )
        return 1
    if adopted.stdout:
        # The feature-start helper is untrusted process output.  Preserve
        # only its successful human-facing status; never echo raw stderr into
        # the queue control plane.
        print(adopted.stdout, end="")
    if adopted.returncode != 0:
        print(
            "Claim is valid (acquired), but worktree adoption failed. "
            "Create it manually with feature-start.sh --adopt " + feature,
            file=sys.stderr,
        )
        return 1
    return 0


def command_adopt(args: argparse.Namespace) -> int:
    """Adopt only a remote ref already claimed by this execution."""

    repo_slug = _repo(args.repo)
    return _adopt_claim(
        repo_slug,
        args.number,
        args.execution_id,
        enforce_author=autonomous_gate_enabled(),
    )


def command_block(args: argparse.Namespace) -> int:
    """Record the reason and mark the task as blocked."""

    repo_slug = _repo(args.repo)
    provider_github.comment(repo_slug, args.number, f"Task blocked: {args.reason}")
    provider_github.add_labels(repo_slug, args.number, ("aes:blocked",))
    # A blocked issue keeps its remote feature ref as an audit/ownership
    # record, but it must release the active worker slot immediately.
    provider_github.remove_labels(
        repo_slug,
        args.number,
        ("aes:claimed", "aes:waiting-provider"),
    )
    print(f"Blocked {repo_slug}#{args.number}: {args.reason}")
    return 0


def command_needs_human(args: argparse.Namespace) -> int:
    repo_slug = _repo(args.repo)
    notify_needs_human(repo_slug, args.number, args.execution_id, args.reason, args.question)
    print(f"Needs human: {repo_slug}#{args.number}")
    return 0


def command_waiting_provider(args: argparse.Namespace) -> int:
    repo_slug = _repo(args.repo)
    notify_waiting_provider(
        repo_slug,
        args.number,
        args.execution_id,
        args.reason,
        getattr(args, "artifact_root", None),
    )
    print(f"Waiting for provider: {repo_slug}#{args.number}")
    return 0


def _validate_resume_claim(repo_slug: str, number: int, execution_id: str) -> None:
    """Verify the owner before changing a waiting execution's queue label."""

    task = provider_github.get_task(repo_slug, number)
    labels = set(task.labels)
    if (
        task.state != "open"
        or "aes:claimed" not in labels
        or "aes:waiting-provider" not in labels
        or labels.intersection({"aes:blocked", "aes:needs-human", "aes:pr-open"})
    ):
        raise RuntimeError("task is no longer in the claimed waiting state")
    try:
        task_claim.validate_claim(
            repo_slug,
            number,
            execution_id,
            enforce_author=True,
        )
    except task_claim.ClaimError as error:
        raise RuntimeError("claim is stale or owned by another execution") from error


def command_resume_start(args: argparse.Namespace) -> int:
    """Move a waiting execution back to the claimed state before orchestration."""

    repo_slug = _repo(args.repo)
    _validate_resume_claim(repo_slug, args.number, args.execution_id)
    provider_github.remove_labels(repo_slug, args.number, ("aes:waiting-provider",))
    print(f"Resuming provider wait: {repo_slug}#{args.number}")
    return 0


def command_resume_cleanup(args: argparse.Namespace) -> int:
    """Release a resume CAS ref, never deleting a newer owner's lease."""

    repo_slug = _repo(args.repo)
    released = task_claim.release_resume_lease(
        repo_slug,
        args.number,
        args.execution_id,
        args.expected_sha,
        args.lease_ref,
    )
    if not released:
        # A missing ref is idempotent; an owner/SHA mismatch is deliberately
        # visible to an always-run workflow cleanup step.
        print(f"Resume lease was not released for {repo_slug}#{args.number}", file=sys.stderr)
        return 1
    print(f"Released resume lease for {repo_slug}#{args.number}")
    return 0


def _notification_key(repo_slug: str, number: int, event: str, execution_id: str) -> str:
    return f"{repo_slug}#{number}:{event}:{execution_id}"


def _notification_marker(key: str) -> str:
    """Return the canonical, whole-line marker used for notification dedupe."""

    return f"dedupe_key: `{key}`"


def _trusted_actor() -> str:
    actor = os.environ.get("AES_TRUSTED_ACTOR", "").strip()
    if not actor:
        raise RuntimeError("AES_TRUSTED_ACTOR is required for notification dedupe")
    return actor


def _has_exact_notification_marker(comment, marker: str, trusted_actor: str) -> bool:
    """Match only an exact structured marker authored by the trusted actor."""

    if getattr(comment, "author", None) != trusted_actor:
        return False
    body = getattr(comment, "body", "")
    return isinstance(body, str) and any(line.strip() == marker for line in body.splitlines())


def _public_reason(event: str, execution_id: str) -> str:
    """Keep model/provider details in trusted artifacts, never in comments."""

    return f"AES {event} requires review; execution_id={execution_id}"[:240]


def _configured_mention() -> str:
    value = os.environ.get("AES_PRODUCT_MANAGER", "").strip()
    if not value:
        return ""
    return value if value.startswith("@") else f"@{value}"


def notify_event(
    repo_slug: str,
    number: int,
    event: str,
    execution_id: str,
    payload: dict[str, str],
    *,
    mention_pm: bool = True,
) -> bool:
    """Post one best-effort deduplicated issue event, optionally mentioning PM.

    GitHub issue comments have no compare-and-swap create primitive. Two
    independent runners can therefore observe the same absent key and both
    publish; retries after an already visible key are suppressed.
    """

    if not execution_id:
        raise RuntimeError("execution_id is required for queue notifications")
    key = _notification_key(repo_slug, number, event, execution_id)
    marker = _notification_marker(key)
    trusted_actor = _trusted_actor()
    if any(
        _has_exact_notification_marker(comment, marker, trusted_actor)
        for comment in provider_github.list_issue_comments(repo_slug, number)
    ):
        return False
    mention = _configured_mention() if mention_pm else ""
    lines = [mention] if mention else []
    lines.append(f"AES event: `{event}`")
    lines.append(marker)
    # Event payloads are useful for local diagnostics, but model/provider
    # output must not become a public control-plane comment.  Keep a bounded,
    # generic reason and the trusted execution handle only.
    for key_name, value in payload.items():
        if key_name == "reason":
            value = _public_reason(event, execution_id)
        lines.append(f"{key_name}: {value}")
    if not any(line.startswith("execution_id:") for line in lines):
        lines.append(f"execution_id: {execution_id}")
    provider_github.comment(repo_slug, number, "\n".join(lines))
    return True


def notify_needs_human(
    repo_slug: str,
    number: int,
    execution_id: str,
    reason: str,
    question: str,
) -> bool:
    """Transition a task to the HITL gate and emit its deduplicated event."""

    if not reason.strip() or not question.strip():
        raise RuntimeError("reason and question are required")
    provider_github.add_labels(repo_slug, number, ("aes:needs-human",))
    provider_github.remove_labels(
        repo_slug,
        number,
        ("aes:ready", "aes:claimed", "aes:waiting-provider"),
    )
    return notify_event(
        repo_slug,
        number,
        "aes:needs-human",
        execution_id,
        {"repo": repo_slug, "issue": str(number), "reason": reason, "question": question},
    )


def notify_waiting_provider(
    repo_slug: str,
    number: int,
    execution_id: str,
    reason: str,
    artifact_root: str | None = None,
) -> bool:
    """Persist a provider wait without exposing local artifact paths.

    ``artifact_root`` remains an ignored compatibility argument for callers
    from older runners.  It is deliberately never copied into the issue
    comment: the execution id is the only safe remote handle for resumption.
    """

    del artifact_root
    if not isinstance(reason, str) or not reason.strip():
        raise RuntimeError("reason is required")
    if not isinstance(execution_id, str) or not execution_id.strip():
        raise RuntimeError("execution_id is required")
    safe_reason = " ".join(reason.split())
    # A provider wait is resumable work, not a released task.  Keep
    # ``aes:claimed`` and the remote feature ref untouched; only the queue
    # state mirror changes.
    provider_github.add_labels(repo_slug, number, ("aes:waiting-provider",))
    provider_github.remove_labels(repo_slug, number, ("aes:ready",))
    return notify_event(
        repo_slug,
        number,
        "aes:waiting-provider",
        execution_id,
        {
            "execution_id": execution_id,
            "reason": safe_reason,
        },
        mention_pm=False,
    )


def _execution_id_from_issue(repo_slug: str, number: int) -> str | None:
    for comment in reversed(provider_github.list_issue_comments(repo_slug, number)):
        ids = task_claim.extract_execution_ids(comment.body)
        if ids:
            return ids[-1]
    return None


def _task_branches(repo_slug: str, number: int) -> tuple[str, ...]:
    """Return branch names associated with the issue number."""

    refs = provider_github.list_matching_refs(repo_slug, f"feature/{number}-")
    prefix = "refs/heads/"
    return tuple(ref[len(prefix):] if ref.startswith(prefix) else ref for ref in refs)


def _validate_link_claim(
    repo_slug: str,
    number: int,
    execution_id: str,
    *,
    expected_branch: str | None = None,
) -> str:
    """Re-read issue state and ownership immediately before label mutation."""

    try:
        task_claim.validate_execution_id(execution_id)
    except task_claim.ClaimError as error:
        raise RuntimeError("execution_id is invalid") from error
    try:
        task = provider_github.get_task(repo_slug, number)
    except provider_github.GitHubError as error:
        raise RuntimeError("live task state could not be read") from error
    labels = set(task.labels)
    if (
        task.state != "open"
        or "aes:claimed" not in labels
        or labels.intersection({"aes:blocked", "aes:needs-human", "aes:pr-open", "aes:waiting-provider"})
    ):
        raise RuntimeError("task is no longer in the claimed open state")
    try:
        branch = task_claim.validate_claim(
            repo_slug,
            number,
            execution_id,
            enforce_author=True,
        )
    except task_claim.ClaimError as error:
        raise RuntimeError("claim is stale or owned by another execution") from error
    if expected_branch is not None and branch != expected_branch:
        raise RuntimeError("claim branch changed before linking")
    return branch


def command_link_pr(args: argparse.Namespace) -> int:
    """Link the branch PR and move the issue to pr-open state."""

    repo_slug = _repo(args.repo)
    branches = _task_branches(repo_slug, args.number)
    if not branches:
        raise RuntimeError(f"no feature/{args.number}-* branch found")
    if len(branches) > 1:
        raise RuntimeError(
            f"multiple branches found for task {repo_slug}#{args.number}; run doctor"
        )

    pull_request = provider_github.find_pr_for_branch(repo_slug, branches[0])
    if pull_request is None:
        raise RuntimeError(f"no pull request found for branch {branches[0]}")
    execution_id = args.execution_id or _execution_id_from_issue(repo_slug, args.number)
    if not execution_id:
        raise RuntimeError("execution_id is required to link a pull request")
    claimed_branch = _validate_link_claim(
        repo_slug,
        args.number,
        execution_id,
        expected_branch=branches[0],
    )
    # The PR lookup itself is also mutable.  Re-read claim/state and the PR
    # immediately before the first label write; a stale runner must be
    # harmless even if the earlier lookup found a valid PR.
    latest_branch = _validate_link_claim(
        repo_slug,
        args.number,
        execution_id,
        expected_branch=claimed_branch,
    )
    latest_pr = provider_github.find_pr_for_branch(repo_slug, latest_branch)
    if latest_pr is None or latest_pr != pull_request:
        raise RuntimeError("pull request changed before linking")
    provider_github.add_labels(repo_slug, args.number, ("aes:pr-open",))
    provider_github.remove_labels(
        repo_slug,
        args.number,
        ("aes:claimed", "aes:waiting-provider"),
    )
    notify_event(
        repo_slug,
        args.number,
        "aes:pr-open",
        execution_id,
        {
            "repo": repo_slug,
            "issue": str(args.number),
            "pr_url": f"https://github.com/{repo_slug}/pull/{pull_request}",
        },
    )
    print(f"Linked PR #{pull_request} to {repo_slug}#{args.number}.")
    return 0


def command_init_labels(args: argparse.Namespace) -> int:
    """Create only AES labels that are still missing."""

    repo_slug = _repo(args.repo)
    existing = set(provider_github.list_labels(repo_slug))
    created: list[str] = []
    for name, (color, description) in TASK_LABELS.items():
        if name in existing:
            continue
        provider_github.create_label(repo_slug, name, color, description)
        created.append(name)
    if created:
        print(f"Created labels: {', '.join(created)}")
    else:
        print("All AES task labels already exist.")
    return 0


def _refs_by_issue(repo_slug: str) -> dict[int, list[str]]:
    """Index valid feature refs by issue number."""

    indexed: dict[int, list[str]] = defaultdict(list)
    for ref in provider_github.list_matching_refs(repo_slug, "feature/"):
        match = REF_PATTERN.match(ref)
        if match:
            indexed[int(match.group(1))].append(ref)
    return dict(indexed)


def command_doctor(args: argparse.Namespace) -> int:
    """Report label/ref divergences without attempting repairs."""

    repo_slug = _repo(args.repo)
    problems: list[str] = []
    existing_labels = set(provider_github.list_labels(repo_slug))
    for label in TASK_LABELS:
        if label not in existing_labels:
            problems.append(f"missing repository label: {label}")

    tasks = provider_github.list_tasks(repo_slug, ())
    tasks_by_number = {task.number: task for task in tasks}
    open_tasks = {
        f"{task.repo_slug}#{task.number}": task
        for task in tasks
        if task.state == "open"
    }
    cycle = find_cycle(open_tasks)
    if cycle is not None:
        problems.append(f"dependency cycle: {' -> '.join(cycle)}")
    refs_by_number = _refs_by_issue(repo_slug)

    for task in tasks:
        if (
            task.state == "open"
            and "aes:claimed" in task.labels
            and not refs_by_number.get(task.number)
        ):
            problems.append(
                f"{repo_slug}#{task.number} has aes:claimed but no feature/{task.number}-* ref"
            )
    for number, refs in sorted(refs_by_number.items()):
        task = tasks_by_number.get(number)
        if task is None:
            problems.append(f"feature ref for missing issue {repo_slug}#{number}: {refs[0]}")
        elif (
            task.state == "open"
            and "aes:claimed" not in task.labels
            and not any(
                label in task.labels
                for label in ("aes:blocked", "aes:needs-human", "aes:pr-open")
            )
        ):
            problems.append(
                f"{repo_slug}#{number} has feature ref but no aes:claimed label: {refs[0]}"
            )
        elif task.state == "open" and "aes:claimed" in task.labels and any(
            label in task.labels
            for label in ("aes:blocked", "aes:needs-human", "aes:pr-open")
        ):
            problems.append(
                f"{repo_slug}#{number} has aes:claimed together with a released state label"
            )
        if task is not None and task.state == "open" and len(refs) > 1:
            problems.append(
                f"{repo_slug}#{number} has multiple feature refs: {', '.join(refs)}"
            )
        if task is not None and task.state == "open" and "aes:claimed" in task.labels:
            comments = provider_github.list_issue_comments(repo_slug, number)
            claim_comments = [
                comment
                for comment in comments
                if EXECUTION_ID_PATTERN.search(comment.body)
            ]
            if not claim_comments:
                problems.append(
                    f"{repo_slug}#{number} has a feature ref without an execution_id comment: {refs[0]}"
                )
            cutoff = datetime.now(timezone.utc) - timedelta(
                hours=float(os.environ.get("AES_LEASE_STALE_HOURS", DEFAULT_LEASE_STALE_HOURS))
            )
            last_commit = provider_github.latest_branch_commit_date(repo_slug, refs[0])
            # The lease starts at the claim, therefore at the OLDEST comment:
            # with the newest one, anyone allowed to comment on the issue could
            # refresh it by posting any execution_id, and an abandoned claim
            # would never expire. Subsequent activity is measured by
            # last_commit.
            claim_date = min(
                (comment.created_at for comment in claim_comments),
                default=datetime.min.replace(tzinfo=timezone.utc),
            )
            if max(claim_date, last_commit) < cutoff:
                problems.append(
                    f"{repo_slug}#{number} has a stale lease; last activity on {refs[0]} was "
                    f"{max(claim_date, last_commit).isoformat()}"
                )

    if problems:
        print("Task flow anomalies found:")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print("Task flow is healthy.")
    return 0


def _add_repo_argument(parser: argparse.ArgumentParser) -> None:
    """Add the repository option shared by all commands."""

    parser.add_argument(
        "--repo",
        metavar="OWNER/NAME",
        help="GitHub repository; defaults to the current repository",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the public CLI parser."""

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    next_parser = commands.add_parser("next", help="list dispatchable tasks")
    _add_repo_argument(next_parser)
    next_parser.add_argument("--role", help="require the role:<role> label")
    next_parser.set_defaults(handler=command_next)

    show_parser = commands.add_parser("show", help="show a task execution brief")
    show_parser.add_argument("number", type=int)
    _add_repo_argument(show_parser)
    show_parser.set_defaults(handler=command_show)

    claim_parser = commands.add_parser("claim", help="atomically claim a task")
    claim_parser.add_argument("number", type=int)
    claim_parser.add_argument("--base", help="base branch for the claim ref")
    claim_parser.add_argument("--execution-id")
    _add_repo_argument(claim_parser)
    claim_parser.set_defaults(handler=command_claim)

    adopt_parser = commands.add_parser("adopt", help="adopt an existing remote task claim")
    adopt_parser.add_argument("number", type=int)
    adopt_parser.add_argument("--execution-id", required=True)
    _add_repo_argument(adopt_parser)
    adopt_parser.set_defaults(handler=command_adopt)

    block_parser = commands.add_parser("block", help="report a task blocker")
    block_parser.add_argument("number", type=int)
    block_parser.add_argument("reason")
    _add_repo_argument(block_parser)
    block_parser.set_defaults(handler=command_block)

    waiting_parser = commands.add_parser(
        "waiting-provider", help="pause a claimed task until provider capacity returns"
    )
    waiting_parser.add_argument("number", type=int)
    waiting_parser.add_argument("reason")
    # Kept optional for old runner scripts; command_waiting_provider ignores
    # this local path and never sends it to GitHub.
    waiting_parser.add_argument("artifact_root", nargs="?")
    waiting_parser.add_argument("--execution-id", required=True)
    _add_repo_argument(waiting_parser)
    waiting_parser.set_defaults(
        handler=lambda args: command_waiting_provider(args)
    )

    resume_parser = commands.add_parser(
        "resume-start", help="remove the provider-wait label before retrying"
    )
    resume_parser.add_argument("number", type=int)
    resume_parser.add_argument("--execution-id", required=True)
    _add_repo_argument(resume_parser)
    resume_parser.set_defaults(handler=command_resume_start)

    cleanup_parser = commands.add_parser(
        "resume-cleanup", help="release one owned provider-resume lease"
    )
    cleanup_parser.add_argument("number", type=int)
    cleanup_parser.add_argument("--execution-id", required=True)
    cleanup_parser.add_argument("--expected-sha", required=True)
    cleanup_parser.add_argument("--lease-ref")
    _add_repo_argument(cleanup_parser)
    cleanup_parser.set_defaults(handler=command_resume_cleanup)

    link_parser = commands.add_parser("link-pr", help="link the task pull request")
    link_parser.add_argument("number", type=int)
    link_parser.add_argument("--execution-id")
    _add_repo_argument(link_parser)
    link_parser.set_defaults(handler=command_link_pr)

    human_parser = commands.add_parser("needs-human", help="pause a task for product-manager action")
    human_parser.add_argument("number", type=int)
    human_parser.add_argument("reason")
    human_parser.add_argument("question")
    human_parser.add_argument("--execution-id", required=True)
    _add_repo_argument(human_parser)
    human_parser.set_defaults(
        handler=lambda args: command_needs_human(args)
    )

    labels_parser = commands.add_parser("init-labels", help="create missing AES labels")
    _add_repo_argument(labels_parser)
    labels_parser.set_defaults(handler=command_init_labels)

    doctor_parser = commands.add_parser("doctor", help="diagnose task-flow anomalies")
    _add_repo_argument(doctor_parser)
    doctor_parser.set_defaults(handler=command_doctor)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the requested command, turning errors into useful output."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except provider_github.GitHubError:
        print("error: GitHub control-plane operation failed", file=sys.stderr)
        return 2
    except task_claim.ClaimError:
        print("error: claim validation failed", file=sys.stderr)
        return 2
    except (ContractError, RuntimeError) as error:
        # RuntimeError messages in this module are local, bounded protocol
        # failures.  Provider subprocess diagnostics are handled above and
        # are never echoed verbatim.
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
