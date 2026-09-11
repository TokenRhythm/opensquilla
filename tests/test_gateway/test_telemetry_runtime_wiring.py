from __future__ import annotations

from types import SimpleNamespace

from opensquilla.gateway.boot import ServiceContainer, build_turn_runner_from_services


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
        async def close(self) -> None:
            calls.append("telemetry_runtime")

    class FakeGrowthSink:
        async def close(self) -> None:
            calls.append("growth_event_sink")

    container = ServiceContainer(
        config=SimpleNamespace(),
        task_runtime=FakeTaskRuntime(),
        growth_event_sink=FakeGrowthSink(),
        telemetry_runtime=FakeTelemetryRuntime(),
    )

    await container.close()

    assert calls == ["task_runtime", "growth_event_sink", "telemetry_runtime"]
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
