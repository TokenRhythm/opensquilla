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
