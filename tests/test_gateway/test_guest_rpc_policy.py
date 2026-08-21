from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import AuthConfig, GatewayConfig
from opensquilla.gateway.guest_rpc_policy import (
    GuestRpcPolicy,
    GuestRpcPolicyError,
    guest_owned_session_key,
)
from opensquilla.gateway.rpc import RpcContext, RpcRegistry, get_dispatcher
from opensquilla.gateway.rpc_sessions import _handle_sessions_list
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionNode, SessionStatus
from opensquilla.session.storage import SessionStorage

GUEST_KEY = "osqg_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def _guest(*, owner_id: str = "a" * 64) -> Principal:
    return Principal(
        role="operator",
        scopes=frozenset({"operator.read", "operator.write", "operator.admin"}),
        is_owner=False,
        authenticated=False,
        capabilities=frozenset({"guest.safe"}),
        auth_state="guest",
        guest_owner_id=owner_id,
        guest_session_key=GUEST_KEY,
    )


def _ctx(*, owner_id: str = "a" * 64, session_manager=None) -> RpcContext:
    return RpcContext(
        conn_id="guest",
        principal=_guest(owner_id=owner_id),
        session_manager=session_manager,
    )


@pytest.mark.parametrize(
    "method",
    [
        "sessions.search",
        "history.list",
        "logs.tail",
        "memory.search",
        "agents.files.get",
        "config.get",
        "setup.status",
        "tokens.list",
        "approvals.list",
        "skills.list",
        "sessions.subscribe",
        "sessions.send",
        "sessions.routing.get",
        "sessions.routing.set",
    ],
)
def test_guest_policy_denies_global_control_plane(method: str) -> None:
    with pytest.raises(GuestRpcPolicyError):
        GuestRpcPolicy.authorize(method, {}, _ctx())


def test_policy_does_not_reclassify_legacy_context_without_guest_markers() -> None:
    params = {"visible": True}
    ctx = RpcContext(
        conn_id="legacy-test",
        principal=SimpleNamespace(
            role="operator",
            scopes=frozenset({"operator.read"}),
        ),
    )

    assert GuestRpcPolicy.authorize("config.get", params, ctx) is params


@pytest.mark.asyncio
async def test_anonymous_node_role_cannot_bypass_guest_allowlist() -> None:
    from opensquilla.gateway.auth import resolve_auth

    principal = resolve_auth(
        GatewayConfig(host="0.0.0.0", auth=AuthConfig(mode="none")),
        auth_params={},
        role_claim="node",
        peer_ip="192.168.1.7",
    )
    assert principal is not None
    registry = RpcRegistry()

    async def handler(params, ctx):
        return {"bins": ["secret-tool"]}

    registry.register("skills.bins", handler, scope="node")
    response = await registry.dispatch(
        "node-guest",
        "skills.bins",
        {},
        RpcContext(conn_id="node-guest", principal=principal),
    )

    assert response.ok is False
    assert response.error.code == "UNAUTHORIZED"


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("chat.history", {"sessionKey": "agent:main:webchat:owner"}),
        ("chat.abort", {"sessionKey": "agent:main:webchat:owner"}),
        (
            "chat.clarify_submit",
            {"sessionKey": "agent:main:webchat:owner", "fields": {"answer": "no"}},
        ),
        ("artifacts.list", {"sessionKey": "agent:main:webchat:owner"}),
        (
            "artifacts.get",
            {
                "sessionKey": "agent:main:webchat:owner",
                "artifactId": "art-unowned",
            },
        ),
        ("sessions.bootstrap", {"key": "agent:main:webchat:owner"}),
        ("sessions.messages.subscribe", {"key": "agent:main:webchat:owner"}),
        ("sessions.messages.hydrate", {"key": "agent:main:webchat:owner"}),
        ("sessions.messages.snapshot", {"key": "agent:main:webchat:owner"}),
        ("sessions.messages.unsubscribe", {"key": "agent:main:webchat:owner"}),
    ],
)
def test_guest_policy_rejects_unowned_session_methods(method: str, params: dict[str, str]) -> None:
    with pytest.raises(GuestRpcPolicyError):
        GuestRpcPolicy.authorize(method, params, _ctx())


def test_guest_chat_send_rewrites_client_key_into_server_owner_namespace() -> None:
    ctx = _ctx()

    normalized = GuestRpcPolicy.authorize(
        "chat.send",
        {
            "sessionKey": "agent:main:webchat:owner-session",
            "message": "hello",
            "intent": "new_chat",
        },
        ctx,
    )

    expected = guest_owned_session_key(ctx.principal.guest_owner_id, "owner-session")
    assert normalized["sessionKey"] == expected
    assert expected.startswith(f"agent:main:webchat:guest:{'a' * 64}:")


def test_guest_chat_send_preserves_own_server_session_key() -> None:
    ctx = _ctx()
    owned = guest_owned_session_key(ctx.principal.guest_owner_id, "mine")

    normalized = GuestRpcPolicy.authorize(
        "chat.send", {"sessionKey": owned, "message": "again"}, ctx
    )

    assert normalized["sessionKey"] == owned


