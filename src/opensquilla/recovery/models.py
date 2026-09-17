"""Data-only recovery protocol models.

These types deliberately depend only on the standard library so the recovery
CLI can branch before the ordinary runtime and dotenv bootstrap.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

RecoveryOutcome = Literal["ready", "attention", "recovery_required", "recovery_profile"]

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class WorkspaceCandidate:
    kind: str
    path: Path
    exists: bool
    valid: bool
    configured: bool = False
    identity: str | None = None
    modified_at_ns: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "path": str(self.path),
            "exists": self.exists,
            "valid": self.valid,
            "configured": self.configured,
            "identity": self.identity,
            "modified_at_ns": self.modified_at_ns,
        }


@dataclass(frozen=True)
class RecoveryReport:
    outcome: RecoveryOutcome
    stable_code: str
    primary_home: Path
    effective_workspace: Path | None
    candidates: tuple[WorkspaceCandidate, ...]
    allowed_actions: tuple[str, ...]
    transaction_id: str
    revision: int
    schema_version: int = SCHEMA_VERSION
    # Human-readable diagnosis (sanitized: the profile home is spelled
    # ``<HOME>``). Additive to schema 1; absent detail stays ``None``.
    detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the fixed JSON protocol shape expected by Desktop."""
        return {
            "schema_version": self.schema_version,
            "outcome": self.outcome,
            "stable_code": self.stable_code,
            "primary_home": str(self.primary_home),
            "effective_workspace": (
                str(self.effective_workspace) if self.effective_workspace is not None else None
            ),
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "allowed_actions": list(self.allowed_actions),
            "transaction_id": self.transaction_id,
            "revision": self.revision,
            "detail": self.detail,
        }
