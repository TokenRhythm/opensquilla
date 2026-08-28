"""Memory flush plan — pre-compaction save via sub-agent."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any

SILENT_REPLY_TOKEN = "[SILENT_REPLY_TOKEN]"
DEFAULT_FLUSH_ARCHIVE_MAX_BYTES = 800_000
FLUSH_RESERVED_PATH_PREFIXES = ("memory/.raw_fallbacks/", "memory/.checkpoints/")
FLUSH_RESERVED_PATHS = frozenset(
    {"MEMORY.md", "AGENTS.md", "USER.md", "SOUL.md", "TOOLS.md"}
)

FLUSH_SYSTEM_PROMPT_TEMPLATE = """\
Pre-compaction memory flush.
Store durable memories only in {relative_path} (create the memory/ directory if needed).
If {relative_path} already exists, APPEND new content only — do not overwrite existing entries.
Workspace files such as MEMORY.md, SOUL.md, TOOLS.md, and AGENTS.md are READ-ONLY during this flush.
Do NOT create timestamped variant files; always use the canonical {relative_path} filename.
If there is nothing worth storing, reply with {silent_token}.
"""

FLUSH_USER_PROMPT_TEMPLATE = """\
Below is a transcript excerpt from the current session. Review it and save any \
important context, decisions, facts, or user preferences to {relative_path}.

Fidelity rules:
- Preserve atomic facts with enough detail to answer later questions directly.
- For personal-history conversations, retain specific named-person events, activities, \
relationships, preferences, goals, and dated or relative-time statements; do not replace \
specific events with only broad category summaries.
- Resolve relative dates such as yesterday, tomorrow, and last year when a \
transcript line includes a [opensquilla-message: date=...] source date.
- If a message contains a dated pasted transcript header, use that header date \
for facts inside the pasted transcript; if a source line includes an inline id \
such as [dia_id: D1:3], use that id as the source anchor.
- For each factual bullet derived from a opensquilla-message line, append a source \
comment in this exact key order: date, message, optional anchor. Example: \
<!-- opensquilla-source: date=YYYY-MM-DD message=N anchor=ANCHOR -->.

If there is nothing worth storing, reply with {silent_token}.

--- Transcript ---
{transcript_excerpt}
"""


@dataclass
class MemoryFlushPlan:
    """Instructions for a memory flush operation."""

    relative_path: str
    system_prompt: str
    soft_threshold_tokens: int = 4000
    force_flush_transcript_bytes: int = 2_000_000
    reserve_tokens_floor: int = 1000


def validate_flush_save_arguments(
    arguments: Mapping[str, Any],
    *,
    relative_path: str,
) -> str | None:
    """Return a denial reason unless a flush save appends to the exact plan path."""
    path = arguments.get("path")
    mode = arguments.get("mode")
    if (
        not isinstance(path, str)
        or path != relative_path
        or mode != "append"
        or any(path.startswith(prefix) for prefix in FLUSH_RESERVED_PATH_PREFIXES)
        or path in FLUSH_RESERVED_PATHS
    ):
        return f"Flush may only append to {relative_path}."
    return None


@dataclass(frozen=True)
class TranscriptExcerpt:
    """Transcript excerpt plus source-coverage metadata."""

    text: str
    input_message_count: int
    prompt_message_count: int
    truncated: bool
    truncation_policy: str
    first_included_message: int | None
    last_included_message: int | None
    source_coverage: float


@dataclass(frozen=True)
class FlushPrompt:
    """Flush prompt plus source-coverage metadata for receipts."""

    text: str
    input_message_count: int
    prompt_message_count: int
    prompt_char_count: int
    truncated: bool
    truncation_policy: str
    first_included_message: int | None
    last_included_message: int | None
    source_coverage: float


def resolve_flush_plan(
    tz: timezone | None = None,
    soft_threshold_tokens: int = 4000,
    force_flush_transcript_bytes: int = 2_000_000,
    reserve_tokens_floor: int = 1000,
    workspace_dir: str | Path | None = None,
    archive_max_bytes: int | None = DEFAULT_FLUSH_ARCHIVE_MAX_BYTES,
) -> MemoryFlushPlan:
    """Build a flush plan with today's date as the target file."""
    tz = tz or UTC
    today = datetime.now(tz).strftime("%Y-%m-%d")
    relative_path = _select_daily_archive_path(
        today,
        workspace_dir=workspace_dir,
        archive_max_bytes=archive_max_bytes,
    )

    system_prompt = FLUSH_SYSTEM_PROMPT_TEMPLATE.format(
        relative_path=relative_path,
        silent_token=SILENT_REPLY_TOKEN,
    )

    return MemoryFlushPlan(
        relative_path=relative_path,
        system_prompt=system_prompt,
        soft_threshold_tokens=soft_threshold_tokens,
        force_flush_transcript_bytes=force_flush_transcript_bytes,
        reserve_tokens_floor=reserve_tokens_floor,
    )


