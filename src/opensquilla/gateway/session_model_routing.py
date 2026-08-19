"""Acceptance-time model-routing resolution for session-scoped turns.

The persisted session value is deliberately resolved only for ordinary user
turns.  Background work must retain the global deployment strategy even when
it happens to target a user session for delivery or transcript context.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast

from opensquilla.gateway.model_routing import (
    ModelRoutingMode,
    capture_model_routing_config,
    durable_model_routing_config_snapshot,
    model_routing_snapshot,
)
from opensquilla.gateway.session_services import get_session_storage

# This is intentionally an allow-list rather than a deny-list.  New system
# run kinds must opt in deliberately, which keeps cron, retries, maintenance,
# and subagents on the global policy by default.
_SESSION_SCOPED_RUN_KINDS = frozenset(
    {
        "session_turn",
        "web_turn",
        "channel_turn",
    }
)
_VALID_MODES = frozenset({"direct", "router", "ensemble"})


@dataclass(frozen=True, slots=True)
class SessionModelRoutingResolution:
    """The durable session-routing coordinates accepted for one turn."""

    mode: ModelRoutingMode | None
    revision: int | None = None
    source: str = "global_policy"


def uses_session_model_routing(run_kind: str | None) -> bool:
    """Whether this run kind is an ordinary user session turn."""

    return str(run_kind or "default") in _SESSION_SCOPED_RUN_KINDS


def _as_mode(value: Any) -> ModelRoutingMode | None:
    raw = str(value or "").strip().lower()
    if raw not in _VALID_MODES:
        return None
    return cast(ModelRoutingMode, raw)


def _resolution_from_value(
    value: Any,
    *,
    fallback_source: str,
) -> SessionModelRoutingResolution:
    if isinstance(value, dict):
        mode = _as_mode(value.get("mode"))
        raw_revision = value.get("revision")
        initialized = bool(value.get("initialized", False))
        raw_source = value.get("source")
    else:
        mode = _as_mode(getattr(value, "mode", value))
        raw_revision = getattr(value, "revision", None)
        initialized = bool(getattr(value, "initialized", False))
        raw_source = getattr(value, "source", None)
    revision = (
        raw_revision
        if isinstance(raw_revision, int) and not isinstance(raw_revision, bool)
        else None
    )
    source = str(raw_source or "").strip()
    if not source:
        source = "session_default_initialized" if initialized else fallback_source
    return SessionModelRoutingResolution(mode=mode, revision=revision, source=source)


async def _await_if_needed(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _resolve_via_manager(
    session_manager: Any,
    *,
    session_key: str,
    fallback_mode: ModelRoutingMode,
) -> SessionModelRoutingResolution:
    """Use the public manager resolver, including legacy-NULL initialization."""

    resolver = getattr(session_manager, "get_session_routing", None)
    if callable(resolver):
        result = await _await_if_needed(
            resolver(session_key, fallback_mode=fallback_mode)
        )
        return _resolution_from_value(
            result,
            fallback_source="session_persisted",
        )

    # During staged upgrades a manager can precede the public facade while its
    # storage already owns the atomic NULL -> global-mode transition.  This is
    # deliberately a compatibility bridge, not a second persistence policy.
    storage = get_session_storage(session_manager)
    resolver = getattr(storage, "resolve_model_routing_mode", None)
    if callable(resolver):
        result = await _await_if_needed(resolver(session_key, fallback_mode))
        return _resolution_from_value(
            result,
            fallback_source="session_storage_compat",
        )
    return SessionModelRoutingResolution(mode=None)


async def resolve_session_model_routing_resolution(
    config: Any,
    session_manager: Any,
    *,
    session_key: str,
    run_kind: str,
) -> SessionModelRoutingResolution:
    """Resolve one interactive session's persisted mode at acceptance.

    A ``resolution.mode`` of ``None`` means the resolver is unavailable (for
    an older embedded manager/test double) and callers retain the global
    snapshot.  A production resolver atomically materializes a legacy NULL
    from the global mode before returning, so subsequent global changes cannot
    drift that session's default.
    """

    if not uses_session_model_routing(run_kind) or session_manager is None:
        return SessionModelRoutingResolution(mode=None)
    global_mode = cast(ModelRoutingMode, model_routing_snapshot(config)["mode"])
    return await _resolve_via_manager(
        session_manager,
        session_key=session_key,
        fallback_mode=global_mode,
    )


async def resolve_session_model_routing_mode(
    config: Any,
    session_manager: Any,
    *,
    session_key: str,
    run_kind: str,
) -> ModelRoutingMode | None:
    """Compatibility projection returning only the persisted mode."""

    resolution = await resolve_session_model_routing_resolution(
        config,
        session_manager,
        session_key=session_key,
        run_kind=run_kind,
    )
    return resolution.mode


async def capture_accepted_model_routing_config(
    config: Any,
    session_manager: Any,
    *,
    session_key: str,
    run_kind: str,
) -> Any:
    """Capture the routing policy accepted by one task or direct turn.

    The session resolver is intentionally read immediately before this capture
    rather than at queue reservation time.  That makes the snapshot correspond
    to durable task acceptance; a queued task retains it while a follow-up
    task resolves the (possibly newer) session mode afresh.
    """

    resolution = await resolve_session_model_routing_resolution(
        config,
        session_manager,
        session_key=session_key,
        run_kind=run_kind,
    )
    return capture_model_routing_config(
        config,
        session_mode=resolution.mode,
        session_routing_revision=resolution.revision,
        session_routing_source=resolution.source,
    )


def capture_prepared_session_model_routing_config(
    config: Any,
    session_node: Any,
) -> Any:
    """Capture a new/forked Session before its node is durably visible."""

    mode = _as_mode(getattr(session_node, "model_routing_mode", None))
    if mode is None:
        raise ValueError("prepared session is missing a model routing mode")
    raw_revision = getattr(session_node, "model_routing_revision", 0)
    revision = (
        raw_revision
        if isinstance(raw_revision, int) and not isinstance(raw_revision, bool)
        else 0
    )
    return capture_model_routing_config(
        config,
        session_mode=mode,
        session_routing_revision=max(0, revision),
        session_routing_source="session",
    )


def accepted_model_routing_audit(
    accepted_config: Any,
    *,
    run_kind: str,
) -> dict[str, Any] | None:
    """Return a compact, non-secret audit projection for an accepted task."""

    if accepted_config is None:
        return None
    routing = model_routing_snapshot(accepted_config)
    session_mode = _as_mode(getattr(accepted_config, "session_mode", None))
    revision = getattr(accepted_config, "session_routing_revision", None)
    if not isinstance(revision, int) or isinstance(revision, bool):
        revision = None
    source = str(
        getattr(accepted_config, "session_routing_source", "global_policy")
        or "global_policy"
    )
    audit = {
        "scope": "session" if session_mode is not None else "global",
        "session_mode": session_mode,
        "session_revision": revision,
        "source": source,
        "effective_mode": routing["mode"],
        "router_enabled": routing["router_enabled"],
        "ensemble_enabled": routing["ensemble_enabled"],
        "rollout_phase": routing["rollout_phase"],
        "selection_mode": routing["selection_mode"],
        "run_kind": run_kind,
    }
    config_snapshot = durable_model_routing_config_snapshot(accepted_config)
    if config_snapshot is not None:
        audit["config_snapshot"] = config_snapshot
    return audit


async def accepted_model_routing_stream(
    stream: AsyncIterator[Any],
    accepted_config: Any,
) -> AsyncIterator[Any]:
    """Keep an acceptance snapshot installed while a direct stream is read."""

    from opensquilla.engine.runtime import accepted_turn_config_scope

    with accepted_turn_config_scope(accepted_config):
        async for event in stream:
            yield event


__all__ = [
    "accepted_model_routing_audit",
    "accepted_model_routing_stream",
    "capture_accepted_model_routing_config",
    "capture_prepared_session_model_routing_config",
    "resolve_session_model_routing_resolution",
    "resolve_session_model_routing_mode",
    "uses_session_model_routing",
]
