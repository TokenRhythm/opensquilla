from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from types import SimpleNamespace

import numpy as np

from src.router.features import ContextMetadata
from src.router.flags import compute_flags
from src.router.inference.types import FinalDecision, InferenceRequest
from src.router.predictor import (
    ROUTE_CLASSES,
    _apply_flag_overrides,
    _apply_margin_upgrade,
    _apply_r1_rescue,
    _apply_sticky_tier,
    _derive_prompt_policy,
    _derive_thinking_mode,
    _select_model,
)

_CLASS_TO_IDX = {route_class: idx for idx, route_class in enumerate(ROUTE_CLASSES)}
_TRIVIAL_ACK_TEXTS = frozenset(
    {
        "thanks",
        "thank you",
        "ok",
        "okay",
        "yes",
        "no",
        "收到",
        "好的",
        "谢谢",
        "是的",
        "不用了",
    }
)


def _apply_aux_downgrade(
    route_class: str,
    aux_probs: Mapping[str, float] | None,
    config: dict,
) -> tuple[str, bool]:
    aux_cfg = config.get("v4", {}).get("aux_downgrade", {})
    if not aux_cfg.get("enabled", False) or not aux_probs:
        return route_class, False

    downgrade_threshold = float(aux_cfg.get("threshold", 0.55))
    non_initial_mass = (
        float(aux_probs.get("maintain", 0.0))
        + float(aux_probs.get("upgrade", 0.0))
        + float(aux_probs.get("downgrade", 0.0))
    )
    downgrade_prob = (
        float(aux_probs.get("downgrade", 0.0)) / non_initial_mass
        if non_initial_mass > 0
        else 0.0
    )
    if downgrade_prob < downgrade_threshold or route_class == "R0":
        return route_class, False

    downgraded = ROUTE_CLASSES[max(0, _CLASS_TO_IDX[route_class] - 1)]
    return downgraded, downgraded != route_class


def _apply_optional_sticky_tier(
    route_class: str,
    fused_probs: np.ndarray,
    request: InferenceRequest,
    config: dict,
) -> tuple[str, bool]:
    sticky_cfg = config.get("v4", {}).get("sticky_tier", {})
    if not sticky_cfg.get("enabled", False):
        return route_class, False

    max_user_len = sticky_cfg.get("max_user_len")
    if max_user_len is not None and len(request.current_user_text) > int(max_user_len):
        return route_class, False

    sticky_config = {
        **config,
        "thresholds": {
            **config.get("thresholds", {}),
            "kv_cache_aware": True,
        },
    }

    sticky_route = _apply_sticky_tier(
        route_class,
        fused_probs,
        _normalize_history(request.prev_route_decisions),
        sticky_config,
    )
    return sticky_route, sticky_route != route_class


def _context_from_request(request: InferenceRequest) -> ContextMetadata | None:
    metadata = request.context_metadata or {}
    if not metadata:
        return None
    allowed = ContextMetadata.__dataclass_fields__.keys()
    return ContextMetadata(**{key: value for key, value in metadata.items() if key in allowed})


def _apply_under_routing_safety(
    route_class: str,
    fused_probs: np.ndarray,
    config: dict,
) -> str:
    if _CLASS_TO_IDX[route_class] >= _CLASS_TO_IDX["R2"]:
        return route_class
    safety_threshold = config.get("thresholds", {}).get("under_routing_safety", 0.45)
    heavy_prob = float(fused_probs[2] + fused_probs[3])
    if heavy_prob > safety_threshold:
        return "R2"
    return route_class


def _apply_context_rules(
    route_class: str,
    context: ContextMetadata | None,
    config: dict,
) -> str:
    if context is None:
        return route_class
    ctx_rules = config.get("context_rules", {})
    deep_threshold = int(ctx_rules.get("deep_conversation_threshold", 4))
    if context.turn_index >= deep_threshold:
        deep_min = ctx_rules.get("deep_conversation_min_class", "R1")
        idx = max(_CLASS_TO_IDX[route_class], _CLASS_TO_IDX.get(deep_min, _CLASS_TO_IDX["R1"]))
        return ROUTE_CLASSES[idx]
    return route_class


def _is_trivial_ack(text: str) -> bool:
    normalized = text.strip().lower()
    normalized = normalized.strip(" \t\r\n.!?。！？,，;；:：")
    return normalized in _TRIVIAL_ACK_TEXTS


def _normalize_history(prev_route_decisions: list) -> list:
    normalized = []
    for item in prev_route_decisions or []:
        if hasattr(item, "route_class"):
            normalized.append(item)
        elif isinstance(item, dict) and "route_class" in item:
            normalized.append(SimpleNamespace(route_class=item["route_class"]))
        else:
            raise ValueError(
                "prev_route_decisions entries must expose route_class"
            )
    return normalized


def apply_postprocess(
    fused_probs: np.ndarray,
    aux_probs: Mapping[str, float] | None,
    request: InferenceRequest,
    config: dict,
) -> FinalDecision:
    fused_probs = np.asarray(fused_probs, dtype=np.float64)
    if fused_probs.shape != (4,):
        raise ValueError("postprocess expects a 4-class probability vector")

    idx = int(np.argmax(fused_probs))
    route = ROUTE_CLASSES[idx]
    sorted_p = np.sort(fused_probs)[::-1]
    margin = float(sorted_p[0] - sorted_p[1])
    difficulty = float(np.dot(fused_probs, np.arange(4, dtype=np.float64)))

    pre_upgrade_route = route
    route = _apply_margin_upgrade(route, margin, config)
    margin_upgraded = route != pre_upgrade_route
    if margin_upgraded:
        aux_downgrade_applied = False
    else:
        route, aux_downgrade_applied = _apply_aux_downgrade(route, aux_probs, config)
    route = _apply_r1_rescue(route, fused_probs, config)
    route = _apply_under_routing_safety(route, fused_probs, config)

    flags_text = (
        request.flags_text_override
        if request.flags_text_override is not None
        else request.current_user_text
    )
    context = _context_from_request(request)
    flags = compute_flags(flags_text, config, context=context)
    route = _apply_flag_overrides(route, flags, config)
    route = _apply_context_rules(route, context, config)
    route, sticky_applied = _apply_optional_sticky_tier(
        route, fused_probs, request, config
    )

    thinking_mode = _derive_thinking_mode(route, margin, flags, config)
    prompt_policy = _derive_prompt_policy(difficulty, margin, flags, config)
    if route == "R0" and _is_trivial_ack(flags_text):
        thinking_mode = "T0"
        prompt_policy = "P0"
    _, selected_model = _select_model(route, config)

    return FinalDecision(
        route_class=route,
        margin=margin,
        difficulty_score=difficulty,
        flags=asdict(flags),
        thinking_mode=thinking_mode,
        prompt_policy=prompt_policy,
        selected_model=selected_model,
        aux_downgrade_applied=aux_downgrade_applied,
        sticky_applied=sticky_applied,
    )
