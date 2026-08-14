"""RPC cron handlers honour the structured schedule contract.

Covers:
- structured ``cron.create`` round-trip (expression on the wire is normalized).
- structured ``cron.create`` validation surfaces a field-named error.
- legacy ``expression`` flat-string CLI shim still works.
- ``cron.update`` CLI shim still accepts ``expression`` and returns the
  normalized value.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_cron import _handle_cron_add, _handle_cron_update, _job_to_wire
from opensquilla.scheduler.payloads import AGENT_TURN_KIND, normalize_contract
from opensquilla.scheduler.types import CronJob, DeliveryConfig, ScheduleKind


class _FakeScheduler:
    def __init__(self) -> None:
        self.added: dict | None = None
        self.updated: dict | None = None
        self.job: CronJob | None = None

    async def add_job(self, **kwargs) -> CronJob:
        self.added = kwargs
        idempotency_key = kwargs.get("idempotency_key", "")
        if (
            self.job is not None
            and idempotency_key
            and self.job.idempotency_key == idempotency_key
        ):
            self.job.deduplicated = True
            return self.job
        kind = kwargs.get("schedule_kind") or ScheduleKind.CRON
        value = kwargs.get("schedule_value", "")
        self.job = CronJob(
            id="rpc-strict-1",
            name=kwargs["name"],
            cron_expr=value,
            schedule_raw=value,
            schedule_kind=kind,
            handler_key=kwargs["handler_key"],
            payload=kwargs["payload"],
            session_target=kwargs["session_target"],
            session_key=kwargs.get("session_key", ""),
            origin_session_key=kwargs.get("origin_session_key", ""),
            delivery=kwargs.get("delivery") or DeliveryConfig(),
            tz=kwargs.get("schedule_tz") or kwargs.get("tz", "") or "",
            creator_is_owner=bool(kwargs.get("creator_is_owner", False)),
            run_mode=kwargs.get("run_mode", ""),
            elevated="full" if kwargs.get("run_mode") == "full" else "",
            execution_target="host" if kwargs.get("run_mode") == "full" else "sandbox",
            idempotency_key=idempotency_key,
        )
        return self.job

    async def update_job(self, job_id: str, **patch) -> CronJob:
        self.updated = patch
        if self.job is None:
            self.job = CronJob(id=job_id)
        for key, value in patch.items():
            if key == "schedule_value":
                self.job.cron_expr = value
                self.job.schedule_raw = value
            elif key == "schedule_kind":
                self.job.schedule_kind = value
            else:
                setattr(self.job, key, value)
        return self.job

    async def get_job(self, job_id: str) -> CronJob | None:
        return self.job


def test_scheduler_normalization_preserves_validated_workspace_metadata() -> None:
    _, payload, _, _ = normalize_contract(
        handler_key="agent_run",
        payload={
            "kind": AGENT_TURN_KIND,
            "task": "inspect",
            "agent_id": "main",
            "_workspace_id": "project-123",
            "_workspace_name": "Customer portal",
            "_template_id": "project-risk",
            "untrusted_extra": "drop me",
        },
        session_target="isolated",
    )

    assert payload["_workspace_id"] == "project-123"
    assert payload["_workspace_name"] == "Customer portal"
    assert payload["_template_id"] == "project-risk"
    assert "untrusted_extra" not in payload


@pytest.mark.asyncio
async def test_rpc_create_with_structured_cron_returns_normalized_expression() -> None:
    scheduler = _FakeScheduler()

    result = await _handle_cron_add(
        {
            "name": "five",
            "schedule": {"kind": "cron", "expr": "*/5 * * * *"},
            "payloadKind": AGENT_TURN_KIND,
            "text": "ping",
            "agentId": "main",
        },
        RpcContext(conn_id="test", cron_scheduler=scheduler),
    )

    assert scheduler.added is not None
    assert scheduler.added["schedule_kind"] == ScheduleKind.CRON
    assert scheduler.added["schedule_value"] == "*/5 * * * *"
    assert scheduler.added["creator_is_owner"] is True
    assert result["expression"] == "*/5 * * * *"
    assert result["scheduleRaw"] == "*/5 * * * *"
    assert result["scheduleKind"] == "cron"


@pytest.mark.asyncio
async def test_rpc_create_persists_a_validated_project_workspace(monkeypatch) -> None:
    scheduler = _FakeScheduler()
    storage = object()

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_cron.get_session_storage",
        lambda _manager: storage,
    )

    async def resolve_workspace(candidate, workspace_id):
        assert candidate is storage
        assert workspace_id == "project-123"
        return SimpleNamespace(
            workspace=SimpleNamespace(display_name="Customer portal"),
        )

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_cron.resolve_validated_project_workspace",
        resolve_workspace,
    )

    result = await _handle_cron_add(
        {
            "name": "project check",
            "schedule": {"kind": "cron", "expr": "0 9 * * 1"},
            "payloadKind": AGENT_TURN_KIND,
            "text": "inspect the project",
            "agentId": "main",
            "workspaceId": "project-123",
            "templateId": "project-risk",
        },
        RpcContext(
            conn_id="test",
            cron_scheduler=scheduler,
            session_manager=object(),
        ),
    )

    assert scheduler.added is not None
    assert scheduler.added["payload"]["_workspace_id"] == "project-123"
    assert scheduler.added["payload"]["_workspace_name"] == "Customer portal"
    assert scheduler.added["payload"]["_template_id"] == "project-risk"
    assert result["workspaceId"] == "project-123"
    assert result["workspaceName"] == "Customer portal"
    assert result["templateId"] == "project-risk"


@pytest.mark.asyncio
async def test_rpc_create_rejects_a_project_template_without_a_workspace() -> None:
    scheduler = _FakeScheduler()

    with pytest.raises(ValueError, match="requires a project workspace"):
        await _handle_cron_add(
            {
                "name": "project check",
                "schedule": {"kind": "cron", "expr": "0 9 * * 1"},
                "payloadKind": AGENT_TURN_KIND,
                "text": "inspect the project",
                "agentId": "main",
                "templateId": "project-risk",
            },
            RpcContext(conn_id="test", cron_scheduler=scheduler),
        )

    assert scheduler.added is None


@pytest.mark.asyncio
async def test_rpc_create_accepts_explicit_idempotency_key() -> None:
    scheduler = _FakeScheduler()

    result = await _handle_cron_add(
        {
            "name": "five",
            "schedule": {"kind": "cron", "expr": "*/5 * * * *"},
            "payloadKind": AGENT_TURN_KIND,
            "text": "ping",
            "agentId": "main",
            "idempotencyKey": "client-request-123",
        },
        RpcContext(conn_id="test", cron_scheduler=scheduler),
    )

    assert scheduler.added is not None
    assert scheduler.added["idempotency_key"] == "client-request-123"
    assert scheduler.added["run_mode"] == "full"
    assert result["runMode"] == "full"
    assert result["executionTarget"] == "host"
    assert result["deduplicated"] is False


@pytest.mark.asyncio
async def test_rpc_retry_returns_existing_job_as_deduplicated() -> None:
    scheduler = _FakeScheduler()
    params = {
        "name": "five",
        "schedule": {"kind": "cron", "expr": "*/5 * * * *"},
        "payloadKind": AGENT_TURN_KIND,
        "text": "ping",
        "agentId": "main",
        "idempotency_key": "client-request-123",
    }
    context = RpcContext(conn_id="test", cron_scheduler=scheduler)

    first = await _handle_cron_add(params, context)
    retry = await _handle_cron_add(params, context)

    assert first["id"] == retry["id"] == "rpc-strict-1"
    assert first["deduplicated"] is False
    assert retry["deduplicated"] is True


@pytest.mark.asyncio
async def test_rpc_create_rejects_oversized_idempotency_key() -> None:
    with pytest.raises(ValueError, match="at most 256"):
        await _handle_cron_add(
            {
                "schedule": {"kind": "cron", "expr": "*/5 * * * *"},
                "payloadKind": AGENT_TURN_KIND,
                "text": "ping",
                "idempotencyKey": "x" * 257,
            },
            RpcContext(conn_id="test", cron_scheduler=_FakeScheduler()),
        )


@pytest.mark.asyncio
async def test_rpc_create_with_natural_language_expr_raises_field_named_error() -> None:
    scheduler = _FakeScheduler()

    with pytest.raises(ValueError, match="schedule.expr"):
        await _handle_cron_add(
            {
                "name": "bad",
                "schedule": {"kind": "cron", "expr": "每5分钟"},
                "payloadKind": AGENT_TURN_KIND,
                "text": "ping",
                "agentId": "main",
            },
            RpcContext(conn_id="test", cron_scheduler=scheduler),
        )


@pytest.mark.asyncio
async def test_rpc_create_with_legacy_expression_string_still_works() -> None:
    """CLI shim: a flat ``expression`` string is wrapped as kind='cron'."""
    scheduler = _FakeScheduler()

    result = await _handle_cron_add(
        {
            "name": "five",
            "expression": "*/5 * * * *",
            "payloadKind": AGENT_TURN_KIND,
            "text": "ping",
            "agentId": "main",
        },
        RpcContext(conn_id="test", cron_scheduler=scheduler),
    )

    assert scheduler.added["schedule_kind"] == ScheduleKind.CRON
    assert scheduler.added["schedule_value"] == "*/5 * * * *"
    assert result["expression"] == "*/5 * * * *"


@pytest.mark.asyncio
async def test_rpc_create_with_every_schedule() -> None:
    scheduler = _FakeScheduler()

    result = await _handle_cron_add(
        {
            "name": "interval",
            "schedule": {"kind": "every", "every_seconds": 300},
            "payloadKind": AGENT_TURN_KIND,
            "text": "ping",
            "agentId": "main",
        },
        RpcContext(conn_id="test", cron_scheduler=scheduler),
    )

    assert scheduler.added["schedule_kind"] == ScheduleKind.EVERY
    assert scheduler.added["schedule_value"] == "300"
    assert result["scheduleKind"] == "every"
    assert result["scheduleRaw"] == "300"


@pytest.mark.asyncio
async def test_rpc_create_with_at_schedule() -> None:
    scheduler = _FakeScheduler()

    result = await _handle_cron_add(
        {
            "name": "once",
            "schedule": {"kind": "at", "at": "2026-05-18T09:00:00+08:00"},
            "payloadKind": AGENT_TURN_KIND,
            "text": "ping",
            "agentId": "main",
        },
        RpcContext(conn_id="test", cron_scheduler=scheduler),
    )

    assert scheduler.added["schedule_kind"] == ScheduleKind.AT
    assert scheduler.added["schedule_value"] == "2026-05-18T09:00:00+08:00"
    assert result["scheduleKind"] == "at"
    assert result["scheduleRaw"] == "2026-05-18T09:00:00+08:00"


@pytest.mark.asyncio
async def test_rpc_update_via_legacy_expression_returns_normalized_wire() -> None:
    scheduler = _FakeScheduler()
    scheduler.job = CronJob(
        id="job-A",
        name="orig",
        cron_expr="*/5 * * * *",
        schedule_raw="*/5 * * * *",
        schedule_kind=ScheduleKind.CRON,
        handler_key="agent_run",
    )

    result = await _handle_cron_update(
        {
            "id": "job-A",
            "expression": "0 9 * * *",
        },
        RpcContext(conn_id="test", cron_scheduler=scheduler),
    )

    assert scheduler.updated is not None
    assert scheduler.updated["schedule_kind"] == ScheduleKind.CRON
    assert scheduler.updated["schedule_value"] == "0 9 * * *"
    assert result["expression"] == "0 9 * * *"


@pytest.mark.asyncio
async def test_legacy_text_only_update_preserves_workspace_and_template() -> None:
    scheduler = _FakeScheduler()
    scheduler.job = CronJob(
        id="job-A",
        name="project check",
        cron_expr="0 9 * * *",
        schedule_kind=ScheduleKind.CRON,
        handler_key="agent_run",
        payload={
            "kind": AGENT_TURN_KIND,
            "task": "before",
            "agent_id": "main",
            "_workspace_id": "project-123",
            "_workspace_name": "Customer portal",
            "_template_id": "project-risk",
        },
    )

    await _handle_cron_update(
        {"id": "job-A", "text": "changed"},
        RpcContext(conn_id="test", cron_scheduler=scheduler),
    )

    assert scheduler.updated is not None
    assert scheduler.updated["payload"]["task"] == "changed"
    assert scheduler.updated["payload"]["_workspace_id"] == "project-123"
    assert scheduler.updated["payload"]["_workspace_name"] == "Customer portal"
    assert scheduler.updated["payload"]["_template_id"] == "project-risk"


@pytest.mark.asyncio
async def test_explicit_empty_workspace_unbinds_non_project_template() -> None:
    scheduler = _FakeScheduler()
    scheduler.job = CronJob(
        id="job-A",
        handler_key="agent_run",
        payload={
            "kind": AGENT_TURN_KIND,
            "task": "daily brief",
            "agent_id": "main",
            "_workspace_id": "project-123",
            "_workspace_name": "Customer portal",
            "_template_id": "daily-ai-brief",
        },
    )

    await _handle_cron_update(
        {"id": "job-A", "workspaceId": ""},
        RpcContext(conn_id="test", cron_scheduler=scheduler),
    )

    assert scheduler.updated is not None
    assert "_workspace_id" not in scheduler.updated["payload"]
    assert "_workspace_name" not in scheduler.updated["payload"]


def test_job_to_wire_exposes_authoritative_last_status() -> None:
    never_run = _job_to_wire(CronJob(id="never"))
    failed = _job_to_wire(
        CronJob(
            id="failed",
            last_run_at=SimpleNamespace(isoformat=lambda: "2026-07-30T10:00:00+00:00"),
            last_error="boom",
        )
    )

    assert never_run["lastStatus"] is None
    assert failed["lastStatus"] == "error"
    assert failed["last_status"] == "error"


@pytest.mark.asyncio
async def test_rpc_update_tz_only_recomputes_existing_cron_schedule() -> None:
    scheduler = _FakeScheduler()
    scheduler.job = CronJob(
        id="job-A",
        name="orig",
        cron_expr="0 9 * * *",
        schedule_raw="0 9 * * *",
        schedule_kind=ScheduleKind.CRON,
        handler_key="agent_run",
        tz="",
    )

    await _handle_cron_update(
        {
            "id": "job-A",
            "tz": "Asia/Shanghai",
        },
        RpcContext(conn_id="test", cron_scheduler=scheduler),
    )

    assert scheduler.updated is not None
    assert scheduler.updated["schedule_kind"] == ScheduleKind.CRON
    assert scheduler.updated["schedule_value"] == "0 9 * * *"
    assert scheduler.updated["schedule_tz"] == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_rpc_update_tz_only_can_clear_existing_cron_timezone() -> None:
    scheduler = _FakeScheduler()
    scheduler.job = CronJob(
        id="job-A",
        name="orig",
        cron_expr="0 9 * * *",
        schedule_raw="0 9 * * *",
        schedule_kind=ScheduleKind.CRON,
        handler_key="agent_run",
        tz="Asia/Shanghai",
    )

    await _handle_cron_update(
        {
            "id": "job-A",
            "tz": "",
        },
        RpcContext(conn_id="test", cron_scheduler=scheduler),
    )

    assert scheduler.updated is not None
    assert scheduler.updated["schedule_kind"] == ScheduleKind.CRON
    assert scheduler.updated["schedule_value"] == "0 9 * * *"
    assert scheduler.updated["schedule_tz"] == ""


def test_job_to_wire_serializes_normalized_expression() -> None:
    """Direct unit test of the wire mapper: expression must come from cron_expr."""
    job = CronJob(
        id="x",
        name="n",
        cron_expr="*/5 * * * *",
        schedule_raw="每5分钟",  # historical raw text persisted from older versions
        schedule_kind=ScheduleKind.CRON,
        handler_key="agent_run",
    )
    wire = _job_to_wire(job)
    assert wire["expression"] == "*/5 * * * *"
    assert wire["scheduleRaw"] == "每5分钟"
    assert wire["scheduleKind"] == "cron"


def test_job_to_wire_exposes_status_for_cron_countdown_state() -> None:
    job = CronJob(
        id="x",
        name="n",
        cron_expr="60",
        schedule_raw="60",
        schedule_kind=ScheduleKind.EVERY,
        handler_key="agent_run",
        status="running",
    )

    wire = _job_to_wire(job)

    assert wire["status"] == "running"
