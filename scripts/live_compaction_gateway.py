"""Real Gateway adapter for the existing opt-in compaction acceptance harness.

The browser creates sessions normally. This module only configures an isolated
Gateway and observes HTTP/SQLite; it never seeds transcripts or replaces model
responses except explicitly requested summary-only synthetic faults.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from opensquilla.engine.cache_break_monitor import add_compaction_listener
from opensquilla.gateway.boot import start_gateway_server
from opensquilla.gateway.config import AuthConfig, GatewayConfig
from opensquilla.gateway.input_normalization import LARGE_PASTE_CHARS
from opensquilla.tools.registry import ToolRegistry, get_default_registry
from scripts.live_harness_security import (
    _secret_needles,
    _temporary_tree_contains_secret,
    write_safe_report,
)
from scripts.live_reasoning_replay_e2e import (
    CompactionCaseOptions,
    WireObserver,
    _config,
    _is_compaction_wire_call,
    _require,
    _usage_report,
    _wire_diagnostics,
    compaction_answer_fact_checks,
    compaction_fact_coverage,
    compaction_task_facts,
    compaction_task_instructions,
    deployment_window_evidence,
)


def public_execution_overlay(value: Any) -> dict[str, Any]:
    """Allow model selection and explicit generation caps, never credentials or W."""
    _require(isinstance(value, dict), "invalid_execution_config")
    _require(set(value) <= {"squilla_router", "llm_ensemble", "models"},
             "invalid_execution_config")
    models = value.get("models", {})
    _require(isinstance(models, dict), "invalid_execution_model_caps")
    for entries in models.values():
        _require(isinstance(entries, dict), "invalid_execution_model_caps")
        for fields in entries.values():
            _require(isinstance(fields, dict) and set(fields) == {"max_output_tokens"}
                     and type(fields["max_output_tokens"]) is int
                     and fields["max_output_tokens"] > 0, "invalid_execution_model_caps")

    def check(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                _require(not re.search(r"api.?key|secret|password|authorization|base.?url", key,
                                       re.IGNORECASE), "secret_or_endpoint_in_execution_config")
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif isinstance(item, str):
            _require(not re.search(r"\bsk[-_]", item), "secret_in_execution_config")

    check(value)
    return value


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _provider_env(provider: str) -> str:
    from opensquilla.provider.registry import get_provider_spec

    spec = get_provider_spec(provider)
    return spec.env_key if spec is not None else ""


def execution_metadata_evidence(value: Any) -> Any:
    """Project persisted execution evidence without candidate/input text or endpoints."""
    keys = {
        "model", "provider", "role", "label", "index", "ok", "request_started",
        "stop_reason", "error_code", "attempt_index", "request_count", "attempt_count",
        "input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens",
        "routed_model", "routed_tier", "routing_source", "routing_applied", "baseline_model",
        "model_usage_breakdown", "ensemble_trace", "execution_legs", "candidates",
        "execution", "final_request", "attempts", "mode", "profile", "selection_strategy",
        "successful_proposers", "total_candidates", "fallback_used", "fallback_code",
        "final_request_role", "min_successful_proposers", "effective_min_successful_proposers",
        "configured_min_successful_proposers", "target_successful_proposers",
        "proposer_max_retries", "llm_request_count", "selected_candidate_count",
        "candidate_bundle_budget_chars", "candidate_bundle_actual_chars",
        "candidate_bundle_budget_source", "effective_context_window_tokens",
        "effective_context_window_source", "effective_max_tokens", "max_tokens_override",
        "effective_provider_request_max_chars", "provider_request_max_chars_source",
        "deployment_ready", "effective_thinking", "effective_thinking_level",
    }
    if isinstance(value, dict):
        return {key: execution_metadata_evidence(child) for key, child in value.items()
                if key in keys}
    if isinstance(value, list):
        return [execution_metadata_evidence(child) for child in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:/+@-]{0,199}", value):
        return value if not value.startswith(("sk-", "sk_")) else None
    return None


def pressure_file_evidence(
    request: dict[str, Any], workspace: Path, media_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Verify complete synthetic read_file output, allowing the tool's line-number prefixes."""
    results = []
    for message in request.get("messages", []):
        if message.get("role") != "tool" or not isinstance(message.get("content"), str):
            continue
        content = message["content"]
        for fixture_id in sorted(set(re.findall(
            r"SYNTHETIC_PRESSURE_FILE_([0-9]{4})_BEGIN", content,
        ))):
            complete = False
            fixture_source = "media" if media_root is not None else "workspace"
            for root in (media_root,) if media_root is not None else (workspace,):
                if root is None:
                    continue
                source = root / "compaction-pressure" / f"{fixture_id}.txt"
                if source.is_file() and not source.is_symlink():
                    lines = source.read_text(encoding="utf-8").splitlines()
                    complete = bool(lines) and all(line in content for line in lines if line)
                    if complete:
                        break
            results.append({"fixture_id": fixture_id, "complete": complete,
                            "fixture_source": fixture_source})
    return results


