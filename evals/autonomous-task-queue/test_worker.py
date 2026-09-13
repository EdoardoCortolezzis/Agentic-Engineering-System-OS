from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / "harness" / "scripts" / "tasks"
sys.path.insert(0, str(TASKS))

from contract import Contract  # noqa: E402
from dispatch import DispatchMetadata  # noqa: E402
from readiness import Task  # noqa: E402
import worker  # noqa: E402


def _task(*, contract: Contract | None = Contract(None, (), "", (), ())) -> Task:
    return Task(
        number=42,
        repo_slug="owner/repo",
        title="Worker task",
        state="open",
        labels=("aes:claimed", "role:backend", "autonomy:worker"),
        contract=contract,
        dispatch=DispatchMetadata("auto", "normal", None, 1),
    )


def _worker_env(monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setenv("AES_PRODUCT_MANAGER", "edo")
    monkeypatch.setenv("AES_TRUSTED_ACTOR", "aes-queue-bot")
    monkeypatch.setenv("AES_SELF_HOSTED", "true")
    monkeypatch.setenv("RUNNER_NAME", "runner-1")
    monkeypatch.setattr(worker, "_verify_authenticated_actor", lambda: None)


def test_missing_contract_transitions_claimed_worker_to_needs_human(monkeypatch) -> None:
    _worker_env(monkeypatch)
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(worker.provider_github, "get_task", lambda repo, issue: _task(contract=None))
    monkeypatch.setattr(
        worker,
        "_needs_human",
        lambda root, repo, issue, execution_id, reason, question: events.append((reason, question)),
    )
    monkeypatch.setattr(
        worker,
        "_run_tasks",
        lambda *args: (_ for _ in ()).throw(AssertionError("adopt must not run")),
    )

    result = worker.run_worker("owner/repo", 42, "execution-1")

    assert result == 2
    assert events and "contract" in events[0][0].lower()


def test_task_brief_contains_rest_and_contract_metadata_without_network_dependency(tmp_path) -> None:
    task = Task(
        number=42,
        repo_slug="owner/private-repo",
        title="Implement worker brief",
        state="open",
        labels=("aes:ready", "role:backend", "autonomy:worker"),
        contract=Contract(
            "owner/private-repo",
            ("harness/scripts/tasks/",),
            "Keep the provider seam.",
            ("owner/private-repo#7",),
            ("tests pass",),
        ),
        dispatch=DispatchMetadata("auto", "high", None, 3),
        body="untrusted issue body",
        author="edo",
    )

    brief = worker._write_task_brief(tmp_path, task, "execution-1")
    payload = json.loads(brief.read_text(encoding="utf-8"))

    assert payload["repo"] == "owner/private-repo"
    assert payload["body"] == "untrusted issue body"
    assert payload["author"] == "edo"
    assert payload["contract"]["paths"] == ["harness/scripts/tasks/"]
    assert payload["dispatch"] == {
        "dispatch": "auto",
        "priority": "high",
        "not_before": None,
        "budget": 3,
    }
    assert brief.stat().st_mode & 0o777 == 0o600

    brief.unlink()


def test_adoption_failure_is_reported_as_human_gate(monkeypatch) -> None:
    _worker_env(monkeypatch)
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(worker.provider_github, "get_task", lambda repo, issue: _task())
    monkeypatch.setattr(
        worker,
        "_run_tasks",
        lambda *args: subprocess.CompletedProcess(args, 1, "", "adopt failed"),
    )
    monkeypatch.setattr(
        worker,
        "_needs_human",
        lambda root, repo, issue, execution_id, reason, question: events.append((reason, question)),
    )

    result = worker.run_worker("owner/repo", 42, "execution-1")

    assert result == 1
    assert events and "adopt" in events[0][0].lower()


def test_worker_removes_brief_on_keyboard_interrupt(monkeypatch, tmp_path) -> None:
    artifact_root = tmp_path / ".agent" / "tasks" / "artifacts" / "execution-1"

    def interrupt(*args, **kwargs) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(worker.json, "dump", interrupt)

    try:
        worker._write_task_brief(
            tmp_path,
            _task(),
            "execution-1",
            artifact_root=artifact_root,
        )
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("brief interruption was swallowed")

    assert list(artifact_root.glob("*")) == []


def test_resume_requires_existing_state_without_creating_artifacts(tmp_path) -> None:
    with pytest.raises(worker.WorkerError, match="trusted artifact root"):
        worker._artifact_root(tmp_path, "execution-1", resume=True)
    assert not (tmp_path / ".agent").exists()


def test_resume_reuses_matching_state_and_brief(tmp_path) -> None:
    execution_id = "execution-1"
    artifact_root = tmp_path / ".agent" / "tasks" / "artifacts" / execution_id
    state_directory = artifact_root / ".agent" / "orchestration" / execution_id
    state_directory.mkdir(parents=True)
    brief = artifact_root / "brief.json"
    brief.write_text('{"contract": {"paths": ["src"]}}', encoding="utf-8")
    (state_directory / "state.json").write_text(
        json.dumps(
            {
                "schema": "aes.orchestration-state.v1",
                "execution_id": execution_id,
                "state": "waiting_provider",
                "metadata": {"brief_path": str(brief)},
            }
        ),
        encoding="utf-8",
    )

    assert worker._artifact_root(tmp_path, execution_id, resume=True) == artifact_root
    assert worker._resume_brief(artifact_root, execution_id) == brief


def test_waiting_resume_rejects_missing_ledger_without_creating_one(monkeypatch, tmp_path) -> None:
    _worker_env(monkeypatch)
    task = _task().__class__(
        **{
            **_task().__dict__,
            "labels": ("aes:waiting-provider", "aes:claimed", "role:backend", "autonomy:worker"),
        }
    )
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(worker, "_needs_human", lambda *args: events.append((args[-2], args[-1])))
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    result = worker._run_phase4(
        tmp_path,
        "owner/repo",
        42,
        "execution-1",
        "runner-1",
        task,
        worktree,
    )

    assert result == worker.EXIT_NEEDS_HUMAN
    assert events
    assert not (tmp_path / ".agent").exists()


def test_resume_lease_requires_complete_dispatcher_identity_without_remote_lookup(monkeypatch) -> None:
    sha = "a" * 40
    monkeypatch.setattr(
        worker.provider_github,
        "ref_sha",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not discover lease")),
    )

    with pytest.raises(worker.WorkerError, match="lease_ref"):
        worker._resume_lease(
            "owner/repo",
            42,
            "exec-42",
            lease_ref="aes/resume/42",
            expected_sha=None,
            lease_owner="exec-42",
        )
    lease = worker._resume_lease(
        "owner/repo",
        42,
        "exec-42",
        lease_ref="aes/resume/42",
        expected_sha=sha,
        lease_owner="exec-42",
    )
    assert lease == worker.provider_github.ResumeLease("aes/resume/42", sha, "exec-42")


def test_worker_authentication_check_uses_sanitized_environment(monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "trusted-token")
    monkeypatch.setenv("AES_TRUSTED_ACTOR", "aes-queue-bot")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-gh")
    calls: list[dict[str, str]] = []

    def fake_run(argv, **kwargs):
        calls.append(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "aes-queue-bot\n", "")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    worker._verify_authenticated_actor()
    assert calls[0]["GH_TOKEN"] == "trusted-token"
    assert "OPENAI_API_KEY" not in calls[0]
    assert calls[0]["GIT_TERMINAL_PROMPT"] == "0"
