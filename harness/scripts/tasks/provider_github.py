"""Access GitHub for task flow through the ``gh`` command."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import subprocess
from datetime import datetime
import sys
from typing import Any, Iterable, Mapping
from urllib.parse import quote

from contract import OPEN_MARKER, parse
from dispatch import OPEN_MARKER as DISPATCH_OPEN_MARKER, DispatchError, parse as parse_dispatch
from readiness import Task


class GitHubError(RuntimeError):
    """Indicate a GitHub error other than a claim conflict."""


# The publisher passes a scrubbed environment explicitly for every control
# plane read/write.  Keeping this type at the provider boundary makes it hard
# to accidentally fall back to a caller's ambient provider credentials.
Environment = Mapping[str, str]


@dataclass(frozen=True)
class IssueComment:
    """Body and publication time of a GitHub comment."""

    body: str
    created_at: datetime
    # GitHub's REST response exposes this as ``user.login``.  Older fakes and
    # callers do not provide it, hence the optional default; ownership checks
    # enforce it whenever the provider gives us the value.
    author: str | None = None


RESUME_LEASE_PREFIX = "aes/resume/"
_SHA_RE = re.compile(r"[0-9a-f]{40}\Z", re.IGNORECASE)


@dataclass(frozen=True)
class ResumeLease:
    """Remote resume lock identity passed from planner to worker.

    Git refs do not carry arbitrary metadata.  The expected object id is
    therefore part of the lease identity and acts as a compare-and-swap
    guard when the worker performs cleanup.
    """

    ref: str
    expected_sha: str
    owner: str


def resume_lease_branch(number: int) -> str:
    """Return the deterministic branch used as a resume CAS lock."""

    if not isinstance(number, int) or number <= 0:
        raise GitHubError("issue number must be a positive integer")
    return f"{RESUME_LEASE_PREFIX}{number}"


def current_repo(*, environment: Environment | None = None) -> str:
    """Resolve the GitHub repository associated with the current directory."""

    return _require_success(
        _run("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner", environment=environment)
    )


def _run(*args: str, environment: Environment | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``gh`` without interpreting arguments through a shell."""

    try:
        return subprocess.run(
            ("gh", *args),
            capture_output=True,
            text=True,
            check=False,
            # ``None`` retains the historical CLI behaviour.  The publisher
            # and claim preflight always provide a scrubbed mapping here.
            env=dict(environment) if environment is not None else None,
        )
    except OSError as error:
        raise GitHubError(f"failed to execute gh: {error}") from error


def _result_text(result: subprocess.CompletedProcess[str]) -> str:
    """Combine useful output from a failed command for diagnostics."""

    return "\n".join(part.strip() for part in (result.stderr, result.stdout) if part.strip())


def _require_success(result: subprocess.CompletedProcess[str]) -> str:
    """Return stdout or raise an error retaining the detail."""

    if result.returncode != 0:
        detail = _result_text(result) or f"gh exited with status {result.returncode}"
        raise GitHubError(detail)
    return result.stdout.strip()


def _json_output(result: subprocess.CompletedProcess[str]) -> Any:
    """Decode the JSON response from a successful ``gh`` command."""

    output = _require_success(result)
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise GitHubError("gh returned invalid JSON") from error


def _flatten_paginated(payload: Any, description: str) -> list[Any]:
    """Flatten the arrays emitted by ``gh api --paginate --slurp``.

    ``--paginate`` alone can emit one JSON document per page, which is not a
    valid single JSON document for ``json.loads``.  ``--slurp`` wraps those
    page arrays in an outer array; accepting a flat array too keeps the seam
    compatible with small fakes and older gh versions.
    """

    if not isinstance(payload, list):
        raise GitHubError(f"gh returned an invalid {description} response")
    if not payload:
        return []
    if all(isinstance(page, list) for page in payload):
        flattened: list[Any] = []
        for page in payload:
            flattened.extend(page)
        return flattened
    if any(isinstance(page, list) for page in payload):
        raise GitHubError(f"gh returned an invalid {description} pagination response")
    return payload


