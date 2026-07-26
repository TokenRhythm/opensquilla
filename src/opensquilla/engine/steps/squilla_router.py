"""Step 2: Squilla router — classify message complexity and route to appropriate model.

Runs 2-level ThinkingController + PromptController on top of the routing
output.  Rollout is gated via ``squilla_router.rollout_phase`` so existing
deployments see no behavioral change until the operator opts in.
"""

from __future__ import annotations

import logging
import threading
import time
from inspect import Parameter, signature
from pathlib import Path
from typing import Any, Protocol, cast

import structlog

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.pricing import lookup_price
from opensquilla.engine.routing import (
    BudgetGateInput,
    CalibrationState,
    PolicyInputs,
    RoutingDecision,
    RoutingPolicyEngine,
    TierCapability,
    calibration_path,
    load_calibration,
    provider_mismatch,
    provider_mismatch_veto,
    reconcile_controller_with_final_tier,
    record_provider_mismatch_veto_trail,
    route_class_for_tier,
)
from opensquilla.engine.routing.policy_data import DEFAULT_CONTEXT_WINDOW_TOKENS
from opensquilla.engine.steps.router_decision_record import stage_router_decision
from opensquilla.provider.context_capabilities import provider_state_continuity_diagnostic
from opensquilla.provider.model_catalog import shared_catalog
from opensquilla.provider.types import ModelCapabilities
from opensquilla.router_control import RouterControlHoldStore
from opensquilla.router_runtime_diagnostics import (
    classify_router_runtime_error,
    router_runtime_operator_message,
)
from opensquilla.router_tiers import (
    DEFAULT_TEXT_TIER,
    TEXT_TIERS,
    TierConfig,
    normalize_text_tier,
    tier_index,
)
from opensquilla.squilla_router.controller import (
    derive_prompt_policy,
    derive_thinking_mode,
    get_prompt_hint,
    normalize_decisions,
    synthetic_one_hot,
    thinking_mode_to_level,
)

log = structlog.get_logger(__name__)
_log_std = logging.getLogger(__name__)


class RouterStrategy(Protocol):
    async def classify(
        self,
        message: str,
        valid_tiers: list[str],
        routing_history: list[dict] | None = None,
        **kwargs: object,
    ) -> tuple[str, float, str, dict]: ...


_strategy: RouterStrategy | None = None
_strategy_key: tuple | None = None
_strategy_lock = threading.Lock()
_router_runtime_warning_lock = threading.Lock()
_router_runtime_warning_emitted = False
_MAX_ROUTING_HISTORY = 5
_ROUTING_HISTORY_WINDOW = 1800
def _router_text_fallback_chain(
    selected_tier: object,
    tiers: dict,
) -> list[dict[str, str]]:
    selected = normalize_text_tier(selected_tier)
    if selected is None:
        return []
    try:
        selected_index = TEXT_TIERS.index(selected)
    except ValueError:
        return []

    chain: list[dict[str, str]] = []
    for tier_name in reversed(TEXT_TIERS[:selected_index]):
        tier_cfg = tiers.get(tier_name)
        if not isinstance(tier_cfg, dict) or tier_cfg.get("image_only", False):
            continue
        model = str(tier_cfg.get("model") or "").strip()
        if not model:
            continue
        provider = str(tier_cfg.get("provider") or "").strip()
        entry = {"tier": tier_name, "model": model}
        if provider:
            entry["provider"] = provider
        chain.append(entry)
    return chain


class RoutingHistoryStore:
    """Per-session routing history with bounded size and eviction.

    Wraps the previous module-level dict so the gateway can drop entries when
    a session terminates, preventing unbounded growth in long-running
    deployments.
    """

    def __init__(self, max_entries: int = _MAX_ROUTING_HISTORY) -> None:
        self._entries: dict[str, list[dict]] = {}
        self._max_entries = max_entries

    def get(self, session_key: str) -> list[dict] | None:
        return self._entries.get(session_key)

    def set(self, session_key: str, value: list[dict]) -> None:
        self._entries[session_key] = value

    def setdefault(self, session_key: str, default: list[dict]) -> list[dict]:
        return self._entries.setdefault(session_key, default)

    def length(self, session_key: str) -> int:
        return len(self._entries.get(session_key, []))

    def clear(self) -> None:
        self._entries.clear()

    def evict(self, session_key: str) -> bool:
        return self._entries.pop(session_key, None) is not None


_history_store = RoutingHistoryStore()


def seed_routing_history(entries_by_session: dict[str, list[dict]]) -> int:
    """Seed the in-process history store from persisted decision records.

    Boot-time rehydration hook (see engine/steps/router_decision_record.py):
    sessions that already accumulated live in-process history are never
    clobbered. Returns the number of sessions seeded.
    """
    seeded = 0
    for session_key, entries in entries_by_session.items():
        if not session_key or not entries:
            continue
        if _history_store.get(session_key):
            continue
        _history_store.set(
            session_key,
            [dict(entry) for entry in entries][-_MAX_ROUTING_HISTORY:],
        )
        seeded += 1
    return seeded


_DEFER_ROUTING_HISTORY_KEY = "_defer_squilla_router_history"
_PENDING_ROUTING_HISTORY_ENTRY_KEY = "_pending_squilla_router_history_entry"
_PENDING_ROUTING_HISTORY_SESSION_KEY = "_pending_squilla_router_history_session"
_THINKING_LEVELS = {"minimal", "low", "medium", "high", "xhigh", "adaptive"}


def _routing_history_entry(
    *,
    text: str,
    extra: dict,
    decision: RoutingDecision,
) -> dict:
    return {
        "text": text,
        **extra,
        "base_tier": extra.get("base_tier", decision.tier),
        "final_tier": extra.get("final_tier", decision.tier),
        "final_route_class": extra.get("final_route_class"),
    }


def _append_routing_history(session_key: str, entry_payload: dict) -> list[dict]:
    history = _history_store.setdefault(session_key, [])
    entry = {
        "turn_index": len(history),
        "_ts": time.monotonic(),
        **entry_payload,
    }
    history.append(entry)
    if len(history) > _MAX_ROUTING_HISTORY:
        _history_store.set(session_key, history[-_MAX_ROUTING_HISTORY:])
    log.debug(
        "squilla_router.history_appended",
        session=session_key,
        turn_index=entry["turn_index"],
        route_class=entry.get("route_class"),
        total_history=_history_store.length(session_key),
    )
    return _history_store.get(session_key) or []


def commit_deferred_router_history(ctx: TurnContext) -> TurnContext:
    """Commit deferred routing history after a bounded router step succeeds."""

    entry_payload = ctx.metadata.pop(_PENDING_ROUTING_HISTORY_ENTRY_KEY, None)
    session_key = ctx.metadata.pop(_PENDING_ROUTING_HISTORY_SESSION_KEY, ctx.session_key)
    ctx.metadata.pop(_DEFER_ROUTING_HISTORY_KEY, None)
    if isinstance(entry_payload, dict):
        ctx.metadata["routing_history"] = _append_routing_history(session_key, entry_payload)
    return ctx


