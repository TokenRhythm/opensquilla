"""Pipeline step: detect Meta-Skill trigger matches and emit a soft hint.

Behaviour (post-hard-takeover-removal)
--------------------------------------
* Scans the loaded skills for entries with ``kind == "meta"``.
* Matches the current user message (case-insensitive substring for CJK,
  word-boundary regex for ASCII) against each meta-skill's ``triggers``.
* If at least one matches, the highest ``meta_priority`` wins; a
  :class:`MetaMatch` is written to ``ctx.metadata['meta_match']`` for
  downstream observability (decision log card, persistence, audit) and a
  short hint string is appended to ``ctx.system_prompt`` telling the LLM
  *"this looks like meta-skill X; call meta_invoke(name=X) if that's the
  intent"*.
* Trigger matches still use the soft ``meta_invoke`` path rather than
  directly calling ``MetaOrchestrator``, but the first provider request is
  forced to choose ``meta_invoke``. This keeps execution observable through
  the normal tool path while preventing routed models from bypassing the
  meta DAG with ordinary tools after a deterministic trigger match.
* Semantic-only matches remain advisory: they inject the hint and leave tool
  choice automatic so retrieval false positives do not hard-start a DAG.
* Any parse error on a meta-skill is logged and skipped — the rest of the
  turn falls back to normal handling (fail-open).
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import Mapping
from typing import Any

import structlog

from opensquilla.engine.pipeline import TurnContext
from opensquilla.skills.meta.clarify_autofill import (
    autofill_required_clarify_fields,
    is_empty_clarify_submission,
)
from opensquilla.skills.meta.clarify_nl_extract import extract as _nl_extract
from opensquilla.skills.meta.clarify_text import parse_clarify_reply
from opensquilla.skills.meta.inputs import (
    make_meta_inputs,
    meta_input_overrides_from_metadata,
)
from opensquilla.skills.meta.parser import MetaPlanError, parse_meta_plan
from opensquilla.skills.meta.types import MetaMatch
from opensquilla.skills.retrieval import HybridRetriever
from opensquilla.skills.types import SkillSpec

log = structlog.get_logger(__name__)


# ── Session-sticky meta match (continuation across multi-turn chats) ──
#
# Hard problem the trigger-only matcher could not solve: once T1 of a
# chat hits a meta-skill trigger (e.g. "帮我写篇论文"), the LLM might
# fail to actually emit ``meta_invoke`` on that turn — for example
# deepseek-v4-flash can length-cap on reasoning before producing the
# forced tool call. Then T2 carries follow-up details ("我想写 RAG…")
# which does not contain the trigger phrase, so the matcher returns
# nothing, ``meta_invoke`` falls out of the tool surface, and the LLM
# tries ``read_file`` / ``glob_search`` instead.
#
# This module-level cache keeps the chosen ``(skill_name, trigger)``
# alive for a small number of follow-up turns. It is in-memory only
# (lost on gateway restart, which is fine — restart is rare and an
# unstuck T1 will re-trigger naturally). The cache is bounded by:
# * ``_STICKY_TTL_SECONDS`` — wall-clock window
# * ``_STICKY_MAX_USES`` — max follow-up turns where a sticky hit re-arms
#   the match before we give up
#
# Eviction triggers:
# * TTL expires → entry dropped on next access
# * Uses exhausted → entry dropped on next access
# * User message contains a sticky-cancel keyword → entry dropped now
# * The awaiting branch fires (proper continuation took over) → entry
#   dropped because we never reach the trigger/sticky code path
_STICKY_TTL_SECONDS = 1800.0
_STICKY_MAX_USES = 3
_STICKY_CANCEL_KEYWORDS = (
    "取消", "算了", "别写了", "不写了", "停止",
    "cancel", "stop", "nevermind", "never mind", "forget it",
)
_sticky_lock = threading.Lock()
_meta_sticky_cache: dict[str, dict[str, Any]] = {}


def _sticky_get(session_id: str) -> dict[str, Any] | None:
    if not session_id:
        return None
    now = time.time()
    with _sticky_lock:
        entry = _meta_sticky_cache.get(session_id)
        if entry is None:
            return None
        if now - entry["ts"] > _STICKY_TTL_SECONDS or entry["uses"] <= 0:
            _meta_sticky_cache.pop(session_id, None)
            return None
        return dict(entry)


def _sticky_put(session_id: str, skill: str, trigger: str) -> None:
    if not session_id or not skill:
        return
    with _sticky_lock:
        _meta_sticky_cache[session_id] = {
            "ts": time.time(),
            "uses": _STICKY_MAX_USES,
            "skill": skill,
            "trigger": trigger,
        }


def _sticky_consume(session_id: str) -> None:
    """Decrement uses on a sticky hit; drop entry when exhausted."""
    if not session_id:
        return
    with _sticky_lock:
        entry = _meta_sticky_cache.get(session_id)
        if entry is None:
            return
        entry["uses"] -= 1
        if entry["uses"] <= 0:
            _meta_sticky_cache.pop(session_id, None)


def _sticky_drop(session_id: str) -> None:
    if not session_id:
        return
    with _sticky_lock:
        _meta_sticky_cache.pop(session_id, None)


def _clamp_thinking_for_meta(ctx: TurnContext) -> None:
    """Force ``thinking_level=low`` on a meta-matched turn.

    Even with ``meta_match_tool_choice`` forcing ``meta_invoke``, some
    models (notably deepseek-v4-flash) burn the entire output budget on
    reasoning before producing the tool call. The structured argument
    list for ``meta_invoke`` does not need deep reasoning, so we clamp
    to low here. Recorded in ``thinking_source`` so the next pipeline
    step can see who set it last.
    """
    ctx.metadata["thinking_level"] = "low"
    ctx.metadata["thinking_requested"] = True
    ctx.metadata["thinking_source"] = "meta_resolution"


def _input_provenance_kind(ctx: TurnContext) -> str:
    """Return normalized input provenance kind from pipeline metadata."""

    metadata = getattr(ctx, "metadata", {})
    provenance = metadata.get("input_provenance")
    if isinstance(provenance, Mapping):
        kind = provenance.get("kind")
        return str(kind) if kind is not None and str(kind) else ""
    if isinstance(provenance, str):
        return provenance
    kind = metadata.get("input_provenance_kind")
    return str(kind) if kind is not None and str(kind) else ""


def _hits_cancel_keywords(message: str, keywords: tuple[str, ...]) -> bool:
    if not keywords:
        return False
    lower = (message or "").lower()
    for kw in keywords:
        if kw and kw in lower:
            return True
    return False


def _clarify_errors_allow_autofill(errors: list[str]) -> bool:
    if not errors:
        return True
    for error in errors:
        lowered = str(error).lower()
        if "required field" in lowered or "empty value" in lowered:
            continue
        return False
    return True


def _current_semantic_text(ctx: TurnContext) -> str:
    candidate = (
        getattr(ctx, "semantic_message", None)
        or getattr(ctx, "raw_message", None)
        or getattr(ctx, "message", "")
        or ""
    )
    return str(candidate)


def _chat_pending_fields(schema, awaiting):
    """Return tuple of fields not yet in awaiting_filled_json (chat mode).

    The nl_extract whitelist is the SINGLE currently-asked field in chat
    mode so the LLM cannot accidentally fill out later fields the user
    has not been prompted for yet.
    """
    import json as _json
    try:
        filled = _json.loads(awaiting.awaiting_filled_json or "{}")
    except Exception:  # noqa: BLE001
        filled = {}
    for field in schema.fields:
        if field.name not in filled:
            return (field,)
    return tuple(schema.fields)  # all filled — defensive (shouldn't reach here)


def _json_object(raw: str) -> dict[str, Any]:
    import json as _json

    try:
        parsed = _json.loads(raw or "{}")
    except Exception:  # noqa: BLE001
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _clip_context_value(value: Any, *, max_chars: int = 2000) -> Any:
    if isinstance(value, str):
        if len(value) <= max_chars:
            return value
        return value[:max_chars] + "...[truncated]"
    if isinstance(value, dict):
        return {
            str(k): _clip_context_value(v, max_chars=max_chars)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_clip_context_value(v, max_chars=max_chars) for v in value[:20]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _clip_context_value(str(value), max_chars=max_chars)


def _clarify_extract_context(
    awaiting, active_fields, ctx: TurnContext | None = None,
) -> dict[str, Any]:
    """Build bounded prior context for nl_extract reference resolution.

    This context is advisory only: ``clarify_nl_extract`` still white-lists
    returned keys against ``active_fields`` and re-runs field validators.

    When ``ctx`` is supplied we pull a bounded slice of the conversation
    that preceded the trigger turn from
    ``ctx.metadata["conversation_history"]`` (a list of role/content
    dicts produced by the upstream gateway/agent). The slice is the
    LAST three turns, each clipped to a small character cap, formatted
    as plain ``[role] text`` lines so the NL extractor can mine
    pre-stated field values without being shown the entire chat
    transcript. The metadata key is optional — when absent (e.g. unit
    tests, channels that don't yet inject it), the resolver behaves
    exactly as before.
    """

    inputs = _json_object(awaiting.inputs_json)
    step_outputs = _json_object(awaiting.step_outputs_json)
    already_filled = _json_object(awaiting.awaiting_filled_json)
    context: dict[str, Any] = {
        "awaiting_step_id": awaiting.step_id,
        "active_field_names": [getattr(f, "name", "") for f in active_fields],
    }
    user_message = inputs.get("user_message")
    if isinstance(user_message, str) and user_message.strip():
        context["original_user_message"] = _clip_context_value(user_message)
    collected = inputs.get("collected")
    if isinstance(collected, dict) and collected:
        context["already_collected"] = _clip_context_value(collected)
    if already_filled:
        context["already_filled"] = _clip_context_value(already_filled)
    if step_outputs:
        context["prior_step_outputs"] = _clip_context_value(step_outputs)
    history_block = _conversation_history_block(ctx)
    if history_block:
        context["conversation_history"] = history_block
    return context


# Tunables for the conversation_history channel (F-history step b).
# Three turns balances "user has likely already said the answer" with
# "don't blow out the prompt budget"; 200 characters per turn is enough
# for a sentence and matches the operator's expectation that long
# pasted artefacts (logs, code) are summarised away rather than dumped.
_CONVERSATION_HISTORY_TURNS = 3
_CONVERSATION_HISTORY_CHARS_PER_TURN = 200
_CONVERSATION_HISTORY_TOTAL_CAP = 1500


def _conversation_history_block(ctx: TurnContext | None) -> str:
    """Render the last few conversation turns as ``[role] text`` lines.

    The resolver consults two sources, in order:

    * ``ctx.metadata["conversation_history"]`` — the canonical
      role/content list. When the upstream gateway / agent / channel
      adapter populates this directly we use it as-is. Each entry is
      expected to be a Mapping with ``role`` and ``content`` keys; the
      OpenAI/Anthropic content-block list shape is also accepted.

    * ``ctx.metadata["router_history_user_texts"]`` +
      ``ctx.metadata["router_prev_assistant_text"]`` — fallback C2
      producer. The squilla router already populates these for every
      turn (``runtime.py``), so we synthesise a minimal
      ``[user]``/``[assistant]`` interleave when no explicit history
      key is present. This is what lights up ``conversation_history``
      for live traffic without adding a new ingress hop.

    Both sources are bounded by the same per-turn / per-block char
    caps and the three-turn slice (newest last). Anything else is
    skipped silently — clarify resolution is fail-open by design.
    """
    if ctx is None:
        return ""
    raw_history = ctx.metadata.get("conversation_history")
    if isinstance(raw_history, list) and raw_history:
        return _render_history_entries(raw_history)
    fallback = _conversation_history_from_router_metadata(ctx)
    if fallback:
        return _render_history_entries(fallback)
    return ""


def _render_history_entries(raw: list) -> str:
    """Shared formatter for history entries (canonical or fallback)."""
    lines: list[str] = []
    used = 0
    for entry in raw[-_CONVERSATION_HISTORY_TURNS:]:
        if not isinstance(entry, Mapping):
            continue
        role = str(entry.get("role") or "user").strip().lower() or "user"
        content = entry.get("content")
        text = _coerce_history_content(content)
        text = text.strip()
        if not text:
            continue
        clipped = text[:_CONVERSATION_HISTORY_CHARS_PER_TURN]
        if len(text) > _CONVERSATION_HISTORY_CHARS_PER_TURN:
            clipped = clipped + "...[truncated]"
        line = f"[{role}] {clipped}"
        used += len(line) + 1
        if used > _CONVERSATION_HISTORY_TOTAL_CAP:
            break
        lines.append(line)
    return "\n".join(lines)


def _conversation_history_from_router_metadata(
    ctx: TurnContext,
) -> list[Mapping[str, Any]]:
    """Build a role/content history from the router's existing
    metadata channels. ``router_history_user_texts`` carries the
    last few user turns (excluding the current one);
    ``router_prev_assistant_text`` carries the most recent
    assistant reply. Interleaving them as ``[user]...[assistant]
    user`` gives the NL extractor enough context to recognise
    references like 'I said earlier' without forcing every channel
    adapter to inject a separate history payload.
    """
    user_texts = ctx.metadata.get("router_history_user_texts")
    prev_assistant = ctx.metadata.get("router_prev_assistant_text")
    if not isinstance(user_texts, list) or not user_texts:
        if not (isinstance(prev_assistant, str) and prev_assistant.strip()):
            return []
        return [{"role": "assistant", "content": prev_assistant}]
    # Take the last (3 - 1) = 2 user turns so the assembled list
    # interleave doesn't exceed the 3-turn slice when an assistant
    # reply is also present.
    tail = list(user_texts)[-(_CONVERSATION_HISTORY_TURNS - 1):]
    history: list[Mapping[str, Any]] = []
    for text in tail:
        if isinstance(text, str) and text.strip():
            history.append({"role": "user", "content": text})
    if isinstance(prev_assistant, str) and prev_assistant.strip():
        # The router-published assistant reply belongs immediately
        # after the most recent user turn but before the trigger
        # turn. Insert at the tail; the truncator will keep the
        # newest slice.
        history.append({"role": "assistant", "content": prev_assistant})
    return history


def _coerce_history_content(content: Any) -> str:
    """Flatten the heterogeneous shapes ``content`` can take.

    Accepts a plain string, a list of ``{"type": "text", "text": ...}``
    blocks (OpenAI / Anthropic shape), or an arbitrary structure
    (which we cast through ``str``). Skips non-text blocks.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    return ""


def _deserialize_awaiting_schema(schema_json: str):
    """Re-create a ClarifyStepConfig from the awaiting_schema_json column."""
    import json as _json  # noqa: PLC0415

    from opensquilla.skills.meta.plan_serde import clarify_config_from_jsonable  # noqa: PLC0415

    try:
        raw = _json.loads(schema_json or "{}")
    except Exception:  # noqa: BLE001
        return clarify_config_from_jsonable({"mode": "form", "fields": []})
    return clarify_config_from_jsonable(raw)


_META_SKILL_EXPLANATION_RE = re.compile(
    r"\b(how|what|why|explain|describe)\b.*\bmeta[-\s]?skills?\b"
)

_META_SKILL_SUBJECT_RE = re.compile(
    r"(?:meta[-_\s]?skills?|元技能|meta\s*技能)",
    re.IGNORECASE,
)
_META_SKILL_DISCUSSION_CUES = (
    "吗",
    "么",
    "是否",
    "有没有",
    "调用了",
    "调用过",
    "为什么",
    "怎么",
    "如何",
    "机制",
    "教程",
    "注意",
    "原因",
    "质量",
    "好差",
    "太差",
    "生成的差",
    "generated poorly",
    "bad quality",
    "called",
    "invoked",
    "why",
    "how",
    "explain",
    "describe",
)

_PASTED_CONTEXT_MARKERS = (
    "webchat dump",
    "chat dump",
    "page dump",
    "transcript",
    "conversation dump",
    "history dump",
    "skill list",
    "old skill",
    "历史记录",
    "历史 transcript",
    "历史页面",
    "页面内容",
    "粘贴材料",
    "整页 webchat",
    "旧 skill",
)

_PASTED_CONTEXT_BOUNDARY_RE = re.compile(
    r"^\s*(?:```|~~~|[-=]{3,}|<skill\b|</skill>|"
    r"(?:user|assistant|system|tool)\s*:)",
    re.IGNORECASE,
)


def _looks_like_pasted_context(message: str) -> bool:
    text = (message or "").strip()
    if not text:
        return False
    lower = text.lower()
    marker_hit = any(marker in lower for marker in _PASTED_CONTEXT_MARKERS)
    if not marker_hit:
        return False
    return len(text) > 1200 or text.count("\n") >= 8


def _is_meta_skill_discussion_intent(query: str) -> bool:
    """Detect questions about meta-skills themselves, not requests to run one."""

    text = (query or "").strip()
    if not text:
        return False
    lower = text.lower()
    if not _META_SKILL_SUBJECT_RE.search(lower):
        return False
    return any(cue in lower for cue in _META_SKILL_DISCUSSION_CUES)


def _trigger_match_text(message: str) -> str:
    """Return the current-intent slice used for deterministic trigger scans.

    Meta-skill triggers are intentionally cheap and deterministic. For normal
    short user requests, scanning the whole message is the right behavior. For
    long pasted chat/page dumps, however, the full message often includes old
    skill lists, historical assistant text, and quoted examples. In that shape,
    only the user's leading instruction is a reasonable current-intent signal.
    """

    if not _looks_like_pasted_context(message):
        return message

    prefix: list[str] = []
    for index, line in enumerate(message.splitlines()):
        lower = line.lower()
        if index > 0 and _PASTED_CONTEXT_BOUNDARY_RE.match(line):
            break
        if index > 0 and any(marker in lower for marker in _PASTED_CONTEXT_MARKERS):
            break
        prefix.append(line)

    candidate = "\n".join(prefix).strip()
    if candidate:
        return candidate
    return "\n".join(message.splitlines()[:3]).strip()


def _tier_sort_key(name: str, index: int) -> tuple[int, int]:
    """Prefer canonical router tiers (c0 < c1 < c2 < c3), then declaration order."""

    match = re.fullmatch(r"c(\d+)", name.strip().lower())
    if match:
        return (int(match.group(1)), index)
    return (-1, index)


def _highest_text_tier(ctx: TurnContext) -> tuple[str, str] | None:
    """Return ``(tier_name, model)`` for the highest configured text tier."""

    router_cfg = getattr(getattr(ctx, "config", None), "squilla_router", None)
    tiers = getattr(router_cfg, "tiers", None)
    if not isinstance(tiers, dict) or not tiers:
        return None

    candidates: list[tuple[tuple[int, int], str, str]] = []
    for index, (name, tier_cfg) in enumerate(tiers.items()):
        if not isinstance(tier_cfg, dict):
            continue
        if bool(tier_cfg.get("image_only", False)):
            continue
        model = str(tier_cfg.get("model") or "").strip()
        tier_name = str(name).strip()
        if tier_name and model:
            candidates.append((_tier_sort_key(tier_name, index), tier_name, model))
    if not candidates:
        return None
    _key, tier_name, model = max(candidates, key=lambda item: item[0])
    return tier_name, model


def _text_tier_at_least(ctx: TurnContext, minimum: str) -> tuple[str, str] | None:
    """Return the lowest configured text tier at or above ``minimum``."""

    router_cfg = getattr(getattr(ctx, "config", None), "squilla_router", None)
    tiers = getattr(router_cfg, "tiers", None)
    if not isinstance(tiers, dict) or not tiers:
        return None
    minimum_key = _tier_sort_key(minimum, 0)[0]

    candidates: list[tuple[tuple[int, int], str, str]] = []
    for index, (name, tier_cfg) in enumerate(tiers.items()):
        if not isinstance(tier_cfg, dict):
            continue
        if bool(tier_cfg.get("image_only", False)):
            continue
        model = str(tier_cfg.get("model") or "").strip()
        tier_name = str(name).strip()
        tier_key = _tier_sort_key(tier_name, index)
        if tier_name and model and tier_key[0] >= minimum_key:
            candidates.append((tier_key, tier_name, model))
    if not candidates:
        return None
    _key, tier_name, model = min(candidates, key=lambda item: item[0])
    return tier_name, model


def _upgrade_meta_entry_model(
    ctx: TurnContext,
    *,
    tier_name: str,
    model: str,
    source: str,
) -> None:
    baseline_model = str(getattr(ctx, "model", "") or "")
    ctx.model = model
    ctx.metadata["meta_required_tier"] = tier_name
    ctx.metadata["meta_required_model"] = model
    ctx.metadata["meta_required_source"] = source
    ctx.metadata.setdefault("baseline_model", baseline_model)
    ctx.metadata["routed_tier"] = tier_name
    ctx.metadata["routed_model"] = model
    ctx.metadata["routing_source"] = "meta_skill_required_tier"
    ctx.metadata["routing_confidence"] = 1.0
    ctx.metadata["routing_applied"] = True
    ctx.metadata["applied_model"] = model
    # Realign routed_provider with the UPGRADED tier: the router set it from the
    # original (cheaper) tier, and the selector-apply tail pairs routed_provider
    # with routed_model. Without this, the upgraded model would be sent to the
    # wrong provider. Look up the target tier's provider from config; pop the
    # key when the tier declares none (so a stale value can't linger).
    tiers = getattr(getattr(ctx.config, "squilla_router", None), "tiers", None)
    tier_cfg = tiers.get(tier_name) if isinstance(tiers, dict) else None
    tier_provider = ""
    if isinstance(tier_cfg, dict):
        tier_provider = str(tier_cfg.get("provider") or "").strip().lower()
    if tier_provider:
        ctx.metadata["routed_provider"] = tier_provider
    else:
        ctx.metadata.pop("routed_provider", None)
    ctx.metadata["meta_resolution_model_upgrade"] = {
        "from_model": baseline_model,
        "to_model": model,
        "to_tier": tier_name,
        "source": source,
    }


def _trigger_matches(trigger: str, message_lower: str) -> bool:
    """Match a trigger phrase against the user message.

    * Pure-ASCII triggers (English) require word boundaries — so the
      trigger "research report" does NOT fire on
      "How does the *research report* meta-skill work?" because the
      phrase is embedded in a larger sentence about the skill itself.
    * Triggers containing CJK characters fall back to substring match
      since Chinese phrases have no word boundaries in the regex sense
      and are typically distinctive enough (for example, compliance-audit
      phrases in CJK languages) that
      substring matching does not produce ambiguous fires.
    """
    tl = trigger.lower()
    if all(ord(c) < 128 for c in tl):
        if _META_SKILL_EXPLANATION_RE.search(message_lower):
            return False
        normalized_trigger = re.sub(r"[^a-z0-9]+", " ", tl).strip()
        normalized_message = re.sub(r"[^a-z0-9]+", " ", message_lower).strip()
        if not normalized_trigger or normalized_trigger not in normalized_message:
            return False
        return bool(
            re.search(
                r"\b" + re.escape(normalized_trigger) + r"\b",
                normalized_message,
            )
        )
    if tl not in message_lower:
        return False
    return True


def _first_matching_trigger(triggers: list[str], message_lower: str) -> str:
    """Return the trigger phrase that fired, for the hint text."""
    for t in triggers:
        if isinstance(t, str) and t and _trigger_matches(t, message_lower):
            return t
    return ""  # unreachable when caller already verified ``any(...)``


_SEMANTIC_WORKFLOW_CUES = (
    "pdf",
    "document",
    "doc",
    "report",
    "research",
    "summarize",
    "summary",
    "analyze",
    "analysis",
    "review",
    "migration",
    "migrate",
    "upgrade",
    "travel",
    "trip",
    "itinerary",
    "skill",
    "workflow",
    "文件",
    "文档",
    "报告",
    "调研",
    "研究",
    "总结",
    "分析",
    "看看",
    "看一下",
    "读一下",
    "迁移",
    "升级",
    "旅行",
    "行程",
    "技能",
    "流程",
)


def _has_semantic_workflow_cue(query: str) -> bool:
    """Keep semantic fallback from turning every utterance into a meta hint."""

    text = query.lower().strip()
    if not text:
        return False
    if re.search(r"\.(pdf|docx?|md|txt|csv|json)\b", text):
        return True
    return any(cue in text for cue in _SEMANTIC_WORKFLOW_CUES)


_SKILL_MARKETPLACE_SUBJECT_CUES = (
    "skill",
    "skills",
    "clawhub",
    "marketplace",
    "community",
    "技能",
    "技能市场",
    "社区",
)

_SKILL_MARKETPLACE_ACTION_CUES = (
    "install",
    "search",
    "find",
    "browse",
    "hub",
    "安装",
    "搜索",
    "查找",
    "找",
)


def _is_skill_marketplace_intent(query: str) -> bool:
    """Detect install/search marketplace turns that should use skill tools."""

    text = query.lower().strip()
    if not text:
        return False
    has_subject = any(cue in text for cue in _SKILL_MARKETPLACE_SUBJECT_CUES)
    has_action = any(cue in text for cue in _SKILL_MARKETPLACE_ACTION_CUES)
    return has_subject and has_action


def _semantic_meta_candidate(
    ctx: TurnContext,
    candidates: list[tuple[int, str, object, SkillSpec]],
) -> tuple[int, str, object, str] | None:
    """Return the best meta-skill candidate from retrieval, if any.

    Trigger matching is the high-precision path. This fallback keeps the
    product behavior soft: retrieval only chooses which meta-skill to hint;
    the outer LLM still decides whether invoking the DAG fits the user intent.
    """

    if not candidates:
        return None
    query = (
        getattr(ctx, "semantic_message", None)
        or getattr(ctx, "raw_message", None)
        or getattr(ctx, "message", "")
        or ""
    )
    if not str(query).strip():
        return None
    query = _trigger_match_text(str(query))
    if not str(query).strip():
        return None
    if not _has_semantic_workflow_cue(str(query)):
        return None

    retriever = HybridRetriever(strategy="hybrid")
    specs = [spec for _priority, _name, _plan, spec in candidates]
    try:
        ranked = retriever.retrieve(specs, str(query), top_k=1)
    except Exception as exc:  # noqa: BLE001 - fail open; triggers still work.
        log.warning("meta_resolution.semantic_match_failed", error=str(exc))
        return None
    if not ranked:
        return None
    chosen_name = getattr(ranked[0], "name", "")
    for priority, name, plan, _spec in candidates:
        if name == chosen_name:
            return (priority, name, plan, "semantic")
    return None


def _build_hint(
    skill_name: str,
    trigger_phrase: str,
    candidates: list[tuple[int, str, str]] | None = None,
    activation_mode: str = "recommend",
) -> str:
    """Render the soft-hint suffix appended to ``system_prompt``.

    The phrasing is deliberately balanced: it nudges the model toward
    ``meta_invoke`` *only when intent matches*, and explicitly allows
    declining when the trigger word appears in an off-topic context
    (e.g. "my **travel plan** got cancelled" should NOT auto-run the
    travel-planner DAG just because "travel plan" was uttered).

    When more than one meta-skill matched the user's message (D1 + D2),
    the hint also surfaces the runner-up candidates so the LLM can pick
    a better match than the priority-winner. The phrasing tells the LLM
    it is free to pick any skill from ``<available_skills>`` if none
    of the candidates fits (D4) — the substring matcher is not the
    final arbiter of intent.
    """
    mode = "hint" if activation_mode == "hint" else "recommend"
    if mode == "hint":
        lead = (
            f"The user message is semantically similar to the meta-skill "
            f"`{skill_name}`. Treat this as a candidate, not a command."
        )
        action = (
            f"First decide whether the user is asking for the end-to-end "
            f"meta-skill deliverable. If yes, call "
            f"`meta_invoke(name=\"{skill_name}\")` as the first action; the "
            f"framework will drive the DAG and the deliverable becomes the "
            f"assistant reply. Do not emit explanatory text before calling "
            f"`meta_invoke`. Do not answer directly in that case. Do not call "
            f"ordinary tools before `meta_invoke`; the meta-skill will call "
            f"its own sub-skills internally. If a direct answer is more "
            f"appropriate because the user is only asking a quick question or "
            f"asking about the meta-skill itself, ignore this candidate and "
            f"answer normally."
        )
    else:
        if trigger_phrase == "semantic":
            evidence = (
                f"The previous turn selected `{skill_name}` by semantic "
                "similarity and this turn is a sticky continuation."
            )
        else:
            evidence = (
                f'The user message contains the phrase "{trigger_phrase}", '
                f"which is a registered trigger for the meta-skill `{skill_name}`."
            )
        lead = evidence
        action = (
            f"For a concrete deliverable request that matches this meta-skill, "
            f"call `meta_invoke(name=\"{skill_name}\")` as the next action. "
            f"Concrete deliverable "
            f"requests include asking for an audit, review, decision brief, "
            f"plan, comparison, extraction, report, rollback plan, or other "
            f"multi-step work product. Do not emit explanatory text before "
            f"calling `meta_invoke`. Do not answer directly or call ordinary "
            f"tools such as web/search/http/file tools before `meta_invoke` "
            f"in that case; the meta-skill DAG will call any required "
            f"sub-skills internally and its deliverable becomes the assistant "
            f"reply."
        )
    lines = [
        "\n\n## Meta-skill activation guidance",
        f"Activation mode: {mode}",
        lead,
        action,
        "Do not call `skill_view` for this meta-skill; `skill_view` is for "
        "ordinary skills, while this meta-skill should be started with "
        "`meta_invoke` directly.",
    ]
    if candidates and len(candidates) > 1:
        lines.append("")
        lines.append(
            "Other candidates also matched (in priority order, winner first):"
        )
        for prio, name, phrase in candidates:
            marker = " ← chosen" if name == skill_name else ""
            lines.append(f"  • `{name}` (priority {prio}, trigger {phrase!r}){marker}")
        lines.append(
            "If one of the runner-ups fits the user's intent better, call "
            "`meta_invoke(name=\"<runner-up name>\")` instead of the chosen one."
        )
    lines.append("")
    lines.append(
        "If the user is asking *about* a meta-skill, querying status, or their "
        "request is only tangentially related to the trigger phrase, ignore "
        "this hint and answer normally. Otherwise, for matched multi-step "
        "deliverable requests, prefer starting the matched meta-skill over "
        "manually reproducing its steps with ordinary tools. You can also call "
        "`meta_invoke` for any other meta-skill listed in `<available_skills>` "
        "whose description fits the request — the substring trigger above is "
        "a hint, not a constraint."
    )
    return "\n".join(lines)


async def meta_resolution(ctx: TurnContext) -> TurnContext:
    """Resolve a Meta-Skill trigger, stash a MetaMatch, and inject a soft hint."""

    from opensquilla.skills.meta.enabled import is_meta_skill_enabled

    if not is_meta_skill_enabled(getattr(ctx, "config", None)):
        return ctx

    writer = ctx.metadata.get("meta_run_writer")
    # TurnContext field is `session_key`; DAO interface alias is `session_id`.
    session_id = getattr(ctx, "session_key", "") or ""

    # ── Leading awaiting branch (PR3, design §8.2) ─────────────────
    if writer is not None and session_id:
        try:
            awaiting = await asyncio.to_thread(
                writer.peek_awaiting, session_id=session_id,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open
            log.warning("meta_resolution.peek_awaiting_failed", error=str(exc))
            awaiting = None

        if awaiting is not None:
            schema = _deserialize_awaiting_schema(awaiting.awaiting_schema_json)
            now = time.time()

            # Parse the clarify reply from the UNMUTATED user text. Earlier
            # pipeline steps (e.g. the router) may append a RESPONSE_POLICY hint
            # to ctx.message for the LLM path; the deterministic clarify parser
            # must see only what the user typed, or that suffix corrupts field
            # parsing and cancel-keyword matching.
            reply_text = _current_semantic_text(ctx)

            if now - awaiting.awaiting_since > schema.timeout_hours * 3600:
                await asyncio.to_thread(
                    writer.mark_expired, run_id=awaiting.run_id,
                )
                ctx.metadata["meta_clarify_expired"] = awaiting
                return ctx

            if _hits_cancel_keywords(reply_text, schema.cancel_keywords):
                await asyncio.to_thread(
                    writer.mark_cancelled,
                    run_id=awaiting.run_id,
                    reason="user_cancel",
                )
                ctx.metadata["meta_clarify_cancelled"] = awaiting
                ctx.metadata["meta_clarify_cancel_reason"] = "user_cancel"
                return ctx

            parsed: dict[str, Any] = {}
            errors: list[str] = []
            nl_attempted = False
            # Fields the user filled in earlier turns of the SAME step
            # (chat mode collects one field per turn; form mode can re-prompt
            # after a parse failure with partial fills carried forward).
            # Both parse paths must merge this with the current turn before
            # the DAG resumes, otherwise the cumulative state visible to
            # downstream steps loses every previously answered field.
            #
            # Step (d) reserves the ``__prefill_audit__`` key inside
            # ``awaiting_filled_json`` for the auto-prefill audit
            # payload (see ``executors/user_input.py``). Strip it
            # before treating the remainder as field values so the
            # downstream merge / required-completeness checks never
            # see it as a field name. Surface it on ``ctx.metadata``
            # so the surface can render the ``confirmed_fields``
            # protocol (which fields were inferred + from where).
            raw_filled = _json_object(awaiting.awaiting_filled_json)
            prefill_audit = raw_filled.pop("__prefill_audit__", None)
            previously_filled = raw_filled
            if isinstance(prefill_audit, dict) and prefill_audit:
                ctx.metadata["meta_clarify_prefill_audit"] = prefill_audit
            is_structured_clarify_form = _input_provenance_kind(ctx) == "clarify_form"
            if schema.nl_extract and not is_structured_clarify_form:
                # PR9+: when explicitly enabled, prefer LLM extraction over
                # deterministic text parsing. Validators are reapplied inside
                # extract() so prompt-injection in user_message cannot bypass
                # type/range/choice constraints.
                nl_chat = ctx.metadata.get("meta_llm_chat")
                if nl_chat is not None:
                    nl_attempted = True
                    active = (
                        _chat_pending_fields(schema, awaiting)
                        if schema.mode == "chat"
                        else schema.fields
                    )
                    try:
                        nl_result = await _nl_extract(
                            reply_text=reply_text,
                            schema=schema,
                            active_fields=active,
                            llm_chat=nl_chat,
                            tier=schema.nl_extract_tier,
                            context=_clarify_extract_context(awaiting, active, ctx),
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "meta_resolution.nl_extract_failed",
                            error=str(exc),
                        )
                        errors = [f"nl_extract failed: {exc}"]
                    else:
                        nl_intent = (nl_result.intent or "FILL").upper()
                        if nl_result.errors:
                            errors = list(nl_result.errors)
                        elif not nl_result.fields and nl_intent == "FILL":
                            # The LLM produced valid JSON but omitted every
                            # field — typical when the reply is terse ("ok",
                            # "继续") and the model decides nothing was
                            # explicitly stated. Fall through to the
                            # deterministic parser, which copes fine with a
                            # single catch-all string field (the whole reply
                            # becomes its value). Leaving nl_attempted True
                            # would otherwise strand the user in an
                            # unrecoverable "no fields extracted" loop.
                            nl_attempted = False
                        else:
                            # Soft-clarify path (free-form continuation):
                            # whatever the extractor produced — fields,
                            # intent, ambiguous_fields — is now used to
                            # decide whether the DAG resumes or whether
                            # the user gets another chance to chat
                            # without blocking on a modal form.
                            # Explicit cancel: same effect as the
                            # deterministic cancel_keyword path.
                            if nl_intent == "CANCEL_ALL":
                                await asyncio.to_thread(
                                    writer.mark_cancelled,
                                    run_id=awaiting.run_id,
                                    reason="user_cancel_nl",
                                )
                                ctx.metadata["meta_clarify_cancelled"] = awaiting
                                ctx.metadata["meta_clarify_cancel_reason"] = (
                                    "user_cancel_nl"
                                )
                                return ctx

                            # Always merge any new fields into the
                            # incremental ``awaiting_filled_json`` so the
                            # next turn starts from the cumulative state.
                            # Carry the prefill audit forward so the
                            # surface keeps rendering ``confirmed_fields``
                            # / ``ambiguous_fields`` markers across the
                            # whole soft-clarify session.
                            cumulative = {**previously_filled, **nl_result.fields}
                            # Persist flat ``{field: value, __prefill_audit__: ...}``
                            # — that's the shape the awaiting-resume path
                            # reads. Wrapping fields under another ``filled``
                            # key would break the next turn's
                            # ``previously_filled`` view.
                            progress_payload: dict[str, Any] = dict(cumulative)
                            if isinstance(prefill_audit, dict) and prefill_audit:
                                progress_payload["__prefill_audit__"] = prefill_audit
                            try:
                                await asyncio.to_thread(
                                    writer.update_awaiting_partial,
                                    run_id=awaiting.run_id,
                                    filled_json=json.dumps(
                                        progress_payload,
                                        ensure_ascii=False,
                                        sort_keys=True,
                                    ),
                                    awaiting_since=time.time(),
                                )
                            except Exception as exc:  # noqa: BLE001
                                log.warning(
                                    "meta_resolution.update_filled_failed",
                                    error=str(exc),
                                )

                            missing_required = [
                                f.name for f in schema.fields
                                if f.required and f.name not in cumulative
                            ]

                            if nl_intent == "PROCEED_NOW":
                                # User explicitly wants to start. Resume if
                                # required fields are satisfied; otherwise
                                # surface a targeted message about what's
                                # still missing and stay in soft-clarify.
                                if missing_required:
                                    ctx.metadata["meta_clarify_proceed_blocked"] = {
                                        "awaiting": awaiting,
                                        "filled": cumulative,
                                        "missing_required": missing_required,
                                    }
                                    return ctx
                                parsed, errors = cumulative, []
                            elif missing_required:
                                # FILL intent but not done yet — let the
                                # turn flow through as a normal LLM chat
                                # response. The model sees
                                # ``meta_clarify_soft_progress`` and can
                                # naturally acknowledge what was captured
                                # while continuing the conversation.
                                ctx.metadata["meta_clarify_soft_progress"] = {
                                    "awaiting": awaiting,
                                    "step_id": awaiting.step_id,
                                    "filled": cumulative,
                                    "newly_filled": list(nl_result.fields.keys()),
                                    "missing_required": missing_required,
                                    "ambiguous_fields": [
                                        {"name": a.name, "reason": a.reason}
                                        for a in nl_result.ambiguous_fields
                                    ],
                                }
                                return ctx
                            else:
                                # All required satisfied without explicit
                                # PROCEED_NOW — resume the DAG so the
                                # workflow moves forward on its own.
                                parsed, errors = cumulative, []
            if not parsed and (
                not schema.nl_extract
                or not nl_attempted
                or is_structured_clarify_form
            ):
                # Bug-X + Bug-Y mirror for the deterministic path: the
                # parser's ``_check_required`` only sees this turn's
                # reply and wipes ``parsed`` to ``{}`` on any error
                # (``clarify_text.py`` returns ``({}, errors)`` whenever
                # the error list is non-empty). In chat mode that means
                # a user filling one required field per turn would
                # always be reprompted because earlier turns' fields
                # appear "missing" to the parser. Relax ``required`` on
                # fields the user has already answered before invoking
                # the parser, then merge the cumulative state on
                # success.
                effective_schema = schema
                if previously_filled:
                    from dataclasses import replace
                    relaxed_fields = tuple(
                        replace(f, required=False)
                        if f.name in previously_filled
                        else f
                        for f in schema.fields
                    )
                    effective_schema = replace(schema, fields=relaxed_fields)
                parsed, errors = parse_clarify_reply(
                    reply_text, effective_schema,
                    surface=getattr(ctx, "surface_kind", "unknown"),
                )
                if not errors and parsed and previously_filled:
                    parsed = {**previously_filled, **parsed}
            if errors and not parsed and reply_text.strip():
                from dataclasses import replace

                relaxed_schema = replace(
                    schema,
                    fields=tuple(replace(field, required=False) for field in schema.fields),
                )
                partial, partial_errors = parse_clarify_reply(
                    reply_text,
                    relaxed_schema,
                    surface=getattr(ctx, "surface_kind", "unknown"),
                )
                if not partial_errors and partial:
                    parsed = {**previously_filled, **partial}
            candidate_fields = {**previously_filled, **parsed}
            infer_optional_fields = (
                is_structured_clarify_form
                and is_empty_clarify_submission(candidate_fields)
            )
            if infer_optional_fields or _clarify_errors_allow_autofill(errors):
                try:
                    inputs_for_autofill = _json_object(awaiting.inputs_json)
                    outputs_for_autofill = _json_object(awaiting.step_outputs_json)
                    filled_auto, completed_auto = await autofill_required_clarify_fields(
                        schema=schema,
                        filled_fields=candidate_fields,
                        user_message=str(inputs_for_autofill.get("user_message") or ""),
                        clarify_reply=reply_text,
                        prior_step_outputs=outputs_for_autofill,
                        llm_chat=ctx.metadata.get("meta_llm_chat"),
                        infer_optional_fields=infer_optional_fields,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "meta_resolution.clarify_autofill_failed",
                        error=str(exc),
                    )
                    filled_auto, completed_auto = candidate_fields, {}
            else:
                filled_auto, completed_auto = candidate_fields, {}
            missing_after_autofill = [
                field.name
                for field in schema.fields
                if field.required and field.name not in filled_auto
            ]
            if completed_auto:
                parsed = filled_auto
                if not missing_after_autofill:
                    errors = []
                ctx.metadata["meta_clarify_autofilled_fields"] = sorted(
                    completed_auto.keys()
                )
            elif infer_optional_fields and not missing_after_autofill:
                parsed = filled_auto
                errors = []
            if errors:
                failure_count = await asyncio.to_thread(
                    writer.increment_parse_failures,
                    run_id=awaiting.run_id,
                )
                if failure_count >= 3:
                    await asyncio.to_thread(
                        writer.mark_cancelled,
                        run_id=awaiting.run_id,
                        reason="parse_failure_limit",
                    )
                    ctx.metadata["meta_clarify_cancelled"] = awaiting
                    ctx.metadata["meta_clarify_cancel_reason"] = (
                        "parse_failure_limit"
                    )
                    return ctx
                ctx.metadata["meta_clarify_errors"] = errors
                ctx.metadata["meta_clarify_reprompt"] = awaiting
                return ctx

            # Parse-success path: the resume CAS belongs before DAG reentry.
            claim = await asyncio.to_thread(
                writer.try_claim_resume,
                run_id=awaiting.run_id,
                session_id=session_id,
            )
            if claim is None:
                ctx.metadata["meta_clarify_race_lost"] = awaiting.run_id
                return ctx
            ctx.metadata["meta_resume"] = (claim, parsed)
            # Proper continuation (awaiting → resume) took over. Drop any
            # stale sticky entry — the DAG is now driving and the trigger
            # path should be inert for the remainder of this run.
            _sticky_drop(session_id)
            return ctx

    # Manual-only mode: automatic activation is disabled. Resume of an in-flight
    # run is handled by the awaiting branch above and still works; here we
    # short-circuit BEFORE any fresh keyword/semantic matching, soft-hint
    # injection, model upgrade, or sticky-cache write, so meta-skills only run
    # via the explicit /meta command.
    from opensquilla.skills.meta.enabled import is_meta_auto_trigger_enabled

    if not is_meta_auto_trigger_enabled(getattr(ctx, "config", None)):
        return ctx

    # ── Original trigger-matching path (with sticky continuation) ──
    catalog = getattr(ctx, "skill_catalog", None)
    if catalog is not None:
        all_skills = list(getattr(catalog, "skills", ()))
    else:
        loader = ctx.metadata.get("skill_loader")
        if loader is None:
            return ctx
        try:
            all_skills = loader.load_all()
        except Exception as exc:  # noqa: BLE001 — fail-open by design
            log.warning("meta_resolution.load_failed", error=str(exc))
            return ctx

    # Use the normalized current user intent for semantic trigger work.
    # Raw/page-dump material can still live in ``ctx.message`` on some direct
    # paths after input normalization, but meta triggers and templates should
    # see the same semantic text. Long pasted chat/page dumps are narrowed to
    # the leading current-intent slice so quoted skill names and historical
    # trigger phrases do not force a DAG.
    semantic_text = _current_semantic_text(ctx)
    trigger_text = _trigger_match_text(semantic_text)
    message_lower = trigger_text.lower()
    if not message_lower:
        return ctx

    pasted_context = _looks_like_pasted_context(semantic_text)
    if pasted_context:
        _sticky_drop(session_id)

    if _is_meta_skill_discussion_intent(semantic_text):
        _sticky_drop(session_id)
        return ctx

    # Sticky-cancel: explicit user opt-out always wins over a stale match.
    if _hits_cancel_keywords(semantic_text, _STICKY_CANCEL_KEYWORDS):
        _sticky_drop(session_id)

    matched: list[tuple[int, str, object, str]] = []
    semantic_candidates: list[tuple[int, str, object, SkillSpec]] = []
    for spec in all_skills:
        if getattr(spec, "kind", "skill") != "meta":
            continue
        if getattr(spec, "disable_model_invocation", False):
            continue
        triggers = getattr(spec, "triggers", None) or []
        try:
            plan = parse_meta_plan(spec)
        except MetaPlanError as exc:
            log.warning(
                "meta_resolution.plan_invalid",
                skill=spec.name,
                error=str(exc),
            )
            continue
        if plan is None:
            continue
        semantic_candidates.append((plan.priority, plan.name, plan, spec))
        if any(
            isinstance(t, str) and t and _trigger_matches(t, message_lower) for t in triggers
        ):
            trigger_phrase = _first_matching_trigger(triggers, message_lower)
            matched.append((plan.priority, plan.name, plan, trigger_phrase))

    sticky_replay = False
    if not matched:
        if _is_skill_marketplace_intent(semantic_text):
            _sticky_drop(session_id)
            return ctx
        if pasted_context:
            return ctx

        # No current trigger — try the sticky cache for this session.
        # The contract: a recent prior turn matched a meta-skill but the
        # LLM never managed to actually fire ``meta_invoke`` (e.g. it
        # length-capped on reasoning, the user closed the form and is
        # now retrying with details, etc.). Replay the stored choice
        # for up to ``_STICKY_MAX_USES`` follow-up turns so meta_invoke
        # stays on the toolbox and trigger-originated tool_choice stays forced.
        sticky = _sticky_get(session_id)
        if sticky is None:
            semantic_match = _semantic_meta_candidate(ctx, semantic_candidates)
            if semantic_match is None:
                return ctx
            matched.append(semantic_match)
            ctx.metadata["meta_match_source"] = "semantic"
        else:
            for spec in all_skills:
                if (
                    getattr(spec, "kind", "skill") != "meta"
                    or getattr(spec, "name", None) != sticky["skill"]
                ):
                    continue
                try:
                    plan = parse_meta_plan(spec)
                except MetaPlanError:
                    continue
                if plan is None:
                    continue
                matched.append((plan.priority, plan.name, plan, sticky["trigger"]))
                break
            if not matched:
                # Skill no longer present (loader changed) — drop stale entry.
                _sticky_drop(session_id)
                return ctx
            _sticky_consume(session_id)
            sticky_replay = True
    else:
        ctx.metadata["meta_match_source"] = "trigger"

    # Highest priority wins; ties broken by name for determinism.
    matched.sort(key=lambda item: (-item[0], item[1]))
    chosen_plan = matched[0][2]
    chosen_trigger = matched[0][3]

    if not sticky_replay:
        # Fresh trigger match — arm/refresh the sticky cache so the next
        # 1-3 turns can replay this choice if the LLM stalls on the
        # current turn.
        _sticky_put(session_id, str(matched[0][1]), str(chosen_trigger))

    # Candidate digest for the hint (priority, name, trigger). The hint
    # surfaces this so the LLM sees runner-ups, not just the
    # priority-winner — important when two meta-skills' triggers
    # overlap and the substring matcher's pick may not be the user's
    # intent (D1 + D2).
    candidate_digest: list[tuple[int, str, str]] = [
        (int(prio), str(name), str(phrase))
        for prio, name, _plan, phrase in matched
    ]

    match = MetaMatch(
        plan=chosen_plan,  # type: ignore[arg-type]
        inputs=make_meta_inputs(
            user_message=semantic_text,
            system_prompt=getattr(ctx, "system_prompt", ""),
            **meta_input_overrides_from_metadata(getattr(ctx, "metadata", {})),
        ),
    )
    ctx.metadata["meta_match"] = match
    ctx.metadata["meta_match_trigger"] = chosen_trigger
    ctx.metadata["meta_match_candidates"] = candidate_digest
    activation_mode = "hint" if str(chosen_trigger) == "semantic" else "recommend"
    ctx.metadata["meta_activation_mode"] = activation_mode
    if sticky_replay:
        ctx.metadata["meta_match_sticky"] = True
        ctx.metadata["meta_match_source"] = "sticky"

    if activation_mode == "recommend":
        ctx.metadata["meta_match_tool_choice"] = {
            "type": "function",
            "function": {"name": "meta_invoke"},
        }

    # Clamp reasoning budget so the LLM cannot length-cap on thinking
    # before producing the forced ``meta_invoke`` call. Applies to both
    # fresh matches and sticky replays — the meta-invoke argument shape
    # never needs deep reasoning.
    _clamp_thinking_for_meta(ctx)

    if getattr(chosen_plan, "name", "") == "meta-skill-creator":
        highest = _highest_text_tier(ctx)
        if highest is not None:
            tier_name, model = highest
            _upgrade_meta_entry_model(
                ctx,
                tier_name=tier_name,
                model=model,
                source="meta-skill-creator",
            )
    else:
        minimum = _text_tier_at_least(ctx, "c2")
        current_tier_key = _tier_sort_key(str(ctx.metadata.get("routed_tier") or ""), 0)[0]
        if minimum is not None and 0 <= current_tier_key < 2:
            tier_name, model = minimum
            _upgrade_meta_entry_model(
                ctx,
                tier_name=tier_name,
                model=model,
                source="meta-skill-entry",
            )

    # ── Soft-hint injection ────────────────────────────────────────────
    # Append to the uncached suffix slot of system_prompt so cache
    # breakpoints upstream stay stable across turns. Both str and tuple
    # shapes are handled the same way as in skills_filter.py. Skipped
    # silently when ctx has no system_prompt attribute (some unit tests
    # construct ctx as a bare SimpleNamespace).
    skill_name = getattr(chosen_plan, "name", "")
    sp = getattr(ctx, "system_prompt", None)
    if skill_name and chosen_trigger and sp is not None:
        hint = _build_hint(
            skill_name,
            chosen_trigger,
            candidate_digest,
            activation_mode=activation_mode,
        )
        if isinstance(sp, str):
            base, suffix = sp, ""
        else:
            base, suffix = sp
        new_suffix = f"{suffix}{hint}" if suffix else hint
        ctx.system_prompt = (base, new_suffix)

    log.info(
        "meta_resolution.matched",
        meta_skill=skill_name,
        trigger=chosen_trigger,
        activation_mode=activation_mode,
        candidates=len(matched),
        sticky_replay=sticky_replay,
        # D1: log all candidate names + priorities so operators can
        # spot trigger overlaps from logs without re-running the turn.
        candidate_list=[(n, p) for p, n, _t in candidate_digest],
        # Include the head of the actual input so an operator can
        # diagnose accidental fires from the log alone.
        message_head=semantic_text[:200],
        trigger_scan_head=trigger_text[:200],
    )
    return ctx
