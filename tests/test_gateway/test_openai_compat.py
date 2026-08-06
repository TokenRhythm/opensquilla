"""OpenAI-compatible relay (issues #978 / #979) — auth, routing, wire format."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.config import (
    AuthConfig,
    GatewayConfig,
    LlmProviderProfile,
    OpenAICompatConfig,
    SquillaRouterConfig,
)
from opensquilla.provider.types import (
    DoneEvent,
    ErrorEvent,
    TextDeltaEvent,
    ToolDefinition,
    ToolUseEndEvent,
)


class _FakeProvider:
    """Deterministic provider double capturing the relay's call arguments."""

    def __init__(self, events=None) -> None:
        self._events = events or []
        self.captured: dict = {}

    async def chat(self, messages, tools=None, config=None):
        self.captured = {"messages": messages, "tools": tools, "config": config}
        for event in self._events:
            yield event


def _install_fake_provider(monkeypatch, fake: _FakeProvider) -> None:
    import opensquilla.gateway.openai_compat as oc

    monkeypatch.setattr(oc, "build_provider", lambda *args, **kwargs: fake)


def _app(**config_kwargs) -> TestClient:
    # Relay is disabled by default; enable it for tests.  Loopback client so
    # the default auth.mode=none passes; remote-peer tests pass their own
    # `client=` explicitly.
    config_kwargs.setdefault("openai_compat", OpenAICompatConfig(enabled=True))
    return TestClient(
        create_gateway_app(GatewayConfig(**config_kwargs)), client=("127.0.0.1", 50000)
    )


