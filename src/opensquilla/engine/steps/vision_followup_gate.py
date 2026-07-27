"""Semantic gate for text-only follow-ups to historical images."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.steps.squilla_router import _attachments_include_image
from opensquilla.provider.types import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    TextDeltaEvent,
    derive_provider_request_correlation,
)

_VALID_DECISIONS = {"needs_image", "text_only", "unknown"}
_IMAGE_REF_RE = re.compile(r"\b(image|picture|photo|screenshot|screen|diagram)\b", re.I)
_ZH_IMAGE_REFS = ("图", "图片", "截图", "照片")
_IMAGE_OPTOUT_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|without|no\s+need\s+to)\b"
    r".{0,80}\b(?:use|inspect|look\s+at|view|analy[sz]e|consider)\b"
    r".{0,80}\b(?:image|picture|photo|screenshot|screen|diagram)\b"
    r"|"
    r"\bignore\b.{0,80}\b(?:image|picture|photo|screenshot|screen|diagram)\b"
    r"|"
    r"(?:不要|不用|无需|不需要|别).{0,40}(?:看|使用|参考|分析|检查).{0,40}(?:图|图片|截图|照片)"
    r"|"
    r"(?:忽略|无视).{0,40}(?:图|图片|截图|照片)",
    re.I,
)
_PREVIOUS_IMAGE_REF_RE = re.compile(
    r"\b(?:previous|last|earlier|above|that|the)\b"
    r".{0,50}\b(?:image|picture|photo|screenshot|screen|diagram)\b"
    r"|"
    r"\b(?:image|picture|photo|screenshot|screen|diagram)\b"
    r".{0,50}\b(?:above|before|earlier)\b",
    re.I,
)
_ZH_PREVIOUS_IMAGE_REFS = (
    "上一张图",
    "上一张图片",
    "上张图",
    "上张图片",
    "刚才那张图",
    "刚才那张图片",
    "前面那张图",
    "前面那张图片",
    "之前那张图",
    "之前那张图片",
    "那张图",
    "那张图片",
)


def _router_cfg(ctx: TurnContext) -> Any:
    return getattr(ctx.config, "squilla_router", None)


def _gate_enabled(ctx: TurnContext) -> bool:
    return bool(getattr(_router_cfg(ctx), "vision_followup_gate_enabled", True))


def _truncate(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    return text[-limit:] if len(text) > limit else text


def _turns_since_last_image(ctx: TurnContext) -> int | None:
    value = ctx.metadata.get("router_turns_since_last_image")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _candidate_window_expired(ctx: TurnContext) -> bool:
    turns = _turns_since_last_image(ctx)
    if turns is None:
        return False
    candidate_turns = ctx.metadata.get("router_vision_candidate_turns")
    if not isinstance(candidate_turns, int) or isinstance(candidate_turns, bool):
        return False
    return candidate_turns > 0 and turns >= candidate_turns


def _gate_payload(ctx: TurnContext) -> dict[str, Any]:
    history_user_texts = ctx.metadata.get("router_history_user_texts", [])
    if not isinstance(history_user_texts, list):
        history_user_texts = []
    return {
        "current_user_text": _truncate(ctx.semantic_message, 1200),
        "recent_user_texts": [
            _truncate(item, 600)
            for item in history_user_texts[-4:]
            if isinstance(item, str)
        ],
        "prev_assistant_text": _truncate(
            ctx.metadata.get("router_prev_assistant_text"),
            1600,
        ),
        "history_has_image": ctx.metadata.get("router_history_has_recent_image") is True,
        "turns_since_last_image": _turns_since_last_image(ctx),
        "last_image_turn_text": _truncate(
            ctx.metadata.get("router_last_image_turn_text"),
            800,
        ),
    }


def _gate_system_prompt() -> str:
    return (
        "You classify whether the current user turn requires reusing a previous image. "
        "Do not answer the user. Return only JSON with keys decision, confidence, reason. "
        "decision must be one of needs_image, text_only, unknown. "
        "Use needs_image only when text history alone is insufficient. "
        "Use text_only when the user asks a general text/task question, asks you to ignore "
        "the image, or explicitly says not to use, inspect, view, analyze, or consider the "
        "previous image, even if the word image appears. "
        "Use unknown only when the current text is ambiguous and could depend on the image."
    )


async def _call_gate_provider(ctx: TurnContext) -> str:
    chat = ctx.metadata.get("router_vision_followup_gate_chat")
    if not callable(chat):
        provider = ctx.provider
        if provider is None:
            raise RuntimeError("vision follow-up gate has no provider")
        chat = provider.chat
    cfg = _router_cfg(ctx)
    timeout = float(getattr(cfg, "vision_followup_gate_timeout_seconds", 3.0) or 3.0)
    config = ChatConfig(
        max_tokens=int(getattr(cfg, "vision_followup_gate_max_output_tokens", 120) or 120),
        temperature=0,
        timeout=timeout,
        system=_gate_system_prompt(),
        provider_request_correlation=derive_provider_request_correlation(
            ctx.provider_request_correlation,
            execution_id=uuid.uuid4().hex,
            call_kind="auxiliary.vision_gate",
        ),
    )
    messages = [
        Message(
            role="user",
            content=json.dumps(_gate_payload(ctx), ensure_ascii=False, separators=(",", ":")),
        )
    ]
    chunks: list[str] = []
    reasoning_chunks: list[str] = []
    async for event in chat(messages, tools=[], config=config):
        if isinstance(event, TextDeltaEvent):
            chunks.append(event.text)
        elif isinstance(event, ErrorEvent):
            raise RuntimeError(event.message or "vision follow-up gate provider error")
        elif isinstance(event, DoneEvent):
            if event.reasoning_content:
                reasoning_chunks.append(event.reasoning_content)
            break
    text = "".join(chunks).strip()
    if text:
        return text
    return "".join(reasoning_chunks).strip()


def _parse_gate_response(text: str) -> tuple[str, float, str]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("` \n")
        if raw.startswith("json"):
            raw = raw[4:].strip()
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end >= start:
            raw = raw[start : end + 1]
    payload = json.loads(raw)
    decision = str(payload.get("decision", "unknown")).strip()
    if decision not in _VALID_DECISIONS:
        decision = "unknown"
    confidence = float(payload.get("confidence", 0.0) or 0.0)
    confidence = max(0.0, min(1.0, confidence))
    reason = _truncate(payload.get("reason"), 300)
    return decision, confidence, reason


def _apply_gate_decision(
    ctx: TurnContext,
    *,
    decision: str,
    confidence: float,
    reason: str,
) -> None:
    if decision == "unknown":
        _apply_unknown_fallback(ctx, source="unknown", reason=reason)
        return
    ctx.metadata["router_vision_followup_gate_decision"] = decision
    ctx.metadata["router_vision_followup_gate_confidence"] = confidence
    ctx.metadata["router_vision_followup_gate_reason"] = reason
    ctx.metadata["router_vision_followup_gate_source"] = "llm"
    ctx.metadata["router_vision_followup_needs_image"] = decision == "needs_image"


def _apply_explicit_opt_out(ctx: TurnContext) -> None:
    ctx.metadata["router_vision_followup_gate_decision"] = "text_only"
    ctx.metadata["router_vision_followup_gate_confidence"] = 1.0
    ctx.metadata["router_vision_followup_gate_reason"] = (
        "current turn explicitly opts out of image use"
    )
    ctx.metadata["router_vision_followup_gate_source"] = "explicit_opt_out"
    ctx.metadata["router_vision_followup_needs_image"] = False


def _current_text_explicitly_opts_out_image(ctx: TurnContext) -> bool:
    text = str(ctx.semantic_message or ctx.message or "")
    if not text.strip() or (
        not _IMAGE_REF_RE.search(text)
        and not any(ref in text for ref in _ZH_IMAGE_REFS)
    ):
        return False
    return bool(_IMAGE_OPTOUT_RE.search(text))


def _current_text_explicitly_requests_previous_image(ctx: TurnContext) -> bool:
    text = str(ctx.semantic_message or ctx.message or "")
    if not text.strip():
        return False
    if any(ref in text for ref in _ZH_PREVIOUS_IMAGE_REFS):
        return True
    return bool(_PREVIOUS_IMAGE_REF_RE.search(text))


def _apply_explicit_previous_image_request(ctx: TurnContext) -> None:
    ctx.metadata["router_vision_followup_gate_decision"] = "needs_image"
    ctx.metadata["router_vision_followup_gate_confidence"] = 1.0
    ctx.metadata["router_vision_followup_gate_reason"] = (
        "current turn explicitly references a previous image"
    )
    ctx.metadata["router_vision_followup_gate_source"] = "explicit_image_reference"
    ctx.metadata["router_vision_followup_needs_image"] = True


def _apply_unknown_fallback(ctx: TurnContext, *, source: str, reason: str) -> None:
    cfg = _router_cfg(ctx)
    recent_limit = int(getattr(cfg, "vision_followup_gate_fallback_recent_turns", 2) or 0)
    policy = str(
        getattr(cfg, "vision_followup_gate_unknown_policy", "image_if_recent")
        or "image_if_recent"
    )
    turns = _turns_since_last_image(ctx)
    needs_image = (
        policy == "image_if_recent"
        and turns is not None
        and turns <= recent_limit
    )
    ctx.metadata["router_vision_followup_gate_decision"] = "unknown"
    ctx.metadata["router_vision_followup_gate_confidence"] = 0.0
    ctx.metadata["router_vision_followup_gate_reason"] = reason
    ctx.metadata["router_vision_followup_gate_source"] = source
    ctx.metadata["router_vision_followup_needs_image"] = needs_image
    if needs_image:
        ctx.metadata["router_vision_followup_fallback"] = "image_if_recent"
    else:
        ctx.metadata.pop("router_vision_followup_fallback", None)


def _apply_gate_error(ctx: TurnContext, *, reason: str) -> None:
    ctx.metadata["router_vision_followup_gate_decision"] = "unknown"
    ctx.metadata["router_vision_followup_gate_confidence"] = 0.0
    ctx.metadata["router_vision_followup_gate_reason"] = reason
    ctx.metadata["router_vision_followup_gate_source"] = "error"
    ctx.metadata["router_vision_followup_needs_image"] = False
    ctx.metadata.pop("router_vision_followup_fallback", None)


async def _run_gate_or_fallback(ctx: TurnContext) -> TurnContext:
    cfg = _router_cfg(ctx)
    timeout = float(getattr(cfg, "vision_followup_gate_timeout_seconds", 3.0) or 3.0)
    try:
        raw = await asyncio.wait_for(_call_gate_provider(ctx), timeout=timeout + 0.2)
        decision, confidence, reason = _parse_gate_response(raw)
    except Exception as exc:  # noqa: BLE001 - gate failure must not block routing
        _apply_gate_error(ctx, reason=type(exc).__name__)
        return ctx
    _apply_gate_decision(ctx, decision=decision, confidence=confidence, reason=reason)
    return ctx


async def apply_vision_followup_gate(ctx: TurnContext) -> TurnContext:
    if not _gate_enabled(ctx):
        ctx.metadata["router_vision_followup_gate_decision"] = "disabled"
        return ctx
    if _attachments_include_image(ctx.attachments):
        ctx.metadata["router_vision_followup_gate_decision"] = "current_image"
        return ctx
    if ctx.metadata.get("router_history_has_recent_image") is not True:
        ctx.metadata["router_vision_followup_gate_decision"] = "not_applicable"
        return ctx
    if _candidate_window_expired(ctx):
        ctx.metadata["router_vision_followup_gate_decision"] = "not_applicable"
        ctx.metadata["router_vision_followup_gate_reason"] = "candidate_window_expired"
        return ctx
    if _current_text_explicitly_opts_out_image(ctx):
        _apply_explicit_opt_out(ctx)
        return ctx
    if _current_text_explicitly_requests_previous_image(ctx):
        _apply_explicit_previous_image_request(ctx)
        return ctx
    return await _run_gate_or_fallback(ctx)
