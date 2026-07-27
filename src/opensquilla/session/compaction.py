"""Context window compaction — summarize older messages to free token budget."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

import httpx
import structlog

from opensquilla.env import trust_env as _trust_env
from opensquilla.provider.app_attribution import provider_app_headers
from opensquilla.provider.protocol import provider_connection_config
from opensquilla.provider.tokenrhythm_correlation import (
    tokenrhythm_correlation_headers,
)
from opensquilla.session.compaction_state import (
    build_structured_summary_from_text,
    extract_compaction_obligations,
)

if TYPE_CHECKING:
    from opensquilla.provider.types import ProviderRequestCorrelation

log = structlog.get_logger(__name__)

_COMPACTION_TIMEOUT = 90.0
_MAX_CUSTOM_INSTRUCTIONS_CHARS = 2000
CompactionProfile = Literal["conversation", "coding", "research", "support"]
CompactionTrigger = Literal["token_budget", "message_count"]


@dataclass
class CompactionConfig:
    base_chunk_ratio: float = 0.4
    min_chunk_ratio: float = 0.15
    safety_margin: float = 1.2
    default_parts: int = 2
    identifier_policy: str = "strict"  # strict | custom | off
    model: str | None = None  # None = use session model
    api_key: str = ""
    base_url: str = "https://openrouter.ai/api/v1"
    timeout_seconds: float = 90.0
    provider: str = ""
    coverage_blocking: bool = False
    compaction_profile: CompactionProfile = "conversation"
    protected_recent_messages: int = 0


@dataclass
class CompactionRequest:
    session_id: str
    entries: list[dict[str, Any]]  # list of {role, content, token_count?}
    context_window_tokens: int
    config: CompactionConfig = field(default_factory=CompactionConfig)
    custom_instructions: str | None = None
    # Optional caller-selected prefix boundary for non-token compaction.  The
    # compactor validates that the exact boundary preserves the configured
    # protected tail and tool-call pairing; it never silently chooses another
    # cut when this is set.
    forced_prefix_cut: int | None = None
    trigger: CompactionTrigger = "token_budget"
    reason: str | None = None
    provider_request_correlation: ProviderRequestCorrelation | None = field(
        default=None,
        repr=False,
    )


@dataclass
class CompactionResult:
    summary: str
    kept_entries: list[dict[str, Any]]
    removed_count: int
    chunks_processed: int
    summary_source: str = "unknown"  # skipped | fallback | llm | mixed | unknown
    tokens_before: int = 0
    tokens_after: int = 0
    remaining_budget_tokens: int = 0
    summary_payload: dict[str, Any] | None = None
    summary_format: str = "text"
    coverage_status: str = "unknown"
    missing_obligations: list[str] | None = None
    critical_carry_forward: list[str] | None = None
    skip_reason: str | None = None
    quality_report: dict[str, Any] = field(default_factory=dict)
    # Index in the original request.entries at which kept_entries begins.
    # Prefix-only compaction therefore guarantees kept_start_index ==
    # removed_count on successful results.  Zero also covers every no-op.
    kept_start_index: int = 0


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    get_secret_value = getattr(value, "get_secret_value", None)
    if callable(get_secret_value):
        value = get_secret_value()
    return str(value).strip()


def build_compaction_config_from_provider(
    provider: Any | None,
    *,
    model_override: str | None = None,
    default_model: str | None = None,
    compaction_config: Any | None = None,
) -> CompactionConfig:
    """Build CompactionConfig from a resolved provider without owning selection."""

    timeout_seconds = getattr(compaction_config, "timeout_seconds", _COMPACTION_TIMEOUT)
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError):
        timeout = _COMPACTION_TIMEOUT

    cfg = CompactionConfig(timeout_seconds=timeout)
    for attr in (
        "compaction_profile",
        "protected_recent_messages",
    ):
        if compaction_config is not None and hasattr(compaction_config, attr):
            setattr(cfg, attr, getattr(compaction_config, attr))
    if compaction_config is not None and not bool(getattr(compaction_config, "enabled", True)):
        return cfg

    configured_model = getattr(compaction_config, "model", None) if compaction_config else None
    connection_config = provider_connection_config(provider)
    api_key = connection_config.api_key
    model = connection_config.model
    base_url = connection_config.base_url

    cfg.api_key = api_key
    cfg.model = configured_model or model_override or model or default_model
    cfg.provider = connection_config.provider_kind
    if base_url:
        cfg.base_url = base_url
    return cfg


def compact_accepts_config(compact_fn: Any) -> bool:
    """Return whether a compact callable can accept the optional config arg."""

    side_effect = getattr(compact_fn, "side_effect", None)
    if callable(side_effect):
        compact_fn = side_effect

    try:
        params = list(inspect.signature(compact_fn).parameters.values())
    except (TypeError, ValueError):
        return True

    variadic_kinds = {
        inspect.Parameter.VAR_POSITIONAL,
        inspect.Parameter.VAR_KEYWORD,
    }
    if any(p.kind in variadic_kinds for p in params):
        return True
    if any(p.name == "config" for p in params):
        return True

    positional_kinds = {
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }
    return len([p for p in params if p.kind in positional_kinds]) >= 3


async def call_compact_with_optional_config(
    compact_fn: Any,
    session_key: str,
    context_window_tokens: int,
    config: CompactionConfig | None,
    *,
    provider_request_correlation: ProviderRequestCorrelation | None = None,
) -> str:
    """Call compact with config only when the target supports the argument."""

    kwargs: dict[str, Any] = {}
    try:
        parameters = tuple(inspect.signature(compact_fn).parameters.values())
    except (TypeError, ValueError):
        parameters = ()
    if provider_request_correlation is not None and any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or parameter.name == "provider_request_correlation"
        for parameter in parameters
    ):
        kwargs["provider_request_correlation"] = provider_request_correlation
    if config is not None and compact_accepts_config(compact_fn):
        return cast(
            str,
            await compact_fn(
                session_key,
                context_window_tokens,
                config,
                **kwargs,
            ),
        )
    return cast(
        str,
        await compact_fn(session_key, context_window_tokens, **kwargs),
    )


def _estimate_tokens(text: str) -> int:
    """Delegate to centralized tokenizer (tiktoken with len//4 fallback)."""
    from opensquilla.session.tokenizer import estimate_tokens

    return estimate_tokens(text)


def _entry_get(entry: Any, key: str, default: Any = None) -> Any:
    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def estimate_entry_replay_tokens(entry: Any) -> int:
    """Estimate the compaction-input size of a persisted transcript entry."""

    content = _entry_get(entry, "content") or ""
    token_count = _entry_get(entry, "token_count")
    try:
        persisted_tokens = int(token_count or 0)
    except (TypeError, ValueError):
        persisted_tokens = 0
    content_tokens = persisted_tokens or (_estimate_tokens(str(content)) if content else 0)

    extra_parts: list[str] = []
    tool_calls = _entry_get(entry, "tool_calls")
    if tool_calls:
        tool_summary = _summarize_tool_calls_for_llm(tool_calls)
        extra_parts.append(tool_summary or _json_text(tool_calls))
    tool_call_id = _entry_get(entry, "tool_call_id")
    if tool_call_id:
        extra_parts.append(str(tool_call_id))
    reasoning_content = _entry_get(entry, "reasoning_content")
    if reasoning_content:
        extra_parts.append(
            "[assistant reasoning omitted from compaction input: "
            f"{len(str(reasoning_content))} chars]"
        )
    extra_tokens = _estimate_tokens("\n".join(extra_parts)) if extra_parts else 0
    return content_tokens + extra_tokens


def estimate_entry_model_replay_tokens(entry: Any) -> int:
    """Estimate the full transcript payload size replayed to the model."""

    content = _entry_get(entry, "content") or ""
    token_count = _entry_get(entry, "token_count")
    try:
        persisted_tokens = int(token_count or 0)
    except (TypeError, ValueError):
        persisted_tokens = 0
    content_tokens = persisted_tokens or (_estimate_tokens(str(content)) if content else 0)

    extra_parts: list[str] = []
    tool_calls = _entry_get(entry, "tool_calls")
    if tool_calls:
        extra_parts.append(_json_text(tool_calls))
    tool_call_id = _entry_get(entry, "tool_call_id")
    if tool_call_id:
        extra_parts.append(str(tool_call_id))
    reasoning_content = _entry_get(entry, "reasoning_content")
    if reasoning_content:
        extra_parts.append(str(reasoning_content))
    extra_tokens = _estimate_tokens("\n".join(extra_parts)) if extra_parts else 0
    return content_tokens + extra_tokens


def _entry_tokens(entry: dict[str, Any]) -> int:
    # Budget/skip/cut decisions must measure what the model actually replays
    # (the full tool_calls JSON), NOT the summarized compaction-LLM input. The
    # preflight trigger (runtime.py) uses the model-replay estimator; using the
    # smaller summarized estimate here made compaction veto itself on
    # tool-heavy transcripts that genuinely overflow the window.
    return estimate_entry_model_replay_tokens(entry)


def _profile_protected_recent_messages(cfg: CompactionConfig) -> int:
    configured = max(0, int(getattr(cfg, "protected_recent_messages", 0) or 0))
    if configured:
        return configured
    profile = str(getattr(cfg, "compaction_profile", "conversation") or "conversation")
    if profile in {"coding", "research", "support"}:
        return 12
    return 0


def _apply_protected_tail(
    entries: list[dict[str, Any]],
    cut: int,
    cfg: CompactionConfig,
) -> int:
    protected_recent = _profile_protected_recent_messages(cfg)
    if protected_recent <= 0:
        return cut
    protected_start = max(0, len(entries) - protected_recent)
    return min(cut, protected_start)


def _retreat_to_turn_boundary(entries: list[dict[str, Any]], cut: int) -> int:
    """Move cut earlier until it does not orphan a kept tool result."""

    while cut > 0:
        first_kept = entries[cut] if cut < len(entries) else None
        if _is_tool_result_entry(first_kept):
            result_start = cut
            while result_start > 0 and _is_tool_result_entry(entries[result_start - 1]):
                result_start -= 1
            if result_start > 0 and _is_assistant_tool_call_entry(
                entries[result_start - 1]
            ):
                cut = result_start - 1
                continue
            if result_start != cut:
                cut = result_start
                continue
        if not (
            _is_assistant_tool_call_entry(entries[cut - 1])
            and _is_tool_result_entry(first_kept)
        ):
            return cut
        cut -= 1
    return 0


def _validate_forced_prefix_cut(
    entries: list[dict[str, Any]],
    cut: int | None,
    cfg: CompactionConfig,
) -> tuple[int | None, str | None]:
    """Validate a caller-owned prefix cut without silently changing it."""

    if cut is None:
        return None, None
    if isinstance(cut, bool) or not isinstance(cut, int):
        return None, "invalid_forced_prefix_cut"
    if cut <= 0 or cut > len(entries):
        return None, "invalid_forced_prefix_cut"
    if _apply_protected_tail(entries, cut, cfg) != cut:
        return None, "forced_prefix_cut_overlaps_protected_tail"
    if _retreat_to_turn_boundary(entries, cut) != cut:
        return None, "forced_prefix_cut_splits_tool_segment"
    return cut, None


def _compaction_quality_report(
    *,
    cfg: CompactionConfig,
    entries: list[dict[str, Any]],
    kept: list[dict[str, Any]],
    tokens_before: int,
    tokens_after: int,
    removed_count: int,
    context_window_tokens: int,
    trigger: CompactionTrigger = "token_budget",
) -> dict[str, Any]:
    protected_recent = _profile_protected_recent_messages(cfg)
    protected_tail_preserved = True
    if protected_recent > 0:
        protected_tail = entries[-protected_recent:]
        protected_tail_preserved = (
            len(kept) >= len(protected_tail)
            and kept[-len(protected_tail) :] == protected_tail
        )
    compression_ratio = (
        float(tokens_after) / float(tokens_before)
        if tokens_before > 0
        else 1.0
    )
    fits_context_window = bool(tokens_after * cfg.safety_margin <= context_window_tokens)
    reduces_tokens = tokens_after < tokens_before
    # Message-count recovery removes wire-message cardinality rather than
    # necessarily reducing token usage.  It remains safe only when the result
    # still fits the context window.  The default token-budget path retains its
    # historical strict token-reduction gate.
    passes_structural_gate = bool(
        removed_count > 0
        and protected_tail_preserved
        and (
            reduces_tokens
            or (trigger == "message_count" and fits_context_window)
        )
    )
    return {
        "profile": str(getattr(cfg, "compaction_profile", "conversation") or "conversation"),
        "protected_recent_messages": protected_recent,
        "protected_tail_preserved": protected_tail_preserved,
        "compression_ratio": compression_ratio,
        "fits_context_window": fits_context_window,
        "passes_structural_gate": passes_structural_gate,
    }


def _chunk_entries(entries: list[dict[str, Any]], chunk_ratio: float) -> list[list[dict[str, Any]]]:
    """Split entries into chunks based on ratio of total entries."""
    if not entries:
        return []
    chunk_size = max(1, int(len(entries) * chunk_ratio))
    return [entries[i : i + chunk_size] for i in range(0, len(entries), chunk_size)]


def _build_strict_identifier_instruction() -> str:
    return (
        "IMPORTANT: Preserve all opaque identifiers exactly as written — "
        "UUIDs, hashes, IDs, tokens, API keys, hostnames, IPs, ports, URLs, file names. "
        "Do NOT shorten, reconstruct, or paraphrase any identifier."
    )


def _summarize_if_envelope(content: str) -> str:
    """Replace attachment-envelope JSON with a concise placeholder.

    User messages carrying images are persisted as
    ``{"text": "...", "attachments": [{"type": "image/png", "data": "<base64>"}...]}``
    (see gateway/rpc_sessions.py:_persist_user_message). Feeding the raw JSON
    blob to the compaction LLM wastes context on base64 and confuses the summary.
    Detect the envelope shape and return ``text`` plus a short attachment
    descriptor instead. Non-envelope strings pass through unchanged.
    """
    if not content.startswith('{"text":'):
        return content
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return content
    if not isinstance(parsed, dict) or "text" not in parsed:
        return content
    text = parsed.get("text")
    if not isinstance(text, str):
        return content
    atts = parsed.get("attachments") or []
    if not isinstance(atts, list) or not atts:
        return text
    descs: list[str] = []
    for att in atts:
        if not isinstance(att, dict):
            continue
        name = att.get("name") or "image"
        media = att.get("type") or "image/*"
        descs.append(f"{name} ({media})")
    if descs:
        return f"{text}\n[user attached: {', '.join(descs)}]"
    return text


def _preview_text(text: str, max_chars: int = 240) -> str:
    if len(text) <= max_chars:
        return text
    head_chars = max_chars // 2
    tail_chars = max_chars - head_chars
    omitted = len(text) - head_chars - tail_chars
    return f"{text[:head_chars]}\n[...omitted {omitted} chars...]\n{text[-tail_chars:]}"


def _summarize_tool_value(value: Any) -> str:
    if isinstance(value, str):
        if len(value) <= 240:
            return value
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        return f"<string chars={len(value)} sha256={digest} preview={_preview_text(value)!r}>"
    if isinstance(value, (int, float, bool)) or value is None:
        return repr(value)
    rendered = _json_text(value)
    if len(rendered) <= 240:
        return rendered
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]
    return f"<json chars={len(rendered)} sha256={digest} preview={_preview_text(rendered)!r}>"


