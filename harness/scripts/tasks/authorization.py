"""Centralized author authorization for autonomous issue execution."""

from __future__ import annotations

import os


def author_allowed(author: str, configured: str | None = None) -> bool:
    """Return true only for an explicitly configured exact GitHub login."""

    values = configured if configured is not None else os.environ.get("AES_QUEUE_ALLOWED_AUTHORS", "")
    allowed = {item.strip() for item in values.split(",") if item.strip()}
    return bool(author.strip()) and author.strip() in allowed


def author_gate_reason(author: str, configured: str | None = None) -> str | None:
    """Return a diagnostic reason when an issue author is not authorized."""

    values = configured if configured is not None else os.environ.get("AES_QUEUE_ALLOWED_AUTHORS", "")
    if not values.strip():
        return "author allowlist is missing"
    if not author.strip():
        return "issue author is missing"
    if not author_allowed(author, values):
        return f"issue author {author!r} is not allowed"
    return None


def autonomous_gate_enabled() -> bool:
    """Whether compatibility CLI paths must apply the autonomous author gate."""

    return os.environ.get("AES_QUEUE_ENABLED", "").strip().lower() == "true"
