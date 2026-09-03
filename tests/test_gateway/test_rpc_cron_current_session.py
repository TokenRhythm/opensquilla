import asyncio
from types import SimpleNamespace

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_cron import (
    _build_payload,
    _handle_cron_add,
    _handle_cron_update,
    _resolve_origin_session_key,
    _resolve_session_target,
    _resolve_target_session_key,
)
from opensquilla.scheduler.delivery import DeliveryChain
from opensquilla.scheduler.handlers import (
    _resolve_session_key,
    make_agent_run_handler,
    make_static_message_handler,
)
from opensquilla.scheduler.payloads import AGENT_TURN_KIND, REMINDER_KIND, SYSTEM_EVENT_KIND
from opensquilla.scheduler.types import (
    CronJob,
    DeliveryConfig,
    DeliveryMode,
    ReplyTargetSnapshot,
    SessionTarget,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage, StaleEpochError

SESSION_KEY = "agent:main:webchat:abc123"
CRON_SESSION_KEY = "cron:drink:run:def456"


async def _record_async(target: list, value) -> None:
    target.append(value)


class _FakeScheduler:
    def __init__(self, job: CronJob | None = None) -> None:
        self.added = None
        self.updated = None
        self.job = job

    async def add_job(self, **kwargs) -> CronJob:
        self.added = kwargs
        return CronJob(
            id="drink",
            name=kwargs["name"],
            cron_expr=kwargs.get("schedule_value") or kwargs.get("schedule_raw", ""),
            schedule_raw=kwargs.get("schedule_value") or kwargs.get("schedule_raw", ""),
            handler_key=kwargs["handler_key"],
            payload=kwargs["payload"],
            session_target=kwargs["session_target"],
            session_key=kwargs["session_key"],
            origin_session_key=kwargs["origin_session_key"],
            delivery=kwargs.get("delivery") or DeliveryConfig(),
            tool_policy=kwargs.get("tool_policy") or {},
            creator_is_owner=bool(kwargs.get("creator_is_owner", False)),
            creator_host_execute=bool(kwargs.get("creator_host_execute", False)),
        )

    async def update_job(self, job_id, **patch) -> CronJob:
        self.updated = patch
        if self.job is None:
            return CronJob(id=job_id, **patch)
        for key, value in patch.items():
            setattr(self.job, key, value)
        return self.job

    async def get_job(self, job_id) -> CronJob | None:
        if self.job is not None and self.job.id == job_id:
            return self.job
        return None


class _FakeSessionManager:
    def __init__(self) -> None:
        self.created = []
        self.rows = {}
        self._storage = SimpleNamespace(bind_session_workspace=self._bind_session_workspace)
        self.workspace_bindings = []

    async def _bind_session_workspace(self, session_key, workspace_id):
        self.workspace_bindings.append((session_key, workspace_id))

    async def get_or_create(self, **kwargs):
        self.created.append(kwargs)
        return kwargs

    async def append_message(self, session_key, role, content, provenance=None):
        row = {"role": role, "content": content}
        if provenance is not None:
            row["provenance"] = provenance
        self.rows.setdefault(session_key, []).append(row)
        return SimpleNamespace(
            role=role,
            content=content,
            message_id=f"message-{len(self.rows[session_key])}",
        )

    async def read_transcript(self, session_key):
        return list(self.rows.get(session_key, []))


class _FakeTurnRunner:
    def __init__(
        self,
        session_manager: _FakeSessionManager,
        text: str = "drink logged",
        *,
        events: list[object] | None = None,
    ) -> None:
        self.session_manager = session_manager
        self.text = text
        self.events = events
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)

        async def events():
            await self.session_manager.append_message(
                kwargs["session_key"],
                role="assistant",
                content=self.text,
            )
            if self.events is not None:
                for event in self.events:
                    yield event
                return
            yield SimpleNamespace(kind="message", text=self.text)
            yield SimpleNamespace(kind="done")

        return events()


@pytest.mark.asyncio
async def test_agent_run_binds_the_isolated_session_to_the_job_workspace() -> None:
    session_manager = _FakeSessionManager()
    turn_runner = _FakeTurnRunner(session_manager)
    job = CronJob(
        id="project-check",
        name="Project check",
        handler_key="agent_run",
        payload={
            "kind": AGENT_TURN_KIND,
            "task": "inspect the project",
            "agent_id": "main",
            "_workspace_id": "project-123",
        },
        session_target=SessionTarget.ISOLATED,
    )
    handler = make_agent_run_handler(
        DeliveryChain(),
        turn_runner_ref=lambda: turn_runner,
        session_manager_ref=lambda: session_manager,
    )

    result = await handler(job)

    assert session_manager.workspace_bindings == [(result.session_key, "project-123")]


