from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult, ToolResultEvent
from opensquilla.engine.history import limit_turns
from opensquilla.engine.session_sanitize import (
    project_historical_tool_payloads,
    recoverable_tool_result_reference,
    sanitize_session_messages,
)
from opensquilla.engine.tool_result_store import ToolResultStore
from opensquilla.engine.types import ThinkingLevel
from opensquilla.memory.session_flush import _usage_from_complete_response
from opensquilla.provider import (
    ChatConfig,
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    ModelCapabilities,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import (
    DoneEvent as ProviderDone,
)
from opensquilla.provider import (
    TextDeltaEvent as ProviderText,
)
from opensquilla.provider import (
    ToolUseEndEvent as ProviderToolUseEnd,
)
from opensquilla.provider import (
    ToolUseStartEvent as ProviderToolUseStart,
)
from opensquilla.provider.types import ContentBlockImage


def _tool_definition(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Mock {name} tool.",
        input_schema=ToolInputSchema(properties={}, required=[]),
    )


def _retrieval_tool_definition() -> ToolDefinition:
    return _tool_definition("retrieve_tool_result")


async def _unused_retrieval_handler(call: Any) -> ToolResult:
    raise AssertionError(f"unexpected tool call: {call.tool_name}")


def _recoverable_projection_agent(
    tmp_path: Any,
    *,
    provider: Any | None = None,
    tool_handler: Any = _unused_retrieval_handler,
    tool_names: tuple[str, ...] = (),
    **config_overrides: Any,
) -> Agent:
    store_dir = tmp_path / "tool-results"

    async def recovery_capable_handler(call: Any) -> ToolResult:
        if call.tool_name == "retrieve_tool_result":
            record = ToolResultStore(store_dir).read(
                call.arguments["handle"],
                session_id="session-1",
            )
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content=record.content,
            )
        return await tool_handler(call)

    setattr(
        recovery_capable_handler,
        "_opensquilla_available_tools",
        frozenset({"retrieve_tool_result", *tool_names}),
    )
    return Agent(
        provider=provider or CapturingProvider(),
        config=AgentConfig(
            tool_result_store_dir=str(store_dir),
            tool_result_store_session_id="session-1",
            tool_result_store_session_key="agent:main:session-1",
            tool_result_store_agent_id="main",
            **config_overrides,
        ),
        tool_definitions=[
            *(_tool_definition(name) for name in tool_names),
            _retrieval_tool_definition(),
        ],
        tool_handler=recovery_capable_handler,
    )


def test_agent_config_disables_tool_argument_projection_by_default() -> None:
    assert AgentConfig().tool_use_argument_projection_enabled is False


class CapturingProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append({"messages": messages, "tools": tools, "config": config})
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderText(text="ok")
        yield ProviderDone(stop_reason="end_turn", input_tokens=3, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class StaticCostProvider(CapturingProvider):
    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderText(text="ok")
        yield ProviderDone(
            stop_reason="end_turn",
            input_tokens=1000,
            output_tokens=1000,
            billed_cost=0.0,
            model="deepseek-v4-flash",
        )


class ToolLoopCapturingProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        call_number = len(self.calls) + 1
        self.calls.append({"messages": messages, "tools": tools, "config": config})
        return self._stream(call_number)

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderToolUseStart(tool_use_id="tool-1", tool_name="echo")
            yield ProviderToolUseEnd(
                tool_use_id="tool-1",
                tool_name="echo",
                arguments={"value": "ok"},
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1)
            return
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="end_turn", input_tokens=4, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class ReasoningToolLoopCapturingProvider(ToolLoopCapturingProvider):
    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderToolUseStart(tool_use_id="tool-1", tool_name="echo")
            yield ProviderToolUseEnd(
                tool_use_id="tool-1",
                tool_name="echo",
                arguments={"value": "ok"},
            )
            yield ProviderDone(
                stop_reason="tool_use",
                input_tokens=3,
                output_tokens=1,
                reasoning_content="I should call echo before finalizing.",
            )
            return
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="end_turn", input_tokens=4, output_tokens=1)


class LargeArgumentToolLoopCapturingProvider(ToolLoopCapturingProvider):
    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderToolUseStart(tool_use_id="tool-1", tool_name="execute_code")
            yield ProviderToolUseEnd(
                tool_use_id="tool-1",
                tool_name="execute_code",
                arguments={"code": self.code, "timeout": 10},
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1)
            return
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="end_turn", input_tokens=4, output_tokens=1)


class CopiedProjectionToolLoopCapturingProvider(LargeArgumentToolLoopCapturingProvider):
    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            async for event in super()._stream(call_number):
                yield event
            return
        if call_number == 2:
            projection = self._legacy_projected_code_argument()
            yield ProviderToolUseStart(tool_use_id="tool-2", tool_name="execute_code")
            yield ProviderToolUseEnd(
                tool_use_id="tool-2",
                tool_name="execute_code",
                arguments={"code": projection, "timeout": 99},
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1)
            return
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="end_turn", input_tokens=4, output_tokens=1)

    def _legacy_projected_code_argument(self) -> str:
        return (
            "[tool_use_argument_projection]\n"
            "tool: execute_code\n"
            "tool_use_id: tool-1\n"
            "field: code\n"
            f"original_chars: {len(self.code)}\n"
            f"original_input_chars: {len(self.code)}\n"
            "sha256: " + hashlib.sha256(self.code.encode("utf-8")).hexdigest() + "\n"
            "tool_argument_handle: tr-1234567890abcdef1234567890abcdef\n"
            "omitted_chars: 123\n"
            "reason: legacy test marker\n"
        )


class CompactedToolArgumentsProvider(ToolLoopCapturingProvider):
    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderToolUseStart(tool_use_id="tool-compact", tool_name="exec_command")
            yield ProviderToolUseEnd(
                tool_use_id="tool-compact",
                tool_name="exec_command",
                arguments={
                    "_opensquilla_compacted_tool_arguments": True,
                    "original_chars": 549,
                    "sha256": "0" * 64,
                    "argument_keys": ["command", "timeout"],
                },
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1)
            return
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="end_turn", input_tokens=4, output_tokens=1)


class TextThenCompactedToolArgumentsProvider(ToolLoopCapturingProvider):
    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderText(text="I will prepare the file, then run the tool.")
            yield ProviderToolUseStart(tool_use_id="tool-compact", tool_name="write_file")
            yield ProviderToolUseEnd(
                tool_use_id="tool-compact",
                tool_name="write_file",
                arguments={
                    "_opensquilla_compacted_tool_arguments": True,
                    "tool": "write_file",
                    "reason": "provider_context_omitted",
                },
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1)
            return
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="end_turn", input_tokens=4, output_tokens=1)


class ProviderRequestCompactedArgumentStringProvider(ToolLoopCapturingProvider):
    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderToolUseStart(
                tool_use_id="tool-provider-request",
                tool_name="exec_command",
            )
            yield ProviderToolUseEnd(
                tool_use_id="tool-provider-request",
                tool_name="exec_command",
                arguments={
                    "command": (
                        "[provider_request_tool_input_compacted: original_chars=987; "
                        "sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa]"
                    ),
                },
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1)
            return
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="end_turn", input_tokens=4, output_tokens=1)


class MidStringCompactedArgumentStringProvider(ToolLoopCapturingProvider):
    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderToolUseStart(
                tool_use_id="tool-mid-string",
                tool_name="edit_file",
            )
            yield ProviderToolUseEnd(
                tool_use_id="tool-mid-string",
                tool_name="edit_file",
                arguments={
                    "path": "src/example.py",
                    "old_text": (
                        "def load(self):\n"
                        "    [provider_request_tool_result_compacted: omitted 4210 "
                        "chars; original_chars=4655; sha256="
                        + "b" * 64
                        + "]\n\n    return data\n"
                    ),
                    "new_text": "def load(self):\n    return data\n",
                },
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1)
            return
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="end_turn", input_tokens=4, output_tokens=1)


class CapturingTurnLog:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, kind: str, payload: dict[str, Any]) -> None:
        self.records.append({"kind": kind, "payload": payload})


def test_session_sanitize_strips_block_metadata_without_compressing_content() -> None:
    message = Message.model_construct(
        role="user",
        content=[
            {
                "type": "tool_result",
                "tool_use_id": "call_1",
                "content": "result text with details that must remain factual",
                "is_error": False,
                "details": {"raw_provider": "debug-only"},
                "timestamp": "2026-04-28T14:35:00Z",
            }
        ],
        reasoning_content=None,
    )

    sanitized, result = sanitize_session_messages([message])

    assert result.metadata_keys_removed == 2
    block = sanitized[0].content[0]
    assert isinstance(block, ContentBlockToolResult)
    assert block.content == "result text with details that must remain factual"
    assert "details" not in block.model_dump(mode="json")
    assert "timestamp" not in block.model_dump(mode="json")


def test_historical_replay_projection_preserves_raw_tool_result_without_handle() -> None:
    large_argument = "STALE_ARGUMENT_START\n" + ("x" * 6000)
    large_result = "STALE_RESULT_START\n" + ("y" * 6000)
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="write-1",
                    name="write_file",
                    input={"path": "index.html", "content": large_argument},
                )
            ],
            reasoning_content="hidden reasoning\n" + ("r" * 4000),
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="write-1",
                    content=large_result,
                    is_error=False,
                )
            ],
        ),
    ]

    projected, result = project_historical_tool_payloads(messages)

    assert result.tool_uses_projected == 1
    assert result.tool_results_projected == 0
    assert result.reasoning_chars_removed > 0
    assert projected[0].reasoning_content is None
    tool_use = projected[0].content[0]
    assert isinstance(tool_use, ContentBlockToolUse)
    assert tool_use.input["path"] == "index.html"
    assert tool_use.input["content"].startswith("[historical_tool_argument_omitted]\n")
    assert large_argument not in tool_use.input["content"]
    tool_result = projected[1].content[0]
    assert isinstance(tool_result, ContentBlockToolResult)
    assert tool_result.content == large_result
    assert messages[0].reasoning_content is not None
    assert messages[0].content[0].input["content"] == large_argument


