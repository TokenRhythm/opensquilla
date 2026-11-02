"""Compatibility recovery stays inside the model policy's physical-call budget."""
from __future__ import annotations

import json
from copy import deepcopy

import httpx
import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.engine.subagent import SubagentSpec
from opensquilla.engine.types import ErrorEvent, TextDeltaEvent
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.openai_codex import OpenAICodexProvider
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import ChatConfig


class _TimeoutBody(httpx.AsyncByteStream):
    def __init__(self, prefix=b""):
        self.prefix = prefix

    async def __aiter__(self):
        if self.prefix:
            yield self.prefix
        raise httpx.ReadTimeout("offline read timeout")


def _success():
    return httpx.Response(200, json={
        "choices": [{"message": {"content": "recovered"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1},
    })


def _agent(monkeypatch, tmp_path, handler, *, provider="moonshot", retries=3, child=False):
    auth_path = tmp_path / "codex-auth.json"
    auth_path.write_text(json.dumps({"tokens": {
        "access_token": "synthetic-old", "refresh_token": "synthetic-refresh",
        "account_id": "synthetic-account",
    }}), encoding="utf-8")
    real_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)

    def build(config):
        if config.provider == "openai_codex":
            return OpenAICodexProvider(model=config.model, auth_path=str(auth_path))
        return OpenAIProvider(
            api_key="synthetic-key", model=config.model, provider_kind=config.provider,
            base_url="https://offline.invalid/v1",
        )

    monkeypatch.setattr("opensquilla.provider.selector._build_provider", build)
    model = "gpt-5.2-codex" if provider == "openai_codex" else "kimi-for-coding"
    selector = ModelSelector(SelectorConfig(primary=ProviderConfig(
        provider=provider, model=model, api_key="synthetic-key",
    )))
    metadata = {
        "router_tier_policy": "model", "routing_applied": True, "routed_tier": "c1",
        "router_model_state": {"tier": "c1"},
        "router_model_quality_retry_callback": lambda **_: pytest.fail("parent quality callback"),
    }
    wrapper = _SelectorFallbackProvider(selector.resolve(), selector, metadata)
    parent = Agent(wrapper, AgentConfig(
        provider_id=provider, model_id=model, metadata=metadata,
        max_provider_retries=retries, retry_base_backoff_ms=0, retry_max_backoff_ms=0,
    ))
    agent = (
        parent._make_child_agent(SubagentSpec(task="Reply briefly"), depth=1) if child else parent
    )
    assert agent.provider._config_for_active_leg(ChatConfig()).physical_attempt_limit == 1
    if child:
        assert agent.config.metadata == {}
        assert agent.provider._turn_metadata == {}
        assert agent.provider.coordinates_physical_retries
        assert parent.config.metadata is metadata
    return agent


@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize("failure", ["timeout", "empty_eof", "empty_stop"])
async def test_budgeted_nonstream_recovery(monkeypatch, tmp_path, child, failure):
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        assert "compatibility_recovery" not in payload
        assert "physical_attempt_limit" not in payload
        calls.append(payload["stream"])
        if not payload["stream"]:
            return _success()
        if failure == "timeout":
            return httpx.Response(200, stream=_TimeoutBody())
        content = b"" if failure == "empty_eof" else (
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        )
        return httpx.Response(200, content=content)

    agent = _agent(
        monkeypatch, tmp_path, handler, child=child,
        provider="openrouter" if failure == "timeout" else "moonshot",
    )
    events = [event async for event in agent.run_turn("Reply briefly")]
    assert calls == [True, False]
    assert "".join(e.text for e in events if isinstance(e, TextDeltaEvent)) == "recovered"
    assert not any(isinstance(e, ErrorEvent) for e in events)


@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize("outcome", ["success", "refresh_failure", "still_unauthorized"])
async def test_budgeted_codex_refresh(monkeypatch, tmp_path, child, outcome):
    calls = []

    def handler(request):
        if request.url.host == "auth.openai.com":
            calls.append("refresh")
            if outcome == "refresh_failure":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "synthetic-new"})
        token = request.headers["authorization"]
        calls.append("new" if token == "Bearer synthetic-new" else "old")
        if calls[-1] == "old" or outcome == "still_unauthorized":
            return httpx.Response(401, json={"error": {"message": "expired"}})
        return httpx.Response(200, content=(
            b'data: {"type":"response.output_text.delta","delta":"recovered"}\n\n'
            b'data: {"type":"response.completed","response":{"id":"r","usage":{}}}\n\n'
        ))

    agent = _agent(monkeypatch, tmp_path, handler, provider="openai_codex", child=child)
    events = [event async for event in agent.run_turn("Reply briefly")]
    assert calls == (["old", "refresh"] if outcome == "refresh_failure"
                     else ["old", "refresh", "new"])
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    if outcome == "success":
        assert not errors
        assert "".join(e.text for e in events if isinstance(e, TextDeltaEvent)) == "recovered"
    else:
        assert len(errors) == 1 and errors[0].failure_kind == "auth_invalid"


