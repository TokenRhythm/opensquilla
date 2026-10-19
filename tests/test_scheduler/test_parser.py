"""Cron parser surface: parse_cron acceptance/rejection + parse_iso_at."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from opensquilla.scheduler.parser import CronParseError, parse_cron, parse_iso_at

# --- parse_cron ----------------------------------------------------------


def test_parse_cron_accepts_basic_five_field() -> None:
    assert parse_cron("*/5 * * * *").raw == "*/5 * * * *"


def test_parse_cron_accepts_named_dow_and_month() -> None:
    assert parse_cron("0 9 * * 1-5").raw == "0 9 * * 1-5"
    assert parse_cron("30 8 1 jan *").raw == "30 8 1 jan *"


def test_parse_cron_accepts_preset_alias() -> None:
    assert parse_cron("@hourly").raw == "0 * * * *"


def test_parse_cron_rejects_wrong_field_count() -> None:
    with pytest.raises(CronParseError, match="Expected 5 fields"):
        parse_cron("0 9 * *")


def test_parse_cron_rejects_out_of_range_value() -> None:
    with pytest.raises(CronParseError, match="out of range"):
        parse_cron("0 25 * * *")


@pytest.mark.parametrize("step", ["", "/2"])
@pytest.mark.parametrize("expression", ["50-10{step} * * * *", "0 9 * * fri-mon{step}"])
def test_parse_cron_rejects_reversed_ranges(expression: str, step: str) -> None:
    with pytest.raises(CronParseError, match="Range start > end"):
        parse_cron(expression.format(step=step))


def test_parse_cron_preserves_ascending_stepped_ranges() -> None:
    expression = parse_cron("10-20/5 9 * * mon-fri/2")

    assert expression.minute.values == frozenset({10, 15, 20})
    assert expression.day_of_week.values == frozenset({1, 3, 5})


def test_parse_cron_rejects_garbage() -> None:
    with pytest.raises(CronParseError):
        parse_cron("not-a-cron")


def test_parse_cron_rejects_unknown_preset() -> None:
    with pytest.raises(CronParseError, match="Unknown preset"):
        parse_cron("@bogus")


# --- parse_iso_at --------------------------------------------------------


def test_parse_iso_at_accepts_offset() -> None:
    dt = parse_iso_at("2026-05-15T09:00:00+08:00")
    assert dt.tzinfo is not None
    assert dt.year == 2026 and dt.hour == 9


def test_parse_iso_at_accepts_z_suffix() -> None:
    dt = parse_iso_at("2026-05-15T01:00:00Z")
    assert dt.tzinfo is not None
    assert dt.astimezone(UTC) == datetime(2026, 5, 15, 1, 0, tzinfo=UTC)


def test_parse_iso_at_rejects_naive_datetime() -> None:
    with pytest.raises(CronParseError, match="timezone"):
        parse_iso_at("2026-05-15T09:00:00")


def test_parse_iso_at_rejects_garbage() -> None:
    with pytest.raises(CronParseError, match="Invalid ISO-8601"):
        parse_iso_at("not-a-timestamp")


def test_parse_iso_at_rejects_empty() -> None:
    with pytest.raises(CronParseError, match="must not be empty"):
        parse_iso_at("   ")


def test_parse_iso_at_rejects_non_string() -> None:
    with pytest.raises(CronParseError, match="Expected ISO-8601 string"):
        parse_iso_at(12345)  # type: ignore[arg-type]


def test_matches_ors_restricted_day_of_month_and_day_of_week() -> None:
    # POSIX day rule: with BOTH day fields restricted, either match fires.
    expr = parse_cron("0 0 13 * 5")
    monday_13th = datetime(2026, 4, 13, 0, 0, tzinfo=UTC)
    friday_10th = datetime(2026, 4, 10, 0, 0, tzinfo=UTC)

    assert expr.matches(monday_13th)
    assert expr.matches(friday_10th)


def test_matches_ands_day_fields_when_only_one_is_restricted() -> None:
    dom_only = parse_cron("0 0 13 * *")
    dow_only = parse_cron("0 0 * * 5")
    monday_13th = datetime(2026, 4, 13, 0, 0, tzinfo=UTC)

    assert dom_only.matches(monday_13th)
    assert not dow_only.matches(monday_13th)


def test_next_run_uses_posix_day_union() -> None:
    from opensquilla.scheduler.engine import _next_run

    expr = parse_cron("0 9 1,15 * 1")
    after = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)

    assert _next_run(expr, after) == datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
