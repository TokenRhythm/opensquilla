"""Per-turn structured decision log.

A :class:`DecisionEntry` is one row appended to
``~/.opensquilla/logs/decisions-YYYYMMDD.jsonl`` at the end of every completed turn.
Writes are best-effort and never block turn execution (see
``engine/runtime.py``).

Raw prompt bytes are **never** written to the default log — only hashes.
When ``OPENSQUILLA_DEBUG_LOG=1``, a mirror of the structured entry is written
under ``~/.opensquilla/logs/debug/`` for operator debugging.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from opensquilla.bootstrap_types import BootstrapFileReport
from opensquilla.paths import default_opensquilla_home

SCHEMA_VERSION = 16
_INTENT_SUMMARY_MAX_CHARS = 500
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_SECRET_ASSIGN_RE = re.compile(
    r"\b(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*([^\s,;]+)",
    re.IGNORECASE,
)
_LONG_SECRET_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|[A-Za-z0-9_/-]{32,})\b")
_ABS_PATH_RE = re.compile(r"(?<!\w)(?:/home/[^/\s]+|/Users/[^/\s]+|/root)(?:/[^\s]+)*")
_VISION_GATE_DECISIONS = {"needs_image", "text_only", "unknown"}
_VISION_GATE_STATIC_DECISIONS = {"disabled", "current_image", "not_applicable"}
_VISION_GATE_STATIC_REASONS = {"candidate_window_expired"}

RoutingSource = Literal[
    "v4_phase3",
    "v4_unavailable",
    "cache",
    "image_route",
    "fts",
    "substring",
    "none",
]


@dataclass
class SavingsTelemetry:
    """Per-turn savings telemetry."""

    # Squilla router (V4_phase3 ML + heuristic)
    routed_model: str | None = None
    baseline_model: str | None = None
    routing_confidence: float | None = None
    routing_savings_pct: float | None = None
    routing_savings_usd_estimated_vs_baseline: float | None = None

    # Tool-result projection
    tool_projection_applied: bool = False
    tool_projection_calls: int = 0
    tool_projection_tokens_before: int = 0
    tool_projection_tokens_after: int = 0
    tool_projection_tokens_saved: int = 0
    tool_result_store_writes: int = 0
    tool_result_store_skips: int = 0

    # Thinking mode
    thinking_mode: str | None = None

    # Short-reply prompt enforcement
    short_reply_active: bool = False
    short_reply_savings_tokens_estimated: int = 0
    short_reply_savings_usd_estimated_vs_baseline: float | None = None

    # Cache Hit (5th mechanism)
    cache_hit_active: bool = False
    cache_hit_tokens_saved: int = 0
    cache_hit_usd_estimated_vs_baseline: float | None = None

    # Billed cost from provider telemetry. Not used by the popup savings score.
    billed_cost_usd: float | None = None
    cost_usd: float | None = None
    cost_source: str | None = None

    # Comprehensive per-turn popup score from token counts and model prices.
    # Cache-hit and billed-cost effects remain separate telemetry.
    total_savings_pct: float | None = None
    total_savings_usd: float | None = None


@dataclass
class PipelineStepRecord:
    """One row per pipeline step, populated by ``run_pipeline``.

    A record is emitted on the success, fail-open (exception), and
    skipped-by-gate (early-return) paths.
    """

    step_name: str
    applied: bool
    routed_tier: str | None = None
    filtered_skill_ids: list[str] | None = None
    routing_source: RoutingSource = "none"
    confidence: float | None = None
    fallback_reason: str | None = None


@dataclass
class DecisionEntry:
    """Canonical per-turn decision-log row."""

    turn_id: str
    session_key: str
    prompt_hash: str
    system_prompt_hash: str
    tool_list_hash: str
    tool_choice: str
    tokens_input: int
    tokens_output: int
    model: str
    provider: str
    latency_ms: int
    ts: str
    session_id: str | None = None
    session_intent: str | None = None
    intent_summary: str | None = None
    trace_id: str | None = None
    # Join key into the persisted V017 router_decisions table (uuid4 hex,
    # stamped by engine/steps/router_decision_record.py). Additive-only.
    decision_id: str | None = None
    tool_profile: str | None = None
    system_chars: int = 0
    tool_count: int = 0
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
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    resolved_model: str | None = None
    alias_resolution_chain: list[str] = field(default_factory=list)
    provider_after_rewrite: str | None = None
    cache_legacy_hash: str | None = None
    cache_shadow_final_hash: str | None = None
    cache_key_collision: bool = False
    reasoning_hint_resolved: str | None = None
    cache_base_chars: int = 0
    cache_dynamic_chars: int = 0
    runtime_context_hash: str | None = None
    runtime_context_chars: int = 0
    session_flush_extraction_model: str | None = None
    session_flush_fallback_used: bool = False
    session_flush_fallback_reason: str | None = None
    image_route_reason: str | None = None
    vision_followup_gate_decision: str | None = None
    vision_followup_gate_confidence: float | None = None
    vision_followup_gate_reason: str | None = None
    vision_followup_gate_source: str | None = None
    vision_followup_gate_model: str | None = None
    vision_followup_needs_image: bool | None = None
    vision_followup_fallback: str | None = None
    skills_invoked: list[str] = field(default_factory=list)
    pipeline_steps: list[PipelineStepRecord] = field(default_factory=list)
    savings: SavingsTelemetry = field(default_factory=SavingsTelemetry)
    schema_version: int = SCHEMA_VERSION


def _hash16(text: str) -> str:
    """Return the first 16 hex chars of the sha256 digest."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def build_intent_summary(message: str, max_chars: int = _INTENT_SUMMARY_MAX_CHARS) -> str:
    """Return a bounded, redacted intent hint for history aggregation.

    Default decision logs still avoid raw prompt storage. This derived summary
    preserves enough task shape for history mining while removing common
    secrets, emails, URLs, and machine-local absolute paths.
    """

    text = " ".join(str(message or "").split())
    if not text:
        return ""
    text = _URL_RE.sub("[url]", text)
    text = _EMAIL_RE.sub("[email]", text)
    text = _SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(1)}=[secret]", text)
    text = _LONG_SECRET_RE.sub("[secret]", text)
    text = _ABS_PATH_RE.sub(_redact_path_keep_basename, text)
    if len(text) > max_chars:
        text = text[: max(0, max_chars - 1)].rstrip() + "…"
    return text


