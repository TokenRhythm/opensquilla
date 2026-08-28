"""Plugin runtime for renderer-independent TUI projections."""

from __future__ import annotations

import collections
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

import structlog

from opensquilla.cli.tui.backend.domain_events import TuiDomainEvent

log = structlog.get_logger(__name__)

# Errors are recorded per event dispatch, which can run tens of times per
# second during streaming — the ledger must stay bounded for the lifetime of
# a long session.
_MAX_RECORDED_ERRORS = 100


@dataclass(frozen=True)
class TuiPluginError:
    plugin_id: str
    message: str


class TuiPluginContext:
    def __init__(self) -> None:
        self._state: dict[str, object] = {}
        self._errors: collections.deque[TuiPluginError] = collections.deque(
            maxlen=_MAX_RECORDED_ERRORS
        )
        self._warned_plugin_ids: set[str] = set()

    def set_state(self, key: str, value: object) -> None:
        self._state[key] = value

    def get_state(self, key: str, default: object | None = None) -> object | None:
        return self._state.get(key, default)

    def record_error(self, plugin_id: str, message: str) -> None:
        self._errors.append(TuiPluginError(plugin_id=plugin_id, message=message))
        # One warning per plugin per session: enough for operators to notice a
        # broken plugin without flooding the log on every streamed event.
        if plugin_id not in self._warned_plugin_ids:
            self._warned_plugin_ids.add(plugin_id)
            log.warning("tui_plugin.error", plugin_id=plugin_id, error=message)

    @property
    def errors(self) -> tuple[TuiPluginError, ...]:
        return tuple(self._errors)


class TuiPlugin(Protocol):
    plugin_id: str
    slots: frozenset[str]

    def on_event(self, event: TuiDomainEvent, context: TuiPluginContext) -> None: ...

    def snapshot(self, slot: str) -> object | None: ...


@dataclass(frozen=True)
class _RegisteredPlugin:
    priority: int
    order: int
    plugin: TuiPlugin


class TuiPluginManager:
    def __init__(self, plugins: Iterable[TuiPlugin] = ()) -> None:
        self.context = TuiPluginContext()
        self._registered: list[_RegisteredPlugin] = []
        self._next_order = 0
        for plugin in plugins:
            self.register(plugin)

    @property
    def plugins(self) -> tuple[TuiPlugin, ...]:
        return tuple(entry.plugin for entry in self._ordered_plugins())

    @property
    def errors(self) -> tuple[TuiPluginError, ...]:
        return self.context.errors

    def register(self, plugin: TuiPlugin, *, priority: int = 0) -> None:
        self._registered.append(
            _RegisteredPlugin(
                priority=priority,
                order=self._next_order,
                plugin=plugin,
            )
        )
        self._next_order += 1

    def dispatch(self, event: TuiDomainEvent) -> None:
        for entry in self._ordered_plugins():
            plugin = entry.plugin
            try:
                plugin.on_event(event, self.context)
            except Exception as exc:
                self.context.record_error(plugin.plugin_id, str(exc))

    def snapshot(self, slot: str) -> object | None:
        for entry in self._ordered_plugins():
            plugin = entry.plugin
            if slot not in plugin.slots:
                continue
            try:
                value = plugin.snapshot(slot)
            except Exception as exc:
                self.context.record_error(plugin.plugin_id, str(exc))
                continue
            if value is not None:
                return value
        return None

    def _ordered_plugins(self) -> tuple[_RegisteredPlugin, ...]:
        return tuple(
            sorted(
                self._registered,
                key=lambda entry: (-entry.priority, entry.order),
            )
        )
