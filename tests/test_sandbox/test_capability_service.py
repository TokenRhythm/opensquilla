from __future__ import annotations

from opensquilla.sandbox.capability_service import (
    capability_report_from_setup,
)
from opensquilla.sandbox.setup_state import SandboxSetupState, SetupResult


def test_ready_setup_reports_availability_without_measured_capabilities() -> None:
    report = capability_report_from_setup(
        SetupResult(
            state=SandboxSetupState.READY,
            platform="win32",
            message="ready",
            detail="windows_default=ready",
        ),
        backend="windows_default",
    )

    assert report.available is True
    assert report.capabilities == frozenset()
    assert report.backend == "windows_default"
    assert report.code == "ready"
    assert report.probe_version == 0


def test_failed_setup_is_not_available() -> None:
    report = capability_report_from_setup(
        SetupResult(
            state=SandboxSetupState.FAILED,
            platform="win32",
            message="failed",
            detail="wfp missing",
        ),
        backend="windows_default",
    )

    assert report.available is False
    assert report.code == "setup_failed"
    assert report.capabilities == frozenset()
