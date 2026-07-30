from __future__ import annotations

import pytest

from opensquilla.cli.tui.adapters.native_bridge import NativeTerminalOutputHandle
from opensquilla.cli.tui.native.renderer import NativeStreamRenderer
from opensquilla.engine.commands import Surface
from opensquilla.ui import ACCENT


class _RecordingOutputHandle:
    approval_surface = object()

    def __init__(self) -> None:
        self.writes: list[str] = []

    async def write_through(self, payload: str) -> None:
        self.writes.append(payload)

    def stream_output(self):
        raise AssertionError("native renderer writes through directly")


@pytest.mark.asyncio
async def test_native_renderer_writes_answer_text_to_terminal_output() -> None:
    output = _RecordingOutputHandle()
    renderer = NativeStreamRenderer(output_handle=output)

    renderer.__enter__()
    await renderer.aappend_text("hello", presentation="answer")
    await renderer.aappend_text(" there", presentation="answer")
    await renderer.afinalize(None)

    assert renderer.buffer == "hello there"
    assert output.writes == ["hello", " there", "\n"]


@pytest.mark.asyncio
async def test_native_conflicting_final_snapshot_prints_unambiguous_correction_after_tools() -> (
    None
):
    output = _RecordingOutputHandle()
    renderer = NativeStreamRenderer(output_handle=output)

    await renderer.aappend_text("stale preview")
    await renderer.atool_start("lookup", tool_use_id="t1")
    await renderer.atool_finished("t1", success=True)
    await renderer.areconcile_final_text("canonical answer")
    await renderer.afinalize(None)

    rendered = "".join(output.writes)
    assert renderer.buffer == "canonical answer"
    assert "earlier streamed preview was superseded" in rendered
    assert "canonical answer" in rendered
    assert rendered.index("lookup") < rendered.index("canonical answer")
    # Plain-terminal scrollback cannot safely erase the preview, so the visible
    # correction—not a silent buffer mutation—is the terminal contract.
    assert rendered.index("stale preview") < rendered.index("superseded")


@pytest.mark.asyncio
async def test_native_explicit_empty_snapshot_visibly_withdraws_streamed_preview() -> None:
    output = _RecordingOutputHandle()
    renderer = NativeStreamRenderer(output_handle=output)

    await renderer.aappend_text("preview to withdraw")
    await renderer.areconcile_final_text("")
    await renderer.afinalize(None)

    rendered = "".join(output.writes)
    assert renderer.buffer == ""
    assert "preview to withdraw" in rendered
    assert "Streamed preview withdrawn; the final answer is empty." in rendered


@pytest.mark.asyncio
async def test_native_strict_snapshot_extension_prints_only_the_missing_suffix() -> None:
    output = _RecordingOutputHandle()
    renderer = NativeStreamRenderer(output_handle=output)

    await renderer.aappend_text("prefix")
    await renderer.areconcile_final_text("prefix suffix")

    assert renderer.buffer == "prefix suffix"
    assert output.writes == ["prefix", " suffix"]


@pytest.mark.asyncio
async def test_native_tool_start_separates_from_midline_answer_text() -> None:
    # A common "I'll check…" + tool-call sequence: aappend_text streams prose with
    # no trailing newline, so atool_start must break to a fresh line or the glyph
    # collides with the text ("I'll check⚙ grep").
    output = _RecordingOutputHandle()
    renderer = NativeStreamRenderer(output_handle=output)
    renderer.__enter__()
    await renderer.aappend_text("I'll check", presentation="answer")
    await renderer.atool_start("grep", tool_use_id="t1")

    joined = "".join(output.writes)
    assert "I'll check⚙" not in joined  # no collision
    assert "I'll check\n" in joined  # a separator was inserted before the tool row
    assert f"[{ACCENT}]⚙ grep[/]" in joined


@pytest.mark.asyncio
async def test_native_tool_start_adds_no_blank_line_when_already_at_line_start() -> None:
    # When prose already ended with a newline, the separator must not add a second
    # (a spurious blank line before the tool row).
    output = _RecordingOutputHandle()
    renderer = NativeStreamRenderer(output_handle=output)
    renderer.__enter__()
    await renderer.aappend_text("done\n", presentation="answer")
    await renderer.atool_start("grep", tool_use_id="t1")

    assert "\n\n" not in "".join(output.writes)