def test_historical_replay_projection_preserves_tool_result_projection_envelope() -> None:
    handle = "tr-" + ("a" * 32)
    projection = (
        "[tool_result_projection]\n"
        f"tool_result_handle: {handle}\n"
        "sha256: " + ("a" * 64) + "\n"
        "original_chars: 50000\n"
        "preview_complete: false\n"
        "retrieve_hint: use retrieve_tool_result with this tool_result_handle.\n"
        + ("body line before hints\n" * 80)
        + "search_hints:\n"
        "- L42: FAILED tests/test_api.py::test_bad - AssertionError\n"
        + ("large projected body\n" * 2000)
    )
    messages = [
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="tool-1",
                    content=projection,
                    is_error=True,
                )
            ],
        )
    ]

    projected, result = project_historical_tool_payloads(
        messages,
        recoverable_references=frozenset({(handle, "a" * 64)}),
    )

    assert result.tool_results_projected == 1
    tool_result = projected[0].content[0]
    assert isinstance(tool_result, ContentBlockToolResult)
    assert isinstance(tool_result.content, str)
    assert tool_result.content.startswith("[tool_result_projection]\n")
    assert f"tool_result_handle: {handle}" in tool_result.content
    assert "sha256: " + ("a" * 64) in tool_result.content
    assert "original_chars: 50000" in tool_result.content
    assert "preview_complete: false" in tool_result.content
    assert "retrieve_hint:" in tool_result.content
    assert "search_hints:" in tool_result.content
    assert "FAILED tests/test_api.py::test_bad" in tool_result.content
    assert "[historical_tool_result_projection_body_compacted]" in tool_result.content
    assert tool_result.content.count("large projected body") < 50


def test_historical_replay_projection_requires_verified_reference() -> None:
    handle = "tr-" + ("a" * 32)
    sha256 = "a" * 64
    projection = (
        "[tool_result_projection]\n"
        f"tool_result_handle: {handle}\n"
        f"sha256: {sha256}\n"
        "retrieve_hint: use retrieve_tool_result.\n"
        + ("unverified projected body\n" * 500)
    )
    messages = [
        Message(
            role="user",
            content=[ContentBlockToolResult(tool_use_id="tool-1", content=projection)],
        )
    ]

    without_proof, without_proof_result = project_historical_tool_payloads(messages)
    wrong_digest, wrong_digest_result = project_historical_tool_payloads(
        messages,
        recoverable_references=frozenset({(handle, "b" * 64)}),
    )

    assert without_proof is messages
    assert without_proof_result.tool_results_projected == 0
    assert wrong_digest is messages
    assert wrong_digest_result.tool_results_projected == 0


def test_agent_verifies_historical_references_against_session_store(tmp_path) -> None:
    agent = _recoverable_projection_agent(tmp_path)
    store = ToolResultStore(tmp_path / "tool-results")
    valid = store.write(
        "valid raw output",
        tool_use_id="valid",
        tool_name="exec_command",
        session_id="session-1",
        session_key="agent:main:session-1",
        agent_id="main",
    )
    same_session_other_writer = store.write(
        "other agent raw output",
        tool_use_id="same-session-other-writer",
        tool_name="exec_command",
        session_id="session-1",
        session_key="agent:other:session-1",
        agent_id="other",
    )

    def envelope(handle: str, sha256: str, marker: str) -> str:
        return (
            "[tool_result_projection]\n"
            f"tool_result_handle: {handle}\n"
            f"sha256: {sha256}\n"
            "retrieve_hint: use retrieve_tool_result.\n"
            + ((marker + "\n") * 500)
        )

    contents = {
        "valid": envelope(valid.handle, valid.sha256, "valid projected body"),
        "same-session-other-writer": envelope(
            same_session_other_writer.handle,
            same_session_other_writer.sha256,
            "same session projected body",
        ),
        "stale": envelope("tr-" + ("f" * 32), "f" * 64, "stale projected body"),
        "forged": envelope(valid.handle, "0" * 64, "forged projected body"),
    }
    messages = [
        Message(
            role="user",
            content=[ContentBlockToolResult(tool_use_id=tool_use_id, content=content)],
        )
        for tool_use_id, content in contents.items()
    ]

    verified = agent._verified_tool_result_references(messages)
    projected, result = project_historical_tool_payloads(
        messages,
        recoverable_references=verified,
    )

    assert verified == frozenset(
        {
            (valid.handle, valid.sha256),
            (same_session_other_writer.handle, same_session_other_writer.sha256),
        }
    )
    assert result.tool_results_projected == 2
    projected_by_id = {
        block.tool_use_id: block.content
        for message in projected
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolResult)
    }
    assert projected_by_id["valid"] != contents["valid"]
    assert (
        projected_by_id["same-session-other-writer"]
        != contents["same-session-other-writer"]
    )
    for tool_use_id in ("stale", "forged"):
        assert projected_by_id[tool_use_id] == contents[tool_use_id]


def test_historical_reference_verification_bounds_unique_store_reads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    agent = _recoverable_projection_agent(tmp_path)
    references: dict[str, str] = {}
    messages: list[Message] = []
    for index in range(300):
        handle = f"tr-{index:032x}"
        sha256 = f"{index:064x}"
        references[handle] = sha256
        messages.append(
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(
                        tool_use_id=f"tool-{index}",
                        content=(
                            "[tool_result_projection]\n"
                            f"tool_result_handle: {handle}\n"
                            f"sha256: {sha256}\n"
                            "retrieve_hint: use retrieve_tool_result.\n"
                        ),
                    )
                ],
            )
        )

    reads: list[str] = []

    def fake_read(
        _store: ToolResultStore,
        handle: str,
        *,
        session_id: str,
    ) -> Any:
        reads.append(handle)
        return SimpleNamespace(
            session_id=session_id,
            sha256=references[handle],
        )

    monkeypatch.setattr(ToolResultStore, "read", fake_read)

    verified = agent._verified_tool_result_references(messages)

    assert len(reads) == 256
    assert len(verified) == 256
    assert reads[0] == "tr-0000000000000000000000000000012b"


@pytest.mark.asyncio
async def test_run_turn_verifies_only_retained_history_turns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    provider = CapturingProvider()
    agent = _recoverable_projection_agent(
        tmp_path,
        provider=provider,
        max_history_turns=1,
        max_iterations=1,
        flush_enabled=False,
    )
    agent.set_history(
        [
            Message(role="user", content="old user turn"),
            Message(role="assistant", content="old response"),
            Message(role="user", content="retained user turn"),
            Message(role="assistant", content="retained response"),
        ]
    )
    verified_messages: list[Message] = []

    def capture_verification(messages: list[Message]) -> frozenset[tuple[str, str]]:
        verified_messages.extend(messages)
        return frozenset()

    monkeypatch.setattr(agent, "_verified_tool_result_references", capture_verification)

    events = [event async for event in agent.run_turn("continue")]

    assert any(event.kind == "done" for event in events)
    assert [message.content for message in verified_messages] == [
        "retained user turn",
        "retained response",
    ]


def _deep_recoverable_reference_candidate(*, depth: int = 10_000) -> str:
    return (
        '{"result_truncated":true,"retrieve_hint":"retrieve raw output",'
        '"tool_result_handle":"tr-'
        + ("a" * 32)
        + '","tool_result_sha256":"'
        + ("b" * 64)
        + '","nested":'
        + ("[" * depth)
        + "0"
        + ("]" * depth)
        + "}"
    )


def test_recoverable_tool_result_reference_prefilters_unmarked_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_json_loads(_content: str) -> Any:
        raise AssertionError("unmarked JSON must not reach json.loads")

    monkeypatch.setattr(json, "loads", unexpected_json_loads)

    assert recoverable_tool_result_reference('{"nested":{"value":1}}') is None


def test_recoverable_tool_result_reference_rejects_deep_marked_json() -> None:
    assert (
        recoverable_tool_result_reference(_deep_recoverable_reference_candidate())
        is None
    )


def test_recoverable_tool_result_reference_accepts_structured_envelope() -> None:
    handle = "tr-" + ("a" * 32)
    sha256 = "b" * 64
    envelope = json.dumps(
        {
            "result_truncated": True,
            "retrieve_hint": "retrieve raw output",
            "tool_result_handle": handle,
            "tool_result_sha256": sha256,
        },
        indent=2,
    )

    assert recoverable_tool_result_reference(f"\n{envelope}\t") == (handle, sha256)


@pytest.mark.asyncio
async def test_agent_run_turn_ignores_deep_historical_json_reference(tmp_path) -> None:
    provider = CapturingProvider()
    agent = _recoverable_projection_agent(
        tmp_path,
        provider=provider,
        max_iterations=1,
        flush_enabled=False,
    )
    agent.set_history(
        [
            Message(
                role="assistant",
                content=[
                    ContentBlockToolUse(id="deep-json", name="exec_command", input={})
                ],
            ),
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(
                        tool_use_id="deep-json",
                        content=_deep_recoverable_reference_candidate(),
                    )
                ],
            ),
        ]
    )

    events = [event async for event in agent.run_turn("continue")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    "handle_line",
    [
        "",
        "tool_result_handle: tr-abc123\n",
        "tool_result_handle: tr-" + ("g" * 32) + "\n",
    ],
)
def test_historical_replay_projection_preserves_unrecoverable_envelope(
    handle_line: str,
) -> None:
    projection = (
        "[tool_result_projection]\n"
        + handle_line
        + "retrieve_hint: use retrieve_tool_result.\n"
        + ("unrecoverable projected body\n" * 500)
    )
    messages = [
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="tool-1",
                    content=projection,
                    is_error=False,
                )
            ],
        )
    ]

    projected, result = project_historical_tool_payloads(messages)

    assert result.tool_results_projected == 0
    assert projected is messages
    tool_result = projected[0].content[0]
    assert isinstance(tool_result, ContentBlockToolResult)
    assert tool_result.content == projection


def test_historical_replay_projection_compacts_nested_tool_payloads() -> None:
    large_content = "CONTENT_START\n" + ("c" * 3000)
    large_nested = {"blob": "NESTED_START\n" + ("n" * 20_000)}
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="nested-1",
                    name="write_file",
                    input={
                        "path": "index.html",
                        "content": large_content,
                        "metadata": large_nested,
                    },
                )
            ],
        )
    ]

    projected, result = project_historical_tool_payloads(messages)

    assert result.tool_uses_projected == 1
    tool_use = projected[0].content[0]
    assert isinstance(tool_use, ContentBlockToolUse)
    assert tool_use.input["path"] == "index.html"
    assert tool_use.input["content"].startswith("[historical_tool_argument_omitted]\n")
    assert tool_use.input["metadata"].startswith("[historical_tool_argument_omitted]\n")
    payload = json.dumps(tool_use.input, ensure_ascii=False)
    assert "c" * 1000 not in payload
    assert "n" * 1000 not in payload
    assert messages[0].content[0].input["metadata"] == large_nested


def test_historical_replay_projection_preserves_list_tool_results_without_handle() -> None:
    large_result = [{"type": "text", "text": "LIST_RESULT_START\n" + ("y" * 8000)}]
    messages = [
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="list-result-1",
                    content=large_result,
                    is_error=False,
                )
            ],
        )
    ]

    projected, result = project_historical_tool_payloads(messages)

    assert result.tool_results_projected == 0
    tool_result = projected[0].content[0]
    assert isinstance(tool_result, ContentBlockToolResult)
    assert tool_result.content == large_result
    assert messages[0].content[0].content == large_result


