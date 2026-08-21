"""Tool registry type definitions: ToolSpec, ToolContext, registered ToolHandler."""

from __future__ import annotations

import contextvars
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from opensquilla.contracts.turn_execution import SurfaceCapabilities
from opensquilla.sandbox.operation_runtime import SandboxToolDescriptor


class CallerKind(StrEnum):
    """Entry-point caller type — used in ToolContext for filtering decisions."""

    AGENT = "agent"
    SUBAGENT = "subagent"
    CRON = "cron"
    CHANNEL = "channel"
    CLI = "cli"
    WEB = "web"


class InteractionMode(StrEnum):
    """Whether the entry point has a live operator available for tool approvals."""

    INTERACTIVE = "interactive"
    UNATTENDED = "unattended"


class PlanAccess(StrEnum):
    """Whether a tool may be exposed or dispatched while planning."""

    DENY = "deny"
    READ_ONLY = "read_only"
    CONTROL = "control"


@dataclass
class ToolContext:
    """Constructed at the entry point, flows through to tool list building.

    Every entry point (gateway, CLI, cron, channel) must explicitly construct
    a ToolContext. There is no default — omitting it is a TypeError.
    """

    is_owner: bool = False
    caller_kind: CallerKind = CallerKind.AGENT
    interaction_mode: InteractionMode = InteractionMode.INTERACTIVE
    subagent_depth: int = 0
    agent_id: str = "main"
    workspace_dir: str | None = None
    guest_safe: bool = False
    environment: dict[str, str] | None = None
    memory_source_dir: str | None = None
    workspace_strict: bool = False
    scratch_dir: str | None = None
    workspace_lockdown: bool = False
    workspace_write_deny_globs: list[str] = field(default_factory=list)
    run_mode: str | None = None
    sandbox_mounts: list[dict[str, Any]] = field(default_factory=list)
    sandbox_run_context: Any | None = None
    source_diff_preservation_mode: str = "log"
    source_diff_candidate_mode: str = "log"
    source_diff_candidates: list[dict[str, Any]] = field(default_factory=list)
    source_diff_candidate_counter: int = 0
    file_edit_requires_fresh_read: bool = False
    file_edit_flexible_recovery: bool = True
    missing_required_argument_shape_guidance: bool = False
    session_key: str | None = None
    channel_kind: str | None = None
    channel_id: str | None = None
    sender_id: str | None = None
    source_kind: str | None = None
    source_name: str | None = None
    task_id: str | None = None
    artifact_media_root: str | None = None
    artifact_session_id: str | None = None
    tool_result_store_dir: str | None = None
    tool_result_store_session_id: str | None = None
    artifact_max_bytes: int | None = None
    artifact_disk_budget_bytes: int | None = None
    published_artifacts: list[dict[str, Any]] = field(default_factory=list)
    workspace_file_reads: list[dict[str, Any]] = field(default_factory=list)
    workspace_file_read_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    workspace_file_writes: list[dict[str, Any]] = field(default_factory=list)
    workspace_mutation_records: list[dict[str, Any]] = field(default_factory=list)
    workspace_mutation_receipts: list[dict[str, Any]] = field(default_factory=list)
    workspace_epoch: int = 0
    scratch_file_writes: list[dict[str, Any]] = field(default_factory=list)
    allowed_tools: set[str] | None = None
    denied_tools: set[str] = field(default_factory=set)
    coding_mode: bool = False  # operator coding-mode toggle (affects tool defaults)
    on_memory_source_write: Callable[[str, str], None] | None = None
    on_bootstrap_source_write: Callable[[str, str], None] | None = None
    on_runtime_event: Callable[[dict[str, Any]], None] | None = None
    # Legacy elevated mode compatibility. New code should treat only "full" as
    # host execution; Safe mode stays sandboxed.
    elevated: str | None = None
    # Additive per-call tool surface overrides (surfaced tools are made visible even
    # when exposed_by_default=False). Does NOT relax allowed_tools strict denylist.
    surfaced_tools: set[str] | None = None
    tool_policy: dict[str, Any] | None = None
    tool_result_budget_policy: Any | None = None
    tool_result_budget_tracker_factory: Callable[[], Any] | None = None
    tool_run_budget_policy: Any | None = None
    tool_run_budget_tracker_factory: Callable[[], Any] | None = None
    tool_run_budget_key: str | None = None
    router_control_config: Any | None = None
    router_control_hold_store: Any | None = None
    router_control_replay_depth: int = 0
    router_control_turn_hold_applied: bool = False
    # Read-only SkillCatalogSnapshot pinned at the start of this turn. Skill
    # tools consult it so a concurrent catalog publish cannot change the
    # definitions visible halfway through a tool loop.
    skill_catalog: Any | None = None
    # Armed by the engine (mutated in place, same pattern as
    # router_control_turn_hold_applied) once the endgame git freeze margin is
    # reached; shell tools then block workspace-reverting git commands.
    endgame_git_freeze_active: bool = False
    # New runtime-only fields stay at the end to preserve the public dataclass's
    # historical positional constructor contract for embedded callers.
    sandbox_file_system_profile: Any | None = None
    on_sandbox_auto_review: Callable[[dict[str, object]], Awaitable[Any]] | None = None
    session_epoch: int | None = None
    workspace_id: str | None = None
    execution_id: str | None = None
    sandbox_session_manager: Any | None = None
    sandbox_gateway_config: Any | None = None
    # Resolved per turn by the engine (see tools.description_overrides).
    # Keys name a tool or a "tool.param" parameter; values replace the
    # matching model-facing description verbatim. None = mechanism off.
    tool_description_overrides: dict[str, str] | None = None
    tool_description_overrides_source: str | None = None  # "config" | "env_file"
    # Set by the engine alongside the freeze margin reset: when True, a frozen
    # git revert whose targeted diff is instrumentation-only (added print/log
    # lines, nothing removed) is allowed through — cleaning up diagnostic
    # output is exactly what the wrap-up window is for.
    endgame_git_freeze_instrumentation_exempt: bool = False
    # Armed by the engine (mutated in place, pattern above) when the scratch
    # verify-mirror lever is on: workspace write-deny messages then append
    # guidance pointing at <scratch_dir>/verify-mirror/<workspace-relative-path>.
    scratch_verify_mirror_active: bool = False

    # Immutable Safe policy snapshot pinned at the start of this turn. New
    # runtime fields stay after the legacy positional tail so embedded callers
    # that still construct ToolContext positionally keep their field mapping.
    sandbox_policy: Any | None = field(default=None, repr=False)
    # Set only by the authenticated channel ingress boundary. Keeping this
    # separate from ``is_owner`` prevents a generic owner-context leak from
    # promoting a channel caller through the admin-only tool matrix.
    channel_admin_verified: bool = False
    # Collaboration mode frozen for this turn. Kept separate from run_mode:
    # run_mode controls sandbox strength, while collaboration_mode constrains
    # the agent's allowed intent. Unknown modes retain legacy/default behavior
    # until the collaboration subsystem validates them at its boundary.
    collaboration_mode: str = "default"
    collaboration_revision: int = 0
    active_plan_revision_id: str | None = None
    # Present only for turns created by ``plans.implement`` (and, later, a
    # Goal driver). Ordinary Default turns deliberately have no PlanRun.
    plan_run_id: str | None = None
    # Runtime-only services are injected after durable turn acceptance. They
    # must never be serialized into task details or route metadata.
    plan_storage: Any | None = field(default=None, repr=False)
    plan_event_emitter: (
        Callable[[str, str, dict[str, Any]], Awaitable[None]] | None
    ) = field(default=None, repr=False)
    # Runtime-owned deferred interaction service. It returns structured answers
    # to the exact tool call instead of injecting a new user turn.
    user_input_provider: Any | None = field(default=None, repr=False)
    # Immutable, validated PlanRevision selected for this turn. This is a
    # process-local prompt input and is never serialized into task metadata.
    plan_revision: Any | None = field(default=None, repr=False)
    # Authoritative mutable PlanRun snapshot captured after this task claims the
    # run. Runtime-only prompt input; checkpoint tools continue to read live
    # storage for compare-and-set transitions.
    plan_run: Any | None = field(default=None, repr=False)
    # Dormant compatibility surface for callers built against the earlier
    # Goal-aware PlanRun runtime. The session Goal path no longer populates it,
    # but retaining its positional slot avoids shifting embedded callers.
    goal_run: Any | None = field(default=None, repr=False)
    # Immutable generation-fenced Goal context captured for this exact task.
    # It is runtime-only authority and never reconstructed from a current row.
    goal_context: Any | None = field(default=None, repr=False)
    # Process-local Goal coordinator used only by Goal-owned main-agent turns.
    # The service is never serialized into task details or route metadata.
    goal_service: Any | None = field(default=None, repr=False)
    # Validated editor state injected only by the Web/Desktop ingress after
    # durable turn acceptance.  These handles are process-local authority and
    # must never be copied into route metadata, transcripts, or decision logs.
    artifact_context: Any | None = field(default=None, repr=False)
    artifact_session: Any | None = field(default=None, repr=False)
    desktop_artifact_bridge: Any | None = field(default=None, repr=False)
    artifact_event_emitter: (
        Callable[[dict[str, Any]], Awaitable[None]] | None
    ) = field(default=None, repr=False)
    # Narrow, runtime-only hook that turns a freshly published editable
    # deliverable into the session's canonical Document before the artifact
    # event crosses the public stream boundary. The engine never receives the
    # underlying persistence service and adoption failures remain recoverable
    # through the Workbench open path.
    generated_artifact_adopter: (
        Callable[[Any], Awaitable[None]] | None
    ) = field(default=None, repr=False)
    # Hard upper bound on the tools that may be exposed or dispatched during
    # this turn. Unlike ``allowed_tools``, declarative policy layers may never
    # widen this set. It is used only for narrowly scoped runtime authorities
    # such as a PromptAnnotation turn; ordinary contexts leave it unset.
    #
    # Runtime-only fields must remain appended here to preserve the historical
    # positional constructor contract for embedded callers.
    exclusive_tools: frozenset[str] | None = field(default=None, repr=False)
    # Durable single-writer receipt controller for a PromptAnnotation turn.
    # The Gateway constructs this only after TaskRuntime has attached the
    # accepted task id. Dispatch consumes it before validating the first
    # writer call; it must never be serialized or copied to another turn.
    artifact_mutation_attempt_controller: Any | None = field(
        default=None,
        repr=False,
    )
    # Process-local authority cleanup registered by ingress/runtime adapters.
    # The shared Agent turn boundary invokes these callbacks on every terminal
    # path without importing feature-specific tool implementations.
    turn_cleanup_callbacks: list[Callable[[], None]] = field(
        default_factory=list,
        repr=False,
    )
    # Frozen after the final provider-visible tool schema is built. Dispatch
    # and projection code use this bit to avoid replacing raw tool output with
    # a handle that the current model is not authorized to retrieve.
    tool_result_retrieval_available: bool = False
    # Process-local task ancestry used only to persist exact subprocess
    # ownership. Raw values are never written to the owner registry.
    parent_session_key: str | None = field(default=None, repr=False)
    parent_task_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # A restricted turn's ceiling is an authority boundary, not a policy
        # preference.  Normalize every caller (including embedded callers
        # that still pass a mutable set) so no later hook can widen the live
        # schema/dispatch ceiling in place.
        if self.exclusive_tools is not None:
            self.exclusive_tools = frozenset(self.exclusive_tools)
        self.validate_path_roots()

    def validate_path_roots(self) -> None:
        """Reject scratch roots that equal or contain the active workspace."""

        if not self.workspace_dir or not self.scratch_dir:
            return
        try:
            workspace = Path(self.workspace_dir).expanduser().resolve(strict=False)
            scratch = Path(self.scratch_dir).expanduser().resolve(strict=False)
            workspace.relative_to(scratch)
        except ValueError:
            return
        except (OSError, RuntimeError) as exc:
            raise ValueError("workspace_dir and scratch_dir must resolve safely") from exc
        raise ValueError(
            "scratch_dir must not equal or contain workspace_dir; use a disjoint "
            "scratch root or a dedicated scratch subdirectory inside the workspace"
        )


