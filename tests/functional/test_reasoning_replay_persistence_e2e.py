"""Real provider parser + Agent + finalizer + SQLite, with synthetic HTTP SSE."""

from __future__ import annotations

import gzip
import json
import zlib
from collections.abc import AsyncIterator
from copy import deepcopy

import httpx
import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import public_agent_event_payload
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolContext, ToolSpec
from scripts import live_reasoning_replay_e2e as harness


class _SSE(httpx.AsyncByteStream):
    def __init__(self, frames: list[dict], encoding: str = "identity"):
        self.frames = frames
        self.encoding = encoding

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.encoding != "identity":
            body = (
                "".join(f"data: {json.dumps(frame)}\n\n" for frame in self.frames)
                + "data: [DONE]\n\n"
            ).encode()
            compressed = gzip.compress(body) if self.encoding == "gzip" else zlib.compress(body)
            for index in range(0, len(compressed), 17):
                yield compressed[index : index + 17]
            return
        for frame in self.frames:
            yield f"data: {json.dumps(frame)}\n\n".encode()
        yield b"data: [DONE]\n\n"


def _anthropic_response(call_index: int, model: str) -> tuple[list[dict], list[dict]]:
    """Independent synthetic wire blocks, including separate per-block signatures."""
    blocks = [
        {
            "type": "thinking",
            "thinking": f"synthetic first thought {call_index}",
            "signature": f"synthetic-signature-{call_index}-first",
        },
        {"type": "redacted_thinking", "data": f"synthetic-opaque-{call_index}"},
        {
            "type": "thinking",
            "thinking": f"synthetic second thought {call_index}",
            "signature": f"synthetic-signature-{call_index}-second",
        },
        {
            "type": "text",
            "text": (
                "REPLAY_FIRST_OK" if call_index == 2 else
                "REPLAY_SECOND_OK" if call_index == 4 else "Checking the synthetic value."
            ),
        },
    ]
    if call_index in {0, 1, 3}:
        blocks.append({
            "type": "tool_use", "id": f"synthetic-call-{call_index}",
            "name": "replay_step", "input": {"value": {0: 7, 1: 18, 3: 23}[call_index]},
        })
    frames = [{
        "type": "message_start",
        "message": {
            "id": f"synthetic-message-{call_index}", "type": "message", "role": "assistant",
            "model": model, "content": [], "usage": {"input_tokens": 20, "output_tokens": 0},
        },
    }]
    for index, block in enumerate(blocks):
        if block["type"] == "redacted_thinking":
            start = deepcopy(block)
            deltas = []
        elif block["type"] == "thinking":
            start = {"type": "thinking", "thinking": "", "signature": ""}
            deltas = [
                {"type": "thinking_delta", "thinking": block["thinking"][:10]},
                {"type": "thinking_delta", "thinking": block["thinking"][10:]},
                {"type": "signature_delta", "signature": block["signature"][:12]},
                {"type": "signature_delta", "signature": block["signature"][12:]},
            ]
        elif block["type"] == "text":
            start = {"type": "text", "text": ""}
            deltas = [{"type": "text_delta", "text": block["text"]}]
        else:
            start = {**block, "input": {}}
            deltas = [{"type": "input_json_delta", "partial_json": json.dumps(block["input"])}]
        frames.append({"type": "content_block_start", "index": index, "content_block": start})
        frames.extend(
            {"type": "content_block_delta", "index": index, "delta": delta}
            for delta in deltas
        )
        frames.append({"type": "content_block_stop", "index": index})
    frames.extend([
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use" if call_index in {0, 1, 3} else "end_turn"},
            "usage": {"output_tokens": 10},
        },
        {"type": "message_stop"},
    ])
    return blocks, frames


