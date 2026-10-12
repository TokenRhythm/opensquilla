#!/usr/bin/env python3
"""Run one MetaSkill using the historical OpenSquilla runtime."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager, nullcontext
import logging
import json
import math
import os
from pathlib import Path
import signal
import sys
import tomllib
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
# Also support `python scripts/run_metaskill.py` from a source checkout.
sys.path.insert(0, str(ROOT / "src"))


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-dir", type=Path, default=ROOT / "examples" / "skills")
    parser.add_argument("--name", default="meta-text-demo", help="MetaSkill name in SKILL.md")
    task_input = parser.add_mutually_exclusive_group()
    task_input.add_argument("--input", default="Hello MetaSkill", help="Task text (not a file path)")
    task_input.add_argument("--input-file", type=Path, help="Read task text from a UTF-8 file")
    parser.add_argument("--workspace", type=Path, default=ROOT / "workspace")
    parser.add_argument("--config", type=Path, help="Explicit TOML model configuration")
    parser.add_argument("--run-mode", choices=("safe", "full"), help="Override tools.run_mode")
    parser.add_argument("--inputs-json", type=Path, help="JSON object with additional DAG inputs")
    parser.add_argument("--trace-json", type=Path, help="Write a native execution trace inside --workspace")
    parser.add_argument("--max-trace-field-bytes", type=int, help="Positive trace field limit; requires --trace-json")
    parser.add_argument("--list-tools", action="store_true", help="List the standalone native tool surface")
    return parser.parse_args(argv)


@contextmanager
def isolated_state(workspace: Path):
    """Keep runtime caches inside this invocation's workspace, not a user profile."""
    keys = ("OPENSQUILLA_STATE_DIR", "OPENSQUILLA_VISION_PROVIDER", "OPENSQUILLA_VISION_MODEL")
    previous = {key: os.environ.get(key) for key in keys}
    state = workspace / ".metaskill-state"
    state.mkdir(parents=True, exist_ok=True)
    os.environ["OPENSQUILLA_STATE_DIR"] = str(state)
    # Native vision tools must use this CLI's deployment configuration.
    os.environ.pop("OPENSQUILLA_VISION_PROVIDER", None)
    os.environ.pop("OPENSQUILLA_VISION_MODEL", None)
    try:
        yield state
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def read_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    with path.expanduser().resolve().open("rb") as stream:
        config = tomllib.load(stream)
    unknown = set(config) - {"model", "vision", "runtime", "tools", "search"}
    if unknown:
        raise ValueError(f"Unknown configuration sections: {', '.join(sorted(unknown))}")
    allowed_model = {"name", "base_url", "api_key_env", "provider", "provider_kind", "proxy", "org_id", "provider_routing"}
    allowed_runtime = {"max_parallelism", "max_iterations", "max_tokens", "timeout_seconds",
                       "context_window_tokens", "request_timeout", "tool_timeout",
                       "temperature", "top_p", "thinking", "thinking_budget_tokens"}
    allowed_sections = {
        "model": allowed_model, "vision": allowed_model, "runtime": allowed_runtime,
        "tools": {"run_mode", "sandbox_backend", "allow", "deny"},
        "search": {"provider", "api_key_env", "proxy", "max_results"},
    }
    for section, allowed in allowed_sections.items():
        if not isinstance(config.get(section, {}), dict):
            raise ValueError(f"[{section}] must be a TOML table")
        unknown = set(config.get(section, {})) - allowed
        if unknown:
            raise ValueError(f"Unknown [{section}] settings: {', '.join(sorted(unknown))}")
    integer_fields = {"max_parallelism", "max_iterations", "max_tokens", "context_window_tokens", "thinking_budget_tokens"}
    for key, value in config.get("runtime", {}).items():
        if key == "thinking":
            levels = {"off", "minimal", "low", "medium", "high", "xhigh", "adaptive"}
            if not isinstance(value, bool) and (not isinstance(value, str) or value not in levels):
                raise ValueError("runtime.thinking must be a boolean or a native thinking level")
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError(f"runtime.{key} must be a finite number")
        if key == "temperature":
            if not 0 <= value <= 2:
                raise ValueError("runtime.temperature must be between 0 and 2")
            continue
        if key == "top_p":
            if not 0 < value <= 1:
                raise ValueError("runtime.top_p must be greater than 0 and at most 1")
            continue
        if value <= 0:
            raise ValueError(f"runtime.{key} must be a positive number")
        if key in integer_fields and not isinstance(value, int):
            raise ValueError(f"runtime.{key} must be an integer")
    if config.get("runtime", {}).get("max_iterations", 20) > 30:
        raise ValueError("runtime.max_iterations must be at most 30 (native MetaSkill limit)")
    tools = config.get("tools", {})
    if "run_mode" in tools:
        mode = tools["run_mode"]
        if not isinstance(mode, str) or mode not in {"safe", "full"}:
            raise ValueError("tools.run_mode must be 'safe' or 'full'")
    if "sandbox_backend" in tools:
        # Match the native SandboxSettings BackendName; platform availability
        # and mode combinations remain the native runtime's responsibility.
        backends = {"auto", "bubblewrap", "seatbelt", "noop", "windows_default"}
        backend = tools["sandbox_backend"]
        if not isinstance(backend, str) or backend not in backends:
            raise ValueError("tools.sandbox_backend must be one of: auto, bubblewrap, seatbelt, noop, windows_default")
    for key in ("allow", "deny"):
        value = tools.get(key)
        if value is not None and (not isinstance(value, list) or any(not isinstance(x, str) for x in value)):
            raise ValueError(f"tools.{key} must be a list of native tool names")
    search = config.get("search", {})
    for key in ("provider", "api_key_env", "proxy"):
        if key in search and not isinstance(search[key], str):
            raise ValueError(f"search.{key} must be a string")
    if "max_results" in search:
        value = search["max_results"]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError("search.max_results must be a positive integer")
    return config


