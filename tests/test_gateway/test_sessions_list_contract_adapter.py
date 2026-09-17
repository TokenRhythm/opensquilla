from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import structlog
from starlette.testclient import TestClient

import opensquilla.gateway.adapters.sessions_list_contract as sessions_list_gateway_adapter
import opensquilla.gateway.rpc_sessions as rpc_sessions
from opensquilla.contracts.adapters.sessions_list_contract import (
    SESSIONS_LIST_METHOD,
    SessionsListContractError,
    call_sessions_list,
    sessions_list_params_contract_errors,
    validate_sessions_list_result,
)
from opensquilla.contracts.generated.v4.gateway_contract_registry import (
    GATEWAY_METHOD_CONTRACTS,
)
from opensquilla.contracts.generated.v4.sessions_list_metadata import SESSIONS_LIST_SCOPE
from opensquilla.gateway.adapters.sessions_list_contract import (
    register_sessions_list_contract,
)
from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, RpcRegistry, get_dispatcher
from opensquilla.gateway.rpc_sessions import (
    _handle_sessions_list,
    _handle_sessions_list_contract,
)
from opensquilla.session.models import SessionNode, SessionStatus
from opensquilla.session.storage import SessionStorage

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "contracts" / "gateway" / "v4" / "sessions" / "fixtures"


def _fixture_document(name: str) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads((FIXTURES / name).read_text(encoding="utf-8")),
    )


def _fixture_case(document: str, case_id: str) -> dict[str, Any]:
    cases = _fixture_document(document)["cases"]
    return cast(dict[str, Any], next(case for case in cases if case["id"] == case_id))


def _request_from_case(case: dict[str, Any]) -> dict[str, Any]:
    request_case = case.get("request_case")
    if request_case:
        return cast(dict[str, Any], _fixture_case("requests.json", request_case)["wire"])
    return {
        "type": "req",
        "id": case["id"],
        "method": SESSIONS_LIST_METHOD,
        "params": case.get("request"),
    }


def _principal(spec: dict[str, Any]) -> Principal:
    authenticated = bool(spec.get("authenticated", True))
    return Principal(
        role=str(spec.get("role", "operator")),
        scopes=frozenset(str(scope) for scope in spec.get("scopes", [])),
        is_owner=bool(spec.get("is_owner", authenticated)),
        authenticated=authenticated,
        capabilities=(frozenset() if authenticated else frozenset({"guest.safe"})),
        auth_state=str(spec.get("auth_state", "authenticated")),
        guest_owner_id=spec.get("guest_owner_id"),
    )


class _LegacyListStorage:
    """The pre-Contract storage seam: list only, without paging or count helpers."""

    def __init__(
        self,
        sessions: list[Any] | None = None,
        agent_tasks: dict[str, list[Any]] | None = None,
    ) -> None:
        self.sessions = sessions or []
        self.agent_tasks = agent_tasks or {}
        self.list_calls: list[tuple[int, str | None]] = []

    async def list_sessions(
        self,
        *,
        limit: int,
        guest_owner_id: str | None = None,
    ) -> list[Any]:
        normalized_limit = int(limit)
        self.list_calls.append((normalized_limit, guest_owner_id))
        rows = self.sessions
        if guest_owner_id is not None:
            marker = f":webchat:guest:{guest_owner_id}:"
            rows = [row for row in rows if marker in row.session_key]
        return rows[:normalized_limit]

    async def get_transcript(self, _session_id: str, *, limit: int) -> list[Any]:
        del limit
        return []

    async def count_transcript_entries(self, _session_id: str) -> int:
        return 0

    async def list_agent_tasks(self, *, session_key: str) -> list[Any]:
        return list(self.agent_tasks.get(session_key, []))


def _result(*, future: object | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "sessions": [],
        "count": 0,
        "ts": 1_700_000_000_000,
    }
    if future is not None:
        result["future"] = future
    return result


def test_production_registry_uses_contract_adapter_as_sole_handler() -> None:
    entry = get_dispatcher().get_entry(SESSIONS_LIST_METHOD)

    assert entry is not None
    assert entry.required_scope == SESSIONS_LIST_SCOPE
    assert entry.handler is _handle_sessions_list_contract
    assert entry.handler is not _handle_sessions_list


