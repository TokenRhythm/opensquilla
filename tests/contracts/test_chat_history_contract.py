"""Canonical contract assertions for strict chat.history cursor failures."""

from __future__ import annotations

import json
from pathlib import Path

from opensquilla.contracts.generated.v4.chat_history_metadata import (
    CHAT_HISTORY_ERRORS,
)
from opensquilla.contracts.generated.v4.gateway_contract_registry import (
    GATEWAY_METHOD_CONTRACTS,
)

ROOT = Path(__file__).resolve().parents[2]
ERROR_FIXTURE = ROOT / "tests/fixtures/gateway/chat_history/errors.json"
EXPECTED_CURSOR_ERRORS = {
    "HISTORY_CURSOR_INVALID",
    "HISTORY_CURSOR_INVALIDATED",
}


def test_chat_history_contract_declares_characterized_cursor_errors() -> None:
    fixture = json.loads(ERROR_FIXTURE.read_text(encoding="utf-8"))
    fixture_codes = {
        case["wire"]["code"]
        for case in fixture["cases"]
        if case["id"].startswith("error.history-cursor-")
    }
    metadata_codes = {str(error["code"]) for error in CHAT_HISTORY_ERRORS}
    registry_codes = {
        str(error["code"])
        for error in GATEWAY_METHOD_CONTRACTS["chat.history"].errors
    }

    assert fixture_codes == EXPECTED_CURSOR_ERRORS
    assert EXPECTED_CURSOR_ERRORS <= metadata_codes
    assert EXPECTED_CURSOR_ERRORS <= registry_codes