def provider_config(settings: dict[str, Any]):
    from opensquilla.provider.registry import get_provider_spec
    from opensquilla.provider.selector import ProviderConfig

    for field in ("name", "provider", "provider_kind", "base_url", "api_key_env", "proxy", "org_id"):
        if field in settings and not isinstance(settings[field], str):
            raise ValueError(f"model.{field} must be a string")
    if not isinstance(settings.get("provider_routing", {}), dict):
        raise ValueError("model.provider_routing must be a TOML table")
    if not isinstance(settings.get("name"), str) or not settings["name"].strip():
        raise ValueError("Set model.name in your --config file")
    provider_id = settings.get("provider", settings.get("provider_kind", "openai"))
    spec = get_provider_spec(provider_id)
    # OAuth adapters require a separate application login lifecycle.
    if not spec.runtime_supported or spec.backend.startswith("unsupported") or spec.backend == "openai_codex":
        raise ValueError(f"Provider {provider_id!r} requires a separate application login")
    key_name = settings.get("api_key_env", "")
    api_key = os.environ.get(key_name, "").strip()
    if spec.requires_api_key() and not api_key:
        raise ValueError(f"Set {key_name or 'model.api_key_env'} before using this provider")
    endpoint_text = settings.get("base_url", "") or spec.default_base_url
    if endpoint_text:
        endpoint = urlparse(endpoint_text)
        if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
            raise ValueError("model.base_url must be an HTTP(S) API endpoint")
        if endpoint.username or endpoint.password:
            raise ValueError("Keep credentials in the API key environment variable")
    return ProviderConfig(
        provider=provider_id, model=settings["name"], api_key=api_key,
        base_url=endpoint_text, org_id=settings.get("org_id", ""),
        proxy=settings.get("proxy", ""), provider_routing=settings.get("provider_routing", {}),
    )


def build_model_runtime(config: dict[str, Any], workspace: Path, skills_dir: Path, *, catalog=None,
                        usage_tracker=None, usage_event_sink=None, usage_context=None, session_key=None):
    """Compose the real provider, Agent tool loop, and MetaSkill host factories."""
    from opensquilla.engine.agent import Agent
    from opensquilla.engine.types import AgentConfig, ThinkingLevel
    from opensquilla.provider.selector import build_provider_from_config
    from opensquilla.skills.meta.orchestrator import (
        make_agent_runner_from_parent,
        make_llm_chat_from_provider,
    )
    from metaskill_tools import build_tools

    deployment = provider_config(config.get("model", {}))
    runtime = config.get("runtime", {})
    from opensquilla.provider.selector import ProviderBuildError

    try:
        provider = build_provider_from_config(deployment)
    except ProviderBuildError as exc:
        raise ValueError(str(exc)) from exc
    extra = {key: value for key, value in runtime.items()
             if key not in {"max_parallelism", "max_iterations", "max_tokens", "timeout_seconds"}}
    if isinstance(extra.get("thinking"), str):
        extra["thinking"] = ThinkingLevel(extra["thinking"])
    base_config = AgentConfig(
        model_id=deployment.model,
        provider_id=deployment.provider,
        workspace_dir=str(workspace),
        max_iterations=runtime.get("max_iterations", 20),
        max_tokens=runtime.get("max_tokens", 8192),
        timeout=float(runtime.get("timeout_seconds", 600)),
        iteration_timeout=float(runtime.get("timeout_seconds", 600)),
        flush_enabled=False,
        **extra,
    )
    configure_media(config)
    definitions, handler = build_tools(workspace, skills_dir, config=config, catalog=catalog,
                                       session_key=session_key)
    runner = make_agent_runner_from_parent(
        provider=provider,
        base_config=base_config,
        tool_definitions=definitions,
        tool_handler=handler,
        agent_factory=Agent,
        workspace_dir=str(workspace),
        usage_tracker=usage_tracker, session_key=session_key,
        usage_event_sink=usage_event_sink, usage_execution_context=usage_context,
    )
    chat = make_llm_chat_from_provider(
        provider=provider, base_config=base_config, max_tokens=base_config.max_tokens,
        usage_tracker=usage_tracker, session_key=session_key,
        usage_event_sink=usage_event_sink, usage_execution_context=usage_context,
    )
    return runner, chat, handler


