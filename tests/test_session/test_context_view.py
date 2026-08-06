from __future__ import annotations

from opensquilla.provider import ContentBlockCompaction
from opensquilla.session.compaction_state import (
    StructuredCompactionSummary,
    render_structured_summary,
)
from opensquilla.session.context_view import (
    build_compaction_context_items,
    build_compaction_context_records,
    build_provider_compaction_context,
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

    rendered = format_compaction_summary_context([summary])

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

    rendered = format_compaction_summary_context([older, newer])

    assert rendered is not None
    assert len(rendered) <= 16_000
    assert rendered == format_compaction_summary_context([older, newer])
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

    rendered = format_compaction_summary_context([summary])

    assert rendered is not None
    assert len(rendered) <= 16_000
    assert "LEGACY_GOAL_START" in rendered
    assert "RECENT_STATUS_START" in rendered
    assert "RECENT_STATUS_END" in rendered
    assert "Legacy compaction summary text omitted in the middle" in rendered


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


def test_invalid_replacement_state_preserves_prior_context_and_text_fallback() -> None:
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
        covered_through_id=9,
    )

    items = build_compaction_context_items(
        context_states=[older, invalid_replacement],
        summaries=[replacement_text_fallback],
        now_ms=3000,
    )

    rendered = "\n".join(items)
    assert "still usable prior checkpoint" in rendered
    assert "usable replacement text fallback" in rendered
    assert "must not suppress fallbacks" not in rendered
