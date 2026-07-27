from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest

from opensquilla.cli.tui.adapters.runtime_helpers import classify_chat_input
from opensquilla.cli.tui.backend.contracts import (
    TuiInputKind,
    TuiRuntimeConfig,
    TuiRuntimeHooks,
    TuiSubmittedInput,
)
from opensquilla.cli.tui.backend.runtime import run_tui_runtime
from opensquilla.cli.tui.backend.state import TuiRuntimeState
from opensquilla.engine.agent_injection import PendingInputProvider
from opensquilla.engine.commands import Surface


class _FakeSurface:
    def __init__(
        self,
        inputs: asyncio.Queue[str | None],
        *,
        on_next_line: Callable[[], None] | None = None,
    ) -> None:
        self._inputs = inputs
        self._on_next_line = on_next_line
        self.next_line_calls = 0
        self.cancel_callbacks: list[Any] = []
        self.shutdown_callbacks: list[Any] = []
        self.writes: list[str] = []
        self.eof_count = 0
        self.redraw_count = 0

    async def next_line(self) -> str | None:
        self.next_line_calls += 1
        if self._on_next_line is not None:
            self._on_next_line()
        return await self._inputs.get()

    def set_cancel_callback(self, cb) -> None:  # noqa: ANN001
        self.cancel_callbacks.append(cb)

    def set_shutdown_callback(self, cb) -> None:  # noqa: ANN001
        self.shutdown_callbacks.append(cb)

    def emit_eof(self) -> None:
        self.eof_count += 1
        self._inputs.put_nowait(None)

    async def write_through(self, payload: str) -> None:
        self.writes.append(payload)

    @property
    def redraw_callback(self) -> Callable[[], None]:
        return self._invalidate

    def _invalidate(self) -> None:
        self.redraw_count += 1


def _surface_factory(surface: _FakeSurface):
    @asynccontextmanager
    async def _factory() -> AsyncIterator[_FakeSurface]:
        yield surface

    return _factory


async def _noop_echo(surface: Any, text: str) -> None:
    await surface.write_through(f"echo:{text}")


async def _queued_echo(surface: Any) -> None:
    await surface.write_through("queued")


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("timed out waiting for runtime condition")
        await asyncio.sleep(0)


def _runtime_config(**kwargs: Any) -> TuiRuntimeConfig:
    return TuiRuntimeConfig(task_name="chat-turn-test", **kwargs)


def _runtime_hooks(**kwargs: Any) -> TuiRuntimeHooks:
    return TuiRuntimeHooks(
        on_user_input_echo=_noop_echo,
        on_queued_turn_start=_queued_echo,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_runtime_ignores_blank_input_lines() -> None:
    """A blank Enter is never a message: no dispatch, no echo, no queue entry.

    Surfaces guard this too, but the backend is the defense shared by every
    frontend — a phantom empty prompt card queued behind a running turn was
    the visible failure.
    """
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    state = TuiRuntimeState()
    executed: list[str] = []
    echoed: list[str] = []

    async def _dispatch(user_input: str) -> bool:
        executed.append(user_input)
        return True

    async def _echo(_surface: Any, text: str) -> None:
        echoed.append(text)

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(state=state),
            hooks=TuiRuntimeHooks(
                on_user_input_echo=_echo,
                on_queued_turn_start=_queued_echo,
            ),
        )
    )
    inputs.put_nowait("")
    inputs.put_nowait("   ")
    inputs.put_nowait("\t")
    inputs.put_nowait("real message")
    await _wait_until(lambda: executed == ["real message"])
    inputs.put_nowait(None)
    await task

    assert executed == ["real message"]
    assert echoed == ["real message"]
    assert state.pending_items == ()


def test_runtime_state_drains_pending_inputs_for_agent_injection() -> None:
    state = TuiRuntimeState()
    state.enqueue("second")
    state.enqueue("third")

    assert isinstance(state, PendingInputProvider)
    assert state.drain_pending() == ["second", "third"]
    assert state.pending_items == ()


