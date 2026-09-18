"""Plan discussion and proposals share ordinary task terminal persistence."""

from pathlib import Path

import pytest
from test_plan_rpc import SOURCE_KEY, _ignore_subscriber_event, _open_plan_rpc_stack

from opensquilla.gateway.rpc_sessions import _handle_plans_revise, _task_summary
from opensquilla.gateway.task_runtime import TaskRun
from opensquilla.session.models import AgentTaskStatus
from opensquilla.session.plans import new_plan_revision


@pytest.mark.parametrize("submission", ["none", "current_turn", "other_turn"])
async def test_plan_revision_result_is_durable_and_attributed_to_its_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    submission: str,
) -> None:
    events = []

    async def capture(key, name, payload):
        events.append((name, payload))

    async def handler(run: TaskRun) -> None:
        if submission == "none":
            return
        node = await stack.storage.get_session(SOURCE_KEY)
        await stack.storage.create_plan_revision(
            new_plan_revision(
                source_session_key=SOURCE_KEY,
                source_session_id=node.session_id,
                source_epoch=node.epoch,
                source_turn_id=run.task_id if submission == "current_turn" else "other-turn",
                title="Revised proposal",
                markdown="Inspect and repair the synthetic example.",
                steps=[{"title": "Inspect"}, {"title": "Verify"}],
                parent=stack.source_revision,
            ),
            expected_parent_revision_id=stack.source_revision.revision_id,
        )

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers",
        _ignore_subscriber_event,
    )
    async with _open_plan_rpc_stack(tmp_path / "outcomes.sqlite", handler=handler) as stack:
        monkeypatch.setattr(stack.runtime, "_emit", capture)
        response = await _handle_plans_revise(
            {
                "sessionKey": SOURCE_KEY,
                "planRevisionId": stack.source_revision.revision_id,
                "prompt": "Discuss this proposal and revise it if needed.",
                "clientRequestId": "synthetic-plan-outcome",
            },
            stack.context,
        )
        terminal = await stack.runtime.wait(response["turn_id"], timeout=2)
        assert terminal.status == AgentTaskStatus.SUCCEEDED, terminal.error_message
        stored = await stack.storage.get_agent_task(response["turn_id"])
        result = stored.details["metadata"]["plan_result"]
        assert result["status"] == ("submitted" if submission == "current_turn" else "discussion")
        assert result["previousRevisionId"] == stack.source_revision.revision_id
        assert (
            result["revisionId"]
            == (await stack.storage.get_session(SOURCE_KEY)).active_plan_revision_id
        )
        assert _task_summary(stored)["plan_result"] == result
        terminal_events = [payload for name, payload in events if name == "task.succeeded"]
        assert len(terminal_events) == 1
        assert terminal_events[0]["plan_result"] == result
        revision_events = [
            payload for name, payload in events if name == "session.event.plan_revision"
        ]
        assert len(revision_events) == int(submission == "current_turn")
