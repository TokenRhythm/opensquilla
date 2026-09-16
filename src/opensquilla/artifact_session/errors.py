"""Domain errors for durable artifact editing sessions."""

from __future__ import annotations


class ArtifactSessionError(RuntimeError):
    """Base class for ArtifactSession failures."""


class ArtifactNotFoundError(ArtifactSessionError):
    """Raised when an ArtifactSession record does not exist."""


class ArtifactConflictError(ArtifactSessionError):
    """Raised when optimistic concurrency expectations are stale."""


class ArtifactValidationError(ArtifactSessionError, ValueError):
    """Raised when an ArtifactSession command is structurally invalid."""
