"""Transport limits shared by OpenSquilla's Python Gateway clients."""

from __future__ import annotations

GATEWAY_CLIENT_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
GATEWAY_CLIENT_MAX_QUEUE = 1
GATEWAY_HISTORY_RESPONSE_BUDGET_BYTES = 768 * 1024
GATEWAY_HISTORY_MAX_RESPONSE_BUDGET_BYTES = 4 * 1024 * 1024
GATEWAY_HISTORY_RESPONSE_RETRY_CODES = frozenset(
    {
        "BOOTSTRAP_RESPONSE_TOO_LARGE",
        "HISTORY_RESPONSE_TOO_LARGE",
        "RESPONSE_BUDGET_EXCEEDED",
    }
)


def gateway_feature_methods(hello: object) -> frozenset[str] | None:
    """Return advertised RPC methods, or ``None`` when capabilities are unknown."""

    if not isinstance(hello, dict):
        return None
    features = hello.get("features")
    if not isinstance(features, dict):
        return None
    methods = features.get("methods")
    if not isinstance(methods, list) or not all(isinstance(method, str) for method in methods):
        return None
    return frozenset(methods)


__all__ = [
    "GATEWAY_CLIENT_MAX_MESSAGE_BYTES",
    "GATEWAY_CLIENT_MAX_QUEUE",
    "GATEWAY_HISTORY_MAX_RESPONSE_BUDGET_BYTES",
    "GATEWAY_HISTORY_RESPONSE_BUDGET_BYTES",
    "GATEWAY_HISTORY_RESPONSE_RETRY_CODES",
    "gateway_feature_methods",
]
