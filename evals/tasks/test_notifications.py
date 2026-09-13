from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys


TASKS_DIR = Path(__file__).resolve().parents[2] / "harness" / "scripts" / "tasks"
sys.path.insert(0, str(TASKS_DIR))

import tasks  # noqa: E402


def test_notification_dedupe_requires_exact_marker_and_trusted_actor(monkeypatch) -> None:
    monkeypatch.setenv("AES_TRUSTED_ACTOR", "aes-queue-bot")
    key = tasks._notification_key("owner/repo", 42, "aes:needs-human", "exec-42")
    comments = [
        SimpleNamespace(
            author="other-bot",
            body=f"{tasks._notification_marker(key)}",
        ),
        SimpleNamespace(
            author="aes-queue-bot",
            body=f"not a marker: {key}",
        ),
    ]
    posted: list[str] = []
    monkeypatch.setattr(tasks.provider_github, "list_issue_comments", lambda *args: tuple(comments))
    monkeypatch.setattr(tasks.provider_github, "comment", lambda repo, number, body: posted.append(body))

    assert tasks.notify_event(
        "owner/repo",
        42,
        "aes:needs-human",
        "exec-42",
        {"reason": "raw model verdict with token=secret", "question": "review"},
    )
    assert posted
    assert "raw model verdict" not in posted[0]
    assert "token=secret" not in posted[0]
    assert "execution_id: exec-42" in posted[0]


def test_notification_dedupe_suppresses_only_trusted_exact_marker(monkeypatch) -> None:
    monkeypatch.setenv("AES_TRUSTED_ACTOR", "aes-queue-bot")
    key = tasks._notification_key("owner/repo", 42, "aes:pr-open", "exec-42")
    marker = tasks._notification_marker(key)
    monkeypatch.setattr(
        tasks.provider_github,
        "list_issue_comments",
        lambda *args: (SimpleNamespace(author="aes-queue-bot", body=f"prefix\n{marker}\nsuffix"),),
    )
    assert tasks.notify_event("owner/repo", 42, "aes:pr-open", "exec-42", {}) is False