def test_gateway_adapter_uses_the_full_generated_method_descriptor() -> None:
    descriptor = GATEWAY_METHOD_CONTRACTS[SESSIONS_LIST_METHOD]

    assert sessions_list_gateway_adapter._SESSIONS_LIST_DESCRIPTOR is descriptor
    assert descriptor.guest_allowed is True


def test_every_behavior_fixture_case_has_an_executable_oracle() -> None:
    fixture_ids = {
        str(case["id"])
        for case in _fixture_document("behavior.json")["cases"]
    }
    executed_ids = {
        "behavior.operator-legacy",
        "behavior.guest-filter-before-limit",
        "behavior.guest-limit-clamp-low",
        "behavior.guest-limit-clamp-high",
        "behavior.no-scope",
        "behavior.page-first",
        "behavior.page-middle",
        "behavior.page-last",
        "behavior.legacy-storage-terminal-page",
        "behavior.unknown-view-legacy",
    }

    assert fixture_ids == executed_ids


@pytest.mark.asyncio
async def test_behavior_oracle_executes_operator_and_scope_expectations() -> None:
    operator_case = _fixture_case("behavior.json", "behavior.operator-legacy")
    operator_request = _request_from_case(operator_case)
    operator_storage = _LegacyListStorage()
    operator_response = await get_dispatcher().dispatch(
        str(operator_request["id"]),
        str(operator_request["method"]),
        operator_request.get("params"),
        RpcContext(
            conn_id="behavior.operator-legacy",
            principal=_principal(operator_case["principal"]),
            session_manager=SimpleNamespace(storage=operator_storage),
        ),
    )
    operator_expected = operator_case["expected"]

    assert operator_response.ok is operator_expected["authorized"]
    assert bool(operator_storage.list_calls) is operator_expected["handler_invoked"]

    denied_case = _fixture_case("behavior.json", "behavior.no-scope")
    denied_response = await get_dispatcher().dispatch(
        denied_case["id"],
        SESSIONS_LIST_METHOD,
        None,
        RpcContext(
            conn_id=denied_case["id"],
            principal=_principal(denied_case["principal"]),
        ),
    )
    expected_error = _fixture_case(
        "errors.json",
        str(denied_case["expected_error_case"]),
    )["wire"]["error"]

    assert denied_response.ok is False
    assert denied_response.error.code == expected_error["code"]
    assert denied_response.error.message == expected_error["message"]


@pytest.mark.asyncio
async def test_behavior_oracle_executes_guest_filter_before_limit(tmp_path: Path) -> None:
    case = _fixture_case("behavior.json", "behavior.guest-filter-before-limit")
    storage = await SessionStorage.open(str(tmp_path / "behavior-guest-filter.db"))
    try:
        keys = case["setup"]["session_keys_newest_first"]
        for index, session_key in enumerate(reversed(keys), start=1):
            await storage.upsert_session(
                SessionNode(
                    session_key=session_key,
                    session_id=f"behavior-guest-{index}",
                    agent_id="main",
                    created_at=index,
                    updated_at=index,
                    started_at=index,
                    status=SessionStatus.DONE,
                )
            )
        response = await get_dispatcher().dispatch(
            case["id"],
            SESSIONS_LIST_METHOD,
            case["request"],
            RpcContext(
                conn_id=case["id"],
                principal=_principal(case["principal"]),
                session_manager=SimpleNamespace(storage=storage),
            ),
        )
    finally:
        await storage.close()

    expected = case["expected"]
    assert response.ok is True
    assert [row["key"] for row in response.payload["sessions"]] == expected["session_keys"]
    assert response.payload["count"] == expected["count"]


@pytest.mark.parametrize(
    "case_id",
    [
        "behavior.guest-limit-clamp-low",
        "behavior.guest-limit-clamp-high",
    ],
)
@pytest.mark.asyncio
async def test_behavior_oracle_executes_guest_limit_clamps(case_id: str) -> None:
    case = _fixture_case("behavior.json", case_id)
    guest_case = _fixture_case("behavior.json", "behavior.guest-filter-before-limit")
    storage = _LegacyListStorage()

    response = await get_dispatcher().dispatch(
        case["id"],
        SESSIONS_LIST_METHOD,
        case["request"],
        RpcContext(
            conn_id=case["id"],
            principal=_principal(guest_case["principal"]),
            session_manager=SimpleNamespace(storage=storage),
        ),
    )

    assert response.ok is True
    assert storage.list_calls[-1][0] == case["expected"]["storage_limit"]


