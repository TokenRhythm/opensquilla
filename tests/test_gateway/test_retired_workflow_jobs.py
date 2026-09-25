"""Upgrade startup disables removed handlers before scheduler catch-up."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from opensquilla.gateway.boot import _disable_retired_skill_jobs


async def test_legacy_proposal_jobs_disabled_without_affecting_ordinary_jobs():
    jobs = [
        SimpleNamespace(id="old-handler", handler_key="auto_propose", name="old", enabled=True),
        SimpleNamespace(id="old-name", handler_key="", name="auto_propose:main", enabled=True),
        SimpleNamespace(id="paused", handler_key="auto_propose", name="old", enabled=False),
        SimpleNamespace(id="ordinary", handler_key="agent_run", name="digest", enabled=True),
    ]
    scheduler = SimpleNamespace(list_jobs=AsyncMock(return_value=jobs), update_job=AsyncMock())
    await _disable_retired_skill_jobs(scheduler)
    assert scheduler.update_job.await_args_list == [
        (("old-handler",), {"enabled": False}),
        (("old-name",), {"enabled": False}),
    ]


async def test_retired_job_scan_failure_does_not_block_boot():
    scheduler = SimpleNamespace(
        list_jobs=AsyncMock(side_effect=RuntimeError("scheduler database is busy")),
        update_job=AsyncMock(),
    )

    await _disable_retired_skill_jobs(scheduler)

    scheduler.update_job.assert_not_awaited()


async def test_retired_job_update_failure_does_not_block_other_cleanup():
    jobs = [
        SimpleNamespace(id="broken-update", handler_key="auto_propose", name="old", enabled=True),
        SimpleNamespace(
            id="second-retired", handler_key="", name="auto_propose:main", enabled=True
        ),
        SimpleNamespace(id="ordinary", handler_key="agent_run", name="digest", enabled=True),
    ]
    scheduler = SimpleNamespace(
        list_jobs=AsyncMock(return_value=jobs),
        update_job=AsyncMock(side_effect=RuntimeError("scheduler database is busy")),
    )

    await _disable_retired_skill_jobs(scheduler)

    assert scheduler.update_job.await_args_list == [
        (("broken-update",), {"enabled": False}),
        (("second-retired",), {"enabled": False}),
    ]
