"""Surface-agnostic JSON protocol for user_input form schemas.

When a meta-skill pauses at a ``user_input`` step, the runtime needs to
hand the form description to one of three surfaces (Web / CLI / IM)
*without* leaking implementation-internal types. This module produces a
stable, JSON-safe dict that all surfaces can render against.

The protocol is intentionally minimal:
- Only fields the renderer needs (name, type, prompt, required,
  defaults, choices, range, length).
- All user-facing text (``prompt``, ``intro``) is XML-escaped at the
  boundary so surface templates that embed the strings in HTML or
  XML-shaped tool descriptions cannot be injected.
- The same payload shape is consumed by:
  - ``gateway/rpc_chat.py`` (PR5) — emits as
    ``session.event.meta_clarify_request``
  - ``cli/repl/*`` (PR6) — prompts via ``prompt-toolkit``
  - ``channels/*`` (PR7) — renders as plain text fallback

Cross-references:
- Design §9 — Surface Renderers
- Design §10 — Error Handling (the protocol does NOT carry validation
  errors; those go on a separate ``meta_clarify_errors`` event)
"""

from __future__ import annotations

import re
from typing import Any

from opensquilla.skills.meta.types import ClarifyField, ClarifyStepConfig


def _xml_escape(text: str) -> str:
    """Minimal XML/HTML escape applied to author-supplied text.

    Mirrors the existing escape in ``opensquilla.skills.injector`` so a
    single string can be re-rendered into the meta-skill catalogue, a
    WebSocket payload, or a plain-text bot message without double
    escaping.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_CHOICE_LABELS_ZH: dict[str, str] = {
    "en": "英文",
    "zh": "中文",
    "ja": "日文",
    "other": "其他",
    "mixed": "中英混合",
    "YES": "是",
    "NO": "否",
    "LAST_WEEK": "过去一周",
    "LAST_MONTH": "过去一个月",
    "LAST_QUARTER": "过去一个季度",
    "ENTRY": "初级",
    "MID": "中级",
    "SENIOR": "高级",
    "STAFF": "专家级",
    "PRE_K": "学龄前（3-5 岁）",
    "EARLY_GRADE": "小学低年级（6-9 岁）",
    "TWEEN": "小学高年级/初中前（10-12 岁）",
    "TEEN": "青少年（13-17 岁）",
    "SHOESTRING": "低预算",
    "MODEST": "适中预算",
    "COMFORTABLE": "宽裕预算",
    "SOLO": "孩子独立完成",
    "LIGHT": "家长偶尔帮忙",
    "HANDS_ON": "家长全程陪同",
    "budget": "低预算",
    "mid": "中等",
    "premium": "高预算",
    "academic": "学术读者",
    "technical": "技术读者",
    "business": "商业读者",
    "general": "普通读者",
    "FULL_MANUSCRIPT": "完整论文",
    "COMPACT_SKELETON": "快速草稿",
    "readable_pdf": "可读取 PDF",
    "inline_excerpts_only": "仅使用粘贴摘录",
    "reference_only": "只有引用/文件名",
}


def _humanize_choice(choice: str) -> str:
    text = re.sub(r"[_-]+", " ", choice).strip()
    if not text:
        return choice
    if choice.isupper() or "_" in choice or "-" in choice:
        return text.capitalize()
    return text


def _choice_label(choice: str, language: str) -> str:
    lang = language.lower()
    if lang.startswith("zh"):
        return _CHOICE_LABELS_ZH.get(choice, choice)
    if lang.startswith("en"):
        return _humanize_choice(choice)
    return choice


def _choice_options(choices: tuple[str, ...], language: str) -> list[dict[str, str]]:
    return [
        {"value": choice, "label": _xml_escape(_choice_label(choice, language))}
        for choice in choices
    ]


def field_to_protocol(field: ClarifyField, *, language: str = "") -> dict[str, Any]:
    """Convert one ClarifyField into the stable JSON shape surfaces consume.

    Only the keys the renderer needs are exposed; defaults / range /
    length appear only when the author set them, so the payload size
    stays minimal for fields with no constraints.
    """

    payload: dict[str, Any] = {
        "name": field.name,
        "type": field.type,
        "required": field.required,
        "prompt": _xml_escape(field.prompt),
    }
    if field.choices:
        payload["choices"] = list(field.choices)
        payload["options"] = _choice_options(field.choices, language)
    if field.default is not None:
        payload["default"] = field.default
    if field.min is not None:
        payload["min"] = field.min
    if field.max is not None:
        payload["max"] = field.max
    if field.max_chars is not None:
        payload["max_chars"] = field.max_chars
    return payload


def schema_to_protocol(
    schema: ClarifyStepConfig,
    *,
    intro_override: str = "",
    language: str = "",
    confirmed_fields: dict[str, Any] | None = None,
    prefill_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert a ClarifyStepConfig to a JSON-safe surface payload.

    ``intro_override`` lets the caller (executor / orchestrator) supply
    a step-specific intro line that takes precedence over the schema's
    own intro — useful when the step body has author-customised
    pre-form text.

    ``confirmed_fields`` and ``prefill_audit`` carry the step-(c)
    auto-prefill payload to the surface so the user sees "we noticed
    X from your earlier message — please confirm" instead of being
    asked to re-enter information they already supplied. Both default
    to ``None``; callers without a prefill scan get the historical
    payload shape unchanged.

    Returns a dict with these keys (all serialisable):
      ``mode``           — "form" | "chat"
      ``intro``          — XML-escaped intro string (may be empty)
      ``fields``         — list of field protocol dicts
      ``cancel_keywords`` — tuple-as-list of normalised cancel words
      ``timeout_hours``  — int
      ``nl_extract``     — bool (informational; surfaces don't render
                            this differently, but operators may inspect)
      ``confirmed_fields`` — list of {name, value, source, reason?} —
                            entries the system inferred from earlier
                            context. Surfaces should render these as
                            pre-filled values requiring confirmation.
      ``ambiguous_fields`` — list of {name, reason} — fields the
                            inference saw mentions of but could not
                            pin down. Surfaces should highlight these
                            in the form.
      ``unknown_mentions`` — list of {text, guess?} — user-mentioned
                            spans that did not map to any schema
                            field. Surfaces may show "we noticed you
                            also said: ..." for transparency.
    """

    intro_source = intro_override if intro_override else schema.intro
    payload: dict[str, Any] = {
        "mode": schema.mode,
        "intro": _xml_escape(intro_source),
        "fields": [field_to_protocol(f, language=language) for f in schema.fields],
        "cancel_keywords": list(schema.cancel_keywords),
        "timeout_hours": schema.timeout_hours,
        "nl_extract": schema.nl_extract,
    }

    confirmed = confirmed_fields or {}
    audit = prefill_audit or {}
    if confirmed or audit:
        # The schema's field whitelist guards against an audit record
        # naming a non-schema field (defence in depth — the executor
        # already validates against the same schema).
        valid_names = {f.name for f in schema.fields}
        payload["confirmed_fields"] = _confirmed_field_entries(
            confirmed, audit, valid_names,
        )
        payload["ambiguous_fields"] = _ambiguous_field_entries(
            audit, valid_names,
        )
        payload["unknown_mentions"] = _unknown_mention_entries(audit)
    return payload


