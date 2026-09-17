"""Boot-time ingest of keyless public provider model listings.

Some hosted aggregators publish a public (no-auth) model listing with the
per-model limits their relay actually enforces — context windows, output
caps, prices. Pinned ``catalog_overrides.toml`` rows for such platforms rot
as the platform raises limits, and a stale window under-budgets every turn
(the provider request proof then rejects payloads the platform would happily
accept). This module fetches those listings at gateway boot and feeds them
into the catalog's provider-scoped live layer, so budgets track the platform
while the packaged corrections rows remain the offline fallback.

Which providers participate is registry metadata (``ProviderSpec.
live_catalog_url`` / ``live_catalog_shape``), never call-site branching;
each shape names a parser here that maps the platform payload to
``ModelCatalogEntry`` field dicts. Parsers emit only fields the listing
GENUINELY KNOWS — notably no reasoning fields, which stay owned by the
corrections ladder (a relay's streaming dialect is not listing data).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable, Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from opensquilla.env import trust_env as _trust_env

from .app_attribution import provider_app_headers
from .error_redaction import redacted_httpx_error
from .fx import TOKENRHYTHM_CNY_PER_USD
from .registry import UnknownProviderError, get_provider_spec
from .tokenrhythm_catalog import (
    parse_tokenrhythm_published,
    tokenrhythm_published_catalog_entries,
)
from .tokenrhythm_correlation import (
    redact_tokenrhythm_install_ids,
    tokenrhythm_install_id_headers,
)

if TYPE_CHECKING:
    from .model_catalog import ModelCatalog

log = structlog.get_logger(__name__)

# Per-fetch client timeout; boot treats every failure as a degrade-and-log.
LIVE_CATALOG_TIMEOUT_SECONDS = 5.0

# TokenRhythm publishes CNY prices per billingUnit tokens (1M so far);
# catalog costs are USD per-Mtok. Same documented conversion the packaged
# corrections rows use (catalog_overrides.toml) and the billing receipts
# record — one canonical rate in ``provider/fx.py``.
_TOKENRHYTHM_CNY_PER_USD = float(TOKENRHYTHM_CNY_PER_USD)


def parse_tokenrhythm_models(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Map a TokenRhythm ``/api/models`` payload to catalog entry fields.

    Envelope is ``{"code": 0, "data": [model, ...]}``. Emitted per model:
    ``context_window`` (``contextWindow``), ``max_output_tokens``
    (``maxOutputTokens``), ``display_name``, ``supports_tools`` /
    ``supports_vision`` (the listing's capability booleans are
    authoritative both ways), and CNY→USD converted costs. Public ``testing``
    rows are retained as metadata (only the authenticated listing grants
    entitlement); offline, non-chat, and malformed rows do not enter the
    runtime compatibility table.
    """
    published = parse_tokenrhythm_published(payload)
    return tokenrhythm_published_catalog_entries(published)


LiveCatalogParser = Callable[[Mapping[str, Any]], dict[str, dict[str, Any]]]

_LIVE_CATALOG_PARSERS: dict[str, LiveCatalogParser] = {
    "tokenrhythm": parse_tokenrhythm_models,
}


async def fetch_live_catalog_entries(
    url: str,
    shape: str,
    *,
    proxy: str = "",
    timeout: float = LIVE_CATALOG_TIMEOUT_SECONDS,
) -> dict[str, dict[str, Any]]:
    """Fetch one keyless listing and parse it with the shape's parser."""
    parser = _LIVE_CATALOG_PARSERS.get(shape)
    if parser is None:
        raise ValueError(f"unknown live catalog shape: {shape!r}")
    safe_request_error: Exception | None = None
    cancelled_request_error: asyncio.CancelledError | None = None
    headers: dict[str, str] = {}
    client: Any = None
    resp: Any = None
    payload: Any = None
    parsed: dict[str, dict[str, Any]] | None = None
    raw_message = ""
    raw_state = ""
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            trust_env=_trust_env(),
            proxy=proxy or None,
            follow_redirects=False,
        ) as client:
            headers = provider_app_headers(url)
            headers.update(
                tokenrhythm_install_id_headers(shape, url, proxy=proxy or None)
            )
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            payload = (
                resp.json(parse_float=Decimal)
                if shape == "tokenrhythm"
                else resp.json()
            )
            parsed = parser(payload if isinstance(payload, Mapping) else {})
    except asyncio.CancelledError:
        cancelled_request_error = asyncio.CancelledError()
    except httpx.HTTPError as exc:
        safe_request_error = redacted_httpx_error(exc, api_key="")
    except json.JSONDecodeError as exc:
        if redact_tokenrhythm_install_ids(exc.doc) == exc.doc:
            exc.__cause__ = None
            exc.__context__ = None
            exc.__traceback__ = None
            safe_request_error = exc
        else:
            safe_request_error = RuntimeError(
                "Live provider catalog returned invalid JSON"
            )
    except Exception as exc:
        raw_message = str(exc)
        safe_message = redact_tokenrhythm_install_ids(raw_message)
        raw_state = repr(getattr(exc, "__dict__", {}))
        if (
            safe_message != raw_message
            or redact_tokenrhythm_install_ids(raw_state) != raw_state
        ):
            safe_request_error = RuntimeError(
                safe_message
                if safe_message != raw_message
                else "Live provider catalog parsing failed"
            )
        else:
            exc.__cause__ = None
            exc.__context__ = None
            exc.__traceback__ = None
            safe_request_error = exc

    if cancelled_request_error is not None:
        headers.clear()
        client = None
        resp = None
        payload = None
        parsed = None
        raw_message = ""
        raw_state = ""
        url = redact_tokenrhythm_install_ids(url)
        proxy = redact_tokenrhythm_install_ids(proxy)
        raise cancelled_request_error
    if safe_request_error is not None:
        headers.clear()
        client = None
        resp = None
        payload = None
        parsed = None
        raw_message = ""
        raw_state = ""
        url = redact_tokenrhythm_install_ids(url)
        proxy = redact_tokenrhythm_install_ids(proxy)
        raise safe_request_error
    return parsed or {}


async def warm_live_provider_catalogs(
    catalog: ModelCatalog,
    provider_ids: Iterable[str],
    *,
    proxy: str = "",
) -> dict[str, int]:
    """Ingest live listings for every provider whose spec names one.

    ``catalog`` is the shared ``ModelCatalog``. Providers without
    live-catalog registry metadata are skipped silently; a fetch/parse
    failure degrades to a warning and leaves that provider on its packaged
    corrections rows. Returns the per-provider ingested row counts.
    """
    counts: dict[str, int] = {}
    for provider_id in dict.fromkeys((pid or "").strip().lower() for pid in provider_ids):
        if not provider_id:
            continue
        try:
            spec = get_provider_spec(provider_id)
        except UnknownProviderError:
            continue
        if not (spec.live_catalog_url and spec.live_catalog_shape):
            continue
        try:
            entries = await fetch_live_catalog_entries(
                spec.live_catalog_url, spec.live_catalog_shape, proxy=proxy
            )
            catalog.set_live_provider_entries(provider_id, entries)
            counts[provider_id] = len(entries)
            log.info("live_catalog.ready", provider=provider_id, count=len(entries))
        except Exception as exc:  # noqa: BLE001 - a live listing degrades, never blocks boot
            log.warning(
                "live_catalog.failed",
                provider=provider_id,
                error=redact_tokenrhythm_install_ids(str(exc)),
            )
    return counts
