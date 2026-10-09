from __future__ import annotations

import json

from opensquilla.engine import ToolResult
from opensquilla.engine.subagent_failure_fallback import (
    SubagentFailureFallback,
    build_subagent_failure_json,
)


def _result(
    tool: str,
    content: str,
    *,
    is_error: bool = False,
) -> ToolResult:
    return ToolResult(
        tool_use_id=f"{tool}-id",
        tool_name=tool,
        content=content,
        is_error=is_error,
    )


def test_fallback_stops_after_three_consecutive_failed_tool_rounds() -> None:
    fallback = SubagentFailureFallback(threshold=3)

    assert fallback.observe([_result("read_file", "missing", is_error=True)]) is None
    assert fallback.observe([_result("exec_command", "exit 1", is_error=True)]) is None
    rendered = fallback.observe([_result("web_fetch", "HTTP 503", is_error=True)])

    assert rendered is not None
    assert json.loads(rendered) == {
        "error": "Three consecutive tool rounds failed without progress."
    }


def test_successful_tool_progress_resets_the_consecutive_failure_count() -> None:
    fallback = SubagentFailureFallback(threshold=3)

    assert fallback.observe([_result("read_file", "missing", is_error=True)]) is None
    assert fallback.observe([_result("read_file", "file contents")]) is None
    assert fallback.observe([_result("exec_command", "exit 1", is_error=True)]) is None
    assert fallback.observe([_result("web_fetch", "HTTP 503", is_error=True)]) is None


def test_empty_web_search_results_join_the_same_generic_fallback() -> None:
    fallback = SubagentFailureFallback(threshold=1)

    rendered = fallback.observe(
        [_result("web_search", '{"ok": true, "results": [], "sources": []}')]
    )

    assert rendered is not None
    assert json.loads(rendered) == {
        "error": "One consecutive tool round failed without progress."
    }


def test_web_fetch_payload_error_joins_the_generic_fallback() -> None:
    fallback = SubagentFailureFallback(threshold=1)

    rendered = fallback.observe(
        [
            _result(
                "web_fetch",
                json.dumps(
                    {
                        "url": "https://example.com/story",
                        "final_url": "https://example.com/story",
                        "status": 503,
                        "text": "",
                        "error": "rate-limited or blocked upstream",
                    }
                ),
            )
        ]
    )

    assert rendered is not None
    assert json.loads(rendered) == {
        "error": "One consecutive tool round failed without progress."
    }


def test_non_tool_terminal_failure_uses_the_same_standard_shape() -> None:
    rendered = build_subagent_failure_json(
        reason_code="provider_overloaded",
        reason="The model provider is temporarily overloaded.",
        next_step="Retry later.",
    )

    assert json.loads(rendered) == {
        "error": "The model provider is temporarily overloaded."
    }
