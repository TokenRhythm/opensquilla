"""Best-effort redaction for secret-looking text before persistence or LLM replay."""

from __future__ import annotations

import re
from typing import Any

_REDACTED = "[REDACTED]"

_SECRET_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-or-v1-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    # TokenRhythm keys use underscores (sk_tr_...), invisible to the
    # hyphen-anchored patterns above. The tail class includes -_ like the
    # sk- pattern's so a key abutting punctuation over-masks instead of
    # failing the trailing \b and leaking whole.
    re.compile(r"\bsk_tr_[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
)
# Matches an Authorization / Proxy-Authorization header for ANY scheme (Bearer,
# Basic, Digest, Negotiate, NTLM, token, ...) and masks the entire credential
# after the header, up to a line boundary. A known scheme word is preserved for
# readability (optional group 2); an unknown/absent scheme is masked whole. The
# prior Bearer-only regex left Basic/Digest credentials (which contain '=', ' ',
# '"') exposed.
_AUTH_HEADER_RE = re.compile(
    r"(?i)((?:proxy-)?authorization\s*:\s*)"
    r"((?:bearer|basic|digest|negotiate|ntlm|token)\s+)?"
    r"[^\r\n]+"
)


def _redact_auth_header(match: re.Match[str]) -> str:
    header = match.group(1)
    scheme = match.group(2) or ""
    return f"{header}{scheme}{_REDACTED}"
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]+)\s*([:=])\s*([^\s,;'\")}\]]+)"
)
# Second pass: values that start with a quote are invisible to the pattern above
# (its value class excludes quotes so nested assignments inside quoted strings can
# still be redacted individually). Match whole quoted values here so
# ``password: "hunter2"`` is masked; the trailing ``["']\S*`` arm covers
# unterminated quotes.
_SECRET_QUOTED_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]+)\s*([:=])\s*"
    r"(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[\"']\S*)"
)

_SECRET_KEY_PARTS = (
    "authorization",
    "api-key",
    "apikey",
    "api_key",
    "x-api-key",
    "password",
    "secret",
    "credential",
)
_SECRET_KEY_EXACT = {
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "bearer_token",
}


def is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in _SECRET_KEY_EXACT or any(part in lowered for part in _SECRET_KEY_PARTS)


def _is_secret_assignment_key(key: str) -> bool:
    lowered = key.lower()
    if lowered == "authorization":
        return False
    return is_secret_key(lowered) or lowered.endswith(("token", ".token", "_token", "-token"))


def _redact_assignment(match: re.Match[str]) -> str:
    key, separator = match.group(1), match.group(2)
    if not _is_secret_assignment_key(key):
        return match.group(0)
    return f"{key}{separator}{_REDACTED}"


def redact_secret_text(text: str) -> str:
    redacted = text
    redacted = _AUTH_HEADER_RE.sub(_redact_auth_header, redacted)
    redacted = _SECRET_ASSIGNMENT_RE.sub(_redact_assignment, redacted)
    redacted = _SECRET_QUOTED_ASSIGNMENT_RE.sub(_redact_assignment, redacted)
    for pattern in _SECRET_TOKEN_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted


def redact_secret_value(value: Any, *, key: str | None = None) -> Any:
    if key and is_secret_key(key):
        return _REDACTED
    if isinstance(value, str):
        return redact_secret_text(value)
    if isinstance(value, dict):
        return {str(k): redact_secret_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_secret_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secret_value(item) for item in value)
    return value