@pytest.mark.asyncio
async def test_runtime_allows_active_turn_to_drain_mid_turn_input() -> None:
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    state = TuiRuntimeState()
    executed: list[str] = []
    drained: list[str] = []
    first_started = asyncio.Event()
    drain_completed = asyncio.Event()

    async def _dispatch(user_input: str) -> bool:
        executed.append(user_input)
        if user_input == "first":
            first_started.set()
            await _wait_until(lambda: state.pending_items == ("second",))
            drained.extend(state.drain_pending())
            drain_completed.set()
        return True

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(state=state),
            hooks=_runtime_hooks(),
        )
    )

    await inputs.put("first")
    await asyncio.wait_for(first_started.wait(), timeout=2.0)
    await inputs.put("second")
    await asyncio.wait_for(drain_completed.wait(), timeout=2.0)
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)

    assert drained == ["second"]
    assert executed == ["first"]
    assert state.pending_items == ()
    assert "queued" not in surface.writes


@pytest.mark.asyncio
async def test_runtime_dispatches_single_input_against_fake_surface() -> None:
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    executed: list[str] = []

    async def _dispatch(user_input: str) -> bool:
        executed.append(user_input)
        return True

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(),
            hooks=_runtime_hooks(),
        )
    )

    await inputs.put("hello")
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)

    assert executed == ["hello"]
    assert surface.writes == ["echo:hello"]
    assert surface.cancel_callbacks[-1] is None
    assert surface.shutdown_callbacks[-1] is None


@pytest.mark.asyncio
async def test_runtime_queues_pending_input_and_promotes_fifo() -> None:
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    executed: list[str] = []
    first_started = asyncio.Event()
    finish_first = asyncio.Event()

    async def _dispatch(user_input: str) -> bool:
        executed.append(user_input)
        if user_input == "first":
            first_started.set()
            await finish_first.wait()
        return True

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(),
            hooks=_runtime_hooks(),
        )
    )

    await inputs.put("first")
    await asyncio.wait_for(first_started.wait(), timeout=2.0)
    await inputs.put("second")
    await inputs.put("third")
    for _ in range(20):
        await asyncio.sleep(0)
    finish_first.set()
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)

    assert executed == ["first", "second", "third"]
    assert surface.writes.count("queued") == 2


@pytest.mark.asyncio
async def test_runtime_can_defer_next_input_until_turn_finishes() -> None:
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    executed: list[str] = []
    first_started = asyncio.Event()
    finish_first = asyncio.Event()

    async def _dispatch(user_input: str) -> bool:
        executed.append(user_input)
        first_started.set()
        await finish_first.wait()
        return True

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(concurrent_input_during_turn=False),
            hooks=_runtime_hooks(),
        )
    )

    await inputs.put("first")
    await asyncio.wait_for(first_started.wait(), timeout=2.0)
    for _ in range(20):
        await asyncio.sleep(0)

    assert surface.next_line_calls == 1

    finish_first.set()
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)

    assert executed == ["first"]
    assert surface.next_line_calls == 2


@pytest.mark.asyncio
async def test_runtime_can_keep_bottom_input_live_during_turn() -> None:
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    second_input_started = asyncio.Event()
    surface = _FakeSurface(
        inputs,
        on_next_line=lambda: second_input_started.set() if surface.next_line_calls >= 2 else None,
    )
    first_started = asyncio.Event()
    finish_first = asyncio.Event()
    executed: list[str] = []

    async def _dispatch(user_input: str) -> bool:
        executed.append(user_input)
        first_started.set()
        await finish_first.wait()
        return True

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(concurrent_input_during_turn=True),
            hooks=_runtime_hooks(),
        )
    )

    await inputs.put("first")
    await asyncio.wait_for(first_started.wait(), timeout=2.0)
    await asyncio.wait_for(second_input_started.wait(), timeout=2.0)

    assert surface.next_line_calls >= 2

    await inputs.put(None)
    finish_first.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert executed == ["first"]


