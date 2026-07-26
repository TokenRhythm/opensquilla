from __future__ import annotations

import types

import pytest

from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.run_mode import (
    RunMode,
    approval_behavior,
    config_run_mode,
    execution_target,
    legacy_state_to_run_mode,
    normalize_run_mode,
    run_mode_config_patch,
)


def test_sandbox_defaults_to_root_readonly_and_auto_review() -> None:
    settings = SandboxSettings()

    assert settings.host_root_readonly is True
    assert settings.approvals_reviewer == "auto_review"


def test_removed_model_review_settings_are_ignored_for_upgrade_compatibility() -> None:
    settings = SandboxSettings(
        approval_review_timeout_seconds=90,
        approval_review_max_attempts=3,
    )

    assert settings.approvals_reviewer == "auto_review"
    assert "approval_review_timeout_seconds" not in settings.model_fields_set
    assert "approval_review_max_attempts" not in settings.model_fields_set


def test_trusted_sandbox_is_sandboxed_and_skips_only_routine_prompts() -> None:
    patch = run_mode_config_patch(RunMode.TRUSTED)

    assert patch.sandbox is True
    assert patch.security_grading is True
    assert patch.network_default == "proxy_allowlist"
    assert patch.permissions_default_mode == "off"
    assert execution_target(RunMode.TRUSTED) == "sandbox"
    assert approval_behavior(RunMode.TRUSTED) == "trusted"


def test_full_host_access_is_the_only_global_host_target() -> None:
    patch = run_mode_config_patch(RunMode.FULL)

    assert patch.network_default == "none"
    assert execution_target(RunMode.STANDARD) == "sandbox"
    assert execution_target(RunMode.TRUSTED) == "sandbox"
    assert execution_target(RunMode.FULL) == "host"


def test_normalize_run_mode_defaults_to_full() -> None:
    assert normalize_run_mode(None) == RunMode.FULL
    assert normalize_run_mode("") == RunMode.FULL
    assert normalize_run_mode("trusted") == RunMode.TRUSTED
    assert normalize_run_mode("standard") == RunMode.STANDARD


def test_legacy_bypass_state_maps_to_trusted_without_preserving_host_bypass() -> None:
    mode = legacy_state_to_run_mode(
        sandbox_enabled=False,
        grading_enabled=False,
        permissions_default_mode="bypass",
    )

    assert mode == RunMode.TRUSTED


def test_default_sandbox_settings_resolve_to_full_host_access_run_mode() -> None:
    settings = SandboxSettings()
    config = types.SimpleNamespace(
        sandbox=settings,
        permissions=types.SimpleNamespace(default_mode="off"),
    )

    effective = settings.validate_combination()

    assert effective.sandbox_enabled is True
    assert effective.grading_enabled is True
    assert config_run_mode(config) == RunMode.FULL


def test_legacy_off_state_maps_to_trusted_sandbox_default() -> None:
    mode = legacy_state_to_run_mode(
        sandbox_enabled=False,
        grading_enabled=False,
        permissions_default_mode="off",
    )

    assert mode == RunMode.TRUSTED


def test_explicit_legacy_sandbox_disabled_config_preserves_full_host_access() -> None:
    settings = SandboxSettings(sandbox=False, security_grading=False)
    config = types.SimpleNamespace(
        sandbox=settings,
        permissions=types.SimpleNamespace(default_mode="off"),
    )

    assert config_run_mode(config) == RunMode.FULL


def test_trusted_patch_round_trips_through_config_run_mode() -> None:
    patch = run_mode_config_patch(RunMode.TRUSTED)
    config = types.SimpleNamespace(
        sandbox=types.SimpleNamespace(
            run_mode=patch.run_mode,
            sandbox=patch.sandbox,
            security_grading=patch.security_grading,
        ),
        permissions=types.SimpleNamespace(default_mode=patch.permissions_default_mode),
    )

    assert config_run_mode(config) == RunMode.TRUSTED


def test_explicit_trusted_run_mode_enables_sandbox_booleans() -> None:
    settings = SandboxSettings(run_mode="trusted")
    config = types.SimpleNamespace(
        sandbox=settings,
        permissions=types.SimpleNamespace(default_mode="off"),
    )

    effective = settings.validate_combination()

    assert effective.sandbox_enabled is True
    assert effective.grading_enabled is True
    assert config_run_mode(config) == RunMode.TRUSTED


def test_explicit_full_run_mode_disables_sandbox_booleans() -> None:
    settings = SandboxSettings(run_mode="full", sandbox=True, security_grading=True)
    config = types.SimpleNamespace(
        sandbox=settings,
        permissions=types.SimpleNamespace(default_mode="full"),
    )

    effective = settings.validate_combination()

    assert effective.sandbox_enabled is False
    assert effective.grading_enabled is False
    assert config_run_mode(config) == RunMode.FULL


def test_removed_windows_restricted_token_backend_config_is_rejected() -> None:
    with pytest.raises(ValueError, match="windows_restricted_token.*windows_default"):
        SandboxSettings(backend="windows_restricted_token")


def test_configured_default_elevated_only_returns_full() -> None:
    from opensquilla.permissions import configured_default_elevated, configured_default_run_mode

    config = types.SimpleNamespace(
        sandbox=types.SimpleNamespace(run_mode="trusted", sandbox=True, security_grading=True),
        permissions=types.SimpleNamespace(default_mode="off"),
    )

    assert configured_default_run_mode(config) == RunMode.TRUSTED
    assert configured_default_elevated(config) is None

    config.sandbox.run_mode = "full"
    assert configured_default_run_mode(config) == RunMode.FULL
    assert configured_default_elevated(config) == "full"


def test_normalize_run_mode_accepts_user_facing_spellings() -> None:
    assert normalize_run_mode("standard-sandbox") == RunMode.STANDARD
    assert normalize_run_mode("trusted") == RunMode.TRUSTED
    assert normalize_run_mode("full-host-access") == RunMode.FULL
