"""Plain terminal stream renderer for the stable chat backend.

The renderer writes assistant output through a Rich console that has markup
enabled (so backend notices such as ``[yellow]Cancelled.[/yellow]`` keep their
styling). Model- and tool-provided text is therefore *untrusted markup*: it is
escaped here before it reaches the console so bracketed content (file paths like
``[/usr/bin]``, code, or markup-like tokens) renders literally instead of being
parsed — which would otherwise corrupt styling or raise ``MarkupError`` and tear
down the session.
"""

from __future__ import annotations

from typing import Any, Literal

from rich.markup import escape as _escape

from opensquilla.cli.tui.backend.directives import StreamDirectiveFilter
from opensquilla.ui import ACCENT

# Map renderer-internal status styles onto Rich styles for the plain terminal.
_STATUS_STYLES = {
    "dim": "dim",
    "normal": "default",
    "warning": "yellow",
    "error": "red",
}


def status_markup(message: str, *, style: str = "dim") -> str:
    """Render one status line as Rich markup with the message escaped.

    Shared with the native chat runtime, which writes router-decision status
    lines through the output handle without going through the renderer.
    """
    rich_style = _STATUS_STYLES.get(style, "dim")
    return f"[{rich_style}]{_escape(message)}[/{rich_style}]\n"


class NativeStreamRenderer:
    """Async renderer that writes assistant output directly to the terminal."""

    def __init__(self, *, title: str = "squilla", output_handle: Any | None = None) -> None:
        del title
        self.output_handle = output_handle
        self.buffer = ""
        self._saw_output = False
        self._saw_reasoning = False
        self._reasoning_open = False
        # True when the last write left the cursor mid-line (no trailing newline),
        # e.g. streamed answer prose. Block-level rows (tool start) use it to break
        # to a fresh line so a glyph never collides with preceding text.
        self._line_open = False
        self._tool_names: dict[str, str] = {}
        # Strips [[reply_to_current]]-style routing directives from the
        # streamed answer text before it reaches the terminal.
        self._directive_filter = StreamDirectiveFilter()

    def __enter__(self) -> NativeStreamRenderer:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Literal[False]:
        return False

    async def _write(self, payload: str) -> None:
        if not payload:
            return
        handle = self.output_handle
        if handle is None:
            return
        await handle.write_through(payload)
        self._line_open = not payload.endswith("\n")

    async def _close_reasoning(self) -> None:
        """Separate the dim reasoning section from following answer/tool output."""
        if self._reasoning_open:
            self._reasoning_open = False
            await self._write("\n")

    async def _ensure_line_start(self) -> None:
        """Break to a fresh line if the last write left the cursor mid-line."""
        if self._line_open:
            await self._write("\n")

    async def aappend_text(self, delta: str, *, presentation: str = "answer") -> None:
        del presentation
        if not delta:
            return
        await self._close_reasoning()
        # ``buffer`` is the logical assistant text consumed by ``TurnResult``;
        # keep it raw and only escape what is sent to the markup-enabled console.
        self.buffer += delta
        # Routing directives ([[reply_to_current]]-style) are for channel
        # delivery, not the terminal transcript.
        visible = self._directive_filter.feed(delta)
        if not visible:
            return
        self._saw_output = True
        await self._write(_escape(visible))

    async def areconcile_final_text(self, text: str) -> None:
        """Make an authoritative terminal snapshot unambiguous in scrollback.

        A plain terminal cannot reliably erase arbitrary output once tool rows or
        wrapping have moved it into scrollback.  Exact snapshots therefore need
        no work, strict extensions can still stream normally, and a conflicting
        snapshot is rendered as an explicit correction.  The correction keeps
        prior tool rows intact while making it clear that the earlier preview is
        no longer the answer.
        """

        previous = self.buffer
        if text == previous:
            return
        if text.startswith(previous):
            await self.aappend_text(text[len(previous) :])
            return

        # Do not flush an ambiguous directive prefix from the superseded preview.
        # The authoritative snapshot starts a fresh directive stream.
        self._directive_filter = StreamDirectiveFilter()
        self.buffer = text
        await self._close_reasoning()
        await self._ensure_line_start()
        self._saw_output = True
        if not text:
            await self._write(
                "[yellow]↻ Streamed preview withdrawn; the final answer is empty.[/yellow]\n"
            )
            return

        await self._write(
            "[yellow]↻ Final answer corrected; an earlier streamed preview was "
            "superseded.[/yellow]\n"
        )
        visible = self._directive_filter.feed(text)
        if visible:
            await self._write(_escape(visible))
        await self._flush_directive_tail()

    async def _flush_directive_tail(self) -> None:
        """Print a held tail that never completed into a directive tag."""
        tail = self._directive_filter.flush()
        if tail:
            self._saw_output = True
            await self._write(_escape(tail))

    async def aappend_reasoning(self, delta: str) -> None:
        if not delta:
            return
        if not self._saw_reasoning:
            self._saw_reasoning = True
            self._reasoning_open = True
            await self._write("[dim]✻ Thinking[/dim]\n")
        await self._write(f"[dim]{_escape(delta)}[/dim]")

    async def atool_start(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        tool_use_id: str | None = None,
    ) -> None:
        del args
        await self._flush_directive_tail()
        await self._close_reasoning()
        # Separate the tool row from any preceding answer prose that streamed
        # without a trailing newline ("I'll check…" + tool call), so the glyph
        # doesn't render on the same line as the text.
        await self._ensure_line_start()
        if tool_use_id is not None:
            self._tool_names[tool_use_id] = name
        await self._write(f"[{ACCENT}]⚙ {_escape(name)}[/]\n")

    async def atool_finished(
        self,
        tool_use_id: str | None,
        *,
        success: bool,
        elapsed: float | None = None,
        error: str | None = None,
        result: object | None = None,
    ) -> None:
        del result
        name = self._tool_names.pop(tool_use_id, None) if tool_use_id is not None else None
        label = f" {_escape(name)}" if name else ""
        took = f" [dim]({elapsed:.1f}s)[/dim]" if elapsed is not None else ""
        if success:
            await self._write(f"[dim]  ✓{label}[/dim]{took}\n")
            return
        detail = _escape(error or "failed")
        await self._write(f"[red]  ✗{label}: {detail}[/red]{took}\n")

    async def astatus(self, message: str, *, style: str = "dim") -> None:
        if not message:
            return
        await self._close_reasoning()
        await self._write(status_markup(message, style=style))

    async def aerror(self, message: str) -> None:
        await self._close_reasoning()
        await self._write(f"\n[red]{_escape(message)}[/red]\n")

    def pulse(self) -> None:
        """Heartbeat tick for long, quiet turns.

        The plain terminal renderer has no live region to refresh, so this is a
        no-op. It exists because the shared turn-stream loop calls ``pulse()``
        unconditionally on every ``RunHeartbeatEvent``; without it a turn that
        stays quiet past the heartbeat interval would raise ``AttributeError``
        and tear down the chat session.
        """
        return None

    async def afinalize(self, usage: Any | None = None, *, cancelled: bool = False) -> None:
        del usage
        await self._flush_directive_tail()
        await self._close_reasoning()
        if cancelled:
            await self._write("\n[yellow]✋ Cancelled[/yellow]\n")
            return
        if self._saw_output:
            await self._write("\n")

    async def aclose(self) -> None:
        return None