def test_agent_aggregate_tool_result_budget_preserves_raw_without_recovery() -> None:
    raw_old_output = "old bulky output\n" + ("x" * 4000)
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(
            context_window_tokens=200,
        ),
    )
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(id="old-1", name="execute_code", input={}),
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="old-1",
                    content=raw_old_output,
                    is_error=False,
                )
            ],
        ),
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(id="err-1", name="execute_code", input={}),
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="err-1",
                    content="Traceback\n" + ("e" * 4000),
                    is_error=True,
                )
            ],
        ),
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(id="new-1", name="execute_code", input={}),
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="new-1",
                    content="recent output\n" + ("r" * 4000),
                    is_error=False,
                )
            ],
        ),
    ]

    compacted = agent._compact_aggregate_tool_results_for_provider(messages)

    old_result = compacted[1].content[0]
    err_result = compacted[3].content[0]
    new_result = compacted[5].content[0]
    assert isinstance(old_result, ContentBlockToolResult)
    assert isinstance(err_result, ContentBlockToolResult)
    assert isinstance(new_result, ContentBlockToolResult)
    assert old_result.content == raw_old_output
    assert "Traceback" in err_result.content
    assert len(err_result.content) > 4000
    assert "recent output" in new_result.content
    assert len(new_result.content) > 4000
    assert "tool_aggregate_projection_applied" not in agent.config.metadata
    assert "tool_projection_applied" not in agent.config.metadata


def test_agent_large_context_compacts_old_local_tool_results_for_provider(
    tmp_path,
) -> None:
    def _tool_pair(tool_id: str, body: str, *, is_error: bool = False) -> list[Message]:
        return [
            Message(
                role="assistant",
                content=[ContentBlockToolUse(id=tool_id, name="local_tool", input={})],
            ),
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(
                        tool_use_id=tool_id,
                        content=body,
                        is_error=is_error,
                    )
                ],
            ),
        ]

    agent = _recoverable_projection_agent(
        tmp_path,
        context_window_tokens=200_000,
    )
    messages = [
        block
        for pair in (
            _tool_pair("old-1", "old local output\n" + ("x" * 70_000)),
            _tool_pair(
                "err-1",
                "Traceback preserved\n" + ("e" * 20_000),
                is_error=True,
            ),
            _tool_pair("mid-1", "middle local output\n" + ("m" * 50_000)),
            _tool_pair("new-1", "recent local output\n" + ("r" * 50_000)),
        )
        for block in pair
    ]

    compacted = agent._compact_aggregate_tool_results_for_provider(messages)

    old_result = compacted[1].content[0]
    error_result = compacted[3].content[0]
    middle_result = compacted[5].content[0]
    recent_result = compacted[7].content[0]
    assert isinstance(old_result, ContentBlockToolResult)
    assert isinstance(error_result, ContentBlockToolResult)
    assert isinstance(middle_result, ContentBlockToolResult)
    assert isinstance(recent_result, ContentBlockToolResult)
    assert "[tool_result_projection]" in old_result.content
    assert "tool_result_handle: tr-" in old_result.content
    assert len(old_result.content) < 5_000
    assert "Traceback preserved" in error_result.content
    assert len(error_result.content) > 20_000
    assert "middle local output" in middle_result.content
    assert "recent local output" in recent_result.content
    total_result_chars = sum(
        len(block.content)
        for message in compacted
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolResult)
    )
    assert total_result_chars <= agent._tool_result_provider_request_max_chars()
    assert agent.config.metadata["tool_provider_guard_projection_applied"] is True


@pytest.mark.asyncio
async def test_agent_single_tool_result_projection_stores_recoverable_raw_content(
    tmp_path,
) -> None:
    agent = _recoverable_projection_agent(
        tmp_path,
        context_window_tokens=200,
    )
    raw_output = "single bulky output\n" + ("x" * 8000)

    messages = [
        Message(
            role="assistant",
            content=[ContentBlockToolUse(id="tool-1", name="execute_code", input={})],
        ),
        Message(
            role="user",
            content=[ContentBlockToolResult(tool_use_id="tool-1", content=raw_output)],
        ),
    ]

    projected = agent._compact_aggregate_tool_results_for_provider(messages)
    result = projected[1].content[0]
    assert isinstance(result, ContentBlockToolResult)

    assert "[tool_result_projection]" in result.content
    assert "tool_result_handle: tr-" in result.content
    assert raw_output not in result.content
    stored_contents = list((tmp_path / "tool-results").rglob("content.txt"))
    assert len(stored_contents) == 1
    assert stored_contents[0].read_text() == raw_output


@pytest.mark.parametrize(
    ("has_store", "has_schema", "has_handler"),
    [
        (False, True, True),
        (True, False, True),
        (True, True, False),
    ],
)
def test_agent_tool_result_projection_preserves_raw_when_recovery_is_incomplete(
    tmp_path,
    has_store: bool,
    has_schema: bool,
    has_handler: bool,
) -> None:
    config_kwargs: dict[str, Any] = {"context_window_tokens": 200}
    if has_store:
        config_kwargs.update(
            tool_result_store_dir=str(tmp_path / "tool-results"),
            tool_result_store_session_id="session-1",
            tool_result_store_session_key="agent:main:session-1",
            tool_result_store_agent_id="main",
        )
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(**config_kwargs),
        tool_definitions=[_retrieval_tool_definition()] if has_schema else None,
        tool_handler=_unused_retrieval_handler if has_handler else None,
    )
    raw_output = "single bulky output\n" + ("x" * 8000)

    messages = [
        Message(
            role="assistant",
            content=[ContentBlockToolUse(id="tool-1", name="execute_code", input={})],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="tool-1",
                    content=raw_output,
                )
            ],
        ),
    ]

    projected = agent._compact_aggregate_tool_results_for_provider(messages)
    result = projected[1].content[0]
    assert isinstance(result, ContentBlockToolResult)

    assert result.content == raw_output
    assert "[tool_result_projection]" not in result.content
    assert not list((tmp_path / "tool-results").rglob("content.txt"))


def test_agent_provider_backstop_preserves_historical_read_file_source_context(
    tmp_path,
) -> None:
    agent = _recoverable_projection_agent(
        tmp_path,
        context_window_tokens=1_000_000,
        tool_result_provider_request_max_chars=1000,
    )
    source = "\n".join(f"{index}: important implementation detail" for index in range(600))
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="read-1",
                    name="read_file",
                    input={"path": "src/lib.rs"},
                )
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="read-1",
                    content=source,
                    is_error=False,
                )
            ],
        ),
    ]

    compacted = agent._compact_aggregate_tool_results_for_provider(messages)
    result = compacted[1].content[0]
    assert isinstance(result, ContentBlockToolResult)
    assert result.content == source
    assert "[tool_result_projection]" not in result.content
    assert agent.config.metadata["tool_provider_projection_semantic_preserves"] == 1
    assert "tool_provider_guard_projection_applied" not in agent.config.metadata


def test_agent_provider_backstop_preserves_historical_exec_source_context(
    tmp_path,
) -> None:
    agent = _recoverable_projection_agent(
        tmp_path,
        context_window_tokens=1_000_000,
        tool_result_provider_request_max_chars=1000,
    )
    source = "\n".join(f"{index}: fn important_{index}() {{}}" for index in range(500))
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="exec-1",
                    name="exec_command",
                    input={"command": "sed -n '1,500p' src/lib.rs"},
                )
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="exec-1",
                    content=source,
                    is_error=False,
                )
            ],
        ),
    ]

    compacted = agent._compact_aggregate_tool_results_for_provider(messages)
    result = compacted[1].content[0]
    assert isinstance(result, ContentBlockToolResult)
    assert result.content == source
    assert "[tool_result_projection]" not in result.content
    assert agent.config.metadata["tool_provider_projection_semantic_preserves"] == 1
    assert "tool_provider_guard_projection_applied" not in agent.config.metadata


def test_agent_aggregate_tool_result_budget_uses_total_not_single_result_size(
    tmp_path,
) -> None:
    agent = _recoverable_projection_agent(
        tmp_path,
        context_window_tokens=1200,
    )
    messages: list[Message] = []
    for index in range(5):
        tool_id = f"tool-{index}"
        messages.extend(
            [
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(id=tool_id, name="execute_code", input={}),
                    ],
                ),
                Message(
                    role="user",
                    content=[
                        ContentBlockToolResult(
                            tool_use_id=tool_id,
                            content=f"chunk {index}\n" + ("x" * 800),
                            is_error=False,
                        )
                    ],
                ),
            ]
        )

    compacted = agent._compact_aggregate_tool_results_for_provider(messages)

    compacted_contents = [
        message.content[0].content
        for message in compacted
        if isinstance(message.content, list)
        and message.content
        and isinstance(message.content[0], ContentBlockToolResult)
    ]
    assert any("aggregate_tool_result_compacted" in content for content in compacted_contents)
    assert "recent output" not in "\n".join(compacted_contents)


def test_agent_aggregate_projection_preserves_historical_read_file_source_context(
    tmp_path,
) -> None:
    agent = _recoverable_projection_agent(
        tmp_path,
        context_window_tokens=1200,
        tool_result_provider_request_max_chars=1_000_000,
    )
    source = "\n".join(f"{index}: important source line" for index in range(500))
    messages: list[Message] = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="read-1",
                    name="read_file",
                    input={"path": "src/lib.rs"},
                )
            ],
        ),
        Message(
            role="user",
            content=[ContentBlockToolResult(tool_use_id="read-1", content=source)],
        ),
    ]
    for index in range(5):
        tool_id = f"tool-{index}"
        messages.extend(
            [
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(id=tool_id, name="execute_code", input={}),
                    ],
                ),
                Message(
                    role="user",
                    content=[
                        ContentBlockToolResult(
                            tool_use_id=tool_id,
                            content=f"chunk {index}\n" + ("x" * 800),
                            is_error=False,
                        )
                    ],
                ),
            ]
        )

    compacted = agent._compact_aggregate_tool_results_for_provider(messages)
    projected_contents = [
        block.content
        for message in compacted
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolResult)
    ]

    assert projected_contents[0] == source
    assert "[tool_result_projection]" not in projected_contents[0]
    assert any("aggregate_tool_result_compacted" in content for content in projected_contents[1:])
    assert agent.config.metadata["tool_provider_projection_semantic_preserves"] >= 1


