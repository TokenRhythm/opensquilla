"""Persistence layer: schema migration + related primitives.

Public entry point is :func:`opensquilla.persistence.migrator.apply_pending`.
"""

from opensquilla.persistence.migrator import apply_pending

__all__ = ["apply_pending"]
