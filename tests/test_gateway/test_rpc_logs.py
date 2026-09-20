from __future__ import annotations

import json
import threading

import pytest

import opensquilla.gateway.rpc_logs as rpc_logs
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.diagnostics import DiagnosticsState
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.rpc_logs import _handle_logs_status, _handle_logs_tail
from opensquilla.gateway.scopes import (
    ADMIN_SCOPE,
    METHOD_SCOPES,
    READ_SCOPE,
    REMOTE_OPERATOR_SCOPES,
    WRITE_SCOPE,
    authorize_call,
)
from opensquilla.observability.trace import TraceContext, TraceEvent, write_trace_event


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "loader_name", "params"),
    [
        ("logs.trace", "load_trace_events", {"trace_id": "trace-worker"}),
        ("logs.trace_projection", "load_trace_events", {"trace_id": "trace-worker"}),
        ("logs.trace_details", "load_turn_call_records", {"trace_id": "trace-worker"}),
        (
            "logs.trace_payload",
            "load_turn_call_records",
            {"trace_id": "trace-worker", "seq": 1},
        ),
    ],
)
async def test_trace_rpc_file_reads_use_worker_thread(
    method, loader_name, params, monkeypatch,
) -> None:
    event_loop_thread = threading.get_ident()
    loader_threads: list[int] = []

    def fake_loader(trace_id: str) -> list:
        assert trace_id == "trace-worker"
        loader_threads.append(threading.get_ident())
        return []

    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG", "1")
    monkeypatch.setattr(rpc_logs, loader_name, fake_loader)
    response = await get_dispatcher().dispatch(
        "req-worker",
        method,
        params,
        RpcContext(
            conn_id="test",
            principal=Principal(
                role="operator",
                scopes=frozenset({ADMIN_SCOPE}),
                is_owner=True,
                authenticated=True,
            ),
        ),
    )

    assert response.ok is True
    assert loader_threads and all(thread != event_loop_thread for thread in loader_threads)


@pytest.mark.asyncio
async def test_logs_tail_uses_opensquilla_log_dir_and_filters_level(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    log_file = tmp_path / "debug.log"
    log_file.write_text(
        "2026-05-03 [DEBUG] opensquilla: ignored\n"
        "2026-05-03 [INFO] opensquilla: selected\n",
        encoding="utf-8",
    )

    result = await _handle_logs_tail({"limit": 10, "cursor": 0, "level": "INFO"}, None)  # type: ignore[arg-type]

    assert result["lines"] == ["2026-05-03 [INFO] opensquilla: selected"]
    assert result["cursor"] == log_file.stat().st_size
    assert result["has_more"] is False


@pytest.mark.asyncio
async def test_logs_tail_missing_file_returns_empty_payload(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))

    result = await _handle_logs_tail({"limit": 10, "cursor": 0}, None)  # type: ignore[arg-type]

    assert result == {"lines": [], "cursor": 0, "has_more": False}


@pytest.mark.asyncio
async def test_logs_status_reports_raw_capture_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_TURN_CALL_LOG", raising=False)
    monkeypatch.delenv("OPENSQUILLA_TURN_CALL_LOG_DIR", raising=False)
    monkeypatch.delenv("OPENSQUILLA_LOG_DIR", raising=False)

    result = await _handle_logs_status({}, RpcContext(conn_id="test", config=GatewayConfig()))

    assert result["raw_turn_call_log"]["enabled"] is False
    assert result["raw_turn_call_log"]["source"] == "off"
    assert result["raw_turn_call_log"]["enable_env"]["set"] is False
    assert result["raw_turn_call_log"]["enable_env"]["truthy"] is False
    assert result["raw_turn_call_log"]["directory"]["source"] == "default"
    assert result["diagnostics_enabled"]["configured"] is False
    assert result["diagnostics_enabled"]["effective"] is False
    assert result["diagnostics_enabled"]["detail"] == "off"
    assert result["diagnostics_enabled"]["controls_raw_turn_call"] is False


@pytest.mark.asyncio
async def test_logs_status_reports_truthy_and_falsy_raw_capture_env(monkeypatch) -> None:
    for value in ("1", "TRUE", " yes ", "on"):
        monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG", value)
        result = await _handle_logs_status({}, RpcContext(conn_id="test", config=GatewayConfig()))
        assert result["raw_turn_call_log"]["enabled"] is True
        assert result["raw_turn_call_log"]["source"] == "env"
        assert result["raw_turn_call_log"]["enable_env"]["truthy"] is True

    for value in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG", value)
        result = await _handle_logs_status({}, RpcContext(conn_id="test", config=GatewayConfig()))
        assert result["raw_turn_call_log"]["enabled"] is False
        assert result["raw_turn_call_log"]["source"] == "off"
        assert result["raw_turn_call_log"]["enable_env"]["truthy"] is False


