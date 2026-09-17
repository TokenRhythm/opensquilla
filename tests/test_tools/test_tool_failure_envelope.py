from __future__ import annotations

import json

import httpx

from opensquilla.sandbox.types import SandboxBackendError
from opensquilla.tools.envelope import build_tool_failure_envelope
from opensquilla.tools.types import RetryableToolInputError, SafeToolError


def test_type_error_tool_failure_is_model_retriable_without_raw_traceback() -> None:
    envelope = build_tool_failure_envelope(
        TypeError("secret raw argument detail"),
        "write_file",
    )

    assert envelope["status"] == "error"
    assert envelope["tool"] == "write_file"
    assert envelope["error_class"] == "TypeError"
    assert envelope["retry_allowed"] is True
    assert "tool schema" in envelope["user_message"]
    assert "secret raw argument detail" not in envelope["user_message"]


def test_httpx_timeout_exception_is_retriable_with_specific_message() -> None:
    envelope = build_tool_failure_envelope(
        httpx.TimeoutException("secret endpoint"),
        "web_fetch",
    )

    assert envelope["error_class"] == "TimeoutException"
    assert envelope["retry_allowed"] is True
    assert "timed out" in envelope["user_message"]
    assert "secret endpoint" not in envelope["user_message"]


def test_httpx_connect_error_is_retriable_with_specific_message() -> None:
    envelope = build_tool_failure_envelope(
        httpx.ConnectError("secret host"),
        "web_fetch",
    )

    assert envelope["error_class"] == "ConnectError"
    assert envelope["retry_allowed"] is True
    assert "could not connect" in envelope["user_message"]
    assert "secret host" not in envelope["user_message"]


def test_json_decode_error_has_specific_message() -> None:
    envelope = build_tool_failure_envelope(
        json.JSONDecodeError("secret payload", "not-json", 0),
        "web_fetch",
    )

    assert envelope["error_class"] == "JSONDecodeError"
    assert envelope["retry_allowed"] is False
    assert "invalid response payload" in envelope["user_message"]
    assert "secret payload" not in envelope["user_message"]


def test_sandbox_backend_failure_is_terminal_and_tells_model_not_to_fallback() -> None:
    envelope = build_tool_failure_envelope(
        SandboxBackendError("secret ACL or lease diagnostics"),
        "list_dir",
    )

    assert envelope["error_class"] == "SandboxBackendError"
    assert envelope["retry_allowed"] is False
    assert "sandbox environment could not run" in envelope["user_message"].lower()
    assert "do not retry with another tool" in envelope["user_message"].lower()
    assert "secret ACL or lease diagnostics" not in envelope["user_message"]


def test_policy_denial_envelope_has_exactly_five_keys() -> None:
    envelope = build_tool_failure_envelope(
        PermissionError("raw denied detail"),
        "exec_command",
        policy_denial=True,
        error_class_override="PolicyDenied",
        user_message_override="Blocked by policy.",
    )

    assert set(envelope) == {
        "status",
        "tool",
        "error_class",
        "user_message",
        "retry_allowed",
    }
    assert envelope["status"] == "error"
    assert envelope["tool"] == "exec_command"
    assert envelope["error_class"] == "PolicyDenied"
    assert envelope["user_message"] == "Blocked by policy."
    assert envelope["retry_allowed"] is False


def test_safe_tool_error_instance_message_preserves_five_key_shape() -> None:
    envelope = build_tool_failure_envelope(
        SafeToolError("PDF file not found: input.pdf (resolved=/workspace/input.pdf)", "secret"),
        "pdf",
    )

    assert set(envelope) == {
        "status",
        "tool",
        "error_class",
        "user_message",
        "retry_allowed",
    }
    assert envelope["status"] == "error"
    assert envelope["tool"] == "pdf"
    assert envelope["error_class"] == "SafeToolError"
    assert "PDF file not found" in envelope["user_message"]
    assert "secret" not in envelope["user_message"]


def test_retryable_tool_input_error_marks_model_correctable_failures() -> None:
    envelope = build_tool_failure_envelope(
        RetryableToolInputError(
            "edit_file could not find old_text. Read the file and retry with exact text."
        ),
        "edit_file",
    )

    assert set(envelope) == {
        "status",
        "tool",
        "error_class",
        "user_message",
        "retry_allowed",
    }
    assert envelope["tool"] == "edit_file"
    assert envelope["error_class"] == "RetryableToolInputError"
    assert envelope["retry_allowed"] is True
    assert "retry with exact text" in envelope["user_message"]


def test_image_attachment_path_safe_tool_error_is_not_generic_internal_error() -> None:
    envelope = build_tool_failure_envelope(
        SafeToolError(
            "Image path is not accessible by the image tool: ab367.png. "
            "Pass a real local file path or HTTP(S) URL. If this is a chat attachment, "
            "answer from the attached image directly instead of calling the image tool.",
            "resolved=/secret/workspace/ab367.png",
        ),
        "image",
    )

    assert set(envelope) == {
        "status",
        "tool",
        "error_class",
        "user_message",
        "retry_allowed",
    }
    assert envelope["tool"] == "image"
    assert envelope["error_class"] == "SafeToolError"
    assert envelope["retry_allowed"] is False
    assert "not accessible by the image tool" in envelope["user_message"]
    assert "chat attachment" in envelope["user_message"]
    assert "internal error" not in envelope["user_message"]
    assert "secret" not in envelope["user_message"]
