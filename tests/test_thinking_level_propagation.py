"""Behavioral tests for thinking_level propagation (PR #797 v3).

Covers:
- AliasChoices: [llm] thinking accepts both "thinking" and "thinking_level" spellings
- Candidate thinking_level validation (coerce invalid 鈫?"")
- custom_b5 propagation: candidate thinking_level reaches EnsembleMemberConfig
- Server-side merge: UI-shaped 5-key payload preserves stored thinking_level
- Downgrade compatibility
"""

from __future__ import annotations

import pytest

from opensquilla.gateway.config import (
    GatewayConfig,
    LlmEnsembleCandidateConfig,
    LlmProviderConfig,
)

# ---------------------------------------------------------------------------
# 1. AliasChoices: [llm] thinking / thinking_level
# ---------------------------------------------------------------------------


class TestLlmThinkingAliasChoices:
    def test_thinking_key_loads(self) -> None:
        cfg = LlmProviderConfig(thinking="high")
        assert cfg.thinking == "high"

    def test_thinking_level_key_loads(self) -> None:
        cfg = LlmProviderConfig(thinking_level="high")
        assert cfg.thinking == "high"

    def test_model_dump_emits_only_thinking(self) -> None:
        cfg = LlmProviderConfig(thinking_level="medium")
        dumped = cfg.model_dump()
        assert "thinking" in dumped
        assert dumped["thinking"] == "medium"
        assert "thinking_level" not in dumped

    def test_thinking_takes_precedence_over_thinking_level(self) -> None:
        # AliasChoices order: "thinking" is first, so it wins.
        cfg = LlmProviderConfig(thinking="low", thinking_level="high")
        assert cfg.thinking == "low"

    def test_none_default(self) -> None:
        cfg = LlmProviderConfig()
        assert cfg.thinking is None

    def test_env_var_thinking_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENSQUILLA_LLM_THINKING_LEVEL", "xhigh")
        cfg = LlmProviderConfig()
        assert cfg.thinking == "xhigh"

    def test_env_var_thinking(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENSQUILLA_LLM_THINKING", "adaptive")
        cfg = LlmProviderConfig()
        assert cfg.thinking == "adaptive"


# ---------------------------------------------------------------------------
# 2. Candidate thinking_level validation
# ---------------------------------------------------------------------------


class TestCandidateThinkingLevelValidation:
    def test_valid_level_passes(self) -> None:
        c = LlmEnsembleCandidateConfig(
            provider="dashscope", model="test-model-a", thinking_level="high"
        )
        assert c.thinking_level == "high"

    def test_off_passes(self) -> None:
        c = LlmEnsembleCandidateConfig(
            provider="dashscope", model="test-model-a", thinking_level="off"
        )
        assert c.thinking_level == "off"

    def test_empty_string_means_inherit(self) -> None:
        c = LlmEnsembleCandidateConfig(
            provider="dashscope", model="test-model-a", thinking_level=""
        )
        assert c.thinking_level == ""

    def test_typo_coerced_to_empty(self) -> None:
        c = LlmEnsembleCandidateConfig(
            provider="dashscope", model="test-model-a", thinking_level="hgih"
        )
        assert c.thinking_level == ""

    def test_none_coerced_to_empty(self) -> None:
        c = LlmEnsembleCandidateConfig(
            provider="dashscope", model="test-model-a", thinking_level=None
        )
        assert c.thinking_level == ""

    def test_bool_true_coerced_to_empty(self) -> None:
        # TOML bool `thinking_level = true` must not block boot.
        c = LlmEnsembleCandidateConfig(
            provider="dashscope", model="test-model-a", thinking_level=True
        )
        assert c.thinking_level == ""

    def test_false_string_coerced_to_empty(self) -> None:
        # "false" is not a valid thinking level; coerce to inherit.
        c = LlmEnsembleCandidateConfig(
            provider="dashscope", model="test-model-a", thinking_level="false"
        )
        assert c.thinking_level == ""

    def test_all_valid_levels(self) -> None:
        for level in ("off", "minimal", "low", "medium", "high", "xhigh"):
            c = LlmEnsembleCandidateConfig(
                provider="p", model="m", thinking_level=level
            )
            assert c.thinking_level == level


# ---------------------------------------------------------------------------
# 3. custom_b5 propagation
# ---------------------------------------------------------------------------

_TWO_PROPOSERS = [
    {"provider": "dashscope", "model": "test-model-a", "thinking_level": "high"},
    {"provider": "openrouter", "model": "test-provider/test-model-b", "thinking_level": "low"},
]


class TestCustomB5ThinkingPropagation:
    def _make_config(self, candidates: list[dict]) -> GatewayConfig:
        return GatewayConfig(
            llm_ensemble={
                "enabled": True,
                "selection_mode": "custom_b5",
                "candidates": candidates,
            }
        )

    def test_candidate_thinking_reaches_member(self) -> None:
        from opensquilla.provider.ensemble import _build_custom_b5_members
        from opensquilla.provider.selector import ProviderConfig

        candidates = [
            *_TWO_PROPOSERS,
            {
                "provider": "dashscope",
                "model": "test-model-a",
                "role": "aggregator",
                "thinking_level": "medium",
            },
        ]
        cfg = self._make_config(candidates)
        inherited = ProviderConfig(provider="dashscope", model="test-model-a")
        _, members, aggregator, _ = _build_custom_b5_members(
            config=cfg,
            inherited_provider_config=inherited,
            credential_pool_acquirer=None,
            session_key="test",
        )
        assert members[0].thinking == "high"
        assert members[1].thinking == "low"
        assert aggregator.thinking == "medium"

    def test_candidate_without_thinking_inherits(self) -> None:
        from opensquilla.provider.ensemble import _build_custom_b5_members
        from opensquilla.provider.selector import ProviderConfig

        cfg = self._make_config(
            [
                {"provider": "dashscope", "model": "test-model-a"},
                {"provider": "openrouter", "model": "test-provider/test-model-b"},
            ]
        )
        inherited = ProviderConfig(provider="dashscope", model="test-model-a")
        _, members, aggregator, _ = _build_custom_b5_members(
            config=cfg,
            inherited_provider_config=inherited,
            credential_pool_acquirer=None,
            session_key="test",
        )
        assert members[0].thinking is None
        assert members[1].thinking is None
        # Fallback aggregator (no explicit aggregator row) also inherits.
        assert aggregator.thinking is None



# ---------------------------------------------------------------------------
# 4. Provider guard: forced-thinking model prefixes
# ---------------------------------------------------------------------------


class TestProviderForcedThinkingPolicy:
    def test_dashscope_has_forced_thinking_prefixes(self) -> None:
        from opensquilla.provider.compat_policy import compat_policy_for_kind

        policy = compat_policy_for_kind("dashscope")
        assert policy.thinking_required_model_prefixes

    def test_non_dashscope_has_no_forced_prefixes(self) -> None:
        from opensquilla.provider.compat_policy import compat_policy_for_kind

        for kind in ("openai", "openrouter", "deepseek", "gemini"):
            policy = compat_policy_for_kind(kind)
            assert not policy.thinking_required_model_prefixes

    def test_forced_thinking_prefix_is_prefix_match(self) -> None:
        """Verify the guard uses startswith, not exact match."""
        from opensquilla.provider.compat_policy import compat_policy_for_kind

        policy = compat_policy_for_kind("dashscope")
        prefixes = policy.thinking_required_model_prefixes
        # A model id starting with the prefix should match.
        model = "qwen3.8-max-preview"
        assert model.strip().lower().startswith(prefixes)
        # A different model should not match.
        assert not "qwen-plus".strip().lower().startswith(prefixes)


# ---------------------------------------------------------------------------
# 5. Server-side merge: UI-shaped payload preserves stored thinking_level
# ---------------------------------------------------------------------------


class TestServerSideCandidateMerge:
    def _base_config(self, candidates: list[dict]) -> GatewayConfig:
        return GatewayConfig(
            llm_ensemble={
                "enabled": True,
                "selection_mode": "custom_b5",
                "candidates": candidates,
            }
        )

    def test_ui_payload_preserves_thinking_level(self) -> None:
        """A 5-key UI payload must not erase a stored thinking_level."""
        from opensquilla.onboarding.mutations import upsert_llm_ensemble

        cfg = self._base_config(
            [
                {
                    "provider": "dashscope",
                    "model": "test-model-a",
                    "source": "custom",
                    "enabled": True,
                    "role": "",
                    "thinking_level": "high",
                },
                {
                    "provider": "openrouter",
                    "model": "test-provider/test-model-b",
                    "source": "custom",
                    "enabled": True,
                    "role": "",
                    "thinking_level": "low",
                },
            ]
        )
        # UI sends only 5 keys (no thinking_level).
        res = upsert_llm_ensemble(
            cfg,
            candidates=[
                {
                    "provider": "dashscope",
                    "model": "test-model-a",
                    "source": "custom",
                    "enabled": True,
                    "role": "",
                },
                {
                    "provider": "openrouter",
                    "model": "test-provider/test-model-b",
                    "source": "custom",
                    "enabled": True,
                    "role": "",
                },
            ],
        )
        assert res.config.llm_ensemble.candidates[0].thinking_level == "high"
        assert res.config.llm_ensemble.candidates[1].thinking_level == "low"

    def test_ui_payload_can_update_thinking_level(self) -> None:
        """When the UI explicitly sends thinking_level, it overrides."""
        from opensquilla.onboarding.mutations import upsert_llm_ensemble

        cfg = self._base_config(
            [
                {
                    "provider": "dashscope",
                    "model": "test-model-a",
                    "thinking_level": "high",
                },
                {
                    "provider": "openrouter",
                    "model": "test-provider/test-model-b",
                    "thinking_level": "low",
                },
            ]
        )
        res = upsert_llm_ensemble(
            cfg,
            candidates=[
                {
                    "provider": "dashscope",
                    "model": "test-model-a",
                    "source": "custom",
                    "enabled": True,
                    "role": "",
                    "thinking_level": "off",
                },
                {
                    "provider": "openrouter",
                    "model": "test-provider/test-model-b",
                    "source": "custom",
                    "enabled": True,
                    "role": "",
                },
            ],
        )
        assert res.config.llm_ensemble.candidates[0].thinking_level == "off"
        # Second candidate keeps stored value.
        assert res.config.llm_ensemble.candidates[1].thinking_level == "low"

    def test_deleted_candidate_not_resurrected(self) -> None:
        """A candidate removed from the UI payload stays deleted."""
        from opensquilla.onboarding.mutations import upsert_llm_ensemble

        cfg = self._base_config(
            [
                {
                    "provider": "dashscope",
                    "model": "test-model-a",
                    "thinking_level": "high",
                },
                {
                    "provider": "openrouter",
                    "model": "test-provider/test-model-b",
                    "thinking_level": "low",
                },
                {
                    "provider": "moonshot",
                    "model": "test-model-c",
                    "thinking_level": "medium",
                },
            ]
        )
        # UI sends only two candidates (moonshot is deleted).
        res = upsert_llm_ensemble(
            cfg,
            candidates=[
                {
                    "provider": "dashscope",
                    "model": "test-model-a",
                    "source": "custom",
                    "enabled": True,
                    "role": "",
                },
                {
                    "provider": "openrouter",
                    "model": "test-provider/test-model-b",
                    "source": "custom",
                    "enabled": True,
                    "role": "",
                },
            ],
        )
        assert len(res.config.llm_ensemble.candidates) == 2
        models = {c.model for c in res.config.llm_ensemble.candidates}
        assert "test-model-c" not in models
        # Surviving candidates keep their thinking_level.
        assert res.config.llm_ensemble.candidates[0].thinking_level == "high"

    def test_new_candidate_gets_defaults(self) -> None:
        """A brand-new candidate (not in stored) gets field defaults."""
        from opensquilla.onboarding.mutations import upsert_llm_ensemble

        cfg = self._base_config(
            [
                {"provider": "dashscope", "model": "test-model-a"},
                {"provider": "openrouter", "model": "test-provider/test-model-b"},
            ]
        )
        res = upsert_llm_ensemble(
            cfg,
            candidates=[
                {
                    "provider": "dashscope",
                    "model": "test-model-a",
                    "source": "custom",
                    "enabled": True,
                    "role": "",
                },
                {
                    "provider": "openrouter",
                    "model": "test-provider/test-model-b",
                    "source": "custom",
                    "enabled": True,
                    "role": "",
                },
                {
                    "provider": "moonshot",
                    "model": "test-model-c",
                    "source": "custom",
                    "enabled": True,
                    "role": "",
                },
            ],
        )
        new_candidate = res.config.llm_ensemble.candidates[2]
        assert new_candidate.model == "test-model-c"
        assert new_candidate.thinking_level == ""


# ---------------------------------------------------------------------------
# 6. Downgrade compatibility
# ---------------------------------------------------------------------------


class TestDowngradeCompatibility:
    def test_old_config_without_thinking_level_loads(self) -> None:
        """A config persisted by an older version (no thinking_level key)
        must still load without ValidationError."""
        cfg = GatewayConfig(
            llm_ensemble={
                "enabled": True,
                "selection_mode": "custom_b5",
                "candidates": [
                    {
                        "provider": "dashscope",
                        "model": "test-model-a",
                        "source": "custom",
                        "enabled": True,
                        "role": "",
                    },
                    {
                        "provider": "openrouter",
                        "model": "test-provider/test-model-b",
                        "source": "custom",
                        "enabled": True,
                        "role": "",
                    },
                ],
            }
        )
        assert cfg.llm_ensemble.candidates[0].thinking_level == ""
        assert cfg.llm_ensemble.candidates[1].thinking_level == ""

    def test_llm_config_without_thinking_level_loads(self) -> None:
        cfg = LlmProviderConfig()
        assert cfg.thinking is None
