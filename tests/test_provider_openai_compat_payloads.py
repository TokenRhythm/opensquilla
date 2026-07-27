from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import pytest
import structlog.testing

from opensquilla.engine.types import ThinkingLevel
from opensquilla.provider.compat_policy import compat_policy_for_kind
from opensquilla.provider.openai import (
    OpenAIProvider,
    _build_openai_tool,
    _stream_timeout,
    _tool_schema_accepts_arguments,
)
from opensquilla.provider.selector import build_provider
from opensquilla.provider.types import (
    ChatConfig,
    ContentBlockToolResult,
    ContentBlockToolUse,
    DoneEvent,
    ErrorEvent,
    Message,
    ModelCapabilities,
    ProviderHeartbeatEvent,
    ProviderRequestCorrelation,
    ToolDefinition,
    ToolInputSchema,
    ToolUseEndEvent,
)
from opensquilla.tools.policy_helpers import ToolPolicy, apply_tool_policy
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import ToolContext

STRICT_SOURCE_EDIT_TOOL_NAMES = {
    "read_source",
    "edit_source",
    "grep_search",
    "glob_search",
    "exec_command",
    "git_status",
    "git_diff",
    "retrieve_tool_result",
}
SOURCE_EDIT_V2_TOOL_NAMES = {
    "read_source",
    "edit_source",
    "source_symbols",
    "grep_search",
    "glob_search",
    "exec_command",
    "git_status",
    "git_diff",
    "retrieve_tool_result",
}
BALANCED_SOURCE_EDIT_TOOL_NAMES = {
    "read_source",
    "edit_source",
    "create_source",
    "write_scratch",
    "source_symbols",
    "read_file",
    "grep_search",
    "glob_search",
    "list_dir",
    "exec_command",
    "git_status",
    "git_diff",
    "retrieve_tool_result",
}
PATCH_FALLBACK_SOURCE_EDIT_TOOL_NAMES = BALANCED_SOURCE_EDIT_TOOL_NAMES | {"apply_patch"}
SCAFFOLD_EDIT_TOOL_NAMES = {
    "exec_command",
    "read_file",
    "edit_file",
    "write_file",
    "glob_search",
    "grep_search",
    "list_dir",
    "git_status",
    "git_diff",
    "retrieve_tool_result",
}
SCAFFOLD_PATCH_TOOL_NAMES = SCAFFOLD_EDIT_TOOL_NAMES | {"apply_patch"}
STRICT_SOURCE_EDIT_FORBIDDEN_TOOL_NAMES = {
    "read_file",
    "list_dir",
    "write_file",
    "edit_file",
    "apply_patch",
    "execute_code",
    "background_process",
    "process",
    "git_log",
}
SCAFFOLD_FORBIDDEN_TOOL_NAMES = {
    "background_process",
    "process",
    "execute_code",
    "git_log",
    "read_source",
    "edit_source",
    "source_symbols",
}
SCAFFOLD_EDIT_FORBIDDEN_DESCRIPTION_NAMES = SCAFFOLD_FORBIDDEN_TOOL_NAMES | {
    "apply_patch",
    "read_spreadsheet",
}
SCAFFOLD_PATCH_FORBIDDEN_DESCRIPTION_NAMES = SCAFFOLD_FORBIDDEN_TOOL_NAMES | {
    "read_spreadsheet",
}


def _sse_body(model: str = "test-model") -> bytes:
    chunks = [
        {
            "model": model,
            "choices": [{"delta": {"content": "ok"}, "finish_reason": None}],
        },
        {
            "model": model,
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
    ]
    body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
    return body + b"data: [DONE]\n\n"


def _patch_transport(monkeypatch: Any, captured: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_body(),
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)


def _patch_transport_body(monkeypatch: Any, captured: dict[str, Any], body: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)


def _assistant_tool_call_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        message
        for message in messages
        if message.get("role") == "assistant" and "tool_calls" in message
    ]


def _tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [message for message in messages if message.get("role") == "tool"]


def _payload_tool_descriptions(payload: dict[str, Any]) -> str:
    return "\n".join(
        str(tool["function"].get("description", ""))
        for tool in payload.get("tools", [])
    )


def _assert_no_dashscope_duplicate_omission(messages: list[dict[str, Any]]) -> None:
    serialized = json.dumps(messages, ensure_ascii=False)
    assert "duplicate tool interaction omitted" not in serialized
    assert "arguments_sha256" not in serialized


def _patch_transport_response(
    monkeypatch: Any,
    captured: dict[str, Any],
    response: httpx.Response,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return response

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)


def _patch_get_transport_response(
    monkeypatch: Any,
    captured: dict[str, Any],
    response: httpx.Response,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        return response

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)


def _collect(provider: OpenAIProvider, cfg: ChatConfig) -> DoneEvent:
    async def _run() -> DoneEvent:
        done: DoneEvent | None = None
        async for event in provider.chat([Message(role="user", content="hi")], config=cfg):
            if isinstance(event, DoneEvent):
                done = event
        assert done is not None
        return done

    return asyncio.run(_run())


@pytest.mark.parametrize("provider_id", ["dashscope", "deepseek"])
def test_registry_openai_compat_identity_survives_into_done_event(
    monkeypatch: Any,
    provider_id: str,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = build_provider(
        provider=provider_id,
        model="test-model",
        api_key="test-key",
    )

    assert isinstance(provider, OpenAIProvider)
    assert provider.provider_name == "openai"  # adapter family stays stable
    assert provider.provider_id == provider_id
    assert provider.provider_metadata().provider_kind == provider_id
    assert provider.provider_metadata().provider_id == provider_id

    done = _collect(provider, ChatConfig())
    assert done.provider == provider_id
    assert done.model == "test-model"


def test_direct_openai_adapter_defaults_configured_identity_to_family(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(api_key="test-key", model="test-model")

    done = _collect(provider, ChatConfig())

    assert provider.provider_name == "openai"
    assert provider.provider_id == "openai"
    assert done.provider == "openai"


def test_openrouter_stream_write_timeout_defaults_to_request_timeout(
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_LLM_STREAM_WRITE_TIMEOUT_SECONDS", raising=False)

    timeout = _stream_timeout(120.0)

    assert timeout.write == 120.0


def test_openrouter_stream_write_timeout_allows_env_override(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_LLM_STREAM_WRITE_TIMEOUT_SECONDS", "75")

    timeout = _stream_timeout(120.0)

    assert timeout.write == 75.0


def test_openrouter_stream_timeout_emits_heartbeat_before_non_stream_fallback(
    monkeypatch: Any,
) -> None:
    class TimeoutStream:
        async def __aenter__(self) -> Any:
            raise httpx.ReadTimeout("stream idle")

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    class TimeoutClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> TimeoutClient:
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            return None

        def stream(self, *args: Any, **kwargs: Any) -> TimeoutStream:
            return TimeoutStream()

    class SlowFallbackProvider(OpenAIProvider):
        async def _complete_non_stream(self, **kwargs: Any):
            await asyncio.sleep(0.05)
            yield ErrorEvent(message="fallback finished", code="timeout")

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", TimeoutClient)
    provider = SlowFallbackProvider(
        api_key="test",
        model="deepseek/deepseek-v4-flash",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )

    async def _first_event() -> Any:
        events = provider.chat(
            [Message(role="user", content="hi")],
            config=ChatConfig(timeout=1.0),
        )
        return await asyncio.wait_for(anext(events), timeout=0.02)

    with structlog.testing.capture_logs() as captured:
        event = asyncio.run(_first_event())

    assert isinstance(event, ProviderHeartbeatEvent)
    assert event.phase == "llm_fallback"
    assert any(
        item["event"] == "openrouter.stream_timeout_fallback_started"
        for item in captured
    )


def test_stream_timeout_fallback_preserves_tokenrhythm_correlation_headers(
    monkeypatch: Any,
) -> None:
    stream_headers: dict[str, str] = {}
    fallback_headers: dict[str, str] = {}

    class TimeoutStream:
        async def __aenter__(self) -> Any:
            raise httpx.ReadTimeout("stream idle")

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    class TimeoutClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> TimeoutClient:
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            return None

        def stream(self, *args: Any, **kwargs: Any) -> TimeoutStream:
            stream_headers.update(kwargs["headers"])
            return TimeoutStream()

    class CapturingFallbackProvider(OpenAIProvider):
        async def _complete_non_stream(self, **kwargs: Any):
            fallback_headers.update(kwargs["headers"])
            yield DoneEvent(model="deepseek-v4-flash")

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", TimeoutClient)
    provider = CapturingFallbackProvider(
        api_key="test",
        model="deepseek-v4-flash",
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
        compat=compat_policy_for_kind("openrouter"),
    )
    correlation = ProviderRequestCorrelation(
        session_id="session-1",
        turn_id="turn-1",
        execution_id="execution-1",
        call_kind="agent.chat",
    )

    _collect(
        provider,
        ChatConfig(
            timeout=1.0,
            provider_request_correlation=correlation,
        ),
    )

    expected = {
        "X-OpenSquilla-Session-Id": "session-1",
        "X-OpenSquilla-Turn-Id": "turn-1",
        "X-OpenSquilla-Execution-Id": "execution-1",
        "X-OpenSquilla-Call-Kind": "agent.chat",
    }
    assert {name: stream_headers[name] for name in expected} == expected
    assert {name: fallback_headers[name] for name in expected} == expected


def test_dashscope_stream_timeout_emits_heartbeat_before_non_stream_fallback(
    monkeypatch: Any,
) -> None:
    class TimeoutStream:
        async def __aenter__(self) -> Any:
            raise httpx.ReadTimeout("stream idle")

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    class TimeoutClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> TimeoutClient:
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            return None

        def stream(self, *args: Any, **kwargs: Any) -> TimeoutStream:
            return TimeoutStream()

    class SlowFallbackProvider(OpenAIProvider):
        async def _complete_non_stream(self, **kwargs: Any):
            await asyncio.sleep(0.05)
            yield ErrorEvent(message="fallback finished", code="timeout")

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", TimeoutClient)
    provider = SlowFallbackProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )

    async def _first_event() -> Any:
        events = provider.chat(
            [Message(role="user", content="hi")],
            config=ChatConfig(timeout=1.0),
        )
        return await asyncio.wait_for(anext(events), timeout=0.02)

    with structlog.testing.capture_logs() as captured:
        event = asyncio.run(_first_event())

    assert isinstance(event, ProviderHeartbeatEvent)
    assert event.phase == "llm_fallback"
    assert any(
        item["event"] == "dashscope.non_stream_fallback_started" for item in captured
    )


def test_tokenrhythm_chat_adds_app_attribution_headers(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-v4-flash",
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
    )

    _collect(provider, ChatConfig())

    assert captured["url"] == "https://tokenrhythm.studio/v1/chat/completions"
    assert captured["headers"].get("HTTP-Referer") == "https://opensquilla.ai"
    assert captured["headers"].get("X-Title") == "OpenSquilla"


def test_tokenrhythm_chat_adds_session_correlation_headers_only(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-v4-flash",
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
    )

    _collect(
        provider,
        ChatConfig(
            provider_request_correlation=ProviderRequestCorrelation(
                session_id="session-1",
                turn_id="turn-1",
                execution_id="execution-1",
                call_kind="agent.chat",
            )
        ),
    )

    assert captured["headers"].get("X-OpenSquilla-Session-Id") == "session-1"
    assert captured["headers"].get("X-OpenSquilla-Turn-Id") == "turn-1"
    assert captured["headers"].get("X-OpenSquilla-Execution-Id") == "execution-1"
    assert captured["headers"].get("X-OpenSquilla-Call-Kind") == "agent.chat"
    serialized_payload = json.dumps(captured["payload"], sort_keys=True)
    assert "session-1" not in serialized_payload
    assert "turn-1" not in serialized_payload
    assert "execution-1" not in serialized_payload
    assert "agent.chat" not in serialized_payload


def test_tokenrhythm_chat_never_forwards_correlation_across_redirects(
    monkeypatch: Any,
) -> None:
    requests: list[httpx.Request] = []
    client_options: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "tokenrhythm.studio":
            return httpx.Response(
                307,
                headers={"location": "https://untrusted.example/v1/chat/completions"},
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_body(),
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        client_options["follow_redirects"] = kwargs.get("follow_redirects")
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(
        "opensquilla.provider.openai.httpx.AsyncClient",
        patched_async_client,
    )
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-v4-flash",
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
    )

    config = ChatConfig(
        provider_request_correlation=ProviderRequestCorrelation(
            session_id="session-1",
            turn_id="turn-1",
            execution_id="execution-1",
            call_kind="agent.chat",
        )
    )

    async def collect_events() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="hi")],
                config=config,
            )
        ]

    events = asyncio.run(collect_events())

    assert client_options["follow_redirects"] is False
    assert len(requests) == 1
    assert requests[0].url.host == "tokenrhythm.studio"
    assert requests[0].headers["X-OpenSquilla-Session-Id"] == "session-1"
    assert any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.parametrize(
    ("provider_kind", "base_url"),
    [
        ("openrouter", "https://openrouter.ai/api/v1"),
        ("tokenrhythm", "https://proxy.example.com/v1"),
    ],
)
def test_session_correlation_is_not_sent_to_other_or_custom_provider_origins(
    monkeypatch: Any,
    provider_kind: str,
    base_url: str,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="test-model",
        base_url=base_url,
        provider_kind=provider_kind,
    )

    _collect(
        provider,
        ChatConfig(
            provider_request_correlation=ProviderRequestCorrelation(
                session_id="session-1",
                turn_id="turn-1",
                execution_id="execution-1",
                call_kind="agent.chat",
            )
        ),
    )

    assert "X-OpenSquilla-Session-Id" not in captured["headers"]
    assert "X-OpenSquilla-Turn-Id" not in captured["headers"]
    assert "X-OpenSquilla-Execution-Id" not in captured["headers"]
    assert "X-OpenSquilla-Call-Kind" not in captured["headers"]


