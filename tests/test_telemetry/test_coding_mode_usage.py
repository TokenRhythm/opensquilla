from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from opensquilla.telemetry import coding_mode_usage


@pytest.mark.asyncio
async def test_current_profile_observer_requires_coding_mode_enabled(monkeypatch) -> None:
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.telemetry import runtime as runtime_module

    monkeypatch.setattr(
        GatewayConfig,
        "load",
        lambda *args, **kwargs: SimpleNamespace(
            skills=SimpleNamespace(coding_mode=False),
        ),
    )
    monkeypatch.setattr(
        runtime_module,
        "ScopedTelemetryRuntime",
        lambda **kwargs: pytest.fail("runtime must stay unopened while Coding Mode is off"),
    )

    assert await coding_mode_usage.record_current_profile_coding_mode_usage("run-1") is False


@pytest.mark.asyncio
async def test_explicit_disabled_snapshot_stays_disabled(monkeypatch) -> None:
    from opensquilla.gateway.config import GatewayConfig

    monkeypatch.setattr(
        GatewayConfig,
        "load",
        lambda *args, **kwargs: pytest.fail(
            "an explicit disabled snapshot must not rediscover a later config value"
        ),
    )

    assert (
        await coding_mode_usage.record_current_profile_coding_mode_usage(
            "run-disabled",
            coding_mode_active=False,
        )
        is False
    )


@pytest.mark.asyncio
async def test_current_profile_observer_enqueues_and_closes_local_runtime(monkeypatch) -> None:
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.telemetry import growth_sink as growth_sink_module
    from opensquilla.telemetry import runtime as runtime_module

    config = SimpleNamespace(skills=SimpleNamespace(coding_mode=True))
    observed: list[tuple[str, object]] = []

    class FakeRuntime:
        async def close(self) -> None:
            observed.append(("runtime_closed", None))

    class FakeSink:
        async def record_coding_mode_usage(self, run_id, occurred_at) -> bool:
            observed.append((run_id, occurred_at))
            return True

        async def close(self) -> None:
            observed.append(("sink_closed", None))

    runtime = FakeRuntime()
    monkeypatch.setattr(GatewayConfig, "load", lambda *args, **kwargs: config)
    monkeypatch.setattr(runtime_module, "ScopedTelemetryRuntime", lambda **kwargs: runtime)
    monkeypatch.setattr(
        growth_sink_module,
        "GrowthEventSink",
        lambda supplied_runtime, **kwargs: FakeSink(),
    )

    result = await coding_mode_usage.record_current_profile_coding_mode_usage("run-actual")

    assert result is True
    assert observed[0][0] == "run-actual"
    assert [item[0] for item in observed[1:]] == ["sink_closed", "runtime_closed"]


@pytest.mark.asyncio
async def test_current_profile_observer_uses_explicit_gate_and_config_path(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.telemetry import growth_sink as growth_sink_module
    from opensquilla.telemetry import runtime as runtime_module

    config_path = tmp_path / "selected-config.toml"
    config = SimpleNamespace(skills=SimpleNamespace(coding_mode=False))
    load_calls: list[tuple[object, object]] = []
    observed: list[tuple[str, datetime]] = []
    occurred_at = datetime(2026, 9, 11, 15, 30, tzinfo=UTC)

    class FakeRuntime:
        async def close(self) -> None:
            return None

    class FakeSink:
        async def record_coding_mode_usage(
            self,
            run_id: str,
            event_time: datetime,
        ) -> bool:
            observed.append((run_id, event_time))
            return True

        async def close(self) -> None:
            return None

    def load_config(path, *, read_only):
        load_calls.append((path, read_only))
        return config

    monkeypatch.setattr(GatewayConfig, "load", load_config)
    monkeypatch.setattr(runtime_module, "ScopedTelemetryRuntime", lambda **kwargs: FakeRuntime())
    monkeypatch.setattr(
        growth_sink_module,
        "GrowthEventSink",
        lambda supplied_runtime, **kwargs: FakeSink(),
    )

    result = await coding_mode_usage.record_current_profile_coding_mode_usage(
        "run-explicit",
        occurred_at=occurred_at,
        coding_mode_active=True,
        config_path=str(config_path),
    )

    assert result is True
    assert load_calls == [(str(config_path), True)]
    assert observed == [("run-explicit", occurred_at)]


@pytest.mark.parametrize(
    ("active_value", "expected_active"),
    [("1", True), ("0", False)],
)
def test_observer_persists_before_return_and_captures_runtime_snapshot(
    monkeypatch,
    tmp_path,
    active_value,
    expected_active,
) -> None:
    fixed_time = datetime(2026, 9, 11, 23, 59, 58, tzinfo=UTC)
    config_path = tmp_path / "active-profile.toml"
    calls: list[tuple[str, datetime, bool | None, str | None]] = []

    class FrozenDateTime:
        @classmethod
        def now(cls, timezone):
            assert timezone is UTC
            return fixed_time

    async def record(
        run_id: str,
        *,
        occurred_at: datetime | None = None,
        coding_mode_active: bool | None = None,
        config_path: str | None = None,
    ) -> bool:
        # Yield once so this assertion proves observe() drains the coroutine
        # instead of handing persistence to a disposable background task.
        await asyncio.sleep(0)
        assert occurred_at is not None
        calls.append((run_id, occurred_at, coding_mode_active, config_path))
        return True

    monkeypatch.setattr(coding_mode_usage, "datetime", FrozenDateTime)
    monkeypatch.setattr(coding_mode_usage, "record_current_profile_coding_mode_usage", record)
    monkeypatch.setenv(coding_mode_usage.CODING_MODE_ACTIVE_ENV, active_value)
    monkeypatch.setenv(coding_mode_usage.CODING_MODE_CONFIG_PATH_ENV, str(config_path))

    coding_mode_usage.observe_current_profile_coding_mode_usage("run-safe")

    assert calls == [("run-safe", fixed_time, expected_active, str(config_path))]


def test_observer_swallows_enqueue_failure(monkeypatch) -> None:

    async def fail_record(run_id: str, **kwargs) -> bool:
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(coding_mode_usage, "record_current_profile_coding_mode_usage", fail_record)

    coding_mode_usage.observe_current_profile_coding_mode_usage("run-safe")
