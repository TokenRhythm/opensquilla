"""Agent command — one-shot agent runner for automation."""

from __future__ import annotations

import asyncio
import copy
import getpass
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from rich.panel import Panel
from rich.text import Text
from typer.models import OptionInfo

from opensquilla.cli.attachments import attachments_from_paths
from opensquilla.cli.ui import console


@dataclass
class AgentRunResult:
    status: str
    agent_id: str
    session_key: str
    text: str
    usage: dict[str, Any]
    errors: list[dict[str, str]]
    workspace: str | None = None
    workspace_strict: bool = False
    workspace_lockdown: bool = False
    workspace_write_deny_globs: list[str] | None = None
    scratch_dir: str | None = None
    thinking: str | None = None
    transcript_path: str | None = None
    usage_path: str | None = None
    artifacts: list[dict[str, Any]] | None = None
    routing: dict[str, Any] | None = None


def _cli_sender_id() -> str:
    raw = os.environ.get("USER")
    if raw and raw.strip():
        return raw.strip()
    try:
        return getpass.getuser() or "cli-user"
    except Exception:
        return "cli-user"


_AGENT_PERMISSION_PROFILES = frozenset({"restricted", "off", "on", "bypass", "full"})


def _resolve_permissions_profile(value: str | None, config: Any | None = None) -> str:
    from opensquilla.permissions import normalize_permission_mode

    env_value = os.environ.get("OPENSQUILLA_AGENT_PERMISSIONS")
    if value is not None:
        raw: Any = value
    elif env_value is not None:
        raw = env_value
    elif config is not None:
        raw = getattr(getattr(config, "permissions", None), "default_mode", "bypass")
    else:
        raw = "bypass"
    mode = normalize_permission_mode(raw)
    profile = "restricted" if mode == "off" else mode
    if profile not in _AGENT_PERMISSION_PROFILES:
        allowed = ", ".join(sorted(_AGENT_PERMISSION_PROFILES))
        raise ValueError(f"permissions must be one of: {allowed}")
    return profile


