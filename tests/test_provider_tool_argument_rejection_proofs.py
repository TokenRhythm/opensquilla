"""A rejected batch is recoverable only after authoritative response completion."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from opensquilla.provider.anthropic import AnthropicProvider
from opensquilla.provider.ollama import OllamaProvider
from opensquilla.provider.openai_codex import OpenAICodexProvider
from opensquilla.provider.openai_responses import OpenAIResponsesProvider
from opensquilla.provider.types import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    TextDeltaEvent,
    ToolUseEndEvent,
)

_BAD = '{"content":"private-do-not-replay'
_CALLS = [("good", '{"content":"ok"}'), ("bad", _BAD)]
_PROVIDERS = ("anthropic", "openai_codex", "openai_responses", "ollama")


def _body(
    kind: str,
    calls: list[tuple[str, Any]],
    *,
    terminal: str = "complete",
    usage: bool = True,
    late_conflict: bool = False,
) -> bytes:
    if kind == "openai_responses":
        data: dict[str, Any] = {
            "status": "completed" if terminal == "complete" else "incomplete",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "Preparing."}]},
                *[
                    {
                        "type": "function_call",
                        "id": f"item_{i}",
                        "call_id": call_id,
                        "name": "write_file",
                        "arguments": arguments,
                    }
                    for i, (call_id, arguments) in enumerate(calls)
                ],
            ],
        }
        if terminal == "length":
            data["incomplete_details"] = {"reason": "max_output_tokens"}
        if late_conflict:
            data["output"][-1]["id"] = "item_0"
        if usage:
            data["usage"] = {"input_tokens": 7, "output_tokens": 3}
        return json.dumps(data).encode()
    if kind == "ollama":
        chunks: list[dict[str, Any]] = [{"message": {"content": "Preparing."}}]
        for call_id, arguments in calls:
            try:
                parsed = json.loads(arguments)
            except (json.JSONDecodeError, TypeError):
                parsed = arguments
            chunks.append(
                {
                    "message": {
                        "tool_calls": [
                            {"id": call_id, "function": {"name": "write_file", "arguments": parsed}}
                        ]
                    }
                }
            )
        if late_conflict:
            chunks.append(
                {
                    "message": {
                        "tool_calls": [
                            {"id": calls[0][0], "function": {"name": "other", "arguments": {}}}
                        ]
                    }
                }
            )
        if terminal != "eof":
            last = {"done": True, "done_reason": "length" if terminal == "length" else "stop"}
            if usage:
                last.update(prompt_eval_count=7, eval_count=3)
            chunks.append(last)
        return b"".join((json.dumps(chunk) + "\n").encode() for chunk in chunks)
    if kind == "anthropic":
        start: dict[str, Any] = {"type": "message_start", "message": {"id": "test"}}
        if usage:
            start["message"]["usage"] = {"input_tokens": 7}
        events = [
            start,
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": "Preparing."},
            },
            {"type": "content_block_stop", "index": 0},
        ]
        for index, (call_id, arguments) in enumerate(calls, 1):
            events.extend(
                [
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "tool_use", "id": call_id, "name": "write_file"},
                    },
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "input_json_delta", "partial_json": arguments},
                    },
                    {"type": "content_block_stop", "index": index},
                ]
            )
        if late_conflict:
            events.append(
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "tool_use", "id": "changed", "name": "other"},
                }
            )
        if terminal != "eof":
            delta = {
                "type": "message_delta",
                "delta": {"stop_reason": "max_tokens" if terminal == "length" else "tool_use"},
            }
            if usage:
                delta["usage"] = {"output_tokens": 3}
            events.extend([delta, {"type": "message_stop"}])
    else:
        events = [{"type": "response.output_text.delta", "delta": "Preparing."}]
        for index, (call_id, arguments) in enumerate(calls):
            item = {
                "type": "function_call",
                "id": f"item_{index}",
                "call_id": call_id,
                "name": "write_file",
            }
            events.extend(
                [
                    {"type": "response.output_item.added", "item": item},
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": f"item_{index}",
                        "delta": arguments,
                    },
                    {"type": "response.output_item.done", "item": {**item, "arguments": arguments}},
                ]
            )
        if late_conflict:
            events.append(
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": f"item_{len(calls) - 1}",
                    "delta": "late",
                }
            )
        if terminal != "eof":
            response: dict[str, Any] = {"status": "completed"}
            if usage:
                response["usage"] = {"input_tokens": 7, "output_tokens": 3}
            if terminal == "length":
                response.update(
                    status="incomplete", incomplete_details={"reason": "max_output_tokens"}
                )
            events.append(
                {
                    "type": "response.incomplete" if terminal == "length" else "response.completed",
                    "response": response,
                }
            )
    return b"".join(("data: " + json.dumps(event) + "\n\n").encode() for event in events)


def _collect(kind: str, body: bytes, monkeypatch: Any, tmp_path: Path) -> list[Any]:
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=body))

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real_client(*args, **{**kwargs, "transport": transport})

    monkeypatch.setattr(httpx, "AsyncClient", client)
    if kind == "anthropic":
        provider = AnthropicProvider(api_key="offline-test", model="test")
    elif kind == "ollama":
        provider = OllamaProvider(model="test")
    elif kind == "openai_responses":
        provider = OpenAIResponsesProvider(api_key="offline-test", model="test")
    else:
        auth_path = tmp_path / "synthetic-auth.json"
        auth_path.write_text(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "tokens": {
                        "access_token": "offline-test",
                        "refresh_token": "offline-test",
                        "account_id": "test",
                    },
                }
            )
        )
        provider = OpenAICodexProvider(auth_path=str(auth_path))

    async def run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="Write a page")], config=ChatConfig()
            )
        ]

    return asyncio.run(run())


@pytest.mark.parametrize("kind", _PROVIDERS)
@pytest.mark.parametrize("usage", [True, False])
def test_rejection_proves_entire_unexecuted_batch_and_preserves_usage(
    kind: str,
    usage: bool,
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    events = _collect(kind, _body(kind, _CALLS, usage=usage), monkeypatch, tmp_path)
    assert not any(isinstance(event, (ToolUseEndEvent, DoneEvent)) for event in events)
    assert any(isinstance(event, TextDeltaEvent) and event.text == "Preparing." for event in events)
    (error,) = [event for event in events if isinstance(event, ErrorEvent)]
    proof = error.tool_argument_rejection
    assert proof is not None
    assert [(call.tool_call_id, call.reason) for call in proof.calls] == [
        ("good", "batch_not_executed"),
        ("bad", "invalid_json"),
    ]
    assert all(call.tool_name == "write_file" for call in proof.calls)
    assert "private-do-not-replay" not in repr(proof)
    if usage:
        assert error.model_usage_breakdown[0]["input_tokens"] == 7
        assert error.model_usage_breakdown[0]["output_tokens"] == 3
        assert error.usage_missing_count == 0
    else:
        assert error.model_usage_breakdown == []
        assert error.usage_missing_count == 1


@pytest.mark.parametrize("kind", _PROVIDERS)
@pytest.mark.parametrize("terminal", ["eof", "length"])
def test_no_rejection_proof_without_successful_response_terminal(
    kind: str,
    terminal: str,
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    events = _collect(kind, _body(kind, _CALLS, terminal=terminal), monkeypatch, tmp_path)
    assert not any(isinstance(event, ToolUseEndEvent) for event in events)
    assert all(
        event.tool_argument_rejection is None for event in events if isinstance(event, ErrorEvent)
    )


@pytest.mark.parametrize("kind", _PROVIDERS)
def test_no_rejection_proof_when_a_sibling_identity_conflicts(
    kind: str,
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    events = _collect(kind, _body(kind, [("same", "{}"), ("same", _BAD)]), monkeypatch, tmp_path)
    assert not any(isinstance(event, (ToolUseEndEvent, DoneEvent)) for event in events)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert errors
    assert all(event.tool_argument_rejection is None for event in errors)


@pytest.mark.parametrize("kind", _PROVIDERS)
def test_later_protocol_mutation_cannot_launder_argument_rejection(
    kind: str,
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    events = _collect(kind, _body(kind, _CALLS, late_conflict=True), monkeypatch, tmp_path)
    assert not any(isinstance(event, (ToolUseEndEvent, DoneEvent)) for event in events)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert errors
    assert all(event.tool_argument_rejection is None for event in errors)


@pytest.mark.parametrize("kind", _PROVIDERS)
def test_oversized_bad_arguments_are_not_recoverable_protocol_evidence(
    kind: str,
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    events = _collect(
        kind, _body(kind, [("bad", '{"content":"' + "x" * 256_001)]), monkeypatch, tmp_path
    )
    assert not any(isinstance(event, (ToolUseEndEvent, DoneEvent)) for event in events)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert errors
    assert all(event.tool_argument_rejection is None for event in errors)


@pytest.mark.parametrize("kind", _PROVIDERS)
@pytest.mark.parametrize("arguments", ['["not-an-object"]', '{"content":NaN}', '{"content":1e999}'])
def test_complete_non_object_or_non_finite_arguments_are_rejected_without_execution(
    kind: str,
    arguments: str,
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    events = _collect(kind, _body(kind, [("bad", arguments)]), monkeypatch, tmp_path)
    assert not any(isinstance(event, (ToolUseEndEvent, DoneEvent)) for event in events)
    (error,) = [event for event in events if isinstance(event, ErrorEvent)]
    assert error.tool_argument_rejection is not None
    assert error.tool_argument_rejection.calls[0].reason == "invalid_json"


@pytest.mark.parametrize(
    ("kind", "mutation"),
    [
        ("anthropic", "missing_block_stop"),
        ("anthropic", "missing_message_delta"),
        ("openai_codex", "missing_item_done"),
        ("openai_codex", "repeated_item_added"),
        ("openai_codex", "repeated_item_done"),
    ],
)
def test_response_success_alone_does_not_prove_complete_tool_lifecycle(
    kind: str,
    mutation: str,
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    wire_events = [
        json.loads(line.removeprefix("data: "))
        for line in _body(kind, [("bad", _BAD)]).decode().splitlines()
        if line.startswith("data: ")
    ]
    if mutation == "missing_block_stop":
        wire_events = [
            event
            for event in wire_events
            if not (event["type"] == "content_block_stop" and event["index"] == 1)
        ]
    elif mutation == "missing_message_delta":
        wire_events = [event for event in wire_events if event["type"] != "message_delta"]
    elif mutation == "missing_item_done":
        wire_events = [
            event for event in wire_events if event["type"] != "response.output_item.done"
        ]
    else:
        duplicate_type = (
            "response.output_item.added"
            if mutation == "repeated_item_added"
            else "response.output_item.done"
        )
        duplicate = next(event for event in wire_events if event["type"] == duplicate_type)
        wire_events.insert(len(wire_events) - 1, duplicate)
    body = b"".join(("data: " + json.dumps(event) + "\n\n").encode() for event in wire_events)
    events = _collect(kind, body, monkeypatch, tmp_path)
    assert not any(isinstance(event, (ToolUseEndEvent, DoneEvent)) for event in events)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert errors
    assert all(event.tool_argument_rejection is None for event in errors)


@pytest.mark.parametrize("mutation", ["missing_identity", "object_arguments", "null_arguments"])
def test_responses_protocol_shape_cannot_authorize_argument_recovery(
    mutation: str,
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    body = json.loads(_body("openai_responses", _CALLS))
    call = body["output"][1]
    if mutation == "missing_identity":
        del call["call_id"]
        del call["id"]
    elif mutation == "object_arguments":
        call["arguments"] = {"content": "ok"}
    else:
        call["arguments"] = None
    events = _collect("openai_responses", json.dumps(body).encode(), monkeypatch, tmp_path)
    assert not any(isinstance(event, (ToolUseEndEvent, DoneEvent)) for event in events)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert errors
    assert all(event.tool_argument_rejection is None for event in errors)


@pytest.mark.parametrize("batch", [False, True])
def test_ollama_non_finite_arguments_cannot_bypass_argument_bounds(
    batch: bool,
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    arguments = '{"content":"' + "x" * (210_000 if batch else 256_001) + '","value":NaN}'
    calls = [(f"bad_{index}", arguments) for index in range(5 if batch else 1)]
    events = _collect("ollama", _body("ollama", calls), monkeypatch, tmp_path)
    assert not any(isinstance(event, (ToolUseEndEvent, DoneEvent)) for event in events)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert errors
    assert all(event.tool_argument_rejection is None for event in errors)


@pytest.mark.parametrize("kind", _PROVIDERS)
def test_unreported_token_counters_are_not_fabricated_as_zero_usage(
    kind: str,
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    raw_body = _body(kind, _CALLS, usage=False)
    if kind == "openai_responses":
        data = json.loads(raw_body)
        data["usage"] = {"metadata": "not a token receipt"}
        body = json.dumps(data).encode()
    elif kind == "ollama":
        chunks = [json.loads(line) for line in raw_body.splitlines()]
        chunks[-1].update(prompt_eval_count=None, eval_count=None)
        body = b"".join((json.dumps(chunk) + "\n").encode() for chunk in chunks)
    else:
        chunks = [
            json.loads(line.removeprefix("data: "))
            for line in raw_body.decode().splitlines()
            if line.startswith("data: ")
        ]
        if kind == "anthropic":
            chunks[0]["message"]["usage"] = {"metadata": "not a token receipt"}
            chunks[-2]["usage"] = {"metadata": "not a token receipt"}
        else:
            chunks[-1]["response"]["usage"] = {"metadata": "not a token receipt"}
        body = b"".join(("data: " + json.dumps(chunk) + "\n\n").encode() for chunk in chunks)
    events = _collect(kind, body, monkeypatch, tmp_path)
    (error,) = [event for event in events if isinstance(event, ErrorEvent)]
    assert error.tool_argument_rejection is not None
    assert error.model_usage_breakdown == []
    assert error.usage_missing_count == 1


@pytest.mark.parametrize("arguments", [None, {"content": "not a wire string"}])
def test_codex_non_string_terminal_arguments_are_a_protocol_error(
    arguments: Any,
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    chunks = [
        json.loads(line.removeprefix("data: "))
        for line in _body("openai_codex", _CALLS).decode().splitlines()
        if line.startswith("data: ")
    ]
    for chunk in chunks:
        if chunk["type"] == "response.output_item.done":
            chunk["item"]["arguments"] = arguments
            break
    body = b"".join(("data: " + json.dumps(chunk) + "\n\n").encode() for chunk in chunks)
    events = _collect("openai_codex", body, monkeypatch, tmp_path)
    assert not any(isinstance(event, (ToolUseEndEvent, DoneEvent)) for event in events)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert errors
    assert all(event.tool_argument_rejection is None for event in errors)
