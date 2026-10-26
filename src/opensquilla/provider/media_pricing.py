"""Strict adapters for official media price catalogs; no network on valuation.

Unknown SKU formulas fail closed. A catalog snapshot is a local estimate, never
a provider receipt. Only prices and model identities enter this process cache.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from opensquilla.endpoint_identity import base_url_matches_official_api
from opensquilla.provider.fx import TOKENRHYTHM_CNY_PER_USD, settlement_fx_evidence
from opensquilla.usage_metering import (
    UsageMeter,
    UsagePriceRule,
    UsagePriceSnapshot,
    UsageSettlementRevision,
    canonical_json,
    estimate_metered_cost,
)

_FEEDS: dict[tuple[str, str], dict[tuple[str, str], UsagePriceSnapshot]] = {}


def _official(provider: str, base_url: str) -> bool:
    endpoints = {
        "tokenrhythm": "https://tokenrhythm.studio/v1",
        "openrouter": "https://openrouter.ai/api/v1",
    }
    return provider in endpoints and base_url_matches_official_api(endpoints[provider], base_url)


def _rules(
    provider: str, row: Mapping[str, Any]
) -> tuple[str, str, tuple[UsagePriceRule, ...]] | None:
    if provider == "tokenrhythm":
        if row.get("billingMode") == "per_image" and row.get("currency") == "CNY":
            return (
                "image_generation",
                "CNY",
                (
                    UsagePriceRule(
                        "output_images",
                        "image",
                        row["pricePerImage"],
                        str(row.get("billingUnit", 1)),
                    ),
                ),
            )
        pricing = row.get("videoPricing")
        if (
            not isinstance(pricing, Mapping)
            or pricing.get("pricingScheme") != "wan_total_seconds"
            or int(pricing.get("inputImageFreeCount", 0)) != 0
        ):
            return None
        rules = []
        for field, metric in (
            ("outputPerSecondCny", "video_seconds"),
            ("inputVideoPerSecondCny", "input_video_seconds"),
        ):
            for resolution, amount in pricing[field].items():
                rules.append(
                    UsagePriceRule(
                        metric, "second", amount, dimensions={"resolution": str(resolution).lower()}
                    )
                )
        for field, metric, unit in (
            ("inputImagePerImageCny", "input_images", "image"),
            ("inputAudioPerSecondCny", "input_audio_seconds", "second"),
        ):
            rules.append(UsagePriceRule(metric, unit, pricing[field]))
        return "video_generation", "CNY", tuple(rules)
    skus = row.get("pricing_skus")
    if provider != "openrouter" or not isinstance(skus, Mapping) or not skus:
        return None
    rules = []
    for name, amount in skus.items():
        dimensions: dict[str, str] = {}
        key = str(name)
        if key in {"cents_per_image_input", "reference_input_images", "reference_images"}:
            metric, unit = "input_images", "image"
        else:
            suffix = re.search(r"_(480p|720p|768p|1080p|2k|4k)$", key)
            if suffix:
                dimensions["resolution"] = suffix[1]
                key = key[: suffix.start()]
            if key.endswith("_with_audio") or key.endswith("_without_audio"):
                dimensions["audio"] = "true" if key.endswith("_with_audio") else "false"
                key = key.removesuffix("_with_audio").removesuffix("_without_audio")
            for mode in ("text_to_video", "image_to_video"):
                if key.startswith(mode + "_"):
                    dimensions["mode"] = mode
                    key = key.removeprefix(mode + "_")
            if key in {
                "duration_seconds",
                "cents_per_video_output_second",
                "cents_per_second_output",
            }:
                metric, unit = "video_seconds", "second"
            elif key == "reference_duration_seconds":
                metric, unit = "input_video_seconds", "second"
            else:
                # Minimums, video-token conversions and continuation formulas
                # require a dedicated adapter, not guessed unit conversion.
                return None
        price = str(Decimal(str(amount)) / 100) if str(name).startswith("cents_") else str(amount)
        rules.append(UsagePriceRule(metric, unit, price, dimensions=dimensions))
    return "video_generation", "USD", tuple(rules)


def register_media_catalog(
    provider: str, rows: Sequence[Mapping[str, Any]], *, base_url: str, source_url: str = ""
) -> int:
    """Replace one official feed atomically; malformed models remain unpriced."""
    if not _official(provider, base_url):
        return 0
    feed = source_url or base_url
    source = urlsplit(feed)
    if (
        source.scheme != "https"
        or source.hostname != urlsplit(base_url).hostname
        or source.username
        or source.password
        or source.query
        or source.fragment
    ):
        return 0
    snapshots: dict[tuple[str, str], UsagePriceSnapshot] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            continue
        try:
            parsed = _rules(provider, row)
            if parsed is None:
                continue
            operation, currency, rules = parsed
            revision = hashlib.sha256(
                canonical_json([asdict(rule) for rule in rules]).encode()
            ).hexdigest()
            snapshots[(row["id"], operation)] = UsagePriceSnapshot(
                "provider_catalog",
                "sha256:" + revision,
                rules,
                currency=currency,
                provider=provider,
                model=row["id"],
                source_url=feed,
            )
        except (KeyError, ValueError, TypeError, ArithmeticError, AttributeError):
            continue
    _FEEDS[(provider, feed)] = snapshots
    return len(snapshots)


def resolve_media_price(
    provider: str,
    model: str,
    operation: str,
    base_url: str,
    dimensions: Mapping[str, str] | None = None,
) -> UsagePriceSnapshot | None:
    if not _official(provider, base_url):
        return None
    dimensions = {key: str(value).lower() for key, value in (dimensions or {}).items()}
    candidates = [
        feed[(model, operation)]
        for (p, _), feed in _FEEDS.items()
        if p == provider and (model, operation) in feed
    ]
    if not candidates:
        return None
    snapshot = candidates[-1]
    selected: dict[tuple[str, str], UsagePriceRule] = {}
    for rule in snapshot.rules:
        if not all(dimensions.get(k) == v for k, v in rule.dimensions.items()):
            continue
        key = (rule.metric, rule.unit)
        old = selected.get(key)
        if old is None or len(rule.dimensions) > len(old.dimensions):
            selected[key] = rule
        elif len(rule.dimensions) == len(old.dimensions) and rule != old:
            return None
    if not selected or not any(key[0] in {"output_images", "video_seconds"} for key in selected):
        return None
    return replace(snapshot, rules=tuple(selected.values()))


def media_estimate(
    *,
    provider: str,
    model: str,
    operation: str,
    base_url: str,
    meters: tuple[UsageMeter, ...],
    dimensions: Mapping[str, str],
) -> tuple[UsagePriceSnapshot | None, UsageSettlementRevision | None]:
    """Value measured quantities against a captured official catalog only."""
    snapshot = resolve_media_price(provider, model, operation, base_url, dimensions)
    if snapshot is None:
        return None, None
    priced_metrics = {(rule.metric, rule.unit) for rule in snapshot.rules}
    measured = tuple(m for m in meters if (m.metric, m.unit) in priced_metrics)
    try:
        amount, _missing = estimate_metered_cost(measured, snapshot)
        if amount is None:
            return snapshot, None
        usd = None
        fx = {}
        if snapshot.currency == "USD":
            usd = format(amount, "f")
            fx = settlement_fx_evidence("USD", "1")
        elif provider == "tokenrhythm" and snapshot.currency == "CNY":
            usd = format(amount / TOKENRHYTHM_CNY_PER_USD, ".18f").rstrip("0").rstrip(".") or "0"
            fx = settlement_fx_evidence("CNY", str(TOKENRHYTHM_CNY_PER_USD))
        estimate = UsageSettlementRevision(
            "initial",
            "local_estimate",
            "final",
            currency=snapshot.currency,
            native_amount=format(amount, "f"),
            usd_amount=usd,
            price_snapshot_id=snapshot.snapshot_id,
            reason_code="provider_catalog_estimate",
            **fx,
        )
        estimate.cost_fields()
    except (ValueError, ArithmeticError):
        # Catalog values and measured quantities can each be valid while their
        # product exceeds the ledger range. Keep the observation unpriced.
        return snapshot, None
    return snapshot, estimate


def media_settlement(
    settlement: UsageSettlementRevision | None,
    estimate: UsageSettlementRevision | None,
) -> UsageSettlementRevision | None:
    """Keep a catalog quote and a pending provider amount as separate evidence."""
    if settlement is None:
        return estimate
    if (
        estimate is not None
        and settlement.authority == "unknown"
        and settlement.status == "pending"
    ):
        return replace(
            estimate,
            status="pending",
            reason_code="provider_catalog_estimate_receipt_pending",
            provider_pending_native_amount=settlement.native_amount,
            provider_pending_currency=settlement.currency
            if settlement.native_amount is not None
            else None,
        )
    return settlement
