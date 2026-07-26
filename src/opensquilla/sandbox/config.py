"""Sandbox settings model and combination validation.

The settings live in their own module rather than being glued onto
:class:`opensquilla.gateway.config.GatewayConfig` directly so the validation rules
can be unit-tested without booting the gateway. ``GatewayConfig`` is expected
to attach a :class:`SandboxSettings` submodel in a later integration step.

The four-way truth table for the two feature switches is implemented in
:meth:`SandboxSettings.validate_combination`, which returns an
:class:`EffectiveMode` instead of mutating silently. The caller decides
whether to log a warning or abort.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from opensquilla.sandbox.run_mode import RunMode, normalize_run_mode
from opensquilla.sandbox.types import SecurityLevel

log = logging.getLogger(__name__)

BackendName = Literal[
    "auto",
    "bubblewrap",
    "seatbelt",
    "noop",
    "windows_default",
]
NetworkDefault = Literal["none", "proxy_allowlist"]
RunModeName = Literal["standard", "trusted", "full"]
ApprovalsReviewerName = Literal["user", "auto_review"]


@dataclass(frozen=True)
class EffectiveMode:
    """Resolved runtime posture after combination validation.

    The gateway logs one line containing these fields on boot so operators
    can see at a glance which way the switches ended up pointing.
    """

    sandbox_enabled: bool
    grading_enabled: bool
    default_level: SecurityLevel
    backend: BackendName
    insecure_mode: bool
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {
            "sandbox_enabled": self.sandbox_enabled,
            "grading_enabled": self.grading_enabled,
            "default_level": self.default_level.label,
            "backend": self.backend,
            "insecure_mode": self.insecure_mode,
            "notes": list(self.notes),
        }


class SandboxSettings(BaseSettings):
    """Top-level sandbox configuration.

    Two independent switches (§6):

    * ``sandbox`` — whether isolation is enforced at all.
    * ``security_grading`` — whether the level-selection + approval flow is
      active. When false, the system uses a fixed ``STANDARD`` policy with no
      dynamic escalation.

    Both default to ``True`` so fresh local/operator installs start in the
    Managed Execution posture. Invalid combinations are coerced with an explicit
    warning via :meth:`validate_combination`; the coercion is deliberate so
    upgrades of existing deployments do not hard-fail.
    """

    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_SANDBOX_")

    sandbox: bool = True
    security_grading: bool = True
    default_level: SecurityLevel = SecurityLevel.STANDARD
    backend: BackendName = "auto"
    allow_legacy_mode: bool = False
    run_mode: RunModeName | None = None
    auto_setup: bool = True
    host_root_readonly: bool = True
    exclude_slash_tmp: bool = False
    exclude_tmpdir_env_var: bool = False
    approvals_reviewer: ApprovalsReviewerName = "auto_review"

    network_default: NetworkDefault = "proxy_allowlist"
    denial_threshold: int = 3

    extra_ro_mounts: list[str] = Field(default_factory=list)
    extra_rw_mounts: list[str] = Field(default_factory=list)
    denied_read_roots: list[str] = Field(default_factory=list)
    denied_read_globs: list[str] = Field(default_factory=list)

    cpu_seconds: int = 30
    memory_mb: int = 1024
    wall_seconds: int = 60

    @model_validator(mode="before")
    @classmethod
    def _discard_removed_model_review_settings(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        cleaned = dict(values)
        cleaned.pop("approval_review_timeout_seconds", None)
        cleaned.pop("approval_review_max_attempts", None)
        return cleaned

    @field_validator("backend", mode="before")
    @classmethod
    def _reject_removed_windows_backend(cls, value: object) -> object:
        if str(value).strip().lower() == "windows_restricted_token":
            raise ValueError(
                "windows_restricted_token was removed; use backend='windows_default' "
                "or backend='auto'"
            )
        return value

    @model_validator(mode="after")
    def _check_legacy_level(self) -> SandboxSettings:
        """Prevent the DISABLED level from leaking in through default config.

        The ``DISABLED`` security level is legacy/compat only; selecting it
        requires the operator to also flip ``allow_legacy_mode`` on, which
        is a second explicit action. This matches §7.2's "no silent default"
        rule.
        """
        if self.default_level == SecurityLevel.DISABLED and not self.allow_legacy_mode:
            raise ValueError(
                "default_level=DISABLED requires allow_legacy_mode=True; "
                "legacy mode must be opted into explicitly"
            )
        if self.run_mode is not None:
            mode = normalize_run_mode(self.run_mode)
            if mode in {RunMode.STANDARD, RunMode.TRUSTED}:
                self.sandbox = True
                self.security_grading = True
            elif mode == RunMode.FULL:
                self.sandbox = False
                self.security_grading = False
        return self

    def validate_combination(self) -> EffectiveMode:
        """Resolve the two switches into an :class:`EffectiveMode`.

        Truth table:

        * ``sandbox=True, grading=True`` — full mode, level selection on.
        * ``sandbox=True, grading=False`` — isolation on, fixed ``STANDARD``
          policy, approval escalation off.
        * ``sandbox=False, grading=True`` — inconsistent; grading coerced to
          ``False`` with a warning. Never silent.
        * ``sandbox=False, grading=False`` — legacy mode; single ``WARNING``
          emitted so running without sandbox is never invisible.

        The method emits logs as a side effect and returns the resolved
        posture. Callers should log ``EffectiveMode.as_dict()`` at boot.
        """
        notes: list[str] = []
        sandbox_enabled = self.sandbox
        grading_enabled = self.security_grading
        level = self.default_level

        if not sandbox_enabled and grading_enabled:
            log.warning(
                "sandbox.invalid_combo: sandbox=false with grading=true coerced to grading=false"
            )
            grading_enabled = False
            notes.append("grading_coerced_to_false_because_sandbox_disabled")

        if not grading_enabled and sandbox_enabled:
            log.info("sandbox.grading_disabled: using fixed STANDARD policy, no approval flow")
            level = SecurityLevel.STANDARD
            notes.append("fixed_standard_policy")

        insecure = not sandbox_enabled
        if insecure:
            log.warning("sandbox.disabled_insecure_mode: sandbox=false; host isolation is OFF")
            notes.append("insecure_mode")
            if not self.allow_legacy_mode:
                notes.append("legacy_flag_missing")

        return EffectiveMode(
            sandbox_enabled=sandbox_enabled,
            grading_enabled=grading_enabled,
            default_level=level,
            backend=self.backend,
            insecure_mode=insecure,
            notes=tuple(notes),
        )


__all__ = [
    "ApprovalsReviewerName",
    "BackendName",
    "EffectiveMode",
    "NetworkDefault",
    "RunModeName",
    "SandboxSettings",
]