def is_goal_owned_main_default_turn(ctx: ToolContext | None) -> bool:
    """Return whether ``ctx`` carries authority for a top-level Goal turn.

    ``main`` describes the execution role here, not the configured agent ID.
    Named top-level agents own ordinary sessions too; caller provenance and
    subagent depth are the authority boundary.
    """

    return bool(
        ctx is not None
        and isinstance(getattr(ctx, "goal_context", None), Mapping)
        and str(getattr(ctx, "collaboration_mode", "default")) == "default"
        and ctx.caller_kind
        in {
            CallerKind.AGENT,
            CallerKind.CHANNEL,
            CallerKind.CLI,
            CallerKind.WEB,
        }
        and int(getattr(ctx, "subagent_depth", 0) or 0) == 0
    )


def surface_capabilities_for_tool_context(
    tool_context: ToolContext | None,
) -> SurfaceCapabilities:
    """Resolve publication capabilities from the authenticated entry surface.

    ``caller_kind`` is the canonical route classification.  In particular,
    ``channel_id`` is deliberately not used as a proxy: WebUI routes also have
    a channel id, but WebUI can replace a generation in place.  External
    channel delivery cannot retract speculative text, so it receives a fully
    buffered surface contract.
    """

    if tool_context is None:
        return SurfaceCapabilities()

    raw_caller_kind = getattr(tool_context, "caller_kind", CallerKind.AGENT)
    try:
        caller_kind = CallerKind(raw_caller_kind)
    except (TypeError, ValueError):
        caller_kind = CallerKind.AGENT

    if caller_kind is CallerKind.CHANNEL:
        return SurfaceCapabilities(
            supports_streaming=False,
            supports_edit=False,
            supports_generation_reset=False,
        )

    return SurfaceCapabilities()


