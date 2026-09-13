"""Atomically claim GitHub tasks and record local state."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
import re
import subprocess
import unicodedata
import uuid
from typing import Mapping

from authorization import author_gate_reason
import provider_github
from readiness import is_ready


class ClaimResult(str, Enum):
    """Possible outcomes of an atomic claim attempt."""

    WON = "WON"
    LOST = "LOST"


WON = ClaimResult.WON
LOST = ClaimResult.LOST


class ClaimError(RuntimeError):
    """Indicate that the task no longer meets readiness requirements."""


Environment = Mapping[str, str]


# Execution ids are passed through issue comments, argv and artifact paths.
# Keep one strict, bounded grammar at this boundary instead of accepting an
# arbitrary substring from a comment body.
_EXECUTION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CANONICAL_CLAIM_RE = re.compile(
    r"\s*AES claim: execution_id=(?P<execution_id>[A-Za-z0-9][A-Za-z0-9_.:-]{0,127})\s*\Z"
)
_LEGACY_CLAIM_RE = re.compile(
    r"\s*Claim acquisito dall'esecuzione `(?P<execution_id>[A-Za-z0-9][A-Za-z0-9_.:-]{0,127})`"
    r" alle [^\r\n`]+\.\s*\Z"
)
_KEY_CLAIM_RE = re.compile(
    r"\s*Claim execution_id:\s*(?P<execution_id>[A-Za-z0-9][A-Za-z0-9_.:-]{0,127})\s*\Z",
    re.IGNORECASE,
)


def validate_execution_id(execution_id: str) -> str:
    """Validate the bounded execution-id grammar shared by claim/resume."""

    if not isinstance(execution_id, str) or _EXECUTION_ID_RE.fullmatch(execution_id) is None:
        raise ClaimError(
            "execution_id must start with a letter or digit and contain only "
            "letters, digits, '_', '.', ':' or '-' (max 128 characters)"
        )
    return execution_id


def extract_execution_ids(body: str) -> tuple[str, ...]:
    """Extract only complete, structured claim markers from a comment.

    A marker must occupy an entire line (or the entire body).  In particular,
    ``execution_id`` text in a free-form sentence and prefix matches such as
    ``exec-1`` inside ``exec-10`` are not ownership evidence.
    """

    if not isinstance(body, str):
        return ()
    found: list[str] = []
    for raw_line in body.splitlines() or [body]:
        line = raw_line.strip()
        for pattern in (_CANONICAL_CLAIM_RE, _LEGACY_CLAIM_RE, _KEY_CLAIM_RE):
            match = pattern.fullmatch(line)
            if match:
                value = match.group("execution_id")
                # Every parser branch has the same bounded grammar, but keep
                # this call as the single validation point for future formats.
                try:
                    validate_execution_id(value)
                except ClaimError:
                    continue
                found.append(value)
                break
    return tuple(found)


def _trusted_comment_actor(comment, trusted_actor: str | None = None) -> bool:
    """Accept ownership evidence only from the explicitly trusted token actor.

    ``GITHUB_ACTOR`` identifies the workflow event initiator, not the account
    represented by ``GH_TOKEN``.  Using it here would let an unrelated event
    actor authorize a claim made by a different bot token, so it is
    intentionally never consulted.  Missing actor data or configuration is a
    hard failure rather than a compatibility success.
    """

    author = getattr(comment, "author", None)
    if not isinstance(author, str) or not author.strip():
        return False
    configured_actor = (
        trusted_actor
        if trusted_actor is not None
        else os.environ.get("AES_TRUSTED_ACTOR", "")
    ).strip()
    if not configured_actor or author.strip() != configured_actor:
        return False
    # This is deliberately separate from issue-author authorization: the
    # former authenticates the claim comment's token actor, while the latter
    # decides which users may submit autonomous issues.
    return True


def _github_call(method, *args, environment: Environment | None = None):
    """Call the provider with an explicit environment when one is supplied.

    Omitting the keyword for legacy CLI callers keeps small test seams and
    third-party callers compatible; publisher preflight always supplies the
    scrubbed mapping and therefore never reaches the ambient environment.
    """

    if environment is None:
        return method(*args)
    return method(*args, environment=environment)


def _claim_remote(
    repo_slug: str,
    number: int,
    base_branch: str,
    execution_id: str | None = None,
    enforce_author: bool = False,
    environment: Environment | None = None,
) -> tuple[ClaimResult, str | None, str | None, str | None]:
    """Create the durable remote claim and return its audit details.

    This deliberately does not create a local worktree.  The remote feature
    ref is the ownership lock; a local state marker is only an audit aid.
    """

    # Reject an unsafe caller-owned id before any remote ref or issue write.
    # Generated ids use the same grammar, so the ownership marker is always
    # safe for comments, argv and artifact paths.
    owner = validate_execution_id(execution_id) if execution_id else str(uuid.uuid4())
    task = _github_call(provider_github.get_task, repo_slug, number, environment=environment)
    if enforce_author:
        reason = author_gate_reason(task.author)
        if reason:
            raise ClaimError(reason)
    dependencies = task.contract.depends_on if task.contract else ()
    dep_states = _github_call(
        provider_github.get_states,
        repo_slug,
        dependencies,
        environment=environment,
    )
    ready, reason = is_ready(task, dep_states)
    if not ready:
        raise ClaimError(f"task {repo_slug}#{number} is not ready: {reason}")

    branch = f"feature/{feature_name(number, task.title)}"
    if _github_call(
        provider_github.list_matching_refs,
        repo_slug,
        f"feature/{number}-",
        environment=environment,
    ):
        return LOST, None, None, None
    sha = _github_call(provider_github.base_sha, repo_slug, base_branch, environment=environment)
    if not _github_call(
        provider_github.create_ref,
        repo_slug,
        branch,
        sha,
        environment=environment,
    ):
        return LOST, None, None, None

    claimed_at = datetime.now(timezone.utc).isoformat()
    _github_call(
        provider_github.comment,
        repo_slug,
        number,
        f"AES claim: execution_id={owner}\n"
        f"Claim acquired by execution `{owner}` at {claimed_at}.",
        environment=environment,
    )
    # Make the claim visible first: a crash leaves two diagnostic labels.
    _github_call(
        provider_github.add_labels,
        repo_slug,
        number,
        ("aes:claimed",),
        environment=environment,
    )
    _github_call(
        provider_github.remove_labels,
        repo_slug,
        number,
        ("aes:ready",),
        environment=environment,
    )
    return WON, branch, owner, claimed_at


def claim_remote(
    repo_slug: str,
    number: int,
    base_branch: str,
    execution_id: str | None = None,
    *,
    enforce_author: bool | None = None,
    environment: Environment | None = None,
) -> ClaimResult:
    """Atomically claim a task on GitHub without adopting a local worktree."""

    result, _branch, _owner, _claimed_at = _claim_remote(
        repo_slug,
        number,
        base_branch,
        execution_id,
        os.environ.get("AES_QUEUE_ENABLED", "").strip().lower() == "true"
        if enforce_author is None
        else enforce_author,
        environment,
    )
    return result


def claimed_branch(
    repo_slug: str,
    number: int,
    execution_id: str | None = None,
    *,
    enforce_author: bool = False,
    trusted_actor: str | None = None,
    environment: Environment | None = None,
) -> str:
    """Return the sole remote branch owned by a claimed execution."""

    if execution_id is not None:
        validate_execution_id(execution_id)
    task = _github_call(provider_github.get_task, repo_slug, number, environment=environment)
    if task.state != "open":
        raise ClaimError("claimed task is not open")
    if any(label in task.labels for label in ("aes:blocked", "aes:needs-human", "aes:pr-open")):
        raise ClaimError("claimed task is blocked, needs human, or already has an open PR")
    if "aes:claimed" not in task.labels:
        raise ClaimError("claimed task has no aes:claimed label")
    if enforce_author:
        reason = author_gate_reason(task.author)
        if reason:
            raise ClaimError(reason)

    # The ref is the durable ownership lock.  Labels and comments are audit
    # mirrors and may be temporarily stale if a runner dies between the ref
    # creation and the GitHub issue update.  Requiring ``aes:claimed`` here
    # would make the compatibility ``claim`` command unable to adopt a ref
    # that it has just created, and would contradict ADR 0015.
    refs = _github_call(
        provider_github.list_matching_refs,
        repo_slug,
        f"feature/{number}-",
        environment=environment,
    )
    if len(refs) != 1:
        found = ", ".join(refs) or "(none)"
        raise ClaimError(f"claimed task has {len(refs)} feature refs: {found}")
    if execution_id:
        comments = _github_call(
            provider_github.list_issue_comments,
            repo_slug,
            number,
            environment=environment,
        )
        matching_comments = [
            comment
            for comment in comments
            if execution_id in extract_execution_ids(comment.body)
            and _trusted_comment_actor(comment, trusted_actor)
        ]
        if not matching_comments:
            raise ClaimError("claim is owned by a different execution_id")
    prefix = "refs/heads/"
    ref = refs[0]
    return ref[len(prefix) :] if ref.startswith(prefix) else ref


def validate_claim(
    repo_slug: str,
    number: int,
    execution_id: str,
    *,
    enforce_author: bool = False,
    trusted_actor: str | None = None,
    environment: Environment | None = None,
) -> str:
    """Revalidate live ownership immediately before a worker side effect."""

    validate_execution_id(execution_id)
    return claimed_branch(
        repo_slug,
        number,
        execution_id,
        enforce_author=enforce_author,
        trusted_actor=trusted_actor,
        environment=environment,
    )


def acquire_resume_lease(
    repo_slug: str,
    number: int,
    execution_id: str,
    claim_branch: str,
    *,
    environment: Environment | None = None,
) -> provider_github.ResumeLease | None | bool:
    """Acquire the remote CAS lock for one waiting execution."""

    validate_execution_id(execution_id)
    return _github_call(
        provider_github.acquire_resume_lease,
        repo_slug,
        number,
        claim_branch,
        execution_id,
        environment=environment,
    )


def release_resume_lease(
    repo_slug: str,
    number: int,
    execution_id: str,
    expected_sha: str | None = None,
    lease_ref: str | None = None,
    *,
    environment: Environment | None = None,
) -> bool:
    """Release a waiting-execution CAS lock after terminal worker cleanup."""

    validate_execution_id(execution_id)
    return _github_call(
        provider_github.release_resume_lease,
        repo_slug,
        number,
        execution_id,
        expected_sha,
        lease_ref,
        environment=environment,
    )


def _title_slug(title: str) -> str:
    """Convert the title into a stable, readable branch segment."""

    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return slug[:60].rstrip("-") or "task"


def feature_name(number: int, title: str) -> str:
    """Return the branch name without the issue prefix."""

    return f"{number}-{_title_slug(title)}"


def _record_local_state(
    execution_id: str,
    repo_slug: str,
    number: int,
    branch: str,
    claimed_at: str,
) -> None:
    """Save the local claim in a directory entirely ignored by Git."""

    task_dir = _repository_root() / ".agent" / "tasks"
    task_dir.mkdir(parents=True, exist_ok=True)
    marker = task_dir / ".gitignore"
    if not marker.exists():
        marker.write_text("*\n", encoding="utf-8")

    state = {
        "execution_id": execution_id,
        "number": number,
        "repo_slug": repo_slug,
        "branch": branch,
        "claimed_at": claimed_at,
    }
    path = task_dir / "current.json"
    temporary = task_dir / "current.json.tmp"
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _repository_root() -> Path:
    """Resolve the repository root containing the current directory."""

    try:
        result = subprocess.run(
            ("git", "rev-parse", "--show-toplevel"),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise ClaimError(f"failed to execute git: {error}") from error
    if result.returncode != 0 or not result.stdout.strip():
        detail = result.stderr.strip() or f"git exited with status {result.returncode}"
        raise ClaimError(f"failed to resolve repository root: {detail}")
    return Path(result.stdout.strip())


def claim(
    repo_slug: str,
    number: int,
    base_branch: str,
    execution_id: str | None = None,
    enforce_author: bool = False,
    environment: Environment | None = None,
) -> ClaimResult:
    """Try to claim a task and record effects only after winning.

    Scanning by number avoids duplicate claims when the title changes. A known
    race remains if the title changes between scan and creation: ``doctor``
    detects divergent refs, while claim does not attempt to resolve them.
    """

    result, branch, owner, claimed_at = _claim_remote(
        repo_slug, number, base_branch, execution_id, enforce_author, environment
    )
    if result is LOST:
        return result
    assert branch is not None and owner is not None and claimed_at is not None
    _record_local_state(owner, repo_slug, number, branch, claimed_at)
    return result
