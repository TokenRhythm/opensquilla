"""Shared channel utilities: deduplication, rate limiting, retry logic.

Also hosts the ``ChannelAccessPolicy`` primitive that adapters declare to
describe their admit/deny semantics. Item-5 adapter adoptions wire the
``policy`` attribute through; future dispatch refactors will consume
``evaluate_policy`` directly.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

import httpx
import structlog

from opensquilla.channels.contract import ChannelLengthUnit
from opensquilla.http_retry import parse_retry_after

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Channel access policy
# ---------------------------------------------------------------------------


class ChannelDmAccess(StrEnum):
    """Typed direct-message admission modes for channel adapters."""

    PAIRING = "pairing"
    OPEN = "open"
    ALLOWLIST = "allowlist"


@dataclass(frozen=True, slots=True)
class ChannelAccessPolicy:
    """Per-adapter admit/deny declaration consumed by ``evaluate_policy``.

    The fields capture every dimension currently exercised by gateway
    dispatch (DM allow, group allow, mention requirement, sender allowlist)
    plus the ``allowlist`` slot reserved for item-5b/c/d/e per-adapter
    adoption. ``allowlist`` empty means "no sender filtering"; populated
    means strict allow-only.
    """

    dm_allowed: bool = True
    dm_access: ChannelDmAccess = ChannelDmAccess.PAIRING
    group_allowed: bool = True
    mention_required_in_group: bool = True
    allowlist: frozenset[str] = field(default_factory=frozenset)


def sender_is_channel_admin(sender_id: str | None, *, configured: Any) -> bool:
    """True when ``sender_id`` matches a ``channel_admin_senders`` entry.

    ``configured`` is the raw configured value for one channel: a single
    string or a collection of ids; anything else fails closed. This is the
    single matcher shared by operator RPC standing (command registry) and
    chat-side approval powers (gateway dispatch) so both surfaces always
    agree on who is a channel admin.
    """
    if not isinstance(sender_id, str) or not sender_id:
        return False
    if isinstance(configured, str):
        return sender_id == configured
    if not isinstance(configured, list | tuple | set | frozenset):
        return False
    return sender_id in {str(item) for item in configured}


@dataclass(frozen=True, slots=True)
class AccessDecision:
    """Result of ``evaluate_policy`` — paired with a stable reason code."""

    admit: bool
    reason: Literal[
        "dm_admitted",
        "dm_denied",
        "group_admitted",
        "group_denied",
        "not_mentioned_in_group",
        "not_in_allowlist",
        "pairing_required",
        "pairing_revoked",
    ]


def evaluate_policy(
    policy: ChannelAccessPolicy,
    *,
    is_group: bool,
    mentioned: bool,
    sender_id: str = "",
    pairing_status: Literal["pending", "approved", "revoked"] | None = None,
) -> AccessDecision:
    """Evaluate a single inbound message against a channel's access policy.

    Pure function. Adapters provide the policy; dispatch provides the runtime
    inputs (``is_group``, ``mentioned``, ``sender_id``). ``ChannelAccessPolicy``
    instances must be tuned so this evaluator preserves each adapter's access
    baseline when that adapter adopts the shared evaluator.
    """
    if is_group:
        if not policy.group_allowed:
            return AccessDecision(admit=False, reason="group_denied")
        if policy.mention_required_in_group and not mentioned:
            return AccessDecision(admit=False, reason="not_mentioned_in_group")
        if policy.allowlist and sender_id not in policy.allowlist:
            return AccessDecision(admit=False, reason="not_in_allowlist")
        return AccessDecision(admit=True, reason="group_admitted")
    if not policy.dm_allowed:
        return AccessDecision(admit=False, reason="dm_denied")
    if policy.dm_access == ChannelDmAccess.PAIRING:
        if pairing_status == "revoked":
            return AccessDecision(admit=False, reason="pairing_revoked")
        if pairing_status != "approved":
            return AccessDecision(admit=False, reason="pairing_required")
    if policy.dm_access == ChannelDmAccess.ALLOWLIST and sender_id not in policy.allowlist:
        return AccessDecision(admit=False, reason="not_in_allowlist")
    return AccessDecision(admit=True, reason="dm_admitted")


class EventDedupeCache:
    """Bounded set for deduplicating event IDs."""

    def __init__(self, max_size: int = 10_000) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._max_size = max_size

    def check_and_add(self, event_id: str) -> bool:
        """Return True if the event_id is new (not a duplicate)."""
        if event_id in self._seen:
            self._seen.move_to_end(event_id)
            return False
        self._seen[event_id] = None
        if len(self._seen) > self._max_size:
            self._seen.popitem(last=False)
        return True


@dataclass
class RateLimiter:
    """Async token-bucket rate limiter for HTTP API calls."""

    max_tokens: int = 30
    refill_rate: float = 30.0  # tokens per second
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        self._tokens = float(self.max_tokens)
        self._last_refill = time.monotonic()

    async def acquire(self) -> None:
        """Wait until a token is available."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self.max_tokens, self._tokens + elapsed * self.refill_rate)
            self._last_refill = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self.refill_rate
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0