def _task_from_json(repo_slug: str, data: dict[str, Any]) -> Task:
    """Convert issue fields into the pure readiness model."""

    body = data.get("body") or ""
    parsed_contract = None
    contract_error: str | None = None
    if OPEN_MARKER in body:
        try:
            parsed_contract = parse(body)
        except ValueError as error:
            contract_error = str(error)

    parsed_dispatch = None
    dispatch_error: str | None = None
    if DISPATCH_OPEN_MARKER in body:
        try:
            parsed_dispatch = parse_dispatch(body)
        except DispatchError as error:
            dispatch_error = str(error)
    else:
        dispatch_error = "dispatch block not found"

    labels = tuple(
        label["name"] if isinstance(label, dict) else str(label)
        for label in data.get("labels", ())
    )
    # ``gh issue view --json`` calls these fields ``author``/``createdAt``;
    # the paginated REST endpoint calls them ``user``/``created_at``.
    raw_author = data.get("author") or data.get("user")
    if isinstance(raw_author, dict):
        author = str(raw_author.get("login") or "")
    elif isinstance(raw_author, str):
        author = raw_author
    else:
        author = ""
    return Task(
        number=int(data["number"]),
        repo_slug=repo_slug,
        title=str(data["title"]),
        state=str(data["state"]).lower(),
        labels=labels,
        contract=parsed_contract,
        dispatch=parsed_dispatch,
        created_at=_created_at(data.get("createdAt") or data.get("created_at")),
        body=body,
        dispatch_error=dispatch_error or contract_error,
        author=author,
    )


def _created_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def list_tasks(
    repo_slug: str,
    labels: Iterable[str],
    *,
    environment: Environment | None = None,
) -> list[Task]:
    """List all repository tasks with the requested labels.

    ``gh issue list --limit 100`` deliberately leaves the dispatcher blind as
    soon as the queue exceeds the first page. The REST endpoint, invoked with
    ``--paginate --slurp``, follows every ``next`` link and returns one JSON
    list; ``_flatten_paginated`` keeps the seam testable with fakes that return
    a single page too.
    """

    required = tuple(labels)
    query = ["state=all", "per_page=100"]
    if required:
        # GitHub expects comma-separated labels; encode colons and spaces but
        # keep commas as separators in the query value.
        query.append(f"labels={quote(','.join(required), safe=',')}")
    endpoint = f"repos/{repo_slug}/issues?{'&'.join(query)}"
    rows = _flatten_paginated(
        _json_output(_run("api", endpoint, "--paginate", "--slurp", environment=environment)),
        "issues",
    )
    # The REST issues endpoint also returns pull requests.  They are not work
    # items and ``gh issue list`` used to omit them, so preserve that contract.
    issues = [
        row for row in rows
        if isinstance(row, dict) and "pull_request" not in row
    ]
    return [_task_from_json(repo_slug, row) for row in issues]


def get_task(
    repo_slug: str,
    number: int,
    *,
    environment: Environment | None = None,
) -> Task:
    """Read an issue and convert it into a task."""

    data = _json_output(
        _run(
            "issue",
            "view",
            str(number),
            "--repo",
            repo_slug,
            "--json",
            "number,title,state,labels,body,author,createdAt",
            environment=environment,
        )
    )
    return _task_from_json(repo_slug, data)


def _is_not_found(result: subprocess.CompletedProcess[str]) -> bool:
    """Recognize a GitHub 404 without absorbing other errors."""

    for raw_payload in (result.stderr.strip(), result.stdout.strip()):
        try:
            payload = json.loads(raw_payload)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict) and str(payload.get("message", "")).lower() == "not found":
            return True
    return "http 404" in _result_text(result).lower()


def get_states(
    repo_slug: str,
    dep_keys: Iterable[str],
    *,
    environment: Environment | None = None,
) -> dict[str, str]:
    """Read states using qualified ``owner/repo#N`` keys throughout."""

    states: dict[str, str] = {}
    for dependency in dep_keys:
        if "#" in dependency:
            dependency_repo, number = dependency.rsplit("#", 1)
        else:
            dependency_repo, number = repo_slug, dependency
        key = f"{dependency_repo}#{number}"
        result = _run(
            "api",
            f"repos/{dependency_repo}/issues/{number}",
            "--jq",
            ".state",
            environment=environment,
        )
        if result.returncode != 0 and _is_not_found(result):
            continue
        states[key] = _require_success(result).lower()
    return states


def base_sha(
    repo_slug: str,
    branch: str,
    *,
    environment: Environment | None = None,
) -> str:
    """Return the SHA pointed to by the base branch."""

    encoded_branch = quote(branch, safe="")
    return _require_success(
        _run(
            "api",
            f"repos/{repo_slug}/git/ref/heads/{encoded_branch}",
            "--jq",
            ".object.sha",
            environment=environment,
        )
    )


