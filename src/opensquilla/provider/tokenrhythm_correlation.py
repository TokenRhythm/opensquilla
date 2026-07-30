"""Host-gated request correlation for the official TokenRhythm API."""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from .types import ProviderRequestCorrelation

TOKENRHYTHM_SESSION_ID_HEADER = "X-OpenSquilla-Session-Id"
TOKENRHYTHM_TURN_ID_HEADER = "X-OpenSquilla-Turn-Id"
TOKENRHYTHM_EXECUTION_ID_HEADER = "X-OpenSquilla-Execution-Id"
TOKENRHYTHM_CALL_KIND_HEADER = "X-OpenSquilla-Call-Kind"

_TOKENRHYTHM_CORRELATION_HOSTS = frozenset(
    {
        "tokenrhythm.studio",
        "api.tokenrhythm.studio",
    }
)
_CORRELATION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_ABSENT_CORRELATION_IDS = frozenset({"none", "null", "unknown"})
_AUXILIARY_CALL_ROLES = frozenset(
    {
        "meta",
        "vision_gate",
        "session_flush",
        "media",
        "naming",
        "compaction",
        "image_generation",
        "other",
    }
)
_ENSEMBLE_CALL_PHASES = frozenset(
    {
        "proposer",
        "aggregator",
        "fallback_single",
    }
)
_PROVIDER_FALLBACK_SEGMENT = "provider_fallback"
_NETWORK_OBSERVABILITY_DISABLED_ENV = (
    "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY"
)
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_CALL_KIND_MAX_LENGTH = 96


def _safe_correlation_id(value: str | None) -> str:
    candidate = str(value or "").strip()
    if (
        not candidate
        or candidate.lower() in _ABSENT_CORRELATION_IDS
        or _CORRELATION_ID_RE.fullmatch(candidate) is None
    ):
        return ""
    return candidate


def _safe_call_kind(value: str | None) -> str:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > _CALL_KIND_MAX_LENGTH:
        return ""
    parts = candidate.split(".")
    if parts[-1:] == [_PROVIDER_FALLBACK_SEGMENT]:
        parts = parts[:-1]
    if parts in (["agent", "chat"], ["subagent", "chat"]):
        return candidate
    if (
        len(parts) == 2
        and parts[0] == "auxiliary"
        and parts[1] in _AUXILIARY_CALL_ROLES
    ):
        return candidate
    if (
        len(parts) == 3
        and parts[0] in {"agent", "subagent"}
        and parts[1] == "ensemble"
        and parts[2] in _ENSEMBLE_CALL_PHASES
    ):
        return candidate
    return ""


def is_tokenrhythm_correlation_target(
    provider_kind: str | None,
    base_url: str | None,
) -> bool:
    """Return whether correlation metadata may be sent to this provider origin."""

    if str(provider_kind or "").strip().lower() != "tokenrhythm":
        return False
    try:
        parsed = urlparse(str(base_url or "").strip())
        if (
            parsed.scheme.lower() != "https"
            or parsed.username is not None
            or parsed.password is not None
        ):
            return False
        host = (parsed.hostname or "").lower()
        return host in _TOKENRHYTHM_CORRELATION_HOSTS and parsed.port in {None, 443}
    except ValueError:
        return False


def tokenrhythm_correlation_headers(
    provider_kind: str | None,
    base_url: str | None,
    correlation: ProviderRequestCorrelation | None,
) -> dict[str, str]:
    """Build passive correlation headers for a trusted TokenRhythm request."""

    privacy_disabled = (
        os.environ.get(_NETWORK_OBSERVABILITY_DISABLED_ENV, "").strip().lower()
        in _TRUE_VALUES
    )
    if (
        privacy_disabled
        or correlation is None
        or not is_tokenrhythm_correlation_target(provider_kind, base_url)
    ):
        return {}

    candidates = (
        (
            TOKENRHYTHM_SESSION_ID_HEADER,
            _safe_correlation_id(correlation.session_id),
        ),
        (
            TOKENRHYTHM_TURN_ID_HEADER,
            _safe_correlation_id(correlation.turn_id),
        ),
        (
            TOKENRHYTHM_EXECUTION_ID_HEADER,
            _safe_correlation_id(correlation.execution_id),
        ),
        (
            TOKENRHYTHM_CALL_KIND_HEADER,
            _safe_call_kind(correlation.call_kind),
        ),
    )
    if any(not value for _header, value in candidates):
        return {}
    return dict(candidates)