def test_tokenrhythm_list_models_adds_app_attribution_headers(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_get_transport_response(
        monkeypatch,
        captured,
        httpx.Response(
            200,
            json={"data": []},
            request=httpx.Request("GET", "https://tokenrhythm.studio/v1/models"),
        ),
    )
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-v4-flash",
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
    )

    assert asyncio.run(provider.list_models()) == []

    assert captured["url"] == "https://tokenrhythm.studio/v1/models"
    assert captured["headers"].get("HTTP-Referer") == "https://opensquilla.ai"
    assert captured["headers"].get("X-Title") == "OpenSquilla"


def test_openrouter_list_models_reports_openrouter_provider(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_get_transport_response(
        monkeypatch,
        captured,
        httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "deepseek/deepseek-v4-flash",
                        "name": "DeepSeek V4 Flash",
                        "context_length": 128000,
                        "top_provider": {"max_completion_tokens": 8192},
                    }
                ]
            },
            request=httpx.Request("GET", "https://openrouter.ai/api/v1/models"),
        ),
    )
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek/deepseek-v4-flash",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )

    rows = asyncio.run(provider.list_models())

    assert captured["url"] == "https://openrouter.ai/api/v1/models"
    assert rows[0].provider == "openrouter"
    assert rows[0].model_id == "deepseek/deepseek-v4-flash"


def test_openrouter_http_error_names_provider_request(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport_response(
        monkeypatch,
        captured,
        httpx.Response(
            500,
            content=b"Internal Server Error",
            request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        ),
    )
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek/deepseek-v4-flash",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )

    events = _collect_events(provider, ChatConfig())

    error = next(event for event in events if isinstance(event, ErrorEvent))
    assert error.code == "500"
    assert error.message == "OpenRouter chat request failed (HTTP 500): Internal Server Error"


def test_openai_compatible_provider_writes_llm_trace(monkeypatch: Any, tmp_path: Any) -> None:
    captured: dict[str, Any] = {}
    trace_path = tmp_path / "llm_calls.jsonl"
    monkeypatch.setenv("OPENSQUILLA_LLM_TRACE_RECORDER", "full")
    monkeypatch.setenv("OPENSQUILLA_LLM_TRACE_PATH", str(trace_path))
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )

    events = _collect_events(provider, ChatConfig(cache_mode="on"))

    assert any(isinstance(event, DoneEvent) for event in events)
    rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert [row["event"] for row in rows] == [
        "llm.request",
        "llm.response_chunk",
        "llm.response_chunk",
        "llm.response",
    ]
    assert rows[0]["provider"] == "dashscope"
    assert rows[0]["payload"]["model"] == "qwen3.6-flash"
    assert rows[0]["headers"]["Authorization"] == "[REDACTED]"
    assert rows[-1]["usage"]["input_tokens"] == 2
    assert rows[-1]["assistant_text"] == "ok"


def test_llm_trace_request_metadata_carries_compaction_proof(
    monkeypatch: Any, tmp_path: Any
) -> None:
    captured: dict[str, Any] = {}
    trace_path = tmp_path / "llm_calls.jsonl"
    monkeypatch.setenv("OPENSQUILLA_LLM_TRACE_RECORDER", "full")
    monkeypatch.setenv("OPENSQUILLA_LLM_TRACE_PATH", str(trace_path))
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )

    events = _collect_events(provider, ChatConfig(provider_request_max_chars=100_000))

    assert any(isinstance(event, DoneEvent) for event in events)
    rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    request_proof = rows[0]["metadata"]["request_proof"]
    assert request_proof["compaction_tier"] == 0
    assert request_proof["retry_count"] == 0
    assert "compaction_tiny_guard_chars" in request_proof
    assert "compaction_protect_recent_assistant" in request_proof


def test_openrouter_deepseek_v4_returns_reasoning_content_from_details(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    chunks = [
        {
            "model": "deepseek/deepseek-v4-flash",
            "choices": [
                {
                    "delta": {
                        "reasoning_details": [
                            {"type": "reasoning.text", "text": "I considered the request."}
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "model": "deepseek/deepseek-v4-flash",
            "choices": [{"delta": {"content": "ok"}, "finish_reason": None}],
        },
        {
            "model": "deepseek/deepseek-v4-flash",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
    ]
    body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
    body += b"data: [DONE]\n\n"
    _patch_transport_body(monkeypatch, captured, body)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek/deepseek-v4-flash",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
        provider_routing={"deepseek/deepseek-v4-flash": "deepseek"},
    )
    cfg = ChatConfig(
        thinking=True,
        thinking_level=ThinkingLevel.HIGH,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="openrouter",
        ),
    )

    done = _collect(provider, cfg)

    assert captured["payload"]["provider"] == {
        "order": ["deepseek"],
        "allow_fallbacks": True,
    }
    assert captured["payload"]["reasoning"] == {"effort": "high"}
    assert done.reasoning_content == "I considered the request."


def _collect_events(
    provider: OpenAIProvider,
    cfg: ChatConfig,
    tools: list[ToolDefinition] | None = None,
) -> list[Any]:
    async def _run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="hi")],
                config=cfg,
                tools=tools,
            )
        ]

    return asyncio.run(_run())


def _assert_invalid_native_arguments_fail_closed(
    events: list[Any],
    *,
    raw_arguments: str,
) -> None:
    assert not any(isinstance(event, ToolUseEndEvent) for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)
    error = next(
        event
        for event in events
        if isinstance(event, ErrorEvent) and event.code == "incomplete_tool_call"
    )
    assert raw_arguments not in error.message
    assert "_raw" not in error.message


def test_strict_source_edit_profile_provider_payload_exposes_exact_tool_surface(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    registry = get_default_registry()
    ctx = apply_tool_policy(
        ToolContext(is_owner=True),
        available_tools=registry.list_names(),
        agent_policy=ToolPolicy(profile="repo_coding_source_edit_strict"),
    )
    tools = registry.to_tool_definitions(ctx)
    cfg = ChatConfig(
        model_capabilities=ModelCapabilities(
            supports_tools=True,
            reasoning_format="dashscope",
        )
    )

    _collect_events(provider, cfg, tools=tools)

    tool_names = {
        tool["function"]["name"]
        for tool in captured["payload"]["tools"]
    }
    assert tool_names == STRICT_SOURCE_EDIT_TOOL_NAMES
    assert STRICT_SOURCE_EDIT_FORBIDDEN_TOOL_NAMES.isdisjoint(tool_names)


def test_source_edit_v2_profile_provider_payload_exposes_exact_tool_surface(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    registry = get_default_registry()
    ctx = apply_tool_policy(
        ToolContext(is_owner=True),
        available_tools=registry.list_names(),
        agent_policy=ToolPolicy(profile="repo_coding_source_edit_v2"),
    )
    tools = registry.to_tool_definitions(ctx)
    cfg = ChatConfig(
        model_capabilities=ModelCapabilities(
            supports_tools=True,
            reasoning_format="dashscope",
        )
    )

    _collect_events(provider, cfg, tools=tools)

    tool_names = {
        tool["function"]["name"]
        for tool in captured["payload"]["tools"]
    }
    assert tool_names == SOURCE_EDIT_V2_TOOL_NAMES
    assert STRICT_SOURCE_EDIT_FORBIDDEN_TOOL_NAMES.isdisjoint(tool_names)


def test_balanced_source_edit_profile_provider_payload_exposes_exact_tool_surface(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    registry = get_default_registry()
    ctx = apply_tool_policy(
        ToolContext(is_owner=True),
        available_tools=registry.list_names(),
        agent_policy=ToolPolicy(profile="repo_coding_source_edit_balanced"),
    )
    tools = registry.to_tool_definitions(ctx)
    cfg = ChatConfig(
        model_capabilities=ModelCapabilities(
            supports_tools=True,
            reasoning_format="dashscope",
        )
    )

    _collect_events(provider, cfg, tools=tools)

    tool_names = {
        tool["function"]["name"]
        for tool in captured["payload"]["tools"]
    }
    assert tool_names == BALANCED_SOURCE_EDIT_TOOL_NAMES
    assert {"write_file", "edit_file", "apply_patch", "execute_code"}.isdisjoint(tool_names)


def test_patch_fallback_source_edit_profile_provider_payload_adds_only_apply_patch(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    registry = get_default_registry()
    ctx = apply_tool_policy(
        ToolContext(is_owner=True),
        available_tools=registry.list_names(),
        agent_policy=ToolPolicy(profile="repo_coding_source_edit_patch_fallback"),
    )
    tools = registry.to_tool_definitions(ctx)
    cfg = ChatConfig(
        model_capabilities=ModelCapabilities(
            supports_tools=True,
            reasoning_format="dashscope",
        )
    )

    _collect_events(provider, cfg, tools=tools)

    tool_names = {
        tool["function"]["name"]
        for tool in captured["payload"]["tools"]
    }
    assert tool_names == PATCH_FALLBACK_SOURCE_EDIT_TOOL_NAMES
    assert {"write_file", "edit_file", "execute_code"}.isdisjoint(tool_names)


def test_scaffold_edit_profile_provider_payload_exposes_exact_tool_surface(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    registry = get_default_registry()
    ctx = apply_tool_policy(
        ToolContext(is_owner=True),
        available_tools=registry.list_names(),
        agent_policy=ToolPolicy(profile="repo_coding_scaffold_edit"),
    )
    tools = registry.to_tool_definitions(ctx)

    _collect_events(provider, ChatConfig(), tools=tools)

    tool_names = {
        tool["function"]["name"]
        for tool in captured["payload"]["tools"]
    }
    assert tool_names == SCAFFOLD_EDIT_TOOL_NAMES
    assert SCAFFOLD_FORBIDDEN_TOOL_NAMES.isdisjoint(tool_names)
    assert "apply_patch" not in tool_names
    descriptions = _payload_tool_descriptions(captured["payload"])
    for hidden_name in SCAFFOLD_EDIT_FORBIDDEN_DESCRIPTION_NAMES:
        assert hidden_name not in descriptions


def test_scaffold_patch_profile_provider_payload_adds_only_apply_patch(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    registry = get_default_registry()
    ctx = apply_tool_policy(
        ToolContext(is_owner=True),
        available_tools=registry.list_names(),
        agent_policy=ToolPolicy(profile="repo_coding_scaffold_patch"),
    )
    tools = registry.to_tool_definitions(ctx)

    _collect_events(provider, ChatConfig(), tools=tools)

    tool_names = {
        tool["function"]["name"]
        for tool in captured["payload"]["tools"]
    }
    assert tool_names == SCAFFOLD_PATCH_TOOL_NAMES
    assert SCAFFOLD_FORBIDDEN_TOOL_NAMES.isdisjoint(tool_names)
    descriptions = _payload_tool_descriptions(captured["payload"])
    assert "apply_patch" in descriptions
    for hidden_name in SCAFFOLD_PATCH_FORBIDDEN_DESCRIPTION_NAMES:
        assert hidden_name not in descriptions


def test_tool_input_schema_omits_additional_properties_by_default() -> None:
    tool = ToolDefinition(
        name="lookup",
        description="Lookup a value.",
        input_schema=ToolInputSchema(properties={"q": {"type": "string"}}, required=["q"]),
    )

    payload = _build_openai_tool(tool)

    assert payload["function"]["parameters"] == {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }
    assert _tool_schema_accepts_arguments(tool, {"q": "hi", "extra": "ignored"})


def test_tool_input_schema_supports_explicit_additional_properties_false() -> None:
    tool = ToolDefinition(
        name="lookup",
        description="Lookup a value.",
        input_schema=ToolInputSchema(
            properties={"q": {"type": "string"}},
            required=["q"],
            additional_properties=False,
        ),
    )

    payload = _build_openai_tool(tool)

    assert payload["function"]["parameters"] == {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
        "additionalProperties": False,
    }
    assert _tool_schema_accepts_arguments(tool, {"q": "hi"})
    assert not _tool_schema_accepts_arguments(tool, {"q": "hi", "extra": "rejected"})


def test_deepseek_thinking_uses_provider_thinking_field_not_openai_reasoning_effort(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        provider_kind="deepseek",
    )
    cfg = ChatConfig(
        thinking=True,
        thinking_level=ThinkingLevel.HIGH,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="deepseek",
        ),
    )

    _collect(provider, cfg)

    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert captured["payload"]["thinking"] == {"type": "enabled"}
    assert captured["payload"]["reasoning_effort"] == "high"


def test_deepseek_non_thinking_sends_provider_disabled_for_default_thinking_model(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        provider_kind="deepseek",
    )
    cfg = ChatConfig(
        thinking=False,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="deepseek",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in captured["payload"]


def test_deepseek_tool_replay_preserves_reasoning_content_in_payload(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.com",
        provider_kind="deepseek",
    )
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="call_lookup",
                    name="lookup",
                    input={"q": "cache"},
                )
            ],
            reasoning_content="I need to inspect the cache state before answering.",
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="call_lookup",
                    content="cache is warm",
                )
            ],
        ),
        Message(role="user", content="continue"),
    ]
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="deepseek",
        ),
    )

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert captured["payload"]["messages"][0]["role"] == "assistant"
    assert captured["payload"]["messages"][0]["tool_calls"][0]["id"] == "call_lookup"
    assert (
        captured["payload"]["messages"][0]["reasoning_content"]
        == "I need to inspect the cache state before answering."
    )
    assert captured["payload"]["messages"][1] == {
        "role": "tool",
        "tool_call_id": "call_lookup",
        "content": "cache is warm",
    }


