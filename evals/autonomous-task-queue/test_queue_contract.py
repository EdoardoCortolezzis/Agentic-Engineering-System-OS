from pathlib import Path
import re

import yaml

# Nome distinto da suite tasks: pytest importa i moduli per basename.


ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "specs" / "007-autonomous-task-queue" / "spec.md"
POLICY = ROOT / "policies" / "autonomous-task-queue.md"
SKILL = ROOT / ".agent" / "skills" / "aes-tasks" / "SKILL.md"
TEMPLATE = ROOT / ".github" / "ISSUE_TEMPLATE" / "aes-task.md"
WORKFLOW = ROOT / ".github" / "workflows" / "aes-queue.yml"
MANIFEST = ROOT / "harness" / "manifest.txt"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_spec_smoke_defines_autonomous_dispatch_contract_and_human_gates() -> None:
    text = read(SPEC)

    for marker in (
        "repo-local",
        "aes:ready",
        "aes:needs-human",
        "aes:pr-open",
        "concurrency",
        "budget",
        "fail-closed",
        "auto-merge",
        "cross-repository dependencies",
    ):
        assert marker in text


def test_policy_smoke_makes_dispatch_safety_limits_explicit() -> None:
    text = read(POLICY).lower()

    for marker in (
        "aes_queue_enabled",
        "aes_queue_max_concurrency",
        "aes_queue_budget",
        "aes:needs-human",
        "aes:pr-open",
        "does not modify",
        "does not merge",
    ):
        assert marker in text


def test_task_skill_smoke_describes_worker_queue_and_event_protocol() -> None:
    text = read(SKILL)

    for marker in (
        "dispatcher",
        "worker",
        "aes:ready",
        "aes:needs-human",
        "aes:pr-open",
        "budget",
        "concurrency",
        "repository",
    ):
        assert marker in text


def test_issue_template_carries_dispatch_metadata_without_moving_the_lock() -> None:
    text = read(TEMPLATE)

    dispatch = re.search(
        r"<!-- aes:dispatch -->(.*?)<!-- /aes:dispatch -->",
        text,
        flags=re.DOTALL,
    )
    assert dispatch is not None
    keys = {
        line.split(":", 1)[0].strip()
        for line in dispatch.group(1).splitlines()
        if ":" in line and not line.lstrip().startswith("#")
    }
    assert keys == {"dispatch", "priority", "not_before", "budget"}
    assert "role:*" in text
    assert "autonomy:*" in text
    assert "depends_on:" in text

    assert "the lock remains the git ref created by\n     the claim" in text.lower()
    assert "aes:ready" in text


def test_tasks_cli_preserves_canonical_role_label_filter() -> None:
    source = (ROOT / "harness" / "scripts" / "tasks" / "tasks.py").read_text(
        encoding="utf-8"
    )

    assert 'required_labels.append(f"role:{args.role}")' in source
    assert "role:*" in read(TEMPLATE)


def test_invalid_dispatch_metadata_transitions_ready_to_needs_human() -> None:
    spec = read(SPEC)
    policy = read(POLICY).lower()

    assert "`aes:ready` to `aes:needs-human" in spec or "`aes:ready` | `aes:needs-human`" in policy
    assert "`aes:ready` | `aes:needs-human`" in policy


def test_spec_bdd_scenarios_have_given_when_then_structure() -> None:
    scenarios = re.findall(
        r"Scenario: .*?(?=\nScenario:|\n## |\Z)",
        read(SPEC),
        flags=re.DOTALL,
    )

    assert len(scenarios) >= 8
    assert all(
        all(keyword in scenario for keyword in ("Given", "When", "Then"))
        for scenario in scenarios
    )


def test_queue_workflow_is_parseable_and_preserves_worker_rollout_contract() -> None:
    source = read(WORKFLOW)
    payload = yaml.safe_load(source)
    assert isinstance(payload, dict)
    worker = payload["jobs"]["worker"]
    assert worker["runs-on"] == ["self-hosted", "aes", "codex"]
    assert "max-parallel" in worker["strategy"]
    assert "fromJSON(vars.AES_QUEUE_MAX_CONCURRENCY || '1')" in source
    assert "matrix.task.repo" in worker["concurrency"]["group"]
    assert "matrix.task.issue" in worker["concurrency"]["group"]
    assert "persist-credentials: false" in source
    assert "AES_QUEUE_ALLOWED_AUTHORS" in source
    assert "AES_TRUSTED_ACTOR" in source
    assert "gh api user --jq '.login'" in source
    assert "GITHUB_ACTOR" not in source
    assert "AES_WORKER_ADAPTER" not in source
    for field in (
        "matrix.task.resume",
        "matrix.task.lease_ref",
        "matrix.task.lease_expected_sha",
        "matrix.task.lease_owner",
    ):
        assert field in source
    assert "args+=(--resume)" in source
    assert '[[ "${TASK_RESUME}" == "true" ]]' in source
    assert '[[ "${status}" -eq 75 ]]' in source
    assert "always()" in source


def test_manifest_propagates_the_complete_queue_runtime_without_legacy_adapter() -> None:
    destinations = {
        fields[2]
        for line in read(MANIFEST).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
        for fields in [line.split()]
        if len(fields) >= 3
    }
    required = {
        str(path.relative_to(ROOT))
        for path in (ROOT / "harness" / "scripts" / "tasks").glob("*.py")
    }
    required.add("docs/queue-quota.md")
    assert required <= destinations
    assert "scripts/codex-orchestrate/run.sh" not in destinations


def test_queue_docs_define_provider_quota_and_human_notification_contract() -> None:
    docs = "\n".join(
        read(path)
        for path in (ROOT / "README.md", SPEC, POLICY, ROOT / "docs" / "queue-quota.md")
    )
    for marker in (
        "gpt-5.6-sol",
        "claude-opus-5",
        "gpt-5.6-luna",
        "claude-sonnet-5",
        "AES_QUOTA_CACHE_ROOT",
        "AES_QUOTA_CACHE_PATH",
        "AES_TRUSTED_ACTOR",
        "70%",
        "Europe/Berlin",
        "waiting-provider",
        "aes:needs-human",
        "aes:pr-open",
        "auto-merge",
        "notifications/watch",
    ):
        assert marker in docs
