"""Owned task directories follow real session acceptance, including cancellation."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from opensquilla.execution_workspaces import prepare_managed_workspace
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.execution_workspaces import build_execution_workspace_factory
from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.gateway.task_runtime import TaskRuntimeShuttingDownError
from opensquilla.session.manager import SessionIntent, SessionManager
from opensquilla.session.storage import SessionStorage, StorageBusyError
from tests.test_gateway.test_channel_turn_ingress import SESSION_KEY as CHANNEL_KEY
from tests.test_gateway.test_channel_turn_ingress import _accept, _open_stack
from tests.test_gateway.test_turn_ingress_rpc import _open_real_stack

NEW_KEY = "agent:main:webchat:workspace-preparation"


def _managed_config(tmp_path: Path) -> GatewayConfig:
    return GatewayConfig(
        sandbox={"run_mode": "full"},
        attachments={"media_root": str(tmp_path / "media")},
        memory={"flush_enabled": False},
        naming={"enabled": False},
    )


def _roots(tmp_path: Path) -> list[Path]:
    parent = tmp_path / "tasks"
    return sorted(parent.iterdir()) if parent.exists() else []


async def _send_new(stack: Any, request_id: str = "new-workspace") -> Any:
    return await get_dispatcher().dispatch(
        request_id,
        "sessions.send",
        {
            "key": NEW_KEY,
            "message": "a new local task",
            "intent": "new_chat",
            "clientRequestId": request_id,
        },
        stack.context,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True])
async def test_rpc_rejected_first_turn_removes_only_its_uncommitted_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, direct: bool,
) -> None:
    async with _open_real_stack(tmp_path / "rpc.db") as stack:
        config = _managed_config(tmp_path)
        stack.context.config = config
        stack.manager._execution_workspace_factory = build_execution_workspace_factory(
            config, profile_home=tmp_path,
        )
        if direct:
            stack.context.task_runtime = None

        async def reject(*args: Any, **kwargs: Any) -> Any:
            raise StorageBusyError("workspace-test", waited_ms=1, retry_after_ms=1)

        monkeypatch.setattr(stack.storage, "accept_turn", reject)
        response = await _send_new(stack)

        assert response.ok is False
        assert response.error.code == "STORAGE_BUSY"
        assert await stack.storage.get_session(NEW_KEY) is None
        assert stack.received_runs == []
        assert _roots(tmp_path) == []


@pytest.mark.asyncio
async def test_channel_rejected_first_turn_removes_uncommitted_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _open_stack(tmp_path / "channel.db") as stack:
        config = _managed_config(tmp_path)
        stack.manager._execution_workspace_factory = build_execution_workspace_factory(
            config, profile_home=tmp_path,
        )

        async def reject(*args: Any, **kwargs: Any) -> Any:
            raise TaskRuntimeShuttingDownError(session_key=CHANNEL_KEY)

        monkeypatch.setattr(stack.runtime, "reserve", reject)
        with pytest.raises(TaskRuntimeShuttingDownError):
            await _accept(stack, "unaccepted first task", config=config)

        assert await stack.storage.get_session(CHANNEL_KEY) is None
        assert stack.received_runs == []
        assert _roots(tmp_path) == []


@pytest.mark.asyncio
async def test_direct_create_failure_removes_uncommitted_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with SessionStorage(tmp_path / "direct.db") as storage:
        manager = SessionManager(storage, execution_workspace_factory=(
            build_execution_workspace_factory(_managed_config(tmp_path), profile_home=tmp_path)
        ))

        async def reject(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("write rejected")

        monkeypatch.setattr(storage, "upsert_session", reject)
        with pytest.raises(RuntimeError, match="write rejected"):
            await manager.create(NEW_KEY)

        assert await storage.get_session(NEW_KEY) is None
        assert _roots(tmp_path) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["rpc", "channel"])
async def test_commit_then_cancel_retains_workspace_and_replays_without_reallocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str,
) -> None:
    open_stack = _open_real_stack if surface == "rpc" else _open_stack
    async with open_stack(tmp_path / "committed.db") as stack:
        config = _managed_config(tmp_path)
        stack.manager._execution_workspace_factory = build_execution_workspace_factory(
            config, profile_home=tmp_path,
        )
        if surface == "rpc":
            stack.context.config = config
        key = NEW_KEY if surface == "rpc" else CHANNEL_KEY
        committed = asyncio.Event()
        release = asyncio.Event()
        original_accept = stack.storage.accept_turn

        async def pause_after_commit(*args: Any, **kwargs: Any) -> Any:
            result = await original_accept(*args, **kwargs)
            committed.set()
            await release.wait()
            return result

        async def send() -> Any:
            if surface == "rpc":
                return await _send_new(stack)
            return await _accept(stack, "a new local task", config=config)

        monkeypatch.setattr(stack.storage, "accept_turn", pause_after_commit)
        operation = asyncio.create_task(send())
        try:
            await asyncio.wait_for(committed.wait(), timeout=2)
            current = await stack.storage.get_session(key)
            root = Path(current.execution_workspace["root"])
            operation.cancel()
            await asyncio.sleep(0)
            operation.cancel()
            assert root.is_dir()
        finally:
            release.set()
        result = await asyncio.wait_for(operation, timeout=2)
        assert result.ok if surface == "rpc" else result[0] is not None
        await stack.wait_until_running()
        roots = _roots(tmp_path)
        replay = await send()
        assert replay.payload["replayed"] if surface == "rpc" else replay[3]
        assert root.is_dir()
        assert _roots(tmp_path) == roots
        assert len(stack.received_runs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["cancelled", "internal_commit_cancel", "post_commit_error", "unknown"],
)
async def test_direct_create_retains_committed_or_unknown_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    async with SessionStorage(tmp_path / "direct-outcome.db") as storage:
        manager = SessionManager(storage, execution_workspace_factory=(
            build_execution_workspace_factory(_managed_config(tmp_path), profile_home=tmp_path)
        ))
        original_upsert = storage.upsert_session
        original_get = storage.get_session
        committed = asyncio.Event()
        release = asyncio.Event()

        async def unavailable(*args: Any, **kwargs: Any) -> Any:
            raise OSError("outcome unavailable")

        async def persist(*args: Any, **kwargs: Any) -> Any:
            if outcome == "unknown":
                monkeypatch.setattr(storage, "get_session", unavailable)
                raise OSError("write outcome unknown")
            await original_upsert(*args, **kwargs)
            committed.set()
            if outcome == "post_commit_error":
                raise OSError("error after commit")
            if outcome == "internal_commit_cancel":
                raise asyncio.CancelledError()
            await release.wait()

        monkeypatch.setattr(storage, "upsert_session", persist)
        operation = asyncio.create_task(manager.create(NEW_KEY))
        if outcome in {"cancelled", "internal_commit_cancel"}:
            if outcome == "cancelled":
                await asyncio.wait_for(committed.wait(), timeout=2)
                operation.cancel()
                await asyncio.sleep(0)
                operation.cancel()
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await operation
        else:
            with pytest.raises(OSError):
                await operation
        roots = _roots(tmp_path)
        assert len(roots) == 1 and roots[0].is_dir()
        current = await original_get(NEW_KEY)
        assert (current is None) == (outcome == "unknown")
        if current is not None:
            assert current.execution_workspace["root"] == str(roots[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["route_failure", "route_cancel"])
async def test_rpc_preparation_exit_before_reservation_cleans_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    import opensquilla.gateway.rpc_sessions as rpc_sessions

    async with _open_real_stack(tmp_path / "preparation-exit.db") as stack:
        config = _managed_config(tmp_path)
        stack.context.config = config
        stack.manager._execution_workspace_factory = build_execution_workspace_factory(
            config, profile_home=tmp_path,
        )
        entered = asyncio.Event()

        async def prepare(*args: Any, **kwargs: Any) -> Any:
            entered.set()
            if boundary == "route_failure":
                raise ValueError("route preparation rejected")
            await asyncio.Event().wait()

        monkeypatch.setattr(rpc_sessions, "prepare_admission_route", prepare)
        operation = asyncio.create_task(_send_new(stack))
        await asyncio.wait_for(entered.wait(), timeout=2)
        if boundary == "route_cancel":
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation
        else:
            response = await operation
            assert not response.ok
        assert await stack.storage.get_session(NEW_KEY) is None
        assert _roots(tmp_path) == []


@pytest.mark.asyncio
async def test_channel_cancelled_message_preparation_cleans_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.gateway.channel_dispatch as channel_dispatch

    async with _open_stack(tmp_path / "channel-cancel.db") as stack:
        config = _managed_config(tmp_path)
        stack.manager._execution_workspace_factory = build_execution_workspace_factory(
            config, profile_home=tmp_path,
        )
        entered = asyncio.Event()

        async def prepare(*args: Any, **kwargs: Any) -> Any:
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(channel_dispatch, "_prepare_channel_user_message", prepare)
        operation = asyncio.create_task(_accept(stack, "cancel first task", config=config))
        await asyncio.wait_for(entered.wait(), timeout=2)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert await stack.storage.get_session(CHANNEL_KEY) is None
        assert _roots(tmp_path) == []


def test_failed_allocation_validation_cleans_created_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.execution_workspaces as workspaces
    from opensquilla.project_workspaces import ProjectWorkspaceStateError

    def reject(*args: Any, **kwargs: Any) -> Any:
        raise ProjectWorkspaceStateError("unavailable")

    monkeypatch.setattr(workspaces, "validate_execution_workspace", reject)
    with pytest.raises(ProjectWorkspaceStateError):
        workspaces.prepare_managed_workspace(tmp_path)
    assert _roots(tmp_path) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["rpc", "channel"])
async def test_ambiguous_acceptance_error_reads_back_committed_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str,
) -> None:
    open_stack = _open_real_stack if surface == "rpc" else _open_stack
    async with open_stack(tmp_path / "accept-outcome.db") as stack:
        config = _managed_config(tmp_path)
        stack.manager._execution_workspace_factory = build_execution_workspace_factory(
            config, profile_home=tmp_path,
        )
        if surface == "rpc":
            stack.context.config = config
        original_accept = stack.storage.accept_turn

        async def error_after_commit(*args: Any, **kwargs: Any) -> Any:
            await original_accept(*args, **kwargs)
            raise OSError("commit response unavailable")

        monkeypatch.setattr(stack.storage, "accept_turn", error_after_commit)
        if surface == "rpc":
            result = await _send_new(stack)
            assert not result.ok
        else:
            with pytest.raises(OSError, match="commit response unavailable"):
                await _accept(stack, "ambiguous commit", config=config)
        current = await stack.storage.get_session(NEW_KEY if surface == "rpc" else CHANNEL_KEY)
        assert current is not None
        assert Path(current.execution_workspace["root"]).is_dir()
        assert _roots(tmp_path) == [Path(current.execution_workspace["root"])]


@pytest.mark.asyncio
async def test_same_key_receipt_race_discards_only_losing_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.gateway.rpc_sessions as rpc_sessions

    async with _open_real_stack(tmp_path / "receipt-race.db") as stack:
        config = _managed_config(tmp_path)
        stack.context.config = config
        stack.manager._execution_workspace_factory = build_execution_workspace_factory(
            config, profile_home=tmp_path,
        )
        original_prepare = rpc_sessions.prepare_admission_route
        both_prepared = asyncio.Event()
        candidates = []

        async def prepare(*args: Any, **kwargs: Any) -> Any:
            candidates.append(kwargs["atomic_intent_plan"].node.execution_workspace)
            if len(candidates) == 2:
                both_prepared.set()
            await asyncio.wait_for(both_prepared.wait(), timeout=2)
            return await original_prepare(*args, **kwargs)

        monkeypatch.setattr(rpc_sessions, "prepare_admission_route", prepare)
        first, second = await asyncio.gather(_send_new(stack), _send_new(stack))
        assert first.ok and second.ok
        assert sorted([first.payload["replayed"], second.payload["replayed"]]) == [False, True]
        current = await stack.storage.get_session(NEW_KEY)
        assert len(candidates) == 2 and candidates[0] != candidates[1]
        assert _roots(tmp_path) == [Path(current.execution_workspace["root"])]


@pytest.mark.parametrize("mutation", ["none", "nonempty", "replacement", "symlink", "parent"])
def test_allocation_rollback_is_empty_and_identity_scoped(tmp_path: Path, mutation: str) -> None:
    prepared = prepare_managed_workspace(tmp_path)
    root = Path(prepared.binding["root"])
    preserved = tmp_path / "preserved"
    if mutation == "nonempty":
        (root / "source.txt").write_text("user source", encoding="utf-8")
    elif mutation in {"replacement", "symlink"}:
        root.rename(preserved)
        if mutation == "symlink":
            root.symlink_to(preserved, target_is_directory=True)
        else:
            root.mkdir()
    elif mutation == "parent":
        root.parent.rename(preserved)
        root.parent.mkdir()
        root.mkdir()

    prepared.rollback()
    prepared.rollback()
    if mutation == "none":
        assert not root.exists()
    else:
        assert root.exists()
        if mutation == "nonempty":
            assert (root / "source.txt").read_text(encoding="utf-8") == "user source"
    assert (tmp_path / "tasks").is_dir()


def test_late_local_replacement_with_contents_is_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepare_managed_workspace(tmp_path)
    root = Path(prepared.binding["root"])
    original_rmdir = Path.rmdir
    preserved = tmp_path / "original-allocation"

    def replace_before_rmdir(path: Path) -> None:
        assert path == root
        path.rename(preserved)
        path.mkdir()
        (path / "user-source.txt").write_text("preserve these bytes", encoding="utf-8")
        original_rmdir(path)

    # A trusted local process can still change the path after the last check.
    # This is not an atomic identity guarantee; non-recursive rmdir protects data.
    monkeypatch.setattr(Path, "rmdir", replace_before_rmdir)
    prepared.rollback()
    assert preserved.is_dir()
    assert (root / "user-source.txt").read_text(encoding="utf-8") == "preserve these bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["configured", "inherited", "legacy"])
async def test_preparation_never_owns_existing_or_configured_workspace(
    tmp_path: Path, kind: str,
) -> None:
    from opensquilla.execution_workspaces import configured_execution_workspace

    root = tmp_path / "existing"
    root.mkdir()
    config = GatewayConfig(workspace_dir=str(root)) if kind == "configured" else GatewayConfig()
    async with SessionStorage(tmp_path / "existing.db") as storage:
        manager = SessionManager(storage, execution_workspace_factory=(
            build_execution_workspace_factory(config, profile_home=tmp_path)
        ))
        options = {}
        if kind == "inherited":
            binding = configured_execution_workspace(root)
            binding["kind"] = "managed"
            options["execution_workspace"] = binding
        if kind == "legacy":
            from opensquilla.session.models import SessionNode

            await storage.upsert_session(SessionNode(session_key=NEW_KEY, session_id="legacy"))
        plan = await manager.prepare_intent(NEW_KEY, SessionIntent.CONTINUE, **options)
        assert plan.workspace_preparation is None
        assert root.is_dir()
        assert _roots(tmp_path) == []


@pytest.mark.asyncio
async def test_cancelled_allocation_waits_for_worker_and_cleans_its_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with SessionStorage(tmp_path / "allocation.db") as storage:
        manager = SessionManager(storage, execution_workspace_factory=(
            build_execution_workspace_factory(_managed_config(tmp_path), profile_home=tmp_path)
        ))
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        original_mkdir = Path.mkdir

        def paused_mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
            original_mkdir(path, *args, **kwargs)
            if path.parent == tmp_path / "tasks":
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(timeout=5), "allocation test did not release its worker"

        monkeypatch.setattr(Path, "mkdir", paused_mkdir)
        operation = asyncio.create_task(manager.prepare_intent(NEW_KEY, SessionIntent.NEW_CHAT))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            operation.cancel()
            await asyncio.sleep(0)
            operation.cancel()
            await asyncio.sleep(0)
            assert not operation.done(), "cancel abandoned a still-running allocation worker"
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await operation

        assert await storage.get_session(NEW_KEY) is None
        assert _roots(tmp_path) == []