_RESPONSE_POLICY_OPEN = "[RESPONSE_POLICY:"

# Post-classifier heuristics (confidence gate, complaint upgrade,
# anti-downgrade, large-context floor, bind) live in engine/routing/policy.py;
# the engine is stateless so one shared instance serves every turn.
_POLICY_ENGINE = RoutingPolicyEngine()

# On-device calibration: cached by the calibration file's mtime so the common
# path costs one stat() per turn, not a full parse. Only consulted when the
# operator opts into ``squilla_router.calibration_enabled`` — with the flag off
# (the default) this returns ``None`` and the confidence gate is byte-identical
# to today.
_calibration_cache_lock = threading.Lock()
_calibration_cache_state: CalibrationState | None = None
_calibration_cache_mtime: float | None = None
_calibration_cache_valid = False


def _calibration_for_turn(router_cfg: object) -> CalibrationState | None:
    """Return the active calibration state, or ``None`` for the default path.

    ``None`` is returned when calibration is disabled or the state is neutral,
    so the only time a non-``None`` state reaches the policy engine is when a
    real, non-neutral calibration file exists — keeping the parity contract.
    """
    if not getattr(router_cfg, "calibration_enabled", False):
        return None
    global _calibration_cache_state, _calibration_cache_mtime, _calibration_cache_valid
    try:
        path = calibration_path()
    except Exception:  # noqa: BLE001 - calibration must never break routing
        return None
    try:
        mtime: float | None = path.stat().st_mtime
    except OSError:
        mtime = None
    with _calibration_cache_lock:
        if not (_calibration_cache_valid and _calibration_cache_mtime == mtime):
            try:
                _calibration_cache_state = load_calibration(path)
            except Exception:  # noqa: BLE001 - fall back to the neutral/default path
                _calibration_cache_state = None
            _calibration_cache_mtime = mtime
            _calibration_cache_valid = True
        state = _calibration_cache_state
    if state is None or state.is_neutral():
        return None
    return state


class _UnavailableV4Strategy:
    source = "v4_unavailable"
    requires_history = True

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def classify(
        self,
        message: str,
        valid_tiers: list[str],
        routing_history: list[dict] | None = None,
        **kwargs: object,
    ) -> tuple[str, float, str, dict]:
        tier = (
            DEFAULT_TEXT_TIER
            if DEFAULT_TEXT_TIER in valid_tiers
            else (valid_tiers[0] if valid_tiers else DEFAULT_TEXT_TIER)
        )
        return (
            tier,
            0.0,
            "v4_unavailable",
            {
                "route_class": "R1",
                "top1_label": "R1",
                "thinking_mode": "T1",
                "prompt_policy": "P1",
                "model_version": "unavailable",
                "error": str(self.error),
            },
        )


def _capture_flags(config: object) -> tuple[bool, bool]:
    """Return ``(emit_train_features, emit_raw_bge)`` from self-learning config."""

    sl = getattr(config, "self_learning", None)
    if sl is None:
        return (False, False)
    capture = bool(getattr(sl, "enabled", False)) and bool(getattr(sl, "capture_enabled", True))
    return (capture, bool(getattr(sl, "enable_mlp", False)))


def _active_bundle_dir(config: object) -> str | None:
    """Resolve a promoted self-learning bundle dir, or None to use the base.

    Only consulted when self-learning is enabled, so the default install pays no
    extra cost. Verifies the candidate's base-fingerprint pin first (memoized
    per pointer/base pair inside ``verify_active_bundle``, so the hash cost is
    paid once per swap, not per turn): a package upgrade that replaced the
    shipped weights detaches the now-stale candidate instead of serving a
    hybrid of new projections and an old head. Falls back to baseline on any
    error.
    """

    sl = getattr(config, "self_learning", None)
    if sl is None or not getattr(sl, "enabled", False):
        return None
    try:
        from opensquilla.squilla_router.self_learning.promotion import (
            resolve_active_bundle_dir,
            verify_active_bundle,
        )

        verify_active_bundle(_base_bundle_dir(config))
        resolved = resolve_active_bundle_dir()
        return str(resolved) if resolved is not None else None
    except Exception:  # noqa: BLE001 — never let pointer resolution break routing
        return None


def _base_bundle_dir(config: object) -> Path:
    """The configured or packaged base bundle root (never the learned one)."""

    configured = getattr(config, "v4_bundle_dir", None)
    if configured:
        return Path(configured)
    from opensquilla.squilla_router.v4_phase3 import default_bundle_dir

    return default_bundle_dir()


def invalidate_strategy_cache() -> None:
    """Drop the cached strategy so the next turn reloads the active bundle.

    Called after a promotion/rollback swaps the active pointer in-process.
    """

    global _strategy, _strategy_key  # noqa: PLW0603
    with _strategy_lock:
        _strategy = None
        _strategy_key = None
        _history_store.clear()


def _register_self_learning_invalidator() -> None:
    """Hand the offline self-learning loop a way to drop our strategy cache.

    Registration lives here (engine -> squilla_router is an approved import
    edge) so the orchestrator never has to import the engine back.
    """

    try:
        from opensquilla.squilla_router.self_learning.hooks import set_cache_invalidator

        set_cache_invalidator(invalidate_strategy_cache)
    except Exception:  # noqa: BLE001 — the seam is optional; routing must not care
        pass


_register_self_learning_invalidator()


def _strategy_cache_key(config: object) -> tuple:
    strategy_name = _strategy_name(config)
    confidence = getattr(config, "confidence_threshold", 0.5)
    return (
        strategy_name,
        getattr(config, "v4_bundle_dir", None),
        getattr(config, "v4_use_aux_head", None),
        getattr(config, "require_router_runtime", False),
        confidence,
        _capture_flags(config),
        _active_bundle_dir(config),
    )


def _strategy_name(config: object) -> str:
    configured = str(getattr(config, "strategy", "v4_phase3") or "v4_phase3")
    if configured != "v4_phase3":
        log.warning(
            "squilla_router.removed_strategy_ignored",
            strategy=configured,
            using="v4_phase3",
        )
    return "v4_phase3"


def _is_history_strategy(strategy_name: str) -> bool:
    return strategy_name == "v4_phase3"


def _warn_router_runtime_fallback_once(error: Exception | str) -> None:
    global _router_runtime_warning_emitted  # noqa: PLW0603
    with _router_runtime_warning_lock:
        if _router_runtime_warning_emitted:
            return
        _router_runtime_warning_emitted = True
    _log_std.warning("%s Error: %s", router_runtime_operator_message(error), error)


