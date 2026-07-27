"""Shared sandbox run-mode vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal


class RunMode(StrEnum):
    STANDARD = "standard"
    TRUSTED = "trusted"
    FULL = "full"


@dataclass(frozen=True)
class RunModeConfigPatch:
    run_mode: RunMode
    sandbox: bool
    security_grading: bool
    network_default: Literal["none", "proxy_allowlist"]
    permissions_default_mode: str


_RUN_MODE_ALIASES = {
    "on": RunMode.STANDARD,
    "off": RunMode.STANDARD,
    "standard": RunMode.STANDARD,
    "standard-sandbox": RunMode.STANDARD,
    "standard_sandbox": RunMode.STANDARD,
    "trust": RunMode.TRUSTED,
    "trusted": RunMode.TRUSTED,
    "trusted-sandbox": RunMode.TRUSTED,
    "trusted_sandbox": RunMode.TRUSTED,
    "full": RunMode.FULL,
    "full-host-access": RunMode.FULL,
    "full_host_access": RunMode.FULL,
}


def normalize_run_mode(value: Any, default: RunMode = RunMode.FULL) -> RunMode:
    if isinstance(value, RunMode):
        return value
    if value is None or str(value).strip() == "":
        return normalize_run_mode(default)

    key = str(value).strip().lower()
    try:
        return _RUN_MODE_ALIASES[key]
    except KeyError as exc:
        allowed = ", ".join(sorted(_RUN_MODE_ALIASES))
        raise ValueError(f"run_mode must be one of: {allowed}") from exc


def display_name(mode: Any) -> str:
    normalized = normalize_run_mode(mode)
    if normalized == RunMode.STANDARD:
        return "Standard-Sandbox"
    if normalized == RunMode.TRUSTED:
        return "Managed Execution"
    return "Full Host Access"


def execution_target(mode: Any) -> Literal["sandbox", "host"]:
    return "host" if normalize_run_mode(mode) == RunMode.FULL else "sandbox"


def approval_behavior(mode: Any) -> Literal["standard", "trusted", "full"]:
    return normalize_run_mode(mode).value


def run_mode_config_patch(mode: Any) -> RunModeConfigPatch:
    normalized = normalize_run_mode(mode)
    if normalized == RunMode.FULL:
        return RunModeConfigPatch(
            run_mode=normalized,
            sandbox=False,
            security_grading=False,
            network_default="none",
            permissions_default_mode="full",
        )
    return RunModeConfigPatch(
        run_mode=normalized,
        sandbox=True,
        security_grading=True,
        network_default="proxy_allowlist",
        permissions_default_mode="off",
    )


def legacy_state_to_run_mode(
    *,
    sandbox_enabled: Any,
    grading_enabled: Any,
    permissions_default_mode: Any,
) -> RunMode:
    permission_mode = str(permissions_default_mode or "").strip().lower()
    if permission_mode == "full":
        return RunMode.FULL
    if permission_mode in {"bypass", "off", "on", ""}:
        return RunMode.TRUSTED
    if permission_mode in {"standard", "standard-sandbox", "standard_sandbox", "restricted"}:
        return RunMode.STANDARD
    if not bool(sandbox_enabled):
        return RunMode.TRUSTED
    if bool(sandbox_enabled) and not bool(grading_enabled):
        return RunMode.STANDARD
    return RunMode.TRUSTED


def config_run_mode(config: Any) -> RunMode:
    sandbox = getattr(config, "sandbox", None)
    explicit = getattr(sandbox, "run_mode", None)
    if explicit is not None:
        return normalize_run_mode(explicit)

    permissions = getattr(config, "permissions", None)
    permission_mode = str(getattr(permissions, "default_mode", "off") or "").strip().lower()
    if permission_mode == "full":
        return RunMode.FULL
    if _field_was_set(sandbox, "sandbox") and not bool(getattr(sandbox, "sandbox", False)):
        return RunMode.FULL
    if not _field_was_set(sandbox, "sandbox") and not _field_was_set(
        sandbox,
        "security_grading",
    ):
        return RunMode.FULL

    return legacy_state_to_run_mode(
        sandbox_enabled=getattr(sandbox, "sandbox", False),
        grading_enabled=getattr(sandbox, "security_grading", False),
        permissions_default_mode=permission_mode,
    )


def _field_was_set(model: Any, field_name: str) -> bool:
    fields_set = getattr(model, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(model, "__fields_set__", None)
    return field_name in fields_set if fields_set is not None else False


def _full_mode_is_explicit(config: Any) -> bool:
    sandbox = getattr(config, "sandbox", None)
    permissions = getattr(config, "permissions", None)
    configured_run_mode = getattr(sandbox, "run_mode", None)
    if configured_run_mode is not None:
        return normalize_run_mode(configured_run_mode) is RunMode.FULL
    sandbox_enabled = getattr(sandbox, "sandbox", None)
    if _field_was_set(sandbox, "sandbox") and not bool(sandbox_enabled):
        return True
    return str(getattr(permissions, "default_mode", "")).strip().lower() == "full"


def project_default_run_mode(config: Any) -> RunMode:
    return config_run_mode(config)


def sandbox_runtime_capability_mode(config: Any) -> RunMode:
    configured = config_run_mode(config)
    if configured is RunMode.FULL and not _full_mode_is_explicit(config):
        return RunMode.STANDARD
    return configured


__all__ = [
    "RunMode",
    "RunModeConfigPatch",
    "approval_behavior",
    "config_run_mode",
    "display_name",
    "execution_target",
    "legacy_state_to_run_mode",
    "normalize_run_mode",
    "project_default_run_mode",
    "run_mode_config_patch",
    "sandbox_runtime_capability_mode",
]