@pytest.mark.asyncio
async def test_agent_run_workspace_binding_rejects_reset_owner(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "cron-workspace-owner.db"))

    class RotatingSessionManager(_FakeSessionManager):
        def __init__(self) -> None:
            super().__init__()
            self.admitted = SessionNode(
                session_key=SESSION_KEY,
                session_id="cron-workspace-owner-old",
                epoch=4,
            )
            self.bind_owner = None
            self._storage = SimpleNamespace(
                bind_session_workspace=self._bind_rotating_workspace,
            )

        async def get_or_create(self, **kwargs):
            self.created.append(kwargs)
            return self.admitted, False

        async def _bind_rotating_workspace(
            self,
            session_key,
            workspace_id,
            *,
            expected_session_id=None,
            expected_session_epoch=None,
        ):
            self.bind_owner = (expected_session_id, expected_session_epoch)
            await storage.upsert_session(
                SessionNode(
                    session_key=session_key,
                    session_id="cron-workspace-owner-new",
                    epoch=5,
                )
            )
            await storage.bind_session_workspace(
                session_key,
                workspace_id,
                expected_session_id=expected_session_id,
                expected_session_epoch=expected_session_epoch,
            )

    session_manager = RotatingSessionManager()
    await storage.upsert_session(session_manager.admitted)
    turn_runner = _FakeTurnRunner(session_manager)
    job = CronJob(
        id="project-owner-race",
        name="Project owner race",
        handler_key="agent_run",
        payload={
            "kind": AGENT_TURN_KIND,
            "task": "inspect the project",
            "agent_id": "main",
            "_workspace_id": "project-123",
        },
        session_target=SessionTarget.CURRENT,
        session_key=SESSION_KEY,
    )
    handler = make_agent_run_handler(
        DeliveryChain(),
        turn_runner_ref=lambda: turn_runner,
        session_manager_ref=lambda: session_manager,
    )

    try:
        with pytest.raises(StaleEpochError, match="cron-workspace-owner-old@4"):
            await handler(job)

        current = await storage.get_session(SESSION_KEY)
        assert session_manager.bind_owner == ("cron-workspace-owner-old", 4)
        assert current is not None
        assert (current.session_id, current.epoch, current.workspace_id) == (
            "cron-workspace-owner-new",
            5,
            None,
        )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_agent_run_workspace_binding_rejects_kwargs_only_proxy() -> None:
    class KwargsOnlySessionManager(_FakeSessionManager):
        def __init__(self) -> None:
            super().__init__()
            self._storage = SimpleNamespace(
                bind_session_workspace=self._bind_without_cas,
            )

        async def get_or_create(self, **kwargs):
            self.created.append(kwargs)
            return SimpleNamespace(session_id="cron-owner", epoch=2), False

        async def _bind_without_cas(self, *args, **kwargs):
            raise AssertionError("kwargs-only proxy must not receive an owner write")

    session_manager = KwargsOnlySessionManager()
    job = CronJob(
        id="project-owner-proxy",
        name="Project owner proxy",
        handler_key="agent_run",
        payload={
            "kind": AGENT_TURN_KIND,
            "task": "inspect the project",
            "agent_id": "main",
            "_workspace_id": "project-123",
        },
        session_target=SessionTarget.CURRENT,
        session_key=SESSION_KEY,
    )
    handler = make_agent_run_handler(
        DeliveryChain(),
        turn_runner_ref=lambda: _FakeTurnRunner(session_manager),
        session_manager_ref=lambda: session_manager,
    )

    with pytest.raises(RuntimeError, match="cannot enforce the admitted session owner"):
        await handler(job)


@pytest.mark.asyncio
async def test_agent_run_rejects_dropping_runner_for_modern_owner() -> None:
    class ModernSessionManager(_FakeSessionManager):
        def __init__(self) -> None:
            super().__init__()
            self.append_owner = None

        async def get_or_create(self, **kwargs):
            self.created.append(kwargs)
            return SimpleNamespace(session_id="cron-modern-owner", epoch=3), False

        async def append_message(
            self,
            session_key,
            role,
            content,
            provenance=None,
            *,
            expected_session_id=None,
            expected_session_epoch=None,
        ):
            self.append_owner = (expected_session_id, expected_session_epoch)
            return await super().append_message(
                session_key,
                role,
                content,
                provenance=provenance,
            )

    class DroppingTurnRunner:
        def __init__(self) -> None:
            self.calls = []

        def run(self, **kwargs):
            self.calls.append(kwargs)

            async def events():
                yield SimpleNamespace(kind="done")

            return events()

    session_manager = ModernSessionManager()
    runner = DroppingTurnRunner()
    job = CronJob(
        id="modern-owner-runner-proxy",
        name="Modern owner runner proxy",
        handler_key="agent_run",
        payload={"kind": AGENT_TURN_KIND, "task": "stay fenced", "agent_id": "main"},
        session_target=SessionTarget.CURRENT,
        session_key=SESSION_KEY,
    )
    handler = make_agent_run_handler(
        DeliveryChain(),
        turn_runner_ref=lambda: runner,
        session_manager_ref=lambda: session_manager,
    )

    with pytest.raises(RuntimeError, match="cannot enforce the admitted session owner"):
        await handler(job)

    assert session_manager.append_owner == ("cron-modern-owner", 3)
    assert runner.calls == []


@pytest.mark.asyncio
async def test_agent_run_rejects_dropping_writer_for_modern_owner() -> None:
    class DroppingSessionManager(_FakeSessionManager):
        def __init__(self) -> None:
            super().__init__()
            self.append_calls = []

        async def get_or_create(self, **kwargs):
            self.created.append(kwargs)
            return SimpleNamespace(session_id="cron-modern-owner", epoch=3), False

        async def append_message(self, *args, **kwargs):
            self.append_calls.append((args, kwargs))
            return await super().append_message(*args, **kwargs)

    session_manager = DroppingSessionManager()
    runner = _FakeTurnRunner(session_manager)
    job = CronJob(
        id="modern-owner-writer-proxy",
        name="Modern owner writer proxy",
        handler_key="agent_run",
        payload={"kind": AGENT_TURN_KIND, "task": "stay fenced", "agent_id": "main"},
        session_target=SessionTarget.CURRENT,
        session_key=SESSION_KEY,
    )
    handler = make_agent_run_handler(
        DeliveryChain(),
        turn_runner_ref=lambda: runner,
        session_manager_ref=lambda: session_manager,
    )

    with pytest.raises(RuntimeError, match="writer cannot enforce the admitted session owner"):
        await handler(job)

    assert session_manager.append_calls == []
    assert runner.calls == []


class _FakeTaskRuntime:
    def __init__(self, record) -> None:
        self.record = record
        self.enqueued = []

    async def enqueue(
        self,
        route_envelope,
        task,
        *,
        mode,
        run_kind,
        persisted_user_message_id=None,
    ):
        self.enqueued.append(
            {
                "route_envelope": route_envelope,
                "task": task,
                "mode": mode,
                "run_kind": run_kind,
                "persisted_user_message_id": persisted_user_message_id,
            }
        )
        return SimpleNamespace(task_id="task-1")

    async def wait(self, task_id, *, timeout):
        assert task_id == "task-1"
        return self.record


class _RejectingTaskRuntime(_FakeTaskRuntime):
    async def validate_acceptance(self, route_envelope, accepted_run_mode_override=None):
        del route_envelope, accepted_run_mode_override
        raise RuntimeError("sandbox_unavailable")


