"""Bounded failure fallback for subagent tool loops."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

_WEB_SEARCH_TOOLS = frozenset({"web_search", "web_discover"})
_WEB_FETCH_TOOLS = frozenset({"web_fetch"})
_MAX_FAILED_TOOLS = 12
_MAX_ERROR_CHARS = 500
DEFAULT_SUBAGENT_FAILURE_FALLBACK_THRESHOLD = 3
DEFAULT_SUBAGENT_MAX_ITERATIONS = 16


def build_subagent_failure_json(
    *,
    reason_code: str,
    reason: str,
    failed_tools: list[dict[str, str]] | None = None,
    partial_result: Any = None,
    next_step: str,
) -> str:
    """Return the stable, minimal failure contract sent back to a parent."""

    return json.dumps(
        {"error": _minimal_error(reason)},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _json_mapping(content: str) -> dict[str, Any] | None:
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def web_fetch_payload_failure(payload: Mapping[str, Any]) -> str | None:
    """Return the built-in web_fetch failure reason, if present."""

    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()
    status = payload.get("status")
    if isinstance(status, int) and not isinstance(status, bool):
        if status == 0 or status >= 400:
            return f"http_status_{status}"
    return None


def _result_failure(result: Any) -> dict[str, str] | None:
    tool = str(getattr(result, "tool_name", "") or "unknown")
    content = str(getattr(result, "content", "") or "").strip()
    execution_status = getattr(result, "execution_status", None)
    status_reason = (
        str(execution_status.get("reason") or "").strip()
        if isinstance(execution_status, dict)
        else ""
    )

    if bool(getattr(result, "is_error", False)):
        return {
            "tool": tool,
            "error": _minimal_error(content or status_reason or "tool_error"),
        }
    if not content:
        return {"tool": tool, "error": "empty_result"}

    if tool in _WEB_SEARCH_TOOLS:
        payload = _json_mapping(content)
        if payload is not None:
            if payload.get("ok") is False:
                error = payload.get("error")
                if isinstance(error, dict):
                    error = error.get("message") or error.get("kind")
                return {
                    "tool": tool,
                    "error": _minimal_error(
                        str(error or payload.get("error_kind") or "tool_error")
                    ),
                }
            results = payload.get("results")
            if isinstance(results, list) and not results:
                return {"tool": tool, "error": "no_results"}
    if tool in _WEB_FETCH_TOOLS:
        payload = _json_mapping(content)
        if payload is not None:
            failure = web_fetch_payload_failure(payload)
            if failure is not None:
                return {"tool": tool, "error": _minimal_error(failure)}
    return None


def _minimal_error(error: str) -> str:
    text = " ".join(error.split())
    if len(text) <= _MAX_ERROR_CHARS:
        return text
    return text[: _MAX_ERROR_CHARS - 3] + "..."


@dataclass
class SubagentFailureFallback:
    """Track consecutive all-failed tool rounds and render one terminal result."""

    threshold: int
    consecutive_failed_rounds: int = 0
    failed_tools: list[dict[str, str]] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.threshold > 0

    def observe(self, results: list[Any]) -> str | None:
        if not self.enabled:
            return None

        failures = [_result_failure(result) for result in results]
        if not failures or any(failure is None for failure in failures):
            self.consecutive_failed_rounds = 0
            self.failed_tools.clear()
            return None

        self.consecutive_failed_rounds += 1
        for failure in failures:
            if (
                failure is not None
                and failure not in self.failed_tools
                and len(self.failed_tools) < _MAX_FAILED_TOOLS
            ):
                self.failed_tools.append(failure)
        if self.consecutive_failed_rounds < self.threshold:
            return None

        count = {1: "One", 2: "Two", 3: "Three"}.get(
            self.consecutive_failed_rounds,
            str(self.consecutive_failed_rounds),
        )
        round_label = "round" if self.consecutive_failed_rounds == 1 else "rounds"
        return build_subagent_failure_json(
            reason_code="tool_failure",
            reason=(
                f"{count} consecutive tool {round_label} failed "
                "without progress."
            ),
            failed_tools=self.failed_tools,
            next_step="Retry with corrected inputs, required access, or a different approach.",
        )


__all__ = [
    "DEFAULT_SUBAGENT_FAILURE_FALLBACK_THRESHOLD",
    "DEFAULT_SUBAGENT_MAX_ITERATIONS",
    "SubagentFailureFallback",
    "build_subagent_failure_json",
    "web_fetch_payload_failure",
]
