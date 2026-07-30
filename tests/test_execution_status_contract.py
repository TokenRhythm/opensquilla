from __future__ import annotations

import importlib
import json


def _execution_status_module():
    return importlib.import_module("opensquilla.execution_status")


def test_execution_status_defaults_to_unknown_normal_status() -> None:
    module = _execution_status_module()

    status = module.normalize_execution_status(None)

    assert status == {
        "version": 1,
        "status": "unknown",
        "exit_code": None,
        "timed_out": False,
        "truncated": False,
        "reason": None,
        "source": "unknown",
        "preservation_class": "normal",
    }


def test_execution_status_rejects_invalid_status_with_fallback_reason() -> None:
    module = _execution_status_module()

    status = module.normalize_execution_status(
        {
            "version": 1,
            "status": "not-a-status",
            "source": "adapter",
            "preservation_class": "normal",
        }
    )

    assert status["status"] == "unknown"
    assert status["reason"] == "invalid_status"


def test_derive_is_error_is_true_only_for_terminal_failures() -> None:
    module = _execution_status_module()

    assert module.derive_is_error({"status": "success"}) is False
    assert module.derive_is_error({"status": "unknown"}) is False
    assert module.derive_is_error({"status": "error"}) is True
    assert module.derive_is_error({"status": "timeout"}) is True
    assert module.derive_is_error({"status": "cancelled"}) is True


def test_execution_status_truncated_does_not_change_success_status() -> None:
    module = _execution_status_module()

    status = module.normalize_execution_status(
        {
            "version": 1,
            "status": "success",
            "exit_code": 0,
            "timed_out": False,
            "truncated": True,
            "reason": None,
            "source": "adapter",
            "preservation_class": "retain_summary",
        }
    )

    assert status["status"] == "success"
    assert status["truncated"] is True
    assert module.derive_is_error(status) is False


def test_legacy_error_normalizes_to_diagnostic_execution_status() -> None:
    module = _execution_status_module()

    status = module.normalize_legacy_execution_status(is_error=True)

    assert status["version"] == 1
    assert status["status"] == "error"
    assert status["source"] == "legacy"
    assert status["reason"] == "legacy_missing_status"
    assert status["preservation_class"] == "diagnostic"


def test_legacy_non_error_normalizes_to_unknown_normal_status() -> None:
    module = _execution_status_module()

    status = module.normalize_legacy_execution_status(is_error=False)

    assert status["version"] == 1
    assert status["status"] == "unknown"
    assert status["source"] == "legacy"
    assert status["reason"] == "legacy_missing_status"
    assert status["preservation_class"] == "normal"


def test_web_search_explicit_failure_maps_to_error_status() -> None:
    module = _execution_status_module()

    status = module.execution_status_for_tool_result(
        "web_search",
        json.dumps(
            {
                "ok": False,
                "error_kind": "blocked",
                "retry_allowed": False,
                "results": [],
            }
        ),
    )

    assert status is not None
    assert status["status"] == "error"
    assert status["reason"] == "search_blocked"
    assert status["source"] == "adapter"
    assert status["preservation_class"] == "diagnostic"
    assert module.derive_is_error(status) is True


def test_web_discover_explicit_timeout_maps_to_timeout_status() -> None:
    module = _execution_status_module()

    status = module.execution_status_for_tool_result(
        "web_discover",
        json.dumps(
            {
                "ok": False,
                "error_kind": "timeout",
                "retry_allowed": True,
                "results": [],
            }
        ),
    )

    assert status is not None
    assert status["status"] == "timeout"
    assert status["timed_out"] is True
    assert status["reason"] == "search_timeout"
    assert module.derive_is_error(status) is True


def test_web_outcome_mapper_only_trusts_explicit_failures_from_search_tools() -> None:
    module = _execution_status_module()
    failure = json.dumps({"ok": False, "error_kind": "network"})

    assert module.execution_status_for_tool_result("untrusted_tool", failure) is None
    assert (
        module.execution_status_for_tool_result(
            "web_search",
            json.dumps({"ok": True, "results": []}),
        )
        is None
    )
    assert (
        module.execution_status_for_tool_result(
            "web_search",
            json.dumps({"error_kind": "network", "results": []}),
        )
        is None
    )
