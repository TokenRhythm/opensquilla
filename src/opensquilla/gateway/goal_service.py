"""Single coordinator for durable Goal commands and automatic turns."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import structlog

from opensquilla.engine.start_turn import reserve_turn_via_runtime
from opensquilla.gateway.routing import (
    RouteEnvelope,
    build_cli_route_envelope,
    build_web_route_envelope,
)
from opensquilla.gateway.session_lifecycle import TaskLifecycleEvent
from opensquilla.gateway.turn_ingress import complete_durable_ingress
from opensquilla.sandbox.run_mode_policy import principal_has_host_execute
from opensquilla.session.goals import (
    ExpectedGoal,
    GoalClaimCandidate,
    GoalCommandRequest,
    GoalConflictError,
    GoalGuardrailPause,
    GoalStatus,
    GoalTurnContext,
    StartGoalMutation,
    automatic_goal_task_id,
    effective_goal_turn_context,
    goal_snapshot,
    goal_turn_context,
    new_goal,
    normalize_client_request_id,
    normalize_goal_objective,
)
from opensquilla.session.keys import canonicalize_session_key, parse_agent_id
from opensquilla.session.models import AgentTaskStatus

if TYPE_CHECKING:
    from opensquilla.gateway.rpc import RpcContext
    from opensquilla.project_workspaces import ProjectWorkspaceGuard
    from opensquilla.session.models import GoalRecord

log = structlog.get_logger(__name__)

_AUTOMATIC_GOAL_MESSAGE = (
    "Continue working toward the active Goal from its durable objective and progress. "
    "Use the Goal controls when progress or a terminal result can be recorded."
)


def _emit_goal_metric(
    metric: str,
    *,
    value: int = 1,
    **labels: str,
) -> None:
    """Emit privacy-safe Goal telemetry with enum-only labels.

    Goal objective/progress/reason text and stable user/session identifiers are
    deliberately excluded from this helper's contract.
    """

    log.info(metric, metric=metric, value=value, **labels)


@dataclass(frozen=True, slots=True)
class GoalExecutionLease:
    session_id: str
    epoch: int
    goal_id: str
    owner_connection_id: str
    principal_identity: str
    source_kind: str
    agent_id: str
    output_surface: str
    continuity_token: str = ""


@dataclass(slots=True)
class _GoalTransitionLockState:
    lock: asyncio.Lock
    borrowers: int = 0


def _principal_identity(principal: Any) -> str:
    """Return a stable, non-secret caller namespace for receipts and leases.

    Authorization remains a live per-request/per-turn check.  In particular,
    mutable role and scope sets must not change the idempotency namespace for
    the same credential after a configuration reload.
    """

    token_public_id = str(getattr(principal, "token_public_id", "") or "")
    guest_owner_id = str(getattr(principal, "guest_owner_id", "") or "")
    if token_public_id:
        namespace = f"token:{token_public_id}"
    elif guest_owner_id:
        namespace = f"guest:{guest_owner_id}"
    elif bool(getattr(principal, "is_owner", False)):
        namespace = "local-owner"
    else:
        # Goal mutations require operator.write, so this is principally a
        # deterministic test/fail-closed namespace rather than an authority
        # grant.  The RPC scope gate still rejects unprivileged callers.
        namespace = f"unidentified:{getattr(principal, 'role', 'unknown')}"
    encoded = namespace.encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def goal_command_fingerprint(
    *,
    action: str,
    session_key: str,
    business_params: Mapping[str, Any],
) -> str:
    payload = {
        "schemaVersion": 1,
        "action": action,
        "sessionKey": canonicalize_session_key(session_key),
        **dict(business_params),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class GoalService:
    """Own Goal state transitions; RPC, tools and lifecycle call only this type."""

    def __init__(
        self,
        *,
        storage: Any,
        session_manager: Any,
        task_runtime: Any,
        event_emitter: Any,
        subscription_manager: Any,
        config: Any,
    ) -> None:
        self._storage = storage
        self._session_manager = session_manager
        self._task_runtime = task_runtime
        self._event_emitter = event_emitter
        self._subscriptions = subscription_manager
        self._config = config
        self._leases: dict[str, GoalExecutionLease] = {}
        # A continuity grant survives a transport detach, but never a Gateway
        # restart.  Keeping it separate from the live lease prevents a closed
        # tab/socket from authorizing another automatic turn while allowing an
        # explicitly authenticated replacement connection to reattach.
        self._continuity_grants: dict[str, GoalExecutionLease] = {}
        self._transition_locks: dict[str, _GoalTransitionLockState] = {}
        self._transition_registry_lock = asyncio.Lock()
        self._kick_tasks: dict[str, asyncio.Task[None]] = {}
        self._kick_dirty: set[str] = set()
        self._closed = False

    @property
    def execution_enabled(self) -> bool:
        return bool(getattr(self.config, "execution_enabled", False))

    @property
    def config(self) -> Any:
        # Production keeps the root GatewayConfig so an in-place config reload
        # that replaces ``root.goal`` is observed immediately.  Direct Goal
        # config objects remain supported for focused tests.
        return getattr(self._config, "goal", self._config)

    def _require_execution_available(self) -> None:
        if self._closed:
            raise GoalConflictError(
                "GOAL_EXECUTION_DISABLED",
                "Goal execution is unavailable while the Gateway is shutting down",
            )
        if not self.execution_enabled:
            raise GoalConflictError(
                "GOAL_EXECUTION_DISABLED",
                "Goal execution is disabled by configuration",
            )

    @staticmethod
    def source_scope(ctx: RpcContext, *, source_kind: str) -> str:
        kind = "cli" if source_kind == "cli" else "web"
        return f"{kind}:{_principal_identity(ctx.principal)}"[:256]

    @asynccontextmanager
    async def _lock(self, session_key: str) -> AsyncIterator[None]:
        key = canonicalize_session_key(session_key)
        async with self._transition_registry_lock:
            state = self._transition_locks.get(key)
            if state is None:
                state = _GoalTransitionLockState(lock=asyncio.Lock())
                self._transition_locks[key] = state
            state.borrowers += 1
        try:
            async with state.lock:
                yield
        finally:
            async with self._transition_registry_lock:
                state.borrowers = max(0, state.borrowers - 1)
                if (
                    state.borrowers == 0
                    and not state.lock.locked()
                    and self._transition_locks.get(key) is state
                ):
                    self._transition_locks.pop(key, None)

    @staticmethod
    def _command(
        *,
        action: str,
        session_key: str,
        client_request_id: str,
        source_scope: str,
        business_params: Mapping[str, Any],
    ) -> GoalCommandRequest:
        client_request_id = normalize_client_request_id(client_request_id)
        return GoalCommandRequest(
            source_scope=source_scope,
            request_session_key=canonicalize_session_key(session_key),
            client_request_id=client_request_id,
            action=action,
            request_fingerprint=goal_command_fingerprint(
                action=action,
                session_key=session_key,
                business_params=business_params,
            ),
        )

    def _require_subscription(self, ctx: RpcContext, session_key: str) -> None:
        subscribers = self._subscriptions.get_message_subscribers(session_key)
        if ctx.conn_id not in subscribers:
            raise GoalConflictError(
                "EXECUTION_LEASE_REQUIRED",
                "Subscribe to this session before starting Goal execution",
            )

    def _install_lease(
        self,
        ctx: RpcContext,
        *,
        goal: GoalRecord,
        source_kind: str,
        continuity_token: str | None = None,
    ) -> GoalExecutionLease:
        self._require_subscription(ctx, goal.session_key)
        source_kind = "cli" if source_kind == "cli" else "web"
        lease = GoalExecutionLease(
            session_id=goal.session_id,
            epoch=goal.session_epoch,
            goal_id=goal.goal_id,
            owner_connection_id=ctx.conn_id,
            principal_identity=_principal_identity(ctx.principal),
            source_kind=source_kind,
            agent_id=str(getattr(ctx, "agent_id", "") or "") or "main",
            output_surface=f"{source_kind}:{ctx.conn_id}",
            continuity_token=continuity_token or secrets.token_urlsafe(32),
        )
        self._leases[goal.session_key] = lease
        self._continuity_grants[goal.session_key] = lease
        return lease

    def _revoke_authority(self, session_key: str) -> None:
        key = canonicalize_session_key(session_key)
        self._leases.pop(key, None)
        self._continuity_grants.pop(key, None)

    def _detach_authority(
        self,
        session_key: str,
        *,
        expected: GoalExecutionLease | None = None,
    ) -> None:
        key = canonicalize_session_key(session_key)
        if expected is not None and self._leases.get(key) != expected:
            return
        self._leases.pop(key, None)

    def _restore_authority(
        self,
        session_key: str,
        *,
        lease: GoalExecutionLease | None,
        grant: GoalExecutionLease | None,
    ) -> None:
        key = canonicalize_session_key(session_key)
        if lease is None:
            self._leases.pop(key, None)
        else:
            self._leases[key] = lease
        if grant is None:
            self._continuity_grants.pop(key, None)
        else:
            self._continuity_grants[key] = grant

    @staticmethod
    def _same_continuity(
        left: GoalExecutionLease,
        right: GoalExecutionLease,
    ) -> bool:
        return (
            left.session_id == right.session_id
            and left.epoch == right.epoch
            and left.goal_id == right.goal_id
            and left.principal_identity == right.principal_identity
            and left.source_kind == right.source_kind
            and bool(left.continuity_token)
            and secrets.compare_digest(
                left.continuity_token,
                right.continuity_token,
            )
        )

    def _grant_for(self, goal: GoalRecord) -> GoalExecutionLease | None:
        grant = self._continuity_grants.get(goal.session_key)
        if grant is None:
            return None
        if (
            grant.session_id != goal.session_id
            or grant.epoch != goal.session_epoch
            or grant.goal_id != goal.goal_id
        ):
            self._revoke_authority(goal.session_key)
            return None
        return grant

    def _response_with_continuity(
        self,
        response: Mapping[str, Any],
        *,
        session_key: str,
        ctx: RpcContext,
    ) -> dict[str, Any]:
        rendered = dict(response)
        goal = rendered.get("goal")
        goal_id = goal.get("goalId") if isinstance(goal, Mapping) else None
        grant = self._continuity_grants.get(canonicalize_session_key(session_key))
        if (
            grant is not None
            and grant.goal_id == goal_id
            and grant.owner_connection_id == ctx.conn_id
            and grant.principal_identity == _principal_identity(ctx.principal)
            and grant.continuity_token
        ):
            rendered["continuityToken"] = grant.continuity_token
        return rendered

    def _lease_for(self, goal: GoalRecord) -> GoalExecutionLease | None:
        lease = self._leases.get(goal.session_key)
        if lease is None:
            return None
        if (
            lease.session_id != goal.session_id
            or lease.epoch != goal.session_epoch
            or lease.goal_id != goal.goal_id
        ):
            self._revoke_authority(goal.session_key)
            return None
        from opensquilla.gateway.scopes import operator_scope_satisfies
        from opensquilla.gateway.websocket import get_registry

        connection = get_registry().get(lease.owner_connection_id)
        if connection is None:
            self._detach_authority(goal.session_key, expected=lease)
            return None
        if lease.owner_connection_id not in self._subscriptions.get_message_subscribers(
            goal.session_key
        ):
            self._detach_authority(goal.session_key, expected=lease)
            return None
        principal = getattr(connection, "principal", None)
        if (
            principal is None
            or _principal_identity(principal) != lease.principal_identity
            or not operator_scope_satisfies("operator.write", principal.scopes)
        ):
            self._detach_authority(goal.session_key, expected=lease)
            return None
        return lease

    async def _execution_state(self, goal: GoalRecord) -> str:
        if goal.active_task_id is None:
            return "idle"
        task = await self._storage.get_agent_task(goal.active_task_id)
        if task is not None and task.status == AgentTaskStatus.QUEUED:
            return "queued"
        return "working"

    async def snapshot(
        self,
        goal: GoalRecord | None,
        *,
        include_runtime_defer: bool = True,
    ) -> dict[str, Any] | None:
        if goal is None:
            return None
        deferred_reason: str | None = None
        session = await self._storage.get_session(goal.session_key)
        if (
            goal.status == GoalStatus.ACTIVE.value
            and session is not None
            and str(getattr(session, "collaboration_mode", "default")) == "plan"
        ):
            deferred_reason = "plan_mode"
        elif (
            goal.status == GoalStatus.ACTIVE.value
            and self._lease_for(goal) is None
        ):
            deferred_reason = "owner_disconnected"
        elif (
            include_runtime_defer
            and goal.status == GoalStatus.ACTIVE.value
            and goal.active_task_id is None
        ):
            if await self._task_runtime.has_explicit_ingress_intent(goal.session_key):
                deferred_reason = "pending_user"
            elif await self._task_runtime.has_session_work(goal.session_key):
                deferred_reason = "busy"
        return goal_snapshot(
            goal,
            execution_state=await self._execution_state(goal),
            continuation_deferred_reason=deferred_reason,
        )

    async def _emit_goal(
        self,
        goal: GoalRecord | None,
        *,
        event_type: str,
        session_key: str,
        session_id: str,
        epoch: int,
        state_revision: int,
        progress_revision: int,
        previous_goal_id: str | None = None,
    ) -> None:
        try:
            payload = {
                "session_key": session_key,
                "session_id": session_id,
                "epoch": epoch,
                "event_type": event_type,
                "state_revision": state_revision,
                "progress_revision": progress_revision,
                "previous_goal_id": previous_goal_id,
                # Goal transitions frequently emit while holding the ordered
                # transition lock.  Event rendering must never reverse the
                # lock order by consulting TaskRuntime intent/state locks.
                # Read RPC/hydration snapshots include those derived hints.
                "goal": await self.snapshot(goal, include_runtime_defer=False),
            }
            await self._event_emitter(
                session_key,
                "session.event.goal",
                payload,
            )
        except Exception:
            # Events are an observer projection. Durable Goal state plus stream
            # replay/hydration is authoritative, so notification failure must
            # never turn an accepted/claimed Goal task into a different run.
            log.warning(
                "goal.event_emit_failed",
                event_type=event_type,
                exc_info=True,
            )

    def _route_for(
        self,
        *,
        lease: GoalExecutionLease,
        goal: GoalRecord,
        principal: Any,
        source_name: str,
    ) -> RouteEnvelope:
        if lease.source_kind == "cli":
            envelope = build_cli_route_envelope(
                session_key=goal.session_key,
                agent_id=parse_agent_id(goal.session_key),
                source_name=source_name,
                channel_id=lease.output_surface,
                session_id=goal.session_id,
                principal_is_owner=bool(getattr(principal, "is_owner", False)),
                principal_host_execute=principal_has_host_execute(principal),
            )
        else:
            envelope = build_web_route_envelope(
                session_key=goal.session_key,
                agent_id=parse_agent_id(goal.session_key),
                source_name=source_name,
                conn_id=lease.owner_connection_id,
                channel_id=lease.output_surface,
                session_id=goal.session_id,
                principal_is_owner=bool(getattr(principal, "is_owner", False)),
                principal_host_execute=principal_has_host_execute(principal),
            )
        return envelope

    async def _prepare_execution_envelope(
        self,
        envelope: RouteEnvelope,
        *,
        session: Any,
        principal: Any,
    ) -> tuple[ProjectWorkspaceGuard | None, Any | None]:
        """Apply the same workspace and sandbox admission used by user turns.

        TaskRuntime repeats authoritative workspace resolution at the actual
        execution boundary.  This earlier pass prevents a Goal command from
        durably accepting work that an ordinary ``sessions.send`` would reject
        before transcript/task persistence, and prevents an automatic Goal
        turn from reaching the provider when Safe execution is unavailable.
        """

        from opensquilla.agents.scope import resolve_agent_workspace_dir
        from opensquilla.gateway.project_workspace_runtime import (
            AcceptedRunModeOverride,
            apply_run_context_route_metadata,
            authoritative_project_run_context,
            map_project_workspace_error,
        )
        from opensquilla.gateway.rpc import RpcHandlerError
        from opensquilla.project_workspaces import ProjectWorkspaceStateError
        from opensquilla.run_mode import RunMode
        from opensquilla.sandbox.mode_resolver import (
            ModeResolutionError,
            ResolvedMode,
            resolve_mode,
        )
        from opensquilla.sandbox.run_mode_policy import coerce_run_mode_for_principal
        from opensquilla.sandbox.setup_runtime import current_sandbox_capability_report

        agent_id = str(getattr(session, "agent_id", "") or parse_agent_id(session.session_key))
        workspace = resolve_agent_workspace_dir(agent_id, self._config)
        try:
            run_context, workspace_guard = await authoritative_project_run_context(
                storage=self._storage,
                session_manager=self._session_manager,
                session=session,
                config=self._config,
                default_workspace=str(workspace) if workspace is not None else None,
            )
        except ProjectWorkspaceStateError as exc:
            raise map_project_workspace_error(
                exc,
                owner=bool(getattr(principal, "is_owner", False)),
            ) from exc

        run_context = replace(
            run_context,
            run_mode=coerce_run_mode_for_principal(run_context.run_mode, principal),
        )
        capability = None
        if run_context.run_mode is RunMode.FULL:
            resolution = ResolvedMode(
                desired_mode=RunMode.FULL,
                effective_mode=RunMode.FULL,
            )
        else:
            capability = await current_sandbox_capability_report(self._config)
            try:
                resolution = resolve_mode(run_context.run_mode, principal, capability)
            except ModeResolutionError as exc:
                raise RpcHandlerError(
                    "SANDBOX_MODE_UNAVAILABLE",
                    "The requested execution mode is unavailable.",
                    details={"reason": exc.code, **capability.to_payload()},
                    accepted=False,
                ) from exc
        accepted_run_mode_override = None
        if resolution.effective_mode is not run_context.run_mode:
            accepted_run_mode_override = AcceptedRunModeOverride(
                run_mode=resolution.effective_mode,
                run_mode_source=run_context.run_mode_source,
                source="capability_fallback",
            )
            run_context = replace(
                run_context,
                run_mode=resolution.effective_mode,
                source="capability_fallback",
            )
        apply_run_context_route_metadata(
            envelope,
            run_context,
            principal_is_owner=bool(getattr(principal, "is_owner", False)),
        )
        envelope.metadata["sandbox_mode_resolution"] = resolution.to_payload()
        return workspace_guard, accepted_run_mode_override

    async def set(
        self,
        ctx: RpcContext,
        *,
        session_key: str,
        objective: str,
        client_request_id: str,
        client_message_id: str,
        source_kind: str,
    ) -> dict[str, Any]:
        key = canonicalize_session_key(session_key)
        async with self._task_runtime.explicit_ingress_intent(key):
            return await self._set_with_registered_intent(
                ctx,
                session_key=key,
                objective=objective,
                client_request_id=client_request_id,
                client_message_id=client_message_id,
                source_kind=source_kind,
            )

    async def _set_with_registered_intent(
        self,
        ctx: RpcContext,
        *,
        session_key: str,
        objective: str,
        client_request_id: str,
        client_message_id: str,
        source_kind: str,
    ) -> dict[str, Any]:
        key = canonicalize_session_key(session_key)
        objective = normalize_goal_objective(objective)
        client_message_id = normalize_client_request_id(client_message_id)
        source_kind = "cli" if source_kind == "cli" else "web"
        source_scope = self.source_scope(ctx, source_kind=source_kind)
        command = self._command(
            action="set",
            session_key=key,
            client_request_id=client_request_id,
            source_scope=source_scope,
            business_params={
                "objective": objective,
                "clientMessageId": client_message_id,
                "expectedGoalId": None,
                "expectedStateRevision": None,
            },
        )
        replay = await self._storage.get_goal_command_receipt(command)
        if replay is not None:
            _emit_goal_metric(
                "goal_commands_total",
                action="set",
                outcome="replayed",
            )
            return self._response_with_continuity(
                replay.response,
                session_key=key,
                ctx=ctx,
            )
        self._require_execution_available()
        self._require_subscription(ctx, key)
        session = await self._storage.get_session(key)
        if session is None:
            raise KeyError(f"Session not found: {key}")

        prepare_message = getattr(self._session_manager, "prepare_message", None)
        if not callable(prepare_message):
            raise RuntimeError("Goal set requires atomic transcript preparation")
        task_id = str(uuid.uuid4())
        goal_id = str(uuid.uuid4())
        entry, expected_epoch = await prepare_message(
            key,
            role="user",
            content=objective,
            turn_context={
                "turn_id": task_id,
                "client_message_id": client_message_id,
                "surface_id": f"{source_kind}:{ctx.conn_id}",
                "intent": "goal_set",
                "disposition": "queued",
                "revision": 1,
            },
            session_node=session,
        )
        goal = new_goal(
            goal_id=goal_id,
            session_key=key,
            session_id=str(session.session_id),
            session_epoch=int(expected_epoch),
            objective=objective,
            task_id=task_id,
            source_user_message_id=entry.message_id,
        )
        frozen = goal_turn_context(goal, task_id=task_id, automatic=False)
        # Build from the authenticated caller; later automatic turns rebuild
        # from the live connection and principal rather than caching authority.
        provisional = GoalExecutionLease(
            session_id=goal.session_id,
            epoch=goal.session_epoch,
            goal_id=goal.goal_id,
            owner_connection_id=ctx.conn_id,
            principal_identity=_principal_identity(ctx.principal),
            source_kind=source_kind,
            agent_id=str(getattr(session, "agent_id", "main") or "main"),
            output_surface=f"{source_kind}:{ctx.conn_id}",
        )
        envelope = self._route_for(
            lease=provisional,
            goal=goal,
            principal=ctx.principal,
            source_name="goals.set",
        )
        envelope.metadata.update(
            {
                "client_message_id": client_message_id,
                "surface_id": provisional.output_surface,
                "turn_context_intent": "goal_set",
                "turn_context_disposition": "queued",
                "turn_context_revision": 1,
            }
        )
        workspace_guard, accepted_run_mode_override = await self._prepare_execution_envelope(
            envelope,
            session=session,
            principal=ctx.principal,
        )

        async def _accept_and_activate() -> Any:
            async with self._task_runtime.collect_admission(key):
                reservation = await reserve_turn_via_runtime(
                    self._task_runtime,
                    envelope,
                    objective,
                    mode="followup",
                    run_kind="session_turn",
                    input_mode="user",
                    persist_input=False,
                    history_has_persisted_user=True,
                    goal_context=frozen.as_task_detail(),
                    semantic_message=objective,
                    persisted_user_message_id=entry.message_id,
                    turn_id=task_id,
                    accepted_run_mode_override=accepted_run_mode_override,
                )
                try:
                    await self._task_runtime.freeze_acceptance(reservation)
                except BaseException:
                    await self._task_runtime.abort_reservation(reservation)
                    raise
                async with self._lock(key):
                    previous_lease = self._leases.get(key)
                    previous_grant = self._continuity_grants.get(key)
                    try:
                        self._require_execution_available()
                        # Acquire the process-local execution authority before
                        # the durable set. Subscription loss after this point
                        # is serialized by the Goal transition lock; it detaches
                        # future execution but never rewrites durable Goal state.
                        self._install_lease(
                            ctx,
                            goal=goal,
                            source_kind=source_kind,
                        )
                        acceptance = await self._storage.accept_turn(
                            entry,
                            expected_epoch=expected_epoch,
                            updated_at=int(time.time() * 1000),
                            task_record=reservation.task_record,
                            source_scope=command.source_scope,
                            request_session_key=key,
                            client_request_id=command.client_request_id,
                            request_fingerprint=command.request_fingerprint,
                            workspace_guard=workspace_guard,
                            goal_mutation=StartGoalMutation(goal=goal, command=command),
                        )
                    except BaseException:
                        self._restore_authority(
                            key,
                            lease=previous_lease,
                            grant=previous_grant,
                        )
                        await self._task_runtime.abort_reservation(reservation)
                        raise
                    if acceptance.replayed:
                        self._restore_authority(
                            key,
                            lease=previous_lease,
                            grant=previous_grant,
                        )
                        await self._task_runtime.abort_reservation(reservation)
                        return acceptance
                    assert acceptance.goal is not None
                    assert acceptance.goal_context is not None
                    notify = getattr(self._session_manager, "notify_message_appended", None)
                    if callable(notify):
                        try:
                            notify(entry)
                        except Exception:  # noqa: BLE001 - Goal set is already durable.
                            log.exception(
                                "goal.set_post_accept_notify_failed",
                                session_key=key,
                                task_id=acceptance.receipt.task_id,
                            )
                    response = acceptance.goal_command_response
                    previous_goal_id = (
                        response.get("previousGoalId")
                        if isinstance(response, dict)
                        else None
                    )
                    try:
                        await self._emit_goal(
                            acceptance.goal,
                            event_type="created",
                            session_key=key,
                            session_id=acceptance.goal.session_id,
                            epoch=acceptance.goal.session_epoch,
                            state_revision=acceptance.goal.state_revision,
                            progress_revision=acceptance.goal.progress_revision,
                            previous_goal_id=(
                                str(previous_goal_id) if previous_goal_id else None
                            ),
                        )
                    except Exception:
                        # Event replay/hydration can recover a missed live
                        # notification.  Durable acceptance must still activate.
                        log.warning(
                            "goal.created_event_failed",
                            session_key=key,
                            task_id=acceptance.receipt.task_id,
                            exc_info=True,
                        )
                    try:
                        await self._task_runtime.activate(
                            reservation,
                            persisted_user_message_id=acceptance.receipt.message_id,
                            fresh_user_session=acceptance.fresh_user_session,
                        )
                    except Exception:
                        log.exception(
                            "goal.activation_failed",
                            session_key=key,
                            task_id=acceptance.receipt.task_id,
                        )
                        # Once TaskRuntime crosses its activation boundary the
                        # driver owns terminal settlement.  Never abandon a
                        # task that may already be executing merely because a
                        # post-activation observer failed.
                        if reservation.activated:
                            log.warning(
                                "goal.activation_error_after_start",
                                session_key=key,
                                task_id=acceptance.receipt.task_id,
                            )
                        else:
                            try:
                                compensated = (
                                    await self._storage.compensate_goal_activation_failure(
                                        acceptance.goal_context
                                    )
                                )
                            except Exception:
                                compensated = None
                                log.exception(
                                    "goal.activation_compensation_failed",
                                    session_key=key,
                                    task_id=acceptance.receipt.task_id,
                                )
                            await self._task_runtime.abort_reservation(reservation)
                            self._revoke_authority(key)
                            if compensated is not None:
                                await self._emit_goal(
                                    compensated,
                                    event_type="updated",
                                    session_key=key,
                                    session_id=compensated.session_id,
                                    epoch=compensated.session_epoch,
                                    state_revision=compensated.state_revision,
                                    progress_revision=compensated.progress_revision,
                                )
                    return acceptance

        from opensquilla.gateway.project_workspace_runtime import (
            map_project_workspace_error,
        )
        from opensquilla.project_workspaces import ProjectWorkspaceStateError

        try:
            acceptance = await complete_durable_ingress(_accept_and_activate())
        except ProjectWorkspaceStateError as exc:
            raise map_project_workspace_error(
                exc,
                owner=bool(getattr(ctx.principal, "is_owner", False)),
            ) from exc
        response = acceptance.goal_command_response
        if not isinstance(response, dict):
            raise RuntimeError("Atomic Goal set lost its command response")
        _emit_goal_metric(
            "goal_commands_total",
            action="set",
            outcome="replayed" if acceptance.replayed else "accepted",
        )
        return self._response_with_continuity(
            response,
            session_key=key,
            ctx=ctx,
        )

    async def status(self, session_key: str) -> dict[str, Any]:
        key = canonicalize_session_key(session_key)
        session = await self._storage.get_session(key)
        goal = await self._storage.get_goal(key)
        return {
            "sessionKey": key,
            "sessionId": str(getattr(session, "session_id", "") or ""),
            "epoch": int(getattr(session, "epoch", 0) or 0),
            "goal": await self.snapshot(goal),
        }

    async def edit(
        self,
        ctx: RpcContext,
        *,
        session_key: str,
        expected_goal_id: str,
        expected_state_revision: int,
        objective: str,
        client_request_id: str,
        source_scope: str,
        source_kind: str,
    ) -> dict[str, Any]:
        key = canonicalize_session_key(session_key)
        async with self._task_runtime.explicit_ingress_intent(key):
            normalized_objective = normalize_goal_objective(objective)

            async def _persist(adoption_task_id: str | None) -> dict[str, Any]:
                return await self._edit_with_registered_intent(
                    ctx,
                    session_key=key,
                    expected_goal_id=expected_goal_id,
                    expected_state_revision=expected_state_revision,
                    objective=normalized_objective,
                    client_request_id=client_request_id,
                    source_scope=source_scope,
                    source_kind=source_kind,
                    adoption_task_id=adoption_task_id,
                )

            result = await self._task_runtime.apply_goal_objective_edit(
                key,
                persist=_persist,
            )
            if not isinstance(result, dict):
                raise RuntimeError("Goal edit admission returned an invalid response")
            return result

    async def _edit_with_registered_intent(
        self,
        ctx: RpcContext,
        *,
        session_key: str,
        expected_goal_id: str,
        expected_state_revision: int,
        objective: str,
        client_request_id: str,
        source_scope: str,
        source_kind: str,
        adoption_task_id: str | None = None,
    ) -> dict[str, Any]:
        key = canonicalize_session_key(session_key)
        command = self._command(
            action="edit",
            session_key=session_key,
            client_request_id=client_request_id,
            source_scope=source_scope,
            business_params={
                "expectedGoalId": expected_goal_id,
                "expectedStateRevision": expected_state_revision,
                "objective": objective,
            },
        )
        replay = await self._storage.get_goal_command_receipt(command)
        if replay is not None:
            _emit_goal_metric(
                "goal_commands_total",
                action="edit",
                outcome="replayed",
            )
            return self._response_with_continuity(
                replay.response,
                session_key=key,
                ctx=ctx,
            )

        async with self._lock(key):
            goal = await self._storage.get_goal(key)
            if goal is None:
                raise GoalConflictError(
                    "GOAL_NOT_FOUND",
                    "No Goal exists for this session",
                )
            expected = ExpectedGoal(
                session_id=goal.session_id,
                epoch=goal.session_epoch,
                goal_id=expected_goal_id,
                state_revision=expected_state_revision,
            )
            reactivating = goal.status == GoalStatus.COMPLETE.value
            previous_lease = self._leases.get(key)
            previous_grant = self._continuity_grants.get(key)
            try:
                if reactivating:
                    self._require_execution_available()
                    self._install_lease(
                        ctx,
                        goal=goal,
                        source_kind=source_kind,
                    )
                result = await self._storage.edit_goal(
                    session_key=key,
                    expected=expected,
                    objective=objective,
                    command=command,
                    adoption_task_id=adoption_task_id,
                )
            except BaseException:
                if reactivating:
                    self._restore_authority(
                        key,
                        lease=previous_lease,
                        grant=previous_grant,
                    )
                raise
            if result.replayed and reactivating:
                self._restore_authority(
                    key,
                    lease=previous_lease,
                    grant=previous_grant,
                )
            elif not result.replayed and result.goal is not None:
                await self._emit_goal(
                    result.goal,
                    event_type="updated",
                    session_key=key,
                    session_id=result.goal.session_id,
                    epoch=result.goal.session_epoch,
                    state_revision=result.goal.state_revision,
                    progress_revision=result.goal.progress_revision,
                )

        if (
            not result.replayed
            and result.goal is not None
            and result.goal.status == GoalStatus.ACTIVE.value
            and result.goal.active_task_id is None
        ):
            self.schedule_idle_evaluation(key)
        _emit_goal_metric(
            "goal_commands_total",
            action="edit",
            outcome="replayed" if result.replayed else "accepted",
        )
        return self._response_with_continuity(
            result.response,
            session_key=key,
            ctx=ctx,
        )

    async def pause(self, **kwargs: Any) -> dict[str, Any]:
        return await self._mutate("pause", **kwargs)

    async def clear(self, **kwargs: Any) -> dict[str, Any]:
        result = await self._mutate("clear", **kwargs)
        await self._task_runtime.revoke_goal_objective_updates(
            str(kwargs["session_key"])
        )
        return result

    async def resume(
        self,
        ctx: RpcContext,
        *,
        source_kind: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        key = canonicalize_session_key(str(kwargs["session_key"]))
        async with self._task_runtime.explicit_ingress_intent(key):
            return await self._resume_with_registered_intent(
                ctx,
                source_kind=source_kind,
                **kwargs,
            )

    async def _resume_with_registered_intent(
        self,
        ctx: RpcContext,
        *,
        source_kind: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        key = canonicalize_session_key(str(kwargs["session_key"]))
        client_request_id = str(kwargs["client_request_id"])
        expected_goal_id = str(kwargs["expected_goal_id"])
        expected_state_revision = int(kwargs["expected_state_revision"])
        source_scope = str(kwargs["source_scope"])
        command = self._command(
            action="resume",
            session_key=key,
            client_request_id=client_request_id,
            source_scope=source_scope,
            business_params={
                "expectedGoalId": expected_goal_id,
                "expectedStateRevision": expected_state_revision,
            },
        )
        replay = await self._storage.get_goal_command_receipt(command)
        if replay is not None:
            _emit_goal_metric(
                "goal_commands_total",
                action="resume",
                outcome="replayed",
            )
            return self._response_with_continuity(
                replay.response,
                session_key=key,
                ctx=ctx,
            )
        self._require_execution_available()
        self._require_subscription(ctx, key)

        async def _resume_locked() -> Any:
            async with self._lock(key):
                self._require_execution_available()
                goal = await self._storage.get_goal(key)
                if goal is None:
                    raise GoalConflictError(
                        "GOAL_NOT_FOUND",
                        "No Goal exists for this session",
                    )
                expected = ExpectedGoal(
                    session_id=goal.session_id,
                    epoch=goal.session_epoch,
                    goal_id=expected_goal_id,
                    state_revision=expected_state_revision,
                )
                previous_lease = self._leases.get(key)
                previous_grant = self._continuity_grants.get(key)
                try:
                    self._install_lease(
                        ctx,
                        goal=goal,
                        source_kind=source_kind,
                    )
                    result = await self._storage.resume_goal(
                        session_key=key,
                        expected=expected,
                        command=command,
                    )
                except BaseException:
                    self._restore_authority(
                        key,
                        lease=previous_lease,
                        grant=previous_grant,
                    )
                    raise
                if result.replayed:
                    self._restore_authority(
                        key,
                        lease=previous_lease,
                        grant=previous_grant,
                    )
                elif result.goal is not None:
                    await self._emit_goal(
                        result.goal,
                        event_type="updated",
                        session_key=key,
                        session_id=result.goal.session_id,
                        epoch=result.goal.session_epoch,
                        state_revision=result.goal.state_revision,
                        progress_revision=result.goal.progress_revision,
                    )
                return result

        result = await complete_durable_ingress(_resume_locked())
        if not result.replayed and result.goal is not None:
            self.schedule_idle_evaluation(key)
        _emit_goal_metric(
            "goal_commands_total",
            action="resume",
            outcome="replayed" if result.replayed else "accepted",
        )
        return self._response_with_continuity(
            result.response,
            session_key=key,
            ctx=ctx,
        )

    async def reattach(
        self,
        ctx: RpcContext,
        *,
        session_key: str,
        session_id: str,
        epoch: int,
        expected_goal_id: str,
        continuity_token: str | None,
        source_kind: str,
        takeover: bool = False,
    ) -> dict[str, Any]:
        """Rebind detached execution authority without mutating Goal state.

        Normal reconnects prove continuity with the process-local opaque token
        issued by set/resume. A tokenless takeover is accepted only when an
        authorized user explicitly requests it and no live owner remains.
        """

        self._require_execution_available()
        key = canonicalize_session_key(session_key)
        self._require_subscription(ctx, key)
        source_kind = "cli" if source_kind == "cli" else "web"
        async with self._lock(key):
            self._require_execution_available()
            goal = await self._storage.get_goal(key)
            if goal is None:
                raise GoalConflictError(
                    "GOAL_NOT_FOUND",
                    "No Goal exists for this session",
                )
            if goal.session_id != session_id or goal.session_epoch != epoch:
                raise GoalConflictError(
                    "SESSION_GENERATION_CHANGED",
                    "The session generation changed before Goal reattachment",
                    current=goal,
                )
            if goal.goal_id != expected_goal_id:
                raise GoalConflictError(
                    "STALE_GOAL",
                    "The active Goal changed before reattachment",
                    current=goal,
                )
            if goal.status != GoalStatus.ACTIVE.value:
                raise GoalConflictError(
                    "GOAL_NOT_RESUMABLE",
                    "Only an active detached Goal can be reattached",
                    current=goal,
                )

            live = self._lease_for(goal)
            grant = self._grant_for(goal)
            principal_identity = _principal_identity(ctx.principal)
            if takeover:
                if live is not None:
                    raise GoalConflictError(
                        "EXECUTION_LEASE_REQUIRED",
                        "The Goal already has an attached execution owner",
                        current=goal,
                    )
                token = secrets.token_urlsafe(32)
            else:
                token = str(continuity_token or "")
                if (
                    grant is None
                    or not token
                    or not grant.continuity_token
                    or grant.principal_identity != principal_identity
                    or grant.source_kind != source_kind
                    or not secrets.compare_digest(grant.continuity_token, token)
                ):
                    raise GoalConflictError(
                        "EXECUTION_LEASE_REQUIRED",
                        "Valid Goal execution continuity is required",
                        current=goal,
                    )
                if live is not None and live.owner_connection_id != ctx.conn_id:
                    raise GoalConflictError(
                        "EXECUTION_LEASE_REQUIRED",
                        "The Goal already has an attached execution owner",
                        current=goal,
                    )

            installed = self._install_lease(
                ctx,
                goal=goal,
                source_kind=source_kind,
                continuity_token=token,
            )
            snapshot = await self.snapshot(goal, include_runtime_defer=False)
            assert snapshot is not None
            response = {
                "accepted": True,
                "sessionKey": key,
                "sessionId": goal.session_id,
                "epoch": goal.session_epoch,
                "goal": snapshot,
                "continuityToken": installed.continuity_token,
            }
        self.schedule_idle_evaluation(key)
        return response

    async def _mutate(
        self,
        action: str,
        *,
        session_key: str,
        expected_goal_id: str,
        expected_state_revision: int,
        client_request_id: str,
        source_scope: str,
    ) -> dict[str, Any]:
        key = canonicalize_session_key(session_key)
        async with self._task_runtime.explicit_ingress_intent(key):
            return await self._mutate_with_registered_intent(
                action,
                session_key=key,
                expected_goal_id=expected_goal_id,
                expected_state_revision=expected_state_revision,
                client_request_id=client_request_id,
                source_scope=source_scope,
            )

    async def _mutate_with_registered_intent(
        self,
        action: str,
        *,
        session_key: str,
        expected_goal_id: str,
        expected_state_revision: int,
        client_request_id: str,
        source_scope: str,
    ) -> dict[str, Any]:
        key = canonicalize_session_key(session_key)
        business: dict[str, Any] = {
            "expectedGoalId": expected_goal_id,
            "expectedStateRevision": expected_state_revision,
        }
        command = self._command(
            action=action,
            session_key=key,
            client_request_id=client_request_id,
            source_scope=source_scope,
            business_params=business,
        )
        replay = await self._storage.get_goal_command_receipt(command)
        if replay is not None:
            _emit_goal_metric(
                "goal_commands_total",
                action=action,
                outcome="replayed",
            )
            return dict(replay.response)
        async with self._lock(key):
            goal = await self._storage.get_goal(key)
            if goal is None:
                raise GoalConflictError(
                    "GOAL_NOT_FOUND",
                    "No Goal exists for this session",
                )
            expected = ExpectedGoal(
                session_id=goal.session_id,
                epoch=goal.session_epoch,
                goal_id=expected_goal_id,
                state_revision=expected_state_revision,
            )
            if action == "pause":
                result = await self._storage.pause_goal(
                    session_key=key,
                    expected=expected,
                    command=command,
                    reason="user",
                )
            elif action == "clear":
                result = await self._storage.clear_goal(
                    session_key=key,
                    expected=expected,
                    command=command,
                )
            else:
                raise AssertionError(f"unsupported Goal mutation: {action}")
            if result.replayed:
                return dict(result.response)
            if action in {"pause", "clear"}:
                self._revoke_authority(key)
            if action == "clear":
                await self._emit_goal(
                    None,
                    event_type="cleared",
                    session_key=key,
                    session_id=goal.session_id,
                    epoch=goal.session_epoch,
                    state_revision=goal.state_revision + 1,
                    progress_revision=goal.progress_revision,
                    previous_goal_id=goal.goal_id,
                )
            elif result.goal is not None:
                await self._emit_goal(
                    result.goal,
                    event_type="updated",
                    session_key=key,
                    session_id=result.goal.session_id,
                    epoch=result.goal.session_epoch,
                    state_revision=result.goal.state_revision,
                    progress_revision=result.goal.progress_revision,
                )
        _emit_goal_metric(
            "goal_commands_total",
            action=action,
            outcome="accepted",
        )
        return dict(result.response)

    async def commit_model_status(
        self,
        context_value: Mapping[str, Any],
        *,
        status: str,
        reason: str | None,
    ) -> dict[str, Any]:
        context = GoalTurnContext.from_task_detail(context_value)
        if context is None:
            raise GoalConflictError("STALE_GOAL", "Invalid Goal turn context")
        goal = await self._storage.get_goal_by_id(context.goal_id)
        key = goal.session_key if goal is not None else ""
        async with self._lock(key):
            updated = await self._storage.commit_goal_terminal(
                context,
                status=status,
                blocked_reason=reason,
            )
            await self._emit_goal(
                updated,
                event_type="updated",
                session_key=updated.session_key,
                session_id=updated.session_id,
                epoch=updated.session_epoch,
                state_revision=updated.state_revision,
                progress_revision=updated.progress_revision,
            )
        snapshot = await self.snapshot(updated)
        assert snapshot is not None
        return snapshot

    async def update_progress(
        self,
        context_value: Mapping[str, Any],
        *,
        explanation: str | None,
        steps: list[dict[str, Any]],
    ) -> dict[str, Any]:
        context = GoalTurnContext.from_task_detail(context_value)
        if context is None:
            raise GoalConflictError("STALE_GOAL", "Invalid Goal turn context")
        goal = await self._storage.get_goal_by_id(context.goal_id)
        key = goal.session_key if goal is not None else ""
        async with self._lock(key):
            updated = await self._storage.update_goal_progress(
                context,
                explanation=explanation,
                steps=steps,
            )
            await self._emit_goal(
                updated,
                event_type="updated",
                session_key=updated.session_key,
                session_id=updated.session_id,
                epoch=updated.session_epoch,
                state_revision=updated.state_revision,
                progress_revision=updated.progress_revision,
            )
        snapshot = await self.snapshot(updated)
        assert snapshot is not None
        return snapshot

    async def build_prompt_context(
        self,
        context_value: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        context = GoalTurnContext.from_task_detail(context_value)
        if context is None:
            return None
        goal = await self._storage.get_goal_by_id(context.goal_id)
        if (
            goal is None
            or goal.session_id != context.session_id
            or goal.session_epoch != context.epoch
            or goal.goal_id != context.goal_id
            or goal.objective_revision != context.objective_revision
            or goal.objective != context.objective_snapshot
            or goal.active_task_id != context.task_id
            or goal.continuation_seq != context.continuation_seq
            or goal.status
            not in {GoalStatus.ACTIVE.value, GoalStatus.PAUSED.value}
        ):
            return None
        rendered = dict(context.as_task_detail())
        rendered["progress"] = goal.progress_json
        if goal.blocked_reason:
            # Resume keeps the previous blocker internally until one resumed
            # task has genuinely started and settled.  Name it historically so
            # neither the model nor downstream code can confuse it with the
            # Goal's current state (which is active/paused here).
            rendered["resumeBlockedReason"] = goal.blocked_reason
        return rendered

    async def compensate_activation_failure(
        self,
        context_value: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Close an accepted Goal task that could not enter TaskRuntime."""

        context = GoalTurnContext.from_task_detail(context_value)
        if context is None:
            return None
        goal = await self._storage.get_goal_by_id(context.goal_id)
        if goal is None:
            return None
        async with self._lock(goal.session_key):
            updated = await self._storage.compensate_goal_activation_failure(context)
            if updated is None:
                return None
            self._revoke_authority(goal.session_key)
            await self._emit_goal(
                updated,
                event_type="updated",
                session_key=updated.session_key,
                session_id=updated.session_id,
                epoch=updated.session_epoch,
                state_revision=updated.state_revision,
                progress_revision=updated.progress_revision,
            )
            snapshot = await self.snapshot(updated, include_runtime_defer=False)
            assert snapshot is not None
            return snapshot

    async def on_task_activation(
        self,
        session_key: str,
        task_id: str,
        run_kind: str,
        collaboration_mode: str,
        candidate_value: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        if run_kind in {
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
        }:
            return None
        candidate = GoalClaimCandidate.from_task_detail(candidate_value)
        if candidate is None:
            return None
        key = canonicalize_session_key(session_key)
        async with self._lock(key):
            try:
                accepted = await self._storage.claim_goal_for_queued_task(
                    candidate=candidate,
                    task_id=task_id,
                    frozen_collaboration_mode=collaboration_mode,
                )
            except Exception:
                log.exception(
                    "goal.activation_claim_failed",
                    session_key=key,
                    task_id=task_id,
                )
                current = await self._storage.get_goal(key)
                if (
                    current is not None
                    and current.session_id == candidate.session_id
                    and current.session_epoch == candidate.epoch
                    and current.goal_id == candidate.goal_id
                    and current.status == GoalStatus.ACTIVE.value
                ):
                    try:
                        paused = await self._storage.pause_goal_for_system(
                            session_key=key,
                            goal_id=current.goal_id,
                            expected_state_revision=current.state_revision,
                            reason="activation_failed",
                        )
                    except Exception:
                        log.exception(
                            "goal.activation_claim_compensation_failed",
                            session_key=key,
                            task_id=task_id,
                        )
                    else:
                        if paused is None:
                            return None
                        self._revoke_authority(key)
                        await self._emit_goal(
                            paused,
                            event_type="updated",
                            session_key=key,
                            session_id=paused.session_id,
                            epoch=paused.session_epoch,
                            state_revision=paused.state_revision,
                            progress_revision=paused.progress_revision,
                        )
                return None
            if accepted is None:
                return None
            await self._emit_goal(
                accepted.goal,
                event_type="updated",
                session_key=key,
                session_id=accepted.goal.session_id,
                epoch=accepted.goal.session_epoch,
                state_revision=accepted.goal.state_revision,
                progress_revision=accepted.goal.progress_revision,
            )
            return dict(accepted.context.as_task_detail())

    async def on_task_lifecycle(self, event: TaskLifecycleEvent) -> None:
        if event.phase in {"queued", "running"}:
            task = await self._storage.get_agent_task(event.task_id)
            details = (
                task.details if task is not None and isinstance(task.details, dict) else {}
            )
            context = effective_goal_turn_context(details)
            if context is None:
                return
            goal = await self._storage.get_goal_by_id(context.goal_id)
            if (
                goal is not None
                and goal.session_id == context.session_id
                and goal.session_epoch == context.epoch
                and goal.active_task_id == context.task_id
            ):
                await self._emit_goal(
                    goal,
                    event_type="updated",
                    session_key=goal.session_key,
                    session_id=goal.session_id,
                    epoch=goal.session_epoch,
                    state_revision=goal.state_revision,
                    progress_revision=goal.progress_revision,
                )
            return
        if event.phase != "terminal":
            return
        task = await self._storage.get_agent_task(event.task_id)
        details = task.details if task is not None and isinstance(task.details, dict) else {}
        context = effective_goal_turn_context(details)
        if context is None:
            return
        key = canonicalize_session_key(event.session_key)
        async with self._lock(key):
            if not event.terminal_persisted:
                updated = await self._storage.compensate_terminal_persistence_failure(
                    context
                )
            else:
                cancellation = details.get("cancellation")
                cancellation_source = (
                    str(cancellation.get("source") or "")
                    if isinstance(cancellation, dict)
                    else ""
                )
                successor_expected = False
                if (
                    task is not None
                    and task.status == AgentTaskStatus.CANCELLED
                    and cancellation_source
                    in {"overflow_drop", "queue_interrupt", "queue_overflow", "queue_steer"}
                ):
                    successor_expected = await self._storage.has_queued_goal_successor(
                        session_key=event.session_key,
                        context=context,
                    )
                turn_outcome = details.get("turn_outcome")
                failure_kind = (
                    str(turn_outcome.get("failure_kind") or "").lower()
                    if isinstance(turn_outcome, dict)
                    else ""
                )
                from opensquilla.provider.failures import ProviderFailureKind

                try:
                    updated = await self._storage.settle_goal_task(
                        context,
                        max_turns=int(getattr(self.config, "max_turns", 50)),
                        runtime_budget_seconds=int(
                            getattr(self.config, "runtime_budget_seconds", 3600)
                        ),
                        usage_limited=(
                            failure_kind
                            == ProviderFailureKind.INSUFFICIENT_CREDITS.value
                        ),
                        successor_expected=successor_expected,
                        process_restart=cancellation_source == "gateway_shutdown",
                    )
                except GoalConflictError:
                    raise
                except Exception:
                    # The terminal AgentTask is already durable, but Goal
                    # settlement could not be committed.  Fail closed in a
                    # second transaction instead of allowing the idle hook to
                    # spend again against an owner that was never cleared.
                    log.exception(
                        "goal.terminal_settlement_persistence_failed",
                        session_key=key,
                        task_id=context.task_id,
                    )
                    updated = (
                        await self._storage.compensate_terminal_persistence_failure(
                            context
                        )
                    )
            if updated is not None:
                if updated.status != GoalStatus.ACTIVE.value:
                    self._revoke_authority(key)
                classification = "active"
                if updated.status == GoalStatus.COMPLETE.value:
                    classification = "complete"
                elif updated.status == GoalStatus.BLOCKED.value:
                    classification = "blocked"
                elif updated.status == GoalStatus.USAGE_LIMITED.value:
                    classification = "usage_limited"
                elif updated.pause_reason in {"runtime_limit", "turn_limit"}:
                    classification = str(updated.pause_reason)
                    _emit_goal_metric(
                        "goal_guardrail_pauses_total",
                        guardrail=classification,
                    )
                elif updated.status == GoalStatus.PAUSED.value:
                    classification = "paused"
                _emit_goal_metric(
                    "goal_settlements_total",
                    status=updated.status,
                    classification=classification,
                )
                _emit_goal_metric(
                    "goal_turns_settled",
                    value=updated.turns_settled,
                )
                _emit_goal_metric(
                    "goal_active_time_ms",
                    value=updated.active_time_ms,
                )
                _emit_goal_metric(
                    "goal_tokens_total",
                    value=updated.total_tokens,
                )
                await self._emit_goal(
                    updated,
                    event_type="updated",
                    session_key=updated.session_key,
                    session_id=updated.session_id,
                    epoch=updated.session_epoch,
                    state_revision=updated.state_revision,
                    progress_revision=updated.progress_revision,
                )

    async def on_runtime_idle(self, session_key: str) -> None:
        self.schedule_idle_evaluation(session_key)

    def schedule_idle_evaluation(self, session_key: str) -> None:
        if self._closed:
            return
        key = canonicalize_session_key(session_key)
        current = self._kick_tasks.get(key)
        if current is not None and not current.done():
            self._kick_dirty.add(key)
            return
        task = asyncio.create_task(
            self._run_scheduled_kick(key),
            name=f"goal-idle:{key}",
        )
        self._kick_tasks[key] = task

    async def _run_scheduled_kick(self, session_key: str) -> None:
        try:
            while True:
                self._kick_dirty.discard(session_key)
                try:
                    await self._kick_if_idle(session_key)
                except Exception:
                    log.warning(
                        "goal.idle_evaluation_failed",
                        session_key=session_key,
                        exc_info=True,
                    )
                if session_key not in self._kick_dirty:
                    break
        finally:
            current = self._kick_tasks.get(session_key)
            if current is asyncio.current_task():
                self._kick_tasks.pop(session_key, None)

    async def _kick_if_idle(self, session_key: str) -> None:
        if self._closed:
            return
        session_key = canonicalize_session_key(session_key)
        goal = await self._storage.get_goal(session_key)
        if goal is None or goal.status != GoalStatus.ACTIVE.value or goal.active_task_id:
            if goal is not None and goal.active_task_id:
                _emit_goal_metric(
                    "goal_continuation_deferred_total",
                    reason="busy",
                )
            return
        session = await self._storage.get_session(session_key)
        if (
            session is not None
            and str(getattr(session, "collaboration_mode", "default")) == "plan"
        ):
            _emit_goal_metric(
                "goal_continuation_deferred_total",
                reason="plan_mode",
            )
            return

        async def _pause_current(reason: str) -> None:
            async with self._lock(session_key):
                current = await self._storage.get_goal(session_key)
                if (
                    current is None
                    or current.goal_id != goal.goal_id
                    or current.status != GoalStatus.ACTIVE.value
                    or current.active_task_id is not None
                ):
                    return
                try:
                    paused = await self._storage.pause_goal_for_system(
                        session_key=session_key,
                        goal_id=current.goal_id,
                        expected_state_revision=current.state_revision,
                        reason=reason,
                    )
                except GoalConflictError:
                    return
                if paused is None:
                    return
                self._revoke_authority(session_key)
                await self._emit_goal(
                    paused,
                    event_type="updated",
                    session_key=session_key,
                    session_id=paused.session_id,
                    epoch=paused.session_epoch,
                    state_revision=paused.state_revision,
                    progress_revision=paused.progress_revision,
                )

        if not self.execution_enabled:
            _emit_goal_metric(
                "goal_continuation_deferred_total",
                reason="feature_disabled",
            )
            await _pause_current("feature_disabled")
            return
        lease = self._lease_for(goal)
        if lease is None:
            _emit_goal_metric("goal_active_without_owner_total")
            _emit_goal_metric(
                "goal_continuation_deferred_total",
                reason="owner_disconnected",
            )
            return
        from opensquilla.gateway.websocket import get_registry

        connection = get_registry().get(lease.owner_connection_id)
        if connection is None:
            self._detach_authority(session_key, expected=lease)
            return
        principal = connection.principal
        next_seq = goal.continuation_seq + 1
        task_id = automatic_goal_task_id(
            goal.goal_id,
            goal.objective_revision,
            next_seq,
        )
        context = GoalTurnContext(
            session_id=goal.session_id,
            epoch=goal.session_epoch,
            goal_id=goal.goal_id,
            objective_revision=goal.objective_revision,
            objective_snapshot=goal.objective,
            task_id=task_id,
            continuation_seq=next_seq,
            automatic=True,
        )
        envelope = self._route_for(
            lease=lease,
            goal=goal,
            principal=principal,
            source_name="goals.continue",
        )
        envelope.metadata.update(
            {
                "client_message_id": task_id,
                "surface_id": f"goal:{goal.goal_id}",
                "turn_context_intent": "goal_continuation",
                "turn_context_disposition": "applied",
                "turn_context_revision": 1,
                "goal_id": goal.goal_id,
                "input_mode": "system_event",
            }
        )
        if session is None:
            self._revoke_authority(session_key)
            return
        try:
            workspace_guard, accepted_run_mode_override = await self._prepare_execution_envelope(
                envelope,
                session=session,
                principal=principal,
            )
        except Exception:
            log.warning(
                "goal.continuation_execution_preflight_failed",
                session_key=session_key,
                exc_info=True,
            )
            _emit_goal_metric(
                "goal_continuation_deferred_total",
                reason="execution_preflight_failed",
            )
            await _pause_current("activation_failed")
            return
        async with self._task_runtime.collect_admission(session_key):
            # Fast user-priority/idle check.  The intent fence and SQLite CAS
            # below are still the linearization points and repeat this check.
            has_user_intent = await self._task_runtime.has_explicit_ingress_intent(
                session_key
            )
            has_session_work = await self._task_runtime.has_session_work(session_key)
            if has_user_intent or has_session_work:
                _emit_goal_metric(
                    "goal_continuation_deferred_total",
                    reason="pending_user" if has_user_intent else "busy",
                )
                return
            reservation = await reserve_turn_via_runtime(
                self._task_runtime,
                envelope,
                _AUTOMATIC_GOAL_MESSAGE,
                mode="followup",
                run_kind="goal",
                no_memory_capture=True,
                input_mode="system_event",
                persist_input=False,
                history_has_persisted_user=False,
                goal_context=context.as_task_detail(),
                turn_id=task_id,
                accepted_run_mode_override=accepted_run_mode_override,
                update_envelope_cache=False,
            )
            try:
                await self._task_runtime.freeze_acceptance(reservation)
            except BaseException:
                await self._task_runtime.abort_reservation(reservation)
                raise

            async def _commit_and_activate() -> None:
                async with self._task_runtime.automatic_ingress_fence(
                    session_key
                ) as allowed:
                    if not allowed:
                        await self._task_runtime.abort_reservation(reservation)
                        return
                    async with self._lock(session_key):
                        current = await self._storage.get_goal(session_key)
                        live_lease = (
                            self._lease_for(current) if current is not None else None
                        )
                        if (
                            self._closed
                            or not self.execution_enabled
                            or current is None
                            or current.session_id != goal.session_id
                            or current.session_epoch != goal.session_epoch
                            or current.goal_id != goal.goal_id
                            or current.status != GoalStatus.ACTIVE.value
                            or current.active_task_id is not None
                            or live_lease != lease
                        ):
                            await self._task_runtime.abort_reservation(reservation)
                            return
                        try:
                            accepted = await self._storage.accept_goal_continuation(
                                expected=ExpectedGoal(
                                    session_id=goal.session_id,
                                    epoch=goal.session_epoch,
                                    goal_id=goal.goal_id,
                                    state_revision=goal.state_revision,
                                ),
                                expected_continuation_seq=goal.continuation_seq,
                                task_record=reservation.task_record,
                                max_turns=int(getattr(self.config, "max_turns", 50)),
                                runtime_budget_seconds=int(
                                    getattr(
                                        self.config,
                                        "runtime_budget_seconds",
                                        3600,
                                    )
                                ),
                                workspace_guard=workspace_guard,
                            )
                        except GoalConflictError:
                            _emit_goal_metric(
                                "goal_stale_cas_total",
                                operation="continuation",
                            )
                            await self._task_runtime.abort_reservation(reservation)
                            return
                        except BaseException:
                            await self._task_runtime.abort_reservation(reservation)
                            raise
                        if isinstance(accepted, GoalGuardrailPause):
                            self._revoke_authority(session_key)
                            await self._task_runtime.abort_reservation(reservation)
                            _emit_goal_metric(
                                "goal_guardrail_pauses_total",
                                guardrail=accepted.reason,
                            )
                            await self._emit_goal(
                                accepted.goal,
                                event_type="updated",
                                session_key=session_key,
                                session_id=accepted.goal.session_id,
                                epoch=accepted.goal.session_epoch,
                                state_revision=accepted.goal.state_revision,
                                progress_revision=accepted.goal.progress_revision,
                            )
                            return
                        try:
                            accepted_current = await self._storage.get_goal(session_key)
                        except Exception:
                            # Durable continuation acceptance has already
                            # installed the AgentTask/Goal owner.  A failed
                            # post-commit authority read must therefore take
                            # the same fail-closed path as activation failure;
                            # otherwise the runtime reservation and durable
                            # owner are stranded while a later restart is the
                            # only way to recover them.
                            log.exception(
                                "goal.continuation_post_accept_read_failed",
                                session_key=session_key,
                                task_id=accepted.context.task_id,
                            )
                            try:
                                compensated = (
                                    await self._storage.compensate_goal_activation_failure(
                                        accepted.context
                                    )
                                )
                            except Exception:
                                compensated = None
                                log.exception(
                                    "goal.continuation_compensation_failed",
                                    session_key=session_key,
                                    task_id=accepted.context.task_id,
                                )
                            finally:
                                self._revoke_authority(session_key)
                                await self._task_runtime.abort_reservation(reservation)
                            if compensated is not None:
                                await self._emit_goal(
                                    compensated,
                                    event_type="updated",
                                    session_key=session_key,
                                    session_id=compensated.session_id,
                                    epoch=compensated.session_epoch,
                                    state_revision=compensated.state_revision,
                                    progress_revision=compensated.progress_revision,
                                )
                            return
                        accepted_lease = (
                            self._lease_for(accepted_current)
                            if accepted_current is not None
                            else None
                        )
                        accepted_grant = (
                            self._grant_for(accepted_current)
                            if accepted_current is not None
                            else None
                        )
                        activation_pause_reason: str | None = None
                        if self._closed:
                            activation_pause_reason = "process_restart"
                        elif not self.execution_enabled:
                            activation_pause_reason = "feature_disabled"
                        elif (
                            accepted_current is None
                            or accepted_current.session_id != accepted.goal.session_id
                            or accepted_current.session_epoch != accepted.goal.session_epoch
                            or accepted_current.goal_id != accepted.goal.goal_id
                            or accepted_current.active_task_id != accepted.context.task_id
                        ):
                            activation_pause_reason = "activation_failed"
                        elif accepted_lease is not None and not self._same_continuity(
                            accepted_lease,
                            lease,
                        ):
                            activation_pause_reason = "activation_failed"
                        elif accepted_lease is None and (
                            accepted_grant is None
                            or not self._same_continuity(accepted_grant, lease)
                        ):
                            activation_pause_reason = "activation_failed"
                        if activation_pause_reason is not None:
                            try:
                                compensated = (
                                    await self._storage.compensate_goal_activation_failure(
                                        accepted.context,
                                        reason=activation_pause_reason,
                                    )
                                )
                            finally:
                                self._revoke_authority(session_key)
                                await self._task_runtime.abort_reservation(reservation)
                            if compensated is not None:
                                await self._emit_goal(
                                    compensated,
                                    event_type="updated",
                                    session_key=session_key,
                                    session_id=compensated.session_id,
                                    epoch=compensated.session_epoch,
                                    state_revision=compensated.state_revision,
                                    progress_revision=compensated.progress_revision,
                                )
                            return
                        try:
                            await self._task_runtime.activate(reservation)
                        except (Exception, asyncio.CancelledError) as exc:
                            log.exception(
                                "goal.continuation_activation_failed",
                                session_key=session_key,
                                task_id=accepted.context.task_id,
                            )
                            if reservation.activated:
                                log.warning(
                                    "goal.continuation_activation_error_after_start",
                                    session_key=session_key,
                                    task_id=accepted.context.task_id,
                                )
                                await self._emit_goal(
                                    accepted.goal,
                                    event_type="updated",
                                    session_key=session_key,
                                    session_id=accepted.goal.session_id,
                                    epoch=accepted.goal.session_epoch,
                                    state_revision=accepted.goal.state_revision,
                                    progress_revision=accepted.goal.progress_revision,
                                )
                            else:
                                try:
                                    compensated = (
                                        await self._storage.compensate_goal_activation_failure(
                                            accepted.context
                                        )
                                    )
                                except Exception:
                                    compensated = None
                                    log.exception(
                                        "goal.continuation_compensation_failed",
                                        session_key=session_key,
                                        task_id=accepted.context.task_id,
                                    )
                                self._revoke_authority(session_key)
                                await self._task_runtime.abort_reservation(reservation)
                                if compensated is not None:
                                    await self._emit_goal(
                                        compensated,
                                        event_type="updated",
                                        session_key=session_key,
                                        session_id=compensated.session_id,
                                        epoch=compensated.session_epoch,
                                        state_revision=compensated.state_revision,
                                        progress_revision=compensated.progress_revision,
                                    )
                            if isinstance(exc, asyncio.CancelledError):
                                raise
                            return
                        _emit_goal_metric(
                            "goal_continuations_total",
                            outcome="accepted",
                        )
                        await self._emit_goal(
                            accepted.goal,
                            event_type="updated",
                            session_key=session_key,
                            session_id=accepted.goal.session_id,
                            epoch=accepted.goal.session_epoch,
                            state_revision=accepted.goal.state_revision,
                            progress_revision=accepted.goal.progress_revision,
                        )

            await complete_durable_ingress(_commit_and_activate())

    async def on_mode_committed(self, session_key: str, mode: str) -> None:
        if mode == "default":
            self.schedule_idle_evaluation(session_key)

    async def on_config_changed(self, *, previous_execution_enabled: bool) -> None:
        """Apply a committed Goal config change to live execution authority.

        Re-enabling never resumes work implicitly.  Disabling is an emergency
        stop for future admissions: existing accepted tasks may settle, while
        every Goal whose lease is owned by this process is durably paused.
        """

        if not previous_execution_enabled or self.execution_enabled:
            return
        granted = list(self._continuity_grants.items())
        for key, grant in granted:
            async with self._lock(key):
                current_grant = self._continuity_grants.get(key)
                if current_grant != grant:
                    continue
                self._revoke_authority(key)
                goal = await self._storage.get_goal(key)
                if (
                    goal is None
                    or goal.goal_id != grant.goal_id
                    or goal.session_id != grant.session_id
                    or goal.session_epoch != grant.epoch
                    or goal.status != GoalStatus.ACTIVE.value
                ):
                    continue
                try:
                    paused = await self._storage.pause_goal_for_system(
                        session_key=key,
                        goal_id=goal.goal_id,
                        expected_state_revision=goal.state_revision,
                        reason="feature_disabled",
                    )
                except GoalConflictError:
                    continue
                if paused is None:
                    continue
                await self._emit_goal(
                    paused,
                    event_type="updated",
                    session_key=key,
                    session_id=paused.session_id,
                    epoch=paused.session_epoch,
                    state_revision=paused.state_revision,
                    progress_revision=paused.progress_revision,
                )

    async def on_subscription_lost(self, conn_id: str, session_key: str) -> None:
        """Detach transport authority without changing the durable Goal FSM."""

        key = canonicalize_session_key(session_key)
        async with self._lock(key):
            lease = self._leases.get(key)
            if lease is None or lease.owner_connection_id != conn_id:
                return
            self._detach_authority(key, expected=lease)
            goal = await self._storage.get_goal(key)
            if (
                goal is None
                or goal.goal_id != lease.goal_id
                or goal.session_id != lease.session_id
                or goal.session_epoch != lease.epoch
                or goal.status != GoalStatus.ACTIVE.value
            ):
                self._continuity_grants.pop(key, None)

    def revoke_session(
        self,
        session_key: str,
        *,
        session_id: str | None = None,
    ) -> None:
        key = canonicalize_session_key(session_key)
        authority = self._leases.get(key) or self._continuity_grants.get(key)
        if authority is None:
            return
        if session_id is not None and authority.session_id != session_id:
            return
        self._revoke_authority(key)

    async def prepare_shutdown(self) -> None:
        """Stop automatic work and durably pause every lease owned here."""

        if self._closed:
            return
        self._closed = True
        tasks = list(self._kick_tasks.values())
        self._kick_tasks.clear()
        self._kick_dirty.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        granted = list(self._continuity_grants.items())
        self._leases.clear()
        self._continuity_grants.clear()
        for key, grant in granted:
            async with self._lock(key):
                goal = await self._storage.get_goal(key)
                if (
                    goal is None
                    or goal.goal_id != grant.goal_id
                    or goal.session_id != grant.session_id
                    or goal.session_epoch != grant.epoch
                    or goal.status != GoalStatus.ACTIVE.value
                ):
                    continue
                try:
                    paused = await self._storage.pause_goal_for_system(
                        session_key=key,
                        goal_id=goal.goal_id,
                        expected_state_revision=goal.state_revision,
                        reason="process_restart",
                    )
                except GoalConflictError:
                    continue
                if paused is None:
                    continue
                await self._emit_goal(
                    paused,
                    event_type="updated",
                    session_key=key,
                    session_id=paused.session_id,
                    epoch=paused.session_epoch,
                    state_revision=paused.state_revision,
                    progress_revision=paused.progress_revision,
                )

    async def close(self) -> None:
        await self.prepare_shutdown()
