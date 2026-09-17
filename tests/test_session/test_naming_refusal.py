"""HTTP-to-SQLite naming regressions with synthetic, offline conversations."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from opensquilla.gateway import session_event_publisher
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc_sessions import _list_transcript_titles, _should_auto_title
from opensquilla.gateway.session_view import build_session_view_item
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionNode
from opensquilla.session.naming import generate_session_title
from opensquilla.session.storage import SessionStorage

_KEY = "agent:main:webchat:synthetic-naming"
_FIRST_MESSAGE = "  请说明如何检查数据库连接池\n以及工作线程  "
_FALLBACK_TITLE = "请说明如何检查数据库连接池 以及工作线程"


def _payload(content: object, *, refusal: str | None = None, finish_reason: str = "stop"):
    return {
        "model": "synthetic-naming-model",
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"content": content, "refusal": refusal},
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
    }


def _context(manager: SessionManager) -> SimpleNamespace:
    config = GatewayConfig()
    config.squilla_router.enabled = False
    config.squilla_router.rollout_phase = "observe"
    config.llm_ensemble.enabled = False
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="openrouter",
                model="synthetic-naming-model",
                api_key="synthetic-key",
                base_url="https://naming.example.test/v1",
            )
        )
    )
    return SimpleNamespace(config=config, session_manager=manager, provider_selector=selector)


def _mock_http(monkeypatch, handler) -> None:
    # Preserve the real HTTP request construction, decoding and accounting path.
    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        "opensquilla.session.naming.httpx.AsyncClient",
        lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs),
    )


async def _display_title(storage: SessionStorage) -> str:
    session = await storage.get_session(_KEY)
    assert session is not None
    titles = await _list_transcript_titles(storage, [session])
    return build_session_view_item(
        session,
        entry_count=await storage.count_transcript_entries(session.session_id),
        task_rows=[],
        now_ms=0,
        transcript_title=titles.get(session.session_id),
    )["title"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            _payload("I cannot generate a title for this request"), id="cannot-generate-title"
        ),
        pytest.param(_payload("I cannot assist with that request"), id="cannot-assist"),
        pytest.param(
            _payload("I'm sorry, but I can't help with that request"), id="sorry-cannot-help"
        ),
        pytest.param(
            _payload("I'm unable to provide assistance with this request"), id="unable-to-assist"
        ),
        pytest.param(_payload("抱歉，我无法协助处理该请求"), id="chinese-refusal"),
        pytest.param(
            _payload("Database connection pool", refusal="Request declined"),
            id="structured-refusal-with-content",
        ),
        pytest.param(
            _payload("Database connection pool", finish_reason="content_filter"),
            id="content-filter-with-content",
        ),
    ],
)
async def test_refusal_never_persists_or_retries_and_survives_reopen(
    tmp_path, monkeypatch, payload
) -> None:
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    _mock_http(monkeypatch, respond)
    emit = AsyncMock(wraps=session_event_publisher.emit_session_event)
    monkeypatch.setattr(session_event_publisher, "emit_session_event", emit)
    db_path = tmp_path / "synthetic-naming.db"
    storage = SessionStorage(str(db_path))
    await storage.connect()
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        node = await manager.create(_KEY, display_name="WebChat")
        ctx = _context(manager)
        assert await _should_auto_title(ctx, storage, node, _KEY, node.session_id)
        await manager.append_message(_KEY, "user", _FIRST_MESSAGE)

        await generate_session_title(ctx, _KEY, _FIRST_MESSAGE)

        persisted = await storage.get_session(_KEY)
        assert persisted is not None
        assert persisted.derived_title is None
        assert len(requests) == 1
        emit.assert_not_awaited()
        assert await _display_title(storage) == _FALLBACK_TITLE
        assert not await _should_auto_title(ctx, storage, persisted, _KEY, node.session_id)
        await manager.append_message(_KEY, "user", "继续检查线程池")
        assert not await _should_auto_title(ctx, storage, persisted, _KEY, node.session_id)
        assert len(requests) == 1
    finally:
        await storage.close()

    reopened = SessionStorage(str(db_path))
    await reopened.connect()
    try:
        persisted = await reopened.get_session(_KEY)
        assert persisted is not None
        assert persisted.derived_title is None
        assert await _display_title(reopened) == _FALLBACK_TITLE
    finally:
        await reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "Database connection pool",
        "检查数据库连接池",
        "I cannot log in",
        "Analyze refusal responses",
    ],
)
async def test_normal_title_persists_through_real_naming_pipeline(
    tmp_path, monkeypatch, content
) -> None:
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_payload(content))

    _mock_http(monkeypatch, respond)
    emit = AsyncMock(wraps=session_event_publisher.emit_session_event)
    monkeypatch.setattr(session_event_publisher, "emit_session_event", emit)
    storage = SessionStorage(str(tmp_path / "synthetic-normal-title.db"))
    await storage.connect()
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        await manager.create(_KEY, display_name="WebChat")
        await manager.append_message(_KEY, "user", _FIRST_MESSAGE)

        await generate_session_title(_context(manager), _KEY, _FIRST_MESSAGE)

        session = await storage.get_session(_KEY)
        assert session is not None
        assert session.derived_title == content
        assert await _display_title(storage) == content
        assert len(requests) == 1
        emit.assert_awaited_once()
        assert emit.await_args.args[3]["reason"] == "auto_titled"
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content", ["Database connection pool", "I cannot assist with that request"]
)
async def test_manual_rename_wins_while_naming_http_is_pending(
    tmp_path, monkeypatch, content
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def respond(_request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return httpx.Response(200, json=_payload(content))

    _mock_http(monkeypatch, respond)
    emit = AsyncMock(wraps=session_event_publisher.emit_session_event)
    monkeypatch.setattr(session_event_publisher, "emit_session_event", emit)
    storage = SessionStorage(str(tmp_path / "synthetic-concurrent-title.db"))
    await storage.connect()
    task = None
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        await storage.upsert_session(
            SessionNode(session_key=_KEY, session_id="synthetic-naming", display_name="WebChat")
        )
        await manager.append_message(_KEY, "user", _FIRST_MESSAGE)
        task = asyncio.create_task(generate_session_title(_context(manager), _KEY, _FIRST_MESSAGE))
        await asyncio.wait_for(started.wait(), timeout=5)

        # Even a refusal sentence is user-owned when assigned as a manual name.
        manual_name = "I cannot assist with that request"
        await manager.update(_KEY, display_name=manual_name)
        release.set()
        await asyncio.wait_for(task, timeout=5)

        session = await storage.get_session(_KEY)
        assert session is not None
        assert session.display_name == manual_name
        assert session.derived_title is None
        assert await _display_title(storage) == manual_name
        emit.assert_not_awaited()
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await storage.close()