def _degraded_fallback_strategy(error: Exception) -> RouterStrategy:
    """Fallback chain for a failed ML runtime load: heuristic, then default-only.

    The heuristic strategy is dependency-free, so the import is expected to
    succeed on every install; the guard is a defensive final safety net that
    preserves the legacy degrade-to-default behavior rather than taking the
    router step down entirely.
    """
    try:
        from opensquilla.engine.routing.heuristic import HeuristicRouterStrategy

        return cast(RouterStrategy, HeuristicRouterStrategy(error=error))
    except Exception:  # noqa: BLE001 - any failure here must not break the turn loop.
        log.warning("squilla_router.heuristic_fallback_unavailable", exc_info=True)
        return _UnavailableV4Strategy(error)


def _get_strategy(config: object) -> RouterStrategy:
    global _strategy, _strategy_key  # noqa: PLW0603
    with _strategy_lock:
        key = _strategy_cache_key(config)
        if _strategy is not None and _strategy_key == key:
            return _strategy
        if _strategy_key is not None and _strategy_key != key:
            _history_store.clear()

        emit_train_features, emit_raw_bge = _capture_flags(config)
        learned_dir = _active_bundle_dir(config)
        base_dir = getattr(config, "v4_bundle_dir", None)

        def _build(bundle_dir: str | None) -> RouterStrategy:
            from opensquilla.squilla_router.v4_phase3 import V4Phase3Strategy

            built = cast(
                RouterStrategy,
                V4Phase3Strategy(
                    bundle_dir=bundle_dir,
                    confidence_threshold=getattr(config, "confidence_threshold", 0.5),
                    require_router_runtime=getattr(config, "require_router_runtime", False),
                    use_aux_head=getattr(config, "v4_use_aux_head", None),
                    emit_train_features=emit_train_features,
                    emit_raw_bge=emit_raw_bge,
                ),
            )
            if getattr(built, "source", "") == "v4_phase3" and not getattr(
                built, "_available", True
            ):
                # require_router_runtime=false: the V4 adapter swallowed its
                # own init failure. Surface it so the fallback chain (learned
                # -> baseline -> heuristic) applies uniformly — the flag opts
                # out of loud failure, not of useful routing.
                raise RuntimeError("V4 Phase 3 router did not become available")
            return built

        try:
            strategy = _build(str(learned_dir) if learned_dir else base_dir)
        except Exception as exc:  # noqa: BLE001
            if learned_dir is not None:
                # A broken learned bundle must degrade to the shipped ML
                # baseline, not straight to heuristic tiering.
                log.warning(
                    "squilla_router.learned_bundle_failed",
                    bundle_dir=str(learned_dir),
                    error=str(exc),
                    action="falling_back_to_baseline",
                )
                try:
                    strategy = _build(base_dir)
                except Exception as base_exc:  # noqa: BLE001
                    log.warning(
                        "squilla_router.strategy_unavailable", error=str(base_exc)
                    )
                    _warn_router_runtime_fallback_once(base_exc)
                    strategy = _degraded_fallback_strategy(base_exc)
            else:
                log.warning("squilla_router.strategy_unavailable", error=str(exc))
                _warn_router_runtime_fallback_once(exc)
                strategy = _degraded_fallback_strategy(exc)
        _strategy = strategy
        _strategy_key = key
        return strategy


def preload_strategy(config: object) -> RouterStrategy:
    return _get_strategy(config)


def router_runtime_status() -> dict[str, Any]:
    """Read-only snapshot of the router runtime load outcome.

    Derived entirely from the existing strategy cache (no extra mutable
    state). Consumed by the gateway doctor so a degraded router stays
    visible after the one-time startup warning has scrolled away.

    Shape: ``initialized`` is False until the first strategy construction
    (gateway boot preloads in the background, so this is a transient
    startup state); ``loaded`` reports whether the ML runtime is serving
    turns; ``strategy`` is ``"v4_phase3"`` / ``"heuristic"`` /
    ``"unavailable"``; ``code`` carries the
    ``router_runtime_diagnostics`` failure classification when not loaded.
    """
    with _strategy_lock:
        strategy = _strategy
    if strategy is None:
        return {
            "initialized": False,
            "loaded": False,
            "code": None,
            "strategy": "unavailable",
            "error": None,
        }
    source = str(getattr(strategy, "source", "") or "")
    if source == "v4_phase3" and getattr(strategy, "_available", False):
        return {
            "initialized": True,
            "loaded": True,
            "code": None,
            "strategy": "v4_phase3",
            "error": None,
        }
    error = getattr(strategy, "error", None)
    return {
        "initialized": True,
        "loaded": False,
        "code": classify_router_runtime_error(
            error if error is not None else "router runtime unavailable"
        ),
        "strategy": "heuristic" if source == "heuristic" else "unavailable",
        "error": str(error) if error is not None else None,
    }


def _classify_context_kwargs(strategy: object, values: dict[str, object]) -> dict[str, object]:
    classify = getattr(strategy, "classify", None)
    if not callable(classify):
        return {}
    try:
        params = signature(classify).parameters
    except (TypeError, ValueError):
        return {key: value for key, value in values.items() if value is not None}
    accepts_arbitrary_kwargs = any(param.kind == Parameter.VAR_KEYWORD for param in params.values())
    return {
        key: value
        for key, value in values.items()
        if value is not None and (accepts_arbitrary_kwargs or key in params)
    }


def _normalize_thinking_level(raw: object) -> str | None:
    if isinstance(raw, bool):
        return "medium" if raw else None
    if raw is None:
        return None
    level = str(raw).strip().lower().replace("_", "-")
    aliases = {
        "x-high": "xhigh",
        "extra-high": "xhigh",
        "extra high": "xhigh",
        "max": "high",
        "highest": "high",
        "on": "low",
        "true": "medium",
        "off": "",
        "false": "",
        "none": "",
    }
    level = aliases.get(level, level)
    if not level:
        return None
    if level not in _THINKING_LEVELS:
        log.warning("squilla_router.invalid_thinking_level", value=raw)
        return None
    return level


def _tier_thinking_level(tier_cfg: dict) -> str | None:
    explicit = _normalize_thinking_level(tier_cfg.get("thinking_level", tier_cfg.get("thinking")))
    if explicit:
        return explicit
    if tier_cfg.get("supports_thinking", False):
        return "medium"
    return None


def _compute_savings(routed_model: str, tiers: dict) -> dict:
    """Return savings metadata: pct display + raw prices for per-turn USD computation.

    This intentionally follows 49b7e08: savings are the input-price delta
    between the routed model and the most-expensive configured tier. Runtime
    multiplies the same delta by the turn's input tokens to get USD savings.
    """
    text_tiers = [v for v in tiers.values() if not v.get("image_only", False)]
    priced_tiers = text_tiers or list(tiers.values())
    prices = [lookup_price(v.get("model", "")).input_per_m for v in priced_tiers]
    max_price = max(prices) if prices else 0.0
    routed_price = lookup_price(routed_model).input_per_m
    pct = (
        0.0
        if max_price <= 0 or routed_price >= max_price
        else round((max_price - routed_price) / max_price * 100, 1)
    )
    return {
        "savings_pct": pct,
        "savings_max_price_per_m": max_price,
        "savings_routed_price_per_m": routed_price,
    }


