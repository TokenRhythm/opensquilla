"""V044 - retire unused LLM flush metadata, preserving deterministic checkpoints."""

from __future__ import annotations

from yoyo import step

from opensquilla.persistence.memory_flush_retirement import retire_memory_flush_metadata

# The helper owns an IMMEDIATE transaction so concurrent opens cannot plan
# against a stale schema under a deferred read transaction.
__transactional__ = False

__depends__: set[str] = {"V043__session_execution_workspace"}


def apply_step(conn) -> None:
    retire_memory_flush_metadata(conn)


def rollback_step(conn) -> None:
    # Forward-only retirement. The migrator retains a pre-upgrade DB backup;
    # recreating empty repair metadata would not restore its original meaning.
    pass


steps = [step(apply_step, rollback_step)]