def _summarize_tool_calls_for_llm(tool_calls: Any) -> str:
    if not isinstance(tool_calls, list) or not tool_calls:
        return ""
    lines = ["[tool payload summary]"]
    for index, segment in enumerate(tool_calls, start=1):
        if not isinstance(segment, dict):
            lines.append(f"- segment {index}: {type(segment).__name__}")
            continue
        seg_type = segment.get("type") or "unknown"
        if seg_type == "tool_use" or isinstance(segment.get("function"), dict):
            tool_name = segment.get("name") or segment.get("function", {}).get("name") or "unknown"
            tool_id = segment.get("tool_use_id") or segment.get("id") or "unknown"
            raw_input = segment.get("input")
            if raw_input is None and isinstance(segment.get("function"), dict):
                raw_input = segment["function"].get("arguments")
            if isinstance(raw_input, str):
                try:
                    parsed_input = json.loads(raw_input)
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed_input = {"_raw": raw_input}
            elif isinstance(raw_input, dict):
                parsed_input = raw_input
            else:
                parsed_input = {}
            keys = sorted(str(key) for key in parsed_input)
            lines.append(f"- tool_use {tool_id}: {tool_name} keys={keys}")
            for key in keys:
                lines.append(f"  {key}: {_summarize_tool_value(parsed_input.get(key))}")
            continue
        if seg_type == "tool_result":
            result = segment.get("result", "")
            rendered = result if isinstance(result, str) else _json_text(result)
            digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]
            lines.append(
                "- tool_result "
                f"{segment.get('tool_use_id') or 'unknown'}: "
                f"is_error={bool(segment.get('is_error'))} "
                f"chars={len(rendered)} sha256={digest} "
                f"preview={_preview_text(rendered)!r}"
            )
            continue
        if seg_type == "text":
            text = str(segment.get("text") or "")
            lines.append(f"- text chars={len(text)} preview={_preview_text(text)!r}")
            continue
        lines.append(f"- {seg_type} keys={sorted(str(key) for key in segment)}")
    return "\n".join(lines)