@pytest.mark.asyncio
async def test_behavior_oracle_executes_exact_pagination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _fixture_document("behavior.json")
    setup = document["pagination_setup"]
    cases = {str(case["id"]): case for case in document["cases"]}
    storage = await SessionStorage.open(str(tmp_path / "behavior-pagination.db"))
    try:
        for raw in setup["sessions"]:
            timestamp = int(raw["updated_at"])
            await storage.upsert_session(
                SessionNode(
                    session_key=raw["key"],
                    session_id=str(raw["key"]).rsplit(":", 1)[-1],
                    agent_id="main",
                    created_at=timestamp,
                    updated_at=timestamp,
                    started_at=timestamp,
                    status=SessionStatus.DONE,
                )
            )
        monkeypatch.setattr(
            rpc_sessions.time,
            "time",
            lambda: int(setup["clock_ms"]) / 1000,
        )
        ctx = RpcContext(
            conn_id="behavior-pagination",
            principal=Principal(
                role="operator",
                scopes=frozenset({"operator.admin"}),
                is_owner=True,
                authenticated=True,
            ),
            session_manager=SimpleNamespace(storage=storage),
        )

        for case_id in (
            "behavior.page-first",
            "behavior.page-middle",
            "behavior.page-last",
        ):
            case = cases[case_id]
            response = await get_dispatcher().dispatch(
                case_id,
                SESSIONS_LIST_METHOD,
                case["request"],
                ctx,
            )
            expected = case["expected"]

            assert response.ok is True
            assert [row["key"] for row in response.payload["sessions"]] == expected[
                "session_keys"
            ]
            for field, value in expected.items():
                if field != "session_keys":
                    assert response.payload[field] == value, (case_id, field)
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_behavior_oracle_executes_legacy_terminal_and_unknown_view() -> None:
    session = SessionNode(
        session_key="agent:main:webchat:legacy",
        session_id="legacy",
        agent_id="main",
        created_at=1,
        updated_at=1,
        started_at=1,
        status=SessionStatus.DONE,
    )
    storage = _LegacyListStorage([session])
    ctx = RpcContext(
        conn_id="behavior-legacy-storage",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
        session_manager=SimpleNamespace(storage=storage),
    )

    terminal_case = _fixture_case(
        "behavior.json",
        "behavior.legacy-storage-terminal-page",
    )
    terminal_response = await get_dispatcher().dispatch(
        terminal_case["id"],
        SESSIONS_LIST_METHOD,
        terminal_case["request"],
        ctx,
    )
    assert terminal_response.ok is True
    for field, value in terminal_case["expected"].items():
        assert terminal_response.payload[field] == value

    unknown_case = _fixture_case("behavior.json", "behavior.unknown-view-legacy")
    unknown_request = _request_from_case(unknown_case)
    unknown_response = await get_dispatcher().dispatch(
        unknown_case["id"],
        SESSIONS_LIST_METHOD,
        unknown_request["params"],
        ctx,
    )
    assert unknown_response.ok is True
    expected = unknown_case["expected"]
    for field in expected["payload_has_fields"]:
        assert field in unknown_response.payload
    for field in expected["payload_omits_fields"]:
        assert field not in unknown_response.payload


@pytest.mark.asyncio
async def test_current_nonempty_task_parent_golden_comes_from_production_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _fixture_case("responses.json", "response.current-task-parent")
    setup = case["setup"]
    session = SessionNode(**setup["session"])
    task = SimpleNamespace(**setup["task"])
    storage = _LegacyListStorage(
        [session],
        agent_tasks={session.session_key: [task]},
    )
    monkeypatch.setattr(
        rpc_sessions.time,
        "time",
        lambda: int(setup["clock_ms"]) / 1000,
    )

    response = await get_dispatcher().dispatch(
        case["id"],
        SESSIONS_LIST_METHOD,
        None,
        RpcContext(
            conn_id=case["id"],
            principal=Principal(
                role="operator",
                scopes=frozenset({"operator.admin"}),
                is_owner=True,
                authenticated=True,
            ),
            session_manager=SimpleNamespace(storage=storage),
        ),
    )

    assert response.ok is True
    assert response.payload == case["wire"]["payload"]


