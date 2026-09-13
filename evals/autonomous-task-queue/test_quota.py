from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import stat
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / "harness" / "scripts" / "tasks"
sys.path.insert(0, str(TASKS))

from quota import (  # noqa: E402
    AVAILABLE,
    AUTH_ERROR,
    CLAUDE,
    CODEX,
    POLICY_BLOCKED,
    QUOTA_EXHAUSTED,
    UNKNOWN,
    QuotaCache,
    QuotaCacheError,
    QuotaConfigError,
    QuotaPayloadError,
    evaluate_provider,
    provider_availability,
    normalize_claude_statusline,
    normalize_codex_snapshot,
    select_provider,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)


def _codex(used: float, *, observed: datetime = NOW):
    return normalize_codex_snapshot(
        {"usedPercent": used, "reset": "2026-09-09T00:00:00Z"},
        observed_at=observed,
    )


def _claude(used: float, *, observed: datetime = NOW):
    return normalize_claude_statusline(
        {
            "rate_limits": {
                "five_hour": {
                    "used_percentage": used,
                    "resets_at": "2026-09-09T00:00:00Z",
                }
            }
        },
        observed_at=observed,
    )


@pytest.mark.parametrize(
    ("used", "expected"),
    ((69.9, AVAILABLE), (70.0, POLICY_BLOCKED), (100.0, POLICY_BLOCKED)),
)
def test_claude_work_hours_reserve_boundary(used: float, expected: str) -> None:
    state = evaluate_provider(CLAUDE, _claude(used), now=NOW, ttl=300)
    assert state.status == expected


@pytest.mark.parametrize(
    ("now", "expected"),
    (
        (datetime(2026, 9, 8, 5, 59, tzinfo=UTC), AVAILABLE),  # 07:59 Berlin
        (datetime(2026, 9, 8, 6, 0, tzinfo=UTC), POLICY_BLOCKED),  # 08:00 Berlin
        (datetime(2026, 9, 8, 14, 59, tzinfo=UTC), POLICY_BLOCKED),  # 16:59 Berlin
        (datetime(2026, 9, 8, 15, 0, tzinfo=UTC), AVAILABLE),  # 17:00 Berlin
        (datetime(2026, 9, 12, 10, 0, tzinfo=UTC), AVAILABLE),  # Saturday
    ),
)
def test_claude_work_window_boundaries_and_weekend(
    now: datetime, expected: str
) -> None:
    state = evaluate_provider(CLAUDE, _claude(70.0, observed=now), now=now, ttl=300)
    assert state.status == expected


def test_claude_missing_or_stale_is_blocked_during_work_hours_but_unknown_outside() -> None:
    work_hours = evaluate_provider(CLAUDE, None, now=NOW, ttl=300)
    outside_hours = evaluate_provider(
        CLAUDE,
        _claude(10.0, observed=NOW.replace(hour=7)),
        now=NOW.replace(hour=15),
        ttl=300,
    )
    assert work_hours.status == POLICY_BLOCKED
    assert outside_hours.status == UNKNOWN


def test_stale_snapshot_is_fail_closed_and_ttl_is_inclusive() -> None:
    snapshot = _codex(20.0, observed=NOW.replace(hour=7, minute=55))
    assert evaluate_provider(CODEX, snapshot, now=NOW, ttl=300).status == AVAILABLE
    assert evaluate_provider(CODEX, snapshot, now=NOW, ttl=299).status == UNKNOWN


def test_dst_uses_europe_berlin_local_time() -> None:
    # 2026-10-25 is the DST transition Sunday: Sunday remains outside the
    # weekday reserve regardless of the UTC offset before/after the switch.
    sunday = datetime(2026, 10, 25, 8, 0, tzinfo=UTC)
    assert (
        evaluate_provider(CLAUDE, _claude(70.0, observed=sunday), now=sunday, ttl=300).status
        == AVAILABLE
    )
    # On Monday after the transition, 07:00 UTC is 08:00 local (CET).
    monday = datetime(2026, 10, 26, 7, 0, tzinfo=UTC)
    assert (
        evaluate_provider(CLAUDE, _claude(70.0, observed=monday), now=monday, ttl=300).status
        == POLICY_BLOCKED
    )


