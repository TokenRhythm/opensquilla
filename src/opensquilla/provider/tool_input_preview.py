"""Bounded, revocable file drafts, separate from transactional tool events."""

from __future__ import annotations

import json
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from typing import Any

from .types import (
    DoneEvent,
    ErrorEvent,
    ProviderGenerationResetEvent,
    ToolInputPreviewEvent,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)

PREVIEW_CONTENT_LIMIT = 65_536
PREVIEW_PATH_LIMIT = 4_096
PREVIEW_INTERVAL_SECONDS = 1.0
PREVIEW_ACTIVE_TOOL_LIMIT = 8
PREVIEW_SEEN_TOOL_LIMIT = 64


class PartialFileInput:
    """Read only top-level string path/content using a streaming JSON lexer.

    This is a display projection, not JSON validation or executable arguments.
    The provider's completed argument object remains the execution authority.
    """

    def __init__(self) -> None:
        self.path = ""
        self.content = ""
        self.content_chars = 0
        self.path_complete = False
        self._depth = 0
        self._expect_key = False
        self._key = ""
        self._string = False
        self._role = "skip"
        self._token = ""
        self._token_overflow = False
        self._escape: str | None = None
        self._high_surrogate = ""

    def feed(self, fragment: str) -> None:
        appended: deque[str] = deque(maxlen=PREVIEW_CONTENT_LIMIT)

        def decoded(value: str) -> None:
            for char in value:
                code = ord(char)
                if self._high_surrogate:
                    if 0xDC00 <= code <= 0xDFFF:
                        char = chr(
                            0x10000 + ((ord(self._high_surrogate) - 0xD800) << 10)
                            + code - 0xDC00
                        )
                    # Invalid standalone surrogate stays out of the UI/UTF-8 wire.
                    self._high_surrogate = ""
                if 0xD800 <= code <= 0xDBFF:
                    self._high_surrogate = char
                    continue
                if 0xDC00 <= ord(char) <= 0xDFFF:
                    continue
                if self._role == "content":
                    appended.append(char)
                    self.content_chars += 1
                elif self._role in {"path", "key"}:
                    limit = PREVIEW_PATH_LIMIT if self._role == "path" else 128
                    if len(self._token) < limit:
                        self._token += char
                    else:
                        self._token_overflow = True

        for char in fragment:
            if self._string:
                if self._escape is not None:
                    self._escape += char
                    if self._escape.startswith("u") and len(self._escape) < 5:
                        continue
                    try:
                        decoded(json.loads('"\\' + self._escape + '"'))
                    except (ValueError, TypeError):
                        pass
                    self._escape = None
                elif char == "\\":
                    self._escape = ""
                elif char == '"':
                    self._string = False
                    self._high_surrogate = ""
                    if self._role == "key":
                        self._key = "" if self._token_overflow else self._token
                        self._expect_key = False
                    elif self._role == "path":
                        self.path_complete = not self._token_overflow
                        self.path = self._token if self.path_complete else ""
                else:
                    decoded(char)
                continue
            if char == '"':
                self._string = True
                self._token = ""
                self._token_overflow = False
                self._role = (
                    "key" if self._depth == 1 and self._expect_key
                    else self._key if self._depth == 1 and self._key in {"path", "content"}
                    else "skip"
                )
                if self._role == "path":
                    self.path_complete = False
                elif self._role == "content":
                    self.content = ""
                    appended.clear()
                    self.content_chars = 0
            elif char in "{[":
                self._depth += 1
                if self._depth == 1:
                    self._expect_key = char == "{"
            elif char in "}]":
                self._depth -= 1
            elif char == "," and self._depth == 1:
                self._expect_key = True
                self._key = ""
        if appended:
            self.content = (self.content + "".join(appended))[-PREVIEW_CONTENT_LIMIT:]