@pytest.mark.parametrize(
    "params",
    [
        None,
        {},
        {"limit": 5, "view": "session-list-v-next", "future": True},
        [],
        [1],
        {"limit": True},
    ],
)
def test_request_adapter_preserves_published_v4_shapes(params: Any) -> None:
    assert sessions_list_params_contract_errors(params) == ()


def test_request_contract_reports_drift_without_echoing_input_values() -> None:
    errors = sessions_list_params_contract_errors({"limit": {"future": "value"}})

    assert errors
    assert all("input" not in error for error in errors)


def test_result_adapter_validates_without_rebuilding_json_tree() -> None:
    payload = {
        "sessions": [
            {
                "key": "agent:main:webchat:default",
                "origin": {"extension": {"future": True}},
                "future_row_field": [1, None, "x"],
            }
        ],
        "count": 1,
        "ts": 1_700_000_000_000,
        "future_result_field": {"enabled": True},
    }

    assert validate_sessions_list_result(payload) is payload


def test_result_adapter_rejects_non_contract_payload() -> None:
    with pytest.raises(SessionsListContractError):
        validate_sessions_list_result({"sessions": []})


@pytest.mark.asyncio
async def test_gateway_adapter_delegates_once_and_preserves_result_identity() -> None:
    registry = RpcRegistry()
    observed: list[Any] = []
    expected = _result(future={"kept": True})

    async def implementation(params: Any, _ctx: RpcContext) -> dict[str, Any]:
        observed.append(params)
        return expected

    handler = register_sessions_list_contract(
        registry,
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )
    params = {"limit": "5", "future": True}
    result = await handler(params, cast(RpcContext, object()))

    assert observed == [params]
    assert result is expected
    assert registry.get_entry(SESSIONS_LIST_METHOD).handler is handler  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_gateway_adapter_observes_request_drift_but_preserves_legacy_behavior() -> None:
    registry = RpcRegistry()
    observed: list[Any] = []
    expected = _result()

    async def implementation(params: Any, _ctx: RpcContext) -> dict[str, Any]:
        observed.append(params)
        return expected

    handler = register_sessions_list_contract(
        registry,
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )
    params = {"limit": {"legacy": True}}
    with structlog.testing.capture_logs() as logs:
        result = await handler(params, cast(RpcContext, object()))

    assert result is expected
    assert observed == [params]
    mismatch = [
        entry for entry in logs if entry.get("event") == "sessions.list.request_contract_mismatch"
    ]
    assert len(mismatch) == 1
    assert mismatch[0]["params_type"] == "dict"
    assert mismatch[0]["errors"]


@pytest.mark.asyncio
async def test_gateway_adapter_maps_invalid_implementation_result() -> None:
    registry = RpcRegistry()

    async def implementation(_params: Any, _ctx: RpcContext) -> dict[str, Any]:
        return {"sessions": []}

    handler = register_sessions_list_contract(
        registry,
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )

    with pytest.raises(RpcHandlerError) as error:
        await handler(None, cast(RpcContext, object()))

    assert error.value.code == "INTERNAL_ERROR"
    assert error.value.message == "sessions.list response violated its v4 contract"


@pytest.mark.asyncio
async def test_new_python_adapter_calls_legacy_raw_gateway_shape() -> None:
    """The new client Adapter emits the exact method/params an old Gateway expects."""

    request = _fixture_case("requests.json", "request.legacy-limit")["wire"]
    response = _fixture_case("responses.json", "response.empty-legacy")["wire"]
    calls: list[tuple[str, dict[str, Any] | None]] = []
    expected = response["payload"]

    async def caller(method: str, params: dict[str, Any] | None) -> Any:
        calls.append((method, params))
        return expected

    result = await call_sessions_list(caller, limit=request["params"]["limit"])

    assert calls == [(request["method"], request["params"])]
    assert result is expected


