"""Runtime diagnostics switch state for the gateway."""

from __future__ import annotations

import os
from dataclasses import dataclass
from threading import RLock
from typing import Any

from opensquilla.observability.turn_call_log import (
    TURN_CALL_LOG_ENABLED_VALUES,
    TURN_CALL_LOG_ENV,
)


def env_forces_raw_turn_call() -> bool:
    """Return True when the raw turn-call env override is explicitly truthy."""

    return os.environ.get(TURN_CALL_LOG_ENV, "").strip().lower() in TURN_CALL_LOG_ENABLED_VALUES


@dataclass(frozen=True)
class DiagnosticsSnapshot:
    configured_enabled: bool
    runtime_enabled: bool | None
    runtime_raw: bool
    effective_enabled: bool
    detail: str
    raw_enabled: bool
    raw_source: str
    env_override: bool
    warning: str | None = None


class DiagnosticsState:
    """Gateway-lifetime, non-persistent diagnostics mode state."""

    def __init__(self, *, configured_enabled: bool = False) -> None:
        self._configured_enabled = bool(configured_enabled)
        self._runtime_enabled: bool | None = None
        self._runtime_raw = False
        self._lock = RLock()

    @classmethod
    def from_config(cls, config: Any | None) -> DiagnosticsState:
        return cls(
            configured_enabled=bool(getattr(config, "diagnostics_enabled", False))
            if config is not None
            else False
        )

    def set_runtime(self, *, enabled: bool, raw: bool = False) -> DiagnosticsSnapshot:
        with self._lock:
            self._runtime_enabled = bool(enabled)
            self._runtime_raw = bool(enabled and raw)
            return self.snapshot()

    def raw_turn_call_enabled(self) -> bool:
        return self.snapshot().raw_enabled

    def raw_turn_call_source(self) -> str:
        return self.snapshot().raw_source

    def snapshot(self) -> DiagnosticsSnapshot:
        with self._lock:
            effective_enabled = (
                self._configured_enabled
                if self._runtime_enabled is None
                else self._runtime_enabled
            )
            runtime_raw = bool(effective_enabled and self._runtime_raw)
            env_override = env_forces_raw_turn_call()
            if env_override:
                raw_source = "env"
            elif runtime_raw:
                raw_source = "runtime"
            else:
                raw_source = "off"
            raw_enabled = raw_source != "off"
            detail = "raw" if runtime_raw else "standard" if effective_enabled else "off"
            warning = (
                f"{TURN_CALL_LOG_ENV} still forces raw capture"
                if env_override and not runtime_raw
                else None
            )
            return DiagnosticsSnapshot(
                configured_enabled=self._configured_enabled,
                runtime_enabled=self._runtime_enabled,
                runtime_raw=self._runtime_raw,
                effective_enabled=effective_enabled,
                detail=detail,
                raw_enabled=raw_enabled,
                raw_source=raw_source,
                env_override=env_override,
                warning=warning,
            )


def diagnostics_status_payload(
    state: DiagnosticsState | None,
    config: Any | None,
) -> dict[str, Any]:
    """Build a stable RPC/status payload for the current diagnostics state."""

    resolved_state = state or DiagnosticsState.from_config(config)
    snapshot = resolved_state.snapshot()
    payload: dict[str, Any] = {
        "enabled": snapshot.effective_enabled,
        "detail": snapshot.detail,
        "configured": {
            "diagnostics_enabled": snapshot.configured_enabled,
        },
        "runtime": {
            "enabled": snapshot.runtime_enabled,
            "raw": snapshot.runtime_raw,
        },
        "raw_turn_call": {
            "enabled": snapshot.raw_enabled,
            "source": snapshot.raw_source,
            "env_override": snapshot.env_override,
        },
        "applies_to": "next_turn",
        "server_debug_changed": False,
        "auth_scope_changed": False,
    }
    if snapshot.warning:
        payload["warning"] = snapshot.warning
    return payload
