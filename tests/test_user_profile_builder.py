"""Cross-batch aggregation reproduces the offline plan's §1.4 worked example.

The worked example is the contract: 63 sessions read, 3 unlabeled, so the
capability denominator is 60; a 5/7 batch majority for ``quality_first``; a
model praised repeatedly is positive while one seen in both directions is
neither. If any of these drift, the producer is no longer the one the plan
specifies.
"""

from __future__ import annotations

from opensquilla.squilla_router.user_profile.builder import build_profile
from opensquilla.squilla_router.user_profile.defaults import default_user_profile
from opensquilla.squilla_router.user_profile.schema import (
    BLAME,
    PRAISE,
    BatchAnalysis,
    ModelMention,
    SessionLabel,
)


def _labels() -> tuple[SessionLabel, ...]:
    # 60 non-unknown (30/12/9 in the top three, 9 in a dropped tail) + 3 unknown
    # = 63 read. Denominator is the 60 labeled; top-3 keeps 0.50/0.20/0.15.
    labels: list[SessionLabel] = []
    labels += [SessionLabel(f"c{i}", "code_generation", 0.8) for i in range(30)]
    labels += [SessionLabel(f"r{i}", "reasoning", 0.7) for i in range(12)]
    labels += [SessionLabel(f"w{i}", "writing", 0.6) for i in range(9)]
    labels += [SessionLabel(f"t{i}", "tool_use", 0.5) for i in range(5)]
    labels += [SessionLabel(f"l{i}", "long_context", 0.4) for i in range(2)]
    labels += [SessionLabel(f"g{i}", "format_following", 0.3) for i in range(2)]
    labels += [SessionLabel(f"u{i}", "unknown") for i in range(3)]
    return tuple(labels)


def _batches() -> list[BatchAnalysis]:
    deepseek = ModelMention("deepseek-v4", PRAISE, ("c0", "c1", "c2", "c3", "c4"), 0.9)
    glm_praise = ModelMention("glm-5.2", PRAISE, ("c5",), 0.4)
    glm_blame = ModelMention("glm-5.2", BLAME, ("c6", "c7"), 0.6)
    first = BatchAnalysis(
        ok=True,
        session_labels=_labels(),
        tradeoff="quality_first",
        tradeoff_confidence=0.7,
        tradeoff_session_ids=("c0", "c1"),
        model_mentions=(deepseek, glm_praise, glm_blame),
    )
    # Four more quality_first (5 total) and two balanced -> 5/7 majority.
    quality = [
        BatchAnalysis(
            ok=True,
            tradeoff="quality_first",
            tradeoff_confidence=0.7,
            tradeoff_session_ids=(f"q{i}",),
        )
        for i in range(4)
    ]
    balanced = [
        BatchAnalysis(ok=True, tradeoff="balanced", tradeoff_confidence=0.5) for _ in range(2)
    ]
    return [first, *quality, *balanced]


def _build() -> dict:
    return build_profile(
        batches=_batches(),
        base_profile=default_user_profile(),
        sessions_read=63,
        day="2026-07-20",
        version="2026-07-20.1",
        top_n=3,
        window_days=90,
    )


def test_capability_prior_matches_the_worked_example() -> None:
    prior = _build()["history"]["capability_prior"]
    assert prior == {
        "code_generation": 0.5,
        "reasoning": 0.2,
        "writing": 0.15,
    }
    # The tail is dropped, so the kept weights sum to less than 1.
    assert abs(sum(prior.values()) - 0.85) < 1e-9


def test_tradeoff_is_the_five_of_seven_majority() -> None:
    profile = _build()
    assert profile["preference"]["quality_latency_tradeoff"] == "quality_first"
    field = profile["_meta"]["fields"]["preference.quality_latency_tradeoff"]
    assert field["confidence"] == 0.7
    assert field["vote"] == "5/7"
    assert field["evidence"] == [
        "session:c0",
        "session:c1",
        "session:q0",
        "session:q1",
        "session:q2",
        "session:q3",
    ]


def test_cost_sensitivity_extension_uses_the_same_batch_vote_shape() -> None:
    batches = [
        BatchAnalysis(
            ok=True,
            cost_sensitivity="high",
            cost_sensitivity_confidence=0.8,
        ),
        BatchAnalysis(
            ok=True,
            cost_sensitivity="high",
            cost_sensitivity_confidence=0.6,
        ),
        BatchAnalysis(
            ok=True,
            cost_sensitivity="low",
            cost_sensitivity_confidence=0.9,
        ),
    ]
    profile = build_profile(
        batches=batches,
        base_profile=default_user_profile(),
        sessions_read=3,
        day="2026-07-20",
        version="2026-07-20.1",
        top_n=3,
        window_days=90,
    )

    assert profile["preference"]["cost_sensitivity"] == "high"
    assert profile["_meta"]["fields"]["preference.cost_sensitivity"] == {
        "confidence": 0.7,
        "vote": "2/3",
    }


