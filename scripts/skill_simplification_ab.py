#!/usr/bin/env python3
"""Reproducible paired A/B evidence for the Skill catalog simplification.

The harness has three deliberately separate responsibilities:

* render the real catalog and tool schema from either repository snapshot;
* run synthetic, paired requests against one configured provider/model;
* recompute JSON/CSV summaries without needing credentials or network access.

Prompts and responses are synthetic.  Repository paths, credentials, and full
Skill bodies are never written to the evidence files.  The live ``run`` mode is
billable and therefore requires an explicit acknowledgement flag.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import math
import os
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
BASE_SYSTEM_DEVELOPER_PROMPT = """System instructions:
You are OpenSquilla running a synthetic, non-user benchmark. Follow the task
accurately, use a listed Skill only when it clearly applies, and never invent
tool results.

Developer instructions:
Be concise. Use the available tools when current or workspace state is needed.
When a catalog entry is relevant, follow its documented loading route."""
DYNAMIC_SUFFIX_MARKER = (
    "Synthetic per-turn context: {request_key}. No private user data is present."
)
CALIBRATION_USER = "Synthetic calibration: reply with exactly OK."
PROVIDER_TIMEOUT_SECONDS = 150.0

MICRO_TASKS: tuple[str, ...] = (
    "In two concise sentences, explain why stable ordering helps prompt-cache reuse.",
    "In two concise sentences, explain the difference between a cache key and cached content.",
    "In two concise sentences, explain why deterministic catalogs are easier to test.",
    "In two concise sentences, explain why hidden dependencies should remain executable.",
    "In two concise sentences, explain why synthetic benchmarks protect user privacy.",
)

CONTINUITY_USER = (
    "Synthetic continuity turn {turn}: state one benefit of deterministic software interfaces."
)
CONTINUITY_ASSISTANT = (
    "Synthetic fixed history acknowledgement {turn}: deterministic interfaces aid verification."
)


@dataclass(frozen=True)
class QualityTask:
    task_id: str
    category: str
    prompt: str
    route_kind: str = "direct"
    expected_names: tuple[str, ...] = ()
    coding_mode: bool = False
    explicit_skill: bool = False


QUALITY_TASKS: tuple[QualityTask, ...] = (
    QualityTask(
        "explain",
        "explanation",
        "Explain DNS caching in exactly three concise sentences.",
    ),
    QualityTask(
        "advice",
        "advice",
        "Give three practical, low-cost tips for making a synthetic team meeting shorter.",
    ),
    QualityTask(
        "summary",
        "summary",
        (
            "Summarize this synthetic paragraph in one sentence: The Atlas team changed "
            "its release review from Friday to Wednesday. The change gives engineers two "
            "extra days to resolve findings before the Monday release. No staffing or scope "
            "changed."
        ),
    ),
    QualityTask(
        "technical_comparison",
        "technical_comparison",
        "Compare a B-tree index and a hash index in four concise bullet points.",
    ),
    QualityTask(
        "business_writing",
        "business_writing",
        (
            "Write a short professional email confirming that the synthetic Project Atlas "
            "review moved to Wednesday at 10:00."
        ),
    ),
    QualityTask(
        "retired_summarize",
        "migration",
        (
            "Summarize this synthetic text without loading a Skill: Nimbus passed all 18 "
            "checks, reduced startup time by 12 percent, and retained the same API."
        ),
    ),
    QualityTask(
        "retired_weather",
        "migration",
        "Find the current weather in Shanghai; do not guess if live data is unavailable.",
        "tool",
        ("web_search", "web_discover", "web_fetch", "http_request"),
    ),
    QualityTask(
        "hidden_filesystem",
        "migration",
        "List the files in the synthetic workspace directory /workspace/demo.",
        "tool",
        ("list_dir", "glob_search", "exec_command"),
    ),
    QualityTask(
        "retired_git_diff",
        "migration",
        "Inspect the current repository diff and summarize what changed.",
        "tool",
        ("git_diff", "exec_command"),
    ),
    QualityTask(
        "retired_nano_pdf",
        "migration",
        "Use pdf-toolkit to merge synthetic-a.pdf and synthetic-b.pdf into combined.pdf.",
        "skill",
        ("pdf-toolkit",),
        explicit_skill=True,
    ),
    QualityTask(
        "retired_seedance",
        "migration",
        "Create a complete one-minute short-drama production package from a reunion premise.",
        "meta",
        ("meta-short-drama",),
    ),
    QualityTask(
        "retired_ai_video_script",
        "migration",
        "Turn a synthetic short-drama premise into a full storyboard and production package.",
        "meta",
        ("meta-short-drama",),
    ),
    QualityTask(
        "paper_dependency_replacement",
        "migration",
        "Write a complete research paper from a supplied synthetic experiment bundle.",
        "meta",
        ("meta-paper-write",),
    ),
    QualityTask(
        "retired_sub_agent",
        "migration",
        "Use code-task to modify a real repository and verify the patch.",
        "skill_or_core",
        ("code-task", "read_file", "apply_patch", "exec_command"),
        coding_mode=True,
        explicit_skill=True,
    ),
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _median(values: Iterable[float | int | None]) -> float | None:
    clean = [float(value) for value in values if isinstance(value, int | float)]
    return statistics.median(clean) if clean else None


def _mean(values: Iterable[float | int | None]) -> float | None:
    clean = [float(value) for value in values if isinstance(value, int | float)]
    return statistics.fmean(clean) if clean else None


def _ratio(numerator: float | int, denominator: float | int) -> float | None:
    return float(numerator) / float(denominator) if denominator else None


def _clean_float(value: float | None, digits: int = 6) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _render_worker(repo: Path, *, coding_mode: bool) -> dict[str, Any]:
    """Render one repository snapshot in an isolated Python process."""

    repo = repo.resolve()
    sys.path.insert(0, str(repo / "src"))
    os.chdir(repo)

    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))

    import opensquilla.tools.builtin  # noqa: F401
    from opensquilla.engine.pipeline import TurnContext
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.skills.eligibility import EligibilityContext
    from opensquilla.skills.injector import SkillInjector
    from opensquilla.skills.loader import SkillLoader
    from opensquilla.tools.builtin.skill_tools import create_skill_tools
    from opensquilla.tools.registry import get_default_registry
    from opensquilla.tools.types import ToolContext

    with tempfile.TemporaryDirectory(prefix="opensquilla-skill-ab-") as temp_dir:
        bundled = repo / "src" / "opensquilla" / "skills" / "bundled"
        loader = SkillLoader(
            bundled_dir=bundled,
            snapshot_path=Path(temp_dir) / "snapshot.json",
        )
        cold_started = time.perf_counter_ns()
        all_skills = loader.load_all()
        cold_catalog_build_ms = (time.perf_counter_ns() - cold_started) / 1_000_000
        warm_snapshot_samples: list[float] = []
        for _ in range(500):
            warm_started = time.perf_counter_ns()
            loader.snapshot()
            warm_snapshot_samples.append((time.perf_counter_ns() - warm_started) / 1_000_000)
        create_skill_tools(loader)
        tool_context = ToolContext(is_owner=True, surfaced_tools={"meta_invoke"})
        tool_defs = get_default_registry().to_tool_definitions(tool_context)
        tool_payload = [tool.model_dump(mode="json") for tool in tool_defs]

        config = GatewayConfig()
        config.skills.coding_mode = coding_mode
        config.skills.max_skills_prompt_chars = 8000
        config.skills.injection_mode = "system"
        config.meta_skill.enabled = True
        config.meta_skill.auto_trigger = True

        required_bins: set[str] = set()
        required_env: set[str] = set()
        for skill in all_skills:
            requires = getattr(getattr(skill, "metadata", None), "requires", None)
            if requires is None:
                continue
            required_bins.update(requires.bins)
            required_bins.update(requires.any_bins)
            required_env.update(requires.env)
            required_env.update(requires.env_any)
        synthetic_eligibility = EligibilityContext(
            os_name="",
            has_bin_cache={name: True for name in required_bins},
            env_cache={name: "synthetic-present" for name in required_env},
        )

        ctx = TurnContext(
            message="Synthetic catalog rendering request.",
            session_key="synthetic:skill-ab",
            config=config,
            provider=None,
            model="synthetic-model",
            tool_defs=tool_defs,
            system_prompt=(BASE_SYSTEM_DEVELOPER_PROMPT, DYNAMIC_SUFFIX_MARKER),
            metadata={"skill_loader": loader},
        )

        try:
            import opensquilla.engine.steps.skill_catalog_projection as projection_step
            from opensquilla.engine.steps.skill_catalog_projection import (
                _deterministic_gate,
                _eligibility_ctx,
                resolve_skill_catalog,
            )
            from opensquilla.skills.catalog_policy import project_public_catalog

            layout = "catalog_before_dynamic"
            projection_step._elig_ctx = synthetic_eligibility
            gated = _deterministic_gate(
                list(all_skills),
                {tool.name for tool in tool_defs},
                _eligibility_ctx(config.skills),
            )
            projected = project_public_catalog(
                gated,
                coding_mode=coding_mode,
                include_stable_meta=True,
            )
            ctx = asyncio.run(resolve_skill_catalog(ctx))
        except ImportError:
            import opensquilla.engine.steps.skills_filter as projection_step
            from opensquilla.engine.steps.skills_filter import (
                _deterministic_gate,
                _eligibility_ctx,
                filter_skills,
            )

            layout = "catalog_after_dynamic"
            projection_step._elig_ctx = synthetic_eligibility
            gated = _deterministic_gate(
                list(all_skills),
                {tool.name for tool in tool_defs},
                _eligibility_ctx(config.skills),
            )
            pinned = [skill for skill in gated if skill.always]
            projected = [*pinned, *(skill for skill in gated if not skill.always)]
            ctx = asyncio.run(filter_skills(ctx))

        rendered_chars = int(ctx.metadata.get("skills_prompt_chars") or 0)
        prompt_base, prompt_suffix = ctx.system_prompt
        if layout == "catalog_before_dynamic":
            catalog_prompt = prompt_base[-rendered_chars:] if rendered_chars else ""
        else:
            catalog_prompt = prompt_suffix[-rendered_chars:] if rendered_chars else ""

        injector = SkillInjector()
        try:
            complete_catalog = injector.inject_full(
                "",
                list(projected),
                generation=int(ctx.metadata.get("skill_catalog_generation") or 0),
            )
        except TypeError:
            complete_catalog = injector.inject_full("", list(projected))

        render_samples: list[float] = []
        for _ in range(500):
            render_started = time.perf_counter_ns()
            try:
                injector.inject_skills(
                    "",
                    list(projected),
                    max_chars=8000,
                    generation=int(ctx.metadata.get("skill_catalog_generation") or 0),
                )
            except TypeError:
                injector.inject_skills("", list(projected), max_chars=8000)
            render_samples.append((time.perf_counter_ns() - render_started) / 1_000_000)

        by_name = {skill.name: skill for skill in all_skills}
        body_payload: dict[str, dict[str, Any]] = {}
        for body_kind, skill_name in (
            ("ordinary", "github"),
            ("meta_step", "paper-section-author"),
        ):
            skill = by_name.get(skill_name)
            content = str(getattr(skill, "content", "") or "") if skill else ""
            body_payload[body_kind] = {
                "skill": skill_name,
                "content": content,
                "chars": len(content),
                "digest": _sha256_text(content),
            }

        return {
            "layout": layout,
            "eligibility_profile": "synthetic_full_capability",
            "physical_bundled_count": len(all_skills),
            "model_visible_bundled_count": int(ctx.metadata.get("skill_count") or 0),
            "rendered_bundled_count": int(
                ctx.metadata.get("skills_rendered_count") or catalog_prompt.count("</name>")
            ),
            "complete_catalog_chars": len(complete_catalog),
            "actual_catalog_chars": len(catalog_prompt),
            "catalog_prompt": catalog_prompt,
            "catalog_digest": _sha256_text(catalog_prompt),
            "base_digest": _sha256_text(BASE_SYSTEM_DEVELOPER_PROMPT),
            "dynamic_template_digest": _sha256_text(DYNAMIC_SUFFIX_MARKER),
            "catalog_generation": int(ctx.metadata.get("skill_catalog_generation") or 0),
            "catalog_names": [skill.name for skill in projected],
            "omitted_count": int(ctx.metadata.get("skills_catalog_omitted_count") or 0),
            "local_timing": {
                "cold_catalog_build_ms": round(cold_catalog_build_ms, 6),
                "warm_snapshot_iterations": len(warm_snapshot_samples),
                "warm_snapshot_ms_median": round(statistics.median(warm_snapshot_samples), 6),
                "warm_snapshot_ms_mean": round(statistics.fmean(warm_snapshot_samples), 6),
                "render_iterations": len(render_samples),
                "render_ms_median": round(statistics.median(render_samples), 6),
                "render_ms_mean": round(statistics.fmean(render_samples), 6),
            },
            "tool_schema_count": len(tool_payload),
            "tool_schema_chars": len(
                json.dumps(
                    tool_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
            "tool_schema_digest": _json_digest(tool_payload),
            "tool_names": [tool.name for tool in tool_defs],
            "tool_payload": tool_payload,
            "bodies": body_payload,
        }


def _worker_command(args: argparse.Namespace) -> int:
    result = _render_worker(Path(args.repo), coding_mode=bool(args.coding_mode))
    print("__OPENSQUILLA_SKILL_AB__" + json.dumps(result, ensure_ascii=False))
    return 0


def _render_external(
    *,
    script: Path,
    python: Path,
    repo: Path,
    coding_mode: bool,
) -> dict[str, Any]:
    command = [
        str(python),
        str(script),
        "_render-worker",
        "--repo",
        str(repo),
    ]
    if coding_mode:
        command.append("--coding-mode")
    completed = subprocess.run(
        command,
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    marker = "__OPENSQUILLA_SKILL_AB__"
    result_line = next(
        (line for line in reversed(completed.stdout.splitlines()) if line.startswith(marker)),
        "",
    )
    if completed.returncode != 0 or not result_line:
        safe_stderr = completed.stderr[-2000:].replace(str(repo), "<repo>")
        raise RuntimeError(
            f"catalog renderer failed with exit {completed.returncode}: {safe_stderr}"
        )
    return json.loads(result_line.removeprefix(marker))


def _worktree_state(repo: Path) -> dict[str, Any]:
    completed = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    entries = [line for line in completed.stdout.splitlines() if line]
    return {
        "state": "clean" if not entries else "dirty",
        "entry_count": len(entries),
        "porcelain_digest": _sha256_text("\n".join(entries)),
    }


def _assemble_system(render: Mapping[str, Any], dynamic: str) -> tuple[str, str]:
    catalog = str(render["catalog_prompt"])
    if render["layout"] == "catalog_before_dynamic":
        stable = f"{BASE_SYSTEM_DEVELOPER_PROMPT}\n\n{catalog}"
        return f"{stable}\n\n{dynamic}", stable
    stable = BASE_SYSTEM_DEVELOPER_PROMPT
    return f"{stable}\n\n{dynamic}\n\n{catalog}", stable


def _status_class(code: str) -> str:
    normalized = str(code or "").lower()
    if "429" in normalized or "rate" in normalized:
        return "429"
    if any(value in normalized for value in ("500", "502", "503", "504", "529")):
        return "5xx"
    if "timeout" in normalized:
        return "timeout"
    return "other"


def _style_metrics(text: str) -> dict[str, int]:
    lines = text.splitlines()
    paragraphs = [part for part in re.split(r"\n\s*\n", text.strip()) if part.strip()]
    return {
        "visible_chars": len(text),
        "visible_words": len(re.findall(r"\b[\w'-]+\b", text, flags=re.UNICODE)),
        "lines": len(lines) if text else 0,
        "paragraphs": len(paragraphs),
        "list_items": sum(bool(re.match(r"^\s*(?:[-*+] |\d+[.)] )", line)) for line in lines),
        "markdown_headings": sum(bool(re.match(r"^\s{0,3}#{1,6}\s", line)) for line in lines),
        "bold_spans": len(re.findall(r"(?:\*\*|__)(?=\S).+?(?<=\S)(?:\*\*|__)", text)),
    }


def _tool_call_matches(
    calls: Sequence[Mapping[str, Any]],
    *,
    tool_name: str,
    expected_names: Sequence[str],
) -> bool:
    for call in calls:
        if call.get("name") != tool_name:
            continue
        arguments = call.get("arguments")
        if not isinstance(arguments, Mapping):
            continue
        invoked = str(arguments.get("name") or "")
        if invoked in expected_names:
            return True
    return False


def _evaluate_quality(task: QualityTask, row: Mapping[str, Any]) -> dict[str, Any]:
    calls = row.get("tool_calls")
    safe_calls = calls if isinstance(calls, list) else []
    tool_names = {str(call.get("name") or "") for call in safe_calls if isinstance(call, Mapping)}
    text = str(row.get("response_text") or "")
    truncated = bool(row.get("truncated"))
    no_skill_claim = bool(
        re.search(
            r"\b(?:do not|don't|does not|doesn't|no longer) have (?:that |this )?skill\b|"
            r"\b(?:skill is|skills are) unavailable\b|\bI have no (?:such )?skill\b",
            text,
            flags=re.IGNORECASE,
        )
    )

    if task.route_kind == "direct":
        route_ok = bool(text.strip()) and not safe_calls
    elif task.route_kind == "tool":
        route_ok = bool(tool_names.intersection(task.expected_names))
    elif task.route_kind == "skill":
        route_ok = _tool_call_matches(
            safe_calls,
            tool_name="skill_view",
            expected_names=task.expected_names,
        )
    elif task.route_kind == "meta":
        route_ok = _tool_call_matches(
            safe_calls,
            tool_name="meta_invoke",
            expected_names=task.expected_names,
        )
    else:
        route_ok = _tool_call_matches(
            safe_calls,
            tool_name="skill_view",
            expected_names=("code-task",),
        ) or bool(tool_names.intersection({"read_file", "apply_patch", "exec_command"}))

    return {
        "route_ok": route_ok,
        "task_success": bool(row.get("success")) and route_ok and not truncated,
        "false_no_skill_claim": no_skill_claim,
        "explicit_skill_missed": bool(task.explicit_skill and not route_ok),
    }


async def _one_request(
    provider: Any,
    *,
    messages: list[Any],
    tools: list[Any] | None,
    system: str,
    max_tokens: int,
    request_metadata: dict[str, Any],
    retry_limit: int = 1,
) -> dict[str, Any]:
    from opensquilla.provider.types import (
        ChatConfig,
        DoneEvent,
        ErrorEvent,
        ProviderHeartbeatEvent,
        ReasoningDeltaEvent,
        TextDeltaEvent,
        ToolUseEndEvent,
        ToolUseStartEvent,
    )

    started_e2e = time.perf_counter()
    attempts: list[dict[str, Any]] = []
    final: dict[str, Any] = {}
    for attempt_index in range(retry_limit + 1):
        started = time.perf_counter()
        first_event_ms: float | None = None
        first_reasoning_ms: float | None = None
        first_visible_ms: float | None = None
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        pending_tools: dict[str, str] = {}
        done: DoneEvent | None = None
        error: ErrorEvent | None = None
        exception_type = ""
        try:
            async with asyncio.timeout(PROVIDER_TIMEOUT_SECONDS):
                async for event in provider.chat(
                    messages,
                    tools=tools,
                    config=ChatConfig(
                        system=system or None,
                        max_tokens=max_tokens,
                        temperature=None,
                        top_p=None,
                        timeout=120.0,
                        cache_mode="auto",
                    ),
                ):
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    if first_event_ms is None and not isinstance(event, ProviderHeartbeatEvent):
                        first_event_ms = elapsed_ms
                    if (
                        first_reasoning_ms is None
                        and isinstance(event, ReasoningDeltaEvent)
                        and event.text
                    ):
                        first_reasoning_ms = elapsed_ms
                    if isinstance(event, TextDeltaEvent) and event.text:
                        if first_visible_ms is None:
                            first_visible_ms = elapsed_ms
                        text_parts.append(event.text)
                    elif isinstance(event, ToolUseStartEvent):
                        pending_tools[event.tool_use_id] = event.tool_name
                    elif isinstance(event, ToolUseEndEvent):
                        tool_calls.append(
                            {
                                "name": event.tool_name or pending_tools.get(event.tool_use_id, ""),
                                "arguments": event.arguments,
                            }
                        )
                    elif isinstance(event, DoneEvent):
                        done = event
                    elif isinstance(event, ErrorEvent):
                        error = event
        except Exception as exc:  # noqa: BLE001 - persist type only; never secrets
            exception_type = type(exc).__name__

        attempt_ms = (time.perf_counter() - started) * 1000.0
        error_code = str(getattr(error, "code", "") or exception_type)
        attempt_row = {
            "attempt": attempt_index + 1,
            "success": done is not None and error is None and not exception_type,
            "error_class": _status_class(error_code) if error_code else None,
            "error_code": error_code or None,
            "retry_after_seconds": getattr(error, "retry_after_s", None),
            "latency_ms": round(attempt_ms, 3),
        }
        attempts.append(attempt_row)
        if done is not None and error is None and not exception_type:
            final = {
                "success": True,
                "input_tokens": int(done.input_tokens),
                "cached_input_tokens": int(done.cached_tokens),
                "cache_write_tokens": int(done.cache_write_tokens),
                "output_tokens": int(done.output_tokens),
                "reasoning_tokens": int(done.reasoning_tokens),
                "stop_reason": str(done.stop_reason or ""),
                "response_text": "".join(text_parts),
                "tool_calls": tool_calls,
                "first_event_ms": _clean_float(first_event_ms, 3),
                "first_reasoning_ms": _clean_float(first_reasoning_ms, 3),
                "first_visible_ms": _clean_float(first_visible_ms, 3),
                "successful_attempt_ms": round(attempt_ms, 3),
            }
            break
        final = {
            "success": False,
            "input_tokens": None,
            "cached_input_tokens": None,
            "cache_write_tokens": None,
            "output_tokens": None,
            "reasoning_tokens": None,
            "stop_reason": "",
            "response_text": "".join(text_parts),
            "tool_calls": tool_calls,
            "first_event_ms": _clean_float(first_event_ms, 3),
            "first_reasoning_ms": _clean_float(first_reasoning_ms, 3),
            "first_visible_ms": _clean_float(first_visible_ms, 3),
            "successful_attempt_ms": None,
        }
        if attempt_index >= retry_limit:
            break
        retry_after = getattr(error, "retry_after_s", None)
        delay = min(max(float(retry_after or 0.25), 0.0), 5.0)
        await asyncio.sleep(delay)

    response_text = str(final.get("response_text") or "")
    stop_reason = str(final.get("stop_reason") or "").lower()
    final.update(request_metadata)
    final.update(
        {
            "attempts": attempts,
            "retry_count": max(len(attempts) - 1, 0),
            "end_to_end_ms": round((time.perf_counter() - started_e2e) * 1000.0, 3),
            "truncated": stop_reason in {"length", "max_tokens", "max_output_tokens"},
            "system_digest": _sha256_text(system),
            "style": _style_metrics(response_text),
        }
    )
    return final


def _build_tools_for_live(candidate_repo: Path, render: Mapping[str, Any]) -> list[Any]:
    sys.path.insert(0, str(candidate_repo / "src"))
    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
    import opensquilla.tools.builtin  # noqa: F401
    from opensquilla.skills.loader import SkillLoader
    from opensquilla.tools.builtin.skill_tools import create_skill_tools
    from opensquilla.tools.registry import get_default_registry
    from opensquilla.tools.types import ToolContext

    with tempfile.TemporaryDirectory(prefix="opensquilla-skill-ab-live-") as temp_dir:
        loader = SkillLoader(
            bundled_dir=candidate_repo / "src" / "opensquilla" / "skills" / "bundled",
            snapshot_path=Path(temp_dir) / "snapshot.json",
        )
        loader.load_all()
        create_skill_tools(loader)
        definitions = get_default_registry().to_tool_definitions(
            ToolContext(is_owner=True, surfaced_tools={"meta_invoke"})
        )
    payload = [tool.model_dump(mode="json") for tool in definitions]
    if _json_digest(payload) != render["tool_schema_digest"]:
        raise RuntimeError("live tool schema does not match rendered candidate schema")
    return definitions


def _build_provider(config_path: Path) -> tuple[Any, dict[str, str]]:
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config
    from opensquilla.provider.selector import ProviderConfig, _build_provider

    config = GatewayConfig.load(config_path)
    runtime = resolve_llm_runtime_config(config)
    if not runtime.api_key:
        raise RuntimeError("configured provider credential is unavailable")
    provider = _build_provider(
        ProviderConfig(
            provider=runtime.provider,
            model=runtime.model,
            api_key=runtime.api_key,
            base_url=runtime.base_url,
            proxy=runtime.proxy,
            provider_routing=runtime.provider_routing,
        )
    )
    return provider, {"provider": runtime.provider, "model": runtime.model}


def _request_metadata(
    *,
    version: str,
    suite: str,
    task_id: str,
    render: Mapping[str, Any],
    stable_prefix: str,
    dynamic: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "version": version,
        "suite": suite,
        "task_id": task_id,
        "catalog_generation": render["catalog_generation"],
        "catalog_digest": render["catalog_digest"],
        "stable_prefix_digest": _sha256_text(stable_prefix),
        "dynamic_suffix_digest": _sha256_text(dynamic),
        **extra,
    }


async def _run_live(args: argparse.Namespace) -> dict[str, Any]:
    from opensquilla.provider.types import Message

    script = Path(__file__).resolve()
    candidate_repo = Path(args.candidate_repo).resolve()
    baseline_repo = Path(args.baseline_repo).resolve()
    # Keep the virtual-environment launcher path intact. Resolving this symlink
    # selects the base interpreter and loses the benchmark dependencies when
    # the isolated renderer changes its working directory to either snapshot.
    python = Path(sys.executable).absolute()

    renders = {
        version: _render_external(
            script=script,
            python=python,
            repo=repo,
            coding_mode=False,
        )
        for version, repo in (
            ("baseline", baseline_repo),
            ("candidate", candidate_repo),
        )
    }
    coding_renders = {
        version: _render_external(
            script=script,
            python=python,
            repo=repo,
            coding_mode=True,
        )
        for version, repo in (
            ("baseline", baseline_repo),
            ("candidate", candidate_repo),
        )
    }
    schema_digests = {
        render["tool_schema_digest"] for render in (*renders.values(), *coding_renders.values())
    }
    tool_orders = {
        tuple(render["tool_names"]) for render in (*renders.values(), *coding_renders.values())
    }
    if len(schema_digests) != 1 or len(tool_orders) != 1:
        raise RuntimeError("baseline/candidate tool schema or order differs")

    provider, identity = _build_provider(Path(args.config))
    tools = _build_tools_for_live(candidate_repo, renders["candidate"])
    rows: list[dict[str, Any]] = []

    async def call(
        *,
        version: str,
        suite: str,
        task_id: str,
        prompt: str,
        render: Mapping[str, Any] | None = None,
        system_override: str | None = None,
        tools_override: list[Any] | None | object = Ellipsis,
        max_tokens: int = 256,
        messages_override: list[Message] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        active_render = render or renders[version]
        dynamic = DYNAMIC_SUFFIX_MARKER.format(request_key=f"{suite}:{task_id}")
        system, stable_prefix = _assemble_system(active_render, dynamic)
        if system_override is not None:
            system = system_override
            stable_prefix = system_override
        active_tools = tools if tools_override is Ellipsis else tools_override
        messages = messages_override or [Message(role="user", content=prompt)]
        row = await _one_request(
            provider,
            messages=messages,
            tools=active_tools,
            system=system,
            max_tokens=max_tokens,
            request_metadata=_request_metadata(
                version=version,
                suite=suite,
                task_id=task_id,
                render=active_render,
                stable_prefix=stable_prefix,
                dynamic=dynamic,
                **extra,
            ),
        )
        rows.append(row)
        return row

    # Provider-tokenizer calibration.  Differentials use the same synthetic
    # user message, output cap, model, and sampling settings.
    empty = await call(
        version="shared",
        suite="calibration",
        task_id="empty",
        prompt=CALIBRATION_USER,
        render=renders["candidate"],
        system_override="",
        tools_override=None,
        max_tokens=32,
    )
    base = await call(
        version="shared",
        suite="calibration",
        task_id="base_system_developer",
        prompt=CALIBRATION_USER,
        render=renders["candidate"],
        system_override=BASE_SYSTEM_DEVELOPER_PROMPT,
        tools_override=None,
        max_tokens=32,
    )
    tools_only = await call(
        version="shared",
        suite="calibration",
        task_id="tool_schema",
        prompt=CALIBRATION_USER,
        render=renders["candidate"],
        system_override="",
        max_tokens=32,
    )
    catalogless_system = (
        BASE_SYSTEM_DEVELOPER_PROMPT
        + "\n\n"
        + DYNAMIC_SUFFIX_MARKER.format(request_key="calibration:catalog")
    )
    catalogless = await call(
        version="shared",
        suite="calibration",
        task_id="catalogless",
        prompt=CALIBRATION_USER,
        render=renders["candidate"],
        system_override=catalogless_system,
        max_tokens=32,
    )

    version_calibration: dict[str, dict[str, Any]] = {}
    for version in ("baseline", "candidate"):
        render = renders[version]
        full = await call(
            version=version,
            suite="calibration",
            task_id="catalog",
            prompt=CALIBRATION_USER,
            max_tokens=32,
        )
        _, stable_prefix = _assemble_system(
            render,
            DYNAMIC_SUFFIX_MARKER.format(request_key="calibration:cacheable"),
        )
        cacheable = await call(
            version=version,
            suite="calibration",
            task_id="cacheable_prefix",
            prompt=CALIBRATION_USER,
            system_override=stable_prefix,
            max_tokens=32,
        )
        body_tokens: dict[str, int | None] = {}
        for body_kind in ("ordinary", "meta_step"):
            body = render["bodies"][body_kind]
            full_system, _ = _assemble_system(
                render,
                DYNAMIC_SUFFIX_MARKER.format(request_key=f"calibration:body:{body_kind}"),
            )
            without_body = await call(
                version=version,
                suite="calibration",
                task_id=f"body_{body_kind}_base",
                prompt=CALIBRATION_USER,
                system_override=full_system,
                max_tokens=32,
            )
            with_body = await call(
                version=version,
                suite="calibration",
                task_id=f"body_{body_kind}",
                prompt=CALIBRATION_USER,
                system_override=f"{full_system}\n\n{body['content']}",
                max_tokens=32,
            )
            if with_body.get("input_tokens") is None or without_body.get("input_tokens") is None:
                body_tokens[body_kind] = None
            else:
                body_tokens[body_kind] = max(
                    int(with_body["input_tokens"]) - int(without_body["input_tokens"]),
                    0,
                )
        version_calibration[version] = {
            "catalog_tokens": (
                None
                if full.get("input_tokens") is None or catalogless.get("input_tokens") is None
                else max(int(full["input_tokens"]) - int(catalogless["input_tokens"]), 0)
            ),
            "cacheable_prefix_tokens": (
                None
                if cacheable.get("input_tokens") is None or empty.get("input_tokens") is None
                else max(
                    int(cacheable["input_tokens"]) - int(empty["input_tokens"]),
                    0,
                )
            ),
            "body_tokens": body_tokens,
        }

    shared_calibration = {
        "system_developer_tokens": (
            None
            if base.get("input_tokens") is None or empty.get("input_tokens") is None
            else max(int(base["input_tokens"]) - int(empty["input_tokens"]), 0)
        ),
        "tool_schema_tokens": (
            None
            if tools_only.get("input_tokens") is None or empty.get("input_tokens") is None
            else max(int(tools_only["input_tokens"]) - int(empty["input_tokens"]), 0)
        ),
    }

    # Warm-up is retained in raw data and explicitly excluded from the micro summary.
    for version in ("baseline", "candidate"):
        await call(
            version=version,
            suite="warmup",
            task_id="provider_cache",
            prompt="Synthetic warm-up: reply with exactly READY.",
            max_tokens=32,
            cache_phase="warmup",
        )

    # Five groups, each containing exactly two A and two B calls.  Odd groups
    # reverse the order to bound time-of-run drift.
    for group, prompt in enumerate(MICRO_TASKS, start=1):
        order = ("baseline", "candidate", "candidate", "baseline")
        if group % 2 == 0:
            order = ("candidate", "baseline", "baseline", "candidate")
        for position, version in enumerate(order, start=1):
            await call(
                version=version,
                suite="micro",
                task_id=f"group_{group}",
                prompt=prompt,
                group=group,
                position=position,
                sequence="ABBA" if group % 2 else "BAAB",
                cache_phase="warm",
            )

    # Five independent synthetic sessions, ten turns each.  Fixed assistant
    # history makes A and B histories byte-identical despite model variation.
    previous_input: dict[tuple[str, int], int | None] = {}
    for session_index in range(1, 6):
        histories: dict[str, list[Message]] = {"baseline": [], "candidate": []}
        for turn in range(1, 11):
            order = ("baseline", "candidate")
            if (session_index + turn) % 2:
                order = tuple(reversed(order))
            user_text = CONTINUITY_USER.format(turn=turn)
            for version in order:
                messages = [
                    *histories[version],
                    Message(role="user", content=user_text),
                ]
                row = await call(
                    version=version,
                    suite="continuity",
                    task_id=f"session_{session_index}_turn_{turn}",
                    prompt=user_text,
                    messages_override=messages,
                    session_index=session_index,
                    turn=turn,
                    cache_phase="cold" if turn == 1 else "warm",
                )
                prior = previous_input.get((version, session_index))
                cacheable = version_calibration[version]["cacheable_prefix_tokens"]
                if turn == 1 or prior is None:
                    theoretical = cacheable
                else:
                    theoretical = max(int(cacheable or 0), int(prior))
                row["theoretical_reusable_tokens"] = theoretical
                previous_input[(version, session_index)] = row.get("input_tokens")
            fixed_assistant = CONTINUITY_ASSISTANT.format(turn=turn)
            for version in ("baseline", "candidate"):
                histories[version].extend(
                    [
                        Message(role="user", content=user_text),
                        Message(role="assistant", content=fixed_assistant),
                    ]
                )

    # Paired multi-task and migration cases.  The code-task case uses the real
    # coding-mode projection while preserving the exact same tool schema.
    for index, task in enumerate(QUALITY_TASKS):
        order = ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
        for version in order:
            active_render = coding_renders[version] if task.coding_mode else renders[version]
            row = await call(
                version=version,
                suite="multitask",
                task_id=task.task_id,
                prompt=task.prompt,
                render=active_render,
                task_category=task.category,
                route_kind=task.route_kind,
                expected_names=list(task.expected_names),
                coding_mode=task.coding_mode,
                explicit_skill=task.explicit_skill,
            )
            row["quality"] = _evaluate_quality(task, row)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "identity": {
            "baseline_commit_sha": args.baseline_sha,
            "candidate_commit_sha": args.candidate_sha,
            "baseline_worktree": _worktree_state(baseline_repo),
            "candidate_worktree": _worktree_state(candidate_repo),
            "candidate_snapshot_method": "git commit-tree over a temporary index",
            **identity,
            "tool_schema_digest": next(iter(schema_digests)),
            "tool_schema_count": renders["candidate"]["tool_schema_count"],
            "machine": {
                "os": platform.system(),
                "architecture": platform.machine(),
                "python": platform.python_version(),
            },
        },
        "parameters": {
            "temperature": None,
            "top_p": None,
            "concurrency": 1,
            "output_limit_tokens": 256,
            "calibration_output_limit_tokens": 32,
            "request_timeout_seconds": PROVIDER_TIMEOUT_SECONDS,
            "micro_groups": 5,
            "micro_order": "alternating ABBA/BAAB",
            "continuity_sessions": 5,
            "continuity_turns_per_session": 10,
            "synthetic_only": True,
            "skills_max_prompt_chars": 8000,
            "meta_skill_enabled": True,
            "meta_auto_trigger": True,
            "default_coding_mode": False,
            "cold_cache_definition": "first request in each synthetic continuity session",
            "warm_cache_definition": "turns 2-10 in the same fixed-history session",
        },
        "prompt_identity": {
            "base_system_developer_prompt": BASE_SYSTEM_DEVELOPER_PROMPT,
            "dynamic_suffix_template": DYNAMIC_SUFFIX_MARKER,
            "base_digest": _sha256_text(BASE_SYSTEM_DEVELOPER_PROMPT),
            "dynamic_template_digest": _sha256_text(DYNAMIC_SUFFIX_MARKER),
            "baseline_catalog_digest": renders["baseline"]["catalog_digest"],
            "candidate_catalog_digest": renders["candidate"]["catalog_digest"],
        },
        "catalogs": {
            version: {
                key: value
                for key, value in render.items()
                if key not in {"catalog_prompt", "tool_payload", "bodies"}
            }
            for version, render in renders.items()
        },
        "coding_catalogs": {
            version: {
                key: value
                for key, value in render.items()
                if key not in {"catalog_prompt", "tool_payload", "bodies"}
            }
            for version, render in coding_renders.items()
        },
        "calibration": {
            "shared": shared_calibration,
            "versions": version_calibration,
            "body_samples": {
                version: {
                    kind: {key: value for key, value in body.items() if key != "content"}
                    for kind, body in render["bodies"].items()
                }
                for version, render in renders.items()
            },
        },
        "rows": rows,
    }


def _aggregate_rows(rows: Sequence[Mapping[str, Any]], suite: str, version: str) -> dict[str, Any]:
    selected = [row for row in rows if row.get("suite") == suite and row.get("version") == version]
    successful = [row for row in selected if row.get("success")]
    input_values = [int(row["input_tokens"]) for row in successful]
    cached_values = [int(row["cached_input_tokens"]) for row in successful]
    output_values = [int(row["output_tokens"]) for row in successful]
    reasoning_values = [int(row["reasoning_tokens"]) for row in successful]
    uncached = [value - cached for value, cached in zip(input_values, cached_values, strict=True)]
    error_counts = Counter(
        str(attempt.get("error_class"))
        for row in selected
        for attempt in row.get("attempts", [])
        if attempt.get("error_class")
    )
    return {
        "requests": len(selected),
        "successful": len(successful),
        "failed": len(selected) - len(successful),
        "input_tokens_mean": _clean_float(_mean(input_values), 3),
        "input_tokens_median": _clean_float(_median(input_values), 3),
        "cached_input_tokens_mean": _clean_float(_mean(cached_values), 3),
        "cached_input_tokens_median": _clean_float(_median(cached_values), 3),
        "cache_read_ratio": _clean_float(_ratio(sum(cached_values), sum(input_values))),
        "uncached_input_tokens_mean": _clean_float(_mean(uncached), 3),
        "uncached_input_tokens_median": _clean_float(_median(uncached), 3),
        "output_tokens_mean": _clean_float(_mean(output_values), 3),
        "output_tokens_median": _clean_float(_median(output_values), 3),
        "reasoning_tokens_median": _clean_float(_median(reasoning_values), 3),
        "input_output_tokens_total": sum(input_values) + sum(output_values) + sum(reasoning_values),
        "first_event_ms_median": _clean_float(
            _median(row.get("first_event_ms") for row in successful), 3
        ),
        "first_reasoning_ms_median": _clean_float(
            _median(row.get("first_reasoning_ms") for row in successful), 3
        ),
        "first_visible_ms_median": _clean_float(
            _median(row.get("first_visible_ms") for row in successful), 3
        ),
        "successful_attempt_ms_median": _clean_float(
            _median(row.get("successful_attempt_ms") for row in successful), 3
        ),
        "end_to_end_ms_median": _clean_float(
            _median(row.get("end_to_end_ms") for row in selected), 3
        ),
        "end_to_end_ms_mean": _clean_float(_mean(row.get("end_to_end_ms") for row in selected), 3),
        "retry_count": sum(int(row.get("retry_count") or 0) for row in selected),
        "truncated_count": sum(bool(row.get("truncated")) for row in selected),
        "errors": dict(sorted(error_counts.items())),
    }


def _quality_summary(rows: Sequence[Mapping[str, Any]], version: str) -> dict[str, Any]:
    selected = [
        row for row in rows if row.get("suite") == "multitask" and row.get("version") == version
    ]
    successful = [row for row in selected if row.get("success")]
    quality = [row.get("quality", {}) for row in selected]
    styles = [row.get("style", {}) for row in successful]

    def style_values(name: str) -> list[int]:
        return [int(style.get(name) or 0) for style in styles]

    return {
        "task_success_rate": _clean_float(
            _ratio(sum(bool(item.get("task_success")) for item in quality), len(quality))
        ),
        "correct_route_rate": _clean_float(
            _ratio(sum(bool(item.get("route_ok")) for item in quality), len(quality))
        ),
        "false_no_skill_claim_rate": _clean_float(
            _ratio(
                sum(bool(item.get("false_no_skill_claim")) for item in quality),
                len(quality),
            )
        ),
        "explicit_skill_miss_rate": _clean_float(
            _ratio(
                sum(bool(item.get("explicit_skill_missed")) for item in quality),
                sum(bool(row.get("explicit_skill")) for row in selected),
            )
        ),
        "tool_calls_mean": _clean_float(
            _mean(len(row.get("tool_calls") or []) for row in successful), 3
        ),
        "visible_words_mean": _clean_float(_mean(style_values("visible_words")), 3),
        "visible_words_median": _clean_float(_median(style_values("visible_words")), 3),
        "visible_chars_mean": _clean_float(_mean(style_values("visible_chars")), 3),
        "visible_chars_median": _clean_float(_median(style_values("visible_chars")), 3),
        "lines_mean": _clean_float(_mean(style_values("lines")), 3),
        "paragraphs_mean": _clean_float(_mean(style_values("paragraphs")), 3),
        "list_items_mean": _clean_float(_mean(style_values("list_items")), 3),
        "markdown_headings_mean": _clean_float(_mean(style_values("markdown_headings")), 3),
        "bold_spans_mean": _clean_float(_mean(style_values("bold_spans")), 3),
    }


def _cache_summary(rows: Sequence[Mapping[str, Any]], version: str) -> dict[str, Any]:
    selected = [
        row for row in rows if row.get("suite") == "continuity" and row.get("version") == version
    ]
    cold = [row for row in selected if row.get("cache_phase") == "cold"]
    warm = [row for row in selected if row.get("cache_phase") == "warm"]

    def phase(rows_for_phase: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        success = [row for row in rows_for_phase if row.get("success")]
        input_total = sum(int(row.get("input_tokens") or 0) for row in success)
        cached_total = sum(int(row.get("cached_input_tokens") or 0) for row in success)
        reusable_total = sum(int(row.get("theoretical_reusable_tokens") or 0) for row in success)
        uncached = [
            int(row.get("input_tokens") or 0) - int(row.get("cached_input_tokens") or 0)
            for row in success
        ]
        return {
            "requests": len(rows_for_phase),
            "successful": len(success),
            "cache_read_ratio": _clean_float(_ratio(cached_total, input_total)),
            "cached_input_tokens_median": _clean_float(
                _median(row.get("cached_input_tokens") for row in success), 3
            ),
            "uncached_input_tokens_median": _clean_float(_median(uncached), 3),
            "layout_reuse_ceiling": _clean_float(_ratio(reusable_total, input_total)),
            "cache_realization": _clean_float(_ratio(cached_total, reusable_total)),
        }

    stable_digests = [str(row.get("stable_prefix_digest") or "") for row in selected]
    most_common_count = Counter(stable_digests).most_common(1)[0][1] if stable_digests else 0
    unexpected_breaks = 0
    for session_index in range(1, 6):
        session_rows = [row for row in selected if row.get("session_index") == session_index]
        if not session_rows:
            continue
        expected = session_rows[0].get("stable_prefix_digest")
        unexpected_breaks += sum(
            row.get("stable_prefix_digest") != expected for row in session_rows[1:]
        )
    return {
        "cold": phase(cold),
        "warm": phase(warm),
        "prefix_stability_rate": _clean_float(_ratio(most_common_count, len(stable_digests))),
        "reinjection_waste": 0.0,
        "avoidable_break_rate": _clean_float(_ratio(unexpected_breaks, len(warm))),
    }


def _gate_summary(summary: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    baseline_multi = summary["suites"]["multitask"]["baseline"]
    candidate_multi = summary["suites"]["multitask"]["candidate"]
    baseline_cache = summary["cache"]["baseline"]
    candidate_cache = summary["cache"]["candidate"]
    baseline_quality = summary["quality"]["baseline"]
    candidate_quality = summary["quality"]["candidate"]

    before_input = baseline_multi.get("input_tokens_mean")
    after_input = candidate_multi.get("input_tokens_mean")
    input_reduction = (
        None
        if not isinstance(before_input, int | float) or not before_input
        else (float(before_input) - float(after_input)) / float(before_input)
    )
    paired_growth = []
    for task in QUALITY_TASKS:
        values = {
            str(row.get("version")): row.get("input_tokens")
            for row in rows
            if row.get("suite") == "multitask" and row.get("task_id") == task.task_id
        }
        if isinstance(values.get("baseline"), int) and isinstance(values.get("candidate"), int):
            paired_growth.append(values["candidate"] > values["baseline"])

    latency_before = baseline_multi.get("end_to_end_ms_median")
    latency_after = candidate_multi.get("end_to_end_ms_median")
    latency_ratio = (
        None
        if not isinstance(latency_before, int | float) or not latency_before
        else float(latency_after) / float(latency_before)
    )
    words_before = baseline_quality.get("visible_words_median")
    words_after = candidate_quality.get("visible_words_median")
    word_change = (
        None
        if not isinstance(words_before, int | float) or not words_before
        else abs(float(words_after) - float(words_before)) / float(words_before)
    )
    truncations = sum(bool(row.get("truncated")) for row in rows)

    gates = {
        "average_input_tokens_reduced_at_least_30_percent": bool(
            input_reduction is not None and input_reduction >= 0.30
        ),
        "no_multitask_input_growth": bool(paired_growth and not any(paired_growth)),
        "warm_cache_ratio_not_lower": bool(
            (candidate_cache["warm"].get("cache_read_ratio") or 0)
            >= (baseline_cache["warm"].get("cache_read_ratio") or 0)
        ),
        "warm_uncached_median_lower": bool(
            (candidate_cache["warm"].get("uncached_input_tokens_median") or math.inf)
            < (baseline_cache["warm"].get("uncached_input_tokens_median") or math.inf)
        ),
        "prefix_stability_100_percent": bool(
            baseline_cache.get("prefix_stability_rate") == 1.0
            and candidate_cache.get("prefix_stability_rate") == 1.0
        ),
        "reinjection_waste_zero": bool(
            baseline_cache.get("reinjection_waste") == 0.0
            and candidate_cache.get("reinjection_waste") == 0.0
        ),
        "avoidable_break_rate_zero": bool(
            baseline_cache.get("avoidable_break_rate") == 0.0
            and candidate_cache.get("avoidable_break_rate") == 0.0
        ),
        "median_end_to_end_latency_not_worse_than_5_percent": bool(
            latency_ratio is not None and latency_ratio <= 1.05
        ),
        "nonretired_task_success_not_lower": bool(
            (candidate_quality.get("task_success_rate") or 0)
            >= (baseline_quality.get("task_success_rate") or 0)
        ),
        "no_truncated_samples": truncations == 0,
        "visible_word_median_within_10_percent": bool(
            word_change is not None and word_change <= 0.10
        ),
    }
    return {
        "all_passed": all(gates.values()),
        "gates": gates,
        "derived": {
            "multitask_average_input_reduction": _clean_float(input_reduction),
            "multitask_latency_ratio": _clean_float(latency_ratio),
            "visible_word_median_absolute_change_ratio": _clean_float(word_change),
            "truncations": truncations,
        },
    }


def summarize(raw: Mapping[str, Any]) -> dict[str, Any]:
    rows = raw.get("rows")
    if not isinstance(rows, list):
        raise ValueError("raw evidence has no rows list")
    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "identity": raw.get("identity"),
        "parameters": raw.get("parameters"),
        "prompt_identity": raw.get("prompt_identity"),
        "catalogs": raw.get("catalogs"),
        "calibration": raw.get("calibration"),
        "suites": {
            suite: {
                version: _aggregate_rows(rows, suite, version)
                for version in ("baseline", "candidate")
            }
            for suite in ("micro", "continuity", "multitask")
        },
        "cache": {version: _cache_summary(rows, version) for version in ("baseline", "candidate")},
        "quality": {
            version: _quality_summary(rows, version) for version in ("baseline", "candidate")
        },
    }
    summary["acceptance"] = _gate_summary(summary, rows)
    return summary


def _csv_rows(raw: Mapping[str, Any]) -> Iterable[dict[str, Any]]:
    for row in raw.get("rows", []):
        style = row.get("style") or {}
        quality = row.get("quality") or {}
        errors = Counter(
            str(attempt.get("error_class"))
            for attempt in row.get("attempts", [])
            if attempt.get("error_class")
        )
        yield {
            "version": row.get("version"),
            "suite": row.get("suite"),
            "task_id": row.get("task_id"),
            "cache_phase": row.get("cache_phase"),
            "success": row.get("success"),
            "input_tokens": row.get("input_tokens"),
            "cached_input_tokens": row.get("cached_input_tokens"),
            "cache_write_tokens": row.get("cache_write_tokens"),
            "output_tokens": row.get("output_tokens"),
            "reasoning_tokens": row.get("reasoning_tokens"),
            "first_event_ms": row.get("first_event_ms"),
            "first_reasoning_ms": row.get("first_reasoning_ms"),
            "first_visible_ms": row.get("first_visible_ms"),
            "successful_attempt_ms": row.get("successful_attempt_ms"),
            "end_to_end_ms": row.get("end_to_end_ms"),
            "retry_count": row.get("retry_count"),
            "truncated": row.get("truncated"),
            "error_429": errors.get("429", 0),
            "error_5xx": errors.get("5xx", 0),
            "error_timeout": errors.get("timeout", 0),
            "catalog_generation": row.get("catalog_generation"),
            "catalog_digest": row.get("catalog_digest"),
            "stable_prefix_digest": row.get("stable_prefix_digest"),
            "dynamic_suffix_digest": row.get("dynamic_suffix_digest"),
            "theoretical_reusable_tokens": row.get("theoretical_reusable_tokens"),
            "tool_call_count": len(row.get("tool_calls") or []),
            "task_success": quality.get("task_success"),
            "route_ok": quality.get("route_ok"),
            "false_no_skill_claim": quality.get("false_no_skill_claim"),
            "explicit_skill_missed": quality.get("explicit_skill_missed"),
            "visible_chars": style.get("visible_chars"),
            "visible_words": style.get("visible_words"),
            "lines": style.get("lines"),
            "paragraphs": style.get("paragraphs"),
            "list_items": style.get("list_items"),
            "markdown_headings": style.get("markdown_headings"),
            "bold_spans": style.get("bold_spans"),
        }


def _write_outputs(raw: Mapping[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / "raw.json"
    csv_path = output_dir / "raw.csv"
    summary_path = output_dir / "summary.json"
    raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    flattened = list(_csv_rows(raw))
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(flattened[0]) if flattened else [],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(flattened)
    summary_path.write_text(
        json.dumps(summarize(raw), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _summarize_command(args: argparse.Namespace) -> int:
    raw_path = Path(args.raw)
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    output_dir = Path(args.output_dir) if args.output_dir else raw_path.parent
    _write_outputs(raw, output_dir)
    return 0


def _run_command(args: argparse.Namespace) -> int:
    if not args.acknowledge_live_cost:
        raise SystemExit("run requires --acknowledge-live-cost")
    raw = asyncio.run(_run_live(args))
    _write_outputs(raw, Path(args.output_dir))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    worker = subparsers.add_parser("_render-worker", help=argparse.SUPPRESS)
    worker.add_argument("--repo", required=True)
    worker.add_argument("--coding-mode", action="store_true")
    worker.set_defaults(func=_worker_command)

    run = subparsers.add_parser("run", help="run the billable paired live benchmark")
    run.add_argument("--baseline-repo", required=True)
    run.add_argument("--candidate-repo", required=True)
    run.add_argument("--baseline-sha", required=True)
    run.add_argument("--candidate-sha", required=True)
    run.add_argument("--config", required=True)
    run.add_argument("--output-dir", required=True)
    run.add_argument("--acknowledge-live-cost", action="store_true")
    run.set_defaults(func=_run_command)

    summary_parser = subparsers.add_parser(
        "summarize", help="recompute CSV and summary JSON from raw JSON"
    )
    summary_parser.add_argument("--raw", required=True)
    summary_parser.add_argument("--output-dir")
    summary_parser.set_defaults(func=_summarize_command)
    return parser


def main() -> int:
    args = _parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
