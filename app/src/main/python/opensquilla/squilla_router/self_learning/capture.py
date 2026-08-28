"""Assemble a :class:`RouterTrainSample` from completed-turn metadata.

Kept as a pure function (no IO) so it is unit-testable without the runtime.
Returns ``None`` when there is nothing trainable to capture (no features were
surfaced, e.g. the router fell back to the unavailable strategy, or the turn
bypassed ML classification via image/manual-hold routing).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from opensquilla.router_tiers import ROUTE_CLASS_TO_TIER, normalize_text_tier
from opensquilla.squilla_router.self_learning.schema import RouterTrainSample, encode_features

_TIER_TO_ROUTE = {v: k for k, v in ROUTE_CLASS_TO_TIER.items()}

# Routing sources that bypass the text ML classifier; offline training drops
# these, so there is no point capturing them.
_BYPASS_SOURCES = {"image_route", "hold", "manual_hold"}


def _route_class_for_tier(tier: str | None) -> str:
    norm = normalize_text_tier(tier) if tier else None
    return _TIER_TO_ROUTE.get(norm or "", "R1")


def build_train_sample(
    *,
    session_key: str,
    metadata: dict[str, Any],
    store_audit_summary: bool = False,
    message: str | None = None,
) -> RouterTrainSample | None:
    """Build a sample from ``turn.metadata`` after the pipeline has run.

    The features come from ``metadata['routing_train_features']`` (surfaced at
    inference time); all decision/heuristic fields come from
    ``metadata['routing_extra']`` and the finalized ``routed_tier``.
    """

    features = metadata.get("routing_train_features")
    if not isinstance(features, dict):
        return None
    features_390 = features.get("features_390")
    if features_390 is None:
        return None

    source = str(metadata.get("routing_source") or "")
    if source in _BYPASS_SOURCES:
        return None

    extra = metadata.get("routing_extra")
    if not isinstance(extra, dict):
        extra = {}

    routed_tier = normalize_text_tier(metadata.get("routed_tier")) or str(
        metadata.get("routed_tier") or "c1"
    )

    probs_map = extra.get("probabilities") or {}
    if isinstance(probs_map, dict):
        probabilities = [float(probs_map.get(rc, 0.0)) for rc in ("R0", "R1", "R2", "R3")]
    elif isinstance(probs_map, list | tuple):
        probabilities = [float(p) for p in probs_map[:4]]
    else:
        probabilities = []

    raw_bge = features.get("raw_bge_1536")

    audit_summary: str | None = None
    if store_audit_summary and message:
        # Local import keeps the redactor's regex cost off the no-audit path.
        from opensquilla.observability.decision_log import build_intent_summary

        audit_summary = build_intent_summary(message)

    return RouterTrainSample(
        session_key=session_key,
        turn_index=int(metadata.get("routing_train_turn_index") or 0),
        ts=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        feature_schema_version=str(features.get("feature_schema_version") or "unknown"),
        features_390_b64=encode_features(features_390),
        raw_bge_1536_b64=encode_features(raw_bge) if raw_bge is not None else None,
        route_class=str(extra.get("route_class") or "R1"),
        final_route_class=str(
            extra.get("final_route_class") or _route_class_for_tier(routed_tier)
        ),
        routed_tier=str(routed_tier),
        probabilities=probabilities,
        margin=float(extra.get("margin") or 0.0),
        confidence=float(metadata.get("routing_confidence") or 0.0),
        complaint_detected=bool(extra.get("complaint_detected")),
        anti_downgrade_applied=bool(extra.get("anti_downgrade_applied")),
        confidence_gate_applied=bool(extra.get("confidence_gate_applied")),
        large_context_floor_applied=bool(extra.get("large_context_floor_applied")),
        image_route=source == "image_route",
        exploration=bool(metadata.get("routing_exploration")),
        audit_summary=audit_summary,
        # Key mirrors engine/steps/router_decision_record.DECISION_ID_METADATA_KEY
        # (not imported: this package must not depend on the engine).
        decision_id=(
            str(metadata["router_decision_id"])
            if metadata.get("router_decision_id")
            else None
        ),
    )
