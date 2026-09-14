"""Content-free projection at operational log and support-bundle boundaries.

Secret-pattern scrubbing cannot recognize private prose. Only machine-owned
metadata fields may carry strings here; prompts, previews, exception messages,
unknown objects and unstructured log messages are never rendered. Raw capture
has its own explicit opt-in sink and must not pass through this module.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from opensquilla.observability.redact import scrub_text

# These are identifiers/classifications, not arbitrary descriptions. In
# particular, do not add message, detail, hint, title, path, URL or error prose.
_IDENTIFIERS = frozenset("""
    event kind event_type privacy level logger source step step_name phase status
    state code error_code error_type exception_type reason_code failure_kind
    degraded_reason skip_reason method operation mode
    session_key session_id turn_id trace_id task_id run_id parent_run_id agent_id
    conn_id connection_id request_id response_id message_id user_message_id
    tool_call_id compaction_id decision_id operation_id workspace_id surface_id error_id
    tool_profile tool_choice tool_name tool model provider model_id
    provider_id resolved_model routed_model baseline_model provider_after_rewrite
    target_provider target_model target_source target_fingerprint route tier
    routed_tier routing_source skill meta_skill skill_name skills_invoked
    skill_catalog_ids alias_resolution_chain activation_mode input_mode run_kind
    channel channel_id transport protocol backend platform architecture version
    format summary_format summary_source coverage_status flush_receipt_status
    cost_source thinking_mode retrieval_mode cache_mode daily_notes_policy_reason
    vision_followup_gate_decision vision_followup_gate_source
    vision_followup_gate_model vision_followup_gate_reason vision_followup_fallback
    session_flush_extraction_model consumer_window_source
    pressure_kind target_window_source capture_mode recall_mode file_role role policy
    ts at timestamp prompt_hash system_prompt_hash tool_list_hash message_hash
    cache_base_hash cache_dynamic_hash cache_legacy_hash cache_shadow_final_hash
    runtime_context_hash log_schema filename truncation_cause skipped_reason
