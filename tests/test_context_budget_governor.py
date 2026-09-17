from __future__ import annotations

import pytest

from opensquilla.context_budget import (
    ContextBudgetClass,
    ContextBudgetGovernor,
)
from opensquilla.engine import AgentConfig, ThinkingLevel


def test_context_budget_governor_derives_large_window_caps() -> None:
    budget = ContextBudgetGovernor.from_values(
        context_window_tokens=200_000,
        max_output_tokens=8_192,
        thinking_budget_tokens=0,
        context_overflow_threshold=0.85,
    ).snapshot()

    assert budget.provider_request_max_chars > 500_000
    assert budget.default_tool_argument_max_chars > 8_000
    assert budget.default_tool_result_provider_max_chars > 96_000
    # Both hit the existing result cap once the full physical window is usable.
    assert budget.external_tool_result_provider_max_chars == 160_000
    assert budget.default_tool_result_provider_max_chars == 160_000


def test_context_budget_governor_exhausted_generation_reserve_leaves_no_input() -> None:
    budget = ContextBudgetGovernor.from_values(
        context_window_tokens=8_000,
        max_output_tokens=8_192,
        thinking_budget_tokens=0,
        context_overflow_threshold=0.85,
    ).snapshot()

    assert budget.usable_tokens == 0
    assert budget.reserved_tokens == budget.context_window_tokens
    # Keep a positive character guard; the independent zero token budget is
    # authoritative and must not be mistaken for disabled size admission.
    assert budget.provider_request_max_chars == 1
    assert 2_000 <= budget.default_tool_argument_max_chars <= 16_000
    assert budget.default_tool_result_provider_max_chars <= 32_000


def test_context_budget_governor_honors_explicit_overrides() -> None:
    governor = ContextBudgetGovernor.from_values(
        context_window_tokens=200_000,
        max_output_tokens=8_192,
        thinking_budget_tokens=0,
        context_overflow_threshold=0.85,
        provider_request_proof_max_chars=123_456,
        tool_use_argument_provider_request_max_chars=12_345,
        tool_result_provider_request_max_chars=54_321,
    )

    budget = governor.snapshot()

    assert budget.provider_request_max_chars == 123_456
    assert governor.tool_argument_chars_for(ContextBudgetClass.LOCAL) == 12_345
    assert governor.tool_result_provider_chars_for(ContextBudgetClass.LOCAL) == 54_321


def test_context_budget_governor_large_reasoning_reserve_is_not_thresholded() -> None:
    budget = ContextBudgetGovernor.from_values(
        context_window_tokens=202_752,
        max_output_tokens=32_768,
        thinking_budget_tokens=50_000,
        context_overflow_threshold=0.85,
    ).snapshot()

    assert budget.usable_tokens == 99_984
    assert budget.provider_request_max_chars == 399_936


def test_context_budget_governor_explicit_proof_budget_bypasses_glm_ladder() -> None:
    """An explicit 650k proof budget beats the derived GLM-window ladder."""
    budget = ContextBudgetGovernor.from_values(
        context_window_tokens=202_752,
        max_output_tokens=32_768,
        thinking_budget_tokens=20_000,
        context_overflow_threshold=0.85,
        provider_request_proof_max_chars=650_000,
    ).snapshot()

    assert budget.provider_request_max_chars == 650_000
    # Derived side-effect caps scale from the explicit proof budget.
    assert budget.default_tool_argument_max_chars == 104_000  # 650k * 0.16
    assert budget.external_tool_argument_max_chars == 32_000  # 32k clamp
    assert budget.default_tool_result_provider_max_chars == 160_000  # clamp
    assert budget.external_tool_result_provider_max_chars == 160_000


def test_context_budget_governor_from_agent_config_reads_explicit_proof_budget() -> None:
    """AgentConfig.provider_request_proof_max_chars reaches the governor bypass."""
    config = AgentConfig(
        context_window_tokens=202_752,
        max_tokens=32_768,
        thinking=ThinkingLevel.HIGH,
        provider_request_proof_max_chars=650_000,
    )

    assert (
        ContextBudgetGovernor.from_config(config).snapshot().provider_request_max_chars == 650_000
    )

    derived = AgentConfig(
        context_window_tokens=202_752,
        max_tokens=32_768,
        thinking=ThinkingLevel.HIGH,
    )

    assert (
        ContextBudgetGovernor.from_config(derived).snapshot().provider_request_max_chars == 519_936
    )


def test_context_budget_governor_external_caps_stay_stricter_than_local() -> None:
    governor = ContextBudgetGovernor.from_values(
        context_window_tokens=128_000,
        max_output_tokens=8_192,
        thinking_budget_tokens=0,
        context_overflow_threshold=0.85,
    )

    assert governor.tool_argument_chars_for(ContextBudgetClass.EXTERNAL) < (
        governor.tool_argument_chars_for(ContextBudgetClass.LOCAL)
    )
    assert governor.tool_result_provider_chars_for(ContextBudgetClass.EXTERNAL) < (
        governor.tool_result_provider_chars_for(ContextBudgetClass.LOCAL)
    )


@pytest.mark.parametrize("window", [8_000, 16_000, 32_000, 64_000, 200_000, 1_000_000])
def test_soft_threshold_and_character_override_do_not_change_token_capacity(window: int) -> None:
    baseline = ContextBudgetGovernor.from_values(
        context_window_tokens=window,
        max_output_tokens=2_000,
        thinking_budget_tokens=0,
        context_overflow_threshold=0.85,
    ).snapshot()
    overridden = ContextBudgetGovernor.from_values(
        context_window_tokens=window,
        max_output_tokens=2_000,
        thinking_budget_tokens=0,
        context_overflow_threshold=0.50,
        provider_request_proof_max_chars=1_234,
    ).snapshot()

    assert overridden.usable_tokens == baseline.usable_tokens
    assert baseline.provider_request_max_chars == baseline.usable_tokens * 4
    assert overridden.provider_request_max_chars == 1_234


def test_small_context_capacity_is_not_limited_to_32k_characters() -> None:
    budget = ContextBudgetGovernor.from_values(
        context_window_tokens=32_000,
        max_output_tokens=4_000,
        thinking_budget_tokens=0,
        context_overflow_threshold=0.85,
    ).snapshot()

    assert budget.usable_tokens == 24_000
    assert budget.provider_request_max_chars == 96_000
