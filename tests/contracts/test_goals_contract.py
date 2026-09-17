from __future__ import annotations

import pytest

from opensquilla.contracts.adapters.goals_contract import (
    GoalsContractError,
    goals_capabilities_params_contract_errors,
    goals_reattach_params_contract_errors,
    goals_set_params_contract_errors,
    goals_status_params_contract_errors,
    validate_goals_capabilities_result,
    validate_goals_reattach_result,
    validate_goals_set_params,
    validate_goals_set_result,
    validate_goals_status_params,
    validate_goals_status_result,
)


def test_capabilities_observer_preserves_optional_and_legacy_request_shapes() -> None:
    assert goals_capabilities_params_contract_errors(None) == ()
    assert goals_capabilities_params_contract_errors({"session_key": "agent:demo"}) == ()
    assert goals_capabilities_params_contract_errors({"sessionKey": 1})
    assert goals_capabilities_params_contract_errors("legacy") == ()


def test_capabilities_result_requires_canonical_fields_and_keeps_extensions() -> None:
    result = validate_goals_capabilities_result(
        {
            "supported": True,
            "executionEnabled": False,
            "maxTurns": 50,
            "runtimeBudgetSeconds": 3600,
            "methods": ["goals.set"],
            "future": {"v": 1},
        }
    )
    assert result["methods"] == ["goals.set"]
    assert result["future"] == {"v": 1}


def test_capabilities_result_rejects_legacy_nested_shape_at_gateway_boundary() -> None:
    with pytest.raises(GoalsContractError, match="goals.capabilities"):
        validate_goals_capabilities_result(
            {"capabilities": {"executionEnabled": True}}
        )


def test_status_observer_reports_drift_without_raising() -> None:
    assert goals_status_params_contract_errors({"session_key": "agent:demo"}) == ()
    assert goals_status_params_contract_errors({"sessionKey": 1})
    assert goals_status_params_contract_errors(None) == ()


def test_set_observer_accepts_legacy_shape_and_reports_invalid_values() -> None:
    assert goals_set_params_contract_errors(
        {
            "session_key": "agent:demo",
            "message": "ship",
            "client_request_id": "550e8400-e29b-41d4-a716-446655440000",
            "client_message_id": "550e8400-e29b-41d4-a716-446655440001",
        }
    ) == ()
    assert goals_set_params_contract_errors({"sessionKey": "agent:demo"})


def test_status_accepts_legacy_session_key_alias() -> None:
    assert (
        validate_goals_status_params({"session_key": "agent:demo"})["session_key"] == "agent:demo"
    )


def test_status_result_preserves_nullable_goal_and_extensions() -> None:
    result = validate_goals_status_result(
        {
            "sessionKey": "agent:demo",
            "sessionId": "s1",
            "epoch": 2,
            "goal": None,
            "future": {"v": 1},
        }
    )
    assert result["goal"] is None
    assert result["future"] == {"v": 1}


def test_set_requires_uuid_v4_and_objective() -> None:
    with pytest.raises(GoalsContractError):
        validate_goals_set_params(
            {
                "sessionKey": "agent:demo",
                "objective": "ship",
                "clientRequestId": "bad",
                "clientMessageId": "bad",
            }
        )


def test_set_accepts_legacy_snake_case_aliases() -> None:
    result = validate_goals_set_params(
        {
            "session_key": "agent:demo",
            "message": "ship",
            "client_request_id": "550e8400-e29b-41d4-a716-446655440000",
            "client_message_id": "550e8400-e29b-41d4-a716-446655440001",
        }
    )
    assert result["message"] == "ship"


def test_set_accepts_camel_wire_response_and_unknown_fields() -> None:
    result = validate_goals_set_result(
        {
            "sessionKey": "agent:demo",
            "accepted": True,
            "goal": {"status": "active", "goalId": "g1"},
            "extra": "kept",
        }
    )
    assert result["goal"]["goalId"] == "g1"
    assert result["extra"] == "kept"


def test_set_rejects_missing_acceptance_shape() -> None:
    with pytest.raises(GoalsContractError, match="acceptance outcome"):
        validate_goals_set_result({"accepted": True})


def _reattach_result(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "accepted": True,
        "sessionKey": "agent:demo",
        "sessionId": "s1",
        "epoch": 2,
        "goal": {
            "status": "active",
            "goalId": "g1",
            "sessionKey": "agent:demo",
            "sessionId": "s1",
            "epoch": 2,
            "objective": "ship",
            "stateRevision": 1,
            "objectiveRevision": 1,
            "progressRevision": 0,
        },
        "continuityToken": "token",
    }
    result.update(overrides)
    return result


