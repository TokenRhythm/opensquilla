"""Preserve observed usage when a physical stream does not complete."""

from typing import Any

from opensquilla.usage_metering import UsageMeter

from .recovery_origin import RecoveryOrigin
from .types import DoneEvent, ErrorEvent


def interrupted_stream_error(
    *,
    message: str,
    code: str,
    recovery_origin: RecoveryOrigin,
    usage: DoneEvent | None,
    input_tokens_known: bool = True,
    output_tokens_known: bool = True,
) -> ErrorEvent:
    """Keep a received subtotal while exposing the unreceived remainder."""
    rows: list[dict[str, Any]] = []
    if usage is not None:
        input_tokens = usage.input_tokens if input_tokens_known else 0
        output_tokens = usage.output_tokens if output_tokens_known else 0
        cached_tokens = max(0, usage.cached_tokens)
        cache_write_tokens = max(0, usage.cache_write_tokens)
        rows = list(usage.model_usage_breakdown)
        if not rows:
            row: dict[str, Any] = {
                "provider": usage.provider,
                "model": usage.model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": max(0, usage.reasoning_tokens),
                "cached_tokens": cached_tokens,
                "cache_write_tokens": cache_write_tokens,
                "billed_cost": usage.billed_cost,
                "cost_source": usage.cost_source,
                "token_usage_known": input_tokens_known and output_tokens_known,
                "usage_meters": (
                    UsageMeter(
                        "input_tokens", "token",
                        str(input_tokens) if input_tokens_known else None,
                        known=input_tokens_known,
                    ),
                    UsageMeter(
                        "output_tokens", "token",
                        str(output_tokens) if output_tokens_known else None,
                        known=output_tokens_known,
                    ),
                    UsageMeter("cache_read_tokens", "token", str(cached_tokens)),
                    UsageMeter("cache_write_tokens", "token", str(cache_write_tokens)),
                ),
            }
            if usage.billing_receipt is not None:
                row["billing_receipt"] = usage.billing_receipt
            rows.append(row)
    return ErrorEvent(
        message=message,
        code=code,
        recovery_origin=recovery_origin,
        model_usage_breakdown=rows,
        # An interrupted cumulative snapshot cannot prove the final usage or
        # absence of further charges, even when its received bill is confirmed.
        usage_missing_count=max(1, usage.usage_missing_count if usage is not None else 1),
    )