def test_deepseek_v4_tool_replay_adds_empty_reasoning_content_when_missing(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        provider_kind="deepseek",
    )
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="call_lookup",
                    name="lookup",
                    input={"q": "cache"},
                )
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="call_lookup",
                    content="cache is warm",
                )
            ],
        ),
        Message(role="user", content="continue"),
    ]
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="deepseek",
        ),
    )

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert captured["payload"]["messages"][0]["reasoning_content"] == ""


def test_deepseek_v4_text_replay_adds_empty_reasoning_content_when_missing(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        provider_kind="deepseek",
    )
    messages = [
        Message(role="assistant", content="Prior non-thinking assistant turn."),
        Message(role="user", content="continue in thinking mode"),
    ]
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="deepseek",
        ),
    )

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert captured["payload"]["messages"][0] == {
        "role": "assistant",
        "content": "Prior non-thinking assistant turn.",
        "reasoning_content": "",
    }


def test_deepseek_v4_non_thinking_replays_prior_reasoning_content(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        provider_kind="deepseek",
    )
    messages = [
        Message(
            role="assistant",
            content="previous answer",
            reasoning_content="prior thinking from earlier deepseek turn",
        ),
        Message(role="user", content="continue"),
    ]
    cfg = ChatConfig(
        thinking=False,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="deepseek",
        ),
    )

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert captured["payload"]["thinking"] == {"type": "disabled"}
    assert (
        captured["payload"]["messages"][0]["reasoning_content"]
        == "prior thinking from earlier deepseek turn"
    )


def test_deepseek_v4_replays_reasoning_content_without_catalog_capabilities(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        provider_kind="deepseek",
    )
    messages = [
        Message(
            role="assistant",
            content="previous answer",
            reasoning_content="prior thinking from direct deepseek",
        ),
        Message(role="user", content="continue"),
    ]
    cfg = ChatConfig(thinking=True, model_capabilities=None)

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert captured["payload"]["thinking"] == {"type": "enabled"}
    assert captured["payload"]["reasoning_effort"] == "high"
    assert (
        captured["payload"]["messages"][0]["reasoning_content"]
        == "prior thinking from direct deepseek"
    )


def test_openrouter_reasoning_model_replays_reasoning_content_by_capability(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="anthropic/claude-sonnet-4.5",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )
    messages = [
        Message(
            role="assistant",
            content="previous answer",
            reasoning_content="openrouter-native reasoning should be replayed",
        ),
        Message(role="user", content="continue"),
    ]
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="openrouter",
        ),
    )

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert (
        captured["payload"]["messages"][0]["reasoning_content"]
        == "openrouter-native reasoning should be replayed"
    )


def test_non_deepseek_reasoning_model_does_not_replay_reasoning_content(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="gemini-2.5-pro",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        provider_kind="gemini",
    )
    messages = [
        Message(
            role="assistant",
            content="previous answer",
            reasoning_content="provider-internal reasoning must not be replayed",
        ),
        Message(role="user", content="continue"),
    ]
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="gemini",
        ),
    )

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert "reasoning_content" not in captured["payload"]["messages"][0]


def test_deepseek_non_v4_model_does_not_replay_reasoning_content(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-chat",
        base_url="https://api.deepseek.com/v1",
        provider_kind="deepseek",
    )
    messages = [
        Message(
            role="assistant",
            content="previous answer",
            reasoning_content="must not be replayed for non-v4 direct DeepSeek",
        ),
        Message(role="user", content="continue"),
    ]
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="deepseek",
        ),
    )

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert "reasoning_content" not in captured["payload"]["messages"][0]


def test_deepseek_reasoning_format_without_deepseek_model_does_not_replay_reasoning_content(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="custom-reasoning-model",
        base_url="https://api.deepseek.com/v1",
        provider_kind="deepseek",
    )
    messages = [
        Message(
            role="assistant",
            content="previous answer",
            reasoning_content="must not be replayed for a non-DeepSeek model",
        ),
        Message(role="user", content="continue"),
    ]
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="deepseek",
        ),
    )

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert "reasoning_content" not in captured["payload"]["messages"][0]


def test_gemini_reasoning_uses_openai_compatible_reasoning_effort(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="gemini-2.5-flash",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        provider_kind="gemini",
    )
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="gemini",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["reasoning_effort"] == "medium"
    assert "thinking" not in captured["payload"]


