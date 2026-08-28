"""RPC handlers for operator routing control (per-session router holds).

``routing.hold.set`` / ``routing.hold.clear`` / ``routing.hold.get`` are a
thin authenticated wrapper over the existing in-memory
:class:`~opensquilla.router_control.RouterControlHoldStore`. The store the
handlers mutate is the one living on the shared gateway ``TurnRunner``
(``ctx.turn_runner.router_control_hold_store``) — the same instance the
router step consults via turn metadata — so an operator hold set here pins
the very next routed turn of that session.

Authorization model: like the rest of the session RPC surface
(``sessions.patch`` / ``sessions.reset`` in ``rpc_sessions.py``), sessions
are not per-caller owner-scoped; access is governed purely by operator
scopes. Mutation is therefore admin-gated (matching ``sessions.patch``,
which also rebinds a session's model) and the peek is read-scoped.

Scope note (decision H2): operator holds accept only text-tier targets
(``c0``–``c3``, legacy ``t0``–``t3`` aliases) and ``auto``; ``mode:*``
pinning is deliberately not part of this surface.
"""

from __future__ import annotations

import math
import time
from typing import Any

from opensquilla.gateway.rpc import RpcContext, RpcUnavailableError, get_dispatcher
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.router_control import (
    DEFAULT_HOLD_TTL_SECONDS,
    DEFAULT_HOLD_TURNS,
    RouterControlHold,
    RouterControlHoldStore,
    RouterControlTarget,
    RouterControlValidationError,
    resolve_router_control_target,
)
from opensquilla.router_tiers import normalize_target_id, normalize_text_tier
from opensquilla.session.keys import canonicalize_session_key

_d = get_dispatcher()

# Pseudo-target restoring automatic routing (clears the session's hold).
AUTO_TARGET = "auto"
# ``RouterControlHold.source`` marker distinguishing operator-set holds from
# LLM-directed ``router_control`` tool holds in metadata/decision logs.
HOLD_SOURCE_RPC = "routing_hold_rpc"
_HOLD_EVIDENCE_RPC = "operator rpc routing.hold.set"


def normalize_hold_target(value: object) -> str:
    """Coerce an operator-supplied hold target to ``auto`` or ``tier:cN``.

    Accepts ``auto``, bare text tier ids (``c2``), legacy aliases (``t2``),
    and full target ids (``tier:c2`` / ``tier:t2``), case- and
    whitespace-insensitively. Everything else — including ``mode:*`` targets,
    which are out of scope for the operator hold surface — raises
    :class:`RouterControlValidationError`.
    """
    raw = str(value or "").strip().lower()
    if not raw:
        raise RouterControlValidationError("routing.hold target is required")
    if raw == AUTO_TARGET:
        return AUTO_TARGET
    candidate = normalize_target_id(raw if raw.startswith("tier:") else f"tier:{raw}")
    tier = None
    if candidate.startswith("tier:"):
        tier = normalize_text_tier(candidate.removeprefix("tier:"))
    if tier is None:
        raise RouterControlValidationError(
            f"routing.hold target {str(value).strip()!r} is not supported; "
            "expected 'auto' or a text tier id (c0-c3)"
        )
    return f"tier:{tier}"


def _require_session_key(params: dict | None) -> str:
    if not isinstance(params, dict) or "sessionKey" not in params:
        raise ValueError("params.sessionKey is required")
    key = params["sessionKey"]
    if not isinstance(key, str):
        raise ValueError("params.sessionKey must be a string")
    canonical = canonicalize_session_key(key)
    if not canonical:
        raise ValueError("params.sessionKey must be a non-empty string")
    return canonical


def _require_hold_store(ctx: RpcContext) -> RouterControlHoldStore:
    store = getattr(ctx.turn_runner, "router_control_hold_store", None)
    if not isinstance(store, RouterControlHoldStore):
        raise RpcUnavailableError("router-control hold store is not wired")
    return store


async def _require_session(ctx: RpcContext, key: str) -> Any:
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise RpcUnavailableError("session storage is not wired")
    session = await storage.get_session(key)
    if session is None:
        raise KeyError(f"Session not found: {key}")
    return session


def _router_cfg(ctx: RpcContext) -> Any:
    return getattr(ctx.config, "squilla_router", None)


def _optional_turns(params: dict) -> int:
    value = params.get("turns", DEFAULT_HOLD_TURNS)
    if value is None:
        return DEFAULT_HOLD_TURNS
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("params.turns must be an integer")
    if value < 0:
        raise ValueError("params.turns must be >= 0 (0 = no turn cap, TTL-only)")
    return int(value)


