from pathlib import Path
import sys


TASKS_DIR = Path(__file__).resolve().parents[2] / "harness" / "scripts" / "tasks"
sys.path.insert(0, str(TASKS_DIR))

from contract import Contract  # noqa: E402
from readiness import Task, find_cycle, is_ready  # noqa: E402


def make_contract(*dependencies: str) -> Contract:
    return Contract(
        repo=None,
        paths=(),
        constraints="",
        depends_on=dependencies,
        done=(),
    )


def make_task(
    number: int = 1,
    *,
    state: str = "open",
    labels: tuple[str, ...] = ("aes:ready",),
    dependencies: tuple[str, ...] = (),
    repo_slug: str = "owner/repo",
) -> Task:
    return Task(
        number=number,
        repo_slug=repo_slug,
        title=f"Task {number}",
        state=state,
        labels=labels,
        contract=make_contract(*dependencies),
    )


def test_open_ready_task_without_dependencies_is_ready() -> None:
    ready, reason = is_ready(make_task(), {})

    assert ready is True
    assert reason == ""


def test_open_dependency_blocks_and_reason_names_it() -> None:
    ready, reason = is_ready(
        make_task(dependencies=("38",)),
        {"owner/repo#38": "open"},
    )

    assert ready is False
    assert reason == "dependency 38 is open"


def test_local_dependency_states_do_not_collide_across_repositories() -> None:
    dep_states = {
        "owner/alpha#38": "closed",
        "owner/beta#38": "open",
    }

    alpha_ready, alpha_reason = is_ready(
        make_task(repo_slug="owner/alpha", dependencies=("38",)),
        dep_states,
    )
    beta_ready, beta_reason = is_ready(
        make_task(repo_slug="owner/beta", dependencies=("38",)),
        dep_states,
    )

    assert (alpha_ready, alpha_reason) == (True, "")
    assert (beta_ready, beta_reason) == (False, "dependency 38 is open")


def test_missing_dependency_blocks_and_reason_says_not_found() -> None:
    ready, reason = is_ready(make_task(dependencies=("999",)), {})

    assert ready is False
    assert "999" in reason
    assert "not found" in reason


def test_blocked_label_takes_precedence_over_ready() -> None:
    ready, reason = is_ready(
        make_task(labels=("aes:ready", "aes:blocked")),
        {},
    )

    assert ready is False
    assert "aes:blocked" in reason


def test_needs_human_label_takes_precedence_over_ready() -> None:
    ready, reason = is_ready(
        make_task(labels=("aes:ready", "aes:needs-human")),
        {},
    )

    assert ready is False
    assert "aes:needs-human" in reason


def test_closed_task_is_not_ready() -> None:
    ready, reason = is_ready(make_task(state="closed"), {})

    assert ready is False
    assert reason == "task is not open"


def test_dependency_cycle_is_returned() -> None:
    tasks = {
        "owner/repo#1": make_task(number=1, dependencies=("2",)),
        "owner/repo#2": make_task(number=2, dependencies=("1",)),
    }

    assert find_cycle(tasks) == (
        "owner/repo#1",
        "owner/repo#2",
        "owner/repo#1",
    )
