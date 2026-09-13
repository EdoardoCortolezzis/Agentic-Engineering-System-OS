import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest


TASKS_DIR = Path(__file__).resolve().parents[2] / "harness" / "scripts" / "tasks"
sys.path.insert(0, str(TASKS_DIR))

import claim as task_claim  # noqa: E402
import dispatcher  # noqa: E402
import provider_github  # noqa: E402
import tasks as tasks_cli  # noqa: E402


FAKE_GH = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys


args = sys.argv[1:]
log_path = Path(os.environ["FAKE_GH_LOG"])
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")

if args[:2] == ["issue", "view"]:
    print(json.dumps({
        "number": int(args[2]),
        "title": "Implement Atomic Claim!",
        "state": "OPEN",
        "labels": [{"name": "aes:ready"}],
        "body": """<!-- aes:contract -->
repo: owner/repo
paths: harness/scripts/tasks/
constraints:
depends_on:
done: test verdi
<!-- /aes:contract -->""",
    }))
elif args[:2] == ["issue", "list"]:
    print(json.dumps([{
        "number": 9,
        "title": "Backlog task",
        "state": "OPEN",
        "labels": [],
        "body": "Issue senza contratto.",
    }]))
elif args and args[0] == "api" and args[1].startswith("repos/owner/repo/issues?"):
    default_pages = [[{
        "number": 9,
        "title": "Backlog task",
        "state": "open",
        "labels": [],
        "body": "Issue senza contratto.",
    }]]
    print(os.environ.get("FAKE_GH_TASK_PAGES", json.dumps(default_pages)))
elif args[:2] == ["api", "repos/owner/repo/git/ref/heads/develop"]:
    print("base-sha")
elif args[:2] == ["api", "repos/owner/repo/git/matching-refs/heads/feature/42-"]:
    if os.environ.get("FAKE_GH_MATCHING_REF") == "different-slug":
        print(json.dumps([{
            "ref": "refs/heads/feature/42-title-changed-after-read",
        }]))
    else:
        print("[]")
elif args[:2] == ["api", "repos/owner/repo/issues/7"]:
    print("closed")
elif args[:2] == ["api", "repos/other/project/issues/8"]:
    print("open")
elif args[:2] == ["api", "repos/owner/repo/issues/999"]:
    print(json.dumps({"message": "Not Found"}, separators=(",", ":")), file=sys.stderr)
    raise SystemExit(1)
elif args[:3] == ["api", "-X", "POST"]:
    mode = os.environ.get("FAKE_GH_REF_MODE", "created")
    if mode == "exists":
        print(json.dumps({
            "message": "Validation Failed",
            "errors": [{"message": "Reference already exists"}],
        }), file=sys.stderr)
        raise SystemExit(1)
    if mode == "error":
        print(json.dumps({"message": "Not Found"}), file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps({"ref": "refs/heads/feature/42-implement-atomic-claim"}))
elif args[:2] in (["issue", "comment"], ["issue", "edit"]):
    pass
else:
    print(f"unexpected fake gh invocation: {args}", file=sys.stderr)
    raise SystemExit(2)
'''


FAKE_GIT = r'''#!/usr/bin/env python3
import os
import sys


if sys.argv[1:] == ["rev-parse", "--show-toplevel"]:
    print(os.environ["FAKE_GIT_ROOT"])
else:
    raise SystemExit(2)