@pytest.mark.asyncio
async def test_runtime_starts_next_input_before_dispatch_when_concurrent() -> None:
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    events: list[str] = []

    def _record_next_line() -> None:
        events.append(f"input-{surface.next_line_calls}")

    surface = _FakeSurface(inputs, on_next_line=_record_next_line)
    dispatch_started = asyncio.Event()
    finish_dispatch = asyncio.Event()

    async def _dispatch(user_input: str) -> bool:
        events.append(f"dispatch-{user_input}")
        dispatch_started.set()
        await finish_dispatch.wait()
        return True

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(concurrent_input_during_turn=True),
            hooks=_runtime_hooks(),
        )
    )

    await inputs.put("first")
    await asyncio.wait_for(dispatch_started.wait(), timeout=2.0)

    assert events.index("input-2") < events.index("dispatch-first")

    await inputs.put(None)
    finish_dispatch.set()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_runtime_destructive_slash_cancels_active_turn_and_purges_queue() -> None:
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    executed: list[str] = []
    first_started = asyncio.Event()
    first_cancelled = asyncio.Event()
    clear_completed = asyncio.Event()
    cancel_calls: list[str] = []
    clear_cancel_snapshots: list[list[str]] = []

    async def _dispatch(user_input: str) -> bool:
        if user_input == "first":
            executed.append("first-start")
            first_started.set()
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                first_cancelled.set()
                raise
            executed.append("first-end")
            return True
        executed.append(user_input)
        if user_input == "/clear":
            clear_cancel_snapshots.append(list(cancel_calls))
            clear_completed.set()
        return True

    async def _cancel_active_turn() -> None:
        await asyncio.sleep(0)
        cancel_calls.append("cancel")

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(
                classify_input=lambda text: (
                    TuiInputKind.DESTRUCTIVE if text == "/clear" else TuiInputKind.NORMAL
                )
            ),
            hooks=_runtime_hooks(on_cancel_active_turn=_cancel_active_turn),
        )
    )

    await inputs.put("first")
    await asyncio.wait_for(first_started.wait(), timeout=2.0)
    await inputs.put("second")
    await inputs.put("third")
    for _ in range(20):
        await asyncio.sleep(0)
    await inputs.put("/clear")

    await asyncio.wait_for(first_cancelled.wait(), timeout=2.0)
    await asyncio.wait_for(clear_completed.wait(), timeout=2.0)
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)

    assert executed == ["first-start", "/clear"]
    assert cancel_calls == ["cancel"]
    assert clear_cancel_snapshots == [["cancel"]]


@pytest.mark.asyncio
async def test_runtime_exit_drains_pending_work_before_dispatching_exit() -> None:
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    executed: list[str] = []
    first_started = asyncio.Event()
    finish_first = asyncio.Event()

    async def _dispatch(user_input: str) -> bool:
        if user_input == "first":
            executed.append("first-start")
            first_started.set()
            await finish_first.wait()
            executed.append("first-end")
            return True
        executed.append(user_input)
        return user_input != "/exit"

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(
                classify_input=lambda text: (
                    TuiInputKind.EXIT if text == "/exit" else TuiInputKind.NORMAL
                )
            ),
            hooks=_runtime_hooks(),
        )
    )

    await inputs.put("first")
    await asyncio.wait_for(first_started.wait(), timeout=2.0)
    await inputs.put("second")
    await inputs.put("third")
    for _ in range(20):
        await asyncio.sleep(0)
    await inputs.put("/exit")
    for _ in range(20):
        await asyncio.sleep(0)
    finish_first.set()

    await asyncio.wait_for(task, timeout=2.0)

    assert executed == ["first-start", "first-end", "second", "third", "/exit"]


@pytest.mark.asyncio
async def test_runtime_cancel_invokes_adapter_cancel_hook() -> None:
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    dispatch_started = asyncio.Event()
    dispatch_cancelled = asyncio.Event()
    cancel_calls: list[str] = []

    async def _dispatch(user_input: str) -> bool:
        dispatch_started.set()
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            dispatch_cancelled.set()
            raise
        return True

    async def _cancel_active_turn() -> None:
        cancel_calls.append("cancel")

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(),
            hooks=_runtime_hooks(on_cancel_active_turn=_cancel_active_turn),
        )
    )

    await inputs.put("hello")
    await asyncio.wait_for(dispatch_started.wait(), timeout=2.0)
    active_cb = next(cb for cb in reversed(surface.cancel_callbacks) if cb is not None)
    active_cb()
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)

    assert cancel_calls == ["cancel"]
    assert dispatch_cancelled.is_set() is True


@pytest.mark.asyncio
async def test_runtime_cancel_drops_pending_inputs_from_active_turn() -> None:
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    state = TuiRuntimeState()
    executed: list[str] = []
    first_started = asyncio.Event()

    async def _dispatch(user_input: str) -> bool:
        if user_input == "first":
            executed.append("first-start")
            first_started.set()
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                executed.append("first-cancelled")
                raise
        executed.append(user_input)
        return True

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(state=state),
            hooks=_runtime_hooks(),
        )
    )

    await inputs.put("first")
    await asyncio.wait_for(first_started.wait(), timeout=2.0)
    await inputs.put("second")
    await inputs.put("third")
    await _wait_until(lambda: state.pending_items == ("second", "third"))
    active_cb = next(cb for cb in reversed(surface.cancel_callbacks) if cb is not None)
    active_cb()
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)

    assert executed == ["first-start", "first-cancelled"]
    assert state.pending_items == ()
    assert "queued" not in surface.writes


