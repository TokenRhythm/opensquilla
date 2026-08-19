from __future__ import annotations

import asyncio
import builtins
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.channels.types import (
    AuthenticatedPrincipal,
    IncomingMessage,
    IngressProvenance,
    IngressVerification,
)
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import AgentConfig, DoneEvent
from opensquilla.gateway.boot import (
    _configured_agent_ids,
    _gateway_home,
    _register_dream_crons,
    _sandbox_settings_for_runtime,
    _task_runtime_envelope_owner,
    _task_runtime_turn_hard_deadline_s,
    _warn_workspace_state_mismatch,
    build_flush_service,
    build_services,
    build_task_runtime_run_kwargs,
    dispatch_task_runtime_turn,
    emit_skill_filter_banner,
    validate_squilla_router_runtime,
)
from opensquilla.gateway.channel_dispatch import _stamp_channel_admin_principal
from opensquilla.gateway.config import (
    AgentEntryConfig,
    GatewayConfig,
    effective_agent_stream_idle_timeout_seconds,
    effective_webui_stream_idle_grace_seconds,
)
from opensquilla.gateway.diagnostics import DiagnosticsState
from opensquilla.gateway.model_routing import (
    capture_model_routing_config,
    model_routing_snapshot,
)
from opensquilla.gateway.routing import (
    build_channel_route_envelope,
    build_cli_route_envelope,
    build_cron_route_envelope,
    tool_context_from_envelope,
)
from opensquilla.onboarding.mutations import upsert_channel
from opensquilla.project_workspaces import (
    ProjectWorkspaceStateError,
    project_path_key,
)
from opensquilla.provider import Message, ProviderRequestCorrelation
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.run_context import RUN_CONTEXT_ORIGIN_KEY
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.scheduler.types import CronJob, JobStatus
from opensquilla.session.compaction import CompactionConfig
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionIntent
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import CallerKind, ToolContext, ToolSpec


def test_gateway_boot_bridges_compaction_notifications_to_session_stream() -> None:
    source = Path("src/opensquilla/gateway/boot.py").read_text(encoding="utf-8")

    assert "add_compaction_listener" in source
    assert '"session.event.compaction"' in source
    assert "_compaction_listener_remove" in source


def test_shared_service_boot_prewarms_tokenrhythm_install_id_after_config_load() -> None:
    source = Path("src/opensquilla/gateway/boot.py").read_text(encoding="utf-8")
    build_start = source.index("async def build_services(")
    config_load = source.index("GatewayConfig.load(", build_start)
    prewarm = source.index("_prewarm_tokenrhythm_install_id(config)", build_start)
    provider_setup = source.index("# ── Provider selector", build_start)
    live_catalog = source.index("await refresh_live_model_catalog(", build_start)

    # build_services is shared by Gateway, one-shot agents, and --standalone.
    assert config_load < prewarm < provider_setup < live_catalog


def test_tokenrhythm_install_id_prewarm_never_breaks_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import boot
    from opensquilla.provider import tokenrhythm_correlation

    def fail_prewarm(**_kwargs: Any) -> None:
        raise RuntimeError("synthetic resolver failure")

    monkeypatch.setattr(
        tokenrhythm_correlation,
        "prewarm_tokenrhythm_install_id",
        fail_prewarm,
    )

    boot._prewarm_tokenrhythm_install_id(GatewayConfig())


def test_gateway_startup_phase_log_uses_bounded_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import boot

    events: list[tuple[str, dict[str, Any]]] = []

    class FakeLog:
        def info(self, event: str, **kwargs: Any) -> None:
            events.append((event, kwargs))

    monkeypatch.setattr(boot, "log", FakeLog())
    ticks = iter((12.5, 12.75))
    monkeypatch.setattr(boot.time, "monotonic", lambda: next(ticks))

    completed_at = boot._log_gateway_startup_phase(
        "services",
        startup_started_at=10.0,
        phase_started_at=11.5,
    )

    assert completed_at == 12.75
    assert events == [
        (
            "gateway.startup_phase",
            {
                "phase": "services",
                "status": "ready",
                "duration_ms": 1000,
                "startup_elapsed_ms": 2500,
            },
        )
    ]


def test_uvicorn_listener_callback_observes_a_real_bound_socket() -> None:
    import httpx
    import uvicorn

    async def run_case() -> None:
        callback_called = asyncio.Event()
        callback_ports: list[int] = []
        server: uvicorn.Server

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            assert scope["type"] == "http"
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b""})

        async def listener_ready() -> None:
            assert server.started is True
            assert server.servers
            sockets = server.servers[0].sockets
            assert sockets
            callback_ports.append(int(sockets[0].getsockname()[1]))
            callback_called.set()

        config = uvicorn.Config(
            app=app,
            host="127.0.0.1",
            port=0,
            lifespan="off",
            access_log=False,
            log_level="warning",
            callback_notify=listener_ready,
        )
        server = uvicorn.Server(config)
        setattr(server, "install_signal_handlers", lambda: None)
        task = asyncio.create_task(server.serve())
        try:
            await asyncio.wait_for(callback_called.wait(), timeout=5)
            assert len(callback_ports) == 1
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.get(
                    f"http://127.0.0.1:{callback_ports[0]}/healthz",
                    timeout=5,
                )
            assert response.status_code == 204
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=5)

    asyncio.run(run_case())


def test_task_runtime_default_hard_deadline_is_unbounded() -> None:
    config = GatewayConfig()

    deadline = _task_runtime_turn_hard_deadline_s(config)

    assert deadline is None


def test_task_runtime_hard_deadline_honors_explicit_config() -> None:
    config = GatewayConfig()
    config.task_runtime.turn_hard_deadline_s = 12.5

    assert _task_runtime_turn_hard_deadline_s(config) == 12.5


def test_gateway_server_close_releases_pid_lock_when_shutdown_step_fails() -> None:
    from opensquilla.gateway import boot

    released: list[str] = []

    class FakePidLock:
        def release(self) -> None:
            released.append("released")

    class FailingChannelManager:
        async def stop_all(self) -> None:
            raise RuntimeError("channel stop failed")

    server = boot.GatewayServer(
        app=SimpleNamespace(),
        config=GatewayConfig(),
        _channel_manager=FailingChannelManager(),
        _pid_lock=FakePidLock(),
    )

    async def run_case() -> None:
        with pytest.raises(RuntimeError, match="channel stop failed"):
            await server.close()

        assert released == ["released"]
        assert server._pid_lock is None

    asyncio.run(run_case())


def test_gateway_server_incomplete_runtime_keeps_services_and_pid_lock_open() -> None:
    from opensquilla.gateway import boot
    from opensquilla.gateway.task_runtime import TaskRuntimeShutdownResult

    released: list[str] = []
    services_closed: list[str] = []
    expected = TaskRuntimeShutdownResult(
        clean=False,
        elapsed_ms=25,
        abandoned_task_count=1,
        remaining_driver_count=1,
        remaining_reservation_count=0,
        remaining_auxiliary_count=0,
    )

    class FakePidLock:
        def release(self) -> None:
            released.append("released")

    class FakeRuntime:
        async def shutdown(self, **_kwargs: Any) -> Any:
            return expected

    class FakeServices:
        task_runtime = FakeRuntime()
        goal_service = None

        async def close(self) -> None:
            services_closed.append("closed")

    pid_lock = FakePidLock()
    server = boot.GatewayServer(
        app=SimpleNamespace(),
        config=GatewayConfig(),
        _services=FakeServices(),  # type: ignore[arg-type]
        _pid_lock=pid_lock,
    )

    async def run_case() -> None:
        result = await server.close()

        assert result is expected
        assert services_closed == []
        assert released == []
        assert server._pid_lock is pid_lock

    asyncio.run(run_case())


def test_start_gateway_server_releases_pid_lock_when_build_services_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import boot

    events: list[str] = []

    async def fail_build_services(**_kwargs: Any) -> Any:
        events.append("build_services")
        raise RuntimeError("service construction failed")

    async def reconcile(_state_dir: object) -> int:
        events.append("reconcile_process_owners")
        return 0

    monkeypatch.setattr(
        boot,
        "_start_background_install_telemetry",
        lambda config: events.append("install_telemetry"),
    )
    monkeypatch.setattr(boot, "build_services", fail_build_services)
    monkeypatch.setattr(
        "opensquilla.process_tree.reconcile_persisted_processes",
        reconcile,
    )
    monkeypatch.setattr(boot, "_setup_file_logging", lambda config: None)
    monkeypatch.setattr(boot, "emit_skill_filter_banner", lambda config: None)
    monkeypatch.setattr(
        "opensquilla.gateway.pidlock.GatewayPidLock.acquire",
        lambda self: events.append("acquire"),
    )
    monkeypatch.setattr(
        "opensquilla.gateway.pidlock.GatewayPidLock.release",
        lambda self: events.append("release"),
    )
    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
    )

    async def run_case() -> None:
        with pytest.raises(RuntimeError, match="service construction failed"):
            await boot.start_gateway_server(config=config, run=False)

        assert events == [
            "acquire",
            "reconcile_process_owners",
            "build_services",
            "release",
        ]

    asyncio.run(run_case())


def test_failed_second_start_does_not_reset_active_stream_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import boot
    from opensquilla.gateway.session_streams import (
        get_session_streams,
        reset_session_streams,
    )

    active = reset_session_streams(stream_generation="active-generation")
    active.record(
        "agent:main:webchat:active",
        "session.event.text_delta",
        {"text": "still live"},
    )
    monkeypatch.setattr(boot, "_setup_file_logging", lambda config: None)
    monkeypatch.setattr(boot, "emit_skill_filter_banner", lambda config: None)

    def reject_second_owner(_self: Any) -> None:
        raise RuntimeError("gateway already owns pid lock")

    monkeypatch.setattr(
        "opensquilla.gateway.pidlock.GatewayPidLock.acquire",
        reject_second_owner,
    )
    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
    )

    async def run_case() -> None:
        with pytest.raises(RuntimeError, match="already owns pid lock"):
            await boot.start_gateway_server(config=config, run=False)

        assert get_session_streams() is active
        assert get_session_streams().stream_generation == "active-generation"
        assert get_session_streams().current_seq("agent:main:webchat:active") == 1

    try:
        asyncio.run(run_case())
    finally:
        reset_session_streams()