def _optional_ttl_seconds(params: dict) -> float:
    value = params.get("ttlSeconds", DEFAULT_HOLD_TTL_SECONDS)
    if value is None:
        return DEFAULT_HOLD_TTL_SECONDS
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("params.ttlSeconds must be a number")
    ttl = float(value)
    if not math.isfinite(ttl) or ttl <= 0:
        raise ValueError("params.ttlSeconds must be a positive number")
    return ttl


def _hold_to_wire(hold: RouterControlHold, *, now_monotonic: float) -> dict[str, Any]:
    last_activity = hold.last_activity_at_monotonic
    if last_activity is None:
        last_activity = hold.started_at_monotonic
    ttl_remaining = max(0.0, hold.ttl_seconds - (now_monotonic - last_activity))
    return {
        "targetId": hold.target_id,
        "tier": hold.tier,
        "model": hold.model,
        "provider": hold.provider,
        # 0 = no turn cap; the hold then expires on idle TTL only.
        "turnsRemaining": hold.turns_remaining,
        "ttlSeconds": hold.ttl_seconds,
        "ttlRemainingSeconds": ttl_remaining,
        "source": hold.source,
        "evidence": hold.evidence,
    }


def _target_to_wire(target: RouterControlTarget) -> dict[str, Any]:
    return {
        "targetId": target.target_id,
        "targetType": target.target_type,
        "tier": target.tier,
        "model": target.model,
        "provider": target.provider,
        "description": target.description,
        "thinkingLevel": target.thinking_level,
    }


@_d.method("routing.hold.set", scope="operator.admin")
async def _handle_routing_hold_set(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    key = _require_session_key(params)
    assert isinstance(params, dict)
    target_id = normalize_hold_target(params.get("target"))
    turns = _optional_turns(params)
    ttl_seconds = _optional_ttl_seconds(params)

    router_cfg = _router_cfg(ctx)
    if router_cfg is None or not getattr(router_cfg, "enabled", False):
        # Mirrors the router_control tool: with the router disabled the hold
        # would never be applied, so reject rather than store a silent no-op.
        raise ValueError("squilla router is disabled; routing holds would have no effect")

    store = _require_hold_store(ctx)
    await _require_session(ctx, key)

    if target_id == AUTO_TARGET:
        # Pinning back to auto is the same operation as clearing the hold.
        store.clear(key)
        return {"sessionKey": key, "hold": None}

    target = resolve_router_control_target(router_cfg, target_id)
    if target.target_type != "tier":
        # Defense in depth for decision H2: only tier targets (and auto) are
        # settable through this RPC even if the target menu ever grows.
        raise RouterControlValidationError(
            f"routing.hold target type {target.target_type!r} is not supported"
        )

    now = time.monotonic()
    hold = store.set_hold(
        key,
        target,
        evidence=_HOLD_EVIDENCE_RPC,
        now_monotonic=now,
        turns_remaining=turns,
        ttl_seconds=ttl_seconds,
        source=HOLD_SOURCE_RPC,
    )
    return {"sessionKey": key, "hold": _hold_to_wire(hold, now_monotonic=now)}


@_d.method("routing.hold.get", scope="operator.read")
async def _handle_routing_hold_get(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    key = _require_session_key(params)
    store = _require_hold_store(ctx)
    await _require_session(ctx, key)

    router_cfg = _router_cfg(ctx)
    now = time.monotonic()
    # Non-consuming peek: ``get_valid`` only decrements turn counters (and
    # refreshes the idle TTL) when called with ``decrement=True``, which is
    # reserved for the router step applying the hold to a real turn. The
    # default read here never spends a turn; its only side effect is lazily
    # dropping an already-expired hold.
    hold = store.get_valid(key, now_monotonic=now)
    return {
        "sessionKey": key,
        "hold": _hold_to_wire(hold, now_monotonic=now) if hold is not None else None,
        "targets": [_target_to_wire(t) for t in store.build_targets(router_cfg)],
        # ``auto`` is always a valid set target (restores automatic routing)
        # even though it is not part of the tier-derived target menu.
        "autoTargetId": AUTO_TARGET,
        "routerEnabled": bool(getattr(router_cfg, "enabled", False)),
    }


@_d.method("routing.hold.clear", scope="operator.admin")
async def _handle_routing_hold_clear(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    key = _require_session_key(params)
    store = _require_hold_store(ctx)
    await _require_session(ctx, key)
    return {"sessionKey": key, "cleared": store.clear(key) is not None}
