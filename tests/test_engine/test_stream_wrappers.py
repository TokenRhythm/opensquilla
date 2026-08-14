import asyncio
import contextlib
from contextvars import ContextVar

import pytest

from opensquilla.engine import stream_wrappers
from opensquilla.engine.stream_wrappers import (
    heartbeat_stream,
    idle_timeout_stream,
    wrap_stream,
)
from opensquilla.engine.types import (
    AnswerGenerationResetEvent,
    DoneEvent,
    RunHeartbeatEvent,
    TextDeltaEvent,
)

# A wedged wrapper must fail the assertion, not wedge the test run.
_HARD_LIMIT = 5.0


@pytest.fixture
def short_cancel_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the post-cancel grace so these tests do not pay the real 5s.

    ``raising=False`` keeps the fixture usable against a build without the
    grace, so reverting the fix makes these tests fail on the hang they are
    about rather than on a missing attribute.
    """
    monkeypatch.setattr(stream_wrappers, "_CANCEL_GRACE_SECONDS", 0.02, raising=False)


@pytest.mark.asyncio
async def test_heartbeat_stream_emits_while_upstream_is_quiet() -> None:
    async def source():
        await asyncio.sleep(0.08)
        yield TextDeltaEvent(text="done")

    events = [event async for event in heartbeat_stream(source(), interval=0.02)]

    assert any(isinstance(event, RunHeartbeatEvent) for event in events)
    assert isinstance(events[-1], TextDeltaEvent)
    assert events[-1].text == "done"


@pytest.mark.asyncio
async def test_heartbeat_stream_preserves_upstream_idle_timeout() -> None:
    async def source():
        await asyncio.sleep(0.2)
        yield TextDeltaEvent(text="late")

    wrapped = heartbeat_stream(idle_timeout_stream(source(), timeout=0.06), interval=0.02)
    events = []
    with pytest.raises(TimeoutError):
        async for event in wrapped:
            events.append(event)

    assert any(isinstance(event, RunHeartbeatEvent) for event in events)


@pytest.mark.asyncio
async def test_heartbeat_stream_preserves_upstream_generator_context() -> None:
    owner: ContextVar[str | None] = ContextVar("owner", default=None)

    async def source():
        token = owner.set("turn")
        try:
            yield TextDeltaEvent(text="first")
            yield TextDeltaEvent(text="second")
        finally:
            owner.reset(token)

    events = [event async for event in heartbeat_stream(source(), interval=0.02)]

    text_events = [event for event in events if isinstance(event, TextDeltaEvent)]
    assert [event.text for event in text_events] == ["first", "second"]
    assert owner.get() is None


@pytest.mark.asyncio
async def test_upstream_run_heartbeat_resets_idle_timeout_stream() -> None:
    async def source():
        for _ in range(5):
            await asyncio.sleep(0.03)
            yield RunHeartbeatEvent(phase="tool", message="tool still running")
        await asyncio.sleep(0.03)
        yield TextDeltaEvent(text="done")

    events = [event async for event in idle_timeout_stream(source(), timeout=0.15)]

    assert [event.kind for event in events] == ["run_heartbeat"] * 5 + ["text_delta"]


@pytest.mark.asyncio
async def test_idle_timeout_fires_when_upstream_cleanup_blocks(
    short_cancel_grace: None,
) -> None:
    """An upstream whose ``finally`` never finishes must not hold the deadline.

    This is the gateway-hang shape: the socket is up but dead, so cancelling
    the read lands but the cleanup that follows it never completes.
    """
    release = asyncio.Event()

    async def source():
        try:
            await asyncio.Event().wait()
            yield TextDeltaEvent(text="never")
        finally:
            await release.wait()

    try:
        async with asyncio.timeout(_HARD_LIMIT):
            with pytest.raises(TimeoutError, match="Stream idle"):
                async for _event in idle_timeout_stream(source(), timeout=0.02):
                    pass
    finally:
        release.set()


@pytest.mark.asyncio
async def test_idle_timeout_fires_when_upstream_refuses_cancellation(
    short_cancel_grace: None,
) -> None:
    """A retry loop that swallows ``CancelledError`` must not outlive the deadline."""
    release = asyncio.Event()

    async def source():
        try:
            await release.wait()
        except asyncio.CancelledError:
            # Refuse the first cancellation the way a retry loop would, then
            # stay parked on something the test can free so nothing outlives it.
            await release.wait()
        yield TextDeltaEvent(text="never")

    try:
        async with asyncio.timeout(_HARD_LIMIT):
            with pytest.raises(TimeoutError, match="Stream idle"):
                async for _event in idle_timeout_stream(source(), timeout=0.02):
                    pass
    finally:
        release.set()


@pytest.mark.asyncio
async def test_heartbeat_stream_closes_over_a_wedged_upstream(
    short_cancel_grace: None,
) -> None:
    """Closing the heartbeat wrapper must not block on the driver it cancels.

    Asserted on elapsed time rather than an outer timeout: the wrapper used to
    suppress ``CancelledError`` around ``await driver``, so a timeout aimed at
    this code was swallowed by the very line under test.
    """
    release = asyncio.Event()

    async def source():
        try:
            yield TextDeltaEvent(text="first")
            await asyncio.Event().wait()
            yield TextDeltaEvent(text="never")
        finally:
            await release.wait()

    async def free_upstream_eventually() -> None:
        # Guarantees the test ends even if cleanup blocks, so a regression shows
        # up as a slow close rather than a wedged suite.
        await asyncio.sleep(1.0)
        release.set()

    releaser = asyncio.create_task(free_upstream_eventually())
    stream = heartbeat_stream(source(), interval=0.02)
    loop = asyncio.get_running_loop()
    try:
        async for event in stream:
            if isinstance(event, TextDeltaEvent):
                break
        # Let the driver re-enter the upstream before closing. Without this the
        # cancellation lands before the upstream is parked, its wedged cleanup
        # never runs, and the assertion below cannot see the regression.
        await asyncio.sleep(0.05)

        started = loop.time()
        await stream.aclose()
        elapsed = loop.time() - started
    finally:
        release.set()
        releaser.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await releaser

    assert elapsed < 0.5, f"closing waited {elapsed:.2f}s on a wedged upstream"


@pytest.mark.asyncio
async def test_heartbeat_recovered_orphan_closes_its_upstream(
    short_cancel_grace: None,
) -> None:
    """A detached heartbeat driver must close a late-yielding upstream before exit."""
    release = asyncio.Event()
    finalized = asyncio.Event()

    async def source():
        try:
            yield TextDeltaEvent(text="first")
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            yield TextDeltaEvent(text="recovered too late")
            await asyncio.Event().wait()
            yield TextDeltaEvent(text="never")
        finally:
            finalized.set()

    already_parked = set(stream_wrappers._ORPHANED_TASKS)
    parked: set[asyncio.Task[object]] = set()
    stream = heartbeat_stream(source(), interval=0.02)
    try:
        async with asyncio.timeout(_HARD_LIMIT):
            async for event in stream:
                if isinstance(event, TextDeltaEvent):
                    break
            await asyncio.sleep(0.05)
            await stream.aclose()

            parked = set(stream_wrappers._ORPHANED_TASKS) - already_parked
            assert parked, "the cancellation-resistant heartbeat driver should be parked"

            release.set()
            for _ in range(100):
                if not parked & stream_wrappers._ORPHANED_TASKS:
                    break
                await asyncio.sleep(0.01)

        assert not parked & stream_wrappers._ORPHANED_TASKS
        assert finalized.is_set(), "the recovered upstream should be closed before exit"
    finally:
        release.set()
        for task in parked:
            task.cancel()
        if parked:
            await asyncio.gather(*parked, return_exceptions=True)


@pytest.mark.asyncio
async def test_abandoned_upstream_is_released_once_it_finally_ends(
    short_cancel_grace: None,
) -> None:
    """The orphan set holds a wedged task only until it actually finishes.

    Scoped to the task this test parks: the set is module state shared by every
    test in the process, so asserting it is empty would make this test report
    another one's leftovers.
    """
    release = asyncio.Event()

    async def source():
        try:
            await asyncio.Event().wait()
            yield TextDeltaEvent(text="never")
        finally:
            await release.wait()

    already_parked = set(stream_wrappers._ORPHANED_TASKS)

    async with asyncio.timeout(_HARD_LIMIT):
        with pytest.raises(TimeoutError, match="Stream idle"):
            async for _event in idle_timeout_stream(source(), timeout=0.02):
                pass

        parked = set(stream_wrappers._ORPHANED_TASKS) - already_parked
        assert parked, "a wedged task should be parked, not dropped"

        release.set()
        for _ in range(100):
            if not parked & stream_wrappers._ORPHANED_TASKS:
                break
            await asyncio.sleep(0.01)

    assert not parked & stream_wrappers._ORPHANED_TASKS


@pytest.mark.asyncio
async def test_abandoned_upstream_exits_after_ignored_cancellation_recovers(
    short_cancel_grace: None,
) -> None:
    """A recovered orphan must stop instead of waiting forever for another pull."""
    release = asyncio.Event()
    finalized = asyncio.Event()

    async def source():
        try:
            try:
                await release.wait()
            except asyncio.CancelledError:
                # Ignore the wrapper's cancellation once, then recover and
                # produce the event that used to leave the driver orphaned.
                await release.wait()
            yield TextDeltaEvent(text="recovered too late")
        finally:
            finalized.set()

    already_parked = set(stream_wrappers._ORPHANED_TASKS)
    parked: set[asyncio.Task[object]] = set()
    try:
        async with asyncio.timeout(_HARD_LIMIT):
            with pytest.raises(TimeoutError, match="Stream idle"):
                async for _event in idle_timeout_stream(source(), timeout=0.02):
                    pass

            parked = set(stream_wrappers._ORPHANED_TASKS) - already_parked
            assert parked, "the cancellation-resistant driver should initially be parked"

            release.set()
            for _ in range(100):
                if not parked & stream_wrappers._ORPHANED_TASKS:
                    break
                await asyncio.sleep(0.01)

        assert not parked & stream_wrappers._ORPHANED_TASKS
        assert finalized.is_set(), "the recovered upstream should be closed before release"
    finally:
        # Keep a regression bounded and isolated from later tests.
        release.set()
        for task in parked:
            task.cancel()
        if parked:
            await asyncio.gather(*parked, return_exceptions=True)


@pytest.mark.asyncio
async def test_idle_timeout_propagates_upstream_cancelled_error_immediately() -> None:
    """An upstream cancellation is a result, not silence that becomes an idle timeout."""

    async def source():
        raise asyncio.CancelledError("upstream cancelled itself")
        yield TextDeltaEvent(text="unreachable")

    with pytest.raises(asyncio.CancelledError, match="upstream cancelled itself"):
        async with asyncio.timeout(0.5):
            async for _event in idle_timeout_stream(source(), timeout=_HARD_LIMIT):
                pass


@pytest.mark.asyncio
async def test_heartbeat_propagates_upstream_cancelled_error_immediately() -> None:
    """The default heartbeat composition must preserve upstream cancellation too."""

    async def source():
        raise asyncio.CancelledError("upstream cancelled itself")
        yield TextDeltaEvent(text="unreachable")

    wrapped = heartbeat_stream(
        idle_timeout_stream(source(), timeout=_HARD_LIMIT),
        interval=0.01,
    )
    with pytest.raises(asyncio.CancelledError, match="upstream cancelled itself"):
        async with asyncio.timeout(0.5):
            async for _event in wrapped:
                pass


@pytest.mark.asyncio
async def test_repeated_caller_cancellation_keeps_pending_driver_bookkept() -> None:
    """A second cancellation may interrupt grace without dropping the orphan reference."""
    release = asyncio.Event()
    started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    stop_requested = asyncio.Event()

    async def cancellation_resistant_work() -> None:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release.wait()

    driver = asyncio.create_task(cancellation_resistant_work())
    settle: asyncio.Task[None] | None = None
    try:
        async with asyncio.timeout(_HARD_LIMIT):
            await started.wait()
            settle = asyncio.create_task(
                stream_wrappers._settle_or_abandon(driver, stop_requested)
            )
            await cancellation_seen.wait()

            settle.cancel()
            with pytest.raises(asyncio.CancelledError):
                await settle

            assert driver in stream_wrappers._ORPHANED_TASKS
            release.set()
            for _ in range(100):
                if driver not in stream_wrappers._ORPHANED_TASKS:
                    break
                await asyncio.sleep(0.01)

        assert driver not in stream_wrappers._ORPHANED_TASKS
    finally:
        release.set()
        if settle is not None and not settle.done():
            settle.cancel()
        driver.cancel()
        await asyncio.gather(
            *(task for task in (settle, driver) if task is not None),
            return_exceptions=True,
        )


@pytest.mark.asyncio
async def test_idle_timeout_keeps_one_context_across_events() -> None:
    """The upstream must resume in the context it started in, every event.

    ``TurnRunner.run`` sets the session-lock owner before its first event, reads
    it back between events to detect re-entry, and resets the token in a
    ``finally``. Driving each ``__anext__`` in a task with its own context copy
    silently loses the value and then fails the reset with "created in a
    different Context" — a whole-turn crash no single-event test would catch.
    """
    owner: ContextVar[str | None] = ContextVar("lock_owner", default=None)
    seen: list[str | None] = []

    async def source():
        token = owner.set("turn")
        try:
            for index in range(3):
                seen.append(owner.get())
                yield TextDeltaEvent(text=f"event-{index}")
        finally:
            owner.reset(token)

    async with asyncio.timeout(_HARD_LIMIT):
        events = [event async for event in idle_timeout_stream(source(), timeout=1.0)]

    assert [event.text for event in events] == ["event-0", "event-1", "event-2"]
    assert seen == ["turn", "turn", "turn"]


@pytest.mark.asyncio
async def test_idle_timeout_keeps_one_owner_task_across_events() -> None:
    """The task the upstream runs on must outlive the event it produced.

    ``notify_compaction`` registers ``asyncio.current_task()`` as the owner of
    an in-flight compaction and publishes a terminal event when that task ends.
    An upstream driven by a task per event would hand it an owner that dies with
    the event, terminating a compaction that is still running.
    """
    producers: list[asyncio.Task[object] | None] = []

    async def source():
        for index in range(3):
            producers.append(asyncio.current_task())
            yield TextDeltaEvent(text=f"event-{index}")

    alive_when_delivered: list[bool] = []
    async with asyncio.timeout(_HARD_LIMIT):
        async for _event in idle_timeout_stream(source(), timeout=1.0):
            producer = producers[-1]
            assert producer is not None
            alive_when_delivered.append(not producer.done())

    assert len(set(producers)) == 1, "the upstream ran on a different task per event"
    assert all(alive_when_delivered), "the owner task died with the event it produced"


@pytest.mark.asyncio
async def test_idle_timeout_does_not_run_the_upstream_ahead_of_the_consumer() -> None:
    """Handing the upstream to a driver must not turn a pull into a push.

    A plain producer task would keep calling ``__anext__`` while the consumer is
    still handling the previous event, so the turn would run tool calls the
    caller has not seen yet.
    """
    produced: list[int] = []

    async def source():
        for index in range(4):
            produced.append(index)
            yield TextDeltaEvent(text=f"event-{index}")

    consumed: list[str] = []
    async with asyncio.timeout(_HARD_LIMIT):
        async for event in idle_timeout_stream(source(), timeout=1.0):
            assert len(produced) == len(consumed) + 1, (
                f"upstream ran ahead: produced={produced} consumed={consumed}"
            )
            consumed.append(event.text)

    assert consumed == ["event-0", "event-1", "event-2", "event-3"]


@pytest.mark.asyncio
async def test_idle_timeout_closes_the_upstream_when_the_consumer_stops_early() -> None:
    """Closing the wrapper must run the upstream's own cleanup."""
    finalized: list[str] = []

    async def source():
        try:
            for index in range(100):
                yield TextDeltaEvent(text=f"event-{index}")
        finally:
            finalized.append("closed")

    stream = idle_timeout_stream(source(), timeout=1.0)
    async with asyncio.timeout(_HARD_LIMIT):
        async for _event in stream:
            break
        await stream.aclose()

    assert finalized == ["closed"]


