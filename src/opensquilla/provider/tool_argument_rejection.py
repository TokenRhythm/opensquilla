"""Accounting for completed generations whose tool batch cannot execute."""

from .types import DoneEvent, ErrorEvent, ToolArgumentRejection


def rejected_tool_arguments_error(
    rejection: ToolArgumentRejection,
    *,
    usage: DoneEvent | None = None,
) -> ErrorEvent:
    """Keep known receipts without publishing a successful tool completion."""
    rows = []
    if usage is not None:
        rows = list(usage.model_usage_breakdown)
        if not rows:
            row = {
                "provider": usage.provider,
                "model": usage.model,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "reasoning_tokens": usage.reasoning_tokens,
                "cached_tokens": usage.cached_tokens,
                "cache_write_tokens": usage.cache_write_tokens,
                "billed_cost": usage.billed_cost,
                "cost_source": usage.cost_source,
            }
            if usage.billing_receipt is not None:
                row["billing_receipt"] = usage.billing_receipt
            rows.append(row)
    return ErrorEvent(
        code="incomplete_tool_call",
        message="Tool arguments were rejected; no tools in this batch were executed.",
        tool_argument_rejection=rejection,
        model_usage_breakdown=rows,
        usage_missing_count=usage.usage_missing_count if usage is not None else 1,
        ensemble_trace=usage.ensemble_trace if usage is not None else None,
    )
