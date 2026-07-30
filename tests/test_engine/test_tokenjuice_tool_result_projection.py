from __future__ import annotations

import json
import random
import string
from types import SimpleNamespace
from typing import Any

import pytest

import opensquilla.engine.agent as agent_mod
import opensquilla.engine.tokenjuice_adapter as tokenjuice_adapter_mod
from opensquilla.engine import Agent, AgentConfig, ToolCall, ToolResult
from opensquilla.engine.tool_result_store import ToolResultStore
from opensquilla.engine.types import ToolResultEvent, ToolUseDeltaEvent
from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
from opensquilla.plugins.tokenjuice import reduce_tool_result as backend_reduce_tool_result
from opensquilla.provider import (
    ContentBlockThinking,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import DoneEvent as ProviderDoneEvent
from opensquilla.provider import ToolUseDeltaEvent as ProviderToolUseDeltaEvent
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEndEvent
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStartEvent
from opensquilla.tools.types import ToolContext


class _Provider:
    provider_name = "fake"

    def __init__(self, return_text: str | None = None) -> None:
        self.return_text = return_text
        self.chat_calls = 0

    def chat(self, messages, tools=None, config=None):
        self.chat_calls += 1
        if self.return_text is None:  # pragma: no cover - must not run
            raise AssertionError("provider should not be used")
        return self._stream()

    async def _stream(self):
        yield TextDeltaEvent(text=self.return_text or "")
        yield ProviderDoneEvent(stop_reason="stop", model="fake-model")

    async def list_models(self) -> list[Any]:
        return []


def test_agent_configured_dispatch_result_budget_updates_tool_context() -> None:
    agent = Agent(
        provider=_Provider(),
        config=AgentConfig(
            tool_result_dispatch_max_chars=120_000,
            tool_result_dispatch_turn_max_chars=300_000,
        ),
        tool_context=ToolContext(),
    )

    assert agent._tool_context is not None
    policy = agent._tool_context.tool_result_budget_policy
    assert policy is not None
    assert policy.max_single_tool_result_chars is None
    assert policy.max_single_execution_result_chars == 120_000
    assert policy.max_execution_tool_result_chars_per_turn == 300_000


class _ToolCallingProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    def chat(self, messages, tools=None, config=None):
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int):
        if call_number == 1:
            yield ProviderToolUseStartEvent(tool_use_id="tool-1", tool_name="exec_command")
            yield ProviderToolUseDeltaEvent(
                tool_use_id="tool-1",
                json_fragment='{"command": "pytest -q"',
            )
            yield ProviderToolUseDeltaEvent(
                tool_use_id="tool-1",
                json_fragment=', "workdir": "/repo"}',
            )
            yield ProviderToolUseEndEvent(
                tool_use_id="tool-1",
                tool_name="exec_command",
                arguments={"command": "pytest -q", "workdir": "/repo"},
            )
            yield ProviderDoneEvent(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return
        yield TextDeltaEvent(text="done")
        yield ProviderDoneEvent(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _SignatureOnlyToolCallingProvider(_ToolCallingProvider):
    async def _stream(self, call_number: int):
        if call_number == 1:
            yield ProviderToolUseStartEvent(tool_use_id="tool-1", tool_name="exec_command")
            yield ProviderToolUseDeltaEvent(
                tool_use_id="tool-1",
                json_fragment='{"command": "pytest -q"}',
            )
            yield ProviderToolUseEndEvent(
                tool_use_id="tool-1",
                tool_name="exec_command",
                arguments={"command": "pytest -q"},
            )
            yield ProviderDoneEvent(
                stop_reason="tool_use",
                input_tokens=1,
                output_tokens=1,
                thinking_signature="gemini-signature-only",
            )
            return
        yield TextDeltaEvent(text="done")
        yield ProviderDoneEvent(stop_reason="stop", input_tokens=1, output_tokens=1)


class _ReadFileCallingProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    def chat(self, messages, tools=None, config=None):
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int):
        if call_number == 1:
            yield ProviderToolUseStartEvent(tool_use_id="tool-1", tool_name="read_file")
            yield ProviderToolUseEndEvent(
                tool_use_id="tool-1",
                tool_name="read_file",
                arguments={"path": "src/lib.rs"},
            )
            yield ProviderDoneEvent(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return
        yield TextDeltaEvent(text="done")
        yield ProviderDoneEvent(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _DiagnosticRetrievalGateProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    def chat(self, messages, tools=None, config=None):
        self.calls.append(messages)
        return self._stream(len(self.calls), messages)

    async def _stream(self, call_number: int, messages: list[Any]):
        if call_number == 1:
            yield ProviderToolUseStartEvent(tool_use_id="tool-1", tool_name="exec_command")
            yield ProviderToolUseEndEvent(
                tool_use_id="tool-1",
                tool_name="exec_command",
                arguments={"command": "pytest -q", "workdir": "/repo"},
            )
            yield ProviderDoneEvent(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return
        if call_number == 2:
            yield ProviderToolUseStartEvent(tool_use_id="tool-2", tool_name="apply_patch")
            yield ProviderToolUseEndEvent(
                tool_use_id="tool-2",
                tool_name="apply_patch",
                arguments={
                    "patch": (
                        "*** Begin Patch\n"
                        "*** Update File: src/app.py\n"
                        "@@\n"
                        "-pass\n"
                        "+ok\n"
                        "*** End Patch"
                    )
                },
            )
            yield ProviderDoneEvent(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return
        if call_number == 3:
            handle = _first_tool_result_handle(messages)
            yield ProviderToolUseStartEvent(tool_use_id="tool-3", tool_name="retrieve_tool_result")
            yield ProviderToolUseEndEvent(
                tool_use_id="tool-3",
                tool_name="retrieve_tool_result",
                arguments={
                    "handle": handle,
                    "mode": "query",
                    "query": "FAILED tests/test_api.py::test_bad",
                },
            )
            yield ProviderDoneEvent(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return
        if call_number == 4:
            yield ProviderToolUseStartEvent(tool_use_id="tool-4", tool_name="apply_patch")
            yield ProviderToolUseEndEvent(
                tool_use_id="tool-4",
                tool_name="apply_patch",
                arguments={
                    "patch": (
                        "*** Begin Patch\n"
                        "*** Update File: src/app.py\n"
                        "@@\n"
                        "-pass\n"
                        "+ok\n"
                        "*** End Patch"
                    )
                },
            )
            yield ProviderDoneEvent(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return
        yield TextDeltaEvent(text="done")
        yield ProviderDoneEvent(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


def _last_tool_result_content(messages: list[Any]) -> str:
    for message in reversed(messages):
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if getattr(block, "type", None) == "tool_result":
                return str(getattr(block, "content", ""))
    raise AssertionError("provider request did not include a tool_result block")


def _tool_def(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Mock tool {name}",
        input_schema=ToolInputSchema(properties={}, required=[]),
    )


def _message_texts(messages: list[Any]) -> list[str]:
    texts: list[str] = []
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            texts.append(content)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            block_content = getattr(block, "content", None)
            if isinstance(block_content, str):
                texts.append(block_content)
    return texts


def _first_tool_result_handle(messages: list[Any]) -> str:
    for text in _message_texts(messages):
        for line in text.splitlines():
            if line.startswith("tool_result_handle:"):
                return line.split(":", 1)[1].strip()
    raise AssertionError("expected projected tool result handle in provider messages")


@pytest.mark.asyncio
async def test_agent_projects_tokenjuice_without_context_window_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_reduce(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(
            inline_text="[tokenjuice]\n1 failed, 2 passed",
            raw_chars=len(kwargs["content"]),
            reduced_chars=30,
            ratio=0.1,
            reducer="tests/pytest",
        )

    monkeypatch.setattr(agent_mod, "reduce_tool_result_with_tokenjuice", fake_reduce, raising=False)
    agent = Agent(
        provider=_Provider(),
        config=AgentConfig(context_window_tokens=1_000_000),
    )
    result = ToolResult(
        tool_use_id="tool-1",
        tool_name="exec_command",
        content="pytest output\n" + ("x" * 1000),
    )

    projected = await agent._canonicalize_tool_result(
        result,
        tool_call=ToolCall(
            tool_use_id="tool-1",
            tool_name="exec_command",
            arguments={"command": "pytest -q", "workdir": "/repo"},
        ),
    )

    assert calls
    assert projected.content == "[tokenjuice]\n1 failed, 2 passed"
    assert calls[0]["tool_name"] == "exec_command"
    assert calls[0]["command"] == "pytest -q"
    assert calls[0]["cwd"] == "/repo"
    assert agent.config.metadata["tool_projection_backend"] == "tokenjuice"
    assert agent.config.metadata["tool_projection_attempts"] == 1
    assert agent.config.metadata["tool_projection_calls"] == 1


@pytest.mark.asyncio
async def test_fresh_diagnostic_under_cap_is_preserved_to_next_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_reduce(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(
            inline_text="[tokenjuice]\nFAILED tests/test_api.py::test_bad - AssertionError",
            raw_chars=len(kwargs["content"]),
            reduced_chars=64,
            ratio=0.01,
            reducer="tests/pytest",
        )

    monkeypatch.setattr(agent_mod, "reduce_tool_result_with_tokenjuice", fake_reduce, raising=False)
    raw_output = (
        "platform linux -- Python 3.13\n"
        "rootdir: /repo\n"
        "FAILED tests/test_api.py::test_bad - AssertionError: expected 1 == 2\n"
    )

    async def handler(tool_call: ToolCall) -> ToolResult:
        return ToolResult(
            tool_use_id=tool_call.tool_use_id,
            tool_name=tool_call.tool_name,
            content=raw_output,
            is_error=True,
        )

    provider = _ToolCallingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=1_000_000,
            max_iterations=2,
            tool_result_fresh_diagnostic_policy_enabled=True,
            tool_result_fresh_diagnostic_inline_max_chars=10_000,
        ),
        tool_definitions=[_tool_def("exec_command")],
        tool_handler=handler,
    )

    events = [event async for event in agent.run_turn("run tests")]

    assert len(provider.calls) == 2
    second_call_tool_result = _last_tool_result_content(provider.calls[1])
    assert second_call_tool_result == raw_output
    assert calls == []
    projected_event = next(event for event in events if isinstance(event, ToolResultEvent))
    assert projected_event.result == raw_output
    assert agent.config.metadata["tool_projection_fresh_diagnostic_one_hop_preserves"] == 1
    assert agent.config.metadata["tool_projection_fresh_diagnostic_results"] == 1


@pytest.mark.asyncio
async def test_source_read_file_result_is_preserved_before_tokenjuice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_reduce(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(
            inline_text="summary that must not replace source",
            raw_chars=len(kwargs["content"]),
            reduced_chars=34,
            ratio=0.01,
            reducer="generic/fallback",
        )

    monkeypatch.setattr(agent_mod, "reduce_tool_result_with_tokenjuice", fake_reduce, raising=False)
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    agent = Agent(
        provider=_Provider(),
        config=AgentConfig(runtime_events_path=str(runtime_events_path)),
    )
    source = "\n".join(f"{index}: important source line" for index in range(4000))
    result = ToolResult(
        tool_use_id="tool-1",
        tool_name="read_file",
        content=source,
    )

    projected = await agent._canonicalize_tool_result(
        result,
        tool_call=ToolCall(
            tool_use_id="tool-1",
            tool_name="read_file",
            arguments={"path": "src/lib.rs"},
        ),
    )

    assert projected is result
    assert projected.content == source
    assert calls == []
    assert agent.config.metadata["tool_projection_attempts"] == 1
    assert agent.config.metadata["tool_projection_noops"] == 1
    assert agent.config.metadata["tool_projection_semantic_preserves"] == 1
    assert "tool_projection_applied" not in agent.config.metadata

    logged = [
        json.loads(line)
        for line in runtime_events_path.read_text(encoding="utf-8").splitlines()
    ]
    projection_event = next(
        event for event in logged if event["feature"] == "tool_result_projection"
    )
    assert projection_event["outcome"] == "noop"
    assert projection_event["reason"] == "semantic_read_file_preserved"
    assert projection_event["tool_name"] == "read_file"


@pytest.mark.asyncio
async def test_exec_git_diff_result_is_preserved_before_tokenjuice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_reduce(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(
            inline_text="diff summary that must not replace patch",
            raw_chars=len(kwargs["content"]),
            reduced_chars=40,
            ratio=0.1,
            reducer="git/diff",
        )

    monkeypatch.setattr(agent_mod, "reduce_tool_result_with_tokenjuice", fake_reduce, raising=False)
    agent = Agent(provider=_Provider(), config=AgentConfig())
    diff = "\n".join(
        [
            "diff --git a/src/lib.rs b/src/lib.rs",
            "--- a/src/lib.rs",
            "+++ b/src/lib.rs",
            "@@ -1,3 +1,4 @@",
            "+new line",
            *(f" context line {index}" for index in range(200)),
        ]
    )
    result = ToolResult(
        tool_use_id="tool-1",
        tool_name="exec_command",
        content=diff,
    )

    projected = await agent._canonicalize_tool_result(
        result,
        tool_call=ToolCall(
            tool_use_id="tool-1",
            tool_name="exec_command",
            arguments={"command": "cd /repo && git diff -- src/lib.rs"},
        ),
    )

    assert projected is result
    assert projected.content == diff
    assert calls == []
    assert agent.config.metadata["tool_projection_semantic_preserves"] == 1
    assert "tool_projection_applied" not in agent.config.metadata


@pytest.mark.parametrize(
    "command",
    [
        "cat src/lib.rs",
        "sed -n '10,80p' src/lib.rs",
        "rg -n important src/lib.rs",
        "grep -n important src/lib.rs",
    ],
)
@pytest.mark.asyncio
async def test_exec_source_read_result_is_preserved_before_tokenjuice(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_reduce(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(
            inline_text="source summary that must not replace source snippets",
            raw_chars=len(kwargs["content"]),
            reduced_chars=55,
            ratio=0.1,
            reducer="generic/fallback",
        )

    monkeypatch.setattr(agent_mod, "reduce_tool_result_with_tokenjuice", fake_reduce, raising=False)
    agent = Agent(provider=_Provider(), config=AgentConfig())
    source = "\n".join(f"{index}: important implementation detail" for index in range(300))
    result = ToolResult(
        tool_use_id="tool-1",
        tool_name="exec_command",
        content=source,
    )

    projected = await agent._canonicalize_tool_result(
        result,
        tool_call=ToolCall(
            tool_use_id="tool-1",
            tool_name="exec_command",
            arguments={"command": command},
        ),
    )

    assert projected is result
    assert projected.content == source
    assert calls == []
    assert agent.config.metadata["tool_projection_semantic_preserves"] == 1
    assert "tool_projection_applied" not in agent.config.metadata


@pytest.mark.asyncio
async def test_broad_grep_result_is_not_semantically_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_reduce(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(
            inline_text="[tokenjuice]\nbroad search summary",
            raw_chars=len(kwargs["content"]),
            reduced_chars=32,
            ratio=0.1,
            reducer="generic/fallback",
        )

    monkeypatch.setattr(agent_mod, "reduce_tool_result_with_tokenjuice", fake_reduce, raising=False)
    agent = Agent(provider=_Provider(), config=AgentConfig())
    content = "\n".join(f"src/lib.rs:{index}: important" for index in range(300))
    result = ToolResult(
        tool_use_id="tool-1",
        tool_name="exec_command",
        content=content,
    )

    projected = await agent._canonicalize_tool_result(
        result,
        tool_call=ToolCall(
            tool_use_id="tool-1",
            tool_name="exec_command",
            arguments={"command": "grep -R important ."},
        ),
    )

    assert projected is not result
    assert projected.content == "[tokenjuice]\nbroad search summary"
    assert len(calls) == 1
    assert agent.config.metadata["tool_projection_applied"] is True
    assert "tool_projection_semantic_preserves" not in agent.config.metadata


@pytest.mark.asyncio
async def test_tokenjuice_noop_preserves_tool_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agent_mod,
        "reduce_tool_result_with_tokenjuice",
        lambda **kwargs: None,
        raising=False,
    )
    agent = Agent(provider=_Provider(), config=AgentConfig(context_window_tokens=100))
    result = ToolResult(
        tool_use_id="tool-1",
        tool_name="exec_command",
        content="short output",
    )

    projected = await agent._canonicalize_tool_result(result)

    assert projected is result
    assert projected.content == "short output"
    assert agent.config.metadata["tool_projection_attempts"] == 1
    assert agent.config.metadata["tool_projection_noops"] == 1
    assert "tool_projection_backend" not in agent.config.metadata


@pytest.mark.asyncio
async def test_tokenjuice_projection_does_not_store_raw_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    def fake_reduce(**kwargs: Any) -> Any:
        return SimpleNamespace(
            inline_text="[tokenjuice]\nimportant failure",
            raw_chars=len(kwargs["content"]),
            reduced_chars=28,
            ratio=0.1,
            reducer="tests/pytest",
        )

    monkeypatch.setattr(agent_mod, "reduce_tool_result_with_tokenjuice", fake_reduce, raising=False)
    agent = Agent(
        provider=_Provider(),
        config=AgentConfig(tool_result_fresh_diagnostic_inline_max_chars=1),
    )
    raw_output = "raw output\n" + ("x" * 8000)

    projected = await agent._canonicalize_tool_result(
        ToolResult(
            tool_use_id="tool-1",
            tool_name="exec_command",
            content=raw_output,
            is_error=True,
        )
    )

    assert projected.content == "[tokenjuice]\nimportant failure"
    assert "tool_result_handle:" not in projected.content
    assert raw_output not in projected.content
    assert not (tmp_path / "tool-results").exists()


def test_tokenjuice_adapter_calls_python_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_reduce(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(
            inline_text="reduced output",
            raw_chars=23,
            reduced_chars=14,
            ratio=14 / 23,
            reducer="tests/pytest",
        )

    monkeypatch.setattr(
        tokenjuice_adapter_mod,
        "_reduce_tool_result_backend",
        fake_reduce,
    )

    result = tokenjuice_adapter_mod.reduce_tool_result_with_tokenjuice(
        tool_name="exec_command",
        content="raw output with details",
        is_error=False,
        tool_use_id="tool-1",
        arguments={"command": "pytest -q", "workdir": "/repo"},
        max_inline_chars=600,
    )

    assert result is not None
    assert result.inline_text == "reduced output"
    assert result.reducer == "tests/pytest"
    assert captured["tool_name"] == "exec_command"
    assert captured["content"] == "raw output with details"
    assert captured["is_error"] is False
    assert captured["tool_use_id"] == "tool-1"
    assert captured["arguments"] == {"command": "pytest -q", "workdir": "/repo"}
    assert captured["command"] == "pytest -q"
    assert captured["cwd"] == "/repo"
    assert captured["max_inline_chars"] == 600


def test_tokenjuice_adapter_returns_none_when_backend_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tokenjuice_adapter_mod,
        "_reduce_tool_result_backend",
        lambda **kwargs: None,
    )

    assert (
        tokenjuice_adapter_mod.reduce_tool_result_with_tokenjuice(
            tool_name="exec_command",
            content="raw output",
            is_error=False,
            tool_use_id="tool-1",
        )
        is None
    )


def test_tokenjuice_adapter_ignores_non_shrinking_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_reduce(**kwargs: Any) -> Any:
        return SimpleNamespace(
            inline_text="this output is longer than raw output",
            raw_chars=10,
            reduced_chars=35,
            ratio=3.5,
            reducer="generic/fallback",
        )

    monkeypatch.setattr(
        tokenjuice_adapter_mod,
        "_reduce_tool_result_backend",
        fake_reduce,
    )

    assert (
        tokenjuice_adapter_mod.reduce_tool_result_with_tokenjuice(
            tool_name="exec_command",
            content="raw output",
            is_error=False,
            tool_use_id="tool-1",
        )
        is None
    )


def test_tokenjuice_adapter_ignores_trailing_newline_only_reduction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_reduce(**kwargs: Any) -> Any:
        return SimpleNamespace(
            inline_text="exit_code=0\ninstalled",
            raw_chars=22,
            reduced_chars=21,
            ratio=21 / 22,
            reducer="generic/fallback",
        )

    monkeypatch.setattr(
        tokenjuice_adapter_mod,
        "_reduce_tool_result_backend",
        fake_reduce,
    )

    assert (
        tokenjuice_adapter_mod.reduce_tool_result_with_tokenjuice(
            tool_name="exec_command",
            content="exit_code=0\ninstalled\n",
            is_error=False,
            tool_use_id="tool-1",
        )
        is None
    )


def test_python_backend_reduces_pytest_output() -> None:
    output = "\n".join(
        [
            "platform darwin -- Python 3.13",
            "rootdir: /repo",
            "collected 3 items",
            "tests/test_api.py::test_ok PASSED",
            "tests/test_api.py::test_bad FAILED",
            "E   AssertionError: expected 1 == 2",
            "FAILED tests/test_api.py::test_bad - AssertionError",
            "=========================== 1 failed, 1 passed in 0.12s ===========================",
        ]
    )

    result = backend_reduce_tool_result(
        tool_name="exec_command",
        tool_use_id="tool-1",
        command="pytest -q",
        content=output,
        is_error=True,
        max_inline_chars=600,
    )

    assert result is not None
    assert result.reducer == "tests/pytest"
    assert "FAILED tests/test_api.py::test_bad" in result.inline_text
    assert "AssertionError" in result.inline_text
    assert "rootdir:" not in result.inline_text


@pytest.mark.asyncio
async def test_run_turn_feeds_tokenjuice_reduced_tool_result_to_next_provider_call() -> None:
    output = "\n".join(
        [
            "platform darwin -- Python 3.13",
            "rootdir: /repo",
            "collected 3 items",
            *(f"tests/test_api.py::test_extra_{index} PASSED" for index in range(40)),
            "tests/test_api.py::test_ok PASSED",
            "tests/test_api.py::test_bad FAILED",
            "E   AssertionError: expected 1 == 2",
            "FAILED tests/test_api.py::test_bad - AssertionError",
            "=========================== 1 failed, 1 passed in 0.12s ===========================",
        ]
    )

    async def handler(tool_call: ToolCall) -> ToolResult:
        return ToolResult(
            tool_use_id=tool_call.tool_use_id,
            tool_name=tool_call.tool_name,
            content=output,
            is_error=True,
        )

    provider = _ToolCallingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=1_000_000,
            max_iterations=2,
            tool_result_fresh_diagnostic_inline_max_chars=1,
        ),
        tool_definitions=[_tool_def("exec_command")],
        tool_handler=handler,
    )

    events = [event async for event in agent.run_turn("run tests")]

    assert len(provider.calls) == 2
    second_call_tool_result = _last_tool_result_content(provider.calls[1])
    assert "FAILED tests/test_api.py::test_bad" in second_call_tool_result
    assert "AssertionError" in second_call_tool_result
    assert "rootdir:" not in second_call_tool_result
    assert agent.config.metadata["tool_projection_backend"] == "tokenjuice"
    assert agent.config.metadata["tool_projection_fresh_diagnostic_results"] == 1
    assert agent.config.metadata["tool_projection_fresh_diagnostic_projections"] == 1
    assert "tool_projection_fresh_diagnostic_one_hop_preserves" not in agent.config.metadata
    projected_event = next(event for event in events if isinstance(event, ToolResultEvent))
    delta_fragments = [
        event.json_fragment for event in events if isinstance(event, ToolUseDeltaEvent)
    ]
    assert delta_fragments == ['{"command": "pytest -q"', ', "workdir": "/repo"}']
    assert projected_event.result == second_call_tool_result
    assert projected_event.result != output
    assert "rootdir:" not in projected_event.result


@pytest.mark.asyncio
async def test_projected_diagnostic_requires_focused_retrieval_before_edit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    def fake_reduce(**kwargs: Any) -> Any:
        return SimpleNamespace(
            inline_text="[tokenjuice]\nFAILED tests/test_api.py::test_bad - AssertionError",
            raw_chars=len(kwargs["content"]),
            reduced_chars=64,
            ratio=0.01,
            reducer="tests/pytest",
        )

    monkeypatch.setattr(agent_mod, "reduce_tool_result_with_tokenjuice", fake_reduce, raising=False)
    raw_output = (
        "pytest output\n"
        "FAILED tests/test_api.py::test_bad - AssertionError: expected 1 == 2\n"
        + ("traceback frame\n" * 500)
    )
    executed_tool_names: list[str] = []

    async def handler(tool_call: ToolCall) -> ToolResult:
        executed_tool_names.append(tool_call.tool_name)
        if tool_call.tool_name == "exec_command":
            return ToolResult(
                tool_use_id=tool_call.tool_use_id,
                tool_name=tool_call.tool_name,
                content=raw_output,
                is_error=True,
            )
        if tool_call.tool_name == "retrieve_tool_result":
            return ToolResult(
                tool_use_id=tool_call.tool_use_id,
                tool_name=tool_call.tool_name,
                content=(
                    "[tool_result_retrieval]\n"
                    "returned_content_is_complete: true\n"
                    "---\n"
                    "FAILED tests/test_api.py::test_bad"
                ),
            )
        if tool_call.tool_name == "apply_patch":
            return ToolResult(
                tool_use_id=tool_call.tool_use_id,
                tool_name=tool_call.tool_name,
                content="patch applied",
            )
        raise AssertionError(f"unexpected tool: {tool_call.tool_name}")

    provider = _DiagnosticRetrievalGateProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=1_000_000,
            max_iterations=5,
            tool_result_store_dir=str(tmp_path / "tool-results"),
            tool_result_store_session_id="session-1",
            tool_result_store_session_key="agent:main:session-1",
            tool_result_store_agent_id="main",
            tool_result_fresh_diagnostic_policy_enabled=True,
            tool_result_diagnostic_retrieval_gate_enabled=True,
            tool_result_fresh_diagnostic_inline_max_chars=100,
        ),
        tool_definitions=[
            _tool_def("exec_command"),
            _tool_def("apply_patch"),
            _tool_def("retrieve_tool_result"),
        ],
        tool_handler=handler,
    )

    events = [event async for event in agent.run_turn("fix tests")]

    blocked = [
        event
        for event in events
        if isinstance(event, ToolResultEvent)
        and event.tool_name == "apply_patch"
        and event.is_error
        and "retrieve_tool_result" in event.result
    ]
    assert blocked
    assert executed_tool_names == ["exec_command", "retrieve_tool_result", "apply_patch"]
    assert agent.config.metadata["tool_projection_fresh_diagnostic_projections"] == 1
    assert agent.config.metadata["tool_projection_diagnostic_retrieval_gate_blocks"] == 1
    assert agent.config.metadata["tool_projection_diagnostic_retrievals"] == 1


@pytest.mark.asyncio
async def test_run_turn_preserves_read_file_result_to_next_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_reduce(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(
            inline_text="source summary that would lose middle lines",
            raw_chars=len(kwargs["content"]),
            reduced_chars=43,
            ratio=0.01,
            reducer="generic/fallback",
        )

    monkeypatch.setattr(agent_mod, "reduce_tool_result_with_tokenjuice", fake_reduce, raising=False)
    source = "\n".join(f"line {index}: important implementation detail" for index in range(3000))

    async def handler(tool_call: ToolCall) -> ToolResult:
        return ToolResult(
            tool_use_id=tool_call.tool_use_id,
            tool_name=tool_call.tool_name,
            content=source,
        )

    provider = _ReadFileCallingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(context_window_tokens=1_000_000, max_iterations=2),
        tool_definitions=[_tool_def("read_file")],
        tool_handler=handler,
    )

    events = [event async for event in agent.run_turn("read source")]

    assert len(provider.calls) == 2
    second_call_tool_result = _last_tool_result_content(provider.calls[1])
    assert second_call_tool_result == source
    assert "[tool_result_projection]" not in second_call_tool_result
    assert calls == []
    result_event = next(event for event in events if isinstance(event, ToolResultEvent))
    assert result_event.result == source
    assert agent.config.metadata["tool_projection_semantic_preserves"] == 1
    assert "tool_projection_applied" not in agent.config.metadata


@pytest.mark.asyncio
async def test_runtime_events_record_provider_tool_schema_visibility(tmp_path) -> None:
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    agent = Agent(
        provider=_Provider(return_text="done"),
        config=AgentConfig(
            max_iterations=1,
            runtime_events_path=str(runtime_events_path),
        ),
        tool_definitions=[_tool_def("retrieve_tool_result"), _tool_def("exec_command")],
    )

    events = [event async for event in agent.run_turn("hello")]

    assert events
    logged = [
        json.loads(line)
        for line in runtime_events_path.read_text(encoding="utf-8").splitlines()
    ]
    schema_event = next(
        event for event in logged if event["feature"] == "provider_tool_schema"
    )
    assert schema_event["sent_to_provider"] is True
    assert schema_event["target_tool_visible"]["retrieve_tool_result"] is True
    assert "retrieve_tool_result" in schema_event["tool_names"]
    assert "schema_hash" in schema_event["target_schemas"]["retrieve_tool_result"]


@pytest.mark.asyncio
async def test_runtime_events_record_tokenjuice_projection_applied(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    def fake_reduce(**kwargs: Any) -> Any:
        return SimpleNamespace(
            inline_text="[tokenjuice]\nimportant failure",
            raw_chars=len(kwargs["content"]),
            reduced_chars=28,
            ratio=0.1,
            reducer="tests/pytest",
        )

    monkeypatch.setattr(agent_mod, "reduce_tool_result_with_tokenjuice", fake_reduce, raising=False)
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    agent = Agent(
        provider=_Provider(),
        config=AgentConfig(
            runtime_events_path=str(runtime_events_path),
            tool_result_fresh_diagnostic_inline_max_chars=1,
        ),
    )

    projected = await agent._canonicalize_tool_result(
        ToolResult(
            tool_use_id="tool-1",
            tool_name="exec_command",
            content="pytest output\n" + ("x" * 1000),
            is_error=True,
        ),
        tool_call=ToolCall(
            tool_use_id="tool-1",
            tool_name="exec_command",
            arguments={
                "command": "OPENROUTER_API_KEY=sk-or-v1-abcdefghijklmnopqrstuvwxyz pytest -q",
                "workdir": "/repo",
            },
        ),
    )

    assert projected.content == "[tokenjuice]\nimportant failure"
    logged = [
        json.loads(line)
        for line in runtime_events_path.read_text(encoding="utf-8").splitlines()
    ]
    projection_event = next(
        event for event in logged if event["feature"] == "tool_result_projection"
    )
    assert projection_event["outcome"] == "applied"
    assert projection_event["mechanism"] == "tokenjuice"
    assert projection_event["reducer"] == "tests/pytest"
    assert projection_event["tool_name"] == "exec_command"
    assert projection_event["command"] == "OPENROUTER_API_KEY=[REDACTED] pytest -q"
    assert projection_event["tool_arguments"] == {
        "command": "OPENROUTER_API_KEY=[REDACTED] pytest -q",
        "workdir": "/repo",
    }
    assert projection_event["saved_chars"] > 0


@pytest.mark.asyncio
async def test_projection_envelope_includes_retrieval_hint_and_search_hints(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    def fake_reduce(**kwargs: Any) -> Any:
        return SimpleNamespace(
            inline_text="[tokenjuice]\nFAILED tests/test_api.py::test_bad - AssertionError",
            raw_chars=len(kwargs["content"]),
            reduced_chars=64,
            ratio=0.01,
            reducer="tests/pytest",
        )

    monkeypatch.setattr(agent_mod, "reduce_tool_result_with_tokenjuice", fake_reduce, raising=False)
    agent = Agent(
        provider=_Provider(),
        config=AgentConfig(
            tool_result_store_dir=str(tmp_path / "tool-results"),
            tool_result_store_session_id="session-1",
            tool_result_store_session_key="agent:main:session-1",
            tool_result_store_agent_id="main",
            tool_result_fresh_diagnostic_inline_max_chars=1,
        ),
    )
    content = (
        "pytest output\n"
        "FAILED tests/test_api.py::test_bad - AssertionError: expected 1 == 2\n"
        + ("x" * 20_000)
    )

    projected = await agent._canonicalize_tool_result(
        ToolResult(
            tool_use_id="tool-1",
            tool_name="exec_command",
            content=content,
            is_error=True,
        )
    )

    assert "[tool_result_projection]" in projected.content
    assert "tool_result_handle:" in projected.content
    assert "retrieve_hint:" in projected.content
    assert "this result is incomplete" in projected.content
    assert "Do not infer omitted diagnostics" in projected.content
    assert "search_hints:" in projected.content
    assert "FAILED tests/test_api.py::test_bad" in projected.content
    assert list((tmp_path / "tool-results").rglob("meta.json"))


@pytest.mark.asyncio
async def test_tool_projection_noops_when_handle_envelope_would_grow_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    def fake_reduce(**kwargs: Any) -> Any:
        return SimpleNamespace(
            inline_text="short",
            raw_chars=len(kwargs["content"]),
            reduced_chars=5,
            ratio=0.1,
            reducer="generic/fallback",
        )

    monkeypatch.setattr(agent_mod, "reduce_tool_result_with_tokenjuice", fake_reduce, raising=False)
    agent = Agent(
        provider=_Provider(),
        config=AgentConfig(
            tool_result_store_dir=str(tmp_path / "tool-results"),
            tool_result_store_session_id="session-1",
            tool_result_store_session_key="agent:main:session-1",
            tool_result_store_agent_id="main",
        ),
    )
    result = ToolResult(
        tool_use_id="tool-1",
        tool_name="exec_command",
        content="small output",
    )

    projected = await agent._canonicalize_tool_result(result)

    assert projected is result
    assert projected.content == "small output"
    assert agent.config.metadata["tool_projection_noops"] == 1
    assert "tool_projection_applied" not in agent.config.metadata
    assert not list((tmp_path / "tool-results").rglob("meta.json"))


@pytest.mark.asyncio
async def test_full_trace_store_preserves_raw_snapshot_without_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        agent_mod,
        "reduce_tool_result_with_tokenjuice",
        lambda **_kwargs: None,
        raising=False,
    )
    agent = Agent(
        provider=_Provider(),
        config=AgentConfig(
            tool_result_store_dir=str(tmp_path / "tool-results"),
            tool_result_store_session_id="session-1",
            tool_result_store_session_key="agent:main:session-1",
            tool_result_store_agent_id="main",
            tool_result_store_full_trace=True,
        ),
    )
    result = ToolResult(
        tool_use_id="tool-1",
        tool_name="read_file",
        content="line 1\nline 2\n",
    )

    projected = await agent._canonicalize_tool_result(result)

    assert projected is result
    assert projected.content == "line 1\nline 2\n"
    assert "tool_result_handle:" not in projected.content
    metas = list((tmp_path / "tool-results").rglob("meta.json"))
    assert len(metas) == 1
    handle = json.loads(metas[0].read_text(encoding="utf-8"))["handle"]
    stored = ToolResultStore(tmp_path / "tool-results").read(handle, session_id="session-1")
    assert stored.content == "line 1\nline 2\n"
    assert agent.config.metadata["tool_result_store_writes"] == 1


@pytest.mark.asyncio
async def test_full_trace_store_reuses_snapshot_for_replayed_tool_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        agent_mod,
        "reduce_tool_result_with_tokenjuice",
        lambda **_kwargs: None,
        raising=False,
    )
    agent = Agent(
        provider=_Provider(),
        config=AgentConfig(
            tool_result_store_dir=str(tmp_path / "tool-results"),
            tool_result_store_session_id="session-1",
            tool_result_store_session_key="agent:main:session-1",
            tool_result_store_agent_id="main",
            tool_result_store_full_trace=True,
        ),
    )
    result = ToolResult(
        tool_use_id="tool-1",
        tool_name="exec_command",
        content="same replayed output\n",
    )

    first = await agent._canonicalize_tool_result(result)
    second = await agent._canonicalize_tool_result(result)

    assert first is result
    assert second is result
    metas = list((tmp_path / "tool-results").rglob("meta.json"))
    assert len(metas) == 1
    assert agent.config.metadata["tool_result_store_writes"] == 1
    assert agent.config.metadata["tool_result_store_cache_hits"] == 1


@pytest.mark.asyncio
async def test_tool_projection_noops_when_store_budget_rejects_raw_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    def fake_reduce(**kwargs: Any) -> Any:
        return SimpleNamespace(
            inline_text="[tokenjuice]\nshort summary",
            raw_chars=len(kwargs["content"]),
            reduced_chars=26,
            ratio=0.01,
            reducer="generic/fallback",
        )

    monkeypatch.setattr(agent_mod, "reduce_tool_result_with_tokenjuice", fake_reduce, raising=False)
    agent = Agent(
        provider=_Provider(),
        config=AgentConfig(
            tool_result_store_dir=str(tmp_path / "tool-results"),
            tool_result_store_session_id="session-1",
            tool_result_store_session_key="agent:main:session-1",
            tool_result_store_agent_id="main",
            tool_result_store_max_bytes=200,
        ),
    )
    rng = random.Random(0)
    content = "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(20_000))
    result = ToolResult(
        tool_use_id="tool-1",
        tool_name="exec_command",
        content=content,
    )

    projected = await agent._canonicalize_tool_result(result)

    assert projected is result
    assert projected.content == content
    assert agent.config.metadata["tool_result_store_skips"] == 1
    assert agent.config.metadata["tool_projection_noops"] == 1
    assert "tool_projection_applied" not in agent.config.metadata
    assert not list((tmp_path / "tool-results").rglob("meta.json"))


@pytest.mark.asyncio
async def test_large_compressible_projection_stores_retrievable_raw_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    def fake_reduce(**kwargs: Any) -> Any:
        return SimpleNamespace(
            inline_text="[tokenjuice]\nlarge log summary",
            raw_chars=len(kwargs["content"]),
            reduced_chars=30,
            ratio=0.001,
            reducer="generic/fallback",
        )

    monkeypatch.setattr(agent_mod, "reduce_tool_result_with_tokenjuice", fake_reduce, raising=False)
    max_bytes = 64_000
    agent = Agent(
        provider=_Provider(),
        config=AgentConfig(
            tool_result_store_dir=str(tmp_path / "tool-results"),
            tool_result_store_session_id="session-1",
            tool_result_store_session_key="agent:main:session-1",
            tool_result_store_agent_id="main",
            tool_result_store_max_bytes=max_bytes,
        ),
    )
    content = "ERROR huge log\n" + ("x" * (max_bytes * 2))
    result = ToolResult(
        tool_use_id="tool-1",
        tool_name="exec_command",
        content=content,
    )

    projected = await agent._canonicalize_tool_result(result)

    assert projected is not result
    assert "[tool_result_projection]" in projected.content
    assert "tool_result_handle:" in projected.content
    assert "large log summary" in projected.content
    handle = next(
        line.split(":", 1)[1].strip()
        for line in projected.content.splitlines()
        if line.startswith("tool_result_handle:")
    )
    stored = ToolResultStore(tmp_path / "tool-results").read(handle, session_id="session-1")
    assert stored.content == content
    assert stored.size_bytes > max_bytes
    assert stored.stored_size_bytes is not None
    assert stored.stored_size_bytes <= max_bytes
    assert stored.storage_encoding == "gzip+utf-8"
    assert agent.config.metadata["tool_projection_applied"] is True


@pytest.mark.asyncio
async def test_json_guard_projection_includes_retrievable_raw_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        agent_mod,
        "reduce_tool_result_with_tokenjuice",
        lambda **_kwargs: None,
        raising=False,
    )
    agent = Agent(
        provider=_Provider(),
        config=AgentConfig(
            tool_result_store_dir=str(tmp_path / "tool-results"),
            tool_result_store_session_id="session-1",
            tool_result_store_session_key="agent:main:session-1",
            tool_result_store_agent_id="main",
        ),
    )
    content = json.dumps(
        {
            "status": 200,
            "body": "x" * 20_001,
            "small": "kept",
        }
    )
    result = ToolResult(
        tool_use_id="tool-1",
        tool_name="http_request",
        content=content,
    )

    projected = await agent._canonicalize_tool_result(result)

    assert projected is not result
    assert "[tool_result_projection]" in projected.content
    assert "tool_result_handle:" in projected.content
    assert "retrieve_hint:" in projected.content
    assert "large_tool_result_field" in projected.content
    assert '"small": "kept"' in projected.content
    assert "x" * 20_001 not in projected.content
    handle = next(
        line.split(":", 1)[1].strip()
        for line in projected.content.splitlines()
        if line.startswith("tool_result_handle:")
    )
    stored = ToolResultStore(tmp_path / "tool-results").read(handle, session_id="session-1")
    assert stored.content == content
    assert agent.config.metadata["tool_projection_applied"] is True


@pytest.mark.asyncio
async def test_json_guard_preserves_raw_content_when_store_budget_rejects_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        agent_mod,
        "reduce_tool_result_with_tokenjuice",
        lambda **_kwargs: None,
        raising=False,
    )
    agent = Agent(
        provider=_Provider(),
        config=AgentConfig(
            tool_result_store_dir=str(tmp_path / "tool-results"),
            tool_result_store_session_id="session-1",
            tool_result_store_session_key="agent:main:session-1",
            tool_result_store_agent_id="main",
            tool_result_store_max_bytes=200,
        ),
    )
    rng = random.Random(1)
    body = "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(25_000))
    content = json.dumps({"status": 200, "body": body, "small": "kept"})
    result = ToolResult(
        tool_use_id="tool-1",
        tool_name="http_request",
        content=content,
    )

    projected = await agent._canonicalize_tool_result(result)

    assert projected is result
    assert projected.content == content
    assert body in projected.content
    assert "large_tool_result_field" not in projected.content
    assert agent.config.metadata["tool_result_store_skips"] == 1
    assert agent.config.metadata["tool_projection_noops"] == 1
    assert "tool_projection_applied" not in agent.config.metadata


@pytest.mark.asyncio
async def test_approval_retry_clears_stale_tool_result_projection() -> None:
    reset_approval_queue()
    approval_id = get_approval_queue().request(
        "exec",
        {
            "toolName": "exec_command",
            "command": "pytest -q",
            "args": {"command": "pytest -q", "workdir": "/repo"},
        },
    )
    get_approval_queue().resolve(approval_id, True)
    approval_payload = json.dumps(
        {
            "status": "approval_required",
            "approval_id": approval_id,
            "message": "Approve this command.",
            "lines": [str(index) for index in range(80)],
        },
        indent=2,
    )
    calls = 0

    async def handler(tool_call: ToolCall) -> ToolResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ToolResult(
                tool_use_id=tool_call.tool_use_id,
                tool_name=tool_call.tool_name,
                content=approval_payload,
            )
        assert tool_call.continuation is not None
        assert tool_call.continuation.approval_id == approval_id
        return ToolResult(
            tool_use_id=tool_call.tool_use_id,
            tool_name=tool_call.tool_name,
            content="FINAL_OK",
        )

    provider = _ToolCallingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(context_window_tokens=1_000_000, max_iterations=2),
        tool_definitions=[_tool_def("exec_command")],
        tool_handler=handler,
    )

    try:
        events = [event async for event in agent.run_turn("run risky command")]
    finally:
        reset_approval_queue()

    assert calls == 2
    assert len(provider.calls) == 2
    second_call_tool_result = _last_tool_result_content(provider.calls[1])
    assert second_call_tool_result == "FINAL_OK"
    tool_result_events = [event for event in events if isinstance(event, ToolResultEvent)]
    approval_event_payload = json.loads(tool_result_events[0].result)
    assert approval_event_payload["status"] == "approval_required"
    assert approval_event_payload["approval_id"] == approval_id
    assert tool_result_events[-1].result == "FINAL_OK"


def test_python_backend_reduces_docker_build_output() -> None:
    output = "\n".join(
        [
            "#1 [internal] load build definition from Dockerfile",
            "#1 sha256:1234",
            "#1 DONE 0.1s",
            "#2 [2/3] RUN pnpm install",
            "#2 1.234 lots of progress",
            "#2 ERROR: process exited with code 1",
            "ERROR: failed to solve: process exited with code 1",
        ]
    )

    result = backend_reduce_tool_result(
        tool_name="exec_command",
        tool_use_id="tool-1",
        command="docker build .",
        content=output,
        is_error=True,
        max_inline_chars=600,
    )

    assert result is not None
    assert result.reducer == "devops/docker-build"
    assert "ERROR: failed to solve" in result.inline_text
    assert "sha256:1234" not in result.inline_text


def test_python_backend_generic_fallback_head_tail() -> None:
    output = "\n".join(f"line {index}" for index in range(60))

    result = backend_reduce_tool_result(
        tool_name="exec_command",
        tool_use_id="tool-1",
        command="custom-tool --verbose",
        content=output,
        is_error=False,
        max_inline_chars=400,
    )

    assert result is not None
    assert result.reducer == "generic/fallback"
    assert "line 0" in result.inline_text
    assert "line 59" in result.inline_text
    assert "omitted" in result.inline_text


def test_typescript_runtime_directory_is_not_present() -> None:
    from pathlib import Path

    assert not (Path(__file__).resolve().parents[2] / "src/opensquilla/tokenjuice_runtime").exists()


@pytest.mark.asyncio
async def test_agent_preserves_signature_only_thinking_block_for_next_provider_call() -> None:
    async def handler(tool_call: ToolCall) -> ToolResult:
        return ToolResult(
            tool_use_id=tool_call.tool_use_id,
            tool_name=tool_call.tool_name,
            content="ok",
        )

    provider = _SignatureOnlyToolCallingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(context_window_tokens=1_000_000, max_iterations=2),
        tool_definitions=[_tool_def("exec_command")],
        tool_handler=handler,
    )

    _events = [event async for event in agent.run_turn("run tests")]

    assert len(provider.calls) == 2
    assistant_messages = [message for message in provider.calls[1] if message.role == "assistant"]
    assert assistant_messages
    thinking_blocks = [
        block
        for message in assistant_messages
        for block in message.content
        if isinstance(block, ContentBlockThinking)
    ]
    assert thinking_blocks
    assert thinking_blocks[0].thinking == ""
    assert thinking_blocks[0].signature == "gemini-signature-only"