@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize("provider", ["moonshot", "openai_codex"])
async def test_zero_budget_forbids_protocol_retry_and_token_refresh(
    monkeypatch, tmp_path, child, provider,
):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        assert request.url.host != "auth.openai.com"
        return httpx.Response(401, json={"error": "expired"}) if provider == "openai_codex" else (
            httpx.Response(200, content=b"")
        )

    agent = _agent(monkeypatch, tmp_path, handler, provider=provider, retries=0, child=child)
    events = [event async for event in agent.run_turn("Reply briefly")]
    assert len(calls) == 1
    assert any(isinstance(e, ErrorEvent) for e in events)


@pytest.mark.parametrize("child", [False, True])
async def test_nonstream_transport_failure_cannot_escape_shared_budget(
    monkeypatch, tmp_path, child,
):
    calls = []

    def handler(request):
        streaming = json.loads(request.content)["stream"]
        calls.append(streaming)
        if streaming:
            return httpx.Response(200, content=b"")
        raise httpx.ConnectError("offline connection failure", request=request)

    async def no_wait(_delay):
        pass

    monkeypatch.setattr("opensquilla.engine.agent.sleep_before_retry", no_wait)
    agent = _agent(monkeypatch, tmp_path, handler, child=child)
    events = [event async for event in agent.run_turn("Reply briefly")]
    assert calls == [True, False, False, False]
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1 and errors[0].failure_kind == "transport_transient"


@pytest.mark.parametrize("field", ["content", "reasoning_content"])
async def test_nonstream_recovery_does_not_replay_visible_output(monkeypatch, tmp_path, field):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["stream"])
        prefix = ("data: " + json.dumps({"choices": [{"delta": {field: "partial"}}]}) + "\n\n")
        return httpx.Response(200, stream=_TimeoutBody(prefix.encode()))

    agent = _agent(monkeypatch, tmp_path, handler)
    events = [event async for event in agent.run_turn("Reply briefly")]
    assert calls == [True]
    assert any(isinstance(e, ErrorEvent) for e in events)


async def test_compatibility_recovery_shares_the_turn_budget_across_tool_calls(
    monkeypatch, tmp_path,
):
    from opensquilla.engine.types import ToolResult
    from opensquilla.provider.types import ToolDefinition, ToolInputSchema

    calls = []
    executed = []

    def handler(request):
        streaming = json.loads(request.content)["stream"]
        calls.append(streaming)
        if streaming:
            return httpx.Response(200, content=b"")
        return httpx.Response(200, json={"choices": [{
            "message": {"tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "ping", "arguments": "{}"},
            }]}, "finish_reason": "tool_calls",
        }]})

    async def handle_tool(call):
        executed.append(call.tool_use_id)
        return ToolResult(
            tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="ok",
        )

    base = _agent(monkeypatch, tmp_path, handler, retries=1)
    agent = Agent(base.provider, base.config, tool_handler=handle_tool, tool_definitions=[
        ToolDefinition(name="ping", description="Ping", input_schema=ToolInputSchema()),
    ])
    events = [event async for event in agent.run_turn("Use ping then answer")]
    assert calls == [True, False, True]
    assert executed == ["call-1"]
    assert not [e for e in events if e.kind == "tool_result" and e.is_error]
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1 and errors[0].code == "provider_retry_budget_exhausted"


