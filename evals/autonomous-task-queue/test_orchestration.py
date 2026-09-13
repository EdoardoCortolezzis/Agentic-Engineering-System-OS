from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / "harness" / "scripts" / "tasks"
sys.path.insert(0, str(TASKS))

from orchestration import (  # noqa: E402
    ArtifactError,
    ArtifactLedger,
    ControllerPlan,
    OrchestrationError,
    Subtask,
    new_execution_id,
    parse_plan,
    validate_plan_paths,
)


def _subtask(
    subtask_id: str = "implement",
    *,
    dependencies: list[str] | None = None,
    effort: str = "high",
    paths: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": subtask_id,
        "title": "Implement bounded change",
        "prompt": "Implement the bounded change and run its tests.",
        "paths": paths or ["harness/scripts/tasks/orchestration.py"],
        "dependencies": dependencies or [],
        "difficulty": "high",
        "reasoning_effort": effort,
    }


def _plan(*subtasks: dict[str, object]) -> dict[str, object]:
    return {
        "schema": "aes.orchestration-plan.v1",
        "execution_id": "aes-test-1",
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
        "subtasks": list(subtasks),
    }


def test_controller_plan_has_required_routes_and_strict_subtask_contract() -> None:
    plan = parse_plan(_plan(_subtask()))

    assert plan.controller.primary.model == "gpt-5.6-sol"
    assert plan.controller.primary.reasoning_effort == "medium"
    assert plan.controller.fallback.model == "claude-opus-5"
    assert plan.worker.primary.model == "gpt-5.6-luna"
    assert plan.worker.fallback.model == "claude-sonnet-5"
    assert plan.subtasks[0].reasoning_effort == "high"
    assert json.loads(plan.to_json()) == plan.to_dict()


def test_controller_estimates_only_high_or_xhigh_worker_effort() -> None:
    for effort in ("", "low", "medium", "extra-high"):
        with pytest.raises(OrchestrationError, match="reasoning_effort"):
            parse_plan(_plan(_subtask(effort=effort)))

    assert parse_plan(_plan(_subtask(effort="xhigh"))).subtasks[0].reasoning_effort == "xhigh"


@pytest.mark.parametrize(
    ("route", "entry", "field", "value"),
    [
        ("controller", "primary", "model", "gpt-5.5"),
        ("controller", "fallback", "provider", "openai"),
        ("controller", "primary", "reasoning_effort", "high"),
        ("worker", "primary", "model", "gpt-5.6-sol"),
        ("worker", "fallback", "provider", "openai"),
        ("worker", "fallback", "reasoning_effort", "medium"),
    ],
)
def test_plan_accepts_only_the_versioned_provider_routes(
    route: str, entry: str, field: str, value: str
) -> None:
    payload = _plan(_subtask())
    route_payload = payload[route]
    assert isinstance(route_payload, dict)
    entry_payload = route_payload[entry]
    assert isinstance(entry_payload, dict)
    entry_payload[field] = value
    with pytest.raises(OrchestrationError, match="must use|reasoning_effort"):
        parse_plan(payload)


def test_parse_plan_binds_to_caller_execution_id() -> None:
    payload = _plan(_subtask())
    assert parse_plan(payload, execution_id="aes-test-1").execution_id == "aes-test-1"
    with pytest.raises(OrchestrationError, match="caller execution_id"):
        parse_plan(payload, execution_id="aes-other")
    with pytest.raises(OrchestrationError, match="execution_id"):
        parse_plan(payload, execution_id="../escape")


def test_plan_rejects_empty_tasks_duplicate_ids_unknown_dependencies_and_cycles() -> None:
    with pytest.raises(OrchestrationError, match="at least one subtask"):
        parse_plan(_plan())
    with pytest.raises(OrchestrationError, match="duplicate"):
        parse_plan(_plan(_subtask(), _subtask()))
    with pytest.raises(OrchestrationError, match="unknown dependencies"):
        parse_plan(_plan(_subtask(dependencies=["missing"])))
    with pytest.raises(OrchestrationError, match="cycle"):
        parse_plan(
            _plan(
                _subtask("first", dependencies=["second"]),
                _subtask("second", dependencies=["first"]),
            )
        )


def test_plan_rejects_unsafe_paths_and_unknown_json_keys() -> None:
    with pytest.raises(OrchestrationError, match="safe relative path"):
        parse_plan(_plan(_subtask(paths=["../outside"])))
    with pytest.raises(OrchestrationError, match="unknown"):
        parse_plan({**_plan(_subtask()), "unexpected": True})
    with pytest.raises(OrchestrationError, match="unknown"):
        parse_plan(
            _plan(
                {
                    **_subtask(),
                    "extra": "not allowed",
                }
            )
        )


def test_plan_paths_must_be_subset_of_trusted_contract_scope() -> None:
    plan = _plan(_subtask(paths=["src/allowed.py"]))
    assert parse_plan(plan, contract_paths=["src"]).subtasks[0].paths == ("src/allowed.py",)
    with pytest.raises(OrchestrationError, match="outside contract.paths"):
        parse_plan(_plan(_subtask(paths=["tests/outside.py"])), contract_paths=["src"])
    with pytest.raises(OrchestrationError, match="safe relative path"):
        validate_plan_paths(parse_plan(plan), ["/etc/passwd"])


