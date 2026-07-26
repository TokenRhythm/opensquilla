"""Helpers shared by the gateway and standalone slash-command adapters.

Both adapters render the same user-facing strings (compact outcome, transcript
save) and share the same parsing and post-turn bookkeeping. Keeping the single
copy here prevents the two dispatch chains from drifting apart; the genuinely
backend-specific bodies (gateway RPC vs TurnRunner services) stay in the
adapter modules.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from opensquilla.cli.chat.session_state import ChatSessionState, messages_to_markdown
from opensquilla.cli.chat.turn import TurnResult
from opensquilla.cli.tui.backend.contracts import TuiOutputHandle
from opensquilla.cli.ui import ACCENT
from opensquilla.engine.commands import DEFAULT_REGISTRY, Surface


def slash_parts(cmd: str, name: str) -> list[str] | None:
    """Split ``cmd`` into ``[name, args]`` when it targets ``name``."""
    if cmd == name or cmd.startswith(f"{name} "):
        return cmd.split(maxsplit=1)
    return None


def slash_parts_any(cmd: str, *names: str) -> list[str] | None:
    for name in names:
        parts = slash_parts(cmd, name)
        if parts is not None:
            return parts
    return None


def registry_handler_words(surface: Surface) -> frozenset[str]:
    """Return every slash word (name + aliases) the registry lists for a surface."""
    return frozenset(
        word for command in DEFAULT_REGISTRY.for_surface(surface) for word in command.words()
    )


async def dispatch_theme_command(cmd: str, tui_output: TuiOutputHandle | None) -> None:
    from opensquilla.cli.tui.opentui.themes import handle_theme_command  # noqa: PLC0415

    await handle_theme_command(cmd, tui_output)


def output_supports_host_ui(tui_output: object | None) -> bool:
    """True when the active chat surface is the OpenTUI host (IPC-capable).

    Mirrors the capability probe in ``handle_theme_command``: the plugin
    wrapper always exposes a callable ``send_message`` that silently no-ops on
    the native backend, so prefer its explicit ``supports_send_message`` flag
    and fall back to ``callable()`` only for an unwrapped handle.
    """
    send_message = getattr(tui_output, "send_message", None)
    supports = getattr(tui_output, "supports_send_message", None)
    if supports is None:
        supports = callable(send_message)
    return bool(supports) and callable(send_message)


def record_turn(state: ChatSessionState, prompt: str, result: TurnResult) -> None:
    """Record a completed slash-driven turn on the in-memory session state."""
    state.transcript.add("user", prompt)
    state.transcript.add("assistant", result.text)
    state.usage.apply(result.usage)


def compact_token_stats(
    tokens_before: int,
    tokens_after: int,
    remaining_budget_tokens: int,
    summary_source: str,
) -> str:
    return (
        f"{tokens_before} -> {tokens_after} tokens, "
        f"{remaining_budget_tokens} remaining, {summary_source}"
    )


def compact_summary_stats(summary_len: int) -> str:
    return f"summary {summary_len} chars"


def compact_success_line(token_stats: str) -> str:
    return f"[{ACCENT}]compacted[/] [dim]{token_stats}[/dim]"


def compact_skipped_line() -> str:
    return (
        f"[{ACCENT}]compact skipped[/] "
        "[dim]already within context budget; no compact was applied[/dim]"
    )


def default_transcript_path(session_key: str) -> Path:
    suffix = session_key.replace(":", "-")
    return Path(f"opensquilla-chat-{suffix}.md")


def resolve_transcript_target(cmd: str, session_key: str) -> Path:
    """Return the ``/save`` target: an explicit argument or the default name."""
    parts = cmd.split(maxsplit=1)
    if len(parts) > 1:
        return Path(parts[1]).expanduser()
    return default_transcript_path(session_key)


def save_transcript_markdown(
    target: Path,
    markdown: str,
    *,
    output_console: Any,
    error_panel_factory: Callable[..., Any],
) -> bool:
    """Write the transcript export, mapping filesystem failures to an error panel.

    A typo'd directory or a read-only location must not escape into the
    dispatch loop and tear down the chat session.
    """
    try:
        target.write_text(markdown, encoding="utf-8")
    except OSError as exc:
        output_console.print(error_panel_factory(f"Could not save transcript: {exc}"))
        return False
    output_console.print(f"[green]Saved transcript:[/green] {target}")
    return True


def transcript_messages_to_markdown(messages: Iterable[Any]) -> str:
    """Render durable transcript entries (dicts or attribute rows) as markdown."""
    normalized: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, dict):
            normalized.append(message)
            continue
        normalized.append(
            {
                "role": getattr(message, "role", None),
                "text": getattr(message, "text", None) or getattr(message, "content", None),
            }
        )
    return messages_to_markdown(normalized)


__all__ = [
    "compact_skipped_line",
    "compact_success_line",
    "compact_summary_stats",
    "compact_token_stats",
    "default_transcript_path",
    "dispatch_theme_command",
    "output_supports_host_ui",
    "record_turn",
    "registry_handler_words",
    "resolve_transcript_target",
    "save_transcript_markdown",
    "slash_parts",
    "slash_parts_any",
    "transcript_messages_to_markdown",
]
