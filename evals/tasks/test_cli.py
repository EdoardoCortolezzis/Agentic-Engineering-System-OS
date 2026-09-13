from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import unquote

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "harness" / "scripts" / "tasks" / "tasks.py"
TASKS_DIR = REPO_ROOT / "harness" / "scripts" / "tasks"
sys.path.insert(0, str(TASKS_DIR))
import tasks as tasks_cli  # noqa: E402

# The lease is measured in hours from the claim: a fixed date would make the
# eval fail on its own when the threshold passes, rather than due to a code
# regression.
RECENT_CLAIM = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
    "%Y-%m-%dT%H:%M:%SZ"
)

FAKE_GH = r'''#!/usr/bin/env python3
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from urllib.parse import unquote


args = sys.argv[1:]
log_path = Path(os.environ["FAKE_GH_LOG"])
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")

state_path = Path(os.environ["FAKE_GH_STATE"])
if state_path.exists():
    state = json.loads(state_path.read_text(encoding="utf-8"))
else:
    state = {"labels": [], "issues": [], "refs": []}

if args[:2] == ["repo", "view"]:
    print("owner/repo")
elif args[:2] == ["issue", "list"]:
    required_labels = [
        args[index + 1]
        for index, value in enumerate(args)
        if value == "--label"
    ]
    rows = [
        issue for issue in state["issues"]
        if all(
            label in [item["name"] for item in issue.get("labels", [])]
            for label in required_labels
        )
    ]
    print(json.dumps(rows))
elif args and args[0] == "api" and args[1].startswith("repos/owner/repo/issues?"):
    required_labels = []
    query = args[1].partition("?")[2]
    for pair in query.split("&"):
        key, _, value = pair.partition("=")
        if key == "labels":
            required_labels = unquote(value).split(",") if value else []
    rows = [
        issue for issue in state["issues"]
        if all(
            label in [item["name"] for item in issue.get("labels", [])]
            for label in required_labels
        )
    ]
    print(json.dumps([rows]))
elif args[:2] == ["issue", "view"]:
    number = int(args[2])
    issue = next(item for item in state["issues"] if item["number"] == number)
    print(json.dumps(issue))
elif args[:2] == ["label", "list"]:
    print(json.dumps([{"name": label} for label in state["labels"]]))
elif args[:2] == ["label", "create"]:
    label = args[2]
    if label in state["labels"]:
        print("label already exists", file=sys.stderr)
        raise SystemExit(1)
    state["labels"].append(label)
    state_path.write_text(json.dumps(state), encoding="utf-8")
elif len(args) >= 2 and args[0] == "api" and args[1].startswith(
    "repos/owner/repo/git/matching-refs/heads/feature/"
):
    print(json.dumps([{"ref": ref} for ref in state["refs"]]))
elif len(args) >= 2 and args[0] == "api" and args[1].startswith(
    "repos/owner/repo/issues/"
) and args[1].endswith("/comments"):
    number = args[1].split("/")[-2]
    print(json.dumps(state.get("comments", {}).get(number, [])))
elif args[:2] == ["api", "repos/owner/repo/commits"]:
    branch = next(value.split("=", 1)[1] for value in args if value.startswith("sha="))
    branch = branch.removeprefix("refs/heads/")
    default_date = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    print(state.get("commits", {}).get(branch, default_date))
elif args[:2] == ["api", "repos/owner/repo/issues/2"]:
    print("open")
elif args[:2] == ["api", "repos/owner/repo/git/ref/heads/develop"]:
    print("base-sha")
elif args[:3] == ["api", "-X", "POST"]:
    if os.environ.get("FAKE_GH_REF_MODE") == "exists":
        print(json.dumps({
            "message": "Validation Failed",
            "errors": [{"message": "Reference already exists"}],
        }), file=sys.stderr)
        raise SystemExit(1)
    # Creating a ref makes it visible: without recording it, the fake would
    # lie about the primitive on which the claim relies.
    created = os.environ.get("FAKE_GH_CREATED_REF") or next(
        (value.split("=", 1)[1] for value in args if value.startswith("ref=")),
        "refs/heads/feature/42-implement-atomic-claim",
    )
    state.setdefault("refs", []).append(created)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    print(json.dumps({"ref": created}))
elif args[:2] == ["issue", "edit"]:
    # Keep the fake consistent with the remote primitive under test: the claim
    # adds ``aes:claimed`` before removing ``aes:ready``, and the subsequent
    # adoption rereads the issue through ``gh issue view``.
    number = int(args[2])
    issue = next(item for item in state["issues"] if item["number"] == number)
    labels = [item["name"] for item in issue.get("labels", [])]
    if "--add-label" in args:
        label = args[args.index("--add-label") + 1]
        if label not in labels:
            labels.append(label)
    if "--remove-label" in args:
        label = args[args.index("--remove-label") + 1]
        labels = [value for value in labels if value != label]
    issue["labels"] = [{"name": label} for label in labels]
    state_path.write_text(json.dumps(state), encoding="utf-8")
elif args[:2] == ["issue", "comment"]:
    pass
elif args[:2] == ["pr", "list"]:
    is_open_query = "--state" in args and args[args.index("--state") + 1] == "open"
    has_only_closed_pr = os.environ.get("FAKE_GH_PR_STATE") == "closed"
    print(json.dumps([] if is_open_query and has_only_closed_pr else [{"number": 77}]))
else:
    print(f"unexpected fake gh invocation: {args}", file=sys.stderr)
    raise SystemExit(2)
'''