def test_reattach_observer_covers_token_and_takeover_shapes() -> None:
    base = {
        "sessionKey": "agent:demo",
        "sessionId": "s1",
        "epoch": 2,
        "expectedGoalId": "g1",
    }
    assert goals_reattach_params_contract_errors({**base, "continuityToken": "token"}) == ()
    assert goals_reattach_params_contract_errors({**base, "takeover": True}) == ()
    assert goals_reattach_params_contract_errors(base)
    assert goals_reattach_params_contract_errors({**base, "epoch": -1})
    assert goals_reattach_params_contract_errors({**base, "takeover": None})
    assert goals_reattach_params_contract_errors({**base, "continuityToken": ""})
    assert goals_reattach_params_contract_errors({
        "session_key": "agent:demo",
        "session_id": "s1",
        "session_epoch": 2,
        "expected_goal_id": "g1",
        "continuity_token": "token",
    }) == ()
    assert goals_reattach_params_contract_errors({
        "key": "agent:demo",
        "session_id": "s1",
        "session_epoch": 2,
        "expected_goal_id": "g1",
        "continuity_token": "token",
        "source_kind": "web",
    }) == ()
    assert goals_reattach_params_contract_errors("legacy") == ()


def test_reattach_result_preserves_aliases_and_unknown_extensions() -> None:
    result = validate_goals_reattach_result({
        "accepted": True,
        "session_key": "agent:demo",
        "session_id": "s1",
        "epoch": 2,
        "goal": {
            "status": "active",
            "goal_id": "g1",
            "session_key": "agent:demo",
            "session_id": "s1",
            "epoch": 2,
            "objective": "ship",
            "state_revision": 1,
            "objective_revision": 1,
            "progress_revision": 0,
        },
        "continuity_token": "token",
        "future": {"version": 2},
    })
    assert result["future"] == {"version": 2}


def test_reattach_result_requires_accepted_goal_and_lease_token() -> None:
    with pytest.raises(GoalsContractError, match="goals.reattach"):
        validate_goals_reattach_result(_reattach_result(accepted=False))
    with pytest.raises(GoalsContractError, match="goals.reattach"):
        validate_goals_reattach_result(_reattach_result(goal=None))
    with pytest.raises(GoalsContractError, match="goals.reattach"):
        validate_goals_reattach_result(_reattach_result(continuityToken=""))
    with pytest.raises(GoalsContractError, match="goals.reattach"):
        validate_goals_reattach_result(_reattach_result(sessionKey=None))


def test_reattach_result_requires_a_complete_fenced_goal_snapshot() -> None:
    with pytest.raises(GoalsContractError, match="goals.reattach"):
        validate_goals_reattach_result(_reattach_result(goal={"status": "active"}))
    with pytest.raises(GoalsContractError, match="goals.reattach"):
        validate_goals_reattach_result(_reattach_result(
            sessionKey="",
        ))
    with pytest.raises(GoalsContractError, match="goals.reattach"):
        validate_goals_reattach_result(_reattach_result(
            goal={
                "status": "active",
                "goalId": "g1",
                "goal_id": "g2",
                "sessionKey": "agent:demo",
                "sessionId": "s1",
                "epoch": 2,
                "objective": "ship",
                "stateRevision": 1,
                "objectiveRevision": 1,
                "progressRevision": 0,
            },
        ))


def test_reattach_result_accepts_complete_legacy_goal_aliases() -> None:
    result = validate_goals_reattach_result({
        "accepted": True,
        "session_key": " agent:demo ",
        "session_id": "s1",
        "epoch": 2,
        "goal": {
            "status": "active",
            "goal_id": "g1",
            "session_key": " agent:demo ",
            "session_id": "s1",
            "session_epoch": 2,
            "goal_text": "ship",
            "state_revision": 1,
            "objective_revision": 1,
            "progress_revision": 0,
        },
        "continuity_token": "token",
    })
    assert result["session_key"] == " agent:demo "


def test_reattach_result_matches_json_integer_semantics_across_python_parsers() -> None:
    result = _reattach_result()
    result["epoch"] = 2.0
    goal = result["goal"]
    assert isinstance(goal, dict)
    goal.update({
        "epoch": 2.0,
        "stateRevision": 1.0,
        "objectiveRevision": 1.0,
        "progressRevision": 0.0,
    })

    assert validate_goals_reattach_result(result)["accepted"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("sessionKey", "agent:other"),
        ("sessionId", "other-session"),
        ("epoch", 3),
    ),
)
def test_reattach_result_rejects_outer_and_nested_identity_mismatch(
    field: str,
    value: object,
) -> None:
    result = _reattach_result()
    goal = result["goal"]
    assert isinstance(goal, dict)
    goal[field] = value

    with pytest.raises(GoalsContractError, match="goals.reattach"):
        validate_goals_reattach_result(result)
