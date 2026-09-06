"""Fixed native storage and model primitives for durable acceptance."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from opensquilla.application.admission_views import (
    AdmissionAcceptance,
    AdmissionCommit,
    AdmissionMetaControl,
    AdmissionPlanRevision,
    AdmissionPlanRun,
    AdmissionProjectOrigin,
    AdmissionSessionCapabilities,
    AdmissionSessionIntent,
    AdmissionStorageCapabilities,
    AdmissionTaskRecord,
    AdmissionWorkspaceSelection,
    PreparedAdmissionIntent,
    PreparedTranscriptMessage,
    SessionIdentity,
    TranscriptMessage,
)
from opensquilla.gateway.admission_failures import translate_admission_failure
from opensquilla.project_workspaces import resolve_validated_project_workspace
from opensquilla.sandbox.run_context import RUN_CONTEXT_ORIGIN_KEY, RunContext
from opensquilla.session.goals import ClaimCurrentGoalMutation
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    MetaControlIntent,
    PlanRunRecord,
    SessionIntent,
    SessionStatus,
)
from opensquilla.session.plans import new_plan_revision
from opensquilla.session.storage import TurnAcceptanceResult
from opensquilla.session.turn_context import turn_context_scope


class GatewayAdmissionStorage:
    def __init__(self, raw: object) -> None:
        self.raw = raw
        self.capabilities = AdmissionStorageCapabilities(
            receipts=callable(getattr(raw, "replay_turn_ingress_receipt", None))
            or callable(getattr(raw, "get_turn_ingress_receipt", None)),
            meta_controls=callable(getattr(raw, "get_meta_control_intent", None)),
            atomic_acceptance=callable(getattr(raw, "accept_turn", None)),
        )

    async def get_session(self, key: str) -> SessionIdentity | None:
        with translate_admission_failure():
            result = await getattr(self.raw, "get_session")(key)
        if result is None or isinstance(result, SessionIdentity):
            return result
        raise TypeError("Session lookup did not return a session identity")

    async def resolve_workspace(self, workspace_id: str) -> AdmissionWorkspaceSelection:
        with translate_admission_failure():
            return await resolve_validated_project_workspace(self.raw, workspace_id)

    async def replay_turn_ingress_receipt(
        self,
        *,
        source_scope: str,
        request_session_key: str,
        client_request_id: str,
    ) -> AdmissionAcceptance | None:
        method = getattr(self.raw, "replay_turn_ingress_receipt", None)
        if not callable(method):
            method = getattr(self.raw, "get_turn_ingress_receipt", None)
        if not callable(method):
            return None
        with translate_admission_failure():
            result = await method(
                source_scope=source_scope,
                request_session_key=request_session_key,
                client_request_id=client_request_id,
            )
        if result is None or isinstance(result, AdmissionAcceptance):
            return result
        raise TypeError("Receipt replay did not return durable acceptance")

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
    ) -> None:
        with translate_admission_failure():
            await getattr(self.raw, "consume_replayed_pending_chat_input")(
                pending_input_id=pending_input_id,
                session_key=session_key,
                source_scope=source_scope,
                client_request_id=client_request_id,
                client_message_id=client_message_id,
                request_fingerprint=request_fingerprint,
                expected_revision=expected_revision,
            )

    async def get_plan_revision(self, revision_id: str) -> AdmissionPlanRevision | None:
        with translate_admission_failure():
            result = await getattr(self.raw, "get_plan_revision")(revision_id)
        if result is None or isinstance(result, AdmissionPlanRevision):
            return result
        raise TypeError("Plan lookup did not return a plan revision")

    async def get_active_plan_run(self, key: str) -> AdmissionPlanRun | None:
        with translate_admission_failure():
            result = await getattr(self.raw, "get_active_plan_run")(key)
        if result is None or isinstance(result, AdmissionPlanRun):
            return result
        raise TypeError("Plan lookup did not return a plan run")

    async def get_meta_control_intent(
        self,
        *,
        session_key: str,
        control_kind: str,
        correlation_id: str,
    ) -> AdmissionMetaControl | None:
        method = getattr(self.raw, "get_meta_control_intent", None)
        if not callable(method):
            return None
        with translate_admission_failure():
            result = await method(
                session_key=session_key, control_kind=control_kind, correlation_id=correlation_id
            )
        return result if isinstance(result, MetaControlIntent) else None

    async def get_agent_task(self, task_id: str) -> AgentTaskRecord | None:
        with translate_admission_failure():
            result = await getattr(self.raw, "get_agent_task")(task_id)
        if result is None or isinstance(result, AgentTaskRecord):
            return result
        raise TypeError("Task lookup did not return a durable task")

    async def fail_queued_agent_task_activation(
        self,
        task_id: str,
        *,
        session_key: str,
        error_class: str,
        error_message: str,
    ) -> AgentTaskRecord | None:
        with translate_admission_failure():
            result = await getattr(self.raw, "fail_queued_agent_task_activation")(
                task_id,
                session_key=session_key,
                error_class=error_class,
                error_message=error_message,
            )
        if result is None or isinstance(result, AgentTaskRecord):
            return result
        raise TypeError("Activation compensation did not return a durable task")

    @staticmethod
    def new_plan_run(
        *,
        run_id: str,
        session_key: str,
        session_id: str,
        session_epoch: int,
        plan_revision_id: str,
        driver_kind: str,
        driver_id: str | None,
    ) -> AdmissionPlanRun:
        return PlanRunRecord(
            run_id=run_id,
            session_key=session_key,
            session_id=session_id,
            session_epoch=session_epoch,
            plan_revision_id=plan_revision_id,
            driver_kind=driver_kind,
            driver_id=driver_id,
            status="queued",
            step_states=[],
        )

    @staticmethod
    def fork_plan_revision(
        *,
        source_session_key: str,
        source_session_id: str,
        source_epoch: int,
        title: str,
        markdown: str,
        steps: list[dict[str, Any]],
    ) -> AdmissionPlanRevision:
        return new_plan_revision(
            source_session_key=source_session_key,
            source_session_id=source_session_id,
            source_epoch=source_epoch,
            title=title,
            markdown=markdown,
            steps=steps,
            parent=None,
        )

    @staticmethod
    def bind_plan_run_task(run: AdmissionPlanRun, task_id: str) -> AdmissionPlanRun:
        if not isinstance(run, PlanRunRecord):
            raise TypeError("Plan acceptance requires a durable plan run")
        return run.model_copy(update={"active_task_id": task_id})

    @staticmethod
    def create_collection_task(
        *,
        task_id: str,
        session_key: str,
        agent_id: str,
        source_kind: str,
        run_kind: str,
        details: dict[str, Any],
    ) -> AdmissionTaskRecord:
        return AgentTaskRecord(
            task_id=task_id,
            session_key=session_key,
            agent_id=agent_id,
            source_kind=source_kind,
            queue_mode="collect",
            run_kind=run_kind,
            status=AgentTaskStatus.QUEUED,
            details=details,
        )

    @staticmethod
    def with_task_status(result: AdmissionAcceptance, status: str | None) -> AdmissionAcceptance:
        if not isinstance(result, TurnAcceptanceResult):
            raise TypeError("Failure projection requires durable turn acceptance")
        return replace(result, task_status=AgentTaskStatus(status) if status is not None else None)

    async def accept_turn(self, command: AdmissionCommit) -> AdmissionAcceptance:
        with translate_admission_failure():
            result = await getattr(self.raw, "accept_turn")(
                command.entry,
                expected_epoch=command.expected_epoch,
                updated_at=command.updated_at,
                task_record=command.task_record,
                source_scope=command.source_scope,
                request_session_key=command.request_session_key,
                client_request_id=command.client_request_id,
                request_fingerprint=command.request_fingerprint,
                session_node=command.session_node,
                reset_from_session_id=command.reset_from_session_id,
                reset_archive_writer=command.reset_archive_writer,
                initial_transcript_entries=command.initial_transcript_entries,
                session_updates=command.session_updates,
                plan_revision=command.plan_revision,
                plan_run=command.plan_run,
                merge_into_task=command.merge_into_task,
                meta_control_intent_id=command.meta_control_intent_id,
                workspace_guard=command.workspace_guard,
                expected_collaboration_revision=command.expected_collaboration_revision,
                expected_active_plan_revision_id=command.expected_active_plan_revision_id,
                require_idle_for_current_plan_implementation=(
                    command.require_idle_for_current_plan_implementation
                ),
                goal_mutation=ClaimCurrentGoalMutation() if command.claim_current_goal else None,
                prepared_prompt_annotation_targets=command.prepared_prompt_annotation_targets,
                prompt_annotation_turn_id=command.prompt_annotation_turn_id,
                pending_input_id=command.pending_input_id,
                pending_input_fingerprint=command.pending_input_fingerprint,
                pending_input_revision=command.pending_input_revision,
            )
        if isinstance(result, AdmissionAcceptance):
            return result
        raise TypeError("Turn commit did not return durable acceptance")


class GatewayAdmissionSessions:
    def __init__(self, raw: object) -> None:
        self.raw = raw
        self.capabilities = AdmissionSessionCapabilities(
            prepared_intent=callable(getattr(raw, "prepare_intent", None)),
            prepared_message=callable(getattr(raw, "prepare_message", None)),
            prefix_branch=callable(getattr(raw, "prepare_prefix_branch", None)),
            apply_intent="apply_intent" in dir(raw),
            archive=callable(getattr(raw, "write_session_archive", None)),
            cache_epoch=callable(getattr(raw, "set_cached_epoch", None)),
            notify=callable(getattr(raw, "notify_message_appended", None)),
            fork_materials=callable(getattr(raw, "_copy_fork_materials", None)),
            transcript=callable(getattr(raw, "get_transcript", None)),
            remove_message=hasattr(raw, "remove_message"),
        )

    @staticmethod
    def _creation(
        display_name: str | None,
        workspace_id: str | None,
        origin: AdmissionProjectOrigin | None,
        model_routing_mode: str | None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if display_name is not None:
            result["display_name"] = display_name
        if workspace_id is not None:
            result["workspace_id"] = workspace_id
        if origin is not None:
            result["origin"] = {
                RUN_CONTEXT_ORIGIN_KEY: RunContext(
                    run_mode=origin.run_mode,
                    workspace=origin.workspace,
                    run_mode_source=origin.run_mode_source,
                    source="project_workspace",
                ).to_origin_payload()
            }
        if model_routing_mode is not None:
            result["model_routing_mode"] = model_routing_mode
        return result

    async def get_or_create(
        self,
        *,
        session_key: str,
        agent_id: str,
        display_name: str,
    ) -> SessionIdentity:
        with translate_admission_failure():
            result = await getattr(self.raw, "get_or_create")(
                session_key=session_key,
                agent_id=agent_id,
                display_name=display_name,
            )
        if isinstance(result, SessionIdentity):
            return result
        raise TypeError("Session creation did not return a session identity")

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
    ) -> PreparedAdmissionIntent:
        with translate_admission_failure():
            result = await getattr(self.raw, "prepare_intent")(
                key,
                SessionIntent(intent),
                agent_id=agent_id,
                **self._creation(display_name, workspace_id, origin, model_routing_mode),
            )
        if isinstance(result, PreparedAdmissionIntent):
            return result
        raise TypeError("Intent preparation did not return a prepared session intent")

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
    ) -> tuple[SessionIdentity, bool]:
        with translate_admission_failure():
            result = await getattr(self.raw, "apply_intent")(
                key,
                SessionIntent(intent),
                agent_id=agent_id,
                **self._creation(display_name, workspace_id, origin, model_routing_mode),
            )
        node, fresh = result
        if isinstance(node, SessionIdentity) and isinstance(fresh, bool):
            return node, fresh
        raise TypeError("Intent application did not return a session identity and freshness")

    async def prepare_prefix_branch(
        self,
        parent_key: str,
        new_key: str,
        *,
        fork_before_message_id: str,
        status: str,
    ) -> PreparedAdmissionIntent:
        with translate_admission_failure():
            result = await getattr(self.raw, "prepare_prefix_branch")(
                parent_key,
                new_key,
                fork_before_message_id=fork_before_message_id,
                status=SessionStatus(status),
            )
        if isinstance(result, PreparedAdmissionIntent):
            return result
        raise TypeError("Prefix preparation did not return a prepared session intent")

    async def prepare_message(
        self,
        key: str,
        *,
        role: str,
        content: str,
        turn_context: dict[str, Any],
        session_node: SessionIdentity,
    ) -> tuple[PreparedTranscriptMessage, int]:
        with translate_admission_failure():
            result = await getattr(self.raw, "prepare_message")(
                key,
                role=role,
                content=content,
                turn_context=turn_context,
                session_node=session_node,
            )
        entry, epoch = result
        if isinstance(entry, PreparedTranscriptMessage) and isinstance(epoch, int):
            return entry, epoch
        raise TypeError("Message preparation did not return a transcript entry and epoch")

    async def append_message(
        self,
        key: str,
        *,
        role: str,
        content: str,
        turn_context: dict[str, Any] | None = None,
    ) -> TranscriptMessage:
        with translate_admission_failure(), turn_context_scope(turn_context):
            result = await getattr(self.raw, "append_message")(key, role=role, content=content)
        if isinstance(result, TranscriptMessage):
            return result
        raise TypeError("Message append did not return a transcript entry")

    async def remove_message(self, key: str, message_id: str) -> bool:
        with translate_admission_failure():
            return bool(await getattr(self.raw, "remove_message")(key, message_id))

    def stamp_user_text(self, content: str) -> str:
        method = getattr(self.raw, "stamp_user_text", None)
        if not callable(method):
            return content
        result = method(content)
        return result if isinstance(result, str) else content

    async def has_transcript(self, key: str) -> bool:
        with translate_admission_failure():
            return bool(await getattr(self.raw, "get_transcript")(key))

    def set_cached_epoch(self, key: str, epoch: int) -> None:
        getattr(self.raw, "set_cached_epoch")(key, epoch)

    def notify_message_appended(self, entry: TranscriptMessage) -> None:
        getattr(self.raw, "notify_message_appended")(entry)

    async def write_session_archive(
        self,
        node: SessionIdentity,
        entries: Sequence[TranscriptMessage],
        summaries: Sequence[object],
    ) -> None:
        with translate_admission_failure():
            await getattr(self.raw, "write_session_archive")(node, list(entries), list(summaries))

    async def copy_fork_materials(self, old_session_id: str, new_session_id: str, key: str) -> None:
        with translate_admission_failure():
            await getattr(self.raw, "_copy_fork_materials")(old_session_id, new_session_id, key)
