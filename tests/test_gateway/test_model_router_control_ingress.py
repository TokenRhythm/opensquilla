"""Structured model controls retain their accepted causal input identity."""

from pathlib import Path

import pytest

from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.session.model_router_state import build_model_router_context
from tests.test_gateway.test_turn_ingress_rpc import SESSION_KEY, _open_real_stack


@pytest.mark.parametrize("control", ["new_task", "redo"])
@pytest.mark.parametrize(
    "method,key_field", [("chat.send", "sessionKey"), ("sessions.send", "key")],
)
@pytest.mark.parametrize("queue_mode", ["followup", "collect", "steer"])
async def test_router_control_survives_acceptance_activation_and_replay(
    tmp_path: Path, control: str, method: str, key_field: str, queue_mode: str,
) -> None:
    async with _open_real_stack(tmp_path / "control.db") as stack:
        params = {
            key_field: SESSION_KEY, "message": "Use this exact request",
            "clientRequestId": "control-request", "clientMessageId": "control-message",
            "routerControlEvent": control, "queueMode": queue_mode,
        }
        accepted = await get_dispatcher().dispatch("accept", method, params, stack.context)
        assert accepted.ok, accepted.error
        await stack.wait_until_running()
        entries = await stack.storage.get_canonical_transcript(stack.session_id)
        users = [entry for entry in entries if entry.role == "user"]
        assert len(users) == 1
        assert users[0].turn_context["router_model_control_event"] == control
        router_context = build_model_router_context(
            entries, bound_user_message_id=users[0].message_id,
            exclude_last_user=True,
        )
        assert router_context["router_model_control_event"] == control
        task = await stack.storage.get_agent_task(accepted.payload["task_id"])
        assert task.queue_mode == "followup"
        assert task.details["metadata"]["router_model_control_event"] == control
        assert stack.received_runs[0].envelope.metadata["router_model_control_event"] == control
        replay = await get_dispatcher().dispatch("replay", method, params, stack.context)
        assert replay.ok, replay.error
        assert replay.payload["replayed"] is True
        changed = await get_dispatcher().dispatch(
            "changed", method,
            {**params, "routerControlEvent": "redo" if control == "new_task" else "new_task"},
            stack.context,
        )
        assert not changed.ok
        assert changed.error.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("control", ["new_task", "redo"])
async def test_pending_router_control_survives_staging_and_dispatch(tmp_path: Path, control: str):
    async with _open_real_stack(tmp_path / "pending-control.db") as stack:
        staged = await get_dispatcher().dispatch(
            "stage", "sessions.pending_inputs.enqueue", {
                "key": SESSION_KEY, "message": "Use this queued request",
                "clientRequestId": "pending-control-request", "pendingInputId": "pending-control",
                "clientMessageId": "pending-control-message",
                "router_control_event": control,
            }, stack.context,
        )
        assert staged.ok, staged.error
        assert staged.payload["routerControlEvent"] == control
        accepted = await get_dispatcher().dispatch(
            "dispatch", "sessions.pending_inputs.dispatch", {
                "key": SESSION_KEY, "pendingInputId": "pending-control",
                "clientRequestId": "pending-control-request",
                "requestFingerprint": staged.payload["requestFingerprint"],
            }, stack.context,
        )
        assert accepted.ok, accepted.error
        await stack.wait_until_running()
        entries = await stack.storage.get_canonical_transcript(stack.session_id)
        users = [entry for entry in entries if entry.role == "user"]
        assert len(users) == 1
        assert users[0].turn_context["router_model_control_event"] == control
        router_context = build_model_router_context(
            entries, bound_user_message_id=users[0].message_id,
            exclude_last_user=True,
        )
        assert router_context["router_model_control_event"] == control
        assert stack.received_runs[0].envelope.metadata["router_model_control_event"] == control


@pytest.mark.parametrize("control", ["new_task", "redo"])
async def test_router_control_does_not_leak_to_cached_system_followup(tmp_path: Path, control: str):
    async with _open_real_stack(tmp_path / "cached-control.db") as stack:
        accepted = await get_dispatcher().dispatch(
            "accept", "chat.send", {
                "sessionKey": SESSION_KEY, "message": "Own this control",
                "clientRequestId": "control-request", "routerControlEvent": control,
            }, stack.context,
        )
        assert accepted.ok, accepted.error
        await stack.wait_until_running()
        followup = await stack.runtime.send(SESSION_KEY, "Independent system followup")
        task = await stack.storage.get_agent_task(followup.task_id)
        assert "router_model_control_event" not in task.details["metadata"]