'''


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Installa un ``gh`` finto e restituisce il file che registra le chiamate."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "gh"
    executable.write_text(FAKE_GH, encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    git_executable = bin_dir / "git"
    git_executable.write_text(FAKE_GIT, encoding="utf-8")
    git_executable.chmod(git_executable.stat().st_mode | stat.S_IXUSR)

    log_path = tmp_path / "gh-calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(log_path))
    monkeypatch.setenv("FAKE_GIT_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return log_path


def read_calls(log_path: Path) -> list[list[str]]:
    """Legge le invocazioni registrate dal provider finto."""

    return [json.loads(line) for line in log_path.read_text().splitlines()]


def test_claim_won_creates_ref_updates_issue_and_records_execution(
    fake_gh: Path,
) -> None:
    result = task_claim.claim("owner/repo", 42, "develop")

    assert result is task_claim.WON
    calls = read_calls(fake_gh)
    assert [
        "api",
        "-X",
        "POST",
        "repos/owner/repo/git/refs",
        "-f",
        "ref=refs/heads/feature/42-implement-atomic-claim",
        "-f",
        "sha=base-sha",
    ] in calls
    assert any(call[:2] == ["issue", "comment"] for call in calls)
    assert any("--remove-label" in call and "aes:ready" in call for call in calls)
    assert any("--add-label" in call and "aes:claimed" in call for call in calls)
    operation_order = [call[:2] for call in calls]
    assert operation_order.index(["issue", "comment"]) < operation_order.index(
        ["issue", "edit"]
    )
    label_calls = [call for call in calls if call[:2] == ["issue", "edit"]]
    assert len(label_calls) == 2

    task_dir = Path(".agent/tasks")
    assert (task_dir / ".gitignore").read_text(encoding="utf-8") == "*\n"
    local_state = json.loads((task_dir / "current.json").read_text(encoding="utf-8"))
    assert local_state["number"] == 42
    assert local_state["execution_id"]


def test_claim_adds_claimed_before_removing_ready(fake_gh: Path) -> None:
    task_claim.claim("owner/repo", 42, "develop")

    label_calls = [
        call for call in read_calls(fake_gh) if call[:2] == ["issue", "edit"]
    ]
    assert "--add-label" in label_calls[0]
    assert "aes:claimed" in label_calls[0]
    assert "--remove-label" in label_calls[1]
    assert "aes:ready" in label_calls[1]


def test_claim_lost_has_no_issue_write_side_effects(
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_GH_REF_MODE", "exists")

    result = task_claim.claim("owner/repo", 42, "develop")

    assert result is task_claim.LOST
    calls = read_calls(fake_gh)
    assert not any(call[:2] == ["issue", "comment"] for call in calls)
    assert not any(call[:2] == ["issue", "edit"] for call in calls)
    assert not Path(".agent/tasks").exists()


def test_claim_lost_for_existing_issue_ref_with_different_slug_has_no_writes(
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_GH_MATCHING_REF", "different-slug")

    result = task_claim.claim("owner/repo", 42, "develop")

    assert result is task_claim.LOST
    calls = read_calls(fake_gh)
    assert not any(call[:3] == ["api", "-X", "POST"] for call in calls)
    assert not any(call[:2] == ["issue", "comment"] for call in calls)
    assert not any(call[:2] == ["issue", "edit"] for call in calls)
    assert not Path(".agent/tasks").exists()


def test_claim_records_local_state_at_repository_root_from_subdirectory(
    fake_gh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subdirectory = tmp_path / "nested" / "directory"
    subdirectory.mkdir(parents=True)
    monkeypatch.chdir(subdirectory)

    result = task_claim.claim("owner/repo", 42, "develop")

    assert result is task_claim.WON
    assert (tmp_path / ".agent" / "tasks" / "current.json").is_file()
    assert not (subdirectory / ".agent").exists()


def test_remote_claim_does_not_write_local_authoritative_state(fake_gh: Path) -> None:
    result = task_claim.claim_remote("owner/repo", 42, "develop", execution_id="exec-1")

    assert result is task_claim.WON
    assert not Path(".agent/tasks/current.json").exists()


def test_claimed_branch_requires_remote_claim_and_matching_execution(fake_gh: Path) -> None:
    with pytest.raises(task_claim.ClaimError, match="claimed"):
        task_claim.claimed_branch("owner/repo", 42, execution_id="exec-1")


def test_claim_comment_ownership_requires_explicit_token_actor_and_is_separate_from_issue_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comment = SimpleNamespace(author="aes-queue-bot")
    monkeypatch.setenv("GITHUB_ACTOR", "aes-queue-bot")
    monkeypatch.delenv("AES_TRUSTED_ACTOR", raising=False)
    monkeypatch.setenv("AES_QUEUE_ALLOWED_AUTHORS", "edo")

    # The workflow event actor is not evidence that the token wrote a claim.
    assert not task_claim._trusted_comment_actor(comment)

    monkeypatch.setenv("AES_TRUSTED_ACTOR", "aes-queue-bot")
    # The claim bot need not be an allowed issue author.
    assert task_claim._trusted_comment_actor(comment)
    assert not task_claim._trusted_comment_actor(SimpleNamespace(author="other-bot"))


def test_create_ref_raises_for_error_other_than_existing_ref(
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_GH_REF_MODE", "error")

    with pytest.raises(provider_github.GitHubError, match="Not Found"):
        provider_github.create_ref("owner/repo", "feature/42-task", "base-sha")


def test_list_tasks_keeps_issue_without_contract(fake_gh: Path) -> None:
    tasks = provider_github.list_tasks("owner/repo", ())

    assert len(tasks) == 1
    assert tasks[0].number == 9
    assert tasks[0].contract is None


def test_oversized_budget_is_rejected_by_list_and_dispatcher(
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    oversized_budget = "1" * 4301
    issue = {
        "number": 15,
        "title": "Oversized queue budget",
        "state": "open",
        "labels": [
            {"name": "aes:ready"},
            {"name": "role:backend"},
            {"name": "autonomy:worker"},
        ],
        "user": {"login": "edo"},
        "body": f"""<!-- aes:contract -->
repo: owner/repo
paths: harness/scripts/tasks/
constraints:
depends_on:
done: test verdi
<!-- /aes:contract -->
<!-- aes:dispatch -->
dispatch: auto
priority: normal
not_before:
budget: {oversized_budget}
<!-- /aes:dispatch -->""",
    }
    monkeypatch.setenv("FAKE_GH_TASK_PAGES", json.dumps([[issue]]))
    monkeypatch.setenv("AES_QUEUE_ENABLED", "true")
    monkeypatch.setenv("AES_QUEUE_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("AES_QUEUE_MAX_CONCURRENCY_PER_REPO", "1")
    monkeypatch.setenv("AES_QUEUE_BUDGET", "5")
    monkeypatch.setenv("AES_QUEUE_ALLOWED_AUTHORS", "edo")

    listed = provider_github.list_tasks("owner/repo", ("aes:ready",))
    assert len(listed) == 1
    assert listed[0].dispatch is None
    assert listed[0].dispatch_error is not None
    assert "budget" in listed[0].dispatch_error

    monkeypatch.setattr(
        dispatcher.provider_github,
        "list_tasks",
        lambda repo, labels: listed if labels == ("aes:ready",) else [],
    )
    monkeypatch.setattr(dispatcher.provider_github, "get_states", lambda repo, deps: {})
    events: list[tuple[int, str]] = []
    monkeypatch.setattr(
        tasks_cli,
        "notify_needs_human",
        lambda repo, number, execution_id, reason, question: events.append((number, reason))
        or True,
    )

    assert dispatcher.command_plan(SimpleNamespace(repo="owner/repo", apply=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tasks"] == []
    assert payload["skipped"][0]["issue"] == 15
    assert "budget" in payload["skipped"][0]["reason"]
    assert events and events[0][0] == 15
    assert "budget" in events[0][1]


def test_list_tasks_paginates_beyond_first_hundred_without_starvation(
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_page = [
        {
            "number": number,
            "title": f"Backlog task {number}",
            "state": "open",
            "labels": [],
            "body": "Issue senza contratto.",
        }
        for number in range(1, 101)
    ]
    second_page = [{
        "number": 101,
        "title": "Task after the first page",
        "state": "open",
        "labels": [],
        "body": "Issue senza contratto.",
    }]
    monkeypatch.setenv(
        "FAKE_GH_TASK_PAGES",
        json.dumps([first_page, second_page]),
    )

    tasks = provider_github.list_tasks("owner/repo", ())

    assert len(tasks) == 101
    assert tasks[-1].number == 101


def test_list_tasks_normalizes_rest_author_and_created_at_fields(
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "FAKE_GH_TASK_PAGES",
        json.dumps([[
            {
                "number": 12,
                "title": "REST issue",
                "state": "open",
                "labels": [],
                "body": "Issue senza contratto.",
                "user": {"login": "edo"},
                "created_at": "2026-09-08T12:34:56Z",
            },
        ]]),
    )

    task = provider_github.list_tasks("owner/repo", ())[0]

    assert task.author == "edo"
    assert task.created_at is not None
    assert task.created_at.isoformat() == "2026-09-08T12:34:56+00:00"


def test_get_states_qualifies_local_dependencies_and_omits_missing(
    fake_gh: Path,
) -> None:
    states = provider_github.get_states(
        "owner/repo",
        ("7", "other/project#8", "999"),
    )

    assert states == {
        "owner/repo#7": "closed",
        "other/project#8": "open",
    }
