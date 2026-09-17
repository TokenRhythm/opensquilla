from __future__ import annotations

import pytest

from opensquilla.telemetry.privacy import (
    MAX_TELEMETRY_NESTING_DEPTH,
    ForbiddenTelemetryFieldError,
    assert_no_forbidden_fields,
    is_forbidden_field_name,
    normalize_field_name,
)


@pytest.mark.parametrize(
    "field",
    [
        "Prompt",
        "user_prompt",
        "file-path",
        "toolArgs",
        "task_parameters",
        "rawExceptionMessage",
        "full_stack_trace",
        "userId",
        "mac_address",
        "ip-address",
    ],
)
def test_sensitive_field_variants_are_forbidden(field: str) -> None:
    assert is_forbidden_field_name(field)


def test_normalization_handles_common_field_naming_styles() -> None:
    assert normalize_field_name("Raw-Exception_Message") == "rawexceptionmessage"


def test_nested_privacy_scan_rejects_without_echoing_submitted_value() -> None:
    submitted_value = "synthetic-private-value-that-must-not-be-echoed"

    with pytest.raises(ForbiddenTelemetryFieldError) as exc_info:
        assert_no_forbidden_fields(
            {"safe": [{"nested": {"tool_input": submitted_value}}]}
        )

    error = str(exc_info.value)
    assert "safe.0.nested.tool_input" in error
    assert submitted_value not in error


def test_purpose_specific_and_aggregate_fields_remain_allowed() -> None:
    assert_no_forbidden_fields(
        {
            "analytics_user_id": "00000000-0000-4000-8000-000000000000",
            "error_fingerprint": "bounded-code",
            "component": "gateway",
            "metrics": [{"slow_request_count": 1}],
        }
    )


def test_non_string_mapping_key_fails_closed_without_echoing_it() -> None:
    with pytest.raises(ForbiddenTelemetryFieldError) as exc_info:
        assert_no_forbidden_fields({7: "synthetic-private-value"})

    error = str(exc_info.value)
    assert "field names must be strings" in error
    assert "synthetic-private-value" not in error


def test_excessive_nesting_is_bounded() -> None:
    payload: object = "leaf"
    for _ in range(MAX_TELEMETRY_NESTING_DEPTH + 2):
        payload = {"safe": payload}

    with pytest.raises(ForbiddenTelemetryFieldError, match="depth limit"):
        assert_no_forbidden_fields(payload)
