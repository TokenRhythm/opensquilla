"""Transport-neutral provider readiness projection used by Gateway adapters."""

from __future__ import annotations

import os
import re
from typing import Any

from opensquilla.redaction import redact_error_text


def _active_llm_provider(*, config: Any, provider_selector: Any) -> str | None:
    current_config = getattr(provider_selector, "current_config", None)
    provider = getattr(current_config, "provider", None)
    if provider:
        return str(provider)
    provider = getattr(getattr(config, "llm", None), "provider", None)
    return str(provider) if provider else None


def _provider_api_key_env(
    provider_id: str,
    default_env_key: str,
    *,
    active_provider: str | None,
    config: Any,
) -> str:
    llm_config = getattr(config, "llm", None)
    if provider_id == active_provider:
        configured_env = str(getattr(llm_config, "api_key_env", "") or "")
        if configured_env:
            return configured_env
    return default_env_key


def _provider_key_material(
    provider_id: str,
    env_key: str,
    *,
    active_provider: str | None,
    config: Any,
) -> str:
    """Resolve configured key material without exposing it in the projection."""

    llm_config = getattr(config, "llm", None)
    key_value = ""
    if provider_id == active_provider:
        key_value = str(getattr(llm_config, "api_key", "") or "")
    if not key_value and env_key:
        key_value = os.environ.get(env_key, "") or ""
    return key_value


_URL_SHAPED_KEY_RE = re.compile(r"^https?://")
_ENV_NAME_SHAPED_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
_OPENSQUILLA_ENV_NAME_RE = re.compile(r"^OPENSQUILLA_[A-Z0-9_]+$")
_ENV_NAME_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_KEY_ENV")


def _api_key_shape(key_value: str, *, expected_env_name: str = "") -> str:
    value = key_value.strip()
    if not value:
        return "ok"
    if _URL_SHAPED_KEY_RE.match(value):
        return "looks_like_url"
    if value.startswith("$"):
        return "looks_like_env_name"
    if _ENV_NAME_SHAPED_KEY_RE.match(value) and (
        (expected_env_name and value == expected_env_name)
        or _OPENSQUILLA_ENV_NAME_RE.match(value)
        or value.endswith(_ENV_NAME_SUFFIXES)
    ):
        return "looks_like_env_name"
    return "ok"


def _provider_base_url(
    provider_id: str,
    default_base_url: str,
    *,
    active_provider: str | None,
    config: Any,
) -> str:
    configured = getattr(getattr(config, "llm", None), "base_url", None)
    if provider_id == active_provider and configured:
        return str(configured)
    return default_base_url


async def _model_probe(provider_id: str, provider_selector: Any) -> dict[str, Any]:
    if provider_selector is None or not getattr(provider_selector, "is_configured", True):
        return {
            "attempted": True,
            "status": "unavailable",
            "count": 0,
            "error": "No provider selector configured",
        }
    try:
        detailed_listing = getattr(provider_selector, "list_models_detailed", None)
        if callable(detailed_listing):
            detailed = await detailed_listing()
            rows = list(getattr(detailed, "models", []) or [])
            matching_errors = [
                error
                for error in list(getattr(detailed, "errors", []) or [])
                if str(getattr(error, "provider", "") or "").strip().lower()
                == provider_id.strip().lower()
            ]
        else:
            rows = await provider_selector.list_models()
            matching_errors = []
        matching = [
            row
            for row in rows
            if isinstance(row, dict)
            and str(row.get("provider") or "").strip().lower()
            == provider_id.strip().lower()
        ]
        if matching_errors:
            first = matching_errors[0]
            detail = redact_error_text(str(getattr(first, "detail", "") or ""))
            failure_kind = str(getattr(first, "kind", "") or "unknown")
            return {
                "attempted": True,
                "status": "degraded" if matching else "error",
                "count": len(matching),
                "error": detail or failure_kind,
                "failureKind": failure_kind,
            }
        return {
            "attempted": True,
            "status": "ok",
            "count": len(matching),
            "error": None,
            "failureKind": None,
        }
    except Exception as exc:  # noqa: BLE001 - diagnostics are best-effort.
        return {
            "attempted": True,
            "status": "error",
            "count": 0,
            "error": redact_error_text(str(exc)),
            "failureKind": "unknown",
        }


