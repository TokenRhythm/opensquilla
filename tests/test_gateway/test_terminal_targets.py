from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway import terminal_sessions
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.terminal_targets import (
    TerminalTarget,
    TerminalTargetResolver,
    parse_terminal_target,
)
from opensquilla.gateway.workspace_files import WorkspaceFileUnavailableError, _directory
from opensquilla.project_workspaces import project_path_key
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"target": None},
        {"target": {"kind": "other", "agentId": "main"}},
        {"target": {"kind": "default-agent", "agentId": "../outside"}},
        {"target": {"kind": "default-agent", "agentId": ""}},
        {"target": {"kind": "default-agent", "agentId": "main", "cwd": "C:/outside"}},
        {"target": {"kind": "project", "workspaceId": "\x00"}},
        {"target": {"kind": "session", "sessionKey": 123}},
        {"target": {"kind": "project", "workspaceId": "project"}, "sessionKey": "legacy"},
    ],
)
def test_terminal_target_rejects_ambiguous_or_invalid_authority(values):
    with pytest.raises(ValueError):
        parse_terminal_target(values)


def test_terminal_target_preserves_legacy_and_normalizes_agent():
    key = "agent:main:webchat:existing"
    assert parse_terminal_target({"sessionKey": key}).payload() == {
        "kind": "session",
        "sessionKey": key,
    }
    assert parse_terminal_target(
        {"target": {"kind": "default-agent", "agentId": "default"}}
    ).payload() == {
        "kind": "default-agent",
        "agentId": "main",
    }


@pytest.mark.asyncio
async def test_default_terminal_uses_visible_configured_root_without_session(tmp_path):
    root = tmp_path / "visible"
    config = GatewayConfig(workspace_dir=str(root))
    manager = SimpleNamespace(create=AsyncMock(side_effect=AssertionError("created a session")))
    resolver = TerminalTargetResolver(config, manager, TerminalTarget("default-agent", "main"))
    workspace = await resolver.workspace()
    assert workspace.root == root.resolve()
    assert root.is_dir()
    assert not (tmp_path / "tasks").exists()
    manager.create.assert_not_awaited()

    # Removing a root while a terminal runs must not recreate it during checks.
    root.rmdir()
    with pytest.raises(WorkspaceFileUnavailableError):
        await resolver.workspace()
    assert not root.exists()