class FileInputDrafts:
    """Attempt-local observer. Snapshots can always be revoked before commit."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.attempt_id = uuid.uuid4().hex
        self._clock = clock
        self._inputs: dict[str, PartialFileInput] = {}
        self._previews: dict[str, ToolInputPreviewEvent] = {}
        self._published: dict[str, float] = {}
        self._seen: set[str] = set()
        self._delegated: set[str] = set()
        self._epochs: dict[str, int | None] = {}

    def observe(self, event: Any) -> list[ToolInputPreviewEvent]:
        if isinstance(event, ToolInputPreviewEvent):
            if len(self._delegated) < PREVIEW_SEEN_TOOL_LIMIT:
                self._delegated.add(event.tool_use_id)
            self._inputs.pop(event.tool_use_id, None)
            return []
        if isinstance(event, ToolUseStartEvent):
            tool_id = event.tool_use_id
            if not isinstance(tool_id, str) or not tool_id.strip() or len(tool_id) > 256:
                return []
            if tool_id in self._seen:
                return self.discard()
            if len(self._seen) >= PREVIEW_SEEN_TOOL_LIMIT:
                return []
            self._seen.add(tool_id)
            if (
                event.tool_name == "write_file" and tool_id not in self._delegated
                and len(self._previews.keys() | self._inputs.keys()) < PREVIEW_ACTIVE_TOOL_LIMIT
            ):
                self._inputs[tool_id] = PartialFileInput()
                self._epochs[tool_id] = getattr(event, "generation_epoch", None)
            return []
        if isinstance(event, ToolUseDeltaEvent):
            if not isinstance(event.tool_use_id, str):
                return []
            source = self._inputs.get(event.tool_use_id)
            if source is not None and isinstance(event.json_fragment, str):
                source.feed(event.json_fragment)
                return self._snapshot(event.tool_use_id)
        if isinstance(event, ToolUseEndEvent):
            if not isinstance(event.tool_use_id, str):
                return []
            source = self._inputs.get(event.tool_use_id)
            if source is not None and event.tool_name == "write_file":
                arguments = event.arguments
                if (
                    isinstance(arguments, dict)
                    and isinstance(arguments.get("path"), str)
                    and isinstance(arguments.get("content"), str)
                    and 0 < len(arguments["path"]) <= PREVIEW_PATH_LIMIT
                ):
                    # Providers may supply the final suffix only in the
                    # authoritative completed object. It is still a draft
                    # until Agent's transactional tool commit succeeds.
                    source.path = arguments["path"]
                    source.path_complete = True
                    source.content = arguments["content"][-PREVIEW_CONTENT_LIMIT:]
                    source.content_chars = len(arguments["content"])
            events = self._snapshot(event.tool_use_id, force=True)
            self._inputs.pop(event.tool_use_id, None)
            return events
        if isinstance(event, ProviderGenerationResetEvent):
            events = self.discard()
            self.attempt_id = uuid.uuid4().hex
            self._seen.clear()
            self._delegated.clear()
            self._epochs.clear()
            return events
        if isinstance(event, ErrorEvent):
            return self.discard()
        return []

    def _snapshot(self, tool_id: str, *, force: bool = False) -> list[ToolInputPreviewEvent]:
        source = self._inputs.get(tool_id)
        if (
            source is None or not source.path_complete
            or not source.path or not source.content_chars
        ):
            return []
        previous = self._previews.get(tool_id)
        now = self._clock()
        if previous is not None:
            if (previous.path, previous.content, previous.truncated) == (
                source.path, source.content, source.content_chars > PREVIEW_CONTENT_LIMIT,
            ):
                return []
            if not force and now - self._published[tool_id] < PREVIEW_INTERVAL_SECONDS:
                return []
        event = ToolInputPreviewEvent(
            preview_id=previous.preview_id if previous else uuid.uuid4().hex,
            attempt_id=self.attempt_id,
            generation_epoch=self._epochs.get(tool_id),
            tool_use_id=tool_id,
            revision=previous.revision + 1 if previous else 1,
            path=source.path,
            content=source.content,
            truncated=source.content_chars > PREVIEW_CONTENT_LIMIT,
        )
        self._previews[tool_id] = event
        self._published[tool_id] = now
        return [event]

    def discard(self) -> list[ToolInputPreviewEvent]:
        events = [
            replace(event, state="discarded", revision=event.revision + 1, content="")
            for event in self._previews.values()
        ]
        self._previews.clear()
        self._inputs.clear()
        return events


async def preview_file_inputs(stream: AsyncIterator[Any]) -> AsyncIterator[Any]:
    """Observe physical attempts without committing them or altering their results."""
    drafts = FileInputDrafts()
    completed = False
    try:
        async for event in stream:
            for preview in drafts.observe(event):
                yield preview
            yield event
            if isinstance(event, DoneEvent):
                completed = True
        if not completed:
            for preview in drafts.discard():
                yield preview
    except Exception:
        for preview in drafts.discard():
            yield preview
        raise
    finally:
        close = getattr(stream, "aclose", None)
        if callable(close):
            await close()
