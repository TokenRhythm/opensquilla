"""Real provider parser + Agent + finalizer + SQLite, with synthetic HTTP SSE."""

from __future__ import annotations

import gzip
import hashlib
import json
import zlib
from collections.abc import AsyncIterator
from contextlib import contextmanager
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


async def _compaction_rows(root):
    """Read independent durable evidence while the real runner owns the session."""
    storage = SessionStorage(str(root / "sessions.sqlite"))
    await storage.connect()
    try:
        manager = SessionManager(storage)
        key = "agent:main:synthetic-suffix-compaction"
        canonical = await manager.get_canonical_transcript(key)
        active = await manager.get_transcript(key)
        summaries = await manager.get_summaries(key)
        # Archive bookkeeping can change; message identity and every piece of
        # accepted content, including native provider state, must not change.
        semantic_fields = (
            "message_id", "role", "content", "tool_calls", "tool_call_id",
            "reasoning_content", "assistant_replay",
        )
        hashes = {
            row.message_id: hashlib.sha256(json.dumps(
                {name: getattr(row, name) for name in semantic_fields},
                sort_keys=True, ensure_ascii=False,
            ).encode()).hexdigest()
            for row in canonical
        }
        return hashes, {row.message_id for row in active}, summaries, canonical
    finally:
        await storage.close()


def _compaction_openrouter_catalog(monkeypatch, *models):
    """Supply synthetic public model metadata through the real catalog parser."""
    initialize = harness._Catalog.__init__

    def initialize_with_metadata(self, *args, **kwargs):
        initialize(self, *args, **kwargs)
        self._catalog._populate_from_data([
            {
                "id": model,
                "context_length": 128_000,
                "top_provider": {"max_completion_tokens": 8192},
                "supported_parameters": ["tools", "reasoning", "reasoning_effort"],
            }
            for model in models
        ])

    monkeypatch.setattr(harness._Catalog, "__init__", initialize_with_metadata)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["tokenrhythm", "openrouter"])
