from __future__ import annotations

import json
import os
import stat
from types import SimpleNamespace

import pytest

from opensquilla.telemetry.growth.state import (
    DESKTOP_GROWTH_MILESTONE_STATE_NAME,
    GATEWAY_GROWTH_MILESTONE_STATE_NAME,
    GROWTH_COHORT_STATE_NAME,
    GrowthStateError,
    delete_growth_cohort_state,
    gateway_growth_milestone_state_path,
    growth_cohort_state_path,
    read_active_growth_cohort,
    write_active_growth_cohort,
)


def test_active_cohort_receipt_is_strict_stable_and_cross_process_shaped(tmp_path) -> None:
    path = tmp_path / "telemetry" / GROWTH_COHORT_STATE_NAME

    first = write_active_growth_cohort(
        path,
        activated_at_utc="2026-09-02T01:02:03.004Z",
    )
    second = write_active_growth_cohort(
        path,
        activated_at_utc="2026-09-03T01:02:03.004Z",
    )

    assert second == first
    assert read_active_growth_cohort(path) == first
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "state": "active",
        "activated_at_utc": "2026-09-02T01:02:03.004Z",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1, "state": "active"},
        {
            "schema_version": 1,
            "state": "active",
            "activated_at_utc": "2026-09-02T01:02:03.004Z",
            "unknown": True,
        },
        {
            "schema_version": 2,
            "state": "active",
            "activated_at_utc": "2026-09-02T01:02:03.004Z",
        },
        {
            "schema_version": 1,
            "state": "preexisting",
            "activated_at_utc": "2026-09-02T01:02:03.004Z",
        },
        {
            "schema_version": 1,
            "state": "active",
            "activated_at_utc": "2026-09-02T09:02:03+08:00",
        },
    ],
)
def test_invalid_or_extended_cohort_receipt_fails_closed(tmp_path, payload) -> None:
    path = tmp_path / GROWTH_COHORT_STATE_NAME
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(GrowthStateError):
        read_active_growth_cohort(path)


def test_absence_is_ineligible_without_creating_state(tmp_path) -> None:
    path = tmp_path / "missing" / GROWTH_COHORT_STATE_NAME

    assert read_active_growth_cohort(path) is None
    assert not path.parent.exists()


def test_cleanup_targets_only_growth_cohort_and_gateway_marker(tmp_path) -> None:
    config = SimpleNamespace(state_dir=str(tmp_path))
    cohort = growth_cohort_state_path(config=config)
    gateway = gateway_growth_milestone_state_path(config=config)
    desktop = cohort.parent / DESKTOP_GROWTH_MILESTONE_STATE_NAME
    cohort.parent.mkdir(parents=True)
    cohort.write_text("{}", encoding="utf-8")
    gateway.write_text("{}", encoding="utf-8")
    desktop.write_text("{}", encoding="utf-8")
    keep = cohort.parent / "reliability-outbox.sqlite3"
    keep.write_text("keep", encoding="utf-8")

    removed = delete_growth_cohort_state(config=config)

    assert set(removed) == {cohort, gateway, desktop}
    assert not cohort.exists()
    assert not gateway.exists()
    assert not desktop.exists()
    assert keep.read_text(encoding="utf-8") == "keep"
    assert delete_growth_cohort_state(config=config) == ()
    assert cohort.name == GROWTH_COHORT_STATE_NAME
    assert gateway.name == GATEWAY_GROWTH_MILESTONE_STATE_NAME


def test_symlink_receipt_is_rejected_without_touching_target(tmp_path) -> None:
    if os.name == "nt":
        pytest.skip("symlink permissions vary on Windows")
    target = tmp_path / "outside.json"
    target.write_text("outside", encoding="utf-8")
    path = tmp_path / GROWTH_COHORT_STATE_NAME
    path.symlink_to(target)

    with pytest.raises(GrowthStateError, match="symlink"):
        read_active_growth_cohort(path)

    assert target.read_text(encoding="utf-8") == "outside"


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes are not authoritative on Windows")
def test_cohort_receipt_is_private_on_posix(tmp_path) -> None:
    path = tmp_path / GROWTH_COHORT_STATE_NAME
    write_active_growth_cohort(path, activated_at_utc="2026-09-02T01:02:03.004Z")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