def test_failed_desktop_ownership_does_not_reset_active_stream_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import boot
    from opensquilla.gateway.session_streams import (
        get_session_streams,
        reset_session_streams,
    )

    events: list[str] = []
    active = reset_session_streams(stream_generation="active-generation")
    active.record(
        "agent:main:webchat:active",
        "session.event.text_delta",
        {"text": "still live"},
    )
    monkeypatch.setattr(boot, "_setup_file_logging", lambda config: None)
    monkeypatch.setattr(boot, "emit_skill_filter_banner", lambda config: None)
    monkeypatch.setattr(
        "opensquilla.gateway.pidlock.GatewayPidLock.acquire",
        lambda self: events.append("acquire"),
    )
    monkeypatch.setattr(
        "opensquilla.gateway.pidlock.GatewayPidLock.release",
        lambda self: events.append("release"),
    )

    def reject_desktop_owner(**_kwargs: Any) -> None:
        raise RuntimeError("desktop lifecycle already owned")

    monkeypatch.setattr(
        "opensquilla.gateway.desktop_ownership.activate_desktop_gateway_ownership",
        reject_desktop_owner,
    )
    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
    )

    async def run_case() -> None:
        with pytest.raises(RuntimeError, match="desktop lifecycle already owned"):
            await boot.start_gateway_server(config=config, run=True)

        assert events == ["acquire", "release"]
        assert get_session_streams() is active
        assert get_session_streams().stream_generation == "active-generation"
        assert get_session_streams().current_seq("agent:main:webchat:active") == 1

    try:
        asyncio.run(run_case())
    finally:
        reset_session_streams()


def test_start_gateway_server_starts_telemetry_after_listener_and_runtime_are_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import boot

    debug_logs: list[tuple[str, dict[str, Any]]] = []
    call_order: list[str] = []
    app_holder: dict[str, Any] = {}

    class FakeLog:
        def debug(self, event: str, **kwargs: Any) -> None:
            debug_logs.append((event, kwargs))

        def info(self, event: str, **kwargs: Any) -> None:
            if event != "gateway.startup_phase":
                return
            phase = kwargs.get("phase")
            if phase == "runtime_state":
                assert app_holder["app"].state.gateway_ready is True
                call_order.append("runtime_state")
            elif phase in {"listener", "gateway_ready"}:
                call_order.append(str(phase))

        def warning(self, _event: str, **_kwargs: Any) -> None:
            return None

    class FakeTurnRunner:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        def set_session_lock_provider(self, _provider: Any) -> None:
            return None

    class FakeUvicornServer:
        def __init__(self, config: Any) -> None:
            self.config = config
            self.should_exit = False

        async def serve(self) -> None:
            call_order.append("listener_callback")
            await self.config.callback_notify()

    async def fake_build_services(**kwargs: Any) -> Any:
        call_order.append("build_services")
        config = kwargs["config"]

        async def close() -> None:
            return None

        return SimpleNamespace(
            provider_selector=object(),
            tool_registry=object(),
            session_manager=object(),
            skill_loader=object(),
            usage_tracker=object(),
            config=config,
            memory_sync_managers={},
            model_catalog=None,
            memory_retrievers={},
            turn_capture_services={},
            flush_service=None,
            cron_scheduler=None,
            task_runtime=None,
            agent_registry=None,
            memory_managers={},
            memory_stores={},
            _turn_runner_ref=[],
            close=close,
        )

    def fake_start_background_install_telemetry(
        *,
        config: GatewayConfig,
        on_result: Any,
    ) -> None:
        assert app_holder["app"].state.gateway_ready is True
        call_order.append("install_telemetry")
        on_result(
            SimpleNamespace(
                skipped_reason=None,
                event="install",
                sent=True,
                uploaded=False,
                endpoint_configured=True,
            )
        )

    def fake_daily_usage_loop(storage: Any, *, config: GatewayConfig) -> Any:
        assert app_holder["app"].state.gateway_ready is True
        call_order.append("daily_usage")

        async def complete() -> None:
            return None

        return complete()

    real_create_gateway_app = boot.create_gateway_app

    def capture_gateway_app(*args: Any, **kwargs: Any) -> Any:
        app = real_create_gateway_app(*args, **kwargs)
        app_holder["app"] = app
        return app

    monkeypatch.setattr(boot, "log", FakeLog())
    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr(boot, "build_services", fake_build_services)
    monkeypatch.setattr(boot, "create_gateway_app", capture_gateway_app)
    monkeypatch.setattr(boot, "get_session_storage", lambda manager: object())
    monkeypatch.setattr(boot.uvicorn, "Server", FakeUvicornServer)
    monkeypatch.setattr(boot, "_desktop_router_preload_enabled", lambda: False)
    monkeypatch.setattr(boot, "_setup_file_logging", lambda config: None)
    monkeypatch.setattr(boot, "emit_skill_filter_banner", lambda config: None)
    monkeypatch.setattr(
        "opensquilla.observability.install_telemetry.start_background_install_telemetry",
        fake_start_background_install_telemetry,
    )
    monkeypatch.setattr(
        "opensquilla.observability.usage_telemetry.run_daily_usage_upload_loop",
        fake_daily_usage_loop,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.pidlock.GatewayPidLock.acquire",
        lambda self: None,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.pidlock.GatewayPidLock.release",
        lambda self: None,
    )
    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
    )

    async def run_case() -> None:
        server = await boot.start_gateway_server(config=config, run=True)

        try:
            assert call_order == ["build_services", "runtime_state"]
            await asyncio.sleep(0)
            telemetry_logs = [
                kwargs for event, kwargs in debug_logs if event == "gateway.install_telemetry"
            ]
            assert len(telemetry_logs) == 1
            assert telemetry_logs[0]["telemetry_event"] == "install"
            assert "event" not in telemetry_logs[0]
            assert "gateway.install_telemetry_skipped" not in {
                event for event, _kwargs in debug_logs
            }
            assert call_order == [
                "build_services",
                "runtime_state",
                "listener_callback",
                "listener",
                "gateway_ready",
                "install_telemetry",
                "daily_usage",
            ]
        finally:
            await server.close()

    asyncio.run(run_case())


def test_build_task_runtime_run_kwargs_forwards_fresh_user_session() -> None:
    pending_input_provider = object()
    run = SimpleNamespace(
        agent_id="main",
        attachments=[],
        input_provenance=None,
        run_kind="session_turn",
        no_memory_capture=False,
        fresh_user_session=True,
        ingress_pipeline_steps=(),
        semantic_message=None,
        pending_input_provider=pending_input_provider,
    )

    kwargs = build_task_runtime_run_kwargs(run, tool_context=object(), model="model")

    assert kwargs["fresh_user_session"] is True
    assert kwargs["pending_input_provider"] is pending_input_provider


def test_build_task_runtime_run_kwargs_forwards_task_id_as_root_turn() -> None:
    run = SimpleNamespace(
        task_id="task-turn-123",
        agent_id="main",
        attachments=[],
        input_provenance=None,
        run_kind="session_turn",
        no_memory_capture=False,
        fresh_user_session=False,
        ingress_pipeline_steps=(),
        semantic_message=None,
    )

    kwargs = build_task_runtime_run_kwargs(run, tool_context=object(), model="model")

    assert kwargs["root_turn_id"] == "task-turn-123"


def test_build_task_runtime_run_kwargs_forwards_provider_correlation() -> None:
    correlation = ProviderRequestCorrelation(
        session_id="parent-session",
        turn_id="parent-turn",
        execution_id="subagent-run",
        call_kind="subagent.chat",
    )
    run = SimpleNamespace(
        task_id="subagent-run",
        agent_id="worker",
        attachments=[],
        input_provenance={"kind": "subagent_task"},
        run_kind="subagent",
        no_memory_capture=False,
        fresh_user_session=False,
        ingress_pipeline_steps=(),
        semantic_message=None,
        provider_request_correlation=correlation,
    )

    kwargs = build_task_runtime_run_kwargs(run, tool_context=object(), model="model")

    assert kwargs["provider_request_correlation"] is correlation
    assert kwargs["root_turn_id"] == "subagent-run"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sender_id", "expected_owner"),
    [("channel-admin", True), ("paired-user", False)],
)
async def test_task_runtime_turn_uses_authenticated_channel_admin_boundary(
    sender_id: str,
    expected_owner: bool,
) -> None:
    class RecordingTurnRunner:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run(self, message: str, session_key: str, **kwargs: Any):
            self.calls.append(kwargs)
            yield DoneEvent()

    async def emit(_session_key: str, _event_name: str, _payload: dict[str, Any]) -> None:
        return None

    config = GatewayConfig(
        channel_admin_senders={"feishu": ["channel-admin"]},
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )
    msg = IncomingMessage(
        sender_id=sender_id,
        channel_id="oc-channel",
        content="hello",
        metadata={"principal_is_owner": True, "channel_admin_verified": True},
        provenance=IngressProvenance(
            provider="feishu",
            verification=IngressVerification.SDK_SESSION,
            principal=AuthenticatedPrincipal(subject_id=sender_id),
        ),
    )
    envelope = build_channel_route_envelope(
        msg,
        session_key=f"agent:main:feishu:{sender_id}",
        session_prefix="feishu",
        agent_id="main",
    )
    assert "principal_is_owner" not in envelope.metadata
    assert "channel_admin_verified" not in envelope.metadata
    assert _stamp_channel_admin_principal(config, envelope, msg) is expected_owner
    assert envelope.metadata["principal_is_owner"] is expected_owner
    run = SimpleNamespace(
        agent_id="main",
        task_id=f"task-{sender_id}",
        session_key=envelope.session_key,
        message="hello",
        envelope=envelope,
        attachments=[],
        input_provenance={},
        run_kind="channel_turn",
        no_memory_capture=False,
        ingress_pipeline_steps=[],
        semantic_message=None,
        stream_event_sink=None,
    )
    runner = RecordingTurnRunner()

    await dispatch_task_runtime_turn(
        run,
        config=config,
        session_manager=None,
        turn_runner=runner,
        event_emitter=emit,
    )

    tool_context = runner.calls[0]["tool_context"]
    assert tool_context.is_owner is expected_owner
    assert tool_context.channel_admin_verified is expected_owner
    assert tool_context.run_mode == "safe"


@pytest.mark.parametrize(
    "provenance",
    [
        IngressProvenance(),
        IngressProvenance(
            provider="feishu",
            verification=IngressVerification.SDK_SESSION,
            principal=AuthenticatedPrincipal(subject_id="another-user"),
        ),
    ],
    ids=["unverified", "principal-mismatch"],
)
def test_channel_admin_stamp_rejects_unverified_or_mismatched_identity(
    provenance: IngressProvenance,
) -> None:
    msg = IncomingMessage(
        sender_id="channel-admin",
        channel_id="oc-channel",
        content="hello",
        provenance=provenance,
    )
    envelope = build_channel_route_envelope(
        msg,
        session_key="agent:main:feishu:channel-admin",
        session_prefix="feishu",
    )
    config = GatewayConfig(channel_admin_senders={"feishu": ["channel-admin"]})

    assert _stamp_channel_admin_principal(config, envelope, msg) is False
    assert envelope.metadata["principal_is_owner"] is False
    assert envelope.metadata["channel_admin_verified"] is False
    assert _task_runtime_envelope_owner(envelope) is False

    context = tool_context_from_envelope(envelope, is_owner=True)
    assert context.is_owner is False
    assert context.channel_admin_verified is False


