"""V046 - preserve per-revision plan visibility and idempotent mutation receipts."""

from __future__ import annotations

from yoyo import step

from opensquilla.persistence.plan_presentation import PLAN_PRESENTATION_SCHEMA

__depends__: set[str] = {"V045__goal_runtime_accounting"}


def apply_step(conn) -> None:
    for statement in PLAN_PRESENTATION_SCHEMA:
        conn.execute(statement)


def rollback_step(conn) -> None:
    conn.execute("DROP TABLE IF EXISTS plan_presentation_receipts")
    conn.execute("DROP TABLE IF EXISTS plan_presentations")


steps = [step(apply_step, rollback_step)]
