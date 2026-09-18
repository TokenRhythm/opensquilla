"""Offline naming requests through real selectors, adapters and the usage ledger."""

from __future__ import annotations

import json
import socket
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from opensquilla.gateway import session_event_publisher
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink
from opensquilla.provider import model_catalog
from opensquilla.provider.protocol import provider_metadata
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import ProviderRequestCorrelation
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionNode
from opensquilla.session.naming import generate_session_title
from opensquilla.session.storage import SessionStorage

_KEY = "agent:main:webchat:synthetic-provider-naming"
_SESSION_ID = "synthetic-naming-session"
_TITLE = "Database connection pool"
_CHAT_MODEL = "synthetic-chat-model"
_TIER_MODEL = "synthetic-tier-model"
_NAMING_MODEL = "synthetic-naming-model"


@pytest.fixture
async def naming_runtime(tmp_path, monkeypatch):
    catalog = model_catalog.ModelCatalog()
    monkeypatch.setattr(model_catalog, "_shared_catalog", catalog)
    monkeypatch.delenv("OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY", raising=False)
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "state"))

    def reject_network(*_args, **_kwargs):
        raise AssertionError("Naming integration tests must only use MockTransport")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(socket, "create_connection", reject_network)
    emit = AsyncMock()
    monkeypatch.setattr(session_event_publisher, "emit_session_event", emit)
    storage = SessionStorage(str(tmp_path / "synthetic-naming.db"))
    await storage.connect()
    try:
        await storage.upsert_session(
            SessionNode(session_key=_KEY, session_id=_SESSION_ID, display_name="WebChat")
        )
        manager = SessionManager(storage, inject_time_prefix=False)
        await manager.append_message(_KEY, "user", "Inspect the database connection pool")
        yield SimpleNamespace(
            storage=storage, manager=manager, emit=emit, catalog=catalog,
        )
    finally:
        await storage.close()


def _context(runtime, *, provider="openrouter"):
    config = GatewayConfig()
    config.naming.enabled = True
    config.squilla_router.enabled = False
    config.squilla_router.rollout_phase = "observe"
    config.squilla_router.default_tier = "c1"
    config.squilla_router.tiers = {"c1": {"provider": provider, "model": _TIER_MODEL}}
    config.llm_ensemble.enabled = False
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider=provider,
                model=_CHAT_MODEL,
                api_key="public-dummy-naming-key",
                base_url=(
                    "https://api.tokenrhythm.studio/v1"
                    if provider == "tokenrhythm"
                    else "https://naming.example.test/v1"
                ),
            ),
        ),
    )
    sink = SessionUsageEventSink(runtime.storage, start_retry_delays=(), retry_delays=())
    return SimpleNamespace(
        config=config,
        session_manager=runtime.manager,
        provider_selector=selector,
        usage_event_sink=sink,
    )


def _anthropic_response(model: str, output_tokens: int) -> httpx.Response:
    frames = [
        ("message_start", {
            "type": "message_start",
            "message": {
                "id": "synthetic-naming-message", "type": "message", "role": "assistant",
                "model": model, "usage": {"input_tokens": 12, "output_tokens": 0},
            },
        }),
        ("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        }),
        ("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": _TITLE},
        }),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {
            "type": "message_delta", "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": output_tokens},
        }),
        ("message_stop", {"type": "message_stop"}),
    ]
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text="".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in frames),
    )


