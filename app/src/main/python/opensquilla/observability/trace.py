"""Small trace contract primitives for observability producers and tests."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, Self

from opensquilla.paths import default_opensquilla_home

TRACE_SCHEMA_VERSION = 1
LOG_DIR_ENV = "OPENSQUILLA_LOG_DIR"

TracePrivacy = Literal["operational", "diagnostic", "raw"]
_VALID_PRIVACY: frozenset[str] = frozenset({"operational", "diagnostic", "raw"})


def _utc_ts() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_log_dir() -> Path:
    return Path(os.environ.get(LOG_DIR_ENV, str(default_opensquilla_home() / "logs")))


@dataclass(frozen=True, slots=True)
class TraceContext:
    """Correlation identity shared across gateway, runtime, tools, and children."""

    trace_id: str
    session_key: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    parent_run_id: str | None = None
    agent_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.trace_id, str) or not self.trace_id.strip():
            raise ValueError("trace_id must be non-empty")
        for field_name in (
            "session_key",
            "session_id",
            "turn_id",
            "task_id",
            "run_id",
            "parent_run_id",
            "agent_id",
        ):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field_name} must be non-empty when provided")

    @classmethod
    def new(
        cls,
        *,
        trace_id: str | None = None,
        session_key: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        parent_run_id: str | None = None,
        agent_id: str | None = None,
    ) -> Self:
        return cls(
            trace_id=trace_id if trace_id is not None else uuid.uuid4().hex,
            session_key=session_key,
            session_id=session_id,
            turn_id=turn_id,
            task_id=task_id,
            run_id=run_id,
            parent_run_id=parent_run_id,
            agent_id=agent_id,
        )

    def child(
        self,
        *,
        run_id: str | None = None,
        parent_run_id: str | None = None,
        agent_id: str | None = None,
        task_id: str | None = None,
        turn_id: str | None = None,
        session_key: str | None = None,
        session_id: str | None = None,
    ) -> Self:
        return type(self)(
            trace_id=self.trace_id,
            session_key=session_key if session_key is not None else self.session_key,
            session_id=session_id if session_id is not None else self.session_id,
            turn_id=turn_id if turn_id is not None else self.turn_id,
            task_id=task_id if task_id is not None else self.task_id,
            run_id=run_id,
            parent_run_id=(
                parent_run_id
                if parent_run_id is not None
                else self.run_id or self.task_id or self.turn_id
            ),
            agent_id=agent_id if agent_id is not None else self.agent_id,
        )

    def to_dict(self) -> dict[str, str | None]:
        return {
            "trace_id": self.trace_id,
            "session_key": self.session_key,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "agent_id": self.agent_id,
        }


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """Versioned event envelope used by trace sinks and contract tests."""

    kind: str
    context: TraceContext
    privacy: TracePrivacy = "operational"
    seq: int | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=_utc_ts)
    schema_version: int = TRACE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != TRACE_SCHEMA_VERSION:
            raise ValueError(f"unsupported trace schema_version: {self.schema_version}")
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("trace event kind must be non-empty")
        if self.privacy not in _VALID_PRIVACY:
            raise ValueError(f"invalid trace privacy: {self.privacy}")

    @property
    def trace_id(self) -> str:
        return self.context.trace_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ts": self.ts,
            "kind": self.kind,
            "privacy": self.privacy,
            **self.context.to_dict(),
            "seq": self.seq,
            "attrs": self.attrs,
            "payload": self.payload,
        }


class TraceSink(Protocol):
    """Minimal sink protocol for trace event consumers."""

    def write(self, event: TraceEvent) -> None:
        ...


class MemoryTraceSink:
    """In-memory sink for contract tests and future lightweight diagnostics."""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def write(self, event: TraceEvent) -> None:
        self.events.append(event)

    def by_trace_id(self, trace_id: str) -> list[TraceEvent]:
        return [event for event in self.events if event.trace_id == trace_id]


class JsonlTraceSink:
    """Append safe trace events to ``traces-YYYYMMDD.jsonl``."""

    def __init__(self, log_dir: Path | None = None, *, allow_raw: bool = False) -> None:
        self.log_dir = log_dir or _default_log_dir()
        self.allow_raw = allow_raw

    def write(self, event: TraceEvent) -> None:
        _append_trace_event(event, self.log_dir, allow_raw=self.allow_raw)


class PrivacyGuardSink:
    """Prevent raw trace events from flowing into safe/default sinks."""

    def __init__(self, sink: TraceSink, *, allow_raw: bool = False) -> None:
        self.sink = sink
        self.allow_raw = allow_raw

    def write(self, event: TraceEvent) -> None:
        if event.privacy == "raw" and not self.allow_raw:
            raise ValueError("raw trace event cannot be written through a safe sink")
        self.sink.write(event)


def write_trace_event(
    event: TraceEvent,
    log_dir: Path | None = None,
    *,
    allow_raw: bool = False,
) -> Path:
    """Append one trace event and return the file path written."""

    return _append_trace_event(event, log_dir or _default_log_dir(), allow_raw=allow_raw)


def load_trace_events(trace_id: str, log_dir: Path | None = None) -> list[TraceEvent]:
    """Load all persisted events for ``trace_id`` from trace JSONL files."""

    log_dir = log_dir or _default_log_dir()
    if not trace_id.strip() or not log_dir.is_dir():
        return []
    events: list[TraceEvent] = []
    for jsonl in sorted(log_dir.glob("traces-*.jsonl")):
        with jsonl.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                if payload.get("trace_id") == trace_id:
                    events.append(_trace_event_from_payload(payload))
    return events


def _append_trace_event(event: TraceEvent, log_dir: Path, *, allow_raw: bool) -> Path:
    if event.privacy == "raw" and not allow_raw:
        raise ValueError("raw trace event cannot be written through a safe sink")
    log_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(UTC).strftime("%Y%m%d")
    path = log_dir / f"traces-{day}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
    return path


def _trace_event_from_payload(payload: dict[str, Any]) -> TraceEvent:
    context = TraceContext(
        trace_id=str(payload["trace_id"]),
        session_key=payload.get("session_key"),
        session_id=payload.get("session_id"),
        turn_id=payload.get("turn_id"),
        task_id=payload.get("task_id"),
        run_id=payload.get("run_id"),
        parent_run_id=payload.get("parent_run_id"),
        agent_id=payload.get("agent_id"),
    )
    return TraceEvent(
        kind=str(payload["kind"]),
        context=context,
        privacy=payload.get("privacy", "operational"),
        seq=payload.get("seq"),
        attrs=payload.get("attrs") or {},
        payload=payload.get("payload") or {},
        ts=str(payload.get("ts") or _utc_ts()),
        schema_version=int(payload.get("schema_version", TRACE_SCHEMA_VERSION)),
    )
