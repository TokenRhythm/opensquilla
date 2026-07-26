from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from rich.cells import get_character_cell_size

DEFAULT_BACKGROUND = "default"
APP_BACKGROUND = "rgb:18,18,18"
FOOTER_BACKGROUND = "rgb:26,26,27"
PROMPT_BACKGROUND = FOOTER_BACKGROUND
SCROLLBAR_TRACK_BACKGROUND = "rgb:37,37,39"
FOOTER_HEIGHT = 6
CONTEXT_RAIL_MIN_COLUMNS = 132
BLOCK_LOGO_GLYPHS = frozenset("█▀▄╔╗╚╝║═")
COMPOSER_BORDER_GLYPHS = frozenset("╭╮╰╯─│")
RAIL_EDGE_FRAGMENTS = (
    "AGENT",
    "GENT",
    "TASK",
    "RUNTIME",
    "UNTIME",
    "SAFETY",
    "AFETY",
    "ROUTING",
    "OUTING",
)
WELCOME_HEADER = "OpenSquilla ·"
WELCOME_TAGLINE = "Build with your agent. Stay in the flow."
WELCOME_READY_MARKER = "OPEN_SQUILLA_TUI_READY"
WELCOME_BLOCK_MIN_COLUMNS = 100
WELCOME_TINY_MIN_COLUMNS = 46
WELCOME_DISPLAY_MIN_ROWS = 18
WELCOME_LOGO_MIN_GLYPHS_PER_ROW = 12


class FramebufferParseError(ValueError):
    """The styled tmux capture could not be represented as an exact cell grid."""


@dataclass(frozen=True)
class FramebufferCell:
    glyph: str
    background: str = DEFAULT_BACKGROUND
    continuation: bool = False


@dataclass(frozen=True)
class FrameGeometry:
    """One source of truth for the fixed OpenTUI screen regions.

    The production renderer keeps the transcript and footer as absolute
    siblings and optionally reserves a right-hand context rail.  Visual gates
    must derive all of those boundaries from the same terminal dimensions;
    computing them independently is how an old-width footer previously passed
    while its transcript used the new viewport.
    """

    cols: int
    rows: int
    rail_width: int
    content_width: int
    footer_top: int
    composer_top: int
    composer_body_top: int
    composer_bottom: int
    rail_left: int | None

    @classmethod
    def from_size(cls, *, cols: int, rows: int) -> FrameGeometry:
        if cols <= 0 or rows <= FOOTER_HEIGHT:
            raise ValueError(
                "framebuffer must have positive width and room above the footer"
            )
        rail_width = context_rail_width(cols)
        content_width = cols - rail_width
        footer_top = rows - FOOTER_HEIGHT
        return cls(
            cols=cols,
            rows=rows,
            rail_width=rail_width,
            content_width=content_width,
            footer_top=footer_top,
            composer_top=footer_top + 1,
            composer_body_top=footer_top + 2,
            composer_bottom=rows - 1,
            rail_left=content_width if rail_width else None,
        )


@dataclass(frozen=True)
class StyledFramebuffer:
    checkpoint: str
    raw: str
    captured_at_ms: int
    cols: int
    rows: int
    cells: tuple[tuple[FramebufferCell, ...], ...]

    def __post_init__(self) -> None:
        if self.cols <= 0 or self.rows <= 0:
            raise ValueError("framebuffer dimensions must be positive")
        if len(self.cells) != self.rows:
            raise ValueError(f"framebuffer has {len(self.cells)} rows, expected {self.rows}")
        for index, row in enumerate(self.cells):
            if len(row) != self.cols:
                raise ValueError(
                    f"framebuffer row {index} has {len(row)} cells, expected {self.cols}"
                )

    def row_text(self, row: int) -> str:
        return "".join(cell.glyph for cell in self.cells[row] if not cell.continuation)

    @property
    def text(self) -> str:
        return "\n".join(self.row_text(row) for row in range(self.rows))

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "checkpoint": self.checkpoint,
            "captured_at_ms": self.captured_at_ms,
            "size": {"cols": self.cols, "rows": self.rows},
            "rows": [
                {
                    "text": self.row_text(row_index),
                    "background_runs": _background_runs(row),
                }
                for row_index, row in enumerate(self.cells)
            ],
        }

    @property
    def geometry(self) -> FrameGeometry:
        return FrameGeometry.from_size(cols=self.cols, rows=self.rows)