def test_agent_aggregate_tool_result_budget_keeps_projected_history_stable() -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(
            context_window_tokens=1200,
        ),
    )
    messages: list[Message] = []
    original_contents: dict[str, str] = {}
    for index in range(5):
        tool_id = f"tool-{index}"
        content = (
            "[tool_result_projection]\n"
            f"tool_use_id: {tool_id}\n"
            f"sha256: {hashlib.sha256(tool_id.encode()).hexdigest()}\n"
            f"head:\nprojected chunk {index}\n"
            + ("x" * 800)
        )
        original_contents[tool_id] = content
        messages.extend(
            [
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(id=tool_id, name="execute_code", input={}),
                    ],
                ),
                Message(
                    role="user",
                    content=[
                        ContentBlockToolResult(
                            tool_use_id=tool_id,
                            content=content,
                            is_error=False,
                        )
                    ],
                ),
            ]
        )

    compacted = agent._compact_aggregate_tool_results_for_provider(messages)

    projected_contents = {
        block.tool_use_id: block.content
        for message in compacted
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolResult)
    }
    assert projected_contents == original_contents


def test_agent_provider_backstop_classifies_external_results_from_tool_use_names(
    tmp_path,
) -> None:
    agent = _recoverable_projection_agent(
        tmp_path,
        context_window_tokens=1_000_000,
        tool_result_provider_request_max_chars=1300,
        tool_result_external_keep_recent=2,
    )
    messages: list[Message] = []
    for index in range(4):
        tool_id = f"fetch-{index}"
        messages.extend(
            [
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(
                            id=tool_id,
                            name="web_fetch",
                            input={"url": f"https://example.com/{index}"},
                        ),
                    ],
                ),
                Message(
                    role="user",
                    content=[
                        ContentBlockToolResult(
                            tool_use_id=tool_id,
                            content=f"fetch {index}\n" + ("x" * 5000),
                            is_error=False,
                        )
                    ],
                ),
            ]
        )

    compacted = agent._compact_aggregate_tool_results_for_provider(messages)

    external_contents = [
        message.content[0].content
        for message in compacted
        if isinstance(message.content, list)
        and message.content
        and isinstance(message.content[0], ContentBlockToolResult)
    ]
    assert "fetch 3" in external_contents[-1]
    assert "fetch 2" in external_contents[-2]
    assert any("[tool_result_projection]" in content for content in external_contents[:2])
    assert any(
        "external tool result compacted for provider request context" in content
        for content in external_contents[:2]
    )
    assert sum(len(content) for content in external_contents) < 4 * (len("fetch 0\n") + 5000)


def test_agent_provider_backstop_preserves_sessions_yield_control_json() -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(
            context_window_tokens=1_000_000,
            tool_result_provider_request_max_chars=300,
        ),
    )
    control_payload = json.dumps(
        {
            "status": "yielded",
            "waited": False,
            "message": "Current turn yielded; wait for pushed session events.",
            "yield_message": "y" * 1000,
        }
    )
    messages = [
        Message(
            role="assistant",
            content=[ContentBlockToolUse(id="yield-1", name="sessions_yield", input={})],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="yield-1",
                    content=control_payload,
                    is_error=False,
                )
            ],
        ),
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="fetch-1", name="web_fetch", input={"url": "https://example.com"}
                )
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="fetch-1",
                    content="fetch\n" + ("x" * 1000),
                    is_error=False,
                )
            ],
        ),
    ]

    compacted = agent._compact_aggregate_tool_results_for_provider(messages)
    control_result = compacted[1].content[0]

    assert isinstance(control_result, ContentBlockToolResult)
    payload = json.loads(control_result.content)
    assert payload["status"] == "yielded"
    assert payload["waited"] is False


def test_agent_provider_view_keeps_large_tool_use_arguments_by_default(tmp_path) -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(
            tool_use_argument_provider_request_max_chars=1200,
        ),
    )
    large_code = "print('start')\n" + ("x = 1\n" * 500) + "print('end')\n"
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="code-1",
                    name="execute_code",
                    input={"code": large_code, "timeout": 10},
                )
            ],
        )
    ]

    projected = agent._sanitize_projected_tool_use_arguments_for_provider(messages)

    original_block = messages[0].content[0]
    projected_block = projected[0].content[0]
    assert isinstance(original_block, ContentBlockToolUse)
    assert isinstance(projected_block, ContentBlockToolUse)
    assert original_block.input["code"] == large_code
    assert projected_block.input["code"] == large_code
    assert "tool_use_argument_projection" not in projected_block.input["code"]
    assert "tool_argument_projection_applied" not in agent.config.metadata


def test_agent_provider_view_derives_tool_argument_budget_above_legacy_default(
    tmp_path,
) -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(
            context_window_tokens=200_000,
            max_tokens=8192,
        ),
    )
    large_code = "print('start')\n" + ("x = 1\n" * 2500) + "print('end')\n"
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="code-derived-1",
                    name="execute_code",
                    input={"code": large_code, "timeout": 10},
                )
            ],
        )
    ]

    projected = agent._sanitize_projected_tool_use_arguments_for_provider(messages)

    projected_block = projected[0].content[0]
    assert isinstance(projected_block, ContentBlockToolUse)
    assert projected_block.input["code"] == large_code
    assert "tool_use_argument_projection" not in projected_block.input["code"]


def test_agent_provider_view_scrubs_legacy_projected_tool_argument(tmp_path) -> None:
    projection = (
        "[tool_use_argument_projection]\n"
        "tool: execute_code\n"
        "tool_use_id: tool-legacy\n"
        "field: code\n"
        "sha256: missing\n"
        "tool_argument_handle: tr-missing\n"
        "head:\nprint('legacy')"
    )
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(
            tool_use_argument_provider_request_max_chars=1200,
        ),
    )
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="tool-legacy",
                    name="execute_code",
                    input={"code": projection, "timeout": 99},
                )
            ],
        )
    ]

    projected = agent._sanitize_projected_tool_use_arguments_for_provider(messages)

    block = projected[0].content[0]
    assert isinstance(block, ContentBlockToolUse)
    assert "tool_use_argument_projection" not in block.input["code"]
    assert "invalid_provider_context_projection:execute_code.code" in block.input["code"]
    stored_contents = [
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "tool-results").rglob("content.txt")
    ]
    assert all("tool_use_argument_projection" not in content for content in stored_contents)


def test_agent_provider_view_does_not_project_aggregate_tool_use_arguments(tmp_path) -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(
            tool_use_argument_provider_request_max_chars=1200,
            tool_use_argument_projection_enabled=True,
        ),
    )
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id=f"write-{index}",
                    name="write_file",
                    input={
                        "path": f"generated/file-{index}.html",
                        "content": "x" * 700,
                    },
                )
                for index in range(5)
            ],
        )
    ]

    projected = agent._sanitize_projected_tool_use_arguments_for_provider(messages)

    projected_blocks = [
        block for block in projected[0].content if isinstance(block, ContentBlockToolUse)
    ]
    assert all(block.input["content"] == "x" * 700 for block in projected_blocks)
    assert all(
        original.input["content"] == "x" * 700
        for original in messages[0].content
        if isinstance(original, ContentBlockToolUse)
    )
    assert "tool_argument_projection_applied" not in agent.config.metadata
    assert "tool_argument_projection_calls" not in agent.config.metadata


def test_agent_provider_view_derives_tool_result_budget_above_legacy_default(
    tmp_path,
) -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(
            context_window_tokens=200_000,
            max_tokens=8192,
        ),
    )
    messages: list[Message] = []
    for index in range(3):
        tool_id = f"fetch-derived-{index}"
        payload = f"FETCH_DERIVED_{index}\n" + ("x" * 40_000)
        messages.extend(
            [
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(
                            id=tool_id,
                            name="web_fetch",
                            input={"url": f"https://example.com/{index}"},
                        )
                    ],
                ),
                Message(
                    role="user",
                    content=[
                        ContentBlockToolResult(
                            tool_use_id=tool_id,
                            content=payload,
                            is_error=False,
                        )
                    ],
                ),
            ]
        )

    compacted = agent._compact_aggregate_tool_results_for_provider(messages)

    first_result = compacted[1].content[0]
    assert isinstance(first_result, ContentBlockToolResult)
    assert first_result.content.startswith("FETCH_DERIVED_0")
    assert "external_tool_result_compacted" not in first_result.content


def test_agent_provider_view_does_not_project_small_aggregate_tool_use_arguments(tmp_path) -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(
            tool_use_argument_provider_request_max_chars=1200,
            tool_use_argument_projection_enabled=True,
        ),
    )
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id=f"write-{index}",
                    name="write_file",
                    input={
                        "path": f"generated/file-{index}.html",
                        "content": "x" * 200,
                    },
                )
                for index in range(6)
            ],
        )
    ]

    projected = agent._sanitize_projected_tool_use_arguments_for_provider(messages)

    projected_blocks = [
        block for block in projected[0].content if isinstance(block, ContentBlockToolUse)
    ]
    assert all(block.input["content"] == "x" * 200 for block in projected_blocks)
    assert all(block.input["path"].startswith("generated/") for block in projected_blocks)
    assert "tool_argument_projection_applied" not in agent.config.metadata
    assert "tool_argument_projection_calls" not in agent.config.metadata


def test_agent_provider_view_keeps_successful_file_write_argument_executable(tmp_path) -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(
            tool_use_argument_provider_request_max_chars=8000,
        ),
    )
    large_html = "<html>\n" + ("<p>word</p>\n" * 900) + "</html>\n"
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="write-1",
                    name="write_file",
                    input={"path": "index.html", "content": large_html},
                )
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="write-1",
                    content='{"status":"ok","path":"index.html"}',
                    is_error=False,
                )
            ],
        ),
    ]

    projected = agent._sanitize_projected_tool_use_arguments_for_provider(messages)

    projected_tool_use = next(
        block for block in projected[0].content if isinstance(block, ContentBlockToolUse)
    )
    projected_content = projected_tool_use.input["content"]
    assert projected_tool_use.id == "write-1"
    assert projected_tool_use.name == "write_file"
    assert projected_tool_use.input["path"] == "index.html"
    assert projected_content == large_html
    assert "[tool_use_argument_projection]" not in projected_content
    assert "successful_file_write_projection" not in projected_content
    assert "<p>word</p>" in projected_content
    projected_result = next(
        block for block in projected[1].content if isinstance(block, ContentBlockToolResult)
    )
    assert projected_result.tool_use_id == "write-1"
    assert projected[-1].role == "user"
    history_block = next(
        block for block in messages[0].content if isinstance(block, ContentBlockToolUse)
    )
    assert history_block.input["content"] == large_html
    assert "tool_argument_projection_applied" not in agent.config.metadata
    assert "tool_argument_projection_calls" not in agent.config.metadata


