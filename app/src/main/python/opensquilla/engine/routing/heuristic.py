"""Dependency-free heuristic router strategy for degraded installs.

When the bundled V4 Phase 3 ML runtime cannot load (missing ``recommended``
extra, missing native libraries, incomplete bundle assets), the router step
falls back to this strategy instead of pinning every turn to the default
tier. It classifies from cheap, deterministic surface features of the turn
that are already available at the step's classify seam — no ML runtime, no
network, and no dependencies beyond the standard library and in-tree pure
modules.

Feature bands (checked in order, first match wins):

``heavy`` → **c3 @ 0.60**
    Very long input (>= 12000 chars) or a multi-file shape (>= 3 fenced
    code blocks).

``code_or_material`` → **c2 @ 0.60**
    Any fenced code block, long input (>= 2500 chars), or non-image
    attachments on the turn (image attachments never reach this strategy —
    the step routes them to a vision tier before classification).

``short_plain`` → **c0 @ 0.55**
    Short plain text (<= 240 chars, no code fence).

``medium_plain`` → **c1 @ 0.55**
    Plain text up to 1200 chars.

``borderline_plain`` → **c1 @ 0.40**
    Plain text between 1200 and 2500 chars — long enough that c1 is a
    guess, not a call.

Confidence rationale
--------------------
The post-classifier confidence gate
(:func:`opensquilla.engine.routing.policy.confidence_gate`) flattens any
tier other than the configured default whose confidence falls below the
threshold (default 0.5, discounted by 0.05 for tiers above the default).
The values above are chosen deliberately against those cutoffs:

* Confident bands sit at 0.55–0.60 — above both cutoffs — so the gate does
  not flatten every heuristic decision back to the default tier, which
  would recreate the silent ``v4_unavailable`` degradation this strategy
  replaces.
* They stay modest (< 0.7) so telemetry honestly reads them as low-signal
  guesses, and an operator who raises ``confidence_threshold`` above 0.6
  deliberately disables heuristic tiering in favor of their default tier.
* The ``borderline_plain`` band sits at 0.40 — below both cutoffs — so
  ambiguous mid-length plain text intentionally defers to the operator's
  configured ``default_tier``.

Decisions carry ``source="heuristic"`` and an ``extra`` payload mirroring
the V4 adapter shape (``route_class`` / ``thinking_mode`` /
``prompt_policy`` / ``model_version``) plus the observed feature values, so
history accumulation, the routing policy stages, and telemetry all flow
unchanged and remain distinguishable from real ML decisions.
"""

from __future__ import annotations

from typing import Any

from opensquilla.router_tiers import (
    DEFAULT_TEXT_TIER,
    TEXT_TIERS,
    TIER_TO_ROUTE_CLASS,
    normalize_text_tier,
)

HEURISTIC_SOURCE = "heuristic"
HEURISTIC_MODEL_VERSION = "heuristic-v1"

# Band thresholds (characters of the semantic message / fenced block count).
HEAVY_MIN_CHARS = 12_000
HEAVY_MIN_FENCED_BLOCKS = 3
CODE_OR_MATERIAL_MIN_CHARS = 2_500
SHORT_PLAIN_MAX_CHARS = 240
MEDIUM_PLAIN_MAX_CHARS = 1_200

# Confidence values relative to the confidence gate's default threshold of
# 0.5 (0.45 effective for tiers above the default via the 0.05 margin) —
# see the module docstring for the full rationale.
CONFIDENT_HIGH_TIER_CONFIDENCE = 0.60
CONFIDENT_LOW_TIER_CONFIDENCE = 0.55
BORDERLINE_CONFIDENCE = 0.40

# Thinking modes consistent with the tier floors the policy engine's
# reconcile step enforces (c2 → T2, c3 → T3, default tier → T1).
_TIER_THINKING_MODE = {"c0": "T0", "c1": "T1", "c2": "T2", "c3": "T3"}


