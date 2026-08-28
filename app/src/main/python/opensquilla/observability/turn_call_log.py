"""Opt-in raw per-turn call audit log.

The decision log intentionally stores hashes instead of raw prompt bytes.
This module provides the separate, explicit debug surface for developers who
need to inspect the exact LLM requests, aggregated LLM responses, and raw tool
input/output for a turn.
"""

from __future__ import annotations

import json
import os
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
        self._seq = 0

    def write(self, kind: str, payload: dict[str, Any]) -> Path | None:
        """Append one call-log record.

        Logging is intentionally best-effort: a serialization or filesystem
        error must never break an agent turn.
        """

        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            day = datetime.now(UTC).strftime("%Y%m%d")
            path = self.log_dir / f"turn-calls-{day}.jsonl"
            self._seq += 1
            record = {
                "schema_version": SCHEMA_VERSION,
                "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "privacy": "raw",
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
                "payload": payload,
            }
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")
            return path
        except Exception as exc:  # pragma: no cover - observability must not break turns
            log.debug("turn_call_log.write_failed", kind=kind, error=str(exc))
            return None
