"""Read-only capacity projection using the runtime catalog's own resolvers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import copy
from typing import Any

from .model_catalog import ModelCatalog, resolve_effective_context_window
from .registry import LOCAL_RUNTIME_PROVIDERS


def custom_listing_capacity(row: Any) -> dict[str, int]:
    """Normalize only explicitly declared limits; never promote a fallback."""
    if not isinstance(row, dict):
        return {}
    top = row.get("top_provider")
    top = top if isinstance(top, dict) else {}
    candidates = {
        "context_window": [
            row.get(name) for name in ("context_length", "context_window", "contextWindow")
        ]
        + [top.get("context_length")],
        "max_output_tokens": [
            row.get("max_output_tokens"),
            row.get("maxOutputTokens"),
            top.get("max_completion_tokens"),
        ],
    }
    result = {}
    for field, values in candidates.items():
        valid = [
            value
            for value in values
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        ]
        if valid:
            result[field] = min(valid)
    return result


def _endpoint_identity(config: Any, provider: str) -> str:
    llm = getattr(config, "llm", None)
    deployment = (
        llm
        if getattr(llm, "provider", "") == provider
        else (getattr(config, "llm_profiles", {}) or {}).get(provider)
    )
    fields = [
        getattr(deployment, name, None)
        for name in ("base_url", "proxy", "api_key", "api_key_env", "api_key_env_pool")
    ]
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def sync_custom_capacity_endpoints(catalog: ModelCatalog, config: Any) -> None:
    if not callable(getattr(catalog, "set_live_provider_entries", None)):
        return
    identities = dict(getattr(catalog, "_capacity_endpoint_identities", {}))
    for provider in ("custom", "custom_anthropic"):
        identity = _endpoint_identity(config, provider)
        if identities.get(provider) != identity:
            catalog.set_live_provider_entries(provider, {})
        identities[provider] = identity
    catalog._capacity_endpoint_identities = identities


def custom_capacity_identity(config: Any, provider: str) -> str:
    """Capture a deployment identity before an asynchronous listing starts."""
    return _endpoint_identity(config, provider)


def install_custom_capacity(
    catalog: ModelCatalog,
    identity: str,
    provider: str,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    if provider not in {"custom", "custom_anthropic"}:
        return
    identities = getattr(catalog, "_capacity_endpoint_identities", {})
    if identities.get(provider) != identity:
        return  # A listing for an old endpoint must not overwrite a new deployment.
    entries: dict[str, dict[str, int]] = {}
    for row in rows:
        metadata = row.get("metadata")
        declared = metadata.get("capacity") if isinstance(metadata, dict) else None
        model_id = row.get("id", row.get("model_id"))
        if model_id and isinstance(declared, dict):
            values = entries.setdefault(str(model_id), {})
            for field, value in custom_listing_capacity(declared).items():
                values[field] = min(values.get(field, value), value)
    catalog.set_live_provider_entries(provider, entries)


def resolve_model_capacities(
    catalog: ModelCatalog,
    config: Any,
    models: list[dict[str, str]],
) -> dict[str, Any]:
    llm = getattr(config, "llm", None)
    active_provider = str(getattr(llm, "provider", "") or "").strip().lower()
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for target in models:
        provider, model = target["provider"].strip().lower(), target["model"].strip()
        if (provider, model) in seen:
            continue
        seen.add((provider, model))
        # Restore only this exact model's two fields. Unqualified overrides
        # and other settings still participate in the canonical resolver.
        override_key = f"{provider}/{model}".lower()
        overrides = catalog._user_overrides.get(override_key, {})
        automatic = copy(catalog)
        automatic.set_user_overrides(
            {
                **catalog._user_overrides,
                override_key: {
                    name: value
                    for name, value in overrides.items()
                    if name not in {"context_window", "max_output_tokens"}
                },
            }
        )
        global_context = (
            int(getattr(llm, "context_window_tokens", 0) or 0) if provider == active_provider else 0
        )
        auto_context, auto_context_source = resolve_effective_context_window(
            automatic,
            model,
            provider,
            global_context,
        )
        context, context_source = resolve_effective_context_window(
            catalog,
            model,
            provider,
            global_context,
        )
        auto_output, auto_output_source = automatic.resolve_max_tokens_with_source(
            model,
            0,
            provider,
            capacity_only=True,
        )
        output, output_source = catalog.resolve_max_tokens_with_source(
            model,
            0,
            provider,
            capacity_only=True,
        )
        result.append(
            {
                "provider": provider,
                "model": model,
                "contextWindow": {
                    "automatic": auto_context,
                    "automaticSource": auto_context_source,
                    "override": overrides.get("context_window"),
                    "value": context,
                    "source": context_source,
                    "editable": True,
                },
                "maxOutputTokens": {
                    "automatic": auto_output,
                    "automaticSource": auto_output_source,
                    "override": overrides.get("max_output_tokens"),
                    "value": output,
                    "source": output_source,
                    "editable": provider != "openai_codex",
                },
                "localRuntime": provider
                in LOCAL_RUNTIME_PROVIDERS - {"custom", "custom_anthropic"},
            }
        )
    return {"models": result}
