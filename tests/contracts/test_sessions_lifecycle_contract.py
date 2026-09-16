"""Golden wire and metadata checks for the session lifecycle Contract slice."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from opensquilla.contracts.generated.v4.gateway_contract_registry import (
    GATEWAY_METHOD_CONTRACTS,
)
from opensquilla.contracts.generated.v4.sessions_create import (
    SessionsCreateRequestFrame,
    SessionsCreateResponseFrame,
    SessionsCreateResult,
)
from opensquilla.contracts.generated.v4.sessions_create_metadata import (
    SESSIONS_CREATE_METHOD,
    SESSIONS_CREATE_SCOPE,
)
from opensquilla.contracts.generated.v4.sessions_delete import (
    SessionsDeleteRequestFrame,
    SessionsDeleteResponseFrame,
    SessionsDeleteResult,
)
from opensquilla.contracts.generated.v4.sessions_delete_metadata import (
    SESSIONS_DELETE_METHOD,
    SESSIONS_DELETE_SCOPE,
)
from opensquilla.contracts.generated.v4.sessions_fork import (
    SessionsForkRequestFrame,
    SessionsForkResponseFrame,
    SessionsForkResult,
)
from opensquilla.contracts.generated.v4.sessions_fork_metadata import (
    SESSIONS_FORK_METHOD,
    SESSIONS_FORK_SCOPE,
)
from opensquilla.contracts.generated.v4.sessions_fork_through_turn import (
    SessionsForkThroughTurnRequestFrame,
    SessionsForkThroughTurnResponseFrame,
    SessionsForkThroughTurnResult,
)
from opensquilla.contracts.generated.v4.sessions_fork_through_turn_metadata import (
    SESSIONS_FORK_THROUGH_TURN_METHOD,
    SESSIONS_FORK_THROUGH_TURN_SCOPE,
)
from opensquilla.contracts.generated.v4.sessions_rename import (
    SessionsRenameRequestFrame,
    SessionsRenameResponseFrame,
    SessionsRenameResult,
)
from opensquilla.contracts.generated.v4.sessions_rename_metadata import (
    SESSIONS_RENAME_METHOD,
    SESSIONS_RENAME_SCOPE,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "contracts" / "gateway" / "v4" / "sessions"


def _wire_cases(directory: str, filename: str) -> list[tuple[str, dict[str, Any]]]:
    document = json.loads(
        (CONTRACT_ROOT / "fixtures" / directory / filename).read_text(encoding="utf-8")
    )
    return [
        (str(case["id"]), case["wire"])
        for case in document["cases"]
        if "wire" in case
    ]


@pytest.mark.parametrize(
    ("method", "scope", "directory", "kind", "guest_allowed"),
    [
        (SESSIONS_CREATE_METHOD, SESSIONS_CREATE_SCOPE, "sessions-create", "command", False),
        (SESSIONS_RENAME_METHOD, SESSIONS_RENAME_SCOPE, "sessions-rename", "command", True),
        (SESSIONS_DELETE_METHOD, SESSIONS_DELETE_SCOPE, "sessions-delete", "command", True),
        (SESSIONS_FORK_METHOD, SESSIONS_FORK_SCOPE, "sessions-fork", "command", False),
        (
            SESSIONS_FORK_THROUGH_TURN_METHOD,
            SESSIONS_FORK_THROUGH_TURN_SCOPE,
            "sessions-fork-through-turn",
            "command",
            False,
        ),
    ],
)
def test_lifecycle_metadata_matches_gateway_semantics(
    method: str,
    scope: str,
    directory: str,
    kind: str,
    guest_allowed: bool,
) -> None:
    schema_name = directory.removeprefix("sessions-")
    document = json.loads(
        (CONTRACT_ROOT / f"sessions-{schema_name}.schema.json").read_text(encoding="utf-8")
    )
    metadata = document["x-opensquilla-method"]
    descriptor = GATEWAY_METHOD_CONTRACTS[method]

    assert metadata["name"] == method
    assert metadata["kind"] == kind
    assert metadata["scope"] == scope == "operator.write"
    assert metadata["guestAllowed"] is guest_allowed
    assert descriptor.name == method
    assert descriptor.scope == scope
    assert descriptor.guest_allowed is guest_allowed


@pytest.mark.parametrize(
    ("model", "directory"),
    [
        (SessionsCreateRequestFrame, "sessions-create"),
        (SessionsRenameRequestFrame, "sessions-rename"),
        (SessionsDeleteRequestFrame, "sessions-delete"),
        (SessionsForkRequestFrame, "sessions-fork"),
        (SessionsForkThroughTurnRequestFrame, "sessions-fork-through-turn"),
    ],
)
def test_request_fixtures_round_trip_exact_json_tree(model: type[Any], directory: str) -> None:
    for case_id, wire in _wire_cases(directory, "requests.json"):
        parsed = model.model_validate(wire)
        assert parsed.model_dump(mode="json", exclude_unset=True) == wire, case_id


@pytest.mark.parametrize(
    ("model", "directory"),
    [
        (SessionsCreateResponseFrame, "sessions-create"),
        (SessionsRenameResponseFrame, "sessions-rename"),
        (SessionsDeleteResponseFrame, "sessions-delete"),
        (SessionsForkResponseFrame, "sessions-fork"),
        (SessionsForkThroughTurnResponseFrame, "sessions-fork-through-turn"),
    ],
)
def test_response_and_error_fixtures_round_trip_exact_json_tree(
    model: type[Any],
    directory: str,
) -> None:
    for filename in ("responses.json", "errors.json"):
        for case_id, wire in _wire_cases(directory, filename):
            parsed = model.model_validate(wire)
            assert parsed.model_dump(mode="json", exclude_unset=True) == wire, case_id


def test_create_result_preserves_additive_fields_and_requires_identity() -> None:
    payload = {
        "key": "agent:main:webchat:one",
        "sessionId": "one",
        "future": {"accepted": True},
    }
    assert SessionsCreateResult.model_validate(payload).model_dump(
        mode="json", exclude_unset=True
    ) == payload
    with pytest.raises(ValidationError):
        SessionsCreateResult.model_validate({"key": "only-key"})


def test_rename_and_delete_results_keep_partial_contract_shape() -> None:
    assert SessionsRenameResult.model_validate(
        {"key": "k", "updated": ["displayName"], "future": 1}
    ).model_dump(mode="json", exclude_unset=True) == {
        "key": "k",
        "updated": ["displayName"],
        "future": 1,
    }
    assert SessionsDeleteResult.model_validate(
        {"deleted": ["k"], "errors": ["missing: 'Session not found: missing'"]}
    ).model_dump(mode="json", exclude_unset=True) == {
        "deleted": ["k"],
        "errors": ["missing: 'Session not found: missing'"],
    }
    with pytest.raises(ValidationError):
        SessionsDeleteResult.model_validate({"deleted": []})


def test_fork_result_requires_identity_and_complete_through_turn_acknowledgement() -> None:
    legacy_payload = {
        "key": "agent:main:webchat:child",
        "parentKey": "agent:main:webchat:parent",
        "future": {"accepted": True},
    }
    assert SessionsForkResult.model_validate(legacy_payload).model_dump(
        mode="json", exclude_unset=True
    ) == legacy_payload

    through_payload = {
        "key": "agent:main:webchat:child",
        "parentKey": "agent:main:webchat:parent",
        "forkMode": "through_turn",
        "throughTurnId": "turn-terminal",
    }
    assert SessionsForkResult.model_validate(through_payload).model_dump(
        mode="json", exclude_unset=True
    ) == through_payload
    assert SessionsForkThroughTurnResult.model_validate(through_payload).model_dump(
        mode="json", exclude_unset=True
    ) == through_payload

    invalid_payloads = (
        {"key": "agent:main:webchat:child"},
        {
            "key": "agent:main:webchat:child",
            "parentKey": "agent:main:webchat:parent",
            "forkMode": "through_turn",
        },
        {
            "key": "agent:main:webchat:child",
            "parentKey": "agent:main:webchat:parent",
            "throughTurnId": "turn-terminal",
        },
        {
            "key": "agent:main:webchat:child",
            "parentKey": "agent:main:webchat:parent",
            "forkMode": "full",
            "throughTurnId": "turn-terminal",
        },
    )
    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            SessionsForkResult.model_validate(payload)

    with pytest.raises(ValidationError):
        SessionsForkThroughTurnResult.model_validate(
            {
                "key": "agent:main:webchat:child",
                "parentKey": "agent:main:webchat:parent",
                "forkMode": "through_turn",
            }
        )
