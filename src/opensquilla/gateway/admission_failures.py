"""Translate native persistence failures at the primitive that raised them."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from opensquilla.application.admission_failures import (
    AdmissionAnnotationConflictError,
    AdmissionAnnotationNotFoundError,
    AdmissionAnnotationValidationError,
    AdmissionIngressConflictError,
    AdmissionMetaControlConflictError,
    AdmissionPendingInputConflictError,
    AdmissionPlanConflictError,
    AdmissionPlanSessionBusyError,
    AdmissionStaleEpochError,
    AdmissionStorageBusyError,
    AdmissionTaskCollectionUnavailableError,
)
from opensquilla.artifact_session import (
    ArtifactConflictError,
    ArtifactNotFoundError,
    ArtifactValidationError,
)
from opensquilla.session.plans import PlanConflictError, PlanRunConflictError
from opensquilla.session.storage import (
    MetaControlIntentConflictError,
    PendingChatInputConflictError,
    PlanImplementationSessionBusyError,
    StaleEpochError,
    StorageBusyError,
    TaskCollectionUnavailableError,
    TurnIngressConflictError,
)


def admission_failure(error: Exception) -> Exception | None:
    """Preserve failure identity and retry facts without exposing native models."""
    if isinstance(error, StorageBusyError):
        return AdmissionStorageBusyError(
            error.operation,
            waited_ms=error.waited_ms,
            retry_after_ms=error.retry_after_ms,
            stage=error.stage,
            resource=error.resource,
        )
    if isinstance(error, PlanImplementationSessionBusyError):
        return AdmissionPlanSessionBusyError(
            task_id=error.task_id, task_status=error.task_status
        )
    if isinstance(error, StaleEpochError):
        return AdmissionStaleEpochError(str(error))
    if isinstance(error, TurnIngressConflictError):
        return AdmissionIngressConflictError(str(error))
    if isinstance(error, PendingChatInputConflictError):
        return AdmissionPendingInputConflictError(str(error))
    if isinstance(error, MetaControlIntentConflictError):
        return AdmissionMetaControlConflictError(str(error))
    if isinstance(error, TaskCollectionUnavailableError):
        return AdmissionTaskCollectionUnavailableError(str(error))
    if isinstance(error, (PlanConflictError, PlanRunConflictError)):
        return AdmissionPlanConflictError(str(error))
    if isinstance(error, ArtifactConflictError):
        return AdmissionAnnotationConflictError(str(error))
    if isinstance(error, ArtifactNotFoundError):
        return AdmissionAnnotationNotFoundError(str(error))
    if isinstance(error, ArtifactValidationError):
        return AdmissionAnnotationValidationError(str(error))
    return None


@contextmanager
def translate_admission_failure() -> Iterator[None]:
    """Map one native operation's failure before Application compensation runs."""
    try:
        yield
    except Exception as error:
        projected = admission_failure(error)
        if projected is None:
            raise
        raise projected from error
