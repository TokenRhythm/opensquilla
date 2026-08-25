"""Deterministic reply parser tests (PR4, design §9.3 + §10)."""

from __future__ import annotations

from opensquilla.skills.meta.clarify_text import parse_clarify_reply
from opensquilla.skills.meta.types import ClarifyField, ClarifyStepConfig


def _schema(*fields: ClarifyField) -> ClarifyStepConfig:
    return ClarifyStepConfig(mode="form", fields=tuple(fields))


# ── Mode 1: key:value lines ──

def test_key_value_simple():
    schema = _schema(
        ClarifyField(name="destination", type="string", required=True),
        ClarifyField(name="days", type="int", required=True, min=1, max=14),
    )
    fields, errors = parse_clarify_reply(
        "destination: Tokyo\ndays: 5", schema, surface="cli",
    )
    assert errors == []
    assert fields == {"destination": "Tokyo", "days": 5}


def test_key_value_fullwidth_colon():
    schema = _schema(ClarifyField(name="destination", type="string", required=True))
    fields, errors = parse_clarify_reply(
        "destination：东京", schema, surface="im",
    )
    assert errors == []
    assert fields == {"destination": "东京"}


def test_key_value_cjk_value():
    schema = _schema(ClarifyField(name="destination", type="string", required=True))
    fields, errors = parse_clarify_reply(
        "destination: 上海五日游", schema, surface="cli",
    )
    assert errors == []
    assert fields == {"destination": "上海五日游"}


def test_key_value_unknown_key_rejected():
    schema = _schema(ClarifyField(name="destination", type="string", required=True))
    fields, errors = parse_clarify_reply(
        "destination: Tokyo\nbogus: x", schema, surface="cli",
    )
    assert fields == {}
    assert any("bogus" in e for e in errors)


def test_key_value_partial_missing_required():
    schema = _schema(
        ClarifyField(name="destination", type="string", required=True),
        ClarifyField(name="days", type="int", required=True),
    )
    fields, errors = parse_clarify_reply(
        "destination: Tokyo", schema, surface="cli",
    )
    assert fields == {}
    assert any("days" in e and "required" in e.lower() for e in errors)


def test_key_value_extra_whitespace_tolerated():
    schema = _schema(ClarifyField(name="destination", type="string", required=True))
    fields, errors = parse_clarify_reply(
        "  destination  :   Tokyo   ", schema, surface="cli",
    )
    assert errors == []
    assert fields == {"destination": "Tokyo"}


# ── Mode 2: numbered lines ──

def test_numbered_lines_paren():
    schema = _schema(
        ClarifyField(name="destination", type="string", required=True),
        ClarifyField(name="days", type="int", required=True),
    )
    fields, errors = parse_clarify_reply(
        "1) Tokyo\n2) 5", schema, surface="cli",
    )
    assert errors == []
    assert fields == {"destination": "Tokyo", "days": 5}


def test_numbered_lines_dot():
    schema = _schema(
        ClarifyField(name="destination", type="string", required=True),
        ClarifyField(name="days", type="int", required=True),
    )
    fields, errors = parse_clarify_reply(
        "1. Tokyo\n2. 5", schema, surface="cli",
    )
    assert errors == []
    assert fields == {"destination": "Tokyo", "days": 5}


def test_numbered_out_of_range():
    schema = _schema(ClarifyField(name="x", type="string", required=True))
    fields, errors = parse_clarify_reply("5) bogus", schema, surface="cli")
    assert fields == {}
    assert any("5" in e for e in errors)


# ── Mode 3: positional ──

def test_positional_simple():
    schema = _schema(
        ClarifyField(name="destination", type="string", required=True),
        ClarifyField(name="days", type="int", required=True),
    )
    fields, errors = parse_clarify_reply(
        "Tokyo\n5", schema, surface="cli",
    )
    assert errors == []
    assert fields == {"destination": "Tokyo", "days": 5}