@pytest.mark.asyncio
async def test_runtime_cancel_notifies_when_dropping_queued_inputs() -> None:
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    state = TuiRuntimeState()
    notices: list[str] = []
    first_started = asyncio.Event()

    async def _dispatch(user_input: str) -> bool:
        if user_input == "first":
            first_started.set()
            await asyncio.sleep(5)
        return True

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(state=state),
            hooks=_runtime_hooks(notice=notices.append),
        )
    )

    await inputs.put("first")
    await asyncio.wait_for(first_started.wait(), timeout=2.0)
    await inputs.put("second")
    await inputs.put("third")
    await _wait_until(lambda: state.pending_items == ("second", "third"))
    active_cb = next(cb for cb in reversed(surface.cancel_callbacks) if cb is not None)
    active_cb()
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)

    # The two typed-ahead messages were dropped — the user is told, not left guessing.
    assert any("Discarded 2 queued messages" in notice for notice in notices)


@pytest.mark.asyncio
async def test_runtime_cancel_without_queue_emits_no_discard_notice() -> None:
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    state = TuiRuntimeState()
    notices: list[str] = []
    first_started = asyncio.Event()

    async def _dispatch(user_input: str) -> bool:
        if user_input == "first":
            first_started.set()
            await asyncio.sleep(5)
        return True

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(state=state),
            hooks=_runtime_hooks(notice=notices.append),
        )
    )

    await inputs.put("first")
    await asyncio.wait_for(first_started.wait(), timeout=2.0)
    active_cb = next(cb for cb in reversed(surface.cancel_callbacks) if cb is not None)
    active_cb()  # cancel with an empty queue
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)

    assert not any("Discarded" in notice for notice in notices)


@pytest.mark.asyncio
async def test_runtime_notifies_when_input_is_queued_mid_turn() -> None:
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    state = TuiRuntimeState()
    notices: list[str] = []
    first_started = asyncio.Event()

    async def _dispatch(user_input: str) -> bool:
        if user_input == "first":
            first_started.set()
            await asyncio.sleep(5)
        return True

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(state=state),
            hooks=_runtime_hooks(notice=notices.append),
        )
    )

    await inputs.put("first")
    await asyncio.wait_for(first_started.wait(), timeout=2.0)
    await inputs.put("second")
    await _wait_until(lambda: state.pending_items == ("second",))
    active_cb = next(cb for cb in reversed(surface.cancel_callbacks) if cb is not None)
    active_cb()
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)

    # Typing mid-turn must surface a clear "queued" signal — the message was not
    # silently dropped, nor did it start a turn.
    assert any("Queued (#1) behind the running turn" in notice for notice in notices)


