"""Consistency and unit tests for the reasoning-dialect registry.

Locks two properties of ``provider/reasoning_dialects.py``:

1. Every ``reasoning_format`` value the runtime can produce today resolves in
   ``DIALECTS`` (or is an explicit, deliberate skip), so a new capability
   ladder branch cannot silently serialize without a dialect entry.
2. Each dialect's enable/disable payload matches the exact literals the
   request-builder ladder produced before the extraction.
"""

from __future__ import annotations

from typing import Any

from opensquilla.engine.types import ThinkingLevel
from opensquilla.provider.compat_policy import compat_policy_for_kind, known_policy_kinds
from opensquilla.provider.reasoning_dialects import (
    DIALECTS,
    ReasoningDisableArgs,
    ReasoningEnableArgs,
    apply_reasoning_disable,
    apply_reasoning_enable,
)
from opensquilla.provider.registry import list_provider_specs

# Every reasoning_format value reachable today, with where each comes from:
#   "openrouter" — compat_policy: openrouter policy replay_reasoning_format;
#       model_catalog.get_capabilities live-catalog branch (an OpenRouter
#       /models row with reasoning support); provider/ensemble.py members.
#   "openrouter_explicit_off" — router_dynamic Task Analyzer request profile;
#       it shares OpenRouter's enable payload and always sends explicit off.
#   "openai"     — get_capabilities api.openai.com + gpt-5/o1/o3/o4 prefix
#       branch (model_catalog.py; host trust stays code).
#   "deepseek"   — compat_policy: deepseek policy default_reasoning_format;
#       get_capabilities deepseek-base-url branch (code) and the transcribed
#       [deepseek."*"] corrections row (catalog_overrides.toml).
#   "gemini"     — [gemini."gemini-2.5*"] corrections row (transcribed from
#       the reasoning_shape="gemini" branch).
#   "zai"        — [zhipu."glm-4.5*"/"glm-4.7*"/"glm-5*"] corrections rows
#       (transcribed from the reasoning_shape="zai" branch).
#   "dashscope"  — [dashscope."qwen…*"/"qwq*"] corrections rows (transcribed
#       from the dashscope prefix ladder).
#   "moonshot"   — [moonshot."kimi-k2…*"] corrections rows (transcribed from
#       the moonshot prefix ladder).
#   "volcengine" — [volcengine.…] AND [byteplus.…] corrections rows (two
#       providers, one wire spelling).
#   "tencent_tokenhub" — compat_policy: tencent_tokenhub policy
#       replay_reasoning_format; [tencent_tokenhub."hy3*"] corrections rows.
#   "none"       — ModelCapabilities default plus every non-reasoning
#       fallthrough (catch-all "*" rows and the no-dialect adaptation in
#       model_catalog._capabilities_from_entry).
REACHABLE_REASONING_FORMATS = frozenset(
    {
        "openrouter",
        "openrouter_explicit_off",
        "openai",
        "deepseek",
        "gemini",
        "zai",
        "dashscope",
        "moonshot",
        "volcengine",
        "tencent_tokenhub",
        "none",
    }
)


def test_every_reachable_reasoning_format_has_a_dialect() -> None:
    missing = REACHABLE_REASONING_FORMATS - {"none"} - set(DIALECTS)
    assert not missing, (
        f"reasoning_format values reachable today without a DIALECTS entry: "
        f"{sorted(missing)} — a thinking request for them would silently "
        "serialize with no reasoning payload."
    )


def test_none_format_is_an_explicit_skip_not_an_entry() -> None:
    """Format "none" produced no payload in the ladder; the registry keeps
    that by omission, and both dispatch helpers must stay no-ops for it."""
    assert "none" not in DIALECTS
    payload: dict[str, Any] = {"model": "m"}
    apply_reasoning_enable(
        payload,
        "none",
        ReasoningEnableArgs(thinking_level=ThinkingLevel.HIGH, thinking_budget_tokens=5000),
    )
    apply_reasoning_disable(payload, "none", ReasoningDisableArgs(model="m"))
    assert payload == {"model": "m"}


