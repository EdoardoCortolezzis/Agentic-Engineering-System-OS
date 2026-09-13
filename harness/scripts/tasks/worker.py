"""Single-issue worker for the self-hosted Codex runner.

All dynamic issue data is passed as argv or environment values.  The issue
body is read by the agent through GitHub and is never interpolated into a
shell command.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

import claim as task_claim
from issue_orchestrator import (
    EXIT_FAILED,
    EXIT_NEEDS_HUMAN,
    EXIT_VERIFIED,
    EXIT_WAITING_PROVIDER,
    IssueOrchestrator,
)
from model_router import REFRESH_FAILED, ModelRouter, _quota_cache_availability
from orchestration import ArtifactError, OrchestrationError
from quota import QuotaCache, QuotaConfigError, QuotaError
from quota_collectors import collect_codex_quota
import provider_github
import publisher
from dispatch import validate
from dispatcher import QueueConfigError


class WorkerError(RuntimeError):
    """Worker prerequisites or execution failed closed."""


_AGENT_AUTH_ENV = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "AES_QUEUE_TOKEN",
        "AES_SYNC_TOKEN",
        "ACTIONS_RUNTIME_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CODEX_AUTH",
        "CODEX_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "GIT_ASKPASS",
        "SSH_AUTH_SOCK",
    }
)


def _control_plane_environment() -> dict[str, str]:
    """Return the only environment allowed for authenticated ``gh`` calls."""

    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        raise WorkerError("missing required worker configuration: GH_TOKEN")
    environment = os.environ.copy()
    for name in tuple(environment):
        upper_name = name.upper()
        if name != "GH_TOKEN" and (
            name in _AGENT_AUTH_ENV
            or upper_name.endswith(("_TOKEN", "_API_KEY", "_SECRET", "_PASSWORD"))
            or upper_name.startswith(("GH_", "GIT_", "AWS_", "AZURE_", "OPENAI_", "ANTHROPIC_"))
            or upper_name.endswith("_PROXY")
            or upper_name in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
        ):
            environment.pop(name, None)
    environment["GH_TOKEN"] = token
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def _verify_authenticated_actor() -> None:
    """Fail closed unless the sanitized GitHub token is the trusted actor."""

    trusted_actor = _required("AES_TRUSTED_ACTOR")
    result = subprocess.run(
        ("gh", "api", "user", "--jq", ".login"),
        env=_control_plane_environment(),
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    if result.returncode != 0 or result.stdout.strip() != trusted_actor:
        raise WorkerError("authenticated GitHub actor does not match AES_TRUSTED_ACTOR")


def _agent_environment() -> dict[str, str]:
    """Return a child environment with ambient GitHub/provider authority removed."""

    environment = os.environ.copy()
    for name in tuple(environment):
        if name in _AGENT_AUTH_ENV or name.endswith(("_TOKEN", "_API_KEY", "_SECRET")):
            environment.pop(name, None)
    # Prevent an inherited checkout credential helper from silently granting
    # the model GitHub authority even when token variables were scrubbed.
    for name in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        environment.pop(name, None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise WorkerError(f"missing required worker configuration: {name}")
    return value


def _worktree(root: Path, issue: int) -> Path:
    result = subprocess.run(
        ("git", "worktree", "list", "--porcelain"),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise WorkerError("cannot inspect claimed worktree")
    current: Path | None = None
    matches: list[Path] = []
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree "))
        elif line.startswith("branch refs/heads/feature/") and current is not None:
            branch = line.removeprefix("branch refs/heads/feature/")
            if branch.startswith(f"{issue}-"):
                matches.append(current)
    if len(matches) != 1:
        if not matches:
            raise WorkerError(f"claimed worktree for issue {issue} was not found")
        raise WorkerError(
            f"multiple worktrees match issue {issue}; exact branch identity is required"
        )
    return matches[0]


def _worktree_branch(worktree: Path) -> str:
    result = subprocess.run(
        ("git", "-C", str(worktree), "branch", "--show-current"),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise WorkerError("cannot determine adopted worktree branch")
    return result.stdout.strip()


def _worktree_head(worktree: Path) -> str:
    """Return the committed worktree tip used for orchestration integrity."""

    result = subprocess.run(
        ("git", "-C", str(worktree), "rev-parse", "--verify", "HEAD^{commit}"),
        env=_agent_environment(),
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    head = result.stdout.strip()
    if result.returncode != 0 or not head or any(
        character not in "0123456789abcdefABCDEF" for character in head
    ):
        raise WorkerError("cannot determine adopted worktree HEAD")
    return head


def _validate_adopted_head(repo: str, branch: str, worktree: Path) -> None:
    """Require the adopted local tip to match the durable claimed ref."""

    remote_head = provider_github.ref_sha(repo, branch)
    if remote_head is None:
        raise WorkerError("claimed feature ref disappeared before orchestration")
    local_head = _worktree_head(worktree)
    if local_head.lower() != remote_head.lower():
        raise WorkerError(
            "adopted worktree HEAD does not match the durable claimed feature ref"
        )


def _contract_scope(task: provider_github.Task, worktree: Path, base_branch: str) -> None:
    """Enforce repository and changed-path boundaries before publication."""

    contract = task.contract
    if contract is None:
        raise WorkerError("contract is missing before trusted publication")
    declared_repo = (contract.repo or task.repo_slug).strip()
    if declared_repo != task.repo_slug:
        raise WorkerError(
            f"contract repo {declared_repo!r} does not match task repository {task.repo_slug!r}"
        )
    allowed: tuple[str, ...] = tuple(
        _validate_relative_path(path) for path in contract.paths if path.strip()
    )
    if not allowed:
        raise WorkerError("contract paths are empty before trusted publication")
    def git_names(*args: str) -> list[str]:
        result = subprocess.run(
            ("git", "-C", str(worktree), *args),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise WorkerError("git command failed while checking contract scope")
        return [name for name in result.stdout.splitlines() if name]

    changed = git_names("diff", "--name-only", f"origin/{base_branch}...HEAD")
    changed.extend(git_names("ls-files", "--others", "--exclude-standard"))
    if not changed:
        raise WorkerError("worker produced no changed files")

    def within_scope(name: str) -> bool:
        normalized = name.strip("/")
        return any(normalized == path or normalized.startswith(path + "/") for path in allowed)

    changed = [_validate_relative_path(name) for name in changed]
    for name in changed:
        _reject_unsafe_worktree_entry(worktree, name)
    outside = [name for name in changed if not within_scope(name)]
    if outside:
        raise WorkerError(
            "worker changed paths outside the issue contract: " + ", ".join(sorted(outside))
        )


def _run_tasks(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    script = root / "harness" / "scripts" / "tasks" / "tasks.py"
    return subprocess.run((sys.executable, str(script), *args), cwd=root, text=True, check=False)


def _write_task_brief(
    root: Path,
    task: provider_github.Task,
    execution_id: str,
    *,
    artifact_root: Path | None = None,
) -> Path:
    """Write the trusted, read-only issue context consumed by the model child.

    The child intentionally has no GitHub credentials.  Supplying the issue
    body and validated queue metadata through a local JSON brief avoids
    instructing it to call ``gh`` against a private repository.  Issue body
    text remains untrusted data; the adapter prompt must not treat it as
    higher-priority instructions.
    """

    brief_dir = artifact_root or (root / ".agent" / "tasks")
    brief_dir.mkdir(parents=True, exist_ok=True)
    descriptor = {
        "schema": "aes.worker-task-brief.v1",
        "execution_id": execution_id,
        "repo": task.repo_slug,
        "issue": task.number,
        "title": task.title,
        "state": task.state,
        "author": task.author,
        "labels": list(task.labels),
        "body": task.body,
        "contract": (
            {
                "repo": task.contract.repo,
                "paths": list(task.contract.paths),
                "constraints": task.contract.constraints,
                "depends_on": list(task.contract.depends_on),
                "done": list(task.contract.done),
            }
            if task.contract is not None
            else None
        ),
        "dispatch": (
            {
                "dispatch": task.dispatch.dispatch,
                "priority": task.dispatch.priority,
                "not_before": (
                    task.dispatch.not_before.isoformat()
                    if task.dispatch.not_before is not None
                    else None
                ),
                "budget": task.dispatch.budget,
            }
            if task.dispatch is not None
            else None
        ),
    }
    descriptor_file = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=brief_dir,
        prefix=f"brief-{task.number}-",
        suffix=".json",
        delete=False,
    )
    path = Path(descriptor_file.name)
    try:
        os.chmod(path, 0o600)
        json.dump(descriptor, descriptor_file, ensure_ascii=False, indent=2)
        descriptor_file.write("\n")
        descriptor_file.flush()
        return path
    except BaseException:
        # A serialization/write interruption must not leave issue contents in
        # the shared workspace.  KeyboardInterrupt/SystemExit are included so
        # the cleanup guarantee also holds for the standard termination path.
        try:
            path.unlink()
        except OSError:
            pass
        raise
    finally:
        descriptor_file.close()


def _artifact_root(root: Path, execution_id: str, *, resume: bool = False) -> Path:
    """Return the trusted per-execution control-plane directory.

    The root is the dispatcher checkout, never the adopted model worktree.
    Keeping the execution id in one deterministic path lets a scheduled retry
    resume the same ledger without trusting a path supplied by an issue body.
    """

    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    if (
        not execution_id
        or execution_id[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        or any(
            char not in allowed
            for char in execution_id
        )
        or len(execution_id) > 128
    ):
        raise WorkerError("execution_id contains unsafe characters")
    root = root.resolve(strict=True)
    agent = root / ".agent"
    tasks_directory = agent / "tasks"
    artifacts = tasks_directory / "artifacts"
    if agent.is_symlink() or tasks_directory.is_symlink():
        raise WorkerError("trusted artifact root path must not contain symlinks")
    if artifacts.is_symlink() or (artifacts.exists() and not artifacts.is_dir()):
        raise WorkerError("trusted artifact root parent is not a directory")
    if not resume:
        artifacts.mkdir(parents=True, exist_ok=True)
    elif not artifacts.is_dir():
        raise WorkerError("cannot resume without the trusted artifact root")
    target = artifacts / execution_id
    if target.is_symlink():
        raise WorkerError("trusted artifact root must not be a symlink")
    if resume:
        if not target.is_dir():
            raise WorkerError("cannot resume without the existing execution artifact root")
    else:
        if target.exists():
            raise WorkerError("execution artifact already exists for execution_id")
        target.mkdir()
    if not target.is_dir():
        raise WorkerError("trusted artifact root is not a directory")
    return target


def _quota_cache(root: Path) -> QuotaCache:
    """Open the shared quota cache configured outside the checkout.

    A worker checkout is disposable and each matrix item can land on a
    different runner.  Requiring an explicit PATH/ROOT pair makes the cache a
    runner-managed persistent resource instead of silently creating one under
    ``.agent`` and losing subscription observations at the next checkout.
    """

    raw_path = os.environ.get("AES_QUOTA_CACHE_PATH", "").strip()
    raw_root = os.environ.get("AES_QUOTA_CACHE_ROOT", "").strip()
    if not raw_path or not raw_root:
        raise QuotaConfigError(
            "AES_QUOTA_CACHE_PATH and AES_QUOTA_CACHE_ROOT are required for workers"
        )
    checkout = root.resolve(strict=True)
    cache = QuotaCache(raw_path, root=raw_root)
    # Compare canonical real paths as well as the lexical forms.  A runner
    # state directory reached through a symlink ancestor can look external in
    # the checkout but resolve into ``.agent`` (or any other checkout path).
    # Such a cache would both persist trusted state in model-controlled files
    # and bypass the disposable-checkout boundary.
    for candidate, label in (
        (cache.root, "quota cache root"),
        (cache.path, "quota cache path"),
        (cache.real_root, "quota cache real root"),
        (cache.real_path, "quota cache real path"),
    ):
        try:
            candidate.relative_to(checkout)
        except ValueError:
            continue
        raise QuotaConfigError(f"{label} must be outside the worker checkout")
    return cache


def _refresh_codex_quota(cache: QuotaCache) -> str | None:
    """Refresh Codex quota with the collector's bounded protocol.

    Claude's statusline sidecar already updates this same cache.  A failed
    Codex observation is retained as a safe diagnostic and never turns into a
    provider invocation with unverified quota.
    """

    try:
        collect_codex_quota(cache=cache)
    except (QuotaError, OSError) as error:
        return type(error).__name__
    return None


def _router_availability(cache_path: Path, *, codex_refresh_error: str | None):
    """Return quota availability with this invocation's Codex refresh bound.

    A failed refresh must not be masked by an otherwise fresh/stale cache
    snapshot.  ``refresh_failed`` is a retryable wait, not proof of quota
    exhaustion: the router stops before invoking either provider and the
    scheduler retries the same ledger later.
    """

    if codex_refresh_error:
        def blocked(_provider: str, _role: str):
            return REFRESH_FAILED

        return blocked
    return _quota_cache_availability(cache_path)


def _resume_brief(artifact_root: Path, execution_id: str) -> Path:
    """Load the original trusted brief for a resumed execution.

    Resume is intentionally strict: no brief, ledger, or execution directory
    is created on this path.  The persisted state metadata is the source of
    truth for the brief location and must remain inside ``artifact_root``.
    """

    state_directory = artifact_root / ".agent" / "orchestration" / execution_id
    state_path = state_directory / "state.json"
    for path in (
        artifact_root / ".agent",
        artifact_root / ".agent" / "orchestration",
        state_directory,
    ):
        if path.is_symlink():
            raise WorkerError("existing execution state path must not contain symlinks")
    if state_path.is_symlink() or not state_path.is_file():
        raise WorkerError("cannot resume without the existing execution state")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WorkerError("existing execution state is unreadable") from error
    if not isinstance(state, dict) or state.get("execution_id") != execution_id:
        raise WorkerError("existing execution state does not match execution_id")
    metadata = state.get("metadata")
    brief_value = metadata.get("brief_path") if isinstance(metadata, dict) else None
    if not isinstance(brief_value, str) or not brief_value.strip():
        raise WorkerError("existing execution state has no trusted brief")
    brief = Path(brief_value)
    if brief.is_symlink():
        raise WorkerError("existing trusted brief must not be a symlink")
    try:
        brief = brief.resolve(strict=True)
        artifact = artifact_root.resolve(strict=True)
        brief.relative_to(artifact)
    except (OSError, ValueError) as error:
        raise WorkerError("existing trusted brief is outside the artifact root") from error
    if not brief.is_file():
        raise WorkerError("existing trusted brief is unavailable")
    return brief


def _mark_waiting_provider(
    root: Path,
    repo: str,
    issue: int,
    execution_id: str,
    reason: str,
) -> None:
    result = _run_tasks(
        root,
        "waiting-provider",
        str(issue),
        reason,
        "--repo",
        repo,
        "--execution-id",
        execution_id,
    )
    if result.returncode != 0:
        raise WorkerError("failed to persist aes:waiting-provider")


def _public_outcome_reason(status: str, execution_id: str) -> str:
    """Return a bounded public reason while keeping model detail in artifacts."""

    return f"AES orchestration outcome: {status}; execution_id={execution_id}"[:240]


def _resume_lease(
    repo: str,
    issue: int,
    execution_id: str,
    *,
    lease_ref: str | None = None,
    expected_sha: str | None = None,
    lease_owner: str | None = None,
) -> provider_github.ResumeLease:
    """Validate the complete lease identity received from the dispatcher.

    A worker must never discover a lease by reading a remote SHA itself.  The
    dispatcher owns the atomic acquisition and passes the exact compare-and-
    swap identity through the matrix; accepting a partial identity would let a
    standalone invocation resume (and later delete) another execution's lease.
    """

    expected_ref = provider_github.resume_lease_branch(issue)
    if not lease_ref or not expected_sha or not lease_owner:
        raise WorkerError(
            "resume requires lease_ref, lease_expected_sha, and lease_owner from the dispatcher"
        )
    if lease_ref != expected_ref:
        raise WorkerError("resume lease ref does not match the issue")
    if lease_owner != execution_id:
        raise WorkerError("resume lease owner does not match execution_id")
    if provider_github._SHA_RE.fullmatch(expected_sha) is None:
        raise WorkerError("resume lease expected SHA is invalid")
    return provider_github.ResumeLease(lease_ref, expected_sha, lease_owner)


def _release_resume_lease(lease: provider_github.ResumeLease, repo: str, issue: int) -> None:
    """Best-effort always-run cleanup guarded by the lease's expected SHA."""

    try:
        released = task_claim.release_resume_lease(
            repo,
            issue,
            lease.owner,
            lease.expected_sha,
            lease.ref,
        )
    except (task_claim.ClaimError, provider_github.GitHubError, OSError) as error:
        print(f"error: resume lease cleanup failed: {type(error).__name__}", file=sys.stderr)
        return
    if not released:
        # A mismatch is intentionally not retried: the ref may now belong to
        # a newer dispatcher and deleting it would steal that fresh lease.
        print("warning: resume lease was already gone or owned by another execution", file=sys.stderr)


