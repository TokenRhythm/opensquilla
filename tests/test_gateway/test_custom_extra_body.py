from __future__ import annotations

import asyncio
import json
import tomllib
from datetime import datetime
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from opensquilla.gateway.config import GatewayConfig, LlmProviderConfig
from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config
from opensquilla.gateway.provider_runtime import resolve_provider_selector_config
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_config import (
    _handle_config_apply,
    _handle_config_patch,
    _handle_config_reload,
    _handle_config_schema,
    _handle_config_schema_lookup,
    _handle_config_set,
)
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.selector import (
    ModelSelector,
    ProviderConfig,
    SelectorConfig,
    _provider_config_identity,
    build_provider_from_config,
)
from opensquilla.provider.types import ChatConfig, Message


def _custom_config(*, config_path: str | None = None) -> GatewayConfig:
    return GatewayConfig(
        config_path=config_path,
        llm={
            "provider": "custom",
            "model": "local-model",
            "base_url": "http://127.0.0.1:8000/v1",
            "extra_body": {
                "top_k": 40,
                "min_p": 0.05,
                "chat_template_kwargs": {"enable_thinking": True},
            },
        },
    )


def _ctx(config: GatewayConfig) -> RpcContext:
    return RpcContext(conn_id="test", config=config)


def _sse_body() -> bytes:
    chunks = [
        {
            "model": "local-model",
            "choices": [{"delta": {"content": "ok"}, "finish_reason": None}],
        },
        {
            "model": "local-model",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
    ]
    return b"".join(
        f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks
    ) + b"data: [DONE]\n\n"


def test_custom_extra_body_loads_from_toml_and_stays_out_of_public_config(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[llm]
provider = "custom"
model = "local-model"
base_url = "http://127.0.0.1:8000/v1"

[llm.extra_body]
top_k = 40
min_p = 0.05

[llm.extra_body.chat_template_kwargs]
enable_thinking = true
""".strip(),
        encoding="utf-8",
    )

    config = GatewayConfig.load(str(path))

    assert config.llm.extra_body == {
        "top_k": 40,
        "min_p": 0.05,
        "chat_template_kwargs": {"enable_thinking": True},
    }
    assert "extra_body" not in config.to_public_dict()["llm"]
    assert config.to_toml_dict()["llm"]["extra_body"]["top_k"] == 40
    assert "extra_body" not in GatewayConfig().to_toml_dict()["llm"]


@pytest.mark.parametrize(
    ("provider", "extra_body", "error"),
    [
        ("openai", {"top_k": 1}, "only when provider='custom'"),
        ("custom", {"Temperature": 0.1}, "reserved by OpenSquilla"),
        ("custom", {"top_k": float("nan")}, "finite JSON number"),
        ("custom", {"vendor": datetime(2026, 1, 1)}, "JSON-compatible"),
        ("custom", {" ": 1}, "non-empty strings"),
    ],
)
def test_extra_body_validation_rejects_unsafe_values(
    provider: str,
    extra_body: dict[str, Any],
    error: str,
) -> None:
    with pytest.raises(ValidationError, match=error):
        LlmProviderConfig(provider=provider, extra_body=extra_body)


def test_extra_body_environment_inputs_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_LLM_EXTRA_BODY", '{"top_k": 99}')
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_LLM__EXTRA_BODY", '{"top_k": 98}')

    assert LlmProviderConfig(provider="custom").extra_body == {}
    assert GatewayConfig().llm.extra_body == {}


def test_runtime_and_selector_copy_extra_body_and_use_order_independent_identity() -> None:
    config = _custom_config()
    runtime = resolve_llm_runtime_config(config)
    config.llm.extra_body["chat_template_kwargs"]["enable_thinking"] = False

    assert runtime.extra_body["chat_template_kwargs"]["enable_thinking"] is True

    resolved = resolve_provider_selector_config(_custom_config())
    assert resolved is not None
    built = build_provider_from_config(resolved)
    built_projection = built.project_final_request(
        [Message(role="user", content="hello")],
        config=ChatConfig(max_tokens=64),
    )
    assert built_projection.payload["top_k"] == 40

    left = ProviderConfig(
        provider="custom",
        model="local-model",
        extra_body={"a": 1, "nested": {"b": 2}},
    )
    right = ProviderConfig(
        provider="custom",
        model="local-model",
        extra_body={"nested": {"b": 2}, "a": 1},
    )
    assert _provider_config_identity(left) == _provider_config_identity(right)

    selector = ModelSelector(SelectorConfig(primary=left))
    cloned = selector.clone()
    selector.current_config.extra_body["nested"]["b"] = 3
    assert cloned.current_config.extra_body["nested"]["b"] == 2


async def test_public_schema_and_lookup_hide_extra_body() -> None:
    config = _custom_config()
    response = await _handle_config_schema({}, _ctx(config))
    llm_properties = response["schema"]["$defs"]["LlmProviderConfig"]["properties"]

    assert "extra_body" not in llm_properties
    assert "extra_body" not in json.dumps(response)
    with pytest.raises(KeyError, match="Schema path not found"):
        await _handle_config_schema_lookup(
            {"path": "llm.extra_body"},
            _ctx(config),
        )


async def test_rpc_rejects_extra_body_writes_and_public_apply_preserves_it(tmp_path) -> None:
    path = tmp_path / "config.toml"
    config = _custom_config(config_path=str(path))

    with pytest.raises(ValueError, match="local-file-only"):
        await _handle_config_set(
            {"path": "llm.extra_body.top_k", "value": 20},
            _ctx(config),
        )
    with pytest.raises(ValueError, match="local-file-only"):
        await _handle_config_patch(
            {"patch": {"llm": {"extra_body": {"top_k": 20}}}},
            _ctx(config),
        )
    explicit_apply = config.to_public_dict()
    explicit_apply["llm"]["extra_body"] = {"top_k": 20}
    with pytest.raises(ValueError, match="local-file-only"):
        await _handle_config_apply({"config": explicit_apply}, _ctx(config))

    public_apply = config.to_public_dict()
    public_apply["naming"]["enabled"] = not config.naming.enabled
    await _handle_config_apply({"config": public_apply}, _ctx(config))

    assert config.llm.extra_body["top_k"] == 40
    assert tomllib.loads(path.read_text())["llm"]["extra_body"]["top_k"] == 40

    switch = config.to_public_dict()
    switch["llm"]["provider"] = "ollama"
    switch["llm"]["model"] = "local-model"
    switch["llm"]["base_url"] = "http://127.0.0.1:11434"
    await _handle_config_apply({"config": switch}, _ctx(config))

    assert config.llm.provider == "ollama"
    assert config.llm.extra_body == {}
    assert "extra_body" not in tomllib.loads(path.read_text())["llm"]


async def test_config_reload_applies_hand_edited_extra_body_to_live_selector(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[llm]
provider = "custom"
model = "local-model"
base_url = "http://127.0.0.1:8000/v1"

[llm.extra_body]
top_k = 20
""".strip(),
        encoding="utf-8",
    )
    config = _custom_config(config_path=str(path))

    class CapturingSelector:
        synced: ProviderConfig | None = None

        def sync_primary(self, provider_config: ProviderConfig) -> None:
            self.synced = provider_config

    selector = CapturingSelector()
    context = RpcContext(conn_id="test", config=config, provider_selector=selector)

    result = await _handle_config_reload(None, context)

    assert result["ok"] is True
    assert config.llm.extra_body == {"top_k": 20}
    assert selector.synced is not None
    assert selector.synced.extra_body == {"top_k": 20}


def test_custom_extra_body_is_in_projection_and_wire_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
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
    configured = {
        "top_k": 40,
        "min_p": 0.05,
        "repetition_penalty": 1.1,
        "chat_template_kwargs": {"enable_thinking": True},
    }
    provider = OpenAIProvider(
        api_key="synthetic-key",
        model="local-model",
        base_url="http://127.0.0.1:8000/v1",
        provider_kind="openai",
        provider_id="custom",
        extra_body=configured,
    )
    configured["chat_template_kwargs"]["enable_thinking"] = False
    messages = [Message(role="user", content="hello")]
    projection = provider.project_final_request(messages, config=ChatConfig(max_tokens=64))

    assert projection.payload["top_k"] == 40
    assert projection.payload["chat_template_kwargs"]["enable_thinking"] is True

    async def run() -> None:
        async for _ in provider.chat(messages, config=ChatConfig(max_tokens=64)):
            pass

    asyncio.run(run())

    assert captured["payload"]["top_k"] == 40
    assert captured["payload"]["min_p"] == 0.05
    assert captured["payload"]["repetition_penalty"] == 1.1
    assert captured["payload"]["chat_template_kwargs"] == {"enable_thinking": True}


def test_extra_body_contributes_to_request_proof_budget() -> None:
    provider = OpenAIProvider(
        api_key="synthetic-key",
        model="local-model",
        provider_kind="openai",
        provider_id="custom",
        extra_body={"vendor_blob": "x" * 5_000},
    )

    projection = provider.project_final_request(
        [Message(role="user", content="hello")],
        config=ChatConfig(max_tokens=64, provider_request_max_chars=500),
    )

    assert projection.fits is False
    assert projection.proof["wire_json_chars"] > 5_000


def test_programmatic_non_custom_provider_rejects_extra_body() -> None:
    with pytest.raises(ValueError, match="only for provider 'custom'"):
        OpenAIProvider(
            api_key="synthetic-key",
            model="gpt-test",
            provider_id="openai",
            extra_body={"top_k": 40},
        )