async def test_child_budget_is_independent_without_parent_route_state(monkeypatch, tmp_path):
    calls = []

    def handler(request):
        streaming = json.loads(request.content)["stream"]
        calls.append(streaming)
        return httpx.Response(200, content=b"") if streaming else _success()

    parent = _agent(monkeypatch, tmp_path, handler, retries=1)
    parent_events = [event async for event in parent.run_turn("Parent task")]
    parent_state = deepcopy(parent.config.metadata)
    child = parent._make_child_agent(SubagentSpec(task="Child task"), depth=1)
    child_events = [event async for event in child.run_turn("Child task")]
    assert calls == [True, False, True, False]
    assert not any(isinstance(e, ErrorEvent) for e in parent_events + child_events)
    assert child.config.metadata == {} and child.provider._turn_metadata != parent_state
    assert child.config.max_provider_retries == 1
    assert parent.config.metadata == parent_state


async def test_refreshed_codex_stream_replay_does_not_refresh_again(monkeypatch, tmp_path):
    from opensquilla.contracts.turn_execution import (
        SurfaceCapabilities,
        TurnExecutionContext,
        TurnIdentity,
    )
    from opensquilla.engine.types import AnswerGenerationResetEvent

    calls = []

    def handler(request):
        if request.url.host == "auth.openai.com":
            calls.append("refresh")
            return httpx.Response(200, json={"access_token": "synthetic-new"})
        token = request.headers["authorization"]
        calls.append("new" if token == "Bearer synthetic-new" else "old")
        if calls[-1] == "old":
            return httpx.Response(401, json={"error": "expired"})
        if calls.count("new") == 1:
            return httpx.Response(200, stream=_TimeoutBody(
                b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n',
            ))
        return httpx.Response(200, content=(
            b'data: {"type":"response.output_text.delta","delta":"recovered"}\n\n'
            b'data: {"type":"response.completed","response":{"id":"r","usage":{}}}\n\n'
        ))

    base = _agent(monkeypatch, tmp_path, handler, provider="openai_codex")
    context = TurnExecutionContext.create(
        TurnIdentity("retry-turn", "retry-answer", "agent:main:retry"),
        surface=SurfaceCapabilities(
            supports_streaming=True, supports_edit=True,
            supports_generation_reset=True, supports_provider_call_reset=True,
        ),
    )
    agent = Agent(base.provider, base.config, execution_context=context)
    events = [event async for event in agent.run_turn("Reply briefly")]
    assert calls == ["old", "refresh", "new", "new"]
    assert not any(isinstance(e, ErrorEvent) for e in events)
    assert sum(isinstance(e, AnswerGenerationResetEvent) for e in events) == 1


async def test_child_preserves_physical_credential_failure_attribution(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from opensquilla.provider.failures import ProviderFailureKind

    reported = []

    def report_failure(provider, session, kind, **_kwargs):
        reported.append((provider, session, kind))

    monkeypatch.setattr(
        "opensquilla.gateway.llm_runtime.profile_credential_pools",
        lambda: SimpleNamespace(report_failure=report_failure),
    )
    parent = _agent(
        monkeypatch, tmp_path, lambda _: httpx.Response(401, json={"error": "invalid key"}),
    )
    parent.config.metadata.update(
        credential_pool={"provider": "moonshot", "session_key": "parent-pool-session"},
        routed_provider_applied="moonshot",
    )
    child = parent._make_child_agent(SubagentSpec(task="Child task"), depth=1)
    assert child.provider._turn_metadata["credential_pool"] == (
        parent.config.metadata["credential_pool"]
    )
    assert child.provider._turn_metadata["credential_pool"] is not (
        parent.config.metadata["credential_pool"]
    )
    assert "router_model_state" not in child.provider._turn_metadata
    assert "router_model_quality_retry_callback" not in child.provider._turn_metadata
    events = [event async for event in child.run_turn("Child task")]
    assert any(isinstance(e, ErrorEvent) for e in events)
    assert reported == [("moonshot", "parent-pool-session", ProviderFailureKind.AUTH_INVALID)]
