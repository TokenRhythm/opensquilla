from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import ANY

import pytest

from opensquilla.gateway.rpc import RpcUnavailableError


def test_windows_directory_picker_marks_owner_topmost_before_opening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import windows_directory_picker

    calls: list[object] = []

    class FakeRoot:
        def withdraw(self) -> None:
            calls.append("withdraw")

        def attributes(self, name: str, value: bool) -> None:
            calls.append(("attributes", name, value))

        def update(self) -> None:
            calls.append("update")

        def destroy(self) -> None:
            calls.append("destroy")

    def askdirectory(**kwargs):
        calls.append(("askdirectory", kwargs))
        return r"C:\repos\project"

    fake_tkinter = SimpleNamespace(
        Tk=FakeRoot,
        filedialog=SimpleNamespace(askdirectory=askdirectory),
    )
    monkeypatch.setitem(sys.modules, "tkinter", fake_tkinter)

    selected = windows_directory_picker._pick_directory(r"C:\repos")

    assert selected == r"C:\repos\project"
    assert calls == [
        "withdraw",
        ("attributes", "-topmost", True),
        "update",
        (
            "askdirectory",
            {
                "parent": ANY,
                "initialdir": r"C:\repos",
                "mustexist": True,
            },
        ),
        "destroy",
    ]


def test_windows_directory_picker_main_serializes_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from opensquilla.gateway import windows_directory_picker

    monkeypatch.setattr(
        windows_directory_picker,
        "_pick_directory",
        lambda initial_dir=None: None,
    )

    assert windows_directory_picker.main([r"C:\repos"]) == 0
    assert json.loads(capsys.readouterr().out) == {"path": None}


@pytest.mark.asyncio
async def test_gateway_windows_picker_waits_in_child_process_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.gateway.rpc_sandbox as rpc_sandbox

    communicate_started = asyncio.Event()
    release_process = asyncio.Event()
    create_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class FakeProcess:
        returncode = None

        async def communicate(self):
            communicate_started.set()
            await release_process.wait()
            self.returncode = 0
            return (json.dumps({"path": r"C:\repos\project"}).encode(), b"")

    async def fake_create_subprocess_exec(*argv, **kwargs):
        create_calls.append((argv, kwargs))
        return FakeProcess()

    monkeypatch.setattr(
        rpc_sandbox.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    picker_task = asyncio.create_task(
        rpc_sandbox._pick_directory_path_windows(r"C:\repos")
    )
    await communicate_started.wait()
    await asyncio.sleep(0)

    assert not picker_task.done()

    release_process.set()
    assert await picker_task == r"C:\repos\project"
    argv, kwargs = create_calls[0]
    assert argv == (
        sys.executable,
        "-m",
        "opensquilla.gateway.windows_directory_picker",
        r"C:\repos",
    )
    assert kwargs == {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


@pytest.mark.asyncio
async def test_gateway_windows_picker_terminates_child_when_request_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.gateway.rpc_sandbox as rpc_sandbox

    communicate_started = asyncio.Event()

    class FakeProcess:
        returncode = None
        terminated = False
        waited = False

        async def communicate(self):
            communicate_started.set()
            await asyncio.Event().wait()

        def terminate(self) -> None:
            self.terminated = True

        async def wait(self) -> None:
            self.waited = True
            self.returncode = 1

    process = FakeProcess()

    async def fake_create_subprocess_exec(*_argv, **_kwargs):
        return process

    monkeypatch.setattr(
        rpc_sandbox.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    picker_task = asyncio.create_task(
        rpc_sandbox._pick_directory_path_windows(r"C:\repos")
    )
    await communicate_started.wait()
    picker_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await picker_task

    assert process.terminated is True
    assert process.waited is True


@pytest.mark.asyncio
async def test_gateway_windows_picker_maps_child_failure_to_rpc_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.gateway.rpc_sandbox as rpc_sandbox

    class FakeProcess:
        returncode = 1

        async def communicate(self):
            return (b"", b"native picker failed")

    async def fake_create_subprocess_exec(*_argv, **_kwargs):
        return FakeProcess()

    monkeypatch.setattr(
        rpc_sandbox.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    with pytest.raises(RpcUnavailableError, match="native picker failed"):
        await rpc_sandbox._pick_directory_path_windows(None)
