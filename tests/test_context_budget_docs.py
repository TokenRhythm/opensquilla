"""Keep the context-compaction documentation aligned with shipped settings."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from opensquilla.gateway.config import ContextOverflowPolicy, GatewayConfig

ROOT = Path(__file__).resolve().parents[1]

_EXAMPLE = "opensquilla.toml.example"
_GUIDE = "docs/configuration.md"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _commented_default(example: str, key: str) -> object:
    """Parse a ``# key = value`` line from the example config."""
    match = re.search(rf"^#\s*{re.escape(key)}\s*=\s*(.+)$", example, re.MULTILINE)
    assert match is not None, f"{key} must stay documented in {_EXAMPLE}"
    return tomllib.loads(f"{key} = {match.group(1).strip()}")[key]


def test_example_config_documents_the_shipped_context_budget_defaults() -> None:
    defaults = GatewayConfig()
    example = _read(_EXAMPLE)

    assert _commented_default(example, "context_budget_tokens") == (
        defaults.context_budget_tokens
    )
    assert _commented_default(example, "context_overflow_policy") == str(
        defaults.context_overflow_policy
    )
    assert _commented_default(example, "preflight_compact_ratio") == (
        defaults.preflight_compact_ratio
    )


def test_example_context_budget_keys_still_load_from_the_top_level(tmp_path: Path) -> None:
    """The documented keys are only useful if a hand-edited file honours them."""
    config_path = tmp_path / "opensquilla.toml"
    config_path.write_text(
        "\n".join(
            (
                "context_budget_tokens = 250000",
                'context_overflow_policy = "refuse"',
                "preflight_compact_ratio = 0.7",
            )
        ),
        encoding="utf-8",
    )

    loaded = GatewayConfig.load_from_toml(config_path)

    assert loaded.context_budget_tokens == 250_000
    assert loaded.context_overflow_policy == ContextOverflowPolicy.REFUSE
    assert loaded.preflight_compact_ratio == 0.7


def test_compatibility_policy_values_are_not_presented_as_turn_controls() -> None:
    guide = _read(_GUIDE)
    example = _read(_EXAMPLE)

    for policy in ContextOverflowPolicy:
        assert policy.value in guide, f"{policy.value} is missing from {_GUIDE}"
        assert policy.value in example, f"{policy.value} is missing from {_EXAMPLE}"

    assert "Compatibility-only value" in guide
    assert "does not change ordinary turn behaviour" in guide
    assert "context_overflow_policy is compatibility-only" in example
    assert "refuse is not a fail-closed control" in example


def test_guide_describes_the_active_preflight_and_limited_flat_cap_scopes() -> None:
    guide = " ".join(_read(_GUIDE).split())

    assert "effective token and character capacity" in guide
    assert "active user request and attachments" in guide
    assert "The current request is protected" in guide
    assert "not an automatic next-turn threshold" in guide
    assert "manual session compaction" in guide
    assert "not compared with every ordinary turn's history plus new message" in guide
