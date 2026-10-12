"""Canonical FX normalization rates for provider-native billing currencies.

TokenRhythm bills in CNY, and its usage receipts are normalized to canonical
USD at one pinned platform rate.  Every consumer — billing-receipt emission
(``provider/openai.py``), live-catalog price conversion
(``provider/live_catalog.py``), and usage reporting (``gateway/usage_query.py``)
— must read the same constant here, so ledger receipts, catalog pricing, and
any CNY figure rendered from canonical USD agree with each other.
"""

from __future__ import annotations

from decimal import Decimal

_MONEY_NANO_SCALE = 1_000_000_000

TOKENRHYTHM_CNY_PER_USD = Decimal("6.975")
TOKENRHYTHM_CNY_PER_USD_NANOS = int(TOKENRHYTHM_CNY_PER_USD * _MONEY_NANO_SCALE)


def canonical_native_per_usd_rates() -> dict[str, str]:
    """Native-per-USD rates served to usage clients.

    Keys are ISO 4217 currency codes; values are the decimal rate rendered as
    a string, matching the receipt-level ``normalizationRatesNativePerUsd``
    wire format.  Clients use these instead of hardcoding their own display
    rate so UI conversions match what the ledger recorded.
    """

    return {"CNY": str(TOKENRHYTHM_CNY_PER_USD)}
