"""Slash-command catalog RPC.

Exposes :data:`opensquilla.engine.commands.DEFAULT_REGISTRY` to non-Python
surfaces (initially the web frontend) so the slash-menu list comes from
one source rather than being hardcoded per-surface. Read-only.
"""

from __future__ import annotations

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
    # picker.
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