def test_gemini_25_flash_lite_non_thinking_uses_reasoning_effort_none(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="gemini-2.5-flash-lite",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        provider_kind="gemini",
    )
    cfg = ChatConfig(
        thinking=False,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="gemini",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["reasoning_effort"] == "none"
    assert "thinking" not in captured["payload"]


def test_zai_thinking_uses_provider_thinking_object(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="glm-4.5",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        provider_kind="zhipu",
    )
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="zai",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in captured["payload"]


def test_glm_5_1_thinking_uses_provider_thinking_object(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="glm-5.1",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        provider_kind="zhipu",
    )
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="zai",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in captured["payload"]


def test_zai_non_thinking_sends_provider_disabled_for_default_thinking_model(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="glm-5.1",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        provider_kind="zhipu",
    )
    cfg = ChatConfig(
        thinking=False,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="zai",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["thinking"] == {"type": "disabled"}


def test_dashscope_cache_on_marks_system_and_latest_user(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    cfg = ChatConfig(
        system="stable base",
        cache_breakpoints=[{"text": "stable base", "cache": "true"}],
        cache_mode="on",
    )

    _collect(provider, cfg)

    payload = captured["payload"]
    assert "cache_control" not in payload
    assert payload["messages"][0] == {
        "role": "system",
        "content": [
            {
                "type": "text",
                "text": "stable base",
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }
    assert payload["messages"][1] == {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": "hi",
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }


def test_dashscope_cache_on_marks_recent_tool_history_without_exceeding_limit(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    cfg = ChatConfig(
        system="stable base",
        cache_breakpoints=[{"text": "stable base", "cache": "true"}],
        cache_mode="on",
    )

    async def _run() -> None:
        async for _event in provider.chat(
            [
                Message(role="user", content="initial issue"),
                Message(role="assistant", content="older analysis"),
                Message(role="user", content="older tool result"),
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(
                            id="call_1",
                            name="exec_command",
                            input={"cmd": "pytest"},
                        )
                    ],
                ),
                Message(
                    role="user",
                    content=[
                        ContentBlockToolResult(
                            tool_use_id="call_1",
                            content="long pytest output",
                        )
                    ],
                ),
                Message(role="assistant", content="I will patch the failure."),
            ],
            config=cfg,
        ):
            pass

    asyncio.run(_run())

    messages = captured["payload"]["messages"]
    marker_positions = [
        (message_index, message["role"], block_index)
        for message_index, message in enumerate(messages)
        if isinstance(message.get("content"), list)
        for block_index, block in enumerate(message["content"])
        if block.get("cache_control") == {"type": "ephemeral"}
    ]
    assert marker_positions == [
        (0, "system", 0),
        (1, "user", 0),
        (5, "tool", 0),
        (6, "assistant", 0),
    ]
    fresh_tool_content = messages[5]["content"]
    assert fresh_tool_content == [
        {
            "type": "text",
            "text": "long pytest output",
            "cache_control": {"type": "ephemeral"},
        }
    ]


def test_dashscope_cache_on_keeps_initial_user_marker_in_long_history(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    cfg = ChatConfig(
        system="stable base",
        cache_breakpoints=[{"text": "stable base", "cache": "true"}],
        cache_mode="on",
    )

    async def _run() -> None:
        async for _event in provider.chat(
            [
                Message(role="user", content="initial issue"),
                Message(role="assistant", content="analysis 1"),
                Message(role="user", content="tool result 1"),
                Message(role="assistant", content="analysis 2"),
                Message(role="user", content="tool result 2"),
                Message(role="assistant", content="analysis 3"),
            ],
            config=cfg,
        ):
            pass

    asyncio.run(_run())

    messages = captured["payload"]["messages"]
    marker_positions = [
        (message_index, message["role"], block_index)
        for message_index, message in enumerate(messages)
        if isinstance(message.get("content"), list)
        for block_index, block in enumerate(message["content"])
        if block.get("cache_control") == {"type": "ephemeral"}
    ]
    assert marker_positions == [
        (0, "system", 0),
        (1, "user", 0),
        (5, "user", 0),
        (6, "assistant", 0),
    ]


def test_dashscope_cache_off_does_not_mark_messages(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    cfg = ChatConfig(
        system="stable base",
        cache_breakpoints=[{"text": "stable base", "cache": "true"}],
        cache_mode="off",
    )

    _collect(provider, cfg)

    payload = captured["payload"]
    assert "cache_control" not in payload
    assert payload["messages"][0] == {"role": "system", "content": "stable base"}
    assert payload["messages"][1] == {"role": "user", "content": "hi"}


def test_openrouter_sends_configured_json_output_schema(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek/deepseek-v4-pro",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"outcome": {"type": "string", "enum": ["allow", "deny"]}},
        "required": ["outcome"],
    }

    _collect(
        provider,
        ChatConfig(output_json_schema=schema, output_json_schema_strict=False),
    )

    assert captured["payload"]["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "structured_output",
            "strict": False,
            "schema": schema,
        },
    }


def test_openrouter_omits_response_format_without_output_schema(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek/deepseek-v4-pro",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )

    _collect(provider, ChatConfig())

    assert "response_format" not in captured["payload"]


def test_dashscope_repeated_history_tool_calls_preserves_duplicate_replay_protocol(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )

    async def _run() -> None:
        async for _event in provider.chat(
            [
                Message(role="user", content="build and poll"),
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(
                            id="call_1",
                            name="process",
                            input={"action": "poll", "session_id": "abc"},
                        )
                    ],
                ),
                Message(
                    role="user",
                    content=[
                        ContentBlockToolResult(
                            tool_use_id="call_1",
                            content='{"status":"running"}',
                        )
                    ],
                ),
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(
                            id="call_2",
                            name="process",
                            input={"action": "poll", "session_id": "abc"},
                        )
                    ],
                ),
            ],
            config=ChatConfig(),
        ):
            pass

    asyncio.run(_run())

    messages = captured["payload"]["messages"]
    tool_call_messages = _assistant_tool_call_messages(messages)
    assert [
        message["tool_calls"][0]["id"] for message in tool_call_messages
    ] == ["call_1", "call_2"]
    for message in tool_call_messages:
        raw_args = message["tool_calls"][0]["function"]["arguments"]
        assert json.loads(raw_args) == {"action": "poll", "session_id": "abc"}
    tool_messages = _tool_messages(messages)
    assert [message["tool_call_id"] for message in tool_messages] == ["call_1"]
    assert tool_messages[0]["content"] == '{"status":"running"}'
    _assert_no_dashscope_duplicate_omission(messages)


def test_dashscope_repeated_non_process_tool_calls_preserves_duplicate_replay_protocol(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )

    async def _run() -> None:
        async for _event in provider.chat(
            [
                Message(role="user", content="read twice"),
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(
                            id="call_1",
                            name="read_file",
                            input={"path": "/tmp/example.txt"},
                        )
                    ],
                ),
                Message(
                    role="user",
                    content=[
                        ContentBlockToolResult(
                            tool_use_id="call_1",
                            content="one",
                        )
                    ],
                ),
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(
                            id="call_2",
                            name="read_file",
                            input={"path": "/tmp/example.txt"},
                        )
                    ],
                ),
            ],
            config=ChatConfig(),
        ):
            pass

    asyncio.run(_run())

    messages = captured["payload"]["messages"]
    tool_call_messages = _assistant_tool_call_messages(messages)
    assert [
        message["tool_calls"][0]["id"] for message in tool_call_messages
    ] == ["call_1", "call_2"]
    for message in tool_call_messages:
        raw_args = message["tool_calls"][0]["function"]["arguments"]
        assert json.loads(raw_args) == {"path": "/tmp/example.txt"}
    tool_messages = _tool_messages(messages)
    assert [message["tool_call_id"] for message in tool_messages] == ["call_1"]
    assert tool_messages[0]["content"] == "one"
    _assert_no_dashscope_duplicate_omission(messages)


def test_dashscope_repeated_exec_command_history_preserves_duplicate_replay_protocol(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    command = "cd /workspace/project && cargo test -p mypkg case1"

    async def _run() -> None:
        async for _event in provider.chat(
            [
                Message(role="user", content="run twice"),
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(
                            id="call_1",
                            name="exec_command",
                            input={"command": command},
                        )
                    ],
                ),
                Message(
                    role="user",
                    content=[
                        ContentBlockToolResult(
                            tool_use_id="call_1",
                            content="failed",
                            is_error=True,
                        )
                    ],
                ),
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(
                            id="call_2",
                            name="exec_command",
                            input={"command": command},
                        )
                    ],
                ),
            ],
            config=ChatConfig(),
        ):
            pass

    asyncio.run(_run())

    messages = captured["payload"]["messages"]
    tool_call_messages = _assistant_tool_call_messages(messages)
    assert [
        message["tool_calls"][0]["id"] for message in tool_call_messages
    ] == ["call_1", "call_2"]
    for message in tool_call_messages:
        raw_args = message["tool_calls"][0]["function"]["arguments"]
        assert json.loads(raw_args) == {"command": command}
        assert "approval_id" not in raw_args
    tool_messages = _tool_messages(messages)
    assert [message["tool_call_id"] for message in tool_messages] == ["call_1"]
    assert tool_messages[0]["content"] == "failed"
    _assert_no_dashscope_duplicate_omission(messages)


def test_dashscope_repeated_exec_command_summary_preserves_structured_history(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    command = "pytest tests/test_widget.py::test_handles_blank"

    async def _run() -> None:
        async for _event in provider.chat(
            [
                Message(role="user", content="run test, edit, run again"),
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(
                            id="call_1",
                            name="exec_command",
                            input={"command": command},
                        )
                    ],
                ),
                Message(
                    role="user",
                    content=[
                        ContentBlockToolResult(
                            tool_use_id="call_1",
                            content=(
                                "FAILED tests/test_widget.py::test_handles_blank\n"
                                "E AssertionError: expected 4, got 3\n"
                                "exit code: 1"
                            ),
                            is_error=True,
                        )
                    ],
                ),
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(
                            id="call_edit",
                            name="apply_patch",
                            input={"patch": "*** Begin Patch\n*** End Patch"},
                        )
                    ],
                ),
                Message(
                    role="user",
                    content=[
                        ContentBlockToolResult(
                            tool_use_id="call_edit",
                            content="patch applied",
                        )
                    ],
                ),
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(
                            id="call_2",
                            name="exec_command",
                            input={"command": command},
                        )
                    ],
                ),
            ],
            config=ChatConfig(),
        ):
            pass

    asyncio.run(_run())

    messages = captured["payload"]["messages"]
    tool_call_messages = _assistant_tool_call_messages(messages)
    assert [
        message["tool_calls"][0]["id"] for message in tool_call_messages
    ] == ["call_1", "call_edit", "call_2"]
    tool_messages = _tool_messages(messages)
    assert [message["tool_call_id"] for message in tool_messages] == [
        "call_1",
        "call_edit",
    ]
    assert "AssertionError" in tool_messages[0]["content"]
    assert tool_messages[1]["content"] == "patch applied"
    _assert_no_dashscope_duplicate_omission(messages)


def test_dashscope_repeated_apply_patch_history_preserves_duplicate_replay_protocol(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    patch = "*** Begin Patch: tests/jq.test\n@@ -1,3 +1,5 @@\n+new\n*** End Patch: tests/jq.test"

    async def _run() -> None:
        async for _event in provider.chat(
            [
                Message(role="user", content="patch"),
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(
                            id="call_1",
                            name="apply_patch",
                            input={"patch": patch},
                        )
                    ],
                ),
                Message(
                    role="user",
                    content=[
                        ContentBlockToolResult(
                            tool_use_id="call_1",
                            content="patch failed",
                            is_error=True,
                        )
                    ],
                ),
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(
                            id="call_2",
                            name="apply_patch",
                            input={"patch": patch},
                        )
                    ],
                ),
            ],
            config=ChatConfig(),
        ):
            pass

    asyncio.run(_run())

    messages = captured["payload"]["messages"]
    tool_call_messages = _assistant_tool_call_messages(messages)
    assert [
        message["tool_calls"][0]["id"] for message in tool_call_messages
    ] == ["call_1", "call_2"]
    for message in tool_call_messages:
        raw_args = message["tool_calls"][0]["function"]["arguments"]
        assert json.loads(raw_args) == {"patch": patch}
        assert "approval_id" not in raw_args
    tool_messages = _tool_messages(messages)
    assert [message["tool_call_id"] for message in tool_messages] == ["call_1"]
    assert tool_messages[0]["content"] == "patch failed"
    _assert_no_dashscope_duplicate_omission(messages)


def test_dashscope_thinking_uses_enable_thinking_and_budget(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-plus",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    cfg = ChatConfig(
        thinking=True,
        thinking_budget_tokens=4096,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="dashscope",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["enable_thinking"] is True
    assert captured["payload"]["thinking_budget"] == 4096
    assert captured["payload"]["max_completion_tokens"] == cfg.max_tokens
    assert "max_tokens" not in captured["payload"]
    assert "reasoning_effort" not in captured["payload"]


def test_dashscope_thinking_omits_forced_tool_choice(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    tool = ToolDefinition(
        name="read_file",
        description="Read a file",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )
    cfg = ChatConfig(
        thinking=True,
        tool_choice={"type": "function", "function": {"name": "read_file"}},
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="dashscope",
        ),
    )

    _collect_events(provider, cfg, tools=[tool])

    assert captured["payload"]["enable_thinking"] is True
    assert "tools" in captured["payload"]
    assert "tool_choice" not in captured["payload"]


def test_dashscope_thinking_omits_implicit_level_budget(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    cfg = ChatConfig(
        thinking=True,
        thinking_budget_tokens=20_000,
        thinking_budget_explicit=False,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="dashscope",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["enable_thinking"] is True
    assert "thinking_budget" not in captured["payload"]
    assert "reasoning_effort" not in captured["payload"]


_DASHSCOPE_BUDGET_ENV = "OPENSQUILLA_DASHSCOPE_THINKING_BUDGET"


def test_dashscope_env_thinking_budget_absent_leaves_payload_inert(
    monkeypatch: Any,
) -> None:
    """With the env override unset, the dashscope payload keeps the default
    behaviour (implicit config emits no thinking_budget)."""
    monkeypatch.delenv(_DASHSCOPE_BUDGET_ENV, raising=False)
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    cfg = ChatConfig(
        thinking=True,
        thinking_budget_tokens=20_000,
        thinking_budget_explicit=False,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="dashscope",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["enable_thinking"] is True
    assert "thinking_budget" not in captured["payload"]


def test_dashscope_env_thinking_budget_sets_payload_when_config_implicit(
    monkeypatch: Any,
) -> None:
    """When set, the env override injects an explicit per-call thinking_budget
    even when AgentConfig would emit none."""
    monkeypatch.setenv(_DASHSCOPE_BUDGET_ENV, "18000")
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    cfg = ChatConfig(
        thinking=True,
        thinking_budget_tokens=20_000,
        thinking_budget_explicit=False,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="dashscope",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["enable_thinking"] is True
    assert captured["payload"]["thinking_budget"] == 18000


def test_dashscope_env_thinking_budget_overrides_explicit_config_and_clamps(
    monkeypatch: Any,
) -> None:
    """The env override takes precedence over an explicit config budget and is
    clamped to the DashScope-supported ceiling."""
    monkeypatch.setenv(_DASHSCOPE_BUDGET_ENV, "999999")
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    cfg = ChatConfig(
        thinking=True,
        thinking_budget_tokens=4096,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="dashscope",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["thinking_budget"] == 38_912


def test_dashscope_env_thinking_budget_invalid_falls_back_to_config(
    monkeypatch: Any,
) -> None:
    """A blank/unparseable/non-positive override is ignored, restoring the
    config-driven behaviour rather than breaking the payload."""
    monkeypatch.setenv(_DASHSCOPE_BUDGET_ENV, "not-a-number")
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-plus",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    cfg = ChatConfig(
        thinking=True,
        thinking_budget_tokens=4096,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="dashscope",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["thinking_budget"] == 4096


def test_zai_ignores_dashscope_env_thinking_budget(monkeypatch: Any) -> None:
    """GLM regression guard: the DashScope-only env override must never leak into
    the zai (GLM) payload branch."""
    monkeypatch.setenv(_DASHSCOPE_BUDGET_ENV, "18000")
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="glm-5.1",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="zai",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["thinking"] == {"type": "enabled"}
    assert "thinking_budget" not in captured["payload"]
    assert "enable_thinking" not in captured["payload"]


def test_dashscope_request_logs_qwen_provider_profile(monkeypatch: Any) -> None:
    captured_payload: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured_payload)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )

    # Interactive-chat tests leave a WARNING-filtering wrapper_class configured
    # process-wide; capture_logs only swaps processors, so reset the wrapper to
    # keep this info-level event visible (same guard as _capture_metric_logs).
    old_config = structlog.get_config()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.NOTSET))
    try:
        with structlog.testing.capture_logs() as captured_logs:
            _collect(
                provider,
                ChatConfig(
                    thinking=True,
                    thinking_budget_tokens=2048,
                    cache_mode="on",
                    model_capabilities=ModelCapabilities(
                        supports_reasoning=True,
                        supports_tools=True,
                        reasoning_format="dashscope",
                    ),
                ),
            )
    finally:
        structlog.configure(**old_config)

    profile = next(
        item for item in captured_logs if item["event"] == "provider.qwen_provider_profile"
    )
    assert profile["provider"] == "dashscope"
    assert profile["model"] == "qwen3.6-flash"
    assert profile["endpoint_family"] == "standard_cn"
    assert profile["thinking_enabled"] is True
    assert profile["thinking_budget"] == 2048
    assert profile["cache_mode"] == "on"
    assert profile["text_tool_parser"] == "qwen_tags"
    assert profile["stream_fallback"] == "non_stream_once"


def test_dashscope_qwen36_flash_thinking_does_not_replay_reasoning_content(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="call_patch",
                    name="apply_patch",
                    input={"patch": "*** Begin Patch\n*** End Patch"},
                )
            ],
            reasoning_content="I chose a minimal patch before calling the tool.",
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="call_patch",
                    content="Applied patch",
                )
            ],
        ),
        Message(role="user", content="continue"),
    ]
    cfg = ChatConfig(
        thinking=True,
        thinking_budget_tokens=4096,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="dashscope",
        ),
    )

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert captured["payload"]["enable_thinking"] is True
    assert "preserve_thinking" not in captured["payload"]
    assert "reasoning_content" not in captured["payload"]["messages"][0]