# Request-scoped context — set by build_tool_handler before each dispatch.
current_tool_context: contextvars.ContextVar[ToolContext | None] = contextvars.ContextVar(
    "current_tool_context", default=None
)


# Tool deny-list constants — exact registered tool names

SUBAGENT_TOOL_DENY: frozenset[str] = frozenset(
    {
        "cron",
        "gateway",
        "agents_list",
        "subagents",
        "memory_get",
        "memory_search",
        "session_search",
        "message",
        "publish_artifact",
        "document_apply",
        "document_patch",
        "document_inspect",
        "document_locate",
        "document_read",
    }
)

CRON_AGENT_ALLOW: frozenset[str] = frozenset(
    {
        "git_diff",
        "git_log",
        "git_status",
        "glob_search",
        "grep_search",
        "list_dir",
        "pdf",
        "read_file",
        "session_status",
        "sessions_history",
        "sessions_list",
        "web_discover",
        "web_fetch",
        "web_search",
    }
)

CRON_AGENT_DENY: frozenset[str] = frozenset(
    {
        "cron",
        "agents_list",
        "subagents",
        "message",
        "exec_command",
        "background_process",
        "write_file",
        "edit_file",
        "apply_patch",
        "execute_code",
        "git_commit",
    }
)


# Internal tool spec
@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema properties dict
    required: list[str] = field(default_factory=list)
    owner_only: bool = False
    exposed_by_default: bool = True
    execution_timeout_seconds: float | None = None
    execution_timeout_argument: str | None = None
    execution_timeout_padding: float = 0.0
    # Internal cancellation semantics. ``None`` lets the registry infer
    # must-settle behavior for filesystem mutation descriptors.
    cancellation_policy: Literal["bounded", "must_settle"] | None = None
    result_budget_class: str | None = None
    sandbox: SandboxToolDescriptor = field(
        default_factory=lambda: SandboxToolDescriptor.custom(kind="")
    )
    # Parameters injected only by the runtime after approval. They remain in
    # the Python handler signature/spec but are omitted from provider schemas.
    runtime_only_arguments: frozenset[str] = field(default_factory=frozenset)
    # Fail closed for newly registered built-ins, plugins, and MCP bridges.
    # Read/control access must be granted explicitly by the tool owner.
    plan_access: PlanAccess = PlanAccess.DENY
    # Control tools may end the current model tool loop after their result is
    # persisted. The dispatcher owns this behavior; handlers must not emulate
    # it with tool-name branches.
    terminates_turn: bool = False
    # Successful terminal tools may opt in to an authoritative completion
    # string carried in one top-level JSON result field. The dispatcher, not
    # the model or Agent, extracts and bounds this text.
    terminal_response_field: str | None = None
    # Trusted declaration that itemless arrays have textual wire semantics.
    # Provider policy still decides whether a request needs the projection.
    allow_string_item_schema_projection: bool = False


