from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.gateway.boot import (
    ServiceContainer,
    build_services,
    build_turn_runner_from_services,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.sandbox.integration import reset_runtime


class FakeReliabilitySink:
    def observe_turn(self, _facts) -> None:
        return None

    def observe_tool_call(self, _facts) -> None:
        return None

    def observe_file_parse(self, _facts) -> None:
        return None


class FakeGrowthSink:
    def observe_turn_started(self) -> None:
        return None

    def observe_turn_succeeded(self) -> None:
        return None


async def test_service_container_closes_scoped_telemetry_after_producers() -> None:
    calls: list[str] = []

    class FakeTaskRuntime:
        async def shutdown(self) -> None:
            calls.append("task_runtime")

    class FakeTelemetryRuntime:
        def prepare_shutdown(self) -> None:
            calls.append("prepare_telemetry_shutdown")

        async def close(self) -> None:
            calls.append("telemetry_runtime")

    class FakeGrowthSink:
        async def close(self) -> None:
            calls.append("growth_event_sink")

    class FakeStandaloneUsage:
        async def close(self) -> None:
            calls.append("standalone_usage")

    container = ServiceContainer(
        config=SimpleNamespace(),
        task_runtime=FakeTaskRuntime(),
        growth_event_sink=FakeGrowthSink(),
        telemetry_runtime=FakeTelemetryRuntime(),
        standalone_usage_telemetry=FakeStandaloneUsage(),
    )

    await container.close()

    assert calls == [
        "task_runtime", "standalone_usage", "prepare_telemetry_shutdown",
        "growth_event_sink", "telemetry_runtime",
    ]
    assert container.standalone_usage_telemetry is None
    assert container.growth_event_sink is None
    assert container.telemetry_runtime is None


def test_turn_runner_receives_only_content_free_sink_methods() -> None:
    reliability_sink = FakeReliabilitySink()
    growth_sink = FakeGrowthSink()
    services = SimpleNamespace(
        config=SimpleNamespace(),
        provider_selector=None,
        tool_registry=None,
        session_manager=None,
        skill_loader=None,
        usage_tracker=None,
        reliability_event_sink=reliability_sink,
        growth_event_sink=growth_sink,
    )

    runner = build_turn_runner_from_services(services)

    assert runner._turn_reliability_sink == reliability_sink.observe_turn
    assert runner._tool_reliability_sink == reliability_sink.observe_tool_call
    assert runner._file_parse_reliability_sink == reliability_sink.observe_file_parse
    assert runner._turn_growth_started_sink == growth_sink.observe_turn_started
    assert runner._turn_growth_succeeded_sink == growth_sink.observe_turn_succeeded


@pytest.mark.parametrize("start_fails", [False, True])
async def test_service_boot_starts_growth_replay_and_closes_failed_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, start_fails: bool,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))
    calls: list[str] = []

    class Runtime:
        def __init__(self, **_kwargs):
            pass

        async def start(self):
            calls.append("runtime_start")

        async def close(self):
            calls.append("runtime_close")

    class Sink:
        def __init__(self, *_args, **_kwargs):
            pass

        async def start(self):
            calls.append("replay_start")
            if start_fails:
                raise OSError("synthetic replay initialization failure")

        async def close(self):
            calls.append("replay_close")

    def reject_sandbox_setup(coro):
        coro.close()
        raise AssertionError("offline test must not start sandbox setup")

    monkeypatch.setattr("opensquilla.gateway.boot.create_background_task", reject_sandbox_setup)
    monkeypatch.setattr("opensquilla.telemetry.runtime.ScopedTelemetryRuntime", Runtime)
    monkeypatch.setattr("opensquilla.telemetry.growth_sink.GrowthEventSink", Sink)
    monkeypatch.setattr("opensquilla.telemetry.reliability_sink.ReliabilityEventSink", Sink)
    services = await build_services(
        config=GatewayConfig(memory={}),
        session_db_path=":memory:", seed_agent_workspaces=False,
    )
    try:
        assert calls[0] == "replay_start"
        assert (services.growth_event_sink is None) is start_fails
        assert (services.telemetry_runtime is None) is start_fails
    finally:
        await services.close()
        reset_runtime()
    assert calls == ["replay_start", "replay_close", "runtime_close"]


@pytest.mark.parametrize("desktop", [False, True])
def test_gateway_start_ready_event_has_one_source_owner(monkeypatch, desktop):
    from opensquilla.gateway.boot import _record_gateway_ready_telemetry
    from opensquilla.telemetry.contracts.common import ResultOutcome

    calls = []
    monkeypatch.setattr("opensquilla.paths.desktop_profile_lifecycle_active", lambda: desktop)
    services = ServiceContainer(
        config=SimpleNamespace(),
        reliability_event_sink=SimpleNamespace(observe_gateway_start=lambda **kw: calls.append(kw)),
    )
    _record_gateway_ready_telemetry(services, duration_ms=123)
    assert calls == ([] if desktop else [{
        "outcome": ResultOutcome.SUCCESS, "error_code": None, "failure_stage": None,
        "duration_ms": 123,
    }])
