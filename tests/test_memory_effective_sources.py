from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest

from opensquilla.agents.scope import resolve_agent_source_paths
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_memory import _handle_memory_list, _handle_memory_show
from opensquilla.identity.workspace import load_daily_notes
from opensquilla.memory.dream_factory import build_dream_factory
from opensquilla.memory.manager import build_memory_managers
from opensquilla.memory.retention import prune_expired_memory_files
from opensquilla.memory.source_paths import resolve_memory_source_path
from opensquilla.memory.types import MemorySearchOpts
from opensquilla.tools.builtin.memory_tools import create_memory_tools
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolContext, current_tool_context


@pytest.mark.parametrize("source", ["state", "workspace"])
@pytest.mark.parametrize("agent_id", ["main", "ops"])
def test_source_resolver_preserves_independent_existing_precedence(
    tmp_path, monkeypatch, source, agent_id,
):
    profile = tmp_path / "profile"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(profile))
    monkeypatch.setenv("OPENSQUILLA_MEMORY_DIR", str(tmp_path / "external-notes"))
    config = GatewayConfig(
        state_dir=str(tmp_path / "configured-state"),
        workspace_dir=str(tmp_path / "bootstrap"),
        agents=[{"id": "ops", "workspace": str(tmp_path / "ops-bootstrap")}],
        memory={"source": source},
    )
    paths = resolve_agent_source_paths(agent_id, config)
    expected_bootstrap = tmp_path / ("bootstrap" if agent_id == "main" else "ops-bootstrap")
    assert paths.bootstrap_dir == expected_bootstrap
    if source == "workspace":
        assert paths.memory_root == expected_bootstrap
        assert paths.notes_dir == expected_bootstrap / "memory"
    elif agent_id == "main":
        assert paths.memory_root == profile / "state" / "agents" / "main"
        assert paths.notes_dir == tmp_path / "external-notes"
    else:
        assert paths.memory_root == tmp_path / "configured-state" / "agents" / "ops"
        assert paths.notes_dir == paths.memory_root / "memory"
    dream = build_dream_factory(config=config, need_provider=False)(agent_id)
    assert dream.workspace == paths.memory_root
    assert dream.memory_dir == paths.notes_dir
    assert dream.memory_md == paths.memory_root / "MEMORY.md"
    assert not expected_bootstrap.exists()
    assert not paths.notes_dir.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["state", "workspace"])
async def test_manager_tools_sync_and_recall_share_actual_sources(tmp_path, monkeypatch, source):
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("OPENSQUILLA_MEMORY_DIR", str(tmp_path / "external-notes"))
    config = GatewayConfig(
        state_dir=str(tmp_path / "state"), workspace_dir=str(tmp_path / "bootstrap"),
        memory={"source": source, "embedding": {"provider": "none"}},
    )
    paths = resolve_agent_source_paths("main", config)
    task = tmp_path / "task"
    task.mkdir()
    (task / "MEMORY.md").write_text("project deliverable", encoding="utf-8")
    managers = await build_memory_managers(config, ["main"])
    try:
        manager = managers["main"]
        assert manager.workspace_dir == paths.memory_root
        assert manager.memory_dir == paths.notes_dir
        registry = ToolRegistry()
        create_memory_tools(
            manager.store, manager.retriever, registry=registry, memory_managers=managers,
            memory_base=str(tmp_path / "wrong-fallback"), memory_source=source,
        )
        save = registry.get("memory_save").handler
        get = registry.get("memory_get").handler
        delete = registry.get("memory_delete").handler
        token = current_tool_context.set(ToolContext(
            is_owner=True, workspace_dir=str(task), memory_source_dir=str(task),
        ))
        try:
            await save(path="MEMORY.md", content="durable marigold", mode="replace")
            await save(path="memory/preferences.md", content="notes marigold", mode="replace")
            assert await get(path="MEMORY.md") == "durable marigold"
            assert await get(path="memory/preferences.md") == "notes marigold"
            await manager.sync_manager.sync(reason="manual", force=True)
            found = await manager.retriever.search("marigold", MemorySearchOpts(min_score=0))
            assert {item.path for item in found} == {"MEMORY.md", "memory/preferences.md"}
            inspection = RpcContext(conn_id="actual-sources", memory_managers=managers)
            rows = await _handle_memory_list(None, inspection)
            assert {item["path"] for item in rows["files"]} == {
                "MEMORY.md", "memory/preferences.md",
            }
            shown = await _handle_memory_show({"path": "memory/preferences.md"}, inspection)
            assert shown["content"] == "notes marigold"
            assert "Deleted memory/preferences.md" in await delete(path="memory/preferences.md")
        finally:
            current_tool_context.reset(token)
        assert (paths.memory_root / "MEMORY.md").read_text(encoding="utf-8") == "durable marigold"
        assert not (paths.notes_dir / "preferences.md").exists()
        assert (task / "MEMORY.md").read_text(encoding="utf-8") == "project deliverable"
        assert not (task / "memory").exists()
        assert not (tmp_path / "wrong-fallback").exists()
    finally:
        await managers["main"].close()