def test_build_task_runtime_run_kwargs_forwards_bound_user_message_id() -> None:
    # The persisted user message id must reach TurnRunner.run so history binds to
    # the exact prompt this turn answers (queued sends must not duplicate or leak).
    run = SimpleNamespace(
        agent_id="main",
        attachments=[],
        input_provenance=None,
        run_kind="session_turn",
        no_memory_capture=False,
        fresh_user_session=False,
        ingress_pipeline_steps=(),
        semantic_message=None,
        persisted_user_message_id="msg-123",
    )

    kwargs = build_task_runtime_run_kwargs(run, tool_context=object(), model="model")

    assert kwargs["bound_user_message_id"] == "msg-123"


def test_build_task_runtime_run_kwargs_omits_bound_id_when_absent() -> None:
    # Legacy callers/mocks without the field keep the positional-trim fallback:
    # the kwarg must be omitted, not forwarded as None.
    run = SimpleNamespace(
        agent_id="main",
        attachments=[],
        input_provenance=None,
        run_kind="session_turn",
        no_memory_capture=False,
        fresh_user_session=False,
        ingress_pipeline_steps=(),
        semantic_message=None,
        persisted_user_message_id=None,
    )

    kwargs = build_task_runtime_run_kwargs(run, tool_context=object(), model="model")

    assert "bound_user_message_id" not in kwargs


def test_build_task_runtime_run_kwargs_forwards_exact_assistant_sink() -> None:
    def sink(message_id: str | None, content: str) -> None:
        return None

    run = SimpleNamespace(
        agent_id="main",
        attachments=[],
        input_provenance=None,
        run_kind="channel_turn",
        no_memory_capture=False,
        fresh_user_session=False,
        ingress_pipeline_steps=(),
        semantic_message=None,
        persisted_user_message_id="msg-123",
        assistant_message_sink=sink,
    )

    kwargs = build_task_runtime_run_kwargs(run, tool_context=object(), model="model")

    assert kwargs["assistant_message_sink"] is sink


def test_gateway_stream_timeout_config_defaults_remain_serializable() -> None:
    config = GatewayConfig()

    assert config.agent_stream_idle_timeout_seconds == 600.0
    assert config.webui_stream_idle_grace_seconds == 630.0
    assert config.webui_stream_idle_grace_seconds > config.agent_stream_idle_timeout_seconds
    # A fresh install without an OpenRouter credential cannot run the static-B5
    # ensemble, so the effective hang-detection budgets stay at the defaults.
    assert effective_agent_stream_idle_timeout_seconds(config) == 600.0
    assert effective_webui_stream_idle_grace_seconds(config) == 630.0


def test_gateway_stream_timeout_defaults_stay_single_router_when_openrouter_key_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-synthetic")
    config = GatewayConfig()

    assert effective_agent_stream_idle_timeout_seconds(config) == 600.0
    assert effective_webui_stream_idle_grace_seconds(config) == 630.0


def test_gateway_stream_timeouts_keep_legacy_effective_values_when_static_disabled() -> None:
    config = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
        }
    )

    assert config.agent_stream_idle_timeout_seconds == 600.0
    assert config.webui_stream_idle_grace_seconds == 630.0
    assert effective_agent_stream_idle_timeout_seconds(config) == 600.0
    assert effective_webui_stream_idle_grace_seconds(config) == 630.0


def test_static_openrouter_b5_effective_stream_timeouts_extend_webui_budget() -> None:
    config = GatewayConfig(
        llm={"provider": "openrouter", "api_key": "sk-or-synthetic"},
        llm_ensemble={
            "enabled": True,
            "selection_mode": "static_openrouter_b5",
        },
    )

    assert config.agent_stream_idle_timeout_seconds == 600.0
    assert config.webui_stream_idle_grace_seconds == 630.0
    assert effective_agent_stream_idle_timeout_seconds(config) == 1200.0
    assert effective_webui_stream_idle_grace_seconds(config) == 1260.0


def test_static_openrouter_b5_keyless_install_keeps_default_stream_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    config = GatewayConfig(
        llm={"provider": "groq", "api_key": "sk-groq-synthetic"},
        llm_ensemble={
            "enabled": True,
            "selection_mode": "static_openrouter_b5",
        },
    )

    assert effective_agent_stream_idle_timeout_seconds(config) == 600.0
    assert effective_webui_stream_idle_grace_seconds(config) == 630.0


def test_tier_managed_static_b5_keeps_default_gateway_stream_timeouts() -> None:
    config = GatewayConfig(
        llm={
            "provider": "tokenrhythm",
            "api_key": "sk-tr-synthetic",
        },
        llm_ensemble={"enabled": False},
    )

    assert config.squilla_router.tiers["c3"]["ensemble_enabled"] is True
    assert "ensemble_selection_mode" not in config.squilla_router.tiers["c3"]
    assert effective_agent_stream_idle_timeout_seconds(config) == 600.0
    assert effective_webui_stream_idle_grace_seconds(config) == 630.0


def test_static_openrouter_b5_webui_grace_stays_above_custom_stream_idle() -> None:
    config = GatewayConfig(
        agent_stream_idle_timeout_seconds=2000.0,
        webui_stream_idle_grace_seconds=630.0,
        llm={"provider": "openrouter", "api_key": "sk-or-synthetic"},
        llm_ensemble={
            "enabled": True,
            "selection_mode": "static_openrouter_b5",
        },
    )

    assert effective_agent_stream_idle_timeout_seconds(config) == 2000.0
    assert effective_webui_stream_idle_grace_seconds(config) == 2060.0


def test_compaction_time_budget_defaults_allow_long_chain_work() -> None:
    gateway_config = GatewayConfig()
    agent_config = AgentConfig()
    compaction_config = CompactionConfig()

    assert gateway_config.memory.flush_timeout_seconds == 15.0
    assert gateway_config.memory.flush_background_timeout_seconds == 120.0
    assert gateway_config.compaction.timeout_seconds == 90.0
    assert agent_config.flush_timeout_seconds == 15.0
    assert agent_config.flush_background_timeout_seconds == 120.0
    assert compaction_config.timeout_seconds == 90.0


def test_gateway_home_uses_configured_state_parent(tmp_path: Path) -> None:
    config = GatewayConfig(
        state_dir=str(tmp_path / "instance" / "state"),
        workspace_dir=str(tmp_path / "instance" / "workspace"),
    )

    assert _gateway_home(config) == tmp_path / "instance"


def test_gateway_home_falls_back_to_config_path_parent(tmp_path: Path) -> None:
    config = GatewayConfig(
        state_dir=None,
        config_path=str(tmp_path / "service" / "config.toml"),
        workspace_dir=str(tmp_path / "service" / "workspace"),
    )

    assert _gateway_home(config) == tmp_path / "service"


@pytest.mark.asyncio
async def test_boot_sandbox_setup_prewarms_an_existing_ready_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import boot
    from opensquilla.sandbox.setup_state import SandboxSetupState, SetupResult

    calls: list[str] = []
    config = GatewayConfig(
        sandbox={
            "run_mode": "trusted",
            "sandbox": True,
            "security_grading": True,
            "network_default": "proxy_allowlist",
        },
    )

    async def fake_status(setup_config: GatewayConfig) -> SetupResult:
        calls.append("status")
        assert setup_config is config
        return SetupResult(
            state=SandboxSetupState.READY,
            platform="auto",
            message="Sandbox setup is ready.",
            requires_admin=False,
            detail="proxy_allowlist=ready",
        )

    async def fake_capability(setup_config: GatewayConfig) -> object:
        calls.append("capability")
        assert setup_config is config
        return object()

    monkeypatch.setattr(
        "opensquilla.sandbox.setup_runtime.current_sandbox_setup_runtime_status",
        fake_status,
    )
    monkeypatch.setattr(
        "opensquilla.sandbox.setup_runtime.current_sandbox_capability_report",
        fake_capability,
    )

    result = await boot._ensure_sandbox_setup_on_boot(config)

    assert result is not None
    assert result.state is SandboxSetupState.READY
    assert calls == ["status", "capability"]


@pytest.mark.asyncio
async def test_boot_sandbox_setup_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import boot

    async def fail_if_called(config: GatewayConfig) -> object:
        raise AssertionError("sandbox.auto_setup=false must not inspect setup")

    monkeypatch.setattr(
        "opensquilla.sandbox.setup_runtime.current_sandbox_setup_runtime_status",
        fail_if_called,
    )

    result = await boot._ensure_sandbox_setup_on_boot(
        GatewayConfig(
            sandbox={
                "auto_setup": False,
                "run_mode": "trusted",
                "sandbox": True,
                "security_grading": True,
            },
        )
    )

    assert result is None


@pytest.mark.asyncio
async def test_boot_sandbox_setup_defers_incomplete_setup_for_full_host_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import boot
    from opensquilla.sandbox.setup_state import SandboxSetupState, SetupResult

    calls: list[str] = []
    config = GatewayConfig(
        sandbox={
            "run_mode": "full",
            "sandbox": False,
            "security_grading": False,
        },
    )

    async def fake_status(setup_config: GatewayConfig) -> SetupResult:
        calls.append("status")
        assert setup_config is config
        return SetupResult(
            state=SandboxSetupState.NOT_SETUP,
            platform="auto",
            message="Sandbox setup requires administrator approval.",
            requires_admin=True,
        )

    async def fake_capability(setup_config: GatewayConfig) -> object:
        calls.append("capability")
        assert setup_config is config
        return object()

    monkeypatch.setattr(
        "opensquilla.sandbox.setup_runtime.current_sandbox_setup_runtime_status",
        fake_status,
    )
    monkeypatch.setattr(
        "opensquilla.sandbox.setup_runtime.current_sandbox_capability_report",
        fake_capability,
    )

    result = await boot._ensure_sandbox_setup_on_boot(config)

    assert result is not None
    assert result.state is SandboxSetupState.NOT_SETUP
    assert calls == ["status"]


@pytest.mark.asyncio
async def test_build_services_schedules_sandbox_setup_after_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.gateway import boot

    events: list[str] = []
    scheduled: list[Any] = []
    background_task = SimpleNamespace()

    async def fake_setup(config: GatewayConfig) -> None:
        events.append("setup")

    def fake_configure_runtime(*args: Any, **kwargs: Any) -> Any:
        events.append("runtime")
        return SimpleNamespace(effective=SimpleNamespace(as_dict=lambda: {}))

    def fake_reset_runtime() -> None:
        events.append("runtime_reset")

    def fake_create_background_task(coro: Any) -> Any:
        scheduled.append(coro)
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return background_task

    monkeypatch.setattr(boot, "_ensure_sandbox_setup_on_boot", fake_setup)
    monkeypatch.setattr(boot, "create_background_task", fake_create_background_task)
    monkeypatch.setattr("opensquilla.sandbox.integration.configure_runtime", fake_configure_runtime)
    monkeypatch.setattr("opensquilla.sandbox.integration.reset_runtime", fake_reset_runtime)

    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
        mcp={"enabled": False},
        memory={"flush_enabled": False},
        sandbox={
            "run_mode": "trusted",
            "sandbox": True,
            "security_grading": True,
            "network_default": "proxy_allowlist",
        },
    )

    services = await build_services(
        config=config,
        session_db_path=str(tmp_path / "sessions.sqlite"),
        seed_agent_workspaces=False,
    )
    try:
        assert events == ["runtime"]
        assert len(scheduled) == 1
        assert services.sandbox_setup_task is background_task
    finally:
        await services.close()
    assert events == ["runtime", "runtime_reset"]


