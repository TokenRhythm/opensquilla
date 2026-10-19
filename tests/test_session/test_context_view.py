from __future__ import annotations

import pytest

from opensquilla.provider import ContentBlockCompaction
from opensquilla.session.compaction_state import (
    StructuredCompactionSummary,
    render_structured_summary,
)
from opensquilla.session.context_view import (
    build_compaction_context_items,
    build_compaction_context_records,
    build_provider_compaction_context,
    compaction_context_fingerprint,
    compaction_replay_is_complete,
    format_compaction_summary_context,
)
from opensquilla.session.models import SessionContextState, SessionSummary


def test_compaction_summary_formatter_is_deterministic_and_deduplicates() -> None:
    rendered = format_compaction_summary_context(
        [" first checkpoint ", "second checkpoint", "first checkpoint"]
    )

    assert rendered == (
        "[Compacted Session Summaries]\n"
        "[Summary 1]\nfirst checkpoint\n\n"
        "[Summary 2]\nsecond checkpoint"
    )


def test_compaction_summary_formatter_omits_oversized_structured_section_atomically() -> None:
    current_status = (
        "CURRENT_STATUS_START\n"
        + ("status detail must never be partially replayed. " * 500)
        + "\nCURRENT_STATUS_END"
    )
    summary = render_structured_summary(
        StructuredCompactionSummary(
            user_goal="Preserve the complete user goal.",
            current_status=current_status,
            next_action="Continue from the verified checkpoint.",
            pending_tool_and_approval_ids=["call_pending_1"],
            critical_carry_forward=["Keep the active prompt byte-for-byte unchanged."],
        )
    )

    rendered = format_compaction_summary_context([summary], max_chars=16_000)

    assert rendered is not None
    assert len(rendered) <= 16_000
    assert "Goal:\nPreserve the complete user goal." in rendered
    assert "Next Action:\nContinue from the verified checkpoint." in rendered
    assert "Pending Tool and Approval IDs:\n- call_pending_1" in rendered
    assert (
        "Current Status:\n"
        "[Omitted from request replay to fit the context budget.]"
    ) in rendered
    assert "CURRENT_STATUS_START" not in rendered
    assert "CURRENT_STATUS_END" not in rendered


def test_compaction_summary_formatter_prioritizes_recent_complete_summary() -> None:
    older = "OLD_SUMMARY_START\n" + ("old context " * 4_000) + "\nOLD_SUMMARY_END"
    newer = render_structured_summary(
        StructuredCompactionSummary(
            user_goal="Use the newest checkpoint.",
            current_status="The newest checkpoint remains complete.",
        )
    )

    rendered = format_compaction_summary_context([older, newer], max_chars=16_000)

    assert rendered is not None
    assert len(rendered) <= 16_000
    assert rendered == format_compaction_summary_context([older, newer], max_chars=16_000)
    assert "[Summary 2]" in rendered
    assert "Use the newest checkpoint." in rendered
    assert "The newest checkpoint remains complete." in rendered
    assert "Legacy compaction summary text omitted in the middle" in rendered


def test_compaction_summary_formatter_preserves_oversized_legacy_summary_edges() -> None:
    summary = (
        "LEGACY_GOAL_START preserve the original objective and constraints.\n"
        + ("historical legacy context that cannot be parsed as structured state. " * 800)
        + "\nRECENT_STATUS_START continue the pending operation next. RECENT_STATUS_END"
    )

    rendered = format_compaction_summary_context([summary], max_chars=16_000)

    assert rendered is not None
    assert len(rendered) <= 16_000
    assert "LEGACY_GOAL_START" in rendered
    assert "RECENT_STATUS_START" in rendered
    assert "RECENT_STATUS_END" in rendered
    assert "Legacy compaction summary text omitted in the middle" in rendered


def test_default_replay_preserves_long_checkpoints_and_prose_section_headers() -> None:
    status = (
        "Completed work.\n\nGoal:\nBuild the requested app.\n\n"
        "Current Status:\nAll reviewed behavior must remain available. "
        + "Implementation details and pending decisions. " * 600
    )
    summary = render_structured_summary(StructuredCompactionSummary(current_status=status))
    rendered = format_compaction_summary_context([summary, "Second checkpoint", summary])

    assert len(summary) > 16_000
    assert rendered == (
        f"[Compacted Session Summaries]\n[Summary 1]\n{summary.strip()}\n\n"
        "[Summary 2]\nSecond checkpoint"
    )
    assert compaction_replay_is_complete([summary, "Second checkpoint", summary], rendered)
    assert not compaction_replay_is_complete([summary, "Second checkpoint"], rendered[:-1])


def test_provider_compaction_context_dropped_for_non_anthropic_provider() -> None:
    # An Anthropic-native state must not replay when a different provider is
    # active (the live-provider gate). A fork or mid-session switch can leave
    # such state in the session; replaying it cross-provider corrupts the
    # request, so the builder returns empty for non-anthropic providers.
    anthropic_state = SessionContextState(
        session_id="session",
        session_key="agent:main:ctx",
        provider="anthropic",
        model="claude-opus-4-7",
        state_kind="anthropic_compaction_block",
        payload={"content": "native state"},
        covered_through_id=9,
        created_at=3000,
        portable=False,
        cacheable=True,
    )
    for provider_kind in ("openai", "gemini", "openai_responses"):
        context = build_provider_compaction_context(
            context_states=[anthropic_state],
            provider_kind=provider_kind,
            now_ms=4000,
        )
        assert context.messages == []
        assert context.covered_through_ids == set()


