"""Golden-oracle checks for the formal v4 ``sessions.list`` Contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from opensquilla.contracts.generated.v4.sessions_list import (
    SessionsListRequestFrame,
    SessionsListResponseFrame,
)
from opensquilla.contracts.generated.v4.sessions_list_metadata import (
    SESSIONS_LIST_METHOD,
    SESSIONS_LIST_SCOPE,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "contracts" / "gateway" / "v4" / "sessions"
SCHEMA = CONTRACT_ROOT / "sessions-list.schema.json"
FIXTURES = CONTRACT_ROOT / "fixtures"
GENERATOR = ROOT / "scripts" / "contracts" / "generate_sessions_list_contract.py"
AJV_GENERATOR = ROOT / "scripts" / "contracts" / "generate_sessions_list_ajv.mjs"
GENERATED_ARTIFACTS = (
    ROOT / "src" / "opensquilla" / "contracts" / "generated" / "v4" / "sessions_list.py",
    ROOT / "src" / "opensquilla" / "contracts" / "generated" / "v4" / "sessions_list_metadata.py",
    ROOT / "opensquilla-webui" / "src" / "contracts" / "generated" / "v4" / "sessionsList.ts",
    ROOT
    / "opensquilla-webui"
    / "src"
    / "contracts"
    / "generated"
    / "v4"
    / "sessionsListValidators.cjs",
    ROOT
    / "opensquilla-webui"
    / "src"
    / "contracts"
    / "generated"
    / "v4"
    / "sessionsListValidators.d.cts",
)


def _document(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _wire_cases(name: str) -> list[tuple[str, dict[str, Any]]]:
    return [(str(case["id"]), case["wire"]) for case in _document(name)["cases"] if "wire" in case]


def test_contract_metadata_is_the_single_method_oracle() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    metadata = schema["x-opensquilla-method"]

    assert metadata["name"] == "sessions.list" == SESSIONS_LIST_METHOD
    assert metadata["scope"] == "operator.read" == SESSIONS_LIST_SCOPE
    assert metadata["guestAllowed"] is True
    assert metadata["idempotency"] == "read-only"
    assert metadata["request"] == "#/$defs/SessionsListRequestFrame"
    assert metadata["response"] == "#/$defs/SessionsListResponseFrame"


def test_generated_artifact_headers_match_contract_and_generator() -> None:
    source_digest = hashlib.sha256(SCHEMA.read_bytes()).hexdigest()
    generator_digest = hashlib.sha256(
        GENERATOR.read_bytes() + b"\0" + AJV_GENERATOR.read_bytes()
    ).hexdigest()

    # The frozen validator pair is checked byte-for-byte by the toolchain
    # suite; only the complete Python/TS types remain in production.
    for artifact in GENERATED_ARTIFACTS[:3]:
        header = "\n".join(artifact.read_text(encoding="utf-8").splitlines()[:4])
        assert f"source-sha256: {source_digest}" in header, artifact
        assert f"generator-sha256: {generator_digest}" in header, artifact


@pytest.mark.parametrize(("case_id", "wire"), _wire_cases("requests.json"))
def test_request_fixture_round_trips_exact_json_tree(
    case_id: str,
    wire: dict[str, Any],
) -> None:
    parsed = SessionsListRequestFrame.model_validate(wire)
    assert parsed.model_dump(mode="json", exclude_unset=True) == wire, case_id


@pytest.mark.parametrize(
    ("case_id", "wire"),
    _wire_cases("responses.json") + _wire_cases("errors.json"),
)
def test_response_fixture_round_trips_exact_json_tree(
    case_id: str,
    wire: dict[str, Any],
) -> None:
    parsed = SessionsListResponseFrame.model_validate(wire)
    assert parsed.model_dump(mode="json", exclude_unset=True) == wire, case_id


@pytest.mark.parametrize(
    "partial_alias",
    [
        {"hasMore": True},
        {"has_more": False},
        {"nextCursor": "opaque"},
        {"next_cursor": None},
        {"totalCount": 7},
        {"total_count": 7},
    ],
)
def test_generated_python_accepts_independently_optional_v4_aliases(
    partial_alias: dict[str, Any],
) -> None:
    wire = {
        "type": "res",
        "id": "partial-alias",
        "ok": True,
        "payload": {"sessions": [], "count": 0, "ts": 1, **partial_alias},
        "error": None,
    }

    parsed = SessionsListResponseFrame.model_validate(wire)

    assert parsed.model_dump(mode="json", exclude_unset=True) == wire


def test_generated_python_rejects_incomplete_success_payload() -> None:
    with pytest.raises(ValidationError):
        SessionsListResponseFrame.model_validate(
            {
                "type": "res",
                "id": "missing-fields",
                "ok": True,
                "payload": {"sessions": []},
                "error": None,
            }
        )


def test_task_parent_wire_fields_are_explicit_and_cost_is_not_sessions_list() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    definitions = schema["$defs"]
    task_fields = set(definitions["SessionTaskRecord"]["properties"])
    row_fields = set(definitions["SessionRow"]["properties"])

    assert {
        "task_id",
        "turn_id",
        "status",
        "queue_mode",
        "run_kind",
        "source_kind",
        "created_at",
        "started_at",
        "finished_at",
        "turn_outcome",
        "cancel_requested",
        "terminal_reason",
        "terminal_message",
    }.issubset(task_fields)
    assert definitions["SessionParentRecord"]["properties"] == {
        "key": {"type": "string"},
        "taskId": {"type": "string"},
        "spawnDepth": {"type": "integer"},
    }
    assert definitions["SessionRow"]["properties"]["tasks"] == {
        "type": "array",
        "items": {"$ref": "#/$defs/SessionTaskRecord"},
    }
    assert (
        not {
            "costUsd",
            "cost_usd",
            "totalCostUsd",
            "total_cost_usd",
        }
        & row_fields
    )


def test_query_fixture_ids_and_coverage_are_explicit_and_unique() -> None:
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
        "params-omitted",
        "params-null",
        "legacy-limit",
        "page-first",
        "page-middle",
        "page-last",
        "count",
        "guest-filter",
        "no-scope",
        "STORAGE_BUSY",
        "full-open-row",
        "current-nonempty-task-parent",
        "legacy-string-channel",
    }.issubset({case["coverage"] for case in cases})
