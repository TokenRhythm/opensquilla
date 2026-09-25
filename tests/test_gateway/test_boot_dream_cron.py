"""Boot-time auto-registration of memory_dream crons uses structured schedule.

Covers:
- ``interval_h=6`` → ``(ScheduleKind.CRON, "0 */6 * * *")``.
- ``interval_h=7`` (24%7 != 0) → ``(ScheduleKind.EVERY, str(7 * 3600))``.
- Drift detection: changing ``interval_h`` triggers an update on the existing
  job to the new structured form.
- No supported ``interval_h`` raises ``CronParseError``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.gateway.boot import _interval_h_to_schedule, _register_dream_crons
from opensquilla.scheduler.parser import CronParseError
from opensquilla.scheduler.types import CronJob, ScheduleKind, SessionTarget


def test_interval_h_to_schedule_aligns_to_cron_when_24_divides_evenly() -> None:
    kind, value = _interval_h_to_schedule(6)
    assert (kind, value) == (ScheduleKind.CRON, "0 */6 * * *")


def test_interval_h_to_schedule_falls_back_to_every_seconds() -> None:
    kind, value = _interval_h_to_schedule(7)
    assert (kind, value) == (ScheduleKind.EVERY, str(7 * 3600))


@pytest.mark.parametrize("interval_h", [1, 2, 3, 4, 5, 6, 7, 8, 12, 24])
def test_interval_h_to_schedule_does_not_raise(interval_h: int) -> None:
    """No supported interval should raise during translation."""
    kind, value = _interval_h_to_schedule(interval_h)
    assert kind in (ScheduleKind.CRON, ScheduleKind.EVERY)
    assert value


class _RecordingScheduler:
    def __init__(self, existing: list[CronJob] | None = None) -> None:
        self.existing = list(existing or [])
        self.added: list[dict] = []
        self.updated: list[tuple[str, dict]] = []

    async def list_jobs(self) -> list[CronJob]:
        return list(self.existing)

    async def add_job(self, **kwargs) -> CronJob:
        self.added.append(kwargs)
        job = CronJob(
            id=f"new-{len(self.added)}",
            name=kwargs["name"],
            schedule_kind=kwargs["schedule_kind"],
            cron_expr=kwargs["schedule_value"],
            schedule_raw=kwargs["schedule_value"],
            handler_key=kwargs["handler_key"],
            payload=kwargs["payload"],
            session_target=kwargs["session_target"],
        )
        self.existing.append(job)
        return job

    async def update_job(self, job_id: str, **patch) -> CronJob | None:
        self.updated.append((job_id, dict(patch)))
        for j in self.existing:
            if j.id == job_id:
                for key, value in patch.items():
                    if key == "schedule_value":
                        j.cron_expr = value
                        j.schedule_raw = value
                    elif key == "schedule_kind":
                        j.schedule_kind = value
                    else:
                        setattr(j, key, value)
                return j
        return None


def _memory_config(interval_h: int) -> SimpleNamespace:
    dream = SimpleNamespace(
        enabled=True,
        auto_schedule=True,
        interval_h=interval_h,
        cron=None,
    )
    return SimpleNamespace(dream=dream)


@pytest.mark.asyncio
async def test_register_creates_cron_kind_when_interval_divides_24() -> None:
    scheduler = _RecordingScheduler()
    await _register_dream_crons(
        scheduler=scheduler,
        memory_config=_memory_config(interval_h=6),
        agent_ids=["main"],
    )
    assert len(scheduler.added) == 1
    kwargs = scheduler.added[0]
    assert kwargs["schedule_kind"] == ScheduleKind.CRON
    assert kwargs["schedule_value"] == "0 */6 * * *"
    assert kwargs["session_target"] == SessionTarget.ISOLATED


@pytest.mark.asyncio
async def test_register_creates_every_kind_when_interval_does_not_divide_24() -> None:
    scheduler = _RecordingScheduler()
    await _register_dream_crons(
        scheduler=scheduler,
        memory_config=_memory_config(interval_h=7),
        agent_ids=["main"],
    )
    assert len(scheduler.added) == 1
    kwargs = scheduler.added[0]
    assert kwargs["schedule_kind"] == ScheduleKind.EVERY
    assert kwargs["schedule_value"] == str(7 * 3600)


@pytest.mark.asyncio
async def test_register_drift_detection_updates_to_new_structured_form() -> None:
    """Existing job at interval=6 should update when config switches to interval=8."""
    existing = CronJob(
        id="dream-main",
        name="memory_dream:main",
        schedule_kind=ScheduleKind.CRON,
        cron_expr="0 */6 * * *",
        schedule_raw="0 */6 * * *",
        handler_key="memory_dream",
        payload={"agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
    )
    scheduler = _RecordingScheduler(existing=[existing])

    await _register_dream_crons(
        scheduler=scheduler,
        memory_config=_memory_config(interval_h=8),
        agent_ids=["main"],
    )

    assert scheduler.added == []
    assert len(scheduler.updated) == 1
    job_id, patch = scheduler.updated[0]
    assert job_id == "dream-main"
    assert patch["schedule_kind"] == ScheduleKind.CRON
    assert patch["schedule_value"] == "0 */8 * * *"


@pytest.mark.asyncio
async def test_register_does_not_raise_for_any_supported_interval_h() -> None:
    """Sanity: registration must not raise CronParseError for common intervals."""
    for interval_h in (1, 2, 3, 4, 5, 6, 7, 8, 12, 24):
        scheduler = _RecordingScheduler()
        try:
            await _register_dream_crons(
                scheduler=scheduler,
                memory_config=_memory_config(interval_h=interval_h),
                agent_ids=["main"],
            )
        except CronParseError as exc:  # pragma: no cover — guard
            pytest.fail(f"interval_h={interval_h} raised CronParseError: {exc}")
