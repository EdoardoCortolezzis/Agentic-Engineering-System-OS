from __future__ import annotations

from datetime import datetime, timezone
import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / "harness" / "scripts" / "tasks"
sys.path.insert(0, str(TASKS))

from dispatch import DispatchError, parse, validate  # noqa: E402
from dispatcher import QueueConfig, select_tasks  # noqa: E402
import dispatcher  # noqa: E402
import tasks as tasks_cli  # noqa: E402
from contract import Contract  # noqa: E402
from readiness import Task  # noqa: E402


def body(block: str) -> str:
    return f"<!-- aes:dispatch -->\n{block}\n<!-- /aes:dispatch -->"


def test_dispatch_parser_is_strict_and_requires_one_block() -> None:
    metadata = parse(body("dispatch: auto\npriority: high\nnot_before:\nbudget: 3"))
    assert metadata.dispatch == "auto"
    assert metadata.priority == "high"
    assert metadata.budget == 3
    with pytest.raises(DispatchError):
        parse(body("dispatch: auto\nunknown: value\npriority: normal\nbudget: 1"))
    with pytest.raises(DispatchError):
        parse(body("dispatch: auto\npriority: normal\nbudget: 1") + body("dispatch: auto"))


def test_dispatch_validation_fails_closed_for_auto_without_profile_or_budget() -> None:
    metadata = parse(body("dispatch: auto\npriority: normal\nnot_before:\nbudget:"))
    reasons = validate(metadata, ("aes:ready", "role:backend"))
    assert "budget" in " ".join(reasons)
    assert any("autonomy:worker" in reason for reason in reasons)


def _task(
    number: int,
    priority: str = "normal",
    budget: int = 2,
    *,
    author: str = "edo",
) -> Task:
    metadata = parse(body(f"dispatch: auto\npriority: {priority}\nnot_before:\nbudget: {budget}"))
    return Task(
        number=number,
        repo_slug="owner/repo",
        title=f"Task {number}",
        state="open",
        labels=("aes:ready", "role:backend", "autonomy:worker"),
        contract=Contract(None, (), "", (), ("done",)),
        dispatch=metadata,
        created_at=datetime(2026, 1, number, tzinfo=timezone.utc),
        author=author,
    )


def test_selection_respects_priority_cap_and_budget_without_partial_reservation() -> None:
    config = QueueConfig(enabled=True, max_concurrency=2, max_concurrency_per_repo=2, budget=3)
    selected, skipped = select_tasks(
        [_task(1, "normal", 2), _task(2, "critical", 2)],
        config=config,
        active_global=0,
        active_repo=0,
        budget_used=0,
        now=datetime(2026, 1, 31, tzinfo=timezone.utc),
        allowed_authors="edo",
    )
    assert [task.number for task in selected] == [2]
    assert any(task.number == 1 and "budget" in reason for task, reason in skipped)


def test_selection_leaves_future_task_in_ready_queue() -> None:
    metadata = parse(body("dispatch: auto\npriority: normal\nnot_before: 2099-01-01T00:00:00Z\nbudget: 1"))
    task = _task(1)
    task = Task(
        number=task.number,
        repo_slug=task.repo_slug,
        title=task.title,
        state=task.state,
        labels=task.labels,
        contract=task.contract,
        dispatch=metadata,
        created_at=task.created_at,
        author=task.author,
    )
    config = QueueConfig(enabled=True, max_concurrency=1, max_concurrency_per_repo=1, budget=5)
    selected, skipped = select_tasks(
        [task],
        config,
        0,
        0,
        0,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        allowed_authors="edo",
    )
    assert selected == []
    assert "not_before" in skipped[0][1]


def test_author_gate_is_exact_and_fail_closed() -> None:
    assert dispatcher.author_allowed("edo", "edo,alice")
    assert not dispatcher.author_allowed("ed", "edo,alice")
    assert not dispatcher.author_allowed("edo", "")


def test_selection_enforces_author_gate_for_every_dispatch_mode() -> None:
    task = _task(4, author="attacker")
    config = QueueConfig(enabled=True, max_concurrency=2, max_concurrency_per_repo=2, budget=5)

    selected, skipped = select_tasks(
        [task], config, 0, 0, 0, allowed_authors="edo"
    )
    assert selected == []
    assert skipped[0][0] is task
    assert "author" in skipped[0][1]

    selected, skipped = select_tasks([_task(5)], config, 0, 0, 0)
    assert selected == []
    assert "allowlist" in skipped[0][1]


