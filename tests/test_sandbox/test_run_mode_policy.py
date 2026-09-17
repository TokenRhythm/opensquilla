from __future__ import annotations

from types import SimpleNamespace

from opensquilla.sandbox.run_mode import RunMode
from opensquilla.sandbox.run_mode_policy import (
    allowed_run_modes_for_principal,
    coerce_run_mode_for_principal,
    default_run_mode_for_principal,
    hello_auth_payload,
    principal_payload,
    run_mode_allowed_for_principal,
    run_mode_policy_payload,
)


def _principal(
    is_owner: bool,
    authenticated: bool = True,
    capabilities: frozenset[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        role="operator",
        scopes=frozenset({"operator.read", "operator.write"}),
        is_owner=is_owner,
        authenticated=authenticated,
        capabilities=(
            frozenset({"host.execute"})
            if capabilities is None and is_owner
            else capabilities or frozenset()
        ),
        auth_state="authenticated" if authenticated else "guest",
        token_public_id=None,
    )


def test_owner_can_choose_full_and_defaults_to_full() -> None:
    principal = _principal(is_owner=True)

    assert allowed_run_modes_for_principal(principal) == (
        RunMode.SAFE,
        RunMode.FULL,
    )
    assert default_run_mode_for_principal(principal) == RunMode.FULL
    assert run_mode_allowed_for_principal(RunMode.SAFE, principal) is True
    assert run_mode_allowed_for_principal(RunMode.FULL, principal) is True
    assert run_mode_policy_payload(principal) == {
        "allowedRunModes": ["safe", "full"],
        "defaultRunMode": "full",
        "fullHostAccessDisabledReason": None,
    }


def test_authenticated_non_owner_with_host_capability_can_use_full_host_access() -> None:
    principal = _principal(
        is_owner=False,
        capabilities=frozenset({"host.execute"}),
    )

    assert allowed_run_modes_for_principal(principal) == (RunMode.SAFE, RunMode.FULL)
    assert default_run_mode_for_principal(principal) == RunMode.FULL
    assert run_mode_allowed_for_principal(RunMode.SAFE, principal) is True
    assert run_mode_allowed_for_principal(RunMode.FULL, principal) is True
    assert coerce_run_mode_for_principal(RunMode.FULL, principal) == RunMode.FULL
    assert run_mode_policy_payload(principal) == {
        "allowedRunModes": ["safe", "full"],
        "defaultRunMode": "full",
        "fullHostAccessDisabledReason": None,
    }


def test_unauthenticated_non_owner_uses_safe_policy() -> None:
    principal = _principal(is_owner=False, authenticated=False)

    assert allowed_run_modes_for_principal(principal) == (RunMode.SAFE,)
    assert default_run_mode_for_principal(principal) == RunMode.SAFE
    assert run_mode_allowed_for_principal(None, principal) is True
    assert coerce_run_mode_for_principal("full", principal) == RunMode.SAFE
    assert coerce_run_mode_for_principal("standard", principal) == RunMode.SAFE
    assert coerce_run_mode_for_principal(None, principal) == RunMode.SAFE
    assert principal_payload(principal) == {
        "role": "operator",
        "scopes": ["operator.read", "operator.write"],
        "capabilities": [],
        "isOwner": False,
        "authenticated": False,
        "authState": "guest",
        "tokenPublicId": None,
    }
    assert hello_auth_payload(principal) == {
        "principal": {
            "role": "operator",
            "scopes": ["operator.read", "operator.write"],
            "capabilities": [],
            "isOwner": False,
            "authenticated": False,
            "authState": "guest",
            "tokenPublicId": None,
        },
        "runModePolicy": {
            "allowedRunModes": ["safe"],
            "defaultRunMode": "safe",
            "fullHostAccessDisabledReason": "host_capability_required",
        },
    }


def test_truthy_non_boolean_owner_flag_does_not_grant_owner_policy() -> None:
    principal = _principal(is_owner=False)
    principal.is_owner = "false"

    assert allowed_run_modes_for_principal(principal) == (RunMode.SAFE,)
    assert default_run_mode_for_principal(principal) == RunMode.SAFE
    assert run_mode_allowed_for_principal(RunMode.FULL, principal) is False
    assert coerce_run_mode_for_principal(RunMode.FULL, principal) == RunMode.SAFE
    assert run_mode_policy_payload(principal) == {
        "allowedRunModes": ["safe"],
        "defaultRunMode": "safe",
        "fullHostAccessDisabledReason": "host_capability_required",
    }


def test_invalid_run_mode_is_not_allowed_and_fails_closed() -> None:
    owner = _principal(is_owner=True)
    non_owner = _principal(is_owner=False)

    assert run_mode_allowed_for_principal("nonsense", owner) is False
    assert coerce_run_mode_for_principal("nonsense", owner) == RunMode.SAFE
    assert run_mode_allowed_for_principal("nonsense", non_owner) is False
    assert coerce_run_mode_for_principal("nonsense", non_owner) == RunMode.SAFE
