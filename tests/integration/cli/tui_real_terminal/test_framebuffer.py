from __future__ import annotations

from dataclasses import replace

import pytest

from tui_real_terminal.framebuffer import (
    APP_BACKGROUND,
    DEFAULT_BACKGROUND,
    FOOTER_BACKGROUND,
    FOOTER_HEIGHT,
    PROMPT_BACKGROUND,
    SCROLLBAR_TRACK_BACKGROUND,
    FramebufferCell,
    FramebufferParseError,
    FrameGeometry,
    StyledFramebuffer,
    assert_opentui_framebuffer,
    context_rail_width,
    opentui_framebuffer_violations,
    parse_tmux_styled_framebuffer,
)


@pytest.mark.parametrize(
    ("cols", "rows", "content", "footer_top", "composer_top", "rail_left"),
    [
        (80, 24, 80, 18, 19, None),
        (120, 30, 120, 24, 25, None),
        (160, 40, 124, 34, 35, 124),
    ],
)
def test_frame_geometry_owns_transcript_footer_composer_and_rail_boundaries(
    cols: int,
    rows: int,
    content: int,
    footer_top: int,
    composer_top: int,
    rail_left: int | None,
) -> None:
    geometry = FrameGeometry.from_size(cols=cols, rows=rows)

    assert geometry.content_width == content
    assert geometry.footer_top == footer_top
    assert geometry.composer_top == composer_top
    assert geometry.composer_body_top == composer_top + 1
    assert geometry.composer_bottom == rows - 1
    assert geometry.rail_left == rail_left


def test_framebuffer_gate_optionally_requires_cursor_inside_composer() -> None:
    frame = _canonical_frame(120, 34)

    assert_opentui_framebuffer(frame, cursor=(4, 30))
    with pytest.raises(AssertionError, match="hardware cursor .* outside the composer"):
        assert_opentui_framebuffer(frame, cursor=(4, 28))
    with pytest.raises(AssertionError, match="hardware cursor .* outside the composer"):
        assert_opentui_framebuffer(frame, cursor=(119, 30))


def test_styled_parser_preserves_background_state_but_not_unpainted_padding() -> None:
    frame = parse_tmux_styled_framebuffer(
        "\x1b[48;2;18;18;18mAB  \n\x1b[38;2;255;106;40mCD\n",
        checkpoint="styled",
        cols=4,
        rows=2,
        captured_at_ms=1,
    )

    assert [cell.background for cell in frame.cells[0]] == [APP_BACKGROUND] * 4
    # tmux carries SGR between serialized rows. Its omitted tail positions are
    # still empty/default cells, not painted spaces using that carried state.
    assert [cell.background for cell in frame.cells[1]] == [
        APP_BACKGROUND,
        APP_BACKGROUND,
        DEFAULT_BACKGROUND,
        DEFAULT_BACKGROUND,
    ]
    # RGB foreground components such as 106 or 40 must never be mistaken for
    # standalone ANSI background parameters.
    assert frame.cells[1][0].background == APP_BACKGROUND


def test_styled_parser_expands_wide_glyphs_to_display_cells() -> None:
    frame = parse_tmux_styled_framebuffer(
        "\x1b[48:2::18:18:18m中A \n",
        checkpoint="cjk",
        cols=4,
        rows=1,
        captured_at_ms=1,
    )

    assert frame.row_text(0) == "中A "
    assert frame.cells[0][0] == FramebufferCell("中", APP_BACKGROUND)
    assert frame.cells[0][1] == FramebufferCell("", APP_BACKGROUND, continuation=True)
    assert [cell.background for cell in frame.cells[0]] == [APP_BACKGROUND] * 4


@pytest.mark.parametrize(
    ("raw", "cols", "rows", "message"),
    [
        ("one row\n", 8, 2, "has 1 rows"),
        ("five!\n", 4, 1, "exceeds 4 display cells"),
        ("\x1b[2Jbad\n", 8, 1, "unsupported escape sequence"),
    ],
)
def test_styled_parser_rejects_non_exact_framebuffers(
    raw: str,
    cols: int,
    rows: int,
    message: str,
) -> None:
    with pytest.raises(FramebufferParseError, match=message):
        parse_tmux_styled_framebuffer(
            raw,
            checkpoint="invalid",
            cols=cols,
            rows=rows,
            captured_at_ms=1,
        )


