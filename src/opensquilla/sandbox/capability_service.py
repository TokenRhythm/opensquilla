"""Immutable Safe availability reports (legacy capability RPC response shape)."""

from __future__ import annotations

from dataclasses import dataclass

from opensquilla.sandbox.setup_state import SandboxSetupState, SetupResult

REQUIRED_SAFE_CAPABILITIES = frozenset(
    {
        "process",
        "filesystem-worker",
        "denyWriteCarveout",
        "authorityDenyRead",
    }
)
WINDOWS_REQUIRED_SAFE_CAPABILITIES = frozenset(
    {
        "windowsIdentity",
        "windowsStorage",
        "windowsProxyWfp",
    }
)


def required_safe_capabilities(platform: str) -> frozenset[str]:
    required = REQUIRED_SAFE_CAPABILITIES
    if str(platform).lower().startswith("win"):
        required |= WINDOWS_REQUIRED_SAFE_CAPABILITIES
    return required


@dataclass(frozen=True)
class CapabilityReport:
    available: bool
    backend: str
    platform: str
    code: str
    reason: str
    setup_supported: bool
    restart_required: bool
    probe_version: int
    capabilities: frozenset[str]

    @classmethod
    def available_for(
        cls,
        *,
        backend: str,
        platform: str,
        reason: str = "ready",
        capabilities: frozenset[str] = REQUIRED_SAFE_CAPABILITIES,
    ) -> CapabilityReport:
        return cls(
            available=required_safe_capabilities(platform).issubset(capabilities),
            backend=backend,
            platform=platform,
            code="ready",
            reason=reason,
            setup_supported=True,
            restart_required=False,
            probe_version=1,
            capabilities=capabilities,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "available": self.available,
            "backend": self.backend,
            "platform": self.platform,
            "code": self.code,
            "reason": self.reason,
            "setupSupported": self.setup_supported,
            "restartRequired": self.restart_required,
            "probeVersion": self.probe_version,
            "capabilities": sorted(self.capabilities),
        }


def capability_report_from_setup(
    setup: SetupResult,
    *,
    backend: str,
) -> CapabilityReport:
    code = {
        SandboxSetupState.READY: "ready",
        SandboxSetupState.NOT_SETUP: "not_setup",
        SandboxSetupState.SETTING_UP: "setting_up",
        SandboxSetupState.FAILED: "setup_failed",
        SandboxSetupState.UNAVAILABLE: "backend_unavailable",
    }[setup.state]
    return CapabilityReport(
        available=setup.state is SandboxSetupState.READY,
        backend=str(backend),
        platform=setup.platform,
        code=code,
        reason=setup.detail or setup.message,
        setup_supported=setup.state is not SandboxSetupState.UNAVAILABLE,
        restart_required=False,
        # No measured capability claims: availability describes initialization.
        probe_version=0,
        capabilities=frozenset(),
    )


__all__ = [
    "REQUIRED_SAFE_CAPABILITIES",
    "WINDOWS_REQUIRED_SAFE_CAPABILITIES",
    "CapabilityReport",
    "capability_report_from_setup",
    "required_safe_capabilities",
]
