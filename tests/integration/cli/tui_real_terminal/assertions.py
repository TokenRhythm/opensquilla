from __future__ import annotations

import re

from tui_real_terminal.driver import TerminalFrame

_ANSI_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-?]*[ -/]*[@-~]"
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|P[^\x1b]*\x1b\\"
    r"|[@-Z\\-_]"
    r")"
)
_PROMPT_PLACEHOLDERS = ("send a message", "send a massage")
_COMPLETION_TITLES = ("commands", "files")
_COMPLETION_BACKGROUND_MARKERS = (
    "OPEN_SQUILLA_TUI_READY",
    "fake-response:",
    "stream-token-",
    "intermediate-before-tool",
    "second-intermediate-line",
    "complex-state-complete",
    "terminal-change-response",
    "architecture-analysis-complete",
    "fake_tool",
    "approval requested",
    "Traceback (most recent call last)",
)


def assert_visible_text(frame: TerminalFrame, expected: str) -> None:
    if expected not in frame.text:
        raise AssertionError(
            f"{frame.checkpoint}: expected visible text {expected!r}; screen was:\n"
            f"{frame.text}"
        )


def assert_prompt_ready(frame: TerminalFrame) -> None:
    if "you" not in frame.text and not any(
        placeholder in frame.text for placeholder in _PROMPT_PLACEHOLDERS
    ):
        raise AssertionError(f"{frame.checkpoint}: prompt is not visibly ready:\n{frame.text}")


def assert_no_inline_prompt_chrome_collision(frame: TerminalFrame) -> None:
    for line in frame.text.splitlines():
        if re.match(r"^\s*│\s+s(?:[#|*⠋✓✗]|[\u4e00-\u9fff])", line):
            raise AssertionError(
                f"{frame.checkpoint}: inline prompt chrome overlapped output:\n{frame.text}"
            )
        if (
            sum(line.count(placeholder) for placeholder in _PROMPT_PLACEHOLDERS) > 1
        ):
            raise AssertionError(
                f"{frame.checkpoint}: inline prompt chrome was duplicated:\n{frame.text}"
            )


def assert_no_duplicate_fixed_chrome(frame: TerminalFrame) -> None:
    """Reject old and new fixed-layout surfaces being visible together.

    A viewport remount or wide/narrow transition may move these regions, but a
    settled framebuffer can contain only one current instance. This catches the
    duplicated router/footer and side-rail labels seen in embedded terminals.
    """

    prompt_count = sum(frame.text.count(placeholder) for placeholder in _PROMPT_PLACEHOLDERS)
    if prompt_count > 1:
        raise AssertionError(
            f"{frame.checkpoint}: duplicated composer after viewport recovery:\n"
            f"{frame.text}"
        )

    patterns = {
        "router strip": re.compile(r"^\s*router\s+·\s+model\b"),
        "context AGENT label": re.compile(r"^.*│\s*AGENT\s*$"),
        "context RUNTIME label": re.compile(r"^.*│\s*RUNTIME\s*$"),
    }
    lines = frame.text.splitlines()
    for label, pattern in patterns.items():
        count = sum(bool(pattern.search(line)) for line in lines)
        if count > 1:
            raise AssertionError(
                f"{frame.checkpoint}: duplicated {label} after viewport recovery:\n"
                f"{frame.text}"
            )


def assert_no_completion_menu_overlap(frame: TerminalFrame) -> None:
    """Reject completion-menu rectangles whose border area is polluted.

    The OpenTUI completion menu is a rounded rectangle with a ``commands`` or
    ``files`` title; clean top/bottom border spans contain only border fill and
    that title, and the rectangle body must not contain known conversation text.
    """

    text = _ANSI_RE.sub("", frame.text)
    lines = _screen_lines(frame, text)
    for top_index, left, right in _completion_top_spans(lines):
        top_segment = _line_span(lines[top_index], left, right)
        if not _is_completion_border_segment(top_segment, top=True):
            _raise_completion_assertion(
                frame,
                "completion menu overlap: dirty top border",
            )

        bottom = _find_completion_bottom(lines, top_index, left, right)
        if bottom is None:
            _raise_completion_assertion(
                frame,
                "completion menu overlap: incomplete menu border",
            )
        bottom_index, body_right = bottom

        bottom_segment = _line_span(lines[bottom_index], left, body_right)
        if not _is_completion_border_segment(bottom_segment, top=False):
            _raise_completion_assertion(
                frame,
                "completion menu overlap: dirty bottom border",
            )

        for body_index in range(top_index + 1, bottom_index):
            line = lines[body_index]
            segment = _line_span(line, left, body_right)
            if any(marker in segment for marker in _COMPLETION_BACKGROUND_MARKERS):
                _raise_completion_assertion(
                    frame,
                    "completion menu overlap: conversation text inside menu",
                )
            if (
                not _has_completion_vertical_edges(line, left, body_right)
                and segment.strip()
            ):
                _raise_completion_assertion(
                    frame,
                    "completion menu overlap: broken vertical border",
                )


