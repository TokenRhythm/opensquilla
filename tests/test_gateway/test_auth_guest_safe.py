from __future__ import annotations

import hashlib

from opensquilla.gateway.auth import resolve_auth
from opensquilla.gateway.config import AuthConfig, GatewayConfig


def _token_config(tmp_path, *, token: str = "correct") -> GatewayConfig:
    return GatewayConfig(
        host="0.0.0.0",
        state_dir=str(tmp_path),
        auth=AuthConfig(mode="token", token=token),
    )


def test_missing_and_invalid_token_have_same_guest_execution_authority(tmp_path) -> None:
    config = _token_config(tmp_path)

    missing = resolve_auth(
        config,
        auth_params={},
        role_claim="operator",
        peer_ip="192.168.1.7",
    )
    invalid = resolve_auth(
        config,
        auth_params={"token": "wrong"},
        role_claim="operator",
        peer_ip="192.168.1.7",
    )

    assert missing is not None
    assert invalid is not None
    assert missing.capabilities == invalid.capabilities == frozenset({"guest.safe"})
    assert missing.scopes == invalid.scopes
    assert missing.auth_state == "guest"
    assert invalid.auth_state == "invalid"
    assert missing.authenticated is invalid.authenticated is False
    assert "operator.approvals" not in missing.scopes
    assert "operator.approvals" not in invalid.scopes


def test_guest_owner_id_is_derived_from_shared_browser_key(tmp_path) -> None:
    config = _token_config(tmp_path)
    guest_session_key = "osqg_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

    missing = resolve_auth(
        config,
        auth_params={"guestSessionKey": guest_session_key},
        role_claim="operator",
        peer_ip="192.168.1.7",
    )
    invalid = resolve_auth(
        config,
        auth_params={
            "token": "osq_missing_ABCDEFGHIJKLMNOP",
            "guestSessionKey": guest_session_key,
        },
        role_claim="operator",
        peer_ip="192.168.1.7",
    )

    assert missing is not None
    assert invalid is not None
    assert missing.guest_owner_id == invalid.guest_owner_id
    assert len(missing.guest_owner_id or "") == 64
    assert missing.guest_session_key == invalid.guest_session_key == guest_session_key
    assert "host.execute" not in missing.capabilities
    assert "host.execute" not in invalid.capabilities


def test_missing_browser_key_gets_compatible_server_generated_key(tmp_path) -> None:
    principal = resolve_auth(
        _token_config(tmp_path),
        auth_params={},
        role_claim="operator",
        peer_ip="192.168.1.7",
    )

    assert principal is not None
    assert principal.guest_session_key.startswith("osqg_")
    assert principal.guest_owner_id
    assert principal.guest_session_key not in repr(principal)


def test_open_auth_compatibility_key_matches_derived_owner_id(tmp_path) -> None:
    config = GatewayConfig(
        host="0.0.0.0",
        state_dir=str(tmp_path),
        auth=AuthConfig(mode="none"),
    )

    principal = resolve_auth(
        config,
        auth_params={},
        role_claim="operator",
        peer_ip="192.168.1.7",
    )

    assert principal is not None
    expected_owner_id = hashlib.sha256(
        principal.guest_session_key.encode("utf-8")
    ).hexdigest()
    assert principal.guest_owner_id == expected_owner_id


def test_authenticated_token_never_uses_guest_key_for_authority(tmp_path) -> None:
    principal = resolve_auth(
        _token_config(tmp_path),
        auth_params={
            "token": "correct",
            "guestSessionKey": "osqg_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        },
        role_claim="operator",
        peer_ip="192.168.1.7",
    )

    assert principal is not None
    assert principal.guest_owner_id is None
    assert principal.guest_session_key is None
    assert "host.execute" in principal.capabilities


def test_valid_legacy_operator_token_receives_host_execute(tmp_path) -> None:
    principal = resolve_auth(
        _token_config(tmp_path),
        auth_params={"token": "correct"},
        role_claim="operator",
        peer_ip="192.168.1.7",
    )

    assert principal is not None
    assert principal.auth_state == "authenticated"
    assert principal.authenticated is True
    assert "host.execute" in principal.capabilities
    assert "guest.safe" not in principal.capabilities


def test_missing_token_from_public_peer_is_rejected(tmp_path) -> None:
    principal = resolve_auth(
        _token_config(tmp_path),
        auth_params={},
        role_claim="operator",
        peer_ip="203.0.113.7",
    )

    assert principal is None


def test_allowed_client_cidrs_can_narrow_lan_access(tmp_path) -> None:
    config = GatewayConfig(
        host="0.0.0.0",
        state_dir=str(tmp_path),
        auth=AuthConfig(
            mode="token",
            token="correct",
            allowed_client_cidrs=["192.168.50.0/24"],
        ),
    )

    accepted = resolve_auth(
        config,
        auth_params={"token": "correct"},
        role_claim="operator",
        peer_ip="192.168.50.7",
    )
    rejected = resolve_auth(
        config,
        auth_params={"token": "correct"},
        role_claim="operator",
        peer_ip="192.168.51.7",
    )

    assert accepted is not None
    assert rejected is None