# Registered tool implementation: async fn that accepts keyword args and returns str.
# Agent-level tool-call handlers live in opensquilla.tool_boundary.
ToolHandler = Callable[..., Awaitable[str]]


@dataclass
class RegisteredTool:
    spec: ToolSpec
    handler: ToolHandler


class ToolError(Exception):
    """Raised for invalid tool inputs."""


class SafeToolUserMessage:
    """Marker for exceptions with a sanitized, user-actionable message.

    Subclasses may carry raw details in ``args`` for tests or logs, but only
    ``user_message`` is safe to expose to the model/user.
    """

    user_message = "The tool could not complete this action."


class SafeToolError(SafeToolUserMessage, ToolError):
    """ToolError variant that may expose a sanitized user-actionable message."""

    # Set True by policy gates before raising; read by the failure envelope
    # to select the policy-deny user_message cap.
    policy_gate_denial: bool = False

    def __init__(self, user_message: str | None = None, *raw_details: object) -> None:
        super().__init__(*(raw_details or (user_message or self.user_message,)))
        if user_message is not None and user_message.strip():
            self.user_message = user_message


class RetryableToolInputError(SafeToolError):
    """Tool input was invalid but can be corrected and retried by the caller."""


class InvalidToolArgumentsError(RetryableToolInputError):
    """Raised when provider output did not produce executable tool arguments."""

    user_message = (
        "The tool call arguments were not valid JSON and were not executed. "
        "Reissue the same tool call with complete JSON arguments that match "
        "the tool schema. Do not wrap the arguments in _raw, XML tags, or "
        "markdown fences. For large file edits, split the edit into smaller "
        "calls using an editing tool listed in Available Tools."
    )