def test_preference_and_fallback_only_on_confirmed_codex_exhaustion() -> None:
    decision = select_provider({CODEX: _codex(20), CLAUDE: _claude(20)}, now=NOW, ttl=300)
    assert decision.selected_provider == CODEX
    exhausted = select_provider({CODEX: _codex(100), CLAUDE: _claude(20)}, now=NOW, ttl=300)
    assert exhausted.selected_provider == CLAUDE
    unknown = select_provider({CLAUDE: _claude(20)}, now=NOW, ttl=300)
    assert unknown.selected_provider is None
    assert unknown.status == UNKNOWN


def test_openai_unknown_or_auth_error_does_not_hide_the_failure_with_fallback() -> None:
    missing = select_provider({CLAUDE: _claude(20)}, now=NOW, ttl=300)
    assert missing.providers[CODEX].status == UNKNOWN
    auth = normalize_codex_snapshot({"error": {"type": "authentication_error"}}, observed_at=NOW)
    decision = select_provider({CODEX: auth, CLAUDE: _claude(20)}, now=NOW, ttl=300)
    assert decision.selected_provider is None
    assert decision.status == AUTH_ERROR


def test_normalizers_accept_app_server_wrappers_and_statusline_fields() -> None:
    codex = normalize_codex_snapshot(
        {
            "account": {
                "rateLimits": {
                    "primary": {"usedPercent": 12, "resetsAt": 1_790_000_000}
                }
            }
        },
        observed_at=NOW,
    )
    claude = normalize_claude_statusline(
        {"rate_limits": {"five_hour": {"used_percentage": "12.5", "resets_at": None}}},
        observed_at=NOW,
    )
    assert codex.provider == CODEX
    assert codex.used_percent == 12
    assert codex.reset_at is not None
    assert claude.provider == CLAUDE
    assert claude.used_percent == 12.5
    assert claude.reset_at is None


def test_malformed_payload_is_unknown_and_never_cached() -> None:
    malformed = normalize_codex_snapshot({"usedPercent": "not-a-number"}, observed_at=NOW)
    assert malformed.valid is False
    assert evaluate_provider(CODEX, malformed, now=NOW, ttl=300).status == UNKNOWN
    with pytest.raises(QuotaPayloadError):
        QuotaCache().write(malformed)


def test_malformed_explicit_payload_timestamp_never_becomes_fresh_or_valid() -> None:
    snapshot = normalize_codex_snapshot(
        {"usedPercent": 10, "timestamp": "not-an-rfc3339-timestamp"}
    )
    assert snapshot.valid is False
    assert snapshot.used_percent is None
    assert evaluate_provider(CODEX, snapshot, now=NOW, ttl=300).status == UNKNOWN


def test_cache_root_rejects_path_traversal_and_symlink_components(tmp_path: Path) -> None:
    approved = tmp_path / "approved"
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(QuotaConfigError):
        QuotaCache(approved / ".." / "outside" / "quota.json", root=approved)

    approved.mkdir()
    link = approved / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(QuotaCacheError):
        QuotaCache(link / "quota.json", root=approved).write(_codex(10))


def test_cache_path_and_root_environment_pair_is_shared_and_validated(
    monkeypatch, tmp_path: Path
) -> None:
    root = tmp_path / "runner-state"
    monkeypatch.setenv("AES_QUOTA_CACHE_ROOT", str(root))
    monkeypatch.setenv("AES_QUOTA_CACHE_PATH", "shared/quota.json")
    cache = QuotaCache()
    assert cache.root == root
    assert cache.path == root / "shared" / "quota.json"
    cache.write(_codex(10))
    assert cache.inspect(now=NOW).selected_provider == CODEX

    monkeypatch.setenv("AES_QUOTA_CACHE_PATH", "../checkout/quota.json")
    with pytest.raises(QuotaConfigError):
        QuotaCache()


