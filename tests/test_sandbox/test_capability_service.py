from __future__ import annotations

from opensquilla.sandbox.capability_service import (
    CapabilityReport,
    CapabilityService,
    capability_report_from_setup,
)
from opensquilla.sandbox.setup_state import SandboxSetupState, SetupResult


def test_ready_setup_requires_live_capability_probe() -> None:
    report = capability_report_from_setup(
        SetupResult(
            state=SandboxSetupState.READY,
            platform="win32",
            message="ready",
            detail="windows_default=ready",
        ),
        backend="windows_default",
    )

    assert report.available is False
    assert report.capabilities == frozenset()
    assert report.backend == "windows_default"
    assert report.code == "probe_required"


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


async def test_service_caches_by_fingerprint_and_can_invalidate() -> None:
    calls: list[str] = []

    async def probe(fingerprint: str) -> CapabilityReport:
        calls.append(fingerprint)
        return CapabilityReport.available_for(
            backend="noop",
            platform="test",
            reason=fingerprint,
        )

    service = CapabilityService(probe)

    first = await service.get("a")
    second = await service.get("a")
    third = await service.get("b")
    service.invalidate()
    fourth = await service.get("a")

    assert first is second
    assert third.reason == "b"
    assert fourth.reason == "a"
    assert calls == ["a", "b", "a"]
