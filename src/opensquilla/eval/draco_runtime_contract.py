"""Shared deterministic DRACO runtime-fingerprint helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import urlparse


class _JsonModel(Protocol):
    def model_dump(self, *, mode: str) -> dict[str, Any]: ...


def canonical_json_sha256(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def _sanitize_url_for_fingerprint(value: str) -> str:
    try:
        parsed = urlparse(value)
    except ValueError:
        return "<configured>" if value else ""
    if not parsed.scheme or not parsed.hostname:
        return value
    host = parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return parsed._replace(netloc=host, query="", fragment="").geturl()


def _sanitize_fingerprint_config(value: Any, *, key: str = "") -> Any:
    normalized_key = key.casefold().replace("-", "_")
    if normalized_key.endswith("_env") or normalized_key.endswith("_env_pool"):
        return value
    if normalized_key in {
        "api_key",
        "authorization",
        "credential",
        "credentials",
        "password",
        "secret",
    } or normalized_key.endswith(("_api_key", "_password", "_secret")):
        return "<redacted>" if value else ""
    if isinstance(value, Mapping):
        return {
            str(item_key): _sanitize_fingerprint_config(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_fingerprint_config(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_fingerprint_config(item, key=key) for item in value]
    if normalized_key in {"base_url", "proxy"} and isinstance(value, str):
        return _sanitize_url_for_fingerprint(value)
    return value


def gateway_execution_contract(config: _JsonModel) -> dict[str, Any]:
    dumped = config.model_dump(mode="json")
    relevant = {
        key: dumped.get(key)
        for key in (
            "llm",
            "llm_profiles",
            "llm_ensemble",
            "model_catalog",
            "models",
            "squilla_router",
            "sandbox",
        )
    }
    return _sanitize_fingerprint_config(relevant)