@pytest.mark.asyncio
async def test_service_container_close_cancels_owned_sandbox_setup_task() -> None:
    from opensquilla.gateway import boot

    entered = asyncio.Event()

    async def blocked_setup() -> None:
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(blocked_setup())
    services = boot.ServiceContainer(
        config=GatewayConfig(),
        sandbox_setup_task=task,
    )
    await entered.wait()

    await services.close()

    assert services.sandbox_setup_task is None
    assert task.cancelled()


@pytest.mark.asyncio
async def test_service_container_close_cancels_profile_import_maintenance() -> None:
    from opensquilla.gateway import boot

    entered = asyncio.Event()

    async def blocked_maintenance() -> None:
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(blocked_maintenance())
    services = boot.ServiceContainer(
        config=GatewayConfig(),
        profile_import_maintenance_task=task,
    )
    await entered.wait()

    await services.close()

    assert services.profile_import_maintenance_task is None
    assert task.cancelled()


@pytest.mark.asyncio
async def test_bare_full_default_boots_full_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.gateway import boot

    captured: list[tuple[SandboxSettings, RunMode]] = []

    def fake_configure_runtime(settings: SandboxSettings, **kwargs: Any) -> Any:
        captured.append((settings, kwargs["default_run_mode"]))
        return SimpleNamespace(
            effective=SimpleNamespace(
                sandbox_enabled=True,
                as_dict=lambda: {"sandbox_enabled": True},
            )
        )

    monkeypatch.setattr("opensquilla.sandbox.integration.configure_runtime", fake_configure_runtime)

    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
        mcp={"enabled": False},
        memory={"flush_enabled": False},
        sandbox={"auto_setup": False},
    )

    services = await boot.build_services(
        config=config,
        session_db_path=str(tmp_path / "sessions.sqlite"),
        seed_agent_workspaces=False,
    )
    try:
        settings, default_mode = captured[0]
        assert settings.run_mode == "safe"
        assert settings.sandbox is True
        assert settings.security_grading is True
        assert settings.network_default == "proxy_allowlist"
        assert default_mode is RunMode.FULL
    finally:
        await services.close()


class _FakeDreamScheduler:
    def __init__(self, jobs: list[CronJob] | None = None) -> None:
        self.jobs = jobs or []
        self.added: list[dict[str, Any]] = []
        self.paused: list[str] = []

    async def list_jobs(self) -> list[CronJob]:
        return self.jobs

    async def add_job(self, **kwargs: Any) -> None:
        self.added.append(kwargs)

    async def pause_job(self, job_id: str) -> None:
        self.paused.append(job_id)
        for job in self.jobs:
            if job.id == job_id:
                job.status = JobStatus.PAUSED


def test_build_turn_runner_from_services_wires_memory_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    from opensquilla.gateway import boot

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    services = SimpleNamespace(
        provider_selector=object(),
        tool_registry=object(),
        session_manager=object(),
        skill_loader=object(),
        usage_tracker=object(),
        config=GatewayConfig(),
        memory_sync_managers={"main": object()},
        memory_retrievers={"main": object()},
        turn_capture_services={"main": object()},
        flush_service=object(),
        model_catalog=object(),
    )

    runner = boot.build_turn_runner_from_services(services)

    assert isinstance(runner, FakeTurnRunner)
    assert captured["memory_sync_managers"] is services.memory_sync_managers
    assert captured["memory_retrievers"] is services.memory_retrievers
    assert captured["turn_capture_services"] is services.turn_capture_services
    assert captured["session_flush_service"] is services.flush_service
    assert captured["model_catalog"] is services.model_catalog


def test_build_turn_runner_from_services_wires_diagnostics_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    services = SimpleNamespace(
        provider_selector=object(),
        tool_registry=object(),
        session_manager=object(),
        skill_loader=object(),
        usage_tracker=object(),
        config=GatewayConfig(),
    )
    state = DiagnosticsState.from_config(GatewayConfig())

    from opensquilla.gateway import boot

    runner = boot.build_turn_runner_from_services(services, diagnostics_state=state)

    assert isinstance(runner, FakeTurnRunner)
    assert captured["diagnostics_state"] is state


@pytest.mark.asyncio
async def test_start_gateway_server_shares_diagnostics_state_between_app_and_turn_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_runner: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            captured_runner.update(kwargs)

        def set_session_lock_provider(self, provider: Any) -> None:
            captured_runner["session_lock_provider"] = provider

    async def fake_build_services(**kwargs: Any) -> Any:
        config = kwargs["config"]

        async def close() -> None:
            return None

        return SimpleNamespace(
            provider_selector=object(),
            tool_registry=object(),
            session_manager=object(),
            skill_loader=object(),
            usage_tracker=object(),
            config=config,
            memory_sync_managers={},
            model_catalog=None,
            memory_retrievers={},
            turn_capture_services={},
            flush_service=None,
            cron_scheduler=None,
            task_runtime=None,
            agent_registry=None,
            memory_managers={},
            memory_stores={},
            _turn_runner_ref=[],
            close=close,
        )

    from opensquilla.gateway import boot

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr(boot, "build_services", fake_build_services)
    monkeypatch.setattr(boot, "_setup_file_logging", lambda config: None)
    monkeypatch.setattr(boot, "emit_skill_filter_banner", lambda config: None)
    monkeypatch.setattr(
        "opensquilla.gateway.pidlock.GatewayPidLock.acquire",
        lambda self: None,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.pidlock.GatewayPidLock.release",
        lambda self: None,
    )
    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
        diagnostics_enabled=True,
    )

    server = await boot.start_gateway_server(config=config, run=False)

    try:
        state = server.app.state.diagnostics_state
        assert isinstance(state, DiagnosticsState)
        assert captured_runner["diagnostics_state"] is state
        state.set_runtime(enabled=True, raw=True)
        assert captured_runner["diagnostics_state"].raw_turn_call_enabled() is True
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_start_gateway_server_creates_default_subscription_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_bridge: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def set_session_lock_provider(self, _provider: Any) -> None:
            pass

    class FakeEventBridge:
        def __init__(self, *, subscription_manager: Any, connection_registry: Any) -> None:
            captured_bridge["subscription_manager"] = subscription_manager
            captured_bridge["connection_registry"] = connection_registry

        async def emit(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    async def fake_build_services(**kwargs: Any) -> Any:
        config = kwargs["config"]

        async def close() -> None:
            return None

        return SimpleNamespace(
            provider_selector=object(),
            tool_registry=object(),
            session_manager=object(),
            skill_loader=object(),
            usage_tracker=object(),
            config=config,
            memory_sync_managers={},
            model_catalog=None,
            memory_retrievers={},
            turn_capture_services={},
            flush_service=None,
            cron_scheduler=None,
            task_runtime=None,
            agent_registry=None,
            memory_managers={},
            memory_stores={},
            _turn_runner_ref=[],
            close=close,
        )

    from opensquilla.gateway import boot
    from opensquilla.gateway.websocket import SubscriptionManager

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("opensquilla.gateway.event_bridge.EventBridge", FakeEventBridge)
    monkeypatch.setattr(boot, "build_services", fake_build_services)
    monkeypatch.setattr(boot, "_setup_file_logging", lambda config: None)
    monkeypatch.setattr(boot, "emit_skill_filter_banner", lambda config: None)
    monkeypatch.setattr(
        "opensquilla.gateway.pidlock.GatewayPidLock.acquire",
        lambda self: None,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.pidlock.GatewayPidLock.release",
        lambda self: None,
    )
    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
    )

    server = await boot.start_gateway_server(config=config, run=False)

    try:
        assert isinstance(captured_bridge["subscription_manager"], SubscriptionManager)
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_start_gateway_server_schedules_router_preload_after_channels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeTurnRunner:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def set_session_lock_provider(self, _provider: Any) -> None:
            pass

    class FakeChannelManager:
        async def start_all(self) -> dict[str, bool]:
            events.append("channels.start_all")
            return {"feishu": True}

        def start_errors(self) -> dict[str, dict[str, str]]:
            return {}

        async def stop_all(self) -> None:
            return None

    class FakeServer:
        def __init__(self, _config: Any) -> None:
            self.should_exit = False

        async def serve(self) -> None:
            return None

    async def fake_build_services(**kwargs: Any) -> Any:
        config = kwargs["config"]

        async def close() -> None:
            return None

        return SimpleNamespace(
            provider_selector=object(),
            tool_registry=object(),
            session_manager=object(),
            skill_loader=object(),
            usage_tracker=object(),
            config=config,
            memory_sync_managers={},
            model_catalog=None,
            memory_retrievers={},
            turn_capture_services={},
            flush_service=None,
            cron_scheduler=None,
            task_runtime=None,
            agent_registry=None,
            memory_managers={},
            memory_stores={},
            _turn_runner_ref=[],
            close=close,
        )

    def fake_create_background_task(coro: Any) -> Any:
        code = getattr(coro, "cr_code", None)
        name = getattr(code, "co_name", "")
        if name == "preload_squilla_router_runtime":
            events.append("router.preload.scheduled")
        elif name == "serve":
            events.append("server.serve.scheduled")
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return __import__("asyncio").create_task(__import__("asyncio").sleep(0))

    from opensquilla.gateway import boot

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr(boot, "build_services", fake_build_services)
    monkeypatch.setattr(boot, "_setup_file_logging", lambda config: None)
    monkeypatch.setattr(boot, "emit_skill_filter_banner", lambda config: None)
    monkeypatch.setattr(boot, "create_background_task", fake_create_background_task)
    monkeypatch.setattr(boot.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(
        "opensquilla.gateway.pidlock.GatewayPidLock.acquire",
        lambda self: None,
    )

    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
    )
    config.squilla_router.enabled = True

    server = await boot.start_gateway_server(
        config=config,
        channel_manager=FakeChannelManager(),
        run=True,
    )

    try:
        assert events.index("channels.start_all") < events.index("router.preload.scheduled")
    finally:
        await server.close()


