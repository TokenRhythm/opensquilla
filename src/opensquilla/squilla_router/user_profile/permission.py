"""Build the replay-only permission snapshot stored with a user profile."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

_MODEL_KEYS = ("allow_models", "deny_models")
_RISK_KEY = "risk_allowlist"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple | set | frozenset):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def build_permission_snapshot(
    *,
    baseline: Mapping[str, Any],
    live_override: Mapping[str, Any] | None,
    allowed_tools: Iterable[str],
) -> dict[str, list[str]]:
    """Return the effective gateway permission block for replay.

    Model and risk policy start from the producer baseline and may be overlaid
    by a live operator permission. Tool permission is resolved by the gateway
    tool-policy layer before it reaches this helper. The producer only records
    the result; runtime authorization continues to read live policy.
    """

    snapshot = {key: _string_list(baseline.get(key)) for key in (*_MODEL_KEYS, _RISK_KEY)}
    if isinstance(live_override, Mapping):
        for key in (*_MODEL_KEYS, _RISK_KEY):
            if key in live_override:
                snapshot[key] = _string_list(live_override.get(key))
    snapshot["allow_tools"] = sorted(
        {str(tool).strip() for tool in allowed_tools if str(tool).strip()}
    )
    return snapshot


__all__ = ["build_permission_snapshot"]