def contract(*, depends_on: str = "") -> str:
    """Build a valid flat contract for fake issues."""

    return f"""<!-- aes:contract -->
repo: owner/repo
paths: harness/scripts/tasks/
constraints: Keep the provider seam.
depends_on: {depends_on}
done: tests pass
<!-- /aes:contract -->"""


def issue(
    number: int,
    title: str,
    labels: tuple[str, ...],
    *,
    body: str | None = None,
) -> dict[str, object]:
    """Return an issue in the JSON format emitted by ``gh``."""

    return {
        "number": number,
        "title": title,
        "state": "OPEN",
        "labels": [{"name": label} for label in labels],
        "body": contract() if body is None else body,
    }


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Install a fake ``gh`` with state persisted across invocations."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "gh"
    executable.write_text(FAKE_GH, encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    log_path = tmp_path / "gh-calls.jsonl"
    state_path = tmp_path / "gh-state.json"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(log_path))
    monkeypatch.setenv("FAKE_GH_STATE", str(state_path))
    return state_path, log_path


def write_state(
    path: Path,
    *,
    labels: tuple[str, ...] = (),
    issues: tuple[dict[str, object], ...] = (),
    refs: tuple[str, ...] = (),
    comments: dict[str, list[dict[str, str]]] | None = None,
    commits: dict[str, str] | None = None,
) -> None:
    """Write the state consumed by the fake provider."""

    path.write_text(
        json.dumps({
            "labels": labels,
            "issues": issues,
            "refs": refs,
            "comments": comments or {},
            "commits": commits or {},
        }),
        encoding="utf-8",
    )


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the CLI as a user would, preserving the fake ``PATH``."""

    return subprocess.run(
        (str(CLI), *args),
        capture_output=True,
        text=True,
        check=False,
    )


def read_calls(path: Path) -> list[list[str]]:
    """Read recorded calls, including when the file does not exist yet."""

    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_next_excludes_ready_task_with_open_dependency(
    fake_gh: tuple[Path, Path],
) -> None:
    state_path, _ = fake_gh
    write_state(
        state_path,
        issues=(
            issue(1, "Blocked by dependency", ("aes:ready",), body=contract(depends_on="2")),
            issue(3, "Actually dispatchable", ("aes:ready",)),
        ),
    )

    result = run_cli("next", "--repo", "owner/repo")

    assert result.returncode == 0, result.stderr
    assert "#3" in result.stdout
    assert "Actually dispatchable" in result.stdout
    assert "#1" not in result.stdout
    assert "Blocked by dependency" not in result.stdout


def test_show_reports_readiness_block_reason(fake_gh: tuple[Path, Path]) -> None:
    state_path, _ = fake_gh
    write_state(
        state_path,
        issues=(
            issue(1, "Blocked by dependency", ("aes:ready",), body=contract(depends_on="2")),
        ),
    )

    result = run_cli("show", "1", "--repo", "owner/repo")

    assert result.returncode == 0, result.stderr
    assert "Blocked by dependency" in result.stdout
    assert "dependency 2 is open" in result.stdout
    assert "harness/scripts/tasks/" in result.stdout


def test_init_labels_is_idempotent(fake_gh: tuple[Path, Path]) -> None:
    state_path, log_path = fake_gh
    write_state(state_path)

    first = run_cli("init-labels", "--repo", "owner/repo")
    second = run_cli("init-labels", "--repo", "owner/repo")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    create_calls = [call for call in read_calls(log_path) if call[:2] == ["label", "create"]]
    assert len(create_calls) == 6
    assert {call[2] for call in create_calls} == {
        "aes:ready",
        "aes:claimed",
        "aes:waiting-provider",
        "aes:pr-open",
        "aes:blocked",
        "aes:needs-human",
    }


def test_doctor_reports_orphan_claim_and_never_writes(
    fake_gh: tuple[Path, Path],
) -> None:
    state_path, log_path = fake_gh
    write_state(
        state_path,
        labels=(
            "aes:ready",
            "aes:claimed",
            "aes:waiting-provider",
            "aes:pr-open",
            "aes:blocked",
            "aes:needs-human",
        ),
        issues=(issue(8, "Orphan claim", ("aes:claimed",)),),
    )

    result = run_cli("doctor", "--repo", "owner/repo")

    assert result.returncode != 0
    assert "#8" in result.stdout
    assert "ref" in result.stdout.lower()
    calls = read_calls(log_path)
    assert not any(call[:2] in (["issue", "edit"], ["issue", "comment"]) for call in calls)
    assert not any(call[:2] == ["label", "create"] for call in calls)
    assert not any("--method" in call or "-X" in call for call in calls)


def test_doctor_reports_missing_audit_comment_and_stale_lease_without_writes(
    fake_gh: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path, log_path = fake_gh
    monkeypatch.setenv("AES_LEASE_STALE_HOURS", "24")
    write_state(
        state_path,
        labels=(
            "aes:ready",
            "aes:claimed",
            "aes:waiting-provider",
            "aes:pr-open",
            "aes:blocked",
            "aes:needs-human",
        ),
        issues=(
            issue(8, "Orphan claim", ("aes:claimed",)),
            issue(9, "Recent claim", ("aes:claimed",)),
        ),
        refs=("refs/heads/feature/8-orphan-claim", "refs/heads/feature/9-recent-claim"),
        comments={
            "8": [],
            "9": [{
                "body": "Claim execution_id: 12345678-1234-1234-1234-123456789abc",
                "created_at": RECENT_CLAIM,
            }],
        },
        commits={"feature/8-orphan-claim": "2020-01-01T00:00:00Z"},
    )

    result = run_cli("doctor", "--repo", "owner/repo")

    assert result.returncode != 0
    assert "execution_id" in result.stdout
    assert "stale lease" in result.stdout
    assert "#9" not in result.stdout
    calls = read_calls(log_path)
    assert not any(call[:2] in (["issue", "edit"], ["issue", "comment"]) for call in calls)
    assert not any("-X" in call and "POST" in call for call in calls)


def test_doctor_reports_dependency_cycle(fake_gh: tuple[Path, Path]) -> None:
    state_path, _ = fake_gh
    write_state(
        state_path,
        labels=(
            "aes:ready",
            "aes:claimed",
            "aes:waiting-provider",
            "aes:pr-open",
            "aes:blocked",
            "aes:needs-human",
        ),
        issues=(
            issue(1, "First", ("aes:ready",), body=contract(depends_on="2")),
            issue(2, "Second", ("aes:ready",), body=contract(depends_on="1")),
        ),
    )

    result = run_cli("doctor", "--repo", "owner/repo")

    assert result.returncode == 1
    assert "dependency cycle" in result.stdout
    assert "owner/repo#1" in result.stdout
    assert "owner/repo#2" in result.stdout


def test_doctor_does_not_mark_recent_claim_on_old_base_as_stale(
    fake_gh: tuple[Path, Path],
) -> None:
    state_path, _ = fake_gh
    write_state(
        state_path,
        labels=(
            "aes:ready",
            "aes:claimed",
            "aes:waiting-provider",
            "aes:pr-open",
            "aes:blocked",
            "aes:needs-human",
        ),
        issues=(issue(9, "Recent claim", ("aes:claimed",)),),
        refs=("refs/heads/feature/9-recent-claim",),
        comments={"9": [{
            "body": "Claim execution_id: 12345678-1234-1234-1234-123456789abc",
            "created_at": RECENT_CLAIM,
        }]},
        commits={"feature/9-recent-claim": "2020-01-01T00:00:00Z"},
    )

    result = run_cli("doctor", "--repo", "owner/repo")

    assert result.returncode == 0, result.stdout
    assert "stale lease" not in result.stdout


def test_a_later_comment_does_not_refresh_an_expired_lease(
    fake_gh: tuple[Path, Path],
) -> None:
    """The lease starts at the claim, not at the latest comment.

    With the latest comment as the anchor, anyone who can write to the issue
    refreshes the lease by posting an execution_id: an abandoned claim would
    never expire, leaving the task unassignable.
    """
    old_claim = (datetime.now(timezone.utc) - timedelta(days=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    write_state(
        fake_gh[0],
        labels=(
            "aes:ready",
            "aes:claimed",
            "aes:waiting-provider",
            "aes:pr-open",
            "aes:blocked",
            "aes:needs-human",
        ),
        issues=(issue(9, "Abandoned claim", ("aes:claimed",)),),
        refs=("refs/heads/feature/9-abandoned-claim",),
        comments={"9": [
            {"body": "Claim execution_id: 12345678-1234-1234-1234-123456789abc",
             "created_at": old_claim},
            {"body": "Ancora vivo? execution_id: 87654321-4321-4321-4321-cba987654321",
             "created_at": RECENT_CLAIM},
        ]},
        commits={"feature/9-abandoned-claim": "2020-01-01T00:00:00Z"},
    )

    result = run_cli("doctor", "--repo", "owner/repo")

    assert result.returncode == 1, result.stdout
    assert "stale lease" in result.stdout


def test_provider_uses_pagination_for_refs_and_warns_at_list_caps(
    fake_gh: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_path, log_path = fake_gh
    write_state(
        state_path,
        labels=tuple(f"label-{index}" for index in range(100)),
        issues=tuple(issue(index, f"Task {index}", ()) for index in range(100)),
        refs=("refs/heads/feature/1-one",),
    )

    tasks_cli.provider_github.list_matching_refs("owner/repo", "feature/")
    tasks_cli.provider_github.list_tasks("owner/repo", ())
    tasks_cli.provider_github.list_labels("owner/repo")

    calls = read_calls(log_path)
    ref_call = next(call for call in calls if "matching-refs" in " ".join(call))
    assert "--paginate" in ref_call
    warnings = capsys.readouterr().err
    # Issue listing is now fully paginated; only the intentionally capped
    # label listing should still warn.
    assert warnings.count("potrebbe essere troncato") == 1


def test_link_pr_does_not_link_closed_pull_request(
    fake_gh: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path, log_path = fake_gh
    monkeypatch.setenv("FAKE_GH_PR_STATE", "closed")
    write_state(
        state_path,
        refs=("refs/heads/feature/8-closed-pr",),
        issues=(issue(8, "Closed PR", ("aes:claimed",)),),
    )

    result = run_cli("link-pr", "8", "--repo", "owner/repo")

    assert result.returncode != 0
    assert "no pull request found" in result.stderr
    assert not any(call[:2] == ["issue", "edit"] for call in read_calls(log_path))


def test_claim_won_invokes_feature_start_adopt_with_feature_name(
    fake_gh: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, ...]] = []
    write_state(fake_gh[0], issues=(issue(42, "Implement Atomic Claim!", ("aes:ready",)),))

    def adopt(command: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "Worktree: /tmp/42-implement-atomic-claim\n", "")

    monkeypatch.setattr(tasks_cli, "subprocess", SimpleNamespace(run=adopt))

    result = tasks_cli.main(["claim", "42", "--repo", "owner/repo"])

    assert result == 0
    assert calls == [(
        str(REPO_ROOT / "harness" / "scripts" / "feature-start.sh"),
        "--adopt",
        "42-implement-atomic-claim",
    )]
    assert "Worktree:" in capsys.readouterr().out


def test_claim_adopts_the_branch_that_exists_not_the_one_the_title_implies(
    fake_gh: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The title may change between reading it and creating the ref.

    If the name were recalculated from the title, `--adopt` would receive a
    slug that matches no branch: adoption would fail, and the suggested manual
    command would also point to a nonexistent branch. The ref is the source of
    truth for ownership.
    """
    calls: list[tuple[str, ...]] = []
    write_state(fake_gh[0], issues=(issue(42, "Title changed after claim", ("aes:ready",)),))
    monkeypatch.setenv("FAKE_GH_CREATED_REF", "refs/heads/feature/42-original-title")

    def adopt(command: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(tasks_cli, "subprocess", SimpleNamespace(run=adopt))

    assert tasks_cli.main(["claim", "42", "--repo", "owner/repo"]) == 0
    assert calls[0][-1] == "42-original-title"


def test_claim_lost_does_not_invoke_feature_start(
    fake_gh: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_GH_REF_MODE", "exists")
    calls: list[tuple[str, ...]] = []
    write_state(fake_gh[0], issues=(issue(42, "Implement Atomic Claim!", ("aes:ready",)),))

    def adopt(command: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(tasks_cli, "subprocess", SimpleNamespace(run=adopt))

    result = tasks_cli.main(["claim", "42", "--repo", "owner/repo"])

    assert result == 1
    assert calls == []


def test_claim_keeps_valid_claim_when_worktree_adoption_fails(
    fake_gh: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_state(fake_gh[0], issues=(issue(42, "Implement Atomic Claim!", ("aes:ready",)),))

    def fail_adopt(command: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "cannot adopt")

    monkeypatch.setattr(tasks_cli, "subprocess", SimpleNamespace(run=fail_adopt))

    result = tasks_cli.main(["claim", "42", "--repo", "owner/repo"])

    assert result == 1
    assert "Claim is valid" in capsys.readouterr().err
    calls = read_calls(fake_gh[1])
    assert any(call[:2] == ["api", "-X"] for call in calls)
