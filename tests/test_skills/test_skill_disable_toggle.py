"""Explicitly disabled ordinary skills remain unavailable."""

from __future__ import annotations

from opensquilla.application.app_settings import _SAFE_WRITE_PATCH_PATHS
from opensquilla.engine.steps import skill_catalog_projection
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
        # An empty disabled list reuses the normal default context.
        cfg = SkillsConfig()
        ctx = skill_catalog_projection._eligibility_ctx(cfg)
        assert ctx is skill_catalog_projection._elig_ctx

    def test_disabled_list_builds_gating_ctx(self):
        cfg = SkillsConfig(disabled=["sample-skill"])
        ctx = skill_catalog_projection._eligibility_ctx(cfg)
        assert "sample-skill" in ctx.disabled_set


class TestDeterministicGate:
    def test_disabled_skill_is_gated_out(self):
        ctx = EligibilityContext.auto(disabled_set={"sample-skill"})
        gated = skill_catalog_projection._deterministic_gate(
            [_skill("sample-skill"), _skill("git-diff")], available_tools=set(), elig_ctx=ctx
        )
        names = {s.name for s in gated}
        assert "sample-skill" not in names
        assert "git-diff" in names

    def test_enabled_when_not_disabled(self):
        ctx = EligibilityContext.auto(disabled_set=set())
        gated = skill_catalog_projection._deterministic_gate(
            [_skill("sample-skill")], available_tools=set(), elig_ctx=ctx
        )
        assert {s.name for s in gated} == {"sample-skill"}


def test_disabled_skill_fails_eligibility():
    spec = _skill("sample-skill")
    ctx = EligibilityContext.auto(disabled_set={"sample-skill"})
    assert check_eligibility(spec, ctx) is False


def test_skills_disabled_is_a_safe_write_path():
    # The control-UI toggle patches skills.disabled via config.patch.safe.
    assert "skills.disabled" in _SAFE_WRITE_PATCH_PATHS


def test_config_skills_disabled_defaults_empty():
    cfg = GatewayConfig()
    assert cfg.skills.disabled == []