def test_agent_provider_view_does_not_store_successful_file_write_snapshot(
    tmp_path,
) -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(
            tool_use_argument_provider_request_max_chars=8000,
        ),
    )
    large_html = "<html>\n" + ("<p>word</p>\n" * 900) + "</html>\n"
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="write-1",
                    name="write_file",
                    input={"path": "index.html", "content": large_html},
                )
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="write-1",
                    content='{"status":"ok","path":"index.html"}',
                    is_error=False,
                )
            ],
        ),
    ]

    first = agent._sanitize_projected_tool_use_arguments_for_provider(messages)
    second = agent._sanitize_projected_tool_use_arguments_for_provider(messages)

    first_block = next(
        block for block in first[0].content if isinstance(block, ContentBlockToolUse)
    )
    second_block = next(
        block for block in second[0].content if isinstance(block, ContentBlockToolUse)
    )

    assert first_block.input["content"] == large_html
    assert second_block.input["content"] == large_html
    assert not list((tmp_path / "tool-results" / "s" / "session-1").glob("**/meta.json"))
    assert "tool_argument_projection_applied" not in agent.config.metadata
    assert "tool_argument_projection_calls" not in agent.config.metadata


@pytest.mark.asyncio
async def test_agent_static_cost_source_is_explicitly_distinct_from_provider_billed() -> None:
    provider = StaticCostProvider()
    agent = Agent(provider=provider, config=AgentConfig(model_id="deepseek-v4-flash"))

    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if event.kind == "done")

    assert done.billed_cost == 0.0
    assert done.cost_usd > 0.0
    assert done.cost_source == "opensquilla_static_estimate"


def test_complete_response_usage_cost_is_not_provider_billed_for_direct_providers() -> None:
    response = SimpleNamespace(
        model="deepseek-v4-flash",
        usage={
            "prompt_tokens": 1000,
            "completion_tokens": 1000,
            "cost": 0.0123,
        },
    )
    provider = SimpleNamespace(provider_name="deepseek")

    usage = _usage_from_complete_response(response, provider)

    assert usage["billed_cost"] == 0.0
    assert usage["cost_source"] == "opensquilla_static_estimate"
    assert usage["estimated_cost_usd"] > 0.0


@pytest.mark.asyncio
async def test_agent_uses_sanitized_request_view_and_records_context_stages() -> None:
    provider = CapturingProvider()
    turn_log = CapturingTurnLog()
    agent = Agent(
        provider=provider,
        config=AgentConfig(),
        turn_call_logger=turn_log,  # type: ignore[arg-type]
    )
    agent.set_history(
        [
            Message.model_construct(
                role="assistant",
                content=[
                    {
                        "type": "text",
                        "text": "previous answer",
                        "details": {"debug": True},
                    }
                ],
                reasoning_content=None,
            )
        ]
    )

    events = [event async for event in agent.run_turn("continue")]

    assert any(event.kind == "done" for event in events)
    assert provider.calls
    sent_history_block = provider.calls[0]["messages"][0].content[0]
    assert isinstance(sent_history_block, ContentBlockText)
    assert sent_history_block.text == "previous answer"
    assert "details" not in sent_history_block.model_dump(mode="json")

    stages = [
        record["payload"]["stage"]
        for record in turn_log.records
        if record["kind"] == "context_stage"
    ]
    assert stages == [
        "session:loaded",
        "session:sanitized",
        "session:limited",
        "prompt:before",
        "prompt:images",
        "stream:context",
        "session:after",
    ]


@pytest.mark.asyncio
async def test_agent_provider_view_omits_loaded_history_tool_arguments() -> None:
    provider = CapturingProvider()
    large_argument = "STALE_HISTORY_ARGUMENT\n" + ("x" * 20_000)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1, flush_enabled=False),
    )
    agent.set_history(
        [
            Message(
                role="assistant",
                content=[
                    ContentBlockToolUse(
                        id="write-stale",
                        name="write_file",
                        input={"path": "index.html", "content": large_argument},
                    )
                ],
                reasoning_content="old reasoning\n" + ("r" * 10_000),
            ),
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(
                        tool_use_id="write-stale",
                        content='{"status":"ok"}',
                        is_error=False,
                    )
                ],
            ),
        ]
    )

    events = [event async for event in agent.run_turn("new task")]

    assert any(event.kind == "done" for event in events)
    assert provider.calls
    payload = json.dumps(
        [message.model_dump(mode="json") for message in provider.calls[0]["messages"]],
        ensure_ascii=False,
    )
    assert large_argument not in payload
    assert "x" * 1000 not in payload
    assert "old reasoning" not in payload
    assert "historical_tool_argument_omitted" not in payload
    assert "invalid_provider_context_projection:write_file.content" in payload


@pytest.mark.asyncio
async def test_agent_preserves_deepseek_reasoning_while_projecting_history_payload() -> None:
    provider = CapturingProvider()
    large_argument = "DEEPSEEK_STALE_ARGUMENT\n" + ("x" * 20_000)
    reasoning = "I reasoned before the historical tool call."
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            thinking=ThinkingLevel.HIGH,
            model_id="deepseek-v4-flash",
            model_capabilities=ModelCapabilities(
                supports_reasoning=True,
                supports_tools=True,
                reasoning_format="deepseek",
            ),
            flush_enabled=False,
        ),
    )
    agent.set_history(
        [
            Message(
                role="assistant",
                content=[
                    ContentBlockToolUse(
                        id="deepseek-stale",
                        name="write_file",
                        input={"path": "index.html", "content": large_argument},
                    )
                ],
                reasoning_content=reasoning,
            ),
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(
                        tool_use_id="deepseek-stale",
                        content='{"status":"ok"}',
                        is_error=False,
                    )
                ],
            ),
        ]
    )

    events = [event async for event in agent.run_turn("continue")]

    assert any(event.kind == "done" for event in events)
    sent_assistant = provider.calls[0]["messages"][0]
    assert sent_assistant.reasoning_content == reasoning
    payload = json.dumps(
        [message.model_dump(mode="json") for message in provider.calls[0]["messages"]],
        ensure_ascii=False,
    )
    assert large_argument not in payload
    assert "x" * 1000 not in payload
    assert "historical_tool_argument_omitted" not in payload
    assert "invalid_provider_context_projection:write_file.content" in payload


@pytest.mark.asyncio
async def test_agent_runtime_context_is_request_only_and_not_system_prefix() -> None:
    provider = CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            system_prompt="stable system",
            cache_breakpoints=[{"text": "stable system", "cache": "true"}],
            cache_mode="auto",
            max_iterations=1,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    call = provider.calls[0]
    assert call["config"].system == "stable system"
    assert call["config"].cache_breakpoints == [{"text": "stable system", "cache": "true"}]
    assert call["messages"][0].role == "user"
    assert call["messages"][0].content.startswith("hello")
    assert "[Runtime context for this turn]" in call["messages"][0].content
    assert len(call["messages"]) == 1
    assert all(
        "[Runtime context for this turn]" not in message.content
        for message in agent._history
        if isinstance(message.content, str)
    )


@pytest.mark.asyncio
async def test_agent_runtime_context_does_not_precede_current_user_text() -> None:
    provider = CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            system_prompt="stable system",
            cache_breakpoints=[{"text": "stable system", "cache": "true"}],
            cache_mode="auto",
            max_iterations=1,
        ),
    )
    first_prompt = "first prompt " + ("x" * 2000)

    first_events = [event async for event in agent.run_turn(first_prompt)]
    second_events = [event async for event in agent.run_turn("second prompt")]

    assert any(event.kind == "done" for event in first_events)
    assert any(event.kind == "done" for event in second_events)
    first_call = provider.calls[0]["messages"]
    assert first_call[-1].role == "user"
    assert first_call[-1].content.startswith(first_prompt)
    assert "[Runtime context for this turn]" in first_call[-1].content
    assert not any(
        isinstance(message.content, str)
        and message.content.startswith("[Runtime context for this turn]")
        for message in first_call[:-1]
    )
    assert agent._history[0] == Message(role="user", content=first_prompt)

    second_call = provider.calls[1]["messages"]
    assert second_call[0] == Message(role="user", content=first_prompt)
    assert second_call[1] == Message(
        role="assistant",
        content=[ContentBlockText(text="ok")],
    )
    assert second_call[-1].role == "user"
    assert second_call[-1].content.startswith("second prompt")
    assert "[Runtime context for this turn]" in second_call[-1].content


def test_limit_turns_ignores_synthetic_user_messages_when_counting_turns() -> None:
    messages = [
        Message(role="user", content="first real user"),
        Message(role="assistant", content="first answer"),
        Message(role="user", content="[Available skills for this turn]\n<skill />"),
        Message(role="user", content="second real user"),
        Message(role="assistant", content="second answer"),
    ]

    assert limit_turns(messages, 2) == messages
    assert limit_turns(messages, 1) == messages[3:]


def test_turn_runner_keeps_dynamic_prompt_out_of_system_when_cache_enabled() -> None:
    from opensquilla.engine.runtime import TurnRunner

    runner = TurnRunner.__new__(TurnRunner)
    turn = SimpleNamespace(
        system_prompt=("stable base", "<memory_context>volatile recall</memory_context>"),
        metadata={"cache_enabled": True},
    )

    final_prompt, cache_breakpoints, request_context_prompt = runner._resolve_prompt_config(turn)

    assert final_prompt == "stable base"
    assert cache_breakpoints == [{"text": "stable base", "cache": "true"}]
    assert request_context_prompt == "<memory_context>volatile recall</memory_context>"


def test_turn_runner_preserves_joined_system_prompt_when_cache_disabled() -> None:
    from opensquilla.engine.runtime import TurnRunner

    runner = TurnRunner.__new__(TurnRunner)
    turn = SimpleNamespace(
        system_prompt=("stable base", "<memory_context>volatile recall</memory_context>"),
        metadata={},
    )

    final_prompt, cache_breakpoints, request_context_prompt = runner._resolve_prompt_config(turn)

    assert final_prompt == "stable base\n\n<memory_context>volatile recall</memory_context>"
    assert cache_breakpoints is None
    assert request_context_prompt is None


def test_agent_adjusts_request_context_indexes_after_compaction() -> None:
    request_idx = Agent._adjust_index_after_prefix_compaction(
        2,
        2,
        summary_present=True,
    )
    runtime_idx = Agent._adjust_index_after_prefix_compaction(
        3,
        2,
        summary_present=True,
    )

    compacted_messages = [
        Message(role="user", content="[Context summary]\nsummary"),
        Message(role="assistant", content="Understood. Continuing from summary."),
        Message(role="user", content="[Available skills for this turn]\nskill"),
        Message(role="user", content="current question"),
    ]
    request_context = Message(role="user", content="[Request context for this turn]\nvolatile")
    runtime_context = Message(role="user", content="[Runtime context for this turn]\nnow")

    request_messages = Agent._with_request_context_messages(
        compacted_messages,
        request_context,
        request_idx,
        runtime_context,
        runtime_idx,
    )

    assert [message.content for message in request_messages] == [
        "[Context summary]\nsummary",
        "Understood. Continuing from summary.",
        "[Request context for this turn]\nvolatile",
        "[Available skills for this turn]\nskill",
        "current question\n\n[Runtime context for this turn]\nnow",
    ]