def build_vision_followup_gate_reason_code(
    *,
    decision: str | None,
    source: str | None,
    reason: str | None,
    fallback: str | None,
) -> str | None:
    """Return a privacy-safe reason code for default decision logs.

    Gate ``reason`` values may come from an LLM response or provider exception
    and can echo prompt text. Default logs store only stable classification
    codes; raw reason text stays out of the per-turn JSONL log.
    """

    decision_text = str(decision or "")
    source_text = str(source or "")
    reason_text = str(reason or "")

    if reason_text in _VISION_GATE_STATIC_REASONS:
        return reason_text
    if decision_text in _VISION_GATE_STATIC_DECISIONS:
        return decision_text
    if source_text == "explicit_image_reference":
        return "explicit_image_reference"
    if source_text == "explicit_opt_out":
        return "explicit_opt_out"
    if source_text == "error":
        return "gate_error"
    if fallback:
        return f"fallback_{fallback}"
    if source_text == "llm" and decision_text in _VISION_GATE_DECISIONS:
        return f"llm_{decision_text}"
    if decision_text == "unknown":
        return "unknown"
    return None


def _redact_path_keep_basename(match: re.Match[str]) -> str:
    basename = match.group(0).rstrip("/").rsplit("/", 1)[-1]
    return f"[path:{basename}]" if basename else "[path]"


def _default_log_dir() -> Path:
    """Resolve the decision-log directory.

    Honours the ``OPENSQUILLA_LOG_DIR`` env override; defaults to the user-level
    ``~/.opensquilla/logs`` directory.
    """

    return Path(os.environ.get("OPENSQUILLA_LOG_DIR", str(default_opensquilla_home() / "logs")))


def compute_hashes(
    prompt: str,
    system_prompt: str,
    tool_list: list[str],
) -> tuple[str, str, str]:
    """Compute the three canonical hashes for a turn.

    Tool-list hash sorts the names first so equivalent tool sets produce
    identical hashes regardless of enumeration order.
    """

    return (
        _hash16(prompt),
        _hash16(system_prompt),
        _hash16("\n".join(sorted(tool_list))),
    )


def write_decision_entry(
    entry: DecisionEntry,
    log_dir: Path | None = None,
) -> Path:
    """Append ``entry`` as one JSON line; return the file path written to."""

    log_dir = log_dir or _default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(UTC).strftime("%Y%m%d")
    path = log_dir / f"decisions-{day}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")

    # Debug mirror: opt-in via OPENSQUILLA_DEBUG_LOG=1. Mirrors the *structured*
    # entry, never raw prompt bytes. Reuses `day` so primary and debug files
    # cannot drift across a UTC midnight rollover between writes.
    if os.environ.get("OPENSQUILLA_DEBUG_LOG") == "1":
        debug_dir = log_dir / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_path = debug_dir / f"decisions-{day}-raw.jsonl"
        with debug_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {"turn_id": entry.turn_id, "entry": asdict(entry)},
                    ensure_ascii=False,
                )
                + "\n"
            )

    return path


def load_entries(path: Path) -> list[DecisionEntry]:
    """Read a decisions JSONL file and return hydrated DecisionEntry records.

    Unknown fields are ignored so readers tolerate a future schema bump that
    adds attributes (additive-only policy; see SCHEMA_VERSION).
    """

    if not path.is_file():
        return []

    entries: list[DecisionEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        payload = json.loads(line)
        payload = _coerce_decision_payload(payload)
        steps_payload = payload.pop("pipeline_steps", [])
        steps = [
            PipelineStepRecord(**_filter_payload(PipelineStepRecord, s))
            for s in steps_payload
            if isinstance(s, dict)
        ]
        bootstrap_payload = payload.pop("bootstrap_files", [])
        bootstrap_files = [
            BootstrapFileReport(**_filter_payload(BootstrapFileReport, item))
            for item in bootstrap_payload
            if isinstance(item, dict)
        ]
        savings_payload = payload.pop("savings", {})
        savings = SavingsTelemetry(**_filter_payload(SavingsTelemetry, savings_payload))
        entries.append(
            DecisionEntry(
                pipeline_steps=steps,
                bootstrap_files=bootstrap_files,
                savings=savings,
                **_filter_payload(DecisionEntry, payload),
            )
        )
    return entries


def _filter_payload(cls: type, payload: dict) -> dict:
    """Drop fields unknown to ``cls`` so readers tolerate future log schemas."""

    allowed = {f.name for f in fields(cls)}
    return {k: v for k, v in payload.items() if k in allowed}


def _coerce_decision_payload(payload: dict) -> dict:
    """Normalize legacy decision-log rows into the current identity shape."""

    normalized = dict(payload)
    if "session_key" not in normalized:
        legacy_session = normalized.get("session_id", "")
        normalized["session_key"] = legacy_session if isinstance(legacy_session, str) else ""
        normalized["session_id"] = None
    if "trace_id" not in normalized:
        turn_id = normalized.get("turn_id")
        normalized["trace_id"] = turn_id if isinstance(turn_id, str) else None
    return normalized