def test_start_gateway_server_passes_tls_files_to_uvicorn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_config: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def set_session_lock_provider(self, _provider: Any) -> None:
            pass

    class FakeUvicornConfig:
        def __init__(self, **kwargs: Any) -> None:
            captured_config.update(kwargs)

    class FakeServer:
        def __init__(self, _config: Any) -> None:
            self.should_exit = False

        async def serve(self) -> None:
            return None

    async def fake_build_services(**kwargs: Any) -> Any:
        config = kwargs["config"]

        async def close() -> None:
            return None

        return SimpleNamespace(
            provider_selector=object(),
            tool_registry=object(),
            session_manager=object(),
            skill_loader=object(),
            usage_tracker=object(),
            config=config,
            memory_sync_managers={},
            model_catalog=None,
            memory_retrievers={},
            turn_capture_services={},
            flush_service=None,
            cron_scheduler=None,
            task_runtime=None,
            agent_registry=None,
            memory_managers={},
            memory_stores={},
            _turn_runner_ref=[],
            close=close,
        )

    def fake_create_background_task(coro: Any) -> Any:
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return asyncio.create_task(asyncio.sleep(0))

    from opensquilla.gateway import boot

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr(boot, "build_services", fake_build_services)
    monkeypatch.setattr(boot, "_setup_file_logging", lambda config: None)
    monkeypatch.setattr(boot, "emit_skill_filter_banner", lambda config: None)
    monkeypatch.setattr(boot, "create_background_task", fake_create_background_task)
    monkeypatch.setattr(boot.uvicorn, "Config", FakeUvicornConfig)
    monkeypatch.setattr(boot.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(
        "opensquilla.gateway.pidlock.GatewayPidLock.acquire",
        lambda self: None,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.pidlock.GatewayPidLock.release",
        lambda self: None,
    )

    keyfile = str(tmp_path / "gateway.key")
    certfile = str(tmp_path / "gateway.crt")
    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
        tls={"keyfile": keyfile, "certfile": certfile},
    )

    async def run_case() -> None:
        server = await boot.start_gateway_server(config=config, run=True)

        try:
            assert captured_config["ssl_keyfile"] == keyfile
            assert captured_config["ssl_certfile"] == certfile
            assert captured_config["access_log"] is False
            assert callable(captured_config["callback_notify"])
        finally:
            await server.close()

    asyncio.run(run_case())


@pytest.mark.asyncio
async def test_start_gateway_server_wires_cron_failure_dispatcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driver-level guard for the production cron failure-destination wire.

    When ``svc.cron_scheduler`` exists, boot must register
    ``DeliveryChain.dispatch_failure_alert`` as the global failure dispatcher
    in ``scheduler.jobs`` so failed cron runs reach the configured FD at
    runtime. Without this wire the dispatch plumbing is dead in production
    even though unit tests cover the hook directly.
    """
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **_kw: Any) -> None: ...

        def set_session_lock_provider(self, _provider: Any) -> None: ...

    class FakeCronScheduler:
        def __init__(self) -> None:
            self.registered: dict[str, Any] = {}
            self.started = False

        def register_handler(self, key: str, fn: Any) -> None:
            self.registered[key] = fn

        async def list_jobs(self) -> list:
            return []

        async def start(self) -> None:
            assert set(self.registered) >= {
                "agent_run",
                "static_message",
                "system_event",
                "memory_dream",
                "auto_propose",
            }
            self.started = True

    cron_sched = FakeCronScheduler()

    async def fake_build_services(**kwargs: Any) -> Any:
        async def close() -> None:
            return None

        return SimpleNamespace(
            provider_selector=object(),
            tool_registry=object(),
            session_manager=None,
            skill_loader=object(),
            usage_tracker=object(),
            config=kwargs["config"],
            memory_sync_managers={},
            model_catalog=None,
            memory_retrievers={},
            turn_capture_services={},
            flush_service=None,
            cron_scheduler=cron_sched,
            task_runtime=None,
            agent_registry=None,
            memory_managers={},
            memory_stores={},
            _turn_runner_ref=[],
            close=close,
        )

    from opensquilla.gateway import boot
    from opensquilla.scheduler import jobs as scheduler_jobs

    def _record_dispatcher(fn: Any) -> None:
        captured["dispatcher"] = fn

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr(boot, "build_services", fake_build_services)
    monkeypatch.setattr(boot, "_setup_file_logging", lambda config: None)
    monkeypatch.setattr(boot, "emit_skill_filter_banner", lambda config: None)
    monkeypatch.setattr(scheduler_jobs, "set_failure_dispatcher", _record_dispatcher)
    monkeypatch.setattr("opensquilla.gateway.pidlock.GatewayPidLock.acquire", lambda self: None)
    monkeypatch.setattr("opensquilla.gateway.pidlock.GatewayPidLock.release", lambda self: None)

    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
    )

    server = await boot.start_gateway_server(config=config, run=False)
    try:
        assert callable(captured.get("dispatcher")), (
            "set_failure_dispatcher was not called during boot — the cron "
            "failure-destination wire is missing from gateway/boot.py"
        )
        # The wire must register DeliveryChain.dispatch_failure_alert
        # (a bound method), not some unrelated callable.
        assert getattr(captured["dispatcher"], "__name__", "") == "dispatch_failure_alert"
        # Handler factories ran, confirming the wire ran inside the cron-init
        # branch (not just by coincidence).
        assert set(cron_sched.registered) >= {
            "agent_run",
            "static_message",
            "system_event",
        }
        assert cron_sched.started is True
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_start_gateway_server_wires_meta_skill_auto_propose_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boot must connect the three auto-propose surfaces, not just define them.

    The cron handler factory, runtime bridge, and dream post-hook each have
    isolated unit coverage. This guards the production integration point where
    the previous implementation left those pieces unreachable.
    """
    from opensquilla.gateway import boot
    from opensquilla.gateway.auto_propose_bridge import get_runtime, reset_runtime_for_test
    from opensquilla.scheduler import auto_propose_handler as auto_handler_mod
    from opensquilla.scheduler import dream_handler as dream_handler_mod
    from opensquilla.skills.creator import proposer as proposer_mod
    from opensquilla.skills.creator import runtime_e2e as runtime_e2e_mod

    reset_runtime_for_test()
    captured: dict[str, Any] = {}
    runtime_contexts: list[dict[str, Any]] = []
    installed_runtime_contexts: list[dict[str, Any]] = []
    installed_smoke_contexts: list[dict[str, Any]] = []
    reset_tokens: list[str] = []
    smoke_reset_tokens: list[str] = []

    class FakeProviderSelector:
        def __init__(self) -> None:
            self.model = "primary-model"

        def clone(self) -> FakeProviderSelector:
            captured["provider_selector_cloned"] = True
            return self

        def override_model(self, model: str) -> None:
            captured["provider_override_model"] = model
            self.model = model

        def resolve(self) -> Any:
            return SimpleNamespace(model=self.model)

    class FakeTurnRunner:
        def __init__(self, **_kw: Any) -> None: ...

        def set_session_lock_provider(self, _provider: Any) -> None: ...

    class FakeCronScheduler:
        def __init__(self) -> None:
            self.registered: dict[str, Any] = {}
            self.added: list[dict[str, Any]] = []
            self.paused: list[str] = []
            self.started = False

        def register_handler(self, key: str, fn: Any) -> None:
            self.registered[key] = fn

        async def list_jobs(self) -> list:
            return []

        async def add_job(self, **kwargs: Any) -> Any:
            self.added.append(kwargs)
            return SimpleNamespace(id=kwargs.get("name", "job"))

        async def pause_job(self, job_id: str) -> None:
            self.paused.append(job_id)

        async def start(self) -> None:
            assert "agent_run" in self.registered
            assert "auto_propose" in self.registered
            self.started = True

    cron_sched = FakeCronScheduler()

    async def fake_build_services(**kwargs: Any) -> Any:
        async def close() -> None:
            return None

        return SimpleNamespace(
            provider_selector=FakeProviderSelector(),
            tool_registry=ToolRegistry(),
            session_manager=None,
            skill_loader=object(),
            usage_tracker=object(),
            config=kwargs["config"],
            memory_sync_managers={},
            model_catalog=None,
            memory_retrievers={},
            turn_capture_services={},
            flush_service=None,
            cron_scheduler=cron_sched,
            task_runtime=None,
            agent_registry=None,
            memory_managers={},
            memory_stores={},
            _turn_runner_ref=[],
            close=close,
        )

    def fake_make_auto_propose_handler(**kwargs: Any) -> Any:
        captured["auto_handler_kwargs"] = kwargs

        async def _handler(_job: Any) -> Any:
            return SimpleNamespace(summary="auto_propose fake", delivery_status="delivered")

        return _handler

    def fake_make_memory_dream_handler(*args: Any, **kwargs: Any) -> Any:
        captured["dream_handler_kwargs"] = kwargs
        return "dream-handler"

    def fake_make_runtime_e2e_context(**kwargs: Any) -> dict[str, Any]:
        runtime_contexts.append(kwargs)
        return {"runner": object(), "judge": object(), "baseline_model": kwargs["baseline_model"]}

    def fake_set_runtime_e2e_context(ctx: dict[str, Any]) -> str:
        installed_runtime_contexts.append(ctx)
        return "runtime-token"

    def fake_reset_runtime_e2e_context(token: str) -> None:
        reset_tokens.append(token)

    def fake_set_smoke_fixture_context(ctx: dict[str, Any]) -> str:
        installed_smoke_contexts.append(ctx)
        return "smoke-token"

    def fake_reset_smoke_fixture_context(token: str) -> None:
        smoke_reset_tokens.append(token)

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr(boot, "build_services", fake_build_services)
    monkeypatch.setattr(boot, "_setup_file_logging", lambda config: None)
    monkeypatch.setattr(boot, "emit_skill_filter_banner", lambda config: None)
    monkeypatch.setattr(
        auto_handler_mod,
        "make_auto_propose_handler",
        fake_make_auto_propose_handler,
    )
    monkeypatch.setattr(
        dream_handler_mod,
        "make_memory_dream_handler",
        fake_make_memory_dream_handler,
    )
    monkeypatch.setattr(runtime_e2e_mod, "make_runtime_e2e_context", fake_make_runtime_e2e_context)
    monkeypatch.setattr(proposer_mod, "set_runtime_e2e_context", fake_set_runtime_e2e_context)
    monkeypatch.setattr(proposer_mod, "reset_runtime_e2e_context", fake_reset_runtime_e2e_context)
    monkeypatch.setattr(proposer_mod, "set_smoke_fixture_context", fake_set_smoke_fixture_context)
    monkeypatch.setattr(
        proposer_mod, "reset_smoke_fixture_context", fake_reset_smoke_fixture_context
    )
    monkeypatch.setattr("opensquilla.gateway.pidlock.GatewayPidLock.acquire", lambda self: None)
    monkeypatch.setattr("opensquilla.gateway.pidlock.GatewayPidLock.release", lambda self: None)

    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
        memory={"dream": {"enabled": True}},
        meta_skill={
            "auto_propose": {
                "enabled": True,
                "on_dream_complete": True,
                "auto_enable": True,
            },
        },
        squilla_router={
            "tiers": {
                "c3": {
                    "model": "frontier-t3-model",
                    "thinking_level": "high",
                },
            },
        },
    )

    server = await boot.start_gateway_server(config=config, run=False)
    try:
        assert "auto_propose" in cron_sched.registered
        assert captured["auto_handler_kwargs"]["config"] is config.meta_skill.auto_propose
        assert any(job["handler_key"] == "auto_propose" for job in cron_sched.added)
        assert callable(captured["dream_handler_kwargs"].get("post_dream_hook"))
        orch = captured["auto_handler_kwargs"]["build_orchestrator"]("main")
        assert captured["provider_selector_cloned"] is True
        assert captured["provider_override_model"] == "frontier-t3-model"
        assert runtime_contexts
        assert runtime_contexts[-1]["skill_loader"] is server._services.skill_loader
        base_config = runtime_contexts[-1]["base_config"]
        assert base_config.model_id == "frontier-t3-model"
        assert base_config.metadata["routed_tier"] == "c3"
        assert base_config.metadata["thinking_level"] == "high"
        assert runtime_contexts[-1]["baseline_model"] == "frontier-t3-model"
        with pytest.raises(RuntimeError):
            await orch._tool_invoker("meta_skill_runtime_e2e_run", {"skill_md": "x"})
        assert installed_runtime_contexts[-1] is not None
        assert installed_smoke_contexts[-1]["llm_chat"] is not None
        assert reset_tokens[-1] == "runtime-token"
        assert smoke_reset_tokens[-1] == "smoke-token"
        rt = get_runtime()
        assert rt is not None
        assert rt.config is config.meta_skill.auto_propose
        assert rt.home == tmp_path
    finally:
        await server.close()
        reset_runtime_for_test()