def parse_tmux_styled_framebuffer(
    raw: str,
    *,
    checkpoint: str,
    cols: int,
    rows: int,
    captured_at_ms: int,
) -> StyledFramebuffer:
    """Parse ``tmux capture-pane -e -N -p`` into an exact display-cell grid.

    tmux serializes SGR state across physical row separators. Missing positions
    at the end of a row, however, are empty terminal cells rather than spaces
    painted with the carried SGR state, so they are padded with terminal-default
    background. ``-N`` ensures styled trailing spaces are emitted and therefore
    remain distinguishable from that padding.
    """

    if cols <= 0 or rows <= 0:
        raise FramebufferParseError("framebuffer dimensions must be positive")
    lines = raw.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) != rows:
        raise FramebufferParseError(
            f"styled capture has {len(lines)} rows, expected exactly {rows}"
        )

    background = DEFAULT_BACKGROUND
    parsed_rows: list[tuple[FramebufferCell, ...]] = []
    for row_index, line in enumerate(lines):
        cells: list[FramebufferCell] = []
        offset = 0
        while offset < len(line):
            if line[offset] == "\x1b":
                end = line.find("m", offset + 2)
                if not line.startswith("\x1b[", offset) or end < 0:
                    raise FramebufferParseError(
                        f"unsupported escape sequence on row {row_index} at offset {offset}"
                    )
                parameters = line[offset + 2 : end]
                if any(char not in "0123456789;:" for char in parameters):
                    raise FramebufferParseError(
                        f"unsupported CSI sequence on row {row_index} at offset {offset}"
                    )
                background = _apply_sgr_background(background, parameters)
                offset = end + 1
                continue

            codepoint = ord(line[offset])
            if 0xD800 <= codepoint <= 0xDBFF and offset + 1 < len(line):
                following = ord(line[offset + 1])
                if 0xDC00 <= following <= 0xDFFF:
                    glyph = line[offset : offset + 2]
                    offset += 2
                else:
                    glyph = line[offset]
                    offset += 1
            else:
                glyph = line[offset]
                offset += 1

            width = get_character_cell_size(glyph)
            if width < 0:
                raise FramebufferParseError(f"non-printable glyph {glyph!r} on row {row_index}")
            if width == 0:
                primary = _last_primary_cell(cells)
                if primary is None:
                    raise FramebufferParseError(
                        f"zero-width glyph without a base on row {row_index}"
                    )
                cells[primary] = FramebufferCell(
                    cells[primary].glyph + glyph,
                    cells[primary].background,
                    continuation=False,
                )
                continue
            if len(cells) + width > cols:
                raise FramebufferParseError(
                    f"styled capture row {row_index} exceeds {cols} display cells"
                )
            cells.append(FramebufferCell(glyph, background))
            cells.extend(
                FramebufferCell("", background, continuation=True) for _ in range(width - 1)
            )

        cells.extend(FramebufferCell(" ", DEFAULT_BACKGROUND) for _ in range(cols - len(cells)))
        parsed_rows.append(tuple(cells))

    return StyledFramebuffer(
        checkpoint=checkpoint,
        raw=raw,
        captured_at_ms=captured_at_ms,
        cols=cols,
        rows=rows,
        cells=tuple(parsed_rows),
    )


def context_rail_width(cols: int) -> int:
    if cols < CONTEXT_RAIL_MIN_COLUMNS:
        return 0
    return max(30, min(36, int(cols * 0.225)))


