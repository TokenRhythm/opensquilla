"""Independent password and stateless session authentication for the preview dashboard."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

_CREDENTIAL_VERSION = "v1"
_SESSION_VERSION = "v1"
_CSRF_VERSION = "v1"
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_KEY_BYTES = 32
_SCRYPT_SALT_BYTES = 16
_MAX_PASSWORD_BYTES = 1024
_SESSION_NONCE_BYTES = 32
_MIN_SESSION_SECRET_BYTES = 32
_DEFAULT_SESSION_TTL_SECONDS = 8 * 60 * 60
_LOGIN_CSRF_TTL_SECONDS = 10 * 60
_MAX_CLOCK_SKEW_SECONDS = 60


class DashboardCredentialError(ValueError):
    """A dashboard-only credential is malformed or unsafe."""


@dataclass(frozen=True)
class ScryptCredential:
    """Parsed, versioned scrypt verifier stored independently of Gateway auth."""

    salt: bytes
    digest: bytes
    n: int = _SCRYPT_N
    r: int = _SCRYPT_R
    p: int = _SCRYPT_P

    def encode(self) -> str:
        return "$".join(
            (
                "scrypt",
                _CREDENTIAL_VERSION,
                str(self.n),
                str(self.r),
                str(self.p),
                _base64url_encode(self.salt),
                _base64url_encode(self.digest),
            )
        )

    @classmethod
    def parse(cls, encoded: str) -> ScryptCredential:
        if not isinstance(encoded, str) or len(encoded) > 512:
            raise DashboardCredentialError("dashboard credential is invalid")
        parts = encoded.split("$")
        if len(parts) != 7 or parts[0:2] != ["scrypt", _CREDENTIAL_VERSION]:
            raise DashboardCredentialError("dashboard credential is invalid")
        try:
            n, r, p = (int(value, 10) for value in parts[2:5])
            salt = _base64url_decode(parts[5])
            digest = _base64url_decode(parts[6])
        except (TypeError, ValueError):
            raise DashboardCredentialError("dashboard credential is invalid") from None
        if (n, r, p) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P):
            raise DashboardCredentialError("dashboard credential parameters are unsupported")
        if len(salt) != _SCRYPT_SALT_BYTES or len(digest) != _SCRYPT_KEY_BYTES:
            raise DashboardCredentialError("dashboard credential is invalid")
        return cls(salt=salt, digest=digest, n=n, r=r, p=p)


@dataclass(frozen=True)
class DashboardSession:
    """Verified session claims; deliberately contains no user or device identifier."""

    issued_at: int
    expires_at: int
    nonce: bytes


def hash_dashboard_password(password: str, *, salt: bytes | None = None) -> str:
    """Create a versioned dashboard-only scrypt verifier."""

    password_bytes = _password_bytes(password)
    resolved_salt = secrets.token_bytes(_SCRYPT_SALT_BYTES) if salt is None else salt
    if not isinstance(resolved_salt, bytes) or len(resolved_salt) != _SCRYPT_SALT_BYTES:
        raise DashboardCredentialError("dashboard credential salt is invalid")
    digest = _derive_scrypt(password_bytes, salt=resolved_salt)
    return ScryptCredential(salt=resolved_salt, digest=digest).encode()


def verify_dashboard_password(password: str, credential: ScryptCredential | str) -> bool:
    """Verify a password without consulting the Gateway's operator credential."""

    try:
        parsed = (
            credential
            if isinstance(credential, ScryptCredential)
            else ScryptCredential.parse(credential)
        )
        password_bytes = _password_bytes(password)
    except DashboardCredentialError:
        return False
    candidate = _derive_scrypt(password_bytes, salt=parsed.salt)
    return hmac.compare_digest(candidate, parsed.digest)