def test_daily_notes_can_use_an_independent_notes_tree(tmp_path):
    from datetime import date

    notes = tmp_path / "notes"
    notes.mkdir()
    name = date.today().isoformat() + ".md"
    (notes / name).write_text("actual notes", encoding="utf-8")
    assert load_daily_notes(tmp_path / "memory-root", notes_dir=notes) == {name: "actual notes"}


@pytest.mark.parametrize("path", [
    "MEMORY.md", "./MEMORY.md",
    pytest.param(".\\MEMORY.md", marks=pytest.mark.skipif(os.name != "nt", reason="Windows alias")),
])
def test_logical_long_term_aliases_keep_the_memory_source_root(tmp_path, path):
    root, notes = tmp_path / "source", tmp_path / "notes"
    assert resolve_memory_source_path(root, notes, path) == root / "MEMORY.md"


@pytest.mark.asyncio
async def test_split_notes_ttl_removes_the_same_logical_index_key(tmp_path):
    notes = tmp_path / "external-notes"
    notes.mkdir()
    dated = notes / "2020-01-01.md"
    dated.write_text("expired", encoding="utf-8")
    old = time.time() - 60 * 86400
    os.utime(dated, (old, old))
    removed = []

    class Store:
        async def remove_file(self, path):
            removed.append(path)

    result = await prune_expired_memory_files(
        memory_dir=notes, workspace_dir=tmp_path / "separate-memory-root",
        store=Store(), ttl_days=30,
    )
    assert result.files_pruned == 1
    assert removed == ["memory/2020-01-01.md"]
    assert not dated.exists()


@pytest.mark.asyncio
async def test_dream_split_notes_keep_candidate_state_and_curated_target_separate(tmp_path):
    from opensquilla.memory.dream import Dream

    memory_root = tmp_path / "source"
    notes = tmp_path / "external-notes"
    memory_root.mkdir()
    notes.mkdir()
    (notes / "note.md").write_text("User prefers marigold.", encoding="utf-8")

    class Provider:
        async def complete(self, **_kwargs):
            return SimpleNamespace(content=json.dumps({"operations": [{
                "op": "upsert", "candidate_ids": ["auto"], "section": "Preferences",
                "memory_id": "marigold", "text": "- User prefers marigold.",
            }]}))

    dream = Dream(
        workspace=memory_root, notes_dir=notes, provider=Provider(), session_lock=None,
        config=SimpleNamespace(evidence_min_score=0.0, min_batch_size=1),
    )
    result = await dream.run()
    assert result.error is None
    assert result.apply_status == "ok"
    assert "marigold" in (memory_root / "MEMORY.md").read_text(encoding="utf-8")
    assert (notes / ".dream_cursor").exists()
    assert (notes / ".dream_state" / "promotion_evidence.json").exists()
    receipts = list((notes / ".dream_receipts").glob("*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["candidate_paths"] == ["memory/note.md"]
    assert result.edit_receipt_path == f"memory/.dream_receipts/{receipts[0].name}"
    assert not (memory_root / "memory").exists()
