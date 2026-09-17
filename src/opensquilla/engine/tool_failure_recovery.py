"""Per-turn recovery limits for failed tool operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from opensquilla.execution_status import runtime_execution_status
from opensquilla.tool_boundary import ToolCall, ToolResult

FAILURE_RECOVERY_CODE = "tool_failure_loop_exhausted"
FAILURE_RECOVERY_INSTRUCTION = (
    "Tool recovery has reached its limit. Do not call tools or change providers. "
    "Complete the user's request from the available evidence, including code and assertions "
    "when possible. Clearly distinguish unexecuted code from verified results. If execution "
    "is essential, explain the missing prerequisite and what the user must change."
)

_DIAGNOSTIC_METADATA_KEYS = frozenset({
    "attempt", "attempt_count", "retry", "retry_count", "timestamp", "started_at",
    "completed_at", "duration", "duration_ms", "elapsed", "elapsed_ms", "wall_time_seconds",
    "call_id", "request_id", "tool_use_id", "pid", "process_id",
})
_DIAGNOSTIC_COUNTER = re.compile(
    r"\b(?:attempt(?:s|_count)?|retr(?:y|ies|y_count)|pid|process_id)"
    r"\s*(?:[:=#]\s*)?\d+\b", re.IGNORECASE,
)
_DIAGNOSTIC_ID = re.compile(
    r"\b(?:call_id|request_id|tool_use_id)\s*[:=]\s*[^\s,;]+", re.IGNORECASE,
)
_DIAGNOSTIC_TIME = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}[T ])?\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_DIAGNOSTIC_DURATION = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ms|milliseconds?|s|secs?|seconds?)\b", re.IGNORECASE,
)


def _diagnostic_text(value: str) -> str:
    value = _DIAGNOSTIC_COUNTER.sub("<counter>", value)
    value = _DIAGNOSTIC_ID.sub("<call>", value)
    value = _DIAGNOSTIC_TIME.sub("<time>", value)
    return _DIAGNOSTIC_DURATION.sub("<duration>", value)


def _diagnostic_payload(value: Any, depth: int = 0) -> Any:
    if depth >= 32:
        return "<nested diagnostic>"
    if isinstance(value, dict):
        return {
            key: _diagnostic_payload(item, depth + 1)
            for key, item in value.items()
            if key.lower() not in _DIAGNOSTIC_METADATA_KEYS
        }
    if isinstance(value, list):
        return [_diagnostic_payload(item, depth + 1) for item in value]
    return _diagnostic_text(value) if isinstance(value, str) else value


def _failure_evidence(result: ToolResult) -> str:
    try:
        evidence = json.dumps(
            _diagnostic_payload(json.loads(result.content)), sort_keys=True, ensure_ascii=False,
        )
    except (ValueError, TypeError, RecursionError):
        evidence = _diagnostic_text(result.content)
    status = result.execution_status
    evidence = json.dumps([
        evidence,
        status.get("exit_code") if status is not None else None,
        status.get("reason") if status is not None else None,
    ])
    return hashlib.sha256(evidence.encode()).hexdigest()


@dataclass
class ToolFailureRecovery:
    """Keep failure evidence across model changes, but never across user turns.

    Ordinary failures get two retries. A declared permanent failure cannot be
    repeated unchanged; a different attempt at the same missing capability gets
    one opportunity. An observed repair and new failure evidence renew the
    ordinary streak together, at most three times before a successful verifier.
    Diagnostic metadata alone cannot renew it.
    """

    failures: dict[str, int] = field(default_factory=dict)
    nonretryable: set[str] = field(default_factory=set)
    unavailable: dict[str, int] = field(default_factory=dict)
    exhausted: bool = False
    last_code: str = ""
    _inflight: dict[str, int] = field(default_factory=dict)
    _admission: asyncio.Condition = field(default_factory=asyncio.Condition)
    _repair_generation: int = 0
    _failure_generation: dict[str, int] = field(default_factory=dict)
    _seen_evidence: dict[str, set[str]] = field(default_factory=dict)
    _progress_renewals: dict[str, int] = field(default_factory=dict)

    @staticmethod
    def call_key(call: ToolCall) -> str:
        arguments = json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(f"{call.tool_name}\0{arguments}".encode()).hexdigest()

    @asynccontextmanager
    async def dispatch_slot(self, call: ToolCall) -> AsyncIterator[None]:
        """Bound duplicate attempts without serializing successful tool pairs."""
        key = self.call_key(call)
        async with self._admission:
            await self._admission.wait_for(lambda: (
                self.exhausted or key in self.nonretryable
                or self.failures.get(key, 0) + self._inflight.get(key, 0) < 3
            ))
            self._inflight[key] = self._inflight.get(key, 0) + 1
        try:
            yield
        finally:
            async with self._admission:
                self._inflight[key] -= 1
                self._admission.notify_all()

    @property
    def terminal_message(self) -> str:
        prerequisite = (
            " A required runtime is unavailable; install it or correct the execution PATH."
            if self.last_code == "RUNTIME_UNAVAILABLE" else " Resolve the reported tool failure."
        )
        return (
            "Repeated tool failures exhausted recovery, and the model did not provide a "
            "final answer. Execution remains unverified." + prerequisite
            + " You can request a text-only solution or start a new turn after fixing the cause."
        )

    def before_call(self, call: ToolCall) -> ToolResult | None:
        if not self.exhausted and self.call_key(call) not in self.nonretryable:
            return None
        self.exhausted = True
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content=json.dumps({
                "status": "not_executed", "code": FAILURE_RECOVERY_CODE,
                "retryable": False, "recovery": FAILURE_RECOVERY_INSTRUCTION,
            }),
            is_error=True,
            execution_status=runtime_execution_status("error", reason=FAILURE_RECOVERY_CODE),
        )

    def observe(
        self, call: ToolCall, result: ToolResult, *, repair_observed: bool = False,
    ) -> None:
        key = self.call_key(call)
        failed = result.is_error or (
            result.execution_status is not None
            and result.execution_status.get("status") in {"error", "timeout"}
        )
        if not failed:
            self.failures.pop(key, None)
            self.nonretryable.discard(key)
            self._failure_generation.pop(key, None)
            self._seen_evidence.pop(key, None)
            self._progress_renewals.pop(key, None)
            if repair_observed or (
                result.effect_outcome is not None
                and result.effect_outcome.effect_state == "committed"
            ):
                # Reopen admission for a repair, but renew the failure streak
                # only after a subsequent verifier demonstrates a new outcome.
                self.nonretryable.clear()
                self._repair_generation += 1
            return
        payload: dict[str, Any] = {}
        try:
            decoded = json.loads(result.content)
            if isinstance(decoded, dict):
                payload = decoded
        except (ValueError, TypeError, RecursionError):
            pass
        session = payload.get("session")
        if isinstance(session, dict) and isinstance(session.get("runtime_failure"), dict):
            payload = session["runtime_failure"]
        code = payload.get("code")
        if code == FAILURE_RECOVERY_CODE:
            return
        self.last_code = code if isinstance(code, str) else ""
        count = self.failures.get(key, 0) + 1
        evidence = _failure_evidence(result)
        seen = self._seen_evidence.setdefault(key, set())
        if (
            seen and evidence not in seen
            and self._repair_generation > self._failure_generation.get(key, 0)
            and self._progress_renewals.get(key, 0) < 3
            and payload.get("retryable") is not False
            and code != "RUNTIME_UNAVAILABLE"
        ):
            count = 1
            # Novel diagnostics are evidence of progress, not proof: external
            # paths or payloads can vary even when the underlying fault does not.
            self._progress_renewals[key] = self._progress_renewals.get(key, 0) + 1
        seen.add(evidence)
        self._failure_generation[key] = self._repair_generation
        self.failures[key] = count
        if payload.get("retryable") is False:
            self.nonretryable.add(key)
        component = payload.get("componentId")
        if code == "RUNTIME_UNAVAILABLE" and isinstance(component, str) and component:
            # A different actual execution environment is a distinct repair
            # attempt. Command flags or permission escalation alone are not.
            capability_key = json.dumps(
                [component, call.arguments.get("workdir"), call.arguments.get("env")],
                sort_keys=True, default=str,
            )
            count = self.unavailable.get(capability_key, 0) + 1
            self.unavailable[capability_key] = count
            self.exhausted |= count >= 2
        self.exhausted |= self.failures[key] >= 3