@pytest.mark.asyncio
async def test_idle_timeout_still_propagates_upstream_failures() -> None:
    """Rewriting the wait must not swallow the upstream's own exception."""

    async def source():
        yield TextDeltaEvent(text="first")
        raise RuntimeError("upstream exploded")

    seen = []
    async with asyncio.timeout(_HARD_LIMIT):
        with pytest.raises(RuntimeError, match="upstream exploded"):
            async for event in idle_timeout_stream(source(), timeout=1.0):
                seen.append(event)

    assert [event.text for event in seen] == ["first"]


@pytest.mark.asyncio
async def test_context_bound_stream_does_not_let_legacy_timeout_create_terminal() -> None:
    async def source():
        await asyncio.sleep(0.04)
        yield AnswerGenerationResetEvent(
            turn_id="turn-context-bound",
            assistant_message_id="assistant-context-bound",
            old_generation_epoch=0,
            new_generation_epoch=1,
            safe_reason="canonical ensemble takeover",
            sequence=1,
        )
        yield DoneEvent(text="fixed answer")

    events = [
        event
        async for event in wrap_stream(
            source(),
            idle_timeout=0.001,
            heartbeat_interval=None,
            context_bound=True,
        )
    ]

    assert [event.kind for event in events] == ["answer_generation_reset", "done"]


@pytest.mark.asyncio
async def test_unmarked_stream_keeps_legacy_idle_timeout_behavior() -> None:
    async def source():
        await asyncio.sleep(0.04)
        yield TextDeltaEvent(text="late")

    with pytest.raises(TimeoutError, match="Stream idle"):
        async for _event in wrap_stream(
            source(),
            idle_timeout=0.001,
            heartbeat_interval=None,
        ):
            pass
