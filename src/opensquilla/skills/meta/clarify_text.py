"""Deterministic plain-text parser for user_input step replies.

Replaces the PR3 stub at ``opensquilla.engine.steps.clarify_reply_parser_stub``.
Accepts three reply modes:

1. ``key: value`` lines (most natural; full-width ``：`` or half-width ``:``)
2. Numbered lines (``1) value`` or ``1. value``) where the index matches
   ``fields[i]``'s 1-based position
3. Positional plain-text lines (one value per field, in declaration order,
   no prefix)

Hybrid replies (some ``key: value``, some positional) are rejected with a
clear error. This module never invokes an LLM — that's the opt-in
``clarify_nl_extract`` path for PR9.

Design: §9.3 (reply formats) + §10 (error handling) + §5.2 (per-field
validators reapplied to user input).
"""

from __future__ import annotations

import re
from typing import Any

from opensquilla.skills.meta.types import ClarifyField, ClarifyStepConfig

# Half-width or full-width colon, with arbitrary whitespace around.
# Group 1 captures the key, group 2 captures the value (may be empty).
_KEY_VALUE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*[:：]\s*(.*?)\s*$")

# Numbered line: 1) value  or  1. value
_NUMBERED_RE = re.compile(r"^\s*(\d+)\s*[\)\.]\s*(.*?)\s*$")

# Bool synonym tables. CJK affirmative/negative words ("好", "对",
# "可以", "确认", "不要", "不用", "算了") used to silently fall through
# to "not a bool" because the table only carried "是" / "否". Users
# reading the prompt naturally answered with the conversational
# equivalents, hit a parse error, and were re-prompted — the
# "information not understood" symptom in chat-mode clarify. Keep the
# CLI ``clarify_form.py`` table in lockstep with this set; the two
# surfaces must accept the same vocabulary or a multi-surface skill
# reports inconsistent outcomes.
_TRUE_VALUES = frozenset({
    "true", "yes", "1", "on",
    "是", "好", "对", "嗯", "可以", "确认", "没问题", "ok",
})
_FALSE_VALUES = frozenset({
    "false", "no", "0", "off",
    "否", "不", "不要", "不行", "不用", "算了",
})


def parse_clarify_reply(
    message: str,
    schema: ClarifyStepConfig,
    *,
    surface: str,
) -> tuple[dict[str, Any], list[str]]:
    """Parse a user's plain-text reply against a clarify schema.

    Returns ``(filled_dict, errors)``:
    - On success: ``(dict_of_field_values, [])``
    - On failure: ``({}, [error_strings])``
    """
    if not message or not message.strip():
        missing = [
            f"required field {f.name!r}: no value provided"
            for f in schema.fields if f.required
        ]
        return {}, missing or ["empty reply"]

    lines = [line for line in message.splitlines() if line.strip()]

    kv_matches = [_KEY_VALUE_RE.match(line) for line in lines]
    num_matches = [_NUMBERED_RE.match(line) for line in lines]

    has_kv = any(m is not None for m in kv_matches)
    has_num = any(m is not None for m in num_matches)
    all_kv = has_kv and all(m is not None for m in kv_matches)
    all_num = has_num and all(m is not None for m in num_matches)

    if has_kv and not all_kv:
        return {}, [
            "mixed reply formats: some lines look like 'key: value' but "
            "others don't. Use ONE format consistently (all key:value, "
            "all numbered, or all positional values).",
        ]
    if has_num and not all_num and not all_kv:
        return {}, [
            "mixed reply formats: some lines look like '1) value' but "
            "others don't. Use ONE format consistently.",
        ]

    if all_kv:
        return _parse_key_value(kv_matches, schema)
    if all_num:
        return _parse_numbered(num_matches, schema)
    return _parse_positional(lines, schema)


def _parse_key_value(matches: list, schema: ClarifyStepConfig) -> tuple[dict[str, Any], list[str]]:
    fields_by_name = {f.name: f for f in schema.fields}
    parsed: dict[str, Any] = {}
    errors: list[str] = []

    for m in matches:
        key, raw_value = m.group(1), m.group(2)
        field = fields_by_name.get(key)
        if field is None:
            errors.append(
                f"unknown field {key!r}; valid fields: "
                f"{', '.join(f.name for f in schema.fields)}",
            )
            continue
        coerced, field_errors = _coerce_and_validate(field, raw_value)
        if field_errors:
            errors.extend(field_errors)
        else:
            parsed[field.name] = coerced

    errors.extend(_check_required(schema, parsed))
    if errors:
        return {}, errors
    return parsed, []