class DashboardAuth:
    """Issue and validate signed preview sessions and action-bound CSRF tokens."""

    def __init__(
        self,
        *,
        credential: ScryptCredential | str,
        session_secret: bytes,
        session_ttl_seconds: int = _DEFAULT_SESSION_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._credential = (
            credential
            if isinstance(credential, ScryptCredential)
            else ScryptCredential.parse(credential)
        )
        if not isinstance(session_secret, bytes) or len(session_secret) < _MIN_SESSION_SECRET_BYTES:
            raise DashboardCredentialError("dashboard session secret must be at least 32 bytes")
        if (
            type(session_ttl_seconds) is not int
            or session_ttl_seconds < 60
            or session_ttl_seconds > 7 * 24 * 60 * 60
        ):
            raise DashboardCredentialError("dashboard session lifetime is invalid")
        self._session_secret = session_secret
        self._session_ttl_seconds = session_ttl_seconds
        self._clock = clock

    @property
    def session_ttl_seconds(self) -> int:
        return self._session_ttl_seconds

    def authenticate(self, password: str) -> bool:
        return verify_dashboard_password(password, self._credential)

    def issue_session(self) -> str:
        issued_at = int(self._clock())
        expires_at = issued_at + self._session_ttl_seconds
        nonce = secrets.token_bytes(_SESSION_NONCE_BYTES)
        unsigned = ".".join(
            (
                _SESSION_VERSION,
                str(issued_at),
                str(expires_at),
                _base64url_encode(nonce),
            )
        )
        return f"{unsigned}.{self._sign(b'session', unsigned)}"

    def verify_session(self, token: str | None) -> DashboardSession | None:
        if not isinstance(token, str) or len(token) > 512:
            return None
        parts = token.split(".")
        if len(parts) != 5 or parts[0] != _SESSION_VERSION:
            return None
        unsigned = ".".join(parts[:4])
        if not hmac.compare_digest(parts[4], self._sign(b"session", unsigned)):
            return None
        try:
            if len(parts[1]) > 12 or len(parts[2]) > 12:
                return None
            issued_at = int(parts[1], 10)
            expires_at = int(parts[2], 10)
            nonce = _base64url_decode(parts[3])
        except ValueError:
            return None
        now = int(self._clock())
        if (
            len(nonce) != _SESSION_NONCE_BYTES
            or issued_at > now + _MAX_CLOCK_SKEW_SECONDS
            or expires_at <= now
            or expires_at - issued_at != self._session_ttl_seconds
        ):
            return None
        return DashboardSession(issued_at=issued_at, expires_at=expires_at, nonce=nonce)

    def session_csrf_token(self, session: DashboardSession, *, action: str) -> str:
        normalized_action = _validate_action(action)
        material = b"\0".join(
            (
                session.nonce,
                str(session.expires_at).encode("ascii"),
                normalized_action.encode("ascii"),
            )
        )
        signature = hmac.new(
            self._session_secret,
            b"csrf\0" + material,
            hashlib.sha256,
        ).digest()
        return f"{_CSRF_VERSION}.{_base64url_encode(signature)}"

    def verify_session_csrf(
        self,
        token: str | None,
        session: DashboardSession,
        *,
        action: str,
    ) -> bool:
        if not isinstance(token, str) or len(token) > 128:
            return False
        expected = self.session_csrf_token(session, action=action)
        return hmac.compare_digest(token, expected)

    def issue_login_csrf(self) -> str:
        expires_at = int(self._clock()) + _LOGIN_CSRF_TTL_SECONDS
        unsigned = ".".join(
            (
                _CSRF_VERSION,
                str(expires_at),
                _base64url_encode(secrets.token_bytes(_SESSION_NONCE_BYTES)),
            )
        )
        return f"{unsigned}.{self._sign(b'login-csrf', unsigned)}"

    def verify_login_csrf(self, cookie_token: str | None, submitted_token: str | None) -> bool:
        """Verify a short-lived signed login token and any available cookie.

        Some embedded browser surfaces intentionally suppress HttpOnly cookies.
        The dashboard route separately enforces same-origin POSTs, so the
        signed form token remains a valid synchronizer-token defense when that
        cookie is absent.  If a cookie is present it must still match exactly.
        """

        if not isinstance(submitted_token, str) or len(submitted_token) > 256:
            return False
        if cookie_token is not None and (
            not isinstance(cookie_token, str)
            or len(cookie_token) > 256
            or not hmac.compare_digest(cookie_token, submitted_token)
        ):
            return False
        parts = submitted_token.split(".")
        if len(parts) != 4 or parts[0] != _CSRF_VERSION:
            return False
        unsigned = ".".join(parts[:3])
        if not hmac.compare_digest(parts[3], self._sign(b"login-csrf", unsigned)):
            return False
        try:
            if len(parts[1]) > 12:
                return False
            expires_at = int(parts[1], 10)
            nonce = _base64url_decode(parts[2])
        except ValueError:
            return False
        now = int(self._clock())
        return (
            len(nonce) == _SESSION_NONCE_BYTES
            and now < expires_at <= now + _LOGIN_CSRF_TTL_SECONDS + _MAX_CLOCK_SKEW_SECONDS
        )

    def _sign(self, domain: bytes, value: str) -> str:
        signature = hmac.new(
            self._session_secret,
            domain + b"\0" + value.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return _base64url_encode(signature)


def _derive_scrypt(password: bytes, *, salt: bytes) -> bytes:
    try:
        return hashlib.scrypt(
            password,
            salt=salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            maxmem=128 * 1024 * 1024,
            dklen=_SCRYPT_KEY_BYTES,
        )
    except (OSError, ValueError) as exc:  # pragma: no cover - platform crypto failure
        raise DashboardCredentialError("dashboard credential could not be derived") from exc


def _password_bytes(password: str) -> bytes:
    if not isinstance(password, str):
        raise DashboardCredentialError("dashboard password is invalid")
    try:
        encoded = password.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise DashboardCredentialError("dashboard password is invalid") from None
    if not encoded or len(encoded) > _MAX_PASSWORD_BYTES:
        raise DashboardCredentialError("dashboard password is invalid")
    return encoded


def _validate_action(action: str) -> str:
    if (
        not isinstance(action, str)
        or not action
        or len(action) > 64
        or not all(
            character.isascii() and (character.isalnum() or character in "-_:")
            for character in action
        )
    ):
        raise ValueError("dashboard CSRF action is invalid")
    return action


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    if (
        not value
        or len(value) > 256
        or any(character not in _BASE64URL_CHARS for character in value)
    ):
        raise ValueError("invalid base64url value")
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


_BASE64URL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


__all__ = [
    "DashboardAuth",
    "DashboardCredentialError",
    "DashboardSession",
    "ScryptCredential",
    "hash_dashboard_password",
    "verify_dashboard_password",
]
