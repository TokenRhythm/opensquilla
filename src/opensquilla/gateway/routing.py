"""Canonical route envelopes for gateway, CLI, scheduler, and subagent turns."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from opensquilla.channels.admission import (
    CHANNEL_ADMIN_VERIFIED_METADATA_KEY,
    has_verified_channel_admin_stamp,
)
from opensquilla.channels.types import IncomingMessage
from opensquilla.run_mode import RunMode, execution_target, normalize_run_mode
from opensquilla.sandbox.run_context import (
    normalize_scope,
    run_context_for_subagent,
    run_context_from_origin_payload,
)
from opensquilla.session.keys import normalize_agent_id, parse_agent_id
from opensquilla.tools.policy import apply_tool_policy_layer
from opensquilla.tools.types import (
    CRON_AGENT_ALLOW,
    CRON_AGENT_DENY,
    SUBAGENT_TOOL_DENY,
    CallerKind,
    InteractionMode,
    ToolContext,
)


class SourceKind(StrEnum):
    """Top-level inbound runtime source."""

    WEB = "web"
    CLI = "cli"
    CHANNEL = "channel"
    CRON = "cron"
    SUBAGENT = "subagent"
    SYSTEM = "system"


PRINCIPAL_HOST_EXECUTE_METADATA_KEY = "principal_host_execute"
_ARTIFACT_MUTATION_WRITER_NAMES = frozenset({"document_apply", "document_patch"})


@dataclass(frozen=True)
class ReplyTarget:
    """External or subscriber target that can receive a reply/announce."""

    kind: str
    channel_name: str | None = None
    channel_type: str | None = None
    to: str | None = None
    account_id: str | None = None
    thread_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteEnvelope:
    """Canonical routing data for one inbound turn request."""

    source_kind: SourceKind
    source_name: str
    agent_id: str
    session_key: str
    session_id: str | None = None
    sender_id: str | None = None
    account_id: str | None = None
    channel_type: str | None = None
    channel_name: str | None = None
    channel_id: str | None = None
    thread_id: str | None = None
    reply_target: ReplyTarget | None = None
    input_provenance: dict[str, Any] = field(default_factory=dict)
    delivery_context: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    interaction_mode: InteractionMode = InteractionMode.INTERACTIVE
    sandbox_run_context_fresh: bool = False
    # Process-local services attached only after durable acceptance. Keeping
    # them out of ``metadata`` prevents serialization of live handles.
    runtime_services: dict[str, Any] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    def delivery_fields(self) -> dict[str, Any]:
        """Return session routing fields derived from the reply target."""
        return delivery_fields_from_envelope(self)

    def tool_context(
        self,
        *,
        is_owner: bool = False,
        host_execute_allowed: bool = False,
        workspace_dir: str | None = None,
        workspace_strict: bool = False,
        default_elevated: str | None = None,
    ) -> ToolContext:
        """Build the ToolContext for this route."""
        return tool_context_from_envelope(
            self,
            is_owner=is_owner,
            host_execute_allowed=host_execute_allowed,
            workspace_dir=workspace_dir,
            workspace_strict=workspace_strict,
            default_elevated=default_elevated,
        )


def _agent_id(agent_id: str | None, session_key: str) -> str:
    return normalize_agent_id(agent_id) if agent_id else parse_agent_id(session_key)


def _thread_id(metadata: dict[str, Any]) -> str | None:
    thread = metadata.get("thread_ts") or metadata.get("thread_id")
    return thread if isinstance(thread, str) and thread else None


def build_channel_route_envelope(
    msg: IncomingMessage,
    *,
    session_key: str,
    session_prefix: str,
    agent_id: str | None = None,
    channel_type: str | None = None,
) -> RouteEnvelope:
    """Build a route for a normalized inbound channel message."""
    metadata = dict(msg.metadata or {})
    # Channel adapters provide transport metadata, not authorization claims.
    # ``channel_dispatch`` stamps this after authenticating the sender against
    # the configured channel-admin mapping.
    metadata.pop("principal_is_owner", None)
    metadata.pop(PRINCIPAL_HOST_EXECUTE_METADATA_KEY, None)
    metadata.pop(CHANNEL_ADMIN_VERIFIED_METADATA_KEY, None)
    metadata.setdefault("run_mode", RunMode.SAFE.value)
    resolved_agent_id = _agent_id(agent_id, session_key)
    resolved_channel_type = channel_type or session_prefix
    account_id = metadata.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        account_id = None
    thread_id = _thread_id(metadata)
    delivery_context = {
        "sender_id": msg.sender_id,
        "channel_id": msg.channel_id,
        **metadata,
    }
    return RouteEnvelope(
        source_kind=SourceKind.CHANNEL,
        source_name=session_prefix,
        agent_id=resolved_agent_id,
        session_key=session_key,
        sender_id=msg.sender_id,
        account_id=account_id,
        channel_type=resolved_channel_type,
        channel_name=session_prefix,
        channel_id=msg.channel_id,
        thread_id=thread_id,
        reply_target=ReplyTarget(
            kind="channel",
            channel_name=session_prefix,
            channel_type=resolved_channel_type,
            to=msg.channel_id,
            account_id=account_id,
            thread_id=thread_id,
            metadata=metadata,
        ),
        input_provenance={
            "kind": "channel_message",
            "source": session_prefix,
        },
        delivery_context=delivery_context,
        metadata=metadata,
        interaction_mode=InteractionMode.UNATTENDED,
    )


def build_cli_route_envelope(
    *,
    session_key: str,
    agent_id: str | None = None,
    source_name: str = "run",
    channel_id: str = "cli:agent",
    sender_id: str | None = None,
    session_id: str | None = None,
    principal_is_owner: bool | None = None,
    principal_host_execute: bool | None = None,
    interaction_mode: InteractionMode | str = InteractionMode.INTERACTIVE,
    elevated: str | None = None,
    run_mode: str | None = None,
) -> RouteEnvelope:
    """Build a route for local CLI input."""
    resolved_interaction_mode = _interaction_mode(interaction_mode)
    metadata: dict[str, Any] = {}
    if principal_is_owner is not None:
        metadata["principal_is_owner"] = principal_is_owner
    if principal_host_execute is not None:
        metadata[PRINCIPAL_HOST_EXECUTE_METADATA_KEY] = bool(principal_host_execute)
    if elevated in ("on", "bypass", "full"):
        metadata["elevated"] = elevated
    try:
        normalized_run_mode = normalize_run_mode(run_mode) if run_mode else None
    except ValueError:
        normalized_run_mode = None
    if normalized_run_mode is not None:
        metadata["run_mode"] = normalized_run_mode.value
    return RouteEnvelope(
        source_kind=SourceKind.CLI,
        source_name=source_name,
        agent_id=_agent_id(agent_id, session_key),
        session_key=session_key,
        session_id=session_id,
        sender_id=sender_id,
        channel_type="cli",
        channel_name="cli",
        channel_id=channel_id,
        input_provenance={"kind": "cli_message", "source": source_name},
        metadata=metadata,
        interaction_mode=resolved_interaction_mode,
    )


def build_web_route_envelope(
    *,
    session_key: str,
    agent_id: str | None = None,
    source_name: str = "web",
    conn_id: str | None = None,
    sender_id: str | None = None,
    channel_id: str | None = None,
    session_id: str | None = None,
    tool_source_kind: str | None = None,
    principal_is_owner: bool | None = None,
    principal_host_execute: bool | None = None,
) -> RouteEnvelope:
    """Build a route for Web/RPC-originated input."""
    resolved_channel_id = channel_id or (f"web:{conn_id}" if conn_id else "web")
    channel_name = "webchat" if resolved_channel_id.startswith("webchat:") else "web"
    metadata: dict[str, Any] = {"conn_id": conn_id}
    if tool_source_kind:
        metadata["tool_source_kind"] = tool_source_kind
    if principal_is_owner is not None:
        metadata["principal_is_owner"] = principal_is_owner
    if principal_host_execute is not None:
        metadata[PRINCIPAL_HOST_EXECUTE_METADATA_KEY] = bool(principal_host_execute)
    return RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name=source_name,
        agent_id=_agent_id(agent_id, session_key),
        session_key=session_key,
        session_id=session_id,
        sender_id=sender_id,
        channel_type="web",
        channel_name=channel_name,
        channel_id=resolved_channel_id,
        reply_target=ReplyTarget(
            kind="web",
            channel_name=channel_name,
            channel_type="web",
            to=conn_id,
        ),
        input_provenance={"kind": "web_message", "source": source_name},
        delivery_context={"sender_id": sender_id, "channel_id": resolved_channel_id},
        metadata=metadata,
        interaction_mode=InteractionMode.INTERACTIVE,
    )


def build_cron_route_envelope(
    job: Any,
    *,
    session_key: str,
    agent_id: str | None = None,
    delivery: Any | None = None,
) -> RouteEnvelope:
    """Build a route for scheduler-originated agent work or delivery."""
    resolved_delivery = delivery if delivery is not None else getattr(job, "delivery", None)
    job_id = str(getattr(job, "id", "unknown"))
    job_name = str(getattr(job, "name", ""))
    sender_id = f"cron-job-{job_id}"
    metadata: dict[str, Any] = {"job_id": job_id, "job_name": job_name}
    creator_is_owner = bool(getattr(job, "creator_is_owner", False))
    creator_host_execute = bool(getattr(job, "creator_host_execute", False))
    trusted_creator_owner = creator_is_owner and creator_host_execute
    if trusted_creator_owner:
        metadata["principal_is_owner"] = True
        metadata["cron_trusted_owner"] = True
    if creator_host_execute:
        metadata[PRINCIPAL_HOST_EXECUTE_METADATA_KEY] = True
        metadata["cron_trusted_host"] = True
    job_run_mode = getattr(job, "run_mode", "")
    if job_run_mode:
        try:
            normalized_job_run_mode = normalize_run_mode(job_run_mode)
        except ValueError:
            normalized_job_run_mode = None
        if normalized_job_run_mode is not None:
            if (
                normalized_job_run_mode is RunMode.FULL
                and not creator_host_execute
            ):
                normalized_job_run_mode = RunMode.SAFE
            metadata["run_mode"] = normalized_job_run_mode.value
            metadata["execution_target"] = execution_target(normalized_job_run_mode)
            if normalized_job_run_mode is RunMode.FULL and creator_host_execute:
                metadata["elevated"] = "full"
    tool_policy = getattr(job, "tool_policy", None)
    if isinstance(tool_policy, dict) and tool_policy:
        metadata["tool_policy"] = dict(tool_policy)
    reply_target = None
    delivery_context = {
        "sender_id": sender_id,
        "channel_id": "",
        "job_id": job_id,
        "job_name": job_name,
    }
    if (
        resolved_delivery is not None
        and getattr(resolved_delivery, "mode", None) != "none"
        and getattr(resolved_delivery, "channel_name", "")
    ):
        channel_name = getattr(resolved_delivery, "channel_name", "")
        channel_id = getattr(resolved_delivery, "channel_id", "")
        account_id = getattr(resolved_delivery, "account_id", "")
        thread_id = getattr(resolved_delivery, "thread_id", "")
        reply_target = ReplyTarget(
            kind="channel",
            channel_name=channel_name,
            channel_type=channel_name,
            to=channel_id,
            account_id=account_id or None,
            thread_id=thread_id or None,
        )
        delivery_context["channel_id"] = channel_id
    return RouteEnvelope(
        source_kind=SourceKind.CRON,
        source_name="cron",
        agent_id=_agent_id(agent_id, session_key),
        session_key=session_key,
        sender_id=sender_id,
        channel_type="cron",
        channel_name="cron",
        channel_id=f"cron:{job_id}",
        reply_target=reply_target,
        input_provenance={"kind": "cron_job", "job_id": job_id},
        delivery_context=delivery_context,
        metadata=metadata,
        interaction_mode=InteractionMode.UNATTENDED,
    )


def build_subagent_route_envelope(
    *,
    session_key: str,
    parent_session_key: str,
    agent_id: str | None = None,
    run_id: str | None = None,
    parent_task_id: str | None = None,
    spawn_depth: int = 0,
    origin: str = "sessions_spawn",
    principal_is_owner: bool | None = None,
    principal_host_execute: bool | None = None,
    elevated: str | None = None,
    run_mode: str | RunMode | None = None,
    sandbox_run_context: Any | None = None,
    sandbox_mounts: list[dict[str, Any]] | None = None,
) -> RouteEnvelope:
    """Build a route for a child subagent run."""
    metadata: dict[str, Any] = {
        "parent_session_key": parent_session_key,
        "run_id": run_id,
        "parent_task_id": parent_task_id,
        "spawn_depth": spawn_depth,
        "origin": origin,
    }
    if principal_is_owner is not None:
        metadata["principal_is_owner"] = bool(principal_is_owner)
    if principal_host_execute is not None:
        metadata[PRINCIPAL_HOST_EXECUTE_METADATA_KEY] = bool(principal_host_execute)
    if elevated in ("on", "bypass", "full"):
        metadata["elevated"] = elevated
    normalized_run_mode: RunMode | None = None
    if run_mode is not None:
        try:
            normalized_run_mode = normalize_run_mode(
                run_mode.value if isinstance(run_mode, RunMode) else str(run_mode)
            )
        except ValueError:
            normalized_run_mode = None
    run_context_payload: dict[str, Any] | None = None
    if sandbox_run_context is not None:
        to_origin_payload = getattr(sandbox_run_context, "to_origin_payload", None)
        if callable(to_origin_payload):
            raw_payload = to_origin_payload()
            if isinstance(raw_payload, dict):
                run_context_payload = dict(raw_payload)
        elif isinstance(sandbox_run_context, dict):
            run_context_payload = dict(sandbox_run_context)
    if run_context_payload is not None:
        hydrated_context = run_context_from_origin_payload(
            run_context_payload,
            source="subagent_parent",
            preserve_materialized_user_grants=True,
        )
        if hydrated_context is not None:
            run_context_payload = run_context_for_subagent(
                hydrated_context
            ).to_origin_payload()
        else:
            for grant_key in ("mounts", "domains", "bundles", "public_network"):
                raw_grants = run_context_payload.get(grant_key)
                if isinstance(raw_grants, list):
                    run_context_payload[grant_key] = [
                        dict(item)
                        for item in raw_grants
                        if isinstance(item, dict)
                        and normalize_scope(item.get("scope"), "chat") != "once"
                    ]
            run_context_payload["temporary_grants"] = []
    if normalized_run_mode is None and run_context_payload is not None:
        try:
            normalized_run_mode = normalize_run_mode(run_context_payload.get("run_mode"))
        except ValueError:
            normalized_run_mode = None
    if normalized_run_mode is not None:
        metadata["run_mode"] = normalized_run_mode.value
    if run_context_payload is not None:
        metadata["sandbox_run_context"] = run_context_payload
    if sandbox_mounts is not None:
        metadata["sandbox_mounts"] = [
            dict(item)
            for item in sandbox_mounts
            if isinstance(item, dict)
            and normalize_scope(item.get("scope"), "chat") != "once"
        ]
    if normalized_run_mode is not None:
        metadata["run_mode"] = normalized_run_mode.value
        if normalized_run_mode is RunMode.FULL and principal_is_owner:
            metadata["elevated"] = "full"
    return RouteEnvelope(
        source_kind=SourceKind.SUBAGENT,
        source_name="subagent",
        agent_id=_agent_id(agent_id, session_key),
        session_key=session_key,
        channel_type="subagent",
        channel_name="subagent",
        channel_id=run_id,
        input_provenance={
            "kind": "subagent_task",
            "parent_session_key": parent_session_key,
            "run_id": run_id,
            "parent_task_id": parent_task_id,
        },
        metadata=metadata,
        interaction_mode=InteractionMode.UNATTENDED,
        sandbox_run_context_fresh=run_context_payload is not None,
    )


def delivery_fields_from_envelope(envelope: RouteEnvelope) -> dict[str, Any]:
    """Translate a channel-capable route into SessionNode delivery fields."""
    target = envelope.reply_target
    if target is None or target.kind != "channel":
        return {}
    return {
        "last_channel": target.channel_name,
        "last_to": target.to,
        "last_account_id": target.account_id,
        "last_thread_id": target.thread_id,
        "delivery_context": dict(envelope.delivery_context),
    }


def _filtered_legacy_sandbox_mounts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    mounts: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if normalize_scope(item.get("scope"), "once") != "chat":
            continue
        mounts.append(dict(item))
    return mounts


def tool_context_from_envelope(
    envelope: RouteEnvelope,
    *,
    is_owner: bool = False,
    host_execute_allowed: bool = False,
    workspace_dir: str | None = None,
    workspace_strict: bool = False,
    default_elevated: str | None = None,
) -> ToolContext:
    """Build the runtime ToolContext from the canonical route envelope."""
    caller_kind = _caller_kind(envelope.source_kind)
    channel_admin_verified = (
        caller_kind is CallerKind.CHANNEL
        and is_owner
        and has_verified_channel_admin_stamp(envelope)
    )
    if caller_kind is CallerKind.CHANNEL:
        # A Channel caller can gain owner capabilities only through the
        # authenticated ingress stamp.  Do this before profile and run-mode
        # resolution so a generic ``is_owner=True`` cannot widen a Channel
        # context if a future caller forgets the ingress boundary.
        is_owner = channel_admin_verified
        host_execute_allowed = channel_admin_verified
    full_access_allowed = is_owner or host_execute_allowed
    allowed_tools: set[str] | None = None
    denied_tools: set[str] = set()
    interaction_mode = _interaction_mode(envelope.interaction_mode)
    cron_trusted_owner = (
        caller_kind is CallerKind.CRON
        and bool(envelope.metadata.get("cron_trusted_owner"))
        and is_owner
    )
    cron_trusted_host = (
        caller_kind is CallerKind.CRON
        and bool(envelope.metadata.get("cron_trusted_host"))
        and host_execute_allowed
    )
    cron_trusted = cron_trusted_owner or cron_trusted_host
    if caller_kind is CallerKind.CRON:
        if not cron_trusted:
            allowed_tools = set(CRON_AGENT_ALLOW)
            denied_tools = set(CRON_AGENT_DENY)
    elif caller_kind is CallerKind.SUBAGENT:
        denied_tools = set(SUBAGENT_TOOL_DENY)
    guest_safe = bool(envelope.metadata.get("guest_safe"))
    if guest_safe:
        from opensquilla.tools.visibility import guest_safe_tool_allowlist

        guest_allowlist = set(guest_safe_tool_allowlist())
        allowed_tools = (
            guest_allowlist
            if allowed_tools is None
            else allowed_tools & guest_allowlist
        )
    source_kind = envelope.metadata.get("tool_source_kind") or envelope.source_kind.value
    source_name = envelope.metadata.get("tool_source_name") or envelope.source_name
    legacy_elevated = envelope.metadata.get("elevated")
    elevated = None
    run_mode_value = envelope.metadata.get("run_mode")
    if run_mode_value:
        try:
            run_mode = normalize_run_mode(run_mode_value)
        except ValueError:
            run_mode = None
        if run_mode == RunMode.FULL and not full_access_allowed:
            run_mode = RunMode.SAFE
    elif legacy_elevated == "on" and is_owner:
        run_mode = RunMode.SAFE
    elif legacy_elevated in ("bypass", "full") and full_access_allowed:
        run_mode = RunMode.FULL
    elif default_elevated in ("bypass", "full") and full_access_allowed:
        run_mode = RunMode.FULL
    else:
        run_mode = None
    if run_mode == RunMode.FULL and full_access_allowed:
        elevated = "full"
    elif legacy_elevated == "on" and is_owner:
        elevated = legacy_elevated
    sandbox_run_context_fresh = bool(
        getattr(envelope, "sandbox_run_context_fresh", False)
    )
    sandbox_run_context = run_context_from_origin_payload(
        envelope.metadata.get("sandbox_run_context"),
        source="route_metadata",
        preserve_materialized_user_grants=sandbox_run_context_fresh,
    )
    effective_workspace_dir = (
        sandbox_run_context.workspace
        if sandbox_run_context is not None and sandbox_run_context.workspace
        else workspace_dir
    )
    if (
        sandbox_run_context is not None
        and sandbox_run_context.run_mode == RunMode.FULL
        and not full_access_allowed
    ):
        sandbox_run_context = replace(sandbox_run_context, run_mode=RunMode.SAFE)
    if sandbox_run_context_fresh and sandbox_run_context is not None:
        sandbox_mounts = sandbox_run_context.to_origin_payload()["mounts"]
    else:
        sandbox_mounts = _filtered_legacy_sandbox_mounts(
            envelope.metadata.get("sandbox_mounts")
        )
    artifact_context = envelope.runtime_services.get("artifact_context")
    artifact_session = envelope.runtime_services.get("artifact_session")
    desktop_artifact_bridge = envelope.runtime_services.get("desktop_artifact_bridge")
    artifact_preview_service = envelope.runtime_services.get("artifact_preview_service")
    artifact_event_emitter = envelope.runtime_services.get("artifact_event_emitter")
    generated_artifact_adopter = envelope.runtime_services.get(
        "generated_artifact_adopter"
    )
    artifact_tool_names: object = getattr(artifact_context, "tool_names", frozenset())
    surfaced_tools: set[str] | None = None
    exclusive_tools: frozenset[str] | None = None
    artifact_mutation_attempt_controller: Any | None = None
    artifact_candidate_loop_controller: Any | None = None
    from opensquilla.gateway.artifact_contexts import (
        DOCUMENT_CONTEXT_WORKSPACE_MUTATOR_DENY,
        BoundDocumentContext,
        BoundPromptAnnotationContext,
    )

    if (
        isinstance(artifact_context, (BoundDocumentContext, BoundPromptAnnotationContext))
        and artifact_session is not None
        and caller_kind is CallerKind.WEB
        and interaction_mode is InteractionMode.INTERACTIVE
        and is_owner
        and not guest_safe
        and isinstance(artifact_tool_names, frozenset)
    ):
        surfaced_tools = set(artifact_tool_names)
        if isinstance(artifact_context, BoundPromptAnnotationContext):
            exclusive_tools = frozenset(artifact_tool_names)
            allowed_tools = (
                set(exclusive_tools)
                if allowed_tools is None
                else set(allowed_tools) & exclusive_tools
            )
        elif isinstance(artifact_context, BoundDocumentContext):
            # A workspace file is not the bound Document. Keep ordinary read/search and
            # authoring capabilities additive, but fail closed on the three direct source
            # mutators that models commonly mistake for a Document update.
            denied_tools.update(DOCUMENT_CONTEXT_WORKSPACE_MUTATOR_DENY)
        if _ARTIFACT_MUTATION_WRITER_NAMES & artifact_tool_names:
            task_id = str(envelope.metadata.get("task_id") or "").strip()
            document_id = str(getattr(artifact_context, "document_id", "") or "").strip()
            revision_id = str(getattr(artifact_context, "revision_id", "") or "").strip()
            if task_id and document_id and revision_id:
                from opensquilla.artifact_session import (
                    ArtifactCandidateLoopController,
                    ArtifactMutationAttemptController,
                    ArtifactSessionService,
                )

                if isinstance(artifact_session, ArtifactSessionService):
                    if (
                        isinstance(artifact_context, BoundPromptAnnotationContext)
                        and "document_finish" in artifact_tool_names
                    ):
                        artifact_candidate_loop_controller = ArtifactCandidateLoopController(
                            artifact_session,
                            document_id=document_id,
                            base_revision_id=revision_id,
                            turn_id=task_id,
                        )
                    else:
                        artifact_mutation_attempt_controller = ArtifactMutationAttemptController(
                            artifact_session,
                            document_id=document_id,
                            base_revision_id=revision_id,
                            turn_id=task_id,
                        )
    else:
        artifact_event_emitter = None
        desktop_artifact_bridge = None
    if not callable(artifact_event_emitter):
        artifact_event_emitter = None
    if not (
        callable(generated_artifact_adopter)
        and caller_kind is CallerKind.WEB
        and envelope.source_kind is SourceKind.WEB
        and interaction_mode is InteractionMode.INTERACTIVE
        and is_owner
        and not guest_safe
    ):
        generated_artifact_adopter = None
    if desktop_artifact_bridge is not None:
        from opensquilla.gateway.desktop_artifact_bridge import (
            DesktopArtifactBridgeClient,
        )

        if not isinstance(desktop_artifact_bridge, DesktopArtifactBridgeClient):
            desktop_artifact_bridge = None
    ctx = ToolContext(
        is_owner=is_owner,
        channel_admin_verified=channel_admin_verified,
        caller_kind=caller_kind,
        interaction_mode=interaction_mode,
        subagent_depth=int(envelope.metadata.get("spawn_depth") or 0),
        agent_id=envelope.agent_id,
        workspace_dir=effective_workspace_dir,
        guest_safe=guest_safe,
        environment=(
            {
                str(key): str(value)
                for key, value in envelope.metadata.get("guest_environment", {}).items()
            }
            if isinstance(envelope.metadata.get("guest_environment"), dict)
            else None
        ),
        workspace_strict=workspace_strict,
        run_mode=run_mode.value if run_mode is not None else None,
        sandbox_mounts=sandbox_mounts,
        sandbox_run_context=sandbox_run_context,
        session_key=envelope.session_key,
        channel_kind=envelope.channel_name or envelope.channel_type,
        channel_id=envelope.channel_id,
        sender_id=envelope.sender_id,
        source_kind=source_kind,
        source_name=source_name,
        allowed_tools=allowed_tools,
        denied_tools=denied_tools,
        surfaced_tools=surfaced_tools,
        elevated=elevated,
        tool_policy=(
            envelope.metadata.get("tool_policy") if cron_trusted else None
        ),
        task_id=(
            str(envelope.metadata["task_id"])
            if envelope.metadata.get("task_id")
            else None
        ),
        collaboration_mode=str(
            envelope.metadata.get("collaboration_mode") or "default"
        ),
        collaboration_revision=int(
            envelope.metadata.get("collaboration_revision") or 0
        ),
        active_plan_revision_id=(
            str(envelope.metadata["active_plan_revision_id"])
            if envelope.metadata.get("active_plan_revision_id")
            else None
        ),
        plan_run_id=(
            str(envelope.metadata["plan_run_id"])
            if envelope.metadata.get("plan_run_id")
            else None
        ),
        plan_storage=envelope.runtime_services.get("plan_storage"),
        plan_event_emitter=envelope.runtime_services.get("plan_event_emitter"),
        user_input_provider=envelope.runtime_services.get("user_input_provider"),
        plan_revision=envelope.runtime_services.get("plan_revision"),
        plan_run=envelope.runtime_services.get("plan_run"),
        goal_context=envelope.runtime_services.get("goal_context"),
        goal_service=envelope.runtime_services.get("goal_service"),
        artifact_context=artifact_context,
        artifact_session=artifact_session,
        desktop_artifact_bridge=desktop_artifact_bridge,
        artifact_event_emitter=artifact_event_emitter,
        generated_artifact_adopter=generated_artifact_adopter,
        exclusive_tools=exclusive_tools,
        artifact_mutation_attempt_controller=(
            artifact_mutation_attempt_controller
        ),
        artifact_candidate_loop_controller=artifact_candidate_loop_controller,
        artifact_preview_service=artifact_preview_service,
    )
    if sandbox_run_context_fresh:
        # Runtime-only authority marker copied from the RouteEnvelope field,
        # never from mutable metadata. Execution-time workspace validation is
        # the only ingress that sets this field for ordinary turns.
        setattr(ctx, "_sandbox_run_context_fresh", True)
    if caller_kind is CallerKind.CRON:
        if not cron_trusted:
            ctx = apply_tool_policy_layer(
                ctx,
                envelope.metadata.get("tool_policy"),
                available_tools=CRON_AGENT_ALLOW | CRON_AGENT_DENY,
                hard_denied=CRON_AGENT_DENY,
            )
    return ctx


def _interaction_mode(value: InteractionMode | str) -> InteractionMode:
    if isinstance(value, InteractionMode):
        return value
    return InteractionMode(str(value))


def _caller_kind(source_kind: SourceKind) -> CallerKind:
    match source_kind:
        case SourceKind.WEB:
            return CallerKind.WEB
        case SourceKind.CLI:
            return CallerKind.CLI
        case SourceKind.CHANNEL:
            return CallerKind.CHANNEL
        case SourceKind.CRON:
            return CallerKind.CRON
        case SourceKind.SUBAGENT:
            return CallerKind.SUBAGENT
        case SourceKind.SYSTEM:
            return CallerKind.AGENT
