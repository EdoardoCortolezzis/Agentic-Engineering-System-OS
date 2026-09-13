"""Provider-neutral controller/subtask contracts and execution artifacts.

This module contains only deterministic validation and local artifact I/O.  It
does not invoke a provider CLI, create a worktree, or publish anything.  A
future controller/worker adapter can use the contract here while keeping
provider-specific process handling outside the queue protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any, Iterator, Mapping, Sequence
import uuid


PLAN_SCHEMA = "aes.orchestration-plan.v1"
VERDICT_SCHEMA = "aes.orchestration-verdict.v1"
STATE_SCHEMA = "aes.orchestration-state.v1"
RESULT_SCHEMA = "aes.orchestration-result.v1"

ORCHESTRATION_STATES = frozenset(
    {
        "pending",
        "planned",
        "running",
        "waiting_provider",
        "needs_human",
        "verified",
        "failed",
    }
)

REASONING_EFFORTS = frozenset({"high", "xhigh"})
CONTROLLER_EFFORT = "medium"
DIFFICULTIES = frozenset(
    {
        "low",
        "medium",
        "high",
        "critical",
        "easy",
        "moderate",
        "hard",
        "very_high",
        "very_hard",
    }
)

_EXECUTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SUBTASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class OrchestrationError(ValueError):
    """Indicates an invalid orchestration contract or artifact operation."""


class ArtifactError(RuntimeError):
    """Indicates an unavailable or inconsistent orchestration artifact."""


def _reject_symlink_chain(value: str | os.PathLike[str], field_name: str) -> None:
    """Reject symlink components before a trusted artifact path is used."""

    path = Path(value)
    if not path.is_absolute():
        path = Path.cwd() / path
    for ancestor in reversed(path.parents):
        if ancestor == ancestor.parent:
            break
        if ancestor.is_symlink():
            raise ArtifactError(f"{field_name} path must not contain symlinks")
    if path.is_symlink():
        raise ArtifactError(f"{field_name} path must not be a symlink")


@dataclass(frozen=True)
class ModelSpec:
    """A provider/model/effort tuple that an adapter can resolve later."""

    provider: str
    model: str
    reasoning_effort: str

    def __post_init__(self) -> None:
        for name, value in (
            ("provider", self.provider),
            ("model", self.model),
            ("reasoning_effort", self.reasoning_effort),
        ):
            if not isinstance(value, str) or not value.strip():
                raise OrchestrationError(f"model {name} must be a non-empty string")

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, field_name: str) -> "ModelSpec":
        _require_exact_keys(payload, {"provider", "model", "reasoning_effort"}, field_name)
        return cls(
            provider=_string(payload["provider"], f"{field_name}.provider"),
            model=_string(payload["model"], f"{field_name}.model"),
            reasoning_effort=_string(
                payload["reasoning_effort"], f"{field_name}.reasoning_effort"
            ),
        )


@dataclass(frozen=True)
class ModelRoute:
    """Primary/fallback route declared in a plan, without invoking either."""

    primary: ModelSpec
    fallback: ModelSpec

    def to_dict(self) -> dict[str, dict[str, str]]:
        return {"primary": self.primary.to_dict(), "fallback": self.fallback.to_dict()}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, field_name: str) -> "ModelRoute":
        _require_exact_keys(payload, {"primary", "fallback"}, field_name)
        return cls(
            primary=ModelSpec.from_dict(payload["primary"], field_name=f"{field_name}.primary"),
            fallback=ModelSpec.from_dict(payload["fallback"], field_name=f"{field_name}.fallback"),
        )


# The model names are part of the versioned orchestration policy, while the
# actual invocation remains the responsibility of a provider adapter.
DEFAULT_CONTROLLER_ROUTE = ModelRoute(
    primary=ModelSpec("openai", "gpt-5.6-sol", "medium"),
    fallback=ModelSpec("anthropic", "claude-opus-5", "medium"),
)
DEFAULT_WORKER_ROUTE = ModelRoute(
    primary=ModelSpec("openai", "gpt-5.6-luna", "high"),
    fallback=ModelSpec("anthropic", "claude-sonnet-5", "high"),
)

_ALLOWED_RESULT_KEYS = frozenset(
    {
        "status",
        "summary",
        "changed",
        "tests",
        "notes",
        "error",
        # Invocation metadata is intentionally small and secret-free.  The
        # orchestrator records it next to the worker outcome for auditability.
        "provider",
        "model",
        "effort",
        "classification",
        "attempts",
        "fallback_used",
    }
)
_RESULT_ARTIFACT_KEYS = frozenset(
    {"schema", "execution_id", "subtask_id", "recorded_at", "result"}
)
_RESULT_STATUSES = frozenset({"ok", "failed", "blocked", "needs_human"})
_MAX_RESULT_TEXT = 16 * 1024
MAX_LOG_BYTES = 64 * 1024
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]+\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"(?i)(\bauthorization\s*:\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(r"(?i)(\b(?:token|password|secret|api[_-]?key)\s*[=:]\s*)[^\s,;]+"),
)


def _validate_routes(controller: ModelRoute, worker: ModelRoute) -> None:
    """Enforce the versioned provider/model policy at the contract boundary."""

    if not isinstance(controller, ModelRoute) or not isinstance(worker, ModelRoute):
        raise OrchestrationError("plan controller and worker routes must be objects")

    expected_controller = (
        ("primary", controller.primary, "openai", "gpt-5.6-sol", {CONTROLLER_EFFORT}),
        ("fallback", controller.fallback, "anthropic", "claude-opus-5", {CONTROLLER_EFFORT}),
    )
    for name, actual, provider, model, efforts in expected_controller:
        _validate_route_entry("controller", name, actual, provider, model, efforts)

    expected_worker = (
        ("primary", worker.primary, "openai", "gpt-5.6-luna", REASONING_EFFORTS),
        ("fallback", worker.fallback, "anthropic", "claude-sonnet-5", REASONING_EFFORTS),
    )
    for name, actual, provider, model, efforts in expected_worker:
        _validate_route_entry("worker", name, actual, provider, model, efforts)


def _validate_route_entry(
    route_name: str,
    entry_name: str,
    actual: ModelSpec,
    expected_provider: str,
    expected_model: str,
    allowed_efforts: frozenset[str] | set[str],
) -> None:
    field_name = f"plan.{route_name}.{entry_name}"
    if not isinstance(actual, ModelSpec):
        raise OrchestrationError(f"{field_name} must be a model specification")
    if actual.provider != expected_provider or actual.model != expected_model:
        raise OrchestrationError(
            f"{field_name} must use {expected_provider}/{expected_model}"
        )
    if actual.reasoning_effort not in allowed_efforts:
        allowed = ", ".join(sorted(allowed_efforts))
        raise OrchestrationError(
            f"{field_name}.reasoning_effort must be one of: {allowed}"
        )


@dataclass(frozen=True)
class Subtask:
    """A bounded, independently executable unit returned by the controller."""

    id: str
    title: str
    prompt: str
    paths: tuple[str, ...]
    dependencies: tuple[str, ...]
    difficulty: str
    reasoning_effort: str

    def __post_init__(self) -> None:
        _validate_subtask_id(self.id)
        _non_empty(self.title, "subtask.title")
        _non_empty(self.prompt, "subtask.prompt")
        if not self.paths:
            raise OrchestrationError(f"subtask {self.id!r} must declare at least one path")
        for path in self.paths:
            _validate_relative_path(path, f"subtask {self.id!r} path")
        if len(set(self.paths)) != len(self.paths):
            raise OrchestrationError(f"subtask {self.id!r} contains duplicate paths")
        for dependency in self.dependencies:
            _validate_subtask_id(dependency)
        if len(set(self.dependencies)) != len(self.dependencies):
            raise OrchestrationError(f"subtask {self.id!r} contains duplicate dependencies")
        if self.id in self.dependencies:
            raise OrchestrationError(f"subtask {self.id!r} cannot depend on itself")
        if self.difficulty not in DIFFICULTIES:
            raise OrchestrationError(
                f"subtask {self.id!r} has unsupported difficulty {self.difficulty!r}"
            )
        if self.reasoning_effort not in REASONING_EFFORTS:
            raise OrchestrationError(
                f"subtask {self.id!r} reasoning_effort must be 'high' or 'xhigh'"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "prompt": self.prompt,
            "paths": list(self.paths),
            "dependencies": list(self.dependencies),
            "difficulty": self.difficulty,
            "reasoning_effort": self.reasoning_effort,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Subtask":
        _require_exact_keys(
            payload,
            {
                "id",
                "title",
                "prompt",
                "paths",
                "dependencies",
                "difficulty",
                "reasoning_effort",
            },
            "subtask",
        )
        return cls(
            id=_string(payload["id"], "subtask.id"),
            title=_string(payload["title"], "subtask.title"),
            prompt=_string(payload["prompt"], "subtask.prompt"),
            paths=_string_list(payload["paths"], "subtask.paths"),
            dependencies=_string_list(payload["dependencies"], "subtask.dependencies"),
            difficulty=_string(payload["difficulty"], "subtask.difficulty"),
            reasoning_effort=_string(
                payload["reasoning_effort"], "subtask.reasoning_effort"
            ),
        )


@dataclass(frozen=True)
class ControllerPlan:
    """Strict, provider-neutral plan emitted before worker execution."""

    execution_id: str
    subtasks: tuple[Subtask, ...]
    controller: ModelRoute = DEFAULT_CONTROLLER_ROUTE
    worker: ModelRoute = DEFAULT_WORKER_ROUTE
    schema: str = PLAN_SCHEMA

    def __post_init__(self) -> None:
        validate_execution_id(self.execution_id)
        if self.schema != PLAN_SCHEMA:
            raise OrchestrationError(f"unsupported plan schema: {self.schema!r}")
        _validate_routes(self.controller, self.worker)
        if not self.subtasks:
            raise OrchestrationError("controller plan must contain at least one subtask")
        ids = [subtask.id for subtask in self.subtasks]
        if len(ids) != len(set(ids)):
            raise OrchestrationError("controller plan contains duplicate subtask ids")
        known = set(ids)
        for subtask in self.subtasks:
            unknown = set(subtask.dependencies) - known
            if unknown:
                raise OrchestrationError(
                    f"subtask {subtask.id!r} has unknown dependencies: {', '.join(sorted(unknown))}"
                )
        _assert_acyclic(self.subtasks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "execution_id": self.execution_id,
            "controller": self.controller.to_dict(),
            "worker": self.worker.to_dict(),
            "subtasks": [subtask.to_dict() for subtask in self.subtasks],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2) + "\n"

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ControllerPlan":
        _require_exact_keys(
            payload,
            {"schema", "execution_id", "controller", "worker", "subtasks"},
            "plan",
        )
        subtasks = payload["subtasks"]
        if not isinstance(subtasks, list):
            raise OrchestrationError("plan.subtasks must be a JSON array")
        return cls(
            schema=_string(payload["schema"], "plan.schema"),
            execution_id=_string(payload["execution_id"], "plan.execution_id"),
            controller=ModelRoute.from_dict(payload["controller"], field_name="plan.controller"),
            worker=ModelRoute.from_dict(payload["worker"], field_name="plan.worker"),
            subtasks=tuple(Subtask.from_dict(item) for item in subtasks),
        )

    @classmethod
    def from_json(cls, raw: str) -> "ControllerPlan":
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise OrchestrationError("plan is not valid JSON") from error
        if not isinstance(payload, dict):
            raise OrchestrationError("plan root must be a JSON object")
        return cls.from_dict(payload)


@dataclass(frozen=True)
class VerificationVerdict:
    """Strict final-controller verification result.

    ``checks`` accepts either short text entries or objects with exactly the
    keys ``name``, ``status`` and optional ``details``.  Keeping the root
    schema strict prevents prose or an unrelated JSON object from being
    mistaken for a verification decision while allowing useful check detail.
    """

    execution_id: str
    verdict: str
    summary: str
    checks: tuple[str | dict[str, str], ...]
    schema: str = VERDICT_SCHEMA

    def __post_init__(self) -> None:
        validate_execution_id(self.execution_id)
        if self.schema != VERDICT_SCHEMA:
            raise OrchestrationError(f"unsupported verdict schema: {self.schema!r}")
        if self.verdict not in {"verified", "failed", "needs_human"}:
            raise OrchestrationError(
                "verdict must be one of: failed, needs_human, verified"
            )
        _non_empty(self.summary, "verdict.summary")
        if len(self.summary.encode("utf-8")) > _MAX_RESULT_TEXT:
            raise OrchestrationError("verdict.summary is too large")
        if not self.checks:
            raise OrchestrationError("verdict.checks must contain at least one check")
        for index, check in enumerate(self.checks):
            if isinstance(check, str):
                _non_empty(check, f"verdict.checks[{index}]")
                if len(check.encode("utf-8")) > _MAX_RESULT_TEXT:
                    raise OrchestrationError(f"verdict.checks[{index}] is too large")
                continue
            if not isinstance(check, dict):
                raise OrchestrationError(
                    f"verdict.checks[{index}] must be text or an object"
                )
            keys = set(check)
            if keys not in ({"name", "status"}, {"name", "status", "details"}):
                raise OrchestrationError(
                    f"verdict.checks[{index}] has unsupported fields"
                )
            for field_name in ("name", "status", "details"):
                if field_name not in check:
                    continue
                value = check[field_name]
                if not isinstance(value, str) or not value.strip():
                    raise OrchestrationError(
                        f"verdict.checks[{index}].{field_name} must be text"
                    )
                if len(value.encode("utf-8")) > _MAX_RESULT_TEXT:
                    raise OrchestrationError(
                        f"verdict.checks[{index}].{field_name} is too large"
                    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "execution_id": self.execution_id,
            "verdict": self.verdict,
            "summary": self.summary,
            "checks": list(self.checks),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2) + "\n"

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VerificationVerdict":
        _require_exact_keys(
            payload,
            {"schema", "execution_id", "verdict", "summary", "checks"},
            "verdict",
        )
        checks = payload["checks"]
        if not isinstance(checks, list):
            raise OrchestrationError("verdict.checks must be a JSON array")
        normalized: list[str | dict[str, str]] = []
        for check in checks:
            if isinstance(check, str):
                normalized.append(check)
            elif isinstance(check, Mapping):
                normalized.append(dict(check))
            else:
                # Let the constructor produce the stable field-specific
                # message while keeping non-object values out of the mapping.
                normalized.append(check)  # type: ignore[arg-type]
        return cls(
            schema=_string(payload["schema"], "verdict.schema"),
            execution_id=_string(payload["execution_id"], "verdict.execution_id"),
            verdict=_string(payload["verdict"], "verdict.verdict"),
            summary=_string(payload["summary"], "verdict.summary"),
            checks=tuple(normalized),
        )

    @classmethod
    def from_json(cls, raw: str) -> "VerificationVerdict":
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise OrchestrationError("verdict is not valid JSON") from error
        if not isinstance(payload, dict):
            raise OrchestrationError("verdict root must be a JSON object")
        return cls.from_dict(payload)


def validate_execution_id(execution_id: str) -> str:
    """Validate an execution id before it can become a filesystem component."""

    if not isinstance(execution_id, str) or not _EXECUTION_ID_RE.fullmatch(execution_id):
        raise OrchestrationError(
            "execution_id must contain only letters, digits, '_' or '-' and be at most 128 characters"
        )
    return execution_id


def new_execution_id() -> str:
    """Return a collision-resistant, filesystem-safe execution id."""

    return f"aes-{uuid.uuid4().hex}"


def parse_plan(
    payload: ControllerPlan | str | Mapping[str, Any],
    *,
    execution_id: str | None = None,
    contract_paths: Sequence[str] | None = None,
) -> ControllerPlan:
    """Parse and validate a controller plan from JSON text or a mapping.

    ``execution_id`` is an optional caller-owned binding.  When supplied it is
    validated before parsing and the plan must name that exact execution.  A
    syntactically valid plan for a different ledger is therefore rejected.
    """

    if execution_id is not None:
        validate_execution_id(execution_id)
    if isinstance(payload, ControllerPlan):
        parsed = payload
    elif isinstance(payload, str):
        parsed = ControllerPlan.from_json(payload)
    else:
        if not isinstance(payload, Mapping):
            raise OrchestrationError("plan must be JSON text or an object")
        parsed = ControllerPlan.from_dict(payload)
    if execution_id is not None and parsed.execution_id != execution_id:
        raise OrchestrationError(
            "plan execution_id does not match the caller execution_id"
        )
    if contract_paths is not None:
        validate_plan_paths(parsed, contract_paths)
    return parsed


def validate_plan_paths(
    plan: ControllerPlan,
    contract_paths: Sequence[str],
) -> tuple[str, ...]:
    """Bind every planned path to the trusted issue-contract scope.

    The plan schema deliberately stores only subtask paths.  The contract is
    supplied by the caller from the trusted brief, so this binding is checked
    both when a controller emits a plan and when a plan is resumed.
    """

    normalized = normalize_contract_paths(contract_paths)

    for subtask in plan.subtasks:
        for path in subtask.paths:
            if not any(path == scope or path.startswith(scope + "/") for scope in normalized):
                raise OrchestrationError(
                    f"subtask {subtask.id!r} path {path!r} is outside contract.paths"
                )
    return normalized


def normalize_contract_paths(contract_paths: Sequence[str]) -> tuple[str, ...]:
    """Validate and canonicalize trusted contract path prefixes."""

    if not isinstance(contract_paths, Sequence) or isinstance(
        contract_paths, (str, bytes, bytearray)
    ):
        raise OrchestrationError("contract paths must be a sequence")
    normalized: list[str] = []
    for index, raw_path in enumerate(contract_paths):
        if not isinstance(raw_path, str):
            raise OrchestrationError(f"contract.paths[{index}] must be text")
        path = raw_path.strip()
        if path.endswith("/"):
            path = path.rstrip("/")
        try:
            _validate_relative_path(path, f"contract.paths[{index}]")
        except OrchestrationError:
            raise
        if path not in normalized:
            normalized.append(path)
    if not normalized:
        raise OrchestrationError("contract.paths must contain at least one path")
    return tuple(normalized)


def parse_verdict(
    payload: VerificationVerdict | str | Mapping[str, Any],
    *,
    execution_id: str | None = None,
) -> VerificationVerdict:
    """Parse and validate the final controller verdict.

    The caller-owned execution binding mirrors :func:`parse_plan`; a verdict
    from another execution can therefore never close this ledger.
    """

    if execution_id is not None:
        validate_execution_id(execution_id)
    if isinstance(payload, VerificationVerdict):
        parsed = payload
    elif isinstance(payload, str):
        parsed = VerificationVerdict.from_json(payload)
    else:
        if not isinstance(payload, Mapping):
            raise OrchestrationError("verdict must be JSON text or an object")
        parsed = VerificationVerdict.from_dict(payload)
    if execution_id is not None and parsed.execution_id != execution_id:
        raise OrchestrationError(
            "verdict execution_id does not match the caller execution_id"
        )
    return parsed


@dataclass
class ArtifactLedger:
    """Atomic local artifact store rooted outside the writable worktree.

    The root is a trusted control-plane directory.  It is intentionally not
    called ``repo_root``: callers must not accidentally point the ledger at a
    model-controlled checkout.
    """

    artifact_root: Path
    execution_id: str
    _state_cache: dict[str, Any] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        raw_root = Path(self.artifact_root)
        _reject_symlink_chain(raw_root, "artifact_root")
        try:
            self.artifact_root = raw_root.resolve(strict=True)
        except OSError as error:
            raise ArtifactError(f"artifact_root is not accessible: {raw_root}") from error
        if not self.artifact_root.is_dir():
            raise ArtifactError(f"artifact_root is not a directory: {self.artifact_root}")
        validate_execution_id(self.execution_id)
        self._validate_artifact_layout()

    @property
    def repo_root(self) -> Path:
        """Compatibility alias for older callers; new code uses artifact_root."""

        return self.artifact_root

    @property
    def directory(self) -> Path:
        return self.artifact_root / ".agent" / "orchestration" / self.execution_id

    @property
    def state_path(self) -> Path:
        return self.directory / "state.json"

    @property
    def plan_path(self) -> Path:
        return self.directory / "plan.json"

    @property
    def verdict_path(self) -> Path:
        return self.directory / "verdict.json"

    @property
    def results_directory(self) -> Path:
        return self.directory / "results"

    @property
    def logs_directory(self) -> Path:
        return self.directory / "logs"

    @property
    def lock_path(self) -> Path:
        return self.directory / ".ledger.lock"

    def _validate_artifact_layout(self) -> None:
        """Reject symlinked artifact ancestors before any filesystem write."""

        agent = self.artifact_root / ".agent"
        orchestration = agent / "orchestration"
        # `.agent/repo` is a common harness worktree link.  It must never be
        # allowed to redirect an artifact operation outside this repository.
        repo_link = agent / "repo"
        for path in (agent, repo_link, orchestration, self.directory):
            if path.is_symlink():
                raise ArtifactError(f"artifact path must not be a symlink: {path}")
            if path.exists() and not path.is_dir() and path in (agent, orchestration, self.directory):
                raise ArtifactError(f"artifact path is not a directory: {path}")
        for path in (
            self.results_directory,
            self.logs_directory,
            self.state_path,
            self.plan_path,
            self.verdict_path,
            self.lock_path,
        ):
            if path.is_symlink():
                raise ArtifactError(f"artifact path must not be a symlink: {path}")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialize state/result writes across threads and processes."""

        self._validate_artifact_layout()
        if not self.directory.is_dir():
            raise ArtifactError(f"missing execution artifact directory: {self.directory}")
        try:
            handle = self.lock_path.open("a+", encoding="utf-8")
        except OSError as error:
            raise ArtifactError(f"cannot open artifact lock: {error}") from error
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        except OSError as error:
            raise ArtifactError(f"artifact lock failed: {error}") from error
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def initialize(self, *, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Create the pending ledger, refusing to overwrite another run."""

        self._validate_artifact_layout()
        try:
            self.directory.mkdir(parents=True, exist_ok=False)
        except FileExistsError as error:
            raise ArtifactError(f"execution already initialized: {self.execution_id}") from error
        self.results_directory.mkdir(exist_ok=True)
        self.logs_directory.mkdir(exist_ok=True)
        state: dict[str, Any] = {
            "schema": STATE_SCHEMA,
            "execution_id": self.execution_id,
            "state": "pending",
            "updated_at": _timestamp(),
            "history": [],
        }
        if metadata is not None:
            if not isinstance(metadata, Mapping):
                raise ArtifactError("ledger metadata must be an object")
            state["metadata"] = dict(metadata)
        with self._locked():
            self._atomic_write_json(self.state_path, state, create_only=True)
        self._state_cache = state
        return dict(state)

    def read_state(self) -> dict[str, Any]:
        with self._locked():
            return self._read_state_unlocked()

    def read_plan(self) -> ControllerPlan:
        """Read the immutable plan for a resumable execution."""

        with self._locked():
            return self._read_plan_unlocked()

    def read_results(self) -> dict[str, dict[str, Any]]:
        """Read strict result artifacts keyed by subtask id.

        Missing files are intentionally omitted: a resumed execution can
        continue with exactly the unfinished DAG nodes.
        """

        with self._locked():
            if not self.results_directory.is_dir():
                raise ArtifactError(f"missing results directory: {self.results_directory}")
            plan = self._read_plan_unlocked()
            declared_ids = {subtask.id for subtask in plan.subtasks}
            results: dict[str, dict[str, Any]] = {}
            for path in sorted(self.results_directory.iterdir()):
                if path.is_symlink() or not path.is_file():
                    raise ArtifactError(f"invalid result artifact: {path}")
                if path.suffix != ".json":
                    raise ArtifactError(f"unexpected result artifact: {path}")
                filename_id = path.stem
                try:
                    _validate_subtask_id(filename_id)
                except OrchestrationError as error:
                    raise ArtifactError(f"invalid result artifact filename: {path}") from error
                if filename_id not in declared_ids:
                    raise ArtifactError(
                        f"result artifact is not declared in plan.json: {path.name}"
                    )
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as error:
                    raise ArtifactError(f"cannot read result artifact: {error}") from error
                if not isinstance(payload, dict):
                    raise ArtifactError(f"result artifact is not an object: {path}")
                if set(payload) != _RESULT_ARTIFACT_KEYS:
                    raise ArtifactError(f"result artifact has unsupported fields: {path}")
                if (
                    payload.get("schema") != RESULT_SCHEMA
                    or payload.get("execution_id") != self.execution_id
                ):
                    raise ArtifactError(f"result artifact has an invalid schema: {path}")
                recorded_at = payload.get("recorded_at")
                if not isinstance(recorded_at, str) or not recorded_at.strip():
                    raise ArtifactError(f"result artifact has invalid fields: {path}")
                subtask_id = payload.get("subtask_id")
                result = payload.get("result")
                if not isinstance(subtask_id, str) or not isinstance(result, Mapping):
                    raise ArtifactError(f"result artifact has invalid fields: {path}")
                _validate_subtask_id(subtask_id)
                if subtask_id != filename_id:
                    raise ArtifactError(
                        f"result artifact filename does not match subtask_id: {path.name}"
                    )
                if subtask_id not in declared_ids:
                    raise ArtifactError(
                        f"result artifact is not declared in plan.json: {subtask_id}"
                    )
                if subtask_id in results:
                    raise ArtifactError(f"duplicate result artifact for subtask: {subtask_id}")
                results[subtask_id] = _validate_result(result)
            return results

    def read_verdict(self) -> VerificationVerdict:
        """Read the final controller verdict from a terminal/resumable run."""

        with self._locked():
            return self._read_verdict_unlocked()

    def _read_verdict_unlocked(self) -> VerificationVerdict:
        if not self.verdict_path.is_file() or self.verdict_path.is_symlink():
            raise ArtifactError(f"missing verdict artifact: {self.verdict_path}")
        try:
            raw = self.verdict_path.read_text(encoding="utf-8")
        except OSError as error:
            raise ArtifactError(f"cannot read verdict artifact: {error}") from error
        try:
            return parse_verdict(raw, execution_id=self.execution_id)
        except OrchestrationError as error:
            raise ArtifactError(f"invalid verdict artifact: {error}") from error

    def _read_state_unlocked(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            raise ArtifactError(f"missing state artifact: {self.state_path}")
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ArtifactError(f"cannot read state artifact: {error}") from error
        if not isinstance(payload, dict) or payload.get("schema") != STATE_SCHEMA:
            raise ArtifactError("state artifact has an invalid schema")
        if payload.get("execution_id") != self.execution_id:
            raise ArtifactError("state artifact execution_id does not match ledger")
        if payload.get("state") not in ORCHESTRATION_STATES:
            raise ArtifactError("state artifact has an invalid state")
        self._state_cache = payload
        return payload

    def transition(self, state: str, *, event: str | None = None) -> dict[str, Any]:
        """Atomically append a state transition and return the new state."""

        with self._locked():
            return self._transition_unlocked(state, event=event)

    def _transition_unlocked(self, state: str, *, event: str | None = None) -> dict[str, Any]:
        """Transition while the per-execution lock is already held."""

        if state not in ORCHESTRATION_STATES:
            raise ArtifactError(f"unknown orchestration state: {state}")
        if event is not None and (
            not isinstance(event, str) or not event.strip() or len(event) > 256
        ):
            raise ArtifactError("transition event must be a non-empty short string")
        current = self._read_state_unlocked()
        previous = current["state"]
        if state != previous and state not in _ALLOWED_TRANSITIONS[previous]:
            raise ArtifactError(f"invalid state transition: {previous} -> {state}")
        history = list(current.get("history", []))
        if state != previous or event:
            history.append(
                {
                    "from": previous,
                    "to": state,
                    "event": event,
                    "at": _timestamp(),
                }
            )
        updated = dict(current)
        updated["state"] = state
        updated["updated_at"] = _timestamp()
        updated["history"] = history
        self._atomic_write_json(self.state_path, updated)
        self._state_cache = updated
        return updated

    def write_plan(self, plan: ControllerPlan | Mapping[str, Any] | str) -> Path:
        """Validate and atomically store ``plan.json`` for this execution."""

        parsed = parse_plan(plan, execution_id=self.execution_id)
        with self._locked():
            if self.plan_path.exists():
                existing = self._read_plan_unlocked()
                if existing.to_json() != parsed.to_json():
                    raise ArtifactError(f"plan artifact already exists: {self.plan_path}")
                current = self._read_state_unlocked()
                if current["state"] == "pending":
                    self._transition_unlocked("planned", event="reconciled_existing_plan")
                return self.plan_path
            current = self._read_state_unlocked()
            if current["state"] not in {"pending", "planned", "waiting_provider"}:
                raise ArtifactError(
                    "plan can only be written while execution is pending/planned/waiting_provider"
                )
            self._atomic_write_text(self.plan_path, parsed.to_json(), create_only=True)
            if current["state"] == "pending":
                self._transition_unlocked("planned", event="controller_plan_written")
            elif current["state"] == "waiting_provider":
                self._transition_unlocked("planned", event="controller_plan_written_after_wait")
        return self.plan_path

    def write_verdict(self, verdict: VerificationVerdict | Mapping[str, Any] | str) -> Path:
        """Atomically persist one strict final-controller verdict."""

        parsed = parse_verdict(verdict, execution_id=self.execution_id)
        with self._locked():
            current = self._read_state_unlocked()
            if self.verdict_path.exists() or self.verdict_path.is_symlink():
                if self.verdict_path.is_symlink():
                    raise ArtifactError(f"verdict artifact must not be a symlink: {self.verdict_path}")
                existing = self._read_verdict_unlocked()
                if existing.to_json() != parsed.to_json():
                    raise ArtifactError(f"verdict artifact already exists: {self.verdict_path}")
                return self.verdict_path
            if current["state"] not in {"running", "planned", "waiting_provider"}:
                raise ArtifactError("verdict can only be written while execution is running")
            self._atomic_write_text(self.verdict_path, parsed.to_json(), create_only=True)
        return self.verdict_path

    def write_result(self, subtask_id: str, result: Mapping[str, Any]) -> Path:
        """Atomically store one strict result for a declared plan subtask.

        Provider transcripts, stdout/stderr and arbitrary nested payloads are
        deliberately not accepted here.  The worker must reduce its outcome
        to the small audit record below before it reaches the ledger.
        """

        _validate_subtask_id(subtask_id)
        clean_result = _validate_result(result)
        with self._locked():
            current = self._read_state_unlocked()
            if current["state"] not in {"planned", "running"}:
                raise ArtifactError("results can only be written while execution is planned/running")
            plan = self._read_plan_unlocked()
            declared_ids = {subtask.id for subtask in plan.subtasks}
            if subtask_id not in declared_ids:
                raise ArtifactError(f"subtask {subtask_id!r} is not declared in plan.json")
            self.results_directory.mkdir(parents=True, exist_ok=True)
            path = self.results_directory / f"{subtask_id}.json"
            if path.exists() or path.is_symlink():
                raise ArtifactError(f"result artifact already exists: {path}")
            payload = {
                "schema": RESULT_SCHEMA,
                "execution_id": self.execution_id,
                "subtask_id": subtask_id,
                "recorded_at": _timestamp(),
                "result": clean_result,
            }
            self._atomic_write_json(path, payload, create_only=True)
            return path

    def write_log(self, name: str, content: str) -> Path:
        """Atomically create one bounded, minimally redacted text log."""

        if not isinstance(name, str) or not _FILE_NAME_RE.fullmatch(name):
            raise ArtifactError("log name is not a safe file name")
        if not isinstance(content, str):
            raise ArtifactError("log content must be text")
        redacted = _redact_text(content)
        if len(redacted.encode("utf-8")) > MAX_LOG_BYTES:
            raise ArtifactError(f"log content exceeds {MAX_LOG_BYTES} bytes")
        with self._locked():
            self.logs_directory.mkdir(parents=True, exist_ok=True)
            path = self.logs_directory / name
            if path.exists() or path.is_symlink():
                raise ArtifactError(f"log artifact already exists: {path}")
            self._atomic_write_text(path, redacted, create_only=True)
            return path

    def _read_plan_unlocked(self) -> ControllerPlan:
        if not self.plan_path.is_file():
            raise ArtifactError(f"missing plan artifact: {self.plan_path}")
        try:
            raw = self.plan_path.read_text(encoding="utf-8")
        except OSError as error:
            raise ArtifactError(f"cannot read plan artifact: {error}") from error
        try:
            return parse_plan(raw, execution_id=self.execution_id)
        except OrchestrationError as error:
            raise ArtifactError(f"invalid plan artifact: {error}") from error

    @staticmethod
    def _atomic_write_json(path: Path, payload: Mapping[str, Any], *, create_only: bool = False) -> None:
        ArtifactLedger._atomic_write_text(
            path,
            json.dumps(payload, sort_keys=True, indent=2) + "\n",
            create_only=create_only,
        )

    @staticmethod
    def _atomic_write_text(path: Path, content: str, *, create_only: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if create_only:
                # A hard link gives create-only semantics even when two
                # controllers race between their existence checks.  The
                # temporary file is in the same directory, so the link is
                # atomic on the filesystems supported by the runner.
                try:
                    os.link(temporary_path, path)
                except FileExistsError as error:
                    raise ArtifactError(f"artifact already exists: {path}") from error
                temporary_path.unlink(missing_ok=True)
            else:
                os.replace(temporary_path, path)
        except OSError as error:
            raise ArtifactError(f"cannot write artifact {path}: {error}") from error
        finally:
            temporary_path.unlink(missing_ok=True)


_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"planned", "waiting_provider", "needs_human", "failed"}),
    "planned": frozenset({"running", "waiting_provider", "needs_human", "failed"}),
    "running": frozenset({"waiting_provider", "needs_human", "verified", "failed"}),
    "waiting_provider": frozenset(
        {"planned", "running", "needs_human", "verified", "failed"}
    ),
    "needs_human": frozenset({"planned", "running", "failed"}),
    "verified": frozenset(),
    "failed": frozenset(),
}


def _assert_acyclic(subtasks: tuple[Subtask, ...]) -> None:
    graph = {subtask.id: set(subtask.dependencies) for subtask in subtasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise OrchestrationError("controller plan dependencies contain a cycle")
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph[node]:
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)


def _validate_subtask_id(value: str) -> None:
    if not isinstance(value, str) or not _SUBTASK_ID_RE.fullmatch(value):
        raise OrchestrationError(
            "subtask id must contain only letters, digits, '.', '_' or '-' and be at most 64 characters"
        )


def _validate_relative_path(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise OrchestrationError(f"{field_name} must be a non-empty relative path")
    if (
        "\\" in value
        or "\x00" in value
        or any(character in value for character in "*?[]{}")
        or ":" in value
        or value.startswith(("/", "~"))
    ):
        raise OrchestrationError(f"{field_name} is not a safe relative path")
    try:
        path = PurePosixPath(value)
    except (TypeError, ValueError) as error:
        raise OrchestrationError(f"{field_name} is not a valid relative path") from error
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise OrchestrationError(f"{field_name} is not a safe relative path")


def _validate_result(result: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        raise ArtifactError("subtask result must be a JSON object")
    actual_keys = set(result)
    unknown = actual_keys - _ALLOWED_RESULT_KEYS
    if unknown:
        raise ArtifactError(
            "subtask result contains unsupported fields: " + ", ".join(sorted(unknown))
        )
    status = result.get("status")
    if not isinstance(status, str) or status not in _RESULT_STATUSES:
        raise ArtifactError(
            "subtask result.status must be one of: " + ", ".join(sorted(_RESULT_STATUSES))
        )

    clean: dict[str, Any] = {"status": status}
    for field_name in ("summary", "notes", "error"):
        if field_name not in result:
            continue
        value = result[field_name]
        if not isinstance(value, str):
            raise ArtifactError(f"subtask result.{field_name} must be text")
        if len(value.encode("utf-8")) > _MAX_RESULT_TEXT:
            raise ArtifactError(f"subtask result.{field_name} is too large")
        clean[field_name] = _redact_text(value)

    for field_name in ("changed", "tests"):
        if field_name not in result:
            continue
        value = result[field_name]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ArtifactError(f"subtask result.{field_name} must be an array of strings")
        if len(value) > 256:
            raise ArtifactError(f"subtask result.{field_name} has too many entries")
        if field_name == "changed":
            for item in value:
                try:
                    _validate_relative_path(item, f"subtask result.{field_name} entry")
                except OrchestrationError as error:
                    raise ArtifactError(str(error)) from error
        if any(len(item.encode("utf-8")) > _MAX_RESULT_TEXT for item in value):
            raise ArtifactError(f"subtask result.{field_name} contains an oversized entry")
        clean[field_name] = [_redact_text(item) for item in value]

    for field_name in ("provider", "model", "effort", "classification"):
        if field_name not in result:
            continue
        value = result[field_name]
        if not isinstance(value, str) or not value.strip():
            raise ArtifactError(f"subtask result.{field_name} must be non-empty text")
        if len(value.encode("utf-8")) > 512:
            raise ArtifactError(f"subtask result.{field_name} is too large")
        clean[field_name] = _redact_text(value.strip())

    if "attempts" in result:
        value = result["attempts"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ArtifactError("subtask result.attempts must be a non-negative integer")
        clean["attempts"] = value
    if "fallback_used" in result:
        value = result["fallback_used"]
        if not isinstance(value, bool):
            raise ArtifactError("subtask result.fallback_used must be a boolean")
        clean["fallback_used"] = value
    return clean


def _redact_text(content: str) -> str:
    redacted = content
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _require_exact_keys(payload: Any, expected: set[str], field_name: str) -> None:
    if not isinstance(payload, Mapping):
        raise OrchestrationError(f"{field_name} must be a JSON object")
    actual = set(payload)
    missing = expected - actual
    unknown = actual - expected
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing {', '.join(sorted(missing))}")
        if unknown:
            details.append(f"unknown {', '.join(sorted(unknown))}")
        raise OrchestrationError(f"{field_name} keys invalid ({'; '.join(details)})")


def _string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrchestrationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise OrchestrationError(f"{field_name} must be a non-empty string")


def _string_list(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise OrchestrationError(f"{field_name} must be an array of strings")
    return tuple(item.strip() for item in value)


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
