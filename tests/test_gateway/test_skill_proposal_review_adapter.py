from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from opensquilla.application.skill_proposal_review import ProposalSettings
from opensquilla.gateway.adapters.skill_proposal_review import (
    GatewaySkillProposalReviewAdapter,
)


@pytest.mark.asyncio
async def test_proposal_adapter_accepts_legacy_identity_alias() -> None:
    proposals = AsyncMock()
    proposals.show_proposal.return_value = {"status": "ok"}
    runtime = MagicMock()
    adapter = GatewaySkillProposalReviewAdapter(proposals, runtime)

    await adapter.show_proposal({"proposalId": "deadbeef"})

    proposals.show_proposal.assert_awaited_once_with("deadbeef")


@pytest.mark.asyncio
async def test_proposal_adapter_rejects_conflicting_identity_aliases() -> None:
    adapter = GatewaySkillProposalReviewAdapter(AsyncMock(), MagicMock())

    with pytest.raises(ValueError, match="aliases must match"):
        await adapter.show_proposal(
            {"proposal_id": "deadbeef", "proposalId": "cafebabe"}
        )


@pytest.mark.asyncio
async def test_settings_adapter_rejects_truthy_non_boolean_values() -> None:
    runtime = MagicMock()
    runtime.snapshot.return_value = ProposalSettings()
    adapter = GatewaySkillProposalReviewAdapter(AsyncMock(), runtime)

    with pytest.raises(ValueError, match="enabled must be a boolean"):
        await adapter.update_settings({"enabled": 1})

    runtime.transition_schedule.assert_not_called()
