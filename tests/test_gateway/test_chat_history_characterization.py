"""Executable v4 behavior baseline for ``chat.history``.

This file intentionally describes the current wire behavior without introducing
an application module or a production Contract.  S4c1--S4c4 may move the
implementation behind adapters, but these cases must continue to pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.session.models import TranscriptEntry
from opensquilla.session.storage import StorageBusyError

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "gateway" / "chat_history"


def _document(name: str) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8")),
    )


def _case(document: str, case_id: str) -> dict[str, Any]:
    cases = _document(document)["cases"]
    return cast(dict[str, Any], next(item for item in cases if item["id"] == case_id))


def _request(case_id: str) -> dict[str, Any]:
    return cast(dict[str, Any], _case("requests.json", case_id)["wire"])


def _entry(index: int) -> TranscriptEntry:
    return TranscriptEntry(
        id=index,
        session_id="characterization",
        session_key="agent:main:webchat:characterization",
        role="assistant" if index % 2 == 0 else "user",
        content=f"message {index}",
        created_at=index,
        message_id=f"m{index}",
    )


class _HistoryManager:
    """Small manager double that exposes only the history read surfaces."""

    def __init__(
        self,
        entries: list[TranscriptEntry],
        *,
        canonical: bool = True,
        missing_keys: set[str] | None = None,
        busy_keys: set[str] | None = None,
    ) -> None:
        self.entries = list(entries)
        self.canonical = canonical
        self.missing_keys = missing_keys or set()
        self.busy_keys = busy_keys or set()
        self.canonical_calls: list[dict[str, Any]] = []
        self.active_calls: list[str] = []

    def _check_key(self, session_key: str) -> None:
        if session_key in self.busy_keys:
            raise StorageBusyError(
                "chat.history",
                waited_ms=17,
                retry_after_ms=100,
                stage="characterization",
            )
        if session_key in self.missing_keys:
            raise KeyError(f"Session not found: {session_key}")

    @staticmethod
    def _cursor(entry: TranscriptEntry) -> tuple[int, int]:
        return int(entry.created_at or 0), int(entry.id or 0)

    async def get_canonical_transcript_page(
        self,
        session_key: str,
        *,
        limit: int,
        before: tuple[int, int] | None = None,
        after: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        self.canonical_calls.append(
            {"session_key": session_key, "limit": limit, "before": before, "after": after}
        )
        self._check_key(session_key)
        if not self.canonical:
            raise RuntimeError("canonical reader unavailable")

        entries = self.entries
        if before is not None:
            index = next(
                (position for position, item in enumerate(entries) if self._cursor(item) == before),
                None,
            )
            if index is None:
                page = entries[-limit:]
                has_more = len(entries) > limit
            else:
                start = max(0, index - limit)
                page = entries[start:index]
                has_more = start > 0
        elif after is not None:
            index = next(
                (position for position, item in enumerate(entries) if self._cursor(item) == after),
                None,
            )
            if index is None:
                page = entries[-limit:]
                has_more = len(entries) > limit
            else:
                end = min(len(entries), index + 1 + limit)
                page = entries[index + 1 : end]
                has_more = end < len(entries)
        elif len(entries) <= limit:
            page = entries
            has_more = False
        else:
            page = entries[-limit:]
            has_more = True
        return {"entries": page, "has_more": has_more, "canonical_complete": True}

    async def get_transcript(self, session_key: str) -> list[TranscriptEntry]:
        self.active_calls.append(session_key)
        self._check_key(session_key)
        return list(self.entries)

    async def get_summaries(self, _session_key: str) -> list[Any]:
        return []


def _context(manager: _HistoryManager) -> RpcContext:
    return RpcContext(
        conn_id="chat-history-characterization",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.read"}),
            is_owner=True,
            authenticated=True,
        ),
        session_manager=manager,
        config=GatewayConfig(),
    )


def _expected_for(outcome: str) -> dict[str, Any]:
    if outcome.startswith("response."):
        return cast(dict[str, Any], _case("responses.json", outcome)["assert"])
    return cast(dict[str, Any], _case("errors.json", outcome)["wire"])


def _assert_payload_subset(payload: Any, expected: dict[str, Any]) -> None:
    assert isinstance(payload, dict)
    for key, value in expected.items():
        assert payload.get(key) == value, f"wire field {key!r} changed"


def _assert_error(response: Any, expected: dict[str, Any]) -> None:
    assert response.ok is False
    assert response.error is not None
    assert response.error.code == expected["code"]
    if "message" in expected:
        assert response.error.message == expected["message"]
    if "message_contains" in expected:
        assert expected["message_contains"] in response.error.message
    for key in ("retryable", "retry_after_ms"):
        if key in expected:
            assert getattr(response.error, key) == expected[key]
    if "details" in expected:
        details = response.error.details
        for key, value in expected["details"].items():
            assert isinstance(details, dict)
            assert details.get(key) == value


def test_characterization_fixture_is_self_consistent() -> None:
    manifest = _document("manifest.json")
    assert manifest["kind"] == "chat-history-characterization"
    assert manifest["authority"] == "test-only-pre-contract"
    assert manifest["wire_protocol"] == "opensquilla-websocket-json"
    assert manifest["wire_version"] == 4
    for document in manifest["documents"].values():
        assert (FIXTURE_ROOT / document).is_file()

    request_ids = {item["id"] for item in _document("requests.json")["cases"]}
    outcome_ids = {
        item["id"]
        for document in ("responses.json", "errors.json")
        for item in _document(document)["cases"]
    }
    for request in _document("requests.json")["cases"]:
        assert request["expected_outcome"] in outcome_ids, request["id"]

    behavior = _document("behavior.json")
    referenced_requests: set[str] = set()
    referenced_outcomes: set[str] = set()
    for item in behavior["cases"]:
        for request_id in item.get("request_cases", [item.get("request_case")]):
            if request_id is not None:
                referenced_requests.add(request_id)
        for outcome_id in item.get("asserts", [item.get("assert")]):
            if outcome_id is not None:
                referenced_outcomes.add(outcome_id)
    assert referenced_requests <= request_ids
    assert referenced_outcomes <= outcome_ids


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_case",
    [item["id"] for item in _document("requests.json")["cases"]],
)
async def test_chat_history_request_cases_preserve_v4_wire_behavior(request_case: str) -> None:
    request = _request(request_case)
    outcome = str(_case("requests.json", request_case)["expected_outcome"])
    raw_params = request.get("params")
    session_key = (
        str(raw_params.get("sessionKey", ""))
        if isinstance(raw_params, dict)
        else ""
    )
    entries = [_entry(index) for index in range(1, 5)]

    if request_case == "request.default-null":
        manager = _HistoryManager(entries, missing_keys={"agent:main:webchat:default"})
    elif request_case in {
        "request.missing-non-webchat",
        "request.history-cursor-invalidated",
    }:
        manager = _HistoryManager(entries, missing_keys={session_key})
    elif request_case == "request.storage-busy":
        manager = _HistoryManager(entries, busy_keys={session_key})
    elif request_case == "request.aliases":
        manager = _HistoryManager(entries, canonical=False)
    else:
        manager = _HistoryManager(entries)

    response = await get_dispatcher().dispatch(
        str(request["id"]),
        str(request["method"]),
        request.get("params"),
        _context(manager),
    )

    expected = _expected_for(outcome)
    if outcome.startswith("response."):
        assert response.ok is True
        _assert_payload_subset(response.payload, expected)
    else:
        _assert_error(response, expected)

    if request_case == "request.aliases":
        assert manager.active_calls == ["agent:main:webchat:characterization"]
        assert manager.canonical_calls == []
    if request_case == "request.before-after":
        assert manager.canonical_calls[0]["before"] == (3, 3)
        assert manager.canonical_calls[0]["after"] is None
    if request_case == "request.default-null":
        assert manager.active_calls == ["agent:main:webchat:default"]


@pytest.mark.asyncio
async def test_chat_history_dispatcher_preserves_request_identity_and_unknown_fields() -> None:
    manager = _HistoryManager([_entry(1)])
    request = {
        "type": "req",
        "id": "identity-preserved",
        "method": "chat.history",
        "params": {
            "sessionKey": "agent:main:webchat:characterization",
            "future_option": {"enabled": True},
        },
    }

    response = await get_dispatcher().dispatch(
        str(request["id"]),
        str(request["method"]),
        request["params"],
        _context(manager),
    )

    assert response.type == "res"
    assert response.id == request["id"]
    assert response.ok is True
    payload = response.payload
    assert isinstance(payload, dict)
    assert payload["loaded_count"] == 1
