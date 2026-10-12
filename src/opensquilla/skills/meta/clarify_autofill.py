"""LLM-assisted completion for missing required clarify fields.

The clarify form is a speed bump, not a dead end. When a user leaves a
required field blank or answers with an explicit delegation like "都可以",
the runtime can infer a concrete value from the original request and
continue the meta-skill instead of re-prompting forever.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from opensquilla.skills.meta.inputs import detect_user_language
from opensquilla.skills.meta.types import ClarifyField, ClarifyStepConfig

LLMChat = Callable[[str, str], Awaitable[str]]

_UNINFORMATIVE_VALUES = frozenset({
    "",
    "n/a",
    "na",
    "none",
    "null",
    "any",
    "anything",
    "whatever",
    "no preference",
    "no preferences",
    "up to you",
    "you decide",
    "not sure",
    "都可以",
    "都行",
    "随便",
    "无所谓",
    "不限",
    "任意",
    "看着办",
    "你决定",
    "帮我决定",
    "不确定",
    "不知道",
})

_TRUE_VALUES = frozenset({
    "true",
    "yes",
    "1",
    "on",
    "是",
    "好",
    "对",
    "嗯",
    "可以",
    "确认",
    "没问题",
    "ok",
})
_FALSE_VALUES = frozenset({
    "false",
    "no",
    "0",
    "off",
    "否",
    "不",
    "不要",
    "不行",
    "不用",
    "算了",
})


async def autofill_required_clarify_fields(
    *,
    schema: ClarifyStepConfig,
    filled_fields: Mapping[str, Any],
    user_message: str,
    clarify_reply: str,
    llm_chat: LLMChat | None,
    prior_step_outputs: Mapping[str, Any] | None = None,
    infer_optional_fields: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fill missing or delegated clarify fields.

    Returns ``(merged_fields, completed_fields)``. ``completed_fields`` only
    contains values inferred by this helper.

    By default this remains conservative and only fills required fields.
    ``infer_optional_fields`` is reserved for an empty structured form
    submission, where the user has delegated the whole form to the model.
    """

    merged = dict(filled_fields or {})
    targets = _target_fields(
        schema=schema,
        filled_fields=merged,
        infer_optional_fields=infer_optional_fields,
    )
    if not targets:
        return merged, {}

    completed: dict[str, Any] = {}
    if llm_chat is not None:
        llm_values = await _ask_llm_for_required_fields(
            schema=schema,
            targets=targets,
            filled_fields=merged,
            user_message=user_message,
            clarify_reply=clarify_reply,
            prior_step_outputs=prior_step_outputs or {},
            llm_chat=llm_chat,
        )
        for field in targets:
            if field.name not in llm_values:
                continue
            coerced = _coerce_candidate(field, llm_values[field.name])
            if coerced is not None:
                completed[field.name] = coerced

    for field in targets:
        if field.name in completed:
            continue
        fallback = _fallback_for_target(
            field,
            merged_fields=merged,
            user_message=user_message,
        )
        if fallback is not None:
            completed[field.name] = fallback

    merged.update(completed)
    return merged, completed


def is_empty_clarify_submission(filled_fields: Mapping[str, Any] | None) -> bool:
    """Return True when a structured clarify reply carries no real values."""

    if not filled_fields:
        return True
    for value in filled_fields.values():
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                return False
            continue
        if isinstance(value, (list, tuple, set, dict)):
            if value:
                return False
            continue
        return False
    return True


def _target_fields(
    *,
    schema: ClarifyStepConfig,
    filled_fields: Mapping[str, Any],
    infer_optional_fields: bool,
) -> list[ClarifyField]:
    if infer_optional_fields:
        return list(schema.fields)
    return [
        field for field in schema.fields
        if field.required and _is_uninformative_value(filled_fields.get(field.name))
    ]


def _is_uninformative_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _UNINFORMATIVE_VALUES
    return False


async def _ask_llm_for_required_fields(
    *,
    schema: ClarifyStepConfig,
    targets: list[ClarifyField],
    filled_fields: Mapping[str, Any],
    user_message: str,
    clarify_reply: str,
    prior_step_outputs: Mapping[str, Any],
    llm_chat: LLMChat,
) -> dict[str, Any]:
    system = (
        "You complete fields for an OpenSquilla meta-skill clarify form. "
        "Infer practical values from the available context. "
        "Do not ask the user another question. Preserve already-specific "
        "user answers. Return only one JSON object whose keys are field names."
    )
    field_specs = [
        {
            "name": field.name,
            "type": field.type,
            "required": field.required,
            "prompt": field.prompt,
            "choices": list(field.choices),
            "default": field.default,
            "min": field.min,
            "max": field.max,
            "max_chars": field.max_chars,
        }
        for field in targets
    ]
    prior = {
        key: str(value)[:1200]
        for key, value in (prior_step_outputs or {}).items()
        if str(value).strip()
    }
    user = json.dumps(
        {
            "original_user_request": user_message[:4000],
            "user_clarify_reply": clarify_reply[:1200],
            "existing_fields": dict(filled_fields),
            "fields_to_infer": field_specs,
            "prior_step_outputs": prior,
            "instructions": [
                "For enum fields, choose exactly one value from choices.",
                "For int fields, return an integer inside min/max when present.",
                "For bool fields, return true or false.",
                "For string fields, return a concise concrete value in the user's language.",
            ],
        },
        ensure_ascii=False,
    )
    raw = (await llm_chat(system, user)).strip()
    parsed = _parse_json_object(raw)
    return parsed if isinstance(parsed, dict) else {}


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def _coerce_candidate(field: ClarifyField, value: Any) -> Any | None:
    if _is_uninformative_value(value):
        return None
    if field.type == "string":
        text = str(value).strip()
        if not text:
            return None
        if field.max_chars is not None:
            text = text[: field.max_chars]
        return text
    if field.type == "enum":
        text = str(value).strip()
        if text in field.choices:
            return text
        lowered = text.lower()
        for choice in field.choices:
            if choice.lower() == lowered:
                return choice
        return None
    if field.type == "int":
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        if field.min is not None and number < field.min:
            return None
        if field.max is not None and number > field.max:
            return None
        return number
    if field.type == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE_VALUES:
            return True
        if text in _FALSE_VALUES:
            return False
        return None
    return value


def _fallback_for_target(
    field: ClarifyField,
    *,
    merged_fields: Mapping[str, Any],
    user_message: str,
) -> Any | None:
    if field.required:
        return _fallback_value(field, user_message=user_message)
    existing = merged_fields.get(field.name)
    if not _is_uninformative_value(existing):
        return None
    if field.default is not None:
        return _fallback_value(field, user_message=user_message)
    return None


def _fallback_value(field: ClarifyField, *, user_message: str = "") -> Any | None:
    if field.default is not None:
        coerced = _coerce_candidate(field, field.default)
        if coerced is not None:
            return coerced
    if field.type == "enum" and field.choices:
        return field.choices[0]
    if field.type == "int":
        if field.min is not None:
            return field.min
        if field.max is not None and field.max >= 0:
            return min(field.max, 1)
        return 1
    if field.type == "bool":
        return True
    if field.type == "string":
        if detect_user_language(user_message) == "en":
            return "Automatically inferred from context"
        return "由系统根据上下文自动补全"
    return None