@pytest.mark.parametrize("variant,reasoning_evidence,wire_mutation", [
    ("basic", 1600, None), ("tools", 1600, None), ("replay_off", 1600, None),
    ("model_switch", 1600, None), ("repeated", 1600, None), ("truncated", 1600, None),
    ("long_reasoning", 1600, None), ("long_reasoning", 20, None),
    ("long_reasoning", None, None), ("basic", 1600, "missing_middle_source"),
    ("model_switch", 1600, "wrong_summary_and_resumed_models"),
])
async def test_compaction_acceptance_matrix_uses_real_runner_and_durable_content(
    tmp_path, monkeypatch, provider, variant, reasoning_evidence, wire_mutation
):
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    monkeypatch.setenv("OPENSQUILLA_LIVE_DISABLE_DOTENV", "1")
    model = harness.DEFAULT_MODELS[provider]
    next_model = (
        "deepseek-v4-flash" if provider == "tokenrhythm" else "deepseek/deepseek-v4-pro"
    )
    if provider == "openrouter":
        _compaction_openrouter_catalog(monkeypatch, model, next_model)
    has_tools = variant in {"tools", "replay_off"}
    summary_indexes = [2] if has_tools else [1, 3] if variant == "repeated" else [1]
    requests = []
    source_snapshots = []
    label = "qzmvkrpa"
    second_label = "bzntcpxd"
    summary = (
        f"The durable fact is COMPACTION_LABEL={label}. "
        "SYNTHETIC_COMPACTION_OLD_HISTORY was disposable background about colored paper. "
        "Preserve the generated label for the user's next turn."
    )

    async def respond(request):
        payload = json.loads(request.content)
        index = len(requests)
        requests.append(payload)
        assert index < (5 if variant == "repeated" else 4 if has_tools else 3)
        if index in summary_indexes:
            source_snapshots.append(await _compaction_rows(tmp_path))
            assert "summar" in json.dumps(payload["messages"][-1]).lower()
            assert label in json.dumps(payload["messages"])
            if index == 3:
                assert summary in json.dumps(payload["messages"])
                assert second_label in json.dumps(payload["messages"])
        if has_tools and index == 0:
            delta = {"tool_calls": [{
                "index": 0, "id": "synthetic-compaction-tool", "type": "function",
                "function": {"name": "replay_step", "arguments": '{"value": 7}'},
            }]}
            finish = "tool_calls"
        else:
            if index in summary_indexes:
                content = summary
                if index == 3:
                    content += f" Also preserve COMPACTION_LABEL_2={second_label}."
                if variant == "truncated":
                    content = "The"
            elif index == (1 if has_tools else 0):
                content = f"COMPACTION_LABEL={label}"
            elif variant == "repeated" and index == 2:
                content = f"{label} COMPACTION_RECALL_OK COMPACTION_LABEL_2={second_label}"
            elif variant == "truncated":
                content = "OK"
            else:
                content = f"{label} {second_label} COMPACTION_RECALL_OK"
            delta = {"content": content}
            finish = "length" if variant == "truncated" and index == 1 else "stop"
        native = f"synthetic-native-reasoning-{index}"
        if variant == "truncated" and index >= 1:
            native = ""
        if variant == "long_reasoning" and index == 1 and reasoning_evidence == 1600:
            native = "synthetic reasoning tokens " * 800
        if provider == "openrouter":
            delta["reasoning_details"] = [{
                "type": "reasoning.text", "text": native, "index": 0,
            }]
            delta["reasoning"] = native
        else:
            delta["reasoning_content"] = native
        reasoning_tokens = (
            reasoning_evidence if variant == "long_reasoning" and index == 1 else 10
        )
        if variant == "truncated" and index >= 1:
            reasoning_tokens = 0
        output_tokens = (
            1 if variant == "truncated" and index >= 1 else (reasoning_tokens or 0) + 100
        )
        usage = {
            "prompt_tokens": 8000, "completion_tokens": output_tokens,
            "prompt_tokens_details": {"cached_tokens": 0 if index == 0 else 4000},
        }
        if reasoning_tokens is not None:
            usage["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
        frames = [
            {"choices": [{"index": 0, "delta": delta}]},
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                "usage": usage,
            },
        ]
        return httpx.Response(
            200, stream=_SSE(frames), headers={"content-type": "text/event-stream"}
        )

    observer = harness.WireObserver(
        harness.registry_endpoint(provider), httpx.MockTransport(respond)
    )
    fault_count = 0
    if wire_mutation:
        observe = observer.observe

        @contextmanager
        def observe_faulty_transport():
            with observe():
                send = httpx.AsyncClient.send

                async def send_faulty_request(client, request, **kwargs):
                    nonlocal fault_count
                    payload = json.loads(request.content)
                    if wire_mutation == "missing_middle_source" and len(observer.calls) == 1:
                        messages = payload["messages"]
                        marker = "SYNTHETIC_COMPACTION_ENTRY_2_USER"
                        removed = [message for message in messages if marker in json.dumps(message)]
                        assert len(removed) == 1
                        payload["messages"] = [message for message in messages
                                               if message not in removed]
                        fault_count += 1
                    elif wire_mutation == "wrong_summary_and_resumed_models" and observer.calls:
                        payload["model"] = "synthetic-wrong-model"
                        fault_count += 1
                    headers = dict(request.headers)
                    headers.pop("content-length", None)
                    rewritten = httpx.Request(
                        request.method, request.url, headers=headers,
                        content=json.dumps(payload).encode(), extensions=request.extensions,
                    )
                    return await send(client, rewritten, **kwargs)

                # Inject at the actual HTTP boundary before WireObserver sees
                # the payload. The provider, runner and SQLite lifecycle stay real.
                with monkeypatch.context() as fault_patch:
                    fault_patch.setattr(httpx.AsyncClient, "send", send_faulty_request)
                    yield

        monkeypatch.setattr(observer, "observe", observe_faulty_transport)

    async def run():
        return await harness.run_case(
            tmp_path, provider=provider, model=model, api_key="synthetic-test-key",
            observer=observer, scenario="compaction",
            thinking="high" if variant == "replay_off" else "low",
            compaction_variant=variant,
            compaction_next_model=next_model if variant == "model_switch" else None,
        )

    if wire_mutation:
        expected_error = (
            "compaction_source_entry_missing" if wire_mutation == "missing_middle_source"
            else "compaction_request_model_mismatch"
        )
        with pytest.raises(harness.ReplayCheckError, match=expected_error):
            await run()
        assert len(requests) == 3
        assert fault_count == (1 if wire_mutation == "missing_middle_source" else 2)
        return
    report = await run()
    covered = variant != "long_reasoning" or reasoning_evidence == 1600
    assert report["ok"] is covered
    assert report["coverage"]["status"] == ("covered" if covered else "not_covered")
    required_codes = {
        "source_range", "current_tail", "archive_integrity", "summary_replayed",
        "memory_recall", "current_configuration",
    }
    if has_tools:
        required_codes.update({"tool_roundtrip", "tool_schema"})
    if variant == "replay_off":
        required_codes.update({"native_parent_state", "replay_disabled"})
    if variant == "model_switch":
        required_codes.add("model_switch")
    if variant == "repeated":
        required_codes.update({"repeated_compaction", "cumulative_memory"})
    if variant == "long_reasoning":
        required_codes.add("reasoning_over_1024")
    if variant == "truncated":
        required_codes = {"length_observed", "source_preserved", "no_summary_committed"}
    assert set(report["coverage"]["expected"]) == required_codes
    assert report["coverage"]["observed"] == {
        code: covered or code != "reasoning_over_1024" for code in required_codes
    }
    assert len(source_snapshots) == len(summary_indexes)
    first_summary_source = json.dumps(requests[summary_indexes[0]]["messages"][:-1])
    assert all(
        f"SYNTHETIC_COMPACTION_ENTRY_{index}_{role}" in first_summary_source
        for index in range(5) for role in ("USER", "ASSISTANT")
    )
    assert "SYNTHETIC_COMPACTION_ENTRY_5_USER" in first_summary_source
    hashes, active_ids, summaries, rows = await _compaction_rows(tmp_path)
    for source_hashes, _, _, _ in source_snapshots:
        assert {key: hashes[key] for key in source_hashes} == source_hashes
    if variant == "truncated":
        assert summaries == []
        assert source_snapshots[0][1].issubset(active_ids)
        assert observer.calls[1].finish_reason == "length"
        assert requests[1].get("max_tokens", requests[1].get("max_completion_tokens")) == 1
    else:
        assert report["source_entry_markers_verified"] == (
            [11, 1] if variant == "repeated" else [11]
        )
        assert len(summaries) == len(summary_indexes)
        assert all(item.summary_source == "llm" for item in summaries)
        assert label in summaries[-1].summary_text
        expected_calls = 5 if variant == "repeated" else 4 if has_tools else 3
        assert report["model_calls"] == len(requests) == expected_calls
    if has_tools:
        tools = requests[summary_indexes[0]]["tools"]
        assert tools == requests[0]["tools"] and tools
        messages = requests[summary_indexes[0]]["messages"]
        calls = []
        results = []
        for message in messages:
            for tool in message.get("tool_calls", []):
                calls.append((tool["id"], tool["function"]["name"],
                              json.loads(tool["function"]["arguments"])))
            if message["role"] == "tool":
                results.append((message["tool_call_id"], json.loads(message["content"])))
            content = message.get("content")
            if isinstance(content, str) and content.startswith("Recorded conversation context:"):
                # With replay disabled, the real OpenRouter runtime quotes
                # unavailable native history. The original call and result
                # must still reach the summarizer as associated records.
                recorded = json.loads(content.partition("\n")[2])
                for record in recorded:
                    if not isinstance(record.get("content"), list):
                        continue
                    for block in record["content"]:
                        if block.get("type") == "tool_use":
                            calls.append((block["id"], block["name"], block["input"]))
                        elif block.get("type") == "tool_result":
                            results.append((block["tool_use_id"], json.loads(block["content"])))
        assert calls == [("synthetic-compaction-tool", "replay_step", {"value": 7})]
        assert results == [("synthetic-compaction-tool", {"next_value": 18})]
        assert any(row.assistant_replay for row in rows)
    if variant == "replay_off":
        assert observer.calls[0].response.get("reasoning_content") or (
            observer.calls[0].response.get("reasoning_details")
        )
        for message in requests[2]["messages"]:
            assert not message.get("reasoning_content")
            assert not message.get("reasoning_details")
        assert any(message.get("reasoning_content") or message.get("reasoning_details")
                   for message in requests[1]["messages"])
        assert report["replay_transition"] == {"parent": True, "current": False}
    if variant == "model_switch":
        assert [request["model"] for request in requests] == [model, next_model, next_model]
    if variant == "repeated":
        assert "SYNTHETIC_COMPACTION_ENTRY_6_USER" in json.dumps(requests[3]["messages"][:-1])
        assert second_label in summaries[-1].summary_text
        assert second_label in observer.calls[-1].response["content"]
        assert label in observer.calls[-1].response["content"]
    if variant == "long_reasoning":
        assert report["reasoning_tokens_by_call"][1] == reasoning_evidence
        assert requests[1].get("max_tokens", requests[1].get("max_completion_tokens")) > 1024
        if provider == "openrouter":
            assert requests[1]["reasoning"]["effort"] == "high"
    public = json.dumps(report)
    assert not any(value in public for value in (
        label, second_label, "synthetic-test-key", "synthetic-native-reasoning",
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["tokenrhythm", "openrouter"])
@pytest.mark.parametrize("summary_finish", ["stop", "length"])
async def test_suffix_smoke_uses_real_preflight_and_reopened_sqlite(
    tmp_path, monkeypatch, provider, summary_finish
):
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    monkeypatch.setenv("OPENSQUILLA_LIVE_DISABLE_DOTENV", "1")
    outputs = [
        "COMPACTION_LABEL=qzmvkrpa",
        "The durable fact is COMPACTION_LABEL=qzmvkrpa. "
        "SYNTHETIC_COMPACTION_OLD_HISTORY was disposable background about colored paper. "
        "Preserve the generated label for the user's next turn.",
        "qzmvkrpa COMPACTION_RECALL_OK",
    ]
    requests = []

    async def respond(request):
        payload = json.loads(request.content)
        index = len(requests)
        requests.append(payload)
        assert index < 3, "unexpected recovery or extra provider call"
        if index == 1:
            text = json.dumps(payload["messages"])
            assert "COMPACTION_LABEL=qzmvkrpa" in text
            assert harness.COMPACTION_TAIL_MARKER not in text
            assert "summar" in payload["messages"][-1]["content"].lower()
        frames = [
            {"choices": [{"index": 0, "delta": {"content": outputs[index]}}]},
            {
                "choices": [{
                    "index": 0, "delta": {},
                    "finish_reason": summary_finish if index == 1 else "stop",
                }],
                "usage": {
                    "prompt_tokens": 8000, "completion_tokens": 50,
                    "prompt_tokens_details": {"cached_tokens": 0 if index == 0 else 4000},
                    "completion_tokens_details": {"reasoning_tokens": 0},
                },
            },
        ]
        return httpx.Response(
            200, stream=_SSE(frames), headers={"content-type": "text/event-stream"}
        )

    observer = harness.WireObserver(
        harness.registry_endpoint(provider), httpx.MockTransport(respond)
    )
    async def run():
        return await harness.run_case(
            tmp_path, provider=provider, model=harness.DEFAULT_MODELS[provider],
            api_key="synthetic-test-key", observer=observer, scenario="compaction", thinking="off",
        )

    if summary_finish == "length":
        with pytest.raises(harness.ReplayCheckError, match="single_compaction_not_persisted"):
            await run()
        storage = SessionStorage(str(tmp_path / "sessions.sqlite"))
        await storage.connect()
        try:
            manager = SessionManager(storage)
            key = "agent:main:synthetic-suffix-compaction"
            assert await manager.get_summaries(key) == []
            entries = await manager.get_transcript(key)
            assert any(harness.COMPACTION_SOURCE_MARKER in entry.content for entry in entries)
            assert any(entry.content == outputs[0] for entry in entries)
        finally:
            await storage.close()
        return
    report = await run()
    assert report["ok"] is True
    assert report["model_calls"] == len(requests) == 3
    assert report["storage_reopened"] is True
    assert report["compaction_archive_verified"] is True
    assert report["compaction_memory_recall_verified"] is True
    assert report["cached_input_tokens_by_call"] == [0, 4000, 4000]
    assert "qzmvkrpa" not in json.dumps(report)
    assert "synthetic-test-key" not in json.dumps(report)


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
