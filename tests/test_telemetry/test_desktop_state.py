from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from opensquilla.telemetry.consent import (
    CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
    CURRENT_RELIABILITY_NOTICE_VERSION,
    ConsentDecision,
    ScopeConsentState,
    TelemetryScope,
)
from opensquilla.telemetry.desktop_state import (
    DesktopTelemetryStateError,
    clear_desktop_early_spool_scope,
    desktop_consent_mirror_path,
    desktop_early_spool_root,
    write_desktop_consent_mirror,
)


def _state(
    scope: TelemetryScope,
    decision: ConsentDecision,
    *,
    forced: bool = False,
) -> ScopeConsentState:
    notice = (
        CURRENT_RELIABILITY_NOTICE_VERSION
        if scope is TelemetryScope.RELIABILITY
        else CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION
    )
    return ScopeConsentState(
        scope=scope,
        decision=decision,
        notice_version=notice if decision is ConsentDecision.GRANTED else None,
        consented_at_utc=(
            "2026-09-02T08:00:00Z" if decision is ConsentDecision.GRANTED else None
        ),
        record_complete=decision is ConsentDecision.GRANTED,
        notice_current=decision is ConsentDecision.GRANTED,
        forced_off_reasons=("ci",) if forced else (),
    )


def test_atomic_mirror_matches_electron_closed_shape_and_permissions(tmp_path: Path) -> None:
    path = write_desktop_consent_mirror(
        tmp_path,
        reliability=_state(TelemetryScope.RELIABILITY, ConsentDecision.GRANTED),
        growth=_state(TelemetryScope.GROWTH, ConsentDecision.DECLINED),
    )
    assert path == desktop_consent_mirror_path(tmp_path)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "reliability": {
            "enabled": True,
            "notice_version": CURRENT_RELIABILITY_NOTICE_VERSION,
            "consented_at_utc": "2026-09-02T08:00:00Z",
            "forced_off": False,
        },
        "growth": {
            "enabled": False,
            "notice_version": None,
            "consented_at_utc": None,
            "forced_off": False,
        },
    }
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
        assert path.parent.stat().st_mode & 0o077 == 0


def test_mirror_rejects_wrong_scope_and_symlink_target(tmp_path: Path) -> None:
    reliability = _state(TelemetryScope.RELIABILITY, ConsentDecision.GRANTED)
    growth = _state(TelemetryScope.GROWTH, ConsentDecision.GRANTED)
    with pytest.raises(ValueError, match="wrong scope"):
        write_desktop_consent_mirror(
            tmp_path,
            reliability=growth,
            growth=growth,
        )

    path = desktop_consent_mirror_path(tmp_path)
    path.parent.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("unchanged", encoding="utf-8")
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(DesktopTelemetryStateError, match="symlink"):
        write_desktop_consent_mirror(
            tmp_path,
            reliability=reliability,
            growth=growth,
        )
    assert outside.read_text(encoding="utf-8") == "unchanged"


def test_scope_cleanup_only_removes_managed_files_in_selected_scope(
    tmp_path: Path,
) -> None:
    root = desktop_early_spool_root(tmp_path)
    reliability = root / "reliability"
    growth = root / "growth"
    reliability.mkdir(parents=True)
    growth.mkdir()
    for name in ("one.ready", "two.processing.42", ".three.42.tmp"):
        (reliability / name).write_text("event", encoding="utf-8")
    growth_event = growth / "growth.ready"
    growth_event.write_text("keep", encoding="utf-8")

    result = clear_desktop_early_spool_scope(tmp_path, TelemetryScope.RELIABILITY)
    assert result.complete
    assert result.removed == 3
    assert result.failed == 0
    assert not reliability.exists()
    assert growth_event.exists()


def test_scope_cleanup_detaches_writer_path_before_deleting(tmp_path: Path) -> None:
    root = desktop_early_spool_root(tmp_path)
    scope = root / "reliability"
    scope.mkdir(parents=True)
    temporary = scope / ".event.42.tmp"
    temporary.write_text("event", encoding="utf-8")

    result = clear_desktop_early_spool_scope(tmp_path, TelemetryScope.RELIABILITY)

    assert result.complete
    assert result.removed == 1
    assert not scope.exists()
    with pytest.raises(FileNotFoundError):
        temporary.replace(scope / "event.ready")


def test_scope_cleanup_preserves_but_quarantines_unmanaged_entries(
    tmp_path: Path,
) -> None:
    root = desktop_early_spool_root(tmp_path)
    scope = root / "growth"
    scope.mkdir(parents=True)
    unmanaged = scope / "notes.txt"
    unmanaged.write_text("keep", encoding="utf-8")

    result = clear_desktop_early_spool_scope(tmp_path, TelemetryScope.GROWTH)

    assert result.unsafe
    assert not result.complete
    assert not scope.exists()
    quarantines = tuple(root.glob(".revoked-growth-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "notes.txt").read_text(encoding="utf-8") == "keep"


def test_scope_cleanup_reports_unsafe_directory_without_following_symlink(
    tmp_path: Path,
) -> None:
    root = desktop_early_spool_root(tmp_path)
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "event.ready"
    protected.write_text("keep", encoding="utf-8")
    try:
        (root / "reliability").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    result = clear_desktop_early_spool_scope(tmp_path, TelemetryScope.RELIABILITY)
    assert result.unsafe
    assert not result.complete
    assert protected.exists()


def test_scope_cleanup_is_idempotent_when_spool_is_absent(tmp_path: Path) -> None:
    first = clear_desktop_early_spool_scope(tmp_path, TelemetryScope.GROWTH)
    second = clear_desktop_early_spool_scope(tmp_path, TelemetryScope.GROWTH)
    assert first.complete and second.complete
    assert first.removed == second.removed == 0