class _RecordingDeliveryChain:
    def __init__(self) -> None:
        self.deliveries = []

    async def notify_start(self, job, task) -> None:
        return None

    async def deliver(self, job, **kwargs):
        kwargs["job"] = job
        self.deliveries.append(kwargs)
        return SimpleNamespace(
            channel_status="skipped",
            ws_status="skipped",
            session_status="skipped",
        )


class _FailingChannelAdapter:
    async def send(self, _msg) -> None:
        raise RuntimeError("channel down")


class _RecordingChannelAdapter:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, msg) -> None:
        self.sent.append(msg)


class _FakeChannelManager:
    def get(self, _name: str):
        return _FailingChannelAdapter()


class _RecordingChannelManager:
    def __init__(self) -> None:
        self.adapter = _RecordingChannelAdapter()

    def get(self, _name: str):
        return self.adapter


def test_rpc_current_session_params_bind_target_and_origin_session() -> None:
    params = {
        "payloadKind": AGENT_TURN_KIND,
        "sessionTarget": "current",
        "sessionKey": SESSION_KEY,
        "text": "drink water",
        "agentId": "main",
    }

    session_target = _resolve_session_target(params)
    kind, payload = _build_payload(params, session_target)

    assert session_target == SessionTarget.CURRENT
    assert _resolve_target_session_key(params, session_target) == SESSION_KEY
    assert _resolve_origin_session_key(params, session_target) == SESSION_KEY
    assert kind == AGENT_TURN_KIND
    assert payload == {
        "kind": AGENT_TURN_KIND,
        "task": "drink water",
        "agent_id": "main",
    }


@pytest.mark.asyncio
async def test_rpc_create_current_session_job_passes_session_binding_to_scheduler() -> None:
    scheduler = _FakeScheduler()

    result = await _handle_cron_add(
        {
            "name": "Drink",
            "expression": "*/5 * * * *",
            "payloadKind": AGENT_TURN_KIND,
            "sessionTarget": "current",
            "sessionKey": SESSION_KEY,
            "originSessionKey": SESSION_KEY,
            "text": "drink water",
            "agentId": "main",
        },
        RpcContext(conn_id="test", cron_scheduler=scheduler),
    )

    assert scheduler.added["session_target"] == SessionTarget.CURRENT
    assert scheduler.added["session_key"] == SESSION_KEY
    assert scheduler.added["origin_session_key"] == SESSION_KEY
    assert scheduler.added["handler_key"] == "agent_run"
    assert scheduler.added["creator_is_owner"] is True
    assert result["sessionTarget"] == "current"
    assert result["targetSessionKey"] == SESSION_KEY
    assert result["originSessionKey"] == SESSION_KEY


@pytest.mark.parametrize(
    (
        "stored_mode",
        "capabilities",
        "is_owner",
        "expected_mode",
        "expected_host_execute",
    ),
    [
        pytest.param(
            "safe", frozenset(), True, "safe", True, id="owner-stored-safe"
        ),
        pytest.param(
            "full",
            frozenset({"task.read"}),
            False,
            "safe",
            False,
            id="admin-without-host",
        ),
        pytest.param(
            "full",
            frozenset({"host.execute"}),
            False,
            "full",
            True,
            id="named-host-token",
        ),
        pytest.param(None, frozenset(), True, "full", True, id="fresh-owner"),
    ],
)
@pytest.mark.asyncio
async def test_rpc_create_background_job_resolves_persisted_mode_for_principal(
    tmp_path,
    stored_mode: str | None,
    capabilities: frozenset[str],
    is_owner: bool,
    expected_mode: str,
    expected_host_execute: bool,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "cron-preference.db"))
    if stored_mode is not None:
        await storage.set_runtime_preference("sandbox.run_mode", stored_mode)
    manager = SessionManager(storage, inject_time_prefix=False)
    scheduler = _FakeScheduler()
    principal = Principal(
        role="operator",
        scopes=frozenset({"operator.admin"}),
        is_owner=is_owner,
        authenticated=True,
        capabilities=capabilities,
        auth_state="authenticated",
        token_public_id=None if is_owner else "named-token",
    )

    try:
        await _handle_cron_add(
            {
                "name": "Background",
                "expression": "*/5 * * * *",
                "payloadKind": AGENT_TURN_KIND,
                "text": "check status",
                "agentId": "main",
            },
            RpcContext(
                conn_id="test",
                cron_scheduler=scheduler,
                session_manager=manager,
                config=GatewayConfig(),
                principal=principal,
            ),
        )
    finally:
        await storage.close()

    assert scheduler.added["run_mode"] == expected_mode
    assert scheduler.added["creator_is_owner"] is is_owner
    assert scheduler.added["creator_host_execute"] is expected_host_execute


@pytest.mark.asyncio
async def test_rpc_create_job_round_trips_tool_policy() -> None:
    scheduler = _FakeScheduler()

    result = await _handle_cron_add(
        {
            "name": "Drink",
            "expression": "*/5 * * * *",
            "payloadKind": AGENT_TURN_KIND,
            "text": "drink water",
            "agentId": "main",
            "toolPolicy": {
                "profile": "minimal",
                "alsoAllow": ["memory_search"],
                "deny": ["web_fetch"],
            },
        },
        RpcContext(conn_id="test", cron_scheduler=scheduler),
    )

    assert scheduler.added["tool_policy"] == {
        "profile": "minimal",
        "also_allow": ["memory_search"],
        "deny": ["web_fetch"],
    }
    assert result["toolPolicy"] == {
        "profile": "minimal",
        "allow": [],
        "alsoAllow": ["memory_search"],
        "deny": ["web_fetch"],
    }