@pytest.mark.asyncio
async def test_anthropic_native_blocks_survive_tools_and_sqlite_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_LIVE_DISABLE_DOTENV", "1")
    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG", "0")
    endpoint = "https://synthetic-anthropic.invalid"
    model = "claude-sonnet-4-6"
    requests = []
    returned_blocks = []

    async def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == endpoint + "/v1/messages"
        payload = json.loads(request.content)
        call_index = len(requests)
        assert call_index < 5, "unexpected recovery or extra provider call"
        requests.append(payload)
        blocks, frames = _anthropic_response(call_index, model)
        returned_blocks.append(blocks)
        return httpx.Response(
            200, stream=_SSE(frames), headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(respond)
    real_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    config = harness._config(tmp_path, "anthropic", model, endpoint)
    selector_config = SelectorConfig(primary=ProviderConfig(
        provider="anthropic", model=model, base_url=endpoint, api_key="synthetic-test-key",
    ))
    registry = ToolRegistry()
    tool_values = []

    async def step(value: int) -> str:
        tool_values.append(value)
        return json.dumps({"next_value": value + 11})

    registry.register(ToolSpec(
        name="replay_step", description="Return the next synthetic value.",
        parameters={"value": {"type": "integer"}}, required=["value"],
    ), step)
    key = "agent:main:synthetic-native-anthropic-replay"
    db = tmp_path / "sessions.sqlite"
    persisted_before = []
    native_sources = set()
    for turn, prompt in enumerate((harness.FIRST_PROMPT, harness.SECOND_PROMPT)):
        storage = SessionStorage(str(db))
        await storage.connect()
        try:
            manager = SessionManager(storage, inject_time_prefix=False)
            if turn == 0:
                await manager.create(session_key=key, agent_id="main")
            else:
                restored = await manager.get_canonical_transcript(key)
                assert [row.assistant_replay for row in restored if row.role == "assistant"] == (
                    persisted_before
                )
                assert len(requests) == 3
            runner = TurnRunner(
                provider_selector=ModelSelector(selector_config), tool_registry=registry,
                session_manager=manager, config=config, model_catalog=harness._Catalog(),
            )
            user = await manager.append_message(key, "user", prompt)
            events = [event async for event in runner.run(
                prompt, session_key=key, bound_user_message_id=user.message_id,
                tool_context=ToolContext(is_owner=True, workspace_dir=config.workspace_dir),
            )]
            assert not [event for event in events if event.kind == "error"]
            assert any(event.kind == "done" for event in events)
            # Compare actual next HTTP requests, not a serializer invoked in
            # isolation. Call three has fresh storage/selector/runner state.
            for call_index, payload in enumerate(requests):
                assistant_content = [
                    message["content"] for message in payload["messages"]
                    if message["role"] == "assistant"
                ]
                assert assistant_content == returned_blocks[:call_index]
            # Reasoning text remains intentionally available for presentation;
            # native signatures, opaque data and private replay must not escape.
            public = json.dumps([public_agent_event_payload(event) for event in events])
            assert "assistant_replay" not in public
            assert "synthetic-signature-" not in public
            assert "synthetic-opaque-" not in public
            rows = await manager.get_canonical_transcript(key)
            saved = [row.assistant_replay for row in rows if row.role == "assistant"]
            assert len(saved) == turn + 1
            assistants = [
                message for envelope in saved for message in envelope["messages"]
                if message["role"] == "assistant"
            ]
            assert len(assistants) == len(returned_blocks)
            for message, native in zip(assistants, returned_blocks, strict=True):
                assert message["content"] == native
                state = message["provider_replay"]
                assert state["protocol"] == "anthropic_messages"
                assert state["model"] == model
                assert state["source"]
                assert "synthetic-test-key" not in state["source"]
                native_sources.add(state["source"])
                assert state["native_content"] == native
            persisted_before = deepcopy(saved)
        finally:
            await storage.close()
    assert len(requests) == 5
    assert tool_values == [7, 18, 23]
    assert len(native_sources) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,final_reasoning,encoding",
    [
        ("deepseek", "normal", "identity"),
        ("deepseek", "empty", "identity"),
        ("deepseek", "absent", "identity"),
        ("openrouter", "normal", "identity"),
        ("deepseek", "normal", "gzip"),
        ("openrouter", "normal", "deflate"),
        ("tokenrhythm", "normal", "identity"),
        ("tokenrhythm", "empty", "identity"),
        ("tokenrhythm", "absent", "identity"),
    ],
)
async def test_native_replay_survives_tools_finalizer_and_storage_reopen(
    tmp_path, monkeypatch, provider, final_reasoning, encoding
):
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    monkeypatch.setenv("OPENSQUILLA_LIVE_DISABLE_DOTENV", "1")
    endpoint = harness.registry_endpoint(provider)
    model = "glm-5.2" if provider == "tokenrhythm" else harness.DEFAULT_MODELS[provider]
    requests = []

    async def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        index = len(requests)
        requests.append(payload)
        assert index < 5, "unexpected recovery or extra provider call"
        frames = []
        if provider in {"deepseek", "tokenrhythm"}:
            fragments = (f"synthetic reasoning {index}: ", "retain precisely")
            if index == 2 and final_reasoning == "empty":
                fragments = ("",)
            elif index == 2 and final_reasoning == "absent":
                fragments = ()
            for text in fragments:
                frames.append({"choices": [{"index": 0, "delta": {"reasoning_content": text}}]})
        else:
            for block in (
                {"index": 0, "type": "reasoning.text", "text": f"synthetic thought {index}"},
                {"index": 0, "type": "reasoning.text", "text": " continued"},
                {
                    "index": 1,
                    "type": "reasoning.encrypted",
                    "data": f"opaque-{index}",
                    "signature": f"signature-{index}",
                    "format": "synthetic-v1",
                },
            ):
                frames.append(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "reasoning_details": [block],
                                    "reasoning": block.get("text", ""),
                                },
                            }
                        ]
                    }
                )
        if index in {0, 1, 3}:
            value = {0: 7, 1: 18, 3: 23}[index]
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"test_call_{index}",
                        "type": "function",
                        "function": {
                            "name": "replay_step",
                            "arguments": json.dumps({"value": value}),
                        },
                    }
                ]
            }
            finish = "tool_calls"
        else:
            delta = {"content": "REPLAY_FIRST_OK" if index == 2 else "REPLAY_SECOND_OK"}
            finish = "stop"
        frames.append({"choices": [{"index": 0, "delta": delta}]})
        frames.append(
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "cost": 0.00001},
            }
        )
        return httpx.Response(
            200,
            stream=_SSE(frames, encoding),
            headers={
                "content-type": "text/event-stream",
                "content-encoding": encoding,
            },
        )

    observer = harness.WireObserver(endpoint, httpx.MockTransport(respond))
    report = await harness.run_case(
        tmp_path,
        provider=provider,
        model=model,
        api_key="synthetic-test-key",
        observer=observer,
        require_native_replay=True,
    )
    assert report["ok"] is True
    assert report["model_calls"] == 5
    assert report["tool_calls"] == 3
    assert report["post_restart_comparisons"] > 0
    assert report["storage_reopened"] is True
    assert report["completed_old_round_reasoning_replayed"] is (final_reasoning != "absent")
    assert all(call.content_encoding == encoding for call in observer.calls)
    assert all(call.body_format == "sse" for call in observer.calls)
    assert report["input_tokens"] == 100
    assert report["output_tokens"] == 50
    # The public report must never contain model output, opaque replay values,
    # signatures, tool arguments, or credentials retained by the observer.
    public = json.dumps(report)
    assert not any(
        text in public
        for text in (
            "synthetic thought",
            "synthetic reasoning",
            "signature-",
            "opaque-",
            "synthetic-test-key",
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_mutation", [None, "missing_memory", "missing_marker"])
@pytest.mark.parametrize(
    "provider,model,thinking,native,expected_coverage",
    [
        ("deepseek", "deepseek-v4-flash", "low", True, "complete"),
        ("tokenrhythm", "deepseek-v4-pro-0813", "low", True, "partial"),
        ("tokenrhythm", "glm-5.2", "default", True, "complete"),
        ("tokenrhythm", "glm-5.2", "off", False, "not_returned"),
    ],
)
async def test_plain_chat_reopens_storage_and_reports_actual_native_coverage(
    tmp_path, monkeypatch, provider, model, thinking, native, expected_coverage, reply_mutation
):
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    monkeypatch.setenv("OPENSQUILLA_LIVE_DISABLE_DOTENV", "1")
    requests = []

    async def respond(request):
        payload = json.loads(request.content)
        assert not payload.get("tools")
        index = len(requests)
        requests.append(payload)
        assert index < 2
        content = "CHAT_FIRST_OK" if index == 0 else "amber-17 CHAT_SECOND_OK"
        if index == 1 and reply_mutation == "missing_memory":
            content = "violet-28 CHAT_SECOND_OK"
        elif index == 1 and reply_mutation == "missing_marker":
            content = "amber-17, remembered without the requested completion marker"
        delta = {"content": content}
        if native:
            delta["reasoning_content"] = f"synthetic-native-{index}"
        frames = [
            {"choices": [{"index": 0, "delta": delta}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        return httpx.Response(
            200, stream=_SSE(frames), headers={"content-type": "text/event-stream"}
        )

    observer = harness.WireObserver(
        harness.registry_endpoint(provider), httpx.MockTransport(respond)
    )
    async def run():
        return await harness.run_case(
            tmp_path,
            provider=provider,
            model=model,
            api_key="synthetic-test-key",
            observer=observer,
            scenario="chat",
            thinking=thinking,
        )

    if reply_mutation:
        expected_error = (
            "chat_memory_recall_mismatch"
            if reply_mutation == "missing_memory" else "final_reply_marker_missing"
        )
        with pytest.raises(harness.ReplayCheckError, match=expected_error):
            await run()
        return
    report = await run()
    assert report["ok"] is True
    assert report["scenario"] == "chat"
    assert report["thinking"] == thinking
    assert report["model"] == model
    assert report["model_calls"] == 2
    assert report["tool_calls"] == 0
    assert report["storage_reopened"] is True
    assert report["final_answers_verified"] == 2
    assert report["chat_memory_recall_verified"] is True
    assert report["native_wire_coverage"] == expected_coverage
    assert report["native_state"]["returned"] == (2 if native else 0)
    assert report["native_state"]["persisted"] == (2 if native else 0)
    assert "synthetic-native" not in json.dumps(report)