def test_a_repeatedly_praised_model_is_positive_a_mixed_one_is_neither() -> None:
    history = _build()["history"]
    assert history["positive_model_ids"] == ["deepseek-v4"]
    assert history["negative_model_ids"] == []  # glm-5.2 seen both ways -> neither


def test_model_mentions_are_prompt_admitted_not_threshold_admitted() -> None:
    profile = build_profile(
        batches=[
            BatchAnalysis(
                ok=True,
                model_mentions=(ModelMention("one-session-model", PRAISE, ("s1",), 0.6),),
            )
        ],
        base_profile=default_user_profile(),
        sessions_read=1,
        day="2026-07-20",
        version="2026-07-20.1",
        top_n=3,
        window_days=90,
    )
    assert profile["history"]["positive_model_ids"] == ["one-session-model"]
    field = profile["_meta"]["fields"]["history.positive_model_ids"]
    assert field["mentions"] == 1
    assert field["evidence"] == ["session:s1"]


def test_model_mention_meta_counts_distinct_evidence_sessions_not_models() -> None:
    profile = build_profile(
        batches=[
            BatchAnalysis(
                ok=True,
                model_mentions=(
                    ModelMention("a", PRAISE, ("s1", "s2"), 0.6),
                    ModelMention("b", PRAISE, ("s2", "s3"), 0.7),
                ),
            )
        ],
        base_profile=default_user_profile(),
        sessions_read=3,
        day="2026-07-20",
        version="2026-07-20.1",
        top_n=3,
        window_days=90,
    )
    field = profile["_meta"]["fields"]["history.positive_model_ids"]
    assert field["mentions"] == 3


def test_model_mention_count_is_not_truncated_with_evidence_display() -> None:
    session_ids = tuple(f"s{i:02d}" for i in range(15))
    profile = build_profile(
        batches=[
            BatchAnalysis(
                ok=True,
                model_mentions=(ModelMention("a", PRAISE, session_ids, 0.8),),
            )
        ],
        base_profile=default_user_profile(),
        sessions_read=15,
        day="2026-07-20",
        version="2026-07-20.1",
        top_n=3,
        window_days=90,
    )
    field = profile["_meta"]["fields"]["history.positive_model_ids"]
    assert field["mentions"] == 15
    assert len(field["evidence"]) == 10


def test_feedback_count_is_the_sessions_read_not_the_labeled_count() -> None:
    assert _build()["history"]["feedback_count"] == 63


def test_provenance_rides_in_meta_without_invented_top_level_source() -> None:
    profile = _build()
    assert "profile_source" not in profile
    assert profile["profile_version"] == "2026-07-20.1"
    assert profile["_meta"]["window_days"] == 90
    assert profile["_meta"]["batches"] == 7


def test_capability_prior_meta_confidence_aggregates_label_confidences() -> None:
    field = _build()["_meta"]["fields"]["history.capability_prior"]
    assert field["confidence"] == 0.695


def test_a_tie_or_unknown_majority_is_no_signal() -> None:
    tie = [
        BatchAnalysis(ok=True, tradeoff="quality_first", tradeoff_confidence=0.7),
        BatchAnalysis(ok=True, tradeoff="latency_first", tradeoff_confidence=0.7),
    ]
    profile = build_profile(
        batches=tie,
        base_profile=default_user_profile(),
        sessions_read=2,
        day="2026-07-20",
        version="2026-07-20.1",
        top_n=3,
        window_days=90,
    )
    # None is an honest no-signal, written as null.
    assert profile["preference"]["quality_latency_tradeoff"] is None
    assert "preference.quality_latency_tradeoff" not in profile["_meta"]["fields"]


def test_failed_batches_do_not_contribute() -> None:
    batches = [
        BatchAnalysis(
            ok=True,
            session_labels=(SessionLabel("a", "reasoning"),),
            tradeoff="quality_first",
            tradeoff_confidence=0.8,
        ),
        BatchAnalysis.failed(("b", "c")),
    ]
    profile = build_profile(
        batches=batches,
        base_profile=default_user_profile(),
        sessions_read=1,
        day="2026-07-20",
        version="2026-07-20.1",
        top_n=3,
        window_days=90,
    )
    assert profile["history"]["capability_prior"] == {"reasoning": 1.0}