def test_provider_compaction_context_prefers_latest_state_independent_of_input_order() -> None:
    newer = SessionContextState(
        session_id="session",
        session_key="agent:main:ctx",
        provider="anthropic",
        model="claude-opus-4-7",
        state_kind="anthropic_compaction_block",
        payload={"content": "new native state"},
        covered_through_id=9,
        created_at=3000,
        portable=False,
        cacheable=True,
    )
    older = SessionContextState(
        session_id="session",
        session_key="agent:main:ctx",
        provider="anthropic",
        model="claude-opus-4-7",
        state_kind="anthropic_compaction_block",
        payload={"content": "old native state"},
        covered_through_id=7,
        created_at=1000,
        portable=False,
        cacheable=True,
    )

    context = build_provider_compaction_context(
        context_states=[newer, older],
        provider_kind="anthropic",
        now_ms=4000,
    )

    assert context.covered_through_ids == {9}
    assert len(context.messages) == 1
    block = context.messages[0].content[0]
    assert isinstance(block, ContentBlockCompaction)
    assert block.content == "new native state"


def test_compaction_context_items_deduplicate_structured_state_by_latest_coverage() -> None:
    newer = SessionContextState(
        session_id="session",
        session_key="agent:main:ctx",
        provider="portable",
        state_kind="structured_summary_v1",
        payload={
            "schema_version": 1,
            "current_status": "new structured state",
        },
        covered_through_id=7,
        created_at=3000,
        portable=True,
        cacheable=True,
    )
    older = SessionContextState(
        session_id="session",
        session_key="agent:main:ctx",
        provider="portable",
        state_kind="structured_summary_v1",
        payload={
            "schema_version": 1,
            "current_status": "old structured state",
        },
        covered_through_id=7,
        created_at=1000,
        portable=True,
        cacheable=True,
    )
    summary = SessionSummary(
        session_id="session",
        session_key="agent:main:ctx",
        summary_text="plain summary fallback",
        covered_through_id=7,
    )

    items = build_compaction_context_items(
        context_states=[newer, older],
        summaries=[summary],
        now_ms=4000,
    )

    rendered = "\n".join(items)
    assert "new structured state" in rendered
    assert "old structured state" not in rendered
    assert "plain summary fallback" not in rendered


def test_compaction_context_records_expose_correlation_metadata() -> None:
    state = SessionContextState(
        session_id="session",
        session_key="agent:main:ctx",
        provider="portable",
        state_kind="structured_summary_v1",
        payload={
            "schema_version": 1,
            "current_status": "structured state",
            "compaction_id": "cmp_state_1",
        },
        covered_through_id=9,
        portable=True,
        cacheable=True,
    )

    records = build_compaction_context_records(
        context_states=[state],
        summaries=[],
    )

    assert len(records) == 1
    assert records[0].compaction_id == "cmp_state_1"
    assert records[0].source == "context_state"
    assert records[0].covered_through_id == 9


def test_rolling_context_state_supersedes_older_checkpoint_chain() -> None:
    older = SessionContextState(
        session_id="session",
        session_key="agent:main:ctx",
        provider="portable",
        state_kind="structured_summary_v1",
        payload={
            "schema_version": 1,
            "current_status": "obsolete checkpoint",
        },
        covered_through_id=5,
        created_at=1000,
        portable=True,
        cacheable=True,
    )
    rolling = SessionContextState(
        session_id="session",
        session_key="agent:main:ctx",
        provider="portable",
        state_kind="structured_summary_v1",
        payload={
            "schema_version": 1,
            "current_status": "replacement checkpoint",
            "source_coverage": {"replaces_prior_context": True},
        },
        covered_through_id=9,
        created_at=2000,
        portable=True,
        cacheable=True,
    )
    older_summary = SessionSummary(
        session_id="session",
        session_key="agent:main:ctx",
        summary_text="obsolete summary fallback",
        covered_through_id=5,
    )

    items = build_compaction_context_items(
        context_states=[older, rolling],
        summaries=[older_summary],
        now_ms=3000,
    )

    rendered = "\n".join(items)
    assert "replacement checkpoint" in rendered
    assert "obsolete checkpoint" not in rendered
    assert "obsolete summary fallback" not in rendered