@pytest.mark.parametrize("cols,rows", [(72, 24), (120, 34), (132, 34), (160, 40)])
def test_canonical_framebuffer_passes_background_and_fixed_chrome_gate(
    cols: int,
    rows: int,
) -> None:
    assert_opentui_framebuffer(_canonical_frame(cols, rows))


def test_default_background_hole_is_a_blocking_violation() -> None:
    frame = _replace_cell(
        _canonical_frame(120, 34),
        row=5,
        col=20,
        background=DEFAULT_BACKGROUND,
    )

    violations = opentui_framebuffer_violations(frame)

    assert any("background-mask" in violation for violation in violations)


def test_footer_background_staircase_in_transcript_is_a_blocking_violation() -> None:
    frame = _canonical_frame(120, 34)
    for row in range(4, 10):
        for col in range(12 + row, 22 + row):
            frame = _replace_cell(
                frame,
                row=row,
                col=col,
                background=FOOTER_BACKGROUND,
            )

    violations = opentui_framebuffer_violations(frame)

    assert any("background-mask: 60 mismatched cells" in violation for violation in violations)


def test_exact_semantic_prompt_surface_is_allowed_in_transcript() -> None:
    frame = _canonical_frame(120, 34)
    for row in (5, 6):
        for col in range(1, 119):
            frame = _replace_cell(
                frame,
                row=row,
                col=col,
                background=PROMPT_BACKGROUND,
            )
    frame = _write_text(frame, row=5, col=1, text="│ you  explain this")
    frame = _write_text(frame, row=6, col=1, text="│      second line")

    assert not opentui_framebuffer_violations(frame)


def test_exact_prompt_surface_accounts_for_active_scrollbar_gutter() -> None:
    frame = _canonical_frame(72, 24)
    for row in range(2, 24 - FOOTER_HEIGHT):
        frame = _replace_cell(
            frame,
            row=row,
            col=71,
            background=SCROLLBAR_TRACK_BACKGROUND,
        )
    for col in range(1, 70):
        frame = _replace_cell(
            frame,
            row=5,
            col=col,
            background=PROMPT_BACKGROUND,
        )
    frame = _write_text(frame, row=5, col=1, text="│ you  scrollable prompt")

    assert not opentui_framebuffer_violations(frame)


def test_surface_rectangle_without_prompt_role_is_a_blocking_violation() -> None:
    frame = _canonical_frame(120, 34)
    for col in range(1, 119):
        frame = _replace_cell(
            frame,
            row=5,
            col=col,
            background=PROMPT_BACKGROUND,
        )
    frame = _write_text(frame, row=5, col=1, text="│ stale surface")

    violations = opentui_framebuffer_violations(frame)

    assert any("background-mask: 118 mismatched cells" in violation for violation in violations)


def test_incomplete_prompt_surface_is_a_blocking_violation() -> None:
    frame = _canonical_frame(120, 34)
    for col in range(1, 118):
        frame = _replace_cell(
            frame,
            row=5,
            col=col,
            background=PROMPT_BACKGROUND,
        )
    frame = _write_text(frame, row=5, col=1, text="│ you  clipped surface")

    violations = opentui_framebuffer_violations(frame)

    assert any("background-mask: 117 mismatched cells" in violation for violation in violations)


def test_scrollbar_track_color_is_allowed_only_on_transcript_edge() -> None:
    edge = _replace_cell(
        _canonical_frame(72, 24),
        row=5,
        col=71,
        background=SCROLLBAR_TRACK_BACKGROUND,
    )
    residue = _replace_cell(
        edge,
        row=5,
        col=70,
        background=SCROLLBAR_TRACK_BACKGROUND,
    )

    assert not opentui_framebuffer_violations(edge)
    assert any(
        "background-mask" in violation for violation in opentui_framebuffer_violations(residue)
    )


def test_duplicate_composer_residue_is_a_blocking_violation() -> None:
    frame = _write_text(_canonical_frame(120, 34), row=10, col=6, text="send a message")

    violations = opentui_framebuffer_violations(frame)

    assert any("composer-placeholder" in violation for violation in violations)