def test_hardcoded_reachable_set_covers_policy_and_registry_sources() -> None:
    """Drift guard: any new format introduced through compat_policy or a
    ProviderSpec.reasoning_shape must be added to the hardcoded set above
    (and to DIALECTS) deliberately."""
    policy_formats = set()
    for kind in known_policy_kinds():
        policy = compat_policy_for_kind(kind)
        if policy.default_reasoning_format:
            policy_formats.add(policy.default_reasoning_format)
        if policy.replay_reasoning_format:
            policy_formats.add(policy.replay_reasoning_format)
    assert policy_formats <= REACHABLE_REASONING_FORMATS
    shape_formats = {spec.reasoning_shape for spec in list_provider_specs()}
    assert shape_formats <= REACHABLE_REASONING_FORMATS


def test_dialect_names_match_registry_keys() -> None:
    for key, dialect in DIALECTS.items():
        assert dialect.name == key


def test_moonshot_and_volcengine_are_two_entries_sharing_one_spelling() -> None:
    assert DIALECTS["moonshot"] is not DIALECTS["volcengine"]
    assert DIALECTS["moonshot"].enable is DIALECTS["volcengine"].enable
    assert DIALECTS["moonshot"].disable is DIALECTS["volcengine"].disable


def _enabled(reasoning_format: str, args: ReasoningEnableArgs) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    apply_reasoning_enable(payload, reasoning_format, args)
    return payload


def _disabled(reasoning_format: str, args: ReasoningDisableArgs) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    apply_reasoning_disable(payload, reasoning_format, args)
    return payload


_HIGH_ARGS = ReasoningEnableArgs(thinking_level=ThinkingLevel.HIGH, thinking_budget_tokens=5000)


def test_openrouter_enable_payload() -> None:
    assert _enabled("openrouter", _HIGH_ARGS) == {"reasoning": {"effort": "high"}}


def test_openrouter_preserves_provider_native_max_effort() -> None:
    args = ReasoningEnableArgs(thinking_level="max", thinking_budget_tokens=50_000)
    assert _enabled("openrouter", args) == {"reasoning": {"effort": "max"}}


def test_openrouter_preserves_string_xhigh_effort() -> None:
    args = ReasoningEnableArgs(thinking_level="xhigh", thinking_budget_tokens=50_000)
    assert _enabled("openrouter", args) == {"reasoning": {"effort": "xhigh"}}


def test_openrouter_disable_payload_is_gated_on_policy_model_set() -> None:
    disable_set = frozenset({"z-ai/glm-5"})
    listed = ReasoningDisableArgs(
        model="Z-AI/GLM-5 ", disable_reasoning_by_default_models=disable_set
    )
    assert _disabled("openrouter", listed) == {"reasoning": {"enabled": False}}
    unlisted = ReasoningDisableArgs(
        model="minimax/minimax-m2.5", disable_reasoning_by_default_models=disable_set
    )
    assert _disabled("openrouter", unlisted) == {}


def test_openrouter_explicit_off_reuses_enable_and_always_disables() -> None:
    assert DIALECTS["openrouter_explicit_off"].enable is DIALECTS["openrouter"].enable
    assert _enabled("openrouter_explicit_off", _HIGH_ARGS) == {
        "reasoning": {"effort": "high"}
    }
    assert _disabled(
        "openrouter_explicit_off",
        ReasoningDisableArgs(model="deepseek/deepseek-v4-pro"),
    ) == {"reasoning": {"enabled": False}}


def test_openai_enable_payload_and_no_disable_payload() -> None:
    assert _enabled("openai", _HIGH_ARGS) == {"reasoning_effort": "high"}
    assert DIALECTS["openai"].disable is None
    assert _disabled("openai", ReasoningDisableArgs(model="gpt-5.4")) == {}


def test_deepseek_enable_payload_maps_levels_to_documented_efforts() -> None:
    assert _enabled("deepseek", _HIGH_ARGS) == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }
    xhigh = ReasoningEnableArgs(thinking_level=ThinkingLevel.XHIGH, thinking_budget_tokens=5000)
    assert _enabled("deepseek", xhigh) == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
    }


