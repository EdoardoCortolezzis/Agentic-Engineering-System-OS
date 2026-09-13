#!/usr/bin/env python3
"""Trusted, argv-only publication of one claimed AES task.

The worker/model boundary ends before this module.  This publisher accepts a
local, already committed feature worktree, revalidates the live claim and the
contract scope, then performs the only two external write stages allowed by
the queue protocol: push and pull-request creation.  Issue state is delegated
to ``tasks.py link-pr`` so its label and notification semantics remain the
single source of truth.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Callable, Iterator, Sequence
from urllib.parse import urlsplit

import claim as task_claim
import provider_github


REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
EXECUTION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
BRANCH_PATTERN = re.compile(r"^feature/(?P<issue>[1-9][0-9]*)-[a-z0-9][a-z0-9-]{0,79}$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40,64}$", re.IGNORECASE)
PR_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(?P<number>[1-9][0-9]*)$"
)

_SECRET_SUFFIXES = ("_TOKEN", "_API_KEY", "_SECRET", "_PASSWORD", "_PRIVATE_KEY")
_BLOCKED_ENVIRONMENT = {
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GH_HOST",
    "GH_REPO",
    "GH_API_HOST",
    "GH_CONFIG_DIR",
    "GIT_ASKPASS",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_PROXY_COMMAND",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG",
    "SSH_AUTH_SOCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "AZURE_TENANT_ID",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AZURE_CONFIG_DIR",
    "AZURE_AUTH_LOCATION",
    "CLOUDSDK_CONFIG",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "KUBECONFIG",
    "VAULT_ADDR",
    "VAULT_TOKEN",
}

# The askpass helper is deliberately token-free.  Git receives this variable
# only for the duration of the push and the context manager removes it before
# returning, so an interrupted cleanup can leave no credential-bearing file.
_ASKPASS_TOKEN_ENV = "AES_GIT_ASKPASS_TOKEN"


class PublisherError(RuntimeError):
    """Structured fail-closed publication error.

    ``phase`` is one of ``preflight``, ``claim``, ``scope``, ``push``,
    ``pr`` or ``link``.  A link failure is recoverable because the PR URL is
    retained and a retry can run the idempotent ``link-pr`` command.
    """

    def __init__(
        self,
        phase: str,
        code: str,
        message: str,
        *,
        recoverable: bool = False,
        pr_url: str | None = None,
        classification: str | None = None,
    ) -> None:
        if classification is None:
            classification = "recoverable" if recoverable else "blocked"
        if classification not in {"needs-human", "recoverable", "blocked"}:
            raise ValueError("invalid publisher error classification")
        self.phase = phase
        self.stage = phase
        self.code = code
        self.message = message
        self.reason = message
        self.recoverable = recoverable
        self.pr_url = pr_url
        self.classification = classification
        suffix = f"; pr_url={pr_url}" if pr_url else ""
        super().__init__(f"{phase}/{code}: {message}{suffix}")

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe audit representation without secrets."""

        return {
            "error": self.code,
            "phase": self.phase,
            "message": self.message,
            "recoverable": self.recoverable,
            "pr_url": self.pr_url,
            "classification": self.classification,
        }


@dataclass(frozen=True)
class PublishResult:
    """Immutable result of a successful push/PR/link sequence."""

    repo: str
    issue: int
    execution_id: str
    branch: str
    pr_url: str
    existing: bool = False


def _fail(
    phase: str,
    code: str,
    message: str,
    *,
    recoverable: bool = False,
    pr_url: str | None = None,
    classification: str | None = None,
) -> None:
    raise PublisherError(
        phase,
        code,
        message,
        recoverable=recoverable,
        pr_url=pr_url,
        classification=classification,
    )


def _validate_repo(repo: str) -> None:
    if not REPO_PATTERN.fullmatch(repo):
        _fail("preflight", "invalid_repo", "repository must be an exact owner/name slug")


def _validate_inputs(repo: str, issue: int, execution_id: str, base_branch: str) -> None:
    _validate_repo(repo)
    if issue <= 0:
        _fail("preflight", "invalid_issue", "issue number must be positive")
    if not EXECUTION_PATTERN.fullmatch(execution_id):
        _fail("preflight", "invalid_execution_id", "execution_id contains unsafe characters")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", base_branch):
        _fail("preflight", "invalid_base", "base branch contains unsafe characters")


