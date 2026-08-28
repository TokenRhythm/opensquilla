"""Vendored models.dev lookups for the model catalog.

The snapshot (``models_dev_snapshot.json``, refreshed via
``scripts/refresh_models_dev_snapshot.py``) is the offline source of real
per-``(provider, model)`` context/output limits and capability booleans. It
sits between the live provider catalog (authoritative when reachable) and
the hand-maintained conservative budget rows packaged in
``catalog_overrides.toml`` (emergency floor), so direct-provider deployments
stop running on guessed limits.
"""

from __future__ import annotations

import json
from functools import cache
from importlib import resources
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Optional per-Mtok cost keys a snapshot entry may carry (see
# scripts/refresh_models_dev_snapshot.py). Provider-table hits pass them
# through verbatim; the cross-provider merge strips them (costs are set by
# the provider actually serving the request, so another provider's numbers
# would be actively wrong rather than conservative).
_COST_KEYS = ("in_mtok", "out_mtok", "cr_mtok", "cw_mtok")

# Region-specific provider ids can share an identical model surface while
# keeping distinct endpoints and configuration identities. Prefer a future
# provider-specific snapshot table when present; until the next snapshot
# refresh, reuse the established sibling table instead of falling through to
# a cross-provider merge that would discard costs and AND capabilities.
_PROVIDER_TABLE_ALIASES = {
    "bailian_coding_cn": "bailian_coding",
}


@cache
def _snapshot_providers() -> dict[str, dict[str, dict[str, Any]]]:
    try:
        path = resources.files("opensquilla.provider").joinpath("models_dev_snapshot.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a missing/corrupt snapshot degrades, never crashes
        log.warning("models_dev.snapshot_unavailable")
        return {}
    providers = payload.get("providers")
    return providers if isinstance(providers, dict) else {}


def _entry_from_table(
    table: dict[str, dict[str, Any]] | None,
    model_l: str,
) -> dict[str, Any] | None:
    if not isinstance(table, dict):
        return None
    entry = table.get(model_l)
    if entry is not None:
        return entry
    if "/" in model_l:
        return table.get(model_l.rsplit("/", 1)[-1])
    return None


def lookup_model(provider_id: str, model_id: str) -> dict[str, Any] | None:
    """Return the snapshot entry for ``(provider, model)``.

    Tries the provider's own table (verbatim id, then basename). When the
    provider has no table or no entry, falls back to a conservative
    cross-provider merge of same-id entries (per-dimension minimum for
    limits, AND for capability booleans) — matching the philosophy of the
    static fallback: under-estimating triggers compaction earlier,
    over-estimating causes silent server-side truncation.
    """
    providers = _snapshot_providers()
    model_l = (model_id or "").strip().lower()
    if not model_l:
        return None
    provider_l = (provider_id or "").strip().lower()
    entry = _entry_from_table(providers.get(provider_l), model_l)
    if entry is None and (alias := _PROVIDER_TABLE_ALIASES.get(provider_l)):
        entry = _entry_from_table(providers.get(alias), model_l)
    if entry is not None:
        return entry

    matches = [
        found
        for table in providers.values()
        if (found := _entry_from_table(table, model_l)) is not None
    ]
    if not matches:
        return None
    merged: dict[str, Any] = dict(matches[0])
    for other in matches[1:]:
        merged["ctx"] = min(int(merged.get("ctx") or 0), int(other.get("ctx") or 0))
        merged["out"] = min(int(merged.get("out") or 0), int(other.get("out") or 0))
        for flag in ("reasoning", "tools", "vision"):
            merged[flag] = bool(merged.get(flag)) and bool(other.get(flag))
    for cost_key in _COST_KEYS:
        merged.pop(cost_key, None)
    return merged


def lookup_limits(provider_id: str, model_id: str) -> tuple[int, int] | None:
    """Return ``(max_output_tokens, context_window)`` or None when unknown."""
    entry = lookup_model(provider_id, model_id)
    if entry is None:
        return None
    out = int(entry.get("out") or 0)
    ctx = int(entry.get("ctx") or 0)
    if out <= 0 and ctx <= 0:
        return None
    return out, ctx