@pytest.mark.asyncio
async def test_rpc_update_current_session_job_preserves_existing_binding() -> None:
    current_job = CronJob(
        id="drink",
        name="Drink",
        handler_key="agent_run",
        payload={"kind": AGENT_TURN_KIND, "task": "drink water", "agent_id": "main"},
        session_target=SessionTarget.CURRENT,
        session_key=SESSION_KEY,
        origin_session_key=SESSION_KEY,
    )
    scheduler = _FakeScheduler(job=current_job)

    result = await _handle_cron_update(
        {
            "id": "drink",
            "text": "drink more water",
        },
        RpcContext(conn_id="test", cron_scheduler=scheduler),
    )

    assert scheduler.updated["session_target"] == SessionTarget.CURRENT
    assert scheduler.updated["session_key"] == SESSION_KEY
    assert scheduler.updated["origin_session_key"] == SESSION_KEY
    assert result["sessionTarget"] == "current"
    assert result["targetSessionKey"] == SESSION_KEY
    assert result["originSessionKey"] == SESSION_KEY
    assert result["prompt"] == "drink more water"


@pytest.mark.asyncio
async def test_rpc_update_job_round_trips_tool_policy() -> None:
    current_job = CronJob(
        id="drink",
        name="Drink",
        handler_key="agent_run",
        payload={"kind": AGENT_TURN_KIND, "task": "drink water", "agent_id": "main"},
    )
    scheduler = _FakeScheduler(job=current_job)

    result = await _handle_cron_update(
        {
            "id": "drink",
            "toolPolicy": {
                "profile": "minimal",
                "alsoAllow": ["memory_search"],
                "deny": ["web_fetch"],
            },
        },
        RpcContext(conn_id="test", cron_scheduler=scheduler),
    )

    assert scheduler.updated["tool_policy"] == {
        "profile": "minimal",
        "also_allow": ["memory_search"],
        "deny": ["web_fetch"],
    }
    assert current_job.tool_policy == scheduler.updated["tool_policy"]
    assert result["toolPolicy"]["alsoAllow"] == ["memory_search"]
    assert result["toolPolicy"]["deny"] == ["web_fetch"]


def test_rpc_keeps_system_event_main_only() -> None:
    params = {
        "payloadKind": SYSTEM_EVENT_KIND,
        "sessionTarget": "current",
        "sessionKey": SESSION_KEY,
        "text": "drink water",
    }

    with pytest.raises(ValueError, match="system_event.*main"):
        _build_payload(params, SessionTarget.CURRENT)


def test_rpc_rejects_agent_turn_on_main_session() -> None:
    params = {
        "payloadKind": AGENT_TURN_KIND,
        "sessionTarget": "main",
        "text": "drink water",
    }

    with pytest.raises(ValueError, match="agent_turn.*main"):
        _build_payload(params, SessionTarget.MAIN)


def test_rpc_defaults_non_main_payload_to_static_reminder() -> None:
    kind, payload = _build_payload({"text": "drink water"}, SessionTarget.ISOLATED)

    assert kind == REMINDER_KIND
    assert payload == {
        "kind": REMINDER_KIND,
        "text": "drink water",
        "agent_id": "main",
    }


def test_rpc_rejects_reminder_on_main_session() -> None:
    params = {
        "payloadKind": REMINDER_KIND,
        "sessionTarget": "main",
        "text": "drink water",
    }

    with pytest.raises(ValueError, match="reminder.*main"):
        _build_payload(params, SessionTarget.MAIN)


def test_scheduler_current_session_resolves_bound_session_key() -> None:
    job = CronJob(
        id="drink",
        name="Drink",
        session_target=SessionTarget.CURRENT,
        session_key=SESSION_KEY,
    )

    assert _resolve_session_key(job) == SESSION_KEY


def test_scheduler_current_session_falls_back_to_origin_session_key() -> None:
    job = CronJob(
        id="drink",
        name="Drink",
        session_target=SessionTarget.CURRENT,
        origin_session_key=SESSION_KEY,
    )

    assert _resolve_session_key(job) == SESSION_KEY


def test_scheduler_current_session_requires_a_bound_key() -> None:
    job = CronJob(id="drink", name="Drink", session_target=SessionTarget.CURRENT)

    with pytest.raises(ValueError, match="CURRENT target requires"):
        _resolve_session_key(job)


def test_delivery_skips_same_session_forward_for_current_session_jobs() -> None:
    calls = []

    async def forwarder(**kwargs) -> None:
        calls.append(kwargs)

    job = CronJob(
        id="drink",
        name="Drink",
        session_target=SessionTarget.CURRENT,
        session_key=SESSION_KEY,
        origin_session_key=SESSION_KEY,
    )
    chain = DeliveryChain(session_forwarder=forwarder)

    status = asyncio.run(chain._forward_to_session(job, "done", SESSION_KEY))

    assert status == "skipped"
    assert calls == []


def test_delivery_forwards_isolated_job_results_to_origin_session() -> None:
    calls = []

    async def forwarder(**kwargs) -> None:
        calls.append(kwargs)

    job = CronJob(
        id="drink",
        name="Drink",
        session_target=SessionTarget.ISOLATED,
        session_key=CRON_SESSION_KEY,
        origin_session_key=SESSION_KEY,
    )
    chain = DeliveryChain(session_forwarder=forwarder)

    status = asyncio.run(chain._forward_to_session(job, "done", CRON_SESSION_KEY))

    assert status == "delivered"
    assert calls == [
        {
            "origin_session_key": SESSION_KEY,
            "text": "done",
            "provenance": {
                "kind": "cron",
                "source_session_key": CRON_SESSION_KEY,
                "source_tool": "cron:drink",
            },
        }
    ]
    assert job.delivery.mode == DeliveryMode.NONE


