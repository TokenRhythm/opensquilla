"""Goal recovery policy survives the domain-to-RPC boundary unchanged."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from opensquilla.gateway.rpc import RpcHandlerError
from opensquilla.gateway.rpc_goals import (
    _handle_goals_reattach,
    _handle_goals_set,
    _translate_goal_errors,
)
from opensquilla.gateway.websocket import get_registry
from opensquilla.session.goals import GoalConflictError, GoalRecoveryAction
from tests.test_gateway.test_goal_rpc import (
    SOURCE_KEY,
    _goal_connection,
    _open_goal_rpc_stack,
    _reattach_params,
    _set_params,
)


@pytest.mark.parametrize(
    ("code", "retryable", "action", "expected_retryable", "expected_action"),
    [
        ("STALE_GOAL", None, None, True, "refresh-goal"),
        ("SESSION_GENERATION_CHANGED", None, None, True, "refresh-goal"),
        ("STALE_GOAL", False, GoalRecoveryAction.STOP, False, "stop"),
        ("EXECUTION_LEASE_REQUIRED", None, None, False, "stop"),
        (
            "EXECUTION_LEASE_REQUIRED",
            True,
            GoalRecoveryAction.AWAIT_SUBSCRIPTION,
            True,
            "await-subscription",
        ),
        (
            "EXECUTION_LEASE_REQUIRED",
            False,
            GoalRecoveryAction.REQUIRE_TAKEOVER,
            False,
            "require-takeover",
        ),
        (
            "EXECUTION_LEASE_REQUIRED",
            False,
            GoalRecoveryAction.OWNER_ATTACHED,
            False,
            "owner-attached",
        ),
        ("GOAL_AUTHORITY_UNAVAILABLE", True, GoalRecoveryAction.RETRY, True, "retry"),
        ("GOAL_AUTHORITY_UNAVAILABLE", False, GoalRecoveryAction.STOP, False, "stop"),
        ("GOAL_NOT_RESUMABLE", None, None, False, "stop"),
    ],
)
async def test_conflict_translation_preserves_explicit_recovery_policy(
    code,
    retryable,
    action,
    expected_retryable,
    expected_action,
):
    async def fail():
        raise GoalConflictError(
            code, "contract fixture", retryable=retryable, recovery_action=action
        )

    with pytest.raises(RpcHandlerError) as caught:
        await _translate_goal_errors(fail())
    error = caught.value
    assert error.code == code
    assert error.retryable is expected_retryable
    assert error.accepted is False
    assert error.details == {"recoveryAction": expected_action}


async def test_actual_rpc_missing_subscription_is_retryable_without_acceptance(tmp_path):
    async with _open_goal_rpc_stack(tmp_path / "subscription.sqlite", subscribe=False) as stack:
        with pytest.raises(RpcHandlerError) as caught:
            await _handle_goals_set(_set_params(), stack.context)
        assert caught.value.code == "EXECUTION_LEASE_REQUIRED"
        assert caught.value.retryable is True
        assert caught.value.accepted is False
        assert caught.value.details == {"recoveryAction": "await-subscription"}
        assert await stack.storage.get_goal(SOURCE_KEY) is None


@pytest.mark.parametrize("retryable", [False, True])
async def test_conflict_policy_survives_optional_snapshot_failure(retryable):
    async def fail():
        raise GoalConflictError(
            "STALE_GOAL",
            "known rejection",
            current=object(),
            retryable=retryable,
            recovery_action=GoalRecoveryAction.REFRESH_GOAL,
        )

    async def snapshot(_current):
        raise OSError("controlled snapshot dependency failure")

    with pytest.raises(RpcHandlerError) as caught:
        await _translate_goal_errors(fail(), service=SimpleNamespace(snapshot=snapshot))
    assert caught.value.code == "STALE_GOAL"
    assert caught.value.retryable is retryable
    assert caught.value.accepted is False
    assert caught.value.details == {"recoveryAction": "refresh-goal"}


@pytest.mark.parametrize("status", ["unavailable", "deny"])
async def test_actual_start_authority_outage_and_revocation_have_distinct_policies(
    tmp_path,
    monkeypatch,
    status,
):
    async with _open_goal_rpc_stack(tmp_path / "authority.sqlite") as stack:

        async def decision(_principal):
            return SimpleNamespace(status=status)

        monkeypatch.setattr(stack.service, "_authority_decision", decision)
        with pytest.raises(RpcHandlerError) as caught:
            await _handle_goals_set(_set_params(), stack.context)
        assert caught.value.code == "GOAL_AUTHORITY_UNAVAILABLE"
        assert caught.value.retryable is (status == "unavailable")
        assert caught.value.accepted is False
        assert caught.value.details == {
            "recoveryAction": "retry" if status == "unavailable" else "stop",
        }
        assert await stack.storage.get_goal(SOURCE_KEY) is None


async def test_actual_reattach_authority_read_outage_preserves_grant_and_retry_policy(
    tmp_path,
    monkeypatch,
):
    async with _open_goal_rpc_stack(tmp_path / "reattach-authority.sqlite") as stack:
        created = await _handle_goals_set(_set_params(), stack.context)
        await stack.runtime.wait(created["taskId"], timeout=10)
        original = stack.service._leases[SOURCE_KEY]

        async def unavailable(_principal):
            return "unavailable"

        monkeypatch.setattr(stack.service, "_principal_authority_state_async", unavailable)
        with pytest.raises(RpcHandlerError) as caught:
            await _handle_goals_reattach(
                _reattach_params(created, continuity_token=created["continuityToken"]),
                stack.context,
            )
        assert caught.value.code == "GOAL_AUTHORITY_UNAVAILABLE"
        assert caught.value.retryable is True
        assert caught.value.accepted is False
        assert caught.value.details == {"recoveryAction": "retry"}
        assert stack.service._leases[SOURCE_KEY] is original


async def test_actual_reattach_distinguishes_owner_from_invalid_detached_grant(tmp_path):
    async with _open_goal_rpc_stack(tmp_path / "continuity.sqlite") as stack:
        created = await _handle_goals_set(_set_params(), stack.context)
        await stack.runtime.wait(created["taskId"], timeout=10)
        # Even this same socket cannot explicitly take over an attached lease;
        # the suggested recovery action must match that command's eligibility.
        for params in (
            _reattach_params(created, continuity_token="invalid-token"),
            _reattach_params(created, takeover=True),
        ):
            with pytest.raises(RpcHandlerError) as caught:
                await _handle_goals_reattach(params, stack.context)
            assert caught.value.retryable is False
            assert caught.value.accepted is False
            assert caught.value.details == {"recoveryAction": "owner-attached"}
        spectator_id = "goal-recovery-contract-spectator"
        spectator = replace(stack.context, conn_id=spectator_id)
        get_registry().register(_goal_connection(spectator_id))
        stack.subscriptions.subscribe_messages(spectator_id, SOURCE_KEY)
        try:
            for params in (
                _reattach_params(created, continuity_token="invalid-token"),
                _reattach_params(created, continuity_token=created["continuityToken"]),
                _reattach_params(created, takeover=True),
            ):
                with pytest.raises(RpcHandlerError) as caught:
                    await _handle_goals_reattach(params, spectator)
                assert caught.value.retryable is False
                assert caught.value.accepted is False
                assert caught.value.details == {"recoveryAction": "owner-attached"}
                assert (
                    stack.service._leases[SOURCE_KEY].owner_connection_id == stack.context.conn_id
                )

            stack.subscriptions.unsubscribe_messages(stack.context.conn_id, SOURCE_KEY)
            await stack.service.on_subscription_lost(stack.context.conn_id, SOURCE_KEY)
            with pytest.raises(RpcHandlerError) as caught:
                await _handle_goals_reattach(
                    _reattach_params(created, continuity_token="invalid-token"),
                    spectator,
                )
            assert caught.value.retryable is False
            assert caught.value.details == {"recoveryAction": "require-takeover"}
            assert SOURCE_KEY not in stack.service._leases

            stale = _reattach_params(created, continuity_token=created["continuityToken"])
            stale["epoch"] += 1
            with pytest.raises(RpcHandlerError) as caught:
                await _handle_goals_reattach(stale, spectator)
            assert caught.value.details == {"recoveryAction": "refresh-goal"}

            # The explicit user action remains the only route to new authority.
            accepted = await _handle_goals_reattach(
                _reattach_params(created, takeover=True), spectator
            )
            assert accepted["accepted"] is True
            assert stack.service._leases[SOURCE_KEY].owner_connection_id == spectator_id
        finally:
            stack.subscriptions.unsubscribe_messages(spectator_id, SOURCE_KEY)
            get_registry().unregister(spectator_id)
