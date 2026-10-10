from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from types import SimpleNamespace

import pytest

from opensquilla.sandbox.backend import windows_setup_process as process_setup
from opensquilla.sandbox.setup_state import SandboxSetupState, SetupResult


class FakeProcess:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.returncode = None
        self.killed = False
        self.exited = asyncio.Event()

    def event(self, event: dict) -> None:
        self.stdout.feed_data((process_setup._PROTOCOL_PREFIX + json.dumps(event) + "\n").encode())

    def finish(self, code=0) -> None:
        self.returncode = code
        self.stdout.feed_eof()
        self.exited.set()

    async def wait(self) -> int:
        await self.exited.wait()
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.finish(-1)


def ready_payload() -> dict:
    return SetupResult(
        state=SandboxSetupState.READY, platform="win32", message="Ready"
    ).to_payload()


@pytest.fixture
async def fake_launcher(monkeypatch):
    child = FakeProcess()
    calls = []

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return child

    def no_threads(*args, **kwargs):
        pytest.fail("Gateway setup must not put UAC into the default executor")

    monkeypatch.setattr(process_setup, "_process_identity", lambda pid: f"created:{pid}")
    monkeypatch.setattr(process_setup, "_UNCONFIRMED_LAUNCHER", None)
    monkeypatch.setattr(process_setup.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(process_setup.asyncio, "to_thread", no_threads)
    return child, calls


async def test_progress_invalidates_capability_before_terminal_result(fake_launcher):
    child, calls = fake_launcher
    invalidations = []
    operation = asyncio.create_task(
        process_setup.run_windows_setup_process(None, on_unavailable=invalidations.append)
    )
    await asyncio.sleep(0)
    child.event({"event": "unavailable", "detail": "offline_identity=not ready"})
    await asyncio.sleep(0)
    assert invalidations == ["offline_identity=not ready"]
    assert not operation.done()
    child.event({"event": "result", "result": ready_payload()})
    await asyncio.sleep(0)
    assert not operation.done(), "a ready message alone is not confirmed helper exit"
    child.finish()
    assert (await operation).state is SandboxSetupState.READY
    assert not child.killed
    assert calls[0][1]["limit"] == 8192


async def test_gateway_shutdown_cancels_launcher_without_waiting_for_uac(fake_launcher):
    child, _ = fake_launcher
    operation = asyncio.create_task(process_setup.run_windows_setup_process(None))
    await asyncio.sleep(0)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(operation, 0.5)
    assert child.killed and child.returncode is not None


async def test_timeout_does_not_claim_elevated_work_has_stopped(fake_launcher, monkeypatch):
    child, _ = fake_launcher
    monkeypatch.setattr(process_setup, "OPERATION_TIMEOUT_SECONDS", 0.01)
    result = await process_setup.run_windows_setup_process(None)
    assert result.state is SandboxSetupState.FAILED
    assert "elevated_completion_unconfirmed" in result.detail
    assert child.killed


async def test_unconfirmed_kill_blocks_retry_until_actual_exit(fake_launcher, monkeypatch):
    child, calls = fake_launcher
    monkeypatch.setattr(process_setup, "OPERATION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(process_setup, "CLEANUP_TIMEOUT_SECONDS", 0.01)

    def deny_kill():
        raise PermissionError("simulated native denial")

    monkeypatch.setattr(child, "kill", deny_kill)
    result = await process_setup.run_windows_setup_process(None)
    assert "exit_unconfirmed" in result.detail
    retry = await process_setup.run_windows_setup_process(None)
    assert "previous_operation_unconfirmed" in retry.detail
    assert len(calls) == 1
    child.finish(0)
    # The fake factory returns the same, now-exited child. What matters here
    # is that retry is no longer blocked after observing the real exit.
    await process_setup.run_windows_setup_process(None)
    assert len(calls) == 2


async def test_cancel_retains_unconfirmed_child_without_holding_shutdown(
    fake_launcher, monkeypatch
):
    child, _ = fake_launcher
    monkeypatch.setattr(process_setup, "CLEANUP_TIMEOUT_SECONDS", 0.01)

    def deny_kill():
        raise PermissionError("simulated native denial")

    monkeypatch.setattr(child, "kill", deny_kill)
    operation = asyncio.create_task(process_setup.run_windows_setup_process(None))
    await asyncio.sleep(0)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(operation, 0.5)
    assert process_setup._UNCONFIRMED_LAUNCHER is child
    child.finish(0)


async def test_second_shutdown_cancel_still_retains_retiring_child(fake_launcher, monkeypatch):
    child, _ = fake_launcher
    cleanup_started = asyncio.Event()

    async def interrupted_cleanup(_child):
        cleanup_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(process_setup, "_stop_launcher", interrupted_cleanup)
    operation = asyncio.create_task(process_setup.run_windows_setup_process(None))
    await asyncio.sleep(0)
    operation.cancel()
    await asyncio.wait_for(cleanup_started.wait(), 0.5)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert process_setup._UNCONFIRMED_LAUNCHER is child
    child.finish(0)


async def test_nonzero_exit_cannot_advertise_ready(fake_launcher):
    child, _ = fake_launcher
    child.event({"event": "result", "result": ready_payload()})
    child.finish(2)
    result = await process_setup.run_windows_setup_process(None)
    assert result.state is SandboxSetupState.FAILED


async def test_output_limit_stops_broken_launcher(fake_launcher):
    child, _ = fake_launcher
    child.stdout.feed_data(b"unexpected diagnostic\n" * 4096)
    result = await process_setup.run_windows_setup_process(None)
    assert result.state is SandboxSetupState.FAILED
    assert child.killed


async def test_repair_flag_is_explicit_and_config_not_serialized(fake_launcher):
    child, calls = fake_launcher
    child.event({"event": "result", "result": ready_payload()})
    child.finish()
    await process_setup.run_windows_setup_process(
        SimpleNamespace(secret="not-for-a-command-line"), repair_identity=True
    )
    argv = calls[0][0]
    payload = json.loads(base64.urlsafe_b64decode(argv[-1]))
    assert payload["repairIdentity"] == "true"
    assert "not-for-a-command-line" not in repr(argv)


@pytest.mark.parametrize("stale", ["ownerPid", "launcherPid"])
def test_late_uac_cannot_mutate_after_either_owner_exits(monkeypatch, stale):
    payload = {
        "ownerPid": "1",
        "ownerCreated": "created:1",
        "launcherPid": "2",
        "launcherCreated": "created:2",
    }
    monkeypatch.setattr(
        process_setup,
        "_process_identity",
        lambda pid: None if str(pid) == payload[stale] else f"created:{pid}",
    )
    with pytest.raises(OSError, match="owner_retired"):
        process_setup.require_setup_owner(payload)


def test_recycled_pid_and_partial_owner_payload_fail_closed(monkeypatch):
    monkeypatch.setattr(process_setup, "_process_identity", lambda pid: "new-process")
    with pytest.raises(OSError, match="owner_invalid"):
        process_setup.require_setup_owner({"ownerPid": "1"})
    with pytest.raises(OSError, match="owner_retired"):
        process_setup.require_setup_owner({"ownerPid": "1", "ownerCreated": "old-process"})


def test_launcher_preserves_gateway_owner_and_passes_progress_and_repair(monkeypatch, capsys):
    from opensquilla.sandbox import setup_state

    monkeypatch.setattr(process_setup, "_OPERATION_OWNER", {})
    monkeypatch.setattr(process_setup, "_process_identity", lambda pid: f"created:{pid}")
    monkeypatch.setattr(
        process_setup.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: None)
    )
    observed = []

    def run(config, *, on_unavailable, repair_identity):
        observed.append(process_setup.enrich_setup_payload({"markerPath": "validated-elsewhere"}))
        assert repair_identity is True
        on_unavailable("identity_invalid")
        return SetupResult(SandboxSetupState.READY, "win32", "Ready")

    monkeypatch.setattr(setup_state, "_ensure_windows_setup_sync", run)
    owner = {"ownerPid": "77", "ownerCreated": "created:77", "repairIdentity": "true"}
    code = process_setup.setup_launcher_main(process_setup._launcher_command(owner)[-2:])
    assert code == 0
    assert observed[0]["ownerPid"] == "77"
    assert observed[0]["launcherPid"] == str(os.getpid())
    events = [json.loads(line.split(":", 1)[1]) for line in capsys.readouterr().out.splitlines()]
    assert [event["event"] for event in events] == ["unavailable", "result"]


def test_retired_owner_stops_launcher_before_setup(monkeypatch, capsys):
    from opensquilla.sandbox import setup_state

    monkeypatch.setattr(process_setup, "_OPERATION_OWNER", {})
    monkeypatch.setattr(process_setup, "_process_identity", lambda pid: f"created:{pid}")
    monkeypatch.setattr(
        setup_state, "_ensure_windows_setup_sync", lambda *a, **kw: pytest.fail("must not prepare")
    )
    owner = {"ownerPid": "77", "ownerCreated": "previous-process"}
    assert process_setup.setup_launcher_main(process_setup._launcher_command(owner)[-2:]) == 1
    assert "owner_retired" in capsys.readouterr().out


def test_frozen_launcher_uses_existing_gateway_entry(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    command = process_setup._launcher_command({"ownerPid": "1", "ownerCreated": "created:1"})
    assert command[:2] == [sys.executable, "--windows-setup-launcher"]


async def test_real_child_pipe_and_shutdown_without_any_native_setup(monkeypatch):
    """Exercise Windows subprocess transport; the child only sleeps."""
    monkeypatch.setattr(process_setup, "_process_identity", lambda pid: f"created:{pid}")
    monkeypatch.setattr(
        process_setup,
        "_launcher_command",
        lambda owner: [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    spawned = []
    real_spawn = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        child = await real_spawn(*args, **kwargs)
        spawned.append(child)
        return child

    monkeypatch.setattr(process_setup.asyncio, "create_subprocess_exec", spawn)
    operation = asyncio.create_task(process_setup.run_windows_setup_process(None))
    for _ in range(100):
        if spawned:
            break
        await asyncio.sleep(0.01)
    assert spawned
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(operation, 3)
    assert spawned[0].returncode is not None