@pytest.fixture(autouse=True)
def _default_model_env(monkeypatch):
    # Pin the runtime LLM explicitly: GatewayConfig treats an env-set
    # api_key WITHOUT an explicit provider as legacy openrouter intent and
    # backfills provider/model, which would break every routing test.
    monkeypatch.setenv("OPENSQUILLA_LLM_PROVIDER", "tokenrhythm")
    monkeypatch.setenv("OPENSQUILLA_LLM_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "test-key")
    yield


def test_relay_returns_openai_models_list() -> None:
    with _app() as client:
        response = client.get("/v1/models")
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    ids = [row["id"] for row in payload["data"]]
    assert "deepseek-v4-pro" in ids  # runtime default model
    assert all(row["object"] == "model" for row in payload["data"])


def test_relay_disabled_when_configured_off() -> None:
    with _app(openai_compat=OpenAICompatConfig(enabled=False)) as client:
        response = client.get("/v1/models")
    assert response.status_code == 404


def test_unknown_model_returns_openai_404() -> None:
    with _app() as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "does-not-exist", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "model_not_found"
    assert "does-not-exist" in error["message"]


def test_non_loopback_peer_denied_in_default_auth_mode() -> None:
    # auth.mode=none (default): the relay's own loopback guard is the only
    # boundary, mirroring the gateway's default bind scope.
    app = create_gateway_app(
        GatewayConfig(openai_compat=OpenAICompatConfig(enabled=True))
    )
    with TestClient(app, client=("10.0.0.9", 4321)) as client:
        response = client.get("/v1/models")
    assert response.status_code == 403
    assert "api_key_required" in response.json()["error"]["code"]


def test_token_mode_enforced_by_global_middleware() -> None:
    # The relay implements no auth of its own: /v1/* is control-plane, so
    # AuthMiddleware protects GET /v1/models as well as POST (the GET case
    # would otherwise be treated as a root-mounted UI asset request).
    app = create_gateway_app(
        GatewayConfig(
            openai_compat=OpenAICompatConfig(enabled=True),
            auth=AuthConfig(mode="token", token="secret-123"),
        )
    )
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.get("/v1/models").status_code == 401
        assert (
            client.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code == 401
        )
        assert client.post(
            "/v1/chat/completions",
            json={"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "hi"}]},
        ).status_code == 401
        ok = client.get("/v1/models", headers={"Authorization": "Bearer secret-123"})
        assert ok.status_code == 200


def test_token_mode_allows_remote_peer() -> None:
    app = create_gateway_app(
        GatewayConfig(
            openai_compat=OpenAICompatConfig(enabled=True),
            auth=AuthConfig(mode="token", token="secret-123"),
        )
    )
    with TestClient(app, client=("10.0.0.9", 4321)) as client:
        response = client.get("/v1/models", headers={"Authorization": "Bearer secret-123"})
    assert response.status_code == 200


def test_trusted_proxy_mode_enforced_by_middleware() -> None:
    # Same-host reverse-proxy deployments use the existing trusted-proxy
    # auth mode (X-Forwarded-For validation) — the maintainer-flagged
    # "loopback behind a proxy" case gets the stock answer, no relay code.
    app = create_gateway_app(
        GatewayConfig(
            openai_compat=OpenAICompatConfig(enabled=True),
            auth=AuthConfig(mode="trusted-proxy", trusted_proxy="10.0.0.5"),
        )
    )
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.get("/v1/models").status_code == 401
        ok = client.get("/v1/models", headers={"X-Forwarded-For": "10.0.0.5"})
        assert ok.status_code == 200


def test_cross_origin_browser_request_rejected() -> None:
    with _app() as client:
        response = client.get("/v1/models", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_non_streaming_completion_relays_and_converts(monkeypatch) -> None:
    fake = _FakeProvider(
        [
            TextDeltaEvent(text="Hello "),
            TextDeltaEvent(text="world"),
            DoneEvent(
                stop_reason="end_turn",
                input_tokens=11,
                output_tokens=2,
                model="deepseek-v4-pro",
            ),
        ]
    )
    _install_fake_provider(monkeypatch, fake)

    with _app() as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "deepseek-v4-pro",
                "messages": [
                    {"role": "system", "content": "Be terse"},
                    {"role": "user", "content": "hi"},
                ],
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "chat.completion"
    assert payload["model"] == "deepseek-v4-pro"
    assert payload["choices"][0]["message"]["content"] == "Hello world"
    assert payload["choices"][0]["finish_reason"] == "stop"
    assert payload["usage"] == {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13}

    # The relay must have passed the converted internal contract to the provider.
    assert [m.role for m in fake.captured["messages"]] == ["user"]
    assert fake.captured["config"].system == "Be terse"


def test_streaming_completion_emits_openai_sse(monkeypatch) -> None:
    fake = _FakeProvider(
        [
            TextDeltaEvent(text="part1"),
            TextDeltaEvent(text="part2"),
            DoneEvent(
                stop_reason="end_turn",
                input_tokens=5,
                output_tokens=7,
                model="deepseek-v4-pro",
            ),
        ]
    )
    _install_fake_provider(monkeypatch, fake)

    with _app() as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    lines = [line for line in response.text.splitlines() if line.startswith("data: ")]
    chunks = [json.loads(line[6:]) for line in lines if line[6:] != "[DONE]"]
    assert lines[-1] == "data: [DONE]"
    assert chunks[0]["object"] == "chat.completion.chunk"
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    deltas = [chunk["choices"][0]["delta"] for chunk in chunks if chunk["choices"][0]["delta"]]
    assert deltas[1] == {"content": "part1"}
    assert deltas[2] == {"content": "part2"}
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_tools_forwarded_and_tool_calls_serialized(monkeypatch) -> None:
    fake = _FakeProvider(
        [
            ToolUseEndEvent(
                tool_use_id="call_1",
                tool_name="get_weather",
                arguments={"city": "Paris"},
            ),
            DoneEvent(
                stop_reason="tool_use",
                input_tokens=9,
                output_tokens=3,
                model="deepseek-v4-pro",
            ),
        ]
    )
    _install_fake_provider(monkeypatch, fake)

    with _app() as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "description": "Get weather",
                            "parameters": {
                                "type": "object",
                                "properties": {"city": {"type": "string"}},
                                "required": ["city"],
                            },
                        },
                    }
                ],
            },
        )
    assert response.status_code == 200
    payload = response.json()
    tool_calls = payload["choices"][0]["message"]["tool_calls"]
    assert tool_calls == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
        }
    ]
    assert payload["choices"][0]["finish_reason"] == "tool_calls"

    tools = fake.captured["tools"]
    assert isinstance(tools, list) and len(tools) == 1
    assert isinstance(tools[0], ToolDefinition)
    assert tools[0].name == "get_weather"
    assert tools[0].input_schema.properties == {"city": {"type": "string"}}