def test_guest_chat_send_forces_every_memory_capture_alias_true() -> None:
    ctx = _ctx()

    normalized = GuestRpcPolicy.authorize(
        "chat.send",
        {
            "sessionKey": "mine",
            "message": "do not retain this",
            "noMemoryCapture": False,
            "no_memory_capture": False,
            "_source": {
                "noMemoryCapture": False,
                "no_memory_capture": False,
            },
        },
        ctx,
    )

    assert normalized["noMemoryCapture"] is True
    assert normalized["no_memory_capture"] is True
    assert normalized["_source"]["noMemoryCapture"] is True
    assert normalized["_source"]["no_memory_capture"] is True


def test_guest_first_turn_cannot_override_session_routing() -> None:
    ctx = _ctx()
    owned = guest_owned_session_key(ctx.principal.guest_owner_id, "mine")

    immediate = GuestRpcPolicy.authorize(
        "chat.send",
        {
            "sessionKey": owned,
            "message": "hello",
            "intent": "new_chat",
            "initialRoutingMode": "ensemble",
            "initial_routing_mode": "router",
        },
        ctx,
    )
    staged = GuestRpcPolicy.authorize(
        "sessions.pending_inputs.enqueue",
        {
            "key": owned,
            "initialRoutingMode": "ensemble",
            "initial_routing_mode": "router",
        },
        ctx,
    )

    assert "initialRoutingMode" not in immediate
    assert "initial_routing_mode" not in immediate
    assert "initialRoutingMode" not in staged
    assert "initial_routing_mode" not in staged


def test_guest_policy_normalizes_verified_chat_key_alias_for_handler() -> None:
    ctx = _ctx()
    owned = guest_owned_session_key(ctx.principal.guest_owner_id, "mine")

    normalized = GuestRpcPolicy.authorize("chat.history", {"key": owned}, ctx)

    assert normalized == {"sessionKey": owned}


def test_guest_abort_preserves_task_scope_after_session_ownership_check() -> None:
    ctx = _ctx()
    owned = guest_owned_session_key(ctx.principal.guest_owner_id, "mine")

    normalized = GuestRpcPolicy.authorize(
        "chat.abort",
        {"sessionKey": owned, "taskId": "owned-task", "scope": "task"},
        ctx,
    )

    assert normalized == {
        "sessionKey": owned,
        "taskId": "owned-task",
        "scope": "task",
    }


def test_guest_can_submit_clarification_only_for_owned_session() -> None:
    ctx = _ctx()
    owned = guest_owned_session_key(ctx.principal.guest_owner_id, "mine")
    params = {"sessionKey": owned, "fields": {"destination": "Tokyo"}}

    assert GuestRpcPolicy.authorize("chat.clarify_submit", params, ctx) == params


def test_guest_can_rename_its_owned_session() -> None:
    ctx = _ctx()
    owned = guest_owned_session_key(ctx.principal.guest_owner_id, "mine")

    assert GuestRpcPolicy.authorize(
        "sessions.rename",
        {"sessionKey": owned, "displayName": "Renamed"},
        ctx,
    ) == {"key": owned, "displayName": "Renamed"}


@pytest.mark.parametrize("use_bulk_shape", [False, True])
def test_guest_can_delete_its_owned_session(use_bulk_shape: bool) -> None:
    ctx = _ctx()
    owned = guest_owned_session_key(ctx.principal.guest_owner_id, "mine")
    params = {"keys": [owned]} if use_bulk_shape else {"key": owned}

    assert GuestRpcPolicy.authorize("sessions.delete", params, ctx) == {
        "keys": [owned]
    }


@pytest.mark.parametrize("method", ["sessions.rename", "sessions.delete"])
def test_guest_session_mutations_reject_unowned_keys(method: str) -> None:
    params = (
        {"key": "agent:main:webchat:owner", "displayName": "Renamed"}
        if method == "sessions.rename"
        else {"keys": ["agent:main:webchat:owner"]}
    )

    with pytest.raises(GuestRpcPolicyError):
        GuestRpcPolicy.authorize(method, params, _ctx())


@pytest.mark.asyncio
async def test_default_docker_guest_can_rename_and_delete_owned_sessions(tmp_path) -> None:
    from opensquilla.gateway.auth import resolve_auth

    config = GatewayConfig(host="0.0.0.0", auth=AuthConfig(mode="none"))
    principal = resolve_auth(
        config,
        auth_params={"guestSessionKey": GUEST_KEY},
        role_claim="operator",
        peer_ip="127.0.0.1",
    )
    assert principal is not None
    assert principal.is_owner is False
    assert principal.authenticated is False
    rename_key = guest_owned_session_key(principal.guest_owner_id, "rename-me")
    delete_key = guest_owned_session_key(principal.guest_owner_id, "delete-me")
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    manager = SessionManager(storage)
    await manager.create(rename_key)
    await manager.create(delete_key)
    ctx = RpcContext(
        conn_id="docker-guest",
        principal=principal,
        config=config,
        session_manager=manager,
    )
    dispatcher = get_dispatcher()
    try:
        rename_response = await dispatcher.dispatch(
            "docker-rename",
            "sessions.rename",
            {"key": rename_key, "displayName": "Renamed"},
            ctx,
        )
        delete_response = await dispatcher.dispatch(
            "docker-delete",
            "sessions.delete",
            {"keys": [delete_key]},
            ctx,
        )

        assert rename_response.ok is True
        assert (await storage.get_session(rename_key)).display_name == "Renamed"
        assert delete_response.ok is True
        assert delete_response.payload == {"deleted": [delete_key], "errors": []}
        assert await storage.get_session(delete_key) is None
    finally:
        await storage.close()


