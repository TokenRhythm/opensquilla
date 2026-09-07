"""Native failures keep their phase and original cause at the admission seam."""

from __future__ import annotations

import asyncio

import pytest

from opensquilla.application import admission_failures as domain
from opensquilla.artifact_session import (
    ArtifactConflictError,
    ArtifactNotFoundError,
    ArtifactValidationError,
)
from opensquilla.gateway.adapters.turn_admission import map_admission_error
from opensquilla.gateway.admission_failures import admission_failure, translate_admission_failure
from opensquilla.gateway.rpc import RpcHandlerError
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


@pytest.mark.parametrize(
    ("native", "projected_type"),
    [
        (StaleEpochError("epoch"), domain.AdmissionStaleEpochError),
        (TurnIngressConflictError("identity"), domain.AdmissionIngressConflictError),
        (PendingChatInputConflictError("revision"), domain.AdmissionPendingInputConflictError),
        (MetaControlIntentConflictError("control"), domain.AdmissionMetaControlConflictError),
        (TaskCollectionUnavailableError("started"), domain.AdmissionTaskCollectionUnavailableError),
        (PlanConflictError("plan"), domain.AdmissionPlanConflictError),
        (PlanRunConflictError("run"), domain.AdmissionPlanConflictError),
        (ArtifactConflictError("revision"), domain.AdmissionAnnotationConflictError),
        (ArtifactNotFoundError("annotation"), domain.AdmissionAnnotationNotFoundError),
        (ArtifactValidationError("invalid"), domain.AdmissionAnnotationValidationError),
    ],
)
async def test_native_failure_is_projected_before_application_compensation(native, projected_type):
    order = []
    with pytest.raises(projected_type) as caught:
        try:
            with translate_admission_failure():
                order.append("primitive")
                await asyncio.sleep(0)
                raise native
        except projected_type:
            order.append("compensate")
            raise
    assert order == ["primitive", "compensate"]
    assert str(caught.value) == str(native)
    assert caught.value.__cause__ is native


def test_busy_retry_facts_and_plan_owner_survive_projection():
    busy = admission_failure(StorageBusyError("accept", waited_ms=17, retry_after_ms=31))
    assert isinstance(busy, domain.AdmissionStorageBusyError)
    assert (busy.operation, busy.waited_ms, busy.retry_after_ms) == ("accept", 17, 31)
    plan = admission_failure(
        PlanImplementationSessionBusyError(task_id="synthetic-task", task_status="queued")
    )
    assert isinstance(plan, domain.AdmissionPlanSessionBusyError)
    assert (plan.task_id, plan.task_status) == ("synthetic-task", "queued")


def test_busy_outside_commit_keeps_registry_retry_and_acceptance_semantics():
    projected = admission_failure(
        StorageBusyError(
            "replay", waited_ms=17, retry_after_ms=31, stage="read", resource="receipt"
        )
    )
    assert isinstance(projected, domain.AdmissionStorageBusyError)
    mapped = map_admission_error(projected)
    assert isinstance(mapped, RpcHandlerError)
    assert mapped.code == "STORAGE_BUSY"
    assert mapped.retryable is True
    assert mapped.retry_after_ms == 31
    assert mapped.accepted is None
    assert mapped.details == {
        "operation": "replay",
        "waited_ms": 17,
        "stage": "read",
        "resource": "receipt",
    }
    assert str(mapped) == "Session storage is temporarily busy. Retry this operation."


@pytest.mark.parametrize("failure", [RuntimeError("unknown"), asyncio.CancelledError()])
def test_unknown_failures_and_cancellation_are_not_reclassified(failure):
    with pytest.raises(type(failure)) as caught, translate_admission_failure():
        raise failure
    assert caught.value is failure
