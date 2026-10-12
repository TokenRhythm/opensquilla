"""Terminal dependency composition for TUI turn streaming."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import opensquilla.cli.tui.adapters.input_bridge as _input_bridge
from opensquilla.cli.chat import turn_stream as _turn_stream
from opensquilla.cli.tui.adapters import runtime_helpers as _runtime_helpers
from opensquilla.cli.tui.adapters.approvals import tui_approval_handler
from opensquilla.cli.tui.backend.contracts import TuiOutputHandle
from opensquilla.cli.tui.backend.domain_events import KIND_ROUTER_DECISION, TuiDomainEvent
from opensquilla.cli.tui.backend.plugins import TuiPluginManager
from opensquilla.cli.tui.opentui.renderer import OpenTuiStreamRenderer
from opensquilla.cli.tui.plugins.router_hud import (
    ROUTER_HUD_SLOT,
    RouterHudPlugin,
    RouterHudSnapshot,
)
from opensquilla.cli.ui import console, error_panel
from opensquilla.engine.commands import Surface

TurnStreamDependencies = _turn_stream.TurnStreamDependencies


def default_tui_plugin_manager() -> TuiPluginManager:
    return TuiPluginManager([RouterHudPlugin()])


def approval_surface_for_tui_output(
    tui_output: TuiOutputHandle | None,
    default: Surface,
) -> Surface:
    resolved = _turn_stream.approval_surface_for_tui_output(tui_output, default)
    if isinstance(resolved, Surface):
        return resolved
    return default


def _approval_surface_for_terminal_output(
    tui_output: TuiOutputHandle | None,
    default: object | None,
) -> object | None:
    if not isinstance(default, Surface):
        return default
    return approval_surface_for_tui_output(tui_output, default)


def image_prompt_and_attachments(command: str) -> tuple[str, list[dict[str, str]]]:
    return _input_bridge.image_prompt_and_attachments(command)


def router_hud_event_sink_factory(
    tui_output: TuiOutputHandle | None,
) -> Callable[[TuiDomainEvent], None]:
    manager = _plugin_manager_for_output(tui_output)

    def _sink(event: TuiDomainEvent) -> None:
        if event.kind != KIND_ROUTER_DECISION:
            manager.dispatch(event)
            return
        manager.dispatch(event)
        snapshot = manager.snapshot(ROUTER_HUD_SLOT)
        if not isinstance(snapshot, RouterHudSnapshot):
            return
        _set_toolbar_value(tui_output, "router_hud", snapshot.label)
        _set_toolbar_value(tui_output, "router_hud_style", snapshot.style)
        _set_toolbar_value(tui_output, "router_baseline_model", snapshot.baseline_model)
        _set_toolbar_value(tui_output, "router_source", snapshot.source)
        _set_toolbar_value(tui_output, "router_routing_applied", snapshot.routing_applied)
        _set_toolbar_value(tui_output, "router_rollout_phase", snapshot.rollout_phase)
        _set_toolbar_value(
            tui_output,
            "router_context_window",
            snapshot.context_window,
        )
        _invalidate_output(tui_output)

    return _sink


def _plugin_manager_for_output(tui_output: TuiOutputHandle | None) -> TuiPluginManager:
    manager = getattr(tui_output, "plugin_manager", None)
    if isinstance(manager, TuiPluginManager):
        return manager
    return default_tui_plugin_manager()


def _set_toolbar_value(
    tui_output: TuiOutputHandle | None,
    key: str,
    value: object | None,
) -> None:
    setter = getattr(tui_output, "set_toolbar", None)
    if callable(setter):
        setter(key, value)


async def _noop_approval_handler(*_args: Any, **_kwargs: Any) -> None:
    """Explicit opt-out for callers that must never prompt (replay harnesses)."""
    return None


def _invalidate_output(tui_output: TuiOutputHandle | None) -> None:
    invalidate = getattr(tui_output, "invalidate", None)
    if callable(invalidate):
        invalidate()


def default_turn_stream_dependencies(
    *,
    renderer_factory: Callable[..., Any] | None = None,
    stream_wrapper: Callable[[Any, Any], Any] | None = None,
    approval_handler: Callable[..., Awaitable[None]] | None = None,
    cancel_clearer: Callable[[], None] | None = None,
    image_attachment_builder: Callable[[str], tuple[str, list[dict[str, str]]]]
    | None = None,
    output_console: Any | None = None,
    error_panel_factory: Callable[[str], Any] | None = None,
    tui_event_sink: Callable[[TuiDomainEvent], None] | None = None,
) -> TurnStreamDependencies:
    return _turn_stream.default_turn_stream_dependencies(
        renderer_factory=(
            OpenTuiStreamRenderer if renderer_factory is None else renderer_factory
        ),
        stream_wrapper=stream_wrapper,
        approval_handler=(
            # Built per-call so each dependency set gets a fresh handler bound
            # to current console defaults; the handler reaches the interactive
            # surface through the renderer it is invoked with.
            tui_approval_handler() if approval_handler is None else approval_handler
        ),
        cancel_clearer=(
            _runtime_helpers.clear_current_cancel
            if cancel_clearer is None
            else cancel_clearer
        ),
        image_attachment_builder=(
            image_prompt_and_attachments
            if image_attachment_builder is None
            else image_attachment_builder
        ),
        output_console=console if output_console is None else output_console,
        error_panel_factory=(
            error_panel if error_panel_factory is None else error_panel_factory
        ),
        gateway_approval_surface=Surface.CLI_GATEWAY,
        standalone_approval_surface=Surface.CLI_STANDALONE,
        approval_surface_resolver=_approval_surface_for_terminal_output,
        tui_event_sink=tui_event_sink,
        tui_event_sink_factory=(
            None if tui_event_sink is not None else router_hud_event_sink_factory
        ),
    )
