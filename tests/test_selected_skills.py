from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.steps.selected_skills import SelectedSkillError, load_selected_skills
from opensquilla.gateway.config import GatewayConfig
from opensquilla.skills import eligibility
from opensquilla.skills.catalog_policy import is_public_ordinary, is_user_invocable_ordinary
from opensquilla.skills.tree import compute_tree_sha256
from opensquilla.skills.types import SkillLayer, SkillPlatformMeta, SkillRequires, SkillSpec
from opensquilla.tools.builtin import skill_tools
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import ToolContext, current_tool_context


@pytest.fixture(autouse=True)
def isolate_skill_globals():
    saved_loader = skill_tools._loader
    saved_config = eligibility._live_skills_cfg_getter
    eligibility.set_live_skills_config_getter(None)
    yield
    skill_tools._loader = saved_loader
    eligibility.set_live_skills_config_getter(saved_config)


def skill(tmp_path: Path, name: str = "report") -> SkillSpec:
    directory = tmp_path / name
    directory.mkdir()
    (directory / "SKILL.md").write_text("Synthetic instructions", encoding="utf-8")
    (directory / "references").mkdir()
    (directory / "references" / "guide.txt").write_text(
        "Synthetic supporting resource", encoding="utf-8",
    )
    return SkillSpec(
        name=name, description="Synthetic reporting", layer=SkillLayer.PERSONAL,
        always=False, triggers=[], content="Read {baseDir}/references/guide.txt before reporting.",
        base_dir=str(directory), instance_id=f"personal:{name}",
        tree_digest=compute_tree_sha256(directory),
    )


def turn(specs: list[SkillSpec]):
    refs = [
        {"name": spec.name, "instanceId": spec.instance_id, "digest": spec.tree_digest}
        for spec in specs
    ]
    snapshot = SimpleNamespace(skills=tuple(specs), generation=7)
    ctx = TurnContext(
        message="Synthetic request", session_key="agent:main:test:selected",
        config=GatewayConfig(), provider=None, model="test-model",
        tool_defs=[SimpleNamespace(name="skill_view")], system_prompt=("base", "dynamic"),
        metadata={"selected_skills": refs}, skill_catalog=snapshot,
    )
    receipts = []

    async def emit(receipt):
        receipts.append(receipt)

    tools = ToolContext(skill_catalog=snapshot, skill_load_emitter=emit)
    return ctx, tools, receipts


async def test_explicit_manual_only_load_preserves_auto_policy_and_support_files(tmp_path):
    spec = skill(tmp_path)
    spec.disable_model_invocation = True
    ctx, tools, receipts = turn([spec])
    assert not is_public_ordinary(spec)
    assert is_user_invocable_ordinary(spec)
    skill_tools.create_skill_tools(SimpleNamespace(get_by_name=lambda _: spec))
    view = get_default_registry().get("skill_view").handler
    token = current_tool_context.set(tools)
    try:
        assert "Skill not found" in await view(spec.name)
        receipts.clear()
        await load_selected_skills(ctx, tools)
        assert ctx.system_prompt[0] == "base"
        assert spec.base_dir in ctx.system_prompt[1]
        assert [row["status"] for row in receipts] == ["loading", "loaded"]
        assert receipts[-1]["source"] == "user"
        assert "Synthetic supporting resource" == await view(spec.name, "guide.txt")
        # Explicit selection never authorizes bytes from a later resource tree.
        (Path(spec.base_dir) / "references" / "guide.txt").write_text("Changed", encoding="utf-8")
        assert "resources changed" in await view(spec.name, "guide.txt")
    finally:
        current_tool_context.reset(token)


@pytest.mark.parametrize("mutation", ["instance", "digest", "uninstalled", "disk"])
async def test_changed_selection_fails_closed(tmp_path, mutation):
    spec = skill(tmp_path)
    ctx, tools, receipts = turn([spec])
    expected_identity = dict(ctx.metadata["selected_skills"][0])
    if mutation == "instance":
        spec.instance_id = "replacement-instance"
    elif mutation == "digest":
        spec.tree_digest = "changed-digest"
    elif mutation == "uninstalled":
        ctx.skill_catalog.skills = ()
    else:
        (Path(spec.base_dir) / "references" / "guide.txt").unlink()
    with pytest.raises(SelectedSkillError):
        await load_selected_skills(ctx, tools)
    assert ctx.system_prompt == ("base", "dynamic")
    assert not tools.verified_skill_ids
    assert receipts[-1]["status"] == "failed"
    assert receipts[-1]["instanceId"] == expected_identity["instanceId"]
    assert receipts[-1]["digest"] == expected_identity["digest"]
    assert not any(row["status"] == "loaded" for row in receipts)


