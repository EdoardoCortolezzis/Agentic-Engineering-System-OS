from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / "harness" / "scripts" / "tasks"
sys.path.insert(0, str(TASKS))

HEAD_SHA = "a" * 40
NEXT_HEAD_SHA = "b" * 40

from contract import Contract  # noqa: E402
from dispatch import DispatchMetadata  # noqa: E402
from readiness import Task  # noqa: E402
import publisher  # noqa: E402


def _task() -> Task:
    return Task(
        number=42,
        repo_slug="owner/repo",
        title="Publish the trusted change",
        state="open",
        labels=("aes:claimed",),
        contract=Contract("owner/repo", ("harness/scripts/tasks/",), "", (), ()),
        dispatch=DispatchMetadata("auto", "normal", None, 1),
    )


def _git_result(
    args: tuple[str, ...],
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def _install_happy_seams(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    head_shas: list[str] | None = None,
) -> list[tuple[tuple[str, ...], dict]]:
    calls: list[tuple[tuple[str, ...], dict]] = []
    remaining_heads = list(head_shas or [HEAD_SHA])
    pr_created = False
    monkeypatch.setenv("GH_TOKEN", "secret-token")
    monkeypatch.setenv("AES_TRUSTED_ACTOR", "aes-queue-bot")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-child")
    monkeypatch.setattr(
        publisher.provider_github,
        "get_task",
        lambda repo, issue, **kwargs: _task(),
    )
    monkeypatch.setattr(
        publisher.task_claim,
        "validate_claim",
        lambda repo, issue, execution_id, **kwargs: "feature/42-publish-the-trusted-change",
    )
    monkeypatch.setattr(
        publisher,
        "validate_contract_scope",
        lambda task, worktree, base_branch, **kwargs: None,
    )

    def fake_run(argv, **kwargs):
        nonlocal pr_created
        args = tuple(str(part) for part in argv)
        calls.append((args, kwargs))
        if args[:2] == ("git", "-C"):
            command = args[3:]
            if command[:2] == ("symbolic-ref", "--quiet"):
                return _git_result(args, stdout="feature/42-publish-the-trusted-change\n")
            if command[:2] == ("status", "--porcelain=v1"):
                return _git_result(args)
            if command[:2] == ("rev-parse", "--show-toplevel"):
                return _git_result(args, stdout=f"{tmp_path}\n")
            if command[:2] == ("rev-parse", "--verify"):
                if command[-1] == "HEAD^{commit}":
                    value = remaining_heads.pop(0) if remaining_heads else HEAD_SHA
                    return _git_result(args, stdout=f"{value}\n")
                return _git_result(args, stdout="base-sha\n")
            if command[:2] == ("rev-list", "--count"):
                return _git_result(args, stdout="1\n")
            if command[:2] == ("remote", "get-url"):
                return _git_result(args, stdout="https://github.com/owner/repo.git\n")
            if (
                command[:7]
                == (
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "credential.helper=",
                    "push",
                    "--no-verify",
                    "origin",
                )
                and len(command) == 8
                and ":refs/heads/" in command[7]
            ):
                return _git_result(args)
        if args[:2] == ("gh", "pr") and args[2] == "list":
            if pr_created:
                return _git_result(
                    args,
                    stdout=json.dumps([
                        {
                            "number": 99,
                            "url": "https://github.com/owner/repo/pull/99",
                            "headRefName": "feature/42-publish-the-trusted-change",
                            "headRefOid": HEAD_SHA,
                            "baseRefName": "develop",
                            "headRepositoryOwner": {"login": "owner"},
                            "headRepository": {"nameWithOwner": "owner/repo"},
                        }
                    ]),
                )
            return _git_result(args, stdout="[]\n")
        if args[:4] == ("gh", "api", "user", "--jq"):
            return _git_result(args, stdout="aes-queue-bot\n")
        if args[:2] == ("gh", "pr") and args[2] == "create":
            pr_created = True
            return _git_result(args, stdout="https://github.com/owner/repo/pull/99\n")
        if any(value.endswith("tasks.py") for value in args[1:]):
            return _git_result(args)
        raise AssertionError(f"unexpected subprocess argv: {args}")

    monkeypatch.setattr(publisher.subprocess, "run", fake_run)
    return calls


def test_publish_uses_explicit_push_and_gh_argv_without_secret_or_merge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_happy_seams(monkeypatch, tmp_path)

    result = publisher.publish("owner/repo", 42, "execution-1", tmp_path)

    assert result.pr_url == "https://github.com/owner/repo/pull/99"
    push = next(args for args, _ in calls if args[:2] == ("git", "-C") and "push" in args)
    assert push[3:10] == (
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "credential.helper=",
        "push",
        "--no-verify",
        "origin",
    )
    assert f"{HEAD_SHA}:refs/heads/feature/42-publish-the-trusted-change" in push
    assert all("secret-token" not in value for args, kwargs in calls for value in args)
    gh_create = next(args for args, _ in calls if args[:3] == ("gh", "pr", "create"))
    assert all(option in gh_create for option in ("--title", "--body-file", "--head", "--base", "--repo"))
    assert not any("merge" in args for args, _ in calls)
    link = next((args, kwargs) for args, kwargs in calls if any(value.endswith("tasks.py") for value in args[1:]))
    child_env = link[1]["env"]
    assert child_env["GH_TOKEN"] == "secret-token"
    assert "OPENAI_API_KEY" not in child_env


def test_trusted_tasks_script_is_resolved_from_repository_root() -> None:
    trusted_root = Path(publisher.__file__).resolve().parents[3]
    assert trusted_root == ROOT
    assert (trusted_root / "harness" / "scripts" / "tasks" / "tasks.py").is_file()


def test_child_environments_remove_provider_secrets_authority_and_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret-token")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "provider-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "wrong-token")
    monkeypatch.setenv("GH_HOST", "github.example.invalid")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "remote.origin.url")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://evil.invalid/repo")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")

    gh_environment = publisher._auth_environment("gh")
    git_environment = publisher._auth_environment("git")

    for child_environment in (gh_environment, git_environment):
        assert child_environment["GH_TOKEN"] == "secret-token"
        assert "OPENAI_API_KEY" not in child_environment
        assert "ANTHROPIC_API_KEY" not in child_environment
        assert "GITHUB_TOKEN" not in child_environment
        assert "GH_HOST" not in child_environment
        assert "GIT_CONFIG_COUNT" not in child_environment
        assert "GIT_CONFIG_KEY_0" not in child_environment
        assert "GIT_CONFIG_VALUE_0" not in child_environment
        assert "HTTPS_PROXY" not in child_environment


