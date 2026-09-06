"""Semantic views of native acceptance state, without implementation imports."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypedDict, runtime_checkable

from opensquilla.project_workspaces import ProjectWorkspaceGuard
from opensquilla.run_mode import RunMode

type AdmissionSessionIntent = Literal["continue", "new_chat", "reset_same_key"]


@runtime_checkable
class SessionIdentity(Protocol):
    @property
    def session_key(self) -> str: ...
    @property
    def session_id(self) -> str: ...
    @property
    def agent_id(self) -> str: ...


@runtime_checkable
class PreparedSessionIdentity(SessionIdentity, Protocol):
    display_name: str | None


@runtime_checkable
class TranscriptMessage(Protocol):
    @property
    def message_id(self) -> str: ...
    @property
    def content(self) -> str: ...


@runtime_checkable
class PreparedTranscriptMessage(TranscriptMessage, Protocol):
    turn_context: dict[str, Any] | None


@runtime_checkable
class PreparedAdmissionIntent(Protocol):
    @property
    def node(self) -> PreparedSessionIdentity: ...
    @property
    def action(self) -> str: ...
    @property
    def expected_epoch(self) -> int: ...
    @property
    def previous_session_id(self) -> str | None: ...
    @property
    def previous_node(self) -> SessionIdentity | None: ...
    @property
    def initial_transcript_entries(self) -> tuple[TranscriptMessage, ...]: ...


class AdmissionReceipt(Protocol):
    @property
    def request_fingerprint(self) -> str: ...
    @property
    def accepted_session_key(self) -> str: ...
    @property
    def session_id(self) -> str: ...
    @property
    def message_id(self) -> str: ...
    @property
    def task_id(self) -> str | None: ...


class AdmissionArchive(Protocol):
    @property
    def node(self) -> SessionIdentity: ...
    @property
    def entries(self) -> Sequence[TranscriptMessage]: ...
    @property
    def summaries(self) -> Sequence[object]: ...


class AdmissionGoalContext(Protocol):
    def as_task_detail(self) -> dict[str, Any]: ...


@runtime_checkable
class AdmissionAcceptance(Protocol):
    @property
    def receipt(self) -> AdmissionReceipt: ...
    @property
    def replayed(self) -> bool: ...
    @property
    def fresh_user_session(self) -> bool: ...
    @property
    def collaboration_mode(self) -> str | None: ...
    @property
    def reset_archive_snapshot(self) -> AdmissionArchive | None: ...
    @property
    def goal_context(self) -> AdmissionGoalContext | None: ...


class AdmissionTaskRecord(Protocol):
    @property
    def task_id(self) -> str: ...


class ActivationTask(Protocol):
    @property
    def task_id(self) -> str: ...

    @property
    def session_key(self) -> str: ...

    @property
    def status(self) -> str: ...


@runtime_checkable
class AdmissionPlanRevision(Protocol):
    @property
    def revision_id(self) -> str: ...
    @property
    def title(self) -> str: ...
    @property
    def markdown(self) -> str: ...
    @property
    def steps(self) -> list[dict[str, Any]]: ...


@runtime_checkable
class AdmissionPlanRun(Protocol):
    @property
    def run_id(self) -> str: ...
    @property
    def driver_kind(self) -> str: ...
    @property
    def status(self) -> str: ...
    @property
    def plan_revision_id(self) -> str: ...


class AdmissionMetaControl(Protocol):
    @property
    def status(self) -> str: ...
    @property
    def intent_id(self) -> str: ...
    @property
    def control_kind(self) -> str: ...
    @property
    def meta_skill_name(self) -> str: ...
    @property
    def correlation_id(self) -> str: ...
    @property
    def replay_run_id(self) -> str | None: ...
    @property
    def replay_mode(self) -> str | None: ...


class AdmissionAnnotation(Protocol):
    @property
    def annotation_id(self) -> str: ...


class AdmissionAnnotationTarget(Protocol):
    @property
    def expected_annotation(self) -> AdmissionAnnotation: ...


class AdmissionGuestCleanup(Protocol):
    def cleanup(self) -> None: ...


class AdmissionResolvedMode(Protocol):
    @property
    def desired_mode(self) -> RunMode: ...
    @property
    def effective_mode(self) -> RunMode: ...
    @property
    def fallback_reason(self) -> str | None: ...
    @property
    def confirmation_required(self) -> bool: ...


class AdmissionWorkspace(Protocol):
    @property
    def workspace_id(self) -> str: ...

    @property
    def path(self) -> str: ...


class AdmissionWorkspaceSelection(Protocol):
    @property
    def workspace(self) -> AdmissionWorkspace: ...

    @property
    def guard(self) -> ProjectWorkspaceGuard: ...


@dataclass(frozen=True, slots=True)
class AdmissionProjectOrigin:
    run_mode: RunMode
    workspace: str
    run_mode_source: str


@dataclass(frozen=True, slots=True)
class MetaAdmissionControl:
    kind: Literal["manual", "replay"]
    correlation_id: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class AdmissionSessionCapabilities:
    prepared_intent: bool
    prepared_message: bool
    prefix_branch: bool
    apply_intent: bool
    archive: bool
    cache_epoch: bool
    notify: bool
    fork_materials: bool
    transcript: bool
    remove_message: bool


@dataclass(frozen=True, slots=True)
class AdmissionStorageCapabilities:
    receipts: bool
    meta_controls: bool
    atomic_acceptance: bool


class AdmissionSessionChanges(TypedDict, total=False):
    origin: dict[str, Any]
    collaboration_mode: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AdmissionCommit:
    entry: PreparedTranscriptMessage
    expected_epoch: int
    updated_at: int
    task_record: AdmissionTaskRecord | None
    source_scope: str
    request_session_key: str
    client_request_id: str
    request_fingerprint: str
    session_node: PreparedSessionIdentity | None = None
    reset_from_session_id: str | None = None
    reset_archive_writer: Callable[[AdmissionArchive], Awaitable[None]] | None = None
    initial_transcript_entries: Sequence[TranscriptMessage] = ()
    session_updates: AdmissionSessionChanges | None = None
    plan_revision: AdmissionPlanRevision | None = None
    plan_run: AdmissionPlanRun | None = None
    merge_into_task: bool = False
    meta_control_intent_id: str | None = None
    workspace_guard: ProjectWorkspaceGuard | None = None
    expected_collaboration_revision: int | None = None
    expected_active_plan_revision_id: str | None = None
    require_idle_for_current_plan_implementation: bool = False
    claim_current_goal: bool = False
    prepared_prompt_annotation_targets: Sequence[AdmissionAnnotationTarget] = ()
    prompt_annotation_turn_id: str | None = None
    pending_input_id: str | None = None
    pending_input_fingerprint: str | None = None
    pending_input_revision: int | None = None
