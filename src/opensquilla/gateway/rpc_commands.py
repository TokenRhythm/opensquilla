"""Slash-command catalog RPC.

Exposes :data:`opensquilla.engine.commands.DEFAULT_REGISTRY` to non-Python
surfaces (initially the web frontend) so the slash-menu list comes from
one source rather than being hardcoded per-surface. Read-only.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from opensquilla.application.conversation_ancillary import (
    CommandCatalogPort,
    CommandCatalogQuery,
    CommandCatalogResult,
)
from opensquilla.engine.commands import DEFAULT_REGISTRY, CommandDef, Surface, parse_surface
from opensquilla.gateway.adapters.conversation_ancillary import (
    GatewayConversationAncillaryAdapter,
)
from opensquilla.gateway.adapters.conversation_ancillary_contract import (
    register_conversation_ancillary_contract,
)
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher

_d = get_dispatcher()


def _serialize(cmd: CommandDef, surface: Surface) -> dict[str, Any]:
    """Project a CommandDef into a JSON-safe dict.

    ``rpc_params`` is intentionally omitted — it has no JSON representation
    and is only meaningful inside in-process executors.
    """
    execution = cmd.execution_for(surface)
    if execution is None:
        raise ValueError(f"{cmd.name} is not visible on {surface.value}")
    out: dict[str, Any] = {
        "name": cmd.name,
        "usage": cmd.usage_for(surface),
        "description": cmd.description_for(surface),
        "aliases": list(cmd.aliases),
        "argument_choices": [
            {"value": choice.value, "description": choice.description}
            for choice in cmd.argument_choices_for(surface)
        ],
        "execution": {
            "kind": execution.kind.value,
            "action": execution.action,
        },
    }
    # Scheduling and presentation metadata belongs to the terminal runtime.
    # WebUI and channel clients keep their historic command-list contract;
    # projecting TUI metadata there would mislabel e.g. channel /model as a
    # picker and channel /meta as model-turn input.
    if surface in {Surface.CLI_GATEWAY, Surface.CLI_STANDALONE}:
        out.update(
            category=cmd.category.value,
            busy_policy=cmd.busy_policy.value,
            presentation=cmd.presentation.value,
            order=cmd.order,
            visible_by_default=cmd.visible_by_default,
            deprecated=cmd.deprecated,
        )
    if execution.rpc_method is not None:
        out["execution"]["rpc_method"] = execution.rpc_method
        out["rpc_method"] = execution.rpc_method
    return out


async def _meta_skill_argument_choices(
    skill_loader: Any,
    config: Any,
) -> list[dict[str, Any]]:
    """Live meta-skill names as ``/meta`` argument candidates (value + description).

    Mirrors the ``meta.list`` filter: invokable ``kind="meta"`` skills only, and
    empty when the subsystem is disabled. Sorted for a stable menu.
    """
    from opensquilla.skills.meta.enabled import is_meta_skill_enabled
    from opensquilla.skills.meta.readiness import (
        assess_meta_skill_readiness,
        meta_readiness_context,
    )

    loader = skill_loader
    if loader is None or not is_meta_skill_enabled(config):
        return []
    try:
        refresh = getattr(loader, "refresh_if_changed", None)
        snapshot = getattr(loader, "snapshot", None)
        if callable(refresh) and callable(snapshot):
            await asyncio.to_thread(
                refresh,
                reason="rpc:commands.list_for_surface",
            )
            specs = snapshot().skills
        else:
            specs = await asyncio.to_thread(loader.load_all)
    except Exception:  # noqa: BLE001 — fail-open to an empty candidate list
        return []

    def project_choices() -> list[dict[str, Any]]:
        skill_index = {skill.name: skill for skill in specs}
        choices = []
        for spec in specs:
            if getattr(spec, "kind", "skill") != "meta":
                continue
            if getattr(spec, "disable_model_invocation", False):
                continue
            readiness = assess_meta_skill_readiness(
                spec,
                skill_index=skill_index,
                ctx=meta_readiness_context(config=config),
                verify_capabilities=False,
                config=config,
            )
            choices.append(
                {
                    "value": spec.name,
                    "description": getattr(spec, "description", "") or "",
                    "status": readiness.status,
                    "missing_bins": list(readiness.missing_bins),
                    "missing_env": list(readiness.missing_env),
                    "missing_env_any": [list(group) for group in readiness.missing_env_any],
                    "missing_skills": list(readiness.missing_skills),
                    "missing_capabilities": list(readiness.missing_capabilities),
                }
            )
        choices.sort(key=lambda choice: choice["value"])
        return choices

    # Catalog projection is deliberately passive. Native compiler/encoder smokes
    # are reserved for explicit setup and launch gates.
    return await asyncio.to_thread(project_choices)


async def _command_catalog(
    query: CommandCatalogQuery,
    *,
    skill_loader: Any,
    config: Any,
) -> CommandCatalogResult:
    try:
        surface = parse_surface(query.surface)
    except ValueError as exc:
        valid = ", ".join(sorted({s.value for s in Surface}))
        raise ValueError(f"unknown surface {query.surface!r}; valid: {valid}") from exc
    commands = [_serialize(cmd, surface) for cmd in DEFAULT_REGISTRY.for_surface(surface)]
    # Populate /meta's argument candidates from the live meta-skills so the
    # slash menu can offer them as Tab-completable choices (SPA + TUI).
    meta_choices = await _meta_skill_argument_choices(skill_loader, config)
    if meta_choices:
        for entry in commands:
            if entry.get("name") == "/meta":
                entry["argument_choices"] = meta_choices
                break
    return cast(CommandCatalogResult, {"surface": surface.value, "commands": commands})


class _GatewayCommandCatalogPort(CommandCatalogPort):
    def __init__(self, context: RpcContext) -> None:
        self._skill_loader = getattr(context, "skill_loader", None)
        self._config = getattr(context, "config", None)

    async def list(self, query: CommandCatalogQuery) -> CommandCatalogResult:
        return await _command_catalog(
            query,
            skill_loader=self._skill_loader,
            config=self._config,
        )


async def _handle_commands_list_for_surface_contract(
    params: dict[str, Any] | None, ctx: RpcContext
) -> dict[str, Any]:
    adapter = GatewayConversationAncillaryAdapter(commands=_GatewayCommandCatalogPort(ctx))
    return await adapter.list_commands(params)


_handle_commands_list_for_surface_generated_contract = (
    register_conversation_ancillary_contract(
        _d,
        "commands.list_for_surface",
        _handle_commands_list_for_surface_contract,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )
)