@pytest.mark.asyncio
async def test_new_python_adapter_calls_unwrapped_legacy_implementation() -> None:
    """The Adapter works with the unchanged pre-registration Implementation seam."""

    storage = _LegacyListStorage()
    ctx = RpcContext(
        conn_id="legacy-implementation",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
        session_manager=SimpleNamespace(storage=storage),
    )

    async def legacy_caller(method: str, params: dict[str, Any] | None) -> Any:
        assert method == SESSIONS_LIST_METHOD
        return await _handle_sessions_list(params, ctx)

    result = await call_sessions_list(legacy_caller, limit=50)

    assert storage.list_calls == [(50, None)]
    assert result["sessions"] == []
    assert result["count"] == 0
    assert isinstance(result["ts"], int)


@pytest.mark.asyncio
async def test_legacy_sessions_list_rejects_negative_limit_before_storage() -> None:
    storage = _LegacyListStorage()
    ctx = RpcContext(
        conn_id="invalid-limit",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
        session_manager=SimpleNamespace(storage=storage),
    )

    with pytest.raises(ValueError, match=r"params\.limit must be >= 1"):
        await _handle_sessions_list({"limit": -1}, ctx)

    assert storage.list_calls == []


@pytest.mark.parametrize(
    ("legacy_surface", "params", "expected_page_aliases"),
    [
        (
            "webui",
            {"limit": 200, "view": "session-list-v1"},
            {
                "has_more": False,
                "hasMore": False,
                "next_cursor": None,
                "nextCursor": None,
            },
        ),
        (
            "webui-count",
            {"limit": 200, "view": "session-count-v1"},
            {"totalCount": 0, "total_count": 0},
        ),
        ("cli", {"limit": 50}, {}),
        ("mcp", {"limit": 10}, {}),
    ],
)
def test_legacy_raw_clients_call_new_gateway_over_real_asgi_websocket(
    legacy_surface: str,
    params: dict[str, Any],
    expected_page_aliases: dict[str, Any],
) -> None:
    """Published old WebUI/CLI/MCP frames remain compatible end to end."""

    def receive_response(websocket: Any, request_id: str) -> dict[str, Any]:
        # The v4 server may advertise a hello event around authentication.
        # Legacy clients already ignore unrelated events while awaiting an id.
        for _ in range(4):
            frame = websocket.receive_json()
            if frame.get("type") == "res" and frame.get("id") == request_id:
                return cast(dict[str, Any], frame)
        raise AssertionError(f"response {request_id!r} was not received")

    app = create_gateway_app(GatewayConfig(ws_writer_queue_enabled=False))
    with TestClient(
        app,
        base_url="http://127.0.0.1:18791",
        client=("127.0.0.1", 51000),
    ) as client:
        with client.websocket_connect("/ws") as websocket:
            challenge = websocket.receive_json()
            assert challenge["event"] == "connect.challenge"
            websocket.send_json(
                {
                    "type": "req",
                    "id": "connect",
                    "method": "connect",
                    "params": {"minProtocol": 1, "role": "operator", "auth": {}},
                }
            )
            connected = websocket.receive_json()
            assert connected["type"] == "hello-ok"
            assert connected["protocol"] == 4

            websocket.send_json(
                {
                    "type": "req",
                    "id": f"old-{legacy_surface}-list",
                    "method": "sessions.list",
                    "params": params,
                }
            )
            response = receive_response(websocket, f"old-{legacy_surface}-list")

    assert response["type"] == "res"
    assert response["id"] == f"old-{legacy_surface}-list"
    assert response["ok"] is True
    assert response["error"] is None
    assert response["payload"]["sessions"] == []
    assert response["payload"]["count"] == 0
    assert isinstance(response["payload"]["ts"], int)
    for field, value in expected_page_aliases.items():
        assert response["payload"][field] == value


def test_legacy_raw_http_client_calls_new_gateway_end_to_end() -> None:
    """The published GET /api/sessions surface reaches the sole new handler."""

    app = create_gateway_app(GatewayConfig())
    with TestClient(app) as client:
        response = client.get("/api/sessions", params={"limit": 50})

    assert response.status_code == 200
    assert response.json()["sessions"] == []
    assert response.json()["count"] == 0
    assert isinstance(response.json()["ts"], int)