def _public_artifacts(artifacts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    from opensquilla.artifacts import artifact_payload

    return [artifact_payload(artifact) for artifact in artifacts or []]


def _unwrap_typer_default(value: Any) -> Any:
    if isinstance(value, OptionInfo):
        return value.default
    return value


def _parse_workspace_write_deny_globs(value: Any) -> list[str]:
    if value is None:
        return []
    values = [value] if isinstance(value, str) else list(value)
    patterns: list[str] = []
    for raw in values:
        for part in str(raw).split(","):
            pattern = part.strip()
            if pattern:
                patterns.append(pattern)
    return patterns


def _apply_config_tool_policy_to_context(
    tool_ctx: Any,
    *,
    config: Any,
    tool_registry: Any,
) -> Any:
    list_names = getattr(tool_registry, "list_names", None)
    if not callable(list_names):
        return tool_ctx
    from opensquilla.tools.policy import apply_tool_policy_from_config

    return apply_tool_policy_from_config(
        tool_ctx,
        available_tools=list(list_names()),
        config=config,
    )


async def run_agent_once(
    *,
    message: str,
    agent_id: str = "main",
    session_id: str = "",
    model: str | None = None,
    workspace: str | None = None,
    workspace_strict: bool | None = None,
    thinking: str | None = None,
    timeout: float | None = None,
    max_iterations: int | None = None,
    iteration_timeout: float | None = None,
    tool_timeout: float | None = None,
    request_timeout: float | None = None,
    max_provider_retries: int | None = None,
    length_capped_continuations: int | None = None,
    transcript_path: str | None = None,
    usage_path: str | None = None,
    config: Any | None = None,
    session_db_path: str = ":memory:",
    no_memory_capture: bool = False,
    attachments: list[dict[str, Any]] | None = None,
    attachment_paths: list[str] | tuple[str, ...] | None = None,
    unattended: bool = True,
    stateless: bool = False,
    stateless_keep_project_rules: bool = False,
    scratch_dir: str | None = None,
    workspace_lockdown: bool = False,
    workspace_write_deny_globs: list[str] | tuple[str, ...] | None = None,
    permissions: str | None = None,
) -> AgentRunResult:
    """Run a single agent turn through build_services() and TurnRunner.run()."""
    from opensquilla.agents.scope import resolve_agent_workspace_dir
    from opensquilla.artifacts import artifact_payload
    from opensquilla.engine.types import (
        ArtifactEvent,
        DoneEvent,
        ErrorEvent,
        TextDeltaEvent,
        done_text_snapshot,
    )
    from opensquilla.gateway import attachment_ingest as _attachment_ingest
    from opensquilla.gateway import build_services, build_turn_runner_from_services
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.gateway.routing import build_cli_route_envelope, tool_context_from_envelope
    from opensquilla.paths import media_root_from_config
    from opensquilla.permissions import configured_default_run_mode
    from opensquilla.sandbox.run_mode import normalize_run_mode
    from opensquilla.session.keys import canonicalize_session_key, normalize_agent_id
    from opensquilla.tools.types import InteractionMode

    agent_id = normalize_agent_id(agent_id)
    if max_iterations is not None and max_iterations < 0:
        raise ValueError("max_iterations must be an integer >= 0")
    cfg = config or GatewayConfig.load(os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH"))
    permissions_profile = _resolve_permissions_profile(permissions, cfg)
    elevated = permissions_profile if permissions_profile in {"on", "bypass", "full"} else None
    permissions_override = (
        permissions is not None or os.environ.get("OPENSQUILLA_AGENT_PERMISSIONS") is not None
    )
    sandbox_settings = getattr(cfg, "sandbox", None)
    explicit_run_mode = getattr(sandbox_settings, "run_mode", None)
    run_mode = None
    if not permissions_override:
        if explicit_run_mode:
            run_mode = normalize_run_mode(explicit_run_mode).value
        elif permissions_profile == "restricted":
            run_mode = configured_default_run_mode(cfg).value
    run_attachments: list[dict[str, Any]] = list(attachments or [])
    if attachment_paths:
        run_attachments.extend(attachments_from_paths(tuple(attachment_paths)))
    effective_model = model or _agent_model_from_config(cfg, agent_id)
    active_workspace = workspace or getattr(cfg, "workspace_dir", None)
    service_cfg = _with_agent_workspace_config(cfg, active_workspace) if active_workspace else cfg
    if stateless or stateless_keep_project_rules:
        service_cfg = _with_memory_source_config(service_cfg, "state")
    if effective_model:
        service_cfg = _with_agent_model_config(service_cfg, effective_model)
    if thinking:
        service_cfg = _with_agent_thinking_config(service_cfg, thinking)
    effective_workspace_strict = _resolve_workspace_strict(
        cli_value=workspace_strict,
        config_value=getattr(service_cfg, "workspace_strict", None),
        entrypoint_default=bool(active_workspace),
    )
    # Per-agent workspace isolation: gateway resolves this for channel-driven
    # turns; the CLI ToolContext must do the same so file tools target
    # <root>/agents/<id> for non-main agents instead of stepping on the root
    # workspace. Legacy ``default`` is normalized to ``main`` above.
    tool_workspace_dir: str | None
    if active_workspace and agent_id != "main":
        resolved_path = resolve_agent_workspace_dir(agent_id, service_cfg)
        # Mirror gateway boot.py:594 — pre-create the per-agent dir so
        # shell/cwd-based tools do not hit FileNotFoundError on first use.
        resolved_path.mkdir(parents=True, exist_ok=True)
        tool_workspace_dir = str(resolved_path)
    else:
        tool_workspace_dir = active_workspace
    effective_scratch_dir: str | None = None
    if scratch_dir:
        scratch_path = Path(scratch_dir).expanduser().resolve(strict=False)
        scratch_path.mkdir(parents=True, exist_ok=True)
        effective_scratch_dir = str(scratch_path)
    if workspace_lockdown and not tool_workspace_dir and not effective_scratch_dir:
        raise ValueError(
            "workspace_lockdown requires --workspace, configured workspace_dir, or --scratch-dir"
        )
    effective_workspace_write_deny_globs = _parse_workspace_write_deny_globs(
        workspace_write_deny_globs
    )

    # Hand the runtime agent_id to build_services so its memory store /
    # retriever / sync manager / turn capture are pre-built for that agent.
    # Without this the memory manager only registers ``main`` (channel-derived
    # ids), so non-main CLI invocations would write to the per-agent workspace
    # but the index would never see those writes.
    extra_agents = [agent_id] if agent_id and agent_id != "main" else None
    seed_agent_workspaces = not (stateless or stateless_keep_project_rules)
    svc = await build_services(
        config=service_cfg,
        session_db_path=session_db_path,
        extra_agent_ids=extra_agents,
        seed_agent_workspaces=seed_agent_workspaces,
    )
    assert svc.session_manager is not None
    session_key = canonicalize_session_key(session_id or f"agent:{agent_id}:main")

    text_parts: list[str] = []
    errors: list[dict[str, str]] = []
    artifacts: list[dict[str, Any]] = []
    done: DoneEvent | None = None

    try:
        await svc.session_manager.get_or_create(session_key, agent_id=agent_id)
        attachments_cfg = getattr(service_cfg, "attachments", None)
        opaque_cap = getattr(attachments_cfg, "opaque_max_bytes", None)
        ingested_attachments = await _attachment_ingest.ingest_attachments(
            message,
            run_attachments,
            failure_mode="raise",
            accept_opaque=bool(getattr(attachments_cfg, "accept_opaque", True)),
            opaque_limit_bytes=opaque_cap if isinstance(opaque_cap, int) else None,
        )
        message = ingested_attachments.text
        run_attachments = ingested_attachments.attachments
        if run_attachments:
            from opensquilla.gateway.transcripts import build_transcript_attachment_envelope

            if hasattr(svc.session_manager, "stamp_user_text"):
                _stamped = svc.session_manager.stamp_user_text(message)
                if isinstance(_stamped, str):
                    message = _stamped

            attachments_cfg = getattr(service_cfg, "attachments", None)
            persist_enabled = bool(getattr(attachments_cfg, "persist_transcripts", True))
            media_root = media_root_from_config(service_cfg)
            disk_budget = getattr(attachments_cfg, "transcript_disk_budget_bytes", None)
            persist_content, _writes = build_transcript_attachment_envelope(
                text=message,
                attachments=run_attachments,
                session_id=session_key.split(":")[-1] or session_key,
                media_root=media_root,
                persist_enabled=persist_enabled,
                disk_budget_bytes=disk_budget if isinstance(disk_budget, int) else None,
            )
            await svc.session_manager.append_message(
                session_key, role="user", content=persist_content
            )
        else:
            _persisted = await svc.session_manager.append_message(
                session_key, role="user", content=message
            )
            if _persisted is not None and isinstance(_persisted.content, str):
                message = _persisted.content

        route_envelope = build_cli_route_envelope(
            session_key=session_key,
            agent_id=agent_id,
            channel_id="cli:agent",
            sender_id=_cli_sender_id(),
            source_name="run",
            interaction_mode=(
                InteractionMode.UNATTENDED if unattended else InteractionMode.INTERACTIVE
            ),
            elevated=elevated,
            run_mode=run_mode,
        )
        tool_ctx = tool_context_from_envelope(
            route_envelope,
            is_owner=True,
            workspace_dir=tool_workspace_dir,
            workspace_strict=effective_workspace_strict,
        )
        tool_ctx.scratch_dir = effective_scratch_dir
        tool_ctx.workspace_lockdown = workspace_lockdown
        tool_ctx.workspace_write_deny_globs = list(effective_workspace_write_deny_globs)
        tool_ctx = _apply_config_tool_policy_to_context(
            tool_ctx,
            config=service_cfg,
            tool_registry=getattr(svc, "tool_registry", None),
        )

        runner = build_turn_runner_from_services(svc)
        bootstrap_context_mode = _bootstrap_context_mode(
            unattended=unattended,
            stateless=stateless,
            stateless_keep_project_rules=stateless_keep_project_rules,
        )

        async for event in runner.run(
            message,
            session_key,
            tool_context=tool_ctx,
            agent_id=agent_id,
            model=effective_model,
            timeout=timeout,
            max_iterations=max_iterations,
            iteration_timeout=iteration_timeout,
            tool_timeout=tool_timeout,
            request_timeout=request_timeout,
            max_provider_retries=max_provider_retries,
            length_capped_continuations=length_capped_continuations,
            history_has_persisted_user=True,
            no_memory_capture=no_memory_capture,
            attachments=run_attachments,
            bootstrap_context_mode=bootstrap_context_mode,
        ):
            if isinstance(event, TextDeltaEvent):
                text_parts.append(event.text)
            elif isinstance(event, ErrorEvent):
                errors.append({"message": event.message, "code": event.code})
            elif isinstance(event, ArtifactEvent):
                artifacts.append(artifact_payload(event))
            elif isinstance(event, DoneEvent):
                done = event
        usage = _usage_from_done(done, effective_model)
        transcript_usage = _to_transcript_usage(usage)
        if transcript_path:
            transcript = await svc.session_manager.get_transcript(session_key)
            _write_jsonl(transcript_path, _to_benchmark_transcript(transcript, transcript_usage))
    finally:
        await svc.close()

    if usage_path:
        _write_json(usage_path, usage)

    done_text_present, done_text = (
        done_text_snapshot(done) if done is not None else (False, "")
    )
    return AgentRunResult(
        status="error" if errors else "ok",
        agent_id=agent_id,
        session_key=session_key,
        text=done_text if done_text_present else "".join(text_parts),
        usage=usage,
        errors=errors,
        workspace=tool_workspace_dir,
        workspace_strict=effective_workspace_strict,
        workspace_lockdown=workspace_lockdown,
        workspace_write_deny_globs=list(tool_ctx.workspace_write_deny_globs),
        scratch_dir=effective_scratch_dir,
        thinking=thinking or getattr(getattr(service_cfg, "llm", None), "thinking", None),
        transcript_path=transcript_path,
        usage_path=usage_path,
        artifacts=artifacts,
        routing=_routing_from_done(done),
    )


def _bootstrap_context_mode(
    *,
    unattended: bool,
    stateless: bool,
    stateless_keep_project_rules: bool,
) -> str | None:
    if stateless_keep_project_rules:
        return "stateless_keep_project_rules"
    if stateless:
        return "stateless"
    if unattended:
        return "unattended"
    return None


def _routing_from_done(done: Any | None) -> dict[str, Any] | None:
    if done is None:
        return None
    routing = {
        "routed_tier": getattr(done, "routed_tier", None),
        "routing_source": getattr(done, "routing_source", "none"),
        "routing_confidence": getattr(done, "routing_confidence", 0.0),
        "baseline_model": getattr(done, "baseline_model", ""),
        "routed_model": getattr(done, "routed_model", ""),
    }
    if (
        routing["routed_tier"] is None
        and routing["routing_source"] == "none"
        and not routing["routing_confidence"]
        and not routing["baseline_model"]
        and not routing["routed_model"]
    ):
        return None
    return routing


def _with_agent_workspace_config(config: Any, workspace: str) -> Any:
    memory = getattr(config, "memory", None)
    if memory is not None and hasattr(memory, "model_copy"):
        memory = memory.model_copy(update={"source": "workspace"})
    elif memory is not None:
        memory = copy.copy(memory)
        setattr(memory, "source", "workspace")

    update: dict[str, Any] = {"workspace_dir": workspace}
    if memory is not None:
        update["memory"] = memory
    if hasattr(config, "model_copy"):
        return config.model_copy(update=update)
    copied = copy.copy(config)
    setattr(copied, "workspace_dir", workspace)
    if memory is not None:
        setattr(copied, "memory", memory)
    return copied


def _with_memory_source_config(config: Any, source: str) -> Any:
    memory = getattr(config, "memory", None)
    if memory is None:
        return config
    if hasattr(memory, "model_copy"):
        memory = memory.model_copy(update={"source": source})
    else:
        memory = copy.copy(memory)
        setattr(memory, "source", source)

    if hasattr(config, "model_copy"):
        return config.model_copy(update={"memory": memory})
    copied = copy.copy(config)
    setattr(copied, "memory", memory)
    return copied


def _with_agent_thinking_config(config: Any, thinking: str) -> Any:
    llm = getattr(config, "llm", None)
    if llm is None:
        return config
    if hasattr(llm, "model_copy"):
        llm = llm.model_copy(update={"thinking": thinking})
    else:
        llm = copy.copy(llm)
        setattr(llm, "thinking", thinking)

    if hasattr(config, "model_copy"):
        return config.model_copy(update={"llm": llm})
    copied = copy.copy(config)
    setattr(copied, "llm", llm)
    return copied


def _with_agent_model_config(config: Any, model: str) -> Any:
    llm = getattr(config, "llm", None)
    if llm is None:
        return config
    if hasattr(llm, "model_copy"):
        llm = llm.model_copy(update={"model": model})
    else:
        llm = copy.copy(llm)
        setattr(llm, "model", model)

    if hasattr(config, "model_copy"):
        return config.model_copy(update={"llm": llm})
    copied = copy.copy(config)
    setattr(copied, "llm", llm)
    return copied


def _agent_model_from_config(config: Any, agent_id: str) -> str | None:
    try:
        from opensquilla.agents.scope import resolve_agent_model

        return resolve_agent_model(agent_id, config)
    except Exception:
        return None


def _resolve_workspace_strict(
    *,
    cli_value: bool | None,
    config_value: Any,
    entrypoint_default: bool,
    env: dict[str, str] | None = None,
) -> bool:
    if cli_value is not None:
        return cli_value

    env_value = _parse_bool((env or os.environ).get("OPENSQUILLA_WORKSPACE_STRICT"))
    if env_value is not None:
        return env_value

    if isinstance(config_value, bool):
        return config_value
    return entrypoint_default


def _parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def _usage_from_done(done: Any | None, model: str | None) -> dict[str, Any]:
    usage = {
        "input_tokens": done.input_tokens if done else 0,
        "output_tokens": done.output_tokens if done else 0,
        "total_tokens": (done.input_tokens + done.output_tokens) if done else 0,
        "reasoning_tokens": done.reasoning_tokens if done else 0,
        "cached_tokens": done.cached_tokens if done else 0,
        "cost_usd": done.cost_usd if done else 0.0,
        "billed_cost": done.billed_cost if done else 0.0,
        "model": (done.model or model or "") if done else (model or ""),
        "request_count": done.iterations if done else 0,
    }
    if done is not None:
        model_usage_breakdown = getattr(done, "model_usage_breakdown", None)
        if isinstance(model_usage_breakdown, list) and model_usage_breakdown:
            usage["model_usage_breakdown"] = [
                dict(row) for row in model_usage_breakdown if isinstance(row, dict)
            ]
        ensemble_trace = getattr(done, "ensemble_trace", None)
        if isinstance(ensemble_trace, dict) and ensemble_trace:
            usage["ensemble_trace"] = dict(ensemble_trace)
    return usage


def _to_benchmark_transcript(
    entries: list[Any], usage: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Convert OpenSquilla transcript rows into benchmark-friendly JSONL events."""
    output: list[dict[str, Any]] = []
    for entry in entries:
        role = getattr(entry, "role", "")
        content = getattr(entry, "content", "") or ""
        tool_calls = getattr(entry, "tool_calls", None) or []
        timestamp = _entry_timestamp(entry)
        if role == "assistant" and tool_calls:
            assistant_blocks: list[dict[str, Any]] = []
            for segment in tool_calls:
                segment_type = segment.get("type")
                if segment_type == "text":
                    text = segment.get("text", "")
                    if text:
                        assistant_blocks.append({"type": "text", "text": text})
                elif segment_type == "tool_use":
                    assistant_blocks.append(
                        {
                            "type": "toolCall",
                            "name": segment.get("name", ""),
                            "id": segment.get("tool_use_id", ""),
                            "arguments": segment.get("input") or {},
                        }
                    )
                elif segment_type == "tool_result":
                    if assistant_blocks:
                        output.append(
                            _message_event("assistant", assistant_blocks, timestamp=timestamp)
                        )
                        assistant_blocks = []
                    output.append(
                        _message_event(
                            "toolResult",
                            [{"type": "text", "text": str(segment.get("result", ""))}],
                            timestamp=timestamp,
                            tool_call_id=segment.get("tool_use_id", ""),
                            tool_name=segment.get("name", ""),
                            is_error=bool(segment.get("is_error", False)),
                            execution_status=(
                                segment.get("execution_status")
                                if isinstance(segment.get("execution_status"), dict)
                                else None
                            ),
                            result_truncated=(
                                True if segment.get("result_truncated") is True else None
                            ),
                            result_original_chars=(
                                segment.get("result_original_chars")
                                if isinstance(segment.get("result_original_chars"), int)
                                else None
                            ),
                        )
                    )
            if assistant_blocks:
                output.append(_message_event("assistant", assistant_blocks, timestamp=timestamp))
            continue

        output.append(
            _message_event(
                role,
                [{"type": "text", "text": content}] if content else [],
                timestamp=timestamp,
            )
        )
    if usage is not None:
        for event in reversed(output):
            message = event.get("message", {})
            if message.get("role") == "assistant":
                message["usage"] = usage
                break
    return output


def _message_event(
    role: str,
    content: list[dict[str, Any]],
    *,
    timestamp: str | None = None,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
    is_error: bool | None = None,
    execution_status: dict[str, Any] | None = None,
    result_truncated: bool | None = None,
    result_original_chars: int | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {"type": "message", "message": {"role": role, "content": content}}
    message = event["message"]
    if tool_call_id is not None:
        message["toolCallId"] = tool_call_id
    if tool_name is not None:
        message["toolName"] = tool_name
    if is_error is not None:
        message["isError"] = is_error
    if execution_status is not None:
        message["executionStatus"] = execution_status
    if result_truncated is not None:
        message["resultTruncated"] = result_truncated
    if result_original_chars is not None:
        message["resultOriginalChars"] = result_original_chars
    if timestamp:
        event["timestamp"] = timestamp
    return event


def _entry_timestamp(entry: Any) -> str | None:
    value = getattr(entry, "created_at", None)
    if not isinstance(value, int | float):
        return None
    return datetime.fromtimestamp(value / 1000, UTC).isoformat().replace("+00:00", "Z")


def _to_transcript_usage(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "input": usage["input_tokens"],
        "output": usage["output_tokens"],
        "cacheRead": usage["cached_tokens"],
        "cacheWrite": 0,
        "totalTokens": usage["total_tokens"],
        "cost": {
            "input": 0.0,
            "output": 0.0,
            "cacheRead": 0.0,
            "cacheWrite": 0.0,
            "total": usage["cost_usd"],
            "billed": usage["billed_cost"],
        },
    }


def _write_jsonl(path: str, rows: list[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_json(path: str, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def _print_no_provider_error() -> None:
    """Print a three-section diagnostic panel when no LLM provider is configured."""
    body = Text.assemble(
        ("Symptom\n", "bold red"),
        "No LLM provider configured.\n\n",
        ("Cause\n", "bold yellow"),
        (
            "No API key was found. The following environment variables were all empty:\n"
            "  OPENROUTER_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY,\n"
            "  DEEPSEEK_API_KEY, GEMINI_API_KEY, DASHSCOPE_API_KEY, and others.\n"
            "The config file ~/.opensquilla/config.toml also has no [llm].api_key set.\n\n"
        ),
        ("Next steps\n", "bold green"),
        (
            "Option 1 (recommended) — run the interactive setup wizard:\n"
            "  opensquilla onboard\n\n"
            "Option 2 — set an environment variable for your provider:\n"
            "  export OPENROUTER_API_KEY=sk-or-...        # POSIX / macOS / Linux\n"
            "  setx OPENROUTER_API_KEY \"sk-or-...\"  "
            "# Windows cmd: set OPENROUTER_API_KEY=...\n\n"
            "Option 3 — edit ~/.opensquilla/config.toml and add:\n"
            "  [llm]\n"
            "  api_key = \"your-key-here\"\n"
        ),
    )
    console.print(Panel(body, title="No Provider Configured", border_style="red"))


def run_agent_command(
    message: str = typer.Option(..., "--message", "-m", help="Message to send"),
    agent_id: str = typer.Option("main", "--agent", help="Agent identifier"),
    session_id: str = typer.Option("", "--session-id", help="Session key/id to use"),
    model: str = typer.Option("", "--model", help="Model override (provider/model)"),
    workspace: str = typer.Option("", "--workspace", help="Workspace root for this run"),
    workspace_strict: bool | None = typer.Option(
        None,
        "--workspace-strict/--no-workspace-strict",
        help="Restrict read-side file tools to --workspace",
    ),
    workspace_lockdown: bool = typer.Option(
        False,
        "--workspace-lockdown",
        help=(
            "Opt in to automation write containment: writes must stay under "
            "--workspace or --scratch-dir."
        ),
    ),
    workspace_lockdown_deny_paths: list[str] = typer.Option(
        [],
        "--workspace-lockdown-deny-paths",
        help=(
            "Workspace-relative write deny glob(s) for automation containment; "
            "repeat or comma-separate."
        ),
    ),
    scratch_dir: str = typer.Option(
        "",
        "--scratch-dir",
        help="Directory for temporary scripts, logs, debug output, and candidate patches.",
    ),
    timeout: float | None = typer.Option(
        None, "--timeout", "-T", help="Total agent timeout in seconds (0=unlimited)"
    ),
    max_iterations: int | None = typer.Option(
        None,
        "--max-iterations",
        min=0,
        help="Maximum agent model/tool loop iterations (0=unlimited)",
    ),
    iteration_timeout_seconds: float | None = typer.Option(
        None,
        "--iteration-timeout-seconds",
        help="Per-iteration timeout in seconds (one LLM call + its tool executions)",
    ),
    tool_timeout_seconds: float | None = typer.Option(
        None,
        "--tool-timeout-seconds",
        help="Per-tool execution timeout in seconds",
    ),
    request_timeout_seconds: float | None = typer.Option(
        None,
        "--request-timeout-seconds",
        help="Single LLM HTTP/streaming request timeout in seconds",
    ),
    max_provider_retries: int | None = typer.Option(
        None,
        "--max-provider-retries",
        min=0,
        help="Maximum provider-level retries for transient errors",
    ),
    length_capped_continuations: int | None = typer.Option(
        None,
        "--length-capped-continuations",
        min=1,
        help="Maximum automatic continuations after provider output reaches its length limit",
    ),
    thinking: str = typer.Option(
        "",
        "--thinking",
        help="Thinking level override: off|minimal|low|medium|high|xhigh|adaptive",
    ),
    transcript_path: str = typer.Option(
        "", "--transcript-path", help="Write benchmark-compatible JSONL transcript"
    ),
    usage_path: str = typer.Option("", "--usage-path", help="Write usage JSON to this file"),
    session_db_path: str = typer.Option(
        ":memory:",
        "--session-db-path",
        help="Persistent session SQLite path for cross-invocation replay",
    ),
    no_memory_capture: bool = typer.Option(
        False,
        "--no-memory-capture",
        help="Do not write this invocation to durable searchable memory",
    ),
    file_paths: list[str] = typer.Option(
        [],
        "--file",
        "-f",
        help="Attach a local file; repeat for multiple files",
    ),
    unattended: bool = typer.Option(
        True,
        "--unattended/--interactive",
        help=(
            "Run without a live approval surface. Unattended is the default for "
            "single-shot automation."
        ),
    ),
    stateless: bool = typer.Option(
        False,
        "--stateless/--no-stateless",
        help="Use clean-room prompt bootstrap; does not change --unattended semantics.",
    ),
    clean_room: bool = typer.Option(
        False,
        "--clean-room",
        help="Alias for --stateless.",
    ),
    stateless_keep_project_rules: bool = typer.Option(
        False,
        "--stateless-keep-project-rules",
        help="With clean-room bootstrap, keep AGENTS.md project rules only.",
    ),
    permissions: str | None = typer.Option(
        None,
        "--permissions",
        help=(
            "Permission profile for single-shot runs: restricted/off, on, bypass, or full. "
            "Defaults to OPENSQUILLA_AGENT_PERMISSIONS, then permissions.default_mode."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Run a single agent turn for automation."""
    message = _unwrap_typer_default(message)
    agent_id = _unwrap_typer_default(agent_id)
    session_id = _unwrap_typer_default(session_id)
    model = _unwrap_typer_default(model)
    workspace = _unwrap_typer_default(workspace)
    workspace_strict = _unwrap_typer_default(workspace_strict)
    workspace_lockdown = _unwrap_typer_default(workspace_lockdown)
    workspace_lockdown_deny_paths = _unwrap_typer_default(workspace_lockdown_deny_paths)
    scratch_dir = _unwrap_typer_default(scratch_dir)
    timeout = _unwrap_typer_default(timeout)
    max_iterations = _unwrap_typer_default(max_iterations)
    iteration_timeout_seconds = _unwrap_typer_default(iteration_timeout_seconds)
    tool_timeout_seconds = _unwrap_typer_default(tool_timeout_seconds)
    request_timeout_seconds = _unwrap_typer_default(request_timeout_seconds)
    max_provider_retries = _unwrap_typer_default(max_provider_retries)
    length_capped_continuations = _unwrap_typer_default(length_capped_continuations)
    thinking = _unwrap_typer_default(thinking)
    transcript_path = _unwrap_typer_default(transcript_path)
    usage_path = _unwrap_typer_default(usage_path)
    session_db_path = _unwrap_typer_default(session_db_path)
    no_memory_capture = _unwrap_typer_default(no_memory_capture)
    file_paths = _unwrap_typer_default(file_paths)
    unattended = _unwrap_typer_default(unattended)
    stateless = _unwrap_typer_default(stateless)
    clean_room = _unwrap_typer_default(clean_room)
    stateless_keep_project_rules = _unwrap_typer_default(stateless_keep_project_rules)
    permissions = _unwrap_typer_default(permissions)
    json_output = _unwrap_typer_default(json_output)
    workspace_write_deny_globs = _parse_workspace_write_deny_globs(
        workspace_lockdown_deny_paths
    )

    result = asyncio.run(
        run_agent_once(
            message=message,
            agent_id=agent_id,
            session_id=session_id,
            model=model or None,
            workspace=workspace or None,
            workspace_strict=workspace_strict,
            workspace_lockdown=workspace_lockdown,
            workspace_write_deny_globs=workspace_write_deny_globs,
            scratch_dir=scratch_dir or None,
            thinking=thinking or None,
            timeout=timeout,
            max_iterations=max_iterations,
            iteration_timeout=iteration_timeout_seconds,
            tool_timeout=tool_timeout_seconds,
            request_timeout=request_timeout_seconds,
            max_provider_retries=max_provider_retries,
            length_capped_continuations=length_capped_continuations,
            transcript_path=transcript_path or None,
            usage_path=usage_path or None,
            session_db_path=session_db_path,
            no_memory_capture=no_memory_capture,
            attachment_paths=list(file_paths or []),
            unattended=unattended,
            stateless=stateless or clean_room,
            stateless_keep_project_rules=stateless_keep_project_rules,
            permissions=permissions,
        )
    )
    artifacts = _public_artifacts(result.artifacts)
    payload = {
        "status": result.status,
        "agent_id": result.agent_id,
        "session_key": result.session_key,
        "text": result.text,
        "usage": result.usage,
        "errors": result.errors,
        "workspace": result.workspace,
        "workspace_strict": result.workspace_strict,
        "workspace_lockdown": result.workspace_lockdown,
        "workspace_write_deny_globs": result.workspace_write_deny_globs or [],
        "scratch_dir": result.scratch_dir,
        "routing": result.routing,
        "thinking": result.thinking,
        "transcript_path": result.transcript_path,
        "usage_path": result.usage_path,
        "artifacts": artifacts,
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
    else:
        if result.text:
            typer.echo(result.text)
        for artifact in artifacts:
            name = artifact.get("name") if isinstance(artifact.get("name"), str) else "artifact"
            target = (
                artifact.get("download_url")
                if isinstance(artifact.get("download_url"), str)
                else artifact.get("id", "")
            )
            typer.echo(f"Generated file: {name} -> {target}")
        if result.errors:
            for error in result.errors:
                if error.get("code") == "no_provider":
                    _print_no_provider_error()
                    raise typer.Exit(1)
                typer.echo(f"Error: {error['message']}", err=True)
