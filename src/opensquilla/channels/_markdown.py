"""CommonMark to Telegram-HTML rendering for the Telegram adapter.

Telegram's parse_mode=HTML accepts a narrow entity subset: bold, italic,
strikethrough, inline code, preformatted blocks, links and blockquotes.
Forwarding raw MarkdownV2 instead fails for ordinary agent output, because
MarkdownV2 escapes punctuation strictly while CommonMark does not.

This module renders the CommonMark constructs agent replies actually use
into exactly that subset. Unsupported constructs degrade to readable plain
text rather than being rejected by the API.
"""

from __future__ import annotations

import html
import re

# Inline constructs: (opener, closer, html tag). Longer openers are matched
# first so the asterisks inside ** never masquerade as a lone * delimiter.
_INLINE_DELIMITERS: tuple[tuple[str, str, str], ...] = (
    ("**", "**", "b"),
    ("__", "__", "b"),
    ("~~", "~~", "s"),
    ("*", "*", "i"),
    ("_", "_", "i"),
    ("`", "`", "code"),
)

# Link: [label](url). The URL must not contain spaces or closing parens.
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
# Heading: 1-6 hash markers followed by a space.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# Blockquote body: a leading > and an optional space.
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
# Unordered list item: - , * or + followed by a space.
_UNORDERED_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
# Ordered list item: 1. or 1) followed by a space.
_ORDERED_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
# Fenced code block opener: ``` or ```lang.
_FENCE_RE = re.compile(r"^```(\S*)")


def _is_word_char(char: str) -> bool:
    """True for characters CommonMark treats as word content."""
    return char.isalnum() or char == "_"


def _flanking(text: str, start: int, end: int, closer: str) -> bool:
    """True when an underscore delimiter sits outside a word.

    CommonMark disables intraword underscore emphasis (snake_case must stay
    literal), unlike asterisks which are allowed inside words. The char
    after the closer must be the first char past the full delimiter token:
    for __ the token is two chars, so end+1 would read the second underscore.
    """
    before = text[start - 1] if start > 0 else " "
    after_pos = end + len(closer)
    after = text[after_pos] if after_pos < len(text) else " "
    return not _is_word_char(before) and not _is_word_char(after)


def _match_delimiter(text: str, pos: int) -> tuple[str, str, str] | None:
    """Longest delimiter token starting at pos, or None."""
    for opener, closer, tag in _INLINE_DELIMITERS:
        if text.startswith(opener, pos):
            return opener, closer, tag
    return None


def _find_closer(text: str, start: int, opener: str) -> int:
    """Index of the delimiter matching opener, skipping longer tokens.

    Skipping longer tokens lets *a **b** c* nest correctly: the asterisks
    belonging to ** do not close the outer * span.
    """
    i = start
    while i < len(text):
        token = _match_delimiter(text, i)
        if token is None:
            i += 1
            continue
        token_opener, _token_closer, _token_tag = token
        if token_opener == opener:
            return i
        i += len(token_opener)
    return -1


def _escape_text(text: str) -> str:
    """Escape a literal text run for Telegram's HTML parser."""
    return html.escape(text, quote=False)


def _render_span(text: str) -> str:
    """Convert inline CommonMark on raw text to Telegram HTML entities."""
    out: list[str] = []
    i = 0
    while i < len(text):
        token = _match_delimiter(text, i)
        if token is None:
            link = _LINK_RE.search(text, i)
            if link is None or link.start() != i:
                out.append(_escape_text(text[i]))
                i += 1
                continue
            label = _render_span(link.group(1))
            url = html.escape(link.group(2), quote=True)
            out.append(f'<a href="{url}">{label}</a>')
            i = link.end()
            continue
        opener, closer, tag = token
        end = _find_closer(text, i + len(opener), opener)
        if end < 0:
            out.append(_escape_text(text[i]))
            i += 1
            continue
        if opener in ("_", "__") and not _flanking(text, i, end, closer):
            out.append(_escape_text(text[i]))
            i += 1
            continue
        inner = text[i + len(opener) : end]
        # Code spans render literally: markers inside them are not markup.
        rendered = _escape_text(inner) if tag == "code" else _render_span(inner)
        out.append(f"<{tag}>{rendered}</{tag}>")
        i = end + len(closer)
    return "".join(out)


def _render_inline(text: str) -> str:
    """Render inline CommonMark to Telegram HTML, escaping text safely."""
    return _render_span(text)


def _render_code_block(lang: str, body: str) -> str:
    """Render a fenced code block as a Telegram pre/code pair."""
    escaped = html.escape(body.strip("\n"), quote=False)
    if lang:
        language = html.escape(lang, quote=True)
        return f'<pre><code class="language-{language}">{escaped}</code></pre>'
    return f"<pre><code>{escaped}</code></pre>"


