"""Async authority primitives keep SQLite off the Gateway event loop."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from opensquilla.gateway.token_store import AuthorizationDecision, TokenStore


def test_authorization_decision_distinguishes_deny_and_unavailable(tmp_path):
    store = TokenStore(tmp_path / "sessions.db")

    assert store.get_active_authorization_decision("missing").status == "deny"

    class BrokenStore(TokenStore):
        def _connect(self):  # type: ignore[no-untyped-def]
            raise OSError("synthetic database outage")

    decision = BrokenStore.__new__(BrokenStore).get_active_authorization_decision("x")
    assert decision == AuthorizationDecision.unavailable()


def test_authorization_decision_carries_revision_and_secret_free_fingerprint(tmp_path):
    store = TokenStore(tmp_path / "sessions.db")
    issued = store.create(
        name="revisioned",
        roles={"operator"},
        scopes={"operator.read"},
        capabilities={"task.submit"},
    )

    decision = store.get_active_authorization_decision(issued.record.public_id)

    assert decision.status == "allow"
    assert decision.authorization_revision == 1
    assert isinstance(decision.permission_fingerprint, str)
    assert len(decision.permission_fingerprint) == 64
    assert issued.token not in decision.permission_fingerprint


@pytest.mark.asyncio
async def test_async_authority_read_runs_on_worker_thread(tmp_path, monkeypatch):
    store = TokenStore(tmp_path / "sessions.db")
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def slow_read(public_id: str):
        seen.append(threading.get_ident())
        time.sleep(0.03)
        return AuthorizationDecision.deny()

    monkeypatch.setattr(store, "get_active_authorization_decision", slow_read)
    heartbeat = asyncio.Event()

    async def mark_heartbeat():
        await asyncio.sleep(0)
        heartbeat.set()

    result, _ = await asyncio.gather(
        store.get_active_authorization_decision_async("missing"),
        mark_heartbeat(),
    )
    assert result.status == "deny"
    assert heartbeat.is_set()
    assert seen and seen[0] != loop_thread
