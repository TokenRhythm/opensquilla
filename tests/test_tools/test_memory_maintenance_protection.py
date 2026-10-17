from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.tools.builtin.memory_tools import create_memory_tools
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolContext, current_tool_context


class _Store:
    def __init__(self) -> None:
        self.removed: list[str] = []

    async def index_file(self, *, path, content, source) -> int:
        return 1

    async def remove_file(self, path: str) -> None:
        self.removed.append(path)

    async def total_size(self) -> int:
        return 0


class _Retriever:
    async def search(self, query, opts, *, intent):
        return []


def _tools(**kwargs):
    store = _Store()
    registry = ToolRegistry()
    create_memory_tools(
        store,
        _Retriever(),
        registry=registry,
        memory_config=SimpleNamespace(
            entry_ttl_days=1, max_files=0, max_file_size_kb=0, max_total_size_kb=0,
        ),
        **kwargs,
    )
    return registry.get("memory_save").handler, registry.get("memory_delete").handler, store


def _expired_note(source: Path) -> Path:
    note = source / "memory" / "2000-01-01.md"
    note.parent.mkdir(parents=True)
    note.write_text("Keep this existing dated note.", encoding="utf-8")
    os.utime(note, (946684800, 946684800))
    return note


async def test_workspace_save_and_explicit_delete_do_not_prune_other_files(tmp_path):
    source = tmp_path / "project"
    expired = _expired_note(source)
    original = expired.read_bytes()
    save, delete, store = _tools(memory_dir=str(source), memory_source="workspace")

    result = await save(content="Explicitly saved fact", path="memory/requested.md")

    assert "Saved to memory/requested.md" in result
    assert expired.read_bytes() == original
    assert store.removed == []

    result = await delete(path="memory/requested.md")

    assert "Deleted memory/requested.md" in result
    assert not (source / "memory" / "requested.md").exists()
    assert expired.read_bytes() == original
    assert store.removed == ["memory/requested.md"]


async def test_maintenance_policy_receives_the_actual_runtime_source(tmp_path):
    configured = tmp_path / "managed-source"
    project_source = tmp_path / "project-source"
    expired = _expired_note(project_source)
    seen = []

    def allow(agent_id, source, notes):
        seen.append((agent_id, source, notes))
        return source == configured

    save, _delete, store = _tools(
        workspace_base=str(configured),
        memory_source="workspace",
        maintenance_allowed=allow,
    )
    token = current_tool_context.set(ToolContext(
        agent_id="research",
        workspace_dir=str(tmp_path / "different-task"),
        memory_source_dir=str(project_source),
    ))
    try:
        await save(content="Requested fact", path="memory/requested.md")
    finally:
        current_tool_context.reset(token)

    assert seen and all(
        item == ("research", project_source, project_source / "memory") for item in seen
    )
    assert expired.exists()
    assert store.removed == []


@pytest.mark.parametrize("source_mode", ["state", "workspace"])
@pytest.mark.parametrize("task_location", ["same", "ancestor", "descendant"])
async def test_task_overlap_blocks_prune_even_when_source_policy_allows(
    tmp_path, task_location, source_mode,
):
    state_root = tmp_path / "application-state"
    source = state_root / "agents" / "main" if source_mode == "state" else tmp_path / "project"
    expired = _expired_note(source)
    task_root = {
        "same": source,
        "ancestor": tmp_path,
        "descendant": source / "subproject",
    }[task_location]
    task_root.mkdir(exist_ok=True)
    options = {"memory_base": str(state_root)} if source_mode == "state" else {
        "memory_dir": str(source),
    }
    save, _delete, store = _tools(
        memory_source=source_mode,
        maintenance_allowed=lambda _agent, _source, _notes: True,
        **options,
    )
    token = current_tool_context.set(ToolContext(workspace_dir=str(task_root)))
    try:
        await save(content="Requested fact", path="memory/requested.md")
    finally:
        current_tool_context.reset(token)

    assert expired.exists()
    assert store.removed == []


@pytest.mark.parametrize(
    ("source_mode", "with_policy"), [("state", False), ("state", True), ("workspace", True)],
)
async def test_independent_application_source_keeps_existing_inline_retention(
    tmp_path, source_mode, with_policy,
):
    state_root = tmp_path / "application-state"
    source = state_root / "agents" / "main" if source_mode == "state" else tmp_path / "managed"
    expired = _expired_note(source)
    evergreen = source / "memory" / "preferences.md"
    evergreen.write_text("Keep evergreen content.", encoding="utf-8")
    os.utime(evergreen, (946684800, 946684800))
    options = {"memory_base": str(state_root)} if source_mode == "state" else {
        "memory_dir": str(source),
    }
    if with_policy:
        options["maintenance_allowed"] = lambda _agent, actual, notes: (
            actual == source and notes == source / "memory"
        )
    save, _delete, store = _tools(memory_source=source_mode, **options)
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path / "project")))
    try:
        await save(content="Requested fact", path="memory/requested.md")
    finally:
        current_tool_context.reset(token)

    assert not expired.exists()
    assert evergreen.read_text(encoding="utf-8") == "Keep evergreen content."
    assert (source / "memory" / "requested.md").read_text(encoding="utf-8") == "Requested fact"
    assert store.removed == ["memory/2000-01-01.md"]


async def test_registration_during_sweep_stops_further_implicit_deletion(tmp_path):
    from opensquilla.memory.retention import prune_expired_memory_files

    source = tmp_path / "managed"
    first = _expired_note(source)
    second = first.with_name("2000-01-02.md")
    second.write_text("another old note", encoding="utf-8")
    os.utime(second, (946684800, 946684800))
    protected = False

    class RegisteringStore(_Store):
        async def remove_file(self, path):
            nonlocal protected
            await super().remove_file(path)
            protected = True

    result = await prune_expired_memory_files(
        memory_dir=first.parent, store=RegisteringStore(), ttl_days=1,
        maintenance_allowed=lambda: not protected,
    )
    assert result.files_pruned == 1
    assert sum(path.exists() for path in (first, second)) == 1