def markdown_to_telegram_html(content: str) -> str:
    """Render CommonMark content to Telegram's HTML entity subset.

    Block constructs (fences, headings, blockquotes, lists) are converted
    line by line; inline constructs are handled by _render_span. Newlines
    are preserved as literal line breaks, which Telegram renders natively.
    """
    lines = content.split("\n")
    rendered: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE_RE.match(line)
        if fence is not None:
            lang = fence.group(1)
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                body.append(lines[i])
                i += 1
            if i < len(lines):  # consume the closing fence
                i += 1
            rendered.append(_render_code_block(lang, "\n".join(body)))
            continue
        heading = _HEADING_RE.match(line)
        if heading is not None:
            # Telegram HTML rejects nesting the same tag (<b><b>...). A
            # heading is rendered as its inline content without a wrapping
            # <b> so an inner bold/italic never nests into a rejected form.
            rendered.append(_render_inline(heading.group(2)))
            i += 1
            continue
        if line.startswith(">"):
            quote: list[str] = []
            while i < len(lines) and lines[i].startswith(">"):
                match = _QUOTE_RE.match(lines[i])
                assert match is not None
                quote.append(match.group(1))
                i += 1
            rendered.append(f"<blockquote>{_render_inline('\n'.join(quote))}</blockquote>")
            continue
        unordered = _UNORDERED_RE.match(line)
        if unordered is not None:
            indent, item = unordered.group(1), unordered.group(2)
            rendered.append(f"{indent}\u2022 {_render_inline(item)}")
            i += 1
            continue
        ordered = _ORDERED_RE.match(line)
        if ordered is not None:
            indent, number, item = ordered.group(1), ordered.group(2), ordered.group(3)
            rendered.append(f"{indent}{number}. {_render_inline(item)}")
            i += 1
            continue
        rendered.append(_render_inline(line))
        i += 1
    return "\n".join(rendered)


def _slack_escape_url(text: str) -> str:
    """Escape URL/label text for Slack mrkdwn angle-bracket links."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_inline(text: str) -> str:
    """Convert inline CommonMark to Slack mrkdwn on raw text.

    mrkdwn differs from CommonMark: bold is *text* (single asterisk),
    italic is _text_, links are <url|text> and strikethrough is ~text~.
    Delimiter scanning (not regex) keeps ** and * from clobbering each
    other, and inline code passes through untouched.
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        link = _LINK_RE.search(text, i)
        if link is not None and link.start() == i:
            label = _slack_inline(link.group(1))
            url = _slack_escape_url(link.group(2))
            out.append(f"<{url}|{label}>")
            i = link.end()
            continue
        if text.startswith("**", i):
            end = _find_closer(text, i + 2, "**")
            if end >= 0:
                out.append("*" + _slack_inline(text[i + 2 : end]) + "*")
                i = end + 2
                continue
        if text.startswith("~~", i):
            end = _find_closer(text, i + 2, "~~")
            if end >= 0:
                out.append("~" + _slack_inline(text[i + 2 : end]) + "~")
                i = end + 2
                continue
        if text.startswith("`", i):
            end = text.find("`", i + 1)
            if end >= 0:
                out.append(text[i : end + 1])
                i = end + 1
                continue
        if text.startswith("*", i):
            # A ** run with no matching closer must stay literal; its second
            # asterisk would otherwise pair as an empty italic span.
            if text.startswith("**", i):
                out.append("*")
                i += 1
                continue
            end = _find_closer(text, i + 1, "*")
            if end >= 0:
                out.append("_" + _slack_inline(text[i + 1 : end]) + "_")
                i = end + 1
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def markdown_to_slack_mrkdwn(content: str) -> str:
    """Convert CommonMark content to Slack mrkdwn.

    Slack's native syntax is mrkdwn, not CommonMark: **bold** and
    [label](url) would render literally. Fenced code blocks pass through
    unchanged; headings become bold lines, list bullets become bullet
    characters, and inline constructs are converted by _slack_inline.
    """
    lines = content.split("\n")
    rendered: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE_RE.match(line)
        if fence is not None:
            start = i
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                i += 1
            if i < len(lines):
                i += 1
            rendered.append("\n".join(lines[start:i]))
            continue
        heading = _HEADING_RE.match(line)
        if heading is not None:
            inner = _slack_inline(heading.group(2))
            # Avoid ** collisions when the heading body itself renders bold.
            rendered.append(f"*{inner}*" if "*" not in inner else inner)
            i += 1
            continue
        if line.startswith(">"):
            quote: list[str] = []
            while i < len(lines) and lines[i].startswith(">"):
                match = _QUOTE_RE.match(lines[i])
                assert match is not None
                quote.append(match.group(1))
                i += 1
            rendered.append("> " + _slack_inline(" ".join(quote)))
            continue
        unordered = _UNORDERED_RE.match(line)
        if unordered is not None:
            rendered.append("\u2022 " + _slack_inline(unordered.group(2)))
            i += 1
            continue
        ordered = _ORDERED_RE.match(line)
        if ordered is not None:
            rendered.append(f"{ordered.group(2)}. {_slack_inline(ordered.group(3))}")
            i += 1
            continue
        rendered.append(_slack_inline(line))
        i += 1
    return "\n".join(rendered)