class ProjectedToolArgumentsError(SafeToolUserMessage, ValueError):
    """Raised when provider-context argument projections reach dispatch."""

    user_message = (
        "The tool call arguments contain provider-compacted placeholder text and "
        "were not executed. That placeholder is not real content; do not copy or "
        "retype it. Re-read the relevant file or re-run the command to obtain the "
        "real content, then reissue the tool call with complete arguments."
    )


class UnsupportedSurfaceError(SafeToolError):
    """Raised when a tool needs an interactive surface that is unavailable."""

    user_message = (
        "This tool requires a live approval surface, but the current run is unattended."
    )


class UnsupportedURLSchemeError(SafeToolUserMessage, ValueError):
    """Raised when a URL tool receives a URL without an HTTP(S) scheme."""

    user_message = "The URL must include http:// or https:// before the hostname."


class SSRFBlockedError(SafeToolUserMessage, ValueError):
    """Raised when URL safety checks block a private/internal destination."""

    user_message = (
        "The URL was blocked by the network safety policy. Use a public HTTP(S) URL "
        "from trusted search results instead."
    )


class WorkspaceAccessError(SafeToolError):
    """Raised when a filesystem operation escapes the active workspace."""

    user_message = (
        "Filesystem operations must stay inside the active workspace. Use a relative "
        "path within the workspace or choose an approved workspace file."
    )