def test_execution_id_is_safe_for_artifact_paths(tmp_path: Path) -> None:
    assert new_execution_id().startswith("aes-")
    with pytest.raises(OrchestrationError):
        ArtifactLedger(tmp_path, "../escape")


def test_artifact_ledger_rejects_harness_symlink_escapes(tmp_path: Path) -> None:
    agent = tmp_path / ".agent"
    agent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    (agent / "repo").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ArtifactError, match="symlink"):
        ArtifactLedger(tmp_path, "aes-symlink-repo")

    (agent / "repo").unlink()
    (agent / "orchestration").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ArtifactError, match="symlink"):
        ArtifactLedger(tmp_path, "aes-symlink-orchestration")


def test_artifact_ledger_writes_atomic_state_plan_results_and_logs(tmp_path: Path) -> None:
    ledger = ArtifactLedger(tmp_path, "aes-test-1")
    state = ledger.initialize(metadata={"repo": "owner/repo", "issue": 42})

    assert state["state"] == "pending"
    assert ledger.directory == tmp_path / ".agent" / "orchestration" / "aes-test-1"
    assert (ledger.results_directory).is_dir()
    assert (ledger.logs_directory).is_dir()

    plan = ControllerPlan.from_dict(_plan(_subtask()))
    assert ledger.write_plan(plan) == ledger.plan_path
    assert ledger.read_state()["state"] == "planned"

    ledger.transition("running", event="worker_started")
    result_path = ledger.write_result("implement", {"status": "ok", "changed": ["src/app.py"]})
    log_path = ledger.write_log("worker.log", "worker started\n")
    ledger.transition("verified", event="controller_verified")

    assert json.loads(result_path.read_text(encoding="utf-8"))["subtask_id"] == "implement"
    assert log_path.read_text(encoding="utf-8") == "worker started\n"
    final_state = ledger.read_state()
    assert final_state["state"] == "verified"
    assert [event["to"] for event in final_state["history"]] == ["planned", "running", "verified"]
    assert not list(ledger.directory.glob(".*.json.*"))


def test_artifact_ledger_refuses_overwrite_and_invalid_transitions(tmp_path: Path) -> None:
    ledger = ArtifactLedger(tmp_path, "aes-test-2")
    ledger.initialize()
    with pytest.raises(ArtifactError, match="already initialized"):
        ledger.initialize()
    with pytest.raises(ArtifactError, match="invalid state transition"):
        ledger.transition("verified")
    with pytest.raises(ArtifactError, match="safe file name"):
        ledger.write_log("../secrets", "not safe")


def test_result_must_belong_to_plan_and_cannot_be_overwritten(tmp_path: Path) -> None:
    ledger = ArtifactLedger(tmp_path, "aes-test-1")
    ledger.initialize()
    ledger.write_plan(ControllerPlan.from_dict(_plan(_subtask())))
    ledger.transition("running")
    ledger.write_result("implement", {"status": "ok", "changed": ["src/app.py"]})

    with pytest.raises(ArtifactError, match="already exists"):
        ledger.write_result("implement", {"status": "ok"})
    with pytest.raises(ArtifactError, match="not declared"):
        ledger.write_result("missing", {"status": "ok"})
    with pytest.raises(ArtifactError, match="unsupported fields"):
        ledger.write_result("implement-2", {"status": "ok", "stdout": "raw model output"})


def test_read_results_rejects_filename_spoof_extra_and_mismatched_artifacts(tmp_path: Path) -> None:
    ledger = ArtifactLedger(tmp_path, "aes-result-spoof")
    ledger.initialize()
    plan_payload = _plan(_subtask())
    plan_payload["execution_id"] = "aes-result-spoof"
    ledger.write_plan(ControllerPlan.from_dict(plan_payload))
    ledger.transition("running")
    ledger.write_result("implement", {"status": "ok"})

    payload = json.loads((ledger.results_directory / "implement.json").read_text())
    spoof = dict(payload)
    spoof["subtask_id"] = "missing"
    (ledger.results_directory / "spoof.json").write_text(json.dumps(spoof), encoding="utf-8")
    with pytest.raises(ArtifactError, match="not declared|filename"):
        ledger.read_results()

    (ledger.results_directory / "spoof.json").unlink()
    (ledger.results_directory / "extra.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(ArtifactError, match="unexpected result artifact"):
        ledger.read_results()


def test_logs_are_bounded_redacted_and_create_only(tmp_path: Path) -> None:
    ledger = ArtifactLedger(tmp_path, "aes-log-contract")
    ledger.initialize()
    path = ledger.write_log("worker.log", "token=TEST_ONLY\nfinished\n")
    content = path.read_text(encoding="utf-8")
    assert "TEST_ONLY" not in content
    assert "[REDACTED]" in content
    with pytest.raises(ArtifactError, match="already exists"):
        ledger.write_log("worker.log", "second write")
    with pytest.raises(ArtifactError, match="exceeds"):
        ledger.write_log("oversized.log", "x" * (64 * 1024 + 1))
