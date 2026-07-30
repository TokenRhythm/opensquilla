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
async def test_post_dream_hook_invoked_on_successful_run() -> None:
    """When build_dream succeeds the hook fires with the same agent_id."""

    class _StubDream:
        def __init__(self, agent_id: str) -> None:
            self.agent_id = agent_id

        async def run(self) -> object:
            class _R:
                files_processed = 0
                evidence_status = "ok"
                apply_status = "ok"
            return _R()

    captured: list[tuple[str, str]] = []

    async def hook(agent_id: str, dream_summary: str) -> None:
        captured.append((agent_id, dream_summary))

    handler = make_memory_dream_handler(
        build_dream=lambda aid: _StubDream(aid),
        post_dream_hook=hook,
    )
    result = await handler(CronJob(id="dream-x", payload={"agent_id": "agent-x"}))
    assert result.summary.startswith("dream agent=agent-x")
    assert captured == [("agent-x", result.summary)]


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
async def test_post_dream_hook_exception_does_not_poison_handler_result() -> None:
    """Hook failure logs an exception but the dream's HandlerResult
    must still reflect the successful dream — observability cannot
    convert a working dream into a failed one."""

    class _StubDream:
        async def run(self) -> object:
            class _R:
                files_processed = 1
                evidence_status = "ok"
                apply_status = "ok"
            return _R()

    async def hook(_agent_id: str, _dream_summary: str) -> None:
        raise RuntimeError("auto_propose blew up")

    handler = make_memory_dream_handler(
        build_dream=lambda _aid: _StubDream(),
        post_dream_hook=hook,
    )
    result = await handler(CronJob(id="dream-y", payload={"agent_id": "main"}))
    assert result.summary.startswith("dream agent=main")
    assert "blew up" not in result.summary


@pytest.mark.asyncio
async def test_post_dream_hook_not_called_when_dream_skipped() -> None:
    """Predicate-skip path short-circuits before the hook would fire."""

    fired = False

    async def hook(_agent_id: str, _dream_summary: str) -> None:
        nonlocal fired
        fired = True

    def build(_aid: str) -> object:
        raise AssertionError("dream should not be built when skipped")

    handler = make_memory_dream_handler(
        build_dream=build,
        should_skip=lambda: "disabled",
        post_dream_hook=hook,
    )
    result = await handler(CronJob(id="dream-z", payload={"agent_id": "main"}))
    assert result.delivery_status == "skipped"
    assert fired is False


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
