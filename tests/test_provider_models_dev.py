"""Contract tests for the vendored models.dev snapshot lookups."""

from __future__ import annotations

import pytest

from opensquilla.provider import models_dev
from opensquilla.provider.models_dev import (
    _snapshot_providers,
    lookup_limits,
    lookup_model,
)


def test_snapshot_loads_and_covers_registered_providers() -> None:
    providers = _snapshot_providers()
    assert providers, "vendored snapshot must load"
    for expected in ("openrouter", "openai", "anthropic", "deepseek", "gemini", "zhipu"):
        assert expected in providers, f"snapshot missing provider table: {expected}"


def test_provider_scoped_lookup_prefers_own_table() -> None:
    entry = lookup_model("deepseek", "deepseek-v4-pro")
    assert entry is not None
    assert entry["ctx"] == 1_000_000
    assert entry["out"] == 384_000
    assert entry["reasoning"] is True
    assert entry["tools"] is True


def test_basename_fallback_within_provider_table() -> None:
    # OpenRouter spelling against the direct-provider table resolves via
    # basename, so both spellings agree.
    qualified = lookup_limits("deepseek", "deepseek/deepseek-v4-pro")
    bare = lookup_limits("deepseek", "deepseek-v4-pro")
    assert qualified == bare


def test_cross_provider_merge_is_conservative() -> None:
    # No provider table for "" → merge across providers with per-dimension min.
    merged = lookup_model("", "deepseek-v4-pro")
    assert merged is not None
    scoped_openrouter = lookup_model("openrouter", "deepseek/deepseek-v4-pro")
    scoped_direct = lookup_model("deepseek", "deepseek-v4-pro")
    assert scoped_openrouter is not None and scoped_direct is not None
    assert merged["ctx"] <= min(scoped_openrouter["ctx"], scoped_direct["ctx"])


def test_case_insensitive_lookup() -> None:
    # MiniMax publishes mixed-case ids; snapshot keys and lookups are lowercase.
    assert lookup_model("minimax", "MiniMax-M2.5") is not None


def test_mainland_bailian_reuses_coding_plan_snapshot_until_refresh() -> None:
    international = lookup_model("bailian_coding", "qwen3.7-plus")
    mainland = lookup_model("bailian_coding_cn", "qwen3.7-plus")

    assert international is not None
    assert mainland == international


def test_unknown_model_returns_none() -> None:
    assert lookup_model("openai", "no-such-model-xyz") is None
    assert lookup_limits("openai", "no-such-model-xyz") is None
    assert lookup_model("", "") is None


# ---------------------------------------------------------------------------
# Optional per-Mtok cost keys (in/out/cr/cw_mtok). The committed snapshot
# does not carry them yet, so these drive synthetic tables.
# ---------------------------------------------------------------------------

_COSTED_TABLES = {
    "acme": {"model-a": {"ctx": 10_000, "out": 1_000, "tools": True, "in_mtok": 2.5}},
    "other": {"model-a": {"ctx": 8_000, "out": 900, "tools": True, "in_mtok": 9.9}},
}


def test_provider_table_hit_passes_cost_keys_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(models_dev, "_snapshot_providers", lambda: _COSTED_TABLES)

    entry = lookup_model("acme", "model-a")

    assert entry is not None
    assert entry["in_mtok"] == 2.5
    assert "out_mtok" not in entry  # absent keys stay absent


def test_cross_provider_merge_drops_cost_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models_dev, "_snapshot_providers", lambda: _COSTED_TABLES)

    merged = lookup_model("unlisted-provider", "model-a")

    assert merged is not None
    # Limits still merge conservatively…
    assert merged["ctx"] == 8_000
    assert merged["out"] == 900
    # …but another provider's pricing never survives the fallback merge.
    assert "in_mtok" not in merged
    assert "out_mtok" not in merged
    assert "cr_mtok" not in merged
    assert "cw_mtok" not in merged
