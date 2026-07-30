import asyncio
from types import SimpleNamespace

import pytest

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

SESSION_KEY = "agent:main:webchat:abc123"
CRON_SESSION_KEY = "cron:drink:run:def456"


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

    async def get_or_create(self, **kwargs):
        self.created.append(kwargs)
        return kwargs

    async def append_message(self, session_key, role, content):
        row = {"role": role, "content": content}
        self.rows.setdefault(session_key, []).append(row)
        return SimpleNamespace(role=role, content=content)

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


class _FakeTaskRuntime:
    def __init__(self, record) -> None:
        self.record = record
        self.enqueued = []

    async def enqueue(self, route_envelope, task, *, mode, run_kind):
        self.enqueued.append(
            {
                "route_envelope": route_envelope,
                "task": task,
                "mode": mode,
                "run_kind": run_kind,
            }
        )
        return SimpleNamespace(task_id="task-1")

    async def wait(self, task_id, *, timeout):
        assert task_id == "task-1"
        return self.record


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

    assert report.channel_status == "delivered"
    assert report.ws_status == "delivered"
    assert report.session_status == "skipped"
    assert cm.adapter.sent[0].content == "Here is the scheduled reply"
    assert ws_events[0][2]["summary"] == "Here is the scheduled reply"
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
        )
    )

    result = await handler(job)

    assert result.summary == "drink water"
    assert result.delivery_status == "delivered|ws:skipped|fwd:skipped"
    assert result.session_key.startswith("cron:drink:run:")
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
        DeliveryChain(channel_manager_ref=lambda: _FakeChannelManager())
    )

    with pytest.raises(RuntimeError, match="delivery failed"):
        await handler(job)


@pytest.mark.asyncio
async def test_static_reminder_best_effort_delivery_failure_does_not_fail_job() -> None:
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
        DeliveryChain(channel_manager_ref=lambda: _FakeChannelManager())
    )

    result = await handler(job)

    assert result.delivery_status == "delivery_failed|ws:skipped|fwd:skipped"


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