def test_delivery_sanitizes_reply_directives_across_cron_outputs() -> None:
    forward_calls = []
    ws_events = []

    async def forwarder(**kwargs) -> None:
        forward_calls.append(kwargs)

    async def ws_emitter(topic, event, payload) -> int:
        ws_events.append((topic, event, payload))
        return 1

    cm = _RecordingChannelManager()
    job = CronJob(
        id="poem",
        name="Poem",
        session_target=SessionTarget.ISOLATED,
        session_key=CRON_SESSION_KEY,
        origin_session_key=SESSION_KEY,
        delivery=DeliveryConfig(
            mode=DeliveryMode.CHANNEL,
            channel_name="feishu",
            channel_id="oc_chat",
            ws_topic="cron:poem",
        ),
    )
    chain = DeliveryChain(
        channel_manager_ref=lambda: cm,
        ws_emitter=ws_emitter,
        session_forwarder=forwarder,
    )

    report = asyncio.run(
        chain.deliver(
            job,
            result_text="[[reply_to_current]]Here is the scheduled reply",
            success=True,
            summary="[[reply_to_current]]Here is the scheduled reply",
            session_key=CRON_SESSION_KEY,
        )
    )
    asyncio.run(
        chain.notify_finished(
            job,
            success=True,
            summary="[[reply_to_current]]Here is the scheduled reply",
            session_key=CRON_SESSION_KEY,
            run_id="run-1",
        )
    )

    assert report.channel_status == "delivered"
    assert report.ws_status == "skipped"
    assert report.session_status == "skipped"
    assert cm.adapter.sent[0].content == "Here is the scheduled reply"
    assert ws_events[0][2]["summary"] == "Here is the scheduled reply"
    assert ws_events[0][2]["payloadKind"] == AGENT_TURN_KIND
    assert ws_events[0][2]["runId"] == "run-1"
    assert forward_calls == []

    forward_job = CronJob(
        id="forward-poem",
        name="Forward Poem",
        session_target=SessionTarget.ISOLATED,
        session_key=CRON_SESSION_KEY,
        origin_session_key=SESSION_KEY,
        delivery=DeliveryConfig(mode=DeliveryMode.NONE),
    )
    forward_status = asyncio.run(
        chain._forward_to_session(
            forward_job,
            "[[reply_to_current]]Here is the scheduled reply",
            CRON_SESSION_KEY,
        )
    )

    assert forward_status == "delivered"
    assert forward_calls[0]["text"] == "Here is the scheduled reply"


@pytest.mark.asyncio
async def test_current_session_agent_run_uses_bound_session_transcript_without_forwarding() -> None:
    session_manager = _FakeSessionManager()
    turn_runner = _FakeTurnRunner(session_manager)
    forward_calls = []

    async def forwarder(**kwargs) -> None:
        forward_calls.append(kwargs)

    job = CronJob(
        id="drink",
        name="Drink",
        handler_key="agent_run",
        payload={"kind": AGENT_TURN_KIND, "task": "drink water", "agent_id": "main"},
        session_target=SessionTarget.CURRENT,
        session_key=SESSION_KEY,
        origin_session_key=SESSION_KEY,
        tool_policy={
            "profile": "minimal",
            "also_allow": ["memory_search", "exec_command"],
            "deny": ["web_fetch"],
        },
    )
    handler = make_agent_run_handler(
        DeliveryChain(session_forwarder=forwarder),
        turn_runner_ref=lambda: turn_runner,
        session_manager_ref=lambda: session_manager,
    )

    result = await handler(job)

    assert result.session_key == SESSION_KEY
    assert result.summary == "drink logged"
    assert result.delivery_status == "skipped|ws:skipped|fwd:skipped"
    assert session_manager.created == [
        {
            "session_key": SESSION_KEY,
            "agent_id": "main",
            "display_name": "Cron: Drink",
        }
    ]
    assert turn_runner.calls[0]["session_key"] == SESSION_KEY
    assert turn_runner.calls[0]["run_kind"] == "cron_turn"
    assert turn_runner.calls[0]["input_provenance"] == {
        "kind": "cron_job",
        "job_id": "drink",
    }
    tool_context = turn_runner.calls[0]["tool_context"]
    assert tool_context.allowed_tools == {"session_status"}
    assert "exec_command" in tool_context.denied_tools
    assert "web_fetch" in tool_context.denied_tools
    assert await session_manager.read_transcript(SESSION_KEY) == [
        {"role": "user", "content": "drink water"},
        {"role": "assistant", "content": "drink logged"},
    ]
    assert forward_calls == []


@pytest.mark.asyncio
async def test_cron_safe_admission_failure_happens_before_session_or_message_persistence() -> None:
    session_manager = _FakeSessionManager()
    task_runtime = _RejectingTaskRuntime(SimpleNamespace(status="failed"))
    job = CronJob(
        id="safe-unavailable",
        name="Safe unavailable",
        handler_key="agent_run",
        payload={"kind": AGENT_TURN_KIND, "task": "do not accept", "agent_id": "main"},
        session_target=SessionTarget.CURRENT,
        session_key=SESSION_KEY,
        run_mode="safe",
    )
    handler = make_agent_run_handler(
        DeliveryChain(),
        task_runtime_ref=lambda: task_runtime,
        session_manager_ref=lambda: session_manager,
    )

    with pytest.raises(RuntimeError, match="sandbox_unavailable"):
        await handler(job)

    assert session_manager.created == []
    assert await session_manager.read_transcript(SESSION_KEY) == []
    assert task_runtime.enqueued == []


