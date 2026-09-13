from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / "harness" / "scripts" / "tasks"
sys.path.insert(0, str(TASKS))

import dispatcher  # noqa: E402
from contract import Contract  # noqa: E402
from dispatch import DispatchMetadata  # noqa: E402
from readiness import Task  # noqa: E402
import worker  # noqa: E402
from quota import QuotaConfigError  # noqa: E402


def _task(*, labels: tuple[str, ...] = ("aes:claimed", "role:backend", "autonomy:worker")) -> Task:
    return Task(
        number=42,
        repo_slug="owner/repo",
        title="Phase 4 task",
        state="open",
        labels=labels,
        contract=Contract("owner/repo", ("src",), "", (), ("tests pass",)),
        dispatch=DispatchMetadata("auto", "high", None, 1),
        body="brief",
        author="edo",
    )


def _env(monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "mock-token")
    monkeypatch.setenv("AES_PRODUCT_MANAGER", "edo")
    monkeypatch.setenv("AES_TRUSTED_ACTOR", "aes-queue-bot")
    monkeypatch.setenv("AES_SELF_HOSTED", "true")
    monkeypatch.setenv("RUNNER_NAME", "mock-runner")
    monkeypatch.setattr(worker, "_verify_authenticated_actor", lambda: None)


def _wire_runtime(
    monkeypatch,
    tmp_path: Path,
    outcome: str,
    calls: list[tuple],
    *,
    reason: str = "mock",
) -> None:
    task = _task()
    monkeypatch.setattr(worker.provider_github, "get_task", lambda repo, issue: task)
    monkeypatch.setattr(worker.task_claim, "validate_claim", lambda *a, **k: "feature/42-phase-4-task")
    monkeypatch.setattr(worker, "_run_tasks", lambda *a: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(worker, "_worktree", lambda root, issue: tmp_path / "worktree")
    monkeypatch.setattr(worker, "_worktree_branch", lambda path: "feature/42-phase-4-task")
    monkeypatch.setattr(worker, "_worktree_head", lambda path: "a" * 40)
    monkeypatch.setattr(worker.provider_github, "ref_sha", lambda repo, branch: "a" * 40)
    (tmp_path / "worktree").mkdir()
    monkeypatch.setattr(
        worker,
        "_artifact_root",
        lambda root, execution, **kwargs: tmp_path / "artifacts",
    )
    (tmp_path / "artifacts").mkdir()
    brief = tmp_path / "artifacts" / "brief.json"
    brief.write_text(json.dumps({"contract": {"paths": ["src"]}}), encoding="utf-8")
    monkeypatch.setattr(worker, "_write_task_brief", lambda *a, **k: brief)
    cache = SimpleNamespace(path=tmp_path / "quota.json")
    monkeypatch.setattr(worker, "_quota_cache", lambda root: cache)
    monkeypatch.setattr(worker, "_refresh_codex_quota", lambda cache: None)
    monkeypatch.setattr(worker, "_quota_cache_availability", lambda path: lambda provider, role: True)
    monkeypatch.setattr(worker, "ModelRouter", lambda **kwargs: object())
    monkeypatch.setattr(
        worker,
        "IssueOrchestrator",
        lambda **kwargs: SimpleNamespace(run=lambda: SimpleNamespace(status=outcome, reason=reason)),
    )
    monkeypatch.setattr(worker, "_commit_uncommitted_change", lambda *a: calls.append(("commit",)))
    monkeypatch.setattr(worker, "_contract_scope", lambda *a: calls.append(("scope",)))
    monkeypatch.setattr(worker.publisher, "publish", lambda *a: calls.append(("publish",)))


def test_worker_verified_commits_and_publishes_only_after_revalidation(monkeypatch, tmp_path) -> None:
    _env(monkeypatch)
    calls: list[tuple] = []
    _wire_runtime(monkeypatch, tmp_path, "verified", calls)

    assert worker.run_worker("owner/repo", 42, "exec-42") == 0
    assert calls == [("commit",), ("scope",), ("publish",)]


def test_worker_waiting_provider_preserves_execution_and_returns_retry(monkeypatch, tmp_path) -> None:
    _env(monkeypatch)
    calls: list[tuple] = []
    _wire_runtime(monkeypatch, tmp_path, "waiting_provider", calls)
    commands: list[tuple] = []
    monkeypatch.setattr(
        worker,
        "_run_tasks",
        lambda *args: commands.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    assert worker.run_worker("owner/repo", 42, "exec-42") == worker.EXIT_WAITING_PROVIDER
    assert any(command[1] == "waiting-provider" for command in commands)
    assert any("exec-42" in command for command in commands)
    assert calls == []


def test_worker_failed_orchestration_blocks_without_publishing(monkeypatch, tmp_path) -> None:
    _env(monkeypatch)
    calls: list[tuple] = []
    _wire_runtime(monkeypatch, tmp_path, "failed", calls)
    commands: list[tuple] = []
    monkeypatch.setattr(
        worker,
        "_run_tasks",
        lambda *args: commands.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    assert worker.run_worker("owner/repo", 42, "exec-42") == worker.EXIT_FAILED
    assert any(command[1] == "block" for command in commands)
    assert calls == []


def test_worker_rejects_head_changed_during_orchestration(monkeypatch, tmp_path) -> None:
    _env(monkeypatch)
    calls: list[tuple] = []
    _wire_runtime(monkeypatch, tmp_path, "verified", calls)
    # First read is the trusted adoption-vs-remote check; the next two are
    # the phase-4 orchestration baseline and post-run integrity check.
    heads = iter(("a" * 40, "a" * 40, "b" * 40))
    monkeypatch.setattr(worker, "_worktree_head", lambda path: next(heads))
    commands: list[tuple] = []
    monkeypatch.setattr(
        worker,
        "_run_tasks",
        lambda *args: commands.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    assert worker.run_worker("owner/repo", 42, "exec-42") == worker.EXIT_NEEDS_HUMAN
    assert any(command[1] == "needs-human" for command in commands)
    assert calls == []


def test_worker_rechecks_head_before_trusted_commit(monkeypatch, tmp_path) -> None:
    _env(monkeypatch)
    calls: list[tuple] = []
    _wire_runtime(monkeypatch, tmp_path, "verified", calls)
    # Adoption, orchestration baseline and post-run check are unchanged; only
    # the final read immediately before the trusted commit observes movement.
    heads = iter(("a" * 40, "a" * 40, "a" * 40, "b" * 40))
    monkeypatch.setattr(worker, "_worktree_head", lambda path: next(heads))
    commands: list[tuple] = []
    monkeypatch.setattr(
        worker,
        "_run_tasks",
        lambda *args: commands.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    assert worker.run_worker("owner/repo", 42, "exec-42") == worker.EXIT_NEEDS_HUMAN
    assert any(command[1] == "needs-human" for command in commands)
    assert calls == []


def test_resume_rejects_preexisting_local_commit_before_orchestration(monkeypatch, tmp_path) -> None:
    _env(monkeypatch)
    task = _task(labels=("aes:waiting-provider", "aes:claimed", "role:backend", "autonomy:worker"))
    events: list[tuple] = []
    commands: list[tuple] = []
    monkeypatch.setattr(worker.task_claim, "validate_claim", lambda *a, **k: "feature/42-phase-4-task")
    monkeypatch.setattr(
        worker,
        "_run_tasks",
        lambda *args: commands.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setattr(worker, "_worktree", lambda root, issue: worktree)
    monkeypatch.setattr(worker, "_worktree_branch", lambda path: "feature/42-phase-4-task")
    monkeypatch.setattr(worker, "_worktree_head", lambda path: "b" * 40)
    monkeypatch.setattr(worker.provider_github, "ref_sha", lambda repo, branch: "a" * 40)
    monkeypatch.setattr(
        worker,
        "_needs_human",
        lambda *args: events.append(args),
    )
    monkeypatch.setattr(
        worker,
        "_run_phase4",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("orchestration must not run")),
    )

    result = worker._run_worker_claimed(
        tmp_path,
        "owner/repo",
        42,
        "exec-42",
        "runner-1",
        task,
        resume=True,
    )

    assert result == worker.EXIT_NEEDS_HUMAN
    assert events and "HEAD" in events[0][4]
    assert [command[1] for command in commands] == ["adopt"]


def test_model_outcome_reason_is_not_sent_to_issue_comment(monkeypatch, tmp_path) -> None:
    _env(monkeypatch)
    calls: list[tuple] = []
    _wire_runtime(
        monkeypatch,
        tmp_path,
        "needs_human",
        calls,
        reason="MODEL_SECRET verdict details token=do-not-publish",
    )
    commands: list[tuple] = []
    monkeypatch.setattr(
        worker,
        "_run_tasks",
        lambda *args: commands.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    assert worker.run_worker("owner/repo", 42, "exec-42") == worker.EXIT_NEEDS_HUMAN
    rendered = " ".join(str(item) for command in commands for item in command)
    assert "MODEL_SECRET" not in rendered
    assert "token=do-not-publish" not in rendered
    assert "exec-42" in rendered


def test_dispatcher_resumes_waiting_claim_with_same_execution_without_capacity(monkeypatch, capsys) -> None:
    _env(monkeypatch)
    monkeypatch.setenv("AES_QUEUE_ENABLED", "true")
    monkeypatch.setenv("AES_QUEUE_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("AES_QUEUE_MAX_CONCURRENCY_PER_REPO", "1")
    monkeypatch.setenv("AES_QUEUE_BUDGET", "1")
    monkeypatch.setenv("AES_QUEUE_ALLOWED_AUTHORS", "edo")
    waiting = _task(labels=("aes:waiting-provider", "aes:claimed", "role:backend", "autonomy:worker"))
    comments = (SimpleNamespace(body="AES claim: execution_id=exec-42"),)
    monkeypatch.setattr(
        dispatcher.provider_github,
        "list_tasks",
        lambda repo, labels: [waiting] if labels in (("aes:waiting-provider",), ("aes:claimed",)) else [],
    )
    monkeypatch.setattr(dispatcher.provider_github, "get_task", lambda repo, issue: waiting)
    monkeypatch.setattr(dispatcher.provider_github, "list_issue_comments", lambda repo, issue: comments)
    monkeypatch.setattr(dispatcher.task_claim, "validate_claim", lambda *a, **k: "feature/42-phase-4-task")
    monkeypatch.setattr(
        dispatcher.task_claim,
        "acquire_resume_lease",
        lambda *a, **k: dispatcher.provider_github.ResumeLease(
            "aes/resume/42", "a" * 40, "exec-42"
        ),
    )

    assert dispatcher.command_plan(SimpleNamespace(repo="owner/repo", apply=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tasks"] == [
        {
            "repo": "owner/repo",
            "issue": 42,
            "execution_id": "exec-42",
            "resume": True,
            "lease": {
                "owner": "exec-42",
                "ref": "aes/resume/42",
                "expected_sha": "a" * 40,
            },
            "lease_owner": "exec-42",
            "lease_ref": "aes/resume/42",
            "lease_expected_sha": "a" * 40,
        }
    ]


def test_worker_quota_cache_requires_shared_path_outside_checkout(monkeypatch, tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    shared = tmp_path / "runner-state" / "quota.json"

    monkeypatch.delenv("AES_QUOTA_CACHE_PATH", raising=False)
    monkeypatch.delenv("AES_QUOTA_CACHE_ROOT", raising=False)
    with pytest.raises(QuotaConfigError):
        worker._quota_cache(checkout)

    monkeypatch.setenv("AES_QUOTA_CACHE_PATH", str(shared))
    monkeypatch.setenv("AES_QUOTA_CACHE_ROOT", str(shared.parent))
    cache = worker._quota_cache(checkout)
    assert cache.path == shared
    assert cache.root == shared.parent

    monkeypatch.setenv("AES_QUOTA_CACHE_PATH", ".agent/tasks/quota.json")
    monkeypatch.setenv("AES_QUOTA_CACHE_ROOT", str(checkout / ".agent" / "tasks"))
    with pytest.raises(QuotaConfigError, match="outside"):
        worker._quota_cache(checkout)


def test_worker_rejects_cache_symlink_ancestor_resolving_into_checkout(
    monkeypatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    # The lexical root is outside the checkout, but its ancestor resolves
    # into the checkout.  This was previously accepted and then created the
    # shared cache under model-controlled ``.agent`` state.
    redirected = tmp_path / "runner-state"
    redirected.symlink_to(checkout, target_is_directory=True)
    monkeypatch.setenv("AES_QUOTA_CACHE_ROOT", str(redirected / ".agent" / "tasks"))
    monkeypatch.setenv("AES_QUOTA_CACHE_PATH", "quota.json")

    with pytest.raises(QuotaConfigError, match="outside"):
        worker._quota_cache(checkout)
