from __future__ import annotations

import json
import os
import stat
from types import SimpleNamespace

import pytest

from opensquilla.telemetry.growth.state import (
    CODING_MODE_USAGE_STATE_NAME,
    DESKTOP_GROWTH_MILESTONE_STATE_NAME,
    GATEWAY_GROWTH_MILESTONE_STATE_NAME,
    GROWTH_COHORT_STATE_NAME,
    METASKILL_USAGE_STATE_NAME,
    PRODUCT_ACTIVE_STATE_NAME,
    GrowthStateError,
    delete_growth_cohort_state,
    gateway_growth_milestone_state_path,
    growth_cohort_state_path,
    read_active_growth_cohort,
    write_active_growth_cohort,
)
from opensquilla.telemetry.growth_sink import read_desktop_growth_milestone_state


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
    metaskill = cohort.parent / METASKILL_USAGE_STATE_NAME
    coding_mode = cohort.parent / CODING_MODE_USAGE_STATE_NAME
    product_active = cohort.parent / PRODUCT_ACTIVE_STATE_NAME
    desktop = cohort.parent / DESKTOP_GROWTH_MILESTONE_STATE_NAME
    cohort.parent.mkdir(parents=True)
    cohort.write_text("{}", encoding="utf-8")
    gateway.write_text("{}", encoding="utf-8")
    metaskill.write_text("{}", encoding="utf-8")
    coding_mode.write_text("{}", encoding="utf-8")
    product_active.write_text("{}", encoding="utf-8")
    desktop.write_text("{}", encoding="utf-8")
    keep = cohort.parent / "reliability-outbox.sqlite3"
    keep.write_text("keep", encoding="utf-8")

    removed = delete_growth_cohort_state(config=config)

    assert set(removed) == {cohort, gateway, metaskill, coding_mode, product_active, desktop}
    assert not cohort.exists()
    assert not gateway.exists()
    assert not metaskill.exists()
    assert not coding_mode.exists()
    assert not product_active.exists()
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


@pytest.mark.parametrize(
    "mutation", ["valid", "root", "version", "status", "event", "slot", "oversized"],
)
def test_desktop_enqueue_receipt_uses_existing_closed_wire_shape(tmp_path, mutation):
    event = {
        "event_name": "first_app_ready", "event_version": 1,
        "event_id": "00000000-0000-4000-8000-000000000001",
        "occurred_at_utc": "2026-09-02T01:02:03.004Z", "source": "desktop",
        "app_version": "1.2.3", "platform": "macos", "outcome": None,
        "error_code": None, "duration_ms": None, "consent_scope": "growth",
        "notice_version": "growth-v2", "sample_rate": 1,
        "analytics_user_id": "00000000-0000-4000-8000-000000000002",
        "device_id": "a" * 64,
    }
    record = {"status": "enqueued", "event": event}
    payload = {
        "schema_version": 1, "marker_kind": "growth_desktop_milestones",
        "onboarding_result": None, "first_app_ready": record,
    }
    if mutation == "root":
        payload["unexpected"] = True
    elif mutation == "version":
        payload["schema_version"] = True
    elif mutation == "status":
        record["status"] = "uploaded"
    elif mutation == "event":
        event["prompt"] = "synthetic"
    elif mutation == "slot":
        payload["onboarding_result"], payload["first_app_ready"] = record, None
    path = tmp_path / DESKTOP_GROWTH_MILESTONE_STATE_NAME
    path.write_text(json.dumps(payload) + (" " * 16_384 if mutation == "oversized" else ""))
    if mutation == "valid":
        restored = read_desktop_growth_milestone_state(path)
        assert len(restored) == 1
        assert restored[0].event.model_dump(mode="json") == event
    else:
        with pytest.raises(GrowthStateError):
            read_desktop_growth_milestone_state(path)
