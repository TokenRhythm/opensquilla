"""Real post-accept workspace rejection, bounded together with its owned cleanup."""

from __future__ import annotations

import asyncio
import sys
import time
from functools import wraps
from pathlib import Path
from typing import Any
from unittest.mock import patch

from opensquilla.engine.types import DoneEvent
from opensquilla.gateway import admission_preparation, direct_turn_runtime, rpc_sessions
from opensquilla.gateway.agent_tasks import get_agent_task_registry
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.sandbox.capability_service import (
    REQUIRED_SAFE_CAPABILITIES,
    WINDOWS_REQUIRED_SAFE_CAPABILITIES,
    CapabilityReport,
)
from opensquilla.sandbox.run_context import RUN_CONTEXT_ORIGIN_KEY
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from tests.helpers.sqlite_process_probe import READY

ERROR_TEXT = "Error: The project directory is unavailable."


async def stable_safe_capability(_config: Any) -> CapabilityReport:
    # Preserve the original module's autouse fixture in this standalone child.
    return CapabilityReport.available_for(
        backend="test", platform=sys.platform,
        capabilities=(
            REQUIRED_SAFE_CAPABILITIES | WINDOWS_REQUIRED_SAFE_CAPABILITIES
            if sys.platform.startswith("win") else REQUIRED_SAFE_CAPABILITIES
        ),
    )


async def block_at_fault(fault: str) -> None:
    print(f"fault={fault}", flush=True)
    print(f"phase={fault}-blocked", flush=True)
    print(f"PROJECT_REVALIDATION_{fault.upper().replace('-', '_')}_ENTERED", flush=True)
    while True:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            continue


