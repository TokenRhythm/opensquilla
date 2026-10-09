"""V041 - durable subagent orchestration runs, tasks, sessions, and activations."""

from __future__ import annotations

from yoyo import step

from opensquilla.orchestration.schema import DROP_STATEMENTS, SCHEMA_STATEMENTS

__depends__: set[str] = {"V040__document_resources"}


def apply_step(conn) -> None:
    cursor = conn.cursor()
    for statement in SCHEMA_STATEMENTS:
        cursor.execute(statement)


def rollback_step(conn) -> None:
    cursor = conn.cursor()
    for statement in DROP_STATEMENTS:
        cursor.execute(statement)


steps = [step(apply_step, rollback_step)]
