"""Offline label alignment for the router self-learning loop.

Turns a session's captured runtime signals (the model's raw prediction plus the
heuristic markers — complaint, confidence gate, anti-downgrade) into a corrected
supervised label per turn.

Pure functions over :class:`RouterTrainSample`; no IO, no raw text (alignment
relies only on the captured boolean/class signals), so it is fully unit-testable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import numpy as np

from opensquilla.squilla_router.self_learning.schema import RouterTrainSample, decode_features

ROUTE_CLASSES = ("R0", "R1", "R2", "R3")
# A single retrospective correction may bump at most this many tiers, so one
# complaint never teaches a c0 input to jump straight to c3.
MAX_RETRO_STEP = 2
# Only inputs the model routed at or below this index are eligible for
# retrospective up-routing; higher served tiers make "under-routed" implausible
# and the complaint is more likely about correctness/style than capability.
RETRO_ELIGIBLE_MAX_IDX = 1  # R1

REASON_NORMAL = "normal"
REASON_CONFIDENCE_BACKOFF = "confidence_backoff"
REASON_IMMEDIATE_COMPLAINT = "immediate_complaint"
REASON_RETROSPECTIVE = "retrospective_under_routing"
# Explicit thumbs feedback (F7). The _ENSEMBLE variants exist because an
# ensemble turn's rating judges candidates + aggregator, not the tier choice:
# they never produce upgrade labels and are weighted accordingly in dataset.py.
REASON_EXPLICIT_DOWNVOTE = "explicit_downvote"
REASON_EXPLICIT_UPVOTE = "explicit_upvote"
REASON_EXPLICIT_DOWNVOTE_ENSEMBLE = "explicit_downvote_ensemble"
REASON_EXPLICIT_UPVOTE_ENSEMBLE = "explicit_upvote_ensemble"
# Single-model down-vote on an already-high tier: not attributable to
# under-routing, so no upgrade label. A distinct reason (not the _ENSEMBLE
# one) because reasons persist into datasets/receipts and the two exclusion
# causes must stay distinguishable for diagnostics.
REASON_EXPLICIT_DOWNVOTE_HIGH_TIER = "explicit_downvote_high_tier"
# A down-voted turn whose signal cannot become an upgrade label is *excluded*
# from training rather than kept as a confirmation: the user just said the
# outcome was bad.
EXCLUDED_REASONS = frozenset(
    {REASON_EXPLICIT_DOWNVOTE_ENSEMBLE, REASON_EXPLICIT_DOWNVOTE_HIGH_TIER}
)


def route_index(route_class: str) -> int:
    """Map ``R0``..``R3`` to ``0``..``3`` (unknown -> R1, the safe default)."""

    try:
        return ROUTE_CLASSES.index(route_class)
    except ValueError:
        return 1


def class_at(idx: int) -> str:
    return ROUTE_CLASSES[max(0, min(len(ROUTE_CLASSES) - 1, idx))]


def _day(ts: str) -> str:
    return ts[:10] if ts else ""


def _feature_hash(b64: str) -> str:
    return hashlib.sha256(b64.encode("ascii")).hexdigest()[:16]


@dataclass
class AlignedSample:
    """One turn with its corrected training target and provenance."""

    features_390: np.ndarray  # float32, decoded
    target_idx: int  # 0..3 (corrected label)
    served_idx: int  # 0..3 (what actually ran; cost-gate reference)
    reason: str
    session_key: str
    turn_index: int
    day: str
    feature_hash: str
    confirmed: bool = True  # retrospective: did T+2 confirm resolution?


def align_session(
    samples: list[RouterTrainSample],
    feedback: dict[str, Any] | None = None,
) -> list[AlignedSample]:
    """Align one session's samples into corrected training targets.

    Per turn ``i`` the label is decided in increasing priority:
    normal -> confidence_backoff -> [explicit up-vote] -> immediate_complaint,
    then a retrospective override when the *next* turn complains and turn ``i``
    was under-routed, then an explicit down-vote override (the strongest
    signal: the user's own judgment of *this* turn).

    ``feedback`` maps a V017 ``decision_id`` to a rating entry with
    ``rating`` ("up" | "down") and ``executed_kind`` ("single" | "ensemble")
    attributes (see ``feedback.load_feedback_map``). Joining by decision id is
    exact: legacy samples without one never match. ``None`` — the default —
    keeps the output byte-identical to the pre-feedback behavior; that
    invariant is pinned by a regression test.

    Down-votes obey the same anti-noise rules as retrospective corrections,
    plus two of their own: an already-high prediction (above
    ``RETRO_ELIGIBLE_MAX_IDX``) and any ensemble turn produce **no** upgrade
    label — the dissatisfaction cannot be attributed to an under-routed tier —
    and instead mark the turn excluded so a bad outcome is never kept as a
    confirmation sample.
    """

    # Sort by capture timestamp, not turn_index: turn_index can saturate/reset
    # across a history reset (e.g. 5 -> 0), which would scramble the true
    # chronological order and misattribute retrospective complaint labels.
    # Python's sort is stable and iter_samples yields in append order, so
    # same-second ties keep their real capture order.
    ordered = sorted(samples, key=lambda s: s.ts)
    n = len(ordered)
    out: list[AlignedSample] = []

    for i, s in enumerate(ordered):
        target_idx = route_index(s.final_route_class)
        reason = REASON_NORMAL
        confirmed = True

        entry = (
            feedback.get(s.decision_id)
            if feedback is not None and s.decision_id
            else None
        )
        rating = getattr(entry, "rating", None) if entry is not None else None
        is_ensemble = (
            getattr(entry, "executed_kind", "single") == "ensemble"
            if entry is not None
            else False
        )

        if s.confidence_gate_applied:
            # The gate forced the served (default) tier on a low-confidence call;
            # teach the classifier that this ambiguous input resolves there.
            reason = REASON_CONFIDENCE_BACKOFF

        if rating == "up":
            # Explicit confirmation of the served tier. On ensemble turns the
            # endorsement covers the whole candidates+aggregator chain, so the
            # dedicated reason lets dataset.py weight it down to normal-level.
            reason = (
                REASON_EXPLICIT_UPVOTE_ENSEMBLE if is_ensemble else REASON_EXPLICIT_UPVOTE
            )

        if s.complaint_detected:
            # The user's complaint drove an upgrade this turn; the upgraded tier
            # is the truest label for this (complaint) input.
            reason = REASON_IMMEDIATE_COMPLAINT
        else:
            retro = _retrospective_target(ordered, i, n)
            if retro is not None:
                target_idx, confirmed = retro
                reason = REASON_RETROSPECTIVE

        if rating == "down":
            target_idx, reason, confirmed = _apply_downvote(
                ordered, i, n,
                is_ensemble=is_ensemble,
                base_target=target_idx,
                base_reason=reason,
                base_confirmed=confirmed,
            )

        out.append(
            AlignedSample(
                features_390=decode_features(s.features_390_b64, 390),
                target_idx=target_idx,
                served_idx=route_index(s.final_route_class),
                reason=reason,
                session_key=s.session_key,
                turn_index=s.turn_index,
                day=_day(s.ts),
                feature_hash=_feature_hash(s.features_390_b64),
                confirmed=confirmed,
            )
        )

    return out


def _apply_downvote(
    ordered: list[RouterTrainSample],
    i: int,
    n: int,
    *,
    is_ensemble: bool,
    base_target: int,
    base_reason: str,
    base_confirmed: bool,
) -> tuple[int, str, bool]:
    """Resolve turn ``i``'s label under an explicit down-vote.

    Returns ``(target_idx, reason, confirmed)``. Three cases:

    1. Ensemble: no upgrade label ever (tier / candidate / aggregator blame is
       inseparable). The turn is marked ``explicit_downvote_ensemble`` and
       excluded from training by weight (see ``EXCLUDED_REASONS``).
    2. A correction signal already produced an upgrade (complaint upgrade or
       retrospective): the down-vote endorses it — keep the target, upgrade
       the reason to ``explicit_downvote`` (higher base weight).
    3. Standalone down-vote: the retrospective anti-noise gate applies, but
       with a **+1 step only** (no resolving-tier anchor exists, so be more
       conservative than retrospective's +2 cap). A prediction already above
       ``RETRO_ELIGIBLE_MAX_IDX`` yields no upgrade — the dissatisfaction is
       more plausibly about content than capability — and the turn is instead
       excluded (weight 0) so it never trains as a confirmation.
    """

    s = ordered[i]

    if is_ensemble:
        return route_index(s.final_route_class), REASON_EXPLICIT_DOWNVOTE_ENSEMBLE, base_confirmed

    served_idx = route_index(s.final_route_class)
    if (
        base_reason in (REASON_IMMEDIATE_COMPLAINT, REASON_RETROSPECTIVE)
        and base_target > route_index(s.route_class)
    ):
        # The correction's label genuinely exceeds the model's raw prediction;
        # the down-vote endorses it. A complaint whose upgrade was capped/held
        # (final == route, so the label IS the rejected tier) is NOT an
        # upgrade to endorse — falling through would train the rejected tier
        # at the table's highest weight; the standalone path below handles it.
        return base_target, REASON_EXPLICIT_DOWNVOTE, base_confirmed

    # Anchor on what actually SERVED, not just the raw prediction: a
    # confidence-gate or anti-downgrade hold can serve a higher tier than the
    # model predicted, and the user's judgment is about the served response.
    # Bumping from the prediction alone could train the very tier the user
    # rejected (gate case) or a tier below it (hold case).
    cur_idx = max(route_index(s.route_class), served_idx)
    if cur_idx > RETRO_ELIGIBLE_MAX_IDX:
        # Not attributable to under-routing; drop the confirmation instead.
        return served_idx, REASON_EXPLICIT_DOWNVOTE_HIGH_TIER, base_confirmed

    target_idx = min(cur_idx + 1, len(ROUTE_CLASSES) - 1)
    # Confirmation mirror of retrospective: a calm follow-up turn confirms the
    # bump; a complaint on the next turn means +1 was not enough — keep the
    # label (the direction is still right) but unconfirmed halves its weight.
    nxt = ordered[i + 1] if i + 1 < n else None
    confirmed = nxt is not None and not nxt.complaint_detected
    return target_idx, REASON_EXPLICIT_DOWNVOTE, confirmed


def _retrospective_target(
    ordered: list[RouterTrainSample],
    i: int,
    n: int,
) -> tuple[int, bool] | None:
    """Return ``(target_idx, confirmed)`` if turn ``i`` was under-routed.

    Triggered when turn ``i+1`` complains and turn ``i`` was routed low. The
    target is capped at ``+MAX_RETRO_STEP`` and never exceeds the tier that
    actually resolved the complaint. If turn ``i+2`` complains again, the higher
    tier did *not* resolve the issue, so the signal is rejected as noise.
    """

    if i + 1 >= n:
        return None
    nxt = ordered[i + 1]
    if not nxt.complaint_detected:
        return None

    cur_idx = route_index(ordered[i].route_class)
    if cur_idx > RETRO_ELIGIBLE_MAX_IDX:
        return None

    resolved_idx = route_index(nxt.final_route_class)
    if resolved_idx <= cur_idx:
        return None  # no genuine upgrade signal to learn from

    after = ordered[i + 2] if i + 2 < n else None
    if after is not None and after.complaint_detected:
        return None  # the upgrade did not resolve it -> reject as noise

    target_idx = min(resolved_idx, cur_idx + MAX_RETRO_STEP, len(ROUTE_CLASSES) - 1)
    target_idx = max(target_idx, cur_idx + 1)
    confirmed = after is not None  # i+2 exists and (per above) did not complain
    return target_idx, confirmed
