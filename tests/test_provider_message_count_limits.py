from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any

import httpx
import pytest
import structlog.testing

from opensquilla.provider import project_provider_message_count
from opensquilla.provider.openai import (
    OpenAIProvider,
    _tokenrhythm_message_limit_evidence,
)
from opensquilla.provider.types import (
    ChatConfig,
    ContentBlockToolResult,
    ErrorEvent,
    Message,
)


def _tokenrhythm_limit_body(*, maximum: int = 100, inclusive: bool = True) -> dict[str, Any]:
    return {
        "code": "BAD_REQUEST",
        "message": "请求参数错误",
        "data": [
            {
                "origin": "array",
                "code": "too_big",
                "maximum": maximum,
                "inclusive": inclusive,
                "path": ["messages"],
                "message": f"Too big: expected array to have <= {maximum} items",
                "value": ["response content must never be logged"],
            }
        ],
    }


def _patch_chat_response(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status_code: int,
    response_json: dict[str, Any],
    captured: dict[str, Any],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(status_code, json=response_json, request=request)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)


def _collect_errors(
    provider: OpenAIProvider,
    messages: list[Message],
    config: ChatConfig | None = None,
) -> list[ErrorEvent]:
    async def run() -> list[ErrorEvent]:
        return [
            event
            async for event in provider.chat(messages, config=config)
            if isinstance(event, ErrorEvent)
        ]

    return asyncio.run(run())


def test_openai_message_count_projection_matches_system_and_tool_result_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_chat_response(
        monkeypatch,
        status_code=400,
        response_json={"message": "synthetic stop"},
        captured=captured,
    )
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-v4-pro",
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
    )
    messages = [
        Message(role="user", content=f"message-{index}") for index in range(98)
    ]
    messages.append(
        Message(
            role="user",
            content=[
                ContentBlockToolResult(tool_use_id=f"call-{index}", content="ok")
                for index in range(4)
            ],
        )
    )

    projection = project_provider_message_count(
        provider,
        messages,
        ChatConfig(system="system"),
    )

    assert projection is not None
    assert projection.actual_wire_messages == 103
    assert projection.logical_messages == 99
    assert projection.system_messages == 1
    assert projection.tool_result_messages == 4
    assert projection.additional_messages == 0
    assert projection.provider_kind == "tokenrhythm"
    assert projection.model == "deepseek-v4-pro"
    assert projection.base_host == "tokenrhythm.studio"

    _collect_errors(provider, messages, ChatConfig(system="system"))
    assert projection.actual_wire_messages == len(captured["payload"]["messages"])


def test_openai_projection_counts_99_logical_messages_plus_system_as_100() -> None:
    provider = OpenAIProvider(api_key="test")
    messages = [Message(role="user", content="x") for _ in range(99)]

    projection = provider.project_message_count(messages, ChatConfig(system="system"))
    aggregator_projection = provider.project_message_count(
        messages,
        ChatConfig(system="system"),
        additional_messages=1,
    )

    assert projection.actual_wire_messages == 100
    assert aggregator_projection.actual_wire_messages == 101
    assert aggregator_projection.logical_messages == 100
    assert aggregator_projection.additional_messages == 1


def test_openai_projection_uses_same_prompt_schema_fallback_as_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_chat_response(
        monkeypatch,
        status_code=400,
        response_json={"message": "synthetic stop"},
        captured=captured,
    )
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-v4-pro",
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
    )
    messages = [Message(role="user", content="return structured output")]
    config = ChatConfig(
        output_json_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        }
    )

    projection = provider.project_message_count(messages, config)
    _collect_errors(provider, messages, config)

    assert projection.system_messages == 1
    assert projection.actual_wire_messages == 2
    assert projection.actual_wire_messages == len(captured["payload"]["messages"])
    assert captured["payload"]["messages"][0]["role"] == "system"