def _record_thinking_metadata(ctx: TurnContext, router_cfg: object, tier_cfg: dict) -> None:
    if not getattr(router_cfg, "auto_thinking", True):
        return
    level = _tier_thinking_level(tier_cfg)
    if level is None:
        return
    ctx.metadata["thinking_requested"] = True
    ctx.metadata["thinking_level"] = level


def _record_controller_thinking_metadata(
    ctx: TurnContext,
    router_cfg: object,
    tier_cfg: dict,
    thinking_mode: str | None,
) -> None:
    if not getattr(router_cfg, "auto_thinking", True):
        return
    if thinking_mode is not None:
        level = thinking_mode_to_level(thinking_mode)
    else:
        level = _tier_thinking_level(tier_cfg)
    if level is None:
        return
    ctx.metadata["thinking_requested"] = True
    ctx.metadata["thinking_level"] = level


def _inject_prompt_hint(message: str, hint: str) -> str:
    """Append a ``[RESPONSE_POLICY: ...]`` hint after the message; idempotent.

    Single-bracket format (not XML tags) following caveman-style guidance:
    models treat ``[Label: ...]`` as meta-instruction more reliably.
    Placed at the end with a ``---`` separator so recency bias maximises
    instruction adherence.
    """
    if _RESPONSE_POLICY_OPEN in message or not hint:
        return message
    return f"{message}\n\n---\n[RESPONSE_POLICY: {hint}]"


