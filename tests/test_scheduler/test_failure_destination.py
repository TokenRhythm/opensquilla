"""FailureDestination data layer + RPC wire + delivery routing.

Each cron job can send failure notifications to a different channel/webhook
than the primary delivery via ``DeliveryConfig.failure_destination`` (a
``FailureDestination`` dataclass with mode + channel/to/account or webhook
URL). On a failed run, ``DeliveryChain`` routes to the FD instead of the
primary target — operators can separate alert routing from successful
output. This test file covers the data plumbing through types,
persistence, RPC, and the actual failure-routing decision.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_cron import (
    _cron_create_contract as _handle_cron_add,
)
from opensquilla.gateway.rpc_cron import (
    _job_to_wire,
)
from opensquilla.scheduler.delivery import DeliveryChain
from opensquilla.scheduler.jobs import (
    execute_with_timeout,
    set_failure_dispatcher,
)
from opensquilla.scheduler.ops import SchedulerOps
from opensquilla.scheduler.payloads import (
    AGENT_TURN_KIND,
    make_agent_turn_payload,
)
from opensquilla.scheduler.persistence import JobStore
from opensquilla.scheduler.types import (
    CronJob,
    DeliveryConfig,
    DeliveryMode,
    FailureDestination,
    ScheduleKind,
    SessionTarget,
)

# --- persistence round-trip ------------------------------------------------


async def test_failure_destination_channel_round_trips(tmp_path: Path) -> None:
    db = tmp_path / "cron.db"
    store = JobStore(str(db))
    await store.open()
    try:
        ops = SchedulerOps(store)
        fd = FailureDestination(
            mode=DeliveryMode.CHANNEL,
            channel_name="slack",
            channel_id="C-ops",
            account_id="T1",
        )
        delivery = DeliveryConfig(mode=DeliveryMode.NONE, failure_destination=fd)
        job = await ops.add(
            name="job",
            schedule_kind=ScheduleKind.CRON,
            schedule_value="*/5 * * * *",
            handler_key="agent_run",
            payload=make_agent_turn_payload("x"),
            session_target=SessionTarget.ISOLATED,
            delivery=delivery,
        )
        assert job.delivery.failure_destination is not None
        assert job.delivery.failure_destination.channel_id == "C-ops"

        reloaded = await store.get(job.id)
        assert reloaded is not None
        assert reloaded.delivery.failure_destination is not None
        assert reloaded.delivery.failure_destination.mode == DeliveryMode.CHANNEL
        assert reloaded.delivery.failure_destination.channel_name == "slack"
        assert reloaded.delivery.failure_destination.channel_id == "C-ops"
    finally:
        await store.close()


async def test_failure_destination_webhook_round_trips(tmp_path: Path) -> None:
    db = tmp_path / "cron.db"
    store = JobStore(str(db))
    await store.open()
    try:
        ops = SchedulerOps(store)
        fd = FailureDestination(
            mode=DeliveryMode.WEBHOOK,
            webhook_url="https://hooks.example/alerts",
            webhook_token="ops-secret",
        )
        delivery = DeliveryConfig(
            mode=DeliveryMode.WEBHOOK,
            webhook_url="https://hooks.example/primary",
            failure_destination=fd,
        )
        job = await ops.add(
            name="job",
            schedule_kind=ScheduleKind.CRON,
            schedule_value="*/5 * * * *",
            handler_key="agent_run",
            payload=make_agent_turn_payload("x"),
            session_target=SessionTarget.ISOLATED,
            delivery=delivery,
        )
        reloaded = await store.get(job.id)
        assert reloaded is not None
        assert reloaded.delivery.failure_destination is not None
        assert reloaded.delivery.failure_destination.mode == DeliveryMode.WEBHOOK
        assert (
            reloaded.delivery.failure_destination.webhook_url
            == "https://hooks.example/alerts"
        )
    finally:
        await store.close()


# --- RPC wire ----------------------------------------------------------------


class _FakeScheduler:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    async def add_job(self, **kwargs):
        self.kwargs = kwargs
        return CronJob(
            id="job-fd",
            name=kwargs["name"],
            cron_expr=kwargs.get("schedule_value") or kwargs.get("schedule_raw", ""),
            schedule_raw=kwargs.get("schedule_value") or kwargs.get("schedule_raw", ""),
            handler_key=kwargs["handler_key"],
            payload=kwargs["payload"],
            session_target=kwargs["session_target"],
            session_key=kwargs["session_key"],
            origin_session_key=kwargs["origin_session_key"],
            delivery=kwargs.get("delivery") or DeliveryConfig(),
        )

    async def update_job(self, job_id, **patch):
        return None


async def test_rpc_cron_add_webhook_with_failure_destination_channel() -> None:
    scheduler = _FakeScheduler()
    await _handle_cron_add(
        {
            "name": "report",
            "expression": "0 9 * * *",
            "payloadKind": AGENT_TURN_KIND,
            "text": "x",
            "sessionTarget": "isolated",
            "delivery": {
                "mode": "webhook",
                "webhookUrl": "https://hooks.example/primary",
                "failureDestination": {
                    "mode": "channel",
                    "channelName": "slack",
                    "channelId": "C-ops",
                },
            },
        },
        RpcContext(conn_id="t", cron_scheduler=scheduler),
    )
    delivery = scheduler.kwargs["delivery"]
    assert delivery.failure_destination is not None
    assert delivery.failure_destination.mode == DeliveryMode.CHANNEL
    assert delivery.failure_destination.channel_name == "slack"
    assert delivery.failure_destination.channel_id == "C-ops"


async def test_rpc_cron_add_with_failure_destination_webhook() -> None:
    scheduler = _FakeScheduler()
    await _handle_cron_add(
        {
            "name": "report",
            "expression": "0 9 * * *",
            "payloadKind": AGENT_TURN_KIND,
            "text": "x",
            "sessionTarget": "isolated",
            "delivery": {
                "mode": "webhook",
                "webhookUrl": "https://hooks.example/primary",
                "failureDestination": {
                    "mode": "webhook",
                    "webhookUrl": "https://hooks.example/alerts",
                },
            },
        },
        RpcContext(conn_id="t", cron_scheduler=scheduler),
    )
    fd = scheduler.kwargs["delivery"].failure_destination
    assert fd.mode == DeliveryMode.WEBHOOK
    assert fd.webhook_url == "https://hooks.example/alerts"


async def test_rpc_cron_add_failure_destination_webhook_requires_url() -> None:
    scheduler = _FakeScheduler()
    with pytest.raises(ValueError, match="webhookUrl"):
        await _handle_cron_add(
            {
                "name": "x",
                "expression": "0 9 * * *",
                "payloadKind": AGENT_TURN_KIND,
                "text": "x",
                "sessionTarget": "isolated",
                "delivery": {
                    "mode": "webhook",
                    "webhookUrl": "https://hooks.example/primary",
                    "failureDestination": {"mode": "webhook"},
                },
            },
            RpcContext(conn_id="t", cron_scheduler=scheduler),
        )


def test_job_to_wire_emits_failure_destination() -> None:
    job = CronJob(
        id="job-1",
        name="x",
        cron_expr="0 9 * * *",
        schedule_raw="0 9 * * *",
        handler_key="agent_run",
        payload={"kind": AGENT_TURN_KIND, "task": "x", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
        delivery=DeliveryConfig(
            mode=DeliveryMode.WEBHOOK,
            webhook_url="https://hooks.example/primary",
            failure_destination=FailureDestination(
                mode=DeliveryMode.CHANNEL,
                channel_name="slack",
                channel_id="C-ops",
            ),
        ),
    )
    wire = _job_to_wire(job)
    assert wire["delivery"]["failureDestination"]["mode"] == "channel"
    assert wire["delivery"]["failureDestination"]["channelName"] == "slack"
    assert wire["delivery"]["failureDestination"]["channelId"] == "C-ops"


def test_job_to_wire_omits_failure_destination_when_absent() -> None:
    job = CronJob(
        id="job-1",
        name="x",
        cron_expr="0 9 * * *",
        schedule_raw="0 9 * * *",
        handler_key="agent_run",
        payload={"kind": AGENT_TURN_KIND, "task": "x", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
    )
    wire = _job_to_wire(job)
    # Field present but null is the simpler contract for clients.
    assert wire["delivery"]["failureDestination"] is None


# --- Failure-destination dispatch on run failure -------------------------
#
# The dispatch hook in scheduler.jobs.execute_with_timeout fires for ANY
# failed run regardless of handler_key (agent_run, system_event, timeout,
# generic exception), so the FD wire contract is honored uniformly.


class _RecordingAdapter:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, msg: Any) -> None:
        self.sent.append(msg)


class _RecordingChannelManager:
    def __init__(self) -> None:
        self.adapters: dict[str, _RecordingAdapter] = {}

    def register(self, name: str) -> _RecordingAdapter:
        adapter = _RecordingAdapter()
        self.adapters[name] = adapter
        return adapter

    def get(self, name: str) -> _RecordingAdapter | None:
        return self.adapters.get(name)


class _RecordingHttpxClient:
    posts: list[dict] = []

    def __init__(self, *, timeout: float | None = None, **_kw) -> None:
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url: str, json: dict, headers: dict):
        _RecordingHttpxClient.posts.append(
            {"url": url, "json": json, "headers": headers}
        )

        class _Resp:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

        return _Resp()


def _job_with_delivery(
    *,
    handler_key: str,
    delivery: DeliveryConfig | None,
    timeout_seconds: float = 10.0,
) -> CronJob:
    payload = (
        {"kind": "system_event", "text": "reminder", "agent_id": "main"}
        if handler_key == "system_event"
        else {"kind": AGENT_TURN_KIND, "task": "x", "agent_id": "main"}
    )
    target = (
        SessionTarget.MAIN if handler_key == "system_event" else SessionTarget.ISOLATED
    )
    return CronJob(
        id="job-fd-route",
        name="hook",
        cron_expr="*/5 * * * *",
        handler_key=handler_key,
        payload=payload,
        session_target=target,
        delivery=delivery or DeliveryConfig(),
        timeout_seconds=timeout_seconds,
    )


@pytest.fixture
def fd_dispatcher_hook():
    """Install DeliveryChain.dispatch_failure_alert as the global FD dispatcher."""
    calls: list[tuple[str, str]] = []

    async def _stub(job: CronJob, text: str) -> str:
        calls.append((job.id, text))
        return "delivered"

    set_failure_dispatcher(_stub)
    try:
        yield calls
    finally:
        set_failure_dispatcher(None)


async def test_failure_dispatches_fd_for_agent_run_handler(fd_dispatcher_hook) -> None:
    """A failing agent_run handler triggers the FD dispatcher."""
    job = _job_with_delivery(
        handler_key="agent_run",
        delivery=DeliveryConfig(
            mode=DeliveryMode.CHANNEL,
            channel_name="feishu",
            failure_destination=FailureDestination(
                mode=DeliveryMode.WEBHOOK,
                webhook_url="https://hooks.example/alerts",
            ),
        ),
    )

    async def _failing_handler(job: CronJob):
        raise RuntimeError("agent crashed: 401")

    execution = await execute_with_timeout(job, _failing_handler)

    assert execution.success is False
    assert fd_dispatcher_hook == [("job-fd-route", "agent crashed: 401")]


async def test_failure_dispatches_fd_for_system_event_handler(fd_dispatcher_hook) -> None:
    """A failing system_event handler also triggers the FD dispatcher —
    the dispatch is handler-agnostic, so non-agent failures are covered."""
    job = _job_with_delivery(
        handler_key="system_event",
        delivery=DeliveryConfig(
            mode=DeliveryMode.NONE,
            failure_destination=FailureDestination(
                mode=DeliveryMode.CHANNEL,
                channel_name="slack",
                channel_id="C-ops",
            ),
        ),
    )

    async def _failing_handler(job: CronJob):
        raise RuntimeError("heartbeat failed")

    execution = await execute_with_timeout(job, _failing_handler)

    assert execution.success is False
    assert fd_dispatcher_hook == [("job-fd-route", "heartbeat failed")]


async def test_failure_dispatches_fd_on_timeout(fd_dispatcher_hook) -> None:
    """A handler timeout also routes through the FD dispatcher."""
    import asyncio

    job = _job_with_delivery(
        handler_key="agent_run",
        delivery=DeliveryConfig(
            failure_destination=FailureDestination(
                mode=DeliveryMode.WEBHOOK,
                webhook_url="https://hooks.example/alerts",
            ),
        ),
        timeout_seconds=0.05,
    )

    async def _slow_handler(job: CronJob):
        await asyncio.sleep(5.0)

    execution = await execute_with_timeout(job, _slow_handler)

    assert execution.success is False
    assert "Timeout" in (execution.error or "")
    assert len(fd_dispatcher_hook) == 1
    assert fd_dispatcher_hook[0][0] == "job-fd-route"


async def test_success_does_not_dispatch_fd(fd_dispatcher_hook) -> None:
    """Successful runs never invoke the FD dispatcher."""
    job = _job_with_delivery(
        handler_key="agent_run",
        delivery=DeliveryConfig(
            failure_destination=FailureDestination(
                mode=DeliveryMode.WEBHOOK,
                webhook_url="https://hooks.example/alerts",
            ),
        ),
    )

    async def _ok_handler(job: CronJob):
        return "all good"

    execution = await execute_with_timeout(job, _ok_handler)

    assert execution.success is True
    assert fd_dispatcher_hook == []


async def test_failure_without_failure_destination_skips_dispatch(
    fd_dispatcher_hook,
) -> None:
    """A failed run with no FD configured does NOT invoke the dispatcher."""
    job = _job_with_delivery(
        handler_key="agent_run",
        delivery=DeliveryConfig(failure_destination=None),
    )

    async def _failing_handler(job: CronJob):
        raise RuntimeError("oops")

    execution = await execute_with_timeout(job, _failing_handler)

    assert execution.success is False
    assert fd_dispatcher_hook == []


async def test_dispatcher_exception_does_not_break_state_machine() -> None:
    """A crashing FD dispatcher must not corrupt the execution record."""

    async def _boom(job: CronJob, text: str):
        raise RuntimeError("dispatcher exploded")

    set_failure_dispatcher(_boom)
    try:
        job = _job_with_delivery(
            handler_key="agent_run",
            delivery=DeliveryConfig(
                failure_destination=FailureDestination(
                    mode=DeliveryMode.WEBHOOK,
                    webhook_url="https://hooks.example/alerts",
                ),
            ),
        )

        async def _failing_handler(job: CronJob):
            raise RuntimeError("real failure")

        execution = await execute_with_timeout(job, _failing_handler)

        # Original failure preserved; dispatcher crash swallowed.
        assert execution.success is False
        assert "real failure" in (execution.error or "")
    finally:
        set_failure_dispatcher(None)


# --- DeliveryChain.dispatch_failure_alert (the wired implementation) -----


async def test_dispatch_failure_alert_via_webhook(monkeypatch) -> None:
    _RecordingHttpxClient.posts.clear()
    monkeypatch.setitem(
        sys.modules, "httpx", type("M", (), {"AsyncClient": _RecordingHttpxClient})
    )

    chain = DeliveryChain()
    job = _job_with_delivery(
        handler_key="agent_run",
        delivery=DeliveryConfig(
            failure_destination=FailureDestination(
                mode=DeliveryMode.WEBHOOK,
                webhook_url="https://hooks.example/alerts",
                webhook_token="ops-secret",
            ),
        ),
    )

    status = await chain.dispatch_failure_alert(job, "agent failed: timeout")

    assert status == "delivered"
    assert len(_RecordingHttpxClient.posts) == 1
    post = _RecordingHttpxClient.posts[-1]
    assert post["url"] == "https://hooks.example/alerts"
    assert post["headers"]["Authorization"] == "Bearer ops-secret"
    assert post["json"]["summary"] == "agent failed: timeout"


async def test_dispatch_failure_alert_via_channel() -> None:
    cm = _RecordingChannelManager()
    cm.register("slack")
    chain = DeliveryChain(channel_manager_ref=lambda: cm)

    job = _job_with_delivery(
        handler_key="system_event",
        delivery=DeliveryConfig(
            failure_destination=FailureDestination(
                mode=DeliveryMode.CHANNEL,
                channel_name="slack",
                channel_id="C-ops",
            ),
        ),
    )

    status = await chain.dispatch_failure_alert(job, "heartbeat failed")

    assert status == "delivered"
    assert len(cm.adapters["slack"].sent) == 1
    assert cm.adapters["slack"].sent[-1].content == "heartbeat failed"


async def test_dispatch_failure_alert_no_fd_is_skip() -> None:
    chain = DeliveryChain()
    job = _job_with_delivery(handler_key="agent_run", delivery=DeliveryConfig())
    assert await chain.dispatch_failure_alert(job, "x") == "skipped"


# --- Gateway boot wiring contract ---------------------------------------


async def test_set_failure_dispatcher_with_delivery_chain_drives_end_to_end() -> None:
    """Locks the contract gateway boot relies on: registering
    ``DeliveryChain.dispatch_failure_alert`` via ``set_failure_dispatcher``
    causes any failed cron run to land on the configured FailureDestination.
    Regression guard for the boot-time wire in gateway/boot.py."""
    cm = _RecordingChannelManager()
    cm.register("slack")
    chain = DeliveryChain(channel_manager_ref=lambda: cm)

    set_failure_dispatcher(chain.dispatch_failure_alert)
    try:
        job = _job_with_delivery(
            handler_key="system_event",
            delivery=DeliveryConfig(
                failure_destination=FailureDestination(
                    mode=DeliveryMode.CHANNEL,
                    channel_name="slack",
                    channel_id="C-ops",
                ),
            ),
        )

        async def _failing_handler(job: CronJob):
            raise RuntimeError("heartbeat dead")

        execution = await execute_with_timeout(job, _failing_handler)

        assert execution.success is False
        # End-to-end: hook → DeliveryChain.dispatch_failure_alert →
        # _post_to_channel → adapter.send. The slack FD adapter sees the alert.
        assert len(cm.adapters["slack"].sent) == 1
        assert cm.adapters["slack"].sent[-1].content == "heartbeat dead"
    finally:
        set_failure_dispatcher(None)
