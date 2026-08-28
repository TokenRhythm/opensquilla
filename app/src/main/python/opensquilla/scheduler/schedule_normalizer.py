"""Shared cron schedule wire normalization."""

from __future__ import annotations

from typing import Any

from opensquilla.scheduler.parser import CronParseError, parse_cron, parse_iso_at, validate_tz
from opensquilla.scheduler.types import ScheduleKind


def coerce_schedule_from_params(params: dict[str, Any]) -> tuple[ScheduleKind, str, str]:
    schedule_raw = params.get("schedule")
    if isinstance(schedule_raw, dict):
        schedule = dict(schedule_raw)
        top_level_tz = _top_level_tz(params)
        if top_level_tz and schedule.get("kind") == ScheduleKind.CRON.value:
            schedule_tz = schedule.get("tz")
            if isinstance(schedule_tz, str):
                schedule_tz = schedule_tz.strip()
            else:
                schedule_tz = ""
            if schedule_tz and schedule_tz != top_level_tz:
                raise ValueError("schedule.tz conflicts with tz")
            schedule["tz"] = top_level_tz
        return coerce_schedule(schedule)
    expression = params.get("expression")
    if isinstance(expression, str) and expression.strip():
        return coerce_schedule(
            {"kind": "cron", "expr": expression, "tz": params.get("tz", "") or ""}
        )
    raise ValueError("params required: schedule (object) or expression (string)")


def _top_level_tz(params: dict[str, Any]) -> str:
    tz_raw = params.get("tz")
    timezone_raw = params.get("timezone")
    tz = tz_raw.strip() if isinstance(tz_raw, str) else ""
    timezone = timezone_raw.strip() if isinstance(timezone_raw, str) else ""
    if tz and timezone and tz != timezone:
        raise ValueError("tz conflicts with timezone")
    return tz or timezone


def coerce_schedule(raw: dict[str, Any]) -> tuple[ScheduleKind, str, str]:
    kind_raw = raw.get("kind")
    if not isinstance(kind_raw, str) or not kind_raw:
        raise ValueError("schedule.kind required; one of: cron, every, at")
    try:
        kind = ScheduleKind(kind_raw)
    except ValueError as exc:
        raise ValueError(
            f"schedule.kind must be one of: cron, every, at; got {kind_raw!r}"
        ) from exc

    if kind == ScheduleKind.CRON:
        return _coerce_cron(raw)
    if kind == ScheduleKind.EVERY:
        return _coerce_every(raw)
    return _coerce_at(raw)


def _coerce_cron(raw: dict[str, Any]) -> tuple[ScheduleKind, str, str]:
    expr = raw.get("expr")
    if not isinstance(expr, str) or not expr.strip():
        raise ValueError(
            "schedule.expr required when kind='cron'; expected 5-field POSIX cron"
        )
    expr = expr.strip()
    try:
        parse_cron(expr)
    except CronParseError as exc:
        raise ValueError(
            f"schedule.expr invalid: {exc}; expected 5-field POSIX cron"
        ) from exc
    tz_raw = raw.get("tz") or ""
    if not isinstance(tz_raw, str):
        raise ValueError("schedule.tz must be a string IANA timezone name")
    tz_value = tz_raw.strip()
    try:
        validate_tz(tz_value)
    except ValueError as exc:
        raise ValueError(
            f"schedule.tz invalid: {exc}; expected IANA name like 'Asia/Shanghai'"
        ) from exc
    return ScheduleKind.CRON, expr, tz_value


def _coerce_every(raw: dict[str, Any]) -> tuple[ScheduleKind, str, str]:
    if raw.get("anchor_at") not in (None, ""):
        raise ValueError(
            "schedule.anchor_at is not supported for kind='every'; omit it"
        )
    every_seconds = raw.get("every_seconds")
    if not isinstance(every_seconds, int) or isinstance(every_seconds, bool):
        raise ValueError(
            "schedule.every_seconds required (integer >= 1) when kind='every'"
        )
    if every_seconds < 1:
        raise ValueError("schedule.every_seconds must be >= 1 second")
    return ScheduleKind.EVERY, str(every_seconds), ""


def _coerce_at(raw: dict[str, Any]) -> tuple[ScheduleKind, str, str]:
    at_raw = raw.get("at")
    if not isinstance(at_raw, str) or not at_raw.strip():
        raise ValueError(
            "schedule.at required when kind='at'; expected ISO-8601 with timezone"
        )
    try:
        parse_iso_at(at_raw)
    except CronParseError as exc:
        msg = str(exc)
        if "timezone" in msg.lower():
            raise ValueError(
                "schedule.at invalid: must include a timezone offset, "
                "e.g. '2026-05-15T09:00:00+08:00'"
            ) from exc
        raise ValueError(
            f"schedule.at invalid: {exc}; "
            "expected ISO-8601 with timezone like '2026-05-15T09:00:00+08:00'"
        ) from exc
    return ScheduleKind.AT, at_raw.strip(), ""