@pytest.mark.asyncio
async def test_runtime_steers_enter_without_adding_fifo_item() -> None:
    from opensquilla.cli.tui.backend.input_identity import current_tui_client_message_id

    inputs: asyncio.Queue[Any] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    state = TuiRuntimeState()
    first_started = asyncio.Event()
    steered: list[str] = []
    steer_identities: list[str | None] = []
    notices: list[str] = []

    async def _dispatch(user_input: str) -> bool:
        if user_input == "first":
            first_started.set()
            await asyncio.sleep(5)
        return True

    async def _steer(text: str) -> bool:
        steered.append(text)
        steer_identities.append(current_tui_client_message_id())
        return True

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(state=state),
            hooks=_runtime_hooks(
                notice=notices.append,
                on_steer_active_turn=_steer,
            ),
        )
    )
    await inputs.put("first")
    await first_started.wait()
    await inputs.put(
        TuiSubmittedInput(
            "change direction",
            intent="steer",
            client_message_id="client-steer-1",
        )
    )
    await _wait_until(lambda: steered == ["change direction"])

    assert state.pending_items == ()
    assert steer_identities == ["client-steer-1"]
    assert "echo:change direction" in surface.writes
    assert any("Steering the running turn" in notice for notice in notices)

    active_cb = next(cb for cb in reversed(surface.cancel_callbacks) if cb is not None)
    active_cb()
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_runtime_late_steer_falls_back_to_visible_queue() -> None:
    inputs: asyncio.Queue[Any] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    state = TuiRuntimeState()
    first_started = asyncio.Event()
    notices: list[str] = []

    async def _dispatch(user_input: str) -> bool:
        if user_input == "first":
            first_started.set()
            await asyncio.sleep(5)
        return True

    async def _unavailable(_text: str) -> bool:
        return False

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(state=state),
            hooks=_runtime_hooks(
                notice=notices.append,
                on_steer_active_turn=_unavailable,
            ),
        )
    )
    await inputs.put("first")
    await first_started.wait()
    await inputs.put(TuiSubmittedInput("run next", intent="steer"))
    await _wait_until(lambda: state.pending_items == ("run next",))

    assert any("Steer unavailable; queued (#1)" in notice for notice in notices)

    active_cb = next(cb for cb in reversed(surface.cancel_callbacks) if cb is not None)
    active_cb()
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_runtime_dirty_steer_failure_does_not_duplicate_into_local_queue() -> None:
    inputs: asyncio.Queue[Any] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    state = TuiRuntimeState()
    first_started = asyncio.Event()
    notices: list[str] = []

    class DirtySteerError(RuntimeError):
        data = {"fallback_safe": False, "orphan_message_id": "message-orphan"}

    async def _dispatch(user_input: str) -> bool:
        if user_input == "first":
            first_started.set()
            await asyncio.sleep(5)
        return True

    async def _dirty(_text: str) -> bool:
        raise DirtySteerError("steer rollback left a durable orphan")

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(state=state),
            hooks=_runtime_hooks(
                notice=notices.append,
                on_steer_active_turn=_dirty,
            ),
        )
    )
    await inputs.put("first")
    await first_started.wait()
    await inputs.put(
        TuiSubmittedInput(
            "must not duplicate",
            intent="steer",
            client_message_id="client-dirty",
        )
    )
    await _wait_until(lambda: any("Steer failed" in notice for notice in notices))

    assert state.pending_items == ()
    assert "echo:must not duplicate" not in surface.writes
    assert not any("queued" in notice.lower() for notice in notices)

    active_cb = next(cb for cb in reversed(surface.cancel_callbacks) if cb is not None)
    active_cb()
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_runtime_ambiguous_steer_transport_failure_fails_closed() -> None:
    inputs: asyncio.Queue[Any] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    state = TuiRuntimeState()
    first_started = asyncio.Event()
    notices: list[str] = []

    async def _dispatch(user_input: str) -> bool:
        if user_input == "first":
            first_started.set()
            await asyncio.sleep(5)
        return True

    async def _ambiguous(_text: str) -> bool:
        raise ConnectionError("reply lost after request write")

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(state=state),
            hooks=_runtime_hooks(
                notice=notices.append,
                on_steer_active_turn=_ambiguous,
            ),
        )
    )
    await inputs.put("first")
    await first_started.wait()
    await inputs.put(
        TuiSubmittedInput(
            "maybe already accepted",
            intent="steer",
            client_message_id="client-ambiguous",
        )
    )
    await _wait_until(lambda: any("Steer failed" in notice for notice in notices))

    assert state.pending_items == ()
    assert "echo:maybe already accepted" not in surface.writes
    assert not any("queued" in notice.lower() for notice in notices)

    active_cb = next(cb for cb in reversed(surface.cancel_callbacks) if cb is not None)
    active_cb()
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_runtime_legacy_method_missing_steer_uses_visible_queue() -> None:
    inputs: asyncio.Queue[Any] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    state = TuiRuntimeState()
    first_started = asyncio.Event()
    notices: list[str] = []

    class MethodMissingError(RuntimeError):
        code = "METHOD_NOT_FOUND"

    async def _dispatch(user_input: str) -> bool:
        if user_input == "first":
            first_started.set()
            await asyncio.sleep(5)
        return True

    async def _missing(_text: str) -> bool:
        raise MethodMissingError("sessions.steer is unavailable")

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(state=state),
            hooks=_runtime_hooks(
                notice=notices.append,
                on_steer_active_turn=_missing,
            ),
        )
    )
    await inputs.put("first")
    await first_started.wait()
    await inputs.put(
        TuiSubmittedInput(
            "queue on old gateway",
            intent="steer",
            client_message_id="client-old-gateway",
        )
    )
    await _wait_until(lambda: state.pending_items == ("queue on old gateway",))

    assert "echo:queue on old gateway" in surface.writes
    assert any("Steer unavailable; queued (#1)" in notice for notice in notices)

    active_cb = next(cb for cb in reversed(surface.cancel_callbacks) if cb is not None)
    active_cb()
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_runtime_full_queue_rejects_message_without_echoing_it() -> None:
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    state = TuiRuntimeState()
    echoed: list[str] = []
    notices: list[str] = []
    first_started = asyncio.Event()

    async def _record_echo(_surface: Any, text: str) -> None:
        echoed.append(text)

    async def _dispatch(user_input: str) -> bool:
        if user_input == "first":
            first_started.set()
            await asyncio.sleep(5)
        return True

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(state=state, queue_max_size=1),
            hooks=TuiRuntimeHooks(
                on_user_input_echo=_record_echo,
                on_queued_turn_start=_queued_echo,
                notice=notices.append,
            ),
        )
    )

    await inputs.put("first")
    await asyncio.wait_for(first_started.wait(), timeout=2.0)
    await inputs.put("second")  # fills the single queue slot
    await _wait_until(lambda: state.pending_items == ("second",))
    await inputs.put("third")  # queue is full -> must be rejected
    await _wait_until(lambda: any("Queue full" in notice for notice in notices))
    active_cb = next(cb for cb in reversed(surface.cancel_callbacks) if cb is not None)
    active_cb()
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)

    # The rejected message must NOT appear accepted in the transcript; the one that
    # fit was echoed normally.
    assert "second" in echoed
    assert "third" not in echoed
    assert state.pending_items == ()