def opentui_framebuffer_violations(
    frame: StyledFramebuffer,
    *,
    required_rail_headings: tuple[str, ...] = ("AGENT", "RUNTIME"),
) -> tuple[str, ...]:
    """Return structural violations for the deterministic dark-theme TUI gate."""

    violations: list[str] = []
    geometry = frame.geometry
    rail_width = geometry.rail_width
    content_width = geometry.content_width
    footer_top = geometry.footer_top
    prompt_surface_cells = _prompt_surface_cells(
        frame,
        footer_top=footer_top,
        content_width=content_width,
    )

    mismatches: list[tuple[int, int, str, str]] = []
    for row_index, row in enumerate(frame.cells):
        for col_index, cell in enumerate(row):
            expected = (
                FOOTER_BACKGROUND
                if row_index >= footer_top and col_index < content_width
                else APP_BACKGROUND
            )
            scrollbar_cell = (
                row_index < footer_top
                and col_index == content_width - 1
                and cell.background in {APP_BACKGROUND, SCROLLBAR_TRACK_BACKGROUND}
            )
            prompt_surface_cell = (row_index, col_index) in prompt_surface_cells
            if cell.background != expected and not scrollbar_cell and not prompt_surface_cell:
                mismatches.append((row_index, col_index, cell.background, expected))
    if mismatches:
        sample = ", ".join(
            f"({row},{col})={actual}, expected {expected}"
            for row, col, actual, expected in mismatches[:6]
        )
        violations.append(f"background-mask: {len(mismatches)} mismatched cells; {sample}")

    rows = [frame.row_text(index) for index in range(frame.rows)]
    fixed_header_locations = [
        (row_index, match.start())
        for row_index, row in enumerate(rows[:footer_top])
        for match in re.finditer(re.escape(WELCOME_HEADER), row)
    ]
    if len(fixed_header_locations) != 1:
        violations.append(
            "fixed-header: expected exactly one OpenSquilla identity header, "
            f"found {fixed_header_locations}"
        )
    composer_placeholders = ("send a message", "steer current turn · Tab queues")
    placeholder_locations = [
        (row_index, row.find(placeholder), placeholder)
        for row_index, row in enumerate(rows)
        for placeholder in composer_placeholders
        if placeholder in row
    ]
    if len(placeholder_locations) != 1:
        violations.append(
            "composer-placeholder: expected exactly one idle or busy placeholder, "
            f"found {placeholder_locations}"
        )
    elif placeholder_locations[0][0] != footer_top + 2:
        violations.append(
            "composer-placeholder: placeholder is outside the composer row at "
            f"{placeholder_locations[0]}"
        )

    top_row = geometry.composer_top
    bottom_row = geometry.composer_bottom
    expected_border: dict[tuple[int, int], str] = {}
    for col_index in range(1, content_width - 1):
        expected_border[(top_row, col_index)] = (
            "╭" if col_index == 1 else "╮" if col_index == content_width - 2 else "─"
        )
        expected_border[(bottom_row, col_index)] = (
            "╰" if col_index == 1 else "╯" if col_index == content_width - 2 else "─"
        )
    for row_index in range(top_row + 1, bottom_row):
        expected_border[(row_index, 1)] = "│"
        expected_border[(row_index, content_width - 2)] = "│"

    missing_border = [
        (row, col, glyph, frame.cells[row][col].glyph)
        for (row, col), glyph in expected_border.items()
        if frame.cells[row][col].glyph != glyph
    ]
    if missing_border:
        sample = ", ".join(
            f"({row},{col})={actual!r}, expected {expected!r}"
            for row, col, expected, actual in missing_border[:6]
        )
        violations.append(
            f"composer-border: {len(missing_border)} missing/mutated perimeter cells; {sample}"
        )

    unexpected_border = [
        (row_index, col_index, cell.glyph)
        for row_index in range(footer_top, frame.rows)
        for col_index, cell in enumerate(frame.cells[row_index][:content_width])
        if cell.glyph in COMPOSER_BORDER_GLYPHS and (row_index, col_index) not in expected_border
    ]
    if unexpected_border:
        violations.append(
            f"composer-border: unexpected duplicate/residual border cells {unexpected_border[:12]}"
        )

    footer_strip_marker = re.compile(
        r"(?:next )?(?:direct|router|ensemble)(?: …)? ·"
    )
    footer_strip_locations = [
        row_index for row_index, row in enumerate(rows) if footer_strip_marker.search(row)
    ]
    if footer_strip_locations != [footer_top]:
        violations.append(
            "footer-strip: expected one direct/router/ensemble strategy strip on row "
            f"{footer_top}, "
            f"found {footer_strip_locations}"
        )

    # Background ownership alone cannot detect an old transcript glyph painted
    # onto an otherwise-correct footer surface. These gates run with the idle or
    # busy placeholder (an empty draft), so any turn-role marker below
    # ``footer_top`` is unambiguously stale transcript content.
    transcript_footer_markers = (
        re.compile(r"(?:^|\s)you\s{2}"),
        re.compile(r"(?:^|\s)(?:main|squilla)\s*$"),
        re.compile(r"\b(?:Thinking|Thought for|Worked for)\b"),
        re.compile(r"(?:^|\s)in [\d,]+ / out [\d,]+"),
    )
    transcript_in_footer = [
        (row_index, marker.pattern)
        for row_index in range(footer_top, frame.rows)
        for marker in transcript_footer_markers
        if marker.search(rows[row_index])
    ]
    if transcript_in_footer:
        violations.append(
            "surface-ownership: transcript markers painted inside footer rows "
            f"{transcript_in_footer}"
        )

    full_height_rails = [
        col_index
        for col_index in range(frame.cols)
        if all(frame.cells[row][col_index].glyph == "│" for row in range(frame.rows))
    ]
    if rail_width:
        if full_height_rails != [content_width]:
            violations.append(
                f"context-rail: expected only full-height rail {content_width}, "
                f"found {full_height_rails}"
            )
        for heading in required_rail_headings:
            locations = [
                (row_index, row.find(heading))
                for row_index, row in enumerate(rows)
                if heading in row
            ]
            if len(locations) != 1 or locations[0][1] <= content_width:
                violations.append(
                    f"context-rail: expected one {heading} heading inside rail, found {locations}"
                )
        logo_in_rail = [
            (row_index, col_index, frame.cells[row_index][col_index].glyph)
            for row_index in range(footer_top)
            for col_index in range(content_width + 1, frame.cols)
            if frame.cells[row_index][col_index].glyph in BLOCK_LOGO_GLYPHS
        ]
        if logo_in_rail:
            violations.append(
                f"context-rail: block logo overflowed into rail at {logo_in_rail[:12]}"
            )
    else:
        if full_height_rails:
            violations.append(
                f"context-rail: narrow layout retained full-height rails {full_height_rails}"
            )

    # A stale wider rail can hard-wrap its headings into fragments at either
    # edge after an embedded pane remounts narrow (the supplied screenshots
    # show GENT / AFETY / OUTING down the left edge). Restrict this heuristic to
    # edge cells in the upper chrome/welcome area so ordinary transcript prose
    # containing words such as "runtime" cannot trip the deterministic gate.
    edge_residue: list[tuple[int, str]] = []
    for row_index in range(min(footer_top, 20)):
        left = "".join(cell.glyph for cell in frame.cells[row_index][: min(12, content_width)])
        right_start = max(0, content_width - 12)
        right = "".join(cell.glyph for cell in frame.cells[row_index][right_start:content_width])
        for fragment in RAIL_EDGE_FRAGMENTS:
            if fragment in left or fragment in right:
                edge_residue.append((row_index, fragment))
                break
    if edge_residue:
        violations.append(f"context-rail: wrapped heading fragments at content edge {edge_residue}")

    violations.extend(
        _welcome_identity_violations(
            frame,
            rows=rows,
            geometry=geometry,
        )
    )

    return tuple(violations)


