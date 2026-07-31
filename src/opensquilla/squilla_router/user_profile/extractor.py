"""Transcript rendering, batching, and the provider-neutral streaming loop.

The gateway injects the concrete LLM stream factory, keeping this package free
of the provider layer while preserving the same Dream provider/model selection.
Rendering preserves every non-empty transcript entry with its role and
truncates head+tail so a long session still fits a bounded prompt. Batching
drops a session that alone blows the batch budget rather than cutting it
mid-way — keeping every ``session_id`` the LLM sees honest.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import AsyncIterator, Sequence
from typing import Any, Protocol

from opensquilla.squilla_router.user_profile.prompts import (
    SYSTEM_PROMPT,
    build_batch_prompt,
    parse_batch_response,
)
from opensquilla.squilla_router.user_profile.schema import (
    BatchAnalysis,
    SessionTranscript,
)

_TRUNCATION_MARKER = "\n[transcript truncated]\n"


class _TranscriptRow(Protocol):
    role: str
    content: str | None


class StreamFactory(Protocol):
    """Build the concrete provider stream without importing the provider layer."""

    def __call__(
        self,
        *,
        provider: Any,
        user_prompt: str,
        system_prompt: str,
        max_output_tokens: int,
        temperature: float,
        timeout: float,
    ) -> AsyncIterator[Any]: ...


def _truncate(text: str, max_chars: int, head_fraction: float = 0.5) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = _TRUNCATION_MARKER[:max_chars]
    retained = max_chars - len(marker)
    if retained <= 0:
        return marker
    head_chars = math.floor(retained * head_fraction)
    tail_chars = retained - head_chars
    tail = text[-tail_chars:] if tail_chars else ""
    return text[:head_chars] + marker + tail


def render_transcript(
    session_id: str,
    rows: Sequence[_TranscriptRow],
    *,
    per_session_max_chars: int,
) -> SessionTranscript:
    """Render one session to a role-prefixed, truncated plain-text blob."""

    lines: list[str] = []
    for row in rows:
        role = getattr(row, "role", "") or ""
        if not role:
            continue
        content = getattr(row, "content", None)
        if not content:
            continue
        lines.append(f"{role}: {content}")
    text = _truncate("\n".join(lines), per_session_max_chars)
    return SessionTranscript(session_id=session_id, text=text)


def batch_sessions(
    sessions: list[SessionTranscript],
    *,
    batch_size: int,
    batch_input_max_chars: int,
) -> list[list[SessionTranscript]]:
    """Group sessions into batches of ~``batch_size`` within a char budget.

    A session whose rendered text alone exceeds ``batch_input_max_chars`` is
    dropped (not split), so no batch references a session it only partially
    showed the model.
    """

    batches: list[list[SessionTranscript]] = []
    current: list[SessionTranscript] = []
    current_chars = 0
    for session in sessions:
        size = len(session.text)
        if batch_input_max_chars > 0 and size > batch_input_max_chars:
            continue  # too big even alone -> drop
        would_overflow = (
            batch_input_max_chars > 0 and current and current_chars + size > batch_input_max_chars
        )
        if current and (len(current) >= batch_size or would_overflow):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(session)
        current_chars += size
    if current:
        batches.append(current)
    return batches


async def extract_batch(
    *,
    provider: Any,
    stream_factory: StreamFactory,
    batch: list[SessionTranscript],
    max_output_tokens: int,
    temperature: float,
    timeout: float,
    response_max_chars: int,
) -> BatchAnalysis:
    """Run one batch through the provider and parse its reply. Fail-open.

    Mirrors the task-analyzer consumption loop: bounded by an ``asyncio.timeout``,
    accumulates ``text_delta`` events under a size cap, stops on ``done``, raises
    on ``error``, and always closes the stream. Any failure returns a failed
    :class:`BatchAnalysis` so the run continues best-effort.
    """

    session_ids = tuple(s.session_id for s in batch)
    if not batch:
        return BatchAnalysis.failed(session_ids)
    try:
        stream = stream_factory(
            provider=provider,
            user_prompt=build_batch_prompt(batch),
            system_prompt=SYSTEM_PROMPT,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            timeout=timeout,
        )
        text_parts: list[str] = []
        total_chars = 0
        got_done = False
        try:
            async with asyncio.timeout(timeout):
                async for event in stream:
                    kind = getattr(event, "kind", None)
                    if kind == "text_delta":
                        text = str(getattr(event, "text", ""))
                        total_chars += len(text)
                        if response_max_chars > 0 and total_chars > response_max_chars:
                            raise ValueError("profile analyst response exceeded size limit")
                        text_parts.append(text)
                    elif kind == "done":
                        got_done = True
                        break
                    elif kind == "error":
                        code = getattr(event, "code", None) or "unknown"
                        raise RuntimeError(f"provider_error:{code}")
        finally:
            aclose = getattr(stream, "aclose", None)
            if callable(aclose):
                with contextlib.suppress(Exception):
                    await aclose()
        if not got_done:
            raise RuntimeError("profile analyst stream ended before DoneEvent")
        return parse_batch_response("".join(text_parts), session_ids)
    except Exception:  # noqa: BLE001 — a bad batch must not abort the run
        return BatchAnalysis.failed(session_ids)


__all__ = [
    "batch_sessions",
    "extract_batch",
    "render_transcript",
    "StreamFactory",
]