async def scenario(directory: Path, fault: str) -> None:
    print("phase=storage-setup", flush=True)
    storage = await SessionStorage.open(str(directory / "sessions.db"))
    connections = [storage.conn]
    if storage._transcript_reader is not None:
        connections.append(storage._transcript_reader)
    threads = [connection._thread for connection in connections]
    manager = SessionManager(storage, inject_time_prefix=False)
    context = RpcContext(
        conn_id="project-workspace",
        principal=Principal(
            role="operator", scopes=frozenset({"operator.admin"}),
            is_owner=True, authenticated=True,
        ),
        config=GatewayConfig(
            workspace_dir=str(directory / "default-workspace"),
            memory={}, naming={"enabled": False},
        ),
        session_manager=manager,
        task_runtime=None,
    )
    registry = get_agent_task_registry()
    owned: list[tuple[str, asyncio.Task[Any]]] = []
    events: list[tuple[str, str, dict[str, Any]]] = []
    calls: list[dict[str, Any]] = []
    replaced = False
    admission_checked = asyncio.Event()
    persistence_entered = asyncio.Event()
    original_register = registry.register
    original_publish = rpc_sessions._emit_to_subscribers
    original_append = manager.append_message
    original_accept = storage.accept_turn
    original_context = direct_turn_runtime.authoritative_project_run_context
    project_path = directory / "project"

    class Runner:
        async def run(
            self, message: str, session_key: str, *,
            expected_session_id: str | None = None,
            expected_session_epoch: int | None = None, **kwargs: Any,
        ):
            calls.append(kwargs)
            if fault == "bypass-revalidation":
                print("fault=bypass-revalidation", flush=True)
            yield DoneEvent()

    def register(key: str, task: asyncio.Task[Any], **kwargs: Any) -> None:
        original_register(key, task, **kwargs)
        owned.append((key, task))  # Retain the actual task even after registry removal.

    async def publish(ctx: Any, key: str, name: str, payload: dict[str, Any]) -> None:
        await original_publish(ctx, key, name, payload)
        events.append((key, name, payload))

    async def replace_after_accept(*args: Any, **kwargs: Any) -> Any:
        nonlocal replaced
        acceptance = await original_accept(*args, **kwargs)
        if not replaced:
            project_path.rmdir()
            project_path.write_text("not a directory", encoding="utf-8")
            replaced = True
            print("phase=replaced-after-real-accept", flush=True)
        return acceptance

    @wraps(original_append)
    async def append_message(key: str, role: str, content: Any, **kwargs: Any) -> Any:
        if role != "system" or content != ERROR_TEXT:
            return await original_append(key, role, content, **kwargs)
        print("phase=error-persistence", flush=True)
        if fault in {"hang-persist", "hang-persist-with-prior-error"}:
            # Until admission has been checked this wait remains cancellable:
            # an unrelated parent assertion must not become an expected hang.
            persistence_entered.set()
            await admission_checked.wait()
            await block_at_fault(fault)
        if fault == "slow-error-commit":
            original_commit = storage.conn.commit
            commits = 0

            async def delayed_commit() -> None:
                nonlocal commits
                commits += 1
                print("error_commit_delay=2.1", flush=True)
                await asyncio.sleep(2.1)  # Inject latency; never use it as readiness.
                await original_commit()

            started = time.monotonic()
            with patch.object(storage.conn, "commit", delayed_commit):
                result = await original_append(key, role, content, **kwargs)
            elapsed = time.monotonic() - started
            print(f"error_commit_elapsed_seconds={elapsed:.6f}", flush=True)
            assert commits == 1, "slow control did not preserve one real error commit"
            assert elapsed > 2.0, "slow control did not exceed the old two-second guard"
            return result
        result = await original_append(key, role, content, **kwargs)
        if fault == "duplicate-error-append":
            print("fault=duplicate-error-append", flush=True)
            await original_append(key, role, content, **kwargs)
        return result

    try:
        with patch.object(admission_preparation, "current_sandbox_capability_report",
                          stable_safe_capability):
            project_path.mkdir()
            opened = await get_dispatcher().dispatch(
                "open-project", "workspaces.open",
                {"path": str(project_path), "trusted": True}, context,
            )
            assert opened.ok is True
            project = await storage.get_project_workspace(opened.payload["workspace"]["id"])
            assert project is not None
            parent = await manager.create(
                "agent:main:webchat:fork-post-accept-file", workspace_id=project.workspace_id,
                origin={RUN_CONTEXT_ORIGIN_KEY: {
                    "run_mode": "standard", "workspace": project.path,
                }},
            )
            fork_before = await manager.append_message(parent.session_key, "user", "fork here")
            accepted_context = None
            if fault == "bypass-revalidation":
                accepted_context = await original_context(
                    storage=storage, session_manager=manager, session=parent,
                    config=context.config, default_workspace=context.config.workspace_dir,
                )

            async def revalidate(**kwargs: Any) -> Any:
                if fault == "bypass-revalidation":
                    assert accepted_context is not None
                    return accepted_context  # Deliberately reuse a stale, once-valid context.
                return await original_context(**kwargs)

            context.turn_runner = Runner()
            with (
                patch.object(storage, "accept_turn", replace_after_accept),
                patch.object(manager, "append_message", append_message),
                patch.object(registry, "register", register),
                patch.object(rpc_sessions, "_emit_to_subscribers", publish),
                patch.object(direct_turn_runtime, "authoritative_project_run_context", revalidate),
            ):
                if fault not in {"hang-persist", "hang-cleanup"}:
                    print(READY, flush=True)
                response = await get_dispatcher().dispatch(
                    "fork-post-accept-file", "sessions.send",
                    {"key": parent.session_key, "message": "forked",
                     "forkBeforeMessageId": fork_before.message_id}, context,
                )
                assert response.ok is True
                child_key = response.payload["sessionKey"]
                assert len(owned) == 1 and owned[0][0] == child_key, (
                    "fork acceptance did not register exactly one owned direct task"
                )
                assert replaced and project_path.is_file()
                task = owned[0][1]
                if fault in {"hang-persist", "hang-persist-with-prior-error"}:
                    assert calls == [], "replaced project reached the runner"
                    print("phase=admission-verified", flush=True)
                    if fault == "hang-persist-with-prior-error":
                        observation = asyncio.create_task(persistence_entered.wait())
                        try:
                            done, _ = await asyncio.wait(
                                [task, observation], return_when=asyncio.FIRST_COMPLETED,
                            )
                            if task in done:
                                await task  # Preserve an early execution error.
                                raise AssertionError(
                                    "direct task exited before its persistence gate"
                                )
                            await observation
                        finally:
                            observation.cancel()
                            await asyncio.gather(observation, return_exceptions=True)
                        print(f"fault={fault}", flush=True)
                        raise RuntimeError(
                            "prior failure must not become an expected persistence hang"
                        )
                    admission_checked.set()
                print("phase=wait-owned-task", flush=True)
                await task  # The parent bounds this task AND its real resource cleanup.
                assert task.done() and not task.cancelled() and task.cancelling() == 0
                assert replaced and project_path.is_file()
                assert calls == [], "replaced project reached the runner"
                transcript = await manager.get_transcript(child_key)
                errors = [entry for entry in transcript
                          if entry.role == "system" and entry.content == ERROR_TEXT]
                assert len(errors) == 1, "workspace rejection was not persisted exactly once"
                terminal = [(name, payload) for key, name, payload in events
                            if key == child_key and name in {
                                "session.event.done", "session.event.error",
                            }]
                assert len(terminal) == 1 and terminal[0][0] == "session.event.error"
                assert terminal[0][1]["code"] == "WORKSPACE_UNAVAILABLE"
                assert terminal[0][1]["details"]["reason"] == "unavailable"
                print("phase=workspace-revalidation-verified", flush=True)
                if fault == "hang-cleanup-with-prior-error":
                    print(f"fault={fault}", flush=True)
                    raise RuntimeError("prior failure must not become an expected cleanup hang")
    finally:
        original_error = sys.exc_info()[1]
        cleanup_errors: list[BaseException] = []
        print("phase=cleanup", flush=True)
        try:
            async with registry.quiesce_sessions(key for key, _task in owned):
                for key, task in owned:
                    assert task.done(), "owned direct task was not settled"
                    assert registry.get(key) is None
                    assert not registry._unsettled_tasks.get(key)
                    assert not registry._cleanup_tasks.get(key)
            print("phase=direct-task-cleanup-complete", flush=True)
        except BaseException as error:
            cleanup_errors.append(error)
        try:
            if (
                fault in {"hang-cleanup", "hang-cleanup-with-prior-error"}
                and original_error is None and not cleanup_errors
            ):
                async def blocked_close() -> None:
                    assert connections[0]._thread.is_alive()
                    await block_at_fault(fault)

                # Inject at close entry, before any reader/writer cleanup can
                # raise; a real close error cannot be hidden by this fault.
                with patch.object(storage, "close", blocked_close):
                    await storage.close()
            else:
                await storage.close()
            for thread in threads:
                await asyncio.to_thread(thread.join)
                assert not thread.is_alive(), "SQLite thread was not released"
            assert storage._conn is None and storage._transcript_reader is None
        except BaseException as error:
            cleanup_errors.append(error)
        if cleanup_errors:
            primary = original_error if original_error is not None else cleanup_errors[0]
            for error in cleanup_errors:
                if error is not primary:
                    primary.add_note(f"cleanup also failed: {error!r}")
            if original_error is None:
                raise primary
        else:
            print("phase=cleanup-complete", flush=True)
    print("project_workspace_revalidation_contract=passed", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    print("probe_stdout_utf8=\u2713", flush=True)
    print("probe_stderr_utf8=\u2713", file=sys.stderr, flush=True)
    asyncio.run(scenario(Path(sys.argv[1]), sys.argv[2]))
