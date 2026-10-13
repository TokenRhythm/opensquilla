from __future__ import annotations

import json

import pytest

from opensquilla.provider.tool_input_preview import (
    PREVIEW_CONTENT_LIMIT,
    FileInputDrafts,
    PartialFileInput,
    preview_file_inputs,
)
from opensquilla.provider.types import (
    DoneEvent,
    ErrorEvent,
    ToolInputPreviewEvent,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)


@pytest.mark.parametrize("fragment_size", [1, 3, 17, 4096])
@pytest.mark.parametrize("content_first", [False, True])
def test_partial_file_input_preserves_real_content_and_top_level_fields(
    fragment_size: int, content_first: bool,
) -> None:
    content = 'line 1\n"path":"fake.txt"\n嵌套\\n😀 \\ end'
    payload = {"path": "实际.txt", "content": content}
    if content_first:
        payload = {"content": content, "nested": {"path": "fake"}, "path": "实际.txt"}
    source = json.dumps(payload, ensure_ascii=True)
    parser = PartialFileInput()
    for offset in range(0, len(source), fragment_size):
        parser.feed(source[offset:offset + fragment_size])
        parser.content.encode("utf-8")  # No dangling surrogate can reach the wire.
    assert parser.path_complete
    assert parser.path == "实际.txt"
    assert parser.content == content


def test_partial_file_input_waits_for_complete_path_and_escape() -> None:
    parser = PartialFileInput()
    parser.feed('{"path":"a')
    assert not parser.path_complete
    parser.feed('.txt","content":"first\\uD83D')
    assert parser.path_complete
    assert parser.content == "first"
    parser.feed('\\uDE00\\')
    assert parser.content == "first😀"
    parser.feed('nlast"}')
    assert parser.content == "first😀\nlast"


def test_partial_file_input_duplicate_fields_replace_and_memory_is_bounded() -> None:
    parser = PartialFileInput()
    parser.feed('{"path":"old","content":"old","path":"new","content":"')
    parser.feed("x" * (PREVIEW_CONTENT_LIMIT * 3) + "tail")
    assert parser.path == "new"
    assert len(parser.content) == PREVIEW_CONTENT_LIMIT
    assert parser.content.endswith("tail")
    assert parser.content_chars == PREVIEW_CONTENT_LIMIT * 3 + 4


def test_drafts_publish_first_content_then_throttle_and_flush_tail() -> None:
    clock = [0.0]
    drafts = FileInputDrafts(clock=lambda: clock[0])
    drafts.observe(ToolUseStartEvent(tool_use_id="one", tool_name="write_file"))
    assert not drafts.observe(ToolUseDeltaEvent(tool_use_id="one", json_fragment='{"path":"a'))
    first = drafts.observe(ToolUseDeltaEvent(
        tool_use_id="one", json_fragment='.txt","content":"first',
    ))[0]
    assert first.content == "first" and first.revision == 1
    assert first.path == "a.txt"
    clock[0] = 0.5
    assert not drafts.observe(ToolUseDeltaEvent(tool_use_id="one", json_fragment=" second"))
    clock[0] = 1.0
    second = drafts.observe(ToolUseDeltaEvent(tool_use_id="one", json_fragment=" third"))[0]
    assert second.content == "first second third"
    assert second.preview_id == first.preview_id and second.revision == 2
    assert not drafts.observe(ToolUseDeltaEvent(tool_use_id="one", json_fragment=' last"}'))
    tail = drafts.observe(ToolUseEndEvent(
        tool_use_id="one", tool_name="write_file", arguments={},
    ))[0]
    assert tail.content.endswith(" last") and tail.revision == 3
    discarded = drafts.observe(ErrorEvent(code="400"))[0]
    assert discarded.state == "discarded" and discarded.content == ""
    assert discarded.revision == 4


def test_drafts_limit_tools_and_ignore_unrelated_arguments() -> None:
    drafts = FileInputDrafts()
    previews = []
    for index in range(100):
        tool_id = str(index)
        drafts.observe(ToolUseStartEvent(tool_use_id=tool_id, tool_name="write_file"))
        previews += drafts.observe(ToolUseDeltaEvent(
            tool_use_id=tool_id,
            json_fragment='{"path":"a.txt","content":"data","credential":"never"}',
        ))
    assert len(previews) == 8
    assert len(drafts._seen) <= 64
    assert "never" not in repr(previews)
    other = FileInputDrafts()
    other.observe(ToolUseStartEvent(tool_use_id="exec", tool_name="execute"))
    assert not other.observe(ToolUseDeltaEvent(
        tool_use_id="exec", json_fragment='{"path":"x","content":"no"}',
    ))


def test_completed_arguments_supply_a_missing_streamed_suffix() -> None:
    drafts = FileInputDrafts()
    drafts.observe(ToolUseStartEvent(tool_use_id="file", tool_name="write_file"))
    first = drafts.observe(ToolUseDeltaEvent(
        tool_use_id="file", json_fragment='{"path":"a.txt","content":"prefix',
    ))[0]
    last = drafts.observe(ToolUseEndEvent(
        tool_use_id="file", tool_name="write_file",
        arguments={"path": "a.txt", "content": "prefix and final suffix"},
    ))[0]
    assert last.preview_id == first.preview_id
    assert last.content == "prefix and final suffix"
    assert last.state == "generating"


@pytest.mark.asyncio
async def test_nested_observers_do_not_duplicate_preview_and_failure_discards() -> None:
    async def source():
        yield ToolUseStartEvent(tool_use_id="file", tool_name="write_file")
        yield ToolUseDeltaEvent(tool_use_id="file", json_fragment='{"path":"a","content":"x')
        yield ErrorEvent(code="400")

    events = [event async for event in preview_file_inputs(preview_file_inputs(source()))]
    previews = [event for event in events if isinstance(event, ToolInputPreviewEvent)]
    assert [event.state for event in previews] == ["generating", "discarded"]
    assert previews[0].preview_id == previews[1].preview_id
    assert isinstance(events[-1], ErrorEvent)
    assert not any(isinstance(event, DoneEvent) for event in events)