@pytest.mark.asyncio
async def test_logs_status_reports_runtime_raw_capture_source(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_TURN_CALL_LOG", raising=False)
    state = DiagnosticsState.from_config(GatewayConfig())
    state.set_runtime(enabled=True, raw=True)

    result = await _handle_logs_status(
        {},
        RpcContext(conn_id="test", config=GatewayConfig(), diagnostics_state=state),
    )

    assert result["raw_turn_call_log"]["enabled"] is True
    assert result["raw_turn_call_log"]["source"] == "runtime"
    assert result["diagnostics_enabled"]["effective"] is True
    assert result["diagnostics_enabled"]["detail"] == "raw"


@pytest.mark.asyncio
async def test_logs_status_reports_env_source_when_env_and_runtime_raw_are_enabled(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG", "1")
    state = DiagnosticsState.from_config(GatewayConfig())
    state.set_runtime(enabled=True, raw=True)

    result = await _handle_logs_status(
        {},
        RpcContext(conn_id="test", config=GatewayConfig(), diagnostics_state=state),
    )

    assert result["raw_turn_call_log"]["enabled"] is True
    assert result["raw_turn_call_log"]["source"] == "env"
    assert result["diagnostics_enabled"]["raw_source"] == "env"


@pytest.mark.asyncio
async def test_logs_status_resolves_raw_directory_precedence_without_creating_paths(
    tmp_path, monkeypatch
) -> None:
    raw_dir = tmp_path / "raw"
    shared_log_dir = tmp_path / "shared"
    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG_DIR", str(raw_dir))
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(shared_log_dir))

    result = await _handle_logs_status({}, RpcContext(conn_id="test", config=GatewayConfig()))

    assert result["raw_turn_call_log"]["directory"] == {
        "path": str(raw_dir),
        "source": "OPENSQUILLA_TURN_CALL_LOG_DIR",
        "exists": False,
    }
    assert not raw_dir.exists()
    assert not shared_log_dir.exists()

    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG_DIR", " ")
    result = await _handle_logs_status({}, RpcContext(conn_id="test", config=GatewayConfig()))

    assert result["raw_turn_call_log"]["directory"] == {
        "path": str(shared_log_dir),
        "source": "OPENSQUILLA_LOG_DIR",
        "exists": False,
    }
    assert not raw_dir.exists()
    assert not shared_log_dir.exists()


@pytest.mark.asyncio
async def test_logs_status_reports_gateway_file_log_path_and_existence(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    log_file = tmp_path / "debug.log"
    log_file.write_text("2026-05-03 [INFO] opensquilla: selected\n", encoding="utf-8")
    config = GatewayConfig(log_file_enabled=False, log_level="INFO", diagnostics_enabled=True)

    result = await _handle_logs_status({}, RpcContext(conn_id="test", config=config))

    assert result["gateway_file_log"]["enabled"] is False
    assert result["gateway_file_log"]["level"] == "INFO"
    assert result["gateway_file_log"]["path"] == str(log_file)
    assert result["gateway_file_log"]["path_source"] == "OPENSQUILLA_LOG_DIR"
    assert result["gateway_file_log"]["exists"] is True
    assert result["gateway_file_log"]["active_tail_path"] == str(log_file)
    assert result["gateway_file_log"]["active_tail_path_exists"] is True
    assert result["diagnostics_enabled"]["configured"] is True
    assert result["diagnostics_enabled"]["effective"] is True
    assert result["diagnostics_enabled"]["detail"] == "standard"
    assert result["diagnostics_enabled"]["controls_raw_turn_call"] is False


@pytest.mark.asyncio
async def test_logs_status_reports_trace_log_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    write_trace_event(
        TraceEvent(kind="turn_start", context=TraceContext.new(trace_id="trace-1")),
        log_dir=tmp_path,
    )

    result = await _handle_logs_status({}, RpcContext(conn_id="test", config=GatewayConfig()))

    assert result["trace_log"] == {
        "directory": {
            "path": str(tmp_path),
            "source": "OPENSQUILLA_LOG_DIR",
            "exists": True,
        },
        "file_count": 1,
        "latest_path": str(next(tmp_path.glob("traces-*.jsonl"))),
    }


@pytest.mark.asyncio
async def test_logs_trace_returns_persisted_trace_events(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    write_trace_event(
        TraceEvent(
            kind="turn_start",
            context=TraceContext.new(
                trace_id="trace-1",
                session_key="agent:main:test",
                turn_id="turn-1",
            ),
            seq=1,
        ),
        log_dir=tmp_path,
    )
    ctx = RpcContext(conn_id="test", config=GatewayConfig())

    response = await get_dispatcher().dispatch(
        "req-1", "logs.trace", {"trace_id": "trace-1"}, ctx
    )

    assert response.ok is True
    assert response.payload["trace_id"] == "trace-1"
    assert response.payload["count"] == 1
    assert response.payload["events"][0]["kind"] == "turn_start"
    assert response.payload["events"][0]["session_key"] == "agent:main:test"


@pytest.mark.asyncio
async def test_logs_trace_projection_returns_cursorable_safe_projection(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    context = TraceContext.new(trace_id="trace-projection", turn_id="turn-1")
    write_trace_event(TraceEvent(kind="turn_start", context=context, seq=1), log_dir=tmp_path)
    write_trace_event(
        TraceEvent(
            kind="route.resolved",
            context=context,
            seq=2,
            attrs={"requested_mode": "router", "effective_mode": "direct"},
        ),
        log_dir=tmp_path,
    )
    write_trace_event(TraceEvent(kind="turn_end", context=context, seq=3), log_dir=tmp_path)

    response = await get_dispatcher().dispatch(
        "req-1",
        "logs.trace_projection",
        {"trace_id": "trace-projection", "after_seq": 1},
        RpcContext(conn_id="test", config=GatewayConfig()),
    )

    assert response.ok is True
    assert response.payload["status"] == "success"
    assert response.payload["requested_mode"] == "router"
    assert [row["seq"] for row in response.payload["spans"]] == [2, 3]


@pytest.mark.asyncio
async def test_logs_trace_projection_view_is_additive_to_logs_trace(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    write_trace_event(
        TraceEvent(kind="turn_start", context=TraceContext.new(trace_id="trace-view"), seq=1),
        log_dir=tmp_path,
    )

    response = await get_dispatcher().dispatch(
        "req-1",
        "logs.trace",
        {"trace_id": "trace-view", "view": "projection"},
        RpcContext(conn_id="test", config=GatewayConfig()),
    )

    assert response.ok is True
    assert response.payload["trace_id"] == "trace-view"
    assert "spans" in response.payload
    assert "events" not in response.payload


@pytest.mark.asyncio
async def test_logs_turn_traces_resolves_running_and_historical_turn(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("OPENSQUILLA_TURN_CALL_LOG", raising=False)
    context = TraceContext.new(
        trace_id="trace-chat", session_key="session-chat", turn_id="turn-chat"
    )
    ctx = RpcContext(conn_id="test")
    params = {"session_key": "session-chat", "turn_id": "turn-chat"}

    empty = await get_dispatcher().dispatch("req-empty", "logs.turn_traces", params, ctx)
    assert empty.ok is True
    assert empty.payload["traces"] == []

    write_trace_event(TraceEvent(kind="turn_start", context=context, seq=1), log_dir=tmp_path)
    active = await get_dispatcher().dispatch("req-active", "logs.turn_traces", params, ctx)
    assert active.ok is True
    assert active.payload["raw_enabled"] is False
    assert active.payload["traces"][0]["trace_id"] == "trace-chat"
    assert active.payload["traces"][0]["complete"] is False

    write_trace_event(TraceEvent(kind="turn_end", context=context, seq=2), log_dir=tmp_path)
    historical = await get_dispatcher().dispatch("req-history", "logs.turn_traces", params, ctx)
    assert historical.payload["traces"][0]["complete"] is True
    assert historical.payload["traces"][0]["status"] == "success"


@pytest.mark.asyncio
async def test_logs_turn_traces_obeys_raw_capture_gate(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("OPENSQUILLA_TURN_CALL_LOG", raising=False)
    (tmp_path / "turn-calls-20260101.jsonl").write_text(
        json.dumps({
            "trace_id": "raw-trace",
            "turn_id": "turn-raw",
            "session_key": "session-raw",
            "kind": "turn_start",
            "ts": "2026-01-01T00:00:00Z",
        }),
        encoding="utf-8",
    )
    ctx = RpcContext(conn_id="test")
    params = {"session_key": "session-raw", "turn_id": "turn-raw"}
    disabled = await get_dispatcher().dispatch("req-off", "logs.turn_traces", params, ctx)
    assert disabled.payload["traces"] == []
    assert disabled.payload["raw_enabled"] is False

    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG", "1")
    enabled = await get_dispatcher().dispatch("req-on", "logs.turn_traces", params, ctx)
    assert enabled.payload["raw_enabled"] is True
    assert enabled.payload["traces"][0]["trace_id"] == "raw-trace"
    assert enabled.payload["traces"][0]["raw_available"] is True


@pytest.mark.asyncio
async def test_logs_turn_traces_requires_operator_read() -> None:
    response = await get_dispatcher().dispatch(
        "req-scope",
        "logs.turn_traces",
        {"session_key": "session-test", "turn_id": "turn-test"},
        RpcContext(
            conn_id="test",
            principal=Principal(
                role="operator", scopes=frozenset(), is_owner=False, authenticated=True
            ),
        ),
    )
    assert response.ok is False
    assert response.error is not None
    assert "Insufficient scope" in response.error.message


@pytest.mark.asyncio
async def test_logs_status_is_mounted_on_dispatcher(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_TURN_CALL_LOG", raising=False)
    ctx = RpcContext(conn_id="test", config=GatewayConfig())

    response = await get_dispatcher().dispatch("req-1", "logs.status", {}, ctx)

    assert response.ok is True
    assert isinstance(response.payload, dict)
    assert response.payload["raw_turn_call_log"]["enabled"] is False


@pytest.mark.asyncio
async def test_logs_trace_details_exposes_redacted_boundary_payload_with_raw_gate(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG", "0")
    record = {
        "trace_id": "trace-routing", "seq": 1, "kind": "routing_decision", "elapsed_ms": 30,
        "payload": {
            "requested_mode": "smart", "effective_mode": "direct",
            "selected_model": "test-model", "api_key": "synthetic-key-for-redaction",
        },
    }
    (tmp_path / "turn-calls-20260101.jsonl").write_text(json.dumps(record), encoding="utf-8")
    ctx = RpcContext(conn_id="test")
    params = {"trace_id": "trace-routing"}

    disabled = await get_dispatcher().dispatch("req-off", "logs.trace_details", params, ctx)
    assert disabled.ok is True
    assert disabled.payload["available"] is False
    assert disabled.payload["rows"] == []

    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG", "1")
    enabled = await get_dispatcher().dispatch("req-on", "logs.trace_details", params, ctx)
    assert enabled.ok is True
    [row] = enabled.payload["rows"]
    assert row["kind"] == "routing_decision"
    assert row["status"] == "success"
    assert row["output"]["selected_model"] == "test-model"
    assert row["output"]["api_key"] == "[REDACTED]"
    assert "duration_ms" not in row

    raw = await get_dispatcher().dispatch(
        "req-full", "logs.trace_payload", {**params, "seq": 1}, ctx,
    )
    assert raw.ok is True
    assert raw.payload["payload"]["payload"]["api_key"] == "[REDACTED]"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["logs.trace_details", "logs.trace_payload"])
async def test_raw_trace_rpc_requires_admin_scope(method, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG", "1")
    record = {
        "trace_id": "trace-admin-scope",
        "seq": 1,
        "kind": "routing_decision",
        "elapsed_ms": 1,
        "payload": {"selected_model": "synthetic-model"},
    }
    (tmp_path / "turn-calls-20260101.jsonl").write_text(json.dumps(record), encoding="utf-8")
    dispatcher = get_dispatcher()
    entry = dispatcher.get_entry(method)
    assert entry is not None
    assert METHOD_SCOPES[method] == entry.required_scope == ADMIN_SCOPE

    params = {"trace_id": "trace-admin-scope", "seq": 1}
    for scopes in (
        frozenset({READ_SCOPE}),
        frozenset({WRITE_SCOPE}),
        REMOTE_OPERATOR_SCOPES,
    ):
        assert authorize_call(method, ADMIN_SCOPE, "operator", scopes) == (False, ADMIN_SCOPE)
        denied = await dispatcher.dispatch(
            "req-denied",
            method,
            params,
            RpcContext(
                conn_id="remote-test",
                principal=Principal(
                    role="operator",
                    scopes=scopes,
                    is_owner=False,
                    authenticated=True,
                ),
            ),
        )
        assert denied.ok is False
        assert denied.error is not None
        assert denied.error.code == "UNAUTHORIZED"

    allowed = await dispatcher.dispatch(
        "req-admin",
        method,
        params,
        RpcContext(
            conn_id="admin-test",
            principal=Principal(
                role="operator",
                scopes=frozenset({ADMIN_SCOPE}),
                is_owner=False,
                authenticated=True,
            ),
        ),
    )
    assert allowed.ok is True
    assert allowed.payload["available"] is True