def _confirmed_field_entries(
    confirmed: dict[str, Any],
    audit: dict[str, Any],
    valid_names: set[str],
) -> list[dict[str, Any]]:
    """Render ``confirmed_fields`` from the (name, value) pairs the
    executor already extracted, attributing each entry to the prefill
    source recorded in the audit payload."""
    source = str(audit.get("source") or "auto_prefill")
    out: list[dict[str, Any]] = []
    for name in audit.get("fields") or list(confirmed.keys()):
        if not isinstance(name, str) or name not in valid_names:
            continue
        if name not in confirmed:
            continue
        entry: dict[str, Any] = {
            "name": name,
            "value": confirmed[name],
            "source": source,
        }
        out.append(entry)
    return out


def _ambiguous_field_entries(
    audit: dict[str, Any],
    valid_names: set[str],
) -> list[dict[str, Any]]:
    raw = audit.get("ambiguous") or []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("field")
        if not isinstance(name, str) or name not in valid_names:
            continue
        out.append({
            "name": name,
            "reason": _xml_escape(str(entry.get("reason") or "")),
        })
    return out


def _unknown_mention_entries(audit: dict[str, Any]) -> list[dict[str, Any]]:
    raw = audit.get("unknown_mentions") or []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, str):
            out.append({"text": _xml_escape(entry)})
        elif isinstance(entry, dict):
            text = entry.get("text") or entry.get("mention")
            if not isinstance(text, str) or not text:
                continue
            payload: dict[str, Any] = {"text": _xml_escape(text)}
            guess = entry.get("guess") or entry.get("hint")
            if isinstance(guess, str) and guess:
                payload["guess"] = _xml_escape(guess)
            out.append(payload)
    return out