def ref_sha(
    repo_slug: str,
    branch: str,
    *,
    environment: Environment | None = None,
) -> str | None:
    """Read a branch ref object id, returning ``None`` only for a 404."""

    encoded_branch = quote(branch, safe="/")
    result = _run(
        "api",
        f"repos/{repo_slug}/git/ref/heads/{encoded_branch}",
        "--jq",
        ".object.sha",
        environment=environment,
    )
    if result.returncode != 0 and _is_not_found(result):
        return None
    value = _require_success(result)
    if not _SHA_RE.fullmatch(value):
        raise GitHubError("gh returned an invalid ref object id")
    return value


def list_matching_refs(
    repo_slug: str,
    branch_prefix: str,
    *,
    environment: Environment | None = None,
) -> tuple[str, ...]:
    """List remote branches beginning with the given prefix."""

    encoded_prefix = quote(branch_prefix, safe="/")
    rows = _json_output(
        _run(
            "api",
            f"repos/{repo_slug}/git/matching-refs/heads/{encoded_prefix}",
            "--paginate",
            "--slurp",
            environment=environment,
        )
    )
    rows = _flatten_paginated(rows, "matching refs")

    refs: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("ref"), str):
            raise GitHubError("gh returned an invalid matching refs response")
        refs.append(row["ref"])
    return tuple(refs)


def _contains_existing_ref_error(result: subprocess.CompletedProcess[str]) -> bool:
    """Distinguish an existing ref from other POST failures."""

    expected = "reference already exists"
    for raw_payload in (result.stderr.strip(), result.stdout.strip()):
        try:
            payload = json.loads(raw_payload)
        except (json.JSONDecodeError, TypeError):
            continue
        messages = [payload.get("message", "")] if isinstance(payload, dict) else []
        if isinstance(payload, dict):
            messages.extend(
                error.get("message", "")
                for error in payload.get("errors", ())
                if isinstance(error, dict)
            )
        if any(str(message).lower() == expected for message in messages):
            return True

    detail = _result_text(result).lower()
    return expected in detail and "http 422" in detail


def create_ref(
    repo_slug: str,
    branch: str,
    sha: str,
    *,
    environment: Environment | None = None,
) -> bool:
    """Create the atomic ref; false means exclusively that it already exists."""

    result = _run(
        "api",
        "-X",
        "POST",
        f"repos/{repo_slug}/git/refs",
        "-f",
        f"ref=refs/heads/{branch}",
        "-f",
        f"sha={sha}",
        environment=environment,
    )
    if result.returncode == 0:
        return True
    if _contains_existing_ref_error(result):
        return False
    _require_success(result)
    raise AssertionError("unreachable")


def add_labels(
    repo_slug: str,
    number: int,
    labels: Iterable[str],
    *,
    environment: Environment | None = None,
) -> None:
    """Add labels to an issue."""

    for label in labels:
        _require_success(
            _run(
                "issue", "edit", str(number), "--repo", repo_slug, "--add-label", label,
                environment=environment,
            )
        )


def remove_labels(
    repo_slug: str,
    number: int,
    labels: Iterable[str],
    *,
    environment: Environment | None = None,
) -> None:
    """Remove labels from an issue."""

    for label in labels:
        _require_success(
            _run(
                "issue", "edit", str(number), "--repo", repo_slug, "--remove-label", label,
                environment=environment,
            )
        )


def comment(
    repo_slug: str,
    number: int,
    body: str,
    *,
    environment: Environment | None = None,
) -> None:
    """Write an audit comment on the issue."""

    _require_success(
        _run(
            "issue", "comment", str(number), "--repo", repo_slug, "--body", body,
            environment=environment,
        )
    )


def list_issue_comments(
    repo_slug: str,
    number: int,
    *,
    environment: Environment | None = None,
) -> tuple[IssueComment, ...]:
    """Read body and creation time for an issue's comments."""

    rows = _json_output(
        _run(
            "api",
            f"repos/{repo_slug}/issues/{number}/comments",
            "--paginate",
            "--slurp",
            environment=environment,
        )
    )
    rows = _flatten_paginated(rows, "issue comments")
    comments: list[IssueComment] = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("body"), str)
            or not isinstance(row.get("created_at"), str)
        ):
            raise GitHubError("gh returned an invalid issue comments response")
        try:
            created_at = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
        except ValueError as error:
            raise GitHubError("gh returned an invalid issue comment date") from error
        raw_author = row.get("user") or row.get("author")
        if isinstance(raw_author, dict):
            author = str(raw_author.get("login") or "") or None
        elif isinstance(raw_author, str):
            author = raw_author or None
        else:
            author = None
        comments.append(IssueComment(row["body"], created_at, author))
    return tuple(comments)