def configure_media(config: dict[str, Any]) -> None:
    """Bind native image analysis to the explicitly selected deployment."""
    from dataclasses import asdict
    from opensquilla.tools.builtin.media import configure_image_generation

    settings = config.get("vision", config.get("model"))
    if settings is None:
        return
    vision = provider_config(settings)
    configure_image_generation(
        None, llm_config=asdict(vision),
        squilla_router_config={"tiers": {"image_model": {
            "provider": vision.provider, "model": vision.model, "supports_image": True,
        }}},
    )


async def _no_model(*_args):
    raise RuntimeError("Model steps require an explicit --config file")
    yield  # Keep the AgentRunner async-iterator contract.


async def run(args: argparse.Namespace) -> int:
    skills_dir = args.skills_dir.expanduser().resolve()
    workspace = args.workspace.expanduser().resolve()
    trace_path = getattr(args, "trace_json", None)
    field_limit = getattr(args, "max_trace_field_bytes", None)
    if field_limit is not None:
        if isinstance(field_limit, bool) or not isinstance(field_limit, int) or field_limit <= 0:
            raise ValueError("--max-trace-field-bytes must be a positive integer")
        if trace_path is None:
            raise ValueError("--max-trace-field-bytes requires --trace-json")
    if trace_path is not None:
        trace_path = trace_path.expanduser().resolve()
        if not trace_path.is_relative_to(workspace):
            raise ValueError("--trace-json must be a file inside --workspace")
    if not skills_dir.is_dir():
        raise ValueError(f"Skills directory does not exist: {skills_dir}")
    config = read_config(args.config)
    input_file = getattr(args, "input_file", None)
    task_text = input_file.expanduser().resolve().read_text(encoding="utf-8-sig") if input_file else args.input
    if getattr(args, "run_mode", None):
        config.setdefault("tools", {})["run_mode"] = args.run_mode
    workspace.mkdir(parents=True, exist_ok=True)
    with isolated_state(workspace) as state:
        # Import only after the isolated state root is selected.
        from opensquilla.skills.loader import SkillLoader
        from opensquilla.skills.meta.orchestrator import (
            MetaOrchestrator,
            make_tool_invoker_from_handler,
        )
        from opensquilla.skills.meta.parser import parse_meta_plan
        from opensquilla.skills.meta.types import MetaMatch
        from opensquilla.tools.types import ToolContext, current_tool_context

        if getattr(args, "list_tools", False):
            from metaskill_tools import build_tools, aclose_tools

            definitions, handler = build_tools(workspace, skills_dir, config=config)
            try:
                print("\n".join(sorted(item.name for item in definitions)))
            finally:
                await aclose_tools(handler)
            return 0

        loader = SkillLoader(
            workspace_dir=skills_dir,
            snapshot_path=state / "skills_snapshot.json",
            lockfile_path=state / "skills-lock.json",
        )
        skill = loader.get_by_name(args.name)
        if skill is None:
            available = ", ".join(item.name for item in loader.load_all() if item.kind == "meta")
            raise ValueError(f"MetaSkill {args.name!r} not found. Available: {available or '(none)'}")
        plan = parse_meta_plan(skill)
        if plan is None:
            raise ValueError(f"{args.name!r} is not a MetaSkill")
        supported = {"skill_exec", "agent", "llm_chat", "llm_classify", "tool_call"}
        unsupported = sorted({step.kind for step in plan.steps} - supported)
        if unsupported:
            raise ValueError(f"This CLI does not support interactive step kinds: {', '.join(unsupported)}")
        needs_model = any(step.kind in {"agent", "llm_chat", "llm_classify"} for step in plan.steps)
        if needs_model and args.config is None:
            raise ValueError("This MetaSkill contains model steps; pass --config configs/local.toml")
        runner, chat, tool_invoker, handler = _no_model, None, None, None
        from opensquilla.engine.usage import UsageTracker
        from opensquilla.engine.usage_accounting import UsageAccountingScope, UsageExecutionContext, bind_usage_accounting_scope
        from metaskill_trace import LocalUsageSink

        invocation_id = uuid4().hex
        session_key = f"metaskill-cli:{invocation_id}"
        usage_tracker = UsageTracker()
        usage_context = UsageExecutionContext(
            execution_id=invocation_id, agent_run_id=invocation_id,
            turn_id=invocation_id, session_id=session_key,
            agent_id="standalone", run_kind="metaskill_cli",
        )
        usage_sink = LocalUsageSink(state / f"usage-{invocation_id}.jsonl") if trace_path is not None else None
        if needs_model:
            runner, chat, handler = build_model_runtime(
                config, workspace, skills_dir, catalog=loader.snapshot(),
                usage_tracker=usage_tracker, usage_event_sink=usage_sink,
                usage_context=usage_context, session_key=session_key,
            )
            tool_invoker = make_tool_invoker_from_handler(tool_handler=handler)
        elif any(step.kind == "tool_call" for step in plan.steps):
            from metaskill_tools import build_tools

            _, handler = build_tools(workspace, skills_dir, config=config, catalog=loader.snapshot(),
                                     session_key=session_key)
            configure_media(config)
            tool_invoker = make_tool_invoker_from_handler(tool_handler=handler)
        context_token = current_tool_context.set(getattr(handler, "_metaskill_context", None) or ToolContext(
            workspace_dir=str(workspace),
            skill_catalog=loader.snapshot(),
        ))
        try:
            inputs = {}
            if getattr(args, "inputs_json", None):
                inputs = json.loads(args.inputs_json.read_text(encoding="utf-8"))
                if not isinstance(inputs, dict):
                    raise ValueError("--inputs-json must contain a JSON object")
            inputs.update(user_message=task_text, workspace_dir=str(workspace))
            from metaskill_trace import open_trace

            scope = UsageAccountingScope(sink=usage_sink, context=usage_context) if usage_sink is not None else None
            with (open_trace(state, trace_path, max_field_bytes=field_limit, usage_sink=usage_sink)
                  if trace_path is not None else nullcontext(None)) as trace, bind_usage_accounting_scope(scope):
                orchestrator = MetaOrchestrator(
                    agent_runner=runner,
                    skill_loader=loader,
                    llm_chat=chat,
                    tool_invoker=tool_invoker,
                    workspace_dir=str(workspace),
                    max_parallelism=config.get("runtime", {}).get("max_parallelism", 4),
                    memory_persist_enabled=False,
                    run_writer=trace.writer if trace is not None else None,
                    usage_tracker=usage_tracker, session_key=session_key,
                )
                result = await orchestrator.run(MetaMatch(plan=plan, inputs=inputs))
                if trace is not None:
                    trace.export()
        finally:
            current_tool_context.reset(context_token)
            if handler is not None:
                from metaskill_tools import aclose_tools

                await aclose_tools(handler)
        if not result.ok:
            raise RuntimeError(f"MetaSkill failed at {result.failed_step_id or 'startup'}: {result.error}")
        print(result.final_text)
        return 0


