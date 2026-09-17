from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.skills.hub.management import InstallResult, SkillManagementService
from opensquilla.skills.hub.router import SourceRouter
from opensquilla.skills.loader import SkillLoader


@pytest.mark.asyncio
async def test_status_and_cancel_follow_identity_across_connections(tmp_path) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Service(SkillManagementService):
        async def install(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return InstallResult(success=True, installed=True, name="demo")

    service = Service(
        router=SourceRouter([]),
        managed_dir=tmp_path / "managed",
        lockfile_path=tmp_path / "lock.json",
        journal_path=tmp_path / "state/journal.json",
    )
    loader = SkillLoader(managed_dir=tmp_path / "managed", snapshot_path=tmp_path / "snapshot.json")
    loader.load_all()
    principal = Principal(
        role="operator",
        scopes=frozenset({"operator.admin"}),
        is_owner=True,
        authenticated=True,
        token_public_id="synthetic-owner",
    )
    first = RpcContext(
        conn_id="first", principal=principal, skill_loader=loader, skill_management_service=service
    )
    second = replace(first, conn_id="second")
    other = replace(second, principal=replace(principal, token_public_id="different-owner"))
    reader = replace(second, principal=replace(principal, scopes=frozenset({"operator.read"})))
    operation_id = str(uuid4())
    dispatcher = get_dispatcher()
    wait = asyncio.create_task(
        dispatcher.dispatch(
            "install",
            "skills.install",
            {
                "identifier": "demo",
                "operationId": operation_id,
            },
            first,
        )
    )
    await entered.wait()
    wait.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wait
    result = await dispatcher.dispatch(
        "status", "skills.install.status", {"operationId": operation_id}, second
    )
    assert result.ok and result.payload["state"] == "running"
    denied = await dispatcher.dispatch(
        "status", "skills.install.status", {"operationId": operation_id}, reader
    )
    assert not denied.ok and denied.error.code == "UNAUTHORIZED"
    unknown = await dispatcher.dispatch(
        "status", "skills.install.status", {"operationId": operation_id}, other
    )
    assert unknown.ok and unknown.payload["state"] == "unknown"
    release.set()
    await asyncio.gather(*service.install_operations.tasks.values())
    result = await dispatcher.dispatch(
        "status", "skills.install.status", {"operationId": operation_id}, second
    )
    assert result.ok and result.payload["state"] == "succeeded"
    cancelled = await dispatcher.dispatch(
        "cancel", "skills.install.cancel", {"operationId": operation_id}, second
    )
    assert cancelled.ok and cancelled.payload["success"] and not cancelled.payload["cancelled"]
