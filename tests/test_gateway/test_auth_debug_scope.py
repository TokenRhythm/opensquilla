from __future__ import annotations

import pytest

from opensquilla.gateway.auth import OpenScopeResolver
from opensquilla.gateway.config import AuthConfig, GatewayConfig
from opensquilla.gateway.scopes import (
    CLI_DEFAULT_OPERATOR_SCOPES,
    PROPOSALS_SCOPE,
    REMOTE_OPERATOR_SCOPES,
)


def test_open_auth_loopback_operator_gets_local_owner_scopes_when_debug_false() -> None:
    principal = OpenScopeResolver().resolve(
        {},
        "operator",
        GatewayConfig(debug=False, host="127.0.0.1"),
        peer_ip="127.0.0.1",
    )

    assert principal.scopes == CLI_DEFAULT_OPERATOR_SCOPES
    assert principal.is_owner is True
    assert principal.authenticated is False


def test_open_auth_loopback_is_not_blocked_by_remote_client_cidr_filter() -> None:
    principal = OpenScopeResolver().resolve(
        {},
        "operator",
        GatewayConfig(
            debug=False,
            host="127.0.0.1",
            auth=AuthConfig(allowed_client_cidrs=["192.168.2.10/32"]),
        ),
        peer_ip="127.0.0.1",
    )

    assert principal.scopes == CLI_DEFAULT_OPERATOR_SCOPES
    assert principal.is_owner is True


def test_open_auth_exposed_operator_gets_remote_scopes_when_debug_false() -> None:
    principal = OpenScopeResolver().resolve(
        {},
        "operator",
        GatewayConfig(debug=False, host="0.0.0.0"),
        peer_ip="127.0.0.1",
    )

    assert principal.scopes == REMOTE_OPERATOR_SCOPES
    assert PROPOSALS_SCOPE not in principal.scopes
    assert principal.is_owner is False
    assert principal.authenticated is False


def test_open_auth_public_peer_is_rejected_even_in_debug_mode() -> None:
    configured_scopes = ["operator.write"]
    with pytest.raises(ValueError, match="Public peers"):
        OpenScopeResolver().resolve(
            {},
            "operator",
            GatewayConfig(
                debug=True,
                host="0.0.0.0",
                auth=AuthConfig(token_scopes=configured_scopes),
            ),
            peer_ip="203.0.113.7",
        )


def test_debug_mode_does_not_upgrade_private_remote_guest_scopes() -> None:
    principal = OpenScopeResolver().resolve(
        {},
        "operator",
        GatewayConfig(
            debug=True,
            host="0.0.0.0",
            auth=AuthConfig(token_scopes=["operator.admin", "operator.approvals"]),
        ),
        peer_ip="192.168.1.42",
    )

    assert principal.scopes == REMOTE_OPERATOR_SCOPES
    assert "operator.admin" not in principal.scopes
    assert "operator.approvals" not in principal.scopes
    assert principal.is_owner is False