def _format_chunk_for_llm(chunk: list[dict[str, Any]]) -> str:
    """Format conversation entries into readable text for the compaction LLM."""
    lines: list[str] = []
    for entry in chunk:
        role = entry.get("role", "unknown")
        content = _summarize_if_envelope(str(entry.get("content") or ""))
        rendered_parts = [f"[{role}]: {content}"]
        tool_summary = _summarize_tool_calls_for_llm(entry.get("tool_calls"))
        if tool_summary:
            rendered_parts.append(tool_summary)
        reasoning_content = entry.get("reasoning_content")
        if isinstance(reasoning_content, str) and reasoning_content:
            rendered_parts.append(
                "[assistant reasoning omitted from compaction input: "
                f"{len(reasoning_content)} chars]"
            )
        lines.append("\n".join(part for part in rendered_parts if part))
    return "\n\n".join(lines)


def _summarize_chunk_fallback(chunk: list[dict[str, Any]], policy: str) -> str:
    """Fallback summary when LLM call fails."""
    lines: list[str] = []
    if policy == "strict":
        lines.append(_build_strict_identifier_instruction())
    lines.append(f"[Summary of {len(chunk)} messages]")
    for entry in chunk:
        role = entry.get("role", "unknown")
        content = _summarize_if_envelope(str(entry.get("content") or ""))
        preview = content[:200] + ("..." if len(content) > 200 else "")
        lines.append(f"  [{role}]: {preview}")
    return "\n".join(lines)