async def test_unrelated_generation_change_and_duplicate_selection(tmp_path):
    ctx, tools, receipts = turn([skill(tmp_path)])
    ctx.skill_catalog.generation += 1
    ctx.metadata["selected_skills"] *= 2
    await load_selected_skills(ctx, tools)
    assert [row["status"] for row in receipts] == ["loading", "loaded"]


@pytest.mark.parametrize("gate", ["disabled", "manual_hidden", "internal", "guest", "tool"])
async def test_selection_does_not_bypass_authorization(tmp_path, gate):
    spec = skill(tmp_path)
    ctx, tools, receipts = turn([spec])
    if gate == "disabled":
        ctx.config.skills.disabled = [spec.name]
    elif gate == "manual_hidden":
        spec.user_invocable = False
    elif gate == "internal":
        spec.visibility = "internal"
    elif gate == "guest":
        tools.guest_safe = True
    else:
        tools.authorized_tool_names = frozenset({"read_file"})
    with pytest.raises(SelectedSkillError):
        await load_selected_skills(ctx, tools)
    assert receipts[-1]["status"] == "failed"
    assert not tools.verified_skill_ids


async def test_missing_dependency_and_context_budget_are_explicit_failures(tmp_path):
    spec = skill(tmp_path)
    spec.metadata = SkillPlatformMeta(requires=SkillRequires(env=["SYNTHETIC_MISSING_SKILL_VAR"]))
    ctx, tools, receipts = turn([spec])
    with pytest.raises(SelectedSkillError, match="unavailable"):
        await load_selected_skills(ctx, tools)
    spec.metadata = None
    ctx.config.llm.context_window_tokens = 1
    with pytest.raises(SelectedSkillError, match="context budget"):
        await load_selected_skills(ctx, tools)
    assert not any(row["status"] == "loaded" for row in receipts)


async def test_batch_failure_never_partially_injects_instructions(tmp_path):
    one, two = skill(tmp_path, "one"), skill(tmp_path, "two")
    two.content = ""
    ctx, tools, receipts = turn([one, two])
    with pytest.raises(SelectedSkillError):
        await load_selected_skills(ctx, tools)
    assert ctx.system_prompt == ("base", "dynamic")
    assert not tools.verified_skill_ids
    assert {row["name"] for row in receipts if row["status"] == "failed"} == {"one", "two"}


async def test_cancellation_closes_all_started_loads_without_injecting(tmp_path, monkeypatch):
    one, two = skill(tmp_path, "one"), skill(tmp_path, "two")
    ctx, tools, receipts = turn([one, two])
    digest_started = asyncio.Event()
    original_to_thread = asyncio.to_thread

    async def blocked_digest(function, *args, **kwargs):
        if function is compute_tree_sha256 and args == (Path(two.base_dir),):
            digest_started.set()
            await asyncio.Future()
        return await original_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", blocked_digest)
    task = asyncio.create_task(load_selected_skills(ctx, tools))
    await asyncio.wait_for(digest_started.wait(), timeout=5)
    task.cancel("synthetic stop")
    with pytest.raises(asyncio.CancelledError, match="synthetic stop"):
        await task
    assert ctx.system_prompt == ("base", "dynamic")
    assert not tools.verified_skill_ids
    assert not any(row["status"] == "loaded" for row in receipts)
    for spec in (one, two):
        records = [row for row in receipts if row["instanceId"] == spec.instance_id]
        assert [row["status"] for row in records] == ["loading", "failed"]
        assert records[-1]["digest"] == spec.tree_digest
        assert "cancelled" in records[-1]["error"]


