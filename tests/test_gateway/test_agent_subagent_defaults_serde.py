"""AgentSubagentDefaults round-trips and exposes documented semantics."""

from __future__ import annotations

import pytest

from opensquilla.engine.subagent_iteration_budget import (
    SubagentIterationBudget,
    resolve_subagent_iteration_budget,
)
from opensquilla.gateway.config import (
    AgentDefaults,
    AgentEntryConfig,
    AgentSubagentDefaults,
    GatewayConfig,
    SubagentsGatewayConfig,
)


@pytest.mark.parametrize(
    ("route_tier", "effort_tier", "soft_limit", "hard_limit"),
    [
        ("c0", "small", 6, 10),
        ("c1", "small", 6, 10),
        ("c2", "medium", 12, 20),
        ("c3", "large", 24, 36),
        ("image", "large", 24, 36),
        (None, "medium", 12, 20),
        ("unknown", "medium", 12, 20),
    ],
)
def test_router_tier_selects_iteration_budget(
    route_tier: str | None,
    effort_tier: str,
    soft_limit: int,
    hard_limit: int,
) -> None:
    budget = resolve_subagent_iteration_budget(
        route_tier=route_tier,
        subagents_config=SubagentsGatewayConfig(),
    )

    assert budget == SubagentIterationBudget(
        effort_tier=effort_tier,
        soft_limit=soft_limit,
        hard_limit=hard_limit,
        route_tier=route_tier,
    )


def test_iteration_budget_accepts_operator_tier_overrides() -> None:
    config = SubagentsGatewayConfig.model_validate(
        {
            "iteration_budget": {
                "fallback_tier": "large",
                "small": {"soft": 4, "hard": 8},
                "medium": {"soft": 9, "hard": 15},
                "large": {"soft": 18, "hard": 30},
            }
        }
    )

    budget = resolve_subagent_iteration_budget(
        route_tier=None,
        subagents_config=config,
    )

    assert budget.effort_tier == "large"
    assert budget.soft_limit == 18
    assert budget.hard_limit == 30


def test_iteration_budget_rejects_soft_limit_at_or_above_hard_limit() -> None:
    with pytest.raises(ValueError, match="soft.*hard"):
        SubagentsGatewayConfig.model_validate(
            {"iteration_budget": {"medium": {"soft": 20, "hard": 20}}}
        )


def test_subagent_defaults_optional_fields_default_to_none() -> None:
    d = AgentSubagentDefaults()
    assert d.max_children_per_session is None
    assert d.allow_agents is None
    assert d.cascade_on_parent_kill is True


def test_legacy_subagent_model_override_is_ignored() -> None:
    defaults = AgentSubagentDefaults.model_validate({"model": "haiku"})

    assert "model" not in defaults.model_dump()
    assert not hasattr(defaults, "model")


def test_allow_agents_distinguishes_unset_self_only_and_wildcard() -> None:
    unset = AgentSubagentDefaults()
    self_only = AgentSubagentDefaults(allow_agents=[])
    wildcard = AgentSubagentDefaults(allow_agents=["*"])

    # Round-trip via model_dump preserves the three distinct shapes
    assert unset.model_dump()["allow_agents"] is None
    assert self_only.model_dump()["allow_agents"] == []
    assert wildcard.model_dump()["allow_agents"] == ["*"]


def test_agent_entry_carries_optional_subagents_block() -> None:
    bare = AgentEntryConfig(id="research")
    assert bare.subagents is None

    configured = AgentEntryConfig(
        id="research",
        subagents=AgentSubagentDefaults(max_children_per_session=5),
    )
    assert configured.subagents is not None
    assert configured.subagents.max_children_per_session == 5


def test_gateway_config_exposes_agents_defaults_and_subagents_subtree() -> None:
    cfg = GatewayConfig()
    # Defaults exist and are additive — current behavior preserved.
    assert isinstance(cfg.agents_defaults, AgentDefaults)
    assert cfg.agents_defaults.subagents is None  # unset → no global override
    assert isinstance(cfg.subagents, SubagentsGatewayConfig)
    assert cfg.subagents.enforce_disabled_agents is False
    assert cfg.subagents.subagent_reserved_slots == 2
    assert cfg.subagents.archive_after_minutes == 60
    assert cfg.subagents.delegation_policy == "aggressive"
    assert cfg.subagents.prompt_compact is True
    assert cfg.subagents.max_spawn_depth == 3
    assert cfg.subagents.max_task_attempts == 3
    assert cfg.subagents.failure_fallback_threshold == 3
    assert cfg.subagents.max_iterations == 16
    assert cfg.subagents.iteration_budget.mode == "router"
    assert cfg.subagents.iteration_budget.small.soft == 6
    assert cfg.subagents.iteration_budget.small.hard == 10
    assert cfg.subagents.iteration_budget.medium.soft == 12
    assert cfg.subagents.iteration_budget.medium.hard == 20
    assert cfg.subagents.iteration_budget.large.soft == 24
    assert cfg.subagents.iteration_budget.large.hard == 36


def test_gateway_subagent_depth_accepts_nested_environment_override(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_SUBAGENTS__MAX_SPAWN_DEPTH", "4")

    cfg = GatewayConfig()

    assert cfg.subagents.max_spawn_depth == 4


def test_gateway_subagent_fallback_accepts_nested_environment_overrides(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_SUBAGENTS__FAILURE_FALLBACK_THRESHOLD", "4")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_SUBAGENTS__MAX_ITERATIONS", "20")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_SUBAGENTS__MAX_TASK_ATTEMPTS", "5")

    cfg = GatewayConfig()

    assert cfg.subagents.failure_fallback_threshold == 4
    assert cfg.subagents.max_iterations == 20
    assert cfg.subagents.max_task_attempts == 5


def test_gateway_config_accepts_explicit_subagent_defaults() -> None:
    cfg = GatewayConfig(
        agents_defaults=AgentDefaults(
            subagents=AgentSubagentDefaults(max_children_per_session=5)
        ),
        subagents=SubagentsGatewayConfig(
            delegation_policy="off",
            enforce_disabled_agents=True,
            subagent_reserved_slots=4,
            archive_after_minutes=0,
        ),
    )
    assert cfg.agents_defaults.subagents is not None
    assert cfg.agents_defaults.subagents.max_children_per_session == 5
    assert cfg.subagents.enforce_disabled_agents is True
    assert cfg.subagents.delegation_policy == "off"
    assert cfg.subagents.subagent_reserved_slots == 4
    assert cfg.subagents.archive_after_minutes == 0
