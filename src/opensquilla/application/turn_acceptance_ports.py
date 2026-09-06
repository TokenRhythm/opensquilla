"""Primitive capabilities used by the durable turn-acceptance transaction.

Runtime reservations and route authorities retain their native implementation.
These structural views expose only the fields acceptance reads and the actions
it orders; protocol parsing and concrete runtime error translation stay outside.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict

from opensquilla.application.admission_views import (
    ActivationTask,
    AdmissionAcceptance,
    AdmissionAnnotation,
    AdmissionAnnotationTarget,
    AdmissionCommit,
    AdmissionGuestCleanup,
    AdmissionMetaControl,
    AdmissionPlanRevision,
    AdmissionPlanRun,
    AdmissionProjectOrigin,
    AdmissionResolvedMode,
    AdmissionSessionCapabilities,
    AdmissionSessionIntent,
    AdmissionStorageCapabilities,
    AdmissionTaskRecord,
    AdmissionWorkspaceSelection,
    MetaAdmissionControl,
    PreparedAdmissionIntent,
    PreparedTranscriptMessage,
    SessionIdentity,
    TranscriptMessage,
)
from opensquilla.run_mode import RunMode

if TYPE_CHECKING:
    from opensquilla.application.turn_admission import (
        AcceptedCollaboration,
        AdmitTurn,
        AdmitTurnResult,
    )
    from opensquilla.application.turn_input import IncomingTurnSource
    from opensquilla.project_workspaces import ProjectWorkspaceGuard, ProjectWorkspaceStateError


@dataclass(frozen=True, slots=True)
class AdmissionPolicy:
    media_root: Path
    persist_transcripts: bool
    disk_budget_bytes: int | None
    opaque_max_bytes: int | None
    accept_opaque: bool
    project_run_mode: RunMode
    default_run_mode: RunMode


class AdmissionSourceKind(Protocol):
    @property
    def value(self) -> str: ...


class AdmissionRouteEnvelope(Protocol):
    @property
    def agent_id(self) -> str: ...

    @property
    def session_key(self) -> str: ...

    @property
    def channel_id(self) -> str: ...

    @property
    def source_kind(self) -> AdmissionSourceKind: ...

    @property
    def input_provenance(self) -> dict[str, Any]: ...

    @property
    def metadata(self) -> dict[str, Any]: ...


class AdmissionArtifactBinding(Protocol):
    @property
    def annotations(self) -> tuple[AdmissionAnnotation, ...]: ...

    @property
    def targets(self) -> tuple[AdmissionAnnotationTarget, ...]: ...

    @property
    def snapshots(self) -> tuple[dict[str, Any], ...]: ...


class PreparedAdmissionRoute(Protocol):
    @property
    def agent_id(self) -> str: ...

    @property
    def envelope(self) -> AdmissionRouteEnvelope: ...

    @property
    def turn_id(self) -> str: ...

    @property
    def mode_resolution(self) -> AdmissionResolvedMode: ...

    @property
    def guest_profile(self) -> AdmissionGuestCleanup | None: ...

    @property
    def accepted_run_mode_override(self) -> object | None: ...

    @property
    def accepted_run_mode_origin(self) -> dict[str, Any] | None: ...

    @property
    def workspace_guard(self) -> ProjectWorkspaceGuard | None: ...

    @property
    def session(self) -> SessionIdentity: ...

    @property
    def configured_workspace_dir(self) -> str | None: ...

    @property
    def host_execute_allowed(self) -> bool: ...


class NormalizedAdmissionInput(Protocol):
    @property
    def message_text(self) -> str: ...

    @property
    def semantic_message(self) -> str: ...

    @property
    def generated_attachments(self) -> list[dict[str, Any]]: ...

    @property
    def metadata(self) -> dict[str, Any]: ...


class IngestedAdmissionAttachments(Protocol):
    @property
    def text(self) -> str: ...

    @property
    def attachments(self) -> list[dict[str, Any]]: ...

    @property
    def consumed_file_uuids(self) -> list[str]: ...


class AdmissionHandle(Protocol):
    @property
    def task_id(self) -> str: ...

    @property
    def session_key(self) -> str: ...

    @property
    def status(self) -> str: ...


class AdmissionReservation(Protocol):
    @property
    def task_record(self) -> AdmissionTaskRecord: ...

    @property
    def activated(self) -> bool: ...

    @property
    def aborted(self) -> bool: ...


class AdmissionRuntime(Protocol):
    async def try_collect_atomically(
        self,
        *,
        envelope: AdmissionRouteEnvelope,
        message: str,
        attachments: list[dict[str, Any]],
        run_kind: str,
        no_memory_capture: bool,
        semantic_message: str,
        persisted_user_message_id: str,
        message_count: int,
        accepted_run_mode_override: object | None,
        persist: Callable[[AdmissionHandle, dict[str, Any]], Awaitable[AdmissionAcceptance]],
    ) -> tuple[AdmissionHandle, AdmissionAcceptance] | None: ...

    async def abort_reservation(self, reservation: AdmissionReservation) -> None: ...

    async def activate(
        self,
        reservation: AdmissionReservation,
        *,
        persisted_user_message_id: str,
        fresh_user_session: bool,
    ) -> AdmissionHandle: ...

    def collect_admission(self, session_key: str) -> AbstractAsyncContextManager[None]: ...


class AdmissionAuthority(Protocol):
    async def aclose(self) -> None: ...

    def handoff(self) -> None: ...


class DirectAdmissionRegistry(Protocol):
    def admission(self, session_key: str) -> AbstractAsyncContextManager[None]: ...

    def register(
        self,
        session_key: str,
        task: asyncio.Task[None],
        *,
        terminal_cleanup: Callable[[], Awaitable[None]] | None,
    ) -> None: ...


class AdmissionUploads(Protocol):
    async def evict(self, file_uuid: str) -> None: ...


class AdmissionCollaborationSnapshot(TypedDict):
    mode: str
    revision: int
    appliesTo: str


class AdmissionRoutingSnapshot(TypedDict):
    mode: str
    revision: int
    source: str
    initialized: bool
    appliesTo: str


class AdmissionStorage(Protocol):
    @property
    def capabilities(self) -> AdmissionStorageCapabilities: ...

    async def get_session(self, key: str) -> SessionIdentity | None: ...

    async def resolve_workspace(self, workspace_id: str) -> AdmissionWorkspaceSelection: ...

    async def replay_turn_ingress_receipt(
        self,
        *,
        source_scope: str,
        request_session_key: str,
        client_request_id: str,
    ) -> AdmissionAcceptance | None: ...

    async def consume_replayed_pending_chat_input(
        self,
        *,
        pending_input_id: str,
        session_key: str,
        source_scope: str,
        client_request_id: str,
        client_message_id: str,
        request_fingerprint: str,
        expected_revision: int,
    ) -> None: ...

    async def get_plan_revision(self, revision_id: str) -> AdmissionPlanRevision | None: ...

    async def get_active_plan_run(self, key: str) -> AdmissionPlanRun | None: ...

    async def get_meta_control_intent(
        self,
        *,
        session_key: str,
        control_kind: str,
        correlation_id: str,
    ) -> AdmissionMetaControl | None: ...

    async def get_agent_task(self, task_id: str) -> ActivationTask | None: ...

    async def fail_queued_agent_task_activation(
        self,
        task_id: str,
        *,
        session_key: str,
        error_class: str,
        error_message: str,
    ) -> ActivationTask | None: ...

    def new_plan_run(
        self,
        *,
        run_id: str,
        session_key: str,
        session_id: str,
        session_epoch: int,
        plan_revision_id: str,
        driver_kind: str,
        driver_id: str | None,
    ) -> AdmissionPlanRun: ...

    def fork_plan_revision(
        self,
        *,
        source_session_key: str,
        source_session_id: str,
        source_epoch: int,
        title: str,
        markdown: str,
        steps: list[dict[str, Any]],
    ) -> AdmissionPlanRevision: ...

    def bind_plan_run_task(self, run: AdmissionPlanRun, task_id: str) -> AdmissionPlanRun: ...

    def create_collection_task(
        self,
        *,
        task_id: str,
        session_key: str,
        agent_id: str,
        source_kind: str,
        run_kind: str,
        details: dict[str, Any],
    ) -> AdmissionTaskRecord: ...

    def with_task_status(
        self, result: AdmissionAcceptance, status: str | None
    ) -> AdmissionAcceptance: ...

    async def accept_turn(self, command: AdmissionCommit) -> AdmissionAcceptance: ...


class AdmissionSessions(Protocol):
    @property
    def capabilities(self) -> AdmissionSessionCapabilities: ...

    async def get_or_create(
        self,
        *,
        session_key: str,
        agent_id: str,
        display_name: str,
    ) -> SessionIdentity: ...

    async def prepare_intent(
        self,
        key: str,
        intent: AdmissionSessionIntent,
        *,
        agent_id: str,
        display_name: str | None = None,
        workspace_id: str | None = None,
        origin: AdmissionProjectOrigin | None = None,
        model_routing_mode: str | None = None,
    ) -> PreparedAdmissionIntent: ...

    async def apply_intent(
        self,
        key: str,
        intent: AdmissionSessionIntent,
        *,
        agent_id: str,
        display_name: str | None = None,
        workspace_id: str | None = None,
        origin: AdmissionProjectOrigin | None = None,
        model_routing_mode: str | None = None,
    ) -> tuple[SessionIdentity, bool]: ...

    async def prepare_prefix_branch(
        self,
        parent_key: str,
        new_key: str,
        *,
        fork_before_message_id: str,
        status: str,
    ) -> PreparedAdmissionIntent: ...

    async def prepare_message(
        self,
        key: str,
        *,
        role: str,
        content: str,
        turn_context: dict[str, Any],
        session_node: SessionIdentity,
    ) -> tuple[PreparedTranscriptMessage, int]: ...

    async def append_message(
        self,
        key: str,
        *,
        role: str,
        content: str,
        turn_context: dict[str, Any] | None = None,
    ) -> TranscriptMessage: ...

    async def remove_message(self, key: str, message_id: str) -> bool: ...

    def stamp_user_text(self, content: str) -> str: ...

    async def has_transcript(self, key: str) -> bool: ...

    def set_cached_epoch(self, key: str, epoch: int) -> None: ...

    def notify_message_appended(self, entry: TranscriptMessage) -> None: ...

    async def write_session_archive(
        self,
        node: SessionIdentity,
        entries: Sequence[TranscriptMessage],
        summaries: Sequence[object],
    ) -> None: ...

    async def copy_fork_materials(
        self, old_session_id: str, new_session_id: str, key: str
    ) -> None: ...


class AdmissionPrimitives(Protocol):
    @property
    def sessions(self) -> AdmissionSessions | None: ...

    @property
    def storage(self) -> AdmissionStorage | None: ...

    @property
    def runtime(self) -> AdmissionRuntime | None: ...

    @property
    def policy(self) -> AdmissionPolicy: ...

    @property
    def is_owner(self) -> bool: ...

    @property
    def direct_registry(self) -> DirectAdmissionRegistry: ...

    @property
    def uploads(self) -> AdmissionUploads: ...

    def explicit_ingress_intent(self, session_key: str) -> AbstractAsyncContextManager[None]: ...

    def authority_scope(self) -> AbstractAsyncContextManager[None]: ...

    def clear_compaction_marker(self, session_key: str) -> None: ...

    def normalize_input(self, command: AdmitTurn) -> NormalizedAdmissionInput: ...

    def artifact_error(
        self,
        kind: str,
        error: Exception | None = None,
        *,
        retryable: bool,
        operation: str | None = None,
        session_key: str | None = None,
    ) -> Exception: ...

    def validate_initial_routing(self, mode: str) -> None: ...

    async def accepted_response(
        self,
        acceptance: AdmissionAcceptance,
        *,
        client_request_id: str,
        storage: AdmissionStorage,
        turn_context: dict[str, Any] | None = None,
        accepted_prompt_annotation_ids: Sequence[str] = (),
    ) -> AdmitTurnResult: ...

    def collaboration_snapshot(
        self, session: SessionIdentity
    ) -> AdmissionCollaborationSnapshot: ...

    async def routing_snapshot(self, session_key: str) -> AdmissionRoutingSnapshot: ...

    def is_remote_guest(self, source: IncomingTurnSource) -> bool: ...

    def workspace_error(self, error: ProjectWorkspaceStateError) -> Exception: ...

    def effective_agent_id(self, session: SessionIdentity | None, session_key: str) -> str: ...

    def session_lock(self, session_key: str) -> AbstractAsyncContextManager[None] | None: ...

    def new_session_key(self, agent_id: str, channel: str) -> str: ...

    async def fork_session(
        self,
        storage: AdmissionStorage,
        parent_key: str,
        child_key: str,
        *,
        explicit_title: str | None,
        fork_transcript: bool,
        status: str,
        fork_before_message_id: str,
    ) -> SessionIdentity: ...

    async def publish_forked(self, session_key: str) -> None: ...

    async def bind_artifact(
        self,
        command: AdmitTurn,
        *,
        key: str,
        session_id: str,
        session: SessionIdentity,
    ) -> AdmissionArtifactBinding: ...

    async def should_auto_title(
        self, storage: AdmissionStorage, session: SessionIdentity, key: str, session_id: str
    ) -> bool: ...

    async def ingest_attachments(
        self,
        message: str,
        attachments: list[dict[str, Any]],
        *,
        session_id: str,
        allow_material_refs: bool,
    ) -> IngestedAdmissionAttachments: ...

    def infer_normalized_input(
        self, message: str, attachments: list[dict[str, Any]]
    ) -> NormalizedAdmissionInput | None: ...

    def materialize_normalized_attachments(
        self,
        attachments: list[dict[str, Any]],
        *,
        media_root: str | Path | None,
        session_id: str,
        normalization_metadata: dict[str, Any],
        disk_budget_bytes: int | None,
    ) -> list[dict[str, Any]]: ...

    async def prepare_route(
        self,
        command: AdmitTurn,
        *,
        session: SessionIdentity,
        key: str,
        session_id: str,
        atomic_intent_plan: PreparedAdmissionIntent | None,
        binding: AdmissionArtifactBinding,
        workspace_guard: ProjectWorkspaceGuard | None,
    ) -> PreparedAdmissionRoute: ...

    def refine_route(
        self,
        route: AdmissionRouteEnvelope,
        *,
        input_provenance: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AdmissionRouteEnvelope: ...

    async def run_direct_turn(
        self,
        prepared: PreparedAdmissionRoute,
        *,
        route_envelope: AdmissionRouteEnvelope,
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
    ) -> None: ...

    def steer_metric(self, outcome: str, *, session_key: str) -> None: ...

    def transcript_content(
        self,
        *,
        text: str,
        display_text: str | None,
        attachments: list[dict[str, Any]],
        session_id: str,
        media_root: str | Path | None,
        persist_enabled: bool,
        disk_budget_bytes: int | None,
        prompt_annotations: tuple[dict[str, Any], ...] = (),
    ) -> tuple[str, Sequence[object]]: ...

    def fork_title_allocation(
        self, storage: AdmissionStorage, parent: SessionIdentity
    ) -> AbstractAsyncContextManager[None]: ...

    async def next_fork_title(self, storage: AdmissionStorage, parent: SessionIdentity) -> str: ...

    def positive_int(self, value: object, *, default: int) -> int: ...

    async def try_collect_atomically(
        self,
        runtime: AdmissionRuntime,
        *,
        envelope: AdmissionRouteEnvelope,
        message: str,
        attachments: list[dict[str, Any]],
        run_kind: str,
        no_memory_capture: bool,
        semantic_message: str,
        persisted_user_message_id: str,
        message_count: int,
        accepted_run_mode_override: object | None,
        persist: Callable[[AdmissionHandle, dict[str, Any]], Awaitable[AdmissionAcceptance]],
    ) -> tuple[AdmissionHandle, AdmissionAcceptance] | None: ...

    def collect_admission(
        self, runtime: AdmissionRuntime, session_key: str
    ) -> AbstractAsyncContextManager[None]: ...

    async def reserve_turn(
        self,
        runtime: AdmissionRuntime,
        route: AdmissionRouteEnvelope,
        message: str,
        *,
        attachments: list[dict[str, Any]],
        mode: str,
        run_kind: str,
        no_memory_capture: bool,
        semantic_message: str,
        turn_id: str,
        accepted_run_mode_override: object | None,
    ) -> AdmissionReservation: ...

    async def freeze_acceptance(
        self,
        runtime: AdmissionRuntime,
        reservation: AdmissionReservation,
        *,
        session_node: SessionIdentity | None = None,
    ) -> None: ...

    async def release_untransferred_authorities(self) -> None: ...

    def schedule_auto_title(
        self,
        session_key: str,
        message: str,
        *,
        enabled: bool,
        session_id: str | None = None,
        root_turn_id: str | None = None,
    ) -> None: ...

    async def publish_collaboration(
        self, session_key: str, collaboration: AcceptedCollaboration
    ) -> None: ...

    def turn_authority(self, route: AdmissionRouteEnvelope) -> AdmissionAuthority | None: ...

    async def publish_disposition(self, session_key: str, turn_context: dict[str, Any]) -> None: ...

    async def start_turn(
        self,
        runtime: AdmissionRuntime,
        route: AdmissionRouteEnvelope,
        message: str,
        *,
        attachments: list[dict[str, Any]],
        mode: str,
        run_kind: str,
        no_memory_capture: bool,
        semantic_message: str,
        persisted_user_message_id: str | None,
        fresh_user_session: bool,
        turn_id: str,
        accepted_run_mode_override: object | None,
    ) -> AdmissionHandle: ...

    def parse_meta_control(
        self,
        message: str,
        semantic_message: str,
        *,
        client_request_id: str,
    ) -> MetaAdmissionControl | None: ...

    def peek_meta_launch(self, key: str, *, client_request_id: str) -> str | None: ...

    def promote_meta_launch(
        self,
        key: str,
        *,
        client_request_id: str,
        message: str,
        semantic_message: str,
    ) -> Literal["promoted", "accepted"] | None: ...

    def restage_meta_launch(self, key: str, *, client_request_id: str) -> bool: ...

    def cancel_accepted_meta_launch(self, key: str, *, client_request_id: str) -> bool: ...