class AcceptanceToolRegistry(ToolRegistry):
    """Keep the oracle outside every executable tool path in an isolated live case."""

    def __init__(self, roots: tuple[Path, ...], *, allow_read_files: bool) -> None:
        super().__init__()
        self.roots = tuple(path.resolve() for path in roots)
        self.allow_read_files = allow_read_files
        self.blocked_tool_attempts = 0

    def register(self, spec, handler) -> None:
        async def guarded(**arguments):
            allowed = self.allow_read_files and spec.name == "read_file"
            if allowed:
                candidate = Path(str(arguments.get("path", "")))
                if not candidate.is_absolute():
                    candidate = self.roots[0] / candidate
                candidate = candidate.resolve()
                allowed = any(candidate.is_relative_to(root) for root in self.roots)
            if not allowed:
                self.blocked_tool_attempts += 1
                raise RuntimeError("acceptance_tool_or_path_not_allowlisted")
            return await handler(**arguments)

        super().register(spec, guarded)


def storage_evidence(
    db: Path, facts: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Return safe record hashes plus in-memory summary text for wire matching."""
    if not db.is_file():
        return {"ready": False}, []
    with contextlib.closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        summary_rows = connection.execute(
            "SELECT id,session_key,summary_text,summary_source,coverage_status,"
            "tokens_before,tokens_after,removed_count,kept_count "
            "FROM session_summaries ORDER BY id"
        ).fetchall()
        canonical: dict[str, str] = {}
        duplicate_ids = 0
        counts = {}
        archived_fact_source_ids = []
        turn_executions = []
        for table in ("transcript_entries", "compacted_transcript_entries"):
            rows = connection.execute(
                "SELECT message_id,role,content,tool_calls,tool_call_id,reasoning_content,"
                f"assistant_replay FROM {table} ORDER BY id"
            ).fetchall()
            counts[table] = len(rows)
            for row in rows:
                duplicate_ids += int(row[0] in canonical)
                canonical[row[0]] = _digest(row[1:])
                if table == "compacted_transcript_entries" and facts and all(
                    value in str(row[2]) for value in facts.values()
                ):
                    archived_fact_source_ids.append(row[0])
            for message_id, usage in connection.execute(
                f"SELECT message_id,turn_usage FROM {table} "
                "WHERE turn_usage IS NOT NULL ORDER BY id"
            ):
                with contextlib.suppress(ValueError, TypeError):
                    turn_executions.append({
                        "message_sha256": _digest(message_id),
                        "execution": execution_metadata_evidence(json.loads(usage)),
                    })
        return {
            "ready": True,
            "counts": counts,
            "canonical_message_digests": canonical,
            "duplicate_canonical_ids": duplicate_ids,
            "archived_fact_source_ids": archived_fact_source_ids,
            "turn_executions": turn_executions,
            "summaries": [{
                "id": row[0], "session_sha256": _digest(row[1]), "sha256": _digest(row[2]),
                "chars": len(row[2]), "source": row[3], "coverage": row[4],
                "tokens_before": row[5], "tokens_after": row[6],
                "removed_count": row[7], "kept_count": row[8],
            } for row in summary_rows],
        }, [row[2] for row in summary_rows]


async def serve_compaction_gateway(
    root: Path,
    *,
    provider: str,
    model: str,
    endpoint: str,
    provider_env: str,
    observer: WireObserver,
    options: CompactionCaseOptions,
    port: int,
    report_path: Path,
    secrets: tuple[str, ...],
    thinking: str,
    execution_overlay: dict[str, Any] | None = None,
    allow_read_files: bool = False,
) -> dict[str, Any]:
    """Serve until root/stop exists; reusing root proves actual restart recovery."""
    root = root.resolve()
    _require(not report_path.resolve().is_relative_to(root), "report_must_be_outside_gateway_root")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = _config(root, provider, model, endpoint, thinking=thinking)
    if execution_overlay:
        config = GatewayConfig.model_validate({
            **config.model_dump(), **public_execution_overlay(execution_overlay),
        })
    config.host, config.port = "127.0.0.1", port
    config.auth = AuthConfig(mode="none")
    config.llm.api_key_env = provider_env
    if options.context_window_tokens is not None:
        config.llm.context_window_tokens = options.context_window_tokens
    elif execution_overlay:
        # A fixed test window masks smaller/larger routed physical deployments.
        config.llm.context_window_tokens = 0
    if options.max_output_tokens is not None:
        config.llm.max_tokens = options.max_output_tokens
    config.compaction.enabled = True
    config.preflight_compact_ratio = options.preflight_ratio or 0.85
    config.tools.profile = "minimal"
    config.tools.allow = ["read_file"] if allow_read_files else []
    config.tools.deny = ["session_status"] if allow_read_files else ["*"]
    registry = AcceptanceToolRegistry(
        (Path(config.workspace_dir), Path(config.attachments.media_root)),
        allow_read_files=allow_read_files,
    )
    if allow_read_files:
        from opensquilla.tools.builtin import filesystem  # noqa: F401

        read_tool = get_default_registry().get("read_file")
        _require(read_tool is not None, "read_file_tool_unavailable")
        assert read_tool is not None
        registry.register(read_tool.spec, read_tool.handler)
    config.log_file_enabled = False
    config.privacy.reliability_diagnostics_enabled = False
    config.privacy.product_analytics_enabled = False
    config.privacy.disable_network_observability = True
    config.control_ui.default_locale = "en"
    config.agent_max_provider_retries = 0
    facts = compaction_task_facts(options.task_profile) if options.task_profile else {}
    events: list[dict[str, Any]] = []
    status = "starting"
    artifact_scan_status = "pending_shutdown"
    pressure_results: dict[int, list[dict[str, Any]]] = {}

    def compaction_event(session_key: str, payload: dict[str, Any]) -> None:
        event: dict[str, Any] = {"session_sha256": _digest(session_key)}
        for name in ("status", "source", "phase", "reason", "effect_status", "durability"):
            value = payload.get(name)
            if isinstance(value, str) and re.fullmatch(r"[a-z0-9_]{1,100}", value):
                event[name] = value
        for name in ("tokens_before", "tokens_after", "removed_count", "kept_count",
                     "rendered_summary_len", "history_capacity_tokens", "history_capacity_chars",
                     "durable_history_tokens", "durable_history_chars", "threshold",
                     "char_threshold", "context_window_tokens", "request_capacity_tokens",
                     "request_capacity_chars", "request_tokens", "request_chars"):
            if type(payload.get(name)) is int:
                event[name] = payload[name]
        if isinstance(payload.get("ratio"), (int, float)) and 0 < payload["ratio"] <= 1:
            event["ratio"] = payload["ratio"]
        if type(payload.get("replay_complete")) is bool:
            event["replay_complete"] = payload["replay_complete"]
        events.append(event)

    def report() -> dict[str, Any]:
        storage, summaries = storage_evidence(root / "state" / "sessions.db", facts)
        wires = []
        for index, call in enumerate(observer.calls):
            if index not in pressure_results:
                pressure_results[index] = pressure_file_evidence(
                    call.request, Path(config.workspace_dir), Path(config.attachments.media_root),
                )
            texts = [str(message.get("content") or "")
                     for message in call.request.get("messages", [])]
            wires.append({
                "summary_request": _is_compaction_wire_call(call),
                "read_file_call_count": sum(
                    tool.get("function", {}).get("name") == "read_file"
                    for tool in call.response.get("tool_calls", [])
                ),
                "pressure_file_results": pressure_results[index],
                "summary_occurrences": [sum(text.count(summary) for text in texts)
                                        for summary in summaries],
                "response_facts": compaction_answer_fact_checks(
                    str(call.response.get("content") or ""), facts,
                ),
                "outage_continued": "OUTAGE_CONTINUED" in str(call.response.get("content") or ""),
                "temporary_window_notice": any("[Temporary history window]" in text
                                               for text in texts),
                "generated_paste_placeholder": any(
                    "Please process the attached pasted text." in text for text in texts
                ),
            })
        result = {
            "status": status, "provider": provider, "model": model,
            "lifecycle_status": status,
            "acceptance_status": ("failed_unobserved_request"
                                  if observer.blocked_unobserved_generation_requests
                                  else "requires_browser_assertions"),
            "artifact_scan_status": artifact_scan_status,
            "blocked_tool_attempts": registry.blocked_tool_attempts,
            "tools_mode": "read_file_only" if allow_read_files else "none",
            "large_paste_chars": LARGE_PASTE_CHARS,
            **deployment_window_evidence(
                provider, model, os.environ.get(provider_env, ""), endpoint,
                config.llm.context_window_tokens,
            ),
            "layout": options.layout, "task_profile": options.task_profile,
            "task_facts": facts,
            "task_instructions": (compaction_task_instructions(options.task_profile)
                                  if options.task_profile else ""),
            "context_window_tokens": config.llm.context_window_tokens,
            "window_mode": ("deployment_auto" if config.llm.context_window_tokens == 0
                            else "explicit_override"),
            "execution_overlay": execution_overlay or {},
            "physical_deployments": [
                {"provider": member_provider, "model": member_model,
                 **deployment_window_evidence(
                     member_provider, member_model,
                     os.environ.get(_provider_env(member_provider), ""),
                     observer.endpoints[member_provider], 0,
                 )}
                for member_provider, member_model in sorted({
                    (call.provider, str(call.request.get("model") or ""))
                    for call in observer.calls if call.provider in observer.endpoints
                })
            ],
            "max_output_tokens": config.llm.max_tokens,
            "preflight_ratio": config.preflight_compact_ratio,
            "session_seeded": False, "storage": storage, "compaction_events": events,
            "wire_summary_matches": wires,
            "summary_fact_checks": [compaction_fact_coverage(summary, facts)
                                    for summary in summaries],
            **_usage_report(observer.calls), **_wire_diagnostics(observer),
        }
        write_safe_report(report_path, result, secrets)
        return result

    with contextlib.ExitStack() as stack:
        stack.enter_context(observer.observe())
        stack.callback(add_compaction_listener(compaction_event))
        server = await start_gateway_server(config=config, run=True, tool_registry=registry)
        try:
            status = "running"
            while not (root / "stop").exists():
                report()
                await asyncio.sleep(1)
        finally:
            await server.close()
            status = "stopped"
            artifact_scan_status = (
                "failed" if _temporary_tree_contains_secret(root, _secret_needles(secrets))
                else "passed"
            )
            report()
    return {
        "lifecycle_status": "stopped", "acceptance_status": "requires_browser_assertions",
        "artifact_scan_status": artifact_scan_status, "report": str(report_path),
    }
