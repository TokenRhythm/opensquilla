"""Observability baseline: decision log + safety event log + replay.

This package defines:

* :class:`DecisionEntry` / :class:`PipelineStepRecord` — structured per-turn
  records appended to ``~/.opensquilla/logs/decisions-YYYYMMDD.jsonl``.
* :class:`SafetyEvent` / :class:`SafetyEventType` — independent event stream
  appended to ``~/.opensquilla/logs/safety-YYYYMMDD.jsonl``.
* :class:`TurnCallLogger` — opt-in raw call audit stream appended to
  ``~/.opensquilla/logs/turn-calls-YYYYMMDD.jsonl``.
* :class:`TraceEvent` / :class:`JsonlTraceSink` — safe trace correlation stream
  appended to ``~/.opensquilla/logs/traces-YYYYMMDD.jsonl``.
* :class:`PromptReport` — structured prompt-composition report for a turn.
* :func:`load_turn` / :func:`format_transcript` — read-only replay API that
  never re-executes tools.

Schema version is pinned to :data:`SCHEMA_VERSION`; changes remain additive
until the integer is bumped.
"""

from __future__ import annotations

from opensquilla.observability.decision_log import (
    SCHEMA_VERSION,
    DecisionEntry,
    PipelineStepRecord,
    compute_hashes,
    load_entries,
    write_decision_entry,
)
from opensquilla.observability.prompt_report import PromptReport, ToolEntry, build_prompt_report
from opensquilla.observability.replay import format_transcript, load_turn
from opensquilla.observability.safety_log import (
    SafetyEvent,
    SafetyEventType,
    write_safety_event,
)
from opensquilla.observability.trace import (
    TRACE_SCHEMA_VERSION,
    JsonlTraceSink,
    MemoryTraceSink,
    PrivacyGuardSink,
    TraceContext,
    TraceEvent,
    load_trace_events,
    write_trace_event,
)
from opensquilla.observability.turn_call_log import TurnCallLogger, is_turn_call_log_enabled

__all__ = [
    "SCHEMA_VERSION",
    "TRACE_SCHEMA_VERSION",
    "DecisionEntry",
    "JsonlTraceSink",
    "MemoryTraceSink",
    "PipelineStepRecord",
    "PrivacyGuardSink",
    "PromptReport",
    "SafetyEvent",
    "SafetyEventType",
    "ToolEntry",
    "TraceContext",
    "TraceEvent",
    "TurnCallLogger",
    "build_prompt_report",
    "compute_hashes",
    "format_transcript",
    "is_turn_call_log_enabled",
    "load_trace_events",
    "load_entries",
    "load_turn",
    "write_decision_entry",
    "write_safety_event",
    "write_trace_event",
]
