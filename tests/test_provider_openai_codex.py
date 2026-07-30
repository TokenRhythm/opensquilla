"""Contract tests for the openai_codex (ChatGPT OAuth) provider."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from opensquilla.provider.codex_auth import (
    CodexAuthError,
    load_codex_credentials,
    refresh_codex_credentials,
)
from opensquilla.provider.openai_codex import OpenAICodexProvider, _candidate_wire_digest
from opensquilla.provider.registry import get_provider_spec
from opensquilla.provider.selector import build_provider
from opensquilla.provider.types import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    ReasoningDeltaEvent,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_candidate_wire_digest_rejects_oversized_id_before_stripping() -> None:
    class _NoStripString(str):
        def strip(self, chars: str | None = None) -> str:
            del chars
            raise AssertionError("oversized candidate wire IDs must not be copied")

    assert _candidate_wire_digest(_NoStripString(" " * 4097)) is None


def _write_auth(path: Path, *, access="tok-access", refresh="tok-refresh", account="acct-1"):
    payload = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "",
            "access_token": access,
            "refresh_token": refresh,
            "account_id": account,
        },
        "last_refresh": "2026-06-29T21:53:00Z",
    }
    path.write_text(json.dumps(payload))
    return path


def _sse(events: list[dict[str, Any]]) -> bytes:
    return b"".join(f"data: {json.dumps(ev)}\n\n".encode() for ev in events)


def _happy_sse() -> bytes:
    return _sse(
        [
            {"type": "response.created", "response": {"id": "resp-1"}},
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "id": "fc-1",
                    "call_id": "call-1",
                    "name": "search",
                },
            },
            {"type": "response.function_call_arguments.delta", "item_id": "fc-1", "delta": '{"que'},
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc-1",
                "delta": 'ry": "x"}',
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "fc-1",
                    "call_id": "call-1",
                    "name": "search",
                    "arguments": '{"query": "x"}',
                },
            },
            {"type": "response.output_text.delta", "delta": "done"},
            {
                "type": "response.completed",
                "response": {
                    "id": "resp-1",
                    "model": "gpt-5.2-codex",
                    "usage": {
                        "input_tokens": 40,
                        "output_tokens": 9,
                        "input_tokens_details": {"cached_tokens": 12},
                        "output_tokens_details": {"reasoning_tokens": 3},
                    },
                },
            },
        ]
    )


def _patch_codex_transport(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai_codex.httpx.AsyncClient", patched)


def _collect(provider: OpenAICodexProvider, *, tools=None, cfg=None):
    async def _run():
        return [
            ev
            async for ev in provider.chat(
                [Message(role="user", content="hi")], tools=tools, config=cfg or ChatConfig()
            )
        ]

    return asyncio.run(_run())


_SEARCH_TOOL = ToolDefinition(
    name="search",
    description="Search things.",
    input_schema=ToolInputSchema(properties={"query": {"type": "string"}}),
)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_load_credentials_from_auth_file(tmp_path: Path) -> None:
    auth = _write_auth(tmp_path / "auth.json")
    creds = load_codex_credentials(auth)
    assert creds.access_token == "tok-access"
    assert creds.refresh_token == "tok-refresh"
    assert creds.account_id == "acct-1"


def test_missing_auth_file_points_at_codex_login(tmp_path: Path) -> None:
    with pytest.raises(CodexAuthError, match="codex login"):
        load_codex_credentials(tmp_path / "missing.json")


def test_account_id_falls_back_to_jwt_claim(tmp_path: Path) -> None:
    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": "acct-jwt"}}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    jwt = f"h.{payload}.s"
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps({"tokens": {"access_token": "tok", "id_token": jwt, "account_id": None}})
    )
    assert load_codex_credentials(auth).account_id == "acct-jwt"


def test_refresh_persists_new_tokens(tmp_path: Path, monkeypatch) -> None:
    auth = _write_auth(tmp_path / "auth.json")
    creds = load_codex_credentials(auth)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"access_token": "tok-new", "refresh_token": "refresh-new", "id_token": ""},
        )

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(
        "opensquilla.provider.codex_auth.httpx.AsyncClient",
        lambda *a, **kw: real(*a, **{**kw, "transport": transport}),
    )

    updated = asyncio.run(refresh_codex_credentials(creds, path=auth))
    assert updated.access_token == "tok-new"
    assert captured["body"]["grant_type"] == "refresh_token"
    assert captured["body"]["client_id"].startswith("app_")
    on_disk = json.loads(auth.read_text())
    assert on_disk["tokens"]["access_token"] == "tok-new"
    assert on_disk["tokens"]["refresh_token"] == "refresh-new"
    assert on_disk["last_refresh"].endswith("Z")


# ---------------------------------------------------------------------------
# Provider wire behavior
# ---------------------------------------------------------------------------


def test_stream_maps_responses_events(tmp_path: Path, monkeypatch) -> None:
    auth = _write_auth(tmp_path / "auth.json")
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_happy_sse()
        )

    _patch_codex_transport(monkeypatch, handler)
    provider = OpenAICodexProvider(
        model="gpt-5.2-codex",
        base_url="https://chatgpt.com",  # normalization adds /backend-api
        auth_path=str(auth),
    )
    events = _collect(provider, tools=[_SEARCH_TOOL], cfg=ChatConfig(system="be brief"))

    assert captured["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert captured["headers"]["authorization"] == "Bearer tok-access"
    assert captured["headers"]["chatgpt-account-id"] == "acct-1"
    assert captured["headers"]["originator"] == "codex_cli_rs"
    payload = captured["payload"]
    assert payload["instructions"] == "be brief"
    assert payload["store"] is False and payload["stream"] is True
    assert payload["include"] == ["reasoning.encrypted_content"]
    # Protocol fact (verified live): the ChatGPT codex backend rejects
    # max_output_tokens with HTTP 400 "Unsupported parameter" — it must
    # never be sent; subscription turns carry no client-set output cap.
    assert "max_output_tokens" not in payload
    (tool,) = payload["tools"]
    assert tool["type"] == "function" and tool["name"] == "search"
    assert "function" not in tool  # flat Responses shape, not chat-completions nesting

    starts = [e for e in events if isinstance(e, ToolUseStartEvent)]
    deltas = [e for e in events if isinstance(e, ToolUseDeltaEvent)]
    ends = [e for e in events if isinstance(e, ToolUseEndEvent)]
    texts = [e for e in events if isinstance(e, TextDeltaEvent)]
    (done,) = [e for e in events if isinstance(e, DoneEvent)]
    assert [s.tool_use_id for s in starts] == ["call-1"]
    assert [d.json_fragment for d in deltas] == ['{"que', 'ry": "x"}']
    assert ends[0].arguments == {"query": "x"}
    assert [t.text for t in texts] == ["done"]
    assert done.stop_reason == "tool_use"
    assert (done.input_tokens, done.output_tokens) == (40, 9)
    assert done.cached_tokens == 12 and done.reasoning_tokens == 3
    assert done.model == "gpt-5.2-codex"


def test_burst_function_call_without_deltas(tmp_path: Path, monkeypatch) -> None:
    auth = _write_auth(tmp_path / "auth.json")
    body = _sse(
        [
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "fc-9",
                    "call_id": "call-9",
                    "name": "search",
                    "arguments": '{"query": "y"}',
                },
            },
            {"type": "response.completed", "response": {"id": "r", "usage": {}}},
        ]
    )
    _patch_codex_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        ),
    )
    provider = OpenAICodexProvider(auth_path=str(auth))
    events = _collect(provider, tools=[_SEARCH_TOOL])
    ends = [e for e in events if isinstance(e, ToolUseEndEvent)]
    assert len(ends) == 1 and ends[0].arguments == {"query": "y"}
    assert any(isinstance(e, ToolUseStartEvent) for e in events)


def test_inert_candidate_demotes_codex_function_call_after_completed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    auth = _write_auth(tmp_path / "auth.json")
    long_name = "candidate-" + ("x" * 17_000)
    body = _sse(
        [
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "id": "fc-private",
                    "call_id": "call-private",
                    "name": long_name,
                },
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc-private",
                "delta": "{not-json",
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "fc-private",
                    "call_id": "call-private",
                    "name": long_name,
                    "arguments": "{not-json",
                },
            },
            {"type": "response.output_text.delta", "delta": "plain answer"},
            {
                "type": "response.completed",
                "response": {
                    "id": "resp-private",
                    "model": "gpt-5.5",
                    "usage": {
                        "input_tokens": 13,
                        "output_tokens": 8,
                        "input_tokens_details": {"cached_tokens": 4},
                        "output_tokens_details": {"reasoning_tokens": 2},
                    },
                },
            },
        ]
    )
    _patch_codex_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        ),
    )
    provider = OpenAICodexProvider(auth_path=str(auth))

    events = _collect(
        provider,
        tools=[],
        cfg=ChatConfig(candidate_output_mode="inert_artifact"),
    )

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert not any(
        isinstance(event, (ToolUseStartEvent, ToolUseDeltaEvent, ToolUseEndEvent))
        for event in events
    )
    texts = [event for event in events if isinstance(event, TextDeltaEvent)]
    assert texts[0].text == "plain answer"
    artifact = json.loads(texts[1].text)
    assert artifact["kind"] == "inert_proposer_tool_output"
    assert artifact["executable"] is False
    assert artifact["actions"] == [
        {
            "arguments_text": "{not-json",
            "issues": [
                "invalid_arguments_json",
                "name_over_execution_limit",
            ],
            "name_text": long_name,
        }
    ]
    assert "fc-private" not in texts[1].text
    assert "call-private" not in texts[1].text
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert (done.input_tokens, done.output_tokens) == (13, 8)
    assert done.cached_tokens == 4
    assert done.reasoning_tokens == 2
    assert done.stop_reason == "tool_use"
    assert events[-2:] == [texts[1], done]


def test_inert_candidate_allows_call_id_to_appear_on_done(
    tmp_path: Path,
    monkeypatch,
) -> None:
    auth = _write_auth(tmp_path / "auth.json")
    body = _sse(
        [
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "id": "fc-late-call-id",
                    "name": "get_status",
                },
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "fc-late-call-id",
                    "call_id": "call-late",
                    "name": "get_status",
                    "arguments": "{}",
                },
            },
            {"type": "response.completed", "response": {"status": "completed"}},
        ]
    )
    _patch_codex_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        ),
    )

    events = _collect(
        OpenAICodexProvider(auth_path=str(auth)),
        tools=[],
        cfg=ChatConfig(candidate_output_mode="inert_artifact"),
    )

    assert not any(isinstance(event, ErrorEvent) for event in events)
    artifact = json.loads(
        next(event.text for event in events if isinstance(event, TextDeltaEvent))
    )
    assert artifact["actions"] == [
        {
            "arguments_text": "{}",
            "issues": [],
            "name_text": "get_status",
        }
    ]
    assert next(event for event in events if isinstance(event, DoneEvent)).stop_reason == (
        "tool_use"
    )


def test_inert_candidate_rejects_changed_call_id_without_committing_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    auth = _write_auth(tmp_path / "auth.json")
    body = _sse(
        [
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "id": "fc-stable",
                    "call_id": "call-original",
                    "name": "search",
                },
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "fc-stable",
                    "call_id": "call-conflict",
                    "name": "search",
                    "arguments": '{"query":"Shanghai"}',
                },
            },
            {"type": "response.completed", "response": {"status": "completed"}},
        ]
    )
    _patch_codex_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        ),
    )

    events = _collect(
        OpenAICodexProvider(auth_path=str(auth)),
        tools=[],
        cfg=ChatConfig(candidate_output_mode="inert_artifact"),
    )

    assert not any(isinstance(event, TextDeltaEvent) for event in events)
    assert not any(
        isinstance(event, (ToolUseStartEvent, ToolUseDeltaEvent, ToolUseEndEvent))
        for event in events
    )
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert any(
        isinstance(event, ErrorEvent) and event.code == "incomplete_tool_call"
        for event in events
    )


def test_inert_candidate_rejects_argument_delta_after_item_done(
    tmp_path: Path,
    monkeypatch,
) -> None:
    auth = _write_auth(tmp_path / "auth.json")
    body = _sse(
        [
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "fc-finished",
                    "call_id": "call-finished",
                    "name": "search",
                    "arguments": '{"query":"Shanghai"}',
                },
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc-finished",
                "delta": '"late"',
            },
            {"type": "response.completed", "response": {"status": "completed"}},
        ]
    )
    _patch_codex_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        ),
    )

    events = _collect(
        OpenAICodexProvider(auth_path=str(auth)),
        tools=[],
        cfg=ChatConfig(candidate_output_mode="inert_artifact"),
    )

    assert not any(isinstance(event, TextDeltaEvent) for event in events)
    assert not any(
        isinstance(event, (ToolUseStartEvent, ToolUseDeltaEvent, ToolUseEndEvent))
        for event in events
    )
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert any(
        isinstance(event, ErrorEvent) and event.code == "incomplete_tool_call"
        for event in events
    )


def test_inert_candidate_does_not_publish_partial_codex_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    auth = _write_auth(tmp_path / "auth.json")
    body = _sse(
        [
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "id": "fc-partial",
                    "call_id": "call-partial",
                    "name": "search",
                },
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc-partial",
                "delta": '{"query":"partial"}',
            },
        ]
    )
    _patch_codex_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        ),
    )
    provider = OpenAICodexProvider(auth_path=str(auth))

    events = _collect(
        provider,
        tools=[],
        cfg=ChatConfig(candidate_output_mode="inert_artifact"),
    )

    assert not any(isinstance(event, TextDeltaEvent) for event in events)
    assert not any(
        isinstance(event, (ToolUseStartEvent, ToolUseDeltaEvent, ToolUseEndEvent))
        for event in events
    )
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert any(
        isinstance(event, ErrorEvent) and event.code == "incomplete_stream"
        for event in events
    )


def test_reasoning_deltas_reach_done_event(tmp_path: Path, monkeypatch) -> None:
    auth = _write_auth(tmp_path / "auth.json")
    body = _sse(
        [
            {
                "type": "response.reasoning_summary_text.delta",
                "delta": "think ",
                "summary_index": 0,
            },
            {"type": "response.reasoning_text.delta", "delta": "hard", "content_index": 0},
            {"type": "response.output_text.delta", "delta": "hi"},
            {"type": "response.completed", "response": {"id": "r", "usage": {}}},
        ]
    )
    _patch_codex_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        ),
    )
    provider = OpenAICodexProvider(auth_path=str(auth))
    events = _collect(provider)
    assert [e.text for e in events if isinstance(e, ReasoningDeltaEvent)] == ["think ", "hard"]
    (done,) = [e for e in events if isinstance(e, DoneEvent)]
    assert done.reasoning_content == "think hard"


def test_401_refreshes_and_retries_once(tmp_path: Path, monkeypatch) -> None:
    auth = _write_auth(tmp_path / "auth.json")
    calls = {"stream": 0, "refresh": 0}

    # One handler for both endpoints: the provider and the auth module share
    # the global httpx module, so separate transports would fight over it.
    def handler(request: httpx.Request) -> httpx.Response:
        if "auth.openai.com" in str(request.url):
            calls["refresh"] += 1
            return httpx.Response(200, json={"access_token": "tok-new"})
        calls["stream"] += 1
        if request.headers.get("authorization") == "Bearer tok-access":
            return httpx.Response(401, json={"error": {"message": "expired"}})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                [
                    {"type": "response.output_text.delta", "delta": "ok"},
                    {"type": "response.completed", "response": {"id": "r", "usage": {}}},
                ]
            ),
        )

    _patch_codex_transport(monkeypatch, handler)
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(
        "opensquilla.provider.codex_auth.httpx.AsyncClient",
        lambda *a, **kw: real(*a, **{**kw, "transport": transport}),
    )

    provider = OpenAICodexProvider(auth_path=str(auth))
    events = _collect(provider)
    assert calls["stream"] == 2
    assert calls["refresh"] == 1
    assert any(isinstance(e, TextDeltaEvent) and e.text == "ok" for e in events)
    assert any(isinstance(e, DoneEvent) for e in events)
    assert json.loads(auth.read_text())["tokens"]["access_token"] == "tok-new"


def test_response_failed_yields_error_event(tmp_path: Path, monkeypatch) -> None:
    auth = _write_auth(tmp_path / "auth.json")
    body = _sse(
        [
            {
                "type": "response.failed",
                "response": {"error": {"code": "usage_limit_reached", "message": "limit"}},
            }
        ]
    )
    _patch_codex_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        ),
    )
    provider = OpenAICodexProvider(auth_path=str(auth))
    events = _collect(provider)
    (error,) = [e for e in events if isinstance(e, ErrorEvent)]
    assert error.code == "usage_limit_reached"
    assert not any(isinstance(e, DoneEvent) for e in events)


def test_truncated_stream_does_not_commit_tool_or_done(tmp_path: Path, monkeypatch) -> None:
    auth = _write_auth(tmp_path / "auth.json")
    body = _sse(
        [
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "id": "fc-1",
                    "call_id": "call-1",
                    "name": "search",
                },
            },
            {"type": "response.function_call_arguments.delta", "item_id": "fc-1", "delta": "{}"},
            # stream drops before output_item.done / response.completed
        ]
    )
    _patch_codex_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        ),
    )
    provider = OpenAICodexProvider(auth_path=str(auth))
    events = _collect(provider, tools=[_SEARCH_TOOL])
    assert not any(isinstance(e, ToolUseEndEvent) for e in events)
    assert not any(isinstance(e, DoneEvent) for e in events)
    assert any(
        isinstance(e, ErrorEvent) and e.code == "incomplete_stream"
        for e in events
    )


def test_missing_credentials_is_auth_error_event(tmp_path: Path) -> None:
    provider = OpenAICodexProvider(auth_path=str(tmp_path / "missing.json"))
    events = _collect(provider)
    (error,) = [e for e in events if isinstance(e, ErrorEvent)]
    assert error.code == "401"
    assert "codex login" in error.message


# ---------------------------------------------------------------------------
# Registry / selector wiring
# ---------------------------------------------------------------------------


def test_registry_and_selector_wire_openai_codex() -> None:
    spec = get_provider_spec("openai_codex")
    assert spec.backend == "openai_codex"
    assert spec.runtime_supported is True
    assert spec.requires_api_key() is False  # OAuth, not an API key field
    assert spec.requires_base_url() is False
    provider = build_provider("openai_codex", "gpt-5.2-codex")
    assert isinstance(provider, OpenAICodexProvider)