def test_tokenrhythm_original_583_envelope_yields_typed_limit_proof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    captured: dict[str, Any] = {}
    body = _tokenrhythm_limit_body()
    body["data"][0]["value"] = ["DO_NOT_RECORD_RESPONSE_VALUE"]
    _patch_chat_response(
        monkeypatch,
        status_code=400,
        response_json=body,
        captured=captured,
    )
    trace_path = tmp_path / "llm-calls.jsonl"
    monkeypatch.setenv("OPENSQUILLA_LLM_TRACE_RECORDER", "full")
    monkeypatch.setenv("OPENSQUILLA_LLM_TRACE_PATH", str(trace_path))
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-v4-pro",
        base_url="https://api.tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
    )
    messages = [Message(role="user", content=f"m-{index}") for index in range(101)]

    with structlog.testing.capture_logs() as logs:
        errors = _collect_errors(provider, messages)

    assert len(errors) == 1
    error = errors[0]
    assert error.code == "400"
    assert "BAD_REQUEST: 请求参数错误" in error.message
    assert "Too big: expected array to have <= 100 items" in error.message
    assert "DO_NOT_RECORD_RESPONSE_VALUE" not in error.message
    assert error.message_limit_proof is not None
    assert error.message_limit_proof.actual_wire_messages == 101
    assert error.message_limit_proof.limit == 100
    assert error.message_limit_proof.logical_messages == 101
    assert error.message_limit_proof.system_messages == 0
    assert error.message_limit_proof.tool_result_messages == 0
    assert error.message_limit_proof.provider_kind == "tokenrhythm"
    assert error.message_limit_proof.model == "deepseek-v4-pro"
    assert error.message_limit_proof.base_host == "api.tokenrhythm.studio"

    detected = [
        entry
        for entry in logs
        if entry["event"] == "provider.request_message_limit_detected"
    ]
    assert len(detected) == 1
    assert detected[0]["actual_wire_messages"] == 101
    assert detected[0]["limit"] == 100
    assert "DO_NOT_RECORD_RESPONSE_VALUE" not in json.dumps(logs)
    assert not [entry for entry in logs if entry["event"] == "provider.chat_http_error"]

    trace_rows = [
        json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    error_row = next(row for row in trace_rows if row["event"] == "llm.error")
    assert error_row["code"] == "400"
    assert error_row["message"] == "Provider request failed"
    assert error_row["message_chars"] == len("Provider request message limit detected")
    assert error_row["response_body"] is None
    assert error_row["metadata"]["message_limit_proof"]["limit"] == 100
    assert "DO_NOT_RECORD_RESPONSE_VALUE" not in json.dumps(trace_rows)


def test_tokenrhythm_uses_strictest_valid_limit_and_exclusive_maximum() -> None:
    body = _tokenrhythm_limit_body(maximum=120)
    body["data"].append(
        {
            "origin": "array",
            "code": "too_big",
            "maximum": 101,
            "inclusive": False,
            "path": ["messages"],
            "message": "exclusive constraint",
        }
    )

    evidence = _tokenrhythm_message_limit_evidence(
        provider_kind="tokenrhythm",
        base_url="https://tokenrhythm.studio/v1",
        model="glm-5.2",
        status_code=400,
        body=json.dumps(body),
        wire_messages=[{"role": "user"}] * 101,
        logical_messages=101,
    )

    assert evidence is not None
    proof, first_message = evidence
    assert proof.limit == 100
    assert first_message == "Too big: expected array to have <= 120 items"


def test_tokenrhythm_validation_detail_is_single_line_bounded_and_redacted() -> None:
    body = _tokenrhythm_limit_body(maximum=1)
    secret = "sk_tr_abcdefghijklmnop1234"
    body["data"][0]["message"] = f"Too big\ncredential={secret}"

    evidence = _tokenrhythm_message_limit_evidence(
        provider_kind="tokenrhythm",
        base_url="tokenrhythm.studio/v1",
        model="glm-5.2",
        status_code=400,
        body=json.dumps(body),
        wire_messages=[{"role": "user"}] * 2,
        logical_messages=2,
    )

    assert evidence is not None
    proof, validation_message = evidence
    assert proof.base_host == "tokenrhythm.studio"
    assert validation_message == "Too big credential=[REDACTED]"
    assert secret not in validation_message


@pytest.mark.parametrize(
    ("provider_kind", "base_url", "status_code", "mutator"),
    [
        ("openai", "https://tokenrhythm.studio/v1", 400, None),
        ("tokenrhythm", "https://tokenrhythm.studio.example.com/v1", 400, None),
        ("tokenrhythm", "https://tokenrhythm.studio/v1", 422, None),
        ("tokenrhythm", "https://tokenrhythm.studio/v1", 400, ("code", "OTHER")),
        ("tokenrhythm", "https://tokenrhythm.studio/v1", 400, ("origin", "string")),
        ("tokenrhythm", "https://tokenrhythm.studio/v1", 400, ("code_row", "other")),
        ("tokenrhythm", "https://tokenrhythm.studio/v1", 400, ("path", ["input"])),
        ("tokenrhythm", "https://tokenrhythm.studio/v1", 400, ("maximum", True)),
        ("tokenrhythm", "https://tokenrhythm.studio/v1", 400, ("maximum", "100")),
        ("tokenrhythm", "https://tokenrhythm.studio/v1", 400, ("maximum", 0)),
        ("tokenrhythm", "https://tokenrhythm.studio/v1", 400, ("inclusive", 1)),
    ],
)
def test_message_limit_detection_rejects_inexact_evidence(
    provider_kind: str,
    base_url: str,
    status_code: int,
    mutator: tuple[str, Any] | None,
) -> None:
    body = deepcopy(_tokenrhythm_limit_body(maximum=7))
    if mutator is not None:
        key, value = mutator
        if key == "code":
            body["code"] = value
        elif key == "code_row":
            body["data"][0]["code"] = value
        else:
            body["data"][0][key] = value

    assert (
        _tokenrhythm_message_limit_evidence(
            provider_kind=provider_kind,
            base_url=base_url,
            model="glm-5.2",
            status_code=status_code,
            body=json.dumps(body),
            wire_messages=[{"role": "user"}] * 8,
            logical_messages=8,
        )
        is None
    )


def test_message_limit_detection_requires_local_count_above_observed_limit() -> None:
    evidence = _tokenrhythm_message_limit_evidence(
        provider_kind="tokenrhythm",
        base_url="https://tokenrhythm.studio/v1",
        model="glm-5.2",
        status_code=400,
        body=json.dumps(_tokenrhythm_limit_body(maximum=7)),
        wire_messages=[{"role": "user"}] * 7,
        logical_messages=7,
    )

    assert evidence is None


def test_nonofficial_tokenrhythm_host_keeps_generic_400_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_chat_response(
        monkeypatch,
        status_code=400,
        response_json=_tokenrhythm_limit_body(maximum=1),
        captured=captured,
    )
    provider = OpenAIProvider(
        api_key="test",
        base_url="https://tokenrhythm.studio.example.com/v1",
        provider_kind="tokenrhythm",
    )

    errors = _collect_errors(
        provider,
        [Message(role="user", content="one"), Message(role="user", content="two")],
    )

    assert len(errors) == 1
    assert errors[0].code == "400"
    assert errors[0].message_limit_proof is None
    assert errors[0].message == (
        "TokenRhythm chat request failed (HTTP 400): BAD_REQUEST: 请求参数错误"
    )
