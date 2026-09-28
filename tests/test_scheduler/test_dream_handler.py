from __future__ import annotations

import pytest

from opensquilla.memory.dream_factory import _session_lock_for
from opensquilla.scheduler.dream_handler import make_memory_dream_handler
from opensquilla.scheduler.types import CronJob


@pytest.mark.asyncio
async def test_memory_dream_handler_skips_without_building_dream() -> None:
    def build_dream(agent_id: str) -> object:
        raise AssertionError(f"dream should not be built for {agent_id}")

    handler = make_memory_dream_handler(build_dream, should_skip=lambda: "disabled")

    result = await handler(
        CronJob(
            id="dream-main",
            name="memory_dream:main",
            payload={"agent_id": "main"},
        )
    )

    assert result.summary == "dream skipped: disabled"
    assert result.delivery_status == "skipped"


def test_dream_factory_uses_public_turn_runner_lock_surface() -> None:
    class _Runner:
        def __init__(self) -> None:
            self.keys: list[str] = []
            self.lock = object()

        def get_session_lock(self, key: str) -> object:
            self.keys.append(key)
            return self.lock

        def _get_session_lock(self, _key: str) -> object:
            raise AssertionError("private lock surface should not be used")

    runner = _Runner()

    assert _session_lock_for(runner, "main") is runner.lock
    assert runner.keys == ["memory_dream:main"]


@pytest.mark.asyncio
async def test_memory_dream_handler_binds_synthetic_usage_scope() -> None:
    from opensquilla.engine.usage_accounting import current_usage_accounting_scope

    observed = []

    class _StubDream:
        async def run(self) -> object:
            scope = current_usage_accounting_scope()
            assert scope is not None
            observed.append(scope.context)

            class _R:
                files_processed = 0
                evidence_status = "ok"
                apply_status = "ok"

            return _R()

    handler = make_memory_dream_handler(
        build_dream=lambda _aid: _StubDream(),
        usage_event_sink=object(),
    )
    result = await handler(CronJob(id="dream-worker", payload={"agent_id": "worker"}))

    assert result.summary.startswith("dream agent=worker")
    assert len(observed) == 1
    assert observed[0].agent_id == "worker"
    assert observed[0].run_kind == "memory_dream"
    assert observed[0].session_id


@pytest.mark.asyncio
async def test_memory_dream_handler_kill_switch_skips_before_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_MEMORY_DREAM_DISABLED", "1")

    def build_dream(agent_id: str) -> object:
        raise AssertionError(f"dream should not be built for {agent_id}")

    def should_skip() -> str | None:
        raise AssertionError("kill switch should short-circuit the guard")

    handler = make_memory_dream_handler(build_dream, should_skip=should_skip)

    result = await handler(CronJob(id="dream-main", payload={"agent_id": "main"}))

    assert result.summary == "dream skipped: kill_switch"
    assert result.delivery_status == "skipped"
