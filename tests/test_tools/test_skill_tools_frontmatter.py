"""skill_create / skill_edit frontmatter must round-trip through the loader."""

from __future__ import annotations

import pytest

from opensquilla.skills.loader import SkillLoader
from opensquilla.tools.builtin import skill_tools
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import ToolContext, ToolError, current_tool_context


@pytest.fixture()
def loader(tmp_path):
    workspace = tmp_path / "workspace-skills"
    workspace.mkdir()
    ldr = SkillLoader(
        workspace_dir=workspace,
        snapshot_path=tmp_path / "cache" / "skills_snapshot.json",
    )
    saved = skill_tools._loader
    skill_tools.create_skill_tools(ldr)
    yield ldr
    skill_tools._loader = saved


def _handler(name):
    registered = get_default_registry().get(name)
    assert registered is not None
    return registered.handler


@pytest.mark.parametrize(
    "description",
    [
        "Helps with: budgets and planning",
        "[DRAFT] budget helper",
        "use #tags carefully",
    ],
)
async def test_skill_create_description_round_trips(loader, description):
    await _handler("skill_create")(
        name="budget-helper",
        description=description,
        content="Do budget things.",
    )

    spec = loader.get_by_name("budget-helper")
    assert spec is not None
    assert spec.description == description


async def test_skill_create_trigger_with_colon_stays_a_string(loader):
    await _handler("skill_create")(
        name="deploy-helper",
        description="deploy helper",
        content="Deploy things.",
        triggers=["deploy: production"],
    )

    spec = loader.get_by_name("deploy-helper")
    assert spec is not None
    assert spec.triggers == ["deploy: production"]
    loader.find_by_trigger("please deploy: production now")


async def test_skill_edit_preserves_description_needing_quotes(loader):
    skill_dir = loader.workspace_dir / "notes-helper"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        '---\nname: notes-helper\ndescription: "Notes: capture and file them"\n---\n\nOld body.\n',
        encoding="utf-8",
    )
    loader.invalidate_cache()
    assert loader.get_by_name("notes-helper") is not None

    await _handler("skill_edit")(name="notes-helper", content="New body.")

    spec = loader.get_by_name("notes-helper")
    assert spec is not None
    assert spec.description == "Notes: capture and file them"
    assert spec.content == "New body."


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        ("skill_list", {}),
        ("skill_view", {"name": "secret"}),
        (
            "skill_create",
            {"name": "guest-skill", "description": "guest", "content": "guest"},
        ),
        ("skill_edit", {"name": "secret", "content": "guest"}),
        ("skill_delete", {"name": "secret"}),
    ],
)
async def test_guest_direct_skill_handler_calls_are_denied(loader, name, kwargs):
    token = current_tool_context.set(ToolContext(guest_safe=True))
    try:
        with pytest.raises(ToolError, match="GUEST_TOOL_UNAVAILABLE"):
            await _handler(name)(**kwargs)
    finally:
        current_tool_context.reset(token)
