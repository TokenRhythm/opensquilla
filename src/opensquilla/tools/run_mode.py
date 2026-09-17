"""Request-scoped sandbox run mode helpers for tool implementations."""

from __future__ import annotations

import contextlib
import os
from typing import cast

from opensquilla.run_mode import RunMode, normalize_run_mode
from opensquilla.tools.types import current_tool_context

_VALID_RUN_MODES = frozenset({"safe", "full"})

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

    if ctx is not None and bool(getattr(ctx, "guest_safe", False)):
        # Guest authority is server-computed and cannot soft-land or be
        # approval-upgraded into host execution, even if the backend later
        # becomes unavailable.
        return False

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
        normalized_mode = None
        if mode_value is not None and str(mode_value).strip():
            with contextlib.suppress(ValueError):
                normalized_mode = normalize_run_mode(mode_value)
        if normalized_mode is not None:
            return normalized_mode is RunMode.FULL
        run_context_mode = getattr(getattr(ctx, "sandbox_run_context", None), "run_mode", None)
        run_context_mode_value = getattr(run_context_mode, "value", run_context_mode)
        normalized_context_mode = None
        if run_context_mode_value is not None and str(run_context_mode_value).strip():
            with contextlib.suppress(ValueError):
                normalized_context_mode = normalize_run_mode(run_context_mode_value)
        if normalized_context_mode is not None:
            return normalized_context_mode is RunMode.FULL
        if getattr(ctx, "elevated", None) == "full":
            return True
    if sandbox_disabled_without_fallback:
        return False
    return bool(
        runtime is not None and getattr(runtime, "default_run_mode", None) == "full"
    )


def _declared_run_mode_for_context(ctx: object | None) -> tuple[RunMode | None, bool]:
    """Resolve context/session declarations without consulting runtime fallback.

    The boolean reports whether the canonical value may be cached on the tool
    context, preserving ``current_run_mode``'s existing mutation behavior.
    """

    if ctx is None:
        return None, False
    if bool(getattr(ctx, "guest_safe", False)):
        return RunMode.SAFE, True
    raw_mode = getattr(ctx, "run_mode", None)
    if raw_mode is not None:
        with contextlib.suppress(ValueError):
            return normalize_run_mode(raw_mode), True
    run_context_mode = getattr(getattr(ctx, "sandbox_run_context", None), "run_mode", None)
    run_context_mode_value = getattr(run_context_mode, "value", run_context_mode)
    if run_context_mode_value is not None:
        with contextlib.suppress(ValueError):
            return normalize_run_mode(run_context_mode_value), True
    session_key = getattr(ctx, "session_key", None)
    if session_key:
        with contextlib.suppress(Exception):
            from opensquilla.gateway.approval_queue import get_approval_queue

            queued_mode = get_approval_queue().get_run_mode(session_key)
            if queued_mode in _VALID_RUN_MODES:
                return normalize_run_mode(queued_mode), True
    elevated = getattr(ctx, "elevated", None)
    if elevated == "full":
        return RunMode.FULL, False
    if elevated in ("on", "bypass"):
        return RunMode.SAFE, False
    return None, False


def effective_run_mode_for_context(ctx: object | None) -> RunMode:
    """Return the effective Safe/Full mode for work outside tool dispatch.

    This combines persisted/context declarations with the configured sandbox
    fallback, matching the mode used by actual process execution.
    """

    declared_mode, cache_on_context = _declared_run_mode_for_context(ctx)
    if declared_mode is not None and cache_on_context and ctx is not None:
        # Match current_run_mode(): session and persisted declarations must be
        # visible to full_host_access_for_context before it considers the
        # runtime's default mode.  Otherwise a legacy session explicitly
        # authorized for Safe can inherit a process-wide Full default here.
        setattr(ctx, "run_mode", declared_mode.value)
    if declared_mode is RunMode.FULL or full_host_access_for_context(ctx):
        return RunMode.FULL
    return declared_mode or RunMode.SAFE


def current_run_mode() -> str | None:
    """Return the active canonical Safe/Full mode for this tool call."""

    ctx = current_tool_context.get()
    if ctx is None:
        return None
    mode, cache_on_context = _declared_run_mode_for_context(ctx)
    if mode is None:
        return None
    value = cast(str, mode.value)
    if cache_on_context:
        ctx.run_mode = value
    return value


def full_host_access_active() -> bool:
    """True when the current tool call should use Full Host Access semantics."""

    if current_run_mode() == "full":
        return True
    return full_host_access_for_context(current_tool_context.get())


def trusted_sandbox_active() -> bool:
    """Compatibility alias: true when the current tool call is in Safe mode."""

    return not full_host_access_active() and current_run_mode() == "safe"


__all__ = [
    "current_run_mode",
    "effective_run_mode_for_context",
    "full_host_access_active",
    "full_host_access_for_context",
    "sandbox_disabled_full_host_fallback",
    "trusted_sandbox_active",
]
