"""Provider-aware resolution for the one shared multi-model plan."""

from __future__ import annotations

import os
from typing import Any


def ensemble_selection_configured(config: Any) -> bool:
    """Whether ``llm_ensemble.selection_mode`` is operator-owned."""

    ensemble = getattr(config, "llm_ensemble", None)
    if ensemble is None:
        return False
    force_paths = getattr(config, "force_persist_paths", None)
    if callable(force_paths) and "llm_ensemble.selection_mode" in force_paths():
        return True
    raw = getattr(config, "_persist_raw_base", None)
    if isinstance(raw, dict):
        raw_ensemble = raw.get("llm_ensemble")
        if isinstance(raw_ensemble, dict) and "selection_mode" in raw_ensemble:
            return True
        return bool(
            os.environ.get("OPENSQUILLA_LLM_ENSEMBLE_SELECTION_MODE", "").strip()
        )
    fields_set = getattr(ensemble, "model_fields_set", None)
    if fields_set is None:
        return bool(str(getattr(ensemble, "selection_mode", "") or "").strip())
    return "selection_mode" in set(fields_set)


def recommended_ensemble_selection_mode(config: Any) -> str:
    """Return the active provider preset's recommended shared plan, if any."""

    from opensquilla.provider.preset_registry import get_preset

    provider = str(
        getattr(getattr(config, "llm", None), "provider", "") or ""
    ).strip().lower()
    preset = get_preset(provider)
    return str(
        getattr(preset, "default_ensemble_selection_mode", "") or ""
    ).strip()


def effective_ensemble_selection_mode(config: Any) -> str:
    """Resolve the plan used by global and tier-scoped shared activation.

    Explicit operator configuration always wins.  Before a plan has been
    explicitly saved, curated providers may recommend one; every other
    provider keeps the existing first-activation ``custom_b5`` behavior.
    """

    ensemble = getattr(config, "llm_ensemble", None)
    stored = str(getattr(ensemble, "selection_mode", "") or "").strip()
    if ensemble_selection_configured(config):
        return stored
    # Preserve the pre-shared-field contract for hand-authored/legacy configs
    # that enabled Ensemble without persisting a mode: the model default was
    # the effective choice. Normal activation materializes the recommended
    # plan before flipping this flag.
    if bool(getattr(ensemble, "enabled", False)):
        return stored
    return recommended_ensemble_selection_mode(config) or "custom_b5"


__all__ = [
    "effective_ensemble_selection_mode",
    "ensemble_selection_configured",
    "recommended_ensemble_selection_mode",
]
