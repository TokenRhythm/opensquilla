from __future__ import annotations

import os

import pytest

from opensquilla.agents.registry import AgentRegistry
from opensquilla.agents.scope import resolve_agent_workspace_dir
from opensquilla.gateway.config import AgentEntryConfig, GatewayConfig


@pytest.mark.asyncio
async def test_registry_lists_builtin_and_configured_agents() -> None:
    cfg = GatewayConfig(agents=[AgentEntryConfig(id="ops", model="openai/test")])
    registry = AgentRegistry(cfg, persist_changes=False)

    agents = await registry.list_agents()

    assert [agent["id"] for agent in agents] == ["main", "ops"]
    assert agents[0]["isBuiltin"] is True
    assert agents[1]["model"] == "openai/test"


@pytest.mark.asyncio
async def test_registry_create_and_delete_mutates_config_without_persisting() -> None:
    cfg = GatewayConfig()
    registry = AgentRegistry(cfg, persist_changes=False)

    created = await registry.create_agent(agent_id="Ops Team", model="openai/test")
    await registry.delete_agent("ops-team")

    assert created["id"] == "ops-team"
    assert cfg.agents == []


@pytest.mark.asyncio
async def test_registry_rejects_builtin_main_mutation() -> None:
    registry = AgentRegistry(GatewayConfig(), persist_changes=False)

    with pytest.raises(ValueError, match="builtin agent"):
        await registry.create_agent(agent_id="main")


def test_resolve_agent_workspace_dir_uses_configured_agent_workspace(tmp_path) -> None:
    root = tmp_path / "root"
    agent_workspace = tmp_path / "ops-workspace"
    cfg = GatewayConfig(
        workspace_dir=str(root),
        agents=[AgentEntryConfig(id="ops", workspace=str(agent_workspace))],
    )

    assert resolve_agent_workspace_dir("ops", cfg) == agent_workspace
    assert resolve_agent_workspace_dir("main", cfg) == root


@pytest.mark.asyncio
async def test_registry_get_identity_reads_configured_agent_workspace(tmp_path) -> None:
    root = tmp_path / "root"
    agent_workspace = tmp_path / "ops-workspace"
    agent_workspace.mkdir()
    (agent_workspace / "IDENTITY.md").write_text(
        "\n".join(
            [
                "# IDENTITY.md",
                "Name: **Mira**",
                "Emoji: 🦐",
                "Creature: familiar",
                "Vibe: calm",
                "Theme: ember",
                "Avatar: assets/mira.png",
            ]
        ),
        encoding="utf-8",
    )
    cfg = GatewayConfig(
        workspace_dir=str(root),
        agents=[AgentEntryConfig(id="ops", workspace=str(agent_workspace))],
    )
    registry = AgentRegistry(cfg, persist_changes=False)

    identity = await registry.get_identity("OPS")

    assert identity == {
        "agent_id": "ops",
        "name": "Mira",
        "emoji": "🦐",
        "creature": "familiar",
        "vibe": "calm",
        "theme": "ember",
        "avatar": "assets/mira.png",
    }


@pytest.mark.asyncio
async def test_registry_get_identity_missing_file_does_not_seed_workspace(tmp_path) -> None:
    workspace = tmp_path / "missing-workspace"
    cfg = GatewayConfig(workspace_dir=str(workspace))
    registry = AgentRegistry(cfg, persist_changes=False)

    identity = await registry.get_identity("main")

    assert identity == {
        "agent_id": "main",
        "name": None,
        "emoji": None,
        "creature": None,
        "vibe": None,
        "theme": None,
        "avatar": None,
    }
    assert workspace.exists() is False


@pytest.mark.asyncio
async def test_registry_get_identity_rejects_hardlinked_identity_file(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "outside-identity.md"
    source.write_text("Name: outside\n", encoding="utf-8")
    try:
        os.link(source, workspace / "IDENTITY.md")
    except OSError as exc:  # pragma: no cover - filesystem capability dependent
        pytest.skip(f"hardlinks unavailable: {exc}")
    cfg = GatewayConfig(workspace_dir=str(workspace))
    registry = AgentRegistry(cfg, persist_changes=False)

    with pytest.raises(ValueError, match="hardlinked"):
        await registry.get_identity("main")


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id", ["main", "ops"])
async def test_registry_file_reads_do_not_create_missing_workspace(tmp_path, agent_id) -> None:
    workspace = tmp_path / "missing" / agent_id
    cfg = GatewayConfig(
        workspace_dir=str(workspace),
        agents=[AgentEntryConfig(id="ops", workspace=str(workspace))],
    )
    registry = AgentRegistry(cfg, persist_changes=False)

    listed = await registry.list_agent_files(agent_id)
    with pytest.raises(FileNotFoundError):
        await registry.get_agent_file(agent_id, "AGENTS.md")

    assert listed
    assert all(item["status"] == "missing" for item in listed)
    assert not workspace.parent.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("agents_content", [b"", b"# User rules\r\nKeep this text.\r\n"])
async def test_registry_file_reads_preserve_existing_workspace(tmp_path, agents_content) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    originals = {
        "AGENTS.md": agents_content,
        "SOUL.md": b"User-maintained persona\r\n",
        "notes.txt": b"Ordinary project file\x00",
    }
    for name, content in originals.items():
        (workspace / name).write_bytes(content)
    registry = AgentRegistry(
        GatewayConfig(workspace_dir=str(workspace)), persist_changes=False,
    )

    listed = {item["name"]: item for item in await registry.list_agent_files("main")}
    read = await registry.get_agent_file("main", "AGENTS.md")

    assert listed["AGENTS.md"]["status"] == "present"
    assert listed["AGENTS.md"]["size"] == len(agents_content)
    assert listed["IDENTITY.md"]["status"] == "missing"
    assert read["content"] == agents_content.decode().replace("\r\n", "\n")
    assert {path.name: path.read_bytes() for path in workspace.iterdir()} == originals


@pytest.mark.asyncio
async def test_registry_file_set_creates_only_requested_file(tmp_path) -> None:
    workspace = tmp_path / "missing" / "workspace"
    registry = AgentRegistry(
        GatewayConfig(workspace_dir=str(workspace)), persist_changes=False,
    )

    result = await registry.set_agent_file("main", "USER.md", "Name: Mira\n")

    assert result == {
        "name": "USER.md", "path": "USER.md", "size": 11,
    }
    assert {path.name for path in workspace.iterdir()} == {"USER.md"}
    assert (workspace / "USER.md").read_text(encoding="utf-8") == "Name: Mira\n"
