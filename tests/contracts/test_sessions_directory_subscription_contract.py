"""Wire and metadata checks for the v4 session-directory lease methods."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from opensquilla.contracts.generated.v4.gateway_contract_registry import (
    GATEWAY_METHOD_CONTRACTS,
)
from opensquilla.contracts.generated.v4.sessions_subscribe import (
    SessionsSubscribeRequestFrame,
    SessionsSubscribeResponseFrame,
    SessionsSubscribeResult,
)
from opensquilla.contracts.generated.v4.sessions_subscribe_metadata import (
    SESSIONS_SUBSCRIBE_METHOD,
    SESSIONS_SUBSCRIBE_SCOPE,
)
from opensquilla.contracts.generated.v4.sessions_unsubscribe import (
    SessionsUnsubscribeRequestFrame,
    SessionsUnsubscribeResponseFrame,
    SessionsUnsubscribeResult,
)
from opensquilla.contracts.generated.v4.sessions_unsubscribe_metadata import (
    SESSIONS_UNSUBSCRIBE_METHOD,
    SESSIONS_UNSUBSCRIBE_SCOPE,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "contracts" / "gateway" / "v4" / "sessions"
OMIT = object()


@pytest.mark.parametrize(
    ("method", "scope", "schema_name"),
    [
        (SESSIONS_SUBSCRIBE_METHOD, SESSIONS_SUBSCRIBE_SCOPE, "sessions-subscribe"),
        (SESSIONS_UNSUBSCRIBE_METHOD, SESSIONS_UNSUBSCRIBE_SCOPE, "sessions-unsubscribe"),
    ],
)
def test_lease_method_metadata_matches_existing_gateway_semantics(
    method: str,
    scope: str,
    schema_name: str,
) -> None:
    document = json.loads(
        (CONTRACT_ROOT / f"{schema_name}.schema.json").read_text(encoding="utf-8")
    )
    metadata = document["x-opensquilla-method"]
    descriptor = GATEWAY_METHOD_CONTRACTS[method]

    assert metadata["name"] == method
    assert metadata["kind"] == "command"
    assert metadata["scope"] == scope == "operator.read"
    assert metadata["guestAllowed"] is False
    assert metadata["idempotency"] == "idempotent"
    assert metadata["timeout"] == {"policy": "caller"}
    assert metadata["capability"] == {
        "kind": "method-availability",
        "name": method,
    }
    assert descriptor.name == method
    assert descriptor.scope == scope
    assert descriptor.guest_allowed is False


@pytest.mark.parametrize(
    ("model", "method"),
    [
        (SessionsSubscribeRequestFrame, SESSIONS_SUBSCRIBE_METHOD),
        (SessionsUnsubscribeRequestFrame, SESSIONS_UNSUBSCRIBE_METHOD),
    ],
)
@pytest.mark.parametrize(
    "params",
    [
        pytest.param(OMIT, id="omitted"),
        pytest.param({}, id="empty-object"),
        pytest.param(None, id="explicit-null"),
        pytest.param([], id="legacy-array"),
        pytest.param("legacy", id="legacy-string"),
        pytest.param(7, id="legacy-number"),
        pytest.param(True, id="legacy-boolean"),
    ],
)
def test_request_models_preserve_legacy_params_shapes(
    model: type[Any],
    method: str,
    params: Any,
) -> None:
    wire: dict[str, Any] = {"type": "req", "id": "lease-1", "method": method}
    if params is not OMIT:
        wire["params"] = params
    parsed = model.model_validate(wire)
    assert parsed.model_dump(mode="json", exclude_unset=True) == wire


@pytest.mark.parametrize(
    ("response_model", "result_model"),
    [
        (SessionsSubscribeResponseFrame, SessionsSubscribeResult),
        (SessionsUnsubscribeResponseFrame, SessionsUnsubscribeResult),
    ],
)
def test_null_result_and_error_frames_round_trip_exactly(
    response_model: type[Any],
    result_model: type[Any],
) -> None:
    success = {
        "type": "res",
        "id": "lease-1",
        "ok": True,
        "payload": None,
    }
    error = {
        "type": "res",
        "id": "lease-1",
        "ok": False,
        "error": {
            "code": "UNAUTHORIZED",
            "message": "guest denied",
            "retryable": False,
        },
    }
    assert response_model.model_validate(success).model_dump(
        mode="json", exclude_unset=True
    ) == success
    assert response_model.model_validate(error).model_dump(
        mode="json", exclude_unset=True
    ) == error
    assert result_model.model_validate(None).root is None


def test_generated_lease_models_reject_wrong_method_and_non_null_result() -> None:
    with pytest.raises(ValidationError):
        SessionsSubscribeRequestFrame.model_validate(
            {"type": "req", "id": "bad", "method": "sessions.unsubscribe"}
        )
    with pytest.raises(ValidationError):
        SessionsUnsubscribeResult.model_validate({"subscribed": True})