async def read_provider_status(
    *,
    config: Any,
    provider_selector: Any,
    provider_stats: Any,
    provider_id: str | None,
    probe_models: bool,
) -> dict[str, Any]:
    """Build the public provider projection from primitive runtime dependencies."""

    from opensquilla.onboarding.provider_specs import list_provider_setup_specs
    from opensquilla.provider.selector import ProviderBuildError, build_provider

    specs = list_provider_setup_specs()
    by_id = {spec.provider_id: spec for spec in specs}
    if provider_id:
        provider_id = str(provider_id)
        if provider_id not in by_id:
            raise ValueError(f"Unknown provider: {provider_id}")
        specs = [by_id[provider_id]]

    active = _active_llm_provider(config=config, provider_selector=provider_selector)
    llm_config = getattr(config, "llm", None)
    resolution_getter = getattr(config, "provider_resolution", None)
    resolution = resolution_getter() if callable(resolution_getter) else {}
    provider_resolution_blocked = bool(resolution.get("action_required", False))
    rows: list[dict[str, Any]] = []
    for spec in specs:
        is_active = spec.provider_id == active
        api_key_env = _provider_api_key_env(
            spec.provider_id,
            spec.env_key,
            active_provider=active,
            config=config,
        )
        key_material = _provider_key_material(
            spec.provider_id,
            api_key_env,
            active_provider=active,
            config=config,
        )
        api_key_configured = bool(key_material)
        api_key_shape = _api_key_shape(key_material, expected_env_name=api_key_env)
        base_url = _provider_base_url(
            spec.provider_id,
            spec.default_base_url,
            active_provider=active,
            config=config,
        )
        if is_active:
            from opensquilla.provider.credentials import (
                credential_provider_hint,
                endpoint_provider_hint,
            )

            credential_hint = credential_provider_hint(
                key_material,
                api_key_env=api_key_env,
            )
            endpoint_hint = endpoint_provider_hint(base_url)
            mismatch_reason = ""
            mismatch_source = ""
            if credential_hint and credential_hint != spec.provider_id:
                mismatch_reason = "credential_provider_mismatch"
                mismatch_source = "credential_shape"
            elif credential_hint and endpoint_hint and credential_hint != endpoint_hint:
                mismatch_reason = "credential_endpoint_provider_mismatch"
                mismatch_source = "credential_endpoint"
            if mismatch_reason:
                provider_resolution_blocked = True
                if not bool(resolution.get("action_required", False)):
                    resolution = {
                        "status": "conflict",
                        "effective_provider": spec.provider_id,
                        "source": mismatch_source,
                        "reason_code": mismatch_reason,
                        "action_required": True,
                        "action_recommended": True,
                    }
        base_url_configured = bool(base_url)
        configured = (
            spec.runtime_supported
            and (not spec.requires_api_key or api_key_configured)
            and (not spec.requires_base_url or base_url_configured)
        )
        if is_active and provider_resolution_blocked:
            configured = False
        model = str(getattr(llm_config, "model", "") or "") if is_active else ""
        api_key = key_material if is_active else ""
        error: str | None = None
        buildable = False
        if is_active and provider_resolution_blocked:
            error = str(resolution.get("reason_code") or "provider_resolution_blocked")
        else:
            try:
                build_provider(
                    spec.provider_id,
                    model or "diagnostic-model",
                    api_key=api_key,
                    base_url=base_url,
                )
                buildable = True
            except ProviderBuildError as exc:
                error = str(exc)
            except Exception as exc:  # noqa: BLE001 - diagnostic surface.
                error = str(exc)
        if probe_models and is_active and provider_resolution_blocked:
            probe = {
                "attempted": False,
                "status": "unavailable",
                "count": 0,
                "error": str(resolution.get("reason_code") or "provider_resolution_blocked"),
                "failureKind": str(
                    resolution.get("reason_code") or "provider_resolution_blocked"
                ),
            }
        else:
            probe = (
                await _model_probe(spec.provider_id, provider_selector)
                if probe_models and is_active
                else {
                    "attempted": False,
                    "status": "skipped",
                    "count": 0,
                    "error": None,
                    "failureKind": None,
                }
            )
        rows.append(
            {
                "providerId": spec.provider_id,
                "active": is_active,
                "configured": configured,
                "buildable": buildable,
                "model": model,
                "requiresApiKey": spec.requires_api_key,
                "apiKeyEnv": api_key_env,
                "apiKeyConfigured": api_key_configured,
                "apiKeyShape": api_key_shape,
                "baseUrlConfigured": base_url_configured,
                "error": error,
                "modelProbe": probe,
                "latency": (
                    provider_stats.snapshot(spec.provider_id)
                    if provider_stats is not None
                    else None
                ),
            }
        )
    effective_provider = resolution.get("effective_provider")
    provider_resolution = {
        "status": str(resolution.get("status") or "explicit"),
        "effectiveProvider": str(active if effective_provider is None else effective_provider),
        "source": str(resolution.get("source") or "config"),
        "reasonCode": str(resolution.get("reason_code") or "provider_explicit"),
        "actionRequired": bool(resolution.get("action_required", False)),
        "actionRecommended": bool(resolution.get("action_recommended", False)),
    }
    return {
        "activeProvider": active,
        "providerResolution": provider_resolution,
        "providers": rows,
        "count": len(rows),
    }