def acquire_resume_lease(
    repo_slug: str,
    number: int,
    claim_branch: str,
    owner: str = "unknown",
    *,
    environment: Environment | None = None,
) -> ResumeLease | None:
    """Acquire the per-issue resume lock with GitHub's atomic ref create.

    The feature ref remains the task ownership lock.  This second, stable ref
    serializes retries of a waiting execution: concurrent dispatchers race on
    one POST and exactly one receives ``True``.  The worker must call
    :func:`release_resume_lease` after the resumed execution reaches a terminal
    state; the lease is never inferred from an issue comment.
    """

    if not claim_branch or claim_branch.startswith("refs/"):
        # ``base_sha`` accepts a normal branch name and safely URL-encodes it;
        # accepting a full ref here would make it too easy to lock an
        # unrelated object by accident.
        raise GitHubError("claim_branch must be a branch name")
    sha = base_sha(repo_slug, claim_branch, environment=environment)
    acquired = create_ref(
        repo_slug,
        resume_lease_branch(number),
        sha,
        environment=environment,
    )
    if not acquired:
        return None
    return ResumeLease(resume_lease_branch(number), sha, owner)


def release_resume_lease(
    repo_slug: str,
    number: int,
    owner: str | None = None,
    expected_sha: str | None = None,
    lease_ref: str | None = None,
    *,
    environment: Environment | None = None,
) -> bool:
    """Release a resume CAS ref after a compare-and-swap ownership check.

    Deleting a missing ref is treated as an idempotent no-op.  The caller is a
    ``owner`` is carried for audit and is validated by the claim layer before
    this provider primitive is called.  GitHub exposes no ref-owner field, so
    the immutable expected object id is the provider-side ownership guard.
    A stale worker must never delete a newer lease.
    """

    del owner
    if expected_sha is None or _SHA_RE.fullmatch(expected_sha) is None:
        return False
    expected_ref = resume_lease_branch(number)
    if lease_ref is not None and lease_ref != expected_ref:
        return False
    current_sha = ref_sha(repo_slug, expected_ref, environment=environment)
    if current_sha is None or current_sha.lower() != expected_sha.lower():
        return False
    encoded_branch = quote(expected_ref, safe="/")
    result = _run(
        "api",
        "-X",
        "DELETE",
        f"repos/{repo_slug}/git/refs/heads/{encoded_branch}",
        environment=environment,
    )
    if result.returncode == 0:
        return True
    if _is_not_found(result):
        return False
    _require_success(result)
    raise AssertionError("unreachable")


def latest_branch_commit_date(repo_slug: str, branch: str) -> datetime:
    """Return the UTC date of the branch's latest commit."""

    raw = _require_success(
        _run(
            "api",
            f"repos/{repo_slug}/commits",
            "-f",
            f"sha={branch}",
            "-f",
            "per_page=1",
            "--jq",
            ".[0].commit.committer.date",
        )
    )
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise GitHubError("gh returned an invalid branch commit date") from error


def find_pr_for_branch(repo_slug: str, branch: str) -> int | None:
    """Return the number of the first PR associated with the branch, if any."""

    rows = _json_output(
        _run(
            "pr",
            "list",
            "--repo",
            repo_slug,
            "--head",
            branch,
            "--state",
            "open",
            "--limit",
            "1",
            "--json",
            "number",
        )
    )
    return int(rows[0]["number"]) if rows else None


def list_labels(repo_slug: str) -> tuple[str, ...]:
    """List label names configured in the repository."""

    rows = _json_output(
        _run(
            "label",
            "list",
            "--repo",
            repo_slug,
            "--limit",
            "100",
            "--json",
            "name",
        )
    )
    if not isinstance(rows, list):
        raise GitHubError("gh returned an invalid labels response")
    if len(rows) == 100:
        print("warning: label list potrebbe essere troncato (limite 100)", file=sys.stderr)

    labels: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise GitHubError("gh returned an invalid labels response")
        labels.append(row["name"])
    return tuple(labels)


def create_label(repo_slug: str, name: str, color: str, description: str) -> None:
    """Create a task-flow label in the repository."""

    _require_success(
        _run(
            "label",
            "create",
            name,
            "--repo",
            repo_slug,
            "--color",
            color,
            "--description",
            description,
        )
    )
