"""Opaque UUID generators for telemetry contracts."""

from __future__ import annotations

from uuid import RFC_4122, UUID, uuid4


def new_event_id() -> UUID:
    """Return a random identifier to persist with an event across retries."""

    return uuid4()


def new_batch_id() -> UUID:
    """Return a random upload-batch identifier."""

    return uuid4()


def new_app_session_id() -> UUID:
    """Return a random reliability-only application session identifier."""

    return uuid4()


def new_analytics_user_id() -> UUID:
    """Return a purpose-specific random growth-analysis identifier."""

    return uuid4()


def is_uuid4(value: UUID) -> bool:
    """Return whether *value* is an RFC 4122 UUID version 4 identifier."""

    return value.version == 4 and value.variant == RFC_4122


__all__ = [
    "is_uuid4",
    "new_analytics_user_id",
    "new_app_session_id",
    "new_batch_id",
    "new_event_id",
]
