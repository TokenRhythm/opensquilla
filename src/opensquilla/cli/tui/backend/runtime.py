"""Backend runtime for OpenSquilla interactive TUI surfaces."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from typing import Any

from rich.markup import escape as _escape

from opensquilla.cli.tui.backend.contracts import (
    TuiDispatch,
    TuiInputKind,
    TuiRuntimeConfig,
    TuiRuntimeHooks,
    TuiSubmittedInput,
    TuiSurfaceFactory,
)
from opensquilla.cli.tui.backend.events import TuiEvent, TuiEventKind, TuiEventSink
from opensquilla.cli.tui.backend.input_identity import tui_input_identity_scope
from opensquilla.cli.tui.backend.state import TuiRuntimeState

# Ceiling on the shutdown drain of in-flight abort RPCs: a wedged gateway
# that never answers the abort must not hang TUI exit indefinitely.
_ABORT_DRAIN_TIMEOUT_S = 5.0


def _emit(event_sink: TuiEventSink | None, event: TuiEvent) -> None:
    if event_sink is not None:
        event_sink(event)


async def run_tui_runtime(
    *,
    dispatch: TuiDispatch,
    surface_factory: TuiSurfaceFactory,
    config: TuiRuntimeConfig,
    hooks: TuiRuntimeHooks = TuiRuntimeHooks(),
) -> TuiRuntimeState:
    """Run the concurrent submitted-line/turn loop for one TUI surface."""
    runtime_state = config.state or TuiRuntimeState()

    async with surface_factory() as tui_surface:
        if hooks.expose_surface is not None:
            hooks.expose_surface(tui_surface)
        turn_task: asyncio.Task[bool] | None = None
        # Abort tasks are held (and drained on shutdown) so a fire-and-forget
        # cancel RPC is never garbage-collected mid-flight or abandoned while
        # still pending when the runtime returns.
        abort_tasks: set[asyncio.Task[None]] = set()

        async def _schedule_abort(abort_turn: Awaitable[None]) -> None:
            with contextlib.suppress(Exception):
                await abort_turn

        def _notice_queue_discarded(dropped: tuple[str, ...]) -> None:
            """Tell the user when queued messages are dropped, so a cancel or a
            destructive command never silently swallows their typed-ahead input."""
            count = len(dropped)
            if count and hooks.notice is not None:
                suffix = "" if count == 1 else "s"
                hooks.notice(f"[yellow]Discarded {count} queued message{suffix}.[/yellow]")

        def _notice_turn_failed(exc: Exception) -> None:
            if hooks.notice is not None:
                hooks.notice(f"[red]Turn failed: {_escape(str(exc))}[/red]")

        def _notice_surface_error(exc: Exception) -> None:
            if hooks.notice is not None:
                hooks.notice(f"[red]Input surface error: {_escape(str(exc))}[/red]")

        def _cancel_inflight_turn() -> asyncio.Task[None] | None:
            task = turn_task
            if task is not None and not task.done():
                abort_task: asyncio.Task[None] | None = None
                _notice_queue_discarded(runtime_state.clear_pending())
                with contextlib.suppress(Exception):
                    abort_turn = hooks.on_cancel_active_turn()
                    abort_task = asyncio.create_task(_schedule_abort(abort_turn))
                    abort_tasks.add(abort_task)
                    abort_task.add_done_callback(abort_tasks.discard)
                task.cancel()
                return abort_task
            return None

        def _cancel_callback() -> None:
            _cancel_inflight_turn()

        tui_surface.set_cancel_callback(_cancel_callback)

        def _shutdown_drain_then_exit() -> None:
            tui_surface.emit_eof()

        tui_surface.set_shutdown_callback(_shutdown_drain_then_exit)

        def _is_turn_in_flight() -> bool:
            return turn_task is not None and not turn_task.done()

        uninstall_signals = config.install_signal_handlers(
            loop=asyncio.get_running_loop(),
            on_resize=tui_surface.redraw_callback,
            is_turn_in_flight=_is_turn_in_flight,
        )

        task_name = config.task_name

        async def _run_dispatch(
            user_input: str,
            client_message_id: str | None = None,
        ) -> bool:
            runtime_state.mark_turn_started(user_input)
            _emit(config.event_sink, TuiEvent(TuiEventKind.TURN_STARTED, input_text=user_input))
            try:
                with tui_input_identity_scope(client_message_id):
                    return await dispatch(user_input)
            finally:
                runtime_state.mark_turn_finished()
                _emit(
                    config.event_sink,
                    TuiEvent(TuiEventKind.TURN_FINISHED, input_text=user_input),
                )

        async def _await_turn_or_cancel() -> bool:
            nonlocal turn_task
            current = turn_task
            if current is None:
                return True
            try:
                keep_going = await current
            except asyncio.CancelledError:
                hooks.clear_current_cancel()
                _emit(config.event_sink, TuiEvent(TuiEventKind.TURN_CANCELLED))
                if hooks.notice is not None:
                    hooks.notice("[yellow]Cancelled.[/yellow]")
                keep_going = True
            except Exception as exc:
                # A dispatch failure (gateway restart, provider error, renderer
                # write) ends the turn, not the session: surface it and keep
                # reading input. Loop teardown is reserved for surface read
                # failures.
                _notice_turn_failed(exc)
                keep_going = True
            finally:
                turn_task = None
            return keep_going

        async def _run_shutdown_drain() -> bool:
            nonlocal turn_task
            while runtime_state.pending_size:
                promoted = runtime_state.promote_next_with_identity()
                if promoted is None:
                    break
                queued, queued_client_message_id = promoted
                try:
                    await hooks.on_queued_turn_start(tui_surface)
                except Exception as exc:
                    _notice_surface_error(exc)
                    return False
                _emit(
                    config.event_sink,
                    TuiEvent(TuiEventKind.QUEUED_INPUT_PROMOTED, input_text=queued),
                )
                turn_task = asyncio.create_task(
                    _run_dispatch(queued, queued_client_message_id),
                    name=task_name,
                )
                keep_going = await _await_turn_or_cancel()
                if not keep_going:
                    return False
            return True

        next_line_task: asyncio.Task[TuiSubmittedInput | str | None] | None = None

        async def _ensure_next_line_task() -> None:
            nonlocal next_line_task
            if next_line_task is not None:
                return
            next_line_task = asyncio.create_task(
                tui_surface.next_line(),
                name=f"chat-input-{task_name}",
            )
            await asyncio.sleep(0)

        async def _drop_next_line() -> None:
            nonlocal next_line_task
            if next_line_task is None:
                return
            if not next_line_task.done():
                next_line_task.cancel()
                try:
                    await next_line_task
                except BaseException:  # noqa: BLE001 - shutdown path
                    pass
            next_line_task = None

        try:
            while True:
                can_read_input = (
                    config.concurrent_input_during_turn or turn_task is None or turn_task.done()
                )
                if next_line_task is None and can_read_input:
                    await _ensure_next_line_task()

                waitables: set[asyncio.Task[Any]] = set()
                if next_line_task is not None:
                    waitables.add(next_line_task)
                if turn_task is not None and not turn_task.done():
                    waitables.add(turn_task)
                if not waitables:
                    continue
                await asyncio.wait(waitables, return_when=asyncio.FIRST_COMPLETED)

                if turn_task is not None and turn_task.done():
                    keep_going = await _await_turn_or_cancel()
                    if not keep_going:
                        await _drop_next_line()
                        return runtime_state
                    promoted = runtime_state.promote_next_with_identity()
                    if promoted is not None:
                        queued, queued_client_message_id = promoted
                        try:
                            await hooks.on_queued_turn_start(tui_surface)
                        except Exception as exc:
                            _notice_surface_error(exc)
                            return runtime_state
                        _emit(
                            config.event_sink,
                            TuiEvent(TuiEventKind.QUEUED_INPUT_PROMOTED, input_text=queued),
                        )
                        turn_task = asyncio.create_task(
                            _run_dispatch(queued, queued_client_message_id),
                            name=task_name,
                        )
                        continue
                    if next_line_task is None or not next_line_task.done():
                        continue

                if next_line_task is None or not next_line_task.done():
                    continue
                try:
                    submitted = next_line_task.result()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # A surface/host read failure (e.g. the OpenTUI sidecar
                    # crashed or sent an error frame) must degrade to a clean
                    # shutdown with a notice rather than tearing the chat
                    # process down with an unhandled traceback.
                    next_line_task = None
                    if turn_task is not None and not turn_task.done():
                        turn_task.cancel()
                        with contextlib.suppress(Exception, asyncio.CancelledError):
                            await turn_task
                        turn_task = None
                    _notice_surface_error(exc)
                    return runtime_state
                next_line_task = None

                if submitted is None:
                    if turn_task is not None and not turn_task.done():
                        try:
                            await turn_task
                        except asyncio.CancelledError:
                            _emit(config.event_sink, TuiEvent(TuiEventKind.TURN_CANCELLED))
                        except Exception as exc:
                            _notice_turn_failed(exc)
                        turn_task = None
                    if not await _run_shutdown_drain():
                        return runtime_state
                    if hooks.notice is not None:
                        hooks.notice("[yellow]Goodbye.[/yellow]")
                    return runtime_state

                if isinstance(submitted, TuiSubmittedInput):
                    user_input = submitted.text
                    submit_intent = submitted.intent
                    client_message_id = submitted.client_message_id
                else:
                    # Compatibility for native/test surfaces that predate the
                    # additive intent field.
                    user_input = submitted
                    submit_intent = "auto"
                    client_message_id = None

                # A blank line is never a message: dispatching it would echo an
                # empty prompt card and queue a phantom entry behind a running
                # turn. Surfaces guard this too; this is the backend's defense
                # for every frontend.
                if not user_input.strip():
                    continue

                category = config.classify_input(user_input)

                if (
                    category is TuiInputKind.COMMAND_REQUIRES_IDLE
                    and turn_task is not None
                    and not turn_task.done()
                ):
                    # Session navigation/mutation that cannot safely race a
                    # running turn is rejected at the command boundary. It must
                    # never masquerade as a queued user message.
                    if hooks.notice is not None:
                        hooks.notice(
                            "[yellow]Command requires an idle session. "
                            "Wait for the current turn to finish.[/yellow]"
                        )
                    continue

                if category in (
                    TuiInputKind.LOCAL,
                    TuiInputKind.CONTROL,
                    TuiInputKind.COMMAND,
                    TuiInputKind.COMMAND_REQUIRES_IDLE,
                ):
                    # Host UI, Gateway control, and deterministic slash
                    # commands act now, inline on the loop, with no prompt echo
                    # and no queue. The in-flight turn keeps its captured
                    # runtime strategy; a control write applies only to the
                    # next accepted turn.
                    try:
                        keep_going = await dispatch(user_input)
                    except Exception as exc:
                        _notice_turn_failed(exc)
                        continue
                    if not keep_going:
                        return runtime_state
                    continue

                steer_fell_back = False
                if (
                    submit_intent == "steer"
                    and category is TuiInputKind.NORMAL
                    and turn_task is not None
                    and not turn_task.done()
                    # Slash/shell commands are control-plane input, never text
                    # to inject into the model's current reasoning loop.
                    and not user_input.lstrip().startswith(("/", "!"))
                ):
                    try:
                        # The optimistic prompt, sessions.steer, a safe queue
                        # fallback, and any later sessions.send promotion must
                        # all retain the composer-allocated identity.
                        with tui_input_identity_scope(client_message_id):
                            steered = await hooks.on_steer_active_turn(user_input)
                    except Exception as exc:
                        if hooks.notice is not None:
                            hooks.notice(
                                f"[red]Steer failed: {_escape(str(exc))}[/red]"
                            )
                        # Once the request crossed the transport boundary, a
                        # missing response is ambiguous: the Gateway may have
                        # durably accepted the steer already.  Re-sending it as
                        # a queued message could repeat a tool effect, so fail
                        # closed unless the server explicitly declares the
                        # failure safe.  METHOD_NOT_FOUND is the one legacy
                        # exception because an older Gateway cannot have run a
                        # handler that it does not expose.
                        error_data = getattr(exc, "data", None)
                        fallback_safe = bool(
                            isinstance(error_data, dict)
                            and error_data.get("fallback_safe") is True
                        )
                        error_code = str(getattr(exc, "code", "") or "").upper()
                        if error_code == "METHOD_NOT_FOUND":
                            fallback_safe = True
                        if not fallback_safe:
                            continue
                        steered = False
                    if steered:
                        try:
                            with tui_input_identity_scope(client_message_id):
                                await hooks.on_user_input_echo(tui_surface, user_input)
                        except Exception as exc:
                            _notice_surface_error(exc)
                            return runtime_state
                        _emit(
                            config.event_sink,
                            TuiEvent(TuiEventKind.USER_INPUT_ACCEPTED, input_text=user_input),
                        )
                        if hooks.notice is not None:
                            hooks.notice(
                                "[bold]Steering the running turn at its next safe boundary.[/bold]"
                            )
                        continue
                    steer_fell_back = True

                # A full queue rejects new typed-ahead BEFORE it is echoed —
                # otherwise the message appears accepted in the transcript yet
                # never runs. Destructive/exit commands are exempt: they purge or
                # drain the queue rather than enqueue, so fullness must not block
                # them.
                if (
                    category not in (TuiInputKind.DESTRUCTIVE, TuiInputKind.EXIT)
                    and turn_task is not None
                    and not turn_task.done()
                    and runtime_state.pending_size >= config.queue_max_size
                ):
                    if hooks.notice is not None:
                        hooks.notice(
                            f"[yellow]Queue full ({config.queue_max_size} items)."
                            " Wait for the current turn to complete.[/yellow]"
                        )
                    continue

                try:
                    with tui_input_identity_scope(client_message_id):
                        await hooks.on_user_input_echo(tui_surface, user_input)
                except Exception as exc:
                    # An echo failure means the surface itself is broken (the
                    # write goes through host IPC), so degrade like a read
                    # failure: cancel the in-flight turn and shut down cleanly.
                    if turn_task is not None and not turn_task.done():
                        turn_task.cancel()
                        with contextlib.suppress(Exception, asyncio.CancelledError):
                            await turn_task
                        turn_task = None
                    _notice_surface_error(exc)
                    return runtime_state
                _emit(
                    config.event_sink,
                    TuiEvent(TuiEventKind.USER_INPUT_ACCEPTED, input_text=user_input),
                )

                if category is TuiInputKind.DESTRUCTIVE:
                    _notice_queue_discarded(runtime_state.clear_pending())
                    if turn_task is not None and not turn_task.done():
                        abort_task = _cancel_inflight_turn()
                        try:
                            await turn_task
                        except asyncio.CancelledError:
                            hooks.clear_current_cancel()
                            _emit(config.event_sink, TuiEvent(TuiEventKind.TURN_CANCELLED))
                        except Exception as exc:
                            _notice_turn_failed(exc)
                        if abort_task is not None:
                            await abort_task
                        turn_task = None
                    try:
                        keep_going = await _run_dispatch(user_input, client_message_id)
                    except Exception as exc:
                        _notice_turn_failed(exc)
                        keep_going = True
                    if not keep_going:
                        return runtime_state
                    continue

                if category is TuiInputKind.EXIT:
                    if turn_task is not None and not turn_task.done():
                        try:
                            await turn_task
                        except asyncio.CancelledError:
                            hooks.clear_current_cancel()
                            _emit(config.event_sink, TuiEvent(TuiEventKind.TURN_CANCELLED))
                        except Exception as exc:
                            _notice_turn_failed(exc)
                        turn_task = None
                    if not await _run_shutdown_drain():
                        return runtime_state
                    try:
                        keep_going = await _run_dispatch(user_input, client_message_id)
                    except Exception as exc:
                        # The user asked to leave: a failing exit dispatch must
                        # not trap them in the loop.
                        _notice_turn_failed(exc)
                        keep_going = False
                    if not keep_going:
                        return runtime_state
                    continue

                if turn_task is not None and not turn_task.done():
                    # Fullness was already rejected before the echo above, so the
                    # queue has room here.
                    runtime_state.enqueue(
                        user_input,
                        client_message_id=client_message_id,
                    )
                    # The message was echoed like a normal submission, but it did
                    # NOT start a turn — tell the user it is queued behind the
                    # running one (it will run next, or steer the turn if it makes
                    # a tool call) so "did my message send?" is never ambiguous.
                    if hooks.notice is not None:
                        position = runtime_state.pending_size
                        prefix = "Steer unavailable; queued" if steer_fell_back else "Queued"
                        hooks.notice(
                            f"[dim]{prefix} (#{position}) behind the running turn.[/dim]"
                        )
                    continue

                if config.concurrent_input_during_turn:
                    await _ensure_next_line_task()
                turn_task = asyncio.create_task(
                    _run_dispatch(user_input, client_message_id),
                    name=task_name,
                )
        finally:
            if hooks.clear_exposed_surface is not None:
                hooks.clear_exposed_surface()
            tui_surface.set_cancel_callback(None)
            tui_surface.set_shutdown_callback(None)
            await _drop_next_line()
            if abort_tasks:
                # Drain in-flight abort RPCs so a cancel-then-exit still
                # reaches the backend before the client connection closes.
                # The drain is bounded: a gateway that never answers the
                # abort must not hang exit, so stragglers are cancelled.
                _, stragglers = await asyncio.wait(
                    set(abort_tasks), timeout=_ABORT_DRAIN_TIMEOUT_S
                )
                for straggler in stragglers:
                    straggler.cancel()
            with contextlib.suppress(Exception):
                uninstall_signals()
