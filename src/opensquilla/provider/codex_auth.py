"""ChatGPT-account (Codex CLI) OAuth credential source.

The ``openai_codex`` provider authenticates with the operator's ChatGPT
subscription using the credentials the Codex CLI maintains at
``$CODEX_HOME/auth.json`` (default ``~/.codex/auth.json``): a Bearer access
token, a refresh token, and the ChatGPT account id. OpenSquilla reads that
file, refreshes the access token against ``auth.openai.com`` when the
backend rejects it, and persists refreshed tokens back — token values are
never logged.

A full in-app OAuth login flow is intentionally out of scope: ``codex
login`` owns credential creation; this module only consumes and refreshes.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog

from opensquilla.env import trust_env as _trust_env

log = structlog.get_logger(__name__)

# OAuth client id of the Codex CLI application; the stored refresh token was
# minted for this client, so refreshes must present the same id.
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_TOKEN_REFRESH_URL = "https://auth.openai.com/oauth/token"

_ACCOUNT_ID_JWT_CLAIM = "https://api.openai.com/auth"


class CodexAuthError(Exception):
    """Raised when ChatGPT credentials are missing, unreadable, or expired."""


@dataclass(frozen=True)
class CodexCredentials:
    """In-memory view of the Codex CLI's stored ChatGPT tokens."""

    access_token: str
    refresh_token: str = ""
    account_id: str = ""
    id_token: str = ""


def codex_auth_path() -> Path:
    """Return the Codex CLI auth file path (``$CODEX_HOME`` overrides)."""
    home = os.environ.get("CODEX_HOME", "").strip()
    base = Path(home).expanduser() if home else Path.home() / ".codex"
    return base / "auth.json"


def _jwt_claims(token: str) -> dict[str, Any]:
    """Best-effort decode of a JWT payload (no signature verification)."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _account_id_from_tokens(tokens: dict[str, Any]) -> str:
    explicit = str(tokens.get("account_id") or "").strip()
    if explicit:
        return explicit
    for key in ("id_token", "access_token"):
        claims = _jwt_claims(str(tokens.get(key) or ""))
        auth_claim = claims.get(_ACCOUNT_ID_JWT_CLAIM)
        if isinstance(auth_claim, dict):
            account_id = str(auth_claim.get("chatgpt_account_id") or "").strip()
            if account_id:
                return account_id
    return ""


def load_codex_credentials(path: Path | None = None) -> CodexCredentials:
    """Load ChatGPT tokens from the Codex CLI auth file.

    Raises ``CodexAuthError`` with an actionable message when the file or
    the ChatGPT token section is missing — the fix is always ``codex login``.
    """
    auth_path = path or codex_auth_path()
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CodexAuthError(
            f"No ChatGPT credentials at {auth_path}; run `codex login` to sign in."
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexAuthError(
            f"Could not read ChatGPT credentials at {auth_path}: {exc}"
        ) from exc

    tokens = payload.get("tokens")
    if not isinstance(tokens, dict) or not str(tokens.get("access_token") or "").strip():
        raise CodexAuthError(
            f"{auth_path} has no ChatGPT access token; run `codex login` to sign in."
        )
    return CodexCredentials(
        access_token=str(tokens["access_token"]).strip(),
        refresh_token=str(tokens.get("refresh_token") or "").strip(),
        account_id=_account_id_from_tokens(tokens),
        id_token=str(tokens.get("id_token") or "").strip(),
    )


def _persist_refreshed_tokens(auth_path: Path, refreshed: dict[str, Any]) -> None:
    """Merge refreshed token fields into auth.json atomically."""
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        tokens = {}
    for key in ("id_token", "access_token", "refresh_token"):
        value = refreshed.get(key)
        if isinstance(value, str) and value:
            tokens[key] = value
    payload["tokens"] = tokens
    payload["last_refresh"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    auth_path.parent.mkdir(parents=True, exist_ok=True)
    # ``mkstemp`` creates the temporary file securely and returns an open
    # descriptor. Keep writing through that descriptor so no path-based
    # close-and-reopen race is introduced. On POSIX, ``fchmod`` reasserts
    # the intended mode on the open file before any token bytes are written;
    # platforms without ``fchmod`` retain ``mkstemp``'s native protection.
    fd, tmp_name = tempfile.mkstemp(dir=str(auth_path.parent), prefix=".auth-")
    fd_owned = False
    should_unlink = True
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd_owned = True
            json.dump(payload, handle, indent=2)
        os.replace(tmp_name, auth_path)
        # Ownership transferred to ``auth_path``; don't unlink on
        # subsequent error.
        should_unlink = False
    except Exception:
        # If ``fdopen`` never took ownership of the descriptor, close
        # it ourselves so a pre-write failure doesn't leak the fd.
        if not fd_owned:
            try:
                os.close(fd)
            except OSError:
                pass
        if should_unlink:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise


async def refresh_codex_credentials(
    credentials: CodexCredentials,
    *,
    path: Path | None = None,
    refresh_url: str = CODEX_TOKEN_REFRESH_URL,
    proxy: str | None = None,
) -> CodexCredentials:
    """Exchange the refresh token for a new access token and persist it."""
    if not credentials.refresh_token:
        raise CodexAuthError(
            "ChatGPT access token was rejected and no refresh token is stored; "
            "run `codex login` to sign in again."
        )
    auth_path = path or codex_auth_path()
    async with httpx.AsyncClient(
        timeout=30.0, trust_env=_trust_env(), proxy=proxy or None
    ) as client:
        response = await client.post(
            refresh_url,
            json={
                "client_id": CODEX_OAUTH_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": credentials.refresh_token,
            },
        )
    if response.status_code != 200:
        raise CodexAuthError(
            f"ChatGPT token refresh failed (HTTP {response.status_code}); "
            "run `codex login` to sign in again."
        )
    try:
        refreshed = response.json()
    except json.JSONDecodeError as exc:
        raise CodexAuthError("ChatGPT token refresh returned invalid JSON.") from exc
    if not isinstance(refreshed, dict) or not str(refreshed.get("access_token") or "").strip():
        raise CodexAuthError("ChatGPT token refresh returned no access token.")

    _persist_refreshed_tokens(auth_path, refreshed)
    log.info("codex_auth.token_refreshed", path=str(auth_path))
    updated = replace(
        credentials,
        access_token=str(refreshed["access_token"]).strip(),
        refresh_token=str(refreshed.get("refresh_token") or credentials.refresh_token).strip(),
        id_token=str(refreshed.get("id_token") or credentials.id_token).strip(),
    )
    if not updated.account_id:
        updated = replace(
            updated,
            account_id=_account_id_from_tokens(
                {"id_token": updated.id_token, "access_token": updated.access_token}
            ),
        )
    return updated
