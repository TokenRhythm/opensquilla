"""Natural Goal controls retain live authority inside ordinary task fences."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from opensquilla.gateway.auth import resolve_auth
from opensquilla.gateway.rpc_sandbox import _handle_sandbox_token_revoke
from opensquilla.gateway.rpc_sessions import _handle_sessions_send_contract
from opensquilla.gateway.token_store import TokenStore
from opensquilla.gateway.websocket import get_registry
from opensquilla.session.goals import GoalConflictError
from opensquilla.session.models import AgentTaskStatus
from tests.test_gateway.test_goal_rpc import (
    _PRINCIPAL,
    SOURCE_KEY,
    _handle_goals_set,
    _open_goal_rpc_stack,
)


def _context(run):
    return SimpleNamespace(
        session_key=SOURCE_KEY, task_id=run.task_id,
        session_id=run.envelope.session_id, session_epoch=run.envelope.session_epoch,
        goal_context=None,
    )


async def _send(stack):
    return await _handle_sessions_send_contract(
        {"key": SOURCE_KEY, "message": "Complete the synthetic Goal task.",
         "clientRequestId": "goal-authority-task"}, stack.context,
    )


@pytest.mark.parametrize("operation", ["create", "edit"])
@pytest.mark.parametrize("revocation", ["disconnect", "downgrade", "unsubscribe"])
async def test_natural_goal_rechecks_owner_after_waiting_for_goal_lock(
    tmp_path, monkeypatch, operation, revocation,
):
    ready, proceed, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    rejected = []

    async def handler(run):
        ctx = _context(run)
        if operation == "edit":
            await stack.service.create_from_turn(ctx, objective="Original synthetic objective.")
        ready.set()
        await proceed.wait()
        try:
            if operation == "create":
                await stack.service.create_from_turn(ctx, objective="New synthetic objective.")
            else:
                await stack.service.update_from_turn(ctx, objective="New synthetic objective.")
        except GoalConflictError as exc:
            rejected.append(exc.code)

    async with _open_goal_rpc_stack(tmp_path / "authority.sqlite", handler=handler) as stack:
        sent = await _send(stack)
        await asyncio.wait_for(ready.wait(), timeout=2)
        original_lock = stack.service._lock

        @asynccontextmanager
        async def observed_lock(key):
            waiting.set()
            async with original_lock(key):
                yield

        async with original_lock(SOURCE_KEY):
            monkeypatch.setattr(stack.service, "_lock", observed_lock)
            proceed.set()
            await asyncio.wait_for(waiting.wait(), timeout=2)
            connection = get_registry().get(stack.context.conn_id)
            if revocation == "disconnect":
                get_registry().unregister(connection.conn_id)
            elif revocation == "downgrade":
                connection.principal = replace(connection.principal, scopes=frozenset())
            else:
                stack.subscriptions.unsubscribe_messages(connection.conn_id, SOURCE_KEY)
        task = await stack.runtime.wait(sent["task_id"], timeout=2)
        assert task.status == AgentTaskStatus.SUCCEEDED
        expected = (
            "EXECUTION_LEASE_REQUIRED" if revocation == "unsubscribe"
            else "GOAL_AUTHORITY_UNAVAILABLE"
        )
        assert rejected == [expected]
        goal = await stack.storage.get_goal(SOURCE_KEY)
        if operation == "create":
            assert goal is None
        else:
            assert goal.objective == "Original synthetic objective."
        assert len(await stack.storage.list_agent_tasks(session_key=SOURCE_KEY)) == 1


@pytest.mark.parametrize("revoked", [False, True])
async def test_natural_goal_write_failure_restores_only_current_authority(
    tmp_path, monkeypatch, revoked,
):
    checked = asyncio.Event()

    async def handler(run):
        ctx = _context(run)
        await stack.service.create_from_turn(ctx, objective="Original synthetic objective.")
        previous_context = dict(ctx.goal_context)
        previous_lease = stack.service._leases[SOURCE_KEY]

        async def failed_write(**_kwargs):
            assert stack.service._leases[SOURCE_KEY] is not previous_lease
            if revoked:
                connection = get_registry().get(stack.context.conn_id)
                connection.principal = replace(connection.principal, scopes=frozenset())
                get_registry().unregister(connection.conn_id)
            raise RuntimeError("Synthetic durable write failure")

        monkeypatch.setattr(stack.storage, "edit_goal", failed_write)
        with pytest.raises(RuntimeError, match="Synthetic durable write failure"):
            await stack.service.update_from_turn(ctx, objective="Rejected synthetic objective.")
        assert ctx.goal_context == previous_context
        assert stack.service._leases.get(SOURCE_KEY) is (None if revoked else previous_lease)
        assert stack.service._continuity_grants.get(SOURCE_KEY) is (
            None if revoked else previous_lease
        )
        goal = await stack.storage.get_goal(SOURCE_KEY)
        assert goal.objective == "Original synthetic objective."
        checked.set()

    async with _open_goal_rpc_stack(tmp_path / "rollback.sqlite", handler=handler) as stack:
        sent = await _send(stack)
        # This contract performs several real SQLite transactions. The wait is
        # a deadlock guard, not a two-second product-latency assertion.
        task = await stack.runtime.wait(sent["task_id"], timeout=10)
        assert task.status == AgentTaskStatus.SUCCEEDED
        assert checked.is_set()


async def test_cancel_waits_for_natural_goal_binding_under_same_task_fence(tmp_path, monkeypatch):
    writing, release_write, cancelling = asyncio.Event(), asyncio.Event(), asyncio.Event()
    never_finish = asyncio.Event()

    async def handler(run):
        await stack.service.create_from_turn(_context(run), objective="Synthetic Goal objective.")
        await never_finish.wait()

    async with _open_goal_rpc_stack(tmp_path / "cancel.sqlite", handler=handler) as stack:
        original_write = stack.storage.create_goal_for_running_task
        original_cancel = stack.runtime._cancel_runtime_tasks

        async def blocked_write(*args, **kwargs):
            writing.set()
            await release_write.wait()
            return await original_write(*args, **kwargs)

        async def observed_cancel(*args, **kwargs):
            cancelling.set()
            return await original_cancel(*args, **kwargs)

        monkeypatch.setattr(stack.storage, "create_goal_for_running_task", blocked_write)
        monkeypatch.setattr(stack.runtime, "_cancel_runtime_tasks", observed_cancel)
        sent = await _send(stack)
        await asyncio.wait_for(writing.wait(), timeout=2)
        cancel = asyncio.create_task(stack.runtime.cancel(task_id=sent["task_id"]))
        try:
            await asyncio.wait_for(cancelling.wait(), timeout=2)
            assert not cancel.done()
            assert await stack.storage.get_goal(SOURCE_KEY) is None
        finally:
            release_write.set()
        assert await asyncio.wait_for(cancel, timeout=2) == 1
        task = await stack.runtime.wait(sent["task_id"], timeout=2)
        assert task.status == AgentTaskStatus.CANCELLED
        goal = await stack.storage.get_goal(SOURCE_KEY)
        assert goal is not None and goal.active_task_id == sent["task_id"]
        assert len(await stack.storage.list_agent_tasks(session_key=SOURCE_KEY)) == 1


@pytest.mark.asyncio
async def test_named_token_revoke_at_activation_fence_blocks_goal_provider_dispatch(tmp_path):
    """Revocation after durable Goal acceptance must fail before the handler."""

    validator_entered = asyncio.Event()
    release_validator = asyncio.Event()
    provider_started = asyncio.Event()
    runs = []

    async def handler(run):
        runs.append(run)
        provider_started.set()

    async with _open_goal_rpc_stack(
        tmp_path / "goal-activation-revoke.sqlite", handler=handler,
    ) as stack:
        state = tmp_path / "auth"
        stack.context.config.state_dir = str(state)
        stack.context.config.auth.mode = "token"
        issued = TokenStore(state / "sessions.db").create(
            name="Goal activation owner",
            roles={"operator"},
            scopes={"operator.read", "operator.write"},
            capabilities={"host.execute", "task.read", "task.submit"},
        )
        principal = resolve_auth(
            stack.context.config,
            auth_params={"token": issued.token},
            role_claim="operator",
            peer_ip="192.168.1.7",
        )
        assert principal is not None and principal.authenticated
        stack.context.principal = principal
        connection = get_registry().get(stack.context.conn_id)
        assert connection is not None
        connection.principal = principal

        original_validator = stack.runtime._activation_authority_validator
        assert callable(original_validator)

        async def blocked_validator(task, snapshot):
            validator_entered.set()
            await release_validator.wait()
            return await original_validator(task, snapshot)

        stack.runtime.set_activation_authority_validator(blocked_validator)
        set_task = asyncio.create_task(
            _handle_goals_set(
                {
                    "sessionKey": SOURCE_KEY,
                    "objective": "Activation fence revoke",
                    "clientRequestId": "00000000-0000-4000-8000-000000000901",
                    "clientMessageId": "00000000-0000-4000-8000-000000000902",
                },
                stack.context,
            )
        )
        await asyncio.wait_for(validator_entered.wait(), timeout=3)
        owner_context = replace(stack.context, principal=_PRINCIPAL)
        revoked = await _handle_sandbox_token_revoke(
            {"publicId": issued.record.public_id}, owner_context,
        )
        assert revoked["revoked"] is True
        release_validator.set()
        response = await asyncio.wait_for(set_task, timeout=5)
        task = await stack.runtime.wait(response["taskId"], timeout=5)
        assert task.status == AgentTaskStatus.FAILED
        assert not provider_started.is_set()
        assert runs == []