def _welcome_identity_violations(
    frame: StyledFramebuffer,
    *,
    rows: list[str],
    geometry: FrameGeometry,
) -> tuple[str, ...]:
    """Validate the fixed header and transient welcome identity as one copy.

    This is deliberately conditional. Normal turn frames retain the fixed
    identity header but have no welcome mark, so they must not be required to
    render the logo or tagline. The deterministic fake app exposes a ready
    marker; production welcome frames are recognized by their tagline.

    OpenTUI's approved ``tiny`` and ``block`` faces are structurally different:
    the former is two dense glyph rows at 46--99 content cells, while the
    latter is six rows at 100+ cells. Counting one contiguous dense-row
    component catches duplicated or residual welcome nodes without baking a
    screenshot, font rasterizer, or terminal-specific colour conversion into
    this terminal-cell contract.
    """

    # A welcome node belongs to the scrollable transcript, so a later held or
    # streaming frame may legitimately show only its lower rows. Enforce the
    # complete identity only at the explicit initial-ready checkpoint; later
    # frames still retain the generic one-header/footer ownership checks.
    welcome_present = "ready" in frame.checkpoint.lower() and any(
        WELCOME_READY_MARKER in row or WELCOME_TAGLINE in row
        for row in rows[: geometry.footer_top]
    )
    if not welcome_present:
        return ()

    violations: list[str] = []
    tagline_locations = [
        (row_index, match.start())
        for row_index, row in enumerate(rows[: geometry.footer_top])
        for match in re.finditer(re.escape(WELCOME_TAGLINE), row)
    ]
    if len(tagline_locations) != 1:
        violations.append(
            "welcome-tagline: expected exactly one welcome tagline, "
            f"found {tagline_locations}"
        )

    content_width = geometry.content_width
    if (
        frame.rows >= WELCOME_DISPLAY_MIN_ROWS
        and content_width >= WELCOME_TINY_MIN_COLUMNS
    ):
        expected_height = 6 if content_width >= WELCOME_BLOCK_MIN_COLUMNS else 2
        dense_rows = [
            row_index
            for row_index in range(geometry.footer_top)
            if sum(
                frame.cells[row_index][col_index].glyph in BLOCK_LOGO_GLYPHS
                for col_index in range(content_width)
            )
            >= WELCOME_LOGO_MIN_GLYPHS_PER_ROW
        ]
        groups = _consecutive_row_groups(dense_rows)
        expected_groups = [group for group in groups if len(group) == expected_height]
        valid_order = (
            len(expected_groups) == 1
            and len(groups) == 1
            and len(tagline_locations) == 1
            and expected_groups[0][-1] < tagline_locations[0][0]
        )
        if not valid_order:
            violations.append(
                "welcome-logo: expected exactly one "
                f"{expected_height}-row block logo before the tagline, found {groups}"
            )
    else:
        # The plain geometric fallback renders the wordmark as text. Exclude
        # the fixed header's ``OpenSquilla ·`` occurrence and require one
        # remaining wordmark in the transcript.
        plain_logo_locations = [
            (row_index, match.start())
            for row_index, row in enumerate(rows[: geometry.footer_top])
            for match in re.finditer("OpenSquilla", row)
            if not row[match.end() :].startswith(" ·")
        ]
        if len(plain_logo_locations) != 1:
            violations.append(
                "welcome-logo: expected exactly one plain welcome logo, "
                f"found {plain_logo_locations}"
            )

    return tuple(violations)


