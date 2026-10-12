"""Stable failures raised by the offline desktop recovery engine."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opensquilla.recovery.models import RecoveryReport


class RecoveryError(RuntimeError):
    """Base class for failures that are safe to expose as stable protocol codes."""

    stable_code = "recovery_error"

    def __init__(self, message: str, *, stable_code: str | None = None) -> None:
        super().__init__(message)
        if stable_code is not None:
            self.stable_code = stable_code


class RecoveryRequiredError(RecoveryError):
    """Raised by the bootstrap guard when the selected profile is not safe to run."""

    stable_code = "recovery_required"

    def __init__(self, report: RecoveryReport) -> None:
        self.report = report
        super().__init__(
            f"OpenSquilla profile requires offline recovery ({report.stable_code})",
            stable_code=report.stable_code,
        )


class ProfileLockBusyError(RecoveryError):
    stable_code = "profile_lock_busy"


class LegacyGatewayRunningError(RecoveryError):
    stable_code = "legacy_gateway_running"


class UnsafePathError(RecoveryError):
    stable_code = "unsafe_path"


class NoReplaceUnavailableError(RecoveryError):
    stable_code = "no_replace_unavailable"


class DestinationExistsError(RecoveryError):
    stable_code = "destination_exists"


class CrossDeviceMoveError(RecoveryError):
    stable_code = "cross_device_move"


class AtomicStateUnknownError(RecoveryError):
    stable_code = "atomic_state_unknown"


class ConfigChangedError(RecoveryError):
    stable_code = "config_changed"


class WorkspaceOverrideError(RecoveryError):
    stable_code = "workspace_env_override"


class StaleRecoveryTransactionError(RecoveryError):
    stable_code = "stale_recovery_transaction"


class InvalidWorkspaceError(RecoveryError):
    stable_code = "invalid_workspace"


class RestoreValidationError(RecoveryError):
    stable_code = "restore_validation_failed"
