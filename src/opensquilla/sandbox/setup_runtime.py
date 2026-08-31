"""Sandbox initialization state; status reads never execute capability canaries."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from opensquilla.sandbox.capability_service import CapabilityReport, capability_report_from_setup
from opensquilla.sandbox.setup_state import (
    SandboxSetupState,
    SetupResult,
    ensure_sandbox_setup,
)

_LOCK = asyncio.Lock()
_SETTING_UP = False
_LAST_RESULT: SetupResult | None = None
_GENERATION = 0


def mark_sandbox_startup_pending(*, enabled: bool) -> None:
    """Publish passive status before the gateway schedules initialization."""
    global _GENERATION, _LAST_RESULT, _SETTING_UP

    _GENERATION += 1
    _SETTING_UP = False

    _LAST_RESULT = SetupResult(
        state=SandboxSetupState.SETTING_UP if enabled else SandboxSetupState.NOT_SETUP,
        platform=sys.platform,
        message=(
            "Sandbox initialization will start when the gateway is ready."
            if enabled
            else "Automatic sandbox initialization is disabled."
        ),
    )


async def current_sandbox_setup_runtime_status(config: Any) -> SetupResult:
    if _SETTING_UP:
        return SetupResult(
            state=SandboxSetupState.SETTING_UP,
            platform="auto",
            message="Sandbox initialization is running.",
        )
    if _LAST_RESULT is not None:
        return _LAST_RESULT
    # Standalone callers already selected their backend during construction.
    # Cold reads must not run platform availability checks either.
    from opensquilla.sandbox.integration import get_runtime

    runtime = get_runtime()
    backend = getattr(runtime, "backend", None)
    if backend is None:
        state = SandboxSetupState.NOT_SETUP
    elif getattr(backend, "name", "unavailable") in {"noop", "unavailable"}:
        state = SandboxSetupState.UNAVAILABLE
    else:
        state = SandboxSetupState.READY
    return SetupResult(
        state=state,
        platform=sys.platform,
        message=(
            "Sandbox initialized."
            if state is SandboxSetupState.READY
            else "Sandbox is not initialized."
        ),
        detail=getattr(backend, "reason", None),
    )


async def current_sandbox_capability_report(
    config: Any,
    *,
    force_refresh: bool = False,
) -> CapabilityReport:
    """Report initialization without executing commands or filesystem canaries.

    force_refresh remains accepted for older clients, but status reads never
    start or repair a sandbox. Each operation still enforces its own policy.
    """
    _ = force_refresh
    from opensquilla.sandbox.integration import get_runtime

    setup = await current_sandbox_setup_runtime_status(config)
    runtime = get_runtime()
    backend = str(getattr(getattr(runtime, "backend", None), "name", "unavailable"))
    if setup.state is SandboxSetupState.READY and backend in {"noop", "unavailable"}:
        setup = SetupResult(
            state=SandboxSetupState.UNAVAILABLE,
            platform=setup.platform,
            message="No initialized sandbox backend is available.",
        )
    return capability_report_from_setup(setup, backend=backend)


async def initialize_sandbox_runtime(config: Any) -> SetupResult:
    """Initialize the existing sandbox; never install or elevate on startup."""
    global _LAST_RESULT, _SETTING_UP

    generation = _GENERATION
    async with _LOCK:
        _require_current_generation(generation)
        if _LAST_RESULT is not None and _LAST_RESULT.state in {
            SandboxSetupState.READY,
            SandboxSetupState.FAILED,
            SandboxSetupState.UNAVAILABLE,
        }:
            return _LAST_RESULT
        _SETTING_UP = True
        try:
            from opensquilla.sandbox.integration import initialize_runtime_backend

            await initialize_runtime_backend()
            _require_current_generation(generation)
            result = SetupResult(
                state=SandboxSetupState.READY,
                platform=sys.platform,
                message="Sandbox initialized.",
            )
        except Exception as exc:  # noqa: BLE001 - failure disables only the sandbox
            _require_current_generation(generation)
            result = SetupResult(
                state=SandboxSetupState.FAILED,
                platform=sys.platform,
                message="Sandbox initialization failed. Full access remains available.",
                detail=str(exc),
            )
        finally:
            if generation == _GENERATION:
                _SETTING_UP = False
        _LAST_RESULT = result
        return result


async def ensure_sandbox_setup_auto(config: Any) -> SetupResult:
    global _LAST_RESULT, _SETTING_UP

    generation = _GENERATION
    async with _LOCK:
        _require_current_generation(generation)
        _SETTING_UP = True
        setup_result: SetupResult | None = None
        try:
            setup_result = await ensure_sandbox_setup(config)
            _require_current_generation(generation)
            if setup_result.state is SandboxSetupState.READY:
                from opensquilla.sandbox.integration import initialize_runtime_backend

                await initialize_runtime_backend()
                _require_current_generation(generation)
            _LAST_RESULT = setup_result
            return setup_result
        except Exception as exc:  # noqa: BLE001
            _require_current_generation(generation)
            result = SetupResult(
                state=SandboxSetupState.FAILED,
                platform=setup_result.platform if setup_result is not None else "auto",
                message="Sandbox setup failed.",
                requires_admin=(setup_result.requires_admin if setup_result is not None else False),
                detail=str(exc),
            )
            _LAST_RESULT = result
            return result
        finally:
            if generation == _GENERATION:
                _SETTING_UP = False


def _require_current_generation(generation: int) -> None:
    if generation != _GENERATION:
        raise asyncio.CancelledError("Sandbox initialization belongs to a retired runtime.")


def reset_sandbox_setup_runtime_state() -> None:
    global _GENERATION, _LAST_RESULT, _LOCK, _SETTING_UP

    _GENERATION += 1
    _LOCK = asyncio.Lock()
    _SETTING_UP = False
    _LAST_RESULT = None


__all__ = [
    "current_sandbox_capability_report",
    "current_sandbox_setup_runtime_status",
    "ensure_sandbox_setup_auto",
    "initialize_sandbox_runtime",
    "mark_sandbox_startup_pending",
    "reset_sandbox_setup_runtime_state",
]
