"""Shared input helpers for meta-skill invocations."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from opensquilla.meta_preflight_protocol import (
    PREFLIGHT_CONFIRMED_RE as _PREFLIGHT_CONFIRMED_RE,
)
from opensquilla.meta_preflight_protocol import (
    PREFLIGHT_FIELDS_RE as _PREFLIGHT_FIELDS_RE,
)
from opensquilla.meta_preflight_protocol import (
    PREFLIGHT_RUN_ID_RE as _PREFLIGHT_RUN_ID_RE,
)
from opensquilla.meta_preflight_protocol import (
    decode_preflight_fields as _decode_preflight_fields,
)
from opensquilla.meta_preflight_protocol import (
    display_text_from_preflight_confirmation as display_text_from_preflight_confirmation,
)
from opensquilla.meta_preflight_protocol import (
    strip_preflight_confirmation_protocol_text as strip_preflight_confirmation_protocol_text,
)

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_EXPLICIT_CHINESE_RE = re.compile(
    r"\b(?:in|to|into|as)\s+(?:simplified\s+)?(?:chinese|mandarin)\b"
    r"|\b(?:simplified\s+)?chinese\b"
    r"|中文|简体|普通话",
    re.IGNORECASE,
)
_EXPLICIT_ENGLISH_RE = re.compile(
    r"\b(?:in|to|into|as)\s+english\b|\benglish[- ]only\b|\benglish\b",
    re.IGNORECASE,
)
_SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|auth|bearer|credential|password|secret|token)",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"^(?:sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+|gh[opsu]_[A-Za-z0-9_]{8,})$",
    re.IGNORECASE,
)
_LANGUAGE_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _.-]{0,63}$")


def _clean_optional_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clean_metadata_text(value: Any, *, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if not cleaned:
        return ""
    if _SECRET_VALUE_RE.search(cleaned):
        return ""
    return cleaned[:max_chars]


def _clean_preferences(preferences: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(preferences, Mapping):
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in preferences.items():
        clean_key = _clean_optional_text(key)
        if not clean_key:
            continue
        if _SECRET_KEY_RE.search(clean_key):
            continue
        if isinstance(value, str):
            value = value.strip()
        if value in ("", None):
            continue
        if isinstance(value, str) and _SECRET_VALUE_RE.search(value):
            continue
        cleaned[clean_key] = value
    return cleaned


def meta_input_overrides_from_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Extract optional audience/language/preference metadata for meta inputs."""

    if not isinstance(metadata, Mapping):
        return {}
    raw_preferences = metadata.get("meta_preferences")
    preferences = _clean_preferences(raw_preferences)
    audience = _clean_metadata_text(metadata.get("meta_audience"), max_chars=256)
    language = _clean_metadata_text(metadata.get("meta_language"), max_chars=64)
    overrides: dict[str, Any] = {}
    if audience:
        overrides["audience"] = audience
    if language:
        overrides["language"] = language
    if preferences:
        overrides["preferences"] = preferences
    return overrides


def system_prompt_input(system_prompt: Any) -> str:
    """Serialize the live system prompt into a meta-skill template input."""

    if system_prompt is None:
        return ""
    if isinstance(system_prompt, tuple):
        parts = [str(part) for part in system_prompt if part]
        return "\n\n".join(parts)
    return str(system_prompt)


def detect_user_language(user_message: str) -> str:
    """Return the best-effort output language for a meta-skill request."""

    text = user_message or ""
    if _EXPLICIT_CHINESE_RE.search(text):
        return "zh"
    if _EXPLICIT_ENGLISH_RE.search(text):
        return "en"
    if _CJK_RE.search(text):
        return "zh"
    if _LATIN_RE.search(text):
        return "en"
    return "same"


def language_instruction_for_user_message(user_message: str) -> str:
    """Build the global language guard injected into meta-skill text steps."""

    return language_instruction_for_detected_language(detect_user_language(user_message))


def language_instruction_for_detected_language(language: str) -> str:
    """Build the global language guard for a normalized language bucket."""

    if language == "zh":
        return (
            "Output language rule: write final user-facing prose, headings, "
            "labels, and summaries in Simplified Chinese unless the user "
            "explicitly asks for another language. Do not switch to English "
            "because a template includes English examples; machine-readable "
            "protocol labels may stay in their required format."
        )
    if language == "en":
        return (
            "Output language rule: write final user-facing prose, headings, "
            "labels, and summaries in English only unless the user explicitly "
            "asks for another language. Do not copy Chinese or bilingual "
            "headings from meta-skill templates; translate template examples "
            "to English. Non-user-facing protocol labels may stay in their "
            "required format, and source quotations may preserve their "
            "original language."
        )
    return (
        "Output language rule: match the user's language for final "
        "user-facing prose, headings, labels, and summaries. Treat bilingual "
        "template examples as examples, not required output text. "
        "Non-user-facing protocol labels may stay in their required format."
    )