def extract_features(
    message: str,
    routing_history: list[dict] | None = None,
    attachment_count: int | None = None,
) -> dict[str, Any]:
    """Return the deterministic surface features the bands are built from."""

    fenced_blocks = message.count("```") // 2
    return {
        "char_len": len(message),
        "has_code_fence": "```" in message,
        "code_fence_blocks": fenced_blocks,
        "attachment_count": int(attachment_count or 0),
        "history_depth": len(routing_history or []),
    }


def classify_features(features: dict[str, Any]) -> tuple[str, str, float]:
    """Map extracted features to ``(band, tier, confidence)``.

    Pure and deterministic: the same features always produce the same band.
    Band order matters — the strongest signals win.
    """

    char_len = int(features.get("char_len", 0))
    has_fence = bool(features.get("has_code_fence", False))
    fenced_blocks = int(features.get("code_fence_blocks", 0))
    attachment_count = int(features.get("attachment_count", 0))

    if char_len >= HEAVY_MIN_CHARS or fenced_blocks >= HEAVY_MIN_FENCED_BLOCKS:
        return "heavy", "c3", CONFIDENT_HIGH_TIER_CONFIDENCE
    if has_fence or char_len >= CODE_OR_MATERIAL_MIN_CHARS or attachment_count > 0:
        return "code_or_material", "c2", CONFIDENT_HIGH_TIER_CONFIDENCE
    if char_len <= SHORT_PLAIN_MAX_CHARS:
        return "short_plain", "c0", CONFIDENT_LOW_TIER_CONFIDENCE
    if char_len <= MEDIUM_PLAIN_MAX_CHARS:
        return "medium_plain", "c1", CONFIDENT_LOW_TIER_CONFIDENCE
    # Deliberately below the gate threshold: defer to the operator's
    # configured default_tier for ambiguous mid-length plain text.
    return "borderline_plain", "c1", BORDERLINE_CONFIDENCE


def _nearest_valid_tier(tier: str, valid_tiers: list[str]) -> str:
    """Pick the closest configured tier, preferring equal-or-higher tiers."""

    if not valid_tiers:
        return DEFAULT_TEXT_TIER
    if tier in valid_tiers:
        return tier
    start = TEXT_TIERS.index(tier) if tier in TEXT_TIERS else 1
    for candidate in TEXT_TIERS[start:]:
        if candidate in valid_tiers:
            return candidate
    for candidate in reversed(TEXT_TIERS[:start]):
        if candidate in valid_tiers:
            return candidate
    return valid_tiers[0]


class HeuristicRouterStrategy:
    """Deterministic fallback classifier used when the ML runtime is absent.

    ``error`` records the runtime load failure that triggered the fallback
    (if any); the router step's ``router_runtime_status()`` accessor and the
    doctor read it for operator-facing diagnostics.
    """

    requires_history = True
    source = HEURISTIC_SOURCE

    def __init__(self, error: BaseException | str | None = None) -> None:
        self.error = error

    async def classify(
        self,
        message: str,
        valid_tiers: list[str],
        routing_history: list[dict] | None = None,
        attachment_count: int | None = None,
        **kwargs: object,
    ) -> tuple[str, float, str, dict]:
        features = extract_features(
            message,
            routing_history=routing_history,
            attachment_count=attachment_count,
        )
        band, tier, confidence = classify_features(features)
        tier = _nearest_valid_tier(tier, valid_tiers)
        normalized = normalize_text_tier(tier) or tier
        extra: dict[str, Any] = {
            # Mirror the V4 adapter's extra shape so the policy stages,
            # routing history, and telemetry consume this unchanged.
            "route_class": TIER_TO_ROUTE_CLASS.get(normalized, "R1"),
            "top1_label": TIER_TO_ROUTE_CLASS.get(normalized, "R1"),
            "thinking_mode": _TIER_THINKING_MODE.get(normalized, "T1"),
            # P1 (standard prompting) everywhere: a length heuristic is too
            # weak a signal to inject P0 compression hints into user text.
            "prompt_policy": "P1",
            "model_version": HEURISTIC_MODEL_VERSION,
            "heuristic_band": band,
            "heuristic_features": features,
        }
        return tier, confidence, HEURISTIC_SOURCE, extra
