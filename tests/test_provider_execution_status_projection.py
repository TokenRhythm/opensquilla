from __future__ import annotations

import json

from opensquilla.execution_status import execution_status_for_tool_result
from opensquilla.provider.anthropic import _build_message_payload
from opensquilla.provider.openai import _build_openai_messages
from opensquilla.provider.types import ContentBlockToolResult, Message


def _failure_status() -> dict[str, object]:
    return {
        "version": 1,
        "status": "error",
        "exit_code": 1,
        "timed_out": False,
        "truncated": True,
        "reason": "nonzero_exit",
        "source": "adapter",
        "preservation_class": "diagnostic",
    }


def test_anthropic_projects_native_is_error_from_execution_status() -> None:
    message = Message(
        role="user",
        content=[
            ContentBlockToolResult(
                tool_use_id="call_provider_1",
                content="command failed",
                is_error=False,
                execution_status=_failure_status(),
            )
        ],
    )

    payload = _build_message_payload(message)

    assert payload["content"][0]["is_error"] is True


def test_openai_failure_tool_result_includes_bounded_execution_status_envelope() -> None:
    large_output = "failure details\n" + ("x" * 20_000)
    message = Message(
        role="user",
        content=[
            ContentBlockToolResult(
                tool_use_id="call_provider_2",
                content=large_output,
                is_error=True,
                execution_status=_failure_status(),
            )
        ],
    )

    payload = _build_openai_messages(message)

    tool_content = json.loads(payload[0]["content"])
    assert tool_content["execution_status"] == {
        "version": 1,
        "status": "error",
        "exit_code": 1,
        "timed_out": False,
        "truncated": True,
        "reason": "nonzero_exit",
    }
    assert tool_content["output"].startswith("failure details")
    assert len(tool_content["output"]) < len(large_output)


def test_search_failure_status_projects_as_provider_visible_tool_error() -> None:
    content = json.dumps(
        {
            "ok": False,
            "error_kind": "auth",
            "retry_allowed": False,
            "results": [],
        }
    )
    status = execution_status_for_tool_result("web_search", content)
    assert status is not None
    message = Message(
        role="user",
        content=[
            ContentBlockToolResult(
                tool_use_id="call_search_failure",
                content=content,
                is_error=False,
                execution_status=status,
            )
        ],
    )

    anthropic_payload = _build_message_payload(message)
    openai_payload = _build_openai_messages(message)
    openai_content = json.loads(openai_payload[0]["content"])

    assert anthropic_payload["content"][0]["is_error"] is True
    assert openai_content["execution_status"]["status"] == "error"
    assert openai_content["execution_status"]["reason"] == "search_auth"
    assert json.loads(openai_content["output"])["ok"] is False