def test_border_only_duplicate_composer_residue_is_a_blocking_violation() -> None:
    frame = _write_text(
        _canonical_frame(120, 34),
        row=34 - FOOTER_HEIGHT,
        col=20,
        text="╭────────────╮",
    )

    violations = opentui_framebuffer_violations(frame)

    assert any(
        "composer-border" in violation and "duplicate/residual" in violation
        for violation in violations
    )


def test_transcript_glyphs_on_correct_footer_background_are_blocking() -> None:
    frame = _write_text(
        _canonical_frame(120, 34),
        row=34 - FOOTER_HEIGHT + 3,
        col=10,
        text="Thinking · stale transcript frame",
    )

    violations = opentui_framebuffer_violations(frame)

    assert any("surface-ownership" in violation for violation in violations)


def test_duplicate_context_rail_is_a_blocking_violation() -> None:
    frame = _canonical_frame(132, 34)
    for row in range(frame.rows):
        frame = _replace_cell(frame, row=row, col=80, glyph="│")

    violations = opentui_framebuffer_violations(frame)

    assert any("context-rail" in violation and "80" in violation for violation in violations)


def test_block_logo_overflow_into_context_rail_is_a_blocking_violation() -> None:
    frame = _canonical_frame(132, 34)
    content = frame.cols - context_rail_width(frame.cols)
    frame = _replace_cell(frame, row=4, col=content + 5, glyph="█")

    violations = opentui_framebuffer_violations(frame)

    assert any("block logo overflowed" in violation for violation in violations)


def test_wrapped_partial_context_headings_are_a_blocking_violation() -> None:
    frame = _canonical_frame(72, 24)
    frame = _write_text(frame, row=1, col=0, text="GENT")
    frame = _write_text(frame, row=5, col=0, text="AFETY")
    frame = _write_text(frame, row=8, col=0, text="OUTING")

    violations = opentui_framebuffer_violations(frame)

    assert any("wrapped heading fragments" in violation for violation in violations)


@pytest.mark.parametrize("cols,rows", [(80, 24), (120, 30), (160, 40)])
def test_welcome_identity_passes_at_supported_visual_gate_sizes(
    cols: int,
    rows: int,
) -> None:
    assert_opentui_framebuffer(_canonical_welcome_frame(cols, rows))


def test_duplicate_welcome_header_is_a_blocking_violation() -> None:
    frame = _canonical_welcome_frame(120, 30)
    frame = _write_text(frame, row=16, col=2, text="OpenSquilla · stale header")

    violations = opentui_framebuffer_violations(frame)

    assert any("fixed-header" in violation for violation in violations)


def test_duplicate_welcome_tagline_is_a_blocking_violation() -> None:
    frame = _canonical_welcome_frame(120, 30)
    frame = _write_text(
        frame,
        row=16,
        col=2,
        text="Build with your agent. Stay in the flow.",
    )

    violations = opentui_framebuffer_violations(frame)

    assert any("welcome-tagline" in violation for violation in violations)


def test_scrolled_partial_welcome_is_not_misclassified_as_a_duplicate_logo() -> None:
    frame = replace(_canonical_welcome_frame(160, 40), checkpoint="after-stream")
    for row in (4, 5):
        frame = _write_text(frame, row=row, col=2, text=" " * 20)

    violations = opentui_framebuffer_violations(frame)

    assert not any("welcome-logo" in violation for violation in violations)
    duplicate_header = _write_text(
        frame,
        row=16,
        col=2,
        text="OpenSquilla · stale fixed header",
    )
    assert any(
        "fixed-header" in violation
        for violation in opentui_framebuffer_violations(duplicate_header)
    )


@pytest.mark.parametrize("cols,rows", [(80, 24), (120, 30), (160, 40)])
def test_duplicate_welcome_logo_is_a_blocking_violation(
    cols: int,
    rows: int,
) -> None:
    frame = _canonical_welcome_frame(cols, rows)
    logo_height = 6 if frame.geometry.content_width >= 100 else 2
    duplicate_start = 11 if cols == 80 else 16
    for row in range(duplicate_start, duplicate_start + logo_height):
        frame = _write_text(frame, row=row, col=2, text="█" * 20)

    violations = opentui_framebuffer_violations(frame)

    assert any("welcome-logo" in violation for violation in violations)


