"""tz must be reachable from user surfaces, not just internal APIs.

tz is wired through ops/engine/persistence and must also be exposed on the cron
tool and RPC layer. These tests assert it is reachable through both surfaces and
round-trips on the RPC wire payload.
"""

from __future__ import annotations

import json
from typing import Any

from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_cron import (
    _cron_create_contract as _handle_cron_add,
)
from opensquilla.gateway.rpc_cron import (
    _cron_update_contract as _handle_cron_update,
)
from opensquilla.gateway.rpc_cron import (
    _job_to_wire,
)
from opensquilla.scheduler.payloads import AGENT_TURN_KIND
from opensquilla.scheduler.types import CronJob, DeliveryConfig, SessionTarget


class _FakeScheduler:
    def __init__(self, job: CronJob | None = None) -> None:
        self.added_kwargs: dict[str, Any] | None = None
        self.updated_patch: dict[str, Any] | None = None
        self.job = job

    async def add_job(self, **kwargs):
        self.added_kwargs = kwargs
        return CronJob(
            id="job-1",
            name=kwargs["name"],
            cron_expr=kwargs.get("schedule_value") or kwargs.get("schedule_raw", ""),
            schedule_raw=kwargs.get("schedule_value") or kwargs.get("schedule_raw", ""),
            tz=kwargs.get("tz", ""),
            handler_key=kwargs["handler_key"],
            payload=kwargs["payload"],
            session_target=kwargs["session_target"],
            session_key=kwargs["session_key"],
            origin_session_key=kwargs["origin_session_key"],
            delivery=kwargs.get("delivery") or DeliveryConfig(),
        )

    async def update_job(self, job_id, **patch):
        self.updated_patch = patch
        if self.job is None:
            return CronJob(id=job_id, **patch)
        for key, value in patch.items():
            setattr(self.job, key, value)
        return self.job

    async def get_job(self, job_id):
        if self.job is not None and self.job.id == job_id:
            return self.job
        return None


# --- RPC: cron.add accepts tz --------------------------------------------


async def test_rpc_cron_add_forwards_tz_to_scheduler() -> None:
    scheduler = _FakeScheduler()
    await _handle_cron_add(
        {
            "name": "Morning brief",
            "expression": "0 9 * * *",
            "payloadKind": AGENT_TURN_KIND,
            "text": "Summarize overnight updates",
            "agentId": "main",
            "tz": "America/Los_Angeles",
        },
        RpcContext(conn_id="test", cron_scheduler=scheduler),
    )
    assert scheduler.added_kwargs is not None
    assert scheduler.added_kwargs["tz"] == "America/Los_Angeles"


async def test_rpc_cron_add_accepts_timezone_alias() -> None:
    scheduler = _FakeScheduler()
    await _handle_cron_add(
        {
            "name": "X",
            "expression": "0 9 * * *",
            "payloadKind": AGENT_TURN_KIND,
            "text": "x",
            "timezone": "Asia/Shanghai",
        },
        RpcContext(conn_id="test", cron_scheduler=scheduler),
    )
    assert scheduler.added_kwargs["tz"] == "Asia/Shanghai"


async def test_rpc_cron_add_defaults_tz_to_empty() -> None:
    scheduler = _FakeScheduler()
    await _handle_cron_add(
        {
            "name": "X",
            "expression": "0 9 * * *",
            "payloadKind": AGENT_TURN_KIND,
            "text": "x",
        },
        RpcContext(conn_id="test", cron_scheduler=scheduler),
    )
    assert scheduler.added_kwargs["tz"] == ""


async def test_rpc_cron_update_patches_tz() -> None:
    current = CronJob(
        id="job-1",
        cron_expr="0 9 * * *",
        handler_key="agent_run",
        payload={"kind": AGENT_TURN_KIND, "task": "x", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
    )
    scheduler = _FakeScheduler(job=current)
    await _handle_cron_update(
        {"id": "job-1", "tz": "America/New_York"},
        RpcContext(conn_id="test", cron_scheduler=scheduler),
    )
    assert scheduler.updated_patch is not None
    assert scheduler.updated_patch["tz"] == "America/New_York"


def test_job_to_wire_emits_tz() -> None:
    job = CronJob(
        id="job-1",
        name="Briefing",
        cron_expr="0 9 * * *",
        schedule_raw="0 9 * * *",
        tz="America/Los_Angeles",
        handler_key="agent_run",
        payload={"kind": AGENT_TURN_KIND, "task": "brief", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
    )
    wire = _job_to_wire(job)
    assert wire["tz"] == "America/Los_Angeles"


def test_job_to_wire_emits_empty_tz_when_unset() -> None:
    job = CronJob(
        id="job-1",
        name="Briefing",
        cron_expr="0 9 * * *",
        schedule_raw="0 9 * * *",
        handler_key="agent_run",
        payload={"kind": AGENT_TURN_KIND, "task": "brief", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
    )
    wire = _job_to_wire(job)
    assert wire["tz"] == ""


# --- Cron tool (model-facing): accepts tz --------------------------------


class _ToolFakeScheduler:
    def __init__(self) -> None:
        self.added_kwargs: dict[str, Any] | None = None

    async def list_jobs(self):
        return []

    async def add_job(self, **kwargs):
        self.added_kwargs = kwargs
        return CronJob(
            id="job-tool",
            name=kwargs["name"],
            cron_expr=kwargs.get("schedule_value") or kwargs.get("schedule_raw", ""),
            schedule_raw=kwargs.get("schedule_value") or kwargs.get("schedule_raw", ""),
            tz=kwargs.get("tz", ""),
            handler_key=kwargs["handler_key"],
            payload=kwargs["payload"],
            session_target=kwargs["session_target"],
            session_key=kwargs["session_key"],
            origin_session_key=kwargs["origin_session_key"],
            delivery=kwargs.get("delivery") or DeliveryConfig(),
        )

    async def update_job(self, job_id, **patch):
        return None

    async def remove_job(self, job_id):
        return True

    async def run_job_now(self, job_id):
        return None


async def test_cron_tool_accepts_tz_param() -> None:
    """The model-facing `cron` tool must let callers set tz at add time."""
    import opensquilla.tools.builtin.admin as admin_mod
    from opensquilla.tools.builtin.admin import cron as cron_tool

    fake = _ToolFakeScheduler()
    admin_mod.set_scheduler(fake)
    try:
        raw = await cron_tool(
            action="add",
            schedule={"kind": "cron", "expr": "0 9 * * *"},
            task="Morning briefing",
            job_kind="agent_turn",
            session_target="isolated",
            tz="America/Los_Angeles",
        )
    finally:
        admin_mod.set_scheduler(None)  # type: ignore[arg-type]

    assert fake.added_kwargs is not None
    assert fake.added_kwargs["tz"] == "America/Los_Angeles"

    response = json.loads(raw)
    assert response["tz"] == "America/Los_Angeles"


async def test_cron_tool_defaults_tz_to_empty_string() -> None:
    import opensquilla.tools.builtin.admin as admin_mod
    from opensquilla.tools.builtin.admin import cron as cron_tool

    fake = _ToolFakeScheduler()
    admin_mod.set_scheduler(fake)
    try:
        raw = await cron_tool(
            action="add",
            schedule={"kind": "cron", "expr": "0 9 * * *"},
            task="x",
            job_kind="agent_turn",
            session_target="isolated",
        )
    finally:
        admin_mod.set_scheduler(None)  # type: ignore[arg-type]
    assert fake.added_kwargs["tz"] == ""
    assert json.loads(raw)["tz"] == ""