@pytest.mark.asyncio
async def test_default_terminal_falls_back_to_visible_home_not_task_or_process_cwd(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    resolver = TerminalTargetResolver(
        GatewayConfig(workspace_dir=None),
        None,
        TerminalTarget("default-agent", "main"),
    )
    workspace = await resolver.workspace()
    assert workspace.root == (tmp_path / "workspace").resolve()
    assert not (tmp_path / "tasks").exists()


@pytest.mark.asyncio
async def test_agent_target_validates_configuration_and_binding(tmp_path):
    root = tmp_path / "agent"
    config = SimpleNamespace(
        workspace_dir=str(tmp_path / "default"),
        agents={"research": {"workspace": str(root), "enabled": True}},
    )
    resolver = TerminalTargetResolver(config, None, TerminalTarget("default-agent", "research"))
    original = await resolver.workspace()
    assert original.root == root.resolve()
    new_root = tmp_path / "changed"
    new_root.mkdir()
    config.agents["research"]["workspace"] = str(new_root)
    assert (await resolver.workspace()).binding != original.binding
    config.agents["research"]["enabled"] = False
    with pytest.raises(WorkspaceFileUnavailableError):
        await resolver.workspace()
    with pytest.raises(WorkspaceFileUnavailableError):
        await TerminalTargetResolver(
            config, None, TerminalTarget("default-agent", "unknown")
        ).workspace()


@pytest.mark.asyncio
async def test_project_target_resolves_without_session_and_revokes_on_removal(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    async with SessionStorage(tmp_path / "state.db") as storage:
        project = await storage.create_or_restore_project_workspace(
            path=str(root.resolve()),
            path_key=project_path_key(root, strict=True),
            display_name="project",
            trusted_at=1,
        )
        manager = SessionManager(storage)
        manager.create = AsyncMock(side_effect=AssertionError("created a session"))
        resolver = TerminalTargetResolver(
            GatewayConfig(),
            manager,
            TerminalTarget("project", project.workspace_id),
        )
        assert (await resolver.workspace()).root == root.resolve()
        manager.create.assert_not_awaited()
        await storage.remove_project_workspace(project.workspace_id)
        with pytest.raises(WorkspaceFileUnavailableError):
            await resolver.workspace()


@pytest.mark.asyncio
async def test_project_target_rejects_unknown_untrusted_and_missing_roots(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    async with SessionStorage(tmp_path / "state.db") as storage:
        manager = SessionManager(storage)
        project = await storage.create_or_restore_project_workspace(
            path=str(root.resolve()),
            path_key=project_path_key(root, strict=True),
            display_name="project",
            trusted_at=None,
        )
        for workspace_id in ("unknown", project.workspace_id):
            resolver = TerminalTargetResolver(
                GatewayConfig(),
                manager,
                TerminalTarget("project", workspace_id),
            )
            with pytest.raises(WorkspaceFileUnavailableError):
                await resolver.workspace()


@pytest.mark.asyncio
async def test_default_target_retains_relative_cwd_and_denied_path_checks(tmp_path):
    config = GatewayConfig(workspace_dir=str(tmp_path))
    config.sandbox.denied_read_roots = [str(tmp_path / "private")]
    (tmp_path / "private").mkdir()
    resolver = TerminalTargetResolver(config, None, TerminalTarget("default-agent", "main"))
    workspace = await resolver.workspace()
    for relative in ("../outside", "private"):
        with pytest.raises(ValueError):
            _directory(workspace, relative)


@pytest.mark.asyncio
async def test_standalone_target_output_and_exit_keep_target_without_fake_session(
    tmp_path, monkeypatch
):
    target = TerminalTarget("default-agent", "main")
    resolver = TerminalTargetResolver(GatewayConfig(workspace_dir=str(tmp_path)), None, target)
    events = []
    handle = SimpleNamespace(raw=SimpleNamespace(pid=0))
    chunks = iter([b"ready\r\n"])

    async def read(*_args):
        return next(chunks, b"")

    async def wait(*_args):
        return 0

    async def send(event, payload):
        events.append((event, payload))
        return True

    monkeypatch.setattr(terminal_sessions, "spawn_pty", lambda *args, **kwargs: handle)
    monkeypatch.setattr(terminal_sessions, "read_pty", read)
    monkeypatch.setattr(terminal_sessions, "wait_pty", wait)
    manager = terminal_sessions.TerminalManager(send_event=send)
    result = await manager.create(
        conn_id="standalone",
        session_key=None,
        target=target.payload(),
        workspace_service=resolver,
        cwd="",
        command=None,
        cols=80,
        rows=24,
    )
    task = manager._sessions[result["terminalId"]].reader_task
    await asyncio.wait_for(task, timeout=1)
    assert result["cwdPath"] == str(tmp_path.resolve())
    for payload in [result, *(payload for _, payload in events)]:
        assert payload["target"] == target.payload()
        assert "sessionKey" not in payload
    assert [kind for kind, _ in events] == ["terminal.output", "terminal.exit"]


@pytest.mark.asyncio
async def test_target_change_during_spawn_reaps_pty_before_publishing(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    config = GatewayConfig(workspace_dir=str(first))
    target = TerminalTarget("default-agent", "main")
    resolver = TerminalTargetResolver(config, None, target)
    handle = SimpleNamespace(raw=SimpleNamespace(pid=0))
    terminated = asyncio.Event()

    def spawn(*_args, **_kwargs):
        config.workspace_dir = str(second)
        return handle

    async def terminate(value):
        assert value is handle
        terminated.set()

    send = AsyncMock(return_value=True)
    monkeypatch.setattr(terminal_sessions, "spawn_pty", spawn)
    monkeypatch.setattr(terminal_sessions, "terminate_pty", terminate)
    manager = terminal_sessions.TerminalManager(send_event=send)
    with pytest.raises(terminal_sessions.TerminalSessionError, match="directory changed"):
        await manager.create(
            conn_id="standalone",
            session_key=None,
            target=target.payload(),
            workspace_service=resolver,
            cwd="",
            command=None,
            cols=80,
            rows=24,
        )
    await asyncio.wait_for(terminated.wait(), timeout=1)
    assert not manager._sessions
    assert not manager._creating
    send.assert_not_awaited()