def _mark_blocked(root: Path, repo: str, issue: int, reason: str) -> None:
    result = _run_tasks(root, "block", str(issue), reason, "--repo", repo)
    if result.returncode != 0:
        raise WorkerError("failed to persist aes:blocked")


def _handle_publication_error(
    root: Path,
    repo: str,
    issue: int,
    execution_id: str,
    artifact_root: Path,
    error: BaseException,
) -> int:
    """Map publication failures to an explicit queue state.

    A stale claim or an authorization/product-state change is a human gate;
    a transient PR/link failure keeps the claim and is resumable; only a
    deterministic trusted-worker failure becomes ``aes:blocked``.
    """

    if isinstance(error, publisher.PublisherError):
        classification = error.classification
        reason = f"publication {error.phase}/{error.code}; execution_id={execution_id}"
        if error.pr_url:
            reason += f"; pr_url={error.pr_url}"
        if classification == "needs-human":
            _needs_human(
                root,
                repo,
                issue,
                execution_id,
                reason,
                "Revalidate the claim, authorization, and issue state before retrying.",
            )
            return EXIT_NEEDS_HUMAN
        if classification == "recoverable":
            try:
                _mark_waiting_provider(
                    root,
                    repo,
                    issue,
                    execution_id,
                    _public_outcome_reason("waiting_provider", execution_id),
                )
            except (WorkerError, provider_github.GitHubError, OSError):
                _needs_human(
                    root,
                    repo,
                    issue,
                    execution_id,
                    "Unable to persist recoverable publication retry state",
                    f"Retry publication manually for execution {execution_id}; "
                    f"preserved PR URL: {error.pr_url or '(none)' }.",
                )
                return EXIT_NEEDS_HUMAN
            return EXIT_WAITING_PROVIDER

    try:
        _mark_blocked(root, repo, issue, f"verified task could not be published: {type(error).__name__}")
    except (WorkerError, provider_github.GitHubError, OSError):
        _needs_human(
            root,
            repo,
            issue,
            execution_id,
            "Unable to persist technical publication failure as aes:blocked",
            f"Inspect the verified artifact and claim for execution {execution_id}.",
        )
        return EXIT_NEEDS_HUMAN
    return EXIT_FAILED


