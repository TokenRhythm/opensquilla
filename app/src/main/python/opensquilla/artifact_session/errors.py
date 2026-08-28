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


class WriterLeaseConflictError(ArtifactConflictError):
    """Raised when another writer owns the live document lease."""


class WriterLeaseExpiredError(ArtifactConflictError):
    """Raised when a write presents a stale or expired fencing token."""
