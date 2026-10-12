"""Scheduler payload types + cron contract normalization helpers.

This module combines two orthogonal concerns that both live on the job
delivery path:

1. **Delivery reporting (Option-C envelope).**  :class:`DeliveryReport` is
   a net-new shape for reporting the outcome of a delivered scheduler
   job. It intentionally does not force existing types
   (``CronJob``, ``JobExecution``) to change their schema: callers
   populate a :class:`DeliveryReport` lazily, and legacy rows that
   predate ``session_status`` deserialize with ``None`` (backward-
   compatible default).

2. **Cron payload contract (reminder vs agent_turn vs system_event).**
   :func:`normalize_contract` and friends enforce the canonical payload
   kinds the scheduler hands to its handlers. They accept both
   generations of payload shape (``task``/``text`` aliases) and resolve
   the ``sessionTarget`` binding the job belongs to.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .types import SessionTarget

# ---------------------------------------------------------------------------
# Delivery reporting (Option-C envelope)
# ---------------------------------------------------------------------------


@dataclass
class DeliveryReport:
    """Outcome of a single scheduled-job delivery attempt.

    Attributes:
        job_id: The :class:`CronJob` id this report belongs to.
        run_id: The :class:`JobExecution` id this report is scoped to.
        session_status: Terminal delivery state reported by the transport.
            ``None`` means "unknown / not yet populated" (legacy rows or
            in-flight deliveries). Populated later with e.g. ``'delivered'``,
            ``'failed'``, ``'skipped'``, ``'suppressed_offline'``.
        delivered_at: Wall-clock time the transport confirmed the delivery.
            ``None`` if the attempt has not completed.
        detail: Free-form human-readable note (error message, reason code, …).
    """

    job_id: str = ""
    run_id: str = ""
    session_status: str | None = None
    delivered_at: datetime | None = None
    detail: str = ""


def serialize(report: DeliveryReport) -> dict[str, Any]:
    """Round-trip a :class:`DeliveryReport` through a JSON-safe dict.

    ``delivered_at`` is emitted as ISO-8601 (or ``None``). ``session_status``
    is passed through verbatim (``None`` preserved as ``None``).
    """
    return {
        "job_id": report.job_id,
        "run_id": report.run_id,
        "session_status": report.session_status,
        "delivered_at": (report.delivered_at.isoformat() if report.delivered_at else None),
        "detail": report.detail,
    }


def deserialize(data: dict[str, Any]) -> DeliveryReport:
    """Rebuild a :class:`DeliveryReport` from a JSON-safe dict.

    Missing keys default to ``None`` / empty string — legacy rows that predate
    ``session_status`` deserialize cleanly without raising.
    """
    delivered_raw = data.get("delivered_at")
    delivered_at: datetime | None
    if delivered_raw is None or delivered_raw == "":
        delivered_at = None
    elif isinstance(delivered_raw, datetime):
        delivered_at = delivered_raw
    else:
        delivered_at = datetime.fromisoformat(str(delivered_raw))

    return DeliveryReport(
        job_id=data.get("job_id", "") or "",
        run_id=data.get("run_id", "") or "",
        session_status=data.get("session_status"),
        delivered_at=delivered_at,
        detail=data.get("detail", "") or "",
    )


# ---------------------------------------------------------------------------
# Cron payload contract (agent_turn vs system_event)
# ---------------------------------------------------------------------------

AGENT_TURN_KIND = "agent_turn"
REMINDER_KIND = "reminder"
SYSTEM_EVENT_KIND = "system_event"
_VALID_PAYLOAD_KINDS = frozenset({AGENT_TURN_KIND, REMINDER_KIND, SYSTEM_EVENT_KIND})
_KNOWN_HANDLER_KEYS = frozenset({"agent_run", "static_message", "system_event"})
_PERSISTED_EXTENSION_KEYS = (
    "_workspace_id",
    "_workspace_name",
    "_workspace_unavailable",
    "_template_id",
)


def _preserve_extensions(
    canonical: dict[str, str],
    source: dict[str, Any],
) -> dict[str, str]:
    """Keep validated scheduler metadata while dropping arbitrary payload fields."""
    for key in _PERSISTED_EXTENSION_KEYS:
        value = source.get(key)
        if isinstance(value, str) and value:
            canonical[key] = value
    return canonical


def payload_kind(payload: dict[str, Any] | None, session_target: SessionTarget | str) -> str:
    """Resolve the canonical payload kind for a job payload."""
    data = payload or {}
    raw_kind = data.get("kind")
    if isinstance(raw_kind, str) and raw_kind in _VALID_PAYLOAD_KINDS:
        return raw_kind

    target = (
        session_target
        if isinstance(session_target, SessionTarget)
        else SessionTarget(session_target)
    )
    if target == SessionTarget.MAIN:
        return SYSTEM_EVENT_KIND
    return AGENT_TURN_KIND


def payload_text(payload: dict[str, Any] | None, session_target: SessionTarget | str) -> str:
    """Return the primary text field regardless of payload generation version."""
    data = payload or {}
    kind = payload_kind(data, session_target)
    if kind in {REMINDER_KIND, SYSTEM_EVENT_KIND}:
        value = data.get("text") or data.get("task") or ""
    else:
        value = data.get("task") or data.get("text") or ""
    return value if isinstance(value, str) else str(value)


def payload_agent_id(payload: dict[str, Any] | None, default: str = "main") -> str:
    data = payload or {}
    raw = data.get("agent_id", default)
    return raw if isinstance(raw, str) and raw.strip() else default


def normalize_origin_session_key(
    session_target: SessionTarget | str,
    origin_session_key: str = "",
) -> str:
    """Return the canonical origin-session binding for a cron job."""
    target = (
        session_target
        if isinstance(session_target, SessionTarget)
        else SessionTarget(session_target)
    )
    if target == SessionTarget.MAIN:
        return ""
    return origin_session_key or ""


def make_agent_turn_payload(task: str, agent_id: str = "main") -> dict[str, str]:
    return {
        "kind": AGENT_TURN_KIND,
        "task": task,
        "agent_id": agent_id or "main",
    }


def make_reminder_payload(text: str, agent_id: str = "main") -> dict[str, str]:
    return {
        "kind": REMINDER_KIND,
        "text": text,
        "agent_id": agent_id or "main",
    }


def make_system_event_payload(text: str, agent_id: str = "main") -> dict[str, str]:
    return {
        "kind": SYSTEM_EVENT_KIND,
        "text": text,
        "agent_id": agent_id or "main",
    }


def normalize_contract(
    *,
    handler_key: str,
    payload: dict[str, Any] | None,
    session_target: SessionTarget | str,
    session_key: str = "",
    origin_session_key: str = "",
    strict: bool = True,
) -> tuple[str, dict[str, str], SessionTarget, str]:
    """Normalize a job contract into the supported cron execution modes."""
    target = (
        session_target
        if isinstance(session_target, SessionTarget)
        else SessionTarget(session_target)
    )
    bound_session_key = session_key or ""
    if target == SessionTarget.CURRENT and not bound_session_key and origin_session_key:
        bound_session_key = origin_session_key

    data = dict(payload or {})
    explicit_kind = data.get("kind")
    if handler_key not in _KNOWN_HANDLER_KEYS and explicit_kind not in _VALID_PAYLOAD_KINDS:
        return handler_key, data, target, bound_session_key

    kind = payload_kind(data, target)
    agent_id = payload_agent_id(data)
    text = payload_text(data, target)

    if strict and not text.strip():
        raise ValueError("Cron payload text is required")

    if kind == REMINDER_KIND:
        if strict and target == SessionTarget.MAIN:
            raise ValueError("reminder payloads cannot use sessionTarget='main'")
        if (
            strict
            and target in (SessionTarget.CURRENT, SessionTarget.SESSION)
            and not bound_session_key
        ):
            raise ValueError(f"{target.value} sessionTarget requires a bound session key")
        return (
            "static_message",
            _preserve_extensions(make_reminder_payload(text, agent_id), data),
            target,
            bound_session_key,
        )

    if kind == SYSTEM_EVENT_KIND:
        if strict and target != SessionTarget.MAIN:
            raise ValueError("system_event payloads require sessionTarget='main'")
        return (
            "system_event",
            _preserve_extensions(make_system_event_payload(text, agent_id), data),
            target,
            bound_session_key,
        )

    if strict and target == SessionTarget.MAIN:
        raise ValueError("agent_turn payloads cannot use sessionTarget='main'")
    if (
        strict
        and target in (SessionTarget.CURRENT, SessionTarget.SESSION)
        and not bound_session_key
    ):
        raise ValueError(f"{target.value} sessionTarget requires a bound session key")

    return (
        "agent_run",
        _preserve_extensions(make_agent_turn_payload(text, agent_id), data),
        target,
        bound_session_key,
    )


__all__ = [
    "AGENT_TURN_KIND",
    "REMINDER_KIND",
    "SYSTEM_EVENT_KIND",
    "DeliveryReport",
    "deserialize",
    "make_agent_turn_payload",
    "make_reminder_payload",
    "make_system_event_payload",
    "normalize_contract",
    "normalize_origin_session_key",
    "payload_agent_id",
    "payload_kind",
    "payload_text",
    "serialize",
]
