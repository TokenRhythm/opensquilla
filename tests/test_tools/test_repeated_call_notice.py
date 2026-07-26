"""Repeated-identical-call notice at the dispatch layer.

Covers OPENSQUILLA_REPEATED_CALL_NOTICE (off by default). When armed with a
threshold N, a read-only tool call whose canonicalized arguments and raw
result are byte-identical to the previous identical call gets a notice
injected into the finalized content from the Nth repeat onward, plus a
``dispatch.repeated_call_notice`` runtime event. Error results, tools outside
the read-only allowlist, and changed results never trigger the notice, and
counters are scoped per session_key.
"""

from __future__ import annotations

import json

import pytest

from opensquilla.engine.types import ToolCall
from opensquilla.result_budget import ToolResultBudgetPolicy
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolContext, ToolSpec, current_tool_context

_NOTICE_PREFIX = "[repeated_call_notice]"


def _build_registry(read_file_results: list[str] | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    results = read_file_results if read_file_results is not None else ["stable file body"]
    call_index = {"value": 0}

    async def read_file(path: str) -> str:
        index = min(call_index["value"], len(results) - 1)
        call_index["value"] += 1
        return results[index]

    async def failing_read(path: str) -> str:
        raise ValueError("boom")

    async def echo_probe(value: str) -> str:
        return "identical probe output"

    registry.register(
        ToolSpec(
            name="read_file",
            description="read file",
            parameters={"path": {"type": "string"}},
            required=["path"],
        ),
        read_file,
    )
    registry.register(
        ToolSpec(
            name="git_status",
            description="failing status",
            parameters={"path": {"type": "string"}},
            required=["path"],
        ),
        failing_read,
    )
    registry.register(
        ToolSpec(
            name="echo_probe",
            description="echo probe",
            parameters={"value": {"type": "string"}},
            required=["value"],
        ),
        echo_probe,
    )
    return registry


def _read_file_call(tool_use_id: str) -> ToolCall:
    return ToolCall(
        tool_use_id=tool_use_id,
        tool_name="read_file",
        arguments={"path": "src/a.py"},
    )


def _notice_events(events: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        event for event in events if event.get("name") == "dispatch.repeated_call_notice"
    ]


@pytest.mark.asyncio
async def test_default_off_results_are_byte_identical_and_no_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_REPEATED_CALL_NOTICE", raising=False)
    events: list[dict[str, object]] = []
    handler = build_tool_handler(
        _build_registry(),
        ToolContext(on_runtime_event=events.append, session_key="agent:main:off"),
    )

    contents = []
    for index in range(5):
        result = await handler(_read_file_call(f"tc-off-{index}"))
        assert result.is_error is False
        contents.append(result.content)

    assert contents == ["stable file body"] * 5
    assert _notice_events(events) == []


@pytest.mark.asyncio
async def test_invalid_gate_values_stay_off(monkeypatch: pytest.MonkeyPatch) -> None:
    for raw in ("0", "abc", "-2", "  "):
        monkeypatch.setenv("OPENSQUILLA_REPEATED_CALL_NOTICE", raw)
        events: list[dict[str, object]] = []
        handler = build_tool_handler(
            _build_registry(),
            ToolContext(on_runtime_event=events.append, session_key="agent:main:invalid"),
        )

        for index in range(3):
            result = await handler(_read_file_call(f"tc-invalid-{index}"))
            assert result.content == "stable file body"

        assert _notice_events(events) == []


@pytest.mark.asyncio
async def test_threshold_three_notices_from_third_identical_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_REPEATED_CALL_NOTICE", "3")
    events: list[dict[str, object]] = []
    handler = build_tool_handler(
        _build_registry(),
        ToolContext(on_runtime_event=events.append, session_key="agent:main:notice"),
    )

    first = await handler(_read_file_call("tc-n-1"))
    second = await handler(_read_file_call("tc-n-2"))
    third = await handler(_read_file_call("tc-n-3"))
    fourth = await handler(_read_file_call("tc-n-4"))

    assert first.content == "stable file body"
    assert second.content == "stable file body"
    assert third.content == (
        f"{_NOTICE_PREFIX} This exact read_file call has already been run 3 "
        "times this session and returned an identical result each time.\n"
        "stable file body"
    )
    assert fourth.content.startswith(
        f"{_NOTICE_PREFIX} This exact read_file call has already been run 4 times"
    )

    notice_events = _notice_events(events)
    assert [event["repeat_count"] for event in notice_events] == [3, 4]
    event = notice_events[0]
    assert event["feature"] == "repeated_call_notice"
    assert event["tool"] == "read_file"
    assert event["tool_name"] == "read_file"
    assert event["tool_use_id"] == "tc-n-3"
    assert event["threshold"] == 3
    assert event["injected_to_model"] is True
    assert event["session_key"] == "agent:main:notice"
    assert event["agent_id"] == "main"
    assert isinstance(event["arguments_sha256"], str)
    assert len(event["arguments_sha256"]) == 64
    assert isinstance(event["result_sha256"], str)
    assert len(event["result_sha256"]) == 64


@pytest.mark.asyncio
async def test_changed_result_resets_the_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_REPEATED_CALL_NOTICE", "2")
    events: list[dict[str, object]] = []
    handler = build_tool_handler(
        _build_registry(read_file_results=["body-a", "body-b", "body-c", "body-c"]),
        ToolContext(on_runtime_event=events.append, session_key="agent:main:reset"),
    )

    for index in range(3):
        result = await handler(_read_file_call(f"tc-reset-{index}"))
        assert result.content == f"body-{'abc'[index]}"
    assert _notice_events(events) == []

    # body-c repeats: count restarts at the first stable result, so the second
    # identical result reaches the threshold.
    repeated = await handler(_read_file_call("tc-reset-3"))
    assert repeated.content.startswith(
        f"{_NOTICE_PREFIX} This exact read_file call has already been run 2 times"
    )
    assert [event["repeat_count"] for event in _notice_events(events)] == [2]


@pytest.mark.asyncio
async def test_error_results_and_non_allowlisted_tools_are_never_noticed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_REPEATED_CALL_NOTICE", "1")
    events: list[dict[str, object]] = []
    handler = build_tool_handler(
        _build_registry(),
        ToolContext(on_runtime_event=events.append, session_key="agent:main:excluded"),
    )

    for index in range(3):
        error_result = await handler(
            ToolCall(
                tool_use_id=f"tc-err-{index}",
                tool_name="git_status",
                arguments={"path": "."},
            )
        )
        assert error_result.is_error is True
        assert _NOTICE_PREFIX not in error_result.content

    for index in range(3):
        probe_result = await handler(
            ToolCall(
                tool_use_id=f"tc-probe-{index}",
                tool_name="echo_probe",
                arguments={"value": "same"},
            )
        )
        assert probe_result.is_error is False
        assert probe_result.content == "identical probe output"

    assert _notice_events(events) == []


@pytest.mark.asyncio
async def test_json_dict_content_gets_notice_as_key_and_stays_parseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_REPEATED_CALL_NOTICE", "2")
    events: list[dict[str, object]] = []
    handler = build_tool_handler(
        _build_registry(read_file_results=["y" * 2000]),
        ToolContext(
            on_runtime_event=events.append,
            session_key="agent:main:json",
            tool_result_budget_policy=ToolResultBudgetPolicy(
                max_single_tool_result_chars=200,
            ),
        ),
    )

    first = await handler(_read_file_call("tc-json-1"))
    first_payload = json.loads(first.content)
    assert first_payload["result_truncated"] is True
    assert "repeated_call_notice" not in first_payload

    second = await handler(_read_file_call("tc-json-2"))
    second_payload = json.loads(second.content)
    assert second_payload["result_truncated"] is True
    assert second_payload["tool"] == "read_file"
    assert second_payload["repeated_call_notice"] == (
        f"{_NOTICE_PREFIX} This exact read_file call has already been run 2 "
        "times this session and returned an identical result each time."
    )
    assert [event["repeat_count"] for event in _notice_events(events)] == [2]


@pytest.mark.asyncio
async def test_session_keys_track_independent_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_REPEATED_CALL_NOTICE", "2")
    events: list[dict[str, object]] = []
    handler = build_tool_handler(_build_registry())
    parent_ctx = ToolContext(
        on_runtime_event=events.append,
        session_key="agent:main:parent",
    )
    subagent_ctx = ToolContext(
        on_runtime_event=events.append,
        session_key="subagent:agent:main:parent",
    )

    async def dispatch(ctx: ToolContext, tool_use_id: str):
        token = current_tool_context.set(ctx)
        try:
            return await handler(_read_file_call(tool_use_id))
        finally:
            current_tool_context.reset(token)

    parent_first = await dispatch(parent_ctx, "tc-scope-p1")
    subagent_first = await dispatch(subagent_ctx, "tc-scope-s1")
    assert parent_first.content == "stable file body"
    assert subagent_first.content == "stable file body"
    assert _notice_events(events) == []

    parent_second = await dispatch(parent_ctx, "tc-scope-p2")
    assert parent_second.content.startswith(_NOTICE_PREFIX)

    subagent_second = await dispatch(subagent_ctx, "tc-scope-s2")
    assert subagent_second.content.startswith(_NOTICE_PREFIX)

    notice_events = _notice_events(events)
    assert [event["session_key"] for event in notice_events] == [
        "agent:main:parent",
        "subagent:agent:main:parent",
    ]
    assert [event["repeat_count"] for event in notice_events] == [2, 2]


@pytest.mark.asyncio
async def test_json_file_body_keeps_exact_bytes_with_text_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A file whose content is itself a JSON object (package.json etc.) is NOT
    # the structured result_truncated wrapper: the notice must land as a text
    # prefix and the original bytes must survive verbatim, or a later
    # edit_file old_text taken from the displayed content will not match.
    monkeypatch.setenv("OPENSQUILLA_REPEATED_CALL_NOTICE", "2")
    file_body = '{\n  "name": "pkg",\n  "version": "1.0.0"\n}'
    events: list[dict[str, object]] = []
    handler = build_tool_handler(
        _build_registry(read_file_results=[file_body]),
        ToolContext(
            on_runtime_event=events.append,
            session_key="agent:main:jsonbody",
        ),
    )

    first = await handler(_read_file_call("tc-body-1"))
    assert first.content == file_body

    second = await handler(_read_file_call("tc-body-2"))
    assert second.content.startswith(_NOTICE_PREFIX)
    assert second.content.endswith("\n" + file_body)
    assert "repeated_call_notice" not in json.loads(file_body)
    assert [event["repeat_count"] for event in _notice_events(events)] == [2]


@pytest.mark.asyncio
async def test_no_session_identity_is_never_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A caller without session_key gets no counter at all: id()-derived scope
    # fallbacks alias across unrelated callers (id reuse after GC; every None
    # ctx shares one id), so identity-less traffic must stay notice-free
    # rather than share a merged counter.
    monkeypatch.setenv("OPENSQUILLA_REPEATED_CALL_NOTICE", "2")
    events: list[dict[str, object]] = []
    handler = build_tool_handler(
        _build_registry(),
        ToolContext(on_runtime_event=events.append, session_key=None),
    )

    for index in range(4):
        result = await handler(_read_file_call(f"tc-anon-{index}"))
        assert result.content == "stable file body"

    assert _notice_events(events) == []
