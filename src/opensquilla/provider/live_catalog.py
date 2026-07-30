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

import math
from collections.abc import Callable, Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from opensquilla.env import trust_env as _trust_env

from .app_attribution import provider_app_headers
from .fx import TOKENRHYTHM_CNY_PER_USD
from .model_catalog import DEFAULT_MAX_TOKENS as _NEAR_WINDOW_MARGIN
from .registry import UnknownProviderError, get_provider_spec

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
_TOKENRHYTHM_CNY_PER_USD_DECIMAL = TOKENRHYTHM_CNY_PER_USD
_TOKENS_PER_MTOK = Decimal("1000000")


def _coerce_positive_int(value: object) -> int:
    """Positive int from listing data; 0 for anything unusable.

    Listings serve numbers loosely (TokenRhythm already serves prices as
    strings), so integral floats and digit strings coerce rather than
    silently zeroing the platform's published budget fields.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, float):
        if not value.is_integer():
            return 0
        value = int(value)
    elif isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            return 0
    if not isinstance(value, int):
        return 0
    return value if value > 0 else 0


def _tokenrhythm_cost_per_mtok(value: object, billing_unit: object) -> float | None:
    """CNY-per-``billing_unit``-tokens (string or number) → USD per-Mtok."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    if not isinstance(billing_unit, (str, int, float)) or isinstance(billing_unit, bool):
        return None
    try:
        price = Decimal(str(value).strip())
        unit = Decimal(str(billing_unit).strip())
    except (InvalidOperation, ValueError):
        return None
    if not price.is_finite() or not unit.is_finite() or price < 0 or unit <= 0:
        return None
    converted = (price * (_TOKENS_PER_MTOK / unit)) / _TOKENRHYTHM_CNY_PER_USD_DECIMAL
    try:
        result = float(converted)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _tokenrhythm_bucket_cost(
    row: Mapping[str, Any],
    *,
    effective_key: str,
    discount_key: str,
    standard_key: str,
    billing_unit: object,
) -> float | None:
    keys = [effective_key]
    if row.get("hasDiscount") is True:
        keys.append(discount_key)
    keys.append(standard_key)
    for key in keys:
        if key not in row:
            continue
        cost = _tokenrhythm_cost_per_mtok(row.get(key), billing_unit)
        if cost is not None:
            return cost
    return None


def parse_tokenrhythm_models(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Map a TokenRhythm ``/api/models`` payload to catalog entry fields.

    Envelope is ``{"code": 0, "data": [model, ...]}``. Emitted per model:
    ``context_window`` (``contextWindow``), ``max_output_tokens``
    (``maxOutputTokens``), ``display_name``, ``supports_tools`` /
    ``supports_vision`` (the listing's capability booleans are
    authoritative both ways), and CNY→USD converted costs. Models not
    ``online`` and malformed rows are skipped — live data degrades, it
    never crashes resolution or grants fields it does not know.
    """
    entries: dict[str, dict[str, Any]] = {}
    data = payload.get("data")
    if not isinstance(data, list):
        return entries
    for row in data:
        if not isinstance(row, Mapping):
            continue
        model_id = str(row.get("id") or "").strip()
        if not model_id:
            continue
        status = str(row.get("status") or "").strip().lower()
        if status not in ("", "online"):
            continue
        fields: dict[str, Any] = {}
        context_window = _coerce_positive_int(row.get("contextWindow"))
        if context_window:
            fields["context_window"] = context_window
        max_output = _coerce_positive_int(row.get("maxOutputTokens"))
        # The platform publishes near-window output caps for some models
        # (input + output share the window). Passing such a cap through as
        # max_tokens would trip resolve_max_tokens' request-safety clamp
        # straight down to 8192; halving to the engine's own output-reserve
        # ceiling (ContextBudgetGovernor reserves at most window/2 for
        # output) keeps the budget generous AND leaves genuine input room.
        if max_output and context_window and max_output >= context_window - _NEAR_WINDOW_MARGIN:
            max_output = context_window // 2
        if max_output:
            fields["max_output_tokens"] = max_output
        display_name = row.get("name")
        if isinstance(display_name, str) and display_name.strip():
            fields["display_name"] = display_name.strip()
        capabilities = row.get("capabilities")
        if isinstance(capabilities, Mapping):
            for listing_key, field_name in (
                ("tools", "supports_tools"),
                ("vision", "supports_vision"),
            ):
                flag = capabilities.get(listing_key)
                if isinstance(flag, bool):
                    fields[field_name] = flag
        if str(row.get("currency") or "").strip().upper() == "CNY":
            billing_unit = row.get("billingUnit")
            for effective_key, discount_key, listing_key, field_name in (
                (
                    "effectiveInputPrice",
                    "discountInputPrice",
                    "inputPrice",
                    "input_cost_per_mtok",
                ),
                (
                    "effectiveOutputPrice",
                    "discountOutputPrice",
                    "outputPrice",
                    "output_cost_per_mtok",
                ),
                (
                    "effectiveCacheReadPrice",
                    "discountCacheReadPrice",
                    "cacheReadPrice",
                    "cache_read_cost_per_mtok",
                ),
            ):
                cost = _tokenrhythm_bucket_cost(
                    row,
                    effective_key=effective_key,
                    discount_key=discount_key,
                    standard_key=listing_key,
                    billing_unit=billing_unit,
                )
                if cost is not None:
                    fields[field_name] = cost
        if fields:
            entries[model_id] = fields
    return entries


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
    async with httpx.AsyncClient(
        timeout=timeout, trust_env=_trust_env(), proxy=proxy or None
    ) as client:
        resp = await client.get(url, headers=provider_app_headers(url))
        resp.raise_for_status()
        payload = resp.json()
    return parser(payload if isinstance(payload, Mapping) else {})


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
            log.warning("live_catalog.failed", provider=provider_id, error=str(exc))
    return counts