def _auth_environment(tool: str = "gh") -> dict[str, str]:
    """Build a minimally trusted environment for one child tool.

    ``GH_TOKEN`` is the one credential deliberately retained.  Provider
    credentials, alternate Git authorities, and proxy configuration are
    removed before either ``gh`` or ``git`` is launched.  The two callers use
    separate dictionaries so askpass-specific state can never leak into the
    GitHub CLI child.
    """

    if tool not in {"gh", "git"}:
        raise ValueError("unsupported publisher child tool")
    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        _fail(
            "preflight",
            "missing_token",
            "GH_TOKEN is required for publication",
            classification="needs-human",
        )
    environment = os.environ.copy()
    for name in tuple(environment):
        upper_name = name.upper()
        if name != "GH_TOKEN" and (
            upper_name.endswith(_SECRET_SUFFIXES)
            or upper_name in _BLOCKED_ENVIRONMENT
            or upper_name.startswith("GIT_CONFIG_")
            or upper_name.startswith(("GIT_", "GH_"))
            or upper_name.startswith(
                ("AWS_", "AZURE_", "CLOUDSDK_", "GOOGLE_", "OPENAI_", "ANTHROPIC_")
            )
            or upper_name.endswith("_PROXY")
            or upper_name in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
        ):
            environment.pop(name, None)
    environment["GH_TOKEN"] = token
    environment["GIT_TERMINAL_PROMPT"] = "0"
    # Never consult system/global Git configuration inherited from the host;
    # otherwise a credential helper, URL rewrite, or alternate authority can
    # silently take precedence over the validated origin remote.
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_SYSTEM"] = os.devnull
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    # A caller cannot use the inherited askpass helper or Git authority even
    # when it launches a different child than the one that requested this env.
    environment.pop("GIT_ASKPASS", None)
    return environment


def _verify_authenticated_actor(environment: dict[str, str]) -> None:
    """Fail closed unless the sanitized ``gh`` token belongs to the trusted actor."""

    trusted_actor = os.environ.get("AES_TRUSTED_ACTOR", "").strip()
    if not trusted_actor:
        _fail(
            "preflight",
            "missing_trusted_actor",
            "AES_TRUSTED_ACTOR is required for publication",
            classification="needs-human",
        )
    result = _run(
        ("gh", "api", "user", "--jq", ".login"),
        env=environment,
        error_phase="preflight",
        error_classification="needs-human",
    )
    if result.returncode != 0 or result.stdout.strip() != trusted_actor:
        _fail(
            "preflight",
            "untrusted_actor",
            "authenticated GitHub actor does not match AES_TRUSTED_ACTOR",
            classification="needs-human",
        )


