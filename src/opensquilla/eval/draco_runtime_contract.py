"""Shared deterministic DRACO runtime-fingerprint helpers."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
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


def validate_formal_openrouter_runtime_transport(
    config: Any,
    *,
    resolve_runtime_config: Callable[[Any], Any],
    provider_spec_resolver: Callable[[str], Any],
) -> dict[str, Any]:
    """Fail before a formal run can send an OpenRouter key through a redirect."""

    failures: list[str] = []
    configured_proxy = str(getattr(config.llm, "proxy", "") or "")
    if configured_proxy:
        failures.append("config.llm.proxy must be empty")

    base_url_override_names = (
        "OPENROUTER_BASE_URL",
        "OPENSQUILLA_LLM_BASE_URL",
    )
    active_base_url_overrides = [
        name for name in base_url_override_names if os.environ.get(name, "").strip()
    ]
    if active_base_url_overrides:
        failures.append(
            "OpenRouter base URL environment override(s) forbidden: "
            + ", ".join(sorted(active_base_url_overrides))
        )

    if os.environ.get("OPENSQUILLA_LLM_PROXY", "").strip():
        failures.append("OPENSQUILLA_LLM_PROXY environment override is forbidden")

    generic_credential_override_names = (
        "OPENSQUILLA_LLM_API_KEY_ENV",
        "OPENSQUILLA_LLM_API_KEY",
    )
    active_generic_credential_overrides = [
        name
        for name in generic_credential_override_names
        if os.environ.get(name, "").strip()
    ]
    if active_generic_credential_overrides:
        failures.append(
            "generic OpenRouter credential environment override(s) forbidden: "
            + ", ".join(sorted(active_generic_credential_overrides))
        )

    trust_env = os.environ.get("OPENSQUILLA_TRUST_ENV", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if trust_env:
        failures.append("OPENSQUILLA_TRUST_ENV must be disabled for formal OpenRouter calls")
        ambient_proxy_names = (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "http_proxy",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
        )
        active_ambient_proxies = [
            name for name in ambient_proxy_names if os.environ.get(name, "").strip()
        ]
        if active_ambient_proxies:
            failures.append(
                "ambient proxy environment variable(s) would be trusted: "
                + ", ".join(sorted(active_ambient_proxies))
            )

    if failures:
        raise ValueError(
            "formal OpenRouter runtime transport validation failed: "
            + "; ".join(failures)
        )

    runtime = resolve_runtime_config(config)
    official_base_url = str(provider_spec_resolver("openrouter").default_base_url or "")
    if runtime.provider != "openrouter":
        failures.append("resolved provider must be openrouter")
    if str(runtime.base_url or "") != official_base_url:
        failures.append("resolved OpenRouter base URL must exactly match the official endpoint")
    if bool(runtime.base_url_from_env):
        failures.append("resolved OpenRouter base URL must not come from the environment")
    if str(runtime.proxy or ""):
        failures.append("resolved OpenRouter proxy must be empty")
    if bool(getattr(runtime, "api_key_from_env", False)) and (
        str(getattr(runtime, "api_key_env_name", "") or "") != "OPENROUTER_API_KEY"
    ):
        failures.append(
            "resolved OpenRouter environment credential must come from OPENROUTER_API_KEY"
        )
    if bool(getattr(runtime, "trust_env", False)):
        failures.append("resolved OpenRouter transport must not trust ambient environment")
    runtime_ambient_proxies = getattr(runtime, "ambient_proxies", None)
    if runtime_ambient_proxies:
        failures.append("resolved OpenRouter transport has ambient proxy settings")
    if failures:
        raise ValueError(
            "formal OpenRouter runtime transport validation failed: "
            + "; ".join(failures)
        )
    return {
        "validated": True,
        "provider": "openrouter",
        "base_url": official_base_url,
        "base_url_from_env": False,
        "proxy_configured": False,
        "trust_env": False,
    }


def validate_formal_web_search_transport(
    config: Any,
    experiment_config: Any,
) -> dict[str, Any]:
    """Keep a formal Brave credential off explicit and ambient proxies."""

    provider = experiment_config.tools.web_search.provider.strip().casefold()
    if provider != "brave":
        return {"validated": True, "provider": provider, "credential_proxy_safe": True}

    failures: list[str] = []
    if str(getattr(config, "search_proxy", "") or "").strip():
        failures.append("config.search_proxy must be empty for formal Brave search")
    if bool(getattr(config, "search_use_env_proxy", False)):
        failures.append("config.search_use_env_proxy must be false for formal Brave search")
    if failures:
        raise ValueError(
            "formal Brave search transport validation failed: " + "; ".join(failures)
        )
    return {
        "validated": True,
        "provider": "brave",
        "credential_proxy_safe": True,
        "proxy_configured": False,
        "use_env_proxy": False,
    }


def validate_strict_openrouter_non_byok_environment(
    config: Any,
    *,
    resolve_runtime_config: Callable[[Any], Any],
    provider_spec_resolver: Callable[[str], Any],
) -> dict[str, Any]:
    """Fail closed on every routing/cost isolation prerequisite."""

    truthy = {"1", "true", "yes", "on", "enabled"}
    falsey = {"0", "false", "no", "off", "disabled"}
    required_truthy = (
        "OPENSQUILLA_PROVIDER_ROUTING_STRICT",
        "OPENSQUILLA_PROVIDER_STREAM_ERROR_FRAMES",
        "OPENSQUILLA_OPENROUTER_METADATA_REQUIRED",
        "OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS",
        "OPENSQUILLA_OPENROUTER_DISABLE_RESPONSE_CACHE",
        "DRACO_OPENROUTER_KEY_EXCLUSIVE",
    )
    failures = [
        f"{name}=1 required"
        for name in required_truthy
        if os.environ.get(name, "").strip().casefold() not in truthy
    ]
    if os.environ.get("OPENSQUILLA_TRUST_ENV", "").strip().casefold() not in falsey:
        failures.append("OPENSQUILLA_TRUST_ENV=0 required")
    forbidden_overrides = (
        "OPENROUTER_BASE_URL",
        "OPENSQUILLA_LLM_BASE_URL",
        "OPENSQUILLA_LLM_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    )
    active_overrides = [
        name for name in forbidden_overrides if os.environ.get(name, "").strip()
    ]
    if active_overrides:
        failures.append(
            "proxy/base-url override(s) forbidden: "
            + ", ".join(sorted(active_overrides))
        )

    runtime = resolve_runtime_config(config)
    official_base_url = str(
        provider_spec_resolver("openrouter").default_base_url or ""
    ).rstrip("/")
    if runtime.provider != "openrouter":
        failures.append("resolved provider must be openrouter")
    if not runtime.api_key:
        failures.append("resolved OpenRouter API key is missing")
    if str(runtime.base_url or "").rstrip("/") != official_base_url:
        failures.append("resolved OpenRouter base URL is not the official endpoint")
    if runtime.base_url_from_env:
        failures.append("resolved OpenRouter base URL came from an environment override")
    if str(runtime.proxy or "").strip():
        failures.append("resolved OpenRouter proxy must be empty")
    if failures:
        raise ValueError(
            "strict OpenRouter non-BYOK environment validation failed: "
            + "; ".join(failures)
        )
    return {
        "validated": True,
        "provider": "openrouter",
        "official_base_url": official_base_url,
        "provider_routing_strict": True,
        "stream_error_frames": True,
        "router_metadata_required": True,
        "require_parameters": True,
        "response_cache_disabled": True,
        "key_exclusive": True,
        "trust_env": False,
        "proxy_or_base_url_override": False,
    }


def resolved_llm_runtime_contract(
    config: Any,
    *,
    resolve_runtime_config: Callable[[Any], Any],
) -> dict[str, Any]:
    runtime = resolve_runtime_config(config)
    key_fingerprint = (
        f"sha256:{hashlib.sha256(runtime.api_key.encode('utf-8')).hexdigest()}"
        if runtime.api_key
        else ""
    )
    trust_environment = os.environ.get("OPENSQUILLA_TRUST_ENV", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    ambient_proxies = {}
    if trust_environment:
        ambient_proxies = {
            name: _sanitize_url_for_fingerprint(os.environ.get(name, ""))
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
            if os.environ.get(name)
        }
    cache_namespace = os.environ.get("OPENSQUILLA_BENCHMARK_CACHE_NAMESPACE", "").strip()
    return {
        "provider": runtime.provider,
        "model": runtime.model,
        "api_key_sha256": key_fingerprint,
        "api_key_from_env": runtime.api_key_from_env,
        "base_url": _sanitize_url_for_fingerprint(runtime.base_url),
        "base_url_from_env": runtime.base_url_from_env,
        "proxy": _sanitize_url_for_fingerprint(runtime.proxy),
        "provider_routing": dict(sorted(runtime.provider_routing.items())),
        "provider_routing_strict": (
            os.environ.get("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "").strip().lower()
            in {"1", "true", "yes", "on", "enabled"}
        ),
        "stream_error_frames": (
            os.environ.get("OPENSQUILLA_PROVIDER_STREAM_ERROR_FRAMES", "").strip().lower()
            in {"1", "true", "yes", "on", "enabled"}
        ),
        "router_metadata_required": (
            os.environ.get("OPENSQUILLA_OPENROUTER_METADATA_REQUIRED", "").strip().lower()
            in {"1", "true", "yes", "on", "enabled"}
        ),
        "require_parameters": (
            os.environ.get("OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS", "").strip().lower()
            in {"1", "true", "yes", "on", "enabled"}
        ),
        "response_cache_disabled": (
            os.environ.get("OPENSQUILLA_OPENROUTER_DISABLE_RESPONSE_CACHE", "").strip().lower()
            in {"1", "true", "yes", "on", "enabled"}
        ),
        "key_exclusive": (
            os.environ.get("DRACO_OPENROUTER_KEY_EXCLUSIVE", "").strip().lower()
            in {"1", "true", "yes", "on", "enabled"}
        ),
        "cache_namespace_enabled": bool(cache_namespace),
        "cache_namespace_required": (
            os.environ.get("OPENSQUILLA_BENCHMARK_CACHE_NAMESPACE_REQUIRED", "")
            .strip()
            .lower()
            in {"1", "true", "yes", "on", "enabled"}
        ),
        "cache_namespace_sha256": (
            f"sha256:{hashlib.sha256(cache_namespace.encode('utf-8')).hexdigest()}"
            if cache_namespace
            else ""
        ),
        "trust_env": trust_environment,
        "ambient_proxies": ambient_proxies,
    }