def test_missing_author_is_fail_closed_even_with_allowlist() -> None:
    task = _task(6, author="")
    config = QueueConfig(enabled=True, max_concurrency=1, max_concurrency_per_repo=1, budget=5)
    selected, skipped = select_tasks([task], config, 0, 0, 0, allowed_authors="edo")
    assert selected == []
    assert "author" in skipped[0][1]


def test_queue_runtime_uses_remote_claim_not_local_lease() -> None:
    source = (TASKS / "dispatcher.py").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "aes-queue.yml").read_text(encoding="utf-8")
    assert "AES_QUEUE_STATE_FILE" not in source
    assert "queue-state.json" not in source
    assert "AES_QUEUE_ALLOWED_AUTHORS" in workflow
    assert "self-hosted" in workflow
    assert "ubuntu-latest" not in workflow
    assert "GITHUB_EVENT_NAME" in workflow


def test_command_plan_counts_remote_claims_and_emits_worker_matrix(monkeypatch, capsys) -> None:
    task = _task(7, "high", 2)
    monkeypatch.setenv("AES_QUEUE_ENABLED", "true")
    monkeypatch.setenv("AES_QUEUE_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("AES_QUEUE_MAX_CONCURRENCY_PER_REPO", "2")
    monkeypatch.setenv("AES_QUEUE_BUDGET", "5")
    monkeypatch.setenv("AES_QUEUE_ALLOWED_AUTHORS", "edo")
    monkeypatch.setattr(dispatcher.provider_github, "list_tasks", lambda repo, labels: [task] if labels == ("aes:ready",) else [])
    monkeypatch.setattr(dispatcher.provider_github, "get_states", lambda repo, deps: {})
    result = dispatcher.command_plan(SimpleNamespace(repo="owner/repo", apply=False))
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tasks"][0]["issue"] == 7
    assert payload["tasks"][0]["repo"] == "owner/repo"


def test_apply_claims_before_emitting_worker_matrix(monkeypatch, capsys) -> None:
    task = _task(10, "high", 2)
    calls: list[tuple[str, int, str, str]] = []
    monkeypatch.setenv("AES_QUEUE_ENABLED", "true")
    monkeypatch.setenv("AES_QUEUE_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("AES_QUEUE_MAX_CONCURRENCY_PER_REPO", "2")
    monkeypatch.setenv("AES_QUEUE_BUDGET", "5")
    monkeypatch.setenv("AES_QUEUE_ALLOWED_AUTHORS", "edo")
    monkeypatch.setattr(
        dispatcher.provider_github,
        "list_tasks",
        lambda repo, labels: [task] if labels == ("aes:ready",) else [],
    )
    monkeypatch.setattr(dispatcher.provider_github, "get_states", lambda repo, deps: {})

    def claim_remote(repo: str, number: int, base: str, *, execution_id: str):
        calls.append((repo, number, base, execution_id))
        return dispatcher.task_claim.WON

    monkeypatch.setattr(dispatcher.task_claim, "claim_remote", claim_remote)
    result = dispatcher.command_plan(SimpleNamespace(repo="owner/repo", apply=True))
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tasks"][0]["issue"] == 10
    assert calls == [("owner/repo", 10, "develop", payload["tasks"][0]["execution_id"])]


def test_active_budget_ignores_closed_or_invalid_claims(monkeypatch, capsys) -> None:
    ready = _task(7, "high", 2)
    closed = Task(
        **{
            **ready.__dict__,
            "number": 8,
            "state": "closed",
            "author": "edo",
        }
    )
    invalid = Task(
        **{
            **ready.__dict__,
            "number": 9,
            "dispatch_error": "dispatch block not found",
            "author": "edo",
        }
    )
    monkeypatch.setenv("AES_QUEUE_ENABLED", "true")
    monkeypatch.setenv("AES_QUEUE_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("AES_QUEUE_MAX_CONCURRENCY_PER_REPO", "2")
    monkeypatch.setenv("AES_QUEUE_BUDGET", "3")
    monkeypatch.setenv("AES_QUEUE_ALLOWED_AUTHORS", "edo")
    monkeypatch.setattr(
        dispatcher.provider_github,
        "list_tasks",
        lambda repo, labels: [ready] if labels == ("aes:ready",) else [closed, invalid],
    )
    monkeypatch.setattr(dispatcher.provider_github, "get_states", lambda repo, deps: {})
    result = dispatcher.command_plan(SimpleNamespace(repo="owner/repo", apply=False))
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tasks"][0]["issue"] == 7


def test_active_budget_counts_only_claims_with_contract_and_author_gate(
    monkeypatch, capsys
) -> None:
    ready = _task(11, "high", 2)
    missing_contract = Task(
        **{
            **ready.__dict__,
            "number": 12,
            "contract": None,
            "author": "edo",
        }
    )
    unauthorized = Task(
        **{
            **ready.__dict__,
            "number": 13,
            "author": "attacker",
        }
    )
    monkeypatch.setenv("AES_QUEUE_ENABLED", "true")
    monkeypatch.setenv("AES_QUEUE_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("AES_QUEUE_MAX_CONCURRENCY_PER_REPO", "1")
    monkeypatch.setenv("AES_QUEUE_BUDGET", "2")
    monkeypatch.setenv("AES_QUEUE_ALLOWED_AUTHORS", "edo")
    monkeypatch.setattr(
        dispatcher.provider_github,
        "list_tasks",
        lambda repo, labels: [ready]
        if labels == ("aes:ready",)
        else [missing_contract, unauthorized],
    )
    monkeypatch.setattr(dispatcher.provider_github, "get_states", lambda repo, deps: {})

    result = dispatcher.command_plan(SimpleNamespace(repo="owner/repo", apply=False))

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["issue"] for item in payload["tasks"]] == [11]


def test_claim_failure_is_reported_and_emits_human_event(monkeypatch, capsys) -> None:
    task = _task(14, "high", 2)
    events: list[tuple[int, str]] = []
    monkeypatch.setenv("AES_QUEUE_ENABLED", "true")
    monkeypatch.setenv("AES_QUEUE_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("AES_QUEUE_MAX_CONCURRENCY_PER_REPO", "1")
    monkeypatch.setenv("AES_QUEUE_BUDGET", "2")
    monkeypatch.setenv("AES_QUEUE_ALLOWED_AUTHORS", "edo")
    monkeypatch.setattr(
        dispatcher.provider_github,
        "list_tasks",
        lambda repo, labels: [task] if labels == ("aes:ready",) else [],
    )
    monkeypatch.setattr(dispatcher.provider_github, "get_states", lambda repo, deps: {})
    monkeypatch.setattr(
        dispatcher.task_claim,
        "claim_remote",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            dispatcher.task_claim.ClaimError("permission denied")
        ),
    )
    monkeypatch.setattr(
        tasks_cli,
        "notify_needs_human",
        lambda repo, number, execution_id, reason, question: events.append((number, reason))
        or True,
    )

    result = dispatcher.command_plan(SimpleNamespace(repo="owner/repo", apply=True))

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tasks"] == []
    assert "remote claim failed" in payload["skipped"][0]["reason"]
    assert events and events[0][0] == 14


def test_workflow_planner_claims_before_worker_and_does_not_use_dummy_codex_secret() -> None:
    workflow = (ROOT / ".github" / "workflows" / "aes-queue.yml").read_text(encoding="utf-8")
    dispatcher_source = (TASKS / "dispatcher.py").read_text(encoding="utf-8")
    worker_source = (TASKS / "worker.py").read_text(encoding="utf-8")
    assert "claim_remote" in dispatcher_source
    assert "--apply" in workflow
    assert "AES_CODEX_AUTH" not in workflow
    assert "AES_CODEX_AUTH" not in worker_source
    assert "AES_CODEX_MANAGED_CREDENTIALS: ${{ vars.AES_CODEX_MANAGED_CREDENTIALS }}" in workflow
    assert "CODEX_HOME: ${{ vars.AES_CODEX_HOME }}" in workflow
    assert "AES_CLAUDE_MANAGED_SANDBOX: ${{ vars.AES_CLAUDE_MANAGED_SANDBOX }}" in workflow
    assert 'test "${AES_CODEX_MANAGED_CREDENTIALS}" = \'1\'' in workflow
    assert '"adopt"' in worker_source
