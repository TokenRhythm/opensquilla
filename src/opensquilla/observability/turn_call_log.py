"""Opt-in raw per-turn call audit log.

The decision log intentionally stores hashes instead of raw prompt bytes.
This module provides the separate, explicit debug surface for developers who
need to inspect the exact LLM requests, aggregated LLM responses, and raw tool
input/output for a turn.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import structlog

from opensquilla.paths import default_opensquilla_home

SCHEMA_VERSION = 1

TURN_CALL_LOG_ENV = "OPENSQUILLA_TURN_CALL_LOG"
TURN_CALL_LOG_DIR_ENV = "OPENSQUILLA_TURN_CALL_LOG_DIR"
LOG_DIR_ENV = "OPENSQUILLA_LOG_DIR"
TURN_CALL_LOG_ENABLED_VALUES = frozenset({"1", "true", "yes", "on"})

log = structlog.get_logger(__name__)

_PROGRESS_INTERVAL_SECONDS = 1.0
_PROGRESS_PREVIEW_CHARS = 32_000


def is_turn_call_log_enabled(diagnostics_state: Any | None = None) -> bool:
    """Return whether raw turn-call logging is explicitly enabled."""

    if os.environ.get(TURN_CALL_LOG_ENV, "").strip().lower() in TURN_CALL_LOG_ENABLED_VALUES:
        return True
    if diagnostics_state is None:
        return False
    raw_enabled = getattr(diagnostics_state, "raw_turn_call_enabled", None)
    if callable(raw_enabled):
        return bool(raw_enabled())
    return False


def _non_empty_env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return value


def resolve_turn_call_log_dir_with_source() -> tuple[Path, str]:
    """Resolve the raw turn-call directory and report the source used."""

    turn_call_dir = _non_empty_env(TURN_CALL_LOG_DIR_ENV)
    if turn_call_dir is not None:
        return Path(turn_call_dir), TURN_CALL_LOG_DIR_ENV

    shared_log_dir = _non_empty_env(LOG_DIR_ENV)
    if shared_log_dir is not None:
        return Path(shared_log_dir), LOG_DIR_ENV

    return default_opensquilla_home() / "logs", "default"


def resolve_turn_call_log_dir() -> Path:
    """Resolve the raw turn-call directory without creating it."""

    directory, _source = resolve_turn_call_log_dir_with_source()
    return directory


def _default_log_dir() -> Path:
    """Resolve the call-log directory.

    ``OPENSQUILLA_TURN_CALL_LOG_DIR`` is specific to this raw debug stream. When it
    is not set, reuse ``OPENSQUILLA_LOG_DIR`` so all observability files remain
    colocated by default.
    """

    return resolve_turn_call_log_dir()


def _json_default(value: Any) -> Any:
    """Serialize project dataclasses and Pydantic models for JSONL output."""

    if isinstance(value, Mock):
        return repr(value)
    model_dump = getattr(type(value), "model_dump", None)
    if callable(model_dump):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)  # type: ignore[arg-type]
    if isinstance(value, Path):
        return str(value)
    return repr(value)


class TurnCallLogger:
    """Best-effort JSONL writer for full-fidelity turn debugging."""

    def __init__(
        self,
        *,
        trace_id: str | None = None,
        turn_id: str,
        session_key: str,
        session_id: str | None = None,
        session_intent: str | None = None,
        agent_id: str,
        provider: str,
        model: str,
        source: dict[str, Any] | None = None,
        log_dir: Path | None = None,
        started_monotonic: float | None = None,
        capture_enabled: Callable[[], bool] | None = None,
        agent_trace_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self.trace_id = trace_id or turn_id
        self.turn_id = turn_id
        self.session_key = session_key
        self.session_id = session_id
        self.session_intent = session_intent
        self.agent_id = agent_id
        self.provider = provider
        self.model = model
        self.source = source or {}
        self.log_dir = log_dir or _default_log_dir()
        self._capture_enabled = capture_enabled
        self._agent_trace_enabled = agent_trace_enabled
        self._seq = 0
        # ``ts`` is wall-clock time and can be coarse or adjusted by the OS.
        # Keep a monotonic, turn-relative clock as an additive field so the
        # timeline can reconstruct exact ordering without inferring spans from
        # rounded response timestamps.
        self._started_monotonic = (
            time.monotonic() if started_monotonic is None else started_monotonic
        )
        self._clock_origin = "logger_start" if started_monotonic is None else "turn_runner_start"

    def write(self, kind: str, payload: dict[str, Any]) -> Path | None:
        """Append one call-log record.

        Logging is intentionally best-effort: a serialization or filesystem
        error must never break an agent turn.
        """

        try:
            if self._capture_enabled is not None and not self._capture_enabled():
                return None
            self.log_dir.mkdir(parents=True, exist_ok=True)
            day = datetime.now(UTC).strftime("%Y%m%d")
            path = self.log_dir / f"turn-calls-{day}.jsonl"
            self._seq += 1
            record = {
                "schema_version": SCHEMA_VERSION,
                # Millisecond precision keeps same-second context checkpoints
                # distinguishable in the timeline while remaining compact JSONL.
                "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "privacy": "raw",
                "agent_trace": (
                    self._agent_trace_enabled() if self._agent_trace_enabled is not None else False
                ),
                "trace_id": self.trace_id,
                "seq": self._seq,
                "turn_id": self.turn_id,
                "session_key": self.session_key,
                "session_id": self.session_id,
                "session_intent": self.session_intent,
                "agent_id": self.agent_id,
                "provider": self.provider,
                "model": self.model,
                "source": self.source,
                "kind": kind,
                "clock_origin": self._clock_origin,
                "elapsed_ms": max(0, int((time.monotonic() - self._started_monotonic) * 1000)),
                "payload": payload,
            }
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")
            return path
        except Exception as exc:  # pragma: no cover - observability must not break turns
            log.debug("turn_call_log.write_failed", kind=kind, error=str(exc))
            return None


class TurnCallProgress:
    """Bounded, rate-limited output snapshots for one already captured LLM call."""

    def __init__(
        self,
        logger: TurnCallLogger,
        *,
        call_id: str,
        iteration: int,
        attempt: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._logger = logger
        self._identity = {"call_id": call_id, "iteration": iteration, "attempt": attempt}
        self._clock = clock
        self._last_write: float | None = None
        self._text = ""
        self._reasoning = ""
        self._text_chars = 0
        self._reasoning_chars = 0
        self._tools: dict[str, dict[str, Any]] = {}

    def append(self, *, text: str = "", reasoning: str = "") -> None:
        if not text and not reasoning:
            return
        self._text_chars += len(text)
        self._reasoning_chars += len(reasoning)
        self._text = (self._text + text)[-_PROGRESS_PREVIEW_CHARS:]
        self._reasoning = (self._reasoning + reasoning)[-_PROGRESS_PREVIEW_CHARS:]
        self._maybe_write()

    def tool_delta(self, tool_use_id: str, name: str, arguments_delta: str) -> None:
        # Bound both the number of pending calls and their combined preview.
        if tool_use_id not in self._tools and len(self._tools) >= 64:
            return
        tool = self._tools.setdefault(
            tool_use_id,
            {"tool_use_id": tool_use_id, "name": name, "arguments_text": "", "arguments_chars": 0},
        )
        tool["arguments_chars"] += len(arguments_delta)
        per_tool_limit = _PROGRESS_PREVIEW_CHARS // max(1, len(self._tools))
        tool["arguments_text"] = (tool["arguments_text"] + arguments_delta)[-per_tool_limit:]
        for item in self._tools.values():
            item["arguments_text"] = item["arguments_text"][-per_tool_limit:]
            item["arguments_offset"] = item["arguments_chars"] - len(item["arguments_text"])
            item["arguments_truncated"] = item["arguments_offset"] > 0
        self._maybe_write()

    def _maybe_write(self) -> None:
        now = self._clock()
        if self._last_write is None or now - self._last_write >= _PROGRESS_INTERVAL_SECONDS:
            self._write(now)

    def reset(self) -> None:
        """Replace a discarded generation's preview at the same logical call."""

        self._text = ""
        self._reasoning = ""
        self._text_chars = 0
        self._reasoning_chars = 0
        self._tools.clear()
        self._write(self._clock())

    def _write(self, now: float) -> None:
        self._last_write = now
        self._logger.write(
            "llm_progress",
            {
                **self._identity,
                "partial": True,
                "text": self._text,
                "reasoning_content": self._reasoning or None,
                "text_chars": self._text_chars,
                "reasoning_chars": self._reasoning_chars,
                "text_offset": self._text_chars - len(self._text),
                "reasoning_offset": self._reasoning_chars - len(self._reasoning),
                "text_truncated": self._text_chars > len(self._text),
                "reasoning_truncated": self._reasoning_chars > len(self._reasoning),
                "tool_calls": [dict(tool) for tool in self._tools.values()],
            },
        )
