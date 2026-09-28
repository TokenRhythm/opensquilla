"""Manual candidates stay lightweight and independent from model discovery."""

import json

import pytest

from opensquilla.gateway import rpc_skills
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext
from opensquilla.skills import eligibility
from opensquilla.skills.loader import SkillLoader
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolSpec


@pytest.fixture
def skill_context(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(eligibility, "_live_skills_cfg_getter", None)
    workspace = tmp_path / "skills"
    for name, frontmatter in (
        ("ordinary", ""),
        ("manual", "disable-model-invocation: true\n"),
        ("automatic", "user-invocable: false\n"),
        ("disabled", ""),
        ("code-task", ""),
    ):
        path = workspace / name
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Synthetic instructions\n"
            f"{frontmatter}---\nSynthetic body",
        )
    config = GatewayConfig(config_path=str(tmp_path / "config.toml"))
    config.skills.disabled = ["disabled"]
    loader = SkillLoader(workspace_dir=workspace, snapshot_path=tmp_path / "snapshot.json")
    registry = ToolRegistry()
    registry.register(ToolSpec("skill_view", "Read synthetic instructions", {}), lambda: "")
    return RpcContext(
        conn_id="synthetic", config=config, skill_loader=loader,
        tool_registry=registry,
    )


async def test_candidates_include_manual_disabled_without_bodies_or_doctor(
    skill_context, monkeypatch, tmp_path,
):
    def forbidden(*args, **kwargs):
        raise AssertionError("The composer must not run a full Doctor/dependency scan")

    monkeypatch.setattr(rpc_skills, "SkillDoctor", forbidden)
    monkeypatch.setattr(rpc_skills, "build_dependency_summary", forbidden)
    result = await rpc_skills._handle_skills_candidates({"sessionKey": "synthetic"}, skill_context)
    rows = {row["name"]: row for row in result["candidates"]}
    assert set(rows) == {"ordinary", "manual", "disabled"}
    assert rows["manual"]["manualOnly"] is True
    assert rows["manual"]["ready"] is True
    assert rows["disabled"]["disabled"] is True
    assert rows["disabled"]["ready"] is False
    assert rows["disabled"]["reasonCode"] == "disabled"
    for row in rows.values():
        assert row["instanceId"] and row["digest"]
        assert row["generation"] == result["generation"]
        assert row["source"] == "workspace"
        assert not {"content", "file_path", "base_dir", "path", "dependency_summary"} & row.keys()
    assert str(tmp_path) not in json.dumps(result)


async def test_disabled_public_detail_is_manageable_without_instruction_body(skill_context):
    result = await rpc_skills._handle_skills_get({"name": "disabled"}, skill_context)
    assert result["name"] == "disabled"
    assert result["disabled"] is True
    assert "content" not in result
    assert "file_path" not in result
    listed = await rpc_skills._handle_skills_list({"includeLifecycle": True}, skill_context)
    row = next(item for item in listed["skills"] if item["name"] == "disabled")
    assert row["active"] is False
    assert row["lifecycle"]["selection_state"] == "disabled"


@pytest.mark.parametrize("mode", ["memory_only", "denied", "no_runtime"])
async def test_candidates_do_not_advertise_unavailable_skill_tools(skill_context, mode):
    if mode == "memory_only":
        skill_context.config.tools.profile = "memory_only"
    elif mode == "denied":
        skill_context.config.tools.deny = ["skill_*"]
    else:
        skill_context.tool_registry = None
    result = await rpc_skills._handle_skills_candidates({}, skill_context)
    row = next(item for item in result["candidates"] if item["name"] == "manual")
    assert row["ready"] is False
    assert row["reasonCode"] == "tools_unavailable"


async def test_candidates_honor_runtime_profile_override(skill_context, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_TOOL_PROFILE", "channel_default")
    result = await rpc_skills._handle_skills_candidates({}, skill_context)
    row = next(item for item in result["candidates"] if item["name"] == "manual")
    assert row["ready"] is False
    assert row["reasonCode"] == "tools_unavailable"


@pytest.mark.parametrize("available", [False, True])
async def test_candidates_require_wired_tool_capability(
    skill_context, tmp_path, monkeypatch, available,
):
    directory = tmp_path / "skills" / "image"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: image\ndescription: Synthetic image instructions\n"
        "metadata:\n  opensquilla:\n    requires_tools: [image_generate]\n"
        "---\nSynthetic body",
    )
    skill_context.tool_registry.register(
        ToolSpec("image_generate", "Generate synthetic image", {}), lambda: "",
    )
    monkeypatch.setattr(
        "opensquilla.tools.policy_runtime._detect_image_generation_capability", lambda: available,
    )
    result = await rpc_skills._handle_skills_candidates({}, skill_context)
    row = next(item for item in result["candidates"] if item["name"] == "image")
    assert row["ready"] is available
    if not available:
        assert row["reasonCode"] == "tools_unavailable"


async def test_set_enabled_changes_only_requested_name(skill_context):
    result = await rpc_skills._handle_skills_set_enabled(
        {"name": "manual", "enabled": False}, skill_context,
    )
    assert result["persisted"] is True
    assert result["refreshed"] is True
    assert set(skill_context.config.skills.disabled) == {"manual", "disabled"}
    await rpc_skills._handle_skills_set_enabled({"name": "manual", "enabled": True}, skill_context)
    assert skill_context.config.skills.disabled == ["disabled"]


async def test_allow_use_rejects_retired_or_unknown_skill(skill_context):
    with pytest.raises(KeyError, match="not found"):
        await rpc_skills._handle_skills_set_enabled(
            {"name": "code-task", "enabled": True}, skill_context,
        )
    with pytest.raises(KeyError, match="not found"):
        await rpc_skills._handle_skills_set_enabled(
            {"name": "unknown", "enabled": True}, skill_context,
        )


@pytest.mark.parametrize("name,kind,retired_fields", [
    ("old-workflow", "meta", ""),
    ("code-task", "skill", ""),
    ("old-helper", "skill", "visibility: internal\ninvocation: meta_only\n"),
    ("old-coding", "skill", "invocation: coding_only\n"),
    ("old-meta", "skill", "visibility: meta\n"),
])
async def test_lifecycle_list_does_not_reintroduce_retired_managed_workflows(
    skill_context, tmp_path, name, kind, retired_fields,
):
    managed = tmp_path / "managed"
    retired = managed / name / "SKILL.md"
    retired.parent.mkdir(parents=True)
    retired.write_text(
        f"---\nname: {name}\ndescription: Synthetic old workflow\nkind: {kind}\n"
        f"{retired_fields}---\nHistorical instructions.\n",
    )
    original = retired.read_bytes()
    repairable = managed / "needs-repair" / "SKILL.md"
    repairable.parent.mkdir(parents=True)
    repairable.write_text("Incomplete ordinary skill manifest.\n")
    skill_context.skill_loader = SkillLoader(
        managed_dir=managed,
        snapshot_path=tmp_path / "managed-cache.json",
        lockfile_path=tmp_path / "skills-lock.json",
    )
    result = await rpc_skills._handle_skills_list({"includeLifecycle": True}, skill_context)
    assert [row["name"] for row in result["skills"]] == ["needs-repair"]
    assert result["skills"][0]["instruction_usable"] is False
    assert retired.read_bytes() == original
