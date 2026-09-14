"""Background completion notices use the latest receipt for each process."""

from __future__ import annotations

import json
from typing import Any

import pytest

from opensquilla.engine.turn_runner.runtime_notices import (
    unconfirmed_action_notice,
    with_unconfirmed_action_notice,
)


def _started(session_id: str = "process-a") -> dict[str, Any]:
    return {
        "type": "tool_result",
        "name": "background_process",
        "result": f"session_id={session_id}\ncommand: synthetic-job\nstatus: running",
        "execution_status": {"status": "unknown", "reason": "background_running"},
    }


def _receipt(status: str, session_id: str = "process-a") -> dict[str, Any]:
    return {
        "type": "tool_result",
        "name": "process",
        "result": json.dumps({
            "status": "ok",
            "action": "wait",
            "exited": status != "unknown",
            "session": {"session_id": session_id},
        }),
        "execution_status": {
            "status": status,
            "reason": "background_running" if status == "unknown" else None,
        },
    }


@pytest.mark.parametrize("status", ["success", "error", "timeout", "cancelled"])
def test_terminal_receipt_clears_running_notice(status: str) -> None:
    segments = [_started(), _receipt("unknown"), _receipt(status)]

    assert unconfirmed_action_notice("Process finished.", segments) is None
    assert with_unconfirmed_action_notice("Process finished.", segments) == "Process finished."


def test_unrelated_terminal_receipt_keeps_running_process_notice() -> None:
    segments = [_started(), _started("process-b"), _receipt("success", "process-b")]

    assert "background_process" in (unconfirmed_action_notice("", segments) or "")


def test_later_running_receipt_is_not_cleared_by_an_earlier_terminal_receipt() -> None:
    segments = [_receipt("success"), _started()]

    assert unconfirmed_action_notice("", segments) is not None


@pytest.mark.parametrize("result", ["bad json", "[]", '{"session":{}}'])
def test_terminal_receipt_without_process_identity_does_not_clear_notice(result: str) -> None:
    receipt = _receipt("success")
    receipt["result"] = result

    assert unconfirmed_action_notice("", [_started(), receipt]) is not None


def test_unidentified_legacy_running_result_remains_unconfirmed() -> None:
    started = _started()
    started["result"] = "status: running"

    assert unconfirmed_action_notice("", [started, _receipt("success")]) is not None


def test_repeated_running_receipts_emit_one_notice() -> None:
    notice = with_unconfirmed_action_notice("Waiting.", [_started(), _receipt("unknown")])

    assert notice.count("could not confirm") == 1
    assert with_unconfirmed_action_notice(notice, [_started()]) == notice


@pytest.mark.parametrize("status", ["error", "timeout", "cancelled"])
def test_failed_wait_without_process_exit_does_not_clear_notice(status: str) -> None:
    receipt = _receipt(status)
    receipt["result"] = json.dumps({
        "action": "wait",
        "exited": False,
        "session": {"session_id": "process-a", "status": "running", "returncode": None},
    })

    assert unconfirmed_action_notice("", [_started(), receipt]) is not None


@pytest.mark.parametrize(
    "session_status,returncode", [("done", 0), ("timed_out", -15), ("killed", -9)]
)
def test_terminal_poll_receipt_clears_notice(session_status: str, returncode: int) -> None:
    receipt = _receipt("success" if returncode == 0 else "error")
    receipt["result"] = json.dumps({
        "action": "poll",
        "session": {
            "session_id": "process-a", "status": session_status, "returncode": returncode,
        },
    })

    assert unconfirmed_action_notice("", [_started(), receipt]) is None