def test_cache_exposes_canonical_real_paths_for_boundary_checks(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    redirected = tmp_path / "runner-state"
    redirected.symlink_to(checkout, target_is_directory=True)

    cache = QuotaCache("quota.json", root=redirected / "state")

    assert cache.real_root == (checkout / "state").resolve()
    assert cache.real_path == (checkout / "state" / "quota.json").resolve()


def test_cache_serializes_concurrent_provider_ingests_without_lost_updates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "quota.json"

    def write(provider: str) -> None:
        snapshot = _codex(10) if provider == CODEX else _claude(20)
        QuotaCache(path).write(snapshot)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, (CODEX, CLAUDE)))

    snapshots = QuotaCache(path).read()
    assert set(snapshots) == {CODEX, CLAUDE}
    assert stat.S_IMODE((path.parent / ".quota.json.lock").stat().st_mode) == 0o600


def test_provider_availability_is_canonical_and_applies_ttl_and_policy() -> None:
    assert provider_availability("openai", _codex(10), now=NOW, ttl=300).provider == CODEX
    assert provider_availability("openai", _codex(10), now=NOW, ttl=300).status == AVAILABLE
    assert (
        provider_availability("anthropic", _claude(70), now=NOW, ttl=300).status
        == POLICY_BLOCKED
    )
    assert (
        provider_availability(
            "codex", _codex(10, observed=NOW.replace(hour=7)), now=NOW, ttl=300
        ).status
        == UNKNOWN
    )
    auth = normalize_codex_snapshot({"error": {"type": "authentication_error"}}, observed_at=NOW)
    assert provider_availability(CODEX, auth, now=NOW, ttl=300).status == AUTH_ERROR


def test_cache_is_atomic_mode_0600_and_does_not_persist_raw_payload(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "quota.json"
    cache = QuotaCache(path)
    cache.write(_codex(33.3))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == "aes.quota-cache.v1"
    assert payload["snapshots"]["codex"]["used_percent"] == 33.3
    assert "api_key" not in path.read_text(encoding="utf-8")
    assert cache.inspect(now=NOW).selected_provider == CODEX


def test_cache_rejects_insecure_permissions_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "quota.json"
    path.write_text(
        json.dumps(
            {
                "schema": "aes.quota-cache.v1",
                "snapshots": {"codex": _codex(10).to_cache()},
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o644)
    decision = QuotaCache(path).inspect(now=NOW)
    assert decision.selected_provider is None
    assert decision.status == UNKNOWN
    assert "unavailable" in decision.reason


def test_cli_ingest_from_stdin_and_inspect_json(tmp_path: Path) -> None:
    cache = tmp_path / "quota.json"
    ingest = subprocess.run(
        [
            sys.executable,
            str(TASKS / "quota.py"),
            "ingest",
            "--provider",
            "codex",
            "--cache",
            str(cache),
            "--observed-at",
            "2026-09-08T08:00:00Z",
        ],
        input=json.dumps({"usedPercent": 10, "reset": "2026-09-09T00:00:00Z"}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert ingest.returncode == 0, ingest.stderr
    inspected = subprocess.run(
        [
            sys.executable,
            str(TASKS / "quota.py"),
            "inspect",
            "--cache",
            str(cache),
            "--at",
            "2026-09-08T08:01:00Z",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert inspected.returncode == 0, inspected.stderr
    output = json.loads(inspected.stdout)
    assert output["selected_provider"] == CODEX
    assert output["providers"]["codex"]["status"] == AVAILABLE


def test_cli_rejects_malformed_ingest_without_overwriting_existing_cache(tmp_path: Path) -> None:
    cache_path = tmp_path / "quota.json"
    cache = QuotaCache(cache_path)
    cache.write(_codex(10))
    original = cache_path.read_bytes()
    result = subprocess.run(
        [
            sys.executable,
            str(TASKS / "quota.py"),
            "ingest",
            "--provider",
            "codex",
            "--cache",
            str(cache_path),
        ],
        input="{malformed",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert cache_path.read_bytes() == original


def test_cli_rejects_malformed_provider_timestamp_without_overwriting_cache(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "quota.json"
    cache = QuotaCache(cache_path)
    cache.write(_codex(10))
    original = cache_path.read_bytes()
    result = subprocess.run(
        [
            sys.executable,
            str(TASKS / "quota.py"),
            "ingest",
            "--provider",
            "codex",
            "--cache",
            str(cache_path),
        ],
        input=json.dumps({"usedPercent": 90, "timestamp": "not-a-timestamp"}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert cache_path.read_bytes() == original
