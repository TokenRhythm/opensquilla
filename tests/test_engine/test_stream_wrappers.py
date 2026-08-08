import asyncio
import contextlib
from contextvars import ContextVar

import pytest

from opensquilla.engine import stream_wrappers
from opensquilla.engine.stream_wrappers import heartbeat_stream, idle_timeout_stream
from opensquilla.engine.types import RunHeartbeatEvent, TextDeltaEvent

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