@pytest.mark.asyncio
async def test_runtime_cancel_suppresses_adapter_cancel_hook_errors() -> None:
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    dispatch_started = asyncio.Event()
    dispatch_cancelled = asyncio.Event()

    async def _dispatch(user_input: str) -> bool:
        dispatch_started.set()
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            dispatch_cancelled.set()
            raise
        return True

    def _cancel_active_turn() -> Awaitable[None]:
        raise RuntimeError("abort hook failed")

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(),
            hooks=_runtime_hooks(on_cancel_active_turn=_cancel_active_turn),
        )
    )

    await inputs.put("hello")
    await asyncio.wait_for(dispatch_started.wait(), timeout=2.0)
    active_cb = next(cb for cb in reversed(surface.cancel_callbacks) if cb is not None)
    active_cb()
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)

    assert dispatch_cancelled.is_set() is True


@pytest.mark.asyncio
async def test_runtime_installs_surface_redraw_callback_for_resize() -> None:
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    installed = asyncio.Event()
    captured_resize_callbacks: list[Callable[[], None]] = []

    def _install_signal_handlers(
        *,
        loop: asyncio.AbstractEventLoop,
        on_resize: Callable[[], None],
        is_turn_in_flight: Callable[[], bool],
    ) -> Callable[[], None]:
        del loop, is_turn_in_flight
        captured_resize_callbacks.append(on_resize)
        installed.set()
        return lambda: None

    async def _dispatch(user_input: str) -> bool:
        raise AssertionError(f"dispatch should not run: {user_input}")

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(install_signal_handlers=_install_signal_handlers),
        )
    )

    await asyncio.wait_for(installed.wait(), timeout=2.0)
    captured_resize_callbacks[0]()
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)

    assert surface.redraw_count == 1


