from __future__ import annotations

import pytest

from opensquilla.session.compaction import validate_compaction_artifact
from opensquilla.session.compaction_state import CompactionObligation


def test_missing_semantic_fact_is_reported_without_rejecting_checkpoint() -> None:
    coverage, rejection = validate_compaction_artifact(
        "The user cancelled the prior migration. Continue the current release.",
        [CompactionObligation(kind="user_goal", value="rewrite every module")],
    )
    assert rejection is None
    assert coverage.status == "fail_reported"
    assert coverage.missing_obligations == ["user_goal: rewrite every module"]
    assert not coverage.blocked
    assert coverage.critical_carry_forward == []


@pytest.mark.parametrize("body", ["", "  ", "[Structured Compaction Summary]"])
def test_diagnostic_coverage_does_not_weaken_empty_artifact_gate(body: str) -> None:
    coverage, rejection = validate_compaction_artifact(
        body, [CompactionObligation(kind="user_goal", value="an omitted goal")],
    )
    assert rejection == "empty_summary"
    assert not coverage.blocked


def test_diagnostic_coverage_does_not_weaken_consumer_replay_gate() -> None:
    _coverage, rejection = validate_compaction_artifact(
        "A complete model-authored checkpoint.",
        [],
        summary_replay_renderer=lambda _text: "clipped checkpoint",
    )
    assert rejection == "summary_replay_incomplete"
