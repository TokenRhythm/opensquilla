"""Projection signal-scan notices, runtime events, and Child propagation."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

import opensquilla.engine.agent as agent_mod
from opensquilla.engine import Agent, AgentConfig, SubagentSpec, ToolResult
from opensquilla.engine.agent import (
    _projection_signal_hints_enabled,
    _tool_result_signal_scan,
)
from opensquilla.provider import (
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    TextDeltaEvent,
)
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.tools import ToolRegistry, tool
from opensquilla.tools.dispatch import build_tool_handler


class _TextProvider:
    provider_name = "fake"

    def __init__(self, return_text: str = "done") -> None:
        self.return_text = return_text

    def chat(self, messages, tools=None, config=None):
        return self._stream()

    async def _stream(self):
        yield TextDeltaEvent(text=self.return_text)
        yield ProviderDone(stop_reason="stop", model="fake-model")

    async def list_models(self) -> list[Any]:
        return []


def _events_named(path, name: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip() and json.loads(line).get("name") == name
    ]


def test_child_agent_inherits_tool_surface_lever_fields() -> None:
    agent = Agent(
        provider=_TextProvider(),
        config=AgentConfig(projection_signal_hints=True),
    )

    child = agent._make_child_agent(SubagentSpec(task="child task"), depth=1)

    assert child.config.projection_signal_hints is True


# ---------------------------------------------------------------------------
# M4 — projection signal hints
# ---------------------------------------------------------------------------


def _fake_reduce(**kwargs: Any) -> Any:
    return SimpleNamespace(
        inline_text="[tokenjuice]\ncommand output summarized",
        raw_chars=len(kwargs["content"]),
        reduced_chars=64,
        ratio=0.01,
        reducer="tests/pytest",
    )


def _projection_agent(tmp_path, **config_kwargs: Any) -> Agent:
    registry = ToolRegistry()

    @tool(
        name="retrieve_tool_result",
        description="Retrieve a stored tool result.",
        params={"handle": {"type": "string"}},
        required=["handle"],
        registry=registry,
    )
    async def retrieve_tool_result(handle: str) -> str:
        return handle

    return Agent(
        provider=_TextProvider(),
        config=AgentConfig(
            tool_result_store_dir=str(tmp_path / "tool-results"),
            tool_result_store_session_id="session-1",
            tool_result_store_session_key="agent:main:session-1",
            tool_result_store_agent_id="main",
            tool_result_fresh_diagnostic_inline_max_chars=1,
            **config_kwargs,
        ),
        tool_definitions=registry.to_tool_definitions(),
        tool_handler=build_tool_handler(registry),
    )


_FAILURE_CONTENT = (
    "pytest output\n"
    "collected 12 items\n"
    "FAILED tests/test_api.py::test_bad - AssertionError: expected 1 == 2\n"
    + ("x" * 20_000)
)


def test_projection_signal_hints_env_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_PROJECTION_SIGNAL_HINTS", raising=False)
    assert _projection_signal_hints_enabled() is False
    assert _projection_signal_hints_enabled(True) is True
    monkeypatch.setenv("OPENSQUILLA_PROJECTION_SIGNAL_HINTS", "on")
    assert _projection_signal_hints_enabled() is True
    monkeypatch.setenv("OPENSQUILLA_PROJECTION_SIGNAL_HINTS", "off")
    assert _projection_signal_hints_enabled(True) is False
    monkeypatch.setenv("OPENSQUILLA_PROJECTION_SIGNAL_HINTS", "bogus")
    with pytest.raises(ValueError):
        _projection_signal_hints_enabled()


def test_signal_scan_contiguous_and_preview_modes() -> None:
    content = "ok line\nFAILED tests/test_x.py::test_y\n" + ("pad\n" * 50)
    handle = "tr-" + ("a" * 32)
    # Contiguous mode: the failure line sits inside the omitted span.
    rendered, count, first = _tool_result_signal_scan(
        content, handle=handle, head_chars=4, tail_chars=4
    )
    assert count == 1
    assert first == 2
    assert "signal_scan: 1 lines matching failure patterns" in rendered
    assert "(first at L2)" in rendered
    assert (
        'signal_next_call: retrieve_tool_result {"handle": "' + handle + '"'
        in rendered
    )
    assert '"mode": "query"' in rendered
    assert '"query": "L2"' in rendered
    # Head covers the failure line: nothing omitted matches.
    rendered, count, first = _tool_result_signal_scan(
        content, handle=handle, head_chars=len(content), tail_chars=0
    )
    assert (rendered, count, first) == ("", 0, None)
    # Preview-membership mode: line present in the preview is not omitted.
    preview = frozenset(["FAILED tests/test_x.py::test_y"])
    rendered, count, first = _tool_result_signal_scan(
        content, handle=handle, preview_lines=preview
    )
    assert (rendered, count, first) == ("", 0, None)
    # No handle: unactionable, never renders.
    rendered, count, first = _tool_result_signal_scan(
        content, handle=None, head_chars=4, tail_chars=4
    )
    assert (rendered, count, first) == ("", 0, None)


@pytest.mark.asyncio
async def test_fresh_projection_appends_signal_scan_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_PROJECTION_SIGNAL_HINTS", "on")
    monkeypatch.setattr(
        agent_mod, "reduce_tool_result_with_tokenjuice", _fake_reduce, raising=False
    )
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    agent = _projection_agent(
        tmp_path, runtime_events_path=str(runtime_events_path)
    )

    projected = await agent._project_tool_result_for_llm(
        ToolResult(
            tool_use_id="tool-1",
            tool_name="exec_command",
            content=_FAILURE_CONTENT,
            is_error=True,
        )
    )

    assert "[tool_result_projection]" in projected.content
    assert "signal_scan: " in projected.content
    assert "(first at L3)" in projected.content
    assert 'signal_next_call: retrieve_tool_result {"handle": "tr-' in projected.content
    assert '"query": "L3"' in projected.content
    # Ordering: signal lines sit between search_hints and the projected body.
    assert projected.content.index("search_hints:") < projected.content.index(
        "signal_scan: "
    )
    assert projected.content.index("signal_next_call:") < projected.content.index(
        "[tokenjuice]"
    )
    hint_events = _events_named(runtime_events_path, "projection_signal_hints")
    assert len(hint_events) == 1
    event = hint_events[0]
    assert event["feature"] == "tool_result_projection"
    assert event["action"] == "hint_appended"
    assert event["mechanism"] == "signal_scan"
    assert event["builder"] == "fresh"
    assert event["signal_first_line"] == 3
    assert event["signal_match_lines"] >= 1
    assert event["tool_result_handle"].startswith("tr-")
    assert agent.config.metadata["tool_projection_signal_hints"] == 1


@pytest.mark.asyncio
async def test_fresh_projection_unchanged_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_PROJECTION_SIGNAL_HINTS", raising=False)
    monkeypatch.delenv("OPENSQUILLA_PROJECTION_SIGNAL_PATTERNS", raising=False)
    monkeypatch.setattr(
        agent_mod, "reduce_tool_result_with_tokenjuice", _fake_reduce, raising=False
    )
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    agent = _projection_agent(
        tmp_path, runtime_events_path=str(runtime_events_path)
    )

    projected = await agent._project_tool_result_for_llm(
        ToolResult(
            tool_use_id="tool-1",
            tool_name="exec_command",
            content=_FAILURE_CONTENT,
            is_error=True,
        )
    )

    assert "[tool_result_projection]" in projected.content
    assert "signal_scan:" not in projected.content
    assert "signal_next_call:" not in projected.content
    assert _events_named(runtime_events_path, "projection_signal_hints") == []
    assert "tool_projection_signal_hints" not in agent.config.metadata
    # Byte-identity with the pre-lever envelope: rebuild it from the stored
    # record exactly as the base builder does.
    stored = agent._store_tool_result_snapshot(
        _FAILURE_CONTENT, tool_use_id="tool-1", tool_name="exec_command"
    )
    expected = (
        "[tool_result_projection]\n"
        f"tool_result_handle: {stored.handle}\n"
        f"sha256: {stored.sha256}\n"
        f"original_chars: {stored.chars}\n"
        "preview_complete: false\n"
        f"{agent_mod._TOOL_RESULT_RETRIEVE_HINT}"
        f"{agent_mod._tool_result_search_hints(_FAILURE_CONTENT)}"
        "[tokenjuice]\ncommand output summarized"
    )
    assert projected.content == expected


@pytest.mark.asyncio
async def test_projection_signal_patterns_env_overrides_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_PROJECTION_SIGNAL_HINTS", "on")
    monkeypatch.setenv(
        "OPENSQUILLA_PROJECTION_SIGNAL_PATTERNS", r"\bDIAG_MARKER\b"
    )
    monkeypatch.setattr(
        agent_mod, "reduce_tool_result_with_tokenjuice", _fake_reduce, raising=False
    )
    agent = _projection_agent(tmp_path)
    content = (
        "line one\n"
        "FAILED tests/test_api.py::test_bad - AssertionError\n"
        "DIAG_MARKER custom failure channel\n" + ("x" * 20_000)
    )

    projected = await agent._project_tool_result_for_llm(
        ToolResult(
            tool_use_id="tool-1",
            tool_name="exec_command",
            content=content,
            is_error=True,
        )
    )

    # Only the override pattern matches: first hit is the DIAG_MARKER line.
    assert "signal_scan: 1 lines matching failure patterns" in projected.content
    assert "(first at L3)" in projected.content
    assert '"query": "L3"' in projected.content


def test_projection_signal_patterns_invalid_regex_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_PROJECTION_SIGNAL_PATTERNS", "(unclosed")
    with pytest.raises(ValueError):
        agent_mod._projection_signal_pattern()


def test_provider_projection_appends_signal_scan_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_PROJECTION_SIGNAL_HINTS", "on")
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    agent = _projection_agent(
        tmp_path, runtime_events_path=str(runtime_events_path)
    )

    projection = agent._tool_result_projection_for_provider(
        _FAILURE_CONTENT,
        tool_use_id="tool-1",
        tool_name="exec_command",
        reason="tool result compacted for provider request context",
        max_preview_chars=40,
    )

    assert projection is not None
    assert "signal_scan: " in projection
    assert "(first at L3)" in projection
    assert 'signal_next_call: retrieve_tool_result {"handle": "tr-' in projection
    # Ordering: after search_hints (when present) and before omitted_chars.
    assert projection.index("signal_next_call:") < projection.index("omitted_chars:")
    hint_events = _events_named(runtime_events_path, "projection_signal_hints")
    assert len(hint_events) == 1
    assert hint_events[0]["builder"] == "provider_single"


def test_provider_projection_unchanged_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_PROJECTION_SIGNAL_HINTS", raising=False)
    monkeypatch.delenv("OPENSQUILLA_PROJECTION_SIGNAL_PATTERNS", raising=False)
    agent = _projection_agent(tmp_path)

    projection = agent._tool_result_projection_for_provider(
        _FAILURE_CONTENT,
        tool_use_id="tool-1",
        tool_name="exec_command",
        reason="tool result compacted for provider request context",
        max_preview_chars=40,
    )

    assert projection is not None
    assert "signal_scan:" not in projection
    assert "signal_next_call:" not in projection


def _aggregate_messages(old_content: str) -> list[Message]:
    # Three tool results: the aggregate pass needs more than two and always
    # preserves the newest two, so only "old-1" is eligible for compaction.
    messages: list[Message] = [
        Message(
            role="assistant",
            content=[ContentBlockToolUse(id="old-1", name="execute_code", input={})],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="old-1", content=old_content, is_error=False
                )
            ],
        ),
    ]
    for use_id, filler in (("mid-1", "m"), ("new-1", "r")):
        messages.append(
            Message(
                role="assistant",
                content=[
                    ContentBlockToolUse(id=use_id, name="execute_code", input={})
                ],
            )
        )
        messages.append(
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(
                        tool_use_id=use_id,
                        content=f"recent output {use_id}\n" + (filler * 4000),
                        is_error=False,
                    )
                ],
            )
        )
    return messages


def test_aggregate_compaction_appends_signal_scan_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_PROJECTION_SIGNAL_HINTS", "on")
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    agent = _projection_agent(
        tmp_path,
        context_window_tokens=200,
        runtime_events_path=str(runtime_events_path),
    )
    old_content = (
        "old bulky output\n"
        + ("pad line\n" * 60)
        + "FAILED tests/test_api.py::test_bad - AssertionError\n"
        + ("x" * 4000)
    )
    messages = _aggregate_messages(old_content)

    compacted = agent._compact_aggregate_tool_results_for_provider(messages)

    old_result = compacted[1].content[0]
    assert isinstance(old_result, ContentBlockToolResult)
    assert "aggregate_tool_result_compacted" in old_result.content
    assert "signal_scan: " in old_result.content
    assert "(first at L62)" in old_result.content
    assert 'signal_next_call: retrieve_tool_result {"handle": "tr-' in old_result.content
    assert old_result.content.index("signal_next_call:") < old_result.content.index(
        "omitted_chars:"
    )
    hint_events = _events_named(runtime_events_path, "projection_signal_hints")
    assert len(hint_events) == 1
    assert hint_events[0]["builder"] == "aggregate"
    assert hint_events[0]["signal_first_line"] == 62


def test_aggregate_compaction_unchanged_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_PROJECTION_SIGNAL_HINTS", raising=False)
    monkeypatch.delenv("OPENSQUILLA_PROJECTION_SIGNAL_PATTERNS", raising=False)
    agent = _projection_agent(tmp_path, context_window_tokens=200)
    old_content = (
        "old bulky output\n"
        + "FAILED tests/test_api.py::test_bad - AssertionError\n"
        + ("x" * 4000)
    )
    messages = _aggregate_messages(old_content)

    compacted = agent._compact_aggregate_tool_results_for_provider(messages)

    old_result = compacted[1].content[0]
    assert isinstance(old_result, ContentBlockToolResult)
    assert "aggregate_tool_result_compacted" in old_result.content
    assert "signal_scan:" not in old_result.content
    assert "signal_next_call:" not in old_result.content
