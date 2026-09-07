"""Operator skill-disable toggle: a disabled skill is gated out of the agent.

Backs the control-UI "Code-task plugin" toggle, which writes the skill name
to ``skills.disabled`` via config.patch.safe.
"""

from __future__ import annotations

from opensquilla.application.app_settings import _SAFE_WRITE_PATCH_PATHS
from opensquilla.engine.steps import skills_filter
from opensquilla.gateway.config import GatewayConfig, SkillsConfig
from opensquilla.skills.eligibility import EligibilityContext, check_eligibility
from opensquilla.skills.types import SkillSpec


def _skill(name: str) -> SkillSpec:
    return SkillSpec(
        name=name,
        description=f"{name} skill",
        layer="bundled",
        always=False,
        triggers=[],
        content="body",
    )


class TestEligibilityContextFromConfig:
    def test_empty_effective_disabled_reuses_default_ctx(self):
        # The shared default ctx is reused only when nothing is gated: no
        # disabled skills AND coding mode ON (so code-task is not gated).
        cfg = SkillsConfig(coding_mode=True)
        ctx = skills_filter._eligibility_ctx(cfg)
        assert ctx is skills_filter._elig_ctx

    def test_default_config_gates_codetask(self):
        # Default config (coding mode OFF) gates code-task, so it is NOT the
        # shared singleton.
        cfg = SkillsConfig()
        ctx = skills_filter._eligibility_ctx(cfg)
        assert ctx is not skills_filter._elig_ctx
        assert "code-task" in ctx.disabled_set

    def test_disabled_list_builds_gating_ctx(self):
        cfg = SkillsConfig(disabled=["code-task"])
        ctx = skills_filter._eligibility_ctx(cfg)
        assert "code-task" in ctx.disabled_set


class TestDeterministicGate:
    def test_disabled_skill_is_gated_out(self):
        ctx = EligibilityContext.auto(disabled_set={"code-task"})
        gated = skills_filter._deterministic_gate(
            [_skill("code-task"), _skill("git-diff")], available_tools=set(), elig_ctx=ctx
        )
        names = {s.name for s in gated}
        assert "code-task" not in names
        assert "git-diff" in names

    def test_enabled_when_not_disabled(self):
        ctx = EligibilityContext.auto(disabled_set=set())
        gated = skills_filter._deterministic_gate(
            [_skill("code-task")], available_tools=set(), elig_ctx=ctx
        )
        assert {s.name for s in gated} == {"code-task"}


def test_disabled_skill_fails_eligibility():
    spec = _skill("code-task")
    ctx = EligibilityContext.auto(disabled_set={"code-task"})
    assert check_eligibility(spec, ctx) is False


def test_skills_disabled_is_a_safe_write_path():
    # The control-UI toggle patches skills.disabled via config.patch.safe.
    assert "skills.disabled" in _SAFE_WRITE_PATCH_PATHS


def test_config_skills_disabled_defaults_empty():
    cfg = GatewayConfig()
    assert cfg.skills.disabled == []
