"""Shared permission posture helpers."""

from __future__ import annotations

from typing import Any

from opensquilla.run_mode import RunMode, config_run_mode

ELEVATED_PERMISSION_MODES = frozenset({"on", "bypass", "full"})
PERMISSION_MODES = frozenset({"off", *ELEVATED_PERMISSION_MODES})


def normalize_permission_mode(value: Any, *, default: str = "off") -> str:
    mode = str(value if value is not None else default).strip().lower()
    if mode == "restricted":
        return "off"
    if mode in PERMISSION_MODES:
        return mode
    allowed = ", ".join(sorted(PERMISSION_MODES | {"restricted"}))
    raise ValueError(f"permissions must be one of: {allowed}")


def configured_default_run_mode(config: Any) -> RunMode:
    return config_run_mode(config)


def configured_default_elevated(config: Any) -> str | None:
    return "full" if configured_default_run_mode(config) == RunMode.FULL else None
