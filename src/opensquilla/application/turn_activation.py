"""The reserved-turn commit and activation lifecycle shared by interactive ingress."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import structlog

from opensquilla.application.admission_views import (
    ActivationTask,
    AdmissionGoalContext,
    AdmissionReceipt,
    AdmissionTaskRecord,
)
from opensquilla.application.turn_acceptance_ports import (
    AdmissionHandle,
    AdmissionReservation,
    AdmissionRuntime,
)

log = structlog.get_logger(__name__)


class ActivationStorage(Protocol):
    async def get_agent_task(self, task_id: str) -> ActivationTask | None: ...

    async def fail_queued_agent_task_activation(
        self, task_id: str, *, session_key: str, error_class: str, error_message: str
    ) -> ActivationTask | None: ...


class CommittedTurn(Protocol):
    @property
    def receipt(self) -> AdmissionReceipt: ...

    @property
    def replayed(self) -> bool: ...

    @property
    def fresh_user_session(self) -> bool: ...

    @property
    def goal_context(self) -> AdmissionGoalContext | None: ...


@dataclass(frozen=True, slots=True)
class TurnActivation[Acceptance: CommittedTurn]:
    acceptance: Acceptance
    handle: AdmissionHandle | None = None
    activation_failed: bool = False
    task_status: str | None = None


async def commit_reserved_turn[Acceptance: CommittedTurn](
    *,
    runtime: AdmissionRuntime,
    storage: ActivationStorage,
    reserve: Callable[[], Awaitable[AdmissionReservation]],
    freeze: Callable[[AdmissionReservation], Awaitable[None]],
    commit: Callable[[AdmissionTaskRecord], Awaitable[Acceptance]],
    before_activate: Callable[[Acceptance], None] | None = None,
    on_unactivated: Callable[[], None] | None = None,
    compensate_goal: Callable[[dict[str, object]], Awaitable[object | None]] | None = None,
) -> TurnActivation[Acceptance]:
    """Run inside the caller's shielded admission gate, after any atomic collect.

    Entry-specific preparation and response projection stay with the caller.
    Only this implementation decides when a reservation is committed, replayed,
    activated, or eligible for compensation.
    """
    reservation = await reserve()
    try:
        await freeze(reservation)
        acceptance = await commit(reservation.task_record)
    except BaseException:
        await runtime.abort_reservation(reservation)
        raise
    if acceptance.replayed:
        await runtime.abort_reservation(reservation)
        return TurnActivation(acceptance)
    if before_activate is not None:
        before_activate(acceptance)
    try:
        handle = await runtime.activate(
            reservation,
            persisted_user_message_id=acceptance.receipt.message_id,
            fresh_user_session=acceptance.fresh_user_session,
        )
        return TurnActivation(acceptance, handle)
    except Exception as exc:
        receipt = acceptance.receipt
        log.exception(
            "turn_admission.activation_failed",
            session_key=receipt.accepted_session_key,
            task_id=receipt.task_id,
        )
        if reservation.activated:
            # QUEUED in storage can already be driver-owned. Never compensate
            # an observer failure after this irreversible runtime transition.
            return TurnActivation(acceptance)
        try:
            await runtime.abort_reservation(reservation)
        except Exception:
            log.exception("turn_admission.activation_abort_failed", task_id=receipt.task_id)
        if reservation.activated:
            return TurnActivation(acceptance)

        task: ActivationTask | None = None
        # Aborting claims the reservation under the runtime lock. Cleanup may
        # fail afterwards, but an aborted reservation can no longer activate.
        if reservation.aborted and receipt.task_id == reservation.task_record.task_id:
            compensated = False
            if acceptance.goal_context is not None and compensate_goal is not None:
                try:
                    compensated = (
                        await compensate_goal(acceptance.goal_context.as_task_detail()) is not None
                    )
                except Exception:
                    log.exception(
                        "turn_admission.goal_activation_compensation_failed",
                        task_id=receipt.task_id,
                    )
            if receipt.task_id and not compensated:
                try:
                    task = await storage.fail_queued_agent_task_activation(
                        receipt.task_id,
                        session_key=receipt.accepted_session_key,
                        error_class=type(exc).__name__,
                        error_message=str(exc),
                    )
                except Exception:
                    log.exception(
                        "turn_admission.activation_failure_record_failed", task_id=receipt.task_id
                    )
            if on_unactivated is not None:
                try:
                    on_unactivated()
                except Exception:
                    log.exception(
                        "turn_admission.unactivated_cleanup_failed", task_id=receipt.task_id
                    )
        if task is None and receipt.task_id:
            try:
                task = await storage.get_agent_task(receipt.task_id)
            except Exception:
                log.exception("turn_admission.activation_status_unknown", task_id=receipt.task_id)
        status = (
            task.status
            if task is not None
            and task.task_id == receipt.task_id
            and task.session_key == receipt.accepted_session_key
            else None
        )
        return TurnActivation(acceptance, activation_failed=True, task_status=status)