def _select_daily_archive_path(
    day: str,
    *,
    workspace_dir: str | Path | None = None,
    archive_max_bytes: int | None = DEFAULT_FLUSH_ARCHIVE_MAX_BYTES,
) -> str:
    base = f"memory/{day}.md"
    if workspace_dir is None or archive_max_bytes is None or archive_max_bytes <= 0:
        return base

    root = Path(workspace_dir)
    candidate = base
    part = 0
    while True:
        path = root / candidate
        try:
            if not path.exists() or path.stat().st_size < archive_max_bytes:
                return candidate
        except OSError:
            return candidate
        part += 1
        candidate = f"memory/{day}-part{part:03d}.md"


def should_flush(
    total_tokens: int,
    threshold_tokens: int,
    soft_threshold_tokens: int = 4000,
    transcript_bytes: int = 0,
    force_flush_transcript_bytes: int = 2_000_000,
) -> bool:
    """Determine whether a flush should be triggered.

    Returns True when:
    - total_tokens is within soft_threshold_tokens of threshold, OR
    - transcript_bytes exceeds force_flush_transcript_bytes
    """
    if transcript_bytes >= force_flush_transcript_bytes:
        return True
    remaining = threshold_tokens - total_tokens
    return remaining <= soft_threshold_tokens


def _message_excerpt_line(
    msg: Any,
    *,
    per_message_max_chars: int | None = 500,
) -> str | None:
    role = getattr(msg, "role", "?")
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        # Flatten content blocks: extract text, summarise tools
        parts: list[str] = []
        for b in content:
            btype = getattr(b, "type", "")
            if btype == "text":
                parts.append(getattr(b, "text", ""))
            elif btype == "tool_use":
                parts.append(f"[Used tool: {getattr(b, 'name', '?')}]")
            elif btype == "tool_result":
                raw = getattr(b, "content", "")
                s = raw if isinstance(raw, str) else str(raw)
                if per_message_max_chars is not None:
                    parts.append(s[:200] if len(s) > 200 else s)
                else:
                    parts.append(s)
        text = " ".join(parts)
    else:
        return None
    if not text:
        return None
    snippet = text[:per_message_max_chars] if per_message_max_chars is not None else text
    return f"{role}: {snippet}"


def dump_transcript_excerpt_with_audit(
    messages: list[Any],
    max_chars: int | None = 4000,
    per_message_max_chars: int | None = 500,
) -> TranscriptExcerpt:
    """Extract a tail transcript excerpt and report source coverage."""
    if max_chars is not None and max_chars < 0:
        raise ValueError("max_chars must be >= 0")
    if per_message_max_chars is not None and per_message_max_chars <= 0:
        raise ValueError("per_message_max_chars must be > 0")

    lines: list[str] = []
    included_indices: list[int] = []
    total = 0
    truncated = False
    for idx in range(len(messages) - 1, -1, -1):
        line = _message_excerpt_line(
            messages[idx],
            per_message_max_chars=per_message_max_chars,
        )
        if line is None:
            continue
        projected = total + len(line)
        if max_chars is not None and projected > max_chars:
            truncated = True
            break
        total = projected
        lines.insert(0, line)
        included_indices.insert(0, idx)

    input_count = len(messages)
    included_count = len(included_indices)
    return TranscriptExcerpt(
        text="\n".join(lines),
        input_message_count=input_count,
        prompt_message_count=included_count,
        truncated=truncated,
        truncation_policy=(f"tail_excerpt_max_chars={max_chars}" if truncated else "full"),
        first_included_message=(included_indices[0] + 1 if included_indices else None),
        last_included_message=(included_indices[-1] + 1 if included_indices else None),
        source_coverage=(round(included_count / input_count, 6) if input_count else 0.0),
    )


def dump_transcript_excerpt(messages: list[Any], max_chars: int = 4000) -> str:
    """Extract last N messages as raw text for fallback flush.

    Used when the LLM provider is unavailable. Returns a plain-text
    transcript excerpt suitable for writing directly to a daily note.
    """
    return dump_transcript_excerpt_with_audit(messages, max_chars).text


def build_flush_user_prompt(
    plan: MemoryFlushPlan,
    messages: list[Any],
    max_chars: int = 4000,
) -> str:
    """Build the user message for the flush sub-agent."""
    return build_flush_user_prompt_with_audit(plan, messages, max_chars).text


def build_flush_user_prompt_with_audit(
    plan: MemoryFlushPlan,
    messages: list[Any],
    max_chars: int | None = 4000,
    per_message_max_chars: int | None = 500,
) -> FlushPrompt:
    """Build the user message for the flush sub-agent with coverage metadata."""
    excerpt = dump_transcript_excerpt_with_audit(
        messages,
        max_chars,
        per_message_max_chars=per_message_max_chars,
    )
    prompt = FLUSH_USER_PROMPT_TEMPLATE.format(
        relative_path=plan.relative_path,
        silent_token=SILENT_REPLY_TOKEN,
        transcript_excerpt=excerpt.text,
    )
    return FlushPrompt(
        text=prompt,
        input_message_count=excerpt.input_message_count,
        prompt_message_count=excerpt.prompt_message_count,
        prompt_char_count=len(prompt),
        truncated=excerpt.truncated,
        truncation_policy=excerpt.truncation_policy,
        first_included_message=excerpt.first_included_message,
        last_included_message=excerpt.last_included_message,
        source_coverage=excerpt.source_coverage,
    )
