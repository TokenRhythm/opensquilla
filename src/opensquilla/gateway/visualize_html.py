"""Small preflight for generated HTML; this is not browser or semantic validation."""

from __future__ import annotations

from html.parser import HTMLParser

from opensquilla.tools.types import RetryableToolInputError


class _RawTextCheck(HTMLParser):
    """Catch truncated scripts/styles without imposing an HTML component catalog."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.unclosed: tuple[str, int, int] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            line, column = self.getpos()
            self.unclosed = (tag, line, column + 1)

    def handle_endtag(self, tag: str) -> None:
        if self.unclosed is not None and tag == self.unclosed[0]:
            self.unclosed = None


def validate_visualize_html(payload: bytes) -> None:
    """Reject common correctable input damage before publishing immutable bytes.

    Fragments, complete documents and arbitrary SVG/Canvas/DOM content are
    allowed. Browser execution, CSP, layout and calculations are not checked.
    """
    try:
        html = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RetryableToolInputError(
            "VISUALIZE_ENCODING: Save the HTML as UTF-8 and publish again."
        ) from exc
    if not html.strip("\ufeff \t\r\n") or "\x00" in html:
        raise RetryableToolInputError(
            "VISUALIZE_HTML: Provide nonempty HTML without NUL bytes and publish again."
        )
    if html.lstrip("\ufeff \t\r\n").startswith("```"):
        raise RetryableToolInputError(
            "VISUALIZE_HTML: Save the HTML itself, without enclosing Markdown fences, "
            "and publish again."
        )
    parser = _RawTextCheck()
    parser.feed(html)
    parser.close()
    if parser.unclosed is not None:
        tag, line, column = parser.unclosed
        raise RetryableToolInputError(
            f"VISUALIZE_HTML: <{tag}> at line {line}, column {column} is missing "
            f"</{tag}>. Complete the file and publish again."
        )
