"""Build the known idle-session consumer envelope without starting a turn."""

from __future__ import annotations

from dataclasses import fields, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal

from opensquilla.engine.agent import Agent
from opensquilla.engine.collaboration_prompt import with_collaboration_instructions
from opensquilla.engine.types import AgentConfig
from opensquilla.provider.model_catalog import shared_catalog
from opensquilla.provider.protocol import provider_metadata
from opensquilla.tools.types import ToolContext

if TYPE_CHECKING:
    from opensquilla.engine.runtime import TurnRunner


def _fresh_tool_context(
    session: Any,
    workspace_dir: str | None,
    caller_tool_context: ToolContext | None,
) -> ToolContext:
    if caller_tool_context is None:
        return ToolContext(
            session_key=str(session.session_key),
            agent_id=str(getattr(session, "agent_id", "main") or "main"),
            workspace_dir=workspace_dir,
            collaboration_mode=str(getattr(session, "collaboration_mode", "default")),
            collaboration_revision=int(getattr(session, "collaboration_revision", 0)),
            active_plan_revision_id=getattr(session, "active_plan_revision_id", None),
        )
    # _build_tools mutates its context while applying policy/disclosure. Copy
    # mutable containers, retaining opaque runtime service identities without
    # attempting to deepcopy their locks, connections or callbacks.
    copied: dict[str, Any] = {
        item.name: value.copy()
        for item in fields(caller_tool_context)
        if isinstance(value := getattr(caller_tool_context, item.name), (dict, list, set))
    }
    return replace(caller_tool_context, **copied)


def prepare_manual_compaction_envelope(
    runner: TurnRunner,
    session: Any,
    *,
    provider: Any,
    context_window_tokens: int,
    max_output_tokens: int,
    context_window_known: bool,
    provider_request_max_chars: int,
    workspace_dir: str | None,
    caller_tool_context: ToolContext | None = None,
) -> Agent:
    """Prepare projection only; never route, invoke a tool, or send an LLM request.

    An idle session has no active user/media or future runtime capabilities.
    Include its known prompt and policy-filtered tools, without inventing owner
    authorization. The caller reserves unknown next-request content separately;
    the next real turn must still admit its complete request. A caller with an
    authoritative ToolContext can provide it without having it mutated here.
    """
    ctx = _fresh_tool_context(session, workspace_dir, caller_tool_context)
    tool_defs, _unused_handler = runner._build_tools(ctx=ctx)
    prompt = runner._assemble_prompt(
        ctx.agent_id,
        tool_defs,
        session_key=ctx.session_key,
        extra_context=runner._extra_context_for_tool_context(ctx),
        workspace_dir=workspace_dir,
    )
    prompt = with_collaboration_instructions(prompt, ctx)
    config = runner._turn_config()
    raw_cache_mode = getattr(getattr(config, "prompt_cache", None), "effective_mode", "off")
    cache_mode: Literal["off", "auto", "on"] = (
        "on" if raw_cache_mode == "on" else "auto" if raw_cache_mode == "auto" else "off"
    )
    turn = SimpleNamespace(
        system_prompt=prompt,
        metadata={"cache_enabled": cache_mode != "off"},
    )
    system_prompt, cache_breakpoints, request_context = runner._resolve_prompt_config(turn)
    identity = provider_metadata(provider)
    catalog = runner._model_catalog or shared_catalog()
    capabilities = catalog.get_capabilities(
        identity.model,
        provider_name=identity.provider_id or identity.provider_name,
        base_url=identity.base_url,
    )
    compaction_config = getattr(config, "compaction", None)
    token_config = getattr(config, "agent_token_saving", None)
    llm_config = getattr(config, "llm", None)
    agent_config = AgentConfig(
        system_prompt=system_prompt,
        request_context_prompt=request_context,
        cache_breakpoints=cache_breakpoints,
        cache_mode=cache_mode,
        model_id=identity.model,
        provider_id=identity.provider_id or identity.provider_name,
        model_capabilities=capabilities,
        workspace_dir=workspace_dir,
        max_tokens=max_output_tokens,
        context_window_tokens=context_window_tokens,
        context_window_known=context_window_known,
        provider_request_proof_max_chars=provider_request_max_chars,
        provider_request_proof_max_chars_explicit=False,
        thinking=runner._resolve_turn_thinking(turn),
        temperature=getattr(llm_config, "temperature", None),
        top_p=getattr(llm_config, "top_p", None),
        materialize_historical_attachments=bool(workspace_dir),
        compaction_profile=getattr(compaction_config, "compaction_profile", "conversation"),
        compaction_protected_recent_messages=getattr(
            compaction_config, "protected_recent_messages", 0,
        ),
        compaction_total_timeout_seconds=getattr(
            compaction_config, "total_timeout_seconds", 120.0,
        ),
        compaction_heartbeat_interval_seconds=getattr(
            compaction_config, "heartbeat_interval_seconds", 15.0,
        ),
        tool_result_projection_max_inline_chars=getattr(
            token_config, "tool_result_projection_max_inline_chars", 60_000,
        ),
    )
    # No dispatch handler, ToolContext, hooks, session writer, memory warmer or
    # usage sink is bound to this projection-only Agent.
    return Agent(
        provider=provider,
        config=agent_config,
        tool_definitions=tool_defs,
        session_key=ctx.session_key,
    )
