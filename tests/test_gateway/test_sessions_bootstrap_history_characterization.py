"""Characterize the shared history projection inside ``sessions.bootstrap``."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import opensquilla.gateway.model_routing as model_routing
import opensquilla.gateway.rpc_sessions as rpc_sessions
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "gateway" / "chat_history" / "bootstrap.json"


def _fixture() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(FIXTURE.read_text(encoding="utf-8")))


@dataclass
class _Session:
    session_key: str
    session_id: str
    agent_id: str = "main"
    status: str = "running"
    created_at: int = 1
    updated_at: int = 2
    model: str | None = None
    display_name: str | None = None
    queue_mode: str = "followup"
    epoch: int = 0


class _Storage:
    def __init__(self, session: _Session) -> None:
        self.session = session

    async def get_session(self, key: str) -> _Session | None:
        return self.session if key == self.session.session_key else None


class _Manager:
    def __init__(self, session: _Session) -> None:
        self.storage = _Storage(session)


class _Streams:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def current_seq(self, session_key: str) -> int:
        self.events.append(f"stream_cursor:{session_key}")
        return 42


def _context(manager: _Manager) -> RpcContext:
    return RpcContext(
        conn_id="bootstrap-characterization",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.read"}),
            is_owner=True,
            authenticated=True,
        ),
        session_manager=manager,
        config=GatewayConfig(),
    )


@pytest.mark.asyncio
async def test_bootstrap_forwards_history_aliases_and_captures_cursor_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _fixture()["cases"][0]
    request = cast(dict[str, Any], case["request"])
    expected_history = cast(dict[str, Any], case["expected_history"])
    expected_order = cast(list[str], case["expected_order"])
    key = str(request["key"])
    events: list[str] = []
    received_history: list[dict[str, Any]] = []

    session = _Session(session_key=key, session_id="bootstrap-characterization")
    manager = _Manager(session)

    async def fake_history(params: dict[str, Any] | None, _ctx: RpcContext) -> dict[str, Any]:
        events.append("history")
        received_history.append(dict(params or {}))
        return {
            "messages": [],
            "has_more": False,
            "history_scope": "complete",
            "loaded_count": 0,
            "page_size": int((params or {}).get("limit", 50)),
            "canonical_available": False,
            "canonical_complete": True,
            "compaction_summaries": [],
            "turn_outcomes": [],
        }

    monkeypatch.setattr(rpc_sessions, "read_chat_history_v4", fake_history)
    monkeypatch.setattr(
        rpc_sessions,
        "get_session_streams",
        lambda: _Streams(events),
    )

    async def fake_resolve(_storage: Any, candidate: str) -> _Session:
        assert candidate == key
        return session

    monkeypatch.setattr(rpc_sessions, "_resolve_session_record_for_bootstrap", fake_resolve)
    monkeypatch.setattr(rpc_sessions, "_list_task_rows", _empty_task_rows)
    monkeypatch.setattr(
        rpc_sessions,
        "_bootstrap_epoch",
        lambda *_args: _zero_epoch(),
    )
    monkeypatch.setattr(
        rpc_sessions,
        "_bootstrap_agent_identity",
        lambda *_args: _agent_identity(),
    )
    monkeypatch.setattr(rpc_sessions, "_session_turn_model", lambda *_args: None)
    monkeypatch.setattr(rpc_sessions, "_is_remote_web_guest", lambda *_args: True)
    monkeypatch.setattr(
        rpc_sessions,
        "_resolve_session_routing_snapshot",
        lambda *_args: _routing_snapshot(),
    )
    monkeypatch.setattr(
        model_routing,
        "capture_model_routing_config",
        lambda *_args, **_kwargs: SimpleNamespace(overlay_live_config=None),
    )
    monkeypatch.setattr(
        model_routing,
        "model_routing_snapshot",
        lambda *_args, **_kwargs: {"mode": "direct"},
    )

    response = await get_dispatcher().dispatch(
        "bootstrap-characterization",
        "sessions.bootstrap",
        request,
        _context(manager),
    )

    assert response.ok is True
    assert received_history == [expected_history]
    assert [entry.split(":", 1)[0] for entry in events] == expected_order
    payload = response.payload
    assert isinstance(payload, dict)
    history = payload.get("history")
    assert isinstance(history, dict)
    assert history["page_size"] == request["limit"]
    assert payload["stream_cursor"] == 42


async def _empty_task_rows(*_args: Any, **_kwargs: Any) -> list[Any]:
    return []


async def _zero_epoch() -> int:
    return 0


async def _agent_identity() -> dict[str, str | None]:
    return {"agent_id": "main", "name": "main", "emoji": None, "theme": None}


async def _routing_snapshot() -> dict[str, Any]:
    return {"mode": "direct", "revision": 0, "source": "global"}
