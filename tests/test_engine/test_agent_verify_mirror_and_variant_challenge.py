"""Verification evidence credit and retired scratch mirror compatibility."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, DoneEvent
from opensquilla.engine.finalize_evidence_gate import FinalizeEvidenceTracker
from opensquilla.provider import ChatConfig, Message
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.tools import write_policy
from opensquilla.tools.types import ToolContext


@pytest.mark.parametrize("retired_mirror_active", [False, True])
def test_retired_mirror_slot_does_not_change_write_denial(
    tmp_path, monkeypatch, retired_mirror_active
) -> None:
    monkeypatch.delenv("OPENSQUILLA_WORKSPACE_WRITE_DENY_GUIDANCE", raising=False)
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    ctx = ToolContext(
        workspace_dir=str(workspace),
        scratch_dir=str(scratch),
        workspace_write_deny_globs=["tests/**"],
        scratch_verify_mirror_active=retired_mirror_active,
    )
    token = write_policy.current_tool_context.set(ctx)
    try:
        match = write_policy.match_workspace_write_deny(
            workspace / "tests" / "test_a.py", workspace=workspace, ctx=ctx
        )
        assert match is not None
        block = write_policy.workspace_write_deny_block("write_file", match)
    finally:
        write_policy.current_tool_context.reset(token)

    assert block["reason"] == "workspace_write_deny"
    assert block["retryable"] is False
    assert str(scratch) in str(block["message"])
    assert "verify-mirror" not in str(block["message"])


# ---------------------------------------------------------------------------
# Tracker: evidence_credit=False withholds all verification crediting
# ---------------------------------------------------------------------------


def test_tracker_uncredited_green_does_not_clear_red_evidence() -> None:
    tracker = FinalizeEvidenceTracker()
    tracker.observe_write("src/main.py", iteration=1)
    tracker.observe_execution(
        "pytest tests/test_a.py",
        red=True,
        exit_code=1,
        failure_anchors=["FAILED tests/test_a.py::test_x"],
        iteration=2,
    )
    tracker.observe_execution(
        "pytest /tmp/squilla-scratch/verify-mirror/tests/test_a.py",
        red=False,
        exit_code=0,
        iteration=3,
        evidence_credit=False,
    )

    observation = tracker.build_observation(has_workspace_diff=True)

    # The uncredited mirror green must not become the trailing post-edit
    # record: the earlier red is still the latest credited execution.
    assert observation.should_challenge is True
    assert observation.triggers[0] == "red_execution_after_final_edit"


def test_tracker_uncredited_run_does_not_count_as_post_edit_execution() -> None:
    tracker = FinalizeEvidenceTracker()
    tracker.observe_write("src/main.py", iteration=1)
    tracker.observe_execution(
        "pytest /tmp/squilla-scratch/verify-mirror/tests/test_a.py",
        red=False,
        exit_code=0,
        iteration=2,
        evidence_credit=False,
    )

    observation = tracker.build_observation(has_workspace_diff=True)

    assert observation.post_edit_execution_count == 0
    assert observation.triggers == ["no_execution_after_final_edit"]


def test_tracker_uncredited_run_still_tracks_deletion_side_effects() -> None:
    tracker = FinalizeEvidenceTracker()
    tracker.observe_write("/tmp/squilla-scratch/repro.py", iteration=1)
    tracker.observe_write("src/main.py", iteration=2)
    tracker.observe_execution(
        "python /tmp/squilla-scratch/repro.py",
        red=True,
        exit_code=1,
        iteration=3,
    )
    # The uncredited command still deletes the artifact: side effects are
    # facts about the filesystem, not verification evidence.
    tracker.observe_execution(
        "rm /tmp/squilla-scratch/repro.py"
        " && pytest /tmp/squilla-scratch/verify-mirror/tests/test_a.py",
        red=False,
        exit_code=0,
        iteration=4,
        evidence_credit=False,
    )

    observation = tracker.build_observation(has_workspace_diff=True)

    assert "never_green_repro_deleted" in observation.triggers


def test_tracker_evidence_credit_defaults_true() -> None:
    tracker = FinalizeEvidenceTracker()
    tracker.observe_write("src/main.py", iteration=1)
    tracker.observe_execution(
        "pytest tests/test_a.py", red=False, exit_code=0, iteration=2
    )

    observation = tracker.build_observation(has_workspace_diff=True)

    assert observation.should_challenge is False
    assert observation.post_edit_execution_count == 1


class _FinalProvider:
    provider_name = "fake"

    async def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_retired_mirror_config_does_not_arm_tool_context(tmp_path) -> None:
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=_FinalProvider(),
        config=AgentConfig(scratch_verify_mirror=True),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Inspect the workspace")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert tool_context.scratch_verify_mirror_active is False