def test_build_flush_service_respects_memory_flush_enabled_config() -> None:
    service = build_flush_service(
        tool_registry=ToolRegistry(),
        provider_selector=SimpleNamespace(resolve=lambda: object()),
        config=GatewayConfig(memory={"flush_enabled": False}),
    )

    assert service is None


def test_build_flush_service_uses_configured_background_memory_timeout() -> None:
    service = build_flush_service(
        tool_registry=ToolRegistry(),
        provider_selector=SimpleNamespace(resolve=lambda: object()),
        config=GatewayConfig(
            memory={
                "flush_enabled": True,
                "flush_timeout_seconds": 0.25,
                "flush_background_timeout_seconds": 42.0,
            }
        ),
    )

    assert service is not None
    assert service._default_timeout == 42.0


@pytest.mark.asyncio
async def test_build_flush_service_archive_workspace_falls_back_to_main_workspace(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry()
    main_workspace = tmp_path / "main-workspace"
    matching_memory_dir = tmp_path / "matching-memory"
    service = build_flush_service(
        tool_registry=registry,
        provider_selector=SimpleNamespace(resolve=lambda: None),
        config=GatewayConfig(memory={"flush_enabled": True}),
        memory_managers={
            "side": SimpleNamespace(workspace_dir=None, memory_dir=matching_memory_dir),
            "main": SimpleNamespace(
                workspace_dir=main_workspace,
                memory_dir=tmp_path / "main-memory",
            ),
        },
    )

    receipt = await service.execute(
        [Message(role="user", content="temporary transcript")],
        "agent:side:webchat:s1",
        agent_id="side",
    )

    assert receipt.mode == "raw"
    assert (main_workspace / receipt.flushed_paths[0]).exists()
    assert not (matching_memory_dir / receipt.flushed_paths[0]).exists()


@pytest.mark.asyncio
async def test_build_flush_service_wires_durable_receipt_writer(tmp_path: Path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.sqlite"))
    session_manager = SessionManager(storage)
    registry = ToolRegistry()

    async def memory_save(path: str, content: str, mode: str) -> str:
        assert mode == "append"
        assert content.startswith("# Raw flush")
        return f"Saved to {path} (0 chunks indexed)."

    registry.register(
        ToolSpec(
            name="memory_save",
            description="Save memory",
            parameters={
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string"},
            },
            required=["path", "content", "mode"],
        ),
        memory_save,
    )
    try:
        session_key = "agent:main:webchat:s1"
        session = await session_manager.create(session_key)
        service = build_flush_service(
            tool_registry=registry,
            provider_selector=SimpleNamespace(resolve=lambda: None),
            config=GatewayConfig(memory={"flush_enabled": True}),
            session_manager=session_manager,
            memory_managers={"main": SimpleNamespace(workspace_dir=tmp_path)},
        )

        receipt = await service.execute(
            [Message(role="user", content="temporary transcript")],
            session_key,
            agent_id="main",
        )
        rows = await storage.list_memory_durable_receipts(session_key=session_key)

        assert receipt.result_status == "ok_archive_only"
        assert len(rows) == 2
        assert rows[0].scope == "preimage"
        repair_row = rows[1]
        assert repair_row.session_id == session.session_id
        assert repair_row.scope == "repair"
        assert repair_row.status == "repair_pending"
        assert repair_row.reason == "ok_archive_only"
        assert repair_row.target_path == receipt.flushed_paths[0]
        assert repair_row.source_path == f"session:{session_key}:flush:1-1"
        assert repair_row.content_hash == receipt.content_hash
        assert repair_row.turn_id == "flush:1-1"
        assert repair_row.idempotency_key.startswith(
            f"flush-receipt:repair:{session_key}:{session.session_id}:flush:1-1:"
        )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_build_flush_service_skips_receipt_after_session_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.sqlite"))
    session_manager = SessionManager(storage)
    registry = ToolRegistry()
    archive_started = Event()
    allow_archive = Event()

    from opensquilla.memory import session_flush as session_flush_module

    real_archive_writer = session_flush_module.write_raw_fallback_archive

    def archive_writer(*args: Any, **kwargs: Any) -> Any:
        archive_started.set()
        assert allow_archive.wait(timeout=2.0)
        return real_archive_writer(*args, **kwargs)

    monkeypatch.setattr(
        session_flush_module,
        "write_raw_fallback_archive",
        archive_writer,
    )
    try:
        session_key = "agent:main:webchat:s1"
        original = await session_manager.create(session_key)
        service = build_flush_service(
            tool_registry=registry,
            provider_selector=SimpleNamespace(resolve=lambda: None),
            config=GatewayConfig(memory={"flush_enabled": True}),
            session_manager=session_manager,
            memory_managers={"main": SimpleNamespace(workspace_dir=tmp_path)},
        )

        task = asyncio.create_task(
            service.execute(
                [Message(role="user", content="temporary transcript")],
                session_key,
                agent_id="main",
            )
        )
        await asyncio.wait_for(asyncio.to_thread(archive_started.wait), timeout=2.0)
        rotated, did_rotate = await session_manager.apply_intent(
            session_key,
            SessionIntent.RESET_SAME_KEY,
        )
        allow_archive.set()
        receipt = await task
        rows = await storage.list_memory_durable_receipts(session_key=session_key)

        assert did_rotate
        assert rotated.session_id != original.session_id
        assert receipt.session_id == original.session_id
        assert rows == []
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_build_flush_service_skips_receipt_after_session_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.sqlite"))
    session_manager = SessionManager(storage)
    registry = ToolRegistry()
    archive_started = Event()
    allow_archive = Event()

    from opensquilla.memory import session_flush as session_flush_module

    real_archive_writer = session_flush_module.write_raw_fallback_archive

    def archive_writer(*args: Any, **kwargs: Any) -> Any:
        archive_started.set()
        assert allow_archive.wait(timeout=2.0)
        return real_archive_writer(*args, **kwargs)

    monkeypatch.setattr(
        session_flush_module,
        "write_raw_fallback_archive",
        archive_writer,
    )
    try:
        session_key = "agent:main:webchat:deleted-during-flush"
        original = await session_manager.create(session_key)
        service = build_flush_service(
            tool_registry=registry,
            provider_selector=SimpleNamespace(resolve=lambda: None),
            config=GatewayConfig(memory={"flush_enabled": True}),
            session_manager=session_manager,
            memory_managers={"main": SimpleNamespace(workspace_dir=tmp_path)},
        )

        task = asyncio.create_task(
            service.execute(
                [Message(role="user", content="temporary transcript")],
                session_key,
                agent_id="main",
            )
        )
        await asyncio.wait_for(asyncio.to_thread(archive_started.wait), timeout=2.0)
        await storage.delete_session(session_key)
        allow_archive.set()
        receipt = await task

        assert receipt.session_id == original.session_id
        assert await storage.get_session(session_key) is None
        assert await storage.list_memory_durable_receipts(session_key=session_key) == []
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_build_flush_service_receipts_distinguish_same_window_different_content(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.sqlite"))
    session_manager = SessionManager(storage)
    registry = ToolRegistry()

    async def memory_save(path: str, content: str, mode: str) -> str:
        return f"Saved to {path} (0 chunks indexed)."

    registry.register(
        ToolSpec(
            name="memory_save",
            description="Save memory",
            parameters={
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string"},
            },
            required=["path", "content", "mode"],
        ),
        memory_save,
    )
    try:
        session_key = "agent:main:webchat:s1"
        await session_manager.create(session_key)
        service = build_flush_service(
            tool_registry=registry,
            provider_selector=SimpleNamespace(resolve=lambda: None),
            config=GatewayConfig(memory={"flush_enabled": True}),
            session_manager=session_manager,
            memory_managers={"main": SimpleNamespace(workspace_dir=tmp_path)},
        )

        first = await service.execute(
            [Message(role="user", content="first content")],
            session_key,
            agent_id="main",
        )
        second = await service.execute(
            [Message(role="user", content="second content")],
            session_key,
            agent_id="main",
        )
        rows = await storage.list_memory_durable_receipts(session_key=session_key)

        assert first.content_hash != second.content_hash
        repair_rows = [row for row in rows if row.scope == "repair"]
        assert len(repair_rows) == 2
        assert len({row.content_hash for row in repair_rows}) == 2
        assert len({row.idempotency_key for row in repair_rows}) == 2
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_build_flush_service_archive_failed_without_checkpoint_is_checkpoint_failed(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.sqlite"))
    session_manager = SessionManager(storage)
    registry = ToolRegistry()

    async def memory_save(path: str, content: str, mode: str) -> str:
        raise RuntimeError("disk full")

    registry.register(
        ToolSpec(
            name="memory_save",
            description="Save memory",
            parameters={
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string"},
            },
            required=["path", "content", "mode"],
        ),
        memory_save,
    )
    try:
        session_key = "agent:main:webchat:s1"
        session = await session_manager.create(session_key)
        service = build_flush_service(
            tool_registry=registry,
            provider_selector=SimpleNamespace(resolve=lambda: None),
            config=GatewayConfig(memory={"flush_enabled": True}),
            session_manager=session_manager,
        )

        receipt = await service.execute(
            [Message(role="user", content="temporary transcript")],
            session_key,
            agent_id="main",
        )
        rows = await storage.list_memory_durable_receipts(session_key=session_key)

        assert receipt.result_status == "archive_failed"
        assert len(rows) == 1
        assert rows[0].session_id == session.session_id
        assert rows[0].scope == "checkpoint"
        assert rows[0].status == "checkpoint_failed"
        assert rows[0].reason == "archive_failed"
        assert rows[0].content_hash == receipt.content_hash
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_build_services_registers_session_search_tool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_background_sandbox_setup(coro: Any) -> None:
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        raise AssertionError("unit tests must not schedule real sandbox setup")

    monkeypatch.setattr(
        "opensquilla.gateway.boot.create_background_task",
        fail_background_sandbox_setup,
    )
    monkeypatch.setattr(
        "opensquilla.sandbox.integration.configure_runtime",
        lambda *args, **kwargs: SimpleNamespace(effective=SimpleNamespace(as_dict=lambda: {})),
    )

    captured_memory_kwargs: dict[str, Any] = {}

    async def fake_build_memory_managers(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        captured_memory_kwargs.update(_kwargs)
        return {}

    monkeypatch.setattr(
        "opensquilla.memory.manager.build_memory_managers",
        fake_build_memory_managers,
    )
    registry = ToolRegistry()
    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
        mcp={"enabled": False},
        memory={"flush_enabled": False},
        sandbox={"auto_setup": False},
    )

    services = await build_services(
        config=config,
        tool_registry=registry,
        session_db_path=str(tmp_path / "sessions.sqlite"),
    )
    try:
        session_search = registry.get("session_search")
        assert session_search is not None
        assert "Full-text search across persisted session transcripts" in (
            session_search.spec.description
        )
        assert "defaults to curated memory source files" in (session_search.spec.description)
        assert "use source=sessions or source=all" in session_search.spec.description
        owner_names = {
            tool["name"]
            for tool in await registry.list_tools(
                caller_kind=CallerKind.AGENT,
                is_owner=True,
            )
        }
        channel_names = {
            tool.name
            for tool in registry.to_tool_definitions(
                ToolContext(is_owner=False, caller_kind=CallerKind.CHANNEL)
            )
        }
        assert "session_search" in owner_names
        assert "session_search" not in channel_names

        await services.session_manager.create("agent:main:main")
        await services.session_manager.append_message(
            "agent:main:main",
            "user",
            "needle transcript detail",
        )

        output = await session_search.handler(query="needle", limit=5)

        assert "needle" in output
        assert "agent:main:main" in output
        assert captured_memory_kwargs["session_storage"] is services.session_manager.storage
    finally:
        await services.close()


def test_router_boot_validation_does_not_load_heavy_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_dir = tmp_path / "v4_bundle"
    (bundle_dir / "runtime_src").mkdir(parents=True)
    (bundle_dir / "router.runtime.yaml").write_text("v4: {}\n", encoding="utf-8")

    config = GatewayConfig()
    config.squilla_router.v4_bundle_dir = str(bundle_dir)
    config.squilla_router.require_router_runtime = True

    real_import = builtins.__import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "opensquilla.squilla_router.v4_phase3":
            raise AssertionError("boot validation must not load V4Phase3Strategy")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    validate_squilla_router_runtime(config)


def test_router_boot_validation_still_fails_when_required_bundle_missing(tmp_path: Path) -> None:
    config = GatewayConfig()
    config.squilla_router.v4_bundle_dir = str(tmp_path / "missing")
    config.squilla_router.require_router_runtime = True

    with pytest.raises(RuntimeError, match="missing V4 bundle files"):
        validate_squilla_router_runtime(config)


def test_skill_filter_banner_accepts_tokenizers_without_transformers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from opensquilla.memory.embedding import LocalEmbeddingProvider

    def fake_find_spec(name: str):
        if name in {"onnxruntime", "tokenizers"}:
            return object()
        if name == "transformers":
            return None
        raise AssertionError(name)

    monkeypatch.setattr("importlib.util.find_spec", fake_find_spec)
    monkeypatch.setattr(
        LocalEmbeddingProvider,
        "_bundled_onnx_dir",
        classmethod(lambda cls, model_name: tmp_path),
    )

    emit_skill_filter_banner(
        SimpleNamespace(filter_enabled=True, filter_strategy="semantic", filter_embedding_model="")
    )

    assert "ONNX embedding backend not available" not in caplog.text


@pytest.mark.asyncio
async def test_build_services_fails_fast_for_explicit_remote_memory_without_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "opensquilla.sandbox.integration.configure_runtime",
        lambda *args, **kwargs: SimpleNamespace(effective=SimpleNamespace(as_dict=lambda: {})),
    )
    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        memory={"embedding": {"provider": "openai"}},
        sandbox={"auto_setup": False},
    )

    with pytest.raises(ValueError, match="memory.embedding.remote.api_key"):
        await build_services(config=config)


def test_configured_agent_ids_include_enabled_registry_agents_and_channels() -> None:
    result = upsert_channel(
        GatewayConfig(
            agents=[
                AgentEntryConfig(id="ops"),
                AgentEntryConfig(id="disabled", enabled=False),
            ]
        ),
        entry_payload={
            "type": "slack",
            "name": "work",
            "token": "x",
            "signing_secret": "ss",
            "agent_id": "channel",
        },
    )

    assert _configured_agent_ids(result.config) == ["channel", "main", "ops"]


def test_workspace_state_mismatch_emits_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[dict[str, Any]] = []
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "gateway-3"))
    monkeypatch.setenv(
        "OPENSQUILLA_GATEWAY_CONFIG_PATH",
        str(tmp_path / "gateway-3" / "config.toml"),
    )
    monkeypatch.setattr(
        "opensquilla.gateway.boot.log.warning",
        lambda event, **kwargs: warnings.append({"event": event, **kwargs}),
    )
    config = GatewayConfig(
        state_dir=str(tmp_path / "gateway-3" / "state"),
        workspace_dir=str(tmp_path / "gateway-1" / "workspace"),
        config_path=str(tmp_path / "gateway-3" / "config.toml"),
    )

    _warn_workspace_state_mismatch(config)

    assert warnings
    assert warnings[0]["event"] == "build_services.workspace_state_mismatch"
    assert "OPENSQUILLA_STATE_DIR" in warnings[0]["expected_roots"]