def assert_no_stale_completion_menu(frame: TerminalFrame) -> None:
    """Reject completion-menu remnants after a close or clear operation.

    A cleared frame should not contain a ``commands``/``files`` rounded title
    border, nor a selected completion row left behind with rounded menu chrome.
    """

    text = _ANSI_RE.sub("", frame.text)
    lines = _screen_lines(frame, text)
    if list(_completion_top_spans(lines)):
        raise AssertionError(f"{frame.checkpoint}: stale completion menu:\n{frame.text}")
    has_rounded_chrome = any(char in text for char in "╭╮╰╯")
    for line in lines:
        if has_rounded_chrome and "› " in line and "│" in line:
            raise AssertionError(f"{frame.checkpoint}: stale completion menu:\n{frame.text}")


def assert_no_traceback(frame: TerminalFrame) -> None:
    forbidden = (
        "Traceback (most recent call last)",
        "RuntimeError:",
        "Unhandled exception",
    )
    for marker in forbidden:
        if marker in frame.text:
            raise AssertionError(f"{frame.checkpoint}: unexpected error marker {marker!r}")


def assert_no_raw_ansi_leakage(frame: TerminalFrame) -> None:
    match = _ANSI_RE.search(frame.text)
    if match:
        raise AssertionError(
            f"{frame.checkpoint}: raw terminal escape leaked at offset {match.start()}"
        )


def _completion_top_spans(lines: list[str]) -> list[tuple[int, int, int]]:
    spans: list[tuple[int, int, int]] = []
    for index, line in enumerate(lines):
        search_at = 0
        while True:
            left = line.find("╭", search_at)
            if left < 0:
                break
            right = line.find("╮", left + 1)
            if right < 0:
                break
            segment = line[left : right + 1]
            if _has_completion_title(segment):
                spans.append((index, left, right))
            search_at = right + 1
    return spans


def _screen_lines(frame: TerminalFrame, text: str) -> list[str]:
    lines: list[str] = []
    cols = frame.size.cols
    for line in text.splitlines():
        if cols > 0 and len(line) > cols:
            lines.extend(
                line[index : index + cols] for index in range(0, len(line), cols)
            )
        else:
            lines.append(line)
    return lines


def _find_completion_bottom(
    lines: list[str],
    top_index: int,
    left: int,
    right: int,
) -> tuple[int, int] | None:
    for index in range(top_index + 1, len(lines)):
        line = lines[index]
        if len(line) <= right:
            continue
        bottom_right = line.find("╯", left + 1)
        if line[left] == "╰" and bottom_right >= right:
            return index, bottom_right
    return None


def _line_span(line: str, left: int, right: int) -> str:
    if len(line) <= left:
        return ""
    return line[left : min(len(line), right + 1)]


def _has_completion_title(segment: str) -> bool:
    return any(f" {title} " in segment for title in _COMPLETION_TITLES)


def _is_completion_border_segment(segment: str, *, top: bool) -> bool:
    if len(segment) < 2:
        return False
    expected_left, expected_right = ("╭", "╮") if top else ("╰", "╯")
    if segment[0] != expected_left or segment[-1] != expected_right:
        return False
    inner = segment[1:-1]
    for title in _COMPLETION_TITLES:
        inner = inner.replace(title, "")
    return all(char in {" ", "─"} for char in inner)


def _has_completion_vertical_edges(line: str, left: int, right: int) -> bool:
    return len(line) > right and line[left] == "│" and line[right] == "│"


def _raise_completion_assertion(frame: TerminalFrame, reason: str) -> None:
    raise AssertionError(f"{frame.checkpoint}: {reason}:\n{frame.text}")
