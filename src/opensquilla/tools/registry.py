"""ToolRegistry + @tool decorator."""

from __future__ import annotations

import copy
import functools
import hashlib
import inspect
import json
import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, cast

import structlog

from opensquilla.provider.types import ToolDefinition, ToolInputSchema
from opensquilla.sandbox.operation_runtime import SandboxToolDescriptor
from opensquilla.tools import visibility as visibility_policy
from opensquilla.tools.policy_runtime import ToolSurfaceCapabilities
from opensquilla.tools.types import (
    CallerKind,
    InteractionMode,
    PlanAccess,
    RegisteredTool,
    ToolContext,
    ToolHandler,
    ToolSpec,
)

log = structlog.get_logger(__name__)

# Once-per-session guard for the tool_description_overrides.applied runtime
# event, keyed by (session_key, source, sha256 of the full key->text table):
# to_tool_definitions runs multiple times per turn (debug logging, gateway
# boot, session flush) and an unguarded emit would spam the event stream,
# while a same-keys-different-wording table must still emit fresh. Keying by
# session keeps attribution per-session in multi-session gateway processes
# instead of suppressing every session after the first. Known skew: the event
# fires at definition-build time, before filter_by_profile, so its tools/
# params lists describe the override application, not the final model-visible
# surface — a profile can still hide an overridden tool afterwards.
_description_override_event_keys: set[tuple[str | None, str, str]] = set()

ToolProfile = visibility_policy.ToolProfile
_CHANNEL_DEFAULT_ALLOW = visibility_policy._CHANNEL_DEFAULT_ALLOW
_CHANNEL_HARD_DENY_NON_OWNER = visibility_policy._CHANNEL_HARD_DENY_NON_OWNER
filter_by_profile = visibility_policy.filter_by_profile
profile_allows_tool = visibility_policy.profile_allows_tool
resolve_profile = visibility_policy.resolve_profile