def test_dream_defaults_are_fail_closed() -> None:
    config = GatewayConfig()

    assert config.memory.dream.enabled is False
    assert config.memory.dream.preview_mode is True
    assert config.memory.dream.auto_schedule is False


def test_memory_mode_fingerprint_keeps_dream_auto_schedule_visible() -> None:
    config = GatewayConfig(memory={"dream": {"enabled": True}})

    assert config.memory.dream.enabled is True
    assert config.memory.dream.preview_mode is True
    assert config.memory.dream.auto_schedule is False
    assert config.memory_mode_fingerprint()["dream_auto_schedule"] == "false"


@pytest.mark.asyncio
async def test_dream_boot_does_not_register_when_auto_schedule_is_off() -> None:
    scheduler = _FakeDreamScheduler()
    config = GatewayConfig(memory={"dream": {"enabled": True}})

    await _register_dream_crons(
        scheduler=scheduler,
        memory_config=config.memory,
        agent_ids=["main"],
    )

    assert scheduler.added == []


@pytest.mark.asyncio
async def test_dream_boot_pauses_existing_jobs_when_auto_schedule_is_off() -> None:
    existing = CronJob(id="dream-main", name="memory_dream:main", status=JobStatus.PENDING)
    scheduler = _FakeDreamScheduler([existing])
    config = GatewayConfig(memory={"dream": {"enabled": True}})

    await _register_dream_crons(
        scheduler=scheduler,
        memory_config=config.memory,
        agent_ids=["main"],
    )

    assert scheduler.paused == ["dream-main"]
    assert existing.status == JobStatus.PAUSED
    assert scheduler.added == []


@pytest.mark.asyncio
async def test_dream_boot_pauses_existing_jobs_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_MEMORY_DREAM_DISABLED", "1")
    existing = CronJob(id="dream-main", name="memory_dream:main", status=JobStatus.PENDING)
    scheduler = _FakeDreamScheduler([existing])
    config = GatewayConfig(
        memory={"dream": {"enabled": True, "auto_schedule": True}},
    )

    await _register_dream_crons(
        scheduler=scheduler,
        memory_config=config.memory,
        agent_ids=["main"],
    )

    assert scheduler.paused == ["dream-main"]
    assert existing.status == JobStatus.PAUSED
    assert scheduler.added == []


