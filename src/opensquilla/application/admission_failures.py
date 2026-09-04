"""Persistence failures that durable admission can reconcile by commit phase."""

from __future__ import annotations


class AdmissionStorageBusyError(RuntimeError):
    def __init__(
        self,
        operation: str,
        *,
        waited_ms: int,
        retry_after_ms: int,
        stage: str | None = None,
        resource: str | None = None,
    ) -> None:
        super().__init__("Session storage is temporarily busy")
        self.operation = operation
        self.waited_ms = waited_ms
        self.retry_after_ms = retry_after_ms
        self.stage = stage
        self.resource = resource


class AdmissionStaleEpochError(Exception):
    """The accepted session generation changed before the write."""


class AdmissionIngressConflictError(ValueError):
    """A request identity already belongs to a different input."""


class AdmissionPendingInputConflictError(ValueError):
    """The pending input identity or revision changed."""


class AdmissionMetaControlConflictError(ValueError):
    """A durable control identity belongs to a different operation."""


class AdmissionTaskCollectionUnavailableError(RuntimeError):
    """The queued task stopped accepting collected inputs."""


class AdmissionPlanSessionBusyError(RuntimeError):
    def __init__(self, *, task_id: str, task_status: str) -> None:
        super().__init__("current-session plan implementation requires an idle session")
        self.task_id = task_id
        self.task_status = task_status


class AdmissionPlanConflictError(RuntimeError):
    """A plan revision or its execution overlay changed before acceptance."""


class AdmissionAnnotationConflictError(RuntimeError):
    """The bound annotation revision changed before acceptance."""


class AdmissionAnnotationNotFoundError(RuntimeError):
    """The bound annotation disappeared before acceptance."""


class AdmissionAnnotationValidationError(ValueError):
    """The annotation cannot be accepted in its current state."""