def _parse_numbered(matches: list, schema: ClarifyStepConfig) -> tuple[dict[str, Any], list[str]]:
    parsed: dict[str, Any] = {}
    errors: list[str] = []

    for m in matches:
        idx_1based = int(m.group(1))
        raw_value = m.group(2)
        if idx_1based < 1 or idx_1based > len(schema.fields):
            errors.append(
                f"line numbered {idx_1based} is out of range; "
                f"schema has {len(schema.fields)} fields",
            )
            continue
        field = schema.fields[idx_1based - 1]
        coerced, field_errors = _coerce_and_validate(field, raw_value)
        if field_errors:
            errors.extend(field_errors)
        else:
            parsed[field.name] = coerced

    errors.extend(_check_required(schema, parsed))
    if errors:
        return {}, errors
    return parsed, []


def _parse_positional(
    lines: list[str], schema: ClarifyStepConfig,
) -> tuple[dict[str, Any], list[str]]:
    # Catch-all shortcut: schemas with a single string field (commonly
    # paired with nl_extract: true as a free-form review surface) should
    # accept a multi-line reply as one blob, not error with "too many
    # lines". This is what callers mean by "the entire user reply is
    # the value" and matches how the nl_extract LLM is instructed to
    # behave when it cannot decompose the reply.
    if (
        len(schema.fields) == 1
        and schema.fields[0].type == "string"
        and len(lines) > 1
    ):
        field = schema.fields[0]
        coerced, field_errors = _coerce_and_validate(
            field, "\n".join(lines).strip(),
        )
        if field_errors:
            return {}, field_errors
        return {field.name: coerced}, []

    if len(lines) > len(schema.fields):
        return {}, [
            f"too many lines: got {len(lines)}, schema has only "
            f"{len(schema.fields)} fields. Use 'key: value' format if you "
            f"meant to skip an earlier field.",
        ]

    parsed: dict[str, Any] = {}
    errors: list[str] = []
    for i, raw_value in enumerate(lines):
        field = schema.fields[i]
        coerced, field_errors = _coerce_and_validate(field, raw_value.strip())
        if field_errors:
            errors.extend(field_errors)
        else:
            parsed[field.name] = coerced

    errors.extend(_check_required(schema, parsed))
    if errors:
        return {}, errors
    return parsed, []


def _check_required(schema: ClarifyStepConfig, parsed: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for f in schema.fields:
        if f.required and f.name not in parsed:
            out.append(f"required field {f.name!r}: no value provided")
    return out


def _coerce_and_validate(field: ClarifyField, raw_value: str) -> tuple[Any, list[str]]:
    """Coerce raw text to field.type and run min/max/max_chars/choices check."""
    value = raw_value.strip()
    if not value:
        if field.required:
            return None, [
                f"field {field.name!r}: empty value (required field cannot "
                f"be blank)",
            ]
        return None, []

    if field.type == "string":
        if field.max_chars is not None and len(value) > field.max_chars:
            return None, [
                f"field {field.name!r}: length {len(value)} exceeds "
                f"max_chars={field.max_chars}",
            ]
        return value, []

    if field.type == "int":
        try:
            n = int(value)
        except ValueError:
            return None, [
                f"field {field.name!r}: {value!r} is not a valid integer",
            ]
        if field.min is not None and n < field.min:
            return None, [
                f"field {field.name!r}: {n} is below min={field.min}",
            ]
        if field.max is not None and n > field.max:
            return None, [
                f"field {field.name!r}: {n} is above max={field.max}",
            ]
        return n, []

    if field.type == "bool":
        low = value.lower()
        if low in _TRUE_VALUES:
            return True, []
        if low in _FALSE_VALUES:
            return False, []
        return None, [
            f"field {field.name!r}: {value!r} is not a valid bool "
            f"(use true/false, yes/no, 1/0, 是/否)",
        ]

    if field.type == "enum":
        if value not in field.choices:
            return None, [
                f"field {field.name!r}: {value!r} not in choices "
                f"{list(field.choices)}",
            ]
        return value, []

    return None, [f"field {field.name!r}: unknown type {field.type!r}"]
