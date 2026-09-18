"""Durable presentation state, separate from immutable plans and their execution."""

from __future__ import annotations

PLAN_PRESENTATION_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS plan_presentations (
        session_key TEXT NOT NULL,
        session_id TEXT NOT NULL,
        session_epoch INTEGER NOT NULL CHECK (session_epoch >= 0),
        revision_id TEXT NOT NULL,
        dismissed INTEGER NOT NULL CHECK (dismissed IN (0, 1)),
        state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (session_id, session_epoch, revision_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plan_presentation_receipts (
        session_key TEXT NOT NULL,
        session_id TEXT NOT NULL,
        session_epoch INTEGER NOT NULL CHECK (session_epoch >= 0),
        client_request_id TEXT NOT NULL,
        request_json TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        PRIMARY KEY (session_id, session_epoch, client_request_id)
    )
    """,
)


class PlanPresentationConflictError(ValueError):
    """The visible presentation changed since the client read it."""


class PlanPresentationRequestConflictError(ValueError):
    """A request id was reused for a different presentation mutation."""
