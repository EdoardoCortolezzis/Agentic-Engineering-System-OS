"""Deterministic evals for the provider-neutral autonomous-task router."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import signal
import subprocess
import sys
from typing import Mapping, Sequence

import pytest


REPO = __import__("pathlib").Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "harness" / "scripts" / "tasks"))

import claude_sandbox  # noqa: E402
import model_router  # noqa: E402
from quota import (  # noqa: E402
    QuotaCache,
    normalize_claude_statusline,
    normalize_codex_snapshot,
)
from model_router import (  # noqa: E402
    Availability,
    ModelRequest,
    ModelRouter,
    ProcessResult,
    RoutingPolicy,
    classify_failure,
    child_environment,
)
from claude_sandbox import (  # noqa: E402
    SandboxError,
    build_argv as build_claude_argv,
    build_settings,
    write_settings,
)


@pytest.fixture(autouse=True)
def verified_managed_sandbox_for_native_fallback_tests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Opt in only these tests; production defaults to fail-closed."""

    monkeypatch.setenv("AES_CLAUDE_MANAGED_SANDBOX", "1")
    # Keep the credential root outside every per-test worktree.  This mirrors
    # the production requirement that CODEX_HOME is dedicated and not a
    # subdirectory of the checkout.
    codex_home = tmp_path.parent / f"{tmp_path.name}-codex-home"
    codex_home.mkdir(exist_ok=True)
    monkeypatch.setenv("AES_CODEX_MANAGED_CREDENTIALS", "1")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))


@dataclass
class FakeRunner:
    responses: list[ProcessResult]

    def __post_init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str, Mapping[str, str]]] = []
        self.settings: list[dict[str, object]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: str,
        env: Mapping[str, str],
        cwd: str | None,
        timeout: float | None,
    ) -> ProcessResult:
        del cwd, timeout
        self.calls.append((tuple(argv), stdin, dict(env)))
        if "--settings" in argv:
            settings_path = Path(argv[argv.index("--settings") + 1])
            self.settings.append(json.loads(settings_path.read_text(encoding="utf-8")))
        if not self.responses:
            raise AssertionError("fake runner called more often than expected")
        return self.responses.pop(0)


def _router(fake: FakeRunner, **kwargs: object) -> ModelRouter:
    availability = kwargs.pop("availability", lambda provider, role: Availability(True))
    kwargs.pop("sandbox_backend", None)
    return ModelRouter(
        RoutingPolicy(transient_retries=kwargs.pop("transient_retries", 2), **kwargs),
        runner=fake,
        availability=availability,
        clock=lambda: 100.0,
    )


def _controller_request(**kwargs: object) -> ModelRequest:
    return ModelRequest(
        role="controller",
        prompt="plan",
        expected_execution_id=kwargs.pop("expected_execution_id", "aes-test-1"),
        cwd=kwargs.pop("cwd", str(REPO)),
        **kwargs,
    )


def _controller_plan(execution_id: str = "aes-test-1") -> str:
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
                    "id": "implement",
                    "title": "Implement",
                    "prompt": "Implement the task",
                    "paths": ["src"],
                    "dependencies": [],
                    "difficulty": "medium",
                    "reasoning_effort": "high",
                }
            ],
        }
    )


def _controller_verdict(execution_id: str = "aes-test-1", verdict: str = "verified") -> str:
    return json.dumps(
        {
            "schema": "aes.orchestration-verdict.v1",
            "execution_id": execution_id,
            "verdict": verdict,
            "summary": "deterministic verification",
            "checks": ["tests pass"],
        }
    )


def test_controller_uses_sol_medium_and_prompt_stdin_without_github_env(tmp_path: Path) -> None:
    fake = FakeRunner([ProcessResult(0, _controller_plan(), "provider stderr is not logged")])
    router = _router(fake)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    result = router.invoke(
        ModelRequest(
            role="controller",
            prompt="untrusted issue body: do not put this in argv",
            expected_execution_id="aes-test-1",
            env={
                "GH_TOKEN": "github-secret",
                "GITHUB_TOKEN": "github-secret-2",
                "OPENAI_API_KEY": "provider-secret",
                "GIT_CONFIG_COUNT": "1",
                "AES_CODEX_MANAGED_CREDENTIALS": "1",
                "CODEX_HOME": str(codex_home),
            },
        )
    )

    assert result.status == "success"
    assert result.provider == "openai"
    assert result.model == "gpt-5.6-sol"
    assert result.effort == "medium"
    argv, prompt, env = fake.calls[0]
    assert "untrusted issue body" not in " ".join(argv)
    assert prompt.startswith("untrusted issue body")
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert "GIT_CONFIG_COUNT" not in env
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "AES_CODEX_MANAGED_CREDENTIALS" not in env