def test_tier_model_routes_to_its_provider(monkeypatch) -> None:
    captured: dict = {}

    def _spy_provider(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProvider(
            [TextDeltaEvent(text="ok"), DoneEvent(stop_reason="end_turn", model="kimi-k2.7-code")]
        )

    import opensquilla.gateway.openai_compat as oc

    monkeypatch.setattr(oc, "build_provider", _spy_provider)

    config = GatewayConfig(
        openai_compat=OpenAICompatConfig(enabled=True),
        squilla_router=SquillaRouterConfig(
            tiers={
                "c2": {"provider": "tokenrhythm", "model": "kimi-k2.7-code"},
            }
        ),
        llm_profiles={
            "tokenrhythm": LlmProviderProfile(
                api_key="sk-tier",
                base_url="https://tokenrhythm.studio/v1",
            )
        },
    )
    with TestClient(create_gateway_app(config), client=("127.0.0.1", 50000)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "kimi-k2.7-code",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 200
    assert captured["args"][0] == "tokenrhythm"
    assert captured["args"][1] == "kimi-k2.7-code"
    assert captured["args"][2] == "sk-tier"
    assert captured["args"][3] == "https://tokenrhythm.studio/v1"


def test_cross_provider_tier_falls_back_to_registry_env_key(monkeypatch) -> None:
    """A tier naming a non-runtime provider resolves via the registry env key
    even when no ``llm_profiles`` entry exists (live-gateway gap)."""
    captured: dict = {}

    def _spy_provider(*args, **kwargs):
        captured["args"] = args
        return _FakeProvider(
            [
                TextDeltaEvent(text="ok"),
                DoneEvent(stop_reason="end_turn", model="deepseek/deepseek-v4-pro"),
            ]
        )

    import opensquilla.gateway.openai_compat as oc

    monkeypatch.setattr(oc, "build_provider", _spy_provider)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-env")

    config = GatewayConfig(
        openai_compat=OpenAICompatConfig(enabled=True),
        squilla_router=SquillaRouterConfig(
            tiers={
                "c1": {"provider": "deepseek", "model": "deepseek/deepseek-v4-pro"},
            }
        ),
    )
    with TestClient(create_gateway_app(config), client=("127.0.0.1", 50000)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "deepseek/deepseek-v4-pro",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 200
    assert captured["args"][0] == "deepseek"
    assert captured["args"][2] == "sk-deepseek-env"


def test_provider_error_maps_to_openai_envelope(monkeypatch) -> None:
    fake = _FakeProvider([ErrorEvent(message="upstream exploded", code="timeout")])
    _install_fake_provider(monkeypatch, fake)

    with _app() as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 502
    error = response.json()["error"]
    assert "upstream exploded" in error["message"]
    assert error["code"] == "timeout"


def test_invalid_max_tokens_returns_400_not_502(monkeypatch) -> None:
    # Non-numeric max_tokens is a client error, not a provider failure.
    fake = _FakeProvider([DoneEvent(stop_reason="end_turn", model="deepseek-v4-pro")])
    _install_fake_provider(monkeypatch, fake)

    with _app() as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": "not-a-number",
            },
        )
    assert response.status_code == 400
    assert "max_tokens" in response.json()["error"]["message"]
    # Provider must never have been called.
    assert not fake.captured


def test_invalid_timeout_returns_400(monkeypatch) -> None:
    fake = _FakeProvider([DoneEvent(stop_reason="end_turn", model="deepseek-v4-pro")])
    _install_fake_provider(monkeypatch, fake)

    with _app() as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": "hi"}],
                "timeout": [1, 2, 3],
            },
        )
    assert response.status_code == 400
    assert not fake.captured


def test_missing_messages_returns_400_not_502(monkeypatch) -> None:
    # A request without `messages` is a client error — must be 400, never
    # crash into the generic 502 provider-failure envelope.
    fake = _FakeProvider([DoneEvent(stop_reason="end_turn", model="deepseek-v4-pro")])
    _install_fake_provider(monkeypatch, fake)

    with _app() as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "deepseek-v4-pro"},
        )
    assert response.status_code == 400
    assert not fake.captured
