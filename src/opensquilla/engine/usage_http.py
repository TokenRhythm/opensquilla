"""Usage accounting for direct OpenAI-compatible JSON requests.

Most model traffic flows through ``Provider.chat`` and is accounted by the
stream wrapper.  A small number of internal helpers intentionally issue a
non-streaming ``/chat/completions`` request directly.  This module gives those
helpers the same fail-closed start barrier and terminal receipt semantics
without coupling them to SQLite or duplicating provider calls.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any

from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageCallStart,
    current_usage_accounting_scope,
    finalize_usage_call,
    mark_usage_call_unknown,
    start_usage_call,
)
from opensquilla.provider.openai import (
    _billing_result,
    _exact_provider_billing_payload,
    _ProviderBillingAccumulator,
    _UsageSnapshotAccumulator,
)


def _nonnegative_integer(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative integer")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a non-negative integer") from exc
    if not parsed.is_finite() or parsed < 0 or parsed != parsed.to_integral_value():
        raise ValueError(f"{field} must be a non-negative integer")
    integer = int(parsed)
    if integer > (1 << 63) - 1:
        raise ValueError(f"{field} exceeds the ledger integer range")
    return integer


def _optional_nonnegative_integer(
    source: Mapping[str, Any],
    *names: str,
    default: int = 0,
) -> int:
    for name in names:
        if name in source:
            return _nonnegative_integer(source[name], field=name)
    return default


def _first_nonnegative_integer(
    *sources: tuple[Mapping[str, Any], str],
) -> int:
    for source, name in sources:
        if name in source:
            return _nonnegative_integer(source[name], field=name)
    return 0


def _nonnegative_cost(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("usage cost must be a non-negative finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("usage cost must be a non-negative finite number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError("usage cost must be a non-negative finite number")
    if parsed * 1_000_000_000 > (1 << 63) - 1:
        raise ValueError("usage cost exceeds the ledger integer range")
    return parsed


def openai_compatible_done_event(
    payload: Mapping[str, Any],
    *,
    default_model: str,
    provider_kind: str = "",
    base_url: str = "",
    billing_payload: Mapping[str, Any] | None = None,
) -> Any:
    """Validate one non-streaming JSON response and expose its usage receipt.

    Missing or malformed receipts are rejected.  The caller records them as
    ``unknown`` rather than inventing a dated zero-cost event.
    """

    usage_raw = payload.get("usage")
    if not isinstance(usage_raw, Mapping):
        raise ValueError("provider response has no structured usage receipt")
    usage = usage_raw
    if not any(name in usage for name in ("prompt_tokens", "input_tokens")) or not any(
        name in usage for name in ("completion_tokens", "output_tokens")
    ):
        raise ValueError("provider usage receipt is missing token counters")
    input_tokens = _optional_nonnegative_integer(
        usage,
        "prompt_tokens",
        "input_tokens",
    )
    output_tokens = _optional_nonnegative_integer(
        usage,
        "completion_tokens",
        "output_tokens",
    )
    total_tokens = _optional_nonnegative_integer(
        usage,
        "total_tokens",
        default=input_tokens + output_tokens,
    )
    if total_tokens != input_tokens + output_tokens:
        raise ValueError("provider usage total does not match input plus output")

    completion_details_raw = usage.get("completion_tokens_details") or {}
    if not isinstance(completion_details_raw, Mapping):
        raise ValueError("completion token details must be an object")
    prompt_details_raw = usage.get("prompt_tokens_details") or {}
    if not isinstance(prompt_details_raw, Mapping):
        raise ValueError("prompt token details must be an object")
    cache_creation_raw = usage.get("cache_creation") or {}
    if not isinstance(cache_creation_raw, Mapping):
        raise ValueError("cache creation details must be an object")
    prompt_cache_creation_raw = prompt_details_raw.get("cache_creation") or {}
    if not isinstance(prompt_cache_creation_raw, Mapping):
        raise ValueError("prompt cache creation details must be an object")

    reasoning_tokens = _optional_nonnegative_integer(
        completion_details_raw,
        "reasoning_tokens",
    )
    cache_read_tokens = _first_nonnegative_integer(
        (prompt_details_raw, "cached_tokens"),
        (usage, "cached_tokens"),
        (usage, "prompt_cache_hit_tokens"),
    )
    cache_write_tokens = _first_nonnegative_integer(
        (usage, "cache_creation_input_tokens"),
        (prompt_details_raw, "cache_write_tokens"),
        (usage, "cache_write_tokens"),
        (prompt_details_raw, "cache_creation_input_tokens"),
        (cache_creation_raw, "ephemeral_5m_input_tokens"),
        (prompt_cache_creation_raw, "ephemeral_5m_input_tokens"),
        (prompt_details_raw, "cache_creation_tokens"),
    )

    model = str(payload.get("model") or default_model or "")
    billing_receipt = None
    if provider_kind:
        usage_accumulator = _UsageSnapshotAccumulator()
        usage_accumulator.update(usage)
        billing_accumulator = _ProviderBillingAccumulator()
        billing_accumulator.update(provider_kind, billing_payload or payload)
        billed_cost, cost_source, billing_receipt = _billing_result(
            provider_kind=provider_kind,
            base_url=base_url,
            usage=usage_accumulator,
            billing=billing_accumulator,
            model=model,
        )
    else:
        # Preserve the original helper contract for callers that have not yet
        # supplied provider identity. Production direct-call reservations do.
        raw_cost = usage.get("cost", usage.get("total_cost"))
        billed_cost = 0.0 if raw_cost is None else _nonnegative_cost(raw_cost)
        cost_source = "provider_billed" if billed_cost > 0 else "none"
    return SimpleNamespace(
        kind="done",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        billed_cost=billed_cost,
        cost_source=cost_source,
        model=model,
        provider=provider_kind,
        billing_receipt=billing_receipt,
    )


@dataclass(slots=True)
class DirectUsageReservation:
    """One optional direct-HTTP provider-call reservation."""

    scope: UsageAccountingScope | None
    call: UsageCallStart | None
    base_url: str = ""
    terminal: bool = False

    async def finalize_openai_response(
        self,
        payload: Any,
        *,
        raw_json: str = "",
    ) -> bool:
        """Finalize a valid receipt; mark malformed/missing receipts unknown."""

        if self.terminal or self.scope is None or self.call is None:
            return self.scope is None
        try:
            if not isinstance(payload, Mapping):
                raise ValueError("provider response must be an object")
            done = openai_compatible_done_event(
                payload,
                default_model=self.call.model,
                provider_kind=self.call.provider,
                base_url=self.base_url,
                billing_payload=_exact_provider_billing_payload(
                    self.call.provider,
                    payload,
                    raw_json,
                ),
            )
        except (TypeError, ValueError):
            await self.mark_unknown("missing_or_invalid_usage_receipt")
            return False
        self.terminal = True
        await finalize_usage_call(self.scope, self.call, done)
        return True

    async def mark_unknown(self, reason: str) -> None:
        if self.terminal or self.scope is None or self.call is None:
            return
        self.terminal = True
        await mark_usage_call_unknown(self.scope, self.call, reason[:128])


async def reserve_direct_usage_call(
    *,
    provider: str,
    model: str,
    base_url: str = "",
) -> DirectUsageReservation:
    """Commit a start row for a direct request when an accounting scope exists."""

    scope = current_usage_accounting_scope()
    if scope is None:
        return DirectUsageReservation(scope=None, call=None, base_url=base_url)
    call = await start_usage_call(scope, provider=provider, model=model)
    return DirectUsageReservation(scope=scope, call=call, base_url=base_url)


__all__ = [
    "DirectUsageReservation",
    "openai_compatible_done_event",
    "reserve_direct_usage_call",
]