def _language_instruction_for_preference(language: str) -> str | None:
    preferred = _preferred_language_bucket(language)
    if preferred in {"zh", "en"}:
        return language_instruction_for_detected_language(preferred)
    normalized = language.strip().lower()
    if not normalized:
        return None
    if not _LANGUAGE_CODE_RE.match(language):
        return None
    language_label = re.sub(r"\s+", " ", language).strip()[:64]
    return (
        "Output language rule: write final user-facing prose, headings, "
        f"labels, and summaries in {language_label} unless the user "
        "explicitly asks for another language. Non-user-facing protocol "
        "labels may stay in their required format."
    )


def _preferred_language_bucket(language: str) -> str | None:
    normalized = language.strip().lower()
    if not normalized:
        return None
    if (
        normalized.startswith("zh")
        or normalized in {"cn", "mandarin", "simplified chinese"}
        or "chinese" in normalized
        or "中文" in language
        or "简体" in language
    ):
        return "zh"
    if normalized.startswith("en") or "english" in normalized:
        return "en"
    return None


def apply_clarify_language_preference(
    inputs: dict[str, Any],
    filled_fields: Mapping[str, Any],
) -> None:
    """Update meta language guards from structured clarify answers."""

    text_values = " ".join(
        str(value)
        for key, value in filled_fields.items()
        if key != "language" and value not in ("", None)
    )
    detected = detect_user_language(text_values)
    bucket: str | None
    if detected == "zh":
        bucket = "zh"
    else:
        raw_language = _clean_optional_text(filled_fields.get("language"))
        bucket = _preferred_language_bucket(raw_language) if raw_language else None
        if bucket is None and detected == "en":
            bucket = "en"
    if bucket is None:
        return
    inputs["user_language"] = bucket
    inputs["language_instruction"] = language_instruction_for_detected_language(bucket)


def make_meta_inputs(
    *,
    user_message: str,
    system_prompt: Any = "",
    audience: Any = None,
    language: Any = None,
    preferences: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the common input map visible to meta-skill Jinja templates."""

    meta_preflight_confirmed = bool(_PREFLIGHT_CONFIRMED_RE.search(user_message or ""))
    run_id_match = _PREFLIGHT_RUN_ID_RE.search(user_message or "")
    fields_match = _PREFLIGHT_FIELDS_RE.search(user_message or "")
    stripped_user_message = strip_preflight_confirmation_protocol_text(user_message or "")
    if stripped_user_message is not None:
        clean_user_message = stripped_user_message
    else:
        clean_user_message = _PREFLIGHT_CONFIRMED_RE.sub("\n", user_message or "")
        clean_user_message = _PREFLIGHT_RUN_ID_RE.sub("\n", clean_user_message).strip()
        clean_user_message = _PREFLIGHT_FIELDS_RE.sub("\n", clean_user_message).strip()
    user_language = detect_user_language(clean_user_message)
    preflight_fields = (
        _decode_preflight_fields(fields_match.group(1))
        if fields_match
        else {}
    )
    preference_values = _clean_preferences(preferences)
    audience_value = (
        _clean_optional_text(audience)
        or _clean_optional_text(preference_values.get("audience"))
        or _clean_optional_text(preference_values.get("audience_profile"))
    )
    language_value = (
        _clean_optional_text(language)
        or _clean_optional_text(preference_values.get("language"))
        or _clean_optional_text(preference_values.get("preferred_language"))
    )
    inputs: dict[str, Any] = {
        "user_message": clean_user_message,
        "user_language": user_language,
        "language_instruction": language_instruction_for_user_message(clean_user_message),
        "system_prompt": system_prompt_input(system_prompt),
        # Populated by MetaOrchestrator.resume() in PR3; downstream
        # template authors address structured user_input values as
        # `inputs.collected.<step_id>.<field>` (see design §5.3).
        "collected": {},
    }
    if audience_value:
        inputs["audience"] = audience_value
        preference_values.setdefault("audience", audience_value)
    if language_value:
        inputs["language"] = language_value
        preferred_language = _preferred_language_bucket(language_value)
        if preferred_language:
            user_language = preferred_language
            inputs["user_language"] = user_language
        preferred_instruction = _language_instruction_for_preference(language_value)
        if preferred_instruction:
            inputs["language_instruction"] = preferred_instruction
        preference_values.setdefault("language", language_value)
    if preference_values:
        inputs["preferences"] = preference_values
    if meta_preflight_confirmed:
        inputs["meta_preflight_confirmed"] = True
    if run_id_match:
        inputs["meta_preflight_run_id"] = run_id_match.group(1)
    if preflight_fields:
        inputs["meta_preflight_fields"] = preflight_fields
        inputs["collected"]["preflight"] = preflight_fields
    return inputs