def test_invalid_replacement_state_uses_same_generation_without_reviving_old_context() -> None:
    older = SessionContextState(
        session_id="session",
        session_key="agent:main:ctx",
        provider="portable",
        state_kind="structured_summary_v1",
        payload={
            "schema_version": 1,
            "current_status": "still usable prior checkpoint",
        },
        covered_through_id=5,
        created_at=1000,
        portable=True,
        cacheable=True,
    )
    invalid_replacement = SessionContextState(
        session_id="session",
        session_key="agent:main:ctx",
        provider="portable",
        state_kind="structured_summary_v1",
        payload={
            "schema_version": {"invalid": True},
            "current_status": "must not suppress fallbacks",
            "compaction_id": "replacement",
            "source_coverage": {"replaces_prior_context": True},
        },
        covered_through_id=9,
        created_at=2000,
        portable=True,
        cacheable=True,
    )
    replacement_text_fallback = SessionSummary(
        session_id="session",
        session_key="agent:main:ctx",
        summary_text="usable replacement text fallback",
        compaction_id="replacement",
        covered_through_id=9,
    )

    items = build_compaction_context_items(
        context_states=[older, invalid_replacement],
        summaries=[replacement_text_fallback],
        now_ms=3000,
    )

    rendered = "\n".join(items)
    assert "still usable prior checkpoint" not in rendered
    assert "usable replacement text fallback" in rendered
    assert "must not suppress fallbacks" not in rendered


@pytest.mark.parametrize("mismatch", ["compaction_id", "session_id", "session_key", "boundary"])
def test_invalid_checkpoint_cannot_use_another_generation_as_fallback(mismatch) -> None:
    state = SessionContextState(
        session_id="session", session_key="agent:main:ctx", provider="portable",
        state_kind="structured_summary_v1", portable=True, covered_through_id=9,
        payload={
            "compaction_id": "new", "known_failures": [{"count": 5}],
            "source_coverage": {"replaces_prior_context": True},
        },
    )
    summary = SessionSummary(
        session_id="session", session_key="agent:main:ctx", compaction_id="new",
        covered_through_id=9, summary_text="must not resurrect another task",
    )
    field = "covered_through_id" if mismatch == "boundary" else mismatch
    summary = summary.model_copy(update={field: 8 if mismatch == "boundary" else "other"})
    with pytest.raises(ValueError, match="compaction_checkpoint_unavailable"):
        build_compaction_context_items(context_states=[state], summaries=[summary])


def test_checkpoint_only_corruption_never_falls_back_to_same_boundary_older_task() -> None:
    older = SessionContextState(
        id=1, session_id="session", session_key="agent:main:ctx", provider="portable",
        state_kind="structured_summary_v1", portable=True, covered_through_id=9,
        created_at=1000,
        payload={"compaction_id": "old", "current_status": "obsolete plan"},
    )
    newer = older.model_copy(update={
        "id": 2,
        "payload": {
            "compaction_id": "new", "files_and_artifacts": [{"ref": {"handle": "bad"}}],
            "source_coverage": {"replaces_prior_context": True},
        },
    })
    summaries = [SessionSummary(
        session_id="session", session_key="agent:main:ctx", covered_through_id=9,
        compaction_id=generation, summary_text=text,
    ) for generation, text in [("old", "obsolete plan"), ("new", "current task only")]]
    records = build_compaction_context_records(context_states=[older, newer], summaries=summaries)
    assert [(record.text, record.compaction_id) for record in records] == [
        ("current task only", "new"),
    ]


def test_source_fingerprint_covers_metadata_even_when_rendered_text_is_identical() -> None:
    state = SessionContextState(
        session_id="session", session_key="agent:main:ctx", provider="portable",
        state_kind="structured_summary_v1", portable=True, covered_through_id=9,
        payload={
            "compaction_id": "current", "current_status": "same text",
            "source_coverage": {"replaces_prior_context": False},
        },
    )
    replaced = state.model_copy(update={"payload": {
        **state.payload, "source_coverage": {"replaces_prior_context": True},
    }})
    assert build_compaction_context_items(context_states=[state], summaries=[]) == (
        build_compaction_context_items(context_states=[replaced], summaries=[])
    )
    assert compaction_context_fingerprint(context_states=[state], summaries=[]) != (
        compaction_context_fingerprint(context_states=[replaced], summaries=[])
    )


@pytest.mark.parametrize("damaged_payload", [None, [], "broken"])
def test_non_mapping_latest_checkpoint_cannot_silently_revive_prior_task(damaged_payload) -> None:
    older = SessionContextState(
        id=1, session_id="session", session_key="agent:main:ctx", provider="portable",
        state_kind="structured_summary_v1", portable=True, covered_through_id=5,
        created_at=1000, payload={"current_status": "obsolete task"},
    )
    damaged = older.model_copy(update={
        "id": 2, "covered_through_id": 9, "created_at": 2000, "payload": damaged_payload,
    })
    with pytest.raises(ValueError, match="compaction_checkpoint_unavailable"):
        build_compaction_context_items(context_states=[older, damaged], summaries=[])
    # A later trusted replacement has already retired the corrupt record.
    latest = older.model_copy(update={
        "id": 3, "covered_through_id": 12, "created_at": 3000,
        "payload": {
            "current_status": "current task",
            "source_coverage": {"replaces_prior_context": True},
        },
    })
    rendered = "\n".join(build_compaction_context_items(
        context_states=[older, damaged, latest], summaries=[],
    ))
    assert "current task" in rendered
    assert "obsolete task" not in rendered
