"""Refresh the vendored models.dev snapshot used by the model catalog.

Fetches https://models.dev/api.json (MIT-licensed, community-maintained),
trims it to the providers OpenSquilla registers, and writes the compact
snapshot consumed by ``opensquilla.provider.models_dev``.

Usage::

    uv run python scripts/refresh_models_dev_snapshot.py

Review the diff before committing — the snapshot is deliberately small and
human-reviewable so upstream data mistakes are caught at refresh time, not
at runtime. ``check_snapshot_integrity`` refuses to write a snapshot that
shrank suspiciously or silently lost a runtime provider's table.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import httpx

from opensquilla.provider.registry import list_provider_specs

API_URL = "https://models.dev/api.json"
SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "opensquilla"
    / "provider"
    / "models_dev_snapshot.json"
)

# OpenSquilla provider id -> models.dev provider ids (merged in order; the
# first source of a model id wins). Derived from each registered spec's
# ``catalog_source`` so this script cannot drift from the provider registry.
# Script-only extras (sources for providers the registry does not carry)
# would be added explicitly after the comprehension — none exist today.
PROVIDER_SOURCES: dict[str, tuple[str, ...]] = {
    spec.provider_id: spec.catalog_source
    for spec in list_provider_specs()
    if spec.catalog_source
}

# models.dev ``cost`` field -> compact snapshot key. Both sides are USD per
# MILLION tokens, so values are vendored verbatim (no unit conversion).
_COST_KEYS: tuple[tuple[str, str], ...] = (
    ("input", "in_mtok"),
    ("output", "out_mtok"),
    ("cache_read", "cr_mtok"),
    ("cache_write", "cw_mtok"),
)

# A refresh that loses more than this fraction of the committed snapshot's
# models is treated as an upstream incident (payload truncation, source-id
# rename, …), not a routine cleanup: refuse to write.
MAX_SHRINK_RATIO = 0.8


def _trim_model(entry: dict) -> dict | None:
    limit = entry.get("limit") or {}
    context = int(limit.get("context") or 0)
    output = int(limit.get("output") or 0)
    if context <= 0 and output <= 0:
        return None
    # Self-contradictory upstream data (context smaller than max output —
    # e.g. models.dev's openrouter z-ai/glm-5.1 entry) would poison budget
    # resolution; drop it so lookups fall through to a consistent layer.
    if 0 < context < output:
        return None
    modalities = entry.get("modalities") or {}
    inputs = {str(item).lower() for item in modalities.get("input") or []}
    trimmed = {
        "ctx": context,
        "out": output,
        "reasoning": bool(entry.get("reasoning")),
        "tools": bool(entry.get("tool_call")),
        "vision": "image" in inputs,
        "video": "video" in inputs,
    }
    cost = entry.get("cost")
    if isinstance(cost, dict):
        for source_key, snapshot_key in _COST_KEYS:
            value = cost.get(source_key)
            # Vendor only flat per-Mtok leaf numbers. Some models.dev entries
            # nest tiered pricing (lists/dicts keyed by context bands) here;
            # tiers are deliberately ignored — a single misleading average is
            # worse than "unknown", and nuanced pricing is corrections-owned.
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                trimmed[snapshot_key] = value
    return trimmed


def build_snapshot_providers(
    data: dict,
    provider_sources: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, dict[str, dict]]:
    """Trim a raw models.dev ``api.json`` payload to the snapshot tables.

    Pure transform (no network, no filesystem) so tests can drive it with
    synthetic payloads. ``provider_sources`` defaults to the registry-derived
    ``PROVIDER_SOURCES`` mapping.
    """
    sources_map = PROVIDER_SOURCES if provider_sources is None else provider_sources
    providers: dict[str, dict[str, dict]] = {}
    for osq_id, sources in sources_map.items():
        table: dict[str, dict] = {}
        for source in sources:
            models = (data.get(source) or {}).get("models") or {}
            for model_id, entry in models.items():
                key = str(model_id).strip().lower()
                if key in table:
                    continue
                trimmed = _trim_model(entry)
                if trimmed is not None:
                    table[key] = trimmed
        if table:
            providers[osq_id] = dict(sorted(table.items()))
    return providers


def _provider_tables(snapshot: dict) -> dict[str, dict[str, dict]]:
    providers = snapshot.get("providers")
    return providers if isinstance(providers, dict) else {}


def check_snapshot_integrity(
    new: dict,
    old: dict,
    required_provider_ids: Iterable[str] | None = None,
) -> list[str]:
    """Return human-readable reasons a freshly built snapshot must NOT be written.

    Pure comparison of the new snapshot dict against the committed one (both
    in on-disk shape, i.e. carrying a ``providers`` table) — no network, so
    the guards are unit-testable against synthetic dicts. An empty list means
    the snapshot is safe to write.

    Guards:
    - max-shrink: total model count below ``MAX_SHRINK_RATIO`` of the
      committed count means upstream truncation or source-id drift.
    - table regression: a runtime-supported provider with a non-empty
      ``catalog_source`` that HAS a committed table but produced zero entries
      lost its data — never silently degrade it to the synthesized floor.

    ``required_provider_ids`` defaults to every runtime-supported registry
    spec that declares a ``catalog_source``.
    """
    errors: list[str] = []
    new_tables = _provider_tables(new)
    old_tables = _provider_tables(old)

    new_total = sum(len(models) for models in new_tables.values())
    old_total = sum(len(models) for models in old_tables.values())
    if old_total > 0 and new_total < old_total * MAX_SHRINK_RATIO:
        errors.append(
            f"model count shrank from {old_total} to {new_total} "
            f"(< {MAX_SHRINK_RATIO:.0%} of the committed snapshot)"
        )

    if required_provider_ids is None:
        required_provider_ids = [
            spec.provider_id
            for spec in list_provider_specs()
            if spec.runtime_supported and spec.catalog_source
        ]
    for provider_id in required_provider_ids:
        if old_tables.get(provider_id) and not new_tables.get(provider_id):
            errors.append(
                f"provider {provider_id!r} produced zero entries but the "
                "committed snapshot has a table for it"
            )
    return errors


def main() -> int:
    data = httpx.get(API_URL, timeout=30.0, follow_redirects=True).json()
    providers = build_snapshot_providers(data)

    snapshot = {
        "_source": API_URL,
        "_license": "MIT (models.dev, maintained by the SST team)",
        "_fetched": date.today().isoformat(),
        "providers": providers,
    }

    try:
        committed = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        committed = {}
    errors = check_snapshot_integrity(snapshot, committed)
    if errors:
        for error in errors:
            print(f"refusing to write snapshot: {error}", file=sys.stderr)
        return 1

    SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=1, sort_keys=False) + "\n")
    total = sum(len(models) for models in providers.values())
    print(f"wrote {SNAPSHOT_PATH} ({len(providers)} providers, {total} models)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
