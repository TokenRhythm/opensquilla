"""Project explicitly cumulative composite receipts into per-call accounting."""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar

from opensquilla.provider.types import DoneEvent, ErrorEvent, ProviderGenerationResetEvent

_UsageEvent = TypeVar("_UsageEvent", DoneEvent, ErrorEvent, ProviderGenerationResetEvent)


@dataclass
class ProviderUsageDelta:
    """Turn-local accounting only; the provider's replay/trace stays cumulative."""

    _reported: dict[str, tuple[list[dict[str, Any]], int]] = field(default_factory=dict)

    def consume(self, event: _UsageEvent) -> _UsageEvent:
        scope = event.cumulative_usage_id
        if not scope:
            return event
        rows = event.model_usage_breakdown
        previous, previous_missing = self._reported.get(scope, ([], 0))
        if rows[:len(previous)] != previous or event.usage_missing_count < previous_missing:
            raise ValueError("Cumulative provider usage changed previously reported receipts")
        new_rows = rows[len(previous):]
        changes: dict[str, Any] = {
            "model_usage_breakdown": new_rows,
            "usage_missing_count": event.usage_missing_count - previous_missing,
            "cumulative_usage_id": "",
        }
        if isinstance(event, DoneEvent):
            for key in (
                "input_tokens", "output_tokens", "reasoning_tokens",
                "cached_tokens", "cache_write_tokens",
            ):
                changes[key] = sum(int(row.get(key) or 0) for row in new_rows)
            changes["billed_cost"] = math.fsum(
                float(row.get("billed_cost") or 0) for row in new_rows
            )
        projected = replace(event, **changes)
        # Wrappers stamp physical identity/epoch evidence outside dataclass
        # fields. Preserve those stamps when projecting just the accounting.
        for key, value in getattr(event, "__dict__", {}).items():
            if key not in getattr(projected, "__dict__", {}):
                object.__setattr__(projected, key, value)
        self._reported[scope] = (copy.deepcopy(rows), event.usage_missing_count)
        return projected