def test_positional_fewer_lines_than_fields():
    schema = _schema(
        ClarifyField(name="destination", type="string", required=True),
        ClarifyField(name="days", type="int", required=True),
    )
    fields, errors = parse_clarify_reply("Tokyo", schema, surface="cli")
    assert fields == {}
    assert any("days" in e and "required" in e.lower() for e in errors)


def test_positional_more_lines_than_fields():
    # Two-field schema where the second line genuinely overflows.
    schema = _schema(
        ClarifyField(name="x", type="string", required=True),
        ClarifyField(name="y", type="int", required=True),
    )
    fields, errors = parse_clarify_reply(
        "Tokyo\n42\nextra", schema, surface="cli",
    )
    assert fields == {}
    assert any("too many" in e.lower() for e in errors)


def test_positional_multiline_collapses_into_single_string_field():
    # When the schema is a single string field (the catch-all pattern
    # used by free-form review steps), additional lines should be
    # joined into the same field rather than reported as overflow.
    schema = _schema(ClarifyField(name="x", type="string", required=True))
    fields, errors = parse_clarify_reply(
        "Tokyo\nextra", schema, surface="cli",
    )
    assert errors == []
    assert fields == {"x": "Tokyo\nextra"}


# ── Hybrid rejection ──

def test_hybrid_key_value_and_positional_rejected():
    schema = _schema(
        ClarifyField(name="destination", type="string", required=True),
        ClarifyField(name="days", type="int", required=True),
    )
    fields, errors = parse_clarify_reply(
        "destination: Tokyo\n5", schema, surface="cli",
    )
    assert fields == {}
    assert any("mixed" in e.lower() for e in errors)


# ── Per-field validation ──

def test_int_field_non_numeric_rejected():
    schema = _schema(ClarifyField(name="days", type="int", required=True, min=1, max=14))
    fields, errors = parse_clarify_reply("days: abc", schema, surface="cli")
    assert fields == {}
    assert any("integer" in e.lower() for e in errors)


def test_int_field_below_min_rejected():
    schema = _schema(ClarifyField(name="days", type="int", required=True, min=1, max=14))
    fields, errors = parse_clarify_reply("days: 0", schema, surface="cli")
    assert fields == {}
    assert any("min" in e.lower() for e in errors)


def test_int_field_above_max_rejected():
    schema = _schema(ClarifyField(name="days", type="int", required=True, min=1, max=14))
    fields, errors = parse_clarify_reply("days: 100", schema, surface="cli")
    assert fields == {}
    assert any("max" in e.lower() for e in errors)


def test_enum_field_invalid_rejected():
    schema = _schema(
        ClarifyField(name="budget", type="enum", required=True,
                     choices=("budget", "mid", "premium")),
    )
    fields, errors = parse_clarify_reply("budget: luxury", schema, surface="cli")
    assert fields == {}
    assert any("budget" in e and "luxury" in e for e in errors)


def test_enum_field_valid():
    schema = _schema(
        ClarifyField(name="budget", type="enum", required=True,
                     choices=("budget", "mid", "premium")),
    )
    fields, errors = parse_clarify_reply("budget: mid", schema, surface="cli")
    assert errors == []
    assert fields == {"budget": "mid"}


def test_string_field_exceeds_max_chars_rejected():
    schema = _schema(
        ClarifyField(name="notes", type="string", required=True, max_chars=10),
    )
    fields, errors = parse_clarify_reply(
        "notes: " + ("x" * 50), schema, surface="cli",
    )
    assert fields == {}
    assert any("max_chars" in e or "length" in e.lower() for e in errors)


def test_bool_field_true_variants():
    schema = _schema(ClarifyField(name="b", type="bool", required=True))
    for s in ("true", "True", "yes", "1"):
        fields, errors = parse_clarify_reply(f"b: {s}", schema, surface="cli")
        assert errors == [], s
        assert fields == {"b": True}, s


