from __future__ import annotations

from opensquilla.tools.builtin.memory_tools import create_memory_tools
from opensquilla.tools.registry import ToolRegistry


def test_memory_tool_descriptions_keep_profile_out_of_memory_save() -> None:
    registry = ToolRegistry()
    create_memory_tools(object(), object(), registry=registry, memory_source="workspace")

    memory_search = registry.get("memory_search")
    memory_save = registry.get("memory_save")

    assert memory_search is not None
    assert memory_save is not None
    assert "decisions, dates, people, preferences, or todos" not in memory_search.spec.description
    assert "User identity/profile fields" in memory_search.spec.description
    assert "USER.md" in memory_search.spec.description
    assert "By default, searches curated memory source files" in memory_search.spec.description
    assert "source=sessions for indexed transcript snippets" in memory_search.spec.description
    assert "Use memory_get only for source=memory results" in memory_search.spec.description
    assert "Do not use memory_search for current user identity/profile questions" in (
        memory_search.spec.description
    )
    assert "configured Agent sources" in memory_save.spec.description
    assert (
        "Do not use memory_save for USER.md, IDENTITY.md, or SOUL.md"
        in memory_save.spec.description
    )
    assert "agent_file" not in memory_save.spec.description
