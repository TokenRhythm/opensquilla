"""meta.run RPC: stamps a one-shot pending /meta launch for the surface.

The handler only STAMPS a pending launch (the surface starts the turn
later); it validates the name against loaded meta-skills and respects the
master ``meta_skill.enabled`` flag. The pipeline step ``meta_command_launch``
is what later turns the stamp into ``ctx.metadata["meta_launch"]``.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import opensquilla.gateway.rpc_meta_runs as rpc_meta_runs_module
from opensquilla.engine.steps.meta_command import (
    meta_command_launch,
    pending_meta_launch_peek,
    pending_meta_launch_pop,
    pending_meta_launch_promote,
)
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.protocol import (
    ERROR_INVALID_REQUEST,
    ERROR_UNAUTHORIZED,
    ERROR_UNAVAILABLE,
)
from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.gateway.rpc.registry import RpcContext, RpcHandlerError
from opensquilla.gateway.rpc_meta_runs import (
    _handle_meta_drafts_discard,
    _handle_meta_drafts_list,
    _handle_meta_run,
)
from opensquilla.gateway.scopes import ADMIN_SCOPE, METHOD_SCOPES, READ_SCOPE, WRITE_SCOPE
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage
from opensquilla.session.turn_context import turn_context_scope
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.types import (
    SkillPlatformMeta,
    SkillRequires,
    SkillSpec,
)
from tests.test_engine.test_runtime_meta_invoke_surfacing import _make_loader_with_meta


def _drain(session_key: str) -> None:
    """Clear any residual pending launch so each test starts clean."""
    pending_meta_launch_pop(session_key)


def _enabled_cfg(enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(meta_skill=SimpleNamespace(enabled=enabled, auto_trigger=False))


def _openrouter_cfg(*, api_key: str = "", api_key_env: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        meta_skill=SimpleNamespace(enabled=True, auto_trigger=False),
        llm=SimpleNamespace(
            provider="openrouter",
            api_key=api_key,
            api_key_env=api_key_env,
        ),
    )


class _ShortDramaProviderLoader:
    def __init__(self, tmp_path: Path) -> None:
        bundled = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "opensquilla"
            / "skills"
            / "bundled"
        )
        loader = SkillLoader(
            bundled_dir=bundled,
            snapshot_path=tmp_path / "short-drama-provider-snapshot.json",
        )
        self._specs = loader.load_all()
        for spec in self._specs:
            # Keep this RPC test focused on provider readiness while retaining
            # the exact bundled parent plan used by the capability trust gate.
            spec.metadata = SkillPlatformMeta()
        image = self.get_by_name("nano-banana-pro")
        video = self.get_by_name("seedance-2-prompt")
        assert image is not None
        assert video is not None
        image.metadata = SkillPlatformMeta(
            requires=SkillRequires(env_any=["OPENROUTER_API_KEY"])
        )
        video.metadata = SkillPlatformMeta(
            requires=SkillRequires(env_any=["OPENROUTER_API_KEY", "ARK_API_KEY"])
        )

    def load_all(self) -> list[SkillSpec]:
        return list(self._specs)

    def get_by_name(self, name: str) -> SkillSpec | None:
        return {spec.name: spec for spec in self.load_all()}.get(name)


def test_meta_run_scope_contract() -> None:
    assert METHOD_SCOPES["meta.run"] == WRITE_SCOPE
    assert METHOD_SCOPES["meta.drafts.list"] == WRITE_SCOPE
    assert METHOD_SCOPES["meta.drafts.discard"] == WRITE_SCOPE


@pytest.mark.asyncio
async def test_meta_draft_survives_app_close_reopen_with_exact_request(
    tmp_path: Path,
) -> None:
    session_key = "agent:main:webchat:provisional-durable-request"
    request_id = "app-close-reopen-request"
    launch_text = "/meta meta-tiny -- Keep every word after closing the app"
    db_path = tmp_path / "sessions.db"
    storage = await SessionStorage.open(str(db_path))
    loader = _make_loader_with_meta(tmp_path)
    try:
        first = await _handle_meta_run(
            {
                "name": "meta-tiny",
                "sessionKey": session_key,
                "clientRequestId": request_id,
                "launchText": launch_text,
            },
            RpcContext(
                conn_id="before-close",
                config=_enabled_cfg(),
                skill_loader=loader,
                session_manager=SessionManager(storage, inject_time_prefix=False),
            ),
        )
        assert first["drafted"] is True
        assert first["replayed"] is False
    finally:
        await storage.close()

    reopened = await SessionStorage.open(str(db_path))
    reopened_manager = SessionManager(reopened, inject_time_prefix=False)
    reopened_ctx = RpcContext(
        conn_id="after-reopen",
        config=_enabled_cfg(),
        skill_loader=loader,
        session_manager=reopened_manager,
    )
    try:
        listed = await _handle_meta_drafts_list({"agentId": "main"}, reopened_ctx)
        assert listed["drafts"] == [
            {
                "sessionKey": session_key,
                "clientRequestId": request_id,
                "name": "meta-tiny",
                "launchText": launch_text,
                "createdAt": listed["drafts"][0]["createdAt"],
                "expiresAt": listed["drafts"][0]["expiresAt"],
                "sessionExists": False,
            }
        ]

        retried = await _handle_meta_run(
            {
                "name": "meta-tiny",
                "sessionKey": session_key,
                "clientRequestId": request_id,
                "launchText": launch_text,
            },
            reopened_ctx,
        )
        assert retried["drafted"] is True
        assert retried["replayed"] is True

        discarded = await _handle_meta_drafts_discard(
            {"sessionKey": session_key, "clientRequestId": request_id},
            reopened_ctx,
        )
        assert discarded == {"ok": True, "discarded": True, "accepted": False}
        assert (await _handle_meta_drafts_list({"sessionKey": session_key}, reopened_ctx))[
            "drafts"
        ] == []
        assert await reopened.get_meta_control_intent(
            session_key=session_key,
            control_kind="manual",
            correlation_id=f"request:{request_id}",
        ) is None
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_meta_run_rejects_a_stale_request_after_explicit_discard(
    tmp_path: Path,
) -> None:
    session_key = "agent:main:webchat:terminal-draft-discard"
    request_id = "terminal-draft-discard-request"
    launch_text = "/meta meta-tiny -- Do not run after cancellation"
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    ctx = RpcContext(
        conn_id="terminal-draft-discard",
        config=_enabled_cfg(),
        skill_loader=_make_loader_with_meta(tmp_path),
        session_manager=SessionManager(storage, inject_time_prefix=False),
    )
    try:
        staged = await _handle_meta_run(
            {
                "name": "meta-tiny",
                "sessionKey": session_key,
                "clientRequestId": request_id,
                "launchText": launch_text,
            },
            ctx,
        )
        assert staged["drafted"] is True

        discarded = await _handle_meta_drafts_discard(
            {"sessionKey": session_key, "clientRequestId": request_id},
            ctx,
        )
        assert discarded == {"ok": True, "discarded": True, "accepted": False}

        with pytest.raises(RpcHandlerError) as exc_info:
            await _handle_meta_run(
                {
                    "name": "meta-tiny",
                    "sessionKey": session_key,
                    "clientRequestId": request_id,
                    "launchText": launch_text,
                },
                ctx,
            )
        assert exc_info.value.code == "META_DRAFT_DISCARDED"
        assert exc_info.value.retryable is False
        assert exc_info.value.accepted is False
        assert await storage.get_meta_control_intent(
            session_key=session_key,
            control_kind="manual",
            correlation_id=f"request:{request_id}",
        ) is None
    finally:
        await storage.close()


@pytest.mark.parametrize("boundary", ("reset", "delete"))
@pytest.mark.asyncio
async def test_meta_run_rejects_a_stale_request_after_session_boundary(
    tmp_path: Path,
    boundary: str,
) -> None:
    session_key = f"agent:main:webchat:terminal-draft-{boundary}"
    request_id = f"terminal-draft-{boundary}-request"
    launch_text = "/meta meta-tiny -- Do not restore the old session request"
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    ctx = RpcContext(
        conn_id=f"terminal-draft-{boundary}",
        config=_enabled_cfg(),
        skill_loader=_make_loader_with_meta(tmp_path),
        session_manager=SessionManager(storage, inject_time_prefix=False),
    )
    try:
        await storage.upsert_session(
            SessionNode(
                session_key=session_key,
                session_id=f"terminal-draft-{boundary}-session",
            )
        )
        await storage.stage_meta_launch_draft(
            session_key=session_key,
            client_request_id=request_id,
            meta_skill_name="meta-tiny",
            launch_text=launch_text,
        )
        if boundary == "reset":
            assert await storage.advance_reset_epoch(session_key) == 1
        else:
            await storage.delete_session(session_key)

        with pytest.raises(RpcHandlerError) as exc_info:
            await _handle_meta_run(
                {
                    "name": "meta-tiny",
                    "sessionKey": session_key,
                    "clientRequestId": request_id,
                    "launchText": launch_text,
                },
                ctx,
            )
        assert exc_info.value.code == "META_DRAFT_DISCARDED"
        assert exc_info.value.retryable is False
        assert exc_info.value.accepted is False
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_meta_draft_discard_reports_an_already_accepted_launch(
    tmp_path: Path,
) -> None:
    session_key = "agent:main:webchat:accepted-before-discard"
    request_id = "accepted-before-discard-request"
    launch_text = "/meta meta-tiny -- Run exactly once"
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    loader = _make_loader_with_meta(tmp_path)
    ctx = RpcContext(
        conn_id="accepted-before-discard",
        config=_enabled_cfg(),
        skill_loader=loader,
        session_manager=SessionManager(storage, inject_time_prefix=False),
    )
    try:
        await storage.stage_meta_launch_draft(
            session_key=session_key,
            client_request_id=request_id,
            meta_skill_name="meta-tiny",
            launch_text=launch_text,
        )
        intent, _ = await storage.promote_meta_launch_draft(
            session_key=session_key,
            client_request_id=request_id,
            meta_skill_name="meta-tiny",
            launch_text=launch_text,
        )
        await storage.conn.execute(
            "UPDATE meta_control_intents SET status = 'accepted' WHERE intent_id = ?",
            (intent.intent_id,),
        )
        await storage.conn.commit()

        result = await _handle_meta_drafts_discard(
            {"sessionKey": session_key, "clientRequestId": request_id},
            ctx,
        )

        assert result == {"ok": True, "discarded": False, "accepted": True}
        preserved = await storage.get_meta_control_intent(
            session_key=session_key,
            control_kind="manual",
            correlation_id=f"request:{request_id}",
        )
        assert preserved is not None
        assert preserved.status == "accepted"
    finally:
        await storage.close()


@pytest.mark.parametrize(
    ("session_key", "client_request_id"),
    (
        ("x" * 513, "valid-request"),
        ("agent:main:webchat:discard", "contains whitespace"),
        ("agent:main:webchat:discard", "x" * 257),
        ("agent:main:webchat:discard", "/meta meta-tiny -- raw prompt"),
        (42, "valid-request"),
    ),
)
@pytest.mark.asyncio
async def test_meta_draft_discard_rejects_unbounded_or_contentful_coordinates(
    tmp_path: Path,
    session_key: object,
    client_request_id: object,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    ctx = RpcContext(
        conn_id="invalid-meta-discard",
        config=_enabled_cfg(),
        skill_loader=_make_loader_with_meta(tmp_path),
        session_manager=SessionManager(storage, inject_time_prefix=False),
    )
    try:
        with pytest.raises(RpcHandlerError) as exc_info:
            await _handle_meta_drafts_discard(
                {"sessionKey": session_key, "clientRequestId": client_request_id},
                ctx,
            )
        assert exc_info.value.code == ERROR_INVALID_REQUEST
        async with storage.conn.execute(
            "SELECT COUNT(*) FROM meta_launch_discard_tombstones"
        ) as cur:
            assert (await cur.fetchone())[0] == 0
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_meta_draft_discard_capacity_is_retryable_not_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "opensquilla.session.storage._META_LAUNCH_DISCARD_PER_SESSION_LIMIT",
        0,
    )
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    ctx = RpcContext(
        conn_id="full-meta-discard",
        config=_enabled_cfg(),
        skill_loader=_make_loader_with_meta(tmp_path),
        session_manager=SessionManager(storage, inject_time_prefix=False),
    )
    try:
        with pytest.raises(RpcHandlerError) as exc_info:
            await _handle_meta_drafts_discard(
                {
                    "sessionKey": "agent:main:webchat:discard-full",
                    "clientRequestId": "discard-full-request",
                },
                ctx,
            )
        assert exc_info.value.code == ERROR_UNAVAILABLE
        async with storage.conn.execute(
            "SELECT COUNT(*) FROM meta_launch_discard_tombstones"
        ) as cur:
            assert (await cur.fetchone())[0] == 0
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_raw_draft_list_and_discard_require_owner_or_admin(tmp_path: Path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.stage_meta_launch_draft(
            session_key="agent:main:webchat:private-draft",
            client_request_id="private-draft-request",
            meta_skill_name="meta-tiny",
            launch_text="/meta meta-tiny -- private request text",
        )
        remote_operator = Principal(
            role="operator",
            scopes=frozenset({READ_SCOPE, WRITE_SCOPE}),
            is_owner=False,
            authenticated=True,
        )
        ctx = RpcContext(
            conn_id="remote-operator",
            principal=remote_operator,
            session_manager=SessionManager(storage, inject_time_prefix=False),
        )
        denied_list = await get_dispatcher().dispatch(
            "draft-list-agent",
            "meta.drafts.list",
            {"agentId": "main"},
            ctx,
        )
        assert denied_list.error is not None
        assert denied_list.error.code == ERROR_UNAUTHORIZED

        denied_discard = await get_dispatcher().dispatch(
            "draft-discard-session",
            "meta.drafts.discard",
            {
                "sessionKey": "agent:main:webchat:private-draft",
                "clientRequestId": "private-draft-request",
            },
            ctx,
        )
        assert denied_discard.error is not None
        assert denied_discard.error.code == ERROR_UNAUTHORIZED

        owner_ctx = RpcContext(
            conn_id="local-owner-least-privilege",
            principal=Principal(
                role="operator",
                scopes=frozenset({READ_SCOPE, WRITE_SCOPE}),
                is_owner=True,
                authenticated=True,
            ),
            session_manager=SessionManager(storage, inject_time_prefix=False),
        )
        owner_listed = await get_dispatcher().dispatch(
            "draft-list-owner",
            "meta.drafts.list",
            {"sessionKey": "agent:main:webchat:private-draft"},
            owner_ctx,
        )
        assert owner_listed.error is None
        assert owner_listed.payload["drafts"][0]["clientRequestId"] == (
            "private-draft-request"
        )

        admin_ctx = RpcContext(
            conn_id="remote-admin",
            principal=Principal(
                role="operator",
                scopes=frozenset({ADMIN_SCOPE}),
                is_owner=False,
                authenticated=True,
            ),
            session_manager=SessionManager(storage, inject_time_prefix=False),
        )
        listed = await get_dispatcher().dispatch(
            "draft-list-admin",
            "meta.drafts.list",
            {"sessionKey": "agent:main:webchat:private-draft"},
            admin_ctx,
        )
        assert listed.error is None
        assert listed.payload["drafts"][0]["clientRequestId"] == "private-draft-request"
        discarded = await get_dispatcher().dispatch(
            "draft-discard-admin",
            "meta.drafts.discard",
            {
                "sessionKey": "agent:main:webchat:private-draft",
                "clientRequestId": "private-draft-request",
            },
            admin_ctx,
        )
        assert discarded.error is None
        assert discarded.payload["discarded"] is True
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_meta_draft_list_times_out_as_retryable_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = asyncio.Event()

    class BlockingStorage:
        async def list_meta_launch_drafts(self, **_kwargs: Any) -> list[Any]:
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    monkeypatch.setattr(rpc_meta_runs_module, "_META_DRAFT_LIST_TIMEOUT_SECONDS", 0.01)
    response = await get_dispatcher().dispatch(
        "slow-draft-list",
        "meta.drafts.list",
        {"agentId": "main"},
        RpcContext(
            conn_id="slow-draft-list",
            session_manager=SimpleNamespace(storage=BlockingStorage()),
        ),
    )

    assert response.error is not None
    assert response.error.code == ERROR_UNAVAILABLE
    assert response.error.retryable is True
    assert cancelled.is_set()


def test_meta_run_valid_invokable_skill_stamps_launch(tmp_path: Path) -> None:
    _drain("sess-run-1")
    loader = _make_loader_with_meta(tmp_path)
    ctx = RpcContext(conn_id="test", config=_enabled_cfg(), skill_loader=loader)

    payload = asyncio.run(
        _handle_meta_run({"name": "meta-tiny", "sessionKey": "sess-run-1"}, ctx)
    )

    assert payload == {"ok": True, "name": "meta-tiny", "sessionKey": "sess-run-1"}
    # Store was stamped — the next turn would pop this exact name.
    assert pending_meta_launch_pop("sess-run-1") == "meta-tiny"


@pytest.mark.asyncio
async def test_meta_run_identified_launch_is_durable_across_gateway_reopen(
    tmp_path: Path,
) -> None:
    session_key = "agent:main:webchat:durable-meta-run"
    request_id = "durable-meta-run-request"
    db_path = tmp_path / "sessions.db"
    storage = await SessionStorage.open(str(db_path))
    manager = SessionManager(storage, inject_time_prefix=False)
    await manager.create(session_key, agent_id="main")
    loader = _make_loader_with_meta(tmp_path)
    ctx = RpcContext(
        conn_id="test",
        config=_enabled_cfg(),
        skill_loader=loader,
        session_manager=manager,
    )
    try:
        first = await _handle_meta_run(
            {
                "name": "meta-tiny",
                "sessionKey": session_key,
                "clientRequestId": request_id,
            },
            ctx,
        )
        assert first["replayed"] is False
        assert pending_meta_launch_peek(
            session_key,
            client_request_id=request_id,
        ) is None
    finally:
        await storage.close()

    reopened = await SessionStorage.open(str(db_path))
    try:
        intent = await reopened.get_meta_control_intent(
            session_key=session_key,
            control_kind="manual",
            correlation_id=f"request:{request_id}",
        )
        assert intent is not None
        assert intent.meta_skill_name == "meta-tiny"
        assert intent.status == "staged"

        reopened_manager = SessionManager(reopened, inject_time_prefix=False)
        replayed = await _handle_meta_run(
            {
                "name": "meta-tiny",
                "sessionKey": session_key,
                "clientRequestId": request_id,
            },
            RpcContext(
                conn_id="test-reopened",
                config=_enabled_cfg(),
                    skill_loader=loader,
                session_manager=reopened_manager,
            ),
        )
        assert replayed["replayed"] is True
    finally:
        await reopened.close()


def test_meta_run_client_request_id_is_idempotent_after_launch_is_consumed(
    tmp_path: Path,
) -> None:
    session_key = "sess-run-cross-tab"
    client_request_id = "meta-provider-handoff-cross-tab"
    _drain(session_key)
    loader = _make_loader_with_meta(tmp_path)
    ctx = RpcContext(conn_id="test", config=_enabled_cfg(), skill_loader=loader)

    first = asyncio.run(
        _handle_meta_run(
            {
                "name": "meta-tiny",
                "sessionKey": session_key,
                "clientRequestId": client_request_id,
            },
            ctx,
        )
    )
    assert first == {
        "ok": True,
        "name": "meta-tiny",
        "sessionKey": session_key,
        "clientRequestId": client_request_id,
        "replayed": False,
    }

    launch_turn = SimpleNamespace(
        session_key=session_key,
        message="/meta meta-tiny",
        semantic_message="/meta meta-tiny",
        metadata={},
    )
    assert (
        pending_meta_launch_promote(
            session_key,
            client_request_id=client_request_id,
            message=launch_turn.message,
        )
        == "promoted"
    )
    async def consume_launch() -> None:
        with turn_context_scope({"client_request_id": client_request_id}):
            await meta_command_launch(launch_turn)

    asyncio.run(consume_launch())
    assert launch_turn.metadata["meta_launch"] == {"name": "meta-tiny"}
    assert pending_meta_launch_peek(session_key) is None

    # A late second-tab meta.run uses the same durable request identity as its
    # duplicate chat.send. It succeeds idempotently without leaving a marker
    # that the already-deduplicated chat.send would never consume.
    replay = asyncio.run(
        _handle_meta_run(
            {
                "name": "meta-tiny",
                "sessionKey": session_key,
                "clientRequestId": client_request_id,
            },
            ctx,
        )
    )
    assert replay == {**first, "replayed": True}
    assert pending_meta_launch_peek(session_key) is None


@pytest.mark.parametrize(
    "client_request_id",
    ["", "   ", "request id with spaces", 42, "x" * 257],
)
def test_meta_run_rejects_invalid_client_request_id(
    tmp_path: Path,
    client_request_id: object,
) -> None:
    session_key = "sess-run-invalid-request-id"
    _drain(session_key)
    ctx = RpcContext(
        conn_id="test",
        config=_enabled_cfg(),
        skill_loader=_make_loader_with_meta(tmp_path),
    )

    with pytest.raises(Exception, match="clientRequestId"):
        asyncio.run(
            _handle_meta_run(
                {
                    "name": "meta-tiny",
                    "sessionKey": session_key,
                    "clientRequestId": client_request_id,
                },
                ctx,
            )
        )
    assert pending_meta_launch_peek(session_key) is None


def test_meta_run_pending_capacity_is_retryable_and_not_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = RpcContext(
        conn_id="test",
        config=_enabled_cfg(),
        skill_loader=_make_loader_with_meta(tmp_path),
    )
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_meta_runs.pending_meta_launch_put",
        lambda *_args, **_kwargs: "capacity",
    )

    with pytest.raises(RpcHandlerError) as raised:
        asyncio.run(
            _handle_meta_run(
                {
                    "name": "meta-tiny",
                    "sessionKey": "sess-run-capacity",
                    "clientRequestId": "capacity-request",
                },
                ctx,
            )
        )

    assert raised.value.code == "META_LAUNCH_BUSY"
    assert raised.value.retryable is True
    assert raised.value.retry_after_ms == 1000
    assert raised.value.accepted is False


def test_meta_run_accepts_key_alias(tmp_path: Path) -> None:
    _drain("sess-run-alias")
    loader = _make_loader_with_meta(tmp_path)
    ctx = RpcContext(conn_id="test", config=_enabled_cfg(), skill_loader=loader)

    payload = asyncio.run(
        _handle_meta_run({"name": "meta-tiny", "key": "sess-run-alias"}, ctx)
    )

    assert payload["ok"] is True
    assert payload["sessionKey"] == "sess-run-alias"
    assert pending_meta_launch_pop("sess-run-alias") == "meta-tiny"


def test_meta_run_uses_only_first_name_token(tmp_path: Path) -> None:
    _drain("sess-run-request")
    loader = _make_loader_with_meta(tmp_path)
    ctx = RpcContext(conn_id="test", config=_enabled_cfg(), skill_loader=loader)

    payload = asyncio.run(
        _handle_meta_run(
            {
                "name": "meta-tiny -- Write a ten-page paper",
                "sessionKey": "sess-run-request",
            },
            ctx,
        )
    )

    assert payload["ok"] is True
    assert payload["name"] == "meta-tiny"
    assert pending_meta_launch_pop("sess-run-request") == "meta-tiny"


def test_meta_run_unknown_name_refused_and_not_stamped(tmp_path: Path) -> None:
    _drain("sess-run-2")
    loader = _make_loader_with_meta(tmp_path)
    ctx = RpcContext(conn_id="test", config=_enabled_cfg(), skill_loader=loader)

    payload = asyncio.run(
        _handle_meta_run({"name": "nope-skill", "sessionKey": "sess-run-2"}, ctx)
    )

    assert payload["ok"] is False
    assert "nope-skill" in payload["error"]
    assert pending_meta_launch_pop("sess-run-2") is None


def test_meta_run_disable_model_invocation_refused_and_not_stamped(tmp_path: Path) -> None:
    _drain("sess-run-3")
    loader = _make_loader_with_meta(tmp_path, disable_model_invocation=True)
    ctx = RpcContext(conn_id="test", config=_enabled_cfg(), skill_loader=loader)

    payload = asyncio.run(
        _handle_meta_run({"name": "meta-tiny", "sessionKey": "sess-run-3"}, ctx)
    )

    assert payload["ok"] is False
    assert "meta-tiny" in payload["error"]
    assert pending_meta_launch_pop("sess-run-3") is None


def test_meta_run_disabled_flag_refused_and_not_stamped(tmp_path: Path) -> None:
    _drain("sess-run-4")
    loader = _make_loader_with_meta(tmp_path)
    ctx = RpcContext(
        conn_id="test",
        config={"meta_skill": {"enabled": False}},
        skill_loader=loader,
    )

    payload = asyncio.run(
        _handle_meta_run({"name": "meta-tiny", "sessionKey": "sess-run-4"}, ctx)
    )

    assert payload["ok"] is False
    assert payload.get("disabled") is True
    assert pending_meta_launch_pop("sess-run-4") is None


def test_meta_run_missing_dependency_refused_before_launch(tmp_path: Path) -> None:
    _drain("sess-run-setup")
    loader = _make_loader_with_meta(tmp_path)
    spec = loader.get_by_name("meta-tiny")
    assert spec is not None
    spec.metadata = SkillPlatformMeta(
        requires=SkillRequires(bins=["opensquilla-definitely-missing-binary"])
    )
    ctx = RpcContext(conn_id="test", config=_enabled_cfg(), skill_loader=loader)

    payload = asyncio.run(
        _handle_meta_run(
            {"name": "meta-tiny", "sessionKey": "sess-run-setup"}, ctx
        )
    )

    assert payload["ok"] is False
    assert payload["code"] == "META_SKILL_SETUP_REQUIRED"
    assert payload["setup_required"] is True
    assert payload["readiness"]["missing_bins"] == [
        "opensquilla-definitely-missing-binary"
    ]
    assert pending_meta_launch_pop("sess-run-setup") is None


@pytest.mark.asyncio
async def test_setup_required_meta_run_saves_original_request_before_return(
    tmp_path: Path,
) -> None:
    session_key = "agent:main:webchat:setup-durable"
    request_id = "setup-durable-request"
    launch_text = "/meta meta-tiny -- Produce the complete multi-page result"
    loader = _make_loader_with_meta(tmp_path)
    spec = loader.get_by_name("meta-tiny")
    assert spec is not None
    spec.metadata = SkillPlatformMeta(
        requires=SkillRequires(bins=["opensquilla-definitely-missing-binary"])
    )
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        ctx = RpcContext(
            conn_id="test",
            config=_enabled_cfg(),
            skill_loader=loader,
            session_manager=SessionManager(storage, inject_time_prefix=False),
        )
        payload = await _handle_meta_run(
            {
                "name": "meta-tiny",
                "sessionKey": session_key,
                "clientRequestId": request_id,
                "launchText": launch_text,
            },
            ctx,
        )

        assert payload["code"] == "META_SKILL_SETUP_REQUIRED"
        assert payload["drafted"] is True
        drafts = await storage.list_meta_launch_drafts(session_key=session_key)
        assert [(draft.client_request_id, draft.launch_text) for draft in drafts] == [
            (request_id, launch_text)
        ]
        assert await storage.get_meta_control_intent(
            session_key=session_key,
            control_kind="manual",
            correlation_id=f"request:{request_id}",
        ) is None
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_legacy_meta_run_observes_terminal_discard_before_setup_readiness(
    tmp_path: Path,
) -> None:
    session_key = "agent:main:webchat:legacy-discard-before-readiness"
    request_id = "legacy-discard-before-readiness-request"
    loader = _make_loader_with_meta(tmp_path)
    spec = loader.get_by_name("meta-tiny")
    assert spec is not None
    spec.metadata = SkillPlatformMeta(
        requires=SkillRequires(bins=["opensquilla-definitely-missing-binary"])
    )
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        assert await storage.discard_meta_launch_draft(
            session_key=session_key,
            client_request_id=request_id,
        )
        ctx = RpcContext(
            conn_id="legacy-discard-before-readiness",
            config=_enabled_cfg(),
            skill_loader=loader,
            session_manager=SessionManager(storage, inject_time_prefix=False),
        )

        with pytest.raises(RpcHandlerError) as exc_info:
            await _handle_meta_run(
                {
                    "name": "meta-tiny",
                    "sessionKey": session_key,
                    "clientRequestId": request_id,
                },
                ctx,
            )
        assert exc_info.value.code == "META_DRAFT_DISCARDED"
        assert exc_info.value.retryable is False
        assert exc_info.value.accepted is False
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_discard_during_slow_readiness_prevents_late_control_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "agent:main:webchat:discard-during-readiness"
    request_id = "discard-during-readiness-request"
    launch_text = "/meta meta-tiny -- Do not resurrect this discarded request"
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    entered_readiness = threading.Event()
    release_readiness = threading.Event()
    original_assess = rpc_meta_runs_module.assess_meta_skill_readiness

    def slow_assess(*args: Any, **kwargs: Any):
        entered_readiness.set()
        if not release_readiness.wait(timeout=5):
            raise TimeoutError("test did not release readiness")
        return original_assess(*args, **kwargs)

    monkeypatch.setattr(rpc_meta_runs_module, "assess_meta_skill_readiness", slow_assess)
    try:
        ctx = RpcContext(
            conn_id="slow-readiness",
            config=_enabled_cfg(),
            skill_loader=_make_loader_with_meta(tmp_path),
            session_manager=SessionManager(storage, inject_time_prefix=False),
        )
        run_task = asyncio.create_task(_handle_meta_run(
            {
                "name": "meta-tiny",
                "sessionKey": session_key,
                "clientRequestId": request_id,
                "launchText": launch_text,
            },
            ctx,
        ))
        assert await asyncio.to_thread(entered_readiness.wait, 5)
        assert await storage.discard_meta_launch_draft(
            session_key=session_key,
            client_request_id=request_id,
        )
        release_readiness.set()

        with pytest.raises(RpcHandlerError) as exc_info:
            await run_task
        assert exc_info.value.code == "META_DRAFT_DISCARDED"
        assert exc_info.value.retryable is False
        assert exc_info.value.accepted is False
        assert await storage.get_meta_control_intent(
            session_key=session_key,
            control_kind="manual",
            correlation_id=f"request:{request_id}",
        ) is None
    finally:
        release_readiness.set()
        await storage.close()


def test_short_drama_missing_provider_returns_manual_action_without_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "sess-run-short-drama-provider"
    _drain(session_key)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setattr(
        "opensquilla.skills.meta.readiness.is_skill_available_live",
        lambda _name: True,
    )
    config = SimpleNamespace(
        meta_skill=SimpleNamespace(enabled=True, auto_trigger=False),
        llm=SimpleNamespace(
            provider="tokenrhythm",
            model="deepseek-v4-pro",
            api_key="synthetic-primary-only-key",
            api_key_env="",
            base_url="https://tokenrhythm.studio/v1",
            proxy="",
        ),
        llm_profiles={},
    )
    ctx = RpcContext(
        conn_id="test",
        config=config,
        skill_loader=_ShortDramaProviderLoader(tmp_path),
    )

    payload = asyncio.run(
        _handle_meta_run(
            {"name": "meta-short-drama", "sessionKey": session_key},
            ctx,
        )
    )

    assert payload["code"] == "META_SKILL_SETUP_REQUIRED"
    assert payload["readiness"]["manual_setup_actions"] == [
        {
            "id": "provider:openrouter",
            "kind": "provider_connection",
            "provider_id": "openrouter",
            "capability_ids": ["image.generate.reference", "video.generate"],
            "reason_code": "missing_credential",
            "label": "OpenRouter",
            "recommended": True,
            "available": True,
            "reason": "A provider credential is required.",
        }
    ]
    assert "synthetic-primary-only-key" not in repr(payload)
    assert pending_meta_launch_pop(session_key) is None


def test_short_drama_launch_accepts_secondary_openrouter_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "sess-run-short-drama-secondary"
    _drain(session_key)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        "opensquilla.skills.meta.readiness.is_skill_available_live",
        lambda _name: True,
    )
    secret = "synthetic-secondary-profile-key"
    config = SimpleNamespace(
        meta_skill=SimpleNamespace(enabled=True, auto_trigger=False),
        llm=SimpleNamespace(
            provider="tokenrhythm",
            model="deepseek-v4-pro",
            api_key="synthetic-primary-key",
            api_key_env="",
            base_url="https://tokenrhythm.studio/v1",
            proxy="",
        ),
        llm_profiles={
            "openrouter": SimpleNamespace(
                model="bytedance/seedance-2.0",
                api_key=secret,
                api_key_env="",
                api_key_env_pool=[],
                base_url="https://openrouter.ai/api/v1",
                proxy="",
            )
        },
    )
    ctx = RpcContext(
        conn_id="test",
        config=config,
        skill_loader=_ShortDramaProviderLoader(tmp_path),
    )

    payload = asyncio.run(
        _handle_meta_run(
            {"name": "meta-short-drama", "sessionKey": session_key},
            ctx,
        )
    )

    assert payload == {
        "ok": True,
        "name": "meta-short-drama",
        "sessionKey": session_key,
    }
    assert secret not in repr(payload)
    assert pending_meta_launch_pop(session_key) == "meta-short-drama"


@pytest.mark.parametrize("credential_source", ["config", "custom-env"])
def test_meta_run_does_not_globally_alias_openrouter_for_untrusted_meta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_source: str,
) -> None:
    session_key = f"sess-run-openrouter-{credential_source}"
    _drain(session_key)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    custom_env = "OPENSQUILLA_TEST_META_OPENROUTER_KEY"
    monkeypatch.delenv(custom_env, raising=False)
    if credential_source == "config":
        secret = "synthetic-openrouter-config-key"
        config = _openrouter_cfg(api_key=secret)
    else:
        secret = "synthetic-openrouter-custom-env-key"
        monkeypatch.setenv(custom_env, secret)
        config = _openrouter_cfg(api_key_env=custom_env)

    loader = _make_loader_with_meta(tmp_path)
    spec = loader.get_by_name("meta-tiny")
    assert spec is not None
    spec.metadata = SkillPlatformMeta(
        requires=SkillRequires(env_any=["OPENROUTER_API_KEY"])
    )
    ctx = RpcContext(conn_id="test", config=config, skill_loader=loader)

    payload = asyncio.run(
        _handle_meta_run({"name": "meta-tiny", "sessionKey": session_key}, ctx)
    )

    assert payload["ok"] is False
    assert payload["code"] == "META_SKILL_SETUP_REQUIRED"
    assert payload["readiness"]["missing_env_any"] == [["OPENROUTER_API_KEY"]]
    assert secret not in repr(payload)
    assert pending_meta_launch_pop(session_key) is None


def test_meta_run_requires_name_and_session_key(tmp_path: Path) -> None:
    loader = _make_loader_with_meta(tmp_path)
    ctx = RpcContext(conn_id="test", config=_enabled_cfg(), skill_loader=loader)

    with pytest.raises(Exception):
        asyncio.run(_handle_meta_run({"sessionKey": "sess-x"}, ctx))
    with pytest.raises(Exception):
        asyncio.run(_handle_meta_run({"name": "meta-tiny"}, ctx))
