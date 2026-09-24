"""V047 - retire MetaSkill controls and retain offline audit history."""

from __future__ import annotations

from yoyo import step

from opensquilla.persistence.product_retirement import (
    LEGACY_TABLES_SQL,
    product_retirement_statements,
)

__depends__: set[str] = {"V046__plan_presentation", "V032__meta_launch_discard_tombstones"}


def apply_step(conn) -> None:
    tables = {row[0]: row[1] for row in conn.execute(LEGACY_TABLES_SQL)}
    for statement in product_retirement_statements(tables):
        conn.execute(statement)


def rollback_step(conn) -> None:
    # Downgrades require the migrator's pre-upgrade backup. Never resurrect
    # authorization or discard the retained execution history on rollback.
    pass


steps = [step(apply_step, rollback_step)]
