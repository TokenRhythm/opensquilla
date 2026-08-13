"""Bounded detection for provider text streams stuck in repetition loops.

The detector consumes normalized characters at fixed checkpoints instead of
provider event boundaries.  A provider therefore cannot change the decision
by splitting the same text into different delta sizes.  Detection is kept at
the Provider -> Agent boundary so every UI/channel benefits and the upstream
request can be closed before a terminal error is emitted.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any

import structlog

from opensquilla.provider import TextDeltaEvent as ProviderTextDelta

log = structlog.get_logger(__name__)

MODEL_REPETITION_LOOP_CODE = "model_repetition_loop_detected"
MODEL_REPETITION_LOOP_MESSAGE = (
    "The model began repeating the same output, so OpenSquilla stopped the task."
)

_LOG_LINE_RE = re.compile(
    r"^(?:\[?\d{4}-\d{2}-\d{2}[T ]|\d{2}:\d{2}:\d{2}|"
    r"(?:trace|debug|info|warn(?:ing)?|error|fatal)\b)",
    re.IGNORECASE,
)
_CONTAINER_LOG_LINE_RE = re.compile(
    r"^(?:\[[^\]\n]{1,80}\]\s*)?"
    r"(?:[A-Za-z0-9_.-]{1,80}(?:/[A-Za-z0-9_.-]{1,80})?\s+\|\s+)"
    r"(?:\[?\d{4}-\d{2}-\d{2}[T ]|\d{2}:\d{2}:\d{2}|"
    r"(?:trace|debug|info|warn(?:ing)?|error|fatal)\b)",
    re.IGNORECASE,
)
_CRI_LOG_LINE_RE = re.compile(
    r"^(?:[A-Za-z0-9_.-]{1,128}\s+)?(?:stdout|stderr)\s+[FP]\s+"
    r"(?:\[?\d{4}-\d{2}-\d{2}[T ]|\d{2}:\d{2}:\d{2}|"
    r"(?:trace|debug|info|warn(?:ing)?|error|fatal)\b)",
    re.IGNORECASE,
)
_KUBECTL_PREFIX_LOG_LINE_RE = re.compile(
    r"^\[pod/[A-Za-z0-9_.-]{1,253}(?:/container)?/[A-Za-z0-9_.-]{1,253}\]\s+"
    r"(?:\[?\d{4}-\d{2}-\d{2}[T ]|\d{2}:\d{2}:\d{2}|"
    r"(?:trace|debug|info|warn(?:ing)?|error|fatal)\b)",
    re.IGNORECASE,
)
_QUERY_BORDER_RE = re.compile(r"^(?:\+-{2,}(?:\+-{2,})+\+?|[-=]{2,}(?:\+[-=]{2,})+)$")
_QUERY_FOOTER_RE = re.compile(r"^\(\d+\s+rows?\)$", re.IGNORECASE)
_CODE_PREFIX_RE = re.compile(
    r"^(?:async\s+def|def|class|function|const|let|var|if|else|elif|for|while|"
    r"try|except|catch|return|import|from|package|public|private|protected|"
    r"select|insert|update|delete|create|alter|drop)\b",
    re.IGNORECASE,
)
_CODE_CALL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*\(")


@dataclass(frozen=True, slots=True)
class RepetitionGuardPolicy:
    """Conservative fixed policy for one provider attempt."""

    max_buffer_chars: int = 65_536
    check_stride_chars: int = 256
    required_consecutive_checks: int = 3
    min_period_chars: int = 48
    max_period_chars: int = 8_192
    min_repeated_chars: int = 4_096
    min_repetitions: int = 8
    min_similarity: float = 0.985
    large_period_threshold_chars: int = 2_048
    large_period_min_repeated_chars: int = 16_384
    large_period_min_repetitions: int = 4
    structured_min_repeated_chars: int = 57_344
    structured_min_repetitions: int = 7
    structured_min_similarity: float = 0.9995
    max_candidate_periods: int = 64
    close_timeout_seconds: float = 0.25


@dataclass(frozen=True, slots=True)
class RepetitionDetection:
    period_chars: int
    repeated_chars: int
    repetitions: int
    similarity: float
    structured: bool

    def log_fields(self) -> dict[str, int | float | bool]:
        """Return content-free diagnostics safe for durable logs."""

        return {
            "period_chars": self.period_chars,
            "repeated_chars": self.repeated_chars,
            "repetitions": self.repetitions,
            "similarity": round(self.similarity, 5),
            "structured": self.structured,
        }


class ModelRepetitionLoopError(RuntimeError):
    """Internal control-flow signal after the provider stream is closed."""

    code = MODEL_REPETITION_LOOP_CODE

    def __init__(self, detection: RepetitionDetection) -> None:
        super().__init__(MODEL_REPETITION_LOOP_MESSAGE)
        self.detection = detection


class StreamingRepetitionGuard:
    """Detect a highly periodic suffix using bounded memory.

    Whitespace is collapsed incrementally, including across chunk boundaries.
    Checks happen every ``check_stride_chars`` normalized characters.  ``feed``
    returns only the raw prefix up to the deterministic triggering checkpoint;
    callers must stop feeding after a non-None detection.
    """

    def __init__(self, policy: RepetitionGuardPolicy | None = None) -> None:
        self.policy = policy or RepetitionGuardPolicy()
        self._buffer_chunks: deque[str] = deque()
        self._buffer_chars = 0
        self._normalized_chars = 0
        self._next_check = self.policy.check_stride_chars
        self._last_whitespace: str | None = None
        self._repeat_evidence_streak = 0

    @property
    def buffered_chars(self) -> int:
        return self._buffer_chars

    def reset(self) -> None:
        """Start a fresh text segment at a tool boundary."""

        self._buffer_chunks.clear()
        self._buffer_chars = 0
        self._normalized_chars = 0
        self._next_check = self.policy.check_stride_chars
        self._last_whitespace = None
        self._repeat_evidence_streak = 0

    def feed(self, text: str) -> tuple[str, RepetitionDetection | None]:
        if not text:
            return text, None

        pending: list[str] = []
        for raw_index, char in enumerate(text):
            if char.isspace():
                normalized = "\n" if char in "\r\n" else "\t" if char == "\t" else " "
                if self._last_whitespace == "\n":
                    continue
                if self._last_whitespace == normalized:
                    continue
                if normalized == " " and self._last_whitespace is not None:
                    continue
                self._last_whitespace = normalized
            else:
                normalized = char
                self._last_whitespace = None

            pending.append(normalized)
            self._normalized_chars += 1
            if self._normalized_chars < self._next_check:
                continue

            self._append("".join(pending))
            pending.clear()
            candidate = self._detect()
            self._next_check += self.policy.check_stride_chars
            if candidate is None:
                self._repeat_evidence_streak = 0
                continue
            self._repeat_evidence_streak += 1
            if self._repeat_evidence_streak >= self.policy.required_consecutive_checks:
                return text[: raw_index + 1], candidate

        if pending:
            self._append("".join(pending))
        return text, None

    def _append(self, text: str) -> None:
        if not text:
            return
        self._buffer_chunks.append(text)
        self._buffer_chars += len(text)
        overflow = self._buffer_chars - self.policy.max_buffer_chars
        while overflow > 0:
            oldest = self._buffer_chunks[0]
            if len(oldest) <= overflow:
                self._buffer_chunks.popleft()
                self._buffer_chars -= len(oldest)
                overflow -= len(oldest)
                continue
            self._buffer_chunks[0] = oldest[overflow:]
            self._buffer_chars -= overflow
            overflow = 0

    def _buffer_text(self) -> str:
        if not self._buffer_chunks:
            return ""
        if len(self._buffer_chunks) == 1:
            return self._buffer_chunks[0]
        text = "".join(self._buffer_chunks)
        self._buffer_chunks.clear()
        self._buffer_chunks.append(text)
        return text

    def _detect(self) -> RepetitionDetection | None:
        policy = self.policy
        if self._buffer_chars < policy.min_repeated_chars + policy.min_period_chars:
            return None

        text = self._buffer_text()
        candidates = self._candidate_periods(text)
        for period in candidates:
            structured = _looks_structured(text[-max(8_192, period * 2) :], period)
            min_repeated = (
                policy.structured_min_repeated_chars if structured else policy.min_repeated_chars
            )
            min_repetitions = (
                policy.structured_min_repetitions if structured else policy.min_repetitions
            )
            min_similarity = (
                policy.structured_min_similarity if structured else policy.min_similarity
            )
            if not structured and period > policy.large_period_threshold_chars:
                min_repeated = max(min_repeated, policy.large_period_min_repeated_chars)
                min_repetitions = min(
                    min_repetitions,
                    policy.large_period_min_repetitions,
                )
            repeated_chars = max(min_repeated, period * min_repetitions)
            if repeated_chars + period > len(text):
                continue

            comparison = text[-(repeated_chars + period) :]
            previous = comparison[:-period]
            current = comparison[period:]
            matches = sum(left == right for left, right in zip(previous, current, strict=True))
            similarity = matches / repeated_chars
            if similarity < min_similarity:
                continue
            return RepetitionDetection(
                period_chars=period,
                repeated_chars=repeated_chars,
                repetitions=repeated_chars // period,
                similarity=similarity,
                structured=structured,
            )
        return None

    def _candidate_periods(self, text: str) -> list[int]:
        """Find likely periods from several exact anchors near the suffix.

        A near-repeating stream normally preserves most 24-character anchors;
        gathering anchors at four phases lets a small changing field avoid one
        anchor without defeating detection.  Candidate count is capped so each
        fixed checkpoint has bounded CPU cost.
        """

        policy = self.policy
        votes: dict[int, int] = {}
        # Multiple anchor widths make the discovery resilient to a changing
        # field landing inside one anchor, while the phase offsets keep the
        # result independent of provider chunk boundaries.  Each rfind scans
        # at most max_period_chars and both anchors and candidates are capped.
        for anchor_chars in (24, 48, 96):
            for end_offset in (0, 61, 137, 251, 509):
                anchor_end = len(text) - end_offset
                anchor_start = anchor_end - anchor_chars
                if anchor_start <= 0:
                    continue
                anchor = text[anchor_start:anchor_end]
                search_start = max(0, anchor_start - policy.max_period_chars)
                search_end = anchor_start
                occurrences = 0
                while search_end > search_start and occurrences < 16:
                    previous = text.rfind(anchor, search_start, search_end)
                    if previous < 0:
                        break
                    period = anchor_start - previous
                    if policy.min_period_chars <= period <= policy.max_period_chars:
                        votes[period] = votes.get(period, 0) + 1
                    search_end = previous
                    occurrences += 1
        ranked = sorted(votes, key=lambda period: (-votes[period], period))
        return sorted(ranked[: policy.max_candidate_periods])


def _looks_structured(text: str, period: int) -> bool:
    """Recognize code/table/log shapes that need the slower threshold."""

    unit = text[-period:]
    if "```" in text:
        return True
    lines = [line.strip() for line in text.splitlines()[-128:] if line.strip()]
    if not lines:
        lines = [unit.strip()] if unit.strip() else []
    if not lines:
        return False
    if _looks_delimited_rows(lines) or _looks_query_result(lines):
        return True

    structured_lines = 0
    for line in lines:
        is_table = line.count("|") >= 2
        is_log = bool(
            _LOG_LINE_RE.match(line)
            or _CONTAINER_LOG_LINE_RE.match(line)
            or _CRI_LOG_LINE_RE.match(line)
            or _KUBECTL_PREFIX_LOG_LINE_RE.match(line)
        )
        is_code = bool(
            _CODE_PREFIX_RE.match(line)
            or _CODE_CALL_RE.match(line)
            or line.startswith(("//", "# ", "/*", "* ", "@"))
            or line.endswith((";", "{", "}"))
            or (line.startswith("<") and line.endswith(">"))
            or any(marker in line for marker in ("()", "=>", " = ", " := ", " == "))
        )
        structured_lines += int(is_table or is_log or is_code)
    # A checkpoint may bisect the first/last line, so a two-line code unit can
    # contribute just under half recognized lines in the sampled suffix.
    return structured_lines / len(lines) >= 0.4


def _looks_delimited_rows(lines: list[str]) -> bool:
    """Recognize CSV/TSV-shaped output without parsing or retaining fields."""

    if len(lines) < 4:
        return False
    for delimiter in ("\t", ","):
        counts = [line.count(delimiter) for line in lines]
        eligible = [count for count in counts if count >= 1]
        if len(eligible) < max(4, int(len(lines) * 0.75)):
            continue
        frequencies: dict[int, int] = {}
        for count in eligible:
            frequencies[count] = frequencies.get(count, 0) + 1
        if max(frequencies.values(), default=0) >= max(3, int(len(lines) * 0.5)):
            return True
    return False


def _looks_query_result(lines: list[str]) -> bool:
    """Recognize common psql/sqlite/MySQL-style tabular query output."""

    if len(lines) < 3:
        return False
    table_rows = sum(
        1
        for line in lines
        if (
            line.count("|") >= 1
            or _QUERY_BORDER_RE.match(line) is not None
            or _QUERY_FOOTER_RE.match(line) is not None
        )
    )
    return table_rows >= max(3, int(len(lines) * 0.5))


def _consume_close_result(task: asyncio.Future[Any]) -> None:
    if task.cancelled():
        return
    with contextlib.suppress(BaseException):
        task.result()


async def close_async_iterator_bounded(
    stream_iter: AsyncIterator[Any],
    *,
    timeout: float,
    event_prefix: str = "provider_stream",
) -> None:
    caller = asyncio.current_task()
    caller_cancelling_before = caller.cancelling() if caller is not None else 0
    aclose = getattr(stream_iter, "aclose", None)
    if not callable(aclose):
        return
    try:
        close_task = asyncio.ensure_future(aclose())
    except Exception as exc:  # noqa: BLE001 - cleanup must not mask detection
        log.warning(
            f"{event_prefix}.close_failed",
            error_type=type(exc).__name__,
        )
        return
    try:
        await asyncio.wait_for(
            asyncio.shield(close_task),
            timeout=max(0.001, timeout),
        )
    except TimeoutError:
        close_task.cancel()
        close_task.add_done_callback(_consume_close_result)
        log.warning(
            f"{event_prefix}.close_timeout",
            timeout_seconds=timeout,
        )
        return
    except asyncio.CancelledError:
        caller_cancelling_now = caller.cancelling() if caller is not None else 0
        caller_is_cancelling = caller_cancelling_now > caller_cancelling_before or (
            caller_cancelling_now > 0 and not close_task.cancelled()
        )
        if caller_is_cancelling:
            close_task.cancel()
            close_task.add_done_callback(_consume_close_result)
            raise
        # The provider's aclose() may cancel itself.  That is a cleanup
        # failure, not evidence that the task driving the provider stream was
        # cancelled, so it must not replace the stream's terminal outcome.
        _consume_close_result(close_task)
        log.warning(
            f"{event_prefix}.close_failed",
            error_type=asyncio.CancelledError.__name__,
        )
    except Exception as exc:  # noqa: BLE001 - cleanup must not mask terminal outcome
        log.warning(
            f"{event_prefix}.close_failed",
            error_type=type(exc).__name__,
        )
    else:
        _consume_close_result(close_task)


class _IdempotentStreamCloser:
    """Start at most one bounded close even when exit paths converge."""

    def __init__(self, stream_iter: AsyncIterator[Any], *, timeout: float) -> None:
        self._stream_iter = stream_iter
        self._timeout = timeout
        self._close_task: asyncio.Task[None] | None = None

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                close_async_iterator_bounded(
                    self._stream_iter,
                    timeout=self._timeout,
                    event_prefix="provider_repetition_guard",
                )
            )
        await asyncio.shield(self._close_task)


async def guard_provider_text_stream(
    stream: AsyncIterator[Any],
    *,
    policy: RepetitionGuardPolicy | None = None,
) -> AsyncIterator[Any]:
    """Pass through provider events until repeated text crosses the policy."""

    active_policy = policy or RepetitionGuardPolicy()
    guard = StreamingRepetitionGuard(active_policy)
    stream_iter = stream.__aiter__()
    closer = _IdempotentStreamCloser(
        stream_iter,
        timeout=active_policy.close_timeout_seconds,
    )
    try:
        async for event in stream_iter:
            kind = str(getattr(event, "kind", "") or "")
            if kind in {"tool_use_start", "tool_use_end"}:
                guard.reset()
                yield event
                continue
            if not isinstance(event, ProviderTextDelta):
                yield event
                continue

            accepted_text, detection = guard.feed(event.text)
            if accepted_text:
                yield replace(event, text=accepted_text)
            elif detection is None:
                # Preserve existing empty-delta behavior.
                yield event
            if detection is None:
                continue

            await closer.close()
            raise ModelRepetitionLoopError(detection)
    finally:
        await closer.close()


__all__ = [
    "MODEL_REPETITION_LOOP_CODE",
    "MODEL_REPETITION_LOOP_MESSAGE",
    "ModelRepetitionLoopError",
    "RepetitionDetection",
    "RepetitionGuardPolicy",
    "StreamingRepetitionGuard",
    "close_async_iterator_bounded",
    "guard_provider_text_stream",
]
