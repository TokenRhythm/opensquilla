"""Standard 5-field cron expression parser."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_tz(tz: str) -> None:
    """Raise ValueError if ``tz`` is set but not a valid IANA timezone name."""
    if not tz:
        return
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown timezone: {tz!r}") from exc


class CronParseError(ValueError):
    pass


_FIELD_RANGES = {
    "minute": (0, 59),
    "hour": (0, 23),
    "day_of_month": (1, 31),
    "month": (1, 12),
    "day_of_week": (0, 6),  # 0=Sunday
}

_MONTH_NAMES = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_DOW_NAMES = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}

_PRESETS: dict[str, str] = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


@dataclass(frozen=True)
class CronField:
    values: frozenset[int]
    # True when the field was NOT a bare ``*`` (i.e. the user constrained it).
    # Needed for the POSIX day-of-month/day-of-week OR rule.
    restricted: bool = True

    def matches(self, value: int) -> bool:
        return value in self.values


@dataclass(frozen=True)
class CronExpression:
    minute: CronField
    hour: CronField
    day_of_month: CronField
    month: CronField
    day_of_week: CronField
    raw: str

    def matches(self, dt: datetime) -> bool:
        if not (self.minute.matches(dt.minute) and self.hour.matches(dt.hour)):
            return False
        if not self.month.matches(dt.month):
            return False
        # POSIX/Vixie day rule: day-of-month and day-of-week are ORed when BOTH
        # are restricted (neither is ``*``); if only one is restricted, only
        # that one applies. ANDing them (the old behavior) fired an entry like
        # "0 0 13 * 5" only on the rare 13th-that-is-a-Friday.
        dom_matches = self.day_of_month.matches(dt.day)
        dow = (dt.weekday() + 1) % 7  # Python Mon=0 → cron Sun=0
        dow_matches = self.day_of_week.matches(dow)
        if self.day_of_month.restricted and self.day_of_week.restricted:
            return dom_matches or dow_matches
        return dom_matches and dow_matches


def _parse_field(token: str, field_name: str, names: dict[str, int] | None = None) -> CronField:
    lo, hi = _FIELD_RANGES[field_name]
    values: set[int] = set()

    for part in token.split(","):
        part = part.strip()
        if names:
            # resolve names in ranges and steps too
            sub_parts = part.replace("/", "§").replace("-", "¶")
            for name, val in names.items():
                sub_parts = sub_parts.replace(name.lower(), str(val))
                sub_parts = sub_parts.replace(name.upper(), str(val))
            part = sub_parts.replace("§", "/").replace("¶", "-")

        if "/" in part:
            range_part, step_str = part.split("/", 1)
            try:
                step = int(step_str)
            except ValueError:
                raise CronParseError(f"Invalid step '{step_str}' in field '{field_name}'")
            if step <= 0:
                raise CronParseError(f"Step must be > 0 in field '{field_name}'")

            if range_part == "*":
                start, end = lo, hi
            elif "-" in range_part:
                start_str, end_str = range_part.split("-", 1)
                start = _to_int(start_str, field_name, lo, hi)
                end = _to_int(end_str, field_name, lo, hi)
            else:
                start = _to_int(range_part, field_name, lo, hi)
                end = hi

            values.update(range(start, end + 1, step))

        elif "-" in part:
            start_str, end_str = part.split("-", 1)
            start = _to_int(start_str, field_name, lo, hi)
            end = _to_int(end_str, field_name, lo, hi)
            if start > end:
                raise CronParseError(f"Range start > end in field '{field_name}'")
            values.update(range(start, end + 1))

        elif part == "*":
            values.update(range(lo, hi + 1))

        else:
            values.add(_to_int(part, field_name, lo, hi))

    # A field is "unrestricted" only when the whole token is a bare ``*``.
    # ``*/2`` and explicit ranges/lists are restrictions (they matter for the
    # POSIX day-of-month/day-of-week OR rule in CronExpression.matches).
    restricted = token.strip() != "*"
    return CronField(frozenset(values), restricted=restricted)


def _to_int(s: str, field_name: str, lo: int, hi: int) -> int:
    try:
        v = int(s)
    except ValueError:
        raise CronParseError(f"Invalid value '{s}' in field '{field_name}'")
    if not (lo <= v <= hi):
        raise CronParseError(f"Value {v} out of range [{lo}, {hi}] for field '{field_name}'")
    return v


def parse_iso_at(raw: str) -> datetime:
    """Parse a tz-aware ISO-8601 timestamp; raise CronParseError otherwise."""
    if not isinstance(raw, str):
        raise CronParseError(f"Expected ISO-8601 string, got {type(raw).__name__}")
    text = raw.strip()
    if not text:
        raise CronParseError("ISO-8601 timestamp must not be empty")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CronParseError(f"Invalid ISO-8601 timestamp: {raw!r}") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise CronParseError(
            f"ISO-8601 timestamp must include a timezone offset: {raw!r}"
        )
    return dt


def parse_cron(expr: str) -> CronExpression:
    """Parse a standard 5-field cron expression or @preset shorthand."""
    expr = expr.strip()

    # Handle @presets
    if expr.startswith("@"):
        if expr not in _PRESETS:
            raise CronParseError(f"Unknown preset '{expr}'")
        expr = _PRESETS[expr]

    fields = expr.split()
    if len(fields) != 5:
        raise CronParseError(f"Expected 5 fields, got {len(fields)}: '{expr}'")

    minute_tok, hour_tok, dom_tok, month_tok, dow_tok = fields

    return CronExpression(
        minute=_parse_field(minute_tok, "minute"),
        hour=_parse_field(hour_tok, "hour"),
        day_of_month=_parse_field(dom_tok, "day_of_month"),
        month=_parse_field(month_tok, "month", _MONTH_NAMES),
        day_of_week=_parse_field(dow_tok, "day_of_week", _DOW_NAMES),
        raw=expr,
    )
