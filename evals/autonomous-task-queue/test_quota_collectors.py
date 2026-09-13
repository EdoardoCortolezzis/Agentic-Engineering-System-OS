from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / "harness" / "scripts" / "tasks"
sys.path.insert(0, str(TASKS))

from quota import (  # noqa: E402
    QuotaCache,
    QuotaPayloadError,
    normalize_codex_snapshot,
)
from quota_collectors import (  # noqa: E402
    CollectorError,
    CollectorProtocolError,
    CollectorTimeoutError,
    _process_spec,
    collect_codex_quota,
    ingest_claude_statusline,
    main,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)


def _fake_server(tmp_path: Path, body: str) -> tuple[tuple[str, ...], Path]:
    script = tmp_path / "fake_app_server.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return (sys.executable, str(script)), script


def test_codex_handshake_ignores_notifications_and_normalizes_sparse_response(
    tmp_path: Path,
) -> None:
    command, _ = _fake_server(
        tmp_path,
        """
        import json, sys
        initialized = False
        for raw in sys.stdin:
            message = json.loads(raw)
            if message.get("method") == "initialize":
                print(json.dumps({"method": "account/rateLimits/updated", "params": {"rateLimits": {}}}), flush=True)
                print(json.dumps({"id": 1, "result": {"userAgent": "fake"}}), flush=True)
            elif message.get("method") == "initialized":
                initialized = True
            elif message.get("method") == "account/rateLimits/read":
                assert initialized
                print(json.dumps({"method": "account/rateLimits/updated", "params": {"rateLimits": {"primary": {"usedPercent": 99}}}}), flush=True)
                print(json.dumps({"id": 999, "result": {"rateLimits": {"primary": {"usedPercent": 88}}}}), flush=True)
                print(json.dumps({"id": 2, "result": {"rateLimits": {"primary": {"usedPercent": 12, "resetsAt": 1790000000}, "secondary": None}}}), flush=True)
                break
        """,
    )
    cache_path = tmp_path / "quota.json"
    snapshot = collect_codex_quota(
        command=command,
        cache=QuotaCache(cache_path),
        timeout=2,
        observed_at=NOW,
    )
    assert snapshot.provider == "codex"
    assert snapshot.used_percent == 12
    assert snapshot.reset_at is not None
    assert QuotaCache(cache_path).read()["codex"].used_percent == 12
    assert stat.S_IMODE(cache_path.stat().st_mode) == 0o600


def test_codex_timeout_cleans_process_and_does_not_write_cache(tmp_path: Path) -> None:
    command, _ = _fake_server(
        tmp_path,
        """
        import time
        time.sleep(60)
        """,
    )
    cache_path = tmp_path / "quota.json"
    with pytest.raises(CollectorTimeoutError):
        collect_codex_quota(
            command=command,
            cache=QuotaCache(cache_path),
            timeout=0.15,
        )
    assert not cache_path.exists()


@pytest.mark.parametrize(
    "body",
    [
        "print('not-json', flush=True)",
        "print('{\\\"id\\\": 1, \\\"result\\\": {}}', flush=True)",
    ],
)
def test_codex_protocol_error_is_fail_closed(tmp_path: Path, body: str) -> None:
    command, _ = _fake_server(tmp_path, body)
    cache_path = tmp_path / "quota.json"
    with pytest.raises(CollectorProtocolError):
        collect_codex_quota(command=command, cache=QuotaCache(cache_path), timeout=1)
    assert not cache_path.exists()


def test_codex_api_key_is_removed_from_child_environment(tmp_path: Path) -> None:
    marker = tmp_path / "api-key-seen"
    command, _ = _fake_server(
        tmp_path,
        f"""
        import json, os, pathlib, sys
        pathlib.Path({str(marker)!r}).write_text(str(bool(os.environ.get('OPENAI_API_KEY'))))
        for raw in sys.stdin:
            message = json.loads(raw)
            if message.get('method') == 'initialize':
                print(json.dumps({{'id': 1, 'result': {{'ok': True}}}}), flush=True)
            elif message.get('method') == 'account/rateLimits/read':
                print(json.dumps({{'id': 2, 'result': {{'rateLimits': {{'primary': {{'usedPercent': 1}}}}}}}}), flush=True)
                break
        """,
    )
    snapshot = collect_codex_quota(
        command=command,
        cache=QuotaCache(tmp_path / "quota.json"),
        timeout=1,
        environment={"OPENAI_API_KEY": "do-not-leak", "PATH": sys.path[0]},
        observed_at=NOW,
    )
    assert snapshot.used_percent == 1
    assert marker.read_text(encoding="utf-8") == "False"
    assert "do-not-leak" not in (tmp_path / "quota.json").read_text(encoding="utf-8")


def test_codex_api_key_command_flag_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(CollectorError):
        collect_codex_quota(
            command=("codex", "app-server", "--api-key", "secret"),
            cache=QuotaCache(tmp_path / "quota.json"),
        )


def test_codex_environment_is_allowlisted_and_preserves_subscription_home() -> None:
    spec = _process_spec(
        ("codex", "app-server", "--stdio"),
        {
            "PATH": "/bin",
            "CODEX_HOME": "/private/codex-home",
            "GH_TOKEN": "github-secret",
            "GITHUB_TOKEN": "github-secret",
            "AWS_ACCESS_KEY_ID": "cloud-secret",
            "AWS_SECRET_ACCESS_KEY": "cloud-secret",
            "CUSTOM_TOKEN": "general-secret",
            "PYTHONPATH": "/tmp/injected",
        },
    )
    assert spec.env == {
        "PATH": "/bin",
        "CODEX_HOME": "/private/codex-home",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }


def test_refresh_cli_rejects_arbitrary_command_override(monkeypatch, tmp_path: Path) -> None:
    called = False

    def forbidden(*args: object, **kwargs: object):
        nonlocal called
        called = True
        raise AssertionError("production CLI must not start a command override")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert (
        main(
            [
                "refresh-codex",
                "--cache",
                str(tmp_path / "quota.json"),
                "--command",
                "/tmp/arbitrary-provider",
            ]
        )
        == 2
    )
    assert called is False


def test_claude_statusline_ingest_is_sparse_and_atomic(tmp_path: Path) -> None:
    path = tmp_path / "quota.json"
    snapshot = ingest_claude_statusline(
        {
            "model": {"id": "claude"},
            "rate_limits": {
                "five_hour": {
                    "used_percentage": "21.5",
                    "resets_at": "2026-09-08T12:00:00Z",
                }
            },
            "private_token": "must-not-be-cached",
        },
        cache=QuotaCache(path),
        observed_at=NOW,
    )
    assert snapshot.provider == "claude"
    assert snapshot.used_percent == 21.5
    contents = path.read_text(encoding="utf-8")
    assert "private_token" not in contents
    assert "must-not-be-cached" not in contents


def test_claude_malformed_or_missing_fails_closed_without_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "quota.json"
    cache = QuotaCache(path)
    cache.write(normalize_codex_snapshot({"usedPercent": 10}, observed_at=NOW))
    original = path.read_bytes()
    with pytest.raises(QuotaPayloadError):
        ingest_claude_statusline({"rate_limits": {"five_hour": {"used_percentage": "?"}}}, cache=cache)
    assert path.read_bytes() == original


def test_ingest_claude_cli_is_silent_for_statusline(monkeypatch, tmp_path: Path, capsys) -> None:
    import io

    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"rate_limits": {"five_hour": {"used_percentage": 4}}})),
    )
    assert main(["ingest-claude", "--cache", str(tmp_path / "quota.json")]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (tmp_path / "quota.json").exists()
