"""Parse and serialize the contract contained in issues."""

from dataclasses import dataclass


OPEN_MARKER = "<!-- aes:contract -->"
CLOSE_MARKER = "<!-- /aes:contract -->"
KNOWN_KEYS = frozenset({"repo", "paths", "constraints", "depends_on", "done"})
LIST_KEYS = frozenset({"paths", "depends_on", "done"})


class ContractError(ValueError):
    """Indicate that the contract block is missing or malformed."""


@dataclass(frozen=True)
class Contract:
    """Represent the pure fields of the execution contract."""

    repo: str | None
    paths: tuple[str, ...]
    constraints: str
    depends_on: tuple[str, ...]
    done: tuple[str, ...]


def _parse_list(value: str) -> tuple[str, ...]:
    """Split a flat list, discarding empty items and surrounding whitespace."""

    return tuple(item.strip() for item in value.split(",") if item.strip())


def parse(body: str) -> Contract:
    """Extract a contract from the body or raise ``ContractError``."""

    opening = body.find(OPEN_MARKER)
    if opening == -1:
        raise ContractError("contract block not found")

    content_start = opening + len(OPEN_MARKER)
    closing = body.find(CLOSE_MARKER, content_start)
    if closing == -1:
        raise ContractError("contract closing marker not found")

    values: dict[str, str] = {}
    content = body[content_start:closing]
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if ":" not in line:
            raise ContractError(f"invalid contract line {line_number}: missing ':'")

        key, value = (part.strip() for part in line.split(":", 1))
        if key not in KNOWN_KEYS:
            raise ContractError(f"unknown contract key: {key}")
        if key in values:
            raise ContractError(f"duplicate contract key: {key}")
        values[key] = value

    return Contract(
        repo=values.get("repo") or None,
        paths=_parse_list(values.get("paths", "")),
        constraints=values.get("constraints", ""),
        depends_on=_parse_list(values.get("depends_on", "")),
        done=_parse_list(values.get("done", "")),
    )


def serialize(contract: Contract) -> str:
    """Serialize a contract in the flat format delimited by the spec."""

    values = {
        "repo": contract.repo or "",
        "paths": ", ".join(contract.paths),
        "constraints": contract.constraints,
        "depends_on": ", ".join(contract.depends_on),
        "done": ", ".join(contract.done),
    }
    lines = [OPEN_MARKER]
    lines.extend(f"{key}: {values[key]}" for key in (
        "repo",
        "paths",
        "constraints",
        "depends_on",
        "done",
    ))
    lines.append(CLOSE_MARKER)
    return "\n".join(lines)