def _consecutive_row_groups(rows: list[int]) -> list[list[int]]:
    groups: list[list[int]] = []
    for row in rows:
        if groups and row == groups[-1][-1] + 1:
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


def _prompt_surface_cells(
    frame: StyledFramebuffer,
    *,
    footer_top: int,
    content_width: int,
) -> frozenset[tuple[int, int]]:
    """Return cells owned by a complete, semantically identified Prompt block.

    Prompt blocks intentionally share the footer surface colour, but only in a
    strict current-width rectangle: one app-background gutter on either side,
    a left rail at column 1, and the explicit ``you`` role label on the first
    row.  Matching the semantic head and the exact rectangle keeps this
    exception narrow: arbitrary surface-colour residue and the staircase
    corruption this gate was built for remain blocking violations.

    Multi-line prompts render continuation rows immediately below the role row
    with the same exact surface geometry, so they are admitted only while that
    contiguous rectangle continues.
    """

    if content_width < 4 or footer_top <= 0:
        return frozenset()

    surface_start = 1
    # OpenTUI reserves the transcript's last content column for its scrollbar
    # and leaves one app-background gutter between that track and a full-width
    # child.  Without an active scrollbar only the normal one-cell gutter is
    # present.  Derive the exact current geometry from the styled framebuffer
    # rather than accepting either prompt width indiscriminately.
    has_scrollbar_track = any(
        frame.cells[row_index][content_width - 1].background
        == SCROLLBAR_TRACK_BACKGROUND
        for row_index in range(footer_top)
    )
    surface_end = content_width - (2 if has_scrollbar_track else 1)

    def is_complete_surface_row(row_index: int) -> bool:
        row = frame.cells[row_index]
        return (
            row[surface_start].glyph == "│"
            and row[0].background == APP_BACKGROUND
            and all(
                cell.background == PROMPT_BACKGROUND
                for cell in row[surface_start:surface_end]
            )
        )

    owned: set[tuple[int, int]] = set()
    row_index = 0
    while row_index < footer_top:
        row_text = frame.row_text(row_index)
        is_prompt_head = (
            row_text.startswith(" │ you  ")
            and is_complete_surface_row(row_index)
        )
        if not is_prompt_head:
            row_index += 1
            continue

        while row_index < footer_top and is_complete_surface_row(row_index):
            owned.update(
                (row_index, col_index)
                for col_index in range(surface_start, surface_end)
            )
            row_index += 1

    return frozenset(owned)