def test_bool_field_false_variants():
    schema = _schema(ClarifyField(name="b", type="bool", required=True))
    for s in ("false", "False", "no", "0"):
        fields, errors = parse_clarify_reply(f"b: {s}", schema, surface="cli")
        assert errors == [], s
        assert fields == {"b": False}, s


def test_bool_field_invalid_rejected():
    schema = _schema(ClarifyField(name="b", type="bool", required=True))
    fields, errors = parse_clarify_reply("b: maybe", schema, surface="cli")
    assert fields == {}
    assert any("bool" in e.lower() for e in errors)


# ── Optional fields ──

def test_optional_field_with_default_omitted():
    schema = _schema(
        ClarifyField(name="destination", type="string", required=True),
        ClarifyField(name="budget", type="enum",
                     choices=("budget", "mid"), default="mid"),
    )
    fields, errors = parse_clarify_reply("destination: Tokyo", schema, surface="cli")
    assert errors == []
    assert fields == {"destination": "Tokyo"}


def test_optional_field_without_default_omitted_is_ok():
    schema = _schema(
        ClarifyField(name="destination", type="string", required=True),
        ClarifyField(name="notes", type="string", required=False),
    )
    fields, errors = parse_clarify_reply("destination: Tokyo", schema, surface="cli")
    assert errors == []
    assert fields == {"destination": "Tokyo"}


# ── Edge cases ──

def test_empty_reply_with_required_fields_errors():
    schema = _schema(ClarifyField(name="x", type="string", required=True))
    fields, errors = parse_clarify_reply("", schema, surface="cli")
    assert fields == {}
    assert errors


def test_whitespace_only_reply_with_required_fields_errors():
    schema = _schema(ClarifyField(name="x", type="string", required=True))
    fields, errors = parse_clarify_reply("   \n\n  ", schema, surface="cli")
    assert fields == {}
    assert errors


def test_empty_value_after_colon():
    schema = _schema(ClarifyField(name="x", type="string", required=True))
    fields, errors = parse_clarify_reply("x:", schema, surface="cli")
    assert fields == {}
    assert any("x" in e and ("empty" in e.lower() or "required" in e.lower()) for e in errors)


# ── F4: CJK + conversational bool synonyms ──


import pytest  # noqa: E402  — kept at section boundary for grouping


@pytest.mark.parametrize(
    "value,expected",
    [
        ("好", True),
        ("对", True),
        ("嗯", True),
        ("可以", True),
        ("确认", True),
        ("没问题", True),
        ("ok", True),
        ("OK", True),
        ("不", False),
        ("不要", False),
        ("不行", False),
        ("不用", False),
        ("算了", False),
        # Pre-existing literals must still work.
        ("是", True),
        ("yes", True),
        ("否", False),
        ("no", False),
    ],
)
def test_bool_field_accepts_cjk_and_conversational_synonyms(value, expected) -> None:
    """F4: chat-mode users naturally answer bool prompts with 好 / 对 /
    可以 / 算了 / 不用. Before this contract those replies fell through
    to "not a bool" and triggered a re-prompt, surfacing as the
    "information not understood" symptom. ``_TRUE_VALUES`` /
    ``_FALSE_VALUES`` in ``clarify_text.py`` must accept the full
    conversational set."""
    schema = _schema(ClarifyField(name="ok", type="bool", required=True))
    fields, errors = parse_clarify_reply(f"ok: {value}", schema, surface="cli")
    assert errors == [], f"unexpected errors for {value!r}: {errors}"
    assert fields == {"ok": expected}


def test_bool_field_rejects_unknown_token() -> None:
    """F4 negative control: the synonym tables must NOT silently
    coerce arbitrary CJK or English words to True/False. Only the
    enumerated synonym set is accepted."""
    schema = _schema(ClarifyField(name="ok", type="bool", required=True))
    fields, errors = parse_clarify_reply("ok: maybe", schema, surface="cli")
    assert fields == {}
    assert errors, "expected a parse error for an unknown bool token"