@pytest.mark.asyncio
async def test_native_renderer_pulse_is_a_safe_no_op() -> None:
    # The shared turn-stream loop calls renderer.pulse() unconditionally on every
    # heartbeat; the native renderer must define it so a quiet turn does not raise
    # AttributeError and tear down the session.
    renderer = NativeStreamRenderer(output_handle=_RecordingOutputHandle())
    assert renderer.pulse() is None


@pytest.mark.asyncio
async def test_native_renderer_escapes_bracketed_text_without_markup_error() -> None:
    # Real console + handle: bracketed model output (paths, markup-like tokens)
    # must render literally instead of raising MarkupError or being restyled.
    from opensquilla.ui import console

    handle = NativeTerminalOutputHandle(approval_surface=Surface.CLI_STANDALONE)
    renderer = NativeStreamRenderer(output_handle=handle)
    renderer.__enter__()

    payload = "see [/usr/local/bin] then [dim]styled[/dim] and arr[i]"
    with console.capture() as capture:
        await renderer.aappend_text(payload)
        renderer.pulse()
        await renderer.afinalize(None)

    rendered = capture.get()
    assert "[/usr/local/bin]" in rendered
    assert "[dim]styled[/dim]" in rendered
    assert "arr[i]" in rendered
    # buffer keeps the raw assistant text for TurnResult, unescaped.
    assert renderer.buffer == payload


@pytest.mark.asyncio
async def test_native_renderer_renders_reasoning_dimmed_then_separates_answer() -> None:
    output = _RecordingOutputHandle()
    renderer = NativeStreamRenderer(output_handle=output)

    await renderer.aappend_reasoning("pondering")
    await renderer.aappend_text("answer")

    assert output.writes == [
        "[dim]✻ Thinking[/dim]\n",
        "[dim]pondering[/dim]",
        "\n",
        "answer",
    ]
    # reasoning is not part of the assistant answer buffer.
    assert renderer.buffer == "answer"


@pytest.mark.asyncio
async def test_native_renderer_renders_status_with_mapped_style_and_escaping() -> None:
    output = _RecordingOutputHandle()
    renderer = NativeStreamRenderer(output_handle=output)

    await renderer.astatus("Still working", style="dim")
    await renderer.astatus("oops [x]", style="error")

    assert output.writes[0] == "[dim]Still working[/dim]\n"
    assert output.writes[1] == "[red]oops \\[x][/red]\n"


@pytest.mark.asyncio
async def test_native_renderer_reports_tool_completion_with_elapsed() -> None:
    output = _RecordingOutputHandle()
    renderer = NativeStreamRenderer(output_handle=output)

    await renderer.atool_start("grep", tool_use_id="t1")
    await renderer.atool_finished("t1", success=True, elapsed=1.25)
    await renderer.atool_start("rm", tool_use_id="t2")
    await renderer.atool_finished("t2", success=False, error="bad [path]")

    assert output.writes == [
        f"[{ACCENT}]⚙ grep[/]\n",
        "[dim]  ✓ grep[/dim] [dim](1.2s)[/dim]\n",
        f"[{ACCENT}]⚙ rm[/]\n",
        "[red]  ✗ rm: bad \\[path][/red]\n",
    ]


@pytest.mark.asyncio
async def test_native_renderer_strips_routing_directive_tags() -> None:
    output = _RecordingOutputHandle()
    renderer = NativeStreamRenderer(output_handle=output)

    await renderer.aappend_text("[[reply_to")
    await renderer.aappend_text("_current]]\n")
    await renderer.aappend_text("My name is OpenSquilla.")
    await renderer.afinalize()

    joined = "".join(output.writes)
    assert "reply_to_current" not in joined
    assert "My name is OpenSquilla." in joined
    # The logical buffer (TurnResult text) keeps the model's exact output.
    assert "[[reply_to_current]]" in renderer.buffer


@pytest.mark.asyncio
async def test_native_renderer_flushes_held_bracket_prefix_before_a_tool_row() -> None:
    output = _RecordingOutputHandle()
    renderer = NativeStreamRenderer(output_handle=output)

    await renderer.aappend_text("see [[re")  # held: could still become a tag
    await renderer.atool_start("grep", tool_use_id="t1")
    await renderer.afinalize()

    joined = "".join(output.writes)
    # The held prefix proved to be ordinary text and prints BEFORE the tool row.
    assert joined.index("[[re") < joined.index("grep")
