from __future__ import annotations

import opensquilla.gateway.websocket as websocket
from opensquilla.gateway.auth import Principal
from opensquilla.sandbox.run_mode_policy import hello_auth_payload


def test_owner_hello_auth_payload_allows_full_by_default() -> None:
    principal = Principal(
        role="operator",
        scopes=frozenset({"operator.read", "operator.write"}),
        is_owner=True,
        authenticated=True,
    )

    assert hello_auth_payload(principal) == {
        "principal": {
            "role": "operator",
            "scopes": ["operator.read", "operator.write"],
            "capabilities": ["host.execute", "host.read", "task.read", "task.submit"],
            "isOwner": True,
            "authenticated": True,
            "authState": "authenticated",
            "tokenPublicId": None,
        },
        "runModePolicy": {
            "allowedRunModes": ["safe", "full"],
            "defaultRunMode": "full",
            "fullHostAccessDisabledReason": None,
        },
    }


def test_unauthenticated_non_owner_hello_auth_payload_disables_full() -> None:
    principal = Principal(
        role="operator",
        scopes=frozenset({"operator.read"}),
        is_owner=False,
        authenticated=False,
    )

    assert hello_auth_payload(principal) == {
        "principal": {
            "role": "operator",
            "scopes": ["operator.read"],
            "capabilities": ["guest.safe"],
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


def test_guest_websocket_hello_returns_compatibility_session_key() -> None:
    principal = Principal(
        role="operator",
        scopes=frozenset({"operator.read"}),
        is_owner=False,
        authenticated=False,
        guest_owner_id="a" * 64,
        guest_session_key="osqg_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    )

    helper = getattr(websocket, "_websocket_hello_auth_payload", None)
    assert callable(helper), "WebSocket guest hello helper is not implemented"
    payload = helper(principal)

    assert payload["guestSessionKey"] == principal.guest_session_key
    assert payload["principal"]["guestOwnerId"] == principal.guest_owner_id


def test_owner_websocket_hello_never_echoes_guest_session_key() -> None:
    principal = Principal(
        role="operator",
        scopes=frozenset({"operator.admin"}),
        is_owner=True,
        authenticated=True,
    )

    helper = getattr(websocket, "_websocket_hello_auth_payload", None)
    assert callable(helper), "WebSocket guest hello helper is not implemented"
    payload = helper(principal)

    assert "guestSessionKey" not in payload
    assert payload["principal"]["guestOwnerId"] is None


def test_missing_and_invalid_named_token_have_identical_guest_hello_payloads() -> None:
    common = {
        "role": "operator",
        "scopes": frozenset({"operator.read", "operator.write"}),
        "is_owner": False,
        "authenticated": False,
        "guest_owner_id": "a" * 64,
        "guest_session_key": "osqg_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    }
    missing = Principal(**common, auth_state="guest")
    invalid = Principal(
        **common,
        auth_state="invalid",
        token_public_id="missing",
    )

    assert _payload(missing) == _payload(invalid)


def _payload(principal: Principal) -> dict:
    helper = getattr(websocket, "_websocket_hello_auth_payload", None)
    assert callable(helper)
    return helper(principal)
