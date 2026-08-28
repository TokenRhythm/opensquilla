"""Slash-command catalog RPC.

Exposes :data:`opensquilla.engine.commands.DEFAULT_REGISTRY` to non-Python
surfaces (initially the web frontend) so the slash-menu list comes from
one source rather than being hardcoded per-surface. Read-only.
"""

from __future__ import annotations

import asyncio
from typing import Any

from opensquilla.engine.commands import DEFAULT_REGISTRY, CommandDef, Surface, parse_surface
from opensquilla.gateway.rpc import RpcContext, get_dispatcher

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


async def _meta_skill_argument_choices(ctx: RpcContext) -> list[dict[str, Any]]:
    """Live meta-skill names as ``/meta`` argument candidates (value + description).

    Mirrors the ``meta.list`` filter: invokable ``kind="meta"`` skills only, and
    empty when the subsystem is disabled. Sorted for a stable menu.
    """
    from opensquilla.skills.meta.enabled import is_meta_skill_enabled
    from opensquilla.skills.meta.readiness import (
        assess_meta_skill_readiness,
        meta_readiness_context,
    )

    loader = getattr(ctx, "skill_loader", None)
    if loader is None or not is_meta_skill_enabled(getattr(ctx, "config", None)):
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
                ctx=meta_readiness_context(config=getattr(ctx, "config", None)),
                verify_capabilities=False,
                config=getattr(ctx, "config", None),
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


@_d.method("commands.list_for_surface", scope="operator.read")
async def _handle_commands_list_for_surface(
    params: dict | None, ctx: RpcContext
) -> dict[str, Any]:
    raw = (params or {}).get("surface", "web")
    if not isinstance(raw, str):
        raise ValueError("params.surface must be a string")
    try:
        surface = parse_surface(raw)
    except ValueError as exc:
        valid = ", ".join(sorted({s.value for s in Surface}))
        raise ValueError(f"unknown surface {raw!r}; valid: {valid}") from exc
    commands = [_serialize(cmd, surface) for cmd in DEFAULT_REGISTRY.for_surface(surface)]
    # Populate /meta's argument candidates from the live meta-skills so the
    # slash menu can offer them as Tab-completable choices (SPA + TUI).
    meta_choices = await _meta_skill_argument_choices(ctx)
    if meta_choices:
        for entry in commands:
            if entry.get("name") == "/meta":
                entry["argument_choices"] = meta_choices
                break
    return {"surface": surface.value, "commands": commands}
