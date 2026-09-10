"""Retired projection controls cannot change the default result envelopes."""

from __future__ import annotations

import json
from dataclasses import fields
from types import SimpleNamespace
from typing import Any

import pytest

import opensquilla.engine.agent as agent_mod
from opensquilla.engine import Agent, AgentConfig, SubagentSpec, ToolResult
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


def test_retired_projection_config_keeps_constructor_slots() -> None:
    field_names = [item.name for item in fields(AgentConfig)]
    for expected in (
        [
            "tool_result_projection_max_inline_chars",
            "tool_result_fresh_diagnostic_policy_enabled",
            "tool_result_diagnostic_retrieval_gate_enabled",
            "tool_result_fresh_diagnostic_inline_max_chars",
            "tool_result_dispatch_max_chars",
        ],
        [
            "mid_budget_no_diff_nudge",
            "provider_history_dedup_enabled",
            "provider_history_dedup_min_repeats",
            "projection_signal_hints",
            "tool_loop_observer_mode",
        ],
    ):
        start = field_names.index(expected[0])
        assert field_names[start : start + len(expected)] == expected


def test_retired_projection_options_are_not_propagated_to_child() -> None:
    from opensquilla.gateway.config import AgentTokenSavingConfig

    legacy = AgentTokenSavingConfig(
        tool_result_fresh_diagnostic_policy_enabled=True,
        tool_result_diagnostic_retrieval_gate_enabled=True,
        tool_result_fresh_diagnostic_inline_max_chars=2048,
    )
    assert legacy.tool_result_fresh_diagnostic_policy_enabled is True
    agent = Agent(
        provider=_TextProvider(),
        config=AgentConfig(
            projection_signal_hints=True,
            provider_history_dedup_enabled=True,
            provider_history_dedup_min_repeats=8,
            tool_result_fresh_diagnostic_policy_enabled=True,
            tool_result_diagnostic_retrieval_gate_enabled=True,
            tool_result_fresh_diagnostic_inline_max_chars=2048,
        ),
    )
    child = agent._make_child_agent(SubagentSpec(task="child task"), depth=1)
    assert child.config.projection_signal_hints is False
    assert child.config.provider_history_dedup_enabled is False
    assert child.config.provider_history_dedup_min_repeats == 2
    assert child.config.tool_result_fresh_diagnostic_policy_enabled is False
    assert child.config.tool_result_diagnostic_retrieval_gate_enabled is False
    assert child.config.tool_result_fresh_diagnostic_inline_max_chars == 64_000


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
            projection_signal_hints=True,
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


@pytest.mark.asyncio
async def test_fresh_projection_unchanged_with_retired_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_PROJECTION_SIGNAL_HINTS", "on")
    monkeypatch.setenv("OPENSQUILLA_PROJECTION_SIGNAL_PATTERNS", "[")
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


def test_provider_projection_unchanged_with_retired_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_PROJECTION_SIGNAL_HINTS", "on")
    monkeypatch.setenv("OPENSQUILLA_PROJECTION_SIGNAL_PATTERNS", "[")
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


def test_aggregate_compaction_unchanged_with_retired_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_PROJECTION_SIGNAL_HINTS", "on")
    monkeypatch.setenv("OPENSQUILLA_PROJECTION_SIGNAL_PATTERNS", "[")
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
