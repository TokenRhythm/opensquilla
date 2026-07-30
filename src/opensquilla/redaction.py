"""Dependency-free free-text redaction primitives.

This module must stay stdlib-only. Low-level modules such as
``opensquilla.provider.failures`` import it, and ``opensquilla.onboarding``
re-exports it: importing ``opensquilla.onboarding`` transitively imports the
``opensquilla.provider`` package (via the setup specs and gateway config), so
hosting this helper inside ``onboarding`` would create an import cycle.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_MASK = "***"

# ``scheme://user:password@host`` — mask the whole userinfo component.
_URL_USERINFO_RE = re.compile(r"(?<=://)[^\s/@]{1,256}@")
# ``Bearer <token>`` in any casing.
_BEARER_RE = re.compile(r"\bbearer\s+[a-z0-9._~+/=-]+", re.IGNORECASE)
# ``key=...`` / ``api_key: ...`` / ``token=...`` query or body values. The
# value must not itself be "bearer" (the bearer pass already masked its token
# and must keep the scheme word readable) or an existing mask.
_KEY_VALUE_RE = re.compile(
    r"""
    (   \b(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|key|
             secret|password|passwd|authorization|credentials?)\b
        \s*[=:]\s*["']?
    )
    (?!\*)(?!bearer\b)([^\s&"',;]+)
    """,
    re.IGNORECASE | re.VERBOSE,
)
# ``sk-``-style secret key tokens (sk-live-..., sk-proj-..., sk-test-000).
_SK_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{3,}")
# Long unbroken base64/hex-shaped runs are secret-shaped; when unsure, mask.
_LONG_RUN_RE = re.compile(r"[A-Za-z0-9+/=_-]{21,}")

# Regex passes are bounded to a prefix comfortably larger than the final
# truncation window, so pathological inputs stay cheap while any secret that
# could survive into the output is still seen (and masked) in full.
_MIN_SCAN_WINDOW = 2048


def redact_error_text(
    text: str,
    *,
    max_len: int = 200,
    known_secrets: Iterable[str] = (),
) -> str:
    """Truncate ``text`` and mask credential-shaped material for safe logging.

    Masks exact ``known_secrets`` first, then bearer tokens, ``sk-``-style
    keys, ``key=``/``api_key=``/``token=`` style values, URL userinfo, and
    long unbroken base64/hex runs. Exact replacement protects provider keys
    whose shape is not recognizable (including short or ``AIza``-prefixed
    credentials) when an upstream echoes them in an error. The policy is
    deliberately conservative: secret-shaped runs are masked even when they
    might be innocuous identifiers.
    """
    if not text:
        return ""
    secrets = tuple(
        sorted(
            {secret for secret in known_secrets if isinstance(secret, str) and secret},
            key=len,
            reverse=True,
        )
    )
    scan_window = max(_MIN_SCAN_WINDOW, max_len * 4)
    # Include one maximum secret length beyond the scan boundary so a secret
    # beginning inside the bounded prefix cannot be split before replacement.
    out = text[: scan_window + max((len(secret) for secret in secrets), default=0)]
    for secret in secrets:
        out = out.replace(secret, _MASK)
    out = _URL_USERINFO_RE.sub(f"{_MASK}@", out)
    out = _BEARER_RE.sub(f"Bearer {_MASK}", out)
    out = _KEY_VALUE_RE.sub(rf"\g<1>{_MASK}", out)
    out = _SK_KEY_RE.sub(_MASK, out)
    out = _LONG_RUN_RE.sub(_MASK, out)
    if len(out) > max_len:
        out = out[: max(0, max_len - 1)].rstrip() + "…"
    return out