""".split())
_CONTAINERS = frozenset({
    "attrs", "payload", "fields", "entry", "pipeline_steps", "savings",
    "bootstrap_files", "memory_mode_fingerprint", "metadata", "stats", "timings",
})
# Retain only producer-owned enum values for fields that could otherwise be
# mistaken for free-form reason text. These codes are consumed by diagnostics.
_CLASSIFICATIONS = {"image_route_reason": frozenset({"current_turn", "history_context"})}
_FIELD = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")
_CONTENT_FIELDS = frozenset({
    "content", "text", "prompt", "query", "message", "messages", "input", "output",
    "response", "request", "error", "exception", "detail", "details", "hint",
    "intent_summary", "session_intent", "user_intent", "user_message", "body",
    "api_key", "token", "secret", "password", "authorization", "exc_info", "stack_info",
    # These fields also receive str(exc) or model-produced prose at call sites.
    "reason", "fallback_reason", "image_route_reason", "session_flush_fallback_reason",
})
_IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:/@+\-]{0,255}\Z")
_LEGACY_PREFIX = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d[ T][\d:.,+Z-]+)\s+"
    r"\[(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\]\s+"
    r"(?P<logger>[A-Za-z_][A-Za-z0-9_.]*):"
)


def _identifier(value: str) -> str | None:
    if _IDENTIFIER.fullmatch(value) and scrub_text(value) == value:
        return value
    return None


def log_metadata(value: Mapping[str, Any], *, _depth: int = 0) -> dict[str, Any]:
    """Copy admitted scalar metadata without stringifying unknown values.

    Lists and dictionaries are traversed only in known schema containers.
    The depth/item bounds also keep malformed diagnostics from blocking logging.
    """
    if _depth >= 8:
        return {}
    result: dict[str, Any] = {}
    for index, (key, item) in enumerate(value.items()):
        if index >= 256:
            break
        if not isinstance(key, str) or not _FIELD.fullmatch(key):
            continue
        if key in _CLASSIFICATIONS:
            if isinstance(item, str) and item in _CLASSIFICATIONS[key]:
                result[key] = item
            continue
        if key in _CONTENT_FIELDS or key.endswith(("_preview", "_head", "_body", "_text")):
            continue
        if item is None or isinstance(item, (bool, int)):
            result[key] = item
        elif isinstance(item, float) and math.isfinite(item):
            result[key] = item
        elif key in _IDENTIFIERS:
            if isinstance(item, str):
                safe = _identifier(item)
                if safe is not None:
                    result[key] = safe
            elif isinstance(item, (list, tuple)):
                result[key] = [
                    safe for part in item[:256]
                    if isinstance(part, str) and (safe := _identifier(part)) is not None
                ]
        elif key in _CONTAINERS:
            if isinstance(item, Mapping):
                result[key] = log_metadata(item, _depth=_depth + 1)
            elif isinstance(item, (list, tuple)):
                result[key] = [
                    log_metadata(part, _depth=_depth + 1)
                    for part in item[:256] if isinstance(part, Mapping)
                ]
    return result


def private_log_event(
    logger: Any, method_name: str, event_dict: Mapping[str, Any],
) -> dict[str, Any]:
    """Structlog processor shared by CLI and Gateway, before any renderer."""
    result = log_metadata(event_dict)
    result.setdefault("event", "unstructured_log")
    exc_info = event_dict.get("exc_info")
    if exc_info is True:
        exc_info = sys.exc_info()
    exc_type = None
    if isinstance(exc_info, BaseException):
        exc_type = type(exc_info)
    elif isinstance(exc_info, tuple) and exc_info and isinstance(exc_info[0], type):
        exc_type = exc_info[0]
    if exc_type is not None:
        result["exception_type"] = _identifier(exc_type.__name__) or "Exception"
    # Do not forward exc_info, formatted exception text, stack_info or exception
    # chains: stdlib/structlog renderers can echo payloads and source literals.
    return result


class PrivateLogFormatter(logging.Formatter):
    """JSON metadata after the level prefix understood by existing clients."""

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "_opensquilla_log_metadata", None)
        if not isinstance(fields, dict):
            # Third-party SDKs use %-formatting and HTTP exception reprs. Never
            # call getMessage()/formatException() on those untrusted payloads.
            fields = private_log_event(None, "", {"exc_info": record.exc_info})
        payload = log_metadata(fields)
        payload.update({
            "log_schema": 1,
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": _identifier(record.name) or "unknown",
        })
        metadata = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        return f"{payload['ts']} [{payload['level']}] {payload['logger']}: {metadata}"


def uvicorn_log_config() -> dict[str, Any]:
    """Keep Uvicorn's separate stderr handler behind the same privacy boundary."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"default": {"()": PrivateLogFormatter}},
        "handlers": {"default": {
            "class": "logging.StreamHandler", "formatter": "default", "stream": "ext://sys.stderr",
        }},
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["default"], "level": "INFO", "propagate": False},
        },
    }


def scrub_log_artifact(text: str) -> str:
    """Project current JSON and legacy log tails for a shareable bundle.

    Legacy prose/continuation lines cannot be parsed safely (including a tail
    cut inside a multiline payload). Retain only recognized timestamp, level
    and logger prefixes, or an omission marker. Never rewrite source files.
    """
    lines: list[str] = []
    omitted = 0
    for line in text.splitlines():
        try:
            payload = json.loads(line)
        except (ValueError, RecursionError):
            payload = None
        if isinstance(payload, dict):
            lines.append(json.dumps(log_metadata(payload), ensure_ascii=False))
            continue
        match = _LEGACY_PREFIX.match(line)
        if match is not None:
            try:
                fields = json.loads(line[match.end():].strip())
            except (ValueError, RecursionError):
                fields = None
            metadata = log_metadata(fields) if isinstance(fields, dict) else {
                "content_omitted": True,
            }
            lines.append(json.dumps({**metadata, **match.groupdict()}))
        else:
            omitted += 1
    if omitted:
        lines.append(json.dumps({"content_omitted": True, "omitted_lines": omitted}))
    return "\n".join(lines) + ("\n" if lines else "")
