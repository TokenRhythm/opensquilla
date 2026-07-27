"""Shared live model-catalog refresh boundary for gateway lifecycle paths."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import structlog

from opensquilla.provider.live_catalog import (
    LIVE_CATALOG_TIMEOUT_SECONDS,
    warm_live_provider_catalogs,
)
from opensquilla.provider.model_catalog import ModelCatalog, shared_catalog
from opensquilla.provider.registry import UnknownProviderError, get_provider_spec

log = structlog.get_logger(__name__)

LiveCatalogRefreshFingerprint = tuple[str, bool, str]


def _runtime_config(config: Any) -> Any:
    """Resolve current provider values without mutating the live config graph."""

    from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config

    scratch = (
        config.model_copy(deep=True)
        if hasattr(config, "model_copy")
        else copy.deepcopy(config)
    )
    return resolve_llm_runtime_config(scratch)


def live_catalog_refresh_fingerprint(config: Any) -> LiveCatalogRefreshFingerprint:
    """Return the connection identity that can change live-catalog results.

    The public listings are keyless, so only credential availability matters;
    replacing one non-empty key with another does not change the fetch. Providers
    without registry-declared live catalog metadata collapse to the empty
    fingerprint and therefore never add network work to unrelated config edits.
    """

    if config is None:
        return ("", False, "")
    runtime = _runtime_config(config)
    try:
        spec = get_provider_spec(runtime.provider)
    except UnknownProviderError:
        return ("", False, "")
    if not (spec.live_catalog_url and spec.live_catalog_shape):
        return ("", False, "")
    return (runtime.provider, bool(runtime.api_key), runtime.proxy)


async def refresh_live_model_catalog(
    config: Any,
    *,
    catalog: ModelCatalog | None = None,
) -> dict[str, int]:
    """Best-effort refresh of the active provider's public model listing.

    A resolved credential gates the request so unconfigured/keyless boots stay
    offline. The credential is never sent to the public listing. Failures and
    timeouts leave the existing live table untouched and fall back to packaged
    corrections; configuration writes must not fail because metadata is
    temporarily unavailable.
    """

    if config is None:
        return {}
    try:
        runtime = _runtime_config(config)
        if not runtime.api_key:
            return {}
        spec = get_provider_spec(runtime.provider)
        if not (spec.live_catalog_url and spec.live_catalog_shape):
            return {}
        target = catalog if catalog is not None else shared_catalog()
        return await asyncio.wait_for(
            warm_live_provider_catalogs(target, [runtime.provider], proxy=runtime.proxy),
            timeout=LIVE_CATALOG_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - live metadata is always best-effort
        log.warning(
            "gateway.live_catalog_refresh_failed",
            provider=str(getattr(getattr(config, "llm", None), "provider", "") or ""),
            error=str(exc),
        )
        return {}


async def refresh_live_model_catalog_if_changed(
    previous: LiveCatalogRefreshFingerprint,
    config: Any,
    *,
    catalog: ModelCatalog | None = None,
) -> dict[str, int]:
    """Refresh only when provider, credential availability, or proxy changed."""

    if previous == live_catalog_refresh_fingerprint(config):
        return {}
    return await refresh_live_model_catalog(config, catalog=catalog)