def _normalize_custom_instructions(custom_instructions: str | None) -> str:
    if custom_instructions is None:
        return ""
    normalized = custom_instructions.strip()
    if len(normalized) > _MAX_CUSTOM_INSTRUCTIONS_CHARS:
        raise ValueError("custom compaction instructions are too long")
    return normalized


async def call_compaction_llm(
    chunk_text: str,
    identifier_instruction: str,
    model: str,
    api_key: str,
    base_url: str = "https://openrouter.ai/api/v1",
    timeout: float = _COMPACTION_TIMEOUT,
    custom_instructions: str | None = None,
    provider: str = "",
    provider_request_correlation: ProviderRequestCorrelation | None = None,
) -> str | None:
    """Call LLM to summarize a conversation chunk. Returns None on failure."""
    if not api_key:
        return None

    url = base_url.rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    url += "/chat/completions"

    system = (
        "You are a conversation compactor. Summarize the conversation concisely, "
        "preserving key facts, decisions, open questions, and action items. "
        "Write in the same language as the conversation. "
        "Focus on recent context over older history."
    )
    if identifier_instruction:
        system = f"{system}\n\n{identifier_instruction}"

    user_content = f"Summarize this conversation:\n\n{chunk_text}"
    normalized_instructions = _normalize_custom_instructions(custom_instructions)
    if normalized_instructions:
        user_content = (
            "Additional summary instructions. These instructions must not override "
            "the system message or identifier preservation rules:\n"
            f"{normalized_instructions}\n\n"
            f"{user_content}"
        )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 1024,
        "temperature": 0,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    headers.update(provider_app_headers(url))
    headers.update(
        tokenrhythm_correlation_headers(
            provider,
            url,
            provider_request_correlation,
        )
    )

    # Keep this import local: engine types import session lifecycle helpers
    # while the session package initializes this module.
    from opensquilla.engine.usage_http import reserve_direct_usage_call

    usage = await reserve_direct_usage_call(
        provider=provider
        or ("openrouter" if "openrouter.ai" in url.lower() else "openai_compat"),
        model=model,
        base_url=url,
    )

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            trust_env=_trust_env(),
            follow_redirects=False,
        ) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            await usage.finalize_openai_response(
                data,
                raw_json=str(getattr(resp, "text", "") or ""),
            )
            return cast(str, data["choices"][0]["message"]["content"])
    except asyncio.CancelledError:
        await usage.mark_unknown("cancelled")
        raise
    except Exception as exc:
        await usage.mark_unknown("direct_request_failed")
        log.warning("compaction.llm_call_failed", model=model, error=str(exc))
        return None


