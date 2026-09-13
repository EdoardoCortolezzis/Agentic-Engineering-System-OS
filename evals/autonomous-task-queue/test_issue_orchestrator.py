"""Deterministic Phase 3-A controller/worker/verifier execution evals."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Mapping, Sequence

import pytest


ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / "harness" / "scripts" / "tasks"
sys.path.insert(0, str(TASKS))

from issue_orchestrator import (  # noqa: E402
    EXIT_FAILED,
    EXIT_NEEDS_HUMAN,
    EXIT_VERIFIED,
    EXIT_WAITING_PROVIDER,
    IssueOrchestrator,
)
from orchestration import ArtifactLedger, OrchestrationError  # noqa: E402
from model_router import (  # noqa: E402
    Availability,
    ModelRouter,
    ProcessResult,
    RoutingPolicy,
)


@pytest.fixture(autouse=True)
def verified_managed_sandbox_for_native_fallback_tests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("AES_CLAUDE_MANAGED_SANDBOX", "1")
    codex_home = tmp_path.parent / f"{tmp_path.name}-codex-home"
    codex_home.mkdir(exist_ok=True)
    monkeypatch.setenv("AES_CODEX_MANAGED_CREDENTIALS", "1")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))


def _plan(execution_id: str = "aes-e2e") -> str:
    return json.dumps(
        {
            "schema": "aes.orchestration-plan.v1",
            "execution_id": execution_id,
            "controller": {
                "primary": {
                    "provider": "openai",
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "medium",
                },
                "fallback": {
                    "provider": "anthropic",
                    "model": "claude-opus-5",
                    "reasoning_effort": "medium",
                },
            },
            "worker": {
                "primary": {
                    "provider": "openai",
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "high",
                },
                "fallback": {
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "reasoning_effort": "high",
                },
            },
            "subtasks": [
                {
                    "id": "first",
                    "title": "First worker",
                    "prompt": "Implement first bounded change",
                    "paths": ["src/first.py"],
                    "dependencies": [],
                    "difficulty": "high",
                    "reasoning_effort": "high",
                },
                {
                    "id": "second",
                    "title": "Second worker",
                    "prompt": "Implement second bounded change",
                    "paths": ["src/second.py"],
                    "dependencies": ["first"],
                    "difficulty": "high",
                    "reasoning_effort": "xhigh",
                },
            ],
        }
    )


def _verdict(execution_id: str = "aes-e2e", value: str = "verified") -> str:
    return json.dumps(
        {
            "schema": "aes.orchestration-verdict.v1",
            "execution_id": execution_id,
            "verdict": value,
            "summary": f"final {value}",
            "checks": [
                {"name": "deterministic tests", "status": "passed"},
                "scope checked",
            ],
        }
    )


@dataclass
class FakeRunner:
    responses: list[ProcessResult]

    def __post_init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str, Mapping[str, str], str | None]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: str,
        env: Mapping[str, str],
        cwd: str | None,
        timeout: float | None,
    ) -> ProcessResult:
        del timeout
        self.calls.append((tuple(argv), stdin, dict(env), cwd))
        if not self.responses:
            raise AssertionError("provider invoked after deterministic responses ended")
        return self.responses.pop(0)


def _orchestrator(tmp_path: Path, runner: FakeRunner, *, execution_id: str = "aes-e2e") -> IssueOrchestrator:
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(exist_ok=True)
    brief = artifact_root / "brief.txt"
    brief.write_text(
        json.dumps(
            {
                "schema": "aes.worker-task-brief.v1",
                "body": "bounded task brief",
                "contract": {"paths": ["src"]},
            }
        ),
        encoding="utf-8",
    )
    quota = artifact_root / "quota.json"
    quota.write_text("{}", encoding="utf-8")
    router = ModelRouter(
        RoutingPolicy(transient_retries=0),
        runner=runner,
        availability=lambda provider, role: Availability(True),
    )
    return IssueOrchestrator(
        brief,
        worktree,
        execution_id,
        quota,
        artifact_root=artifact_root,
        router=router,
    )


def test_controller_two_workers_high_xhigh_then_verified(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            ProcessResult(0, _plan(), ""),
            ProcessResult(0, "first completed", ""),
            ProcessResult(0, "second completed", ""),
            ProcessResult(0, _verdict(), ""),
        ]
    )
    outcome = _orchestrator(tmp_path, runner).run()

    assert outcome.status == "verified"
    assert outcome.exit_code == EXIT_VERIFIED
    assert [call[0][0] for call in runner.calls] == ["codex"] * 4
    assert "model_reasoning_effort=high" in runner.calls[1][0]
    assert "model_reasoning_effort=xhigh" in runner.calls[2][0]
    assert runner.calls[1][3] != str(tmp_path.resolve())
    assert "aes-control-" in runner.calls[1][3]
    assert not Path(runner.calls[1][3]).exists(), "control cwd must be cleaned up"
    assert runner.calls[1][1].find("Do not invoke GitHub") >= 0

    results = json.loads(
        (
            tmp_path / "artifacts/.agent/orchestration/aes-e2e/results/second.json"
        ).read_text()
    )
    assert results["result"]["provider"] == "openai"
    assert results["result"]["model"] == "gpt-5.6-luna"
    assert results["result"]["effort"] == "xhigh"


def test_provider_wait_is_persisted_and_resume_skips_completed_workers(tmp_path: Path) -> None:
    first_runner = FakeRunner(
        [
            ProcessResult(0, _plan(), ""),
            ProcessResult(0, "first completed", ""),
            ProcessResult(429, "quota exceeded", ""),
            ProcessResult(429, "quota exceeded", ""),
        ]
    )
    orchestrator = _orchestrator(tmp_path, first_runner)
    first = orchestrator.run()
    assert first.status == "waiting_provider"
    assert first.exit_code == EXIT_WAITING_PROVIDER
    assert json.loads(
        (tmp_path / "artifacts/.agent/orchestration/aes-e2e/state.json").read_text()
    )["state"] == "waiting_provider"

    second_runner = FakeRunner(
        [
            ProcessResult(0, "second completed", ""),
            ProcessResult(0, _verdict(), ""),
        ]
    )
    resumed = _orchestrator(tmp_path, second_runner).run()
    assert resumed.status == "verified"
    # The first worker result is durable; resume starts with second worker.
    assert len(second_runner.calls) == 2
    assert "Subtask id: second" in second_runner.calls[0][1]


def test_controller_and_worker_fallbacks_are_separate_sessions(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            ProcessResult(429, "rate limit exceeded", ""),
            ProcessResult(0, _plan(), ""),
            ProcessResult(429, "rate limit exceeded", ""),
            ProcessResult(0, "first fallback", ""),
            ProcessResult(0, "second primary", ""),
            ProcessResult(0, _verdict(), ""),
        ]
    )
    outcome = _orchestrator(tmp_path, runner).run()
    assert outcome.status == "verified"
    assert [call[0][0] for call in runner.calls] == [
        "codex",
        "claude",
        "codex",
        "claude",
        "codex",
        "codex",
    ]
    assert "--effort" in runner.calls[3][0]
    assert "high" in runner.calls[3][0]


@pytest.mark.parametrize("verdict, expected_status, expected_code", [("failed", "failed", EXIT_FAILED), ("needs_human", "needs_human", EXIT_NEEDS_HUMAN)])
def test_final_failure_is_terminal_and_idempotent(
    tmp_path: Path, verdict: str, expected_status: str, expected_code: int
) -> None:
    runner = FakeRunner(
        [
            ProcessResult(0, _plan(), ""),
            ProcessResult(0, "first completed", ""),
            ProcessResult(0, "second completed", ""),
            ProcessResult(0, _verdict(value=verdict), ""),
        ]
    )
    orchestrator = _orchestrator(tmp_path, runner)
    outcome = orchestrator.run()
    assert outcome.status == expected_status
    assert outcome.exit_code == expected_code
    call_count = len(runner.calls)

    # A retry of a terminal ledger does not create duplicate sessions or
    # overwrite the immutable verdict/results.
    resumed = orchestrator.run()
    assert resumed.status == expected_status
    assert len(runner.calls) == call_count


def test_brief_must_be_inside_artifact_root_and_not_a_symlink(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    outside = tmp_path.parent / "outside-brief.txt"
    outside.write_text(
        json.dumps({"contract": {"paths": ["src"]}}), encoding="utf-8"
    )
    router = ModelRouter(
        runner=FakeRunner([]),
        availability=lambda provider, role: Availability(True),
    )
    with pytest.raises(OrchestrationError, match="inside artifact_root"):
        IssueOrchestrator(
            outside,
            worktree,
            "aes-brief-boundary",
            artifact_root=artifact_root,
            router=router,
        )

    link = tmp_path / "brief-link.json"
    link.symlink_to(outside)
    with pytest.raises(OrchestrationError, match="symlink"):
        IssueOrchestrator(
            link,
            worktree,
            "aes-brief-symlink",
            artifact_root=artifact_root,
            router=router,
        )


def test_plan_scope_violation_is_rejected_before_worker_invocation(tmp_path: Path) -> None:
    plan = json.loads(_plan())
    plan["subtasks"][0]["paths"] = ["/etc/passwd"]
    runner = FakeRunner([ProcessResult(0, json.dumps(plan), "")])
    outcome = _orchestrator(tmp_path, runner).run()
    assert outcome.status == "needs_human"
    assert len(runner.calls) == 1


def test_resume_reconciles_durable_plan_without_controller_reinvocation(tmp_path: Path) -> None:
    first = _orchestrator(tmp_path, FakeRunner([]))
    ledger = first.ledger
    ledger.initialize()
    ledger.write_plan(_plan())
    state = json.loads(ledger.state_path.read_text(encoding="utf-8"))
    state["state"] = "pending"  # crash window: plan durable, state transition lost
    ledger.state_path.write_text(json.dumps(state), encoding="utf-8")

    resumed_runner = FakeRunner(
        [
            ProcessResult(0, "first completed", ""),
            ProcessResult(0, "second completed", ""),
            ProcessResult(0, _verdict(), ""),
        ]
    )
    outcome = _orchestrator(tmp_path, resumed_runner).run()
    assert outcome.status == "verified"
    assert len(resumed_runner.calls) == 3
    assert resumed_runner.calls[0][1].startswith("You are one isolated AES worker")


def test_resume_reconciles_durable_verdict_without_controller_reinvocation(tmp_path: Path) -> None:
    seed = _orchestrator(tmp_path, FakeRunner([]))
    ledger = seed.ledger
    ledger.initialize()
    ledger.write_plan(_plan())
    ledger.transition("running")
    ledger.write_result("first", {"status": "ok"})
    ledger.write_result("second", {"status": "ok"})
    ledger.write_verdict(_verdict())
    resumed_runner = FakeRunner([])

    outcome = _orchestrator(tmp_path, resumed_runner).run()
    assert outcome.status == "verified"
    assert resumed_runner.calls == []
    assert json.loads(ledger.state_path.read_text(encoding="utf-8"))["state"] == "verified"