def assert_opentui_framebuffer(
    frame: StyledFramebuffer,
    *,
    required_rail_headings: tuple[str, ...] = ("AGENT", "RUNTIME"),
    cursor: tuple[int, int] | None = None,
) -> None:
    violations = opentui_framebuffer_violations(
        frame,
        required_rail_headings=required_rail_headings,
    )
    if violations:
        rendered = "\n".join(f"- {violation}" for violation in violations)
        raise AssertionError(
            f"{frame.checkpoint}: invalid styled terminal framebuffer:\n{rendered}\n\n"
            f"Visible cells:\n{frame.text}"
        )
    if cursor is not None:
        cursor_x, cursor_y = cursor
        geometry = frame.geometry
        inside_composer = (
            1 < cursor_x < geometry.content_width - 2
            and geometry.composer_top < cursor_y < geometry.composer_bottom
        )
        if not inside_composer:
            raise AssertionError(
                f"{frame.checkpoint}: hardware cursor {cursor} is outside the "
                "composer content rectangle "
                f"x=2..{geometry.content_width - 3}, "
                f"y={geometry.composer_body_top}..{geometry.composer_bottom - 1}\n\n"
                f"Visible cells:\n{frame.text}"
            )


def _apply_sgr_background(current: str, parameters: str) -> str:
    if not parameters:
        return DEFAULT_BACKGROUND
    groups = parameters.split(";")
    index = 0
    while index < len(groups):
        group = groups[index]
        if ":" in group:
            current = _apply_colon_sgr_background(current, group)
            index += 1
            continue
        try:
            code = int(group or "0")
        except ValueError as exc:  # pragma: no cover - guarded by the caller
            raise FramebufferParseError(f"invalid SGR parameter {group!r}") from exc
        if code in {0, 49}:
            current = DEFAULT_BACKGROUND
            index += 1
            continue
        if code in {38, 48}:
            if index + 1 >= len(groups):
                raise FramebufferParseError(f"incomplete extended SGR {parameters!r}")
            mode = int(groups[index + 1] or "0")
            if mode == 2:
                if index + 4 >= len(groups):
                    raise FramebufferParseError(f"incomplete RGB SGR {parameters!r}")
                red, green, blue = (int(groups[index + offset]) for offset in (2, 3, 4))
                if code == 48:
                    current = f"rgb:{red},{green},{blue}"
                index += 5
                continue
            if mode == 5:
                if index + 2 >= len(groups):
                    raise FramebufferParseError(f"incomplete indexed SGR {parameters!r}")
                if code == 48:
                    current = f"index:{int(groups[index + 2])}"
                index += 3
                continue
            raise FramebufferParseError(f"unsupported extended SGR {parameters!r}")
        if 40 <= code <= 47:
            current = f"ansi:{code - 40}"
        elif 100 <= code <= 107:
            current = f"ansi:{code - 92}"
        index += 1
    return current


def _apply_colon_sgr_background(current: str, group: str) -> str:
    values = group.split(":")
    try:
        code = int(values[0] or "0")
    except ValueError as exc:
        raise FramebufferParseError(f"invalid colon SGR {group!r}") from exc
    if code not in {38, 48}:
        return DEFAULT_BACKGROUND if code in {0, 49} else current
    if len(values) < 3:
        raise FramebufferParseError(f"incomplete colon SGR {group!r}")
    mode = int(values[1] or "0")
    payload = [value for value in values[2:] if value != ""]
    if mode == 2 and len(payload) >= 3:
        if code == 48:
            red, green, blue = (int(value) for value in payload[-3:])
            return f"rgb:{red},{green},{blue}"
        return current
    if mode == 5 and payload:
        return f"index:{int(payload[-1])}" if code == 48 else current
    raise FramebufferParseError(f"unsupported colon SGR {group!r}")


def _last_primary_cell(cells: list[FramebufferCell]) -> int | None:
    for index in range(len(cells) - 1, -1, -1):
        if not cells[index].continuation:
            return index
    return None


def _background_runs(row: tuple[FramebufferCell, ...]) -> list[dict[str, Any]]:
    if not row:
        return []
    runs: list[dict[str, Any]] = []
    start = 0
    background = row[0].background
    for index, cell in enumerate(row[1:], start=1):
        if cell.background == background:
            continue
        runs.append({"start": start, "end": index, "background": background})
        start = index
        background = cell.background
    runs.append({"start": start, "end": len(row), "background": background})
    return runs