def _git_output(worktree: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ("git", "-C", str(worktree), *args),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        # Git stderr can contain remote URLs, helper diagnostics, or leaked
        # credential material.  The worker only needs a stable failure class.
        raise WorkerError("git command failed")
    return result.stdout


def _working_tree_paths(worktree: Path) -> list[str]:
    """List staged, unstaged and untracked paths without shell expansion."""

    raw = _git_output(
        worktree,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        env=_agent_environment(),
    )
    paths: list[str] = []
    for line in raw.splitlines():
        if len(line) < 4:
            continue
        value = line[3:]
        # Porcelain v1 represents a rename as ``old -> new``.  Both names
        # matter for scope validation; staging is still restricted to the new
        # path below.
        if " -> " in value:
            old, new = value.split(" -> ", 1)
            paths.extend((old, new))
        else:
            paths.append(value)
    return paths


def _validate_relative_path(path: str) -> str:
    """Validate a Git path before using it as an exact staging argument."""

    normalized = path.strip()
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith(("/", "\\"))
        or "\\" in normalized
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise WorkerError("worker changed a path outside the repository")
    return normalized


def _reject_unsafe_worktree_entry(worktree: Path, path: str) -> None:
    """Reject symlink traversal and gitlinks before staging a model change."""

    path = _validate_relative_path(path)
    current = worktree
    parts = path.split("/")
    for part in parts:
        current = current / part
        try:
            entry = current.lstat()
        except FileNotFoundError:
            break
        except OSError as error:
            raise WorkerError("cannot inspect changed worker path") from error
        if stat.S_ISLNK(entry.st_mode):
            raise WorkerError("worker changed a symlink")
        if stat.S_ISDIR(entry.st_mode) and (current / ".git").exists():
            raise WorkerError("worker changed a gitlink or submodule")

    indexed = _git_output(worktree, "ls-files", "--stage", "--", path, env=_agent_environment())
    for line in indexed.splitlines():
        mode = line.split(None, 1)[0] if line.split(None, 1) else ""
        if mode == "120000":
            raise WorkerError("worker changed a symlink")
        if mode == "160000":
            raise WorkerError("worker changed a gitlink or submodule")


def _reject_unsafe_index_entries(worktree: Path, paths: list[str]) -> None:
    """Verify the index after exact staging before allowing a commit."""

    indexed = _git_output(
        worktree,
        "ls-files",
        "--stage",
        "--",
        *paths,
        env=_agent_environment(),
    )
    for line in indexed.splitlines():
        fields = line.split(None, 3)
        if not fields:
            continue
        if fields[0] == "120000":
            raise WorkerError("worker changed a symlink")
        if fields[0] == "160000":
            raise WorkerError("worker changed a gitlink or submodule")


def _within_allowed(path: str, allowed: tuple[str, ...]) -> bool:
    normalized = path.strip("/")
    return any(normalized == root or normalized.startswith(root + "/") for root in allowed)


def _commit_uncommitted_change(
    worktree: Path,
    task: provider_github.Task,
    issue: int,
    execution_id: str,
) -> None:
    """Create one deterministic local commit, staging only contract paths."""

    contract = task.contract
    if contract is None:
        raise WorkerError("contract is missing before local commit")
    allowed = tuple(_validate_relative_path(path) for path in contract.paths if path.strip())
    if not allowed:
        raise WorkerError("contract paths are empty before local commit")
    paths = _working_tree_paths(worktree)
    if not paths:
        return
    paths = [_validate_relative_path(path) for path in paths]
    for path in paths:
        _reject_unsafe_worktree_entry(worktree, path)
    outside = sorted({path for path in paths if not _within_allowed(path, allowed)})
    if outside:
        raise WorkerError(
            "worker changed paths outside the issue contract: " + ", ".join(outside)
        )
    # Stage exact files rather than a directory/glob.  ``--`` prevents a
    # model-created filename beginning with '-' from becoming an option.
    stage_paths = sorted({path for path in paths if _within_allowed(path, allowed)})
    if not stage_paths:
        raise WorkerError("worker produced no contract-scoped changes")
    _git_output(
        worktree,
        "add",
        "--",
        *stage_paths,
        env=_agent_environment(),
    )
    _reject_unsafe_index_entries(worktree, stage_paths)
    _git_output(
        worktree,
        "-c",
        "user.name=AES Trusted Worker",
        "-c",
        "user.email=aes-trusted-worker@localhost",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "--no-verify",
        "-m",
        f"AES issue #{issue}: execution {execution_id}",
        env=_agent_environment(),
    )


def _needs_human(
    root: Path,
    repo: str,
    issue: int,
    execution_id: str,
    reason: str,
    question: str,
) -> None:
    """Make every post-claim failure visible instead of leaving a silent claim."""

    result = _run_tasks(
        root,
        "needs-human",
        str(issue),
        reason,
        question,
        "--repo",
        repo,
        "--execution-id",
        execution_id,
    )
    if result.returncode != 0:
        print(
            f"error: failed to transition {repo}#{issue} to aes:needs-human "
            f"after worker failure (status {result.returncode})",
            file=sys.stderr,
        )


def _run_phase4(
    root: Path,
    repo: str,
    issue: int,
    execution_id: str,
    runner: str,
    task: provider_github.Task,
    worktree: Path,
    *,
    resume: bool = False,
) -> int:
    """Run orchestration, state transitions and trusted publication."""

    resume = resume or "aes:waiting-provider" in task.labels
    artifact_root: Path | None = None
    brief_path: Path | None = None
    orchestration_started = False
    head_before_orchestration: str | None = None
    try:
        artifact_root = _artifact_root(root, execution_id, resume=resume)
        brief_path = (
            _resume_brief(artifact_root, execution_id)
            if resume
            else _write_task_brief(
                root,
                task,
                execution_id,
                artifact_root=artifact_root,
            )
        )
        cache = _quota_cache(root)
        refresh_error = _refresh_codex_quota(cache)
        router = ModelRouter(
            availability=_router_availability(
                cache.path,
                codex_refresh_error=refresh_error,
            )
        )
        orchestrator = IssueOrchestrator(
            trusted_brief=brief_path,
            artifact_root=artifact_root,
            worktree=worktree,
            execution_id=execution_id,
            quota_cache=cache.path,
            router=router,
        )
        # Every invocation, including a provider-wait resume, establishes its
        # own integrity baseline.  A model must not be able to move the
        # feature ref through an out-of-band commit and have that commit
        # silently reach the trusted publication path.
        head_before_orchestration = _worktree_head(worktree)
        orchestration_started = True
        outcome = orchestrator.run()
    except KeyboardInterrupt:
        # A newly-created brief is safe to remove only before the ledger has
        # started.  Once orchestration owns a durable state, preserve it for a
        # deliberate resume instead of deleting partial audit artifacts.
        if not resume and not orchestration_started:
            if brief_path is not None:
                try:
                    brief_path.unlink()
                except OSError:
                    pass
            if artifact_root is not None:
                try:
                    artifact_root.rmdir()
                except OSError:
                    pass
        raise
    except (
        WorkerError,
        QuotaError,
        ArtifactError,
        OrchestrationError,
        OSError,
        ValueError,
    ) as error:
        _needs_human(
            root, repo, issue, execution_id,
            f"Trusted orchestration setup failed: {type(error).__name__}",
            f"Inspect runner {runner} and resume execution {execution_id}.",
        )
        return EXIT_NEEDS_HUMAN

    try:
        head_after_orchestration = _worktree_head(worktree)
    except WorkerError as error:
        _needs_human(
            root,
            repo,
            issue,
            execution_id,
            f"Unable to verify worktree integrity after orchestration: {type(error).__name__}",
            f"Inspect worktree HEAD and orchestration artifacts before resuming {execution_id}.",
        )
        return EXIT_NEEDS_HUMAN
    if head_after_orchestration != head_before_orchestration:
        _needs_human(
            root,
            repo,
            issue,
            execution_id,
            "Worktree HEAD changed during orchestration",
            f"Inspect the unexpected feature-branch commit before resuming {execution_id}.",
        )
        return EXIT_NEEDS_HUMAN

    if outcome.status == "waiting_provider":
        try:
            _mark_waiting_provider(
                root,
                repo,
                issue,
                execution_id,
                _public_outcome_reason("waiting_provider", execution_id),
            )
        except (WorkerError, provider_github.GitHubError, OSError) as error:
            _needs_human(
                root, repo, issue, execution_id,
                f"Cannot persist provider wait: {type(error).__name__}",
                f"Inspect the claim and artifact ledger before resuming {execution_id}.",
            )
            return EXIT_NEEDS_HUMAN
        return EXIT_WAITING_PROVIDER

    if outcome.status == "needs_human":
        _needs_human(
            root, repo, issue, execution_id,
            _public_outcome_reason("needs_human", execution_id),
            f"Review trusted orchestration artifacts for execution {execution_id}.",
        )
        return EXIT_NEEDS_HUMAN

    if outcome.status == "failed":
        try:
            _mark_blocked(
                root,
                repo,
                issue,
                _public_outcome_reason("failed", execution_id),
            )
        except (WorkerError, provider_github.GitHubError, OSError):
            _needs_human(
                root, repo, issue, execution_id,
                "Unable to persist technical failure as aes:blocked",
                f"Inspect orchestration artifacts for execution {execution_id}.",
            )
        return EXIT_FAILED

    if outcome.status != "verified":
        _needs_human(
            root, repo, issue, execution_id,
            f"Unsupported orchestration outcome: {outcome.status}",
            f"Inspect orchestration artifacts for execution {execution_id}.",
        )
        return EXIT_NEEDS_HUMAN

    try:
        # Verification is not publication authority. Re-read all mutable
        # remote state before staging and again before calling the publisher.
        live_task = provider_github.get_task(repo, issue)
        task_claim.validate_claim(repo, issue, execution_id, enforce_author=True)
        # Recheck immediately before the first trusted write.  This closes the
        # small window between orchestration completion and commit preparation
        # if another process touched the adopted feature worktree.
        if _worktree_head(worktree) != head_before_orchestration:
            _needs_human(
                root,
                repo,
                issue,
                execution_id,
                "Worktree HEAD changed before trusted commit",
                f"Inspect the unexpected feature-branch commit before resuming {execution_id}.",
            )
            return EXIT_NEEDS_HUMAN
        _commit_uncommitted_change(worktree, live_task, issue, execution_id)
        live_task = provider_github.get_task(repo, issue)
        task_claim.validate_claim(repo, issue, execution_id, enforce_author=True)
        base_branch = os.environ.get("AES_INTEGRATION_BRANCH", "develop")
        _contract_scope(live_task, worktree, base_branch)
        publisher.publish(repo, issue, execution_id, worktree, base_branch)
    except (
        task_claim.ClaimError,
        WorkerError,
        provider_github.GitHubError,
        publisher.PublisherError,
        OSError,
    ) as error:
        if isinstance(error, task_claim.ClaimError):
            _needs_human(
                root,
                repo,
                issue,
                execution_id,
                "publication claim validation failed",
                "Revalidate the claim and resume only with the owning execution.",
            )
            return EXIT_NEEDS_HUMAN
        if isinstance(error, provider_github.GitHubError):
            _needs_human(
                root,
                repo,
                issue,
                execution_id,
                "publication control-plane lookup failed",
                "Verify authentication and issue state before retrying.",
            )
            return EXIT_NEEDS_HUMAN
        return _handle_publication_error(
            root,
            repo,
            issue,
            execution_id,
            artifact_root,
            error,
        )
    return EXIT_VERIFIED


def _run_worker_claimed(
    root: Path,
    repo: str,
    issue: int,
    execution_id: str,
    runner: str,
    task: provider_github.Task,
    *,
    resume: bool,
) -> int:
    """Validate, adopt, and execute after the resume lease is owned."""

    if task.contract is None:
        _needs_human(
            root, repo, issue, execution_id,
            "Issue contract is missing or malformed",
            "Restore a valid aes:contract block before resuming this claimed task.",
        )
        return 2
    if task.dispatch_error:
        _needs_human(
            root, repo, issue, execution_id,
            f"Issue queue metadata is invalid: {task.dispatch_error}",
            "Restore valid aes:contract and aes:dispatch blocks before resuming this task.",
        )
        return 2
    if task.dispatch is None or task.dispatch.dispatch != "auto":
        _needs_human(
            root, repo, issue, execution_id,
            "Issue is not configured for autonomous dispatch",
            "Set dispatch: auto and the required role/autonomy labels before resuming.",
        )
        return 2
    reasons = validate(task.dispatch, task.labels)
    if reasons:
        _needs_human(
            root, repo, issue, execution_id,
            "; ".join(reasons),
            "Repair the issue dispatch metadata and labels before resuming this task.",
        )
        return 2

    # The planner owns the remote atomic claim.  A worker may only adopt it;
    # it must never race another matrix item by claiming again.
    try:
        claimed_branch = task_claim.validate_claim(
            repo, issue, execution_id, enforce_author=True,
        )
    except task_claim.ClaimError as error:
        _needs_human(
            root, repo, issue, execution_id,
            f"Claim/adopt/link validation failed before adoption: {error}",
            f"Inspect claim ownership for execution {execution_id} before resuming.",
        )
        return 1
    previous_queue_enabled = os.environ.get("AES_QUEUE_ENABLED")
    os.environ["AES_QUEUE_ENABLED"] = "true"
    try:
        adopt_result = _run_tasks(
            root, "adopt", str(issue), "--repo", repo, "--execution-id", execution_id,
        )
    finally:
        if previous_queue_enabled is None:
            os.environ.pop("AES_QUEUE_ENABLED", None)
        else:
            os.environ["AES_QUEUE_ENABLED"] = previous_queue_enabled
    if adopt_result.returncode != 0:
        _needs_human(
            root, repo, issue, execution_id,
            "Worker could not adopt the already claimed feature ref",
            f"Inspect runner {runner}, remote claim ownership, and resume execution {execution_id}.",
        )
        return adopt_result.returncode or 2
    try:
        worktree = _worktree(root, issue)
        actual_branch = _worktree_branch(worktree)
        if actual_branch != claimed_branch:
            raise WorkerError(
                f"adopted branch {actual_branch!r} does not match claimed branch {claimed_branch!r}"
            )
        # Adoption may reuse a local worktree after a runner crash.  Never let
        # a model commit made by that previous invocation become this
        # invocation's fresh orchestration baseline: the durable remote claim
        # is the only trusted source for the starting tip.
        _validate_adopted_head(repo, claimed_branch, worktree)
    except (WorkerError, provider_github.GitHubError) as error:
        _needs_human(
            root, repo, issue, execution_id,
            f"Claimed worktree is unavailable after adoption: {error}",
            f"Inspect runner {runner} and resume execution {execution_id}.",
        )
        return 2

    if resume:
        # The trusted transition is intentionally before orchestration and
        # publication.  A provider wait outcome re-adds the label below.
        resume_start = _run_tasks(
            root, "resume-start", str(issue), "--repo", repo, "--execution-id", execution_id,
        )
        if resume_start.returncode != 0:
            _needs_human(
                root, repo, issue, execution_id,
                "Worker could not clear aes:waiting-provider before resume",
                f"Inspect the lease and issue labels before resuming {execution_id}.",
            )
            return resume_start.returncode or 2

    return _run_phase4(
        root, repo, issue, execution_id, runner, task, worktree, resume=resume,
    )


def run_worker(
    repo: str,
    issue: int,
    execution_id: str,
    *,
    resume: bool = False,
    lease_ref: str | None = None,
    lease_expected_sha: str | None = None,
    lease_owner: str | None = None,
) -> int:
    _required("GH_TOKEN")
    _verify_authenticated_actor()
    _required("AES_PRODUCT_MANAGER")
    if os.environ.get("AES_SELF_HOSTED", "").strip().lower() != "true":
        raise WorkerError("AES_SELF_HOSTED must be true on the authenticated runner")
    runner = _required("RUNNER_NAME")
    root = Path.cwd()
    task = provider_github.get_task(repo, issue)
    resume = resume or "aes:waiting-provider" in task.labels
    lease = (
        _resume_lease(
            repo,
            issue,
            execution_id,
            lease_ref=lease_ref,
            expected_sha=lease_expected_sha,
            lease_owner=lease_owner,
        )
        if resume
        else None
    )
    try:
        return _run_worker_claimed(
            root, repo, issue, execution_id, runner, task, resume=resume,
        )
    finally:
        if lease is not None:
            _release_resume_lease(lease, repo, issue)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", required=True, type=int)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--lease-ref")
    parser.add_argument("--lease-expected-sha")
    parser.add_argument("--lease-owner")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume an existing waiting-provider execution ledger",
    )
    args = parser.parse_args()
    try:
        return run_worker(
            args.repo,
            args.issue,
            args.execution_id,
            resume=args.resume,
            lease_ref=args.lease_ref,
            lease_expected_sha=args.lease_expected_sha,
            lease_owner=args.lease_owner,
        )
    except provider_github.GitHubError:
        print("error: GitHub control-plane operation failed", file=sys.stderr)
        return 2
    except (WorkerError, QueueConfigError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