@pytest.mark.asyncio
async def test_agent_request_context_is_request_only_after_history() -> None:
    provider = CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            system_prompt="stable system",
            request_context_prompt="<memory_context>volatile recall</memory_context>",
            skills_context_prompt="<skill id='memory'>Memory helper</skill>",
            cache_breakpoints=[{"text": "stable system", "cache": "true"}],
            cache_mode="auto",
            max_iterations=1,
        ),
    )
    agent.set_history(
        [
            Message(role="user", content="old question"),
            Message(role="assistant", content="old answer"),
        ]
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    call = provider.calls[0]
    assert call["config"].system == "stable system"
    assert call["config"].cache_breakpoints == [{"text": "stable system", "cache": "true"}]
    assert "<memory_context>volatile recall</memory_context>" not in call["config"].system
    assert [message.role for message in call["messages"]] == [
        "user",
        "assistant",
        "user",
        "user",
        "user",
    ]
    assert call["messages"][0] == Message(role="user", content="old question")
    assert call["messages"][1] == Message(role="assistant", content="old answer")
    assert "[Available skills for this turn]" in call["messages"][2].content
    assert "[Request context for this turn]" in call["messages"][3].content
    assert "<memory_context>volatile recall</memory_context>" in call["messages"][3].content
    assert call["messages"][4].content.startswith("hello")
    assert "[Runtime context for this turn]" in call["messages"][4].content
    assert all(
        "<memory_context>volatile recall</memory_context>" not in message.content
        for message in agent._history
        if isinstance(message.content, str)
    )


@pytest.mark.asyncio
async def test_agent_provider_view_prunes_non_adjacent_tool_results() -> None:
    provider = CapturingProvider()
    agent = Agent(provider=provider, config=AgentConfig(max_iterations=1))
    agent.set_history(
        [
            Message(role="user", content="old task"),
            Message(
                role="assistant",
                content=[
                    ContentBlockToolUse(
                        id="call_lookup",
                        name="lookup",
                        input={"q": "old"},
                    )
                ],
            ),
            Message(role="user", content="ordinary user message before the tool result"),
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(
                        tool_use_id="call_lookup",
                        content="lookup result",
                        is_error=False,
                    )
                ],
            ),
        ]
    )

    events = [event async for event in agent.run_turn("continue")]

    assert any(event.kind == "done" for event in events)
    replay_payload = json.dumps(
        [message.model_dump(mode="json") for message in provider.calls[0]["messages"]],
        ensure_ascii=False,
    )
    assert "call_lookup" not in replay_payload
    assert "ordinary user message before the tool result" in replay_payload


@pytest.mark.asyncio
async def test_agent_provider_view_keeps_latest_duplicate_tool_result() -> None:
    provider = CapturingProvider()
    agent = Agent(provider=provider, config=AgentConfig(max_iterations=1))
    agent.set_history(
        [
            Message(role="user", content="old task"),
            Message(
                role="assistant",
                content=[
                    ContentBlockToolUse(
                        id="call_lookup",
                        name="lookup",
                        input={"q": "old"},
                    )
                ],
            ),
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(
                        tool_use_id="call_lookup",
                        content='{"status":"approval_required"}',
                        is_error=False,
                    ),
                    ContentBlockToolResult(
                        tool_use_id="call_lookup",
                        content='{"status":"approval_denied"}',
                        is_error=True,
                    ),
                ],
            ),
        ]
    )

    events = [event async for event in agent.run_turn("continue")]

    assert any(event.kind == "done" for event in events)
    tool_results = [
        block
        for message in provider.calls[0]["messages"]
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolResult)
    ]
    assert len(tool_results) == 1
    assert tool_results[0].tool_use_id == "call_lookup"
    assert tool_results[0].content == '{"status":"approval_denied"}'
    assert tool_results[0].is_error is True


@pytest.mark.asyncio
async def test_agent_provider_view_preserves_split_adjacent_tool_results() -> None:
    provider = CapturingProvider()
    agent = Agent(provider=provider, config=AgentConfig(max_iterations=1))
    agent.set_history(
        [
            Message(role="user", content="old task"),
            Message(
                role="assistant",
                content=[
                    ContentBlockToolUse(
                        id="call_alpha",
                        name="lookup",
                        input={"q": "alpha"},
                    ),
                    ContentBlockToolUse(
                        id="call_beta",
                        name="lookup",
                        input={"q": "beta"},
                    ),
                ],
            ),
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(
                        tool_use_id="call_alpha",
                        content="alpha result",
                        is_error=False,
                    )
                ],
            ),
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(
                        tool_use_id="call_beta",
                        content="beta result",
                        is_error=False,
                    )
                ],
            ),
        ]
    )

    events = [event async for event in agent.run_turn("continue")]

    assert any(event.kind == "done" for event in events)
    replay_payload = json.dumps(
        [message.model_dump(mode="json") for message in provider.calls[0]["messages"]],
        ensure_ascii=False,
    )
    assert "call_alpha" in replay_payload
    assert "alpha result" in replay_payload
    assert "call_beta" in replay_payload
    assert "beta result" in replay_payload


@pytest.mark.asyncio
async def test_agent_request_context_repeats_across_tool_loop_without_persisting() -> None:
    provider = ToolLoopCapturingProvider()

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            system_prompt="stable system",
            request_context_prompt="<memory_context>volatile recall</memory_context>",
            max_iterations=2,
        ),
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 2
    for call in provider.calls:
        request_context_messages = [
            message
            for message in call["messages"]
            if isinstance(message.content, str)
            and "<memory_context>volatile recall</memory_context>" in message.content
        ]
        assert len(request_context_messages) == 1
        assert "[Request context for this turn]" in request_context_messages[0].content
    assert all(
        "<memory_context>volatile recall</memory_context>" not in message.content
        for message in agent._history
        if isinstance(message.content, str)
    )


@pytest.mark.asyncio
async def test_agent_canonicalizes_large_tool_result_for_event_history_and_provider(
    tmp_path,
) -> None:
    provider = ToolLoopCapturingProvider()
    raw_output = "single bulky output\n" + ("x" * 5000)

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content=raw_output,
        )

    agent = _recoverable_projection_agent(
        tmp_path,
        provider=provider,
        tool_handler=tool_handler,
        tool_names=("echo",),
        # Window large enough that the projected (~250-token) tool result
        # fits: this test exercises tool-result projection/canonicalization,
        # not compaction. A 200-token window is below the projection floor,
        # so inline compaction would legitimately fire and prune the result.
        context_window_tokens=200_000,
        max_iterations=2,
        tool_result_projection_max_inline_chars=1000,
    )

    events = [event async for event in agent.run_turn("hello")]

    result_event = next(
        event
        for event in events
        if isinstance(event, ToolResultEvent) and event.tool_use_id == "tool-1"
    )
    assert result_event.result != raw_output
    assert "tool_result_handle: tr-" in result_event.result
    history_result = next(
        block
        for message in agent._history
        if message.role == "user" and isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolResult) and block.tool_use_id == "tool-1"
    )
    assert history_result.content == result_event.result
    assert history_result.content != raw_output

    assert len(provider.calls) == 2
    replay_result = next(
        block
        for message in provider.calls[1]["messages"]
        if message.role == "user" and isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolResult) and block.tool_use_id == "tool-1"
    )
    assert replay_result.content == result_event.result
    assert replay_result.content != raw_output
    assert "tool_result_handle: tr-" in replay_result.content
    assert "single bulky output" in replay_result.content
    assert len(replay_result.content) < len(raw_output)


def test_agent_provider_request_messages_project_overflow_retry_tool_results(
    tmp_path,
) -> None:
    agent = _recoverable_projection_agent(
        tmp_path,
        context_window_tokens=200,
    )
    raw_output = "overflow retry bulky output\n" + ("x" * 8000)
    messages = [
        Message(
            role="assistant",
            content=[ContentBlockToolUse(id="tool-1", name="execute_code", input={})],
        ),
        Message(
            role="user",
            content=[ContentBlockToolResult(tool_use_id="tool-1", content=raw_output)],
        ),
    ]

    request_messages = agent._provider_request_messages(
        messages,
        request_context_message=None,
        request_context_insert_index=0,
        runtime_context_message=Message(role="user", content="[Runtime context]"),
        runtime_context_insert_index=len(messages),
    )

    original_result = messages[1].content[0]
    assert isinstance(original_result, ContentBlockToolResult)
    assert original_result.content == raw_output
    request_result = next(
        block
        for message in request_messages
        if message.role == "user" and isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolResult) and block.tool_use_id == "tool-1"
    )
    assert request_result.content != raw_output
    assert "[tool_result_projection]" in request_result.content
    assert "tool_result_handle: tr-" in request_result.content
    assert len(request_result.content) < len(raw_output)


def test_agent_provider_request_guard_reuses_stable_tool_result_projection(
    tmp_path,
) -> None:
    agent = _recoverable_projection_agent(
        tmp_path,
        context_window_tokens=200,
    )
    raw_output = "overflow retry bulky output\n" + ("x" * 8000)
    messages = [
        Message(
            role="assistant",
            content=[ContentBlockToolUse(id="tool-1", name="execute_code", input={})],
        ),
        Message(
            role="user",
            content=[ContentBlockToolResult(tool_use_id="tool-1", content=raw_output)],
        ),
    ]

    first_request = agent._provider_request_messages(
        messages,
        request_context_message=None,
        request_context_insert_index=0,
        runtime_context_message=Message(role="user", content="[Runtime context]"),
        runtime_context_insert_index=len(messages),
    )
    second_request = agent._provider_request_messages(
        messages,
        request_context_message=None,
        request_context_insert_index=0,
        runtime_context_message=Message(role="user", content="[Runtime context]"),
        runtime_context_insert_index=len(messages),
    )

    def _result_content(request_messages: list[Message]) -> str:
        return next(
            block.content
            for message in request_messages
            if message.role == "user" and isinstance(message.content, list)
            for block in message.content
            if isinstance(block, ContentBlockToolResult) and block.tool_use_id == "tool-1"
        )

    first_content = _result_content(first_request)
    second_content = _result_content(second_request)
    assert first_content == second_content
    assert first_content != raw_output
    assert "[tool_result_projection]" in first_content
    assert agent._provider_tool_result_overrides["tool-1"].content == first_content