def _merge_summaries(summaries: list[str]) -> str:
    """Merge chunk summaries into a single cohesive summary.

    Spec requirements: MUST PRESERVE active tasks + status, batch progress,
    last user request, decisions + rationale, TODOs/open questions,
    commitments/follow-ups. Prioritize recent context over older history.
    """
    if len(summaries) == 1:
        return summaries[0]
    merged_lines = ["[Merged context summary]"]
    # Later summaries (more recent) appear last — they take priority
    for i, summary in enumerate(summaries):
        merged_lines.append(f"\n--- Part {i + 1} ---\n{summary}")
    return "\n".join(merged_lines)


def _is_assistant_tool_call_entry(entry: dict[str, Any]) -> bool:
    if entry.get("role") != "assistant":
        return False
    if entry.get("tool_calls"):
        return True
    content = str(entry.get("content") or "")
    return "[tool_call:" in content or "[Used tool:" in content


def _is_tool_result_entry(entry: dict[str, Any] | None) -> bool:
    if entry is None:
        return False
    if entry.get("role") == "tool" or entry.get("tool_call_id"):
        return True
    content = str(entry.get("content") or "").lstrip()
    return content.startswith("[Tool result ")


def _find_turn_boundary_cut(
    entries: list[dict[str, Any]],
    keep_budget: int,
) -> int:
    """Return the index of the first entry to keep (the cut point).

    The cut is placed at a turn boundary — where the last removed entry is
    NOT an assistant message with a pending tool call, and the first kept
    entry is NOT a tool result that belongs to a removed tool call.

    Strategy:
    1. Start from the token-budget cut (walk from the end, accumulate up to budget).
    2. Walk backward from that cut until we find a boundary that is NOT
       mid-turn (i.e. the cut does not orphan a tool_call/tool_result pair).
    3. If the only possible cut still splits a tool call from its result,
       return 0 so the caller skips compaction instead of orphaning tool state.
    """
    if not entries:
        return 0

    # Compute the legacy cut index: walk from the end, accumulate up to budget.
    kept_tokens = 0
    legacy_keep_start = len(entries)
    for i in range(len(entries) - 1, -1, -1):
        t = _entry_tokens(entries[i])
        if kept_tokens + t <= keep_budget:
            kept_tokens += t
            legacy_keep_start = i
        else:
            break

    if legacy_keep_start == 0:
        # Nothing to remove; caller handles no-op.
        return 0

    # Walk backward from legacy_keep_start toward index 1 looking for a clean
    # turn boundary. A clean boundary: the last removed entry (index cut-1)
    # is NOT an assistant message that ends with a tool call whose result is
    # the first kept entry.
    cut = legacy_keep_start
    while cut > 0:
        last_removed = entries[cut - 1]
        first_kept = entries[cut] if cut < len(entries) else None

        # Mid-turn: assistant tool call removed, tool result would be first kept.
        if _is_assistant_tool_call_entry(last_removed) and _is_tool_result_entry(
            first_kept
        ):
            # Move cut one step earlier to avoid splitting the pair.
            cut -= 1
            continue

        # Clean boundary found.
        break

    return cut