class ToolRegistry:
    """Central registry for all tools."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.name in self._tools:
            log.warning("registry.tool_overwrite", name=spec.name, source="tools")
        self._tools[spec.name] = RegisteredTool(spec=spec, handler=handler)

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def list_names(self) -> list[str]:
        return list(self._tools.keys())

    def unregister(self, name: str) -> bool:
        """Remove a tool by name. Returns True if it existed."""
        return self._tools.pop(name, None) is not None

    def all_tools(self) -> list[RegisteredTool]:
        return list(self._tools.values())

    def _iter_visible_tools(
        self,
        ctx: ToolContext | None = None,
        *,
        sort: bool = False,
    ) -> list[RegisteredTool]:
        return visibility_policy.visible_registered_tools(self._tools.values(), ctx, sort=sort)

    def _is_visible(self, rt: RegisteredTool, ctx: ToolContext | None = None) -> bool:
        return visibility_policy.is_tool_visible(rt, ctx)

    def _default_context(self) -> ToolContext:
        return visibility_policy.default_tool_context()

    def _context_for_profile(self, profile: str | None) -> ToolContext:
        return visibility_policy.tool_context_for_profile(profile)

    def _effective_context(
        self,
        session_key: str | None = None,
        agent_id: str | None = None,
        caller_kind: CallerKind | str | None = None,
        interaction_mode: InteractionMode | str | None = None,
        tool_surface_capabilities: ToolSurfaceCapabilities | None = None,
        is_owner: bool = True,
    ) -> ToolContext:
        return visibility_policy.effective_tool_context(
            session_key=session_key,
            agent_id=agent_id,
            caller_kind=caller_kind,
            interaction_mode=interaction_mode,
            tool_surface_capabilities=tool_surface_capabilities,
            is_owner=is_owner,
        )

    @staticmethod
    def _schema_for(rt: RegisteredTool) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                name: value
                for name, value in rt.spec.parameters.items()
                if name not in rt.spec.runtime_only_arguments
            },
            "required": ToolRegistry._required_for(rt),
        }

    @staticmethod
    def _required_for(rt: RegisteredTool) -> list[str]:
        return [
            name
            for name in rt.spec.required
            if name not in rt.spec.runtime_only_arguments
        ]

    def _parameters_for(self, rt: RegisteredTool, ctx: ToolContext) -> dict[str, Any]:
        raw_parameters = rt.spec.parameters
        if (
            raw_parameters.get("type") == "object"
            and isinstance(raw_parameters.get("properties"), Mapping)
        ):
            raw_parameters = raw_parameters["properties"]
        parameters = copy.deepcopy(raw_parameters)
        for name in rt.spec.runtime_only_arguments:
            parameters.pop(name, None)
        overrides = getattr(ctx, "tool_description_overrides", None)
        if overrides:
            # Dotted keys ("tool.param") replace that parameter's description
            # verbatim; whole-description keys are handled in _description_for.
            # Exact tool-name match wins over the dotted split: plugin tool
            # names may themselves be dotted, and a whole-description key for
            # registered tool "a.b" must not also rewrite parameter "b" of
            # tool "a" (same precedence as the event accounting).
            for key, text in overrides.items():
                if key in self._tools:
                    continue
                tool_name, sep, param = key.partition(".")
                if (
                    sep
                    and tool_name == rt.spec.name
                    and isinstance(parameters.get(param), dict)
                ):
                    parameters[param] = {**parameters[param], "description": text}
        if rt.spec.name != "router_control":
            return parameters
        router_cfg = getattr(ctx, "router_control_config", None)
        if router_cfg is None:
            return parameters
        try:
            from opensquilla.router_control import build_router_control_targets

            target_ids = [
                target.target_id
                for target in build_router_control_targets(router_cfg)
                if target.target_type == "tier"
            ]
        except Exception:  # noqa: BLE001 - schema enrichment must not hide the tool
            return parameters
        if target_ids and "target_id" in parameters:
            parameters["target_id"]["enum"] = target_ids
        return parameters

    @staticmethod
    def _tool_visible(
        visible_tool_names: frozenset[str] | set[str] | None,
        name: str,
    ) -> bool:
        return visible_tool_names is None or name in visible_tool_names

    @staticmethod
    def _normalize_description(description: str) -> str:
        return " ".join(description.split())

    @classmethod
    def _description_for(
        cls,
        rt: RegisteredTool,
        ctx: ToolContext,
        visible_tool_names: frozenset[str] | set[str] | None = None,
    ) -> str:
        description = rt.spec.description
        rewritten = False
        if rt.spec.name == "read_file" and not cls._tool_visible(
            visible_tool_names,
            "read_spreadsheet",
        ):
            original = description
            description = description.replace(
                "For CSV/TSV/Excel workbook data, use read_spreadsheet.",
                "",
            )
            rewritten = rewritten or description != original
        if rt.spec.name == "write_file" and not cls._tool_visible(
            visible_tool_names,
            "apply_patch",
        ):
            if cls._tool_visible(visible_tool_names, "edit_file"):
                replacement = (
                    "read_file without offset or limit, then prefer edit_file for "
                    "exact replacements."
                )
            else:
                replacement = (
                    "read_file without offset or limit, then only rewrite the full "
                    "file when the complete replacement content is intended."
                )
            original = description
            description = description.replace(
                "read_file without offset or limit, then prefer edit_file for exact "
                "replacements or apply_patch for multi-line hunks.",
                replacement,
            )
            rewritten = rewritten or description != original
        if rt.spec.name == "edit_file" and not cls._tool_visible(
            visible_tool_names,
            "apply_patch",
        ):
            original = description
            description = description.replace(
                "For large or line-oriented changes, prefer apply_patch with a small hunk.",
                (
                    "For large or line-oriented changes, split the change into "
                    "smaller exact replacements with complete old_text/new_text "
                    "arguments."
                ),
            )
            rewritten = rewritten or description != original
        if rt.spec.name == "exec_command" and not (
            cls._tool_visible(visible_tool_names, "read_source")
            and cls._tool_visible(visible_tool_names, "edit_source")
        ):
            if cls._tool_visible(visible_tool_names, "edit_file") and cls._tool_visible(
                visible_tool_names,
                "apply_patch",
            ):
                source_change_guidance = (
                    "For workspace source changes, prefer read_file followed by "
                    "edit_file or apply_patch; do not use shell redirection or "
                    "ad hoc scripts as the primary source-edit path."
                )
            elif cls._tool_visible(visible_tool_names, "edit_file"):
                source_change_guidance = (
                    "For workspace source changes, prefer read_file followed by "
                    "edit_file; do not use shell redirection or ad hoc scripts as "
                    "the primary source-edit path."
                )
            else:
                source_change_guidance = (
                    "For workspace source changes, use the visible file-editing "
                    "tools when available; do not use shell redirection or ad hoc "
                    "scripts as the primary source-edit path."
                )
            original = description
            description = description.replace(
                "For workspace source changes, prefer read_source followed by "
                "edit_source so edits stay revision-gated, structured, and reviewable.",
                source_change_guidance,
            )
            rewritten = rewritten or description != original
        if rewritten:
            description = cls._normalize_description(description)
        overrides = getattr(ctx, "tool_description_overrides", None)
        if overrides:
            # Override text is verbatim-final and supersedes the conditional
            # rewrites above; the functional scratch-dir suffix below is still
            # appended so scratch routing keeps working.
            override = overrides.get(rt.spec.name)
            if isinstance(override, str) and override.strip():
                description = override
        scratch_dir = getattr(ctx, "scratch_dir", None)
        if scratch_dir and rt.spec.name in {
            "exec_command",
            "write_file",
            "edit_file",
            "apply_patch",
            "execute_code",
        }:
            description = (
                f"{description} For temporary scripts, logs, debug output, and "
                f"candidate patches, use the configured scratch directory: {scratch_dir}."
            )
        return description

    @staticmethod
    def _record_description_override_event(
        ctx: ToolContext,
        visible_tools: list[RegisteredTool],
    ) -> None:
        overrides = getattr(ctx, "tool_description_overrides", None)
        if not overrides:
            return
        source = getattr(ctx, "tool_description_overrides_source", None) or "config"
        # Fingerprint keys AND values: repointing the source at a same-keyed
        # table with different wording must produce a fresh event, and the
        # fingerprint in the payload lets attribution tie a turn to the exact
        # wording that was live.
        overrides_sha256 = hashlib.sha256(
            json.dumps(dict(sorted(overrides.items())), ensure_ascii=False).encode(
                "utf-8"
            )
        ).hexdigest()
        guard_key = (getattr(ctx, "session_key", None), source, overrides_sha256)
        if guard_key in _description_override_event_keys:
            return
        parameters_by_tool: dict[str, Mapping[str, Any]] = {}
        for rt in visible_tools:
            raw = rt.spec.parameters
            if raw.get("type") == "object" and isinstance(raw.get("properties"), Mapping):
                raw = raw["properties"]
            parameters_by_tool[rt.spec.name] = raw
        applied_tools: list[str] = []
        applied_params: list[str] = []
        for key in overrides:
            # Exact tool-name match first: plugin tool names may themselves be
            # dotted, and _description_for applies whole-description overrides
            # by exact name lookup.
            if key in parameters_by_tool:
                applied_tools.append(key)
                continue
            tool_name, sep, param = key.partition(".")
            if sep and param in parameters_by_tool.get(tool_name, {}):
                applied_params.append(key)
        if not applied_tools and not applied_params:
            return
        event = {
            "feature": "tool_description_overrides",
            "name": "tool_description_overrides.applied",
            "action": "rewrite_tool_descriptions",
            "reason": "env_gate",
            "source": source,
            "overrides_sha256": overrides_sha256,
            "tools": sorted(applied_tools),
            "params": sorted(applied_params),
            "requested": sorted(overrides),
            "session_key": getattr(ctx, "session_key", None),
            "agent_id": getattr(ctx, "agent_id", None),
        }
        on_runtime_event = getattr(ctx, "on_runtime_event", None)
        if on_runtime_event is not None:
            try:
                on_runtime_event(event)
            except Exception:  # noqa: BLE001 - attribution must not break tool export
                # Guard NOT set: the next definition build retries emission.
                return
            _description_override_event_keys.add(guard_key)
            return
        # Definition builds run before the agent wires ctx.on_runtime_event, so
        # fall back to the env-resolved sink to keep the event recorded.
        from opensquilla.engine.runtime_events import append_runtime_event

        try:
            append_runtime_event(
                os.environ.get("OPENSQUILLA_RUNTIME_EVENTS_PATH") or None,
                event,
            )
        except Exception:  # noqa: BLE001 - attribution must not break tool export
            return
        _description_override_event_keys.add(guard_key)

    def to_tool_definitions(self, ctx: ToolContext | None = None) -> list[ToolDefinition]:
        """Export tools as MCP-compatible ToolDefinition list.

        When *ctx* is provided, tools are filtered based on:
        - ``owner_only``: hidden when ``ctx.is_owner`` is False
        - ``denied_tools``: hidden when the tool name is in ``ctx.denied_tools``

        When *ctx* is None, all tools are returned (backward compat for tests).
        """
        active_ctx = ctx if ctx is not None else self._default_context()
        visible_tools = self._iter_visible_tools(active_ctx, sort=True)
        visible_tool_names = frozenset(rt.spec.name for rt in visible_tools)
        self._record_description_override_event(active_ctx, visible_tools)
        definitions: list[ToolDefinition] = []
        for rt in visible_tools:
            definition = ToolDefinition(
                name=rt.spec.name,
                description=self._description_for(
                    rt,
                    active_ctx,
                    visible_tool_names,
                ),
                input_schema=ToolInputSchema(
                    type="object",
                    properties=self._parameters_for(rt, active_ctx),
                    required=self._required_for(rt),
                ),
                execution_timeout_seconds=rt.spec.execution_timeout_seconds,
                execution_timeout_argument=rt.spec.execution_timeout_argument,
                execution_timeout_padding=rt.spec.execution_timeout_padding,
                cancellation_policy=(
                    rt.spec.cancellation_policy
                    or (
                        "must_settle"
                        if rt.spec.sandbox.kind in {"fs.write", "fs.edit"}
                        else "bounded"
                    )
                ),
            )
            if rt.spec.allow_string_item_schema_projection:
                definition._enable_string_item_schema_projection()
            definitions.append(definition)
        return definitions

    async def list_tools(
        self,
        profile: str | None = None,
        *,
        session_key: str | None = None,
        agent_id: str | None = None,
        caller_kind: CallerKind | str | None = None,
        interaction_mode: InteractionMode | str | None = None,
        tool_surface_capabilities: ToolSurfaceCapabilities | None = None,
        is_owner: bool = True,
    ) -> list[dict[str, Any]]:
        has_runtime_context = any(
            value is not None
            for value in (session_key, agent_id, caller_kind, interaction_mode)
        )
        if has_runtime_context:
            ctx = self._effective_context(
                session_key=session_key,
                agent_id=agent_id,
                caller_kind=caller_kind,
                interaction_mode=interaction_mode,
                tool_surface_capabilities=tool_surface_capabilities,
                is_owner=is_owner,
            )
        else:
            ctx = self._context_for_profile(profile)
            if not is_owner:
                ctx = replace(ctx, is_owner=False)
        visible_tools = self._iter_visible_tools(ctx, sort=True)
        visible_tool_names = frozenset(rt.spec.name for rt in visible_tools)
        return [
            {
                "name": rt.spec.name,
                "description": self._description_for(rt, ctx, visible_tool_names),
                "schema": {
                    "type": "object",
                    "properties": self._parameters_for(rt, ctx),
                    "required": self._required_for(rt),
                },
                "source": "plugin" if "." in rt.spec.name else "builtin",
                "enabled": True,
            }
            for rt in visible_tools
        ]

    async def effective_tools(
        self,
        session_key: str | None = None,
        agent_id: str | None = None,
        caller_kind: CallerKind | str | None = None,
        interaction_mode: InteractionMode | str | None = None,
        tool_surface_capabilities: ToolSurfaceCapabilities | None = None,
        is_owner: bool = True,
    ) -> list[dict[str, Any]]:
        ctx = self._effective_context(
            session_key=session_key,
            agent_id=agent_id,
            caller_kind=caller_kind,
            interaction_mode=interaction_mode,
            tool_surface_capabilities=tool_surface_capabilities,
            is_owner=is_owner,
        )
        return [
            {
                "name": rt.spec.name,
                "description": self._description_for(rt, ctx),
                "schema": {
                    "type": "object",
                    "properties": self._parameters_for(rt, ctx),
                    "required": self._required_for(rt),
                },
            }
            for rt in self._iter_visible_tools(ctx, sort=True)
        ]


# Global default registry
_default_registry = ToolRegistry()


def get_default_registry() -> ToolRegistry:
    return _default_registry


def _tool_rpc_params(params: Mapping[str, Any] | None) -> Mapping[str, Any]:
    from opensquilla.tools.rpc_payload import tool_rpc_params

    return tool_rpc_params(params)


def _tool_surface_capabilities_for_runtime(
    *,
    tool_surface_capabilities: ToolSurfaceCapabilities | None = None,
    session_manager: object | None = None,
    task_runtime: object | None = None,
    scheduler: object | None = None,
    gateway_config: object | None = None,
    channel_manager: object | None = None,
    originating_envelope: object | None = None,
) -> ToolSurfaceCapabilities:
    from opensquilla.tools.rpc_payload import tool_surface_capabilities_for_runtime

    return tool_surface_capabilities_for_runtime(
        tool_surface_capabilities=tool_surface_capabilities,
        session_manager=session_manager,
        task_runtime=task_runtime,
        scheduler=scheduler,
        gateway_config=gateway_config,
        channel_manager=channel_manager,
        originating_envelope=originating_envelope,
    )


async def tools_catalog_payload(
    params: Mapping[str, Any] | None,
    *,
    tool_registry: ToolRegistry | None = None,
    is_owner: bool = True,
    tool_surface_capabilities: ToolSurfaceCapabilities | None = None,
    session_manager: object | None = None,
    task_runtime: object | None = None,
    scheduler: object | None = None,
    gateway_config: object | None = None,
    channel_manager: object | None = None,
    originating_envelope: object | None = None,
) -> dict[str, list[dict[str, Any]]]:
    from opensquilla.tools.rpc_payload import tools_catalog_payload as build_payload

    return await build_payload(
        params,
        tool_registry=tool_registry,
        is_owner=is_owner,
        tool_surface_capabilities=tool_surface_capabilities,
        session_manager=session_manager,
        task_runtime=task_runtime,
        scheduler=scheduler,
        gateway_config=gateway_config,
        channel_manager=channel_manager,
        originating_envelope=originating_envelope,
    )


async def tools_effective_payload(
    params: Mapping[str, Any] | None,
    *,
    tool_registry: ToolRegistry | None = None,
    is_owner: bool = True,
    tool_surface_capabilities: ToolSurfaceCapabilities | None = None,
    session_manager: object | None = None,
    task_runtime: object | None = None,
    scheduler: object | None = None,
    gateway_config: object | None = None,
    channel_manager: object | None = None,
    originating_envelope: object | None = None,
) -> dict[str, list[dict[str, Any]]]:
    from opensquilla.tools.rpc_payload import tools_effective_payload as build_payload

    return await build_payload(
        params,
        tool_registry=tool_registry,
        is_owner=is_owner,
        tool_surface_capabilities=tool_surface_capabilities,
        session_manager=session_manager,
        task_runtime=task_runtime,
        scheduler=scheduler,
        gateway_config=gateway_config,
        channel_manager=channel_manager,
        originating_envelope=originating_envelope,
    )


def tool(
    name: str,
    description: str,
    params: dict[str, Any] | None = None,
    required: list[str] | None = None,
    owner_only: bool = False,
    exposed_by_default: bool = True,
    execution_timeout_seconds: float | None = None,
    execution_timeout_argument: str | None = None,
    execution_timeout_padding: float = 0.0,
    cancellation_policy: Literal["bounded", "must_settle"] | None = None,
    result_budget_class: str | None = None,
    sandbox: SandboxToolDescriptor | None = None,
    registry: ToolRegistry | None = None,
    *,
    plan_access: PlanAccess = PlanAccess.DENY,
    terminates_turn: bool = False,
    terminal_response_field: str | None = None,
    runtime_only_arguments: frozenset[str] | set[str] | tuple[str, ...] = (),
    allow_string_item_schema_projection: bool = False,
) -> Any:
    """Decorator to register an async function as a tool.

    Usage::

        @tool(name="read_file", description="Read a file", params={...}, required=["path"])
        async def read_file(path: str) -> str: ...
    """

    def decorator(fn: ToolHandler) -> ToolHandler:
        spec = ToolSpec(
            name=name,
            description=description,
            parameters=params or {},
            required=required or [],
            runtime_only_arguments=frozenset(runtime_only_arguments),
            owner_only=owner_only,
            exposed_by_default=exposed_by_default,
            execution_timeout_seconds=execution_timeout_seconds,
            execution_timeout_argument=execution_timeout_argument,
            execution_timeout_padding=execution_timeout_padding,
            cancellation_policy=cancellation_policy,
            result_budget_class=result_budget_class,
            sandbox=sandbox or SandboxToolDescriptor.custom(kind=name),
            plan_access=plan_access,
            terminates_turn=terminates_turn,
            terminal_response_field=terminal_response_field,
            allow_string_item_schema_projection=allow_string_item_schema_projection,
        )
        target = registry if registry is not None else _default_registry
        target.register(spec, fn)
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> str:
            if not spec.sandbox.enforce:
                return await fn(*args, **kwargs)

            from opensquilla.sandbox.operation_runtime import (
                prepare_tool_operation_guard,
                record_tool_operation_success,
                run_tool_handler_with_operation_guard,
            )
            from opensquilla.tools.types import current_tool_context

            try:
                bound = sig.bind_partial(*args, **kwargs)
                bound.apply_defaults()
                arguments = dict(bound.arguments)
            except TypeError:
                arguments = dict(kwargs)
            ctx = current_tool_context.get()
            workspace = (
                Path(ctx.workspace_dir)
                if ctx is not None and ctx.workspace_dir
                else None
            )
            run_mode = getattr(ctx, "run_mode", None)
            guard = await prepare_tool_operation_guard(
                spec.sandbox,
                tool_name=name,
                arguments=arguments,
                workspace=workspace,
                run_mode=run_mode if isinstance(run_mode, str) else None,
            )
            result = await run_tool_handler_with_operation_guard(fn, arguments, guard)
            if guard.denial_payload is None and guard.record_payload:
                await record_tool_operation_success(guard, result)
            return cast(str, result)

        # Keep the historical two-step ``__wrapped__`` chain even for tools
        # whose descriptor records metadata without enforcing the generic
        # operation guard. A few internal registries deliberately unwrap the
        # public tool wrapper twice to call the raw handler in a controlled
        # ToolContext; changing descriptor enforcement must not break them.
        @functools.wraps(fn)
        async def _descriptor_unwrap_compat(*args: Any, **kwargs: Any) -> str:
            return await fn(*args, **kwargs)

        wrapper.__wrapped__ = _descriptor_unwrap_compat  # type: ignore[attr-defined]

        return wrapper

    return decorator