@pytest.mark.parametrize("method", ["artifacts.list", "artifacts.get"])
def test_guest_can_read_artifacts_only_for_owned_session(method: str) -> None:
    ctx = _ctx()
    owned = guest_owned_session_key(ctx.principal.guest_owner_id, "mine")
    params = {"sessionKey": owned}
    if method == "artifacts.get":
        params["artifactId"] = "art-owned"

    assert GuestRpcPolicy.authorize(method, params, ctx) == params


@pytest.mark.asyncio
async def test_registry_applies_same_policy_to_missing_and_invalid_token_guests() -> None:
    registry = RpcRegistry()

    async def handler(params, ctx):
        return {"unexpected": True}

    registry.register("config.get", handler, scope="operator.read")
    principals = [
        _guest(),
        Principal(
            **{
                **_guest().__dict__,
                "auth_state": "invalid",
            }
        ),
    ]

    responses = [
        await registry.dispatch(
            str(index),
            "config.get",
            {},
            RpcContext(conn_id=str(index), principal=principal),
        )
        for index, principal in enumerate(principals)
    ]

    assert [response.ok for response in responses] == [False, False]
    assert [response.error.code for response in responses] == ["UNAUTHORIZED", "UNAUTHORIZED"]
    assert responses[0].error.message == responses[1].error.message


class _ListStorage:
    def __init__(self, sessions: list[SimpleNamespace]) -> None:
        self.sessions = sessions
        self.last_limit: int | None = None

    async def list_sessions(self, *, limit: int, guest_owner_id: str | None = None):
        assert guest_owner_id is not None
        self.last_limit = limit
        prefix = f":webchat:guest:{guest_owner_id}:"
        return [session for session in self.sessions if prefix in session.session_key][:limit]

    async def get_transcript(self, session_id: str, *, limit: int):
        return []

    async def count_transcript_entries(self, session_id: str) -> int:
        return 0


@pytest.mark.asyncio
async def test_sessions_list_only_returns_guest_owned_rows() -> None:
    owner_id = "a" * 64
    own_key = guest_owned_session_key(owner_id, "mine")
    storage = _ListStorage(
        [
            SimpleNamespace(session_key=own_key, session_id="mine"),
            SimpleNamespace(session_key="agent:main:webchat:owner", session_id="owner"),
            SimpleNamespace(
                session_key=guest_owned_session_key("b" * 64, "theirs"),
                session_id="theirs",
            ),
        ]
    )

    result = await _handle_sessions_list(
        {"limit": 50},
        _ctx(owner_id=owner_id, session_manager=SimpleNamespace(storage=storage)),
    )

    assert [row["key"] for row in result["sessions"]] == [own_key]
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_guest_sessions_list_filters_owner_before_limit(tmp_path) -> None:
    owner_id = "a" * 64
    own_key = guest_owned_session_key(owner_id, "old-owned")
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(
            SessionNode(
                session_key=own_key,
                session_id="old-owned",
                agent_id="main",
                created_at=1,
                updated_at=1,
                started_at=1,
                status=SessionStatus.DONE,
            )
        )
        await storage.upsert_session(
            SessionNode(
                session_key=(
                    f"agent:spoof:extra:webchat:guest:{owner_id}:malformed-collision"
                ),
                session_id="malformed-collision",
                agent_id="spoof",
                created_at=10_000,
                updated_at=10_000,
                started_at=10_000,
                status=SessionStatus.DONE,
            )
        )
        for index in range(75):
            await storage.upsert_session(
                SessionNode(
                    session_key=f"agent:main:webchat:global-{index}",
                    session_id=f"global-{index}",
                    agent_id="main",
                    created_at=1000 + index,
                    updated_at=1000 + index,
                    started_at=1000 + index,
                    status=SessionStatus.DONE,
                )
            )

        result = await _handle_sessions_list(
            {"limit": 1},
            _ctx(owner_id=owner_id, session_manager=SimpleNamespace(storage=storage)),
        )
    finally:
        await storage.close()

    assert [row["key"] for row in result["sessions"]] == [own_key]


@pytest.mark.asyncio
@pytest.mark.parametrize(("requested", "expected"), [(-99, 1), (100_000, 100)])
async def test_guest_sessions_list_clamps_limit(requested: int, expected: int) -> None:
    storage = _ListStorage([])

    await _handle_sessions_list(
        {"limit": requested},
        _ctx(session_manager=SimpleNamespace(storage=storage)),
    )

    assert storage.last_limit == expected