async def compact_context_new(request: CompactionRequest) -> CompactionResult:
    """Cut-point + turn-boundary-aware + incremental-summary compaction (compaction).

    Differences from :func:`compact_context_legacy`:

    * **Turn-boundary cut**: the split point avoids orphaning an assistant tool
      call from its paired tool result.  The legacy path uses a pure
      token-budget split that may land mid-turn.
    * **Previous-summary prefix**: if ``request.custom_instructions`` carries a
      ``__prev_summary__:<text>`` marker the summary is prepended to the new
      chunk summary so incremental context accumulates.  Normal custom
      instructions are unaffected.

    Everything else (chunk splitting, LLM calls, fallback, token accounting)
    is identical to the legacy path so the eval gate can compare the two
    implementations on the same sessions.
    """
    cfg = request.config
    entries = request.entries
    window = request.context_window_tokens
    total_tokens = sum(_entry_tokens(e) for e in entries)

    # Extract an optional previous-summary prefix injected by the caller.
    # Convention: ``custom_instructions`` may carry ``__prev_summary__:<text>``
    # as the first line.  Strip it before forwarding to ``_normalize_custom_instructions``.
    raw_ci = request.custom_instructions or ""
    prev_summary: str = ""
    if raw_ci.startswith("__prev_summary__:"):
        first_newline = raw_ci.find("\n")
        if first_newline == -1:
            prev_summary = raw_ci[len("__prev_summary__:") :]
            raw_ci = ""
        else:
            prev_summary = raw_ci[len("__prev_summary__:") : first_newline]
            raw_ci = raw_ci[first_newline + 1 :]
    custom_instructions = _normalize_custom_instructions(raw_ci or None)

    if not entries:
        return CompactionResult(
            summary="",
            kept_entries=[],
            removed_count=0,
            chunks_processed=0,
            summary_source="skipped",
            tokens_before=0,
            tokens_after=0,
            remaining_budget_tokens=max(window, 0),
            skip_reason="no_entries",
        )

    forced_cut, forced_cut_error = _validate_forced_prefix_cut(
        entries,
        request.forced_prefix_cut,
        cfg,
    )
    if forced_cut_error is not None:
        return CompactionResult(
            summary="",
            kept_entries=entries,
            removed_count=0,
            chunks_processed=0,
            summary_source="skipped",
            tokens_before=total_tokens,
            tokens_after=total_tokens,
            remaining_budget_tokens=max(window - total_tokens, 0),
            skip_reason=forced_cut_error,
        )

    # If we're within budget, no token-driven compaction is needed.  A valid
    # forced prefix cut is an independent cardinality recovery request and must
    # still run even when the transcript already fits the token window.
    if forced_cut is None and total_tokens * cfg.safety_margin <= window:
        return CompactionResult(
            summary="",
            kept_entries=entries,
            removed_count=0,
            chunks_processed=0,
            summary_source="skipped",
            tokens_before=total_tokens,
            tokens_after=total_tokens,
            remaining_budget_tokens=max(window - total_tokens, 0),
            skip_reason="within_compaction_budget",
        )

    if forced_cut is not None:
        # The caller already projected a sufficient count reduction.  Preserve
        # its exact structured tail; validation above refuses unsafe boundaries
        # instead of retreating to a different one.
        cut = forced_cut
    else:
        keep_budget = window // 2
        # compaction: use turn-boundary-aware cut instead of raw token split.
        cut = _find_turn_boundary_cut(entries, keep_budget)
        cut = _retreat_to_turn_boundary(
            entries,
            _apply_protected_tail(entries, cut, cfg),
        )
    kept = entries[cut:]
    to_compact = entries[:cut]

    if not to_compact:
        skip_reason = "no_safe_turn_boundary"
        if _profile_protected_recent_messages(cfg) > 0:
            skip_reason = "protected_tail_exhausts_compaction_window"
        return CompactionResult(
            summary="",
            kept_entries=entries,
            removed_count=0,
            chunks_processed=0,
            summary_source="skipped",
            tokens_before=total_tokens,
            tokens_after=total_tokens,
            remaining_budget_tokens=max(window - total_tokens, 0),
            skip_reason=skip_reason,
        )

    if request.trigger == "message_count":
        # The caller has already found the smallest sufficient prefix cut.
        # Summarize that prefix in one request: chunking would turn one exact
        # provider recovery into several unaccounted, independently-timed LLM
        # calls without improving the cardinality result.
        chunks = [to_compact]
    else:
        chunk_ratio = max(cfg.min_chunk_ratio, cfg.base_chunk_ratio / cfg.default_parts)
        chunks = _chunk_entries(to_compact, chunk_ratio)

    id_instruction = (
        _build_strict_identifier_instruction() if cfg.identifier_policy == "strict" else ""
    )

    summaries: list[str] = []
    llm_chunks = 0
    fallback_chunks = 0
    for chunk in chunks:
        if cfg.api_key and cfg.model:
            llm_kwargs: dict[str, Any] = {}
            if request.provider_request_correlation is not None:
                llm_kwargs["provider_request_correlation"] = (
                    request.provider_request_correlation
                )
            llm_result = await call_compaction_llm(
                chunk_text=_format_chunk_for_llm(chunk),
                identifier_instruction=id_instruction,
                model=cfg.model,
                api_key=cfg.api_key,
                base_url=cfg.base_url,
                timeout=cfg.timeout_seconds,
                custom_instructions=custom_instructions or None,
                provider=cfg.provider,
                **llm_kwargs,
            )
            if llm_result:
                summaries.append(llm_result)
                llm_chunks += 1
                continue
        summaries.append(_summarize_chunk_fallback(chunk, cfg.identifier_policy))
        fallback_chunks += 1

    merged = _merge_summaries(summaries)

    # Prepend previous summary when present (incremental accumulation).
    if prev_summary:
        merged = f"[Previous context]\n{prev_summary}\n\n[New context]\n{merged}"

    tokens_after = _estimate_tokens(merged) + sum(_entry_tokens(e) for e in kept)
    if llm_chunks and fallback_chunks:
        summary_source = "mixed"
    elif llm_chunks:
        summary_source = "llm"
    else:
        summary_source = "fallback"

    obligations = extract_compaction_obligations(to_compact)
    structured_summary, coverage = build_structured_summary_from_text(
        merged,
        obligations,
        block_missing_critical=cfg.coverage_blocking,
    )
    if coverage.blocked:
        quality_report = _compaction_quality_report(
            cfg=cfg,
            entries=entries,
            kept=entries,
            tokens_before=total_tokens,
            tokens_after=total_tokens,
            removed_count=0,
            context_window_tokens=window,
            trigger=request.trigger,
        )
        log.warning(
            "compaction.coverage_blocked",
            missing_obligations=len(coverage.missing_obligations),
            checked_obligations=coverage.checked_obligations,
        )
        return CompactionResult(
            summary="",
            kept_entries=entries,
            removed_count=0,
            chunks_processed=len(chunks),
            summary_source=summary_source,
            tokens_before=total_tokens,
            tokens_after=total_tokens,
            remaining_budget_tokens=max(window - total_tokens, 0),
            summary_payload=structured_summary.model_dump(mode="json"),
            summary_format="structured_v1",
            coverage_status=coverage.status,
            missing_obligations=coverage.missing_obligations,
            critical_carry_forward=coverage.critical_carry_forward,
            skip_reason="coverage_blocked",
            quality_report=quality_report,
        )

    log.info(
        "compaction.new.done",
        removed=len(to_compact),
        kept=len(kept),
        chunks=len(chunks),
        llm_model=cfg.model or "fallback",
        summary_source=summary_source,
        prev_summary_chars=len(prev_summary),
    )

    quality_report = _compaction_quality_report(
        cfg=cfg,
        entries=entries,
        kept=kept,
        tokens_before=total_tokens,
        tokens_after=tokens_after,
        removed_count=len(to_compact),
        context_window_tokens=window,
        trigger=request.trigger,
    )
    if not bool(quality_report.get("passes_structural_gate", False)):
        log.warning(
            "compaction.quality_gate_failed",
            profile=quality_report.get("profile"),
            protected_tail_preserved=quality_report.get("protected_tail_preserved"),
            compression_ratio=quality_report.get("compression_ratio"),
            fits_context_window=quality_report.get("fits_context_window"),
        )
        return CompactionResult(
            summary="",
            kept_entries=entries,
            removed_count=0,
            chunks_processed=len(chunks),
            summary_source=summary_source,
            tokens_before=total_tokens,
            tokens_after=total_tokens,
            remaining_budget_tokens=max(window - total_tokens, 0),
            summary_payload=structured_summary.model_dump(mode="json"),
            summary_format="structured_v1",
            coverage_status=coverage.status,
            missing_obligations=coverage.missing_obligations,
            critical_carry_forward=coverage.critical_carry_forward,
            skip_reason="quality_gate_failed",
            quality_report=quality_report,
        )

    return CompactionResult(
        summary=merged,
        kept_entries=kept,
        removed_count=len(to_compact),
        chunks_processed=len(chunks),
        summary_source=summary_source,
        tokens_before=total_tokens,
        tokens_after=tokens_after,
        remaining_budget_tokens=max(window - tokens_after, 0),
        summary_payload=structured_summary.model_dump(mode="json"),
        summary_format="structured_v1",
        coverage_status=coverage.status,
        missing_obligations=coverage.missing_obligations,
        critical_carry_forward=coverage.critical_carry_forward,
        quality_report=quality_report,
        kept_start_index=cut,
    )


async def compact_context(request: CompactionRequest) -> CompactionResult:
    """Summarize older messages to free context-window budget.

    Delegates to :func:`compact_context_new` — the compaction cut-point +
    turn-boundary-aware pipeline.  The public signature is unchanged so
    every existing call site keeps working without modification.
    """
    return await compact_context_new(request)
