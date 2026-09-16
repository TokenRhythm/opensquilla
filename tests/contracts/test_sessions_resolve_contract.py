"""Golden-oracle checks for the v4 ``sessions.resolve`` Contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from opensquilla.contracts.generated.v4.sessions_resolve import (
    SessionsResolveRequestFrame,
    SessionsResolveResponseFrame,
    SessionsResolveResult,
)
from opensquilla.contracts.generated.v4.sessions_resolve_metadata import (
    SESSIONS_RESOLVE_METHOD,
    SESSIONS_RESOLVE_SCOPE,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "contracts" / "gateway" / "v4" / "sessions"
SCHEMA = CONTRACT_ROOT / "sessions-resolve.schema.json"
FIXTURES = CONTRACT_ROOT / "fixtures" / "sessions-resolve"


def _document(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _wire_cases(name: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        (str(case["id"]), case["wire"])
        for case in _document(name)["cases"]
        if "wire" in case
    ]


def test_contract_metadata_pins_current_query_semantics() -> None:
    method = json.loads(SCHEMA.read_text(encoding="utf-8"))["x-opensquilla-method"]

    assert method["name"] == SESSIONS_RESOLVE_METHOD == "sessions.resolve"
    assert method["kind"] == "query"
    assert method["scope"] == SESSIONS_RESOLVE_SCOPE == "operator.read"
    assert method["guestAllowed"] is False
    assert method["idempotency"] == "read-only"
    assert method["timeout"] == {"policy": "caller"}
    assert method["capability"] == {
        "kind": "method-availability",
        "name": "sessions.resolve",
    }


@pytest.mark.parametrize(("case_id", "wire"), _wire_cases("requests.json"))
def test_request_fixture_round_trips_exact_json_tree(
    case_id: str,
    wire: dict[str, Any],
) -> None:
    parsed = SessionsResolveRequestFrame.model_validate(wire)
    assert parsed.model_dump(mode="json", exclude_unset=True) == wire, case_id


@pytest.mark.parametrize(
    ("case_id", "wire"),
    _wire_cases("responses.json") + _wire_cases("errors.json"),
)
def test_response_fixture_round_trips_exact_json_tree(
    case_id: str,
    wire: dict[str, Any],
) -> None:
    parsed = SessionsResolveResponseFrame.model_validate(wire)
    assert parsed.model_dump(mode="json", exclude_unset=True) == wire, case_id


def test_result_allows_legacy_minimal_and_future_fields() -> None:
    legacy = {"session_key": "agent:main:canonical", "session_id": "canonical"}
    assert SessionsResolveResult.model_validate(legacy).model_dump(
        mode="json", exclude_unset=True
    ) == legacy

    future = {**legacy, "future": {"enabled": True}}
    assert SessionsResolveResult.model_validate(future).model_dump(
        mode="json", exclude_unset=True
    ) == future


def test_generated_result_allows_omission_of_non_nullable_optional_fields() -> None:
    result = SessionsResolveResult.model_validate(
        {"session_key": "agent:main:canonical", "session_id": "canonical"}
    )
    assert result.model_dump(mode="json", exclude_unset=True) == {
        "session_key": "agent:main:canonical",
        "session_id": "canonical",
    }


def test_generated_result_rejects_missing_identity() -> None:
    with pytest.raises(ValidationError):
        SessionsResolveResult.model_validate({"session_key": "only-key"})


def test_generated_integer_accepts_integral_float_like_ajv_and_preserves_tree() -> None:
    wire = {
        "session_key": "key",
        "session_id": "id",
        "created_at": 1000.0,
        "updated_at": 2000.0,
    }

    parsed = SessionsResolveResult.model_validate(wire)

    assert parsed.model_dump(mode="json", exclude_unset=True) == wire


@pytest.mark.parametrize("value", [1.5, True, "1000"])
def test_generated_integer_rejects_non_integral_or_coerced_values(value: Any) -> None:
    with pytest.raises(ValidationError):
        SessionsResolveResult.model_validate(
            {"session_key": "key", "session_id": "id", "created_at": value}
        )


@pytest.mark.parametrize(
    "field",
    ["status", "agent_id", "projectWorkspaceDeferred", "created_at", "updated_at"],
)
def test_generated_result_rejects_explicit_null_for_non_nullable_optional_fields(
    field: str,
) -> None:
    """Omission and explicit null are distinct in the language-neutral schema."""

    with pytest.raises(ValidationError):
        SessionsResolveResult.model_validate(
            {"session_key": "key", "session_id": "id", field: None}
        )


@pytest.mark.parametrize("field", ["model", "workspaceId"])
def test_generated_result_accepts_explicit_null_for_nullable_fields(field: str) -> None:
    SessionsResolveResult.model_validate(
        {"session_key": "key", "session_id": "id", field: None}
    )


def test_behavior_fixture_covers_lookup_and_ownership_invariants() -> None:
    cases = _document("behavior.json")["cases"]
    assert {case["coverage"] for case in cases} == {
        "lookup-order",
        "bounded-fallback",
        "ambiguity",
        "workspace-projection",
        "permissions",
    }
    assert all(case["expectation"] == "exact" for case in cases)
