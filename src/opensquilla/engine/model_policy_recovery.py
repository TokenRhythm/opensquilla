"""Bounded execution recovery for the intent-led model routing policy.

The callback owns deployment preflight, durable effective-tier CAS, and provider
activation. This helper owns signal qualification and the Agent's turn-wide
additional-call budget shared by transport retries and the quality remedy.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from opensquilla.engine.routing.model_policy import quality_retry_tier

VALIDATION_REASONS = frozenset(
    {
        "invalid_arguments",
        "retryable_tool_input_error",
        "schema_validation_failed",
        "output_validation_failed",
        "invalid_tool_arguments",
        "invalid_json",
        "format_validation_failed",
    }
)
_REPEAT_TOOLS = frozenset({"exec_command", "glob_search", "grep_search", "list_dir"})
_SYNTHETIC_WRITE_PREFIXES = ("debug_marker", "guard_unlock", "runtime_guard", "temp_marker")


def effective_write_count(records: Sequence[Mapping[str, Any]]) -> int:
    """Retain the frozen no-progress guard's synthetic-marker exclusion."""
    count = 0
    for record in records:
        name = Path(
            str(record.get("relative_path") or record.get("path") or "")
            .replace("\\", "/")
            .lstrip("./")
        ).name.lower()
        synthetic = bool(record.get("created")) and any(
            name == prefix or name.startswith(f"{prefix}.") or name.startswith(f"{prefix}_")
            for prefix in _SYNTHETIC_WRITE_PREFIXES
        )
        count += not synthetic
    return count


def validation_failure(results: Sequence[Any]) -> bool:
    return any(
        result.is_error
        and isinstance(result.execution_status, Mapping)
        and result.execution_status.get("reason") in VALIDATION_REASONS
        for result in results
    )


class ModelPolicyRecovery:
    def __init__(self, config: Any) -> None:
        self.metadata = config.metadata
        self.enabled = (
            self.metadata.get("router_tier_policy") == "model"
            and self.metadata.get("routing_applied") is True
        )
        self.budget = min(3, max(0, int(config.max_provider_retries or 0)))
        self.used = 0
        self.quality_attempted = False
        self.scope_id = f"model-policy-turn:{uuid.uuid4().hex}"
        self.repeat_threshold = max(0, int(config.repeated_tool_call_recovery_threshold or 0))
        self.repeat_tools = _REPEAT_TOOLS | set(
            config.repeated_tool_call_recovery_extra_tools or ()
        )
        self.repeat_key: tuple[str, str] | None = None
        self.repeat_count = 0
        self.repeat_write_count = 0
        self.repeat_last_error = False

    def reserve_transport_retry(self) -> bool:
        if not self.enabled:
            return True
        if self.used >= self.budget:
            return False
        self.used += 1
        return True

    async def prepare(
        self, provider: Any, reason: str, messages: list[Any]
    ) -> dict[str, Any] | None:
        callback = getattr(provider, "prepare_quality_retry", None)
        tier = self.metadata.get("routed_tier")
        if (
            not self.enabled
            or self.quality_attempted
            or not callable(callback)
            or tier not in {"c0", "c1", "c2", "c3"}
            or quality_retry_tier(
                tier,
                reason,
                self.budget - self.used,
                held_tier=tier
                if self.metadata.get("router_control_hold_applied") is True
                else None,
            )
            is None
        ):
            return None
        # A refused preflight is not repeatedly attempted later in this turn.
        self.quality_attempted = True
        updates = await callback(
            reason=reason,
            scope_id=self.scope_id,
            remaining_budget=self.budget - self.used,
            messages=messages,
        )
        if updates is None:
            return None
        if not isinstance(updates, dict) or set(updates) - {
            "model_id",
            "provider_id",
            "max_tokens",
            "context_window_tokens",
            "context_window_known",
            "model_capabilities",
            "model_vision_support",
            "thinking",
            "thinking_budget_tokens",
        }:
            raise ValueError("model policy quality callback returned invalid configuration updates")
        self.used += 1
        return updates

    @staticmethod
    def _key(call: Any) -> tuple[str, str]:
        payload = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, default=str)
        return call.tool_name, payload

    def repeated_call_message(self, calls: Sequence[Any], write_count: int) -> str | None:
        if not self.enabled:
            return None
        if (
            len(calls) == 1
            and self.repeat_threshold > 0
            and calls[0].tool_name in self.repeat_tools
        ):
            key = self._key(calls[0])
            if key == self.repeat_key and write_count == self.repeat_write_count:
                self.repeat_count += 1
            else:
                self.repeat_key = key
                self.repeat_count = 1
                self.repeat_write_count = write_count
                self.repeat_last_error = False
            if self.repeat_count >= self.repeat_threshold and not self.repeat_last_error:
                args = key[1]
                if len(args) > 400:
                    args = args[:397] + "..."
                return (
                    "[Runtime recovery]\n"
                    f"The exact same {calls[0].tool_name} tool call has been requested "
                    f"{self.repeat_count} times in a row with identical arguments. I skipped "
                    "executing and replaying that duplicate call to avoid provider-side "
                    "rejection for repetitive tool history. Do not call this exact tool "
                    "with the same arguments again. Change the path, pattern, command, or "
                    "arguments; inspect a different source window; use a different tool; "
                    "or move to the patch/final answer if you already have enough evidence.\n"
                    f"Repeated arguments: {args}"
                )
        elif calls:
            self.repeat_key = None
            self.repeat_count = 0
            self.repeat_write_count = write_count
            self.repeat_last_error = False
        return None

    def observe_results(self, calls: Sequence[Any], results: Sequence[Any]) -> None:
        if self.enabled and len(calls) == 1 and self._key(calls[0]) == self.repeat_key:
            self.repeat_last_error = any(result.is_error for result in results)