@pytest.mark.asyncio
async def test_cron_runtime_envelope_freezes_owner_across_persist_enqueue_reset() -> None:
    class RotatingSessionManager(_FakeSessionManager):
        def __init__(self) -> None:
            super().__init__()
            self.admitted = SimpleNamespace(session_id="cron-owner-old", epoch=4)
            self.current = self.admitted
            self.append_owner = None

        async def get_or_create(self, **kwargs):
            self.created.append(kwargs)
            return self.admitted, False

        async def append_message(
            self,
            session_key,
            role,
            content,
            provenance=None,
            *,
            expected_session_id=None,
            expected_session_epoch=None,
        ):
            self.append_owner = (expected_session_id, expected_session_epoch)
            persisted = await super().append_message(
                session_key,
                role,
                content,
                provenance=provenance,
            )
            # Deterministically model reset after old-owner persistence but
            # before the handler reaches TaskRuntime.enqueue.
            self.current = SimpleNamespace(session_id="cron-owner-new", epoch=5)
            return persisted

    class ResettingTaskRuntime(_FakeTaskRuntime):
        async def wait(self, task_id, *, timeout):
            session_manager.rows[SESSION_KEY] = [
                {"role": "user", "content": "replacement prompt"},
                {"role": "assistant", "content": "replacement owner result"},
            ]
            return await super().wait(task_id, timeout=timeout)

    session_manager = RotatingSessionManager()
    task_runtime = ResettingTaskRuntime(
        SimpleNamespace(
            status="succeeded",
            details={
                "session_id": "cron-owner-old",
                "session_epoch": 4,
                "terminal_assistant_message_content": "admitted owner result",
            },
        )
    )
    job = CronJob(
        id="owner-race",
        name="Owner race",
        handler_key="agent_run",
        payload={"kind": AGENT_TURN_KIND, "task": "fenced task", "agent_id": "main"},
        session_target=SessionTarget.CURRENT,
        session_key=SESSION_KEY,
    )
    handler = make_agent_run_handler(
        DeliveryChain(),
        task_runtime_ref=lambda: task_runtime,
        session_manager_ref=lambda: session_manager,
    )

    result = await handler(job)

    envelope = task_runtime.enqueued[0]["route_envelope"]
    assert session_manager.append_owner == ("cron-owner-old", 4)
    assert (session_manager.current.session_id, session_manager.current.epoch) == (
        "cron-owner-new",
        5,
    )
    assert (envelope.session_id, envelope.session_epoch) == ("cron-owner-old", 4)
    assert task_runtime.enqueued[0]["persisted_user_message_id"] == "message-1"
    assert result.summary == "admitted owner result"


