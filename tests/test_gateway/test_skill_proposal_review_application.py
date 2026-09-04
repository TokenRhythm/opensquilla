from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest

from opensquilla.application.skill_proposal_review import (
    ProposalSettings,
    ProposalSettingsPatch,
    SkillProposalReview,
)


@pytest.mark.asyncio
async def test_proposal_review_validates_identity_before_store_access() -> None:
    proposals = AsyncMock()
    proposals.invalidate_catalog = MagicMock()
    runtime = MagicMock()
    review = SkillProposalReview(proposals, runtime)

    with pytest.raises(ValueError, match="8 lowercase hex"):
        await review.show_proposal("../escape")
    with pytest.raises(ValueError, match="valid skill name"):
        await review.disable_auto_enabled("../escape")

    proposals.show_proposal.assert_not_awaited()
    proposals.disable_auto_enabled.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_proposal_activation_invalidates_catalog_once() -> None:
    proposals = AsyncMock()
    proposals.invalidate_catalog = MagicMock()
    proposals.accept_proposal.return_value = {"status": "ok", "name": "demo"}
    proposals.disable_auto_enabled.return_value = {"status": "error"}
    runtime = MagicMock()
    review = SkillProposalReview(proposals, runtime)

    await review.accept_proposal("deadbeef", force=True)
    await review.disable_auto_enabled("demo-skill")

    proposals.accept_proposal.assert_awaited_once_with("deadbeef", force=True)
    proposals.invalidate_catalog.assert_called_once_with()


@pytest.mark.asyncio
async def test_settings_scheduler_failure_leaves_live_and_persisted_state_untouched() -> None:
    previous = ProposalSettings(enabled=False, on_dream_complete=True)
    runtime = MagicMock()
    runtime.snapshot.return_value = previous
    runtime.transition_schedule = AsyncMock(side_effect=RuntimeError("scheduler down"))
    proposals = AsyncMock()
    review = SkillProposalReview(proposals, runtime)

    result = await review.update_settings(ProposalSettingsPatch(enabled=True))

    assert result["status"] == "error"
    assert "scheduler down" in result["reason"]
    runtime.apply_live.assert_not_called()
    runtime.persist.assert_not_called()


@pytest.mark.asyncio
async def test_settings_persistence_failure_restores_live_and_scheduler_state() -> None:
    previous = ProposalSettings(enabled=False, auto_enable=False)
    requested = ProposalSettings(enabled=True, auto_enable=True)
    runtime = MagicMock()
    runtime.snapshot.return_value = previous
    runtime.transition_schedule = AsyncMock()
    runtime.persist.side_effect = OSError("disk full")
    proposals = AsyncMock()
    review = SkillProposalReview(proposals, runtime)

    result = await review.update_settings(
        ProposalSettingsPatch(enabled=True, auto_enable=True)
    )

    assert result["status"] == "error"
    assert "disk full" in result["reason"]
    assert runtime.apply_live.call_args_list == [call(requested), call(previous)]
    assert runtime.transition_schedule.await_args_list == [
        call(was_enabled=False, now_enabled=True),
        call(was_enabled=True, now_enabled=False),
    ]


@pytest.mark.asyncio
async def test_settings_success_applies_schedule_then_live_then_persist() -> None:
    events: list[str] = []
    previous = ProposalSettings(enabled=False)
    runtime = MagicMock()
    runtime.snapshot.return_value = previous

    async def transition_schedule(*, was_enabled: bool, now_enabled: bool) -> None:
        assert (was_enabled, now_enabled) == (False, True)
        events.append("schedule")

    runtime.transition_schedule = transition_schedule
    runtime.apply_live.side_effect = lambda settings: events.append("live")
    runtime.persist.side_effect = lambda settings: events.append("persist")
    review = SkillProposalReview(AsyncMock(), runtime)

    result = await review.update_settings(ProposalSettingsPatch(enabled=True))

    assert result["status"] == "ok"
    assert events == ["schedule", "live", "persist"]
