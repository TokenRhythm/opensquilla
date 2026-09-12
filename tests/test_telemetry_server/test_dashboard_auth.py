from __future__ import annotations

import pytest

from opensquilla.telemetry.server.auth import (
    DashboardAuth,
    DashboardCredentialError,
    ScryptCredential,
    hash_dashboard_password,
    verify_dashboard_password,
)


def _auth(*, now: list[float] | None = None) -> DashboardAuth:
    clock = now if now is not None else [1_800_000_000.0]
    credential = hash_dashboard_password("correct horse", salt=b"s" * 16)
    return DashboardAuth(
        credential=credential,
        session_secret=b"k" * 32,
        session_ttl_seconds=3600,
        clock=lambda: clock[0],
    )


def test_scrypt_credential_is_versioned_and_password_is_never_embedded() -> None:
    encoded = hash_dashboard_password("correct horse", salt=b"s" * 16)

    assert encoded.startswith("scrypt$v1$16384$8$1$")
    assert "correct horse" not in encoded
    assert ScryptCredential.parse(encoded).encode() == encoded
    assert verify_dashboard_password("correct horse", encoded) is True
    assert verify_dashboard_password("wrong horse", encoded) is False


@pytest.mark.parametrize(
    "credential",
    (
        "",
        "scrypt$v2$16384$8$1$bad$bad",
        "scrypt$v1$2$8$1$bad$bad",
        "scrypt$v1$16384$8$1$***$***",
    ),
)
def test_malformed_or_weak_credentials_fail_closed(credential: str) -> None:
    with pytest.raises(DashboardCredentialError):
        ScryptCredential.parse(credential)
    assert verify_dashboard_password("candidate", credential) is False


def test_session_is_signed_expires_and_contains_no_identity() -> None:
    now = [1_800_000_000.0]
    auth = _auth(now=now)

    token = auth.issue_session()
    session = auth.verify_session(token)

    assert session is not None
    assert session.issued_at == 1_800_000_000
    assert session.expires_at == 1_800_003_600
    assert not hasattr(session, "user_id")
    assert auth.verify_session(token + "tampered") is None

    now[0] = 1_800_003_600.0
    assert auth.verify_session(token) is None


def test_session_csrf_is_bound_to_session_and_action() -> None:
    auth = _auth()
    first = auth.verify_session(auth.issue_session())
    second = auth.verify_session(auth.issue_session())
    assert first is not None and second is not None

    token = auth.session_csrf_token(first, action="logout")

    assert auth.verify_session_csrf(token, first, action="logout") is True
    assert auth.verify_session_csrf(token, first, action="refresh") is False
    assert auth.verify_session_csrf(token, second, action="logout") is False


def test_login_csrf_requires_matching_cookie_signature_and_freshness() -> None:
    now = [1_800_000_000.0]
    auth = _auth(now=now)
    token = auth.issue_login_csrf()

    assert auth.verify_login_csrf(token, token) is True
    assert auth.verify_login_csrf(token, token + "x") is False
    # Embedded browsers may suppress the HttpOnly double-submit cookie. The
    # route still requires same-origin and this token is signed and expiring.
    assert auth.verify_login_csrf(None, token) is True

    now[0] += 601
    assert auth.verify_login_csrf(token, token) is False


def test_short_session_secret_is_rejected() -> None:
    credential = hash_dashboard_password("correct horse", salt=b"s" * 16)
    with pytest.raises(DashboardCredentialError, match="at least 32 bytes"):
        DashboardAuth(credential=credential, session_secret=b"short")