@pytest.mark.asyncio
async def test_cron_runtime_missing_owner_bound_terminal_payload_fails_closed() -> None:
    session_manager = _FakeSessionManager()
    task_runtime = _FakeTaskRuntime(
        SimpleNamespace(
            status="succeeded",
            details={"session_id": "cron-owner", "session_epoch": 1},
        )
    )
    job = CronJob(
        id="owner-output-missing",
        name="Owner output missing",
        handler_key="agent_run",
        payload={"kind": AGENT_TURN_KIND, "task": "fenced task", "agent_id": "main"},
        session_target=SessionTarget.CURRENT,
        session_key=SESSION_KEY,
    )
    handler = make_agent_run_handler(
        DeliveryChain(),
        task_runtime_ref=lambda: task_runtime,
        session_manager_ref=lambda: session_manager,
    )

    with pytest.raises(RuntimeError, match="durable terminal output"):
        await handler(job)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot", "expected_text", "expected_summary"),
    [
        ("canonical answer", "canonical answer", "canonical answer"),
        ("", "", None),
    ],
)
async def test_agent_run_delivery_prefers_authoritative_terminal_text_snapshot(
    snapshot: str,
    expected_text: str,
    expected_summary: str | None,
) -> None:
    session_manager = _FakeSessionManager()
    turn_runner = _FakeTurnRunner(
        session_manager,
        text=snapshot,
        events=[
            SimpleNamespace(kind="text_delta", text="stale retry preview"),
            SimpleNamespace(kind="done", text=snapshot, text_snapshot=snapshot),
        ],
    )
    delivery_chain = _RecordingDeliveryChain()
    job = CronJob(
        id="snapshot",
        name="Snapshot",
        handler_key="agent_run",
        payload={"kind": AGENT_TURN_KIND, "task": "synthetic task", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
    )
    handler = make_agent_run_handler(
        delivery_chain,  # type: ignore[arg-type]
        turn_runner_ref=lambda: turn_runner,
        session_manager_ref=lambda: session_manager,
    )

    result = await handler(job)

    assert delivery_chain.deliveries[-1]["result_text"] == expected_text
    assert result.summary == expected_summary


@pytest.mark.asyncio
async def test_agent_run_handler_sanitizes_reply_directive_from_summary() -> None:
    session_manager = _FakeSessionManager()
    turn_runner = _FakeTurnRunner(
        session_manager,
        text="[[reply_to_current]]Here is the scheduled reply",
    )
    job = CronJob(
        id="poem",
        name="Poem",
        handler_key="agent_run",
        payload={"kind": AGENT_TURN_KIND, "task": "write poems", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
        delivery=DeliveryConfig(best_effort=True),
    )
    handler = make_agent_run_handler(
        DeliveryChain(),
        turn_runner_ref=lambda: turn_runner,
        session_manager_ref=lambda: session_manager,
    )

    result = await handler(job)

    assert result.summary == "Here is the scheduled reply"


@pytest.mark.asyncio
async def test_current_webchat_agent_run_treats_same_session_transcript_as_delivery() -> None:
    session_manager = _FakeSessionManager()
    turn_runner = _FakeTurnRunner(session_manager)
    job = CronJob(
        id="drink",
        name="Drink",
        handler_key="agent_run",
        payload={"kind": AGENT_TURN_KIND, "task": "drink water", "agent_id": "main"},
        session_target=SessionTarget.CURRENT,
        session_key=SESSION_KEY,
        origin_session_key=SESSION_KEY,
        delivery=DeliveryConfig(
            mode=DeliveryMode.ORIGIN,
            channel_name="webchat",
            channel_id=f"webchat:{SESSION_KEY}",
            originating_reply_target=ReplyTargetSnapshot(
                channel_name="webchat",
                channel_type="webchat",
                to=f"webchat:{SESSION_KEY}",
            ),
        ),
    )
    handler = make_agent_run_handler(
        DeliveryChain(channel_manager_ref=lambda: _FakeChannelManager()),
        turn_runner_ref=lambda: turn_runner,
        session_manager_ref=lambda: session_manager,
    )

    result = await handler(job)

    assert result.session_key == SESSION_KEY
    assert result.summary == "drink logged"
    assert result.delivery_status == "delivered|ws:skipped|fwd:skipped"
    assert await session_manager.read_transcript(SESSION_KEY) == [
        {"role": "user", "content": "drink water"},
        {"role": "assistant", "content": "drink logged"},
    ]


@pytest.mark.asyncio
async def test_static_webchat_reminder_delivers_without_turn_runner() -> None:
    forward_calls = []
    session_manager = _FakeSessionManager()
    session_events = []

    async def forwarder(**kwargs) -> None:
        forward_calls.append(kwargs)

    job = CronJob(
        id="drink",
        name="Drink",
        handler_key="static_message",
        payload={"kind": REMINDER_KIND, "text": "drink water", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
        origin_session_key=SESSION_KEY,
        delivery=DeliveryConfig(
            mode=DeliveryMode.ORIGIN,
            channel_name="webchat",
            channel_id=f"webchat:{SESSION_KEY}",
            originating_reply_target=ReplyTargetSnapshot(
                channel_name="webchat",
                channel_type="webchat",
                to=f"webchat:{SESSION_KEY}",
            ),
        ),
    )
    handler = make_static_message_handler(
        DeliveryChain(
            channel_manager_ref=lambda: _FakeChannelManager(),
            session_forwarder=forwarder,
        ),
        session_manager_ref=lambda: session_manager,
        session_event_emitter=lambda *args: _record_async(session_events, args),
    )

    result = await handler(job)

    assert result.summary == "drink water"
    assert result.delivery_status == "delivered|ws:skipped|fwd:skipped"
    assert result.session_key.startswith("cron:drink:run:")
    assert session_manager.created == [
        {
            "session_key": result.session_key,
            "agent_id": "main",
            "display_name": "Cron: Drink",
        }
    ]
    assert await session_manager.read_transcript(result.session_key) == [
        {
            "role": "assistant",
            "content": "drink water",
            "provenance": {
                "kind": "cron",
                "source_tool": "cron:drink",
            },
        }
    ]
    assert session_events == [
        (
            result.session_key,
            "sessions.changed",
            {
                "key": result.session_key,
                "reason": "cron_static_message",
                "taskId": result.session_key,
                "status": "succeeded",
            },
        )
    ]
    assert forward_calls == [
        {
            "origin_session_key": SESSION_KEY,
            "text": "drink water",
            "provenance": {
                "kind": "cron",
                "source_session_key": result.session_key,
                "source_tool": "cron:drink",
            },
        }
    ]


@pytest.mark.asyncio
async def test_static_reminder_delivery_failure_fails_job_by_default() -> None:
    session_manager = _FakeSessionManager()
    session_events = []
    job = CronJob(
        id="drink",
        name="Drink",
        handler_key="static_message",
        payload={"kind": REMINDER_KIND, "text": "drink water", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
        delivery=DeliveryConfig(
            mode=DeliveryMode.CHANNEL,
            channel_name="feishu",
            channel_id="chat-1",
        ),
    )
    handler = make_static_message_handler(
        DeliveryChain(channel_manager_ref=_FakeChannelManager),
        session_manager_ref=lambda: session_manager,
        session_event_emitter=lambda *args: _record_async(session_events, args),
    )

    with pytest.raises(RuntimeError, match="delivery failed"):
        await handler(job)

    session_key = session_manager.created[0]["session_key"]
    assert await session_manager.read_transcript(session_key) == [
        {
            "role": "assistant",
            "content": "drink water",
            "provenance": {
                "kind": "cron",
                "source_tool": "cron:drink",
            },
        }
    ]
    assert session_events == [
        (
            session_key,
            "sessions.changed",
            {
                "key": session_key,
                "reason": "cron_static_message",
                "taskId": session_key,
                "status": "failed",
            },
        )
    ]


@pytest.mark.asyncio
async def test_static_reminder_best_effort_delivery_failure_does_not_fail_job() -> None:
    session_manager = _FakeSessionManager()
    session_events = []
    job = CronJob(
        id="drink",
        name="Drink",
        handler_key="static_message",
        payload={"kind": REMINDER_KIND, "text": "drink water", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
        delivery=DeliveryConfig(
            mode=DeliveryMode.CHANNEL,
            channel_name="feishu",
            channel_id="chat-1",
            best_effort=True,
        ),
    )
    handler = make_static_message_handler(
        DeliveryChain(channel_manager_ref=_FakeChannelManager),
        session_manager_ref=lambda: session_manager,
        session_event_emitter=lambda *args: _record_async(session_events, args),
    )

    result = await handler(job)

    assert result.delivery_status == "delivery_failed|ws:skipped|fwd:skipped"
    assert session_events == [
        (
            result.session_key,
            "sessions.changed",
            {
                "key": result.session_key,
                "reason": "cron_static_message",
                "taskId": result.session_key,
                "status": "succeeded",
            },
        )
    ]


@pytest.mark.asyncio
async def test_static_reminder_unexpected_delivery_error_marks_session_failed() -> None:
    session_manager = _FakeSessionManager()
    session_events = []

    class _ExplodingDeliveryChain:
        async def notify_start(self, _job, _text) -> None:
            return None

        async def deliver(self, *_args, **_kwargs):
            raise RuntimeError("delivery exploded")

    job = CronJob(
        id="drink",
        name="Drink",
        handler_key="static_message",
        payload={"kind": REMINDER_KIND, "text": "drink water", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
    )
    handler = make_static_message_handler(
        _ExplodingDeliveryChain(),
        session_manager_ref=lambda: session_manager,
        session_event_emitter=lambda *args: _record_async(session_events, args),
    )

    with pytest.raises(RuntimeError, match="delivery exploded"):
        await handler(job)

    session_key = session_manager.created[0]["session_key"]
    assert session_events == [
        (
            session_key,
            "sessions.changed",
            {
                "key": session_key,
                "reason": "cron_static_message",
                "taskId": session_key,
                "status": "failed",
            },
        )
    ]


@pytest.mark.asyncio
async def test_static_reminder_session_event_failure_does_not_fail_job() -> None:
    session_manager = _FakeSessionManager()
    emitted_statuses = []

    async def failing_emitter(_session_key, _event_name, payload) -> None:
        emitted_statuses.append(payload["status"])
        raise RuntimeError("subscriber unavailable")

    job = CronJob(
        id="drink",
        name="Drink",
        handler_key="static_message",
        payload={"kind": REMINDER_KIND, "text": "drink water", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
    )
    handler = make_static_message_handler(
        DeliveryChain(),
        session_manager_ref=lambda: session_manager,
        session_event_emitter=failing_emitter,
    )

    result = await handler(job)

    assert result.summary == "drink water"
    assert emitted_statuses == ["succeeded"]


@pytest.mark.asyncio
async def test_agent_run_task_runtime_context_exhaustion_delivers_controlled_message() -> None:
    raw_error = (
        "Context overflow is in the current turn's recent tool calls or "
        "reasoning tail; history compaction cannot reduce it."
    )
    task_runtime = _FakeTaskRuntime(
        SimpleNamespace(
            status="failed",
            terminal_reason="error",
            error_class="current_turn_context_exhausted",
            error_message=raw_error,
        )
    )
    delivery_chain = _RecordingDeliveryChain()
    job = CronJob(
        id="research",
        name="Research",
        handler_key="agent_run",
        payload={
            "kind": AGENT_TURN_KIND,
            "task": "research three agent papers",
            "agent_id": "main",
        },
        session_target=SessionTarget.ISOLATED,
    )
    handler = make_agent_run_handler(
        delivery_chain,  # type: ignore[arg-type]
        task_runtime_ref=lambda: task_runtime,
        session_manager_ref=lambda: _FakeSessionManager(),
    )

    with pytest.raises(RuntimeError) as exc_info:
        await handler(job)

    assert raw_error not in str(exc_info.value)
    assert "current_turn_context_exhausted" not in str(exc_info.value)
    assert delivery_chain.deliveries
    delivered_text = delivery_chain.deliveries[-1]["result_text"]
    assert "too large" in delivered_text.lower()
    assert raw_error not in delivered_text
    assert "current_turn_context_exhausted" not in delivered_text


@pytest.mark.asyncio
async def test_agent_run_runtime_context_exception_delivers_controlled_message() -> None:
    raw_error = (
        "Context overflow is in the current turn's recent tool calls or "
        "reasoning tail; history compaction cannot reduce it."
    )

    class RaisingTaskRuntime:
        async def enqueue(self, *args, **kwargs):
            raise RuntimeError(raw_error)

    delivery_chain = _RecordingDeliveryChain()
    job = CronJob(
        id="research",
        name="Research",
        handler_key="agent_run",
        payload={
            "kind": AGENT_TURN_KIND,
            "task": "research three agent papers",
            "agent_id": "main",
        },
        session_target=SessionTarget.ISOLATED,
    )
    handler = make_agent_run_handler(
        delivery_chain,  # type: ignore[arg-type]
        task_runtime_ref=lambda: RaisingTaskRuntime(),
        session_manager_ref=lambda: _FakeSessionManager(),
    )

    with pytest.raises(RuntimeError) as exc_info:
        await handler(job)

    assert raw_error not in str(exc_info.value)
    assert "history compaction cannot reduce it" not in str(exc_info.value)
    assert delivery_chain.deliveries
    delivered_text = delivery_chain.deliveries[-1]["result_text"]
    assert "too large" in delivered_text.lower()
    assert raw_error not in delivered_text
    assert "history compaction cannot reduce it" not in delivered_text


@pytest.mark.asyncio
async def test_owner_current_session_agent_run_uses_owner_tool_boundary() -> None:
    session_manager = _FakeSessionManager()
    turn_runner = _FakeTurnRunner(session_manager)

    job = CronJob(
        id="drink",
        name="Drink",
        handler_key="agent_run",
        payload={"kind": AGENT_TURN_KIND, "task": "drink water", "agent_id": "main"},
        session_target=SessionTarget.CURRENT,
        session_key=SESSION_KEY,
        origin_session_key=SESSION_KEY,
        creator_is_owner=True,
        creator_host_execute=True,
        tool_policy={
            "profile": "minimal",
            "also_allow": ["memory_search", "exec_command"],
            "deny": ["web_fetch"],
        },
    )
    handler = make_agent_run_handler(
        DeliveryChain(session_forwarder=None),
        turn_runner_ref=lambda: turn_runner,
        session_manager_ref=lambda: session_manager,
    )

    await handler(job)

    tool_context = turn_runner.calls[0]["tool_context"]
    assert tool_context.is_owner is True
    assert tool_context.allowed_tools is None
    assert tool_context.tool_policy == job.tool_policy
    assert "exec_command" not in tool_context.denied_tools


@pytest.mark.asyncio
async def test_agent_run_delivery_failure_fails_job_by_default() -> None:
    session_manager = _FakeSessionManager()
    turn_runner = _FakeTurnRunner(session_manager)
    job = CronJob(
        id="drink",
        name="Drink",
        handler_key="agent_run",
        payload={"kind": AGENT_TURN_KIND, "task": "drink water", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
        delivery=DeliveryConfig(
            mode=DeliveryMode.CHANNEL,
            channel_name="feishu",
            channel_id="chat-1",
        ),
    )
    handler = make_agent_run_handler(
        DeliveryChain(channel_manager_ref=lambda: _FakeChannelManager()),
        turn_runner_ref=lambda: turn_runner,
        session_manager_ref=lambda: session_manager,
    )

    with pytest.raises(RuntimeError, match="delivery failed"):
        await handler(job)


@pytest.mark.asyncio
async def test_agent_run_best_effort_delivery_failure_does_not_fail_job() -> None:
    session_manager = _FakeSessionManager()
    turn_runner = _FakeTurnRunner(session_manager)
    job = CronJob(
        id="drink",
        name="Drink",
        handler_key="agent_run",
        payload={"kind": AGENT_TURN_KIND, "task": "drink water", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
        delivery=DeliveryConfig(
            mode=DeliveryMode.CHANNEL,
            channel_name="feishu",
            channel_id="chat-1",
            best_effort=True,
        ),
    )
    handler = make_agent_run_handler(
        DeliveryChain(channel_manager_ref=lambda: _FakeChannelManager()),
        turn_runner_ref=lambda: turn_runner,
        session_manager_ref=lambda: session_manager,
    )

    result = await handler(job)

    assert result.delivery_status == "delivery_failed|ws:skipped|fwd:skipped"
