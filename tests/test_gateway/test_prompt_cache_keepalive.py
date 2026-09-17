from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.prompt_cache_keepalive import PromptCacheKeepaliveCandidate
from opensquilla.gateway.prompt_cache_keepalive import PromptCacheKeepaliveService
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.scopes import METHOD_SCOPES, READ_SCOPE, WRITE_SCOPE
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.provider import ChatConfig, DoneEvent, Message


class _IdleRuntime:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def cancel_auxiliary(self, key: str) -> None:
        self.cancelled.append(key)

    async def run_auxiliary_if_idle(self, _key: str, operation: Any) -> bool:
        await operation()
        return True


class _Provider:
    accounts_physical_usage = False

    def __init__(self, cached_tokens: int) -> None:
        self.cached_tokens = cached_tokens
        self.calls: list[tuple[list[Message], list[Any], ChatConfig]] = []

    async def chat(self, messages: list[Message], tools: Any, config: ChatConfig):
        self.calls.append((messages, tools, config))
        yield DoneEvent(
            model="synthetic-model",
            input_tokens=12,
            output_tokens=1,
            cached_tokens=self.cached_tokens,
        )


def _candidate(provider: _Provider) -> PromptCacheKeepaliveCandidate:
    return PromptCacheKeepaliveCandidate(
        session_key="agent:main:webchat:test",
        provider=provider,
        provider_id="synthetic",
        model="synthetic-model",
        messages=(Message(role="user", content="stable history"),),
        tools=(),
        config=ChatConfig(max_tokens=99, timeout=120),
    )


def test_prompt_cache_keepalive_methods_keep_operator_scopes() -> None:
    assert METHOD_SCOPES["sessions.promptCacheKeepalive.status"] == READ_SCOPE
    assert METHOD_SCOPES["sessions.promptCacheKeepalive.set"] == WRITE_SCOPE


@pytest.mark.asyncio
async def test_default_off_ignores_candidates_and_schedules_no_provider_work() -> None:
    runtime = _IdleRuntime()
    provider = _Provider(cached_tokens=9)
    service = PromptCacheKeepaliveService(
        task_runtime=runtime,
        session_manager=None,
        usage_event_sink=None,
    )
    key = "agent:main:webchat:test"

    service.record_candidate(_candidate(provider))
    await asyncio.sleep(0)

    status = service.status(key)
    assert status["enabled"] is False
    assert status["state"] == "off"
    assert status["hasSnapshot"] is False
    assert status["nextProbeAt"] is None
    assert service._leases == {}
    assert provider.calls == []
    assert runtime.cancelled == []
    await service.close()


@pytest.mark.asyncio
async def test_probe_is_ephemeral_bounded_and_reschedules_only_on_cache_hit() -> None:
    runtime = _IdleRuntime()
    provider = _Provider(cached_tokens=9)
    service = PromptCacheKeepaliveService(
        task_runtime=runtime,
        session_manager=None,
        usage_event_sink=None,
    )
    key = "agent:main:webchat:test"
    await service.set_enabled(
        key,
        enabled=True,
        ttl_seconds=300,
        idle_timeout_seconds=3_600,
    )
    service.record_candidate(_candidate(provider))
    lease = service._leases[key]
    service._cancel_timer(lease)

    await service._probe(key, lease, lease.generation)

    assert service.status(key)["state"] == "scheduled"
    assert service.status(key)["lastCacheHitTokens"] == 9
    assert service.status(key)["idleTimeoutSeconds"] == 3_600
    assert service.status(key)["idleExpiresAt"] is not None
    assert len(provider.calls) == 1
    messages, tools, config = provider.calls[0]
    assert [message.content for message in messages[:1]] == ["stable history"]
    assert "keepalive probe" in str(messages[-1].content)
    assert tools == []
    assert config.max_tokens == 1
    assert config.physical_attempt_limit == 1
    await service.close()


@pytest.mark.asyncio
async def test_cache_miss_stops_lease_without_retry() -> None:
    provider = _Provider(cached_tokens=0)
    service = PromptCacheKeepaliveService(
        task_runtime=_IdleRuntime(),
        session_manager=None,
        usage_event_sink=None,
    )
    key = "agent:main:webchat:test"
    await service.set_enabled(key, enabled=True, ttl_seconds=300)
    service.record_candidate(_candidate(provider))
    lease = service._leases[key]
    service._cancel_timer(lease)

    await service._probe(key, lease, lease.generation)

    status = service.status(key)
    assert status["enabled"] is False
    assert status["state"] == "stopped"
    assert status["reason"] == "cache_miss_or_usage_unreported"
    assert status["nextProbeAt"] is None
    await service.close()


@pytest.mark.asyncio
async def test_history_change_preserves_opt_in_but_discards_snapshot() -> None:
    service = PromptCacheKeepaliveService(
        task_runtime=_IdleRuntime(),
        session_manager=None,
        usage_event_sink=None,
    )
    key = "agent:main:webchat:test"
    await service.set_enabled(key, enabled=True, ttl_seconds=600)
    service.record_candidate(_candidate(_Provider(cached_tokens=4)))

    service.refresh_required(key)

    status = service.status(key)
    assert status["enabled"] is True
    assert status["state"] == "waiting"
    assert status["hasSnapshot"] is False
    assert status["ttlSeconds"] == 600
    assert status["idleExpiresAt"] is None
    await service.close()


