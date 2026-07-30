"""Request-scoped sandbox run mode helpers for tool implementations."""

from __future__ import annotations

import contextlib
import os
from typing import cast

from opensquilla.tools.types import current_tool_context

_VALID_RUN_MODES = frozenset({"standard", "trusted", "full"})

_SANDBOX_DISABLED_FULL_HOST_ENV = "OPENSQUILLA_SANDBOX_DISABLED_FULL_HOST"
_SANDBOX_DISABLED_FULL_HOST_OFF = frozenset({"0", "false", "no", "off", "disabled"})


def sandbox_disabled_full_host_fallback() -> bool:
    """Whether a configured-but-disabled sandbox implies Full Host Access.

    On by default: a runtime configured with ``sandbox=False`` grants Full
    Host Access semantics to every tool call. Embedded deployments that
    disable the sandbox but still rely on the workspace policy layers
    (scratch redirect, write-deny globs, mutation receipts, effect
    enforcement) can set ``OPENSQUILLA_SANDBOX_DISABLED_FULL_HOST=off`` so
    run-mode semantics come from the tool context alone. Explicit Full run
    mode is unaffected. Reads fail safe to the default when the value is
    unrecognized.
    """

    raw = os.environ.get(_SANDBOX_DISABLED_FULL_HOST_ENV, "").strip().lower()
    return raw not in _SANDBOX_DISABLED_FULL_HOST_OFF


def full_host_access_for_context(ctx: object | None) -> bool:
    """Return Full Host Access state without consulting approval storage."""

    runtime = None
    try:
        from opensquilla.sandbox.integration import get_runtime

        runtime = get_runtime()
    except Exception:
        pass
    sandbox_disabled_without_fallback = bool(
        runtime is not None
        and not runtime.effective.sandbox_enabled
        and not sandbox_disabled_full_host_fallback()
    )
    if (
        runtime is not None
        and not runtime.effective.sandbox_enabled
        and not sandbox_disabled_without_fallback
    ):
        return True

    if ctx is not None:
        mode = getattr(ctx, "run_mode", None)
        mode_value = getattr(mode, "value", mode)
        if mode_value in _VALID_RUN_MODES:
            return bool(mode_value == "full")
        run_context_mode = getattr(getattr(ctx, "sandbox_run_context", None), "run_mode", None)
        run_context_mode_value = getattr(run_context_mode, "value", run_context_mode)
        if run_context_mode_value in _VALID_RUN_MODES:
            return bool(run_context_mode_value == "full")
        if getattr(ctx, "elevated", None) == "full":
            return True
    if sandbox_disabled_without_fallback:
        return False
    return bool(
        runtime is not None and getattr(runtime, "default_run_mode", None) == "full"
    )


def current_run_mode() -> str | None:
    """Return the active Standard/Trusted/Full mode for this tool call."""

    ctx = current_tool_context.get()
    if ctx is None:
        return None
    if ctx.run_mode in _VALID_RUN_MODES:
        return ctx.run_mode
    run_context_mode = getattr(getattr(ctx, "sandbox_run_context", None), "run_mode", None)
    run_context_mode_value = getattr(run_context_mode, "value", run_context_mode)
    if run_context_mode_value in _VALID_RUN_MODES:
        mode = cast(str, run_context_mode_value)
        ctx.run_mode = mode
        return mode
    if ctx.session_key:
        with contextlib.suppress(Exception):
            from opensquilla.gateway.approval_queue import get_approval_queue

            queued_mode = get_approval_queue().get_run_mode(ctx.session_key)
            if queued_mode in _VALID_RUN_MODES:
                mode = cast(str, queued_mode)
                ctx.run_mode = mode
                return mode
    if ctx.elevated == "full":
        return "full"
    if ctx.elevated in ("on", "bypass"):
        return "trusted"
    return None


def full_host_access_active() -> bool:
    """True when the current tool call should use Full Host Access semantics."""

    if current_run_mode() == "full":
        return True
    return full_host_access_for_context(current_tool_context.get())


def trusted_sandbox_active() -> bool:
    """True when the current tool call is in Managed Execution mode."""

    return not full_host_access_active() and current_run_mode() == "trusted"


__all__ = [
    "current_run_mode",
    "full_host_access_active",
    "full_host_access_for_context",
    "sandbox_disabled_full_host_fallback",
    "trusted_sandbox_active",
]
