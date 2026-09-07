"""Golden wire checks for the v4 TurnCommands Contract slice.

The fixtures intentionally contain both canonical and legacy spellings.  They
are consumed by the generated Python models here and by the WebUI Adapter
tests; neither side owns a second expected payload tree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from opensquilla.contracts.generated.v4.chat_abort import (
    ChatAbortRequestFrame,
    ChatAbortResponseFrame,
    ChatAbortResult,
)
from opensquilla.contracts.generated.v4.chat_send import (
    ChatSendRequestFrame,
    ChatSendResponseFrame,
    ChatSendResult,
)
from opensquilla.contracts.generated.v4.gateway_contract_registry import (
    GATEWAY_METHOD_CONTRACTS,
)
from opensquilla.contracts.generated.v4.pending_inputs_dispatch import (
    PendingInputsDispatchRequestFrame,
    PendingInputsDispatchResponseFrame,
    PendingInputsDispatchResult,
)
from opensquilla.contracts.generated.v4.pending_inputs_steer import (
    PendingInputsSteerRequestFrame,
    PendingInputsSteerResponseFrame,
    PendingInputsSteerResult,
)
from opensquilla.contracts.generated.v4.sessions_steer_v2 import (
    SessionsSteerV2RequestFrame,
    SessionsSteerV2ResponseFrame,
    SessionsSteerV2Result,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "contracts/gateway/v4/conversation/fixtures/turn-commands.json"


def _cases() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


MODEL_BY_METHOD: dict[str, tuple[type[Any], type[Any], type[Any]]] = {
    "chat.send": (ChatSendRequestFrame, ChatSendResponseFrame, ChatSendResult),
    "chat.abort": (ChatAbortRequestFrame, ChatAbortResponseFrame, ChatAbortResult),
    "sessions.pending_inputs.dispatch": (
        PendingInputsDispatchRequestFrame,
        PendingInputsDispatchResponseFrame,
        PendingInputsDispatchResult,
    ),
    "sessions.steer.v2": (
        SessionsSteerV2RequestFrame,
        SessionsSteerV2ResponseFrame,
        SessionsSteerV2Result,
    ),
    "sessions.pending_inputs.steer": (
        PendingInputsSteerRequestFrame,
        PendingInputsSteerResponseFrame,
        PendingInputsSteerResult,
    ),
}


@pytest.mark.parametrize("method", sorted(MODEL_BY_METHOD))
def test_turn_command_metadata_is_registered_once(method: str) -> None:
    descriptor = GATEWAY_METHOD_CONTRACTS[method]
    assert descriptor.name == method
    assert descriptor.kind == "command"
    assert descriptor.scope == "operator.write"
    assert descriptor.protocol == "opensquilla-websocket-json"
    assert descriptor.wire_version == 4
    request_model, response_model, result_model = MODEL_BY_METHOD[method]
    assert descriptor.request_model is request_model
    assert descriptor.response_model is response_model
    assert descriptor.result_model is result_model


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["id"])
def test_request_fixture_round_trips_exact_json_tree(case: dict[str, Any]) -> None:
    request = case.get("request")
    if request is None:
        pytest.skip("case has no request fixture")
    request_model = MODEL_BY_METHOD[case["method"]][0]
    parsed = request_model.model_validate(request)
    assert parsed.model_dump(mode="json", by_alias=True, exclude_unset=True) == request


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["id"])
def test_result_fixture_round_trips_exact_json_tree(case: dict[str, Any]) -> None:
    result = case.get("result")
    if result is None:
        pytest.skip("case has no result fixture")
    result_model = MODEL_BY_METHOD[case["method"]][2]
    parsed = result_model.model_validate(result)
    assert parsed.model_dump(mode="json", by_alias=True, exclude_unset=True) == result


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["id"])
def test_response_fixture_round_trips_exact_json_tree(case: dict[str, Any]) -> None:
    response = case.get("response") or case.get("error_response")
    if response is None:
        pytest.skip("case has no response fixture")
    response_model = MODEL_BY_METHOD[case["method"]][1]
    parsed = response_model.model_validate(response)
    assert parsed.model_dump(mode="json", by_alias=True, exclude_unset=True) == response


@pytest.mark.parametrize("request_model,method", [
    (ChatSendRequestFrame, "chat.send"),
    (ChatAbortRequestFrame, "chat.abort"),
    (PendingInputsDispatchRequestFrame, "sessions.pending_inputs.dispatch"),
    (SessionsSteerV2RequestFrame, "sessions.steer.v2"),
    (PendingInputsSteerRequestFrame, "sessions.pending_inputs.steer"),
])
def test_v4_request_models_keep_legacy_non_object_params_representable(
    request_model: type[Any],
    method: str,
) -> None:
    """The Adapter may observe malformed input without changing Gateway errors."""

    for value in ([], "legacy", 7, False, None):
        frame = {
            "type": "req",
            "id": "legacy",
            "method": method,
            "params": value,
        }
        parsed = request_model.model_validate(frame)
        assert parsed.model_dump(mode="json", by_alias=True, exclude_unset=True) == frame