class _RaisingSurface:
    """Surface whose ``next_line`` fails, modelling an OpenTUI host read error."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.cancel_callbacks: list[Any] = []
        self.shutdown_callbacks: list[Any] = []

    async def next_line(self) -> str | None:
        raise self._error

    def set_cancel_callback(self, cb) -> None:  # noqa: ANN001
        self.cancel_callbacks.append(cb)

    def set_shutdown_callback(self, cb) -> None:  # noqa: ANN001
        self.shutdown_callbacks.append(cb)

    def emit_eof(self) -> None:
        return None

    async def write_through(self, payload: str) -> None:
        return None

    @property
    def redraw_callback(self) -> Callable[[], None]:
        return lambda: None


@pytest.mark.asyncio
async def test_runtime_degrades_gracefully_on_surface_read_error() -> None:
    surface = _RaisingSurface(RuntimeError("OpenTUI host error: [boom]"))
    notices: list[str] = []

    async def _dispatch(_user_input: str) -> bool:
        raise AssertionError("dispatch should not run when input never arrives")

    result = await run_tui_runtime(
        dispatch=_dispatch,
        surface_factory=_surface_factory(surface),
        config=_runtime_config(),
        hooks=_runtime_hooks(notice=notices.append),
    )

    assert isinstance(result, TuiRuntimeState)
    assert any("Input surface error" in notice for notice in notices)
    # the dynamic error text is markup-escaped so the notice itself cannot crash.
    assert any("\\[boom]" in notice for notice in notices)


@pytest.mark.asyncio
async def test_runtime_runs_local_command_inline_without_echo_or_queue() -> None:
    # A LOCAL command (e.g. /theme) typed while a turn is streaming must run
    # immediately, never echoed as a prompt and never queued behind the turn.
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    dispatched: list[str] = []
    echoed: list[str] = []
    queued_starts = 0
    turn_running = asyncio.Event()
    release_turn = asyncio.Event()

    async def _dispatch(user_input: str) -> bool:
        dispatched.append(user_input)
        if user_input == "hi":
            turn_running.set()
            await release_turn.wait()  # hold the turn in flight
        return True

    async def _echo(_surface: Any, text: str) -> None:
        echoed.append(text)

    async def _queued_start(_surface: Any) -> None:
        nonlocal queued_starts
        queued_starts += 1

    def _classify(text: str) -> TuiInputKind:
        return TuiInputKind.LOCAL if text.startswith("/theme") else TuiInputKind.NORMAL

    task = asyncio.ensure_future(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(concurrent_input_during_turn=True, classify_input=_classify),
            hooks=TuiRuntimeHooks(on_user_input_echo=_echo, on_queued_turn_start=_queued_start),
        )
    )

    await inputs.put("hi")
    await turn_running.wait()
    await inputs.put("/theme")  # LOCAL command while "hi" streams
    await _wait_until(lambda: "/theme" in dispatched)

    # dispatched immediately, NOT echoed as a prompt, NOT queued, no queued marker
    assert echoed == ["hi"]  # only the chat message was echoed
    assert queued_starts == 0
    assert surface.next_line_calls  # input was being read concurrently

    release_turn.set()
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)
    assert dispatched == ["hi", "/theme"]


@pytest.mark.asyncio
async def test_runtime_runs_control_command_inline_without_echo_steer_or_queue() -> None:
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    dispatched: list[str] = []
    echoed: list[str] = []
    steered: list[str] = []
    turn_running = asyncio.Event()
    release_turn = asyncio.Event()

    async def _dispatch(user_input: str) -> bool:
        dispatched.append(user_input)
        if user_input == "hi":
            turn_running.set()
            await release_turn.wait()
        return True

    async def _echo(_surface: Any, text: str) -> None:
        echoed.append(text)

    async def _steer(text: str) -> bool:
        steered.append(text)
        return True

    def _classify(text: str) -> TuiInputKind:
        if text.startswith(("/router", "/ensemble")):
            return TuiInputKind.CONTROL
        return TuiInputKind.NORMAL

    task = asyncio.ensure_future(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(concurrent_input_during_turn=True, classify_input=_classify),
            hooks=TuiRuntimeHooks(on_user_input_echo=_echo, on_steer_active_turn=_steer),
        )
    )
    await inputs.put("hi")
    await turn_running.wait()
    await inputs.put(TuiSubmittedInput("/ensemble on", intent="control"))
    await _wait_until(lambda: "/ensemble on" in dispatched)

    assert echoed == ["hi"]
    assert steered == []
    assert task.done() is False

    release_turn.set()
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)
    assert dispatched == ["hi", "/ensemble on"]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/model gpt-5", "/help", "/does-not-exist"])
async def test_runtime_runs_command_inline_without_echo_or_queue(command: str) -> None:
    """Known query commands and unknown slash input share one command boundary."""
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    state = TuiRuntimeState()
    dispatched: list[str] = []
    echoed: list[str] = []
    turn_running = asyncio.Event()
    release_turn = asyncio.Event()

    async def _dispatch(user_input: str) -> bool:
        dispatched.append(user_input)
        if user_input == "hi":
            turn_running.set()
            await release_turn.wait()
        return True

    async def _echo(_surface: Any, text: str) -> None:
        echoed.append(text)

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(
                concurrent_input_during_turn=True,
                classify_input=classify_chat_input,
                state=state,
            ),
            hooks=TuiRuntimeHooks(on_user_input_echo=_echo),
        )
    )

    await inputs.put("hi")
    await turn_running.wait()
    await inputs.put(command)
    await _wait_until(lambda: command in dispatched)

    assert echoed == ["hi"]
    assert state.pending_size == 0

    release_turn.set()
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)
    assert dispatched == ["hi", command]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/strategy", "/router on", "/ensemble", "/meta foo"])
async def test_standalone_gateway_only_commands_never_become_turns(command: str) -> None:
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    state = TuiRuntimeState()
    dispatched: list[str] = []
    echoed: list[str] = []
    turn_running = asyncio.Event()
    release_turn = asyncio.Event()

    async def _dispatch(user_input: str) -> bool:
        dispatched.append(user_input)
        if user_input == "hi":
            turn_running.set()
            await release_turn.wait()
        return True

    async def _echo(_surface: Any, text: str) -> None:
        echoed.append(text)

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(
                concurrent_input_during_turn=True,
                classify_input=lambda text: classify_chat_input(
                    text,
                    surface=Surface.CLI_STANDALONE,
                ),
                state=state,
            ),
            hooks=TuiRuntimeHooks(on_user_input_echo=_echo),
        )
    )

    await inputs.put("hi")
    await turn_running.wait()
    await inputs.put(command)
    await _wait_until(lambda: command in dispatched)

    assert echoed == ["hi"]
    assert state.pending_size == 0

    release_turn.set()
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)
    assert dispatched == ["hi", command]


@pytest.mark.asyncio
async def test_runtime_rejects_require_idle_command_while_turn_is_running() -> None:
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    state = TuiRuntimeState()
    dispatched: list[str] = []
    echoed: list[str] = []
    notices: list[str] = []
    turn_running = asyncio.Event()
    release_turn = asyncio.Event()

    async def _dispatch(user_input: str) -> bool:
        dispatched.append(user_input)
        if user_input == "hi":
            turn_running.set()
            await release_turn.wait()
        return True

    async def _echo(_surface: Any, text: str) -> None:
        echoed.append(text)

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(
                concurrent_input_during_turn=True,
                classify_input=classify_chat_input,
                state=state,
            ),
            hooks=TuiRuntimeHooks(on_user_input_echo=_echo, notice=notices.append),
        )
    )

    await inputs.put("hi")
    await turn_running.wait()
    await inputs.put("/new")
    await _wait_until(lambda: bool(notices))

    assert dispatched == ["hi"]
    assert echoed == ["hi"]
    assert state.pending_size == 0
    assert "requires an idle session" in notices[-1]

    release_turn.set()
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_runtime_dispatches_require_idle_command_without_echo_when_idle() -> None:
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    dispatched: list[str] = []
    echoed: list[str] = []

    async def _dispatch(user_input: str) -> bool:
        dispatched.append(user_input)
        return True

    async def _echo(_surface: Any, text: str) -> None:
        echoed.append(text)

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(classify_input=classify_chat_input),
            hooks=TuiRuntimeHooks(on_user_input_echo=_echo),
        )
    )
    await inputs.put("/new")
    await _wait_until(lambda: dispatched == ["/new"])
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)

    assert echoed == []


@pytest.mark.asyncio
async def test_runtime_keeps_turn_presentations_in_normal_queue_contract() -> None:
    inputs: asyncio.Queue[str | None] = asyncio.Queue()
    surface = _FakeSurface(inputs)
    state = TuiRuntimeState()
    dispatched: list[str] = []
    echoed: list[str] = []
    queued_starts = 0
    turn_running = asyncio.Event()
    release_turn = asyncio.Event()

    async def _dispatch(user_input: str) -> bool:
        dispatched.append(user_input)
        if user_input == "hi":
            turn_running.set()
            await release_turn.wait()
        return True

    async def _echo(_surface: Any, text: str) -> None:
        echoed.append(text)

    async def _queued_start(_surface: Any) -> None:
        nonlocal queued_starts
        queued_starts += 1

    task = asyncio.create_task(
        run_tui_runtime(
            dispatch=_dispatch,
            surface_factory=_surface_factory(surface),
            config=_runtime_config(
                concurrent_input_during_turn=True,
                classify_input=classify_chat_input,
                state=state,
            ),
            hooks=TuiRuntimeHooks(
                on_user_input_echo=_echo,
                on_queued_turn_start=_queued_start,
            ),
        )
    )

    await inputs.put("hi")
    await turn_running.wait()
    await inputs.put("/file report.md summarize")
    await _wait_until(lambda: state.pending_size == 1)

    assert dispatched == ["hi"]
    assert echoed == ["hi", "/file report.md summarize"]

    release_turn.set()
    await _wait_until(lambda: "/file report.md summarize" in dispatched)
    await inputs.put(None)
    await asyncio.wait_for(task, timeout=2.0)

    assert dispatched == ["hi", "/file report.md summarize"]
    assert queued_starts == 1
