from pathlib import Path
import sys

import pytest


TASKS_DIR = Path(__file__).resolve().parents[2] / "harness" / "scripts" / "tasks"
sys.path.insert(0, str(TASKS_DIR))

import contract  # noqa: E402


VALID_BLOCK = """<!-- aes:contract -->
repo: example-consumer
paths: src/publisher/, tests/publisher/
constraints: Do not change the Post schema.
depends_on: 38, example-owner/example-source#12
done: tests pass; pull request opened with review label
<!-- /aes:contract -->"""


def test_valid_contract_is_parsed_and_round_trips() -> None:
    body = f"Testo introduttivo.\n\n{VALID_BLOCK}\n\nNote finali."

    parsed = contract.parse(body)

    assert parsed == contract.Contract(
        repo="example-consumer",
        paths=("src/publisher/", "tests/publisher/"),
        constraints="Do not change the Post schema.",
        depends_on=("38", "example-owner/example-source#12"),
        done=("tests pass; pull request opened with review label",),
    )
    assert contract.serialize(parsed) == VALID_BLOCK


def test_opening_marker_without_closing_marker_is_rejected() -> None:
    body = "<!-- aes:contract -->\nrepo: example-consumer"

    with pytest.raises(contract.ContractError, match="closing marker"):
        contract.parse(body)


def test_unknown_key_is_rejected_and_named() -> None:
    body = """<!-- aes:contract -->
repo: example-consumer
priorita: alta
<!-- /aes:contract -->"""

    with pytest.raises(contract.ContractError, match="priorita"):
        contract.parse(body)


def test_body_without_contract_is_rejected() -> None:
    with pytest.raises(contract.ContractError, match="contract block"):
        contract.parse("Una issue senza alcun contratto.")
