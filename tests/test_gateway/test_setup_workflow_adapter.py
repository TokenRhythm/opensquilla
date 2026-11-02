"""Setup reads must leave the Gateway loop available for shutdown and RPCs."""

from __future__ import annotations

import asyncio
import threading

from opensquilla import desktop_account
from opensquilla.gateway.adapters import setup_workflow


async def test_setup_status_waits_for_account_without_blocking_gateway(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    snapshot = {"source": "manual", "ready": False}
    reads = []

    def read_account():
        reads.append(True)
        started.set()
        # A watchdog releases the baseline's blocking implementation as well.
        release.wait(2)
        return snapshot

    def project_status(config, *, is_owner):
        assert desktop_account.account_state() == snapshot
        assert is_owner
        return {"llmConfigured": False}

    monkeypatch.setattr(desktop_account, "desktop_account_available", lambda: True)
    monkeypatch.setattr(desktop_account, "account_state_for_turn", read_account)
    monkeypatch.setattr(setup_workflow, "_setup_status", project_status)
    scope = desktop_account.pin_account_state(None)
    port = setup_workflow.GatewaySetupWorkflowPort(object(), is_owner=True)
    task = asyncio.create_task(port.load_setup_status())
    try:
        await asyncio.sleep(0)
        assert await asyncio.to_thread(started.wait, 1)
        assert not task.done(), "account I/O must not block unrelated Gateway work"
        assert desktop_account._turn_state.get() is None
        release.set()
        assert await task == {"llmConfigured": False}
        assert reads == [True]
        assert desktop_account._turn_state.get() is None
    finally:
        release.set()
        await task
        desktop_account.reset_account_state(scope)