def _token_estimate(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(value, 0)
    return None


def _material_estimated_tokens(ctx: TurnContext, semantic_message: str) -> int:
    metadata = getattr(ctx, "metadata", {}) or {}
    candidates: list[int] = [max(len(semantic_message) // 4, 0)]

    top_level = _token_estimate(metadata.get("material_estimated_tokens"))
    if top_level is not None:
        candidates.append(top_level)

    normalization = metadata.get("input_normalization")
    if isinstance(normalization, dict):
        nested = _token_estimate(normalization.get("material_estimated_tokens"))
        if nested is not None:
            candidates.append(nested)

    return max(candidates)


def _context_window_tokens(ctx: TurnContext, router_cfg: object) -> int:
    for candidate in (
        getattr(router_cfg, "context_window_tokens", None),
        getattr(getattr(ctx, "config", None), "context_window_tokens", None),
        getattr(getattr(getattr(ctx, "config", None), "llm", None), "context_window_tokens", None),
    ):
        tokens = _token_estimate(candidate)
        if tokens and tokens > 0:
            return tokens
    return DEFAULT_CONTEXT_WINDOW_TOKENS


def _tier_capability_facts(
    tiers: dict,
    valid_tiers: list[str],
    active_provider: str,
) -> dict[str, TierCapability]:
    """Definite catalog facts per text tier for the capability gate.

    Context gathering stays in the step (the policy engine consumes plain
    data only). Every field is emitted as ``None`` unless the shared model
    catalog gives a definite signal, so the gate never acts on ignorance:

    - ``supports_vision`` is known only when the resolved entry is NOT
      synthesized and ``get_capabilities`` returned something other than
      the empty :class:`ModelCapabilities` — an empty result covers both
      "no layer knew any capability" and the anthropic/ollama flag-gated
      early return, none of which is a definite non-vision signal.
    - ``context_window`` is known only when
      ``resolve_context_window_with_source`` attributes the value to the
      catalog (live/snapshot/corrections) or to a per-model ``[models.*]``
      override (operator-declared knowledge); engine defaults are
      estimates, not knowledge.
    """
    catalog = shared_catalog()
    empty_capabilities = ModelCapabilities()
    facts: dict[str, TierCapability] = {}
    for name in valid_tiers:
        tier = TierConfig.from_value(tiers.get(name))
        if not tier.model:
            facts[name] = TierCapability()
            continue
        provider = (tier.provider or active_provider or "").strip().lower()
        supports_vision: bool | None = None
        entry = catalog.resolve_entry(tier.model, provider=provider)
        if entry.source != "synthesized":
            capabilities = catalog.get_capabilities(tier.model, provider_name=provider)
            if capabilities != empty_capabilities:
                supports_vision = capabilities.supports_vision
        window, window_source = catalog.resolve_context_window_with_source(tier.model, provider)
        # Operator-declared [models.*] windows count as definite facts for the
        # capability gate, same as catalog knowledge; only engine defaults
        # remain estimates.
        context_window = window if window_source in ("catalog", "override") and window > 0 else None
        facts[name] = TierCapability(
            supports_vision=supports_vision,
            context_window=context_window,
        )
    return facts


# Session-spend keys the runtime seeds into router metadata when the budget
# gate is active (see runtime._run_pipeline). Absent on the default path, which
# keeps the gate suspended (never acting on an unknown cost).
_BUDGET_SPEND_KEYS = (
    "session_billed_cost_usd",
    "session_total_cost_usd",
    "session_estimated_cost_usd",
)


def _budget_cost_value(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    as_float = float(value)
    if as_float != as_float or as_float in (float("inf"), float("-inf")):
        return None
    return as_float


def _session_accumulated_spend(ctx: TurnContext) -> tuple[float | None, str]:
    """Read the session's accumulated spend from turn metadata.

    Precedence mirrors ``session/cost_rollup.rollup_cost_source`` (READ only —
    that module is off-limits): provider-billed total first, then the
    estimate/total. Returns ``(None, "unknown")`` when NO cost signal is
    present at all — a fresh session with no billed total yet — so the gate
    suspends rather than acting on missing data. Keys present but all zero are
    a known-zero spend, not an unknown one.

    The returned source records the spend's cost basis. A positive billed total
    is ``"billed"``. Estimate-based spend is refined with the session's seeded
    rollup label (``session_cost_source``): a ``"mixed"`` rollup — billed and
    estimated components together — becomes ``"estimate_mixed"``, while a pure
    ``"opensquilla_estimate"``, an absent label, or anything else collapses to
    the plain ``"estimate"`` the gate already understood. ``"none"`` (known-zero
    spend) and ``"unknown"`` (no signal) semantics are unchanged.
    """
    metadata = getattr(ctx, "metadata", {}) or {}
    if not any(key in metadata for key in _BUDGET_SPEND_KEYS):
        return None, "unknown"
    billed = _budget_cost_value(metadata.get("session_billed_cost_usd"))
    if billed is not None and billed > 0:
        return billed, "billed"
    label = str(metadata.get("session_cost_source", "") or "").strip().lower()
    estimate_source = "estimate_mixed" if label == "mixed" else "estimate"
    total = _budget_cost_value(metadata.get("session_total_cost_usd"))
    if total is not None and total > 0:
        return total, estimate_source
    estimated = _budget_cost_value(metadata.get("session_estimated_cost_usd"))
    if estimated is not None and estimated > 0:
        return estimated, estimate_source
    return 0.0, "none"


def _budget_next_turn_estimate(routed_model: str, material_tokens: int) -> float | None:
    """Rough forward estimate (USD) of this turn's marginal input cost.

    READS ``engine/pricing`` prices (offline-safe static fallback); never
    writes new cost math into the savings path. Returns ``None`` when the model
    or token count is unknown so the gate suspends instead of guessing; a
    free/local model is a known-zero marginal cost.
    """
    model = str(routed_model or "").strip()
    if not model or material_tokens <= 0:
        return None
    input_per_m = float(getattr(lookup_price(model), "input_per_m", 0.0) or 0.0)
    if input_per_m <= 0:
        return 0.0
    return (material_tokens / 1_000_000.0) * input_per_m


def _budget_gate_input_for_turn(
    ctx: TurnContext,
    router_cfg: object,
    *,
    routed_model: str,
    material_tokens: int,
) -> BudgetGateInput | None:
    """Assemble the budget-gate signal, or ``None`` for the default no-op path.

    Mirrors :func:`_calibration_for_turn`: ``None`` is returned whenever the
    gate is disabled (no ``budget`` block, ``action = "off"``, or no positive
    ``limit_usd``), so the only time a non-``None`` input reaches the policy
    engine is when the operator has set an active per-session ceiling. Degrades
    to a suspended input (``spend_usd=None``) when the accumulated spend — or a
    requested forward price — cannot be determined.
    """
    budget_cfg = getattr(router_cfg, "budget", None)
    if budget_cfg is None:
        return None
    action = str(getattr(budget_cfg, "action", "warn") or "warn").strip().lower()
    if action not in ("warn", "cap"):
        return None
    limit = _budget_cost_value(getattr(budget_cfg, "limit_usd", None))
    if limit is None or limit <= 0:
        return None

    cap_tier = getattr(budget_cfg, "cap_tier", None)
    resolved_cap = normalize_text_tier(cap_tier) if cap_tier else None
    if action == "cap" and resolved_cap is None:
        resolved_cap = normalize_text_tier(getattr(router_cfg, "default_tier", None))

    spend, source = _session_accumulated_spend(ctx)
    estimate: float | None = None
    if spend is not None and bool(getattr(budget_cfg, "include_next_turn_estimate", False)):
        estimate = _budget_next_turn_estimate(routed_model, material_tokens)
        if estimate is None:
            # Forward projection requested but the next turn cannot be priced:
            # never act on an unknown cost -> suspend.
            spend = None
            source = "unknown"

    return BudgetGateInput(
        action=action,
        limit_usd=limit,
        spend_usd=spend,
        estimate_usd=estimate,
        cap_tier=resolved_cap,
        spend_source=source,
        session_key=ctx.session_key,
    )


def _log_budget_outcome(ctx: TurnContext) -> None:
    """Emit the ``router_budget.*`` event when the gate acted (warn/cap).

    No secrets and no prompt text — only the session key, spend, limit, and
    action. Suspensions are logged separately at debug from the gather site,
    where the suspended signal is known.
    """
    outcome = ctx.metadata.get("router_budget_outcome")
    if outcome == "warn":
        log.warning(
            "router_budget.warn",
            session=ctx.session_key,
            spend_usd=ctx.metadata.get("router_budget_spend_usd"),
            limit_usd=ctx.metadata.get("router_budget_limit_usd"),
            action=ctx.metadata.get("router_budget_action"),
            spend_source=ctx.metadata.get("router_budget_spend_source"),
        )
    elif outcome == "cap":
        log.warning(
            "router_budget.cap",
            session=ctx.session_key,
            spend_usd=ctx.metadata.get("router_budget_spend_usd"),
            limit_usd=ctx.metadata.get("router_budget_limit_usd"),
            action=ctx.metadata.get("router_budget_action"),
            spend_source=ctx.metadata.get("router_budget_spend_source"),
            from_tier=ctx.metadata.get("router_budget_from_tier"),
            to_tier=ctx.metadata.get("router_budget_to_tier"),
        )


def _flag_tier_provider_mismatch(
    ctx: TurnContext,
    tiers: dict,
    tier_name: str,
    *,
    routing_applied: bool,
) -> None:
    """Record the routed tier's provider; warn on unexecutable mismatches.

    Thin adapter over the flag-only ``provider_mismatch`` policy stage: it
    gathers the active provider + cross-provider flag from the turn config,
    applies the outcome to turn metadata, and emits the operator logs. With
    ``squilla_router.tier_provider_mismatch = "route"`` (the default) the
    silent misroute stays intentional telemetry; ``"veto"`` is handled
    upstream by :func:`_apply_provider_mismatch_veto`, after which this
    adapter re-assesses (and normally records a clean match for) the
    rebound tier.
    """
    outcome = provider_mismatch(
        tiers=tiers,
        tier_name=tier_name,
        routing_applied=routing_applied,
        active_provider=str(getattr(getattr(ctx.config, "llm", None), "provider", "") or ""),
        cross_provider_tiers=bool(
            getattr(getattr(ctx.config, "squilla_router", None), "cross_provider_tiers", False)
        ),
    )
    if outcome.routed_provider:
        ctx.metadata["routed_provider"] = outcome.routed_provider
    if outcome.outcome == "cross_provider":
        log.info(
            "squilla_router.cross_provider_tier_routed",
            tier=tier_name,
            tier_provider=outcome.tier_provider,
            active_provider=outcome.active_provider,
            model=outcome.tier_model,
            session=ctx.session_key,
        )
    elif outcome.outcome == "mismatch":
        ctx.metadata["router_tier_provider_mismatch"] = outcome.tier_provider
        log.warning(
            "squilla_router.tier_provider_mismatch",
            tier=tier_name,
            tier_provider=outcome.tier_provider,
            active_provider=outcome.active_provider,
            model=outcome.tier_model,
            session=ctx.session_key,
        )


def _apply_provider_mismatch_veto(
    ctx: TurnContext,
    router_cfg: object,
    tiers: dict,
    valid_tiers: list[str],
    decision: RoutingDecision,
    thinking_mode: str | None,
    prompt_policy: str | None,
    *,
    routing_applied: bool,
) -> tuple[RoutingDecision, str | None, str | None]:
    """Rebind a mismatched classify-path decision when veto mode is on.

    A strict no-op unless ``squilla_router.tier_provider_mismatch`` is
    ``"veto"`` AND routing applies AND the ``provider_mismatch_veto`` stage
    finds a usable rebind target; every other combination (the default
    ``"route"`` mode above all) leaves the decision byte-identical to the
    historical flag-and-misroute behavior. A vetoed decision with no usable
    rebind target instead fails closed at the selector-apply boundary,
    which keeps the primary deployment (provider and model). The
    router-control hold path is intentionally exempt: an operator hold pins
    an explicit tier/model/provider triple and must not be second-guessed.
    """
    mode = str(getattr(router_cfg, "tier_provider_mismatch", "route") or "route").strip().lower()
    if mode != "veto" or not routing_applied:
        return decision, thinking_mode, prompt_policy
    veto = provider_mismatch_veto(
        tiers=tiers,
        tier_name=decision.tier,
        valid_tiers=valid_tiers,
        routing_applied=routing_applied,
        active_provider=str(getattr(getattr(ctx.config, "llm", None), "provider", "") or ""),
        cross_provider_tiers=bool(getattr(router_cfg, "cross_provider_tiers", False)),
        default_tier=getattr(router_cfg, "default_tier", None),
    )
    if not veto.applied:
        return decision, thinking_mode, prompt_policy

    rebound = RoutingDecision(
        tier=veto.to_tier,
        model=tiers[veto.to_tier].get("model", decision.model),
        confidence=decision.confidence,
        source=decision.source,
    )
    ctx.metadata["provider_mismatch_veto_applied"] = True
    ctx.metadata["provider_mismatch_veto_from_tier"] = veto.from_tier
    ctx.metadata["provider_mismatch_veto_to_tier"] = veto.to_tier
    extra = ctx.metadata.get("routing_extra")
    if isinstance(extra, dict):
        record_provider_mismatch_veto_trail(extra, veto)
        extra["final_tier"] = veto.to_tier
        extra["final_route_class"] = route_class_for_tier(veto.to_tier)
        thinking_mode, prompt_policy = reconcile_controller_with_final_tier(
            thinking_mode,
            prompt_policy,
            extra,
        )
    log.warning(
        "squilla_router.tier_provider_mismatch_vetoed",
        from_tier=veto.from_tier,
        to_tier=veto.to_tier,
        model=rebound.model,
        session=ctx.session_key,
    )
    return rebound, thinking_mode, prompt_policy


def _apply_controller(
    ctx: TurnContext,
    router_cfg: object,
    tier_cfg: dict,
    thinking_mode: str | None,
    prompt_policy: str | None,
    prompt_hint: str | None,
    rollout_phase: str,
) -> None:
    """Apply controller decisions based on rollout phase."""
    ctx.metadata["thinking_mode"] = thinking_mode
    ctx.metadata["prompt_policy"] = prompt_policy

    if rollout_phase == "observe":
        _record_controller_thinking_metadata(ctx, router_cfg, tier_cfg, thinking_mode)
        return

    # prompt_only or full: inject prompt hint. P2 is tracked for observability
    # and thinking control, but intentionally not injected into the user text.
    if prompt_policy == "P2":
        hint = None
    else:
        hint = prompt_hint or get_prompt_hint(prompt_policy, ctx.message)
    if hint:
        ctx.message = _inject_prompt_hint(ctx.message, hint)

    if rollout_phase == "full" and getattr(router_cfg, "auto_thinking", True):
        _record_controller_thinking_metadata(ctx, router_cfg, tier_cfg, thinking_mode)
    else:
        _record_thinking_metadata(ctx, router_cfg, tier_cfg)


def _attachments_include_image(attachments: list[dict[str, Any]] | None) -> bool:
    if not attachments:
        return False
    for att in attachments:
        for key in ("type", "mime", "media_type", "mime_type"):
            media_type = att.get(key)
            if isinstance(media_type, str) and media_type.startswith("image/"):
                return True
    return False


async def apply_squilla_router(ctx: TurnContext) -> TurnContext:
    router_cfg = getattr(ctx.config, "squilla_router", None) if ctx.config else None
    if not router_cfg or not getattr(router_cfg, "enabled", False):
        return ctx

    tiers = getattr(router_cfg, "tiers", {})
    if not tiers:
        return ctx

    semantic_message = getattr(ctx, "semantic_message", None)
    if semantic_message is None:
        semantic_message = getattr(ctx, "raw_message", None)
    if semantic_message is None:
        semantic_message = ctx.message
    if ":subagent:" in ctx.session_key:
        return ctx

    rollout_phase: str = getattr(router_cfg, "rollout_phase", "observe")

    # Image-aware routing: skip ML and pick directly from supports_image tiers
    # for current uploads. Historical images require the upstream semantic
    # follow-up gate; recent-image/sticky metadata alone is observability and
    # replay context, not enough to force vision.
    #
    # This runs BEFORE the empty-text guard below: the image route is
    # deterministic and never consumes the message text, so an image turn with
    # an empty/whitespace caption must still be routed to a vision tier rather
    # than falling through the empty-text early return.
    current_turn_has_image = _attachments_include_image(ctx.attachments)
    history_gate_needs_image = (
        ctx.metadata.get("router_vision_followup_needs_image") is True
    )
    # Computed once and reused below by both the bypass and the policy
    # engine's capability gate (which must not recompute the signal). On the
    # classify path this is always False today — the bypass routes or raises
    # for every image turn — which is exactly the gate's no-op default.
    turn_needs_image = current_turn_has_image or history_gate_needs_image
    if turn_needs_image:
        image_tiers = {k: v for k, v in tiers.items() if v.get("supports_image", False)}
        if not image_tiers:
            log.warning(
                "squilla_router.no_image_tier",
                note="image detected but no supports_image tier",
            )
            raise RuntimeError(
                "No image-capable SquillaRouter tier is configured for this image request. "
                "Configure squilla_router.tiers.image_model with supports_image=true."
            )
        tier_name = next(iter(image_tiers))
        decision = RoutingDecision(
            tier=tier_name,
            model=image_tiers[tier_name].get("model", ctx.model),
            confidence=1.0,
            source="image_route",
        )
        # Vision turns are not just a text-tier routing decision: they require a
        # model that can consume image blocks. Apply this route even during
        # observe rollout so multimodal requests do not remain on a text tier.
        routing_applied = True
        ctx.metadata["baseline_model"] = ctx.model
        if routing_applied:
            ctx.model = decision.model
        ctx.metadata["routed_tier"] = decision.tier
        ctx.metadata["routed_model"] = decision.model
        ctx.metadata["routing_applied"] = routing_applied
        ctx.metadata["rollout_phase"] = rollout_phase
        ctx.metadata["applied_model"] = ctx.model
        ctx.metadata["routing_confidence"] = decision.confidence
        ctx.metadata["routing_source"] = decision.source
        image_route_reason = "current_turn" if current_turn_has_image else "gate_history"
        ctx.metadata["image_route_reason"] = image_route_reason
        history_turns = 1
        if image_route_reason == "gate_history":
            history_turns = max(
                1,
                int(getattr(router_cfg, "vision_history_lookback_turns", 8) or 1),
            )
        ctx.metadata["route_max_history_turns"] = history_turns
        ctx.metadata.update(_compute_savings(decision.model, tiers))
        # Record the image tier's provider (and assess cross-provider/mismatch)
        # like the hold and classify paths — without this, a vision tier that
        # declares provider=X never executes the cross-provider switch and no
        # mismatch telemetry is emitted.
        _flag_tier_provider_mismatch(ctx, tiers, decision.tier, routing_applied=True)
        _record_thinking_metadata(ctx, router_cfg, image_tiers[tier_name])
        stage_router_decision(ctx, decision=decision)
        log.debug("squilla_router.image_routed", tier=decision.tier, model=decision.model)
        return ctx

    # Empty-text guard for the ML text classifier: only reached for non-image
    # turns (the vision bypass above already handled empty-caption images).
    if not semantic_message.strip():
        return ctx

    # Order valid_tiers by the canonical c0<c1<c2<c3 ladder rather than TOML
    # insertion order — downstream policy stages rank tiers by position in this
    # list, so trusting declaration order inverted upgrades/holds for configs
    # that list tiers out of order. Unknown/custom tier names (tier_index == -1)
    # sort after the canonical ones, preserving their relative order (stable).
    valid_tiers = [name for name, tier in tiers.items() if not tier.get("image_only", False)]
    valid_tiers = sorted(
        valid_tiers,
        key=lambda name: (0, tier_index(name)) if tier_index(name) >= 0 else (1, 0),
    )
    if not valid_tiers:
        return ctx

    hold_store = ctx.metadata.get("router_control_hold_store")
    if isinstance(hold_store, RouterControlHoldStore):
        hold = hold_store.get_valid(ctx.session_key, decrement=True)
        if hold is not None and hold.tier in tiers and hold.tier in valid_tiers:
            decision = RoutingDecision(
                tier=hold.tier,
                model=hold.model,
                confidence=1.0,
                source="router_control_hold",
            )
            ctx.metadata["baseline_model"] = ctx.model
            ctx.model = decision.model
            ctx.metadata["routed_tier"] = decision.tier
            ctx.metadata["routed_model"] = decision.model
            ctx.metadata["routing_applied"] = True
            ctx.metadata["applied_model"] = ctx.model
            ctx.metadata["routing_confidence"] = decision.confidence
            ctx.metadata["routing_source"] = decision.source
            ctx.metadata["router_fallback_chain"] = _router_text_fallback_chain(
                decision.tier,
                tiers,
            )
            ctx.metadata["router_control_hold_applied"] = True
            ctx.metadata["router_control_action"] = "set_hold"
            ctx.metadata["router_control_target_tier"] = hold.tier
            ctx.metadata["router_control_target_model"] = hold.model
            ctx.metadata["router_control_target_provider"] = hold.provider
            ctx.metadata["router_control_evidence"] = hold.evidence
            ctx.metadata.update(_compute_savings(decision.model, tiers))
            _flag_tier_provider_mismatch(ctx, tiers, decision.tier, routing_applied=True)
            _record_thinking_metadata(ctx, router_cfg, tiers[decision.tier])
            stage_router_decision(ctx, decision=decision)
            log.debug(
                "squilla_router.router_control_hold_applied",
                tier=decision.tier,
                model=decision.model,
                session=ctx.session_key,
            )
            return ctx

    strategy = _get_strategy(router_cfg)
    strategy_name = _strategy_name(router_cfg)
    defer_history = bool(ctx.metadata.get(_DEFER_ROUTING_HISTORY_KEY))

    # History-aware routers load accumulated routing history for this session.
    routing_history = None
    if _is_history_strategy(strategy_name):
        stored_history = _history_store.get(ctx.session_key)
        routing_history = [dict(entry) for entry in stored_history or []] or None
        if not routing_history:
            persisted = ctx.metadata.get("routing_history")
            if persisted:
                now = time.monotonic()
                routing_history = [
                    {**dict(entry), "_ts": now} if "_ts" not in entry else dict(entry)
                    for entry in persisted
                    if isinstance(entry, dict)
                ]
                if not defer_history:
                    _history_store.set(ctx.session_key, routing_history)
                    log.debug(
                        "squilla_router.history_cold_start",
                        session=ctx.session_key,
                        restored=len(routing_history),
                    )
        if routing_history:
            cutoff = time.monotonic() - _ROUTING_HISTORY_WINDOW
            routing_history = [e for e in routing_history if e.get("_ts", 0) > cutoff]
            routing_history = routing_history[-_MAX_ROUTING_HISTORY:]
            if not defer_history:
                _history_store.set(ctx.session_key, routing_history)
        log.debug(
            "squilla_router.history_loaded",
            session=ctx.session_key,
            history_len=len(routing_history) if routing_history else 0,
        )

    # --- Classification ---
    thinking_mode: str | None = None
    prompt_policy: str | None = None
    extra: dict | None = None
    probs: list[float] | None = None

    classify_context = _classify_context_kwargs(
        strategy,
        {
            "prev_assistant_text": ctx.metadata.get("router_prev_assistant_text"),
            "prev_assistant_usage": ctx.metadata.get("router_prev_assistant_usage"),
            "history_user_texts": ctx.metadata.get("router_history_user_texts"),
            "flags_text_override": ctx.metadata.get("router_flags_text_override"),
            # Non-image attachments only: image turns were routed to a vision
            # tier before classification. Signature-filtered like the other
            # context keys, so strategies that don't declare it never see it.
            "attachment_count": len(ctx.attachments or []),
        },
    )
    tier_name, confidence, source, extra = await strategy.classify(
        semantic_message,
        valid_tiers,
        routing_history=routing_history,
        **classify_context,
    )
    tier_name = normalize_text_tier(tier_name) or tier_name
    if extra:
        ctx.metadata["routing_extra"] = extra
        thinking_mode = extra.get("thinking_mode")
        prompt_policy = extra.get("prompt_policy")
        # Move the (large) self-learning feature vectors out of routing_extra so
        # they never reach decision logs or accumulated routing history.
        train_features = extra.pop("_train_features", None)
        if train_features is not None:
            ctx.metadata["routing_train_features"] = train_features
            ctx.metadata["routing_train_turn_index"] = len(routing_history or [])

    if tier_name is None or tier_name not in tiers:
        default = normalize_text_tier(getattr(router_cfg, "default_tier", DEFAULT_TEXT_TIER))
        if default is None:
            default = DEFAULT_TEXT_TIER
        tier_name = default if default in tiers else next(iter(tiers), None)
        if tier_name is None:
            return ctx
        confidence = 0.0
        source = "default"
        probs = synthetic_one_hot(tier_name)

    decision = RoutingDecision(
        tier=tier_name,
        model=tiers[tier_name].get("model", ctx.model),
        confidence=confidence,
        source=source,
    )

    ctx.metadata["baseline_model"] = ctx.model

    # --- Controller: derive thinking_mode / prompt_policy if v4 returned no head decisions ---
    if thinking_mode is None and probs is not None:
        try:
            flags = extra.get("flags") if extra else None
            thinking_mode = derive_thinking_mode(probs, flags)
            prompt_policy = derive_prompt_policy(probs, flags)
            thinking_mode, prompt_policy = normalize_decisions(thinking_mode, prompt_policy)
            if decision.source in {"v4_unavailable", "default"} and prompt_policy == "P0":
                prompt_policy = "P1"
        except Exception:
            log.warning("squilla_router.controller_error", exc_info=True)
            thinking_mode = None
            prompt_policy = None

    # --- Apply decisions: post-classifier policy stages -----------------------
    # The policy engine consumes plain data only; context gathering stays here.
    if _is_history_strategy(strategy_name):
        routing_extra = ctx.metadata.setdefault("routing_extra", extra or {})
    else:
        routing_extra = ctx.metadata.get("routing_extra")
    material_estimated_tokens = _material_estimated_tokens(ctx, semantic_message)
    budget_input = _budget_gate_input_for_turn(
        ctx,
        router_cfg,
        routed_model=decision.model,
        material_tokens=material_estimated_tokens,
    )
    if budget_input is not None and budget_input.spend_usd is None:
        # Active gate but the accumulated spend (or a required forward price)
        # could not be determined: suspend rather than act on missing data.
        log.debug(
            "router_budget.suspended",
            session=ctx.session_key,
            limit_usd=budget_input.limit_usd,
            spend_source=budget_input.spend_source,
        )
    policy_result = _POLICY_ENGINE.run(
        PolicyInputs(
            decision=decision,
            message=semantic_message,
            router_cfg=router_cfg,
            tiers=tiers,
            valid_tiers=valid_tiers,
            routing_history=routing_history,
            extra=routing_extra if isinstance(routing_extra, dict) else None,
            thinking_mode=thinking_mode,
            prompt_policy=prompt_policy,
            history_strategy=_is_history_strategy(strategy_name),
            material_estimated_tokens=material_estimated_tokens,
            context_window_tokens=_context_window_tokens(ctx, router_cfg),
            turn_has_image=turn_needs_image,
            tier_capabilities=_tier_capability_facts(
                tiers,
                valid_tiers,
                str(getattr(getattr(ctx.config, "llm", None), "provider", "") or ""),
            ),
            calibration=_calibration_for_turn(router_cfg),
            budget=budget_input,
        )
    )
    decision = policy_result.decision
    thinking_mode = policy_result.thinking_mode
    prompt_policy = policy_result.prompt_policy
    ctx.metadata.update(policy_result.metadata_updates)
    _log_budget_outcome(ctx)

    routing_applied = rollout_phase != "observe"
    decision, thinking_mode, prompt_policy = _apply_provider_mismatch_veto(
        ctx,
        router_cfg,
        tiers,
        valid_tiers,
        decision,
        thinking_mode,
        prompt_policy,
        routing_applied=routing_applied,
    )
    if routing_applied:
        ctx.model = decision.model
    ctx.metadata["routed_tier"] = decision.tier
    ctx.metadata["routed_model"] = decision.model
    ctx.metadata["routing_applied"] = routing_applied
    ctx.metadata["rollout_phase"] = rollout_phase
    ctx.metadata["applied_model"] = ctx.model
    ctx.metadata["routing_confidence"] = decision.confidence
    ctx.metadata["routing_source"] = decision.source
    ctx.metadata["router_fallback_chain"] = _router_text_fallback_chain(
        decision.tier,
        tiers,
    )
    ctx.metadata.update(_compute_savings(decision.model, tiers))
    _flag_tier_provider_mismatch(ctx, tiers, decision.tier, routing_applied=routing_applied)

    context_states = ctx.metadata.get("session_context_states") or ctx.metadata.get(
        "active_context_states"
    )
    if isinstance(context_states, list):
        tier_cfg = tiers[decision.tier]
        candidate_provider = str(
            tier_cfg.get("provider") or getattr(router_cfg, "tier_profile", "") or ""
        )
        ctx.metadata["provider_state_continuity"] = provider_state_continuity_diagnostic(
            context_states=context_states,
            candidate_provider=candidate_provider,
            candidate_model=decision.model,
            now_ms=int(time.time() * 1000),
        ).as_metadata()

    try:
        _apply_controller(
            ctx,
            router_cfg,
            tiers[decision.tier],
            thinking_mode,
            prompt_policy,
            prompt_hint=(ctx.metadata.get("routing_extra") or {}).get("prompt_hint"),
            rollout_phase=rollout_phase,
        )
    except Exception:
        log.warning("squilla_router.controller_apply_error", exc_info=True)
        _record_thinking_metadata(ctx, router_cfg, tiers[decision.tier])

    # History-aware routers accumulate routing_extra into per-session history.
    if _is_history_strategy(strategy_name):
        extra = ctx.metadata.get("routing_extra")
        if extra:
            entry_payload = _routing_history_entry(
                text=semantic_message,
                extra=extra,
                decision=decision,
            )
            if defer_history:
                ctx.metadata[_PENDING_ROUTING_HISTORY_ENTRY_KEY] = entry_payload
                ctx.metadata[_PENDING_ROUTING_HISTORY_SESSION_KEY] = ctx.session_key
                local_history = list(routing_history or [])
                local_entry = {
                    "turn_index": len(local_history),
                    "_ts": time.monotonic(),
                    **entry_payload,
                }
                ctx.metadata["routing_history"] = [*local_history, local_entry][
                    -_MAX_ROUTING_HISTORY:
                ]
            else:
                ctx.metadata["routing_history"] = _append_routing_history(
                    ctx.session_key,
                    entry_payload,
                )

    # Pull observability fields from routing_extra so operators can see
    # what the model raw-believed (probabilities) vs what was selected
    # (route_class). With probabilities present the difference between
    # "model strongly chose this class" and "post-processing forced it"
    # is visible in the log without re-running the router.
    routing_extra = ctx.metadata.get("routing_extra") or {}
    stage_router_decision(ctx, decision=decision, routing_extra=routing_extra)
    log.debug(
        "squilla_router.routed",
        tier=decision.tier,
        routed_model=decision.model,
        applied_model=ctx.model,
        routing_applied=routing_applied,
        confidence=decision.confidence,
        source=decision.source,
        thinking_mode=thinking_mode,
        prompt_policy=prompt_policy,
        thinking_level=ctx.metadata.get("thinking_level"),
        rollout_phase=rollout_phase,
        route_class=routing_extra.get("route_class"),
        base_tier=routing_extra.get("base_tier"),
        pre_confidence_tier=routing_extra.get("pre_confidence_tier"),
        final_tier=routing_extra.get("final_tier"),
        final_route_class=routing_extra.get("final_route_class"),
        confidence_threshold=routing_extra.get("confidence_threshold"),
        confidence_gate_applied=routing_extra.get("confidence_gate_applied"),
        anti_downgrade_applied=routing_extra.get("anti_downgrade_applied"),
        probabilities=routing_extra.get("probabilities"),
        margin=routing_extra.get("margin"),
        provider_state_continuity=ctx.metadata.get("provider_state_continuity"),
    )
    return ctx
