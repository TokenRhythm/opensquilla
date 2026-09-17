from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.agents.registry import AgentRegistry
from opensquilla.gateway.boot import _ensure_configured_agent_workspaces
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.identity.bootstrap import RETIRED_WORKSPACE_FILENAMES, ensure_agent_workspace
from opensquilla.profile_operation_lock import ProfileOperationLock

FIXTURES = Path(__file__).parents[1] / "fixtures" / "workspace_md_retirement"


@pytest.mark.parametrize("registry_enabled", [False, True])
@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
async def test_file_rpc_contract_and_no_default_upgrade_on_reads(
    tmp_path, registry_enabled, newline
):
    root = tmp_path / "workspace"
    ensure_agent_workspace(root)
    old_agents_text = (FIXTURES / "AGENTS.txt").read_text(encoding="utf-8")
    old_agents = old_agents_text.replace("\n", newline).encode("utf-8")
    (root / "AGENTS.md").write_bytes(old_agents)
    for name in RETIRED_WORKSPACE_FILENAMES:
        (root / name).write_bytes(b"old data")
    cfg = GatewayConfig(workspace_dir=str(root))
    ctx = RpcContext(
        conn_id="test",
        config=cfg,
        agent_registry=AgentRegistry(cfg, persist_changes=False) if registry_enabled else None,
    )
    dispatcher = get_dispatcher()
    listed = await dispatcher.dispatch("list", "agents.files.list", {"agentId": "main"}, ctx)
    assert listed.error is None
    assert [item["name"] for item in listed.payload["files"]] == [
        "AGENTS.md",
        "SOUL.md",
        "IDENTITY.md",
        "USER.md",
        "MEMORY.md",
        "memory.md",
    ]
    read = await dispatcher.dispatch(
        "get",
        "agents.files.get",
        {"agentId": "main", "name": "AGENTS.md"},
        ctx,
    )
    assert read.error is None
    assert read.payload["content"] == old_agents_text
    assert (root / "AGENTS.md").read_bytes() == old_agents
    assert not (root / ".opensquilla").exists()
    for name in RETIRED_WORKSPACE_FILENAMES:
        for method in ("get", "set"):
            response = await dispatcher.dispatch(
                method,
                f"agents.files.{method}",
                {"agentId": "main", "name": name, "content": "must not be written"},
                ctx,
            )
            assert response.error.code == "INVALID_REQUEST"
            assert "Retired" in response.error.message
        assert (root / name).read_bytes() == b"old data"
    written = await dispatcher.dispatch(
        "set",
        "agents.files.set",
        {"agentId": "main", "name": "AGENTS.md", "content": ""},
        ctx,
    )
    assert written.error is None
    assert written.payload["size"] == 0
    assert (root / "AGENTS.md").read_bytes() == b""


@pytest.mark.parametrize("locked", [False, True])
def test_startup_upgrade_uses_profile_lease_not_external_state(tmp_path, monkeypatch, locked):
    home = tmp_path / "profile"
    root = tmp_path / "workspace"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    ensure_agent_workspace(root)
    originals = {}
    for name in ("AGENTS", "SOUL"):
        originals[name] = (FIXTURES / f"{name}.txt").read_bytes()
        (root / f"{name}.md").write_bytes(originals[name])
    cfg = GatewayConfig(workspace_dir=str(root), state_dir=str(tmp_path / "external-state"))
    if locked:
        with ProfileOperationLock(home):
            _ensure_configured_agent_workspaces(cfg)
        assert (root / "AGENTS.md").read_bytes() == b""
        assert len(list(root.rglob("*.bak"))) == 2
    else:
        _ensure_configured_agent_workspaces(cfg)
        assert (root / "AGENTS.md").read_bytes() == originals["AGENTS"]
        assert not list(root.rglob("*.bak"))


def test_shared_workspace_is_upgraded_once_and_disabled_agent_is_not_scanned(
    tmp_path,
    monkeypatch,
):
    from opensquilla.gateway import workspace_template_upgrade as template_upgrade

    root = tmp_path / "workspace"
    calls = []
    monkeypatch.setattr(
        template_upgrade,
        "upgrade_workspace_defaults",
        lambda workspace, **_kwargs: calls.append(workspace) or (),
    )
    cfg = GatewayConfig(
        workspace_dir=str(root),
        agents=[
            {"id": "same", "workspace": str(root)},
            {"id": "disabled", "enabled": False, "workspace": str(tmp_path / "disabled")},
        ],
    )
    _ensure_configured_agent_workspaces(cfg)
    assert calls == [root]
    assert not (tmp_path / "disabled").exists()
