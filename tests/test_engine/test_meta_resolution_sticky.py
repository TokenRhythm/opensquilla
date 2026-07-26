"""meta_resolution sticky-continuation tests.

Covers the in-memory session-keyed cache that keeps a meta-skill match
alive across follow-up turns when the LLM failed to actually emit
``meta_invoke`` on the originating turn (e.g. length-capped on
reasoning).
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from opensquilla.skills.meta.types import MetaPlan, MetaStep

mr = importlib.import_module("opensquilla.engine.steps.meta_resolution")
meta_resolution = mr.meta_resolution


def _meta_spec(*, name: str, triggers: tuple[str, ...]):
    """Build a minimal meta skill spec that ``parse_meta_plan`` accepts."""
    plan = MetaPlan(
        name=name,
        triggers=triggers,
        priority=50,
        steps=(MetaStep(id="s1", skill="paper-section-author", kind="agent"),),
    )
    spec = SimpleNamespace(
        name=name,
        kind="meta",
        triggers=list(triggers),
        composition_raw={
            "meta_priority": plan.priority,
            "steps": [{"id": "s1", "skill": "paper-section-author", "kind": "agent"}],
        },
        metadata={"opensquilla": {"meta_priority": plan.priority}},
        body="",
    )
    return spec


def _ctx(
    *,
    message: str,
    session_id: str,
    skills: list,
    metadata: dict | None = None,
    model: str = "",
    tiers: dict | None = None,
):
    loader = MagicMock()
    loader.load_all.return_value = skills
    meta = {"skill_loader": loader}
    if metadata:
        meta.update(metadata)
    return SimpleNamespace(
        message=message,
        session_key=session_id,
        metadata=meta,
        model=model,
        system_prompt="",
        config=SimpleNamespace(
            squilla_router=SimpleNamespace(tiers=tiers or {}),
            meta_skill=SimpleNamespace(enabled=True, auto_trigger=True),
        ),
        surface_kind="cli",
    )


@pytest.fixture(autouse=True)
def _clear_sticky_cache():
    """Ensure each test sees a fresh sticky cache."""
    with mr._sticky_lock:
        mr._meta_sticky_cache.clear()
    yield
    with mr._sticky_lock:
        mr._meta_sticky_cache.clear()


@pytest.mark.asyncio
async def test_fresh_match_arms_sticky_cache():
    skills = [_meta_spec(name="meta-paper-write", triggers=("帮我写篇论文",))]
    ctx = _ctx(message="帮我写篇论文", session_id="S-A", skills=skills)

    out = await meta_resolution(ctx)
    assert "meta_match" in out.metadata
    assert out.metadata.get("meta_match_sticky") is not True

    cached = mr._sticky_get("S-A")
    assert cached is not None
    assert cached["skill"] == "meta-paper-write"
    assert cached["trigger"] == "帮我写篇论文"
    assert cached["uses"] == mr._STICKY_MAX_USES


@pytest.mark.asyncio
async def test_pinned_catalog_avoids_reloading_during_meta_resolution():
    skills = [_meta_spec(name="meta-paper-write", triggers=("帮我写篇论文",))]
    ctx = _ctx(message="帮我写篇论文", session_id="S-PINNED", skills=[])
    ctx.skill_catalog = SimpleNamespace(skills=tuple(skills), generation=7)
    ctx.metadata["skill_loader"].load_all.side_effect = AssertionError(
        "loader must not be read after the turn snapshot is pinned"
    )

    out = await meta_resolution(ctx)

    assert out.metadata["meta_match"].plan.name == "meta-paper-write"
    ctx.metadata["skill_loader"].load_all.assert_not_called()


@pytest.mark.asyncio
async def test_meta_match_upgrades_low_router_tier_to_c2_entry_model():
    skills = [_meta_spec(name="meta-short-drama", triggers=("生成一个短剧",))]
    tiers = {
        "c0": {"model": "deepseek-v4-flash"},
        "c1": {"model": "deepseek-v4-flash"},
        "c2": {"model": "deepseek-v4-pro"},
        "c3": {"model": "highest-tier-model"},
    }
    ctx = _ctx(
        message="生成一个短剧，啥都行",
        session_id="S-META-UPGRADE",
        skills=skills,
        model="deepseek-v4-flash",
        tiers=tiers,
        metadata={
            "routed_tier": "c0",
            "routed_model": "deepseek-v4-flash",
            "routing_source": "v4_phase3",
            "routing_applied": True,
        },
    )

    out = await meta_resolution(ctx)

    assert out.model == "deepseek-v4-pro"
    assert out.metadata["routed_tier"] == "c2"
    assert out.metadata["routed_model"] == "deepseek-v4-pro"
    assert out.metadata["meta_required_source"] == "meta-skill-entry"
    assert out.metadata["meta_resolution_model_upgrade"] == {
        "from_model": "deepseek-v4-flash",
        "to_model": "deepseek-v4-pro",
        "to_tier": "c2",
        "source": "meta-skill-entry",
    }


@pytest.mark.asyncio
async def test_meta_upgrade_realigns_routed_provider_to_upgraded_tier():
    skills = [_meta_spec(name="meta-short-drama", triggers=("生成一个短剧",))]
    tiers = {
        "c0": {"model": "small-model-1", "provider": "provider-a"},
        "c1": {"model": "mid-model-1", "provider": "provider-a"},
        "c2": {"model": "big-model-1", "provider": "provider-b"},
        "c3": {"model": "max-model-1", "provider": "provider-b"},
    }
    ctx = _ctx(
        message="生成一个短剧，啥都行",
        session_id="S-META-PROVIDER-REALIGN",
        skills=skills,
        model="small-model-1",
        tiers=tiers,
        metadata={
            "routed_tier": "c0",
            "routed_model": "small-model-1",
            "routed_provider": "provider-a",
            "routing_source": "v4_phase3",
            "routing_applied": True,
        },
    )

    out = await meta_resolution(ctx)

    assert out.metadata["routed_tier"] == "c2"
    assert out.metadata["routed_model"] == "big-model-1"
    assert out.metadata["routed_provider"] == "provider-b"


@pytest.mark.asyncio
async def test_meta_upgrade_clears_routed_provider_when_upgraded_tier_declares_none():
    skills = [_meta_spec(name="meta-short-drama", triggers=("生成一个短剧",))]
    tiers = {
        "c0": {"model": "small-model-1", "provider": "provider-a"},
        "c1": {"model": "mid-model-1", "provider": "provider-a"},
        "c2": {"model": "big-model-1"},
        "c3": {"model": "max-model-1"},
    }
    ctx = _ctx(
        message="生成一个短剧，啥都行",
        session_id="S-META-PROVIDER-CLEAR",
        skills=skills,
        model="small-model-1",
        tiers=tiers,
        metadata={
            "routed_tier": "c0",
            "routed_model": "small-model-1",
            "routed_provider": "provider-a",
            "routing_source": "v4_phase3",
            "routing_applied": True,
        },
    )

    out = await meta_resolution(ctx)

    assert out.metadata["routed_tier"] == "c2"
    assert "routed_provider" not in out.metadata


@pytest.mark.asyncio
async def test_meta_match_without_router_tier_does_not_force_entry_model():
    skills = [_meta_spec(name="meta-short-drama", triggers=("生成一个短剧",))]
    tiers = {"c2": {"model": "deepseek-v4-pro"}}
    ctx = _ctx(
        message="生成一个短剧，啥都行",
        session_id="S-META-NO-ROUTER",
        skills=skills,
        model="operator-selected-model",
        tiers=tiers,
    )

    out = await meta_resolution(ctx)

    assert out.model == "operator-selected-model"
    assert "meta_resolution_model_upgrade" not in out.metadata


@pytest.mark.asyncio
async def test_followup_without_trigger_replays_from_sticky():
    skills = [_meta_spec(name="meta-paper-write", triggers=("帮我写篇论文",))]
    # T1: arm cache.
    await meta_resolution(_ctx(
        message="帮我写篇论文", session_id="S-B", skills=skills,
    ))

    # T2: same session, follow-up text does not contain the trigger.
    out = await meta_resolution(_ctx(
        message="我想写一个关于 RAG 的论文，20 页左右",
        session_id="S-B",
        skills=skills,
    ))
    assert "meta_match" in out.metadata
    assert out.metadata.get("meta_match_sticky") is True
    assert out.metadata.get("meta_match").plan.name == "meta-paper-write"
    # uses decremented by 1
    assert mr._sticky_get("S-B")["uses"] == mr._STICKY_MAX_USES - 1


@pytest.mark.asyncio
async def test_sticky_replay_clamps_thinking_to_low():
    skills = [_meta_spec(name="meta-paper-write", triggers=("帮我写篇论文",))]
    ctx_fresh = _ctx(message="帮我写篇论文", session_id="S-C", skills=skills)
    out_fresh = await meta_resolution(ctx_fresh)
    assert out_fresh.metadata.get("thinking_level") == "low"
    assert out_fresh.metadata.get("thinking_source") == "meta_resolution"

    ctx_followup = _ctx(message="补充细节", session_id="S-C", skills=skills)
    out_followup = await meta_resolution(ctx_followup)
    assert out_followup.metadata.get("meta_match_sticky") is True
    assert out_followup.metadata.get("thinking_level") == "low"


@pytest.mark.asyncio
async def test_sticky_uses_decrement_to_zero_then_drops():
    skills = [_meta_spec(name="meta-paper-write", triggers=("帮我写篇论文",))]
    # Fresh match arms cache with MAX_USES.
    await meta_resolution(_ctx(
        message="帮我写篇论文", session_id="S-D", skills=skills,
    ))

    for _ in range(mr._STICKY_MAX_USES):
        out = await meta_resolution(_ctx(
            message="follow-up", session_id="S-D", skills=skills,
        ))
        assert out.metadata.get("meta_match_sticky") is True

    # After exhausting uses, the next no-trigger turn no longer matches.
    out_after = await meta_resolution(_ctx(
        message="another follow-up", session_id="S-D", skills=skills,
    ))
    assert "meta_match" not in out_after.metadata
    assert mr._sticky_get("S-D") is None


@pytest.mark.asyncio
async def test_sticky_cancel_keyword_drops_entry():
    skills = [_meta_spec(name="meta-paper-write", triggers=("帮我写篇论文",))]
    await meta_resolution(_ctx(
        message="帮我写篇论文", session_id="S-E", skills=skills,
    ))
    assert mr._sticky_get("S-E") is not None

    # User cancels — sticky cache is dropped, no replay on this turn.
    out = await meta_resolution(_ctx(
        message="算了，取消吧", session_id="S-E", skills=skills,
    ))
    assert "meta_match" not in out.metadata
    assert mr._sticky_get("S-E") is None


@pytest.mark.asyncio
async def test_pasted_context_drops_sticky_without_replay():
    skills = [_meta_spec(name="meta-paper-write", triggers=("帮我写篇论文",))]
    await meta_resolution(_ctx(
        message="帮我写篇论文", session_id="S-PASTE", skills=skills,
    ))
    assert mr._sticky_get("S-PASTE") is not None

    dump_body = "\n".join(
        [
            "WebChat dump",
            "assistant: old skill list",
            "meta-paper-write 帮我写篇论文",
        ]
        + [f"history line {i}" for i in range(12)]
    )
    out = await meta_resolution(_ctx(
        message=f"请分析下面历史页面是否误触发。\n{dump_body}",
        session_id="S-PASTE",
        skills=skills,
    ))

    assert "meta_match" not in out.metadata
    assert mr._sticky_get("S-PASTE") is None


@pytest.mark.asyncio
async def test_fresh_match_on_followup_refreshes_uses():
    """If the user re-utters the trigger, uses are re-armed."""
    skills = [_meta_spec(name="meta-paper-write", triggers=("帮我写篇论文",))]
    await meta_resolution(_ctx(
        message="帮我写篇论文", session_id="S-F", skills=skills,
    ))
    # Burn one use.
    await meta_resolution(_ctx(
        message="补充", session_id="S-F", skills=skills,
    ))
    assert mr._sticky_get("S-F")["uses"] == mr._STICKY_MAX_USES - 1

    # Re-trigger restores full budget.
    await meta_resolution(_ctx(
        message="帮我写篇论文", session_id="S-F", skills=skills,
    ))
    assert mr._sticky_get("S-F")["uses"] == mr._STICKY_MAX_USES


@pytest.mark.asyncio
async def test_sessions_are_isolated():
    skills = [_meta_spec(name="meta-paper-write", triggers=("帮我写篇论文",))]
    await meta_resolution(_ctx(
        message="帮我写篇论文", session_id="S-G1", skills=skills,
    ))
    # Different session — no sticky for it.
    out = await meta_resolution(_ctx(
        message="hello", session_id="S-G2", skills=skills,
    ))
    assert "meta_match" not in out.metadata
    assert mr._sticky_get("S-G1") is not None
    assert mr._sticky_get("S-G2") is None


@pytest.mark.asyncio
async def test_no_sticky_when_skill_was_removed():
    """If the meta-skill is no longer loaded, sticky entry is dropped."""
    skills = [_meta_spec(name="meta-paper-write", triggers=("帮我写篇论文",))]
    await meta_resolution(_ctx(
        message="帮我写篇论文", session_id="S-H", skills=skills,
    ))
    # Next turn — loader returns nothing.
    out = await meta_resolution(_ctx(
        message="follow-up", session_id="S-H", skills=[],
    ))
    assert "meta_match" not in out.metadata
    assert mr._sticky_get("S-H") is None


@pytest.mark.asyncio
async def test_skill_marketplace_intent_skips_semantic_meta_fallback(monkeypatch):
    skills = [_meta_spec(name="meta-skill-creator", triggers=("create a meta-skill",))]
    semantic_called = False

    def fake_semantic_candidate(ctx, candidates):
        nonlocal semantic_called
        semantic_called = True
        priority, name, plan, _spec = candidates[0]
        return (priority, name, plan, "semantic")

    monkeypatch.setattr(mr, "_semantic_meta_candidate", fake_semantic_candidate)

    out = await meta_resolution(_ctx(
        message="I want to install skills",
        session_id="S-I",
        skills=skills,
    ))

    assert semantic_called is False
    assert "meta_match" not in out.metadata


@pytest.mark.asyncio
async def test_skill_marketplace_intent_clears_sticky_replay():
    skills = [_meta_spec(name="meta-paper-write", triggers=("帮我写篇论文",))]
    await meta_resolution(_ctx(
        message="帮我写篇论文", session_id="S-J", skills=skills,
    ))
    assert mr._sticky_get("S-J") is not None

    out = await meta_resolution(_ctx(
        message="帮我搜索并安装 plot skill",
        session_id="S-J",
        skills=skills,
    ))

    assert "meta_match" not in out.metadata
    assert mr._sticky_get("S-J") is None


@pytest.mark.asyncio
async def test_meta_skill_discussion_clears_sticky_replay():
    skills = [_meta_spec(name="AwesomeWebpageMetaSkill", triggers=("生成图文音视频网页",))]
    await meta_resolution(_ctx(
        message="帮我生成图文音视频网页：主题是海洋塑料污染",
        session_id="S-META-DISCUSS",
        skills=skills,
    ))
    assert mr._sticky_get("S-META-DISCUSS") is not None

    out = await meta_resolution(_ctx(
        message="你最后生成的时候调用了meta skills吗，怎么生成的好差",
        session_id="S-META-DISCUSS",
        skills=skills,
    ))

    assert "meta_match" not in out.metadata
    assert mr._sticky_get("S-META-DISCUSS") is None


@pytest.mark.asyncio
async def test_meta_skill_discussion_skips_semantic_fallback(monkeypatch):
    skills = [_meta_spec(name="AwesomeWebpageMetaSkill", triggers=("生成图文音视频网页",))]
    semantic_called = False

    def fake_semantic_candidate(ctx, candidates):
        nonlocal semantic_called
        semantic_called = True
        priority, name, plan, _spec = candidates[0]
        return (priority, name, plan, "semantic")

    monkeypatch.setattr(mr, "_semantic_meta_candidate", fake_semantic_candidate)

    out = await meta_resolution(_ctx(
        message="你最后生成的时候调用了meta skills吗，怎么生成的好差",
        session_id="S-META-SEMANTIC",
        skills=skills,
    ))

    assert semantic_called is False
    assert "meta_match" not in out.metadata


@pytest.mark.asyncio
async def test_skill_marketplace_guard_does_not_block_explicit_meta_trigger():
    skills = [_meta_spec(name="meta-skill-creator", triggers=("create a meta-skill",))]

    out = await meta_resolution(_ctx(
        message="create a meta-skill that orchestrates skill search",
        session_id="S-K",
        skills=skills,
    ))

    assert out.metadata.get("meta_match").plan.name == "meta-skill-creator"