def test_publisher_rejects_authenticated_actor_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret-token")
    monkeypatch.setenv("AES_TRUSTED_ACTOR", "aes-queue-bot")
    environment = publisher._auth_environment("gh")
    monkeypatch.setattr(
        publisher,
        "_run",
        lambda *args, **kwargs: _git_result(tuple(args[0]), stdout="different-bot\n"),
    )
    with pytest.raises(publisher.PublisherError, match="untrusted_actor"):
        publisher._verify_authenticated_actor(environment)


def test_askpass_is_removed_after_push(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _install_happy_seams(monkeypatch, tmp_path)
    observed: list[str] = []
    observed_environment: list[dict[str, str]] = []
    observed_snapshot: list[dict[str, str]] = []

    def observe_push(argv, **kwargs):
        observed.append(kwargs["env"]["GIT_ASKPASS"])
        observed_environment.append(kwargs["env"])
        observed_snapshot.append(dict(kwargs["env"]))
        assert Path(observed[-1]).is_file()
        assert "secret-token" not in Path(observed[-1]).read_text(encoding="utf-8")
        assert "GH_TOKEN" not in kwargs["env"]
        assert kwargs["env"][publisher._ASKPASS_TOKEN_ENV] == "secret-token"
        assert all("secret-token" not in value for value in argv)
        return _git_result(tuple(argv))

    original = publisher.subprocess.run

    def fake_run(argv, **kwargs):
        args = tuple(str(part) for part in argv)
        if args[:2] == ("git", "-C") and "push" in args:
            return observe_push(argv, **kwargs)
        return original(argv, **kwargs)

    monkeypatch.setattr(publisher.subprocess, "run", fake_run)
    publisher.publish("owner/repo", 42, "execution-1", tmp_path)
    assert observed and not Path(observed[0]).exists()
    assert observed_snapshot[0][publisher._ASKPASS_TOKEN_ENV] == "secret-token"
    assert "GH_TOKEN" not in observed_environment[0]
    assert publisher._ASKPASS_TOKEN_ENV not in observed_environment[0]
    assert "GIT_ASKPASS" not in observed_environment[0]


def test_askpass_helper_reads_only_dedicated_child_environment_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret-token")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    base_environment = publisher._auth_environment("git")

    with publisher._askpass_environment(base_environment) as child_environment:
        helper = Path(child_environment["GIT_ASKPASS"])
        helper_contents = helper.read_text(encoding="utf-8")
        assert "secret-token" not in helper_contents
        assert publisher._ASKPASS_TOKEN_ENV in helper_contents
        assert "GH_TOKEN" not in child_environment
        assert child_environment[publisher._ASKPASS_TOKEN_ENV] == "secret-token"
        password = subprocess.run(
            (str(helper), "Password:"),
            env=child_environment,
            check=True,
            capture_output=True,
            text=True,
        )
        assert password.stdout == "secret-token\n"

    assert publisher._ASKPASS_TOKEN_ENV not in child_environment
    assert "GIT_ASKPASS" not in child_environment
    assert base_environment["GH_TOKEN"] == "secret-token"


def test_existing_pr_is_reused_without_create_or_duplicate_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_happy_seams(monkeypatch, tmp_path)
    original = publisher.subprocess.run

    def fake_run(argv, **kwargs):
        args = tuple(str(part) for part in argv)
        if args[:3] == ("gh", "pr", "list"):
            return _git_result(
                args,
                stdout=json.dumps([
                    {
                        "number": 99,
                        "url": "https://github.com/owner/repo/pull/99",
                        "headRefName": "feature/42-publish-the-trusted-change",
                        "headRefOid": HEAD_SHA,
                        "baseRefName": "develop",
                    }
                ]),
            )
        return original(argv, **kwargs)

    monkeypatch.setattr(publisher.subprocess, "run", fake_run)
    result = publisher.publish("owner/repo", 42, "execution-1", tmp_path)

    assert result.existing is True
    assert not any(args[:3] == ("gh", "pr", "create") for args, _ in calls)
    assert not any("push" in args for args, _ in calls)


def test_link_failure_keeps_recoverable_pr_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _install_happy_seams(monkeypatch, tmp_path)
    original = publisher.subprocess.run

    def fake_run(argv, **kwargs):
        args = tuple(str(part) for part in argv)
        if any(value.endswith("tasks.py") for value in args[1:]):
            return _git_result(args, returncode=2)
        return original(argv, **kwargs)

    monkeypatch.setattr(publisher.subprocess, "run", fake_run)
    with pytest.raises(publisher.PublisherError) as caught:
        publisher.publish("owner/repo", 42, "execution-1", tmp_path)
    assert caught.value.phase == "link"
    assert caught.value.recoverable is True
    assert caught.value.pr_url == "https://github.com/owner/repo/pull/99"


def test_changed_head_is_rejected_before_push(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _install_happy_seams(monkeypatch, tmp_path, head_shas=[HEAD_SHA, NEXT_HEAD_SHA])

    with pytest.raises(publisher.PublisherError) as caught:
        publisher.publish("owner/repo", 42, "execution-1", tmp_path)

    assert caught.value.code == "changed_head"
    assert not any("push" in args for args, _ in calls)


def test_command_stderr_is_not_exposed_in_publisher_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_happy_seams(monkeypatch, tmp_path)
    original = publisher.subprocess.run

    def fake_run(argv, **kwargs):
        args = tuple(str(part) for part in argv)
        if args[:3] == ("gh", "pr", "list"):
            return _git_result(args, returncode=2, stderr="token=secret-token")
        return original(argv, **kwargs)

    monkeypatch.setattr(publisher.subprocess, "run", fake_run)
    with pytest.raises(publisher.PublisherError) as caught:
        publisher.publish("owner/repo", 42, "execution-1", tmp_path)

    assert "secret-token" not in str(caught.value)
    assert caught.value.code == "pr_lookup_failed"


def test_scope_or_claim_failure_happens_before_push(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _install_happy_seams(monkeypatch, tmp_path)
    monkeypatch.setattr(
        publisher.task_claim,
        "validate_claim",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("wrong owner")),
    )
    with pytest.raises(publisher.PublisherError) as caught:
        publisher.publish("owner/repo", 42, "execution-1", tmp_path)
    assert caught.value.phase == "claim"
    assert not any("push" in args for args, _ in calls)