def test_dashscope_preserve_thinking_model_replays_reasoning_content(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-max-preview",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    messages = [
        Message(
            role="assistant",
            content="previous visible answer",
            reasoning_content="previous DashScope thinking",
        ),
        Message(role="user", content="continue"),
    ]
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="dashscope",
        ),
    )

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert captured["payload"]["enable_thinking"] is True
    assert captured["payload"]["preserve_thinking"] is True
    assert captured["payload"]["messages"][0]["reasoning_content"] == (
        "previous DashScope thinking"
    )


def test_dashscope_non_thinking_does_not_replay_reasoning_content(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    messages = [
        Message(
            role="assistant",
            content="previous answer",
            reasoning_content="prior DashScope thinking should stay hidden when off",
        ),
        Message(role="user", content="continue"),
    ]
    cfg = ChatConfig(
        thinking=False,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="dashscope",
        ),
    )

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert captured["payload"]["enable_thinking"] is False
    assert "reasoning_content" not in captured["payload"]["messages"][0]


def test_dashscope_non_thinking_sends_enable_thinking_false(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    cfg = ChatConfig(
        thinking=False,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="dashscope",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["enable_thinking"] is False
    assert "thinking_budget" not in captured["payload"]


def test_moonshot_kimi_thinking_uses_provider_thinking_object(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="kimi-k2.5",
        base_url="https://api.moonshot.cn/v1",
        provider_kind="moonshot",
    )
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="moonshot",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["thinking"] == {"type": "enabled"}


def test_moonshot_kimi_non_thinking_sends_provider_disabled(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="kimi-k2.5",
        base_url="https://api.moonshot.cn/v1",
        provider_kind="moonshot",
    )
    cfg = ChatConfig(
        thinking=False,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="moonshot",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["thinking"] == {"type": "disabled"}


def test_volcengine_thinking_uses_provider_thinking_object(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="doubao-seed-1-6-thinking-250715",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        provider_kind="volcengine",
    )
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="volcengine",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["thinking"] == {"type": "enabled"}


def test_volcengine_non_thinking_sends_provider_disabled(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="doubao-seed-1-6-thinking-250715",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        provider_kind="volcengine",
    )
    cfg = ChatConfig(
        thinking=False,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="volcengine",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["thinking"] == {"type": "disabled"}


def test_byteplus_seed_thinking_uses_provider_thinking_object(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="seed-2-0-lite-260228",
        base_url="https://ark.ap-southeast.bytepluses.com/api/v3",
        provider_kind="byteplus",
    )
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="volcengine",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["thinking"] == {"type": "enabled"}
    assert captured["payload"]["stream_options"] == {"include_usage": True}


@pytest.mark.parametrize(
    ("provider_kind", "model", "base_url"),
    [
        ("volcengine", "doubao-seed-2-0-lite-260215", "https://ark.cn-beijing.volces.com/api/v3"),
        ("byteplus", "seed-2-0-lite-260228", "https://ark.ap-southeast.bytepluses.com/api/v3"),
    ],
)
def test_volcengine_and_byteplus_strip_unsupported_tool_schema_keywords(
    monkeypatch: Any,
    provider_kind: str,
    model: str,
    base_url: str,
) -> None:
    unsupported = {
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minContains",
        "maxContains",
    }

    def assert_no_unsupported_keys(value: Any) -> None:
        if isinstance(value, dict):
            assert not (set(value) & unsupported)
            for item in value.values():
                assert_no_unsupported_keys(item)
        elif isinstance(value, list):
            for item in value:
                assert_no_unsupported_keys(item)

    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model=model,
        base_url=base_url,
        provider_kind=provider_kind,
    )
    tool = ToolDefinition(
        name="bounded",
        description="Exercise provider schema filtering.",
        input_schema=ToolInputSchema(
            properties={
                "name": {"type": "string", "minLength": 1, "maxLength": 10},
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 3,
                    "items": {"type": "string", "minLength": 2},
                },
                "nested": {
                    "type": "object",
                    "properties": {"value": {"type": "string", "maxLength": 4}},
                },
            },
            required=["name"],
        ),
    )

    _collect_events(provider, ChatConfig(), tools=[tool])

    schema = captured["payload"]["tools"][0]["function"]["parameters"]
    assert_no_unsupported_keys(schema)
    assert schema["properties"]["name"]["type"] == "string"
    assert schema["properties"]["items"]["items"]["type"] == "string"
    assert schema["properties"]["nested"]["properties"]["value"]["type"] == "string"


def test_moonshot_kimi_k2_6_omits_temperature_for_fixed_sampling(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="kimi-k2.6",
        base_url="https://api.moonshot.cn/v1",
        provider_kind="moonshot",
    )

    _collect(provider, ChatConfig(temperature=0))

    assert "temperature" not in captured["payload"]


def test_moonshot_v1_still_sends_temperature(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="moonshot-v1-8k",
        base_url="https://api.moonshot.cn/v1",
        provider_kind="moonshot",
    )

    _collect(provider, ChatConfig(temperature=0))

    assert captured["payload"]["temperature"] == 0


def test_direct_openai_gpt_5_5_reasoning_uses_max_completion_tokens_without_temperature(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="gpt-5.5",
        base_url="https://api.openai.com/v1",
        provider_kind="openai",
    )
    cfg = ChatConfig(
        thinking=True,
        temperature=0,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="openai",
        ),
    )

    _collect(provider, cfg)

    assert captured["payload"]["max_completion_tokens"] == cfg.max_tokens
    assert "max_tokens" not in captured["payload"]
    assert captured["payload"]["reasoning_effort"] == "medium"
    assert "temperature" not in captured["payload"]


def test_openrouter_still_sends_temperature(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="openai/gpt-4o-mini",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )

    _collect(provider, ChatConfig(temperature=0))

    assert captured["payload"]["temperature"] == 0


def test_openai_payload_omits_top_p_by_default(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(api_key="test", model="gpt-4o-mini")

    _collect(provider, ChatConfig())

    assert "top_p" not in captured["payload"]


def test_openai_payload_sends_top_p_when_configured(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )

    _collect(provider, ChatConfig(top_p=0.95))

    assert captured["payload"]["top_p"] == 0.95


def test_siliconflow_baseline_payload_does_not_enable_provider_thinking_by_default(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-ai/DeepSeek-V3",
        base_url="https://api.siliconflow.cn/v1",
        provider_kind="siliconflow",
    )

    _collect(provider, ChatConfig())

    assert "enable_thinking" not in captured["payload"]
    assert "thinking_budget" not in captured["payload"]
    assert "thinking" not in captured["payload"]


def test_ovms_v3_base_url_posts_to_v3_chat_completions(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="unused",
        model="llama3",
        base_url="http://localhost:8000/v3",
        provider_kind="ovms",
    )

    _collect(provider, ChatConfig())

    assert captured["url"] == "http://localhost:8000/v3/chat/completions"


def test_qianfan_v2_base_url_posts_to_v2_chat_completions(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="unused",
        model="ernie-4.0-turbo-8k",
        base_url="https://qianfan.baidubce.com/v2",
        provider_kind="qianfan",
    )

    _collect(provider, ChatConfig())

    assert captured["url"] == "https://qianfan.baidubce.com/v2/chat/completions"


def test_zai_v4_base_url_posts_to_v4_chat_completions(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="unused",
        model="glm-4.5",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        provider_kind="zhipu",
    )

    _collect(provider, ChatConfig())

    assert captured["url"] == "https://open.bigmodel.cn/api/paas/v4/chat/completions"


def test_gemini_stream_tool_call_without_index_is_tolerated(monkeypatch: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {
                "model": "gemini-2.5-flash",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "id": "call_lookup",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"q":"hi"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": "gemini-2.5-flash",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            },
        ]
        body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body + b"data: [DONE]\n\n",
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="test",
        model="gemini-2.5-flash",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        provider_kind="gemini",
    )
    tool = ToolDefinition(
        name="lookup",
        description="Lookup a value.",
        input_schema=ToolInputSchema(properties={"q": {"type": "string"}}, required=["q"]),
    )

    events = _collect_events(provider, ChatConfig(), tools=[tool])

    tool_end = next(event for event in events if isinstance(event, ToolUseEndEvent))
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert tool_end.tool_use_id == "call_lookup"
    assert tool_end.tool_name == "lookup"
    assert tool_end.arguments == {"q": "hi"}
    assert done.model == "gemini-2.5-flash"


def test_stream_malformed_tool_arguments_fail_closed_without_raw_end(
    monkeypatch: Any,
) -> None:
    raw_arguments = '{"path":"demo.py","new_text":"unterminated'

    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {
                "model": "qwen3.6-flash",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_edit",
                                    "type": "function",
                                    "function": {
                                        "name": "edit_file",
                                        "arguments": raw_arguments,
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": "qwen3.6-flash",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            },
        ]
        body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body + b"data: [DONE]\n\n",
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    tool = ToolDefinition(
        name="edit_file",
        description="Edit a file.",
        input_schema=ToolInputSchema(
            properties={
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            required=["path", "old_text", "new_text"],
        ),
    )

    with structlog.testing.capture_logs() as captured:
        events = _collect_events(provider, ChatConfig(), tools=[tool])

    invalid_log = next(
        item for item in captured if item["event"] == "provider.tool_arguments_json_invalid"
    )
    _assert_invalid_native_arguments_fail_closed(
        events,
        raw_arguments=raw_arguments,
    )
    assert invalid_log["provider"] == "dashscope"
    assert invalid_log["model"] == "qwen3.6-flash"
    assert invalid_log["tool"] == "edit_file"
    assert invalid_log["tool_use_id"] == "call_edit"
    assert invalid_log["raw_chars"] == len(raw_arguments)
    assert "Unterminated string" in invalid_log["error"]


def test_stream_dashscope_repairs_parameter_wrapped_tool_arguments(
    monkeypatch: Any,
) -> None:
    raw_arguments = '<parameter>{"path":"demo.py","old_text":"old","new_text":"new"}</parameter>'

    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {
                "model": "qwen3.6-flash",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_edit",
                                    "type": "function",
                                    "function": {
                                        "name": "edit_file",
                                        "arguments": raw_arguments,
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": "qwen3.6-flash",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            },
        ]
        body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body + b"data: [DONE]\n\n",
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    tool = ToolDefinition(
        name="edit_file",
        description="Edit a file.",
        input_schema=ToolInputSchema(
            properties={
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            required=["path", "old_text", "new_text"],
        ),
    )

    with structlog.testing.capture_logs() as captured:
        events = _collect_events(provider, ChatConfig(), tools=[tool])

    tool_end = next(event for event in events if isinstance(event, ToolUseEndEvent))
    assert tool_end.arguments == {
        "path": "demo.py",
        "old_text": "old",
        "new_text": "new",
    }
    assert any(
        item["event"] == "provider.tool_arguments_json_repaired"
        and item["repair"] == "dashscope_wrapper_json"
        for item in captured
    )
    assert not any(item["event"] == "provider.tool_arguments_json_invalid" for item in captured)


def test_stream_dashscope_recovers_qwen_json_text_tool_call(
    monkeypatch: Any,
) -> None:
    text_tool_call = (
        'thinking out loud\n<tool_call>{"name":"edit_file","arguments":'
        '{"path":"demo.py","old_text":"old","new_text":"new"}}</tool_call>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {
                "model": "qwen3.6-flash",
                "choices": [{"delta": {"content": text_tool_call}, "finish_reason": None}],
            },
            {
                "model": "qwen3.6-flash",
                "choices": [{"delta": {}, "finish_reason": "stop"}],
            },
        ]
        body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body + b"data: [DONE]\n\n",
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    tool = ToolDefinition(
        name="edit_file",
        description="Edit a file.",
        input_schema=ToolInputSchema(
            properties={
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            required=["path", "old_text", "new_text"],
        ),
    )

    with structlog.testing.capture_logs() as captured:
        events = _collect_events(provider, ChatConfig(), tools=[tool])

    tool_end = next(event for event in events if isinstance(event, ToolUseEndEvent))
    assert tool_end.synthetic_from_text is True
    assert tool_end.tool_name == "edit_file"
    assert tool_end.arguments == {
        "path": "demo.py",
        "old_text": "old",
        "new_text": "new",
    }
    assert any(item["event"] == "provider.qwen_text_tool_call_parsed" for item in captured)


def test_stream_dashscope_recovers_qwen_xml_text_tool_call_with_aliases(
    monkeypatch: Any,
) -> None:
    text_tool_call = (
        "<tool_call><function=edit_file>"
        "<parameter=filePath>demo.py</parameter>"
        "<parameter=oldString>old</parameter>"
        "<parameter=newString>new</parameter>"
        "</function></tool_call>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {
                "model": "qwen3.6-flash",
                "choices": [{"delta": {"content": text_tool_call}, "finish_reason": None}],
            },
            {
                "model": "qwen3.6-flash",
                "choices": [{"delta": {}, "finish_reason": "stop"}],
            },
        ]
        body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body + b"data: [DONE]\n\n",
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    tool = ToolDefinition(
        name="edit_file",
        description="Edit a file.",
        input_schema=ToolInputSchema(
            properties={
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            required=["path", "old_text", "new_text"],
        ),
    )

    with structlog.testing.capture_logs() as captured:
        events = _collect_events(provider, ChatConfig(), tools=[tool])

    tool_end = next(event for event in events if isinstance(event, ToolUseEndEvent))
    assert tool_end.arguments == {
        "path": "demo.py",
        "old_text": "old",
        "new_text": "new",
    }
    assert any(
        item["event"] == "provider.tool_arguments_aliases_applied"
        and item["provider"] == "dashscope"
        for item in captured
    )


def test_stream_dashscope_rejects_qwen_text_tool_call_with_schema_errors(
    monkeypatch: Any,
) -> None:
    text_tool_call = (
        '<tool_call>{"name":"edit_file","arguments":'
        '{"path":"demo.py","old_text":"old","new_text":7}}</tool_call>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {
                "model": "qwen3.6-flash",
                "choices": [{"delta": {"content": text_tool_call}, "finish_reason": None}],
            },
            {
                "model": "qwen3.6-flash",
                "choices": [{"delta": {}, "finish_reason": "stop"}],
            },
        ]
        body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body + b"data: [DONE]\n\n",
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    tool = ToolDefinition(
        name="edit_file",
        description="Edit a file.",
        input_schema=ToolInputSchema(
            properties={
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            required=["path", "old_text", "new_text"],
        ),
    )

    with structlog.testing.capture_logs() as captured:
        events = _collect_events(provider, ChatConfig(), tools=[tool])

    assert not any(isinstance(event, ToolUseEndEvent) for event in events)
    assert any(
        item["event"] == "provider.qwen_text_tool_call_rejected_schema"
        for item in captured
    )


def test_stream_dashscope_ignores_empty_tool_call_chunks(
    monkeypatch: Any,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {
                "model": "qwen3.6-flash",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": None,
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": "qwen3.6-flash",
                "choices": [{"delta": {}, "finish_reason": "stop"}],
            },
        ]
        body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body + b"data: [DONE]\n\n",
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    tool = ToolDefinition(
        name="edit_file",
        description="Edit a file.",
        input_schema=ToolInputSchema(
            properties={
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            required=["path", "old_text", "new_text"],
        ),
    )

    with structlog.testing.capture_logs() as captured:
        events = _collect_events(provider, ChatConfig(), tools=[tool])

    assert not any(isinstance(event, ToolUseEndEvent) for event in events)
    assert any(
        item["event"] == "dashscope.stream_tool_chunk_sanitized" for item in captured
    )


def test_stream_dashscope_canonicalizes_repaired_edit_file_aliases(
    monkeypatch: Any,
) -> None:
    raw_arguments = (
        '<parameter>{"filePath":"demo.py","oldString":"old","newString":"new"}</parameter>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {
                "model": "qwen3.6-flash",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_edit",
                                    "type": "function",
                                    "function": {
                                        "name": "edit_file",
                                        "arguments": raw_arguments,
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": "qwen3.6-flash",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            },
        ]
        body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body + b"data: [DONE]\n\n",
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    tool = ToolDefinition(
        name="edit_file",
        description="Edit a file.",
        input_schema=ToolInputSchema(
            properties={
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            required=["path", "old_text", "new_text"],
        ),
    )

    with structlog.testing.capture_logs() as captured:
        events = _collect_events(provider, ChatConfig(), tools=[tool])

    tool_end = next(event for event in events if isinstance(event, ToolUseEndEvent))
    assert tool_end.arguments == {
        "path": "demo.py",
        "old_text": "old",
        "new_text": "new",
    }
    assert any(
        item["event"] == "provider.tool_arguments_json_repaired"
        and item["repair"] == "dashscope_wrapper_json"
        for item in captured
    )
    assert any(
        item["event"] == "provider.tool_arguments_aliases_applied" and item["tool"] == "edit_file"
        for item in captured
    )
    assert not any(item["event"] == "provider.tool_arguments_json_invalid" for item in captured)


def test_stream_dashscope_reports_repaired_edit_file_alias_conflicts(
    monkeypatch: Any,
) -> None:
    raw_arguments = (
        '<parameter>{"path":"src/a.py","filePath":"src/b.py",'
        '"old_text":"old","new_text":"new"}</parameter>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {
                "model": "qwen3.6-flash",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_edit",
                                    "type": "function",
                                    "function": {
                                        "name": "edit_file",
                                        "arguments": raw_arguments,
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": "qwen3.6-flash",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            },
        ]
        body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body + b"data: [DONE]\n\n",
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    tool = ToolDefinition(
        name="edit_file",
        description="Edit a file.",
        input_schema=ToolInputSchema(
            properties={
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            required=["path", "old_text", "new_text"],
        ),
    )

    with structlog.testing.capture_logs() as captured:
        events = _collect_events(provider, ChatConfig(), tools=[tool])

    _assert_invalid_native_arguments_fail_closed(
        events,
        raw_arguments=raw_arguments,
    )
    assert any(
        item["event"] == "provider.tool_arguments_alias_conflict" and item["tool"] == "edit_file"
        for item in captured
    )
    assert any(
        item["event"] == "provider.tool_arguments_json_invalid"
        and item["reason"] == "schema_validation_failed"
        for item in captured
    )


def test_stream_dashscope_rejects_repaired_tool_arguments_with_wrong_type(
    monkeypatch: Any,
) -> None:
    raw_arguments = '<parameter>{"path":123,"old_text":"old","new_text":"new"}</parameter>'

    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {
                "model": "qwen3.6-flash",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_edit",
                                    "type": "function",
                                    "function": {
                                        "name": "edit_file",
                                        "arguments": raw_arguments,
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": "qwen3.6-flash",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            },
        ]
        body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body + b"data: [DONE]\n\n",
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    tool = ToolDefinition(
        name="edit_file",
        description="Edit a file.",
        input_schema=ToolInputSchema(
            properties={
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            required=["path", "old_text", "new_text"],
        ),
    )

    with structlog.testing.capture_logs() as captured:
        events = _collect_events(provider, ChatConfig(), tools=[tool])

    _assert_invalid_native_arguments_fail_closed(
        events,
        raw_arguments=raw_arguments,
    )
    assert any(
        item["event"] == "provider.tool_arguments_json_invalid"
        and item["reason"] == "schema_validation_failed"
        for item in captured
    )
    assert not any(item["event"] == "provider.tool_arguments_json_repaired" for item in captured)


def test_stream_dashscope_repairs_embedded_tool_arguments_after_corrupt_prefix(
    monkeypatch: Any,
) -> None:
    raw_arguments = (
        '{"path":"demo.py","new_text":"unterminated prefix '
        '{"path":"demo.py","old_text":"old","new_text":"new"}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {
                "model": "qwen3.6-flash",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_edit",
                                    "type": "function",
                                    "function": {
                                        "name": "edit_file",
                                        "arguments": raw_arguments,
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": "qwen3.6-flash",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            },
        ]
        body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body + b"data: [DONE]\n\n",
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    tool = ToolDefinition(
        name="edit_file",
        description="Edit a file.",
        input_schema=ToolInputSchema(
            properties={
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            required=["path", "old_text", "new_text"],
        ),
    )

    with structlog.testing.capture_logs() as captured:
        events = _collect_events(provider, ChatConfig(), tools=[tool])

    tool_end = next(event for event in events if isinstance(event, ToolUseEndEvent))
    assert tool_end.arguments == {
        "path": "demo.py",
        "old_text": "old",
        "new_text": "new",
    }
    assert any(
        item["event"] == "provider.tool_arguments_json_repaired"
        and item["repair"] == "dashscope_embedded_json_object"
        for item in captured
    )
    assert not any(item["event"] == "provider.tool_arguments_json_invalid" for item in captured)


def test_stream_dashscope_repairs_common_malformed_tool_arguments(
    monkeypatch: Any,
) -> None:
    raw_arguments = '{"path":"demo.py","old_text":"old","new_text":"new",'

    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {
                "model": "qwen3.6-flash",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_edit",
                                    "type": "function",
                                    "function": {
                                        "name": "edit_file",
                                        "arguments": raw_arguments,
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": "qwen3.6-flash",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            },
        ]
        body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body + b"data: [DONE]\n\n",
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    tool = ToolDefinition(
        name="edit_file",
        description="Edit a file.",
        input_schema=ToolInputSchema(
            properties={
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            required=["path", "old_text", "new_text"],
        ),
    )

    with structlog.testing.capture_logs() as captured:
        events = _collect_events(provider, ChatConfig(), tools=[tool])

    tool_end = next(event for event in events if isinstance(event, ToolUseEndEvent))
    assert tool_end.arguments == {
        "path": "demo.py",
        "old_text": "old",
        "new_text": "new",
    }
    assert any(
        item["event"] == "provider.tool_arguments_json_repaired"
        and item["repair"] == "dashscope_malformed_json"
        for item in captured
    )
    assert not any(item["event"] == "provider.tool_arguments_json_invalid" for item in captured)


def test_stream_dashscope_repairs_literal_control_chars_in_tool_arguments(
    monkeypatch: Any,
) -> None:
    raw_arguments = '{"path":"demo.py","old_text":"old\\nline","new_text":"new\nline"}'

    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {
                "model": "qwen3.6-flash",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_edit",
                                    "type": "function",
                                    "function": {
                                        "name": "edit_file",
                                        "arguments": raw_arguments,
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": "qwen3.6-flash",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            },
        ]
        body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body + b"data: [DONE]\n\n",
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    tool = ToolDefinition(
        name="edit_file",
        description="Edit a file.",
        input_schema=ToolInputSchema(
            properties={
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            required=["path", "old_text", "new_text"],
        ),
    )

    with structlog.testing.capture_logs() as captured:
        events = _collect_events(provider, ChatConfig(), tools=[tool])

    tool_end = next(event for event in events if isinstance(event, ToolUseEndEvent))
    assert tool_end.arguments == {
        "path": "demo.py",
        "old_text": "old\nline",
        "new_text": "new\nline",
    }
    assert any(
        item["event"] == "provider.tool_arguments_json_repaired"
        and item["repair"] == "dashscope_malformed_json"
        for item in captured
    )
    assert not any(item["event"] == "provider.tool_arguments_json_invalid" for item in captured)


def test_stream_dashscope_keeps_unrepairable_tool_arguments_invalid(
    monkeypatch: Any,
) -> None:
    raw_arguments = '{"path":"demo.py","old_text":"unterminated'

    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {
                "model": "qwen3.6-flash",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_edit",
                                    "type": "function",
                                    "function": {
                                        "name": "edit_file",
                                        "arguments": raw_arguments,
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": "qwen3.6-flash",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            },
        ]
        body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body + b"data: [DONE]\n\n",
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    tool = ToolDefinition(
        name="edit_file",
        description="Edit a file.",
        input_schema=ToolInputSchema(
            properties={
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            required=["path", "old_text", "new_text"],
        ),
    )

    with structlog.testing.capture_logs() as captured:
        events = _collect_events(provider, ChatConfig(), tools=[tool])

    _assert_invalid_native_arguments_fail_closed(
        events,
        raw_arguments=raw_arguments,
    )
    assert any(item["event"] == "provider.tool_arguments_json_invalid" for item in captured)
    assert not any(item["event"] == "provider.tool_arguments_json_repaired" for item in captured)


def test_stream_dashscope_unwraps_nested_raw_tool_arguments(
    monkeypatch: Any,
) -> None:
    raw_arguments = json.dumps(
        {"_raw": json.dumps({"path": "demo.py", "old_text": "old", "new_text": "new"})}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {
                "model": "qwen3.6-flash",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_edit",
                                    "type": "function",
                                    "function": {
                                        "name": "edit_file",
                                        "arguments": raw_arguments,
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": "qwen3.6-flash",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            },
        ]
        body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body + b"data: [DONE]\n\n",
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    tool = ToolDefinition(
        name="edit_file",
        description="Edit a file.",
        input_schema=ToolInputSchema(
            properties={
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            required=["path", "old_text", "new_text"],
        ),
    )

    with structlog.testing.capture_logs() as captured:
        events = _collect_events(provider, ChatConfig(), tools=[tool])

    tool_end = next(event for event in events if isinstance(event, ToolUseEndEvent))
    assert tool_end.arguments == {
        "path": "demo.py",
        "old_text": "old",
        "new_text": "new",
    }
    assert any(
        item["event"] == "provider.tool_arguments_json_repaired"
        and item["repair"] == "dashscope_nested_raw_json"
        for item in captured
    )


def test_stream_openrouter_does_not_repair_dashscope_wrappers(
    monkeypatch: Any,
) -> None:
    raw_arguments = '<parameter>{"path":"demo.py","old_text":"old","new_text":"new"}</parameter>'

    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {
                "model": "z-ai/glm-5.1",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_edit",
                                    "type": "function",
                                    "function": {
                                        "name": "edit_file",
                                        "arguments": raw_arguments,
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": "z-ai/glm-5.1",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            },
        ]
        body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body + b"data: [DONE]\n\n",
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="test",
        model="z-ai/glm-5.1",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )
    tool = ToolDefinition(
        name="edit_file",
        description="Edit a file.",
        input_schema=ToolInputSchema(
            properties={
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            required=["path", "old_text", "new_text"],
        ),
    )

    with structlog.testing.capture_logs() as captured:
        events = _collect_events(provider, ChatConfig(), tools=[tool])

    _assert_invalid_native_arguments_fail_closed(
        events,
        raw_arguments=raw_arguments,
    )
    assert any(item["event"] == "provider.tool_arguments_json_invalid" for item in captured)
    assert not any(item["event"] == "provider.tool_arguments_json_repaired" for item in captured)


def test_openai_compat_sends_required_tool_choice_when_configured(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="gpt-test",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )
    tool = ToolDefinition(
        name="meta_invoke",
        description="Invoke a meta-skill.",
        input_schema=ToolInputSchema(properties={"name": {"type": "string"}}, required=["name"]),
    )

    _collect_events(provider, ChatConfig(tool_choice="required"), tools=[tool])

    assert captured["payload"]["tool_choice"] == "required"


def test_openai_compat_sends_named_function_tool_choice_when_configured(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="gpt-test",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )
    tool = ToolDefinition(
        name="meta_invoke",
        description="Invoke a meta-skill.",
        input_schema=ToolInputSchema(properties={"name": {"type": "string"}}, required=["name"]),
    )
    tool_choice = {"type": "function", "function": {"name": "meta_invoke"}}

    _collect_events(provider, ChatConfig(tool_choice=tool_choice), tools=[tool])

    assert captured["payload"]["tool_choice"] == tool_choice


def test_gemini_stream_multiple_tool_calls_without_indexes_stay_separate(
    monkeypatch: Any,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {
                "model": "gemini-2.5-flash",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "id": "call_lookup",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"q":"hi"}',
                                    },
                                },
                                {
                                    "id": "call_save",
                                    "type": "function",
                                    "function": {
                                        "name": "save",
                                        "arguments": '{"value":1}',
                                    },
                                },
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": "gemini-2.5-flash",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            },
        ]
        body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body + b"data: [DONE]\n\n",
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="test",
        model="gemini-2.5-flash",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        provider_kind="gemini",
    )
    tools = [
        ToolDefinition(
            name="lookup",
            description="Lookup a value.",
            input_schema=ToolInputSchema(properties={"q": {"type": "string"}}, required=["q"]),
        ),
        ToolDefinition(
            name="save",
            description="Save a value.",
            input_schema=ToolInputSchema(
                properties={"value": {"type": "number"}},
                required=["value"],
            ),
        ),
    ]

    events = _collect_events(provider, ChatConfig(), tools=tools)

    tool_ends = [event for event in events if isinstance(event, ToolUseEndEvent)]
    assert [(event.tool_use_id, event.tool_name, event.arguments) for event in tool_ends] == [
        ("call_lookup", "lookup", {"q": "hi"}),
        ("call_save", "save", {"value": 1}),
    ]


# ---------------------------------------------------------------------------
# Tencent TokenHub (hy3 family)
# ---------------------------------------------------------------------------


def _tokenhub_provider(model: str = "hy3") -> OpenAIProvider:
    return OpenAIProvider(
        api_key="test",
        model=model,
        base_url="https://tokenhub.tencentmaas.com/v1",
        provider_kind="tencent_tokenhub",
    )


def _tokenhub_reasoning_caps() -> ModelCapabilities:
    return ModelCapabilities(
        supports_reasoning=True,
        supports_tools=True,
        reasoning_format="tencent_tokenhub",
    )


@pytest.mark.parametrize(
    ("thinking_level", "expected_effort"),
    [
        (ThinkingLevel.MINIMAL, "low"),
        (ThinkingLevel.LOW, "low"),
        (ThinkingLevel.MEDIUM, "high"),
        (ThinkingLevel.HIGH, "high"),
        (ThinkingLevel.XHIGH, "high"),
        (None, "high"),
    ],
)
def test_tencent_tokenhub_thinking_sends_thinking_object_and_documented_effort(
    monkeypatch: Any,
    thinking_level: ThinkingLevel | None,
    expected_effort: str,
) -> None:
    """TokenHub's hy3 accepts exactly reasoning_effort low|high plus the
    thinking enable object; the five-level ladder collapses onto those two."""
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = _tokenhub_provider()
    cfg = ChatConfig(
        thinking=True,
        thinking_level=thinking_level,
        model_capabilities=_tokenhub_reasoning_caps(),
    )

    _collect(provider, cfg)

    assert captured["url"] == "https://tokenhub.tencentmaas.com/v1/chat/completions"
    assert captured["payload"]["thinking"] == {"type": "enabled"}
    assert captured["payload"]["reasoning_effort"] == expected_effort
    assert captured["payload"]["stream_options"] == {"include_usage": True}


def test_tencent_tokenhub_non_thinking_omits_reasoning_payload(monkeypatch: Any) -> None:
    """hy3 documents no thinking-off payload — thinking-off must omit the
    fields entirely so the endpoint applies its own default."""
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = _tokenhub_provider()
    cfg = ChatConfig(thinking=False, model_capabilities=_tokenhub_reasoning_caps())

    _collect(provider, cfg)

    assert "thinking" not in captured["payload"]
    assert "reasoning_effort" not in captured["payload"]


def test_tencent_tokenhub_tool_replay_preserves_reasoning_content(monkeypatch: Any) -> None:
    """TokenHub's interleaved thinking requires resubmitting historical
    reasoning_content on every tool-loop round."""
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = _tokenhub_provider()
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="call_lookup",
                    name="lookup",
                    input={"q": "cache"},
                )
            ],
            reasoning_content="I need the cache state before answering.",
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="call_lookup",
                    content="cache is warm",
                )
            ],
        ),
        Message(role="user", content="continue"),
    ]
    cfg = ChatConfig(thinking=True, model_capabilities=_tokenhub_reasoning_caps())

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert captured["payload"]["messages"][0]["role"] == "assistant"
    assert captured["payload"]["messages"][0]["tool_calls"][0]["id"] == "call_lookup"
    assert (
        captured["payload"]["messages"][0]["reasoning_content"]
        == "I need the cache state before answering."
    )
    assert captured["payload"]["messages"][1] == {
        "role": "tool",
        "tool_call_id": "call_lookup",
        "content": "cache is warm",
    }


@pytest.mark.parametrize("model", ["hy3", "hy3-preview"])
def test_tencent_tokenhub_hy3_replay_adds_empty_reasoning_content_when_missing(
    monkeypatch: Any,
    model: str,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = _tokenhub_provider(model)
    messages = [
        Message(role="assistant", content="Prior non-thinking assistant turn."),
        Message(role="user", content="continue in thinking mode"),
    ]
    cfg = ChatConfig(thinking=True, model_capabilities=_tokenhub_reasoning_caps())

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert captured["payload"]["messages"][0] == {
        "role": "assistant",
        "content": "Prior non-thinking assistant turn.",
        "reasoning_content": "",
    }


def test_tencent_tokenhub_hy3_replays_reasoning_content_without_catalog_capabilities(
    monkeypatch: Any,
) -> None:
    """The hy3 replay requirement is policy-gated on the exact model ids, so
    it holds even when no capability profile resolved for the request."""
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = _tokenhub_provider()
    messages = [
        Message(
            role="assistant",
            content="earlier turn",
            reasoning_content="prior thinking from tokenhub",
        ),
        Message(role="user", content="continue"),
    ]

    async def _run() -> None:
        async for _ in provider.chat(messages, config=ChatConfig()):
            pass

    asyncio.run(_run())

    assert captured["payload"]["messages"][0]["reasoning_content"] == (
        "prior thinking from tokenhub"
    )


def test_tencent_tokenhub_non_hy3_model_does_not_require_reasoning_content(
    monkeypatch: Any,
) -> None:
    """Third-party models hosted on TokenHub are outside the hy3 replay
    requirement: no reasoning_content is invented for them."""
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = _tokenhub_provider("kimi-k2.6")
    messages = [
        Message(role="assistant", content="earlier turn"),
        Message(role="user", content="continue"),
    ]

    async def _run() -> None:
        async for _ in provider.chat(messages, config=ChatConfig()):
            pass

    asyncio.run(_run())

    assert "reasoning_content" not in captured["payload"]["messages"][0]


def test_tencent_token_plan_thinking_payload_and_url_join(monkeypatch: Any) -> None:
    """The Token Plan endpoint shares the TokenHub dialect: same thinking
    payload and hy3 replay policy, joined onto the /plan/v3 base."""
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="hy3",
        base_url="https://api.lkeap.cloud.tencent.com/plan/v3",
        provider_kind="tencent_tokenhub",
    )
    cfg = ChatConfig(
        thinking=True,
        thinking_level=ThinkingLevel.LOW,
        model_capabilities=_tokenhub_reasoning_caps(),
    )

    _collect(provider, cfg)

    assert captured["url"] == "https://api.lkeap.cloud.tencent.com/plan/v3/chat/completions"
    assert captured["payload"]["thinking"] == {"type": "enabled"}
    assert captured["payload"]["reasoning_effort"] == "low"

def test_openrouter_routing_pin_strict_env_sends_only_without_fallbacks(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    chunks = [
        {
            "model": "deepseek/deepseek-v4-flash",
            "choices": [{"delta": {"content": "ok"}, "finish_reason": None}],
        },
        {
            "model": "deepseek/deepseek-v4-flash",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
    ]
    body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
    body += b"data: [DONE]\n\n"
    _patch_transport_body(monkeypatch, captured, body)
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "on")
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek/deepseek-v4-flash",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
        provider_routing={"deepseek/deepseek-v4-flash": "deepseek"},
    )

    done = _collect(provider, ChatConfig())

    assert captured["payload"]["provider"] == {
        "only": ["deepseek"],
        "allow_fallbacks": False,
    }
    assert done.stop_reason == "stop"


def test_openrouter_routing_pin_default_keeps_order_with_fallbacks(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    chunks = [
        {
            "model": "deepseek/deepseek-v4-flash",
            "choices": [{"delta": {"content": "ok"}, "finish_reason": None}],
        },
        {
            "model": "deepseek/deepseek-v4-flash",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
    ]
    body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
    body += b"data: [DONE]\n\n"
    _patch_transport_body(monkeypatch, captured, body)
    monkeypatch.delenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", raising=False)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek/deepseek-v4-flash",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
        provider_routing={"deepseek/deepseek-v4-flash": "deepseek"},
    )

    done = _collect(provider, ChatConfig())

    assert captured["payload"]["provider"] == {
        "order": ["deepseek"],
        "allow_fallbacks": True,
    }
    assert done.stop_reason == "stop"


def _stream_error_frame_handler(request: httpx.Request) -> httpx.Response:
    chunks = [
        {
            "model": "glm-5.1",
            "choices": [{"delta": {"content": "partial"}, "finish_reason": None}],
        },
        {
            "error": {"code": 502, "message": "Provider returned error"},
        },
        {
            "model": "glm-5.1",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        },
    ]
    body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=body + b"data: [DONE]\n\n",
    )


def _patch_stream_transport(monkeypatch: Any, handler: Any) -> None:
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)


def test_stream_error_frame_is_unconditionally_terminal(monkeypatch: Any) -> None:
    monkeypatch.delenv("OPENSQUILLA_PROVIDER_STREAM_ERROR_FRAMES", raising=False)
    _patch_stream_transport(monkeypatch, _stream_error_frame_handler)
    provider = OpenAIProvider(
        api_key="test",
        model="glm-5.1",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )

    events = _collect_events(provider, ChatConfig())

    error = next(event for event in events if isinstance(event, ErrorEvent))
    assert error.code == "502"
    assert not any(isinstance(event, DoneEvent) for event in events)


def test_stream_error_frame_env_surfaces_error_event(monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_STREAM_ERROR_FRAMES", "1")
    _patch_stream_transport(monkeypatch, _stream_error_frame_handler)
    provider = OpenAIProvider(
        api_key="test",
        model="glm-5.1",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )

    events = _collect_events(provider, ChatConfig())

    error = next(event for event in events if isinstance(event, ErrorEvent))
    assert error.code == "502"
    assert "stream error" in error.message
    assert "Provider returned error" in error.message
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert isinstance(events[-1], ErrorEvent)


def test_stream_error_frame_without_code_uses_stream_error(monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_STREAM_ERROR_FRAMES", "on")

    def handler(request: httpx.Request) -> httpx.Response:
        body = f"data: {json.dumps({'error': {'message': 'upstream reset'}})}\n\n".encode()
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body + b"data: [DONE]\n\n",
        )

    _patch_stream_transport(monkeypatch, handler)
    provider = OpenAIProvider(
        api_key="test",
        model="glm-5.1",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )

    events = _collect_events(provider, ChatConfig())

    error = next(event for event in events if isinstance(event, ErrorEvent))
    assert error.code == "stream_error"
    assert "upstream reset" in error.message
    assert not any(isinstance(event, DoneEvent) for event in events)


def _reasoning_echo_provider(monkeypatch: Any, captured: dict[str, Any]) -> OpenAIProvider:
    _patch_transport(monkeypatch, captured)
    return OpenAIProvider(
        api_key="test",
        model="anthropic/claude-sonnet-4.5",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )


_REASONING_ECHO_MESSAGES = [
    Message(role="assistant", content="answer one", reasoning_content="thinking one"),
    Message(role="user", content="next"),
    Message(role="assistant", content="answer two", reasoning_content="thinking two"),
    Message(role="user", content="next again"),
    Message(role="assistant", content="answer three", reasoning_content="thinking three"),
    Message(role="user", content="continue"),
]

_REASONING_ECHO_CFG = ChatConfig(
    thinking=True,
    model_capabilities=ModelCapabilities(
        supports_reasoning=True,
        supports_tools=True,
        reasoning_format="openrouter",
    ),
)


def _run_reasoning_echo_chat(provider: OpenAIProvider) -> None:
    async def _run() -> None:
        async for _ in provider.chat(_REASONING_ECHO_MESSAGES, config=_REASONING_ECHO_CFG):
            pass

    asyncio.run(_run())


def test_reasoning_echo_default_replays_all_assistant_messages(monkeypatch: Any) -> None:
    monkeypatch.delenv("OPENSQUILLA_REASONING_ECHO_TURNS", raising=False)
    captured: dict[str, Any] = {}
    provider = _reasoning_echo_provider(monkeypatch, captured)

    _run_reasoning_echo_chat(provider)

    payload_messages = captured["payload"]["messages"]
    assert payload_messages[0]["reasoning_content"] == "thinking one"
    assert payload_messages[2]["reasoning_content"] == "thinking two"
    assert payload_messages[4]["reasoning_content"] == "thinking three"


def test_reasoning_echo_all_matches_default(monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENSQUILLA_REASONING_ECHO_TURNS", "all")
    captured: dict[str, Any] = {}
    provider = _reasoning_echo_provider(monkeypatch, captured)

    _run_reasoning_echo_chat(provider)

    payload_messages = captured["payload"]["messages"]
    assert payload_messages[0]["reasoning_content"] == "thinking one"
    assert payload_messages[2]["reasoning_content"] == "thinking two"
    assert payload_messages[4]["reasoning_content"] == "thinking three"


def test_reasoning_echo_turns_keeps_only_last_n(monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENSQUILLA_REASONING_ECHO_TURNS", "1")
    captured: dict[str, Any] = {}
    provider = _reasoning_echo_provider(monkeypatch, captured)

    _run_reasoning_echo_chat(provider)

    payload_messages = captured["payload"]["messages"]
    assert "reasoning_content" not in payload_messages[0]
    assert "reasoning_content" not in payload_messages[2]
    assert payload_messages[4]["reasoning_content"] == "thinking three"


def test_reasoning_echo_turns_zero_drops_all(monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENSQUILLA_REASONING_ECHO_TURNS", "0")
    captured: dict[str, Any] = {}
    provider = _reasoning_echo_provider(monkeypatch, captured)

    _run_reasoning_echo_chat(provider)

    payload_messages = captured["payload"]["messages"]
    for payload_message in payload_messages:
        assert "reasoning_content" not in payload_message


def test_reasoning_echo_turns_larger_than_history_keeps_all(monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENSQUILLA_REASONING_ECHO_TURNS", "99")
    captured: dict[str, Any] = {}
    provider = _reasoning_echo_provider(monkeypatch, captured)

    _run_reasoning_echo_chat(provider)

    payload_messages = captured["payload"]["messages"]
    assert payload_messages[0]["reasoning_content"] == "thinking one"
    assert payload_messages[4]["reasoning_content"] == "thinking three"


def test_reasoning_echo_turns_rejects_unrecognized_value(monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENSQUILLA_REASONING_ECHO_TURNS", "some")

    with pytest.raises(ValueError, match="OPENSQUILLA_REASONING_ECHO_TURNS"):
        OpenAIProvider(
            api_key="test",
            model="anthropic/claude-sonnet-4.5",
            base_url="https://openrouter.ai/api/v1",
            provider_kind="openrouter",
        )


def test_reasoning_echo_truncation_keeps_required_empty_key(monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENSQUILLA_REASONING_ECHO_TURNS", "1")
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        provider_kind="deepseek",
    )
    messages = [
        Message(role="assistant", content="answer one", reasoning_content="thinking one"),
        Message(role="user", content="next"),
        Message(role="assistant", content="answer two", reasoning_content="thinking two"),
        Message(role="user", content="continue"),
    ]
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="deepseek",
        ),
    )

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    payload_messages = captured["payload"]["messages"]
    # The required-key model must keep reasoning_content on every assistant
    # message: truncated turns carry "" instead of losing the key.
    assert payload_messages[0]["reasoning_content"] == ""
    assert payload_messages[2]["reasoning_content"] == "thinking two"


def test_reasoning_echo_env_is_inert_for_non_replay_model(monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENSQUILLA_REASONING_ECHO_TURNS", "1")
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="gemini-2.5-pro",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        provider_kind="gemini",
    )
    messages = [
        Message(role="assistant", content="prior", reasoning_content="never replayed"),
        Message(role="user", content="continue"),
    ]
    cfg = ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="gemini",
        ),
    )

    async def _run() -> None:
        async for _ in provider.chat(messages, config=cfg):
            pass

    asyncio.run(_run())

    assert "reasoning_content" not in captured["payload"]["messages"][0]