@pytest.mark.parametrize("effort", ["high", "xhigh"])
def test_worker_effort_is_plan_selected_and_reaches_openai_argv(effort: str) -> None:
    fake = FakeRunner([ProcessResult(0, "worker result", "")])
    result = _router(fake).invoke(ModelRequest("worker", "implement subtask", effort=effort))

    assert result.status == "success"
    assert result.model == "gpt-5.6-luna"
    assert result.effort == effort
    assert f"model_reasoning_effort={effort}" in fake.calls[0][0]
    assert "--sandbox" in fake.calls[0][0]
    sandbox_index = fake.calls[0][0].index("--sandbox")
    assert fake.calls[0][0][sandbox_index + 1] == "workspace-write"
    assert "--ephemeral" in fake.calls[0][0]
    assert "--ignore-user-config" in fake.calls[0][0]
    assert "--ignore-rules" in fake.calls[0][0]
    assert "--strict-config" in fake.calls[0][0]
    assert 'cli_auth_credentials_store="keyring"' in fake.calls[0][0]
    assert 'shell_environment_policy.filters.HOME="exclude"' in fake.calls[0][0]
    assert 'shell_environment_policy.filters.CODEX_HOME="exclude"' in fake.calls[0][0]


def test_worker_invalid_effort_is_needs_human_without_fallback_or_execution() -> None:
    fake = FakeRunner([])
    result = _router(fake).invoke(ModelRequest("worker", "subtask", effort="medium"))

    assert result.status == "needs_human"
    assert result.classification == "config"
    assert result.attempts == 0
    assert fake.calls == []


def test_quota_failure_falls_back_to_claude_after_openai() -> None:
    fake = FakeRunner(
        [
            ProcessResult(429, "", "rate limit exceeded"),
            ProcessResult(0, "worker fallback result", ""),
        ]
    )
    result = _router(fake).invoke(
        ModelRequest("worker", "subtask", effort="xhigh", cwd=str(REPO))
    )

    assert result.status == "success"
    assert result.provider == "anthropic"
    assert result.model == "claude-sonnet-5"
    assert result.effort == "high", "xhigh maps to Claude's configurable high effort"
    assert result.fallback_used is True
    assert result.attempts == 2
    assert fake.calls[0][0][0] == "codex"
    assert fake.calls[1][0][0] == "claude"


def test_claude_fallback_without_worktree_is_needs_human_without_invocation() -> None:
    fake = FakeRunner([ProcessResult(429, "", "rate limit exceeded")])
    router = ModelRouter(
        RoutingPolicy(transient_retries=0),
        runner=fake,
        availability=lambda provider, role: Availability(True),
    )

    result = router.invoke(
        ModelRequest("worker", "subtask", effort="high")
    )

    assert result.status == "needs_human"
    assert result.classification == "config"
    assert result.provider == "anthropic"
    assert len(fake.calls) == 1
    assert fake.calls[0][0][0] == "codex"


def test_claude_fallback_is_disabled_without_managed_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AES_CLAUDE_MANAGED_SANDBOX", raising=False)
    fake = FakeRunner([ProcessResult(429, "", "rate limit exceeded")])
    router = ModelRouter(
        RoutingPolicy(transient_retries=0),
        runner=fake,
        availability=lambda provider, role: Availability(True),
    )

    result = router.invoke(
        ModelRequest("worker", "subtask", effort="high", cwd=str(tmp_path))
    )

    assert result.status == "needs_human"
    assert result.classification == "config"
    assert result.provider == "anthropic"
    assert result.attempts == 1
    assert len(fake.calls) == 1


