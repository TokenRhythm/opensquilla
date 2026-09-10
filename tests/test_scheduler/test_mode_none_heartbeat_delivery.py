"""mode=NONE cron jobs must not report a skipped heartbeat as delivery failure.

Jobs whose delivery mode is NONE never request delivery (e.g. silent
main-session injection). Their heartbeat run is skipped by design, which the
previous logic surfaced as a hard delivery error, permanently failing the job.
These tests pin the NONE exemption and guard that delivery-required modes
still fail loudly.
"""

from __future__ import annotations

from types import SimpleNamespace

from opensquilla.scheduler.handlers import _required_heartbeat_delivery_error
from opensquilla.scheduler.types import (
    CronJob,
    DeliveryConfig,
    DeliveryMode,
    SessionTarget,
)


def _job(mode: object, *, best_effort: bool = False) -> CronJob:
    return CronJob(
        id="job-1",
        cron_expr="* * * * *",
        handler_key="system_event",
        payload={"kind": "system_event", "text": "x", "agent_id": "main"},
        session_target=SessionTarget.MAIN,
        delivery=DeliveryConfig(mode=mode, best_effort=best_effort),  # type: ignore[arg-type]
    )


_OVERRIDE = {"channel_name": "feishu", "channel_id": "chat-1"}


def _hb(status: str = "skipped", delivery_status: str = "skipped", reason: str = "disabled") -> SimpleNamespace:
    return SimpleNamespace(status=status, delivery_status=delivery_status, reason=reason)


def test_mode_none_skipped_heartbeat_is_not_an_error() -> None:
    """The fixed path: NONE mode must exempt a skipped heartbeat."""
    assert (
        _required_heartbeat_delivery_error(_job(DeliveryMode.NONE), _OVERRIDE, _hb())
        is None
    )


def test_mode_none_string_is_normalized_and_exempt() -> None:
    """Jobs restored from persistence may carry the raw string mode."""
    assert _required_heartbeat_delivery_error(_job("none"), _OVERRIDE, _hb()) is None


def test_channel_mode_skipped_heartbeat_still_errors() -> None:
    """Regression guard: delivery-required modes must still fail loudly."""
    err = _required_heartbeat_delivery_error(_job(DeliveryMode.CHANNEL), _OVERRIDE, _hb())
    assert err is not None
    assert "disabled" in err


def test_best_effort_skipped_heartbeat_is_exempt() -> None:
    """Existing behaviour: best_effort never fails on delivery."""
    assert (
        _required_heartbeat_delivery_error(
            _job(DeliveryMode.CHANNEL, best_effort=True), _OVERRIDE, _hb()
        )
        is None
    )


def test_no_override_is_exempt() -> None:
    """Existing behaviour: no pinned override means nothing is required."""
    assert _required_heartbeat_delivery_error(_job(DeliveryMode.CHANNEL), None, _hb()) is None


def test_mode_none_actual_delivery_failure_still_errors() -> None:
    """A real delivery attempt that failed must fail even for mode=NONE.

    Only the skipped heartbeat is exempt; if the heartbeat actually tried to
    deliver and failed, the job must still report the failure loudly.
    """
    err = _required_heartbeat_delivery_error(
        _job(DeliveryMode.NONE),
        _OVERRIDE,
        _hb(status="failed", delivery_status="delivery_failed", reason="channel down"),
    )
    assert err is not None
    assert "channel down" in err