def _run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    error_phase: str = "preflight",
    error_classification: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a trusted argv tuple without ever invoking a shell."""

    try:
        return subprocess.run(
            tuple(argv),
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
    except (OSError, ValueError) as error:
        # Never echo subprocess exception text: wrappers can include command
        # output or environment material in their message.
        raise PublisherError(
            error_phase,
            "command_failed",
            "trusted command failed",
            classification=error_classification,
            recoverable=error_classification == "recoverable",
        ) from error


def _git_output(
    worktree: Path,
    args: Sequence[str],
    *,
    environment: dict[str, str] | None = None,
    phase: str = "preflight",
    code: str = "git_failed",
) -> str:
    result = _run(
        ("git", "-C", str(worktree), *args),
        env=environment if environment is not None else _auth_environment("git"),
    )
    if result.returncode != 0:
        _fail(phase, code, "git command failed")
    return result.stdout.strip()


def _resolve_worktree(worktree: str | Path) -> Path:
    path = Path(worktree)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        _fail("preflight", "worktree_missing", "worktree is unavailable")
    if not resolved.is_dir():
        _fail("preflight", "worktree_missing", "worktree is not a directory")
    return resolved


def _validate_branch(branch: str, issue: int) -> None:
    match = BRANCH_PATTERN.fullmatch(branch)
    if match is None or int(match.group("issue")) != issue:
        _fail(
            "claim",
            "invalid_claim_branch",
            "claimed branch is not the exact feature branch for this issue",
            classification="needs-human",
        )


def _normalise_allowed_paths(paths: Sequence[str]) -> tuple[str, ...]:
    allowed: list[str] = []
    for raw_path in paths:
        path = str(raw_path).strip()
        # Do not trim an absolute path into an apparently safe relative one.
        # Contract paths are repository-relative and must never be able to
        # escape through a separator or a parent component.
        if path.startswith(("/", "\\")) or "\\" in path:
            _fail("scope", "invalid_contract_path", "contract path is outside the repository")
        path = path.rstrip("/")
        parts = path.split("/") if path else []
        if not parts or any(part in {"", ".", ".."} for part in parts):
            _fail("scope", "invalid_contract_path", "contract path is outside the repository")
        allowed.append("/".join(parts))
    if not allowed:
        _fail("scope", "empty_contract_scope", "contract paths are empty before publication")
    return tuple(allowed)


def validate_contract_scope(
    task: object,
    worktree: Path,
    base_branch: str,
    *,
    environment: dict[str, str] | None = None,
) -> None:
    """Verify repository identity and every changed path against the contract."""

    contract = getattr(task, "contract", None)
    if contract is None:
        _fail("scope", "missing_contract", "contract is missing before publication")
    declared_repo = (getattr(contract, "repo", None) or getattr(task, "repo_slug", "")).strip()
    task_repo = getattr(task, "repo_slug", "")
    if declared_repo != task_repo:
        _fail("scope", "contract_repo_mismatch", "contract repository does not match the task repository")
    allowed = _normalise_allowed_paths(getattr(contract, "paths", ()))

    # A clean worktree is mandatory.  This also ensures no untracked file can
    # evade the committed diff used for scope enforcement.
    status = _git_output(
        worktree,
        ("status", "--porcelain=v1", "--untracked-files=all"),
        environment=environment,
        phase="scope",
    )
    if status:
        _fail("scope", "dirty_worktree", "feature worktree has uncommitted or untracked changes")
    changed_text = _git_output(
        worktree,
        ("diff", "--name-only", f"origin/{base_branch}...HEAD"),
        environment=environment,
        phase="scope",
    )
    changed = [name for name in changed_text.splitlines() if name]
    if not changed:
        _fail("scope", "no_changed_files", "worker produced no changed files")

    def validate_changed_name(name: str) -> str:
        path = name.strip()
        parts = path.split("/")
        if (
            not path
            or path.startswith(("/", "\\"))
            or "\\" in path
            or any(part in {"", ".", ".."} for part in parts)
        ):
            _fail("scope", "changed_path_escape", "changed path is outside the repository")
        return path

    changed = [validate_changed_name(name) for name in changed]

    def tree_mode(revision: str, path: str) -> str | None:
        # ``ls-tree -z`` keeps paths unambiguous even when a model creates a
        # filename containing whitespace.  A mode of 120000 is a symlink and
        # 160000 is a gitlink/submodule; neither is part of the trusted worker
        # contract, including when the entry is being deleted.
        listing = _git_output(
            worktree,
            ("ls-tree", "-z", revision, "--", path),
            environment=environment,
            phase="scope",
            code="tree_inspection_failed",
        )
        for record in listing.split("\0"):
            if not record:
                continue
            metadata, separator, listed_path = record.partition("\t")
            if separator and listed_path == path:
                fields = metadata.split()
                if fields:
                    return fields[0]
        return None

    for path in changed:
        for revision in (f"origin/{base_branch}", "HEAD"):
            mode = tree_mode(revision, path)
            if mode == "120000":
                _fail("scope", "changed_symlink", "changed symlink is not allowed")
            if mode == "160000":
                _fail("scope", "changed_gitlink", "changed gitlink or submodule is not allowed")

    def within_scope(name: str) -> bool:
        normalized = name.strip("/")
        return any(normalized == path or normalized.startswith(path + "/") for path in allowed)

    outside = sorted(name for name in changed if not within_scope(name))
    if outside:
        _fail("scope", "changed_path_outside_contract", "changed paths are outside the contract")


def _capture_head_sha(worktree: Path, environment: dict[str, str] | None = None) -> str:
    if environment is None:
        environment = _auth_environment("git")
    value = _git_output(
        worktree,
        ("rev-parse", "--verify", "HEAD^{commit}"),
        environment=environment,
        phase="preflight",
        code="head_unavailable",
    )
    if not SHA_PATTERN.fullmatch(value):
        _fail("preflight", "invalid_head_sha", "git returned an invalid feature commit")
    return value.lower()


def _validate_local_state(
    worktree: Path,
    branch: str,
    base_branch: str,
    environment: dict[str, str] | None = None,
) -> str:
    if environment is None:
        environment = _auth_environment("git")
    actual_branch = _git_output(
        worktree,
        ("symbolic-ref", "--quiet", "--short", "HEAD"),
        environment=environment,
        phase="preflight",
        code="branch_unavailable",
    )
    if actual_branch != branch:
        _fail(
            "claim",
            "branch_mismatch",
            "worktree branch does not match the claimed branch",
            classification="needs-human",
        )
    status = _git_output(
        worktree,
        ("status", "--porcelain=v1", "--untracked-files=all"),
        environment=environment,
        phase="preflight",
    )
    if status:
        _fail("preflight", "dirty_worktree", "feature worktree has uncommitted or untracked changes")
    head_sha = _capture_head_sha(worktree, environment)
    base_ref = f"origin/{base_branch}"
    _git_output(
        worktree,
        ("rev-parse", "--verify", f"{base_ref}^{{commit}}"),
        environment=environment,
        phase="preflight",
        code="base_unavailable",
    )
    count_text = _git_output(
        worktree,
        ("rev-list", "--count", f"{base_ref}..HEAD"),
        environment=environment,
        phase="preflight",
        code="history_unavailable",
    )
    try:
        count = int(count_text)
    except ValueError:
        _fail("preflight", "history_unavailable", "git returned an invalid ahead commit count")
    if count < 1:
        _fail("preflight", "no_feature_commit", f"branch has no commit ahead of {base_ref}")
    return head_sha


def _remote_slug(url: str) -> str | None:
    """Extract an owner/name slug from a GitHub HTTPS or SCP remote URL."""

    raw = url.strip()
    if raw.startswith("git@github.com:"):
        path = raw.removeprefix("git@github.com:")
    else:
        parsed = urlsplit(raw)
        if parsed.hostname != "github.com" or parsed.scheme not in {"https", "ssh", "git"}:
            return None
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git").strip("/")
    return path if REPO_PATTERN.fullmatch(path) else None


def _validate_worktree_identity(
    worktree: Path,
    repo: str,
    environment: dict[str, str] | None = None,
) -> None:
    if environment is None:
        environment = _auth_environment("git")
    root = _git_output(
        worktree,
        ("rev-parse", "--show-toplevel"),
        environment=environment,
        phase="preflight",
        code="repository_unavailable",
    )
    try:
        if Path(root).resolve(strict=True) != worktree.resolve(strict=True):
            _fail("preflight", "worktree_root_mismatch", "git worktree root does not match the selected worktree")
    except OSError as error:
        _fail("preflight", "repository_unavailable", "cannot resolve worktree root")
    remote = _git_output(
        worktree,
        ("remote", "get-url", "origin"),
        environment=environment,
        phase="preflight",
        code="remote_unavailable",
    )
    push_remote = _git_output(
        worktree,
        ("remote", "get-url", "--push", "origin"),
        environment=environment,
        phase="preflight",
        code="push_remote_unavailable",
    )
    if _remote_slug(remote) != repo or _remote_slug(push_remote) != repo:
        _fail("preflight", "remote_repo_mismatch", "worktree origin does not match the task repository")


@contextmanager
def _publication_lock(worktree: Path) -> Iterator[None]:
    """Serialize publication attempts for one resolved worktree."""

    key = hashlib.sha256(str(worktree).encode("utf-8")).hexdigest()
    lock_path = Path(tempfile.gettempdir()) / f"aes-publisher-{key}.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except (OSError, ValueError) as error:
        raise PublisherError("preflight", "publication_lock_failed", "could not acquire publication lock") from error
    handle = None
    try:
        handle = os.fdopen(descriptor, "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except (OSError, ValueError) as error:
        try:
            if handle is None:
                os.close(descriptor)
            else:
                handle.close()
        except OSError:
            pass
        raise PublisherError("preflight", "publication_lock_failed", "could not acquire publication lock") from error
    try:
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


@contextmanager
def _askpass_environment(base_environment: dict[str, str]) -> Iterator[dict[str, str]]:
    """Yield a sanitized push environment backed by a deleted askpass file.

    Git needs the token for HTTPS authentication, but the Git process itself
    must not carry ``GH_TOKEN`` into any child.  Keep the helper static and
    pass the token only in a dedicated environment variable inherited by the
    Git/askpass child; hooks are disabled independently by ``--no-verify`` and
    ``core.hooksPath=/dev/null``.  A stale helper after SIGKILL is therefore
    harmless because it contains no credential.
    """

    token = base_environment.get("GH_TOKEN", "")
    if not token:
        _fail("push", "missing_token", "GH_TOKEN is required for push", classification="needs-human")
    fd, raw_path = tempfile.mkstemp(prefix="aes-git-askpass-")
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as helper:
            helper.write(
                "#!/bin/sh\n"
                'case "${1:-}" in\n'
                '  *Username*) printf "%s\\n" "x-access-token" ;;\n'
                f'  *Password*) printf "%s\\n" "${{{_ASKPASS_TOKEN_ENV}:-}}" ;;\n'
                "  *) exit 1 ;;\n"
                "esac\n"
            )
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        environment = dict(base_environment)
        environment.pop("GH_TOKEN", None)
        environment[_ASKPASS_TOKEN_ENV] = token
        environment["GIT_ASKPASS"] = str(path)
        environment["GIT_TERMINAL_PROMPT"] = "0"
        yield environment
    finally:
        if "environment" in locals():
            environment.pop("GH_TOKEN", None)
            environment.pop(_ASKPASS_TOKEN_ENV, None)
            environment.pop("GIT_ASKPASS", None)
        token = ""
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            raise PublisherError("push", "askpass_cleanup_failed", "could not remove temporary askpass helper")


def _push(
    worktree: Path,
    branch: str,
    environment: dict[str, str],
    commit_sha: str | None = None,
) -> None:
    if commit_sha is None:
        commit_sha = _capture_head_sha(worktree, environment)
    if not SHA_PATTERN.fullmatch(commit_sha):
        _fail("push", "invalid_head_sha", "feature commit is invalid before push")
    refspec = f"{commit_sha}:refs/heads/{branch}"
    with _askpass_environment(environment) as push_environment:
        result = _run(
            (
                "git",
                "-C",
                str(worktree),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "credential.helper=",
                "push",
                "--no-verify",
                "origin",
                refspec,
            ),
            env=push_environment,
            error_phase="push",
        )
    if result.returncode != 0:
        _fail("push", "push_failed", "git push failed")


def _validate_pr(
    repo: str,
    branch: str,
    base_branch: str,
    value: object,
    *,
    expected_head_sha: str | None = None,
) -> tuple[str, int]:
    if not isinstance(value, dict):
        _fail("pr", "invalid_pr_response", "GitHub returned an invalid pull-request object")
    url = value.get("url")
    if not isinstance(url, str):
        _fail("pr", "invalid_pr_url", "pull-request URL is missing")
    match = PR_URL_PATTERN.fullmatch(url)
    if match is None or match.group("repo") != repo:
        _fail("pr", "invalid_pr_url", "pull-request URL does not match the target repository")
    number = value.get("number")
    try:
        pr_number = int(number)
    except (TypeError, ValueError):
        pr_number = int(match.group("number"))
    if pr_number <= 0:
        _fail("pr", "invalid_pr_number", "pull-request number is invalid")
    head = value.get("headRefName")
    if head is not None and head != branch:
        _fail("pr", "pr_branch_mismatch", "pull request head branch does not match the claimed branch")
    base = value.get("baseRefName")
    if base is not None and base != base_branch:
        _fail("pr", "pr_base_mismatch", "pull request base branch does not match the integration branch")
    repository = value.get("headRepository")
    if isinstance(repository, dict):
        repository = repository.get("nameWithOwner")
    owner = value.get("headRepositoryOwner")
    if isinstance(owner, dict):
        owner = owner.get("login")
    if isinstance(repository, str) and repository and repository != repo:
        _fail("pr", "pr_repo_mismatch", "pull request head repository does not match the task repository")
    if isinstance(owner, str) and owner and not repo.startswith(owner + "/"):
        _fail("pr", "pr_repo_mismatch", "pull request head owner does not match the task repository")
    # A URL/branch match is not sufficient: an old PR for the same feature
    # branch can point at a different commit.  Linking it would mark the task
    # complete for work that was never validated by this publisher.  GitHub's
    # ``headRefOid`` is the authoritative SHA in ``gh pr list`` output; callers
    # must provide the validated local SHA whenever they are about to link.
    head_sha = value.get("headRefOid")
    if head_sha is not None and (
        not isinstance(head_sha, str) or not SHA_PATTERN.fullmatch(head_sha)
    ):
        _fail(
            "pr",
            "invalid_pr_head_sha",
            "pull request head commit is invalid",
            recoverable=True,
            pr_url=url,
        )
    if expected_head_sha is not None:
        if not isinstance(head_sha, str):
            _fail(
                "pr",
                "missing_pr_head_sha",
                "pull request head commit was not returned by GitHub",
                recoverable=True,
                pr_url=url,
            )
        if head_sha.lower() != expected_head_sha.lower():
            _fail(
                "pr",
                "pr_head_sha_mismatch",
                "pull request head commit does not match the validated feature commit",
                recoverable=True,
                pr_url=url,
            )
    return url, pr_number


def _find_existing_pr(
    repo: str,
    branch: str,
    base_branch: str,
    environment: dict[str, str],
    *,
    expected_head_sha: str | None = None,
) -> tuple[str, int] | None:
    result = _run(
        (
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--head",
            branch,
            "--base",
            base_branch,
            "--state",
            "open",
            "--limit",
            "20",
            "--json",
            "number,url,headRefName,headRefOid,baseRefName,headRepositoryOwner,headRepository",
        ),
        env=environment,
        error_phase="pr",
        error_classification="recoverable",
    )
    if result.returncode != 0:
        _fail("pr", "pr_lookup_failed", "pull-request lookup failed")
    try:
        rows = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as error:
        _fail("pr", "invalid_pr_response", "gh returned invalid JSON for pull requests")
    if not isinstance(rows, list):
        _fail("pr", "invalid_pr_response", "gh returned a non-list pull-request response")
    if len(rows) > 1:
        _fail("pr", "ambiguous_pr", "multiple open pull requests match the claimed branch")
    if not rows:
        return None
    return _validate_pr(
        repo,
        branch,
        base_branch,
        rows[0],
        expected_head_sha=expected_head_sha,
    )


def _create_pr(
    task: object,
    repo: str,
    issue: int,
    execution_id: str,
    branch: str,
    base_branch: str,
    environment: dict[str, str],
    *,
    expected_head_sha: str,
) -> tuple[str, int]:
    title = str(getattr(task, "title", "")).strip()
    if not title or any(char in title for char in "\x00\r\n"):
        _fail("pr", "invalid_pr_title", "issue title cannot be used as a pull-request title")
    body = f"Closes #{issue}\n\nAES execution: `{execution_id}`\n"
    fd, raw_path = tempfile.mkstemp(prefix=f"aes-pr-{issue}-", suffix=".md")
    body_path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as body_file:
            body_file.write(body)
        result = _run(
            (
                "gh",
                "pr",
                "create",
                "--repo",
                repo,
                "--title",
                title,
                "--body-file",
                str(body_path),
                "--head",
                branch,
                "--base",
                base_branch,
            ),
            env=environment,
            error_phase="pr",
            error_classification="recoverable",
        )
    finally:
        try:
            body_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            _fail("pr", "body_cleanup_failed", "could not remove temporary pull-request body")
    if result.returncode == 0:
        match = next((PR_URL_PATTERN.fullmatch(line.strip()) for line in result.stdout.splitlines()), None)
        if match is not None:
            url = match.group(0)
            return url, int(match.group("number"))
        # A successful command with unusual output may still have created the
        # PR.  Query before reporting failure so retry remains idempotent.
        existing = _find_existing_pr(
            repo,
            branch,
            base_branch,
            environment,
            expected_head_sha=expected_head_sha,
        )
        if existing is not None:
            return existing
        _fail(
            "pr",
            "pr_verification_failed",
            "created pull request could not be re-fetched and verified",
            recoverable=True,
            pr_url=match.group(0) if match is not None else None,
        )
    # Concurrent publishers can race between list and create.  Re-read the
    # open PR before failing, preserving the already-created result.
    existing = _find_existing_pr(
        repo,
        branch,
        base_branch,
        environment,
        expected_head_sha=expected_head_sha,
    )
    if existing is not None:
        return existing
    _fail("pr", "pr_create_failed", "pull-request creation failed", recoverable=True)


def _link_pr(
    trusted_root: Path,
    repo: str,
    issue: int,
    execution_id: str,
    environment: dict[str, str],
    *,
    pr_url: str,
) -> None:
    # Never execute a tasks.py copied or modified inside the model worktree;
    # the publisher's own checkout is the trusted control-plane source.
    tasks_script = trusted_root / "harness" / "scripts" / "tasks" / "tasks.py"
    if not tasks_script.is_file():
        _fail("link", "trusted_tasks_missing", "trusted tasks.py is unavailable", recoverable=True, pr_url=pr_url)
    result = _run(
        (
            sys.executable,
            str(tasks_script),
            "link-pr",
            str(issue),
            "--repo",
            repo,
            "--execution-id",
            execution_id,
        ),
        cwd=trusted_root,
        env=environment,
        error_phase="link",
        error_classification="recoverable",
    )
    if result.returncode != 0:
        _fail("link", "link_pr_failed", "link-pr command failed", recoverable=True, pr_url=pr_url)


def _validate_scope(
    task: object,
    worktree: Path,
    base_branch: str,
    git_environment: dict[str, str],
    scope_validator: Callable[[object, Path, str], None] | None,
) -> None:
    if scope_validator is None:
        validate_contract_scope(task, worktree, base_branch, environment=git_environment)
        return
    try:
        scope_validator(task, worktree, base_branch)
    except PublisherError:
        raise
    except Exception as error:
        _fail("scope", "scope_validation_failed", "contract scope validation failed")


def _publish_locked(
    repo: str,
    issue: int,
    execution_id: str,
    worktree_path: Path,
    base_branch: str,
    git_environment: dict[str, str],
    gh_environment: dict[str, str],
    scope_validator: Callable[[object, Path, str], None] | None,
) -> PublishResult:
    try:
        task = provider_github.get_task(repo, issue, environment=gh_environment)
    except Exception as error:
        # Provider details may contain URLs, tokens, or host diagnostics from
        # the child process.  Keep those out of the publisher's public error.
        _fail(
            "claim",
            "task_lookup_failed",
            "live task lookup failed before publication",
            classification="needs-human",
        )
    if getattr(task, "repo_slug", None) != repo or int(getattr(task, "number", -1)) != issue:
        _fail(
            "claim",
            "task_identity_mismatch",
            "live issue identity does not match publisher inputs",
            classification="needs-human",
        )
    try:
        claimed_branch = task_claim.validate_claim(
            repo,
            issue,
            execution_id,
            enforce_author=True,
            environment=gh_environment,
        )
    except PublisherError:
        raise
    except Exception as error:
        _fail(
            "claim",
            "claim_validation_failed",
            "live claim validation failed",
            classification="needs-human",
        )
    if not isinstance(claimed_branch, str):
        _fail("claim", "invalid_claim_branch", "claim validator returned no branch")
    _validate_branch(claimed_branch, issue)
    _validate_worktree_identity(worktree_path, repo, git_environment)
    head_sha = _validate_local_state(worktree_path, claimed_branch, base_branch, git_environment)
    _validate_scope(task, worktree_path, base_branch, git_environment, scope_validator)

    existing = _find_existing_pr(
        repo,
        claimed_branch,
        base_branch,
        gh_environment,
        expected_head_sha=head_sha,
    )
    if existing is None:
        # The worker may not share this lock.  Revalidate every publication
        # precondition immediately before using the captured commit.
        _validate_worktree_identity(worktree_path, repo, git_environment)
        current_sha = _validate_local_state(worktree_path, claimed_branch, base_branch, git_environment)
        if current_sha != head_sha:
            _fail("preflight", "changed_head", "feature HEAD changed during publication")
        _validate_scope(task, worktree_path, base_branch, git_environment, scope_validator)
        _push(worktree_path, claimed_branch, git_environment, head_sha)
        # Re-read after push to close the list/create race without creating a
        # duplicate PR when another trusted runner won concurrently.
        existing = _find_existing_pr(
            repo,
            claimed_branch,
            base_branch,
            gh_environment,
            expected_head_sha=head_sha,
        )
        if existing is None:
            pr_url, _pr_number = _create_pr(
                task,
                repo,
                issue,
                execution_id,
                claimed_branch,
                base_branch,
                gh_environment,
                expected_head_sha=head_sha,
            )
            was_existing = False
        else:
            pr_url, _pr_number = existing
            was_existing = True
    else:
        pr_url, _pr_number = existing
        was_existing = True
    # The branch/PR can change after any list/create race.  Re-fetch the
    # mutable issue and claim immediately before delegating the label
    # transition.  This is deliberately a second claim validation, not just
    # a PR lookup: a stale runner must never turn a PR into aes:pr-open.
    verified = _find_existing_pr(
        repo,
        claimed_branch,
        base_branch,
        gh_environment,
        expected_head_sha=head_sha,
    )
    if verified is None:
        _fail(
            "pr",
            "pr_verification_failed",
            "pull request disappeared before linking",
            recoverable=True,
            pr_url=pr_url,
        )
    pr_url, _pr_number = verified
    try:
        live_task = provider_github.get_task(repo, issue, environment=gh_environment)
    except Exception as error:
        _fail(
            "claim",
            "live_task_lookup_failed",
            "live task lookup failed before linking",
            classification="needs-human",
            pr_url=pr_url,
        )
    if (
        getattr(live_task, "repo_slug", None) != repo
        or int(getattr(live_task, "number", -1)) != issue
        or getattr(live_task, "state", "").lower() != "open"
        or "aes:claimed" not in getattr(live_task, "labels", ())
        or any(
            label in getattr(live_task, "labels", ())
            for label in ("aes:blocked", "aes:needs-human", "aes:pr-open", "aes:waiting-provider")
        )
    ):
        _fail(
            "claim",
            "stale_claim",
            "task claim or live issue state changed before linking",
            classification="needs-human",
            pr_url=pr_url,
        )
    try:
        live_branch = task_claim.validate_claim(
            repo,
            issue,
            execution_id,
            enforce_author=True,
            environment=gh_environment,
        )
    except Exception as error:
        _fail(
            "claim",
            "stale_claim",
            "claim ownership changed before linking",
            classification="needs-human",
            pr_url=pr_url,
        )
    if live_branch != claimed_branch:
        _fail(
            "claim",
            "stale_claim",
            "claimed branch changed before linking",
            classification="needs-human",
            pr_url=pr_url,
        )
    trusted_root = Path(__file__).resolve().parents[3]
    _link_pr(trusted_root, repo, issue, execution_id, gh_environment, pr_url=pr_url)
    return PublishResult(repo, issue, execution_id, claimed_branch, pr_url, was_existing)


def publish(
    repo: str,
    issue: int,
    execution_id: str,
    worktree: str | Path,
    base_branch: str = "develop",
    *,
    scope_validator: Callable[[object, Path, str], None] | None = None,
) -> PublishResult:
    """Publish one claimed feature and leave merge to a human reviewer."""

    _validate_inputs(repo, issue, execution_id, base_branch)
    gh_environment = _auth_environment("gh")
    _verify_authenticated_actor(gh_environment)
    git_environment = _auth_environment("git")
    worktree_path = _resolve_worktree(worktree)
    with _publication_lock(worktree_path):
        return _publish_locked(
            repo,
            issue,
            execution_id,
            worktree_path,
            base_branch,
            git_environment,
            gh_environment,
            scope_validator,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--base", default="develop")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = publish(args.repo, args.issue, args.execution_id, args.worktree, args.base)
    except PublisherError as error:
        print(json.dumps(error.as_dict(), sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result.__dict__, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
