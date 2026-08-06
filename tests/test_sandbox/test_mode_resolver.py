from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.sandbox.capability_service import CapabilityReport
from opensquilla.sandbox.mode_resolver import ModeResolutionError, resolve_mode
from opensquilla.sandbox.run_mode import RunMode


def _principal(*capabilities: str, auth_state: str = "authenticated"):
    return SimpleNamespace(
        capabilities=frozenset(capabilities),
        auth_state=auth_state,
    )


def _report(*, available: bool) -> CapabilityReport:
    return CapabilityReport(
        available=available,
        backend="windows_default",
        platform="win32",
        code="ready" if available else "backend_unavailable",
        reason="ready" if available else "not available",
        setup_supported=True,
        restart_required=False,
        probe_version=1,
        capabilities=frozenset(
            {"process", "filesystem-worker", "denyWriteCarveout", "authorityDenyRead"}
            if available
            else set()
        ),
    )


def test_safe_mode_stays_safe_when_capability_is_available() -> None:
    resolved = resolve_mode(
        RunMode.SAFE,
        _principal("host.execute"),
        _report(available=True),
    )

    assert resolved.desired_mode is RunMode.SAFE
    assert resolved.effective_mode is RunMode.SAFE
    assert resolved.fallback_reason is None
    assert resolved.confirmation_required is False


def test_authenticated_host_capability_soft_lands_when_safe_is_unavailable() -> None:
    resolved = resolve_mode(
        RunMode.SAFE,
        _principal("host.execute"),
        _report(available=False),
    )

    assert resolved.desired_mode is RunMode.SAFE
    assert resolved.effective_mode is RunMode.FULL
    assert resolved.fallback_reason == "backend_unavailable"
    assert resolved.confirmation_required is True


def test_guest_cannot_soft_land_when_safe_is_unavailable() -> None:
    with pytest.raises(
        ModeResolutionError,
        match="sandbox_unavailable_for_guest",
    ):
        resolve_mode(
            RunMode.SAFE,
            _principal("guest.safe", auth_state="guest"),
            _report(available=False),
        )


def test_explicit_full_requires_host_capability_without_guest_downgrade() -> None:
    with pytest.raises(ModeResolutionError, match="host_capability_required"):
        resolve_mode(
            RunMode.FULL,
            _principal("guest.safe", auth_state="invalid"),
            _report(available=True),
        )


def test_explicit_full_with_capability_does_not_probe_or_fallback() -> None:
    resolved = resolve_mode(
        RunMode.FULL,
        _principal("host.execute"),
        _report(available=False),
    )

    assert resolved.effective_mode is RunMode.FULL
    assert resolved.confirmation_required is False
