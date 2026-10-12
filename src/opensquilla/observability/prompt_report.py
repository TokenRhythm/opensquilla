"""Structured prompt-composition observability."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, cast

from opensquilla.bootstrap_types import BootstrapFileReport
from opensquilla.observability.decision_log import _hash16

SCHEMA_VERSION = 4


@dataclass
class ToolEntry:
    """Per-tool prompt/schema footprint."""

    name: str
    summary_chars: int = 0
    schema_chars: int = 0
    properties_count: int | None = None


@dataclass
class PromptReport:
    """Compact prompt-composition report for a single turn."""

    turn_id: str
    session_key: str
    session_id: str | None = None
    agent_id: str = ""
    system_chars: int = 0
    tool_count: int = 0
    tool_profile: str | None = None
    tools_schema_chars: int = 0
    skill_count: int = 0
    skills_prompt_chars: int = 0
    memory_md_present: bool = False
    daily_notes_omitted: bool = False
    daily_notes_count_before_omit: int = 0
    daily_notes_policy_reason: str | None = None
    injected_workspace_files_count: int = 0
    bootstrap_files: list[BootstrapFileReport] = field(default_factory=list)
    memory_mode_fingerprint: dict[str, str] = field(default_factory=dict)
    retrieval_mode: str | None = None
    cache_mode: str | None = None
    cache_base_hash: str | None = None
    cache_dynamic_hash: str | None = None
    cache_legacy_hash: str | None = None
    cache_shadow_final_hash: str | None = None
    cache_key_collision: bool = False
    resolved_model: str | None = None
    alias_resolution_chain: list[str] = field(default_factory=list)
    provider_after_rewrite: str | None = None
    reasoning_hint_resolved: str | None = None
    cache_base_chars: int = 0
    cache_dynamic_chars: int = 0
    session_flush_extraction_model: str | None = None
    session_flush_fallback_used: bool = False
    session_flush_fallback_reason: str | None = None
    system_hash: str = ""
    tool_entries: list[ToolEntry] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION


def _tool_schema_payload(tool_def: Any) -> dict:
    schema = getattr(tool_def, "input_schema", None)
    if schema is None:
        return {}
    if hasattr(schema, "model_dump"):
        return cast(dict[str, Any], schema.model_dump(mode="json"))
    if isinstance(schema, dict):
        return schema
    return {}


def _tool_entry(tool_def: Any) -> ToolEntry:
    schema_payload = _tool_schema_payload(tool_def)
    try:
        schema_chars = len(json.dumps(schema_payload, sort_keys=True))
    except TypeError:
        schema_chars = 0
    properties = schema_payload.get("properties") if isinstance(schema_payload, dict) else None
    return ToolEntry(
        name=str(getattr(tool_def, "name", "")),
        summary_chars=len(str(getattr(tool_def, "description", "") or "")),
        schema_chars=schema_chars,
        properties_count=len(properties) if isinstance(properties, dict) else None,
    )


def _bootstrap_file_reports(metadata: dict[str, Any]) -> list[BootstrapFileReport]:
    payload = metadata.get("bootstrap_files")
    if not isinstance(payload, list):
        return []
    reports: list[BootstrapFileReport] = []
    for item in payload:
        if isinstance(item, BootstrapFileReport):
            reports.append(item)
        elif isinstance(item, dict):
            reports.append(
                BootstrapFileReport(
                    filename=str(item.get("filename", "")),
                    raw_chars=int(item.get("raw_chars") or 0),
                    injected_chars=int(item.get("injected_chars") or 0),
                    truncated=bool(item.get("truncated", False)),
                    truncation_cause=(
                        str(item["truncation_cause"])
                        if item.get("truncation_cause") is not None
                        else None
                    ),
                    skipped_reason=(
                        str(item["skipped_reason"])
                        if item.get("skipped_reason") is not None
                        else None
                    ),
                )
            )
    return [report for report in reports if report.filename]


def build_prompt_report(
    *,
    turn_id: str,
    session_key: str,
    session_id: str | None,
    agent_id: str,
    system_prompt: str,
    tool_defs: list[Any],
    metadata: dict[str, Any] | None = None,
    tool_profile: str | None = None,
) -> PromptReport:
    """Build a report from already-resolved prompt/tool inputs."""

    metadata = metadata or {}
    fingerprint = metadata.get("memory_mode_fingerprint")
    if not isinstance(fingerprint, dict):
        fingerprint = {}
    raw_alias_resolution_chain = metadata.get("alias_resolution_chain")
    alias_resolution_chain = (
        raw_alias_resolution_chain if isinstance(raw_alias_resolution_chain, list) else []
    )
    tool_entries = [_tool_entry(tool_def) for tool_def in tool_defs]
    return PromptReport(
        turn_id=turn_id,
        session_key=session_key,
        session_id=session_id,
        agent_id=agent_id,
        system_chars=len(system_prompt),
        system_hash=_hash16(system_prompt),
        tool_count=len(tool_defs),
        tool_profile=tool_profile,
        tools_schema_chars=sum(entry.schema_chars for entry in tool_entries),
        skill_count=int(metadata.get("skill_count") or 0),
        skills_prompt_chars=int(metadata.get("skills_prompt_chars") or 0),
        memory_md_present=bool(metadata.get("memory_md_present", False)),
        daily_notes_omitted=bool(metadata.get("daily_notes_omitted", False)),
        daily_notes_count_before_omit=int(metadata.get("daily_notes_count_before_omit") or 0),
        daily_notes_policy_reason=(
            str(metadata["daily_notes_policy_reason"])
            if metadata.get("daily_notes_policy_reason") is not None
            else None
        ),
        injected_workspace_files_count=int(metadata.get("injected_workspace_files_count") or 0),
        bootstrap_files=_bootstrap_file_reports(metadata),
        memory_mode_fingerprint={str(k): str(v) for k, v in fingerprint.items()},
        retrieval_mode=(
            str(metadata["retrieval_mode"]) if metadata.get("retrieval_mode") is not None else None
        ),
        cache_mode=str(metadata["cache_mode"]) if metadata.get("cache_mode") is not None else None,
        cache_base_hash=(
            str(metadata["cache_base_hash"])
            if metadata.get("cache_base_hash") is not None
            else None
        ),
        cache_dynamic_hash=(
            str(metadata["cache_dynamic_hash"])
            if metadata.get("cache_dynamic_hash") is not None
            else None
        ),
        cache_legacy_hash=(
            str(metadata["cache_legacy_hash"])
            if metadata.get("cache_legacy_hash") is not None
            else None
        ),
        cache_shadow_final_hash=(
            str(metadata["cache_shadow_final_hash"])
            if metadata.get("cache_shadow_final_hash") is not None
            else None
        ),
        cache_key_collision=bool(metadata.get("cache_key_collision", False)),
        resolved_model=(
            str(metadata["resolved_model"]) if metadata.get("resolved_model") is not None else None
        ),
        alias_resolution_chain=[str(item) for item in alias_resolution_chain],
        provider_after_rewrite=(
            str(metadata["provider_after_rewrite"])
            if metadata.get("provider_after_rewrite") is not None
            else None
        ),
        reasoning_hint_resolved=(
            str(metadata["reasoning_hint_resolved"])
            if metadata.get("reasoning_hint_resolved") is not None
            else None
        ),
        cache_base_chars=int(metadata.get("cache_base_chars") or 0),
        cache_dynamic_chars=int(metadata.get("cache_dynamic_chars") or 0),
        session_flush_extraction_model=(
            str(metadata["session_flush_extraction_model"])
            if metadata.get("session_flush_extraction_model") is not None
            else None
        ),
        session_flush_fallback_used=bool(
            metadata.get("session_flush_fallback_used", False)
        ),
        session_flush_fallback_reason=(
            str(metadata["session_flush_fallback_reason"])
            if metadata.get("session_flush_fallback_reason") is not None
            else None
        ),
        tool_entries=tool_entries,
    )
