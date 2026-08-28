"""Typed entry for the layered model catalog.

``ModelCatalogEntry`` is the canonical per-model record produced by
``ModelCatalog.resolve_entry``. Costs are expressed **per million tokens**
(per-Mtok) — the internal canonical unit. The live OpenRouter cache stores
per-1k values (``ModelInfo.input_cost_per_1k``); the live layer adapter in
``model_catalog.py`` converts per-1k → per-Mtok, so existing per-1k consumers
are unaffected.

Per-field "unset" sentinels (a lower-authority layer may fill a field only
while it is unset):

- ``str`` fields — never contributed by omission; layers emit them only when
  known (empty string means "no layer knew it").
- ``int`` fields (``context_window``, ``max_output_tokens``) — layers emit
  them only when ``> 0`` in the source data.
- ``float | None`` cost/quality fields — ``None`` means unknown.
- ``bool`` capability flags — layers emit them only when the source genuinely
  carries the flag, so both ``True`` and ``False`` are authoritative once
  emitted.

The merge therefore operates on *dicts of explicitly known fields*, not on
whole entries, which is what makes ``False``/``0``-valued overrides win.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

CatalogSource = Literal["user", "live", "corrections", "snapshot", "synthesized"]


@dataclass(frozen=True)
class ModelCatalogEntry:
    """One resolved model record; ``source`` names the highest-authority
    layer that contributed at least one field."""

    provider_id: str
    model_id: str
    display_name: str = ""
    family: str = ""
    context_window: int = 0
    max_output_tokens: int = 0
    supports_reasoning: bool = False
    supports_tools: bool = True
    supports_vision: bool = False
    reasoning_format: str = "none"
    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None
    cache_read_cost_per_mtok: float | None = None
    cache_write_cost_per_mtok: float | None = None
    quality_prior: float | None = None
    release_date: str = ""
    status: str = "available"
    source: CatalogSource = "snapshot"


# Data fields settable by the user-override and corrections layers. Identity
# fields (provider_id, model_id) and the derived ``source`` are excluded on
# purpose: they describe the resolution, not the model.
_INT_FIELDS = frozenset({"context_window", "max_output_tokens"})
_BOOL_FIELDS = frozenset({"supports_reasoning", "supports_tools", "supports_vision"})
_FLOAT_FIELDS = frozenset(
    {
        "input_cost_per_mtok",
        "output_cost_per_mtok",
        "cache_read_cost_per_mtok",
        "cache_write_cost_per_mtok",
        "quality_prior",
    }
)
_STR_FIELDS = frozenset({"display_name", "family", "reasoning_format", "release_date", "status"})

OVERRIDABLE_ENTRY_FIELDS = _INT_FIELDS | _BOOL_FIELDS | _FLOAT_FIELDS | _STR_FIELDS


def coerce_entry_field(name: str, value: Any) -> Any:
    """Validate and coerce one overridable entry field.

    Raises ``ValueError`` for unknown field names or type-incompatible
    values. Booleans must be real booleans (a string like ``"false"`` is
    rejected rather than silently truthy); ints must be ints; float fields
    accept int or float.
    """
    if name not in OVERRIDABLE_ENTRY_FIELDS:
        raise ValueError(f"unknown model-catalog entry field: {name!r}")
    if name in _BOOL_FIELDS:
        if not isinstance(value, bool):
            raise ValueError(f"field {name!r} expects a boolean, got {value!r}")
        return value
    if name in _INT_FIELDS:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"field {name!r} expects an integer, got {value!r}")
        return value
    if name in _FLOAT_FIELDS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"field {name!r} expects a number, got {value!r}")
        return float(value)
    if not isinstance(value, str):
        raise ValueError(f"field {name!r} expects a string, got {value!r}")
    return value
