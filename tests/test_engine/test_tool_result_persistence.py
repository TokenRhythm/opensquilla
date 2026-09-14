from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolCall, ToolResult
from opensquilla.engine.history import reconstruct_messages_from_entry
from opensquilla.engine.runtime import _persisted_tool_result_segment
from opensquilla.engine.tool_result_store import ToolResultStore
from opensquilla.engine.types import ToolResultEvent
from opensquilla.provider import (
    ChatConfig,
    ContentBlockToolResult,
    ModelCapabilities,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import DoneEvent as ProviderDoneEvent
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEndEvent
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStartEvent
from opensquilla.provider.protocol import count_provider_image_blocks
from opensquilla.tools.builtin import shell
from opensquilla.tools.builtin.tool_results import retrieve_tool_result
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.output_capture import BoundedOutputCapture
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolContext, ToolSpec, current_execution_log
from tests.helpers.image_bytes import image_bytes


def test_persisted_tool_result_keeps_oversized_json_parseable_with_provider() -> None:
    result = json.dumps(
        {
            "query": "ClickUp pricing plans 2025 2026 per seat",
            "provider": "brave",
            "results": [
                {
                    "title": f"Result {idx}",
                    "url": f"https://example.com/{idx}",
                    "snippet": "x" * 700,
                }
                for idx in range(5)
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
    assert len(result) > 2000

    segment = _persisted_tool_result_segment(
        ToolResultEvent(
            tool_use_id="call_1",
            tool_name="web_search",
            result=result,
            is_error=False,
        )
    )

    assert segment["provider"] == "brave"
    assert segment["query"] == "ClickUp pricing plans 2025 2026 per seat"
    assert segment["result_truncated"] is True
    assert segment["result_original_chars"] == len(result)
    assert len(segment["result"]) <= 2000

    preview = json.loads(segment["result"])
    assert preview["provider"] == "brave"
    assert preview["query"] == "ClickUp pricing plans 2025 2026 per seat"
    assert preview["result_truncated"] is True
    assert preview["result_original_chars"] == len(result)


def test_persisted_web_search_result_promotes_nested_diagnostics() -> None:
    result = json.dumps(
        {
            "ok": True,
            "query": "OpenSquilla search architecture",
            "mode": "technical",
            "provider_attempts": [
                {"provider": "exa", "status": "error", "error_kind": "network"},
                {"provider": "brave", "status": "success"},
            ],
            "diagnostics": {
                "selected_provider": "brave",
                "fallback_from": "exa",
                "fetched_count": 2,
                "fetch_failed_count": 1,
                "returned_chars": 2800,
                "budget_clamped": True,
                "recency_supported": False,
                "recency_degraded": True,
                "provider_attempts": [
                    {"provider": "exa", "status": "error", "error_kind": "network"},
                    {"provider": "brave", "status": "success"},
                ],
            },
            "results": [
                {
                    "title": f"Result {idx}",
                    "url": f"https://example.com/{idx}",
                    "excerpt": "x" * 900,
                }
                for idx in range(5)
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
    assert len(result) > 2000

    segment = _persisted_tool_result_segment(
        ToolResultEvent(
            tool_use_id="call_web_search_diagnostics",
            tool_name="web_search",
            result=result,
            is_error=False,
        )
    )

    assert segment["selected_provider"] == "brave"
    assert segment["fallback_from"] == "exa"
    assert segment["provider_attempt_count"] == 2
    assert segment["fetched_count"] == 2
    assert segment["fetch_failed_count"] == 1
    assert segment["returned_chars"] == 2800
    assert segment["budget_clamped"] is True
    assert segment["recency_supported"] is False
    assert segment["recency_degraded"] is True


def test_persisted_web_search_result_keeps_sources_with_complete_urls() -> None:
    long_excerpt = "Long fetched article body. " * 120
    result = json.dumps(
        {
            "ok": True,
            "query": "Lionel Messi retirement 2026",
            "mode": "auto",
            "sources": [
                {
                    "rank": 1,
                    "title": "Messi retirement dismissed with one condition clear",
                    "url": "https://thefootballfaithful.com/messi-retirement-dismissed-one-condition-clear/",
                    "canonical_url": "https://thefootballfaithful.com/messi-retirement-dismissed-one-condition-clear/",
                    "domain": "thefootballfaithful.com",
                    "provider": "duckduckgo",
                    "fetched": True,
                }
            ],
            "results": [
                {
                    "rank": 1,
                    "title": "Messi retirement dismissed with one condition clear",
                    "url": "https://thefootballfaithful.com/messi-retirement-dismissed-one-condition-clear/",
                    "excerpt": long_excerpt,
                    "provider": "duckduckgo",
                }
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
    assert len(result) > 2000

    segment = _persisted_tool_result_segment(
        ToolResultEvent(
            tool_use_id="call_web_search_sources",
            tool_name="web_search",
            result=result,
            is_error=False,
        )
    )

    assert segment["result_truncated"] is True
    assert segment["sources"] == [
        {
            "rank": 1,
            "title": "Messi retirement dismissed with one condition clear",
            "url": "https://thefootballfaithful.com/messi-retirement-dismissed-one-condition-clear/",
            "canonical_url": "https://thefootballfaithful.com/messi-retirement-dismissed-one-condition-clear/",
            "domain": "thefootballfaithful.com",
            "provider": "duckduckgo",
            "fetched": True,
        }
    ]
    assert not segment["sources"][0]["url"].endswith("…")


def test_persisted_web_search_result_derives_sources_from_results_when_missing() -> None:
    result = json.dumps(
        {
            "ok": True,
            "query": "source fallback",
            "provider_attempts": [{"provider": "duckduckgo", "status": "success"}],
            "results": [
                {
                    "rank": 1,
                    "title": "Fallback source",
                    "url": "https://example.com/fallback-source?utm=tracking",
                    "canonical_url": "https://example.com/fallback-source",
                    "domain": "example.com",
                    "provider": "duckduckgo",
                    "fetched": False,
                    "excerpt": "x" * 3000,
                }
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
    assert len(result) > 2000

    segment = _persisted_tool_result_segment(
        ToolResultEvent(
            tool_use_id="call_web_search_sources_fallback",
            tool_name="web_search",
            result=result,
            is_error=False,
        )
    )

    assert segment["sources"] == [
        {
            "rank": 1,
            "title": "Fallback source",
            "url": "https://example.com/fallback-source?utm=tracking",
            "canonical_url": "https://example.com/fallback-source",
            "domain": "example.com",
            "provider": "duckduckgo",
            "fetched": False,
        }
    ]


def test_persisted_tool_result_bounds_oversized_segment_metadata() -> None:
    result = json.dumps(
        {
            "provider": "brave",
            "query": "q" * 100_000,
            "error": "e" * 100_000,
            "results": [{"snippet": "x" * 700}],
        },
        ensure_ascii=False,
        indent=2,
    )

    segment = _persisted_tool_result_segment(
        ToolResultEvent(
            tool_use_id="call_oversized_metadata",
            tool_name="web_search",
            result=result,
            is_error=False,
        )
    )

    assert segment["provider"] == "brave"
    assert len(segment["query"]) == 256
    assert segment["query"].endswith("…")
    assert len(segment["error"]) == 256
    assert segment["error"].endswith("…")
    assert "fallback_from" not in segment
    assert len(segment["result"]) <= 2000
    assert len(json.dumps(segment, ensure_ascii=False)) < 3000


def test_persisted_tool_result_keeps_short_result_unchanged() -> None:
    result = '{"provider": "brave", "results": []}'

    segment = _persisted_tool_result_segment(
        ToolResultEvent(
            tool_use_id="call_2",
            tool_name="web_search",
            result=result,
            is_error=False,
        )
    )

    assert segment == {
        "type": "tool_result",
        "tool_use_id": "call_2",
        "name": "web_search",
        "result": result,
        "is_error": False,
    }


def test_persisted_tool_result_marks_oversized_non_json_prefix() -> None:
    result = "abc" * 1000

    segment = _persisted_tool_result_segment(
        ToolResultEvent(
            tool_use_id="call_3",
            tool_name="exec_command",
            result=result,
            is_error=False,
        )
    )

    assert segment["result"] == result[:2000]
    assert segment["result_truncated"] is True
    assert segment["result_original_chars"] == len(result)


async def test_parallel_dispatch_keeps_execution_log_references_separate(tmp_path: Path) -> None:
    registry = ToolRegistry()
    both_started = asyncio.Event()
    started = 0

    async def capture_output(label: str) -> str:
        nonlocal started
        capture = await BoundedOutputCapture.create("capture_output")
        started += 1
        if started == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), 5)
        capture.feed(label.encode())
        await capture.finish_async()
        return capture.preview()

    registry.register(ToolSpec(
        name="capture_output", description="Synthetic output",
        parameters={"label": {"type": "string"}}, required=["label"],
    ), capture_output)
    handler = build_tool_handler(registry, ToolContext(
        is_owner=True, session_key="test-session", tool_result_store_dir=str(tmp_path),
    ))
    results = await asyncio.gather(*(
        handler(ToolCall(tool_use_id=f"call-{label}", tool_name="capture_output",
                         arguments={"label": label}))
        for label in ("first", "second")
    ))
    assert current_execution_log.get() is None
    assert len({result.execution_log_handle for result in results}) == 2
    for result, label in zip(results, ("first", "second"), strict=True):
        assert not result.is_error, result.content
        assert result.execution_log_handle is not None
        assert ToolResultStore(tmp_path).read(
            result.execution_log_handle, session_id="test-session",
        ).content == label


async def test_projection_keeps_original_log_reference_when_text_is_reduced(monkeypatch) -> None:
    agent = Agent(provider=object(), config=AgentConfig())
    handle = "tr-" + "1" * 32
    original = ToolResult(
        tool_use_id="call-log", tool_name="exec_command", content="long preview",
        execution_log_handle=handle,
    )
    reduced = ToolResult(tool_use_id="call-log", tool_name="exec_command", content="short")
    monkeypatch.setattr(agent, "_project_tool_result_for_llm", AsyncMock(return_value=reduced))
    result = await agent._project_tool_result_for_delivery(original)
    assert result.execution_log_handle == handle
    assert handle in result.content
    assert result.content.count(handle) == 1


def test_history_retains_log_reference_when_result_text_is_shortened() -> None:
    handle = "tr-" + "2" * 32
    segment = _persisted_tool_result_segment(ToolResultEvent(
        tool_use_id="call-log", tool_name="exec_command", result="output\n" * 1000,
        execution_log_handle=handle,
    ), max_chars=100)
    assert segment["execution_log_handle"] == handle
    assert len(segment["result"]) <= 100
    messages = reconstruct_messages_from_entry("assistant", "", [
        {"type": "tool_use", "tool_use_id": "call-log", "name": "exec_command", "input": {}},
        segment,
    ])
    result = messages[-1].content[0]
    assert isinstance(result, ContentBlockToolResult)
    assert handle in result.content
    assert len(segment["result"]) <= 100


@pytest.mark.parametrize("load_image", [False, True])
async def test_agent_queries_missing_middle_then_finishes_after_real_process_failure(
    tmp_path: Path, load_image: bool,
) -> None:
    """Exercise real pipes/storage/dispatch with a deterministic model decision sequence."""
    marker = "MIDDLE_ERROR: missing synthetic dependency"
    executions = 0

    async def execute(command: str) -> str:
        nonlocal executions
        executions += 1
        code = (
            "import sys; chunk=b'progress row\\n'*5000; "
            "[sys.stdout.buffer.write(chunk) for _ in range(160)]; "
            f"sys.stdout.buffer.write({(marker + chr(10)).encode()!r}); "
            "[sys.stdout.buffer.write(chunk) for _ in range(160)]; sys.exit(1)"
            if command == "test" else "print('verification passed')"
        )
        argv = [sys.executable, "-c", code]
        return await shell._run_host_shell_command(
            subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv),
            cwd=str(tmp_path), env=dict(os.environ), stdin_bytes=None, effective_timeout=30,
        )

    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="exec_command", description="Synthetic verification",
        parameters={"command": {"type": "string"}}, required=["command"],
    ), execute)
    registry.register(ToolSpec(
        name="retrieve_tool_result", description="Read retained execution output",
        parameters={"handle": {"type": "string"}, "pattern": {"type": "string"}},
        required=["handle"],
    ), retrieve_tool_result)
    ctx = ToolContext(
        is_owner=True, session_key="synthetic-session", tool_result_store_dir=str(tmp_path),
        tool_result_store_session_id="synthetic-session", tool_result_retrieval_available=True,
    )

    class Provider:
        provider_name = "deterministic-log-recovery"
        model = "text-model"

        def __init__(self):
            self.calls = 0
            self.log_handle = None
            self.image_routes = 0

        async def prepare_image_continuation(self, messages, config):
            assert count_provider_image_blocks(messages) == 1
            self.image_routes += 1
            self.model = "vision-model"
            return config.model_copy(update={
                "model_vision_support": "supported",
                "model_capabilities": ModelCapabilities(supports_vision=True),
            })

        def chat(self, messages, tools=None, config=None):
            self.calls += 1
            assert isinstance(config, ChatConfig)
            assert config.system == "Complete the synthetic verification."
            assert {tool.name for tool in tools} == {
                "load_image", "exec_command", "retrieve_tool_result",
            }
            if load_image and self.calls > 1:
                assert self.model == "vision-model"
                assert config.model_vision_support == "supported"
                assert count_provider_image_blocks(messages) == 1
            results = [
                block for message in messages if isinstance(message.content, list)
                for block in message.content if isinstance(block, ContentBlockToolResult)
            ]
            return self.stream(results)

        async def stream(self, results):
            step = self.calls - int(load_image)
            if step == 0:
                name, arguments = "load_image", {}
            elif step == 1:
                name, arguments = "exec_command", {"command": "test"}
            elif step == 2:
                assert results[-1].is_error
                assert marker not in results[-1].content
                match = re.search(
                    r"(?:execution_log_handle: |tool_result_handle=)(tr-[0-9a-f]{32})",
                    results[-1].content,
                )
                assert match is not None, results[-1].content[-1000:]
                self.log_handle = match[1]
                name, arguments = "retrieve_tool_result", {
                    "handle": self.log_handle, "pattern": "MIDDLE_ERROR",
                }
            elif step == 3:
                assert marker in results[-1].content
                name, arguments = "exec_command", {"command": "verify"}
            else:
                assert "verification passed" in results[-1].content
                yield TextDeltaEvent(text="Verified successfully.")
                yield ProviderDoneEvent(stop_reason="stop")
                return
            call_id = f"call-{self.calls}"
            yield ProviderToolUseStartEvent(tool_use_id=call_id, tool_name=name)
            yield ProviderToolUseEndEvent(
                tool_use_id=call_id, tool_name=name, arguments=arguments,
            )
            yield ProviderDoneEvent(stop_reason="tool_use")

    provider = Provider()
    dispatch = build_tool_handler(registry, ctx)

    async def handler(call: ToolCall) -> ToolResult:
        if call.tool_name != "load_image":
            return await dispatch(call)
        ctx.tool_result_media[call.tool_use_id] = [{
            "mime": "image/png",
            "data": base64.b64encode(image_bytes()).decode("ascii"),
        }]
        return ToolResult(
            tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="Image loaded.",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=5, tool_result_store_dir=str(tmp_path),
            tool_result_store_session_id="synthetic-session",
            tool_result_store_session_key="synthetic-session",
            tool_result_store_agent_id="main",
            model_id="text-model", model_vision_support="unsupported",
            model_capabilities=ModelCapabilities(supports_vision=False),
            preserve_historical_images=True,
            system_prompt="Complete the synthetic verification.",
        ),
        tool_context=ctx,
        tool_handler=handler,
        tool_definitions=[ToolDefinition(
            name=name, description="Synthetic test tool",
            input_schema=ToolInputSchema(properties={}, required=[]),
        ) for name in ["load_image", *registry.list_names()]],
    )
    events = [event async for event in agent.run_turn("Run the synthetic verification.")]
    assert provider.calls == 4 + int(load_image)
    assert provider.image_routes == int(load_image)
    assert executions == 2
    tool_events = [event for event in events if isinstance(event, ToolResultEvent)]
    assert len(tool_events) == 3 + int(load_image)
    assert tool_events[int(load_image)].execution_log_handle == provider.log_handle
    assert tool_events[int(load_image)].is_error is True
    assert tool_events[-1].is_error is False
    assert not any(event.kind == "error" for event in events)
    assert sum(event.kind == "done" for event in events) == 1
