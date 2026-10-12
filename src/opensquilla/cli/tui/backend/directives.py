"""Streaming filter for inline chat-routing directive tags.

Models embed delivery directives like ``[[reply_to_current]]`` or
``[[reply_to: <target>]]`` in their replies so channel adapters can route the
message. The channel layer strips them before delivery; terminal surfaces
render the raw stream, so without this filter the tag leaks into the
transcript as literal text (and markdown styles the bracketed form as a
link). The filter mirrors the channel-side semantics — the tag and its
trailing whitespace disappear — while tolerating tags split across stream
deltas by holding back an ambiguous tail until it either completes into a
tag or proves to be ordinary text.
"""

from __future__ import annotations

import re

_TAG_BODY = r"(?:reply_to_current|reply_to\s*:\s*[^\]\n]*)"
# Single- and double-bracket spellings both occur in the wild; the channel
# regex accepts only ``[[..]]`` but a model that drops one bracket pair still
# means the directive, so the terminal strips both. Brackets must pair up
# exactly, and trailing spaces plus one newline go with the tag, like the
# channel-side strip.
_DIRECTIVE_TAG_RE = re.compile(
    rf"(?:\[\[\s*{_TAG_BODY}\s*\]\]|(?<!\[)\[\s*{_TAG_BODY}\s*\](?!\]))[ \t]*\r?\n?"
)
# The longest holdback we tolerate: a directive with a generous target. A
# "tag" that grows past this without closing is ordinary text.
_MAX_HOLDBACK = 96


def _possible_tag_start(tail: str) -> int:
    """Index in ``tail`` where a not-yet-complete directive tag may begin.

    Returns -1 when the tail cannot grow into a directive tag. Only the
    rightmost bracket run needs checking: completed tags were already
    stripped, so anything left of it is settled text.
    """
    bracket = tail.rfind("[")
    if bracket < 0:
        return -1
    # ``[[`` spelling: the run starts one earlier.
    if bracket > 0 and tail[bracket - 1] == "[":
        bracket -= 1
    candidate = tail[bracket:]
    if len(candidate) > _MAX_HOLDBACK or "\n" in candidate:
        return -1
    if candidate.startswith("[["):
        # Waiting on "]]": a lone "]" may be its first half, but a "]" followed
        # by anything else can no longer close the pair — ordinary text.
        if re.search(r"\][^\]]", candidate) or "]]" in candidate:
            return -1
    elif "]" in candidate:
        return -1
    body = candidate.lstrip("[").lstrip().rstrip("]")
    if body == "" or "reply_to_current".startswith(body) or body.startswith("reply_to"):
        return bracket
    return -1


class StreamDirectiveFilter:
    """Strip directive tags from a streamed text flow, delta by delta.

    ``feed`` returns the text that is safe to display; ``flush`` releases any
    held tail when the stream segment ends (block close / turn finalize).
    """

    def __init__(self) -> None:
        self._pending = ""
        # True while the whitespace that trails a just-stripped tag may still
        # be arriving in later deltas (the same-buffer case is handled by the
        # regex itself). Cleared by the first non-whitespace or the newline.
        self._eat_trailing_ws = False

    def feed(self, delta: str) -> str:
        self._pending += delta
        if self._eat_trailing_ws:
            trimmed = re.sub(r"^[ \t]*\r?\n?", "", self._pending)
            if trimmed or re.search(r"\n", self._pending):
                self._eat_trailing_ws = False
            self._pending = trimmed
        stripped = _DIRECTIVE_TAG_RE.sub("", self._pending)
        if stripped != self._pending:
            match = None
            for match in _DIRECTIVE_TAG_RE.finditer(self._pending):
                pass
            self._eat_trailing_ws = (
                match is not None
                and match.end() == len(self._pending)
                and not match.group().endswith("\n")
            )
            self._pending = stripped
        hold_at = _possible_tag_start(self._pending)
        if hold_at < 0:
            visible = self._pending
            self._pending = ""
            return visible
        visible = self._pending[:hold_at]
        self._pending = self._pending[hold_at:]
        return visible

    def flush(self) -> str:
        """Release the held tail; an unfinished tag prefix is ordinary text."""
        visible = self._pending
        self._pending = ""
        return visible
