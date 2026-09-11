from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from opensquilla.telemetry import coding_mode_usage


@pytest.mark.asyncio
async def test_current_profile_observer_requires_coding_mode_enabled(monkeypatch) -> None:
    from opensquilla.telemetry import runtime as runtime_module

    monkeypatch.setattr(
        runtime_module,
        "ScopedTelemetryRuntime",
        lambda **kwargs: pytest.fail("runtime must stay unopened while Coding Mode is off"),
    )

    config = SimpleNamespace(skills=SimpleNamespace(coding_mode=False))
    assert (
        await coding_mode_usage.record_current_profile_coding_mode_usage(
            "run-1",
            config=config,
        )
        is False
    )


@pytest.mark.asyncio
async def test_explicit_disabled_snapshot_stays_disabled(monkeypatch) -> None:
    assert (
        await coding_mode_usage.record_current_profile_coding_mode_usage(
            "run-disabled",
            config=SimpleNamespace(skills=SimpleNamespace(coding_mode=True)),
            coding_mode_active=False,
        )
        is False
    )


@pytest.mark.asyncio
async def test_current_profile_observer_enqueues_and_closes_local_runtime(monkeypatch) -> None:
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
    monkeypatch.setattr(runtime_module, "ScopedTelemetryRuntime", lambda **kwargs: runtime)
    monkeypatch.setattr(
        growth_sink_module,
        "GrowthEventSink",
        lambda supplied_runtime, **kwargs: FakeSink(),
    )

    result = await coding_mode_usage.record_current_profile_coding_mode_usage(
        "run-actual",
        config=config,
    )

    assert result is True
    assert observed[0][0] == "run-actual"
    assert [item[0] for item in observed[1:]] == ["sink_closed", "runtime_closed"]


@pytest.mark.asyncio
async def test_current_profile_observer_uses_explicit_gate(
    monkeypatch,
) -> None:
    from opensquilla.telemetry import growth_sink as growth_sink_module
    from opensquilla.telemetry import runtime as runtime_module

    config = SimpleNamespace(skills=SimpleNamespace(coding_mode=False))
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

    monkeypatch.setattr(runtime_module, "ScopedTelemetryRuntime", lambda **kwargs: FakeRuntime())
    monkeypatch.setattr(
        growth_sink_module,
        "GrowthEventSink",
        lambda supplied_runtime, **kwargs: FakeSink(),
    )

    result = await coding_mode_usage.record_current_profile_coding_mode_usage(
        "run-explicit",
        config=config,
        occurred_at=occurred_at,
        coding_mode_active=True,
    )

    assert result is True
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
    calls: list[tuple[str, object, datetime, bool | None]] = []
    load_calls: list[str | None] = []

    class FrozenDateTime:
        @classmethod
        def now(cls, timezone):
            assert timezone is UTC
            return fixed_time

    async def record(
        run_id: str,
        *,
        config: object,
        occurred_at: datetime | None = None,
        coding_mode_active: bool | None = None,
    ) -> bool:
        # Yield once so this assertion proves observe() drains the coroutine
        # instead of handing persistence to a disposable background task.
        await asyncio.sleep(0)
        assert occurred_at is not None
        calls.append((run_id, config, occurred_at, coding_mode_active))
        return True

    config = SimpleNamespace(skills=SimpleNamespace(coding_mode=expected_active))

    def load_config(path: str | None) -> object:
        load_calls.append(path)
        return config

    monkeypatch.setattr(coding_mode_usage, "datetime", FrozenDateTime)
    monkeypatch.setattr(coding_mode_usage, "record_current_profile_coding_mode_usage", record)
    monkeypatch.setenv(coding_mode_usage.CODING_MODE_ACTIVE_ENV, active_value)
    monkeypatch.setenv(coding_mode_usage.CODING_MODE_CONFIG_PATH_ENV, str(config_path))

    coding_mode_usage.observe_current_profile_coding_mode_usage(
        "run-safe",
        config_loader=load_config,
    )

    if expected_active:
        assert load_calls == [str(config_path)]
        assert calls == [("run-safe", config, fixed_time, True)]
    else:
        assert load_calls == []
        assert calls == []


def test_observer_swallows_enqueue_failure(monkeypatch) -> None:

    async def fail_record(run_id: str, **kwargs) -> bool:
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(coding_mode_usage, "record_current_profile_coding_mode_usage", fail_record)

    coding_mode_usage.observe_current_profile_coding_mode_usage(
        "run-safe",
        config_loader=lambda path: SimpleNamespace(
            skills=SimpleNamespace(coding_mode=True)
        ),
    )
