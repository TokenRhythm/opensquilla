from __future__ import annotations

import pytest

from opensquilla.engine.pipeline import TurnContext, run_pipeline


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [False, True])
async def test_failed_step_rolls_back_and_only_required_failure_stops_pipeline(required):
    turn = TurnContext("request", "session", None, None, "model", [], "")
    turn.metadata["existing"] = "preserved"
    calls = []

    async def route(ctx):
        ctx.metadata["partial"] = "discarded"
        raise ValueError("route unavailable")

    async def downstream(ctx):
        calls.append("downstream")
        return ctx

    if required:
        with pytest.raises(ValueError, match="route unavailable"):
            await run_pipeline(turn, [route, downstream], required_steps=("route",))
    else:
        await run_pipeline(turn, [route, downstream])
    assert "partial" not in turn.metadata
    assert turn.metadata["existing"] == "preserved"
    assert calls == ([] if required else ["downstream"])
    record = turn.metadata["pipeline_steps"][0]
    assert record.step_name == "route"
    assert record.applied is False
    assert record.fallback_reason == "route unavailable"