def test_claude_fallback_uses_native_settings_and_no_host_wrapper(tmp_path: Path) -> None:
    fake = FakeRunner(
        [
            ProcessResult(429, "", "rate limit exceeded"),
            ProcessResult(0, "worker fallback result", ""),
        ]
    )
    router = ModelRouter(
        RoutingPolicy(transient_retries=0),
        runner=fake,
        availability=lambda provider, role: Availability(True),
    )

    result = router.invoke(
        ModelRequest("worker", "subtask", effort="high", cwd=str(tmp_path))
    )

    assert result.status == "success"
    claude_argv = fake.calls[1][0]
    assert claude_argv[0] == "claude"
    assert "--ro-bind" not in claude_argv
    assert "--unshare-net" not in claude_argv
    assert "--setting-sources" in claude_argv
    assert claude_argv[claude_argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in claude_argv
    assert "--disable-slash-commands" in claude_argv
    assert "--no-chrome" in claude_argv
    assert "--no-session-persistence" in claude_argv
    settings = fake.settings[-1]
    assert settings["sandbox"]["enabled"] is True
    assert settings["sandbox"]["failIfUnavailable"] is True
    assert settings["sandbox"]["allowUnsandboxedCommands"] is False
    deny_read = settings["sandbox"]["filesystem"]["denyRead"]
    assert deny_read[:2] == ["/", "~/"]
    assert str(tmp_path / ".env") in deny_read
    assert settings["sandbox"]["filesystem"]["allowWrite"] == []
    assert settings["sandbox"]["network"]["allowedDomains"] == []


def test_native_sandbox_settings_are_scoped_and_wrapper_free(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subtask = worktree / "src"
    settings = build_settings(
        worktree=worktree,
        writable_paths=(subtask,),
        environ={"CLAUDE_CONFIG_DIR": str(tmp_path / "claude-config")},
    )
    filesystem = settings["sandbox"]["filesystem"]
    assert filesystem["denyRead"][:2] == ["/", "~/"]
    assert str(worktree / ".env") in filesystem["denyRead"]
    assert str(worktree / ".git-credentials") in filesystem["denyRead"]
    assert str(worktree) in filesystem["allowRead"]
    assert "/" not in filesystem["allowRead"]
    assert filesystem["allowWrite"] == [str(subtask)]
    assert settings["sandbox"]["network"]["allowedDomains"] == []

    settings_path = write_settings(
        tmp_path / "settings.json",
        worktree=worktree,
        writable_paths=(subtask,),
    )
    argv = build_claude_argv(("claude", "-p"), settings_path=settings_path)
    assert argv[0:2] == ("claude", "-p")
    assert "--ro-bind" not in argv
    assert "--setting-sources" in argv

    with pytest.raises(SandboxError):
        build_settings(worktree=worktree, writable_paths=(tmp_path / "outside",))


def test_linux_native_sandbox_never_reopens_procfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setattr(claude_sandbox.platform, "system", lambda: "Linux")

    filesystem = build_settings(worktree=worktree)["sandbox"]["filesystem"]

    assert "/proc" not in filesystem["allowRead"]


def test_availability_callback_can_block_openai_quota_before_invocation() -> None:
    fake = FakeRunner([ProcessResult(0, _controller_plan(), "")])
    seen: list[tuple[str, str]] = []

    def availability(provider: str, role: str) -> Availability:
        seen.append((provider, role))
        if provider == "codex":
            return Availability(False, "quota")
        return Availability(True)

    result = ModelRouter(
        runner=fake,
        availability=availability,
    ).invoke(
        _controller_request()
    )

    assert result.status == "success"
    assert result.provider == "anthropic"
    assert seen == [("codex", "controller"), ("claude", "controller")]
    assert len(fake.calls) == 1


def test_failed_codex_refresh_waits_without_using_stale_cache_or_claude() -> None:
    fake = FakeRunner([])

    def availability(provider: str, role: str) -> str:
        del provider, role
        return model_router.REFRESH_FAILED

    result = _router(fake, availability=availability).invoke(_controller_request())

    assert result.status == "waiting_provider"
    assert result.classification == model_router.REFRESH_FAILED
    assert result.provider == "openai"
    assert result.fallback_used is False
    assert result.attempts == 0
    assert fake.calls == []


def test_quota_cache_adapter_routes_using_canonical_codex_and_claude_names(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "quota.json"
    cache = QuotaCache(cache_path)
    observed = datetime.now(timezone.utc)
    cache.write(
        normalize_codex_snapshot(
            {"usedPercent": 100, "reset": "2099-01-01T00:00:00Z"},
            observed_at=observed,
        )
    )
    cache.write(
        normalize_claude_statusline(
            {
                "rate_limits": {
                    "five_hour": {
                        "used_percentage": 20,
                        "resets_at": "2099-01-01T00:00:00Z",
                    }
                }
            },
            observed_at=observed,
        )
    )

    availability = model_router._quota_cache_availability(cache_path)
    assert availability("codex", "worker").status == "quota"
    assert availability("claude", "worker").available is True

    fake = FakeRunner([ProcessResult(0, "worker fallback result", "")])
    result = ModelRouter(
        runner=fake,
        availability=availability,
        clock=lambda: 100.0,
    ).invoke(
        ModelRequest("worker", "subtask", effort="high", cwd=str(cache_path.parent))
    )

    assert result.status == "success"
    assert result.provider == "anthropic"
    assert result.fallback_used is True
    assert fake.calls[0][0][0] == "claude"


def test_auth_failure_is_fail_closed_and_never_falls_back() -> None:
    fake = FakeRunner([ProcessResult(1, "", "invalid api key")])
    result = _router(fake).invoke(_controller_request())

    assert result.status == "needs_human"
    assert result.provider == "openai"
    assert result.classification == "auth"
    assert len(fake.calls) == 1


def test_invalid_controller_output_is_needs_human_without_fallback() -> None:
    fake = FakeRunner([ProcessResult(0, "not-json", "")])
    result = _router(fake).invoke(_controller_request())

    assert result.status == "needs_human"
    assert result.classification == "invalid_output"
    assert len(fake.calls) == 1


def test_transient_retry_is_bounded_then_falls_back() -> None:
    fake = FakeRunner(
        [
            ProcessResult(503, "", "temporarily unavailable"),
            ProcessResult(503, "", "temporarily unavailable"),
            ProcessResult(503, "", "temporarily unavailable"),
            ProcessResult(0, "worker fallback", ""),
        ]
    )
    result = _router(fake, transient_retries=2).invoke(
        ModelRequest("worker", "subtask", effort="high", cwd=str(REPO))
    )

    assert result.status == "success"
    assert result.provider == "anthropic"
    assert result.attempts == 4
    assert len(fake.calls) == 4


def test_unknown_failure_does_not_trigger_blind_fallback() -> None:
    fake = FakeRunner([ProcessResult(1, "", "segmentation fault")])
    result = _router(fake).invoke(ModelRequest("worker", "subtask", effort="high"))

    assert result.status == "needs_human"
    assert result.classification == "unknown"
    assert len(fake.calls) == 1


def test_both_quota_sources_yield_waiting_provider_not_human_alert() -> None:
    fake = FakeRunner([])

    def unavailable(provider: str, role: str) -> Availability:
        del provider, role
        return Availability(False, "quota")

    result = ModelRouter(runner=fake, availability=unavailable).invoke(
        _controller_request()
    )

    assert result.status == "waiting_provider"
    assert result.classification == "quota"
    assert result.attempts == 0


def test_codex_quota_and_claude_policy_block_yield_waiting_provider() -> None:
    fake = FakeRunner([])

    def unavailable(provider: str, role: str) -> Availability:
        del role
        return Availability(
            False,
            "quota_exhausted" if provider == "codex" else "policy_blocked",
        )

    result = ModelRouter(runner=fake, availability=unavailable).invoke(
        _controller_request()
    )

    assert result.status == "waiting_provider"
    assert result.classification == "policy"
    assert result.attempts == 0


def test_classification_is_conservative_for_auth_and_explicit_transient() -> None:
    assert classify_failure(401, "rate limit") == "auth"
    assert classify_failure(503, "temporary failure") == "transient"
    assert classify_failure(1, "policy_blocked") == "policy"
    assert classify_failure(1, "invalid option") == "config"


def test_redacted_result_has_no_model_output_or_provider_stderr() -> None:
    fake = FakeRunner([ProcessResult(0, _controller_plan(), "secret stderr")])
    result = _router(fake).invoke(_controller_request())
    metadata = result.as_dict()

    assert metadata["status"] == "success"
    assert "ok" not in str(metadata)
    assert "secret stderr" not in str(metadata)


def test_child_environment_is_minimal_and_provider_specific() -> None:
    environment = child_environment(
        {
            "CUSTOM_GITHUB_TOKEN": "secret",
            "GH_ENTERPRISE_TOKEN": "secret",
            "OPENAI_API_KEY": "openai-provider",
            "CODEX_API_KEY": "codex-provider",
            "ANTHROPIC_API_KEY": "anthropic-provider",
            "ANTHROPIC_AUTH_TOKEN": "anthropic-auth-token",
            "CLAUDE_CODE_OAUTH_TOKEN": "claude-oauth-token",
            "CODEX_HOME": "/tmp/codex-home",
            "SSH_AUTH_SOCK": "/tmp/agent",
            "GIT_SSH_COMMAND": "ssh -i secret",
            "PATH": "/bin",
        },
        provider="openai",
    )
    assert "CUSTOM_GITHUB_TOKEN" not in environment
    assert "GH_ENTERPRISE_TOKEN" not in environment
    assert "SSH_AUTH_SOCK" not in environment
    assert "GIT_SSH_COMMAND" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert "CODEX_API_KEY" not in environment
    assert "ANTHROPIC_API_KEY" not in environment
    assert "ANTHROPIC_AUTH_TOKEN" not in environment
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in environment
    assert environment["CODEX_HOME"] == "/tmp/codex-home"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"

    claude_environment = child_environment(
        {
            "OPENAI_API_KEY": "openai",
            "CODEX_API_KEY": "codex",
            "ANTHROPIC_API_KEY": "anthropic",
            "ANTHROPIC_AUTH_TOKEN": "anthropic-auth",
            "CLAUDE_CODE_OAUTH_TOKEN": "claude-oauth",
            "CLAUDE_CONFIG_DIR": "/tmp/claude-config",
            "PATH": "/bin",
        },
        provider="anthropic",
    )
    assert "ANTHROPIC_API_KEY" not in claude_environment
    assert "ANTHROPIC_AUTH_TOKEN" not in claude_environment
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in claude_environment
    assert "OPENAI_API_KEY" not in claude_environment
    assert "CODEX_API_KEY" not in claude_environment
    assert claude_environment["CLAUDE_CONFIG_DIR"] == "/tmp/claude-config"


def test_codex_credentials_require_managed_attestation_before_any_provider_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AES_CODEX_MANAGED_CREDENTIALS", raising=False)
    codex_home = tmp_path.parent / f"{tmp_path.name}-codex-home"
    codex_home.mkdir(exist_ok=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    fake = FakeRunner([])

    result = _router(fake).invoke(
        ModelRequest("worker", "subtask", effort="high", cwd=str(tmp_path))
    )

    assert result.status == "needs_human"
    assert result.classification == "config"
    assert result.provider == "openai"
    assert result.attempts == 0
    assert result.fallback_used is False
    assert fake.calls == []


@pytest.mark.parametrize("auth_kind", ["file", "symlink", "broken_symlink"])
def test_codex_credentials_reject_auth_json_including_symlinks(
    auth_kind: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    auth_path = codex_home / "auth.json"
    if auth_kind == "file":
        auth_path.write_text("not a real token", encoding="utf-8")
    elif auth_kind == "symlink":
        target = tmp_path / "secret.json"
        target.write_text("not a real token", encoding="utf-8")
        auth_path.symlink_to(target)
    else:
        auth_path.symlink_to(tmp_path / "missing-secret.json")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    fake = FakeRunner([])
    result = _router(fake).invoke(
        ModelRequest("worker", "subtask", effort="high", cwd=str(tmp_path.parent))
    )

    assert result.status == "needs_human"
    assert result.classification == "config"
    assert result.attempts == 0
    assert fake.calls == []


def test_codex_credentials_reject_home_overlapping_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    codex_home = tmp_path / "worktree" / ".codex"
    codex_home.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    fake = FakeRunner([])

    result = _router(fake).invoke(
        ModelRequest("worker", "subtask", effort="high", cwd=str(tmp_path / "worktree"))
    )

    assert result.status == "needs_human"
    assert result.classification == "config"
    assert result.attempts == 0
    assert fake.calls == []


def test_codex_credentials_reject_symlinked_ancestor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_root = tmp_path / "real-codex-root"
    codex_home = real_root / "home"
    codex_home.mkdir(parents=True)
    linked_root = tmp_path / "linked-codex-root"
    linked_root.symlink_to(real_root, target_is_directory=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(linked_root / "home"))
    fake = FakeRunner([])

    result = _router(fake).invoke(
        ModelRequest("worker", "subtask", effort="high", cwd=str(worktree))
    )

    assert result.status == "needs_human"
    assert result.classification == "config"
    assert result.attempts == 0
    assert fake.calls == []


def test_missing_availability_is_fail_closed_without_provider_execution() -> None:
    fake = FakeRunner([])
    result = ModelRouter(runner=fake).invoke(ModelRequest("worker", "subtask", effort="high"))
    assert result.status == "needs_human"
    assert result.classification == "config"
    assert result.attempts == 0
    assert fake.calls == []


def test_controller_plan_must_match_expected_execution_id() -> None:
    fake = FakeRunner([ProcessResult(0, _controller_plan("aes-other"), "")])
    result = _router(fake).invoke(_controller_request(expected_execution_id="aes-test-1"))
    assert result.status == "needs_human"
    assert result.classification == "invalid_output"


def test_controller_verdict_mode_validates_verdict_without_weakening_plan_mode() -> None:
    fake = FakeRunner([ProcessResult(0, _controller_verdict(), "")])
    result = _router(fake).invoke(
        ModelRequest(
            role="controller",
            prompt="verify",
            expected_execution_id="aes-test-1",
            controller_mode="verdict",
        )
    )
    assert result.status == "success"

    wrong_shape = FakeRunner([ProcessResult(0, _controller_plan(), "")])
    rejected = _router(wrong_shape).invoke(
        ModelRequest(
            role="controller",
            prompt="verify",
            expected_execution_id="aes-test-1",
            mode="verdict",
        )
    )
    assert rejected.status == "needs_human"
    assert rejected.classification == "invalid_output"


def test_controller_accepts_exact_json_object_in_json_code_fence() -> None:
    fake = FakeRunner([ProcessResult(0, f"```json\n{_controller_plan()}\n```", "")])
    result = _router(fake).invoke(_controller_request())
    assert result.status == "success"


def test_controller_rejects_prose_around_json_object() -> None:
    fake = FakeRunner(
        [ProcessResult(0, f"Here is the plan:\n{_controller_plan()}", "")]
    )
    result = _router(fake).invoke(_controller_request())
    assert result.status == "needs_human"
    assert result.classification == "invalid_output"


def test_timeout_default_is_finite_and_invalid_values_are_rejected() -> None:
    assert RoutingPolicy().timeout_seconds > 0
    with pytest.raises(ValueError):
        RoutingPolicy(timeout_seconds=float("inf"))
    with pytest.raises(ValueError):
        RoutingPolicy(timeout_seconds=float("nan"))


def test_default_runner_kills_process_group_and_reaps_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    class TimedOutProcess:
        pid = 4242
        returncode = -signal.SIGKILL

        def __init__(self) -> None:
            self.alive = True
            self.communicate_calls = 0
            self.wait_calls = 0

        def communicate(self, **kwargs: object) -> tuple[str, str]:
            self.communicate_calls += 1
            events.append(("communicate", kwargs.get("timeout")))
            if self.communicate_calls == 1:
                raise subprocess.TimeoutExpired(["provider"], kwargs.get("timeout"))
            return "", ""

        def terminate(self) -> None:
            events.append(("terminate", None))

        def kill(self) -> None:
            events.append(("kill", None))
            self.alive = False

        def wait(self, *, timeout: float) -> None:
            self.wait_calls += 1
            events.append(("wait", timeout))
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired(["provider"], timeout)
            self.alive = False

        def poll(self) -> int | None:
            return None if self.alive else self.returncode

    process = TimedOutProcess()
    monkeypatch.setattr(model_router.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(model_router.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(model_router.os, "getpgrp", lambda: 1)
    monkeypatch.setattr(
        model_router.os,
        "killpg",
        lambda group_id, sig: events.append(("killpg", (group_id, sig))),
    )

    result = model_router._default_runner(
        ("provider",), stdin="prompt", env={}, cwd=None, timeout=0.1
    )

    assert result == ProcessResult(408, "", "provider process timed out")
    assert events[0] == ("communicate", 0.1)
    assert ("killpg", (4242, signal.SIGTERM)) in events
    assert ("killpg", (4242, signal.SIGKILL)) in events
    assert events.index(("killpg", (4242, signal.SIGTERM))) < events.index(
        ("killpg", (4242, signal.SIGKILL))
    )
    assert process.wait_calls == 2
    assert process.poll() is not None


def test_timeout_cleanup_finishes_before_router_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"active": 0, "constructed": 0, "cleanup_complete": False}
    kill_events: list[tuple[int, int]] = []

    class SequencedProcess:
        def __init__(self, index: int) -> None:
            assert state["active"] == 0, "a retry started beside a live provider process"
            state["active"] = 1
            self.index = index
            self.pid = 5000 + index
            self.alive = True
            self.communicate_calls = 0
            self.wait_calls = 0
            self.returncode = 0

        def communicate(self, **kwargs: object) -> tuple[str, str]:
            self.communicate_calls += 1
            if self.index == 1 and self.communicate_calls == 1:
                raise subprocess.TimeoutExpired(["provider"], kwargs.get("timeout"))
            return ("", "") if self.index == 1 else ("worker result", "")

        def wait(self, *, timeout: float) -> None:
            self.wait_calls += 1
            if self.index == 1 and self.wait_calls == 1:
                raise subprocess.TimeoutExpired(["provider"], timeout)
            self.alive = False
            state["active"] = 0
            state["cleanup_complete"] = True

        def kill(self) -> None:
            self.alive = False
            state["active"] = 0

        def terminate(self) -> None:
            pass

        def poll(self) -> int | None:
            return None if self.alive else self.returncode

    def popen(*args: object, **kwargs: object) -> SequencedProcess:
        del args
        assert kwargs["start_new_session"] is True
        state["constructed"] += 1
        return SequencedProcess(state["constructed"])

    def killpg(group_id: int, sig: int) -> None:
        kill_events.append((group_id, sig))
        if sig == signal.SIGKILL:
            state["active"] = 0
            state["cleanup_complete"] = True

    monkeypatch.setattr(model_router.subprocess, "Popen", popen)
    monkeypatch.setattr(model_router.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(model_router.os, "getpgrp", lambda: 1)
    monkeypatch.setattr(model_router.os, "killpg", killpg)

    router = _router(FakeRunner([]), transient_retries=1)
    # The injected FakeRunner is not used: this exercises the production
    # runner seam while keeping process lifecycle fully deterministic.
    router.runner = model_router._default_runner
    result = router.invoke(ModelRequest("worker", "subtask", effort="high", cwd=str(REPO)))

    assert result.status == "success"
    assert result.attempts == 2
    assert state["constructed"] == 2
    assert state["cleanup_complete"] is True
    assert (5001, signal.SIGTERM) in kill_events
    assert (5001, signal.SIGKILL) in kill_events


def test_cli_without_quota_cache_stops_before_provider_invocation(tmp_path: Path, monkeypatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> ProcessResult:
        raise AssertionError("provider must not be invoked without quota policy")

    monkeypatch.setattr(model_router, "_default_runner", forbidden)
    stdout = StringIO()
    stderr = StringIO()
    code = model_router._cli(
        [
            "--role",
            "worker",
            "--effort",
            "high",
            "--prompt-file",
            "-",
            "--root",
            str(tmp_path),
        ],
        stdin=StringIO("bounded prompt"),
        stdout=stdout,
        stderr=stderr,
    )
    assert code == 1
    assert '"status": "needs_human"' in stdout.getvalue()
    assert stderr.getvalue() == ""


def test_cli_rejects_symlink_output_before_invocation(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    output = tmp_path / "output.txt"
    output.symlink_to(target)
    stdout = StringIO()
    stderr = StringIO()
    code = model_router._cli(
        [
            "--role",
            "worker",
            "--effort",
            "high",
            "--prompt-file",
            "-",
            "--output-file",
            str(output),
            "--root",
            str(tmp_path),
        ],
        stdin=StringIO("prompt"),
        stdout=stdout,
        stderr=stderr,
    )
    assert code == 2
    assert "RouterConfigError" in stderr.getvalue()
    assert not target.exists()
