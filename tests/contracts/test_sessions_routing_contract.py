"""Contract fixtures for the per-session routing query, CAS command and event."""

from __future__ import annotations

import json
from pathlib import Path

from opensquilla.contracts.generated.v4.sessions_routing_changed import Payload
from opensquilla.contracts.generated.v4.sessions_routing_get import RequestFrame as GetRequest
from opensquilla.contracts.generated.v4.sessions_routing_get import Result as GetResult
from opensquilla.contracts.generated.v4.sessions_routing_set import RequestFrame as SetRequest

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts/gateway/v4/sessions"


def test_get_and_set_frames_preserve_aliases_and_cas_fields() -> None:
    get = GetRequest.model_validate({
        "type": "req",
        "id": "1",
        "method": "sessions.routing.get",
        "params": {"session_key": "agent:main:webchat:a"},
    })
    assert get.model_dump(mode="json", exclude_unset=True)["params"] == {
        "session_key": "agent:main:webchat:a",
    }
    set_frame = SetRequest.model_validate({
        "type": "req",
        "id": "2",
        "method": "sessions.routing.set",
        "params": {
            "key": "agent:main:webchat:a",
            "mode": "router",
            "expected_revision": 3,
        },
    })
    assert set_frame.params.key == "agent:main:webchat:a"
    assert set_frame.params.expected_revision == 3


def test_result_and_changed_event_keep_wire_projection() -> None:
    result = GetResult.model_validate({
        "mode": "ensemble",
        "revision": 4,
        "source": "session",
        "initialized": True,
        "appliesTo": "next_accepted_turn",
        "future": {"flag": True},
    })
    assert result.model_dump(mode="json", exclude_unset=True)["revision"] == 4
    event = Payload.model_validate({
        "key": "agent:main:webchat:a",
        "routing": {"mode": "direct", "revision": 5},
        "mode": "direct",
        "revision": 5,
    })
    assert event.model_dump(mode="json", exclude_unset=True)["key"] == "agent:main:webchat:a"


def test_set_schema_requires_cas_revision() -> None:
    schema = json.loads(
        (CONTRACTS / "sessions-routing-set.schema.json").read_text(encoding="utf-8")
    )
    params = schema["$defs"]["Params"]
    serialized = json.dumps(params)
    assert '"expectedRevision"' in serialized
    assert '"expected_revision"' in serialized
    assert any('expectedRevision' in json.dumps(item) for item in params["allOf"])


def test_schema_metadata_is_language_neutral() -> None:
    for name, method, kind, scope in (
        ("sessions-routing-get.schema.json", "sessions.routing.get", "query", "operator.read"),
        ("sessions-routing-set.schema.json", "sessions.routing.set", "command", "operator.write"),
    ):
        document = json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
        metadata = document["x-opensquilla-method"]
        assert (metadata["name"], metadata["kind"], metadata["scope"]) == (method, kind, scope)
