"""Per-adapter channel feedback strategy.

Channel dispatch should not infer user-visible behavior solely from method
presence. Adapters can opt out of streaming edits even when they expose a
compatibility ``send_streaming`` method.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ChannelStreamMode = Literal["adapter_stream", "typing_final", "final_only"]


@dataclass(frozen=True)
class ChannelStreamPolicy:
    mode: ChannelStreamMode
    relay_stream: bool
    typing_keepalive: bool


def _strategy_override(channel: Any) -> str | None:
    raw = getattr(channel, "STREAM_UPDATE_STRATEGY", None)
    if raw is None:
        raw = getattr(channel, "stream_update_strategy", None)
    if raw is None:
        return None
    return str(raw).strip().lower().replace("-", "_")


def _policy_for_mode(mode: ChannelStreamMode, *, has_typing: bool) -> ChannelStreamPolicy:
    return ChannelStreamPolicy(
        mode=mode,
        relay_stream=mode == "adapter_stream",
        typing_keepalive=mode == "typing_final" and has_typing,
    )


def resolve_channel_stream_policy(channel: Any) -> ChannelStreamPolicy:
    """Return how dispatch should keep a channel user informed during a run."""

    has_streaming = callable(getattr(channel, "send_streaming", None))
    has_typing = callable(getattr(channel, "send_typing", None))
    override = _strategy_override(channel)

    if override in {"adapter_stream", "stream", "streaming", "send_streaming"}:
        return _policy_for_mode(
            "adapter_stream" if has_streaming else "final_only",
            has_typing=has_typing,
        )
    if override in {"typing_final", "typing", "typing_indicator", "placeholder"}:
        return _policy_for_mode("typing_final", has_typing=has_typing)
    if override in {"final_only", "final", "batch", "none", "off"}:
        return _policy_for_mode("final_only", has_typing=has_typing)

    if has_streaming:
        return _policy_for_mode("adapter_stream", has_typing=has_typing)
    if has_typing:
        return _policy_for_mode("typing_final", has_typing=has_typing)
    return _policy_for_mode("final_only", has_typing=has_typing)