def test_agent_provider_request_reapplies_frozen_projection_after_turn_reset(
    tmp_path,
) -> None:
    agent = _recoverable_projection_agent(
        tmp_path,
        context_window_tokens=200,
    )
    raw_output = "overflow retry bulky output\n" + ("x" * 8000)
    messages = [
        Message(
            role="assistant",
            content=[ContentBlockToolUse(id="tool-1", name="execute_code", input={})],
        ),
        Message(
            role="user",
            content=[ContentBlockToolResult(tool_use_id="tool-1", content=raw_output)],
        ),
    ]

    first_request = agent._provider_request_messages(
        messages,
        request_context_message=None,
        request_context_insert_index=0,
        runtime_context_message=Message(role="user", content="[Runtime context]"),
        runtime_context_insert_index=len(messages),
    )
    agent._provider_tool_result_overrides = {}
    second_request = agent._provider_request_messages(
        messages,
        request_context_message=None,
        request_context_insert_index=0,
        runtime_context_message=Message(role="user", content="[Runtime context]"),
        runtime_context_insert_index=len(messages),
    )

    def _result_content(request_messages: list[Message]) -> str:
        return next(
            block.content
            for message in request_messages
            if message.role == "user" and isinstance(message.content, list)
            for block in message.content
            if isinstance(block, ContentBlockToolResult) and block.tool_use_id == "tool-1"
        )

    assert _result_content(first_request) == _result_content(second_request)
    assert "[tool_result_projection]" in _result_content(second_request)


def test_agent_provider_request_does_not_project_previously_full_visible_result(
    tmp_path,
) -> None:
    agent = _recoverable_projection_agent(
        tmp_path,
        context_window_tokens=200,
    )
    first_raw = "first result stays full"
    first_messages = [
        Message(
            role="assistant",
            content=[ContentBlockToolUse(id="tool-1", name="execute_code", input={})],
        ),
        Message(
            role="user",
            content=[ContentBlockToolResult(tool_use_id="tool-1", content=first_raw)],
        ),
    ]

    first_request = agent._provider_request_messages(
        first_messages,
        request_context_message=None,
        request_context_insert_index=0,
        runtime_context_message=Message(role="user", content="[Runtime context]"),
        runtime_context_insert_index=len(first_messages),
    )
    assert next(
        block.content
        for message in first_request
        if message.role == "user" and isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolResult) and block.tool_use_id == "tool-1"
    ) == first_raw

    bulky = "later bulky output\n" + ("x" * 8000)
    later_messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(id="tool-1", name="execute_code", input={}),
                ContentBlockToolUse(id="tool-2", name="execute_code", input={}),
                ContentBlockToolUse(id="tool-3", name="execute_code", input={}),
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(tool_use_id="tool-1", content=first_raw),
                ContentBlockToolResult(tool_use_id="tool-2", content=bulky),
                ContentBlockToolResult(tool_use_id="tool-3", content=bulky),
            ],
        ),
    ]
    later_request = agent._provider_request_messages(
        later_messages,
        request_context_message=None,
        request_context_insert_index=0,
        runtime_context_message=Message(role="user", content="[Runtime context]"),
        runtime_context_insert_index=len(later_messages),
    )
    contents = {
        block.tool_use_id: block.content
        for message in later_request
        if message.role == "user" and isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolResult)
    }

    assert contents["tool-1"] == first_raw
    assert any(
        "[tool_result_projection]" in value
        for key, value in contents.items()
        if key != "tool-1"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "strict_flush_config",
    [
        {"flush_compaction_requires_safe_receipt": True},
        {"flush_compaction_safety_mode": "block"},
    ],
)
async def test_agent_inline_strict_flush_receipt_refuses_destructive_compaction(
    monkeypatch: pytest.MonkeyPatch,
    strict_flush_config: dict[str, Any],
) -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(
            context_window_tokens=10,
            context_overflow_threshold=0.1,
            flush_enabled=True,
            flush_pre_compaction=True,
            flush_timeout_seconds=0.1,
            **strict_flush_config,
        ),
    )
    messages = [Message(role="user", content="important history")]
    compact_called = False

    monkeypatch.setattr(
        "opensquilla.memory.flush.should_flush",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        "opensquilla.memory.flush.resolve_flush_plan",
        lambda **_kwargs: SimpleNamespace(relative_path="flush.md"),
    )

    async def degraded_flush(_plan: Any, _messages: list[Message]) -> Any:
        return SimpleNamespace(
            mode="llm",
            indexed_chunk_count=1,
            integrity_status="missing_chunks",
            output_coverage_status="ok",
            invalid_candidate_count=0,
            candidate_missing_ids=[],
            obligation_status="ok",
            obligation_missing_ids=[],
        )

    async def compact_context_should_not_run(_request: Any) -> Any:
        nonlocal compact_called
        compact_called = True
        return SimpleNamespace(summary="", kept_entries=[], removed_count=0)

    monkeypatch.setattr(agent, "_run_flush", degraded_flush)
    monkeypatch.setattr(
        "opensquilla.engine.agent.compact_context",
        compact_context_should_not_run,
    )

    outcome = await agent._check_context_overflow(messages, estimated_context_tokens=100)

    assert outcome is None
    assert compact_called is False
    assert agent._last_compaction_refusal_reason == "memory_flush_degraded_before_compaction"


@pytest.mark.asyncio
async def test_agent_keeps_large_tool_arguments_during_tool_replay(tmp_path) -> None:
    large_code = "print('start')\n" + ("x = 1\n" * 500) + "print('end')\n"
    provider = LargeArgumentToolLoopCapturingProvider(large_code)

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
            tool_use_argument_provider_request_max_chars=1200,
            tool_use_argument_projection_enabled=True,
        ),
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    replay_messages = provider.calls[1]["messages"]
    assistant_replay = next(
        message
        for message in replay_messages
        if message.role == "assistant"
        and isinstance(message.content, list)
        and any(getattr(block, "type", None) == "tool_use" for block in message.content)
    )
    replay_block = next(
        block for block in assistant_replay.content if isinstance(block, ContentBlockToolUse)
    )
    assert replay_block.input["code"] == large_code
    assert "tool_use_argument_projection" not in replay_block.input["code"]
    history_block = next(
        block
        for message in agent._history
        if message.role == "assistant" and isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolUse)
    )
    assert history_block.input["code"] == large_code


@pytest.mark.asyncio
async def test_agent_refuses_copied_tool_argument_projection_without_dispatch(
    tmp_path,
) -> None:
    large_code = "print('start')\n" + ("x = 1\n" * 500) + "print('end')\n"
    provider = CopiedProjectionToolLoopCapturingProvider(large_code)
    dispatched_code_arguments: list[str] = []
    dispatched_arguments: list[dict[str, Any]] = []

    async def tool_handler(call: Any) -> ToolResult:
        dispatched_code_arguments.append(call.arguments["code"])
        dispatched_arguments.append(call.arguments)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=3,
            tool_use_argument_provider_request_max_chars=1200,
            tool_use_argument_projection_enabled=True,
        ),
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("hello")]

    done_event = next(event for event in events if event.kind == "done")
    assert done_event.text == "done"
    assert len(provider.calls) == 3
    assert dispatched_code_arguments == [large_code]
    assert dispatched_arguments == [{"code": large_code, "timeout": 10}]
    assert all(
        not value.startswith("[tool_use_argument_projection]\n")
        for value in dispatched_code_arguments
    )
    result_event = next(
        event
        for event in events
        if isinstance(event, ToolResultEvent) and event.tool_use_id == "tool-2"
    )
    assert result_event.is_error is True
    assert "provider-only compacted tool argument" not in result_event.result
    assert "Projected tool argument" not in result_event.result
    assert result_event.arguments["code"] != large_code
    assert not result_event.arguments["code"].startswith("[tool_use_argument_projection]\n")
    assert "tool_use_argument_projection" not in result_event.arguments["code"]
    assert result_event.arguments["timeout"] == 99
    history_block = next(
        block
        for message in agent._history
        if message.role == "assistant" and isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolUse) and block.id == "tool-2"
    )
    assert history_block.input["code"] == result_event.arguments["code"]
    assert not history_block.input["code"].startswith("[tool_use_argument_projection]\n")
    assert "tool_use_argument_projection" not in history_block.input["code"]
    assert history_block.input["timeout"] == 99

    follow_up_events = [event async for event in agent.run_turn("continue")]
    assert any(event.kind == "done" for event in follow_up_events)
    replay_payload = json.dumps(
        [message.model_dump(mode="json") for message in provider.calls[-1]["messages"]],
        ensure_ascii=False,
    )
    assert "tool-2" not in replay_payload
    assert "tool_use_argument_projection" not in replay_payload


@pytest.mark.asyncio
async def test_agent_refuses_unrestorable_tool_argument_projection(tmp_path) -> None:
    large_code = "print('start')\n" + ("x = 1\n" * 500) + "print('end')\n"
    provider = CopiedProjectionToolLoopCapturingProvider(large_code)
    dispatched_tool_ids: list[str] = []

    original_legacy_projection = provider._legacy_projected_code_argument

    def corrupted_projection() -> str:
        projection = original_legacy_projection()
        bad_hash = "0" * 64
        return projection.replace(
            "sha256: ",
            f"sha256: {bad_hash}\nprevious_sha256: ",
            1,
        )

    provider._legacy_projected_code_argument = corrupted_projection  # type: ignore[method-assign]

    async def tool_handler(call: Any) -> ToolResult:
        dispatched_tool_ids.append(call.tool_use_id)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=3,
            tool_use_argument_provider_request_max_chars=1200,
            tool_use_argument_projection_enabled=True,
        ),
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("hello")]

    result_event = next(
        event
        for event in events
        if isinstance(event, ToolResultEvent) and event.tool_use_id == "tool-2"
    )
    assert dispatched_tool_ids == ["tool-1"]
    assert result_event.is_error is True
    assert "provider-only compacted tool argument" not in result_event.result
    assert "Projected tool argument" not in result_event.result
    assert not result_event.arguments["code"].startswith("[tool_use_argument_projection]\n")
    assert "tool_use_argument_projection" not in result_event.arguments["code"]

    history_block = next(
        block
        for message in agent._history
        if message.role == "assistant" and isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolUse) and block.id == "tool-2"
    )
    assert history_block.input["code"] == result_event.arguments["code"]
    assert not history_block.input["code"].startswith("[tool_use_argument_projection]\n")
    assert "tool_use_argument_projection" not in history_block.input["code"]

    second_events = [event async for event in agent.run_turn("hi")]

    assert any(event.kind == "done" for event in second_events)
    replay_payload = json.dumps(
        [message.model_dump(mode="json") for message in provider.calls[-1]["messages"]],
        ensure_ascii=False,
    )
    assert "tool_use_id: tool-2" not in replay_payload
    stored_contents = [
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "tool-results").rglob("content.txt")
    ]
    assert all("tool_use_argument_projection" not in content for content in stored_contents)