@pytest.mark.asyncio
async def test_idle_timeout_pauses_without_disabling_and_next_turn_rearms() -> None:
    provider = _Provider(cached_tokens=7)
    service = PromptCacheKeepaliveService(
        task_runtime=_IdleRuntime(),
        session_manager=None,
        usage_event_sink=None,
    )
    key = "agent:main:webchat:test"
    await service.set_enabled(
        key,
        enabled=True,
        ttl_seconds=300,
        idle_timeout_seconds=300,
    )
    service.record_candidate(_candidate(provider))
    lease = service._leases[key]
    service._cancel_timer(lease)

    await service._run_after(key, lease.generation, 0, True)

    status = service.status(key)
    assert status["enabled"] is True
    assert status["state"] == "paused"
    assert status["reason"] == "idle_timeout"
    assert status["hasSnapshot"] is False
    assert status["nextProbeAt"] is None
    assert provider.calls == []

    service.record_candidate(_candidate(provider))

    rearmed = service.status(key)
    assert rearmed["enabled"] is True
    assert rearmed["state"] == "scheduled"
    assert rearmed["hasSnapshot"] is True
    assert rearmed["idleExpiresAt"] is not None
    await service.close()


@pytest.mark.asyncio
async def test_task_runtime_auxiliary_is_directly_cancellable() -> None:
    runtime = TaskRuntime(storage=object(), turn_handler=lambda _run: None)
    started = asyncio.Event()
    blocked = asyncio.Event()

    async def operation() -> None:
        started.set()
        await blocked.wait()

    task = asyncio.create_task(
        runtime.run_auxiliary_if_idle("agent:main:webchat:test", operation)
    )
    await started.wait()
    await runtime.cancel_auxiliary("agent:main:webchat:test")
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime._auxiliary_tasks_by_session == {}


@dataclass
class _Storage:
    async def get_session(self, key: str) -> Any:
        if key == "agent:main:webchat:test":
            return SimpleNamespace(session_key=key)
        return None


class _RpcService:
    def __init__(self) -> None:
        self.saved: tuple[str, bool, int, int] | None = None

    def status(self, _key: str) -> dict[str, Any]:
        return {"enabled": False, "ttlSeconds": 300}

    async def set_enabled(
        self,
        key: str,
        *,
        enabled: bool,
        ttl_seconds: int,
        idle_timeout_seconds: int,
    ) -> dict[str, Any]:
        self.saved = (key, enabled, ttl_seconds, idle_timeout_seconds)
        return {
            "enabled": enabled,
            "ttlSeconds": ttl_seconds,
            "idleTimeoutSeconds": idle_timeout_seconds,
        }


@pytest.mark.asyncio
async def test_rpc_requires_explicit_boolean_and_bounded_ttl() -> None:
    service = _RpcService()
    ctx = RpcContext(
        conn_id="test",
        session_manager=SimpleNamespace(storage=_Storage()),
        prompt_cache_keepalive_service=service,
    )
    dispatcher = get_dispatcher()

    invalid = await dispatcher.dispatch(
        "1",
        "sessions.promptCacheKeepalive.set",
        {"key": "agent:main:webchat:test", "enabled": 1, "ttlSeconds": 300},
        ctx,
    )
    assert invalid.ok is False
    assert invalid.error is not None and invalid.error.code == "INVALID_REQUEST"

    valid = await dispatcher.dispatch(
        "2",
        "sessions.promptCacheKeepalive.set",
        {
            "key": "agent:main:webchat:test",
            "enabled": True,
            "ttlSeconds": 600,
            "idleTimeoutSeconds": 3_600,
        },
        ctx,
    )
    assert valid.ok is True
    assert service.saved == ("agent:main:webchat:test", True, 600, 3_600)

    too_short = await dispatcher.dispatch(
        "3",
        "sessions.promptCacheKeepalive.set",
        {
            "key": "agent:main:webchat:test",
            "enabled": True,
            "ttlSeconds": 3_600,
            "idleTimeoutSeconds": 2_880,
        },
        ctx,
    )
    assert too_short.ok is False
    assert too_short.error is not None and too_short.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_rpc_defaults_idle_timeout_for_existing_clients() -> None:
    service = _RpcService()
    ctx = RpcContext(
        conn_id="test",
        session_manager=SimpleNamespace(storage=_Storage()),
        prompt_cache_keepalive_service=service,
    )

    result = await get_dispatcher().dispatch(
        "1",
        "sessions.promptCacheKeepalive.set",
        {"key": "agent:main:webchat:test", "enabled": True, "ttlSeconds": 600},
        ctx,
    )

    assert result.ok is True
    assert service.saved == ("agent:main:webchat:test", True, 600, 3_600)


def test_real_enqueue_preempts_auxiliary_before_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    runtime = TaskRuntime(storage=object(), turn_handler=lambda _run: None)

    async def cancel(_key: str) -> None:
        events.append("cancel")

    async def reserve(*_args: Any, **_kwargs: Any) -> str:
        events.append("reserve")
        return "handle"

    monkeypatch.setattr(runtime, "cancel_auxiliary", cancel)
    monkeypatch.setattr(runtime, "_reserve_persist_and_activate", reserve)
    envelope = RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="test",
        agent_id="main",
        session_key="agent:main:webchat:test",
    )

    result = asyncio.run(runtime.enqueue(envelope, "hello"))

    assert result == "handle"
    assert events == ["cancel", "reserve"]
