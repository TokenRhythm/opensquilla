"""Native input and runtime primitives used by durable turn admission."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal, cast

from opensquilla.application.admission_errors import (
    AdmissionQueueFullError,
    AdmissionShuttingDownError,
)
from opensquilla.application.admission_views import AdmissionSessionIntent, MetaAdmissionControl
from opensquilla.application.turn_admission import AdmitTurn
from opensquilla.engine.start_turn import reserve_turn_via_runtime, start_turn_via_runtime
from opensquilla.engine.steps.meta_command import (
    parse_meta_control_sentinel,
    pending_meta_launch_cancel_accepted,
    pending_meta_launch_peek,
    pending_meta_launch_promote,
    pending_meta_launch_restage,
)
from opensquilla.gateway import attachment_ingest
from opensquilla.gateway.direct_turn_runtime import run_direct_turn
from opensquilla.gateway.input_normalization import (
    NormalizedInput,
    infer_normalized_input_from_attachments,
    materialize_generated_text_attachments,
    normalize_incoming_text,
)
from opensquilla.gateway.rpc import RpcHandlerError
from opensquilla.gateway.session_model_routing import (
    capture_prepared_session_model_routing_config,
)
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.gateway.task_runtime import TaskQueueFullError, TaskRuntimeShuttingDownError
from opensquilla.gateway.transcripts import build_transcript_attachment_envelope
from opensquilla.paths import media_root_from_config
from opensquilla.session.models import SessionIntent

if TYPE_CHECKING:
    from opensquilla.engine.runtime import TurnRunner
    from opensquilla.gateway.admission_preparation import PreparedRuntimeRoute
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.gateway.project_workspace_runtime import AcceptedRunModeOverride
    from opensquilla.gateway.routing import RouteEnvelope
    from opensquilla.gateway.task_runtime import TaskHandle, TaskReservation, TaskRuntime
    from opensquilla.session.manager import SessionManager
    from opensquilla.session.models import SessionNode


@asynccontextmanager
async def _translate_runtime_rejection() -> AsyncIterator[None]:
    try:
        yield
    except TaskQueueFullError as exc:
        raise AdmissionQueueFullError(exc.session_key, exc.max_pending) from exc
    except TaskRuntimeShuttingDownError as exc:
        raise AdmissionShuttingDownError(exc.session_key) from exc


class GatewayAdmissionRuntime:
    """Bind fixed input, reservation, and execution operations to native services."""

    def __init__(
        self,
        *,
        config: GatewayConfig,
        manager: SessionManager | None,
        runtime: TaskRuntime | None,
        runner: TurnRunner | None,
        is_owner: bool,
        host_execute_allowed: bool,
        publish: Callable[[str, str, dict[str, Any]], Awaitable[None]],
        normalize_terminal: Callable[[str, dict[str, Any]], dict[str, Any]],
        session_model: Callable[[SessionNode, str], str | None],
    ) -> None:
        self._runtime_config = config
        self._runtime_manager = manager
        self._runtime_engine = runtime
        self._runtime_runner = runner
        self._runtime_owner = is_owner
        self._runtime_host_execute = host_execute_allowed
        self._runtime_publish = publish
        self._runtime_normalize_terminal = normalize_terminal
        self._runtime_session_model = session_model

    def normalize_input(self, command: AdmitTurn) -> NormalizedInput:
        # Source aliases are decoded once; reconstructing them here would lose
        # the legacy distinction between a missing, null, and empty alias.
        return normalize_incoming_text(
            command.message,
            source_hint={"caller_kind": "web" if command.source.is_web else "cli"},
            attachments=list(command.attachments),
        )

    async def ingest_attachments(
        self,
        message: str,
        attachments: list[dict[str, Any]],
        *,
        session_id: str,
        allow_material_refs: bool,
    ) -> attachment_ingest.AttachmentIngestResult:
        config = getattr(self._runtime_config, "attachments", None)
        disk_budget = getattr(config, "transcript_disk_budget_bytes", None)
        opaque_cap = getattr(config, "opaque_max_bytes", None)
        try:
            return await attachment_ingest.ingest_attachments(
                message,
                attachments,
                failure_mode="raise",
                material_root=media_root_from_config(self._runtime_config),
                session_id=session_id,
                disk_budget_bytes=disk_budget if isinstance(disk_budget, int) else None,
                accept_opaque=bool(getattr(config, "accept_opaque", True)),
                opaque_limit_bytes=opaque_cap if isinstance(opaque_cap, int) else None,
                allow_material_refs=allow_material_refs,
                expected_material_scope=session_id if allow_material_refs else None,
            )
        except attachment_ingest.AttachmentResolutionError as exc:
            raise RpcHandlerError(
                exc.code,
                str(exc),
                details={
                    "attachmentIndex": exc.attachment_index,
                    "fileUuid": exc.file_uuid,
                    "recovery": "reupload" if exc.recoverable else None,
                },
                retryable=exc.recoverable,
            ) from exc

    infer_normalized_input = staticmethod(infer_normalized_input_from_attachments)
    materialize_normalized_attachments = staticmethod(materialize_generated_text_attachments)
    transcript_content = staticmethod(build_transcript_attachment_envelope)

    @staticmethod
    def parse_meta_control(
        message: str,
        semantic_message: str,
        *,
        client_request_id: str,
    ) -> MetaAdmissionControl | None:
        parsed = parse_meta_control_sentinel(
            message, semantic_message, client_request_id=client_request_id
        )
        if parsed is None:
            return None
        return MetaAdmissionControl(
            kind=cast(Literal["manual", "replay"], parsed["kind"]),
            correlation_id=parsed["correlation_id"],
            name=parsed.get("name"),
        )

    @staticmethod
    def peek_meta_launch(key: str, *, client_request_id: str) -> str | None:
        return pending_meta_launch_peek(key, client_request_id=client_request_id)

    @staticmethod
    def promote_meta_launch(
        key: str,
        *,
        client_request_id: str,
        message: str,
        semantic_message: str,
    ) -> Literal["promoted", "accepted"] | None:
        return cast(
            Literal["promoted", "accepted"] | None,
            pending_meta_launch_promote(
                key,
                client_request_id=client_request_id,
                message=message,
                semantic_message=semantic_message,
            ),
        )

    @staticmethod
    def restage_meta_launch(key: str, *, client_request_id: str) -> bool:
        return pending_meta_launch_restage(key, client_request_id=client_request_id)

    @staticmethod
    def cancel_accepted_meta_launch(key: str, *, client_request_id: str) -> bool:
        return pending_meta_launch_cancel_accepted(key, client_request_id=client_request_id)

    @staticmethod
    def refine_route(
        envelope: RouteEnvelope,
        *,
        input_provenance: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RouteEnvelope:
        changes: dict[str, Any] = {}
        if input_provenance is not None:
            changes["input_provenance"] = input_provenance
        if metadata is not None:
            changes["metadata"] = metadata
        return replace(envelope, **changes)

    @staticmethod
    async def reserve_turn(
        runtime: TaskRuntime,
        envelope: RouteEnvelope,
        message: str,
        *,
        attachments: list[dict[str, Any]],
        mode: str,
        run_kind: str,
        no_memory_capture: bool,
        semantic_message: str,
        turn_id: str,
        accepted_run_mode_override: AcceptedRunModeOverride | None,
    ) -> TaskReservation:
        async with _translate_runtime_rejection():
            return await reserve_turn_via_runtime(
                runtime,
                envelope,
                message,
                attachments=attachments,
                mode=mode,
                run_kind=run_kind,
                no_memory_capture=no_memory_capture,
                semantic_message=semantic_message,
                turn_id=turn_id,
                accepted_run_mode_override=accepted_run_mode_override,
            )

    @staticmethod
    async def start_turn(
        runtime: TaskRuntime,
        envelope: RouteEnvelope,
        message: str,
        *,
        attachments: list[dict[str, Any]],
        mode: str,
        run_kind: str,
        no_memory_capture: bool,
        semantic_message: str,
        turn_id: str,
        accepted_run_mode_override: AcceptedRunModeOverride | None,
        persisted_user_message_id: str | None = None,
        fresh_user_session: bool | None = None,
    ) -> TaskHandle:
        async with _translate_runtime_rejection():
            return await start_turn_via_runtime(
                runtime,
                envelope,
                message,
                attachments=attachments,
                mode=mode,
                run_kind=run_kind,
                no_memory_capture=no_memory_capture,
                semantic_message=semantic_message,
                turn_id=turn_id,
                accepted_run_mode_override=accepted_run_mode_override,
                persisted_user_message_id=persisted_user_message_id,
                fresh_user_session=fresh_user_session,
            )

    async def freeze_acceptance(
        self,
        runtime: TaskRuntime,
        reservation: TaskReservation,
        *,
        session_node: SessionNode | None = None,
    ) -> None:
        async with _translate_runtime_rejection():
            if session_node is None:
                await runtime.freeze_acceptance(reservation)
            else:
                await runtime.freeze_acceptance(
                    reservation,
                    accepted_config=capture_prepared_session_model_routing_config(
                        self._runtime_config,
                        session_node,
                    ),
                )

    @staticmethod
    @asynccontextmanager
    async def collect_admission(runtime: TaskRuntime, key: str) -> AsyncIterator[None]:
        async with _translate_runtime_rejection(), runtime.collect_admission(key):
            yield

    @staticmethod
    async def try_collect_atomically[T](
        runtime: TaskRuntime,
        *,
        envelope: RouteEnvelope,
        message: str,
        attachments: list[dict[str, Any]],
        run_kind: str,
        no_memory_capture: bool,
        semantic_message: str,
        persisted_user_message_id: str | None,
        message_count: int,
        accepted_run_mode_override: AcceptedRunModeOverride | None,
        persist: Callable[[TaskHandle, dict[str, Any]], Awaitable[T]],
    ) -> tuple[TaskHandle, T] | None:
        async with _translate_runtime_rejection():
            return await runtime.try_collect_atomically(
                envelope=envelope,
                message=message,
                attachments=attachments,
                run_kind=run_kind,
                no_memory_capture=no_memory_capture,
                semantic_message=semantic_message,
                persisted_user_message_id=persisted_user_message_id,
                message_count=message_count,
                accepted_run_mode_override=accepted_run_mode_override,
                persist=persist,
            )

    async def run_direct_turn(
        self,
        prepared: PreparedRuntimeRoute,
        *,
        route_envelope: RouteEnvelope,
        session_id: str,
        provider_message: str,
        semantic_message: str,
        attachments: list[dict[str, Any]],
        session_intent: AdmissionSessionIntent,
        run_kind: str,
        no_memory_capture: bool,
        fresh_user_session: bool,
        user_message_id: str | None,
        turn_context: dict[str, Any],
    ) -> None:
        manager = self._runtime_manager
        assert manager is not None
        storage = get_session_storage(manager)
        assert storage is not None
        await run_direct_turn(
            runner=self._runtime_runner,
            sessions=manager,
            storage=storage,
            config=self._runtime_config,
            principal_is_owner=self._runtime_owner,
            host_execute_allowed=prepared.host_execute_allowed,
            configured_workspace_dir=prepared.configured_workspace_dir,
            route_envelope=route_envelope,
            guest_profile=prepared.guest_profile,
            accepted_run_mode_override=prepared.accepted_run_mode_override,
            session_key=route_envelope.session_key,
            agent_id=prepared.agent_id,
            turn_id=prepared.turn_id,
            session_id=session_id,
            provider_message=provider_message,
            semantic_message=semantic_message,
            attachments=attachments,
            session_intent=SessionIntent(session_intent),
            run_kind=run_kind,
            no_memory_capture=no_memory_capture,
            fresh_user_session=fresh_user_session,
            user_message_id=user_message_id,
            turn_context=turn_context,
            publish=self._runtime_publish,
            normalize_terminal=self._runtime_normalize_terminal,
            session_model=self._runtime_session_model,
        )
