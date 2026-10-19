"""Automatic naming preserves the relay's reasoning behavior and usage receipt."""

from __future__ import annotations

import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from opensquilla import token_estimation
from opensquilla.engine.types import ThinkingLevel
from opensquilla.gateway import session_event_publisher
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink
from opensquilla.provider import model_catalog
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import ChatConfig, Message, ModelCapabilities
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionNode
from opensquilla.session.naming import generate_session_title
from opensquilla.session.storage import SessionStorage

_MODEL = "qwen3.7-flash"
_BASE_URL = "https://tokenrhythm.studio/v1"
_TITLE = "Inspect queue latency"
_KEY = "agent:main:webchat:synthetic-naming-reasoning"
_REASONING_FIELDS = {"enable_thinking", "thinking", "reasoning", "reasoning_effort"}


@pytest.fixture(autouse=True)
def isolate_catalog_and_state(tmp_path, monkeypatch):
    monkeypatch.setattr(model_catalog, "_shared_catalog", model_catalog.ModelCatalog())
    monkeypatch.setattr(token_estimation, "_get_encoding", lambda: None)
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY", raising=False)


@pytest.mark.parametrize(("thinking", "thinking_level"), [
    (False, "off"),
    (False, "none"),
    (False, None),
    (True, ThinkingLevel.HIGH),
])
def test_qwen_requests_do_not_inject_reasoning_controls(thinking, thinking_level):
    provider = OpenAIProvider(
        api_key="public-dummy-naming-key",
        model=_MODEL,
        base_url=_BASE_URL,
        provider_kind="tokenrhythm",
    )
    payload = provider.project_final_request(
        [Message(role="user", content="Inspect queue latency")],
        config=ChatConfig(
            thinking=thinking,
            thinking_level=thinking_level,
            model_capabilities=ModelCapabilities(),
        ),
    ).payload

    assert _REASONING_FIELDS.isdisjoint(payload)


@pytest.mark.asyncio
async def test_automatic_naming_preserves_reasoning_and_finalizes_its_usage(tmp_path, monkeypatch):
    requests = []
    reasoning = "Review queue ordering. " * 200
    assert token_estimation.estimate_tokens(reasoning) > 2048

    async def respond(request):
        requests.append(request)
        payload = json.loads(request.content)
        assert request.url.host == "tokenrhythm.studio"
        assert request.url.path == "/v1/chat/completions"
        assert payload["model"] == _MODEL
        assert payload["max_tokens"] == 2048
        assert _REASONING_FIELDS.isdisjoint(payload)
        frames = [
            {
                "id": "synthetic-title-response", "model": _MODEL,
                "choices": [{
                    "index": 0, "delta": {"reasoning_content": reasoning},
                    "finish_reason": None,
                }],
            },
            {
                "id": "synthetic-title-response", "model": _MODEL,
                "choices": [{"index": 0, "delta": {"content": _TITLE}, "finish_reason": None}],
            },
            {
                "id": "synthetic-title-response", "model": _MODEL,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 12, "completion_tokens": 1292,
                    "completion_tokens_details": {"reasoning_tokens": 1282},
                },
            },
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
            + "data: [DONE]\n\n",
        )

    client_class = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(respond)
        kwargs["trust_env"] = False
        return client_class(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    emit = AsyncMock()
    monkeypatch.setattr(session_event_publisher, "emit_session_event", emit)
    storage = SessionStorage(str(tmp_path / "synthetic-naming-reasoning.db"))
    await storage.connect()
    try:
        await storage.upsert_session(SessionNode(
            session_key=_KEY, session_id="synthetic-naming-session", display_name="WebChat",
        ))
        manager = SessionManager(storage, inject_time_prefix=False)
        await manager.append_message(_KEY, "user", "Inspect queue latency")
        config = GatewayConfig()
        config.squilla_router.enabled = False
        config.llm_ensemble.enabled = False
        ctx = SimpleNamespace(
            config=config,
            session_manager=manager,
            provider_selector=ModelSelector(SelectorConfig(primary=ProviderConfig(
                provider="tokenrhythm", model=_MODEL,
                api_key="public-dummy-naming-key", base_url=_BASE_URL,
            ))),
            usage_event_sink=SessionUsageEventSink(storage, start_retry_delays=(), retry_delays=()),
        )

        # Windows initializes event-loop socketpairs through loopback connections.
        # Guard only the naming operation, leaving loop setup and cleanup intact.
        with monkeypatch.context() as network_guard:
            def reject_network(*_args, **_kwargs):
                raise AssertionError("Naming reasoning tests must use MockTransport")

            network_guard.setattr(socket.socket, "connect", reject_network)
            network_guard.setattr(socket, "create_connection", reject_network)
            await generate_session_title(ctx, _KEY, "Inspect queue latency")

        assert len(requests) == 1
        session = await storage.get_session(_KEY)
        assert session is not None
        assert session.derived_title == _TITLE
        emit.assert_awaited_once()
        assert emit.await_args.args[2] == "sessions.changed"
        assert emit.await_args.args[3]["reason"] == "auto_titled"
        async with storage.conn.execute(
            "SELECT provider, model, status, output_tokens, reasoning_tokens, run_kind "
            "FROM usage_events"
        ) as cursor:
            assert [tuple(row) for row in await cursor.fetchall()] == [
                ("tokenrhythm", _MODEL, "finalized", 1292, 1282, "session_naming"),
            ]
    finally:
        await storage.close()