@pytest.mark.asyncio
async def test_agent_refuses_copied_provider_compacted_tool_arguments(tmp_path) -> None:
    provider = CompactedToolArgumentsProvider()
    dispatched: list[dict[str, Any]] = []

    async def tool_handler(call: Any) -> ToolResult:
        dispatched.append(call.arguments)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
        ),
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("open in chrome")]

    assert any(event.kind == "done" for event in events)
    done_event = next(event for event in events if event.kind == "done")
    assert done_event.text == "done"
    assert "provider-only compacted tool arguments" not in done_event.text
    assert len(provider.calls) == 2
    assert dispatched == []
    result_event = next(
        event
        for event in events
        if isinstance(event, ToolResultEvent) and event.tool_use_id == "tool-compact"
    )
    assert result_event.is_error is True
    assert "provider-only compacted tool arguments" not in result_event.result
    assert "ProjectedToolArgumentsError" not in result_event.result
    assert "_opensquilla_compacted_tool_arguments" not in result_event.arguments

    history_block = next(
        block
        for message in agent._history
        if message.role == "assistant" and isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolUse) and block.id == "tool-compact"
    )
    assert history_block.input == result_event.arguments
    assert "_opensquilla_compacted_tool_arguments" not in history_block.input

    follow_up_events = [event async for event in agent.run_turn("continue")]
    assert any(event.kind == "done" for event in follow_up_events)
    replay_payload = json.dumps(
        [message.model_dump(mode="json") for message in provider.calls[-1]["messages"]],
        ensure_ascii=False,
    )
    assert "_opensquilla_compacted_tool_arguments" not in replay_payload
    assert "_invalid_provider_context_arguments" not in replay_payload
    assert "provider_context_omitted" not in replay_payload
    assert "tool-compact" not in replay_payload


@pytest.mark.asyncio
async def test_agent_refuses_copied_provider_request_compacted_argument_string(
    tmp_path,
) -> None:
    provider = ProviderRequestCompactedArgumentStringProvider()
    dispatched: list[dict[str, Any]] = []

    async def tool_handler(call: Any) -> ToolResult:
        dispatched.append(call.arguments)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
        ),
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("continue")]

    assert any(event.kind == "done" for event in events)
    assert dispatched == []
    result_event = next(
        event
        for event in events
        if isinstance(event, ToolResultEvent) and event.tool_use_id == "tool-provider-request"
    )
    assert result_event.is_error is True
    assert result_event.execution_status is not None
    assert result_event.execution_status["reason"] == "provider_context_projection_reused"
    assert "compaction placeholder" in result_event.result


@pytest.mark.asyncio
async def test_agent_refuses_mid_string_compacted_marker_in_tool_argument(
    tmp_path,
) -> None:
    provider = MidStringCompactedArgumentStringProvider()
    dispatched: list[dict[str, Any]] = []

    async def tool_handler(call: Any) -> ToolResult:
        dispatched.append(call.arguments)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
        ),
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("continue")]

    assert any(event.kind == "done" for event in events)
    assert dispatched == []
    result_event = next(
        event
        for event in events
        if isinstance(event, ToolResultEvent) and event.tool_use_id == "tool-mid-string"
    )
    assert result_event.is_error is True
    assert result_event.execution_status is not None
    assert result_event.execution_status["reason"] == "provider_context_projection_reused"
    assert "compaction placeholder" in result_event.result
    assert "re-read" in result_event.result.lower()


@pytest.mark.asyncio
async def test_agent_repair_prompt_keeps_provider_request_from_ending_on_assistant(
    tmp_path,
) -> None:
    provider = TextThenCompactedToolArgumentsProvider()

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
        ),
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("make a deck")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 2
    repair_messages = provider.calls[1]["messages"]
    assert repair_messages[-1].role == "user"
    assert isinstance(repair_messages[-1].content, str)
    assert "Regenerate the complete tool arguments" in repair_messages[-1].content
    replay_payload = json.dumps(
        [message.model_dump(mode="json") for message in repair_messages],
        ensure_ascii=False,
    )
    assert "tool-compact" not in replay_payload
    assert "_invalid_provider_context_arguments" not in replay_payload
    assert "provider_context_omitted" not in replay_payload


def test_agent_repair_prompt_handles_tool_use_without_tool_result() -> None:
    agent = Agent(provider=CapturingProvider(), config=AgentConfig())
    messages = [
        Message(role="user", content="make a deck"),
        Message(
            role="assistant",
            content=[
                ContentBlockText(text="I will prepare the file."),
                ContentBlockToolUse(
                    id="tool-compact",
                    name="write_file",
                    input={
                        "_opensquilla_compacted_tool_arguments": True,
                        "reason": "provider_context_omitted",
                    },
                ),
            ],
        ),
    ]

    stripped = agent._strip_provider_context_marker_replay_for_provider(messages)

    assert stripped[-1].role == "user"
    assert isinstance(stripped[-1].content, str)
    assert "Regenerate the complete tool arguments" in stripped[-1].content
    replay_payload = json.dumps(
        [message.model_dump(mode="json") for message in stripped],
        ensure_ascii=False,
    )
    assert "tool-compact" not in replay_payload
    assert "provider_context_omitted" not in replay_payload


def test_agent_strips_string_provider_context_marker_replay() -> None:
    agent = Agent(provider=CapturingProvider(), config=AgentConfig())
    messages = [
        Message(role="user", content="inspect the repo"),
        Message(
            role="assistant",
            content=[
                ContentBlockText(text="I will inspect the repo."),
                ContentBlockToolUse(
                    id="tool-compact-string",
                    name="exec_command",
                    input={
                        "_opensquilla_compacted_tool_arguments": "true",
                        "original_chars": "50",
                        "argument_keys": '["command"]',
                    },
                ),
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="tool-compact-string",
                    content="missing required argument command",
                    is_error=True,
                )
            ],
        ),
    ]

    stripped = agent._strip_provider_context_marker_replay_for_provider(messages)

    replay_payload = json.dumps(
        [message.model_dump(mode="json") for message in stripped],
        ensure_ascii=False,
    )
    assert "tool-compact-string" not in replay_payload
    assert "_opensquilla_compacted_tool_arguments" not in replay_payload
    assert "missing required argument command" not in replay_payload


@pytest.mark.asyncio
async def test_agent_preserves_reasoning_content_for_deepseek_tool_replay() -> None:
    provider = ReasoningToolLoopCapturingProvider()

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
            thinking=ThinkingLevel.HIGH,
            model_id="deepseek-v4-flash",
            model_capabilities=ModelCapabilities(
                supports_reasoning=True,
                supports_tools=True,
                reasoning_format="deepseek",
            ),
        ),
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 2
    replay_messages = provider.calls[1]["messages"]
    assistant_replay = next(
        message
        for message in replay_messages
        if message.role == "assistant"
        and isinstance(message.content, list)
        and any(getattr(block, "type", None) == "tool_use" for block in message.content)
    )
    assert assistant_replay.reasoning_content == "I should call echo before finalizing."


@pytest.mark.asyncio
async def test_agent_preserves_allowed_historical_image_blocks_for_vision_models() -> None:
    provider = CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            model_id="vision-model",
            model_capabilities=ModelCapabilities(supports_vision=True),
            preserve_historical_images=True,
        ),
    )
    agent.set_history(
        [
            Message(
                role="user",
                content=[
                    ContentBlockText(text="describe this"),
                    ContentBlockImage(media_type="image/png", data="aW1hZ2U="),
                ],
            )
        ]
    )

    events = [event async for event in agent.run_turn("continue")]

    assert any(event.kind == "done" for event in events)
    sent_blocks = provider.calls[0]["messages"][0].content
    assert any(isinstance(block, ContentBlockImage) for block in sent_blocks)


@pytest.mark.asyncio
async def test_agent_preserves_reasoning_content_for_deepseek_text_replay() -> None:
    provider = CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            thinking=ThinkingLevel.HIGH,
            model_id="deepseek-v4-flash",
            model_capabilities=ModelCapabilities(
                supports_reasoning=True,
                supports_tools=True,
                reasoning_format="deepseek",
            ),
        ),
    )
    agent.set_history(
        [
            Message(role="user", content="old question"),
            Message(
                role="assistant",
                content=[ContentBlockText(text="old answer")],
                reasoning_content="I reasoned before answering.",
            ),
        ]
    )

    events = [event async for event in agent.run_turn("continue")]

    assert any(event.kind == "done" for event in events)
    assert provider.calls
    sent_assistant = provider.calls[0]["messages"][1]
    assert sent_assistant.reasoning_content == "I reasoned before answering."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_id",
    [
        "deepseek-v4-flash",
        "deepseek-v4-flash-0731",
        "deepseek-v4-pro-0813",
        "tokenrhythm/deepseek-v4-pro-0813",
    ],
)
async def test_agent_preserves_direct_deepseek_v4_reasoning_without_capabilities(
    model_id: str,
) -> None:
    provider = CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            thinking=ThinkingLevel.HIGH,
            model_id=model_id,
            model_capabilities=None,
        ),
    )
    agent.set_history(
        [
            Message(role="user", content="old question"),
            Message(
                role="assistant",
                content=[ContentBlockText(text="old answer")],
                reasoning_content="I reasoned before answering.",
            ),
        ]
    )

    events = [event async for event in agent.run_turn("continue")]

    assert any(event.kind == "done" for event in events)
    assert provider.calls
    sent_assistant = provider.calls[0]["messages"][1]
    assert sent_assistant.reasoning_content == "I reasoned before answering."


@pytest.mark.asyncio
async def test_agent_preserves_reasoning_content_for_dashscope_qwen_replay() -> None:
    provider = CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            thinking=ThinkingLevel.HIGH,
            model_id="qwen3.6-flash",
            model_capabilities=ModelCapabilities(
                supports_reasoning=True,
                supports_tools=True,
                reasoning_format="dashscope",
            ),
        ),
    )
    agent.set_history(
        [
            Message(role="user", content="old question"),
            Message(
                role="assistant",
                content=[ContentBlockText(text="old answer")],
                reasoning_content="I reasoned before answering with Qwen.",
            ),
        ]
    )

    events = [event async for event in agent.run_turn("continue")]

    assert any(event.kind == "done" for event in events)
    assert provider.calls
    sent_assistant = provider.calls[0]["messages"][1]
    assert sent_assistant.reasoning_content == "I reasoned before answering with Qwen."


@pytest.mark.asyncio
async def test_agent_drops_reasoning_content_when_model_is_not_deepseek() -> None:
    provider = CapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            thinking=ThinkingLevel.HIGH,
            model_id="custom-reasoning-model",
            model_capabilities=ModelCapabilities(
                supports_reasoning=True,
                supports_tools=True,
                reasoning_format="deepseek",
            ),
        ),
    )
    agent.set_history(
        [
            Message(role="user", content="old question"),
            Message(
                role="assistant",
                content=[ContentBlockText(text="old answer")],
                reasoning_content="I reasoned before answering.",
            ),
        ]
    )

    events = [event async for event in agent.run_turn("continue")]

    assert any(event.kind == "done" for event in events)
    assert provider.calls
    sent_assistant = provider.calls[0]["messages"][1]
    assert sent_assistant.reasoning_content is None
