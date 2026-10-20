"""V051 - terminal evidence for chat.send.v2, independent of session deletion."""

from __future__ import annotations

from yoyo import step

from opensquilla.persistence.turn_request_resolution import TURN_REQUEST_RESOLUTION_SCHEMA

__depends__: set[str] = {"V050__usage_task_attribution"}


def rollback_step(conn) -> None:
    # Never erase a fence while a delayed versioned send can still arrive.
    pass


steps = [step(TURN_REQUEST_RESOLUTION_SCHEMA, rollback_step)]