def test_deepseek_disable_payload() -> None:
    assert _disabled("deepseek", ReasoningDisableArgs(model="deepseek-v4-flash")) == {
        "thinking": {"type": "disabled"}
    }


def test_gemini_enable_payload() -> None:
    assert _enabled("gemini", _HIGH_ARGS) == {"reasoning_effort": "high"}


def test_gemini_disable_payload_is_gated_on_documented_off_control() -> None:
    flash = ReasoningDisableArgs(model="gemini-2.5-flash")
    assert _disabled("gemini", flash) == {"reasoning_effort": "none"}
    pro = ReasoningDisableArgs(model="gemini-2.5-pro")
    assert _disabled("gemini", pro) == {}


def test_zai_enable_and_disable_payloads() -> None:
    assert _enabled("zai", _HIGH_ARGS) == {"thinking": {"type": "enabled"}}
    assert _disabled("zai", ReasoningDisableArgs(model="glm-5")) == {
        "thinking": {"type": "disabled"}
    }


def test_dashscope_enable_payload_carries_thinking_budget() -> None:
    args = ReasoningEnableArgs(thinking_level=ThinkingLevel.HIGH, thinking_budget_tokens=4096)
    assert _enabled("dashscope", args) == {"enable_thinking": True, "thinking_budget": 4096}


def test_dashscope_disable_payload() -> None:
    assert _disabled("dashscope", ReasoningDisableArgs(model="qwen3-coder-plus")) == {
        "enable_thinking": False
    }


def test_moonshot_enable_and_disable_payloads() -> None:
    assert _enabled("moonshot", _HIGH_ARGS) == {"thinking": {"type": "enabled"}}
    assert _disabled("moonshot", ReasoningDisableArgs(model="kimi-k2.5")) == {
        "thinking": {"type": "disabled"}
    }


def test_volcengine_enable_and_disable_payloads() -> None:
    assert _enabled("volcengine", _HIGH_ARGS) == {"thinking": {"type": "enabled"}}
    assert _disabled("volcengine", ReasoningDisableArgs(model="doubao-seed-1-6")) == {
        "thinking": {"type": "disabled"}
    }


def test_tencent_tokenhub_enable_payload_maps_levels_to_documented_efforts() -> None:
    """TokenHub's hy3 documents exactly reasoning_effort low|high, so the
    five-level ladder collapses onto those two values."""
    assert _enabled("tencent_tokenhub", _HIGH_ARGS) == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }
    for level, expected in (
        (ThinkingLevel.MINIMAL, "low"),
        (ThinkingLevel.LOW, "low"),
        (ThinkingLevel.MEDIUM, "high"),
        (ThinkingLevel.XHIGH, "high"),
        (None, "high"),
    ):
        args = ReasoningEnableArgs(thinking_level=level, thinking_budget_tokens=5000)
        assert _enabled("tencent_tokenhub", args) == {
            "thinking": {"type": "enabled"},
            "reasoning_effort": expected,
        }, level


def test_tencent_tokenhub_has_no_disable_payload() -> None:
    """hy3 documents no thinking-off payload; off omits every field."""
    assert DIALECTS["tencent_tokenhub"].disable is None
    assert _disabled("tencent_tokenhub", ReasoningDisableArgs(model="hy3")) == {}


def test_enable_effort_resolves_from_budget_when_level_is_absent() -> None:
    """The budget fallback ladder moved with the helper: pin its bands."""
    assert ReasoningEnableArgs(thinking_level=None, thinking_budget_tokens=1024).effort == "low"
    assert ReasoningEnableArgs(thinking_level=None, thinking_budget_tokens=5000).effort == "medium"
    assert ReasoningEnableArgs(thinking_level=None, thinking_budget_tokens=20000).effort == "high"


def test_unknown_format_produces_no_payload_in_either_direction() -> None:
    payload: dict[str, Any] = {}
    apply_reasoning_enable(payload, "think_tags", _HIGH_ARGS)
    apply_reasoning_disable(payload, "think_tags", ReasoningDisableArgs(model="m"))
    apply_reasoning_enable(payload, "", _HIGH_ARGS)
    apply_reasoning_disable(payload, "", ReasoningDisableArgs(model="m"))
    assert payload == {}