@pytest.mark.asyncio
async def test_task_runtime_turn_uses_agent_registry_model_when_session_has_no_model() -> None:
    class RecordingTurnRunner:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run(self, message: str, session_key: str, **kwargs: Any):
            self.calls.append(kwargs)
            yield DoneEvent()

    class SessionManager:
        async def get_session(self, session_key: str) -> Any:
            return SimpleNamespace(model=None)

    events: list[tuple[str, str, dict[str, Any]]] = []

    async def emit(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        events.append((session_key, event_name, payload))

    config = GatewayConfig(
        agents=[AgentEntryConfig(id="ops", model="agent/default")],
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )
    run = SimpleNamespace(
        agent_id="ops",
        task_id="task-1",
        session_key="agent:ops:task-runtime",
        message="hello",
        envelope=build_cli_route_envelope(
            session_key="agent:ops:task-runtime",
            agent_id="ops",
        ),
        attachments=[],
        input_provenance={},
        run_kind="interactive",
        no_memory_capture=False,
        ingress_pipeline_steps=[],
        semantic_message=None,
        stream_event_sink=None,
    )
    runner = RecordingTurnRunner()

    await dispatch_task_runtime_turn(
        run,
        config=config,
        session_manager=SessionManager(),
        turn_runner=runner,
        event_emitter=emit,
    )

    assert runner.calls[0]["model"] == "agent/default"


@pytest.mark.asyncio
async def test_task_runtime_turn_uses_workspace_from_saved_run_context(
    tmp_path: Path,
) -> None:
    class RecordingTurnRunner:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run(self, message: str, session_key: str, **kwargs: Any):
            self.calls.append(kwargs)
            yield DoneEvent()

    default_workspace = tmp_path / "default-workspace"
    project_workspace = tmp_path / "project-workspace"
    default_workspace.mkdir()
    project_workspace.mkdir()
    envelope = build_cli_route_envelope(
        session_key="agent:main:project-task",
        agent_id="main",
    )
    envelope.metadata["sandbox_run_context"] = {
        "run_mode": "trusted",
        "workspace": str(project_workspace),
        "mounts": [],
        "domains": [],
        "bundles": [],
        "public_network": [],
        "temporary_grants": [],
    }
    object.__setattr__(envelope, "sandbox_run_context_fresh", True)
    run = SimpleNamespace(
        agent_id="main",
        task_id="task-project-workspace",
        session_key="agent:main:project-task",
        message="pwd",
        envelope=envelope,
        attachments=[],
        input_provenance={},
        run_kind="interactive",
        no_memory_capture=False,
        ingress_pipeline_steps=[],
        semantic_message=None,
        stream_event_sink=None,
    )
    runner = RecordingTurnRunner()

    async def emit(_session_key: str, _event_name: str, _payload: dict[str, Any]) -> None:
        return None

    await dispatch_task_runtime_turn(
        run,
        config=GatewayConfig(
            workspace_dir=str(default_workspace),
            agent_stream_heartbeat_interval_seconds=0.0,
            agent_stream_idle_timeout_seconds=1.0,
        ),
        session_manager=None,
        turn_runner=runner,
        event_emitter=emit,
    )

    assert runner.calls[0]["tool_context"].workspace_dir == str(project_workspace)


@pytest.mark.asyncio
async def test_task_runtime_turn_restores_bound_project_and_owner_full_default(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "queued-project.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    project_path = tmp_path / "project"
    outside = tmp_path / "outside"
    project_path.mkdir()
    outside.mkdir()
    project = await storage.create_or_restore_project_workspace(
        path=str(project_path.resolve()),
        path_key=project_path_key(project_path, strict=True),
        display_name="project",
        trusted_at=1,
    )
    key = "agent:main:webchat:queued-forged-envelope"
    await manager.create(
        key,
        workspace_id=project.workspace_id,
        origin={
            RUN_CONTEXT_ORIGIN_KEY: {
                "run_mode": "full",
                "workspace": str(outside),
            }
        },
    )
    envelope = build_cli_route_envelope(session_key=key, agent_id="main")
    envelope.metadata["sandbox_run_context"] = {
        "run_mode": "full",
        "workspace": str(outside),
    }
    object.__setattr__(envelope, "sandbox_run_context_fresh", True)
    run = SimpleNamespace(
        agent_id="main",
        task_id="queued-forged-envelope-task",
        session_key=key,
        message="pwd",
        envelope=envelope,
        attachments=[],
        input_provenance={},
        run_kind="interactive",
        no_memory_capture=False,
        ingress_pipeline_steps=[],
        semantic_message=None,
        stream_event_sink=None,
    )

    class RecordingTurnRunner:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run(self, message: str, session_key: str, **kwargs: Any):
            self.calls.append(kwargs)
            yield DoneEvent()

    runner = RecordingTurnRunner()

    async def emit(_session_key: str, _event_name: str, _payload: dict[str, Any]) -> None:
        return None

    try:
        await dispatch_task_runtime_turn(
            run,
            config=GatewayConfig(
                workspace_dir=str(tmp_path / "default"),
                agent_stream_heartbeat_interval_seconds=0.0,
                agent_stream_idle_timeout_seconds=1.0,
            ),
            session_manager=manager,
            turn_runner=runner,
            event_emitter=emit,
        )
    finally:
        await storage.close()

    tool_context = runner.calls[0]["tool_context"]
    assert tool_context.workspace_dir == project.path
    assert tool_context.run_mode == "full"
    assert envelope.metadata["sandbox_run_context"]["workspace"] == project.path


@pytest.mark.parametrize("invalid_kind", ["missing", "file", "root"])
@pytest.mark.asyncio
async def test_task_runtime_turn_rejects_unavailable_bound_project_kinds(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / f"queued-{invalid_kind}.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    project_path = tmp_path / f"{invalid_kind}-project"
    if invalid_kind == "root":
        canonical = str(Path("/").resolve())
        path_key = project_path_key(canonical, strict=True)
    else:
        project_path.mkdir()
        canonical = str(project_path.resolve())
        path_key = project_path_key(project_path, strict=True)
    project = await storage.create_or_restore_project_workspace(
        path=canonical,
        path_key=path_key,
        display_name=invalid_kind,
        trusted_at=1,
    )
    key = f"agent:main:webchat:queued-{invalid_kind}"
    await manager.create(
        key,
        workspace_id=project.workspace_id,
        origin={
            RUN_CONTEXT_ORIGIN_KEY: {
                "run_mode": "standard",
                "workspace": project.path,
            }
        },
    )
    if invalid_kind == "missing":
        project_path.rmdir()
    elif invalid_kind == "file":
        project_path.rmdir()
        project_path.write_text("not a directory", encoding="utf-8")
    envelope = build_cli_route_envelope(session_key=key, agent_id="main")
    envelope.metadata["sandbox_run_context"] = {
        "run_mode": "standard",
        "workspace": project.path,
    }
    object.__setattr__(envelope, "sandbox_run_context_fresh", True)
    run = SimpleNamespace(
        agent_id="main",
        task_id=f"queued-{invalid_kind}-task",
        session_key=key,
        message="pwd",
        envelope=envelope,
        attachments=[],
        input_provenance={},
        run_kind="interactive",
        no_memory_capture=False,
        ingress_pipeline_steps=[],
        semantic_message=None,
        stream_event_sink=None,
    )

    class RecordingTurnRunner:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run(self, message: str, session_key: str, **kwargs: Any):
            self.calls.append(kwargs)
            yield DoneEvent()

    runner = RecordingTurnRunner()

    async def emit(_session_key: str, _event_name: str, _payload: dict[str, Any]) -> None:
        return None

    try:
        with pytest.raises(ProjectWorkspaceStateError) as raised:
            await dispatch_task_runtime_turn(
                run,
                config=GatewayConfig(
                    workspace_dir=str(tmp_path / "default"),
                    agent_stream_heartbeat_interval_seconds=0.0,
                    agent_stream_idle_timeout_seconds=1.0,
                ),
                session_manager=manager,
                turn_runner=runner,
                event_emitter=emit,
            )
    finally:
        await storage.close()

    assert raised.value.reason == "unavailable"
    assert runner.calls == []


@pytest.mark.asyncio
async def test_task_runtime_turn_uses_acceptance_time_model_routing_config() -> None:
    live_config = GatewayConfig(
        squilla_router={"enabled": False, "rollout_phase": "observe"},
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )
    accepted_config = capture_model_routing_config(live_config)
    live_config.llm_ensemble.enabled = True
    live_config.squilla_router.enabled = True
    live_config.squilla_router.rollout_phase = "full"

    probe = TurnRunner.__new__(TurnRunner)
    probe._config = live_config
    observed: list[str] = []

    class RecordingTurnRunner:
        async def run(self, message: str, session_key: str, **kwargs: Any):
            observed.append(model_routing_snapshot(probe._turn_config())["mode"])
            yield DoneEvent()

    run = SimpleNamespace(
        agent_id="main",
        task_id="task-routing-snapshot",
        session_key="agent:main:routing-snapshot",
        message="hello",
        envelope=build_cli_route_envelope(
            session_key="agent:main:routing-snapshot",
            agent_id="main",
        ),
        attachments=[],
        input_provenance={},
        run_kind="interactive",
        no_memory_capture=False,
        ingress_pipeline_steps=[],
        semantic_message=None,
        stream_event_sink=None,
        accepted_config=accepted_config,
    )

    async def emit(_session_key: str, _event_name: str, _payload: dict[str, Any]) -> None:
        return None

    await dispatch_task_runtime_turn(
        run,
        config=live_config,
        session_manager=None,
        turn_runner=RecordingTurnRunner(),
        event_emitter=emit,
    )

    assert observed == ["direct"]
    assert model_routing_snapshot(live_config)["mode"] == "ensemble"


@pytest.mark.asyncio
async def test_task_runtime_turn_applies_cron_job_tool_policy() -> None:
    class RecordingTurnRunner:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run(self, message: str, session_key: str, **kwargs: Any):
            self.calls.append(kwargs)
            yield DoneEvent()

    events: list[tuple[str, str, dict[str, Any]]] = []

    async def emit(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        events.append((session_key, event_name, payload))

    job = CronJob(
        id="cron-policy",
        name="Policy",
        payload={"kind": "agent_turn", "agent_id": "ops"},
        tool_policy={
            "profile": "minimal",
            "also_allow": ["memory_search", "exec_command"],
            "deny": ["web_fetch"],
        },
    )
    run = SimpleNamespace(
        agent_id="ops",
        task_id="task-1",
        session_key="cron:cron-policy:run:1",
        message="hello",
        envelope=build_cron_route_envelope(
            job,
            session_key="cron:cron-policy:run:1",
            agent_id="ops",
        ),
        attachments=[],
        input_provenance={},
        run_kind="cron_turn",
        no_memory_capture=False,
        ingress_pipeline_steps=[],
        semantic_message=None,
        stream_event_sink=None,
    )
    runner = RecordingTurnRunner()

    await dispatch_task_runtime_turn(
        run,
        config=GatewayConfig(),
        session_manager=None,
        turn_runner=runner,
        event_emitter=emit,
    )

    tool_context = runner.calls[0]["tool_context"]
    assert tool_context.allowed_tools == {"session_status"}
    assert "exec_command" in tool_context.denied_tools
    assert "web_fetch" in tool_context.denied_tools


@pytest.mark.asyncio
async def test_task_runtime_turn_uses_owner_boundary_for_owner_cron_job() -> None:
    class RecordingTurnRunner:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run(self, message: str, session_key: str, **kwargs: Any):
            self.calls.append(kwargs)
            yield DoneEvent()

    async def emit(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        return None

    job = CronJob(
        id="cron-owner",
        name="Owner",
        payload={"kind": "agent_turn", "agent_id": "ops"},
        creator_is_owner=True,
        creator_host_execute=True,
        run_mode="full",
        elevated="full",
        execution_target="host",
        tool_policy={
            "profile": "minimal",
            "also_allow": ["memory_search", "exec_command"],
            "deny": ["web_fetch"],
        },
    )
    run = SimpleNamespace(
        agent_id="ops",
        task_id="task-1",
        session_key="cron:cron-owner:run:1",
        message="hello",
        envelope=build_cron_route_envelope(
            job,
            session_key="cron:cron-owner:run:1",
            agent_id="ops",
        ),
        attachments=[],
        input_provenance={},
        run_kind="cron_turn",
        no_memory_capture=False,
        ingress_pipeline_steps=[],
        semantic_message=None,
        stream_event_sink=None,
    )
    run.envelope.metadata["parent_session_key"] = "synthetic-parent-session"
    run.envelope.metadata["parent_task_id"] = "synthetic-parent-task"
    runner = RecordingTurnRunner()

    await dispatch_task_runtime_turn(
        run,
        config=GatewayConfig(),
        session_manager=None,
        turn_runner=runner,
        event_emitter=emit,
    )

    tool_context = runner.calls[0]["tool_context"]
    assert tool_context.task_id == "task-1"
    assert tool_context.parent_session_key == "synthetic-parent-session"
    assert tool_context.parent_task_id == "synthetic-parent-task"
    assert tool_context.is_owner is True
    assert tool_context.run_mode == "full"
    assert tool_context.elevated == "full"
    assert tool_context.allowed_tools is None
    assert tool_context.tool_policy == job.tool_policy
    assert "exec_command" not in tool_context.denied_tools


def test_default_bypass_keeps_sandbox_capability_for_explicit_restricted_calls() -> None:
    settings = _sandbox_settings_for_runtime(GatewayConfig())

    assert settings.run_mode == "safe"
    assert settings.sandbox is True
    assert settings.security_grading is True


def test_explicit_full_default_keeps_sandbox_capability_for_safe_mode() -> None:
    config = GatewayConfig(sandbox={"run_mode": "full"})

    settings = _sandbox_settings_for_runtime(config)

    assert settings.run_mode == "safe"
    assert settings.sandbox is True
    assert settings.security_grading is True
