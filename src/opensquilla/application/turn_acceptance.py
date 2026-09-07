"""Durable turn acceptance: prepare, persist, activate, and reconcile failures.

Protocol parsing and native route construction belong to the Gateway. This
application owns the acceptance boundary for every interactive turn producer.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
import uuid
from dataclasses import replace
from functools import partial
from typing import Any, cast

import structlog

from opensquilla.application.admission_errors import (
    AdmissionError,
    AdmissionQueueFullError,
    AdmissionShuttingDownError,
    AdmissionUnavailableError,
)
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
from opensquilla.application.admission_views import (
    AdmissionAcceptance,
    AdmissionAnnotation,
    AdmissionAnnotationTarget,
    AdmissionArchive,
    AdmissionCommit,
    AdmissionMetaControl,
    AdmissionPlanRevision,
    AdmissionPlanRun,
    AdmissionProjectOrigin,
    AdmissionSessionChanges,
    AdmissionSessionIntent,
    AdmissionTaskRecord,
    MetaAdmissionControl,
    PreparedAdmissionIntent,
    SessionIdentity,
)
from opensquilla.application.turn_acceptance_ports import AdmissionPrimitives, AdmissionReservation
from opensquilla.application.turn_activation import commit_reserved_turn
from opensquilla.application.turn_admission import AcceptedCollaboration, AdmitTurn, AdmitTurnResult
from opensquilla.application.turn_input import (
    PlanAdmissionContext,
    TurnRequestIdentity,
    complete_durable_ingress,
)
from opensquilla.attachment_refs import (
    PENDING_CHAT_INPUT_MATERIAL_STORE,
    promote_pending_chat_input_attachments,
)
from opensquilla.project_workspaces import ProjectWorkspaceStateError
from opensquilla.run_mode import RunMode

log = structlog.get_logger(__name__)
_SESSION_ROUTING_MODES = frozenset({"direct", "router", "ensemble"})


class DurableTurnAdmission:
    """Own explicit ingress intent and one durable acceptance implementation."""

    def __init__(self, ports: AdmissionPrimitives) -> None:
        self._ports = ports

    async def admit(self, command: AdmitTurn) -> AdmitTurnResult:
        ports = self._ports
        # Enter the intent guard before session lookup, replay, or preparation.
        intent_guard = (
            contextlib.nullcontext()
            if command.explicit_intent_registered
            else ports.explicit_ingress_intent(command.session_key)
        )
        async with intent_guard:
            try:
                if command.surface == "webchat":
                    if ports.sessions is None:
                        if command.prompt_annotation_ids or command.document_context is not None:
                            raise AdmissionUnavailableError(
                                "Artifact context requires durable session storage"
                            )
                        if command.initial_collaboration_mode or command.initial_routing_mode:
                            raise AdmissionUnavailableError(
                                "Initial session controls require atomic turn acceptance"
                            )
                        return {
                            "ok": True,
                            "sessionKey": command.session_key,
                            "instant_accept": True,
                        }
                    intent = command.intent if command.intent_was_provided else None
                    if intent is None and command.workspace_id is not None:
                        intent = "new_chat"
                    if intent != "new_chat":
                        storage = ports.storage
                        get_session = getattr(storage, "get_session", None)
                        if callable(get_session):
                            try:
                                if await get_session(command.session_key) is None:
                                    intent = "new_chat"
                            except Exception as exc:
                                raise AdmissionUnavailableError(
                                    f"Failed to inspect chat session: {exc}"
                                ) from exc
                        else:
                            try:
                                await ports.sessions.get_or_create(
                                    session_key=command.session_key,
                                    agent_id=ports.effective_agent_id(None, command.session_key),
                                    display_name="WebChat",
                                )
                            except Exception as exc:
                                raise AdmissionUnavailableError(
                                    f"Failed to initialize chat session: {exc}"
                                ) from exc
                    command = replace(command, intent=intent or "continue")
                guard = command.pending_input
                plan = command.plan or PlanAdmissionContext()
                async with ports.authority_scope():
                    result = await _accept_turn(
                        command,
                        ports,
                        plan_revision_id=plan.revision_id,
                        plan_context_revision_id=plan.context_revision_id,
                        plan_run_driver_kind=plan.run_driver_kind,
                        plan_run_driver_id=plan.run_driver_id,
                        required_collaboration_mode=plan.required_collaboration_mode,
                        required_collaboration_revision=plan.required_collaboration_revision,
                        initial_collaboration_mode=command.initial_collaboration_mode,
                        initial_routing_mode=command.initial_routing_mode,
                        expected_collaboration_revision=plan.expected_collaboration_revision,
                        expected_active_plan_revision_id=plan.expected_active_revision_id,
                        require_idle_for_current_plan_implementation=plan.require_idle,
                        atomic_collaboration_mode_update=plan.atomic_mode_update,
                        pending_input_id=guard.pending_input_id if guard else None,
                        pending_input_fingerprint=guard.request_fingerprint if guard else None,
                        pending_input_revision=guard.expected_revision if guard else None,
                        trusted_run_kind=command.trusted_run_kind,
                    )
                if command.surface == "webchat":
                    key = result.get("sessionKey") or result.get("key") or command.session_key
                    result = {"ok": True, "sessionKey": key, **result}
                return cast(AdmitTurnResult, result)
            except Exception:
                if command.surface == "webchat":
                    ports.clear_compaction_marker(command.session_key)
                raise


async def _accept_turn(
    command: AdmitTurn,
    ports: AdmissionPrimitives,
    *,
    plan_revision_id: str | None = None,
    plan_context_revision_id: str | None = None,
    plan_run_driver_kind: str | None = None,
    plan_run_driver_id: str | None = None,
    required_collaboration_mode: str | None = None,
    required_collaboration_revision: int | None = None,
    initial_collaboration_mode: str | None = None,
    initial_routing_mode: str | None = None,
    expected_collaboration_revision: int | None = None,
    expected_active_plan_revision_id: str | None = None,
    require_idle_for_current_plan_implementation: bool = False,
    atomic_collaboration_mode_update: bool = False,
    pending_input_id: str | None = None,
    pending_input_fingerprint: str | None = None,
    pending_input_revision: int | None = None,
    _prompt_annotation_acceptance_retries: int = 1,
    trusted_run_kind: str | None = None,
) -> AdmitTurnResult:
    key = command.session_key
    message_text = command.message
    prompt_annotation_ids = command.prompt_annotation_ids
    document_context_request = command.document_context
    if prompt_annotation_ids and document_context_request is not None:
        raise AdmissionError(
            "DOCUMENT_CONTEXT_CONFLICT",
            "A normal document context cannot be combined with prompt annotations.",
            retryable=False,
            accepted=False,
        )
    if prompt_annotation_ids or document_context_request is not None:
        if command.source.caller_kind != "web" or not ports.is_owner:
            raise AdmissionError(
                (
                    "ARTIFACT_PROMPT_ANNOTATIONS_FORBIDDEN"
                    if prompt_annotation_ids
                    else "DOCUMENT_CONTEXT_FORBIDDEN"
                ),
                "Document editing requires an interactive owner Web session.",
                retryable=False,
                accepted=False,
            )
    requested_client_message_id = command.client_message_id
    requested_surface_id = command.surface_id
    normalized_input = ports.normalize_input(command)
    message_text = normalized_input.message_text
    semantic_message_text = normalized_input.semantic_message
    combined_attachments = [*normalized_input.generated_attachments, *command.attachments]
    persist_enabled = ports.policy.persist_transcripts
    media_root = ports.policy.media_root
    session_intent: AdmissionSessionIntent
    if command.intent == "continue":
        session_intent = "continue"
    elif command.intent == "new_chat":
        session_intent = "new_chat"
    elif command.intent == "reset_same_key":
        session_intent = "reset_same_key"
    else:
        raise ValueError(f"Invalid session intent: {command.intent}")
    fork_before_message_id = command.fork_before_message_id
    if fork_before_message_id is not None and session_intent != "continue":
        raise ValueError("forkBeforeMessageId cannot be combined with non-continue intent")
    if (prompt_annotation_ids or document_context_request is not None) and (
        session_intent != "continue" or fork_before_message_id is not None
    ):
        raise ports.artifact_error(
            "invalid_request",
            retryable=False,
        )
    param_initial_routing_mode = command.initial_routing_mode
    if (
        initial_routing_mode is not None
        and param_initial_routing_mode is not None
        and initial_routing_mode != param_initial_routing_mode
    ):
        raise ValueError("initialRoutingMode does not match initial_routing_mode")
    if initial_routing_mode is None:
        initial_routing_mode = param_initial_routing_mode
    raw_workspace_id = command.workspace_id
    workspace_id: str | None = None
    if raw_workspace_id is not None:
        if not isinstance(raw_workspace_id, str) or not raw_workspace_id.strip():
            raise ValueError("workspaceId must be a non-empty string")
        workspace_id = raw_workspace_id.strip()
        if session_intent != "new_chat":
            raise ValueError("workspaceId is only valid for a new task")
        if not ports.is_owner:
            raise AdmissionError(
                "OWNER_REQUIRED",
                "Project workspaces require a locally proven owner.",
            )
    if plan_revision_id is not None:
        plan_revision_id = plan_revision_id.strip()
        if not plan_revision_id:
            raise ValueError("plan_revision_id must not be empty")
        if session_intent not in {
            "continue",
            "new_chat",
        }:
            raise ValueError("Plan implementation supports continue or new_chat intent only")
        if fork_before_message_id is not None:
            raise ValueError("Plan implementation cannot be combined with a transcript fork")
    if plan_context_revision_id is not None:
        plan_context_revision_id = plan_context_revision_id.strip()
        if not plan_context_revision_id:
            raise ValueError("plan_context_revision_id must not be empty")
    if plan_run_driver_kind is not None:
        if plan_revision_id is None:
            raise ValueError("plan_run_driver_kind requires plan_revision_id")
        if plan_run_driver_kind not in {"manual", "goal"}:
            raise ValueError("plan_run_driver_kind must be manual or goal")
    if plan_run_driver_kind == "goal":
        if not isinstance(plan_run_driver_id, str) or not plan_run_driver_id.strip():
            raise ValueError("plan_run_driver_id is required for a goal plan run")
    elif plan_run_driver_id is not None:
        raise ValueError("plan_run_driver_id is valid only for a goal plan run")
    if required_collaboration_mode not in {None, "default", "plan"}:
        raise ValueError("required_collaboration_mode must be default or plan")
    if required_collaboration_revision is not None and (
        not isinstance(required_collaboration_revision, int)
        or isinstance(required_collaboration_revision, bool)
        or required_collaboration_revision < 0
    ):
        raise ValueError("required_collaboration_revision must be a non-negative integer")
    if initial_collaboration_mode is not None and (
        not isinstance(initial_collaboration_mode, str)
        or initial_collaboration_mode not in {"default", "plan"}
    ):
        raise ValueError("initial_collaboration_mode must be default or plan")
    if initial_collaboration_mode is not None:
        if session_intent != "new_chat":
            raise ValueError("initial_collaboration_mode requires new_chat intent")
        if fork_before_message_id is not None:
            raise ValueError("initial_collaboration_mode cannot be combined with a transcript fork")
        if plan_revision_id is not None or plan_context_revision_id is not None:
            raise ValueError("initial_collaboration_mode cannot be combined with a plan operation")
        if (
            required_collaboration_mode is not None
            and required_collaboration_mode != initial_collaboration_mode
        ):
            raise ValueError("Conflicting required collaboration modes")
        required_collaboration_mode = initial_collaboration_mode
        required_collaboration_revision = 1 if initial_collaboration_mode == "plan" else 0
    if initial_routing_mode is not None:
        if initial_routing_mode not in _SESSION_ROUTING_MODES:
            raise ValueError("initialRoutingMode must be direct, router, or ensemble")
        if session_intent != "new_chat":
            raise ValueError("initialRoutingMode requires new_chat intent")
        if fork_before_message_id is not None:
            raise ValueError("initialRoutingMode cannot be combined with a transcript fork")
        # Validate the activation plan before accepting a first message. This
        # is read-only and rejects an Ensemble that cannot be built today.

        ports.validate_initial_routing(initial_routing_mode)

    session_manager = ports.sessions
    if session_manager is None:
        raise KeyError("No session manager available")

    storage_candidate = ports.storage
    if storage_candidate is None:
        raise KeyError("No session storage available")
    storage = storage_candidate

    ingress_identity = TurnRequestIdentity(
        command.source_scope,
        key,
        command.client_request_id,
        command.request_fingerprint,
    )
    get_ingress_receipt = (
        storage.replay_turn_ingress_receipt if storage.capabilities.receipts else None
    )
    if callable(get_ingress_receipt):
        previous_acceptance = await get_ingress_receipt(
            source_scope=ingress_identity.source_scope,
            request_session_key=ingress_identity.request_session_key,
            client_request_id=ingress_identity.client_request_id,
        )
        if previous_acceptance is not None:
            if (
                previous_acceptance.receipt.request_fingerprint
                != ingress_identity.request_fingerprint
            ):
                raise AdmissionError(
                    "IDEMPOTENCY_CONFLICT",
                    "clientRequestId was already used for a different turn",
                    retryable=False,
                    accepted=False,
                )
            if pending_input_id is not None:
                if (
                    requested_client_message_id is None
                    or pending_input_fingerprint is None
                    or pending_input_revision is None
                    or pending_input_fingerprint != ingress_identity.request_fingerprint
                ):
                    raise AdmissionPendingInputConflictError(
                        "pending input replay identity is incomplete or inconsistent"
                    )
                await storage.consume_replayed_pending_chat_input(
                    pending_input_id=pending_input_id,
                    session_key=ingress_identity.request_session_key,
                    source_scope=ingress_identity.source_scope,
                    client_request_id=ingress_identity.client_request_id,
                    client_message_id=requested_client_message_id,
                    request_fingerprint=ingress_identity.request_fingerprint,
                    expected_revision=pending_input_revision,
                )
            replay_response = await ports.accepted_response(
                previous_acceptance,
                client_request_id=ingress_identity.client_request_id,
                storage=storage,
                accepted_prompt_annotation_ids=prompt_annotation_ids,
            )
            if initial_collaboration_mode is not None:
                replay_response["acceptedCollaboration"] = {
                    "mode": initial_collaboration_mode,
                    "revision": required_collaboration_revision or 0,
                }
                current_session = await storage.get_session(
                    previous_acceptance.receipt.accepted_session_key
                )
                if current_session is not None:
                    replay_response["collaboration"] = ports.collaboration_snapshot(current_session)
            if initial_routing_mode is not None:
                replay_response["acceptedRouting"] = {
                    "mode": initial_routing_mode,
                }
                replay_response["routing"] = await ports.routing_snapshot(
                    previous_acceptance.receipt.accepted_session_key,
                )
            return replay_response

    if prompt_annotation_ids or document_context_request is not None:
        existing_annotation_session = await storage.get_session(key)
        existing_collaboration_mode = (
            str(getattr(existing_annotation_session, "collaboration_mode", "default") or "default")
            .strip()
            .lower()
        )
        if (
            plan_revision_id is not None
            or plan_context_revision_id is not None
            or required_collaboration_mode == "plan"
            or initial_collaboration_mode == "plan"
            or existing_collaboration_mode == "plan"
        ):
            raise AdmissionError(
                (
                    "ARTIFACT_PROMPT_ANNOTATIONS_PLAN_UNSUPPORTED"
                    if prompt_annotation_ids
                    else "DOCUMENT_CONTEXT_PLAN_UNSUPPORTED"
                ),
                "Document editing must be sent from the normal execution mode, not Plan.",
                retryable=False,
                accepted=False,
            )
        if ports.is_remote_guest(command.source):
            raise AdmissionError(
                (
                    "ARTIFACT_PROMPT_ANNOTATIONS_FORBIDDEN"
                    if prompt_annotation_ids
                    else "DOCUMENT_CONTEXT_FORBIDDEN"
                ),
                "Document editing requires a locally proven owner.",
                retryable=False,
                accepted=False,
            )

    if prompt_annotation_ids and combined_attachments:
        # The first annotation release binds only source-backed DOM anchors.
        # Keep idempotent receipts replayable across upgrades, then reject every
        # new mixed annotation/attachment request before ingest or provider work.
        raise AdmissionError(
            "PROMPT_ANNOTATION_ATTACHMENTS_UNSUPPORTED",
            "Prompt annotations cannot be sent with file or image attachments.",
            retryable=False,
            accepted=False,
        )

    prompt_annotation_rows: tuple[AdmissionAnnotation, ...] = ()
    prepared_prompt_annotation_targets: tuple[AdmissionAnnotationTarget, ...] = ()
    prompt_annotation_snapshots: tuple[dict[str, Any], ...] = ()

    if require_idle_for_current_plan_implementation:
        pending_user_inputs = getattr(ports.runtime, "pending_user_inputs", None)
        if callable(pending_user_inputs):
            pending = list(pending_user_inputs(key) or [])
            if pending:
                request = pending[0]
                request_id = str(request.get("request_id") or request.get("requestId") or "")
                task_id = str(request.get("run_id") or request.get("runId") or "")
                log.info(
                    "plan_implementation.admission_rejected",
                    session_key=key,
                    reason="input_pending",
                    request_id=request_id,
                    task_id=task_id,
                )
                raise AdmissionError(
                    "PLAN_INPUT_PENDING",
                    "The current Plan turn is waiting for user input.",
                    details={
                        "requestId": request_id,
                        "turnId": task_id,
                        "allowedActions": ["answer", "stop", "wait"],
                    },
                    retryable=True,
                    accepted=False,
                )

    def _project_workspace_error(exc: ProjectWorkspaceStateError) -> Exception:
        return ports.workspace_error(exc)

    def _preaccept_storage_busy_error(exc: AdmissionStorageBusyError) -> AdmissionError:
        return AdmissionError(
            "STORAGE_BUSY",
            "Session storage is temporarily busy. Retry this send.",
            details={
                "operation": exc.operation,
                "waited_ms": exc.waited_ms,
            },
            retryable=True,
            retry_after_ms=exc.retry_after_ms,
            accepted=False,
        )

    selected_workspace = None
    workspace_guard = None
    if workspace_id is not None:
        try:
            validated_workspace = await storage.resolve_workspace(workspace_id)
        except ProjectWorkspaceStateError as exc:
            raise _project_workspace_error(exc) from exc
        selected_workspace = validated_workspace.workspace
        workspace_guard = validated_workspace.guard

    task_runtime_candidate = ports.runtime
    prepare_intent = (
        session_manager.prepare_intent if session_manager.capabilities.prepared_intent else None
    )
    prepare_message = (
        session_manager.prepare_message if session_manager.capabilities.prepared_message else None
    )
    create_kwargs: dict[str, Any] = {}
    if command.source.caller_kind == "web":
        create_kwargs["display_name"] = "WebChat"
    if selected_workspace is not None:
        mode = ports.policy.project_run_mode
        mode_source = (
            "project_default"
            if mode is RunMode.SAFE and ports.policy.default_run_mode is RunMode.FULL
            else "operator_default"
        )
        create_kwargs["workspace_id"] = selected_workspace.workspace_id
        create_kwargs["origin"] = AdmissionProjectOrigin(
            run_mode=mode,
            workspace=selected_workspace.path,
            run_mode_source=mode_source,
        )
    if initial_routing_mode is not None:
        create_kwargs["model_routing_mode"] = initial_routing_mode
    supports_prepared_acceptance = all(
        callable(value)
        for value in (
            prepare_intent,
            prepare_message,
            (storage.accept_turn if storage.capabilities.atomic_acceptance else None),
        )
    )
    supports_task_runtime_activation = (
        supports_prepared_acceptance
        and task_runtime_candidate is not None
        and callable(getattr(task_runtime_candidate, "reserve", None))
        and callable(getattr(task_runtime_candidate, "activate", None))
        and callable(getattr(task_runtime_candidate, "abort_reservation", None))
    )
    if (
        initial_collaboration_mode is not None or initial_routing_mode is not None
    ) and not supports_task_runtime_activation:
        raise AdmissionUnavailableError("Initial session controls require atomic turn acceptance")

    async def _prepare_or_apply_intent() -> tuple[SessionIdentity, PreparedAdmissionIntent | None]:
        existing_session = await storage.get_session(key)
        if existing_session is None and session_intent == "continue":
            raise KeyError(f"Session not found: {key}")
        if fork_before_message_id is None and supports_prepared_acceptance:
            assert callable(prepare_intent)
            plan = await prepare_intent(
                key,
                session_intent,
                agent_id=ports.effective_agent_id(existing_session, key),
                **create_kwargs,
            )
            return plan.node, plan
        if session_manager.capabilities.apply_intent:
            applied_session, _intent_applied = await session_manager.apply_intent(
                key,
                session_intent,
                agent_id=ports.effective_agent_id(existing_session, key),
                **create_kwargs,
            )
            return applied_session, None
        if session_intent != "continue":
            raise RuntimeError("Session intent handling requires SessionManager.apply_intent")
        assert existing_session is not None
        return existing_session, None

    intent_lock = ports.session_lock(key)
    try:
        if intent_lock is None:
            session, atomic_intent_plan = await _prepare_or_apply_intent()
        else:
            async with intent_lock:
                session, atomic_intent_plan = await _prepare_or_apply_intent()
    except AdmissionStorageBusyError as exc:
        raise _preaccept_storage_busy_error(exc) from exc

    if (initial_collaboration_mode is not None or initial_routing_mode is not None) and (
        atomic_intent_plan is None or getattr(atomic_intent_plan, "action", None) != "create"
    ):
        raise ValueError("Initial session controls require atomic session creation")

    if fork_before_message_id is not None:
        parent_key = key
        agent_id = ports.effective_agent_id(session, parent_key)
        child_key = ports.new_session_key(agent_id, "webchat")
        prepare_prefix_branch = (
            session_manager.prepare_prefix_branch
            if session_manager.capabilities.prefix_branch
            else None
        )
        if (
            callable(prepare_prefix_branch)
            and callable(prepare_message)
            and callable(storage.accept_turn if storage.capabilities.atomic_acceptance else None)
        ):

            async def _prepare_prefix_intent() -> PreparedAdmissionIntent:
                return await prepare_prefix_branch(
                    parent_key,
                    child_key,
                    fork_before_message_id=fork_before_message_id,
                    status="done",
                )

            parent_lock = ports.session_lock(parent_key)
            try:
                if parent_lock is None:
                    atomic_intent_plan = await _prepare_prefix_intent()
                else:
                    async with parent_lock:
                        atomic_intent_plan = await _prepare_prefix_intent()
            except AdmissionStorageBusyError as exc:
                raise _preaccept_storage_busy_error(exc) from exc
            session = atomic_intent_plan.node
            key = child_key
        else:
            session = await ports.fork_session(
                storage,
                parent_key,
                child_key,
                explicit_title=None,
                fork_transcript=True,
                status="done",
                fork_before_message_id=fork_before_message_id,
            )
            key = child_key
            await ports.publish_forked(key)

    bound_workspace_id = getattr(session, "workspace_id", None)
    if isinstance(bound_workspace_id, str) and bound_workspace_id:
        if workspace_guard is None or workspace_guard.workspace_id != bound_workspace_id:
            try:
                validated_workspace = await storage.resolve_workspace(bound_workspace_id)
            except ProjectWorkspaceStateError as exc:
                raise _project_workspace_error(exc) from exc
            workspace_guard = validated_workspace.guard

    canonical_session_id = getattr(session, "session_id", None)
    session_id = (
        canonical_session_id
        if isinstance(canonical_session_id, str) and canonical_session_id
        else key.split(":")[-1] or key
    )
    binding = await ports.bind_artifact(
        command,
        key=key,
        session_id=session_id,
        session=session,
    )
    prompt_annotation_rows = binding.annotations
    prepared_prompt_annotation_targets = binding.targets
    prompt_annotation_snapshots = binding.snapshots
    plan_run: AdmissionPlanRun | None = None
    plan_revision_to_create: AdmissionPlanRevision | None = None
    selected_plan_revision_id = plan_revision_id
    if plan_revision_id is not None:
        selected_revision = await storage.get_plan_revision(plan_revision_id)
        if selected_revision is None:
            raise KeyError(f"Plan revision not found: {plan_revision_id}")
        intent_action = getattr(atomic_intent_plan, "action", "continue")
        if intent_action == "continue":
            current_revision_id = getattr(session, "active_plan_revision_id", None)
            if current_revision_id != plan_revision_id:
                raise AdmissionError(
                    "PLAN_REVISION_CHANGED",
                    "The selected plan is no longer the current revision.",
                    retryable=False,
                    accepted=False,
                )
            active_run = await storage.get_active_plan_run(key)
            if active_run is not None:
                if active_run.status in {"queued", "running"}:
                    raise AdmissionError(
                        "PLAN_RUN_ACTIVE",
                        "This plan already has an implementation task in progress.",
                        details={"runId": active_run.run_id, "status": active_run.status},
                        retryable=False,
                        accepted=False,
                    )
                if active_run.driver_kind == "goal":
                    raise AdmissionError(
                        "PLAN_RUN_GOAL_OWNED",
                        "A Goal controller owns the active plan run.",
                        details={"runId": active_run.run_id, "status": active_run.status},
                        retryable=False,
                        accepted=False,
                    )
                if active_run.plan_revision_id == plan_revision_id:
                    # Resume the same mutable overlay; never hide progress by
                    # manufacturing a replacement run for the same revision.
                    plan_run = active_run
        elif intent_action != "create":
            raise ValueError("A new-task plan implementation must create a fresh session")
        else:
            # A new task gets an independent immutable lineage. Sharing the
            # source plan_id would make two valid replans collide on the global
            # (plan_id, generation) invariant and would couple deletion
            # lifecycles across sessions.
            plan_revision_to_create = storage.fork_plan_revision(
                source_session_key=key,
                source_session_id=session_id,
                source_epoch=int(getattr(session, "epoch", 0) or 0),
                title=selected_revision.title,
                markdown=selected_revision.markdown,
                steps=selected_revision.steps,
            )
            selected_plan_revision_id = plan_revision_to_create.revision_id
        if plan_run is not None and plan_run_driver_kind is not None:
            if plan_run.driver_kind != plan_run_driver_kind:
                raise AdmissionError(
                    "PLAN_RUN_DRIVER_MISMATCH",
                    "The resumed plan run is owned by a different execution driver.",
                    details={"runId": plan_run.run_id, "driverKind": plan_run.driver_kind},
                    retryable=False,
                    accepted=False,
                )
        if plan_run is None:
            assert selected_plan_revision_id is not None
            plan_run = storage.new_plan_run(
                run_id=str(uuid.uuid4()),
                session_key=key,
                session_id=session_id,
                session_epoch=int(getattr(session, "epoch", 0) or 0),
                plan_revision_id=selected_plan_revision_id,
                driver_kind=plan_run_driver_kind or "manual",
                driver_id=(plan_run_driver_id if plan_run_driver_kind == "goal" else None),
            )
    if plan_context_revision_id is not None:
        context_revision = await storage.get_plan_revision(plan_context_revision_id)
        if context_revision is None:
            raise KeyError(f"Plan revision not found: {plan_context_revision_id}")
        if (
            getattr(atomic_intent_plan, "action", "continue") == "continue"
            and getattr(session, "active_plan_revision_id", None) != plan_context_revision_id
        ):
            raise AdmissionError(
                "PLAN_REVISION_CHANGED",
                "The selected plan is no longer the current revision.",
                retryable=False,
                accepted=False,
            )
    # PromptAnnotation turns are a bounded artifact mutation protocol, including
    # when the annotation batch is the session's first transcript entry.  An
    # auxiliary naming request would escape that turn's provider-call budget and
    # strict tool boundary, so annotation ingress must never arm auto-naming.
    generate_title = (
        False
        if prompt_annotation_ids
        else await ports.should_auto_title(storage, session, key, session_id)
    )
    disk_budget = ports.policy.disk_budget_bytes
    if pending_input_id is not None:
        # SQLite deliberately retains queue-owned references. Only after the
        # target session identity has been resolved do we promote those bytes
        # into its canonical transcript store. The request fingerprint still
        # uses the immutable staged payload supplied by the dispatch handler.
        promoted_attachments: list[dict[str, Any]] = []
        try:
            for attachment in combined_attachments:
                if (
                    isinstance(attachment, dict)
                    and attachment.get("store") == PENDING_CHAT_INPUT_MATERIAL_STORE
                ):
                    promoted_attachments.extend(
                        promote_pending_chat_input_attachments(
                            [attachment],
                            media_root=media_root,
                            pending_input_id=pending_input_id,
                            target_session_id=session_id,
                            disk_budget_bytes=(
                                disk_budget if isinstance(disk_budget, int) else None
                            ),
                        )
                    )
                else:
                    promoted_attachments.append(attachment)
        except (OSError, ValueError) as exc:
            raise AdmissionError(
                "PENDING_ATTACHMENT_MATERIAL_UNAVAILABLE",
                "A queued attachment could not be recovered; keep the item and retry",
                retryable=True,
                accepted=False,
            ) from exc
        combined_attachments = promoted_attachments
    ingested_attachments = await ports.ingest_attachments(
        message_text,
        combined_attachments,
        session_id=session_id,
        allow_material_refs=pending_input_id is not None,
    )
    message_text = ingested_attachments.text
    raw_attachments = ingested_attachments.attachments
    inferred_normalized_input = None
    if normalized_input.metadata.get("guard_action") == "none":
        inferred_normalized_input = ports.infer_normalized_input(
            message_text,
            raw_attachments,
        )
        if inferred_normalized_input is not None:
            message_text = inferred_normalized_input.message_text
            semantic_message_text = inferred_normalized_input.semantic_message

    normalization_metadata = (
        normalized_input.metadata
        if normalized_input.metadata.get("guard_action") != "none"
        else (
            inferred_normalized_input.metadata
            if inferred_normalized_input is not None
            and inferred_normalized_input.metadata.get("guard_action") != "none"
            else None
        )
    )
    if normalization_metadata is not None:
        raw_attachments = ports.materialize_normalized_attachments(
            raw_attachments,
            media_root=media_root,
            session_id=session_id,
            normalization_metadata=normalization_metadata,
            disk_budget_bytes=disk_budget if isinstance(disk_budget, int) else None,
        )
    # Evict consumed uuids only after the turn is accepted.
    _consumed_file_uuids: list[str] = list(ingested_attachments.consumed_file_uuids)
    log.info(
        "sessions.send.params",
        session_key=key,
        message_len=len(message_text),
        attachments_count=len(raw_attachments),
    )

    display_text = command.display_text if command.source.caller_kind == "web" else None
    if display_text is not None and not isinstance(display_text, str):
        display_text = None
    if display_text is None and command.source.caller_kind == "web":
        from opensquilla.meta_preflight_protocol import (
            display_text_from_preflight_confirmation,
        )

        display_text = display_text_from_preflight_confirmation(message_text)
    provider_message_text = message_text
    if command.source.caller_kind == "web":
        from opensquilla.meta_preflight_protocol import (
            strip_preflight_confirmation_protocol_text,
        )

        stripped_message = strip_preflight_confirmation_protocol_text(message_text)
        if stripped_message is not None:
            provider_message_text = stripped_message.strip()

    durable_meta_control: AdmissionMetaControl | None = None
    durable_meta_control_payload: dict[str, Any] | None = None
    parsed_control: MetaAdmissionControl | None = None
    get_meta_control = (
        storage.get_meta_control_intent if storage.capabilities.meta_controls else None
    )
    if callable(get_meta_control):
        parsed_control = ports.parse_meta_control(
            provider_message_text,
            semantic_message_text,
            client_request_id=ingress_identity.client_request_id,
        )
        if parsed_control is not None:
            candidate = await get_meta_control(
                session_key=key,
                control_kind=parsed_control.kind,
                correlation_id=parsed_control.correlation_id,
            )
            if (
                candidate is not None
                and candidate.status == "staged"
                and (
                    candidate.control_kind != "manual"
                    or candidate.meta_skill_name == parsed_control.name
                )
            ):
                durable_meta_control = candidate
                durable_meta_control_payload = {
                    "version": 1,
                    "intent_id": candidate.intent_id,
                    "kind": candidate.control_kind,
                    "name": candidate.meta_skill_name,
                    "correlation_id": candidate.correlation_id,
                }
                if candidate.control_kind == "replay":
                    durable_meta_control_payload.update(
                        {
                            "run_id": candidate.replay_run_id,
                            "mode": candidate.replay_mode,
                        }
                    )
        explicit_request_id = command.explicit_request_id
        if parsed_control is not None and durable_meta_control is None and explicit_request_id:
            legacy_match = False
            if parsed_control.kind == "manual":
                pending_name = ports.peek_meta_launch(
                    key,
                    client_request_id=ingress_identity.client_request_id,
                )
                legacy_match = pending_name == parsed_control.name
            if not legacy_match:
                raise AdmissionError(
                    "META_CONTROL_NOT_STAGED",
                    "This MetaSkill control is missing, expired, or already belongs to "
                    "another accepted turn. Start it again from the MetaSkill action.",
                    retryable=False,
                    accepted=False,
                )

    def _promote_pending_meta_launch() -> str | None:
        return ports.promote_meta_launch(
            key,
            client_request_id=ingress_identity.client_request_id,
            message=provider_message_text,
            semantic_message=semantic_message_text,
        )

    prepared_route = await ports.prepare_route(
        command,
        session=session,
        key=key,
        session_id=session_id,
        atomic_intent_plan=atomic_intent_plan,
        binding=binding,
        workspace_guard=workspace_guard,
    )
    agent_id = prepared_route.agent_id
    route_envelope = prepared_route.envelope
    turn_id = prepared_route.turn_id
    mode_resolution = prepared_route.mode_resolution
    guest_profile = prepared_route.guest_profile
    accepted_run_mode_override = prepared_route.accepted_run_mode_override
    accepted_run_mode_origin = prepared_route.accepted_run_mode_origin
    workspace_guard = prepared_route.workspace_guard
    session = prepared_route.session

    def _cleanup_rejected_guest_profile() -> None:
        if guest_profile is not None:
            guest_profile.cleanup()

    capture_controls = {
        "input_provenance": command.capture.input_provenance,
        "no_memory_capture": command.capture.no_memory_capture,
        "run_kind": trusted_run_kind or command.capture.run_kind,
    }
    input_provenance = command.capture.input_provenance
    if input_provenance is not None:
        input_provenance = dict(input_provenance)
    else:
        input_provenance = dict(route_envelope.input_provenance)
    if normalization_metadata is not None:
        input_provenance["input_normalization"] = normalization_metadata
    if input_provenance != route_envelope.input_provenance:
        route_envelope = ports.refine_route(
            route_envelope,
            input_provenance=input_provenance,
        )
    run_kind = trusted_run_kind or command.capture.run_kind or "session_turn"
    claim_current_goal = False
    goal_claim_excluded_kinds = {
        "goal",
        "plan",
        "review",
        "subagent",
        "cron",
        "cron_turn",
        "memory",
        "memory_dream",
        "memory_flush",
        "memory_repair",
        "compaction",
        "session_compaction",
        "runtime_send",
    }
    if (
        getattr(atomic_intent_plan, "action", "continue") == "continue"
        and session_intent == "continue"
        and plan_revision_id is None
        and plan_context_revision_id is None
        and plan_run is None
        and durable_meta_control is None
        and parsed_control is None
        and run_kind not in goal_claim_excluded_kinds
    ):
        claim_current_goal = True

    # Allocate the durable causal identity before persistence.  The same id is
    # handed to TaskRuntime, live events, bootstrap history, and every transcript
    # row produced by this turn.
    client_message_id = requested_client_message_id or uuid.uuid4().hex
    surface_id = (
        requested_surface_id
        or getattr(route_envelope, "channel_id", None)
        or str(getattr(route_envelope, "source_kind", "unknown"))
    )
    route_envelope = ports.refine_route(
        route_envelope,
        metadata={
            **route_envelope.metadata,
            "client_request_id": ingress_identity.client_request_id,
            "client_message_id": client_message_id,
            "surface_id": surface_id,
            # The direct RPC execution path constructs ToolContext before a
            # TaskRuntime record exists. Keep the durable turn identity in the
            # envelope so PromptAnnotation candidate loops never fall back to
            # the legacy one-shot writer merely because task metadata is late.
            "task_id": turn_id,
            "turn_context_intent": "send",
            "turn_context_revision": 1,
            **(
                {"meta_control": durable_meta_control_payload}
                if durable_meta_control_payload is not None
                else {}
            ),
            **(
                {
                    "plan_run_id": plan_run.run_id,
                    "plan_revision_id": selected_plan_revision_id,
                    "require_current_plan_revision": True,
                }
                if plan_run is not None
                else {}
            ),
            **(
                {
                    "plan_revision_id": plan_context_revision_id,
                    "require_current_plan_revision": True,
                }
                if plan_context_revision_id is not None
                else {}
            ),
            **(
                {"required_collaboration_mode": required_collaboration_mode}
                if required_collaboration_mode is not None
                else {}
            ),
            **(
                {"required_collaboration_revision": (required_collaboration_revision)}
                if required_collaboration_revision is not None
                else {}
            ),
        },
    )
    ingress_turn_context: dict[str, Any] = {
        "turn_id": turn_id,
        "client_request_id": ingress_identity.client_request_id,
        "client_message_id": client_message_id,
        "surface_id": surface_id,
        "intent": "send",
        "disposition": "queued" if ports.runtime is not None else "applied",
        "revision": 1,
        **(
            {"meta_control": durable_meta_control_payload}
            if durable_meta_control_payload is not None
            else {}
        ),
        "sandbox_mode_resolution": {
            "desiredMode": mode_resolution.desired_mode.value,
            "effectiveMode": mode_resolution.effective_mode.value,
            "fallbackReason": mode_resolution.fallback_reason,
            "confirmationRequired": mode_resolution.confirmation_required,
        },
    }
    fresh_user_session = False
    user_message_id: str | None = None

    def _turn_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(payload)
        enriched.setdefault("session_id", session_id)
        if not enriched.get("turn_id"):
            enriched["turn_id"] = turn_id
        enriched.setdefault("client_message_id", client_message_id)
        if user_message_id:
            enriched.setdefault("user_message_id", user_message_id)
        enriched.setdefault("surface_id", surface_id)
        return enriched

    async def _run_direct_turn() -> None:
        await ports.run_direct_turn(
            prepared_route,
            route_envelope=route_envelope,
            session_id=session_id,
            provider_message=provider_message_text,
            semantic_message=semantic_message_text,
            attachments=raw_attachments,
            session_intent=session_intent,
            run_kind=run_kind,
            no_memory_capture=bool(capture_controls["no_memory_capture"]),
            fresh_user_session=fresh_user_session,
            user_message_id=user_message_id,
            turn_context=ingress_turn_context,
        )

    task_runtime = task_runtime_candidate
    requested_mode = command.queue_mode or getattr(session, "queue_mode", None) or "followup"
    if requested_mode == "steer":
        log.info(
            "sessions.send.legacy_steer_queue_mode_used",
            session_key=key,
            deprecated=True,
            runtime_mode="interrupt",
            replacement="sessions.steer.v2",
        )
        ports.steer_metric("legacy_interrupt_requested", session_key=key)
    runtime_mode = "interrupt" if requested_mode == "steer" else requested_mode
    if prompt_annotation_ids or document_context_request is not None:
        # One accepted annotation batch owns one distinct turn and one
        # ChangeSet. It must never be merged into or interrupt another turn.
        runtime_mode = "followup"
    if durable_meta_control is not None:
        # A control must begin a fresh pipeline turn and must not interrupt
        # another accepted control. Collect could lose the pipeline marker;
        # steer/interrupt could make recovered controls cancel one another.
        runtime_mode = "followup"
    if atomic_intent_plan is not None and atomic_intent_plan.action == "reset":
        # A reset rotates the session identity. Any old-key task must be stopped
        # only after that rotation commits so it cannot append into the new epoch.
        runtime_mode = "interrupt"
    atomic_runtime_acceptance = (
        supports_task_runtime_activation
        and task_runtime is not None
        and atomic_intent_plan is not None
        and callable(getattr(task_runtime, "collect_admission", None))
        and (
            runtime_mode != "collect"
            or callable(getattr(task_runtime, "try_collect_atomically", None))
        )
    )
    prepared_acceptance = (
        atomic_intent_plan is not None
        and callable(prepare_message)
        and callable(storage.accept_turn if storage.capabilities.atomic_acceptance else None)
    )
    persisted_entry = None
    expected_epoch = 0
    if plan_run is not None and not atomic_runtime_acceptance:
        raise AdmissionUnavailableError(
            "Plan implementation requires atomic TaskRuntime acceptance"
        )
    if (
        initial_collaboration_mode is not None or initial_routing_mode is not None
    ) and not atomic_runtime_acceptance:
        raise AdmissionUnavailableError(
            "Initial session controls require atomic TaskRuntime acceptance"
        )

    if durable_meta_control is not None and not atomic_runtime_acceptance:
        raise AdmissionError(
            "META_CONTROL_DURABILITY_UNAVAILABLE",
            "This MetaSkill control requires durable task ingress; retry after Gateway recovery",
            retryable=True,
            accepted=False,
        )

    if prompt_annotation_ids and not atomic_runtime_acceptance:
        raise AdmissionError(
            "PROMPT_ANNOTATION_DURABILITY_UNAVAILABLE",
            "Prompt annotations require atomic task acceptance; retry after Gateway recovery.",
            retryable=True,
            accepted=False,
        )
    if document_context_request is not None and not atomic_runtime_acceptance:
        raise AdmissionError(
            "DOCUMENT_CONTEXT_DURABILITY_UNAVAILABLE",
            "Document editing requires atomic task acceptance; retry after Gateway recovery.",
            retryable=True,
            accepted=False,
        )

    if pending_input_id is not None and not prepared_acceptance:
        raise AdmissionError(
            "PENDING_DISPATCH_UNAVAILABLE",
            "Durable pending-input dispatch is temporarily unavailable",
            retryable=True,
            accepted=False,
        )

    if prepared_acceptance:
        persist_content = message_text
        if raw_attachments or display_text is not None or prompt_annotation_snapshots:
            if raw_attachments and hasattr(ports.sessions, "stamp_user_text"):
                stamped = session_manager.stamp_user_text(message_text)
                if isinstance(stamped, str):
                    message_text = stamped
            persist_content, _writes = ports.transcript_content(
                text=message_text,
                display_text=display_text,
                attachments=raw_attachments,
                session_id=session_id,
                media_root=media_root,
                persist_enabled=persist_enabled,
                disk_budget_bytes=disk_budget if isinstance(disk_budget, int) else None,
                prompt_annotations=prompt_annotation_snapshots,
            )

        assert callable(prepare_message)
        persisted_entry, expected_epoch = await prepare_message(
            key,
            role="user",
            content=persist_content,
            turn_context=ingress_turn_context,
            session_node=session,
        )
        if (
            not raw_attachments
            and display_text is None
            and not prompt_annotation_snapshots
            and isinstance(persisted_entry.content, str)
        ):
            message_text = persisted_entry.content

    async def _accept_turn_with_fork_title(commit: AdmissionCommit) -> AdmissionAcceptance:
        """Persist a prefix edit and its numbered title in one allocation window."""

        if atomic_intent_plan is None or atomic_intent_plan.action != "fork":
            return await storage.accept_turn(commit)
        title_parent = atomic_intent_plan.previous_node
        if title_parent is None:
            raise RuntimeError("Fork acceptance is missing its parent session")
        async with ports.fork_title_allocation(storage, title_parent):
            atomic_intent_plan.node.display_name = await ports.next_fork_title(
                storage,
                title_parent,
            )
            return await storage.accept_turn(commit)

    if atomic_runtime_acceptance:
        assert task_runtime is not None
        assert atomic_intent_plan is not None
        assert persisted_entry is not None
        atomic_task_runtime = task_runtime

        meta_launch_promotion: str | None = None

        async def _accept_task_record(
            task_record: AdmissionTaskRecord,
            *,
            merge_into_task: bool = False,
        ) -> AdmissionAcceptance:
            nonlocal meta_launch_promotion
            reset_archive_writer = None
            if atomic_intent_plan.action == "reset":
                write_session_archive = (
                    session_manager.write_session_archive
                    if session_manager.capabilities.archive
                    else None
                )
                if not callable(write_session_archive):
                    raise RuntimeError("Reset requires durable session archive support")

                async def reset_archive_writer(snapshot: AdmissionArchive) -> None:
                    await write_session_archive(
                        snapshot.node,
                        list(snapshot.entries),
                        list(snapshot.summaries),
                    )

            accepted_plan_run = (
                storage.bind_plan_run_task(plan_run, task_record.task_id)
                if plan_run is not None
                else None
            )
            accepted_session_updates: AdmissionSessionChanges = {}
            if accepted_run_mode_origin is not None:
                accepted_session_updates["origin"] = accepted_run_mode_origin
            if plan_run is not None:
                accepted_session_updates["collaboration_mode"] = "default"
                # Current-session implementation validates the selected active
                # revision through acceptance CAS; it must never write an old
                # pointer back. A copied new-session revision selects itself
                # atomically when it is created.
            elif initial_collaboration_mode == "plan":
                accepted_session_updates["collaboration_mode"] = "plan"
            elif atomic_collaboration_mode_update:
                assert required_collaboration_mode is not None
                accepted_session_updates["collaboration_mode"] = required_collaboration_mode
            acceptance = await _accept_turn_with_fork_title(
                AdmissionCommit(
                    entry=persisted_entry,
                    expected_epoch=expected_epoch,
                    updated_at=int(time.time() * 1000),
                    task_record=task_record,
                    source_scope=ingress_identity.source_scope,
                    request_session_key=ingress_identity.request_session_key,
                    client_request_id=ingress_identity.client_request_id,
                    request_fingerprint=ingress_identity.request_fingerprint,
                    session_node=(
                        atomic_intent_plan.node
                        if atomic_intent_plan.action in {"create", "reset", "fork"}
                        else None
                    ),
                    reset_from_session_id=(
                        atomic_intent_plan.previous_session_id
                        if atomic_intent_plan.action == "reset"
                        else None
                    ),
                    reset_archive_writer=reset_archive_writer,
                    initial_transcript_entries=(
                        atomic_intent_plan.initial_transcript_entries
                        if atomic_intent_plan.action == "fork"
                        else ()
                    ),
                    session_updates=accepted_session_updates or None,
                    plan_revision=plan_revision_to_create,
                    # Associate the task while the run is still queued.  The UI
                    # remains gated by ``status == running``, but cancellation can
                    # now stop a queued implementation before it begins.
                    plan_run=accepted_plan_run,
                    merge_into_task=merge_into_task,
                    meta_control_intent_id=(
                        durable_meta_control.intent_id if durable_meta_control is not None else None
                    ),
                    workspace_guard=workspace_guard,
                    expected_collaboration_revision=expected_collaboration_revision,
                    expected_active_plan_revision_id=expected_active_plan_revision_id,
                    require_idle_for_current_plan_implementation=(
                        require_idle_for_current_plan_implementation
                    ),
                    claim_current_goal=claim_current_goal,
                    prepared_prompt_annotation_targets=prepared_prompt_annotation_targets,
                    prompt_annotation_turn_id=(turn_id if prompt_annotation_rows else None),
                    pending_input_id=pending_input_id,
                    pending_input_fingerprint=pending_input_fingerprint,
                    pending_input_revision=pending_input_revision,
                )
            )
            if not acceptance.replayed and not merge_into_task:
                # This synchronous in-memory transition sits strictly after
                # the durable commit and before reserve activation, so the
                # turn can never execute while its exact marker is still
                # expirable staging state. A prompt merged into an older
                # collect task is not a distinct matching launch turn.
                meta_launch_promotion = _promote_pending_meta_launch()
            return acceptance

        async def _commit_and_activate() -> AdmissionAcceptance:
            if runtime_mode == "collect" and atomic_intent_plan.action == "continue":

                async def _persist_collection(
                    handle: Any,
                    details: dict[str, Any],
                ) -> AdmissionAcceptance:
                    collected_context = {
                        **ingress_turn_context,
                        "turn_id": handle.task_id,
                        "target_turn_id": handle.task_id,
                        "revision": max(
                            2,
                            ports.positive_int(
                                ingress_turn_context.get("revision"),
                                default=1,
                            )
                            + 1,
                        ),
                    }
                    persisted_entry.turn_context = collected_context
                    task_record = storage.create_collection_task(
                        task_id=handle.task_id,
                        session_key=handle.session_key,
                        agent_id=route_envelope.agent_id,
                        source_kind=route_envelope.source_kind.value,
                        run_kind=run_kind,
                        details=details,
                    )
                    return await _accept_task_record(
                        task_record,
                        merge_into_task=True,
                    )

                collected = await ports.try_collect_atomically(
                    atomic_task_runtime,
                    envelope=route_envelope,
                    message=provider_message_text,
                    attachments=raw_attachments,
                    run_kind=run_kind,
                    no_memory_capture=bool(capture_controls["no_memory_capture"]),
                    semantic_message=semantic_message_text,
                    persisted_user_message_id=persisted_entry.message_id,
                    message_count=1,
                    accepted_run_mode_override=accepted_run_mode_override,
                    persist=_persist_collection,
                )
                if collected is not None:
                    _handle, collected_acceptance = collected
                    return collected_acceptance

            async def _freeze(reservation: AdmissionReservation) -> None:
                await ports.freeze_acceptance(
                    atomic_task_runtime,
                    reservation,
                    session_node=(
                        atomic_intent_plan.node
                        if atomic_intent_plan.action in {"create", "fork"}
                        else None
                    ),
                )

            def _before_activate(_acceptance: AdmissionAcceptance) -> None:
                if (
                    atomic_intent_plan.action == "reset"
                    and session_manager.capabilities.cache_epoch
                ):
                    session_manager.set_cached_epoch(key, expected_epoch)

            def _on_unactivated() -> None:
                if meta_launch_promotion == "promoted":
                    ports.cancel_accepted_meta_launch(
                        key, client_request_id=ingress_identity.client_request_id
                    )

            outcome = await commit_reserved_turn(
                runtime=atomic_task_runtime,
                storage=storage,
                reserve=partial(
                    ports.reserve_turn,
                    atomic_task_runtime,
                    route_envelope,
                    provider_message_text,
                    attachments=raw_attachments,
                    mode=runtime_mode,
                    run_kind=run_kind,
                    no_memory_capture=bool(capture_controls["no_memory_capture"]),
                    semantic_message=semantic_message_text,
                    turn_id=turn_id,
                    accepted_run_mode_override=accepted_run_mode_override,
                ),
                freeze=_freeze,
                commit=_accept_task_record,
                before_activate=_before_activate,
                on_unactivated=_on_unactivated,
                compensate_goal=getattr(
                    getattr(atomic_task_runtime, "goal_service", None),
                    "compensate_activation_failure", None,
                ),
            )
            return (
                storage.with_task_status(outcome.acceptance, outcome.task_status)
                if outcome.activation_failed
                else outcome.acceptance
            )

        async def _commit_with_session_admission() -> AdmissionAcceptance:
            # Serialize the full durable commit -> runtime activation boundary
            # for every queue mode. In particular, a reset/interrupt must not
            # overtake a committed-but-inert continue reservation: interrupt
            # activation can only cancel tasks that have crossed activation.
            async with ports.collect_admission(atomic_task_runtime, route_envelope.session_key):
                return await _commit_and_activate()

        try:
            acceptance = await complete_durable_ingress(_commit_with_session_admission())
        except (
            AdmissionAnnotationConflictError,
            AdmissionAnnotationNotFoundError,
        ) as exc:
            _consumed_file_uuids = []
            _cleanup_rejected_guest_profile()
            if prompt_annotation_ids and _prompt_annotation_acceptance_retries > 0:
                log.info(
                    "prompt_annotations.accept_head_race_retry",
                    session_key=key,
                    attempts_remaining=_prompt_annotation_acceptance_retries,
                )
                await ports.release_untransferred_authorities()
                return cast(
                    AdmitTurnResult,
                    await _accept_turn(
                        command,
                        ports,
                        plan_revision_id=plan_revision_id,
                        plan_context_revision_id=plan_context_revision_id,
                        plan_run_driver_kind=plan_run_driver_kind,
                        plan_run_driver_id=plan_run_driver_id,
                        required_collaboration_mode=required_collaboration_mode,
                        required_collaboration_revision=required_collaboration_revision,
                        initial_collaboration_mode=initial_collaboration_mode,
                        expected_collaboration_revision=expected_collaboration_revision,
                        expected_active_plan_revision_id=expected_active_plan_revision_id,
                        require_idle_for_current_plan_implementation=(
                            require_idle_for_current_plan_implementation
                        ),
                        atomic_collaboration_mode_update=atomic_collaboration_mode_update,
                        pending_input_id=pending_input_id,
                        pending_input_fingerprint=pending_input_fingerprint,
                        pending_input_revision=pending_input_revision,
                        _prompt_annotation_acceptance_retries=(
                            _prompt_annotation_acceptance_retries - 1
                        ),
                    ),
                )
            raise ports.artifact_error(
                "document_changed",
                exc,
                operation="prompt_annotations.accept",
                retryable=True,
                session_key=key,
            ) from exc
        except AdmissionAnnotationValidationError as exc:
            _consumed_file_uuids = []
            _cleanup_rejected_guest_profile()
            raise ports.artifact_error(
                "annotation_unavailable",
                exc,
                operation="prompt_annotations.accept",
                retryable=False,
                session_key=key,
            ) from exc
        except AdmissionShuttingDownError as exc:
            _cleanup_rejected_guest_profile()
            raise AdmissionError(
                "UNAVAILABLE",
                "The Gateway is shutting down. Retry after it restarts.",
                details={"session_key": exc.session_key},
                retryable=True,
                accepted=False,
            ) from exc
        except AdmissionQueueFullError as exc:
            _cleanup_rejected_guest_profile()
            raise AdmissionError(
                "QUEUE_FULL",
                "The session task queue is full. Try again after queued work completes.",
                details={
                    "session_key": exc.session_key,
                    "max_pending": exc.max_pending,
                },
                retryable=True,
                accepted=False,
            ) from exc
        except AdmissionStorageBusyError as exc:
            _cleanup_rejected_guest_profile()
            raise AdmissionError(
                "STORAGE_BUSY",
                "Session storage is temporarily busy. Retry this send.",
                details={
                    "operation": exc.operation,
                    "waited_ms": exc.waited_ms,
                },
                retryable=True,
                retry_after_ms=exc.retry_after_ms,
                accepted=False,
            ) from exc
        except AdmissionStaleEpochError as exc:
            _consumed_file_uuids = []
            _cleanup_rejected_guest_profile()
            raise AdmissionError(
                "SESSION_CHANGED",
                "The session changed while this turn was being accepted. Retry the send.",
                retryable=True,
                accepted=False,
            ) from exc
        except AdmissionIngressConflictError as exc:
            _consumed_file_uuids = []
            _cleanup_rejected_guest_profile()
            raise AdmissionError(
                "IDEMPOTENCY_CONFLICT",
                str(exc),
                retryable=False,
                accepted=False,
            ) from exc
        except AdmissionMetaControlConflictError as exc:
            _consumed_file_uuids = []
            raise AdmissionError(
                "META_CONTROL_CONFLICT",
                str(exc),
                retryable=False,
                accepted=False,
            ) from exc
        except ProjectWorkspaceStateError as exc:
            _consumed_file_uuids = []
            _cleanup_rejected_guest_profile()
            raise _project_workspace_error(exc) from exc
        except AdmissionTaskCollectionUnavailableError as exc:
            _consumed_file_uuids = []
            _cleanup_rejected_guest_profile()
            raise AdmissionError(
                "COLLECT_RACE",
                "The queued task started before this message could be collected. Retry it.",
                retryable=True,
                accepted=False,
            ) from exc
        except AdmissionPlanSessionBusyError as exc:
            _consumed_file_uuids = []
            log.info(
                "plan_implementation.admission_rejected",
                session_key=key,
                reason="session_busy",
                task_id=exc.task_id,
                task_status=exc.task_status,
            )
            raise AdmissionError(
                "PLAN_IMPLEMENTATION_SESSION_BUSY",
                "Current-session plan implementation requires an idle session.",
                details={
                    "turnId": exc.task_id,
                    "taskStatus": exc.task_status,
                },
                retryable=True,
                accepted=False,
            ) from exc
        except AdmissionPlanConflictError as exc:
            _consumed_file_uuids = []
            latest = await storage.get_session(key)
            if (
                expected_active_plan_revision_id is not None
                and latest is not None
                and getattr(latest, "active_plan_revision_id") != expected_active_plan_revision_id
            ):
                log.info(
                    "plan_implementation.admission_rejected",
                    session_key=key,
                    reason="plan_revision_changed",
                )
                raise AdmissionError(
                    "PLAN_REVISION_CHANGED",
                    "The selected plan is no longer the current revision.",
                    details={"collaboration": ports.collaboration_snapshot(latest)},
                    retryable=False,
                    accepted=False,
                ) from exc
            if (
                expected_collaboration_revision is not None
                and latest is not None
                and int(getattr(latest, "collaboration_revision") or 0)
                != expected_collaboration_revision
            ):
                log.info(
                    "plan_implementation.admission_rejected",
                    session_key=key,
                    reason="collaboration_changed",
                )
                raise AdmissionError(
                    "COLLABORATION_CHANGED",
                    "The collaboration state changed before the turn was accepted.",
                    details={"collaboration": ports.collaboration_snapshot(latest)},
                    retryable=True,
                    accepted=False,
                ) from exc
            active_run = await storage.get_active_plan_run(key)
            if active_run is not None and active_run.status in {"queued", "running"}:
                log.info(
                    "plan_implementation.admission_rejected",
                    session_key=key,
                    reason="plan_run_active",
                    plan_run_id=active_run.run_id,
                    plan_run_status=active_run.status,
                )
                raise AdmissionError(
                    "PLAN_RUN_ACTIVE",
                    "This plan already has an implementation task in progress.",
                    details={"runId": active_run.run_id, "status": active_run.status},
                    retryable=False,
                    accepted=False,
                ) from exc
            log.info(
                "plan_implementation.admission_rejected",
                session_key=key,
                reason="plan_run_changed",
            )
            raise AdmissionError(
                "PLAN_RUN_CHANGED",
                "The plan execution state changed before acceptance. Refresh and retry.",
                retryable=True,
                accepted=False,
            ) from exc
        except sqlite3.IntegrityError as exc:
            if atomic_intent_plan.action != "create" or "sessions.session_key" not in str(exc):
                _cleanup_rejected_guest_profile()
                raise
            _consumed_file_uuids = []
            _cleanup_rejected_guest_profile()
            raise AdmissionError(
                "SESSION_CONFLICT",
                "Another request created this session first. Start a new chat and retry.",
                retryable=False,
                accepted=False,
            ) from exc
        except BaseException:
            _cleanup_rejected_guest_profile()
            raise

        goal_service = getattr(atomic_task_runtime, "goal_service", None)
        if not acceptance.replayed and goal_service is not None:
            if atomic_intent_plan.action == "reset":
                revoke_goal_lease = getattr(goal_service, "revoke_session", None)
                if callable(revoke_goal_lease):
                    revoke_goal_lease(key)
            collaboration_changed = any(
                (
                    initial_collaboration_mode is not None,
                    atomic_collaboration_mode_update,
                    plan_run is not None,
                )
            )
            on_mode_committed = getattr(goal_service, "on_mode_committed", None)
            if collaboration_changed and callable(on_mode_committed):
                try:
                    await on_mode_committed(
                        key,
                        str(acceptance.collaboration_mode or "default"),
                    )
                except Exception:  # noqa: BLE001 - turn acceptance is authoritative.
                    log.warning(
                        "sessions.send.goal_mode_hook_failed",
                        session_key=key,
                        exc_info=True,
                    )

        if not acceptance.replayed:
            notify_message_appended = (
                session_manager.notify_message_appended
                if session_manager.capabilities.notify
                else None
            )
            if callable(notify_message_appended):
                try:
                    notify_message_appended(persisted_entry)
                except Exception:  # noqa: BLE001 - turn is already accepted.
                    log.exception(
                        "sessions.send.post_accept_notify_failed",
                        session_key=key,
                        task_id=acceptance.receipt.task_id,
                    )
            reset_archive = acceptance.reset_archive_snapshot
            if reset_archive is not None:
                write_session_archive = (
                    session_manager.write_session_archive
                    if session_manager.capabilities.archive
                    else None
                )
                if callable(write_session_archive):
                    try:
                        await write_session_archive(
                            reset_archive.node,
                            list(reset_archive.entries),
                            list(reset_archive.summaries),
                        )
                    except Exception:  # noqa: BLE001 - turn is already accepted.
                        log.exception(
                            "sessions.send.post_accept_archive_failed",
                            session_key=key,
                            task_id=acceptance.receipt.task_id,
                        )
            if (
                atomic_intent_plan.action == "fork"
                and atomic_intent_plan.previous_session_id is not None
            ):
                copy_fork_materials = (
                    session_manager.copy_fork_materials
                    if session_manager.capabilities.fork_materials
                    else None
                )
                if callable(copy_fork_materials):
                    try:
                        await copy_fork_materials(
                            atomic_intent_plan.previous_session_id,
                            session_id,
                            key,
                        )
                    except Exception:  # noqa: BLE001 - turn is already accepted.
                        log.exception(
                            "sessions.send.post_accept_fork_copy_failed",
                            session_key=key,
                            task_id=acceptance.receipt.task_id,
                        )
                try:
                    await ports.publish_forked(key)
                except Exception:  # noqa: BLE001 - turn is already accepted.
                    log.exception(
                        "sessions.send.post_accept_fork_event_failed",
                        session_key=key,
                        task_id=acceptance.receipt.task_id,
                    )

        if _consumed_file_uuids:
            upload_store = ports.uploads
            for file_uuid in _consumed_file_uuids:
                try:
                    await upload_store.evict(file_uuid)
                except Exception:  # noqa: BLE001 - eviction is best-effort
                    log.warning("uploads.evict_failed_post_turn uuid=%s", file_uuid[:8])
        if not acceptance.replayed and generate_title:
            try:
                ports.schedule_auto_title(
                    key,
                    semantic_message_text or message_text,
                    enabled=generate_title,
                    session_id=session_id,
                    root_turn_id=acceptance.receipt.task_id,
                )
            except Exception:  # noqa: BLE001 - turn is already accepted.
                log.exception(
                    "sessions.send.post_accept_title_failed",
                    session_key=key,
                    task_id=acceptance.receipt.task_id,
                )
        response = await ports.accepted_response(
            acceptance,
            client_request_id=ingress_identity.client_request_id,
            storage=storage,
            turn_context=(persisted_entry.turn_context if not acceptance.replayed else None),
            accepted_prompt_annotation_ids=prompt_annotation_ids,
        )
        if initial_collaboration_mode is not None:
            accepted_collaboration: AcceptedCollaboration = {
                "mode": initial_collaboration_mode,
                "revision": required_collaboration_revision or 0,
            }
            response["acceptedCollaboration"] = accepted_collaboration
            current_session = await storage.get_session(key)
            if current_session is not None:
                response["collaboration"] = ports.collaboration_snapshot(current_session)
            if not acceptance.replayed:
                try:
                    await ports.publish_collaboration(key, accepted_collaboration)
                except Exception:  # noqa: BLE001 - turn is already accepted.
                    log.exception(
                        "sessions.send.initial_collaboration_emit_failed",
                        session_key=key,
                    )
        if initial_routing_mode is not None:
            response["acceptedRouting"] = {"mode": initial_routing_mode}
            response["routing"] = await ports.routing_snapshot(key)
        return response

    if prepared_acceptance:
        assert atomic_intent_plan is not None
        assert persisted_entry is not None
        direct_registry = ports.direct_registry

        async def _commit_and_schedule_direct() -> AdmissionAcceptance:
            nonlocal fresh_user_session, user_message_id
            acceptance = await _accept_turn_with_fork_title(
                AdmissionCommit(
                    entry=persisted_entry,
                    expected_epoch=expected_epoch,
                    updated_at=int(time.time() * 1000),
                    task_record=None,
                    source_scope=ingress_identity.source_scope,
                    request_session_key=ingress_identity.request_session_key,
                    client_request_id=ingress_identity.client_request_id,
                    request_fingerprint=ingress_identity.request_fingerprint,
                    session_node=(
                        atomic_intent_plan.node
                        if atomic_intent_plan.action in {"create", "reset", "fork"}
                        else None
                    ),
                    reset_from_session_id=(
                        atomic_intent_plan.previous_session_id
                        if atomic_intent_plan.action == "reset"
                        else None
                    ),
                    initial_transcript_entries=(
                        atomic_intent_plan.initial_transcript_entries
                        if atomic_intent_plan.action == "fork"
                        else ()
                    ),
                    session_updates=(
                        {"origin": accepted_run_mode_origin}
                        if accepted_run_mode_origin is not None
                        else None
                    ),
                    workspace_guard=workspace_guard,
                    pending_input_id=pending_input_id,
                    pending_input_fingerprint=pending_input_fingerprint,
                    pending_input_revision=pending_input_revision,
                )
            )
            if acceptance.replayed:
                return acceptance
            fresh_user_session = acceptance.fresh_user_session
            user_message_id = acceptance.receipt.message_id
            if atomic_intent_plan.action == "reset":
                set_cached_epoch = (
                    session_manager.set_cached_epoch
                    if session_manager.capabilities.cache_epoch
                    else None
                )
                if callable(set_cached_epoch):
                    set_cached_epoch(key, expected_epoch)
            task = asyncio.create_task(_run_direct_turn())
            setattr(task, "_opensquilla_started", False)
            setattr(task, "_opensquilla_terminal_emitted", False)
            turn_authority = ports.turn_authority(route_envelope)
            try:
                direct_registry.register(
                    key,
                    task,
                    terminal_cleanup=(
                        turn_authority.aclose if turn_authority is not None else None
                    ),
                )
            except BaseException:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            if turn_authority is not None:
                turn_authority.handoff()
            return acceptance

        try:
            async with direct_registry.admission(key):
                acceptance = await complete_durable_ingress(_commit_and_schedule_direct())
        except AdmissionStorageBusyError as exc:
            _consumed_file_uuids = []
            raise AdmissionError(
                "STORAGE_BUSY",
                "Session storage is temporarily busy. Retry this send.",
                details={
                    "operation": exc.operation,
                    "waited_ms": exc.waited_ms,
                },
                retryable=True,
                retry_after_ms=exc.retry_after_ms,
                accepted=False,
            ) from exc
        except AdmissionStaleEpochError as exc:
            _consumed_file_uuids = []
            raise AdmissionError(
                "SESSION_CHANGED",
                "The session changed while this turn was being accepted. Retry the send.",
                retryable=True,
                accepted=False,
            ) from exc
        except AdmissionIngressConflictError as exc:
            _consumed_file_uuids = []
            raise AdmissionError(
                "IDEMPOTENCY_CONFLICT",
                str(exc),
                retryable=False,
                accepted=False,
            ) from exc
        except ProjectWorkspaceStateError as exc:
            _consumed_file_uuids = []
            raise _project_workspace_error(exc) from exc
        except sqlite3.IntegrityError as exc:
            if atomic_intent_plan.action != "create" or "sessions.session_key" not in str(exc):
                raise
            _consumed_file_uuids = []
            raise AdmissionError(
                "SESSION_CONFLICT",
                "Another request created this session first. Start a new chat and retry.",
                retryable=False,
                accepted=False,
            ) from exc

        if not acceptance.replayed:
            notify_message_appended = (
                session_manager.notify_message_appended
                if session_manager.capabilities.notify
                else None
            )
            if callable(notify_message_appended):
                try:
                    notify_message_appended(persisted_entry)
                except Exception:  # noqa: BLE001 - turn is already accepted.
                    log.exception(
                        "sessions.send.post_accept_notify_failed",
                        session_key=key,
                    )
            reset_archive = acceptance.reset_archive_snapshot
            if reset_archive is not None:
                write_session_archive = (
                    session_manager.write_session_archive
                    if session_manager.capabilities.archive
                    else None
                )
                if callable(write_session_archive):
                    try:
                        await write_session_archive(
                            reset_archive.node,
                            list(reset_archive.entries),
                            list(reset_archive.summaries),
                        )
                    except Exception:  # noqa: BLE001 - turn is already accepted.
                        log.exception(
                            "sessions.send.post_accept_archive_failed",
                            session_key=key,
                        )
            if (
                atomic_intent_plan.action == "fork"
                and atomic_intent_plan.previous_session_id is not None
            ):
                copy_fork_materials = (
                    session_manager.copy_fork_materials
                    if session_manager.capabilities.fork_materials
                    else None
                )
                if callable(copy_fork_materials):
                    try:
                        await copy_fork_materials(
                            atomic_intent_plan.previous_session_id,
                            session_id,
                            key,
                        )
                    except Exception:  # noqa: BLE001 - turn is already accepted.
                        log.exception(
                            "sessions.send.post_accept_fork_copy_failed",
                            session_key=key,
                        )
                try:
                    await ports.publish_forked(key)
                except Exception:  # noqa: BLE001 - turn is already accepted.
                    log.exception(
                        "sessions.send.post_accept_fork_event_failed",
                        session_key=key,
                    )
            await ports.publish_disposition(
                key,
                {
                    "session_key": key,
                    "user_message_id": user_message_id,
                    **ingress_turn_context,
                },
            )
            if _consumed_file_uuids:
                upload_store = ports.uploads
                for file_uuid in _consumed_file_uuids:
                    try:
                        await upload_store.evict(file_uuid)
                    except Exception:  # noqa: BLE001 - eviction is best-effort
                        log.warning(
                            "uploads.evict_failed_post_turn uuid=%s",
                            file_uuid[:8],
                        )
            if generate_title:
                try:
                    ports.schedule_auto_title(
                        key,
                        semantic_message_text or message_text,
                        enabled=generate_title,
                    )
                except Exception:  # noqa: BLE001 - turn is already accepted.
                    log.exception(
                        "sessions.send.post_accept_title_failed",
                        session_key=key,
                    )
        return await ports.accepted_response(
            acceptance,
            client_request_id=ingress_identity.client_request_id,
            storage=storage,
            turn_context=(persisted_entry.turn_context if not acceptance.replayed else None),
            accepted_prompt_annotation_ids=prompt_annotation_ids,
        )

    # 1. Persist user message to transcript (include attachment metadata).
    # Hold the per-session lock used by /reset so a concurrent reset cannot
    # tear the append and leak an orphan user turn into the cleared transcript.
    _persist_lock = ports.session_lock(key)
    legacy_persisted_entry: Any = None
    fresh_user_session = False

    async def _persist_user_message() -> None:
        nonlocal message_text, legacy_persisted_entry, fresh_user_session

        get_transcript = (
            session_manager.has_transcript if session_manager.capabilities.transcript else None
        )
        if callable(get_transcript):
            fresh_user_session = not bool(await get_transcript(key))
        if raw_attachments or display_text is not None:
            # Stamp up-front so both the stored envelope and the LLM path agree.
            if raw_attachments and hasattr(ports.sessions, "stamp_user_text"):
                _stamped = session_manager.stamp_user_text(message_text)
                if isinstance(_stamped, str):
                    message_text = _stamped

            persist_content, _writes = ports.transcript_content(
                text=message_text,
                display_text=display_text,
                attachments=raw_attachments,
                session_id=session_id,
                media_root=media_root,
                persist_enabled=persist_enabled,
                disk_budget_bytes=disk_budget if isinstance(disk_budget, int) else None,
            )
            legacy_persisted_entry = await session_manager.append_message(
                key,
                role="user",
                content=persist_content,
                turn_context=ingress_turn_context,
            )
        else:
            legacy_persisted_entry = await session_manager.append_message(
                key,
                role="user",
                content=message_text,
                turn_context=ingress_turn_context,
            )
            if legacy_persisted_entry is not None and isinstance(
                legacy_persisted_entry.content, str
            ):
                message_text = legacy_persisted_entry.content

    async def _persist_user_message_with_lock() -> None:
        if _persist_lock is None:
            await _persist_user_message()
        else:
            async with _persist_lock:
                await _persist_user_message()

    # Compatibility managers without atomic acceptance still persist the user
    # row before runtime enqueue. Promote now, while no task has been admitted,
    # and restage if a clean queue rejection rolls the row back below.
    legacy_meta_launch_promotion = _promote_pending_meta_launch()

    task_runtime = task_runtime_candidate
    if task_runtime is None:
        direct_registry = ports.direct_registry
        async with direct_registry.admission(key):
            await _persist_user_message_with_lock()
            user_message_id = getattr(legacy_persisted_entry, "message_id", None)
            task = asyncio.create_task(_run_direct_turn())
            setattr(task, "_opensquilla_started", False)
            setattr(task, "_opensquilla_terminal_emitted", False)
            turn_authority = ports.turn_authority(route_envelope)
            try:
                direct_registry.register(
                    key,
                    task,
                    terminal_cleanup=(
                        turn_authority.aclose if turn_authority is not None else None
                    ),
                )
            except BaseException:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            if turn_authority is not None:
                turn_authority.handoff()

        await ports.publish_disposition(
            key,
            {
                "session_key": key,
                "user_message_id": user_message_id,
                **ingress_turn_context,
            },
        )
        # Same eviction semantic as the task_runtime success path: the turn was
        # accepted into a background TurnRunner task, so consumed uuids can be
        # evicted from the upload store rather than waiting out the TTL window.
        if _consumed_file_uuids:
            _store = ports.uploads
            for _u in _consumed_file_uuids:
                try:
                    await _store.evict(_u)
                except Exception:  # noqa: BLE001 — eviction is best-effort
                    log.warning("uploads.evict_failed_post_turn uuid=%s", _u[:8])
        if generate_title:
            ports.schedule_auto_title(
                key,
                semantic_message_text or message_text,
                enabled=generate_title,
            )
        return {
            "status": "accepted",
            "key": key,
            "session_key": key,
            "session_id": session_id,
            "turn_id": turn_id,
            "client_message_id": client_message_id,
            "user_message_id": user_message_id,
            "surface_id": surface_id,
        }

    await _persist_user_message_with_lock()
    user_message_id = getattr(legacy_persisted_entry, "message_id", None)

    async def _rollback_persisted_user_message(reason: str) -> tuple[str | None, bool]:
        message_id = getattr(legacy_persisted_entry, "message_id", None)
        if not message_id or not session_manager.capabilities.remove_message:
            return message_id, False
        try:
            if _persist_lock is None:
                removed = await session_manager.remove_message(key, message_id)
            else:
                async with _persist_lock:
                    removed = await session_manager.remove_message(key, message_id)
        except Exception as rb_exc:  # noqa: BLE001 — rollback is best-effort
            log.warning(
                "sessions.send.rollback_failed",
                session_key=key,
                message_id=message_id,
                reason=reason,
                error=str(rb_exc),
            )
            return message_id, False
        if removed:
            log.info(
                "sessions.send.rollback_succeeded",
                session_key=key,
                message_id=message_id,
                reason=reason,
            )
        return message_id, bool(removed)

    if task_runtime is not None:
        requested_mode = command.queue_mode or getattr(session, "queue_mode", None) or "followup"
        runtime_mode = "interrupt" if requested_mode == "steer" else requested_mode
        try:
            handle = await ports.start_turn(
                task_runtime,
                route_envelope,
                provider_message_text,
                attachments=raw_attachments,
                mode=runtime_mode,
                run_kind=run_kind,
                no_memory_capture=bool(capture_controls["no_memory_capture"]),
                semantic_message=semantic_message_text,
                persisted_user_message_id=getattr(legacy_persisted_entry, "message_id", None),
                fresh_user_session=fresh_user_session,
                turn_id=turn_id,
                accepted_run_mode_override=accepted_run_mode_override,
            )
        except Exception as exc:
            # Ensure the uuid eviction does NOT fire on this
            # path. The locked semantic mandates that any rejection /
            # rollback / queue-full leaves the uuid alive until TTL so
            # the user can retry against the same uuid.
            _consumed_file_uuids = []  # noqa: F841 – explicit no-evict marker
            _cleanup_rejected_guest_profile()

            if not isinstance(
                exc,
                (AdmissionQueueFullError, AdmissionShuttingDownError),
            ):
                if legacy_meta_launch_promotion == "promoted":
                    ports.restage_meta_launch(
                        key,
                        client_request_id=ingress_identity.client_request_id,
                    )
                raise

            # Roll back the just-appended user turn so a retry doesn't leave
            # a ghost message in the transcript. If rollback fails (e.g.
            # storage error under load), surface a non-retryable error and
            # hand the orphan message_id to the client as an idempotency
            # token — clients must dedup before retrying.
            shutting_down = isinstance(exc, AdmissionShuttingDownError)
            rollback_reason = "runtime_shutting_down" if shutting_down else "queue_full"
            orphan_id, rollback_ok = await _rollback_persisted_user_message(rollback_reason)

            if rollback_ok:
                if legacy_meta_launch_promotion == "promoted":
                    ports.restage_meta_launch(
                        key,
                        client_request_id=ingress_identity.client_request_id,
                    )
                if shutting_down:
                    raise AdmissionError(
                        "UNAVAILABLE",
                        "The Gateway is shutting down. Retry after it restarts.",
                        details={
                            "session_key": exc.session_key,
                            "rollback_message_id": orphan_id,
                        },
                        retryable=True,
                        accepted=False,
                    ) from exc
                assert isinstance(exc, AdmissionQueueFullError)
                raise AdmissionError(
                    "QUEUE_FULL",
                    "The session task queue is full. Try again after queued work completes.",
                    details={
                        "session_key": exc.session_key,
                        "max_pending": exc.max_pending,
                        "rollback_message_id": orphan_id,
                    },
                    retryable=True,
                    accepted=False,
                ) from exc
            if legacy_meta_launch_promotion == "promoted":
                ports.cancel_accepted_meta_launch(
                    key,
                    client_request_id=ingress_identity.client_request_id,
                )
            if shutting_down:
                raise AdmissionError(
                    "UNAVAILABLE",
                    (
                        "The Gateway is shutting down and the accepted transcript "
                        "entry could not be rolled back."
                    ),
                    details={
                        "session_key": exc.session_key,
                        "orphan_message_id": orphan_id,
                        "remediation": "client must dedup by message_id before retry",
                    },
                    retryable=False,
                    accepted=True,
                ) from exc
            assert isinstance(exc, AdmissionQueueFullError)
            raise AdmissionError(
                "QUEUE_FULL_DIRTY",
                (
                    "The session task queue is full and the just-appended user "
                    "turn could not be rolled back. The transcript now contains "
                    "an orphan message; clients must dedup by orphan_message_id "
                    "before retrying."
                ),
                details={
                    "session_key": exc.session_key,
                    "max_pending": exc.max_pending,
                    "orphan_message_id": orphan_id,
                    "remediation": "client must dedup by message_id before retry",
                },
                retryable=False,
                accepted=True,
            ) from exc
        if handle.task_id != turn_id:
            if legacy_meta_launch_promotion == "promoted":
                ports.restage_meta_launch(
                    key,
                    client_request_id=ingress_identity.client_request_id,
                )
            # ``collect`` coalesces this durable prompt into an already queued
            # runtime turn. TaskRuntime has rebound the stored row; project and
            # return that same canonical identity instead of the unused
            # preallocation so live consumers and a later hydrate agree.
            turn_id = handle.task_id
            ingress_turn_context = {
                **ingress_turn_context,
                "turn_id": turn_id,
                "target_turn_id": turn_id,
                "revision": max(
                    2,
                    ports.positive_int(
                        ingress_turn_context.get("revision"),
                        default=1,
                    )
                    + 1,
                ),
            }
        # Eviction hook: turn was accepted into the runtime,
        # post-resolution + post-engine-acceptance. Evict consumed uuids
        # so memory does not linger for the full TTL window. Locked
        # semantic mandates this fires ONLY here on the success path.
        if _consumed_file_uuids:
            _store = ports.uploads
            for _u in _consumed_file_uuids:
                try:
                    await _store.evict(_u)
                except Exception:  # noqa: BLE001 — eviction is best-effort
                    log.warning("uploads.evict_failed_post_turn uuid=%s", _u[:8])
        if generate_title:
            ports.schedule_auto_title(
                key,
                semantic_message_text or message_text,
                enabled=generate_title,
                session_id=session_id,
                root_turn_id=turn_id,
            )
        await ports.publish_disposition(
            key,
            {
                "session_key": key,
                "user_message_id": user_message_id,
                **ingress_turn_context,
            },
        )
        return {
            "status": "accepted",
            "key": key,
            "session_key": key,
            "session_id": session_id,
            "task_id": handle.task_id,
            "turn_id": turn_id,
            "client_message_id": client_message_id,
            "user_message_id": user_message_id,
            "surface_id": surface_id,
        }

    raise AssertionError("unreachable: direct sends return before runtime dispatch")
