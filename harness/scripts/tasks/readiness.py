"""Pure readiness evaluation and cycles between tasks."""

from dataclasses import dataclass
from datetime import datetime

from contract import Contract
from dispatch import DispatchMetadata


@dataclass(frozen=True)
class Task:
    """Represent issue data needed for readiness evaluation."""

    number: int
    repo_slug: str
    title: str
    state: str
    labels: tuple[str, ...]
    contract: Contract | None
    dispatch: DispatchMetadata | None = None
    created_at: datetime | None = None
    body: str = ""
    dispatch_error: str | None = None
    author: str = ""


def is_ready(task: Task, dep_states: dict[str, str]) -> tuple[bool, str]:
    """Return readiness and the reason for any blocking condition."""

    for blocking_label in ("aes:blocked", "aes:needs-human", "aes:waiting-provider"):
        if blocking_label in task.labels:
            return False, f"task has {blocking_label} label"
    if task.state != "open":
        return False, "task is not open"
    if "aes:ready" not in task.labels:
        return False, "task does not have aes:ready label"
    if task.contract is None:
        return False, "task has no contract"

    for dependency in task.contract.depends_on:
        dependency_key = _dependency_key(task, dependency)
        state = dep_states.get(dependency_key)
        if state is None:
            return False, f"dependency {dependency} not found"
        if state != "closed":
            return False, f"dependency {dependency} is {state}"

    return True, ""


def _dependency_key(task: Task, dependency: str) -> str:
    """Convert a local number into the task's qualified key."""

    if "#" in dependency:
        return dependency
    return f"{task.repo_slug}#{dependency}"


def find_cycle(tasks: dict[str, Task]) -> tuple[str, ...] | None:
    """Return the first cycle found, including the starting node again."""

    visiting: set[str] = set()
    visited: set[str] = set()
    path: list[str] = []
    positions: dict[str, int] = {}

    def visit(task_key: str) -> tuple[str, ...] | None:
        if task_key in visiting:
            start = positions[task_key]
            return tuple(path[start:] + [task_key])
        if task_key in visited:
            return None

        visiting.add(task_key)
        positions[task_key] = len(path)
        path.append(task_key)

        task = tasks[task_key]
        dependencies = task.contract.depends_on if task.contract else ()
        for dependency in dependencies:
            dependency_key = _dependency_key(task, dependency)
            if dependency_key not in tasks:
                continue
            cycle = visit(dependency_key)
            if cycle is not None:
                return cycle

        path.pop()
        positions.pop(task_key)
        visiting.remove(task_key)
        visited.add(task_key)
        return None

    for task_key in tasks:
        cycle = visit(task_key)
        if cycle is not None:
            return cycle
    return None