async def test_live_disable_during_later_digest_prevents_entire_batch(tmp_path, monkeypatch):
    one, two = skill(tmp_path, "one"), skill(tmp_path, "two")
    ctx, tools, receipts = turn([one, two])
    live_config = SimpleNamespace(disabled=[])
    eligibility.set_live_skills_config_getter(lambda: live_config)
    digest_started, finish_digest = asyncio.Event(), asyncio.Event()
    original_to_thread = asyncio.to_thread

    async def blocked_digest(function, *args, **kwargs):
        if function is compute_tree_sha256 and args == (Path(two.base_dir),):
            digest_started.set()
            await finish_digest.wait()
        return await original_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", blocked_digest)
    task = asyncio.create_task(load_selected_skills(ctx, tools))
    await asyncio.wait_for(digest_started.wait(), timeout=5)
    live_config.disabled = [one.name]
    finish_digest.set()
    with pytest.raises(SelectedSkillError, match="Skill 'one' is disabled"):
        await task
    assert ctx.system_prompt == ("base", "dynamic")
    assert not tools.verified_skill_ids
    assert not any(row["status"] == "loaded" for row in receipts)
    assert {row["name"] for row in receipts if row["status"] == "failed"} == {one.name, two.name}


async def test_ordinary_automatic_read_receipt_is_based_on_body(tmp_path):
    spec = skill(tmp_path)
    _, tools, receipts = turn([spec])
    skill_tools.create_skill_tools(SimpleNamespace(get_by_name=lambda _: spec))
    view = get_default_registry().get("skill_view").handler
    token = current_tool_context.set(tools)
    try:
        assert "Read" in await view(spec.name)
        assert receipts[-1]["status"] == "loaded"
        assert receipts[-1]["source"] == "auto"
        spec.content = ""
        await view(spec.name)
        assert receipts[-1]["status"] == "failed"
    finally:
        current_tool_context.reset(token)


async def test_no_selection_is_no_op():
    ctx, tools, receipts = turn([])
    assert await load_selected_skills(ctx, tools) is ctx
    assert not receipts


async def test_unexpected_probe_failure_closes_the_exact_selection_receipt(tmp_path, monkeypatch):
    spec = skill(tmp_path)
    ctx, tools, receipts = turn([spec])

    def failed_probe(*_args):
        raise RuntimeError("synthetic private diagnostic")

    monkeypatch.setattr(
        "opensquilla.engine.steps.selected_skills.diagnose_eligibility", failed_probe,
    )
    with pytest.raises(SelectedSkillError, match="could not be loaded"):
        await load_selected_skills(ctx, tools)
    assert [row["status"] for row in receipts] == ["loading", "failed"]
    assert all(row["instanceId"] == spec.instance_id for row in receipts)
    assert "private diagnostic" not in receipts[-1]["error"]


def test_invocation_summary_prefers_real_receipts_over_attempted_tool_calls():
    from opensquilla.engine.runtime import collect_invoked_skills

    assert collect_invoked_skills([
        {"type": "tool_use", "name": "skill_view", "input": {"name": "missing"}},
        {"type": "skill_load", "name": "missing", "status": "failed"},
        {"type": "skill_load", "name": "selected", "status": "loading"},
        {"type": "skill_load", "name": "selected", "status": "loaded"},
    ]) == ["selected"]


@pytest.mark.parametrize("denied", [False, True])
def test_manual_only_catalog_surfaces_reader_without_overriding_denial(tmp_path, denied):
    from opensquilla.engine.runtime import TurnRunner
    from opensquilla.tools.registry import ToolRegistry, tool
    from opensquilla.tools.types import CallerKind, PlanAccess

    spec = skill(tmp_path)
    spec.disable_model_invocation = True
    ctx, tools, _ = turn([spec])
    tools.caller_kind = CallerKind.WEB
    tools.is_owner = True
    tools.selected_skills = tuple(ctx.metadata["selected_skills"])
    tools.denied_tools = {"skill_view"} if denied else set()
    registry = ToolRegistry()

    @tool(
        name="skill_view", description="Synthetic reader", default_access="deny",
        plan_access=PlanAccess.READ_ONLY, registry=registry,
    )
    async def reader():
        return "Synthetic instructions"

    runner = TurnRunner(provider_selector=None, tool_registry=registry, config=ctx.config)
    runner._build_tools(tools, skill_catalog=ctx.skill_catalog)
    assert ("skill_view" in tools.authorized_tool_names) is not denied
