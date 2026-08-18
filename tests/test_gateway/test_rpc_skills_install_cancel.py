from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from opensquilla.gateway import rpc_skills, websocket
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.scopes import ADMIN_SCOPE, METHOD_SCOPES
from opensquilla.skills.loader import SkillLoader


def _context(tmp_path, installer, *, conn_id: str = "web", state=None) -> RpcContext:
    loader = SkillLoader(
        managed_dir=tmp_path / "managed",
        snapshot_path=tmp_path / "snapshot.json",
    )
    loader.load_all()
    return RpcContext(
        conn_id=conn_id,
        skill_loader=loader,
        skill_management_service=installer,
        skill_management_state={} if state is None else state,
    )


@pytest.mark.asyncio
async def test_active_install_cancellation_waits_for_cleanup(tmp_path) -> None:
    entered = asyncio.Event()
    cleaned_up = asyncio.Event()

    class _Installer:
        async def install(self, *_args, **_kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned_up.set()

    ctx = _context(tmp_path, _Installer())
    operation_id = str(uuid4())
    install_task = asyncio.create_task(
        rpc_skills._handle_skills_install(
            {"identifier": "demo", "operationId": operation_id},
            ctx,
        )
    )
    await entered.wait()

    cancel_payload = await rpc_skills._handle_skills_install_cancel(
        {"operationId": operation_id},
        ctx,
    )

    assert cleaned_up.is_set()
    assert cancel_payload == {
        "success": False,
        "cancelled": True,
        "message": "Skill installation cancelled",
        "pending": False,
    }
    assert await install_task == {
        "success": False,
        "cancelled": True,
        "message": "Skill installation cancelled",
    }
    assert ctx.skill_management_state[rpc_skills._ACTIVE_SKILL_INSTALLS_STATE_KEY] == {}


@pytest.mark.asyncio
async def test_cancel_arriving_before_install_prevents_mutation(tmp_path) -> None:
    calls = 0

    class _Installer:
        async def install(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return SimpleNamespace(success=True, name="demo", message="installed")

    ctx = _context(tmp_path, _Installer())
    operation_id = str(uuid4())

    cancel_payload = await rpc_skills._handle_skills_install_cancel(
        {"operationId": operation_id},
        ctx,
    )
    install_payload = await rpc_skills._handle_skills_install(
        {"identifier": "demo", "operationId": operation_id},
        ctx,
    )

    assert cancel_payload["pending"] is True
    assert install_payload["cancelled"] is True
    assert calls == 0


@pytest.mark.asyncio
async def test_install_can_only_be_cancelled_by_owning_connection(tmp_path) -> None:
    entered = asyncio.Event()

    class _Installer:
        async def install(self, *_args, **_kwargs):
            entered.set()
            await asyncio.Event().wait()

    state: dict = {}
    owner = _context(tmp_path, _Installer(), conn_id="owner", state=state)
    other = _context(tmp_path, owner.skill_management_service, conn_id="other", state=state)
    operation_id = str(uuid4())
    install_task = asyncio.create_task(
        rpc_skills._handle_skills_install(
            {"identifier": "demo", "operationId": operation_id},
            owner,
        )
    )
    await entered.wait()

    other_payload = await rpc_skills._handle_skills_install_cancel(
        {"operationId": operation_id},
        other,
    )
    assert other_payload["pending"] is True
    assert not install_task.done()

    await rpc_skills._handle_skills_install_cancel(
        {"operationId": operation_id},
        owner,
    )
    assert (await install_task)["cancelled"] is True


@pytest.mark.asyncio
async def test_install_cancellation_rejects_invalid_operation_id(tmp_path) -> None:
    ctx = _context(tmp_path, SimpleNamespace())

    with pytest.raises(ValueError, match="must be a UUID"):
        await rpc_skills._handle_skills_install_cancel(
            {"operationId": "not-a-uuid"},
            ctx,
        )


def test_install_cancellation_protocol_is_advertised_and_admin_only() -> None:
    assert "skills.install" in websocket._DETACHED_RPC_METHODS
    assert METHOD_SCOPES["skills.install.cancel"] == ADMIN_SCOPE