def _mock_http(monkeypatch, *, provider="openrouter", output_tokens=4):
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert payload["stream"] is True
        if provider == "anthropic":
            assert request.url.path == "/v1/messages"
            return _anthropic_response(payload["model"], output_tokens)
        assert request.url.path == "/v1/chat/completions"
        frames = [
            {
                "id": "synthetic-naming-response", "model": payload["model"],
                "choices": [{"index": 0, "delta": {"content": _TITLE}, "finish_reason": None}],
            },
            {
                "id": "synthetic-naming-response", "model": payload["model"],
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": output_tokens},
            },
        ]
        data = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            text=data + "data: [DONE]\n\n",
        )

    client_class = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(respond)
        kwargs["trust_env"] = False
        return client_class(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    return requests


async def _assert_result(runtime, *, provider, model, output_tokens=4, title=_TITLE):
    session = await runtime.storage.get_session(_KEY)
    assert session is not None
    assert session.derived_title == title
    if title:
        runtime.emit.assert_awaited_once()
        assert runtime.emit.await_args.args[2] == "sessions.changed"
        assert runtime.emit.await_args.args[3]["reason"] == "auto_titled"
    else:
        runtime.emit.assert_not_awaited()
    async with runtime.storage.conn.execute(
        "SELECT provider, model, status, input_tokens, output_tokens, session_id, run_kind "
        "FROM usage_events"
    ) as cursor:
        rows = [tuple(row) for row in await cursor.fetchall()]
    assert rows == [
        (provider, model, "finalized", 12, output_tokens, _SESSION_ID, "session_naming"),
    ]


@pytest.mark.asyncio
async def test_anthropic_naming_uses_native_messages_api_and_records_usage(
    naming_runtime, monkeypatch,
):
    ctx = _context(naming_runtime, provider="anthropic")
    requests = _mock_http(monkeypatch, provider="anthropic")

    await generate_session_title(ctx, _KEY, "Inspect the database connection pool")

    assert len(requests) == 1
    assert requests[0].headers["x-api-key"] == "public-dummy-naming-key"
    assert "authorization" not in requests[0].headers
    assert json.loads(requests[0].content)["model"] == _CHAT_MODEL
    await _assert_result(naming_runtime, provider="anthropic", model=_CHAT_MODEL)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selection", "expected_model"),
    [
        ("direct", _CHAT_MODEL),
        ("explicit_model", _NAMING_MODEL),
        ("explicit_tier", _TIER_MODEL),
        ("router_default", _TIER_MODEL),
        ("ensemble_default", _TIER_MODEL),
        ("session_override", "synthetic-session-model"),
    ],
)
async def test_naming_target_matches_http_and_ledger_without_mutating_selector(
    naming_runtime, monkeypatch, selection, expected_model,
):
    ctx = _context(naming_runtime)
    if selection == "explicit_model":
        ctx.config.naming.model = _NAMING_MODEL
        ctx.config.naming.tier = "c1"  # An explicit model takes precedence over its tier.
    elif selection == "explicit_tier":
        ctx.config.naming.tier = "c1"
    elif selection == "router_default":
        ctx.config.squilla_router.enabled = True
        ctx.config.squilla_router.rollout_phase = "full"
    elif selection == "ensemble_default":
        ctx.config.llm_ensemble.enabled = True
    elif selection == "session_override":
        await naming_runtime.manager.update(_KEY, model_override=expected_model)
    original_config = deepcopy(ctx.provider_selector.current_config)
    original_provider = ctx.provider_selector.resolve()
    requests = _mock_http(monkeypatch)

    await generate_session_title(ctx, _KEY, "Inspect the database connection pool")

    assert len(requests) == 1
    assert json.loads(requests[0].content)["model"] == expected_model
    assert requests[0].headers["authorization"] == "Bearer public-dummy-naming-key"
    await _assert_result(naming_runtime, provider="openrouter", model=expected_model)
    assert ctx.provider_selector.current_config == original_config
    assert provider_metadata(ctx.provider_selector.resolve()) == provider_metadata(
        original_provider,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "configured_cap", "output_tokens", "expected_budget", "expected_title"),
    [
        ("openrouter", None, 4, 512, _TITLE),
        ("tokenrhythm", None, 700, 1024, _TITLE),
        ("openrouter", 64, 4, 64, _TITLE),
        ("openrouter", 64, 65, 64, None),
    ],
)
async def test_naming_uses_resolved_output_budget_in_request_and_response(
    naming_runtime, monkeypatch, provider, configured_cap,
    output_tokens, expected_budget, expected_title,
):
    ctx = _context(naming_runtime, provider=provider)
    if configured_cap is not None:
        naming_runtime.catalog.set_user_overrides({
            f"{provider}/{_CHAT_MODEL}": {"max_output_tokens": configured_cap},
        })
    requests = _mock_http(monkeypatch, provider=provider, output_tokens=output_tokens)

    await generate_session_title(ctx, _KEY, "Inspect the database connection pool")

    assert len(requests) == 1
    assert json.loads(requests[0].content)["max_tokens"] == expected_budget
    # Rejecting a title must not discard a completed, billable provider receipt.
    await _assert_result(
        naming_runtime, provider=provider, model=_CHAT_MODEL,
        output_tokens=output_tokens, title=expected_title,
    )


@pytest.mark.asyncio
async def test_tokenrhythm_naming_preserves_turn_correlation(naming_runtime, monkeypatch):
    ctx = _context(naming_runtime, provider="tokenrhythm")
    requests = _mock_http(monkeypatch, provider="tokenrhythm")
    correlation = ProviderRequestCorrelation(
        session_id=_SESSION_ID, turn_id="synthetic-turn", execution_id="synthetic-execution",
        call_kind="auxiliary.naming",
    )

    await generate_session_title(
        ctx, _KEY, "Inspect the database connection pool",
        provider_request_correlation=correlation,
    )

    assert len(requests) == 1
    headers = requests[0].headers
    assert headers["x-opensquilla-session-id"] == _SESSION_ID
    assert headers["x-opensquilla-turn-id"] == "synthetic-turn"
    assert headers["x-opensquilla-execution-id"] == "synthetic-execution"
    assert headers["x-opensquilla-call-kind"] == "auxiliary.naming"
    await _assert_result(naming_runtime, provider="tokenrhythm", model=_CHAT_MODEL)


@pytest.mark.asyncio
async def test_naming_budget_uses_wire_kind_but_ledger_keeps_registry_identity(
    naming_runtime, monkeypatch,
):
    from opensquilla.provider import registry

    alias = "synthetic-tokenrhythm"
    monkeypatch.setitem(
        registry._PROVIDER_SPECS, alias,
        replace(registry.get_provider_spec("tokenrhythm"), provider_id=alias),
    )
    ctx = _context(naming_runtime, provider=alias)
    requests = _mock_http(monkeypatch, output_tokens=700)

    await generate_session_title(ctx, _KEY, "Inspect the database connection pool")

    assert len(requests) == 1
    assert json.loads(requests[0].content)["max_tokens"] == 1024
    await _assert_result(naming_runtime, provider=alias, model=_CHAT_MODEL, output_tokens=700)
