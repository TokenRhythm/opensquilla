from __future__ import annotations

import asyncio
import copy
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from opensquilla.sandbox import run_context, run_context_service, user_grants
from opensquilla.sandbox.run_context import DomainGrant, RunContext
from opensquilla.sandbox.run_mode import RunMode


class SessionManager:
    def __init__(self):
        self.initial_context = RunContext(
            run_mode=RunMode.FULL,
            domains=(DomainGrant("example.com", "chat"),),
        )
        self.node = SimpleNamespace(
            workspace_id=None,
            origin={
                run_context.RUN_CONTEXT_ORIGIN_KEY: self.initial_context.to_origin_payload(),
            },
        )
        self.loop_thread = threading.get_ident()

    async def get_session(self, _session_key):
        assert threading.get_ident() == self.loop_thread
        return self.node

    async def update(self, _session_key, **fields):
        assert threading.get_ident() == self.loop_thread
        self.node.origin = copy.deepcopy(fields["origin"])
        return self.node


@pytest.mark.parametrize("second_mutation", ["add_chat", "remove_chat", "set_mode", "persist"])
async def test_failed_grant_write_does_not_leak_into_later_session_mutation(
    monkeypatch, second_mutation,
):
    manager = SessionManager()
    config = SimpleNamespace(sandbox=SimpleNamespace(run_mode="full"))
    write_started = threading.Event()
    release_write = threading.Event()
    second_started = asyncio.Event()
    monkeypatch.setattr(run_context, "load_user_grants_payload", lambda: {})

    def failing_write(_bundle_id):
        write_started.set()
        assert release_write.wait(3), "test did not release the grant write"
        raise RuntimeError("synthetic grant write failure")

    monkeypatch.setattr(user_grants, "remove_bundle_grant", failing_write)

    async def mutate_after_write_started():
        # The event wakes the test only after this coroutine reaches its first
        # suspension in the public API; no wall-clock scheduling guess is needed.
        second_started.set()
        if second_mutation == "add_chat":
            return await run_context_service.add_domain_grant(
                manager, "synthetic-session", domain="other.example.com", scope="chat",
                config=config, workspace=None,
            )
        if second_mutation == "remove_chat":
            return await run_context_service.remove_domain_grant(
                manager, "synthetic-session", domain="example.com", scope="chat",
                config=config, workspace=None,
            )
        if second_mutation == "set_mode":
            return await run_context.set_run_mode(
                manager, "synthetic-session", RunMode.SAFE, config=config,
            )
        return await run_context.persist_run_context(
            manager, "synthetic-session",
            replace(manager.initial_context, run_mode=RunMode.SAFE),
        )

    first = asyncio.create_task(run_context_service.disable_bundle_grant(
        manager, "synthetic-session", bundle_id="python-package-install",
        config=config, workspace=None,
    ))
    second = None
    try:
        assert await asyncio.to_thread(write_started.wait, 3)
        second = asyncio.create_task(mutate_after_write_started())
        await asyncio.wait_for(second_started.wait(), 3)
        release_write.set()
        with pytest.raises(RuntimeError, match="synthetic grant write failure"):
            await asyncio.wait_for(first, 3)
        result = await asyncio.wait_for(second, 3)
        final = await run_context.get_run_context(
            manager, "synthetic-session", config=config, workspace=None,
            include_user_grants=False,
        )
        # A failed disable must neither be observed from a transient snapshot
        # nor be copied back into durable session state by a successful request.
        assert result.bundles == final.bundles == ()
        expected_domains = {
            "add_chat": {"example.com", "other.example.com"},
            "remove_chat": set(),
            "set_mode": {"example.com"},
            "persist": {"example.com"},
        }[second_mutation]
        assert {grant.domain for grant in final.domains} == expected_domains
        expected_mode = RunMode.SAFE if second_mutation in {"set_mode", "persist"} else RunMode.FULL
        assert final.run_mode is expected_mode
    finally:
        release_write.set()
        tasks = [first] if second is None else [first, second]
        await asyncio.gather(*tasks, return_exceptions=True)