# ---------------------------------------------------------------------------
# Streaming resilience helpers (item 4)
# ---------------------------------------------------------------------------
#
# Slack and discord stream chat output by posting an "open" message and then
# editing it with each accumulated chunk. The previous inline implementations
# raced when a fast producer fired two edits concurrently and crashed the
# whole consumer when a single edit raised mid-stream. The two helpers below
# add the minimum-radius safety net: an in-flight serializer with push-back
# semantics, and an adaptive strike counter that flips a circuit when the
# remote keeps returning 429.
#
# Feishu is a non-consumer of this module: ``feishu.send_streaming`` collects
# the entire stream and posts once at the end, so neither helper applies.
# Keeping Feishu outside this helper avoids importing streaming-throttle state
# into its post-once delivery path.


@dataclass
class StreamThrottle:
    """Serialize edit calls against an in-flight network round trip.

    Accumulates incoming chunks; ``maybe_flush`` sends the latest snapshot
    via ``post`` (first call) or ``edit`` (subsequent calls). The
    ``asyncio.Lock`` ensures a second flush cannot start while a first is
    awaiting the network. If a send raises, the accumulated text remains
    intact so the next ``maybe_flush`` retries with the same snapshot.
    """

    interval_s: float = 0.5
    _accumulated: str = field(default="", init=False)
    _last_flush: float = field(default=0.0, init=False)
    _opened: bool = field(default=False, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def add(self, text: str) -> None:
        self._accumulated += text

    @property
    def text(self) -> str:
        return self._accumulated

    @property
    def opened(self) -> bool:
        return self._opened

    async def maybe_flush(
        self,
        *,
        post: Callable[[str], Awaitable[Any]],
        edit: Callable[[str], Awaitable[Any]],
    ) -> Any | None:
        """Send the accumulated snapshot if the throttle window has elapsed."""
        if not self._accumulated:
            return None
        now = time.monotonic()
        if self._opened and now - self._last_flush < self.interval_s:
            return None
        async with self._lock:
            text = self._accumulated
            if not self._opened:
                result = await post(text)
                self._opened = True
            else:
                result = await edit(text)
            self._last_flush = time.monotonic()
            return result

    async def force_flush(
        self,
        *,
        post: Callable[[str], Awaitable[Any]],
        edit: Callable[[str], Awaitable[Any]],
    ) -> Any | None:
        """Final flush bypassing the throttle interval — call at end-of-stream."""
        if not self._accumulated:
            return None
        async with self._lock:
            text = self._accumulated
            if not self._opened:
                result = await post(text)
                self._opened = True
            else:
                result = await edit(text)
            self._last_flush = time.monotonic()
            return result


@dataclass
class FloodStrikeBackoff:
    """Sliding-window strike counter that flips a circuit after N 429s.

    Each ``record_429`` appends a strike timestamp; strikes older than
    ``decay_s`` are dropped before counting. Once ``cap`` consecutive
    strikes accumulate within the window, ``should_fallback`` returns True
    and one ``channel.flood_strike_backoff`` log entry is emitted. The
    fallback latch stays True until ``reset`` is called explicitly so the
    streaming consumer cannot oscillate in/out of fallback every chunk.
    """

    cap: int = 3
    decay_s: float = 30.0
    adapter: str = "unknown"
    _strikes: list[float] = field(default_factory=list, init=False)
    _fallback: bool = field(default=False, init=False)

    def record_429(self) -> None:
        now = time.monotonic()
        self._strikes = [t for t in self._strikes if now - t <= self.decay_s]
        self._strikes.append(now)
        if not self._fallback and len(self._strikes) >= self.cap:
            self._fallback = True
            log.warning(
                "channel.flood_strike_backoff",
                adapter=self.adapter,
                strikes=len(self._strikes),
                cap=self.cap,
                decay_s=self.decay_s,
            )

    def record_success(self) -> None:
        """Successful send drops accumulated strikes — does NOT clear fallback."""
        self._strikes.clear()

    def should_fallback(self) -> bool:
        return self._fallback

    def reset(self) -> None:
        """Operator/manual circuit reset."""
        self._strikes.clear()
        self._fallback = False


#: Defensive ceiling for a provider-supplied ``Retry-After``. A broken or
#: hostile header must not park a channel for hours; generous enough that any
#: realistic chat-provider hint is honored verbatim.
MAX_RETRY_AFTER_S: float = 900.0


def _retry_after_delay(response: httpx.Response, fallback: float) -> float:
    """Seconds to wait before retrying, from ``Retry-After`` or ``fallback``.

    Handles both RFC 9110 forms and rejects absent/negative/unparseable
    values (a bare ``float()`` raises on the HTTP-date form). The honored
    delay is capped so a hostile header cannot park the channel.
    """
    parsed = parse_retry_after(response.headers.get("Retry-After"))
    return min(fallback if parsed is None else parsed, MAX_RETRY_AFTER_S)


async def retry_request(
    func: Callable[..., Awaitable[httpx.Response]],
    *args: Any,
    max_retries: int = 3,
    base_delay: float = 1.0,
    **kwargs: Any,
) -> httpx.Response:
    """Retry an httpx request with exponential backoff on transient errors.

    On exhaustion the final response is returned rather than raised: a 429 or
    5xx that survived every attempt is the provider's answer, and the caller
    needs it to classify the failure.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = await func(*args, **kwargs)
            backoff = base_delay * (2**attempt)
            if resp.status_code == 429 and attempt < max_retries:
                retry_after = _retry_after_delay(resp, backoff)
                log.warning("rate_limited", retry_after=retry_after, attempt=attempt)
                await asyncio.sleep(retry_after)
                continue
            if resp.status_code in {500, 502, 503, 504} and attempt < max_retries:
                delay = backoff + random.random()
                log.warning("transient_error", status=resp.status_code, delay=delay)
                await asyncio.sleep(delay)
                continue
            return resp
        except (httpx.ConnectError, httpx.ReadTimeout) as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = base_delay * (2**attempt) + random.random()
                await asyncio.sleep(delay)
                continue
            raise
    raise last_exc or RuntimeError("retry_request exhausted")


def _measure_for(unit: ChannelLengthUnit) -> Callable[[str], int]:
    if unit is ChannelLengthUnit.UTF8_BYTES:
        return lambda s: len(s.encode("utf-8"))
    if unit is ChannelLengthUnit.UTF16_UNITS:
        return lambda s: len(s.encode("utf-16-le")) // 2
    return len


def measured_len(text: str, unit: ChannelLengthUnit = ChannelLengthUnit.CODE_POINTS) -> int:
    """Length of ``text`` in the unit the platform counts in."""
    return _measure_for(unit)(text)


def _fit_cut(text: str, limit: int, measure: Callable[[str], int]) -> int:
    """Largest code-point index whose measured prefix length is ``<= limit``.

    Bisection over the code-point index — O(log n) measures. The index is a
    Python string index, so an astral code point (emoji) is atomic: a prefix
    either includes it whole or not at all, never a half surrogate.
    """
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure(text[:mid]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    # Always make progress even if a single leading code point exceeds limit.
    return max(lo, 1)


def _boundary_before(text: str, cut: int) -> int:
    """Preferred split index in ``[0, cut)``: paragraph, then line, then word.

    Returns the index just past the chosen separator so it stays with the
    preceding chunk, or 0 when no boundary exists (caller hard-splits at cut).
    """
    paragraph_at = text.rfind("\n\n", 0, cut)
    if paragraph_at >= 0:
        return paragraph_at + 2
    line_at = text.rfind("\n", 0, cut)
    if line_at >= 0:
        return line_at + 1
    word_at = text.rfind(" ", 0, cut)
    return word_at + 1 if word_at >= 0 else 0


def split_text_for_channel(
    text: str,
    limit: int,
    *,
    unit: ChannelLengthUnit = ChannelLengthUnit.CODE_POINTS,
) -> list[str]:
    """Split ``text`` into chunks no longer than ``limit`` in ``unit``.

    Splitting prefers paragraph (blank-line) then line then word boundaries,
    hard-splitting only as a last resort for a single token longer than
    ``limit``. Used to keep outbound replies under a platform's per-message
    length cap (Telegram 4096 UTF-16 units, Discord 2000 code points, ...)
    instead of letting the API reject the whole message. An empty ``text``
    yields ``[""]`` so callers always make at least one send; a non-positive
    ``limit`` disables splitting.

    ``unit`` measures the fit test only; boundary search stays at code-point
    granularity, so an astral character is never split mid-surrogate.
    """
    measure = _measure_for(unit)
    if limit <= 0 or measure(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while measure(remaining) > limit:
        cut = _fit_cut(remaining, limit, measure)
        end = _boundary_before(remaining, cut) or cut
        chunks.append(remaining[:end])
        remaining = remaining[end:]
    chunks.append(remaining)
    return chunks


_TRUNCATE_FOOTER = "\n\n[message truncated to fit this channel's length limit]"


def truncate_to_limit(
    text: str,
    limit: int,
    *,
    unit: ChannelLengthUnit = ChannelLengthUnit.CODE_POINTS,
    footer: str = _TRUNCATE_FOOTER,
) -> str:
    """Cut ``text`` to ``limit`` in ``unit``, appending a footer when it fits.

    The last resort for a single unsplittable unit that still overflows: a
    delivered-but-truncated message beats a platform-rejected one.
    """
    measure = _measure_for(unit)
    if measure(text) <= limit:
        return text
    budget = limit - measure(footer)
    if budget <= 0:
        return text[: _fit_cut(text, limit, measure)]
    cut = _fit_cut(text, budget, measure)
    end = _boundary_before(text, cut) or cut
    return text[:end] + footer