def _canonical_frame(cols: int, rows: int) -> StyledFramebuffer:
    geometry = FrameGeometry.from_size(cols=cols, rows=rows)
    rail = geometry.rail_width
    content = geometry.content_width
    footer_top = geometry.footer_top
    cells: list[list[FramebufferCell]] = []
    for row in range(rows):
        cells.append(
            [
                FramebufferCell(
                    " ",
                    FOOTER_BACKGROUND if row >= footer_top and col < content else APP_BACKGROUND,
                )
                for col in range(cols)
            ]
        )

    if rail:
        for row in range(rows):
            cells[row][content] = replace(cells[row][content], glyph="│")
        _write_mutable(cells, row=0, col=content + 2, text="AGENT")
        _write_mutable(cells, row=6, col=content + 2, text="RUNTIME")

    _write_mutable(cells, row=0, col=1, text="OpenSquilla · Session")

    footer_strip = "direct · model fake-terminal"
    _write_mutable(cells, row=footer_top, col=3, text=footer_strip)
    for col in range(1, content - 1):
        cells[footer_top + 1][col] = replace(
            cells[footer_top + 1][col],
            glyph="╭" if col == 1 else "╮" if col == content - 2 else "─",
        )
        cells[rows - 1][col] = replace(
            cells[rows - 1][col],
            glyph="╰" if col == 1 else "╯" if col == content - 2 else "─",
        )
    for row in range(footer_top + 2, rows - 1):
        cells[row][1] = replace(cells[row][1], glyph="│")
        cells[row][content - 2] = replace(cells[row][content - 2], glyph="│")
    _write_mutable(cells, row=footer_top + 2, col=4, text="send a message")

    return StyledFramebuffer(
        checkpoint=f"canonical-{cols}x{rows}",
        raw="",
        captured_at_ms=1,
        cols=cols,
        rows=rows,
        cells=tuple(tuple(row) for row in cells),
    )


def _canonical_welcome_frame(cols: int, rows: int) -> StyledFramebuffer:
    frame = replace(_canonical_frame(cols, rows), checkpoint="ready")
    frame = _write_text(frame, row=0, col=1, text="OpenSquilla · Session")
    if frame.geometry.content_width >= 100:
        logo_start = 4
        logo_height = 6
        tagline_row = 11
        marker_row = 14
    else:
        logo_start = 3
        logo_height = 2
        tagline_row = 5
        marker_row = 8
    for row in range(logo_start, logo_start + logo_height):
        frame = _write_text(frame, row=row, col=2, text="█" * 20)
    frame = _write_text(
        frame,
        row=tagline_row,
        col=2,
        text="Build with your agent. Stay in the flow.",
    )
    frame = _write_text(
        frame,
        row=marker_row,
        col=0,
        text="OPEN_SQUILLA_TUI_READY",
    )
    return frame


def _replace_cell(
    frame: StyledFramebuffer,
    *,
    row: int,
    col: int,
    glyph: str | None = None,
    background: str | None = None,
) -> StyledFramebuffer:
    cells = [list(line) for line in frame.cells]
    original = cells[row][col]
    cells[row][col] = FramebufferCell(
        original.glyph if glyph is None else glyph,
        original.background if background is None else background,
        original.continuation,
    )
    return replace(frame, cells=tuple(tuple(line) for line in cells))


def _write_text(
    frame: StyledFramebuffer,
    *,
    row: int,
    col: int,
    text: str,
) -> StyledFramebuffer:
    cells = [list(line) for line in frame.cells]
    _write_mutable(cells, row=row, col=col, text=text)
    return replace(frame, cells=tuple(tuple(line) for line in cells))


def _write_mutable(
    cells: list[list[FramebufferCell]],
    *,
    row: int,
    col: int,
    text: str,
) -> None:
    for offset, glyph in enumerate(text):
        cells[row][col + offset] = replace(cells[row][col + offset], glyph=glyph)