async def _run_with_termination(args: argparse.Namespace) -> int:
    """Let POSIX batch termination unwind native tool cleanup before exiting."""
    if os.name != "posix":
        return await run(args)
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    previous = signal.getsignal(signal.SIGTERM)
    terminated = False

    def terminate() -> None:
        nonlocal terminated
        # A second TERM must not cancel the awaits in run()'s finally block.
        # The batch runner still enforces a bounded grace then SIGKILL.
        if not terminated and task is not None:
            terminated = True
            task.cancel()

    loop.add_signal_handler(signal.SIGTERM, terminate)
    try:
        return await run(args)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        signal.signal(signal.SIGTERM, previous)


def main(argv: list[str] | None = None) -> int:
    # Ensure redirected Windows output has the same encoding as Unix output.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = arguments(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    import structlog

    def drop_legacy_debug(_logger, _method, event):
        if str(event.get("event", "")).startswith("DEBUG_TRACE_"):
            raise structlog.DropEvent
        return event

    structlog.configure(
        processors=[drop_legacy_debug, *structlog.get_config()["processors"]],
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )
    try:
        return asyncio.run(_run_with_termination(args))
    except asyncio.CancelledError:
        print("MetaSkill terminated.", file=sys.stderr)
        return 128 + signal.SIGTERM
    except KeyboardInterrupt:
        print("MetaSkill cancelled.", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError) as exc:
        from opensquilla.redaction import redact_error_text

        print(f"Error: {redact_error_text(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
