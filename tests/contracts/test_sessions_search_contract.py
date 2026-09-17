"""Golden-oracle checks for the v4 ``sessions.search`` Contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from opensquilla.contracts.generated.v4.sessions_search import (
    SessionsSearchRequestFrame,
    SessionsSearchResponseFrame,
    SessionsSearchResult,
)
from opensquilla.contracts.generated.v4.sessions_search_metadata import (
    SESSIONS_SEARCH_METHOD,
    SESSIONS_SEARCH_SCOPE,
)

SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "contracts/gateway/v4/sessions/sessions-search.schema.json"
)
FIXTURES = SCHEMA.parent / "fixtures" / "sessions-search"


def _document(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _wire_cases(name: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        (str(case["id"]), case["wire"])
        for case in _document(name)["cases"]
        if "wire" in case
    ]


def _result() -> dict[str, Any]:
    return {
        "sessions": [
            {
                "key": "agent:main:s1",
                "title": "Deploy",
                "effectiveAgentId": "main",
                "surface": "webchat",
                "updatedAt": 1000,
            }
        ],
        "messages": [
            {
                "key": "agent:main:s2",
                "title": "Groceries",
                "role": "user",
                "snippet": "buy >>>milk<<<",
                "createdAt": 2000,
            }
        ],
        "query": "milk",
        "ts": 3000,
    }


def test_contract_metadata_pins_search_semantics() -> None:
    method = json.loads(SCHEMA.read_text(encoding="utf-8"))["x-opensquilla-method"]
    assert method["name"] == SESSIONS_SEARCH_METHOD == "sessions.search"
    assert method["kind"] == "query"
    assert method["scope"] == SESSIONS_SEARCH_SCOPE == "operator.read"
    assert method["guestAllowed"] is False
    assert method["idempotency"] == "read-only"
    assert method["timeout"] == {"policy": "caller"}
    assert method["capability"] == {
        "kind": "method-availability",
        "name": "sessions.search",
    }
    assert {error["code"] for error in method["errors"]} == {
        "UNAUTHORIZED",
        "STORAGE_BUSY",
        "INTERNAL_ERROR",
    }


@pytest.mark.parametrize(("case_id", "wire"), _wire_cases("requests.json"))
def test_request_fixtures_round_trip_exact_json_tree(
    case_id: str,
    wire: dict[str, Any],
) -> None:
    parsed = SessionsSearchRequestFrame.model_validate(wire)
    assert parsed.model_dump(mode="json", exclude_unset=True) == wire, case_id


@pytest.mark.parametrize(
    ("case_id", "wire"),
    _wire_cases("responses.json") + _wire_cases("errors.json"),
)
def test_response_fixtures_round_trip_exact_json_tree(
    case_id: str,
    wire: dict[str, Any],
) -> None:
    parsed = SessionsSearchResponseFrame.model_validate(wire)
    assert parsed.model_dump(mode="json", exclude_unset=True) == wire, case_id


def test_result_allows_additive_fields_and_rejects_missing_shape() -> None:
    payload = {**_result(), "future": {"enabled": True}}
    assert (
        SessionsSearchResult.model_validate(payload).model_dump(
            mode="json", exclude_unset=True
        )
        == payload
    )
    with pytest.raises(ValidationError):
        SessionsSearchResult.model_validate({"sessions": [], "messages": [], "query": "x"})


def test_fixture_ids_and_behavior_coverage_are_explicit_and_unique() -> None:
    documents = [
        _document("requests.json"),
        _document("responses.json"),
        _document("errors.json"),
        _document("behavior.json"),
    ]
    cases = [case for document in documents for case in document["cases"]]
    case_ids = [case["id"] for case in cases]

    assert len(case_ids) == len(set(case_ids))
    assert all(case.get("coverage") for case in cases)
    assert all(case.get("expectation") in {"exact", "observation"} for case in cases)
    assert {
        "object-query-and-limit",
        "legacy-omitted-params",
        "legacy-null-params",
        "legacy-string-limit",
        "legacy-non-object-array",
        "ascii-transcript-search",
        "non-ascii-transcript-search",
        "title-content-deduplication",
        "operator-read-scope-and-guest-denial",
        "UNAUTHORIZED",
        "STORAGE_BUSY",
        "INTERNAL_ERROR",
    }.issubset({case["coverage"] for case in cases})
