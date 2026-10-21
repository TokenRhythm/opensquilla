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
from opensquilla.sandbox.types import SandboxSetupRequiredError

_LOCK = asyncio.Lock()
_SETTING_UP = False
_LAST_RESULT: SetupResult | None = None
_CAPABILITY_RESULT: SetupResult | None = None
_GENERATION = 0
_SETUP_TASK: asyncio.Task[SetupResult] | None = None
_CLOSING = False


def mark_sandbox_startup_pending() -> None:
    """Publish passive status before the gateway schedules initialization."""
    global _LAST_RESULT

    reset_sandbox_setup_runtime_state()

    _LAST_RESULT = SetupResult(
        state=SandboxSetupState.SETTING_UP,
        platform=sys.platform,
        message="Sandbox initialization will start when the gateway is ready.",
    )


async def current_sandbox_setup_runtime_status(config: Any) -> SetupResult:
    if _SETTING_UP or (_SETUP_TASK is not None and not _SETUP_TASK.done()):
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

    # Preparation progress does not withdraw a still-initialized backend.
    # The explicit check publishes invalidity before starting any repair.
    setup = _CAPABILITY_RESULT
    if setup is None:
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
    global _LAST_RESULT, _CAPABILITY_RESULT, _SETTING_UP

    generation = _GENERATION
    async with _LOCK:
        _require_current_generation(generation)
        if _LAST_RESULT is not None and _LAST_RESULT.state in {
            SandboxSetupState.NOT_SETUP,
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
        except SandboxSetupRequiredError as exc:
            _require_current_generation(generation)
            result = SetupResult(
                state=SandboxSetupState.NOT_SETUP,
                platform=sys.platform,
                message="Sandbox setup has not been completed.",
                requires_admin=sys.platform.startswith("win"),
                detail=str(exc),
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
        _CAPABILITY_RESULT = result
        return result


async def _perform_sandbox_setup(
    config: Any, generation: int, *, repair_identity: bool = False,
) -> SetupResult:
    global _LAST_RESULT, _CAPABILITY_RESULT, _SETTING_UP

    async with _LOCK:
        _require_current_generation(generation)
        # Portable setup remains reusable. Each new explicit Windows request
        # revalidates credentials; status reads and repeat clicks while this
        # operation is running do not. External repairs may invalidate a
        # previously successful result.
        if (
            _LAST_RESULT is not None
            and _LAST_RESULT.state is SandboxSetupState.READY
            and _LAST_RESULT.platform != "win32"
            and not repair_identity
        ):
            return _LAST_RESULT
        _SETTING_UP = True
        setup_result: SetupResult | None = None
        try:
            if repair_identity:
                setup_result = await ensure_sandbox_setup(config, repair_identity=True)
            else:
                setup_result = await ensure_sandbox_setup(config)
            _require_current_generation(generation)
            if setup_result.state is SandboxSetupState.READY:
                from opensquilla.sandbox.integration import initialize_runtime_backend

                await initialize_runtime_backend()
                _require_current_generation(generation)
            _LAST_RESULT = setup_result
            if setup_result.state is SandboxSetupState.READY:
                _CAPABILITY_RESULT = setup_result
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


def _start_sandbox_setup(config: Any, *, repair_identity: bool = False) -> None:
    """Register the one runtime-owned operation before yielding to any caller."""
    global _SETUP_TASK, _CAPABILITY_RESULT

    if _CLOSING or (_SETUP_TASK is not None and not _SETUP_TASK.done()):
        return
    if (
        _LAST_RESULT is not None
        and _LAST_RESULT.state is SandboxSetupState.READY
        and _LAST_RESULT.platform != "win32"
        and not repair_identity
    ):
        return
    # Capture committed availability before changing the preparation status.
    # Launch/observation failure alone does not invalidate a working backend.
    if _CAPABILITY_RESULT is None and _LAST_RESULT is not None:
        _CAPABILITY_RESULT = _LAST_RESULT
    _SETUP_TASK = asyncio.create_task(
        _perform_sandbox_setup(config, _GENERATION, repair_identity=repair_identity),
        name="sandbox-explicit-setup",
    )
    # The request/socket is not the owner. Observe even when every client left.
    _SETUP_TASK.add_done_callback(_observe_setup_completion)


def _observe_setup_completion(task: asyncio.Task[SetupResult]) -> None:
    if not task.cancelled():
        task.exception()


async def request_sandbox_setup(config: Any, *, repair_identity: bool = False) -> SetupResult:
    """Start or join preparation without holding the caller's RPC queue."""
    if _CLOSING:
        return SetupResult(
            SandboxSetupState.FAILED, sys.platform, "Gateway is shutting down.",
        )
    _start_sandbox_setup(config, repair_identity=repair_identity)
    return await current_sandbox_setup_runtime_status(config)


async def ensure_sandbox_setup_auto(config: Any, *, repair_identity: bool = False) -> SetupResult:
    """Compatibility for internal callers needing a terminal setup result."""
    status = await request_sandbox_setup(config, repair_identity=repair_identity)
    task = _SETUP_TASK
    if task is not None and not _CLOSING:
        return await asyncio.shield(task)
    return status


def sandbox_setup_generation() -> int:
    return _GENERATION


def mark_sandbox_capability_unavailable(detail: str, *, generation: int) -> None:
    """Publish a failed real check; a retired helper cannot affect new state."""
    global _LAST_RESULT, _CAPABILITY_RESULT
    if generation != _GENERATION or _CLOSING:
        return
    _LAST_RESULT = SetupResult(
        SandboxSetupState.NOT_SETUP, sys.platform,
        "Sandbox repair is required.", detail=detail,
    )
    _CAPABILITY_RESULT = _LAST_RESULT


async def shutdown_sandbox_setup_runtime() -> None:
    """Fence publication and stop observing the owned setup process on close."""
    global _CLOSING
    task = _SETUP_TASK
    reset_sandbox_setup_runtime_state()
    _CLOSING = True
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)


def _require_current_generation(generation: int) -> None:
    if generation != _GENERATION:
        raise asyncio.CancelledError("Sandbox initialization belongs to a retired runtime.")


def reset_sandbox_setup_runtime_state() -> None:
    global _GENERATION, _LAST_RESULT, _LOCK, _SETTING_UP
    global _SETUP_TASK, _CLOSING, _CAPABILITY_RESULT

    if _SETUP_TASK is not None and not _SETUP_TASK.done():
        _SETUP_TASK.cancel()
    _SETUP_TASK = None
    _CLOSING = False

    _GENERATION += 1
    _LOCK = asyncio.Lock()
    _SETTING_UP = False
    _LAST_RESULT = None
    _CAPABILITY_RESULT = None


__all__ = [
    "current_sandbox_capability_report",
    "current_sandbox_setup_runtime_status",
    "ensure_sandbox_setup_auto",
    "request_sandbox_setup",
    "mark_sandbox_capability_unavailable",
    "sandbox_setup_generation",
    "shutdown_sandbox_setup_runtime",
    "initialize_sandbox_runtime",
    "mark_sandbox_startup_pending",
    "reset_sandbox_setup_runtime_state",
]
