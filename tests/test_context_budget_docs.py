"""Keep the context-budget documentation describing the shipped values.

``context_budget_tokens`` is a flat cap that never consults the model, so the
number written down is the only way a reader learns where compaction actually
starts on their model. Documentation that has drifted from the code is worse
here than none: it tells someone with a 1M-token window that they are using it.
"""

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


def test_every_overflow_policy_is_documented() -> None:
    """A new policy that nobody can discover is a policy nobody will use."""
    guide = _read(_GUIDE)
    example = _read(_EXAMPLE)

    for policy in ContextOverflowPolicy:
        assert policy.value in guide, f"{policy.value} is missing from {_GUIDE}"
        assert policy.value in example, f"{policy.value} is missing from {_EXAMPLE}"


def test_guide_separates_the_flat_cap_from_the_model_aware_ratio() -> None:
    """The two limits are the whole point: one ignores the model, one does not.

    ``apply_context_overflow_policy`` takes no model, provider, or window
    argument, so its budget cannot track the window. Losing that distinction in
    the docs is what leaves an operator unable to explain why a large-window
    session compacted early.
    """
    guide = _read(_GUIDE)

    assert "context_budget_tokens" in guide
    assert "preflight_compact_ratio" in guide
    assert "context window" in guide.lower()
