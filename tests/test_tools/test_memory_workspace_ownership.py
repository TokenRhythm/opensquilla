from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.agents.scope import resolve_agent_memory_source_dir
from opensquilla.memory.retrieval import MemoryRetriever
from opensquilla.memory.store import LongTermMemoryStore
from opensquilla.tools.builtin.memory_tools import create_memory_tools
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolContext, current_tool_context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "agent_id", "inject_source"),
    [
        ("workspace", "main", True),
        ("workspace", "research", True),
        ("workspace", "main", False),
        ("state", "research", True),
    ],
)
async def test_memory_stays_with_agent_when_task_workspace_changes(
    tmp_path: Path, source: str, agent_id: str, inject_source: bool,
) -> None:
    config = SimpleNamespace(
        workspace_dir=str(tmp_path / "agent-workspace"),
        state_dir=str(tmp_path / "state"),
        agents={"research": {"workspace": str(tmp_path / "research-workspace")}},
    )
    memory_root = resolve_agent_memory_source_dir(agent_id, config, source=source)
    task_roots = [tmp_path / "task-a", tmp_path / "task-b"]
    for root in task_roots:
        root.mkdir()
        (root / "MEMORY.md").write_text("Task-local source; do not edit.", encoding="utf-8")
    store = LongTermMemoryStore(tmp_path / "memory.sqlite")
    await store.initialize()
    registry = ToolRegistry()
    create_memory_tools(
        stores={agent_id: store},
        retrievers={agent_id: MemoryRetriever(store)},
        registry=registry,
        memory_base=config.state_dir,
        memory_source=source,
        workspace_base=config.workspace_dir,
    )
    save = registry.get("memory_save").handler
    read = registry.get("memory_get").handler
    search = registry.get("memory_search").handler
    try:
        for index, task_root in enumerate(task_roots):
            context = ToolContext(
                is_owner=True,
                agent_id=agent_id,
                workspace_dir=str(task_root),
                memory_source_dir=str(memory_root) if inject_source else None,
            )
            token = current_tool_context.set(context)
            try:
                if index:
                    assert "marigold" in await read(path="memory/preferences.md")
                await save(
                    content="marigold" if index == 0 else "violet",
                    path="memory/preferences.md",
                )
                assert "marigold" in await search(query="marigold", min_score=0)
            finally:
                current_tool_context.reset(token)

        content = (memory_root / "memory" / "preferences.md").read_text(encoding="utf-8")
        assert "marigold" in content and "violet" in content
        for root in task_roots:
            assert not (root / "memory").exists()
            assert (root / "MEMORY.md").read_text(encoding="utf-8") == (
                "Task-local source; do not edit."
            )
    finally:
        await store.close()
