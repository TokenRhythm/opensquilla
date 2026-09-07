from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from opensquilla.application.turn_admission import CancelTurn
from opensquilla.application.turn_cancellation import (
    CancellationTiming,
    ExactCancellationUnavailableError,
    TurnCancellation,
)


class _CancellationPrimitives:
    session_available = True
    runtime_available = True

    def __init__(self) -> None:
        self.started: list[str] = []
        self.cleanup_release = asyncio.Event()

    async def session_exists(self, key: str) -> bool:
        # Lookup must not postpone the three independent safety operations.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert set(self.started) == {"runtime", "auxiliary", "descendants"}
        return True

    async def cancel_runtime(self, key: str, task_id: str | None, source: str) -> int:
        assert (key, task_id, source) == ("agent:main:webchat:one", "task-one", "webui_abort")
        self.started.append("runtime")
        return 1

    async def cancel_auxiliary(self, key: str, task_id: str, deadline: float) -> int:
        self.started.append("auxiliary")
        await self.cleanup_release.wait()
        return 0

    async def cancel_descendants(
        self,
        key: str,
        task_id: str,
        source: str,
        deadline: float,
    ) -> int:
        self.started.append("descendants")
        await self.cleanup_release.wait()
        return 0

    async def bounded[T](
        self,
        operation: Awaitable[T],
        deadline: float,
        label: str,
        default: T,
    ) -> T:
        return await operation

    async def observe[T](
        self,
        task: asyncio.Task[T],
        deadline: float,
        label: str,
        default: T,
    ) -> T:
        return await task


async def test_exact_cancel_starts_cleanup_before_lookup_without_waiting_for_cleanup() -> None:
    ports = _CancellationPrimitives()
    application = TurnCancellation(ports, timing=CancellationTiming(), clock=lambda: 1.0)
    try:
        result = await application.cancel(
            CancelTurn("agent:main:webchat:one", "webchat", "task-one", True, None)
        )
        assert result == {"aborted": True, "key": "agent:main:webchat:one"}
        assert not ports.cleanup_release.is_set()
    finally:
        ports.cleanup_release.set()
        await asyncio.sleep(0)


async def test_missing_exact_identity_never_invokes_runtime_primitives() -> None:
    ports = _CancellationPrimitives()
    application = TurnCancellation(ports, timing=CancellationTiming(), clock=lambda: 1.0)
    result = await application.cancel(
        CancelTurn("agent:main:webchat:one", "webchat", None, True, None)
    )
    assert result["reason"] == "task_id_required"
    assert ports.started == []


async def test_exact_identity_cannot_fall_back_to_session_registry() -> None:
    ports = _CancellationPrimitives()
    ports.runtime_available = False

    async def exists(key: str) -> bool:
        return True

    ports.session_exists = exists
    application = TurnCancellation(ports, timing=CancellationTiming(), clock=lambda: 1.0)
    result = await application.cancel(
        CancelTurn("agent:main:webchat:one", "webchat", "task-one", True, None)
    )
    assert result["reason"] == "task_scope_unsupported"
    assert ports.started == []


async def test_unsupported_exact_cancel_observes_cleanup_but_never_widens() -> None:
    class UnsupportedRuntime(_CancellationPrimitives):
        async def cancel_runtime(self, key: str, task_id: str | None, source: str) -> int:
            self.started.append("runtime")
            raise ExactCancellationUnavailableError

    ports = UnsupportedRuntime()
    ports.cleanup_release.set()
    result = await TurnCancellation(
        ports,
        timing=CancellationTiming(),
        clock=lambda: 1.0,
    ).cancel(CancelTurn("agent:main:webchat:one", "webchat", "task-one", True, None))
    assert result["reason"] == "task_scope_unsupported"
    assert ports.started.count("runtime") == 1
    assert set(ports.started) == {"runtime", "auxiliary", "descendants"}


async def test_response_deadline_does_not_cancel_independent_safety_cleanup() -> None:
    class UnsettledRuntime(_CancellationPrimitives):
        def __init__(self) -> None:
            super().__init__()
            self.runtime_settled = asyncio.Event()

        async def cancel_runtime(self, key: str, task_id: str | None, source: str) -> int:
            self.started.append("runtime")
            await self.cleanup_release.wait()
            self.runtime_settled.set()
            return 1

        async def observe[T](
            self,
            task: asyncio.Task[T],
            deadline: float,
            label: str,
            default: T,
        ) -> T:
            assert label == "cancel_requested_runtime_task"
            assert not task.done()
            return default

    ports = UnsettledRuntime()
    try:
        result = await TurnCancellation(
            ports,
            timing=CancellationTiming(),
            clock=lambda: 1.0,
        ).cancel(CancelTurn("agent:main:webchat:one", "webchat", "task-one", True, None))
        assert result["reason"] == "task_cancel_unknown"
        assert not ports.runtime_settled.is_set()
    finally:
        ports.cleanup_release.set()
        await asyncio.wait_for(ports.runtime_settled.wait(), timeout=1)
