from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway import jev_handoff
from opensquilla.gateway.compaction_target import GatewayCompactionTarget, GatewayConsumerBudget
from opensquilla.gateway.rpc.registry import RpcHandlerError
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.session.compaction import CompactionResult
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionStatus
from opensquilla.session.storage import SessionStorage
from tests.helpers.compaction import synthetic_compaction_config

SOURCE = "agent:main:webchat:source"
DECISION = "synthetic-handoff-decision"
SUMMARY = (
    "Goal: complete the synthetic report.\n"
    "Completed: collected 8 measurements; verified total is 42.\n"
    "Open: review the report.\n"
    "Files: synthetic/report.txt\n"
    "Constraints: write in Chinese.\n"
    "Unresolved: which chart to include."
)


@pytest.fixture
async def setup_handoff(monkeypatch):
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    locks = defaultdict(asyncio.Lock)
    runtime = SimpleNamespace(has_session_work=AsyncMock(return_value=False))
    service = SimpleNamespace(
        get_decision=AsyncMock(return_value={"status": "accepted", "action": "new_session"}),
    )
    ctx = SimpleNamespace(
        session_manager=manager,
        turn_runner=SimpleNamespace(get_session_lock=lambda key: locks[key]),
        task_runtime=runtime,
        config=SimpleNamespace(),
    )
    consumer = GatewayConsumerBudget(
        provider=OpenAIProvider(api_key="synthetic-key", model="synthetic-chat"),
        provider_id="openai", model="synthetic-chat", context_window_tokens=32_000,
        physical_context_window_tokens=32_000, max_output_tokens=1024,
        provider_request_max_chars=128_000,
        next_request_reserve_tokens=2000, next_request_reserve_chars=8000,
    )
    compaction = synthetic_compaction_config(summary=SUMMARY)
    target = GatewayCompactionTarget(
        provider=compaction.llm_plan.primary.provider, plan=compaction.llm_plan,
    )
    monkeypatch.setattr(jev_handoff, "get_jev_service", lambda *args: service)
    monkeypatch.setattr(jev_handoff, "resolve_gateway_consumer_budget", lambda *args: consumer)
    monkeypatch.setattr(jev_handoff, "resolve_gateway_compaction_target", lambda *a, **kw: target)
    await manager.create(
        SOURCE, model="synthetic-chat", model_provider="openai",
        provider_override="openai", auth_profile_override="openai:synthetic",
        model_routing_mode="router", thinking_level="high",
    )
    await storage.update_session_fields(SOURCE, {"status": SessionStatus.DONE})
    for index in range(8):
        await manager.append_message(
            SOURCE, "user" if index % 2 == 0 else "assistant",
            (
                f"Synthetic entry {index}. {SUMMARY}\n"
                + ("Measurements and report details. " * 150)
            ),
        )
    yield SimpleNamespace(
        ctx=ctx, storage=storage, manager=manager, locks=locks, runtime=runtime,
        service=service, provider=compaction.llm_plan.primary.provider,
    )
    await storage.close()


async def test_handoff_runs_real_bounded_compactor_and_leaves_source_unchanged(setup_handoff):
    env = setup_handoff
    before_node = await env.manager.get_session(SOURCE)
    before = await env.manager.get_transcript(SOURCE)

    result = await jev_handoff.create_jev_handoff(env.ctx, SOURCE, DECISION)

    assert result["sourceSessionKey"] == SOURCE
    target = await env.manager.get_session(result["targetSessionKey"])
    seed, = await env.manager.get_transcript(target.session_key)
    assert target.parent_session_key == SOURCE
    assert target.status == SessionStatus.DONE
    assert target.model == "synthetic-chat"
    assert target.provider_override == "openai"
    assert target.auth_profile_override == "openai:synthetic"
    assert target.model_routing_mode == "router"
    assert target.thinking_level == "high"
    assert seed.role == "assistant"
    for fact in ("42", "synthetic/report.txt", "Chinese", "review", "chart"):
        assert fact in seed.content
    assert "/control/chat?session=agent%3Amain%3Awebchat%3Asource" in seed.content
    assert seed.provenance_source_session_key == SOURCE
    assert env.provider.calls
    assert sum(len(entry.content or "") for entry in before) > len(seed.content) * 4
    assert (await env.manager.get_session(SOURCE)).model_dump() == before_node.model_dump()
    assert [entry.model_dump() for entry in await env.manager.get_transcript(SOURCE)] == [
        entry.model_dump() for entry in before
    ]
    assert await env.storage.get_all_summaries(before_node.session_id) == []
    # A normal next turn sees the durable summary without replaying old tool calls.
    await env.manager.append_message(target.session_key, "user", "Continue reviewing the report.")
    replay = await env.manager.get_transcript(target.session_key)
    assert len(replay) == 2 and "42" in replay[0].content


async def test_concurrent_accepts_reuse_one_seed_and_one_summary(setup_handoff):
    env = setup_handoff
    first, second = await asyncio.gather(*(
        jev_handoff.create_jev_handoff(env.ctx, SOURCE, DECISION) for _ in range(2)
    ))
    assert first == second
    assert len(env.provider.calls) == 1
    assert len(await env.manager.get_transcript(first["targetSessionKey"])) == 1
    assert await env.storage.count_sessions() == 2


async def test_busy_source_does_not_call_summary_or_create_session(setup_handoff):
    env = setup_handoff
    env.runtime.has_session_work.return_value = True
    with pytest.raises(RpcHandlerError) as caught:
        await jev_handoff.create_jev_handoff(env.ctx, SOURCE, DECISION)
    assert caught.value.code == "CONFLICT"
    assert not env.provider.calls
    assert await env.storage.count_sessions() == 1


async def test_unaccepted_decision_cannot_start_handoff(setup_handoff):
    env = setup_handoff
    env.service.get_decision.return_value = {"action": "new_session", "status": "expired"}
    with pytest.raises(RpcHandlerError) as caught:
        await jev_handoff.create_jev_handoff(env.ctx, SOURCE, DECISION)
    assert caught.value.code == "CONFLICT"
    assert not env.provider.calls
    assert await env.storage.count_sessions() == 1


async def test_revoked_decision_after_summary_does_not_create_child(setup_handoff, monkeypatch):
    env = setup_handoff
    original = jev_handoff._summarize

    async def summarize(*args):
        result = await original(*args)
        env.service.get_decision.return_value = {"action": "new_session", "status": "expired"}
        return result

    monkeypatch.setattr(jev_handoff, "_summarize", summarize)
    with pytest.raises(RpcHandlerError) as caught:
        await jev_handoff.create_jev_handoff(env.ctx, SOURCE, DECISION)
    assert caught.value.code == "CONFLICT"
    assert env.provider.calls
    assert await env.storage.count_sessions() == 1


@pytest.mark.parametrize("change", ["reset", "append", "edit"])
async def test_changed_source_rejects_stale_summary(setup_handoff, monkeypatch, change):
    env = setup_handoff
    original = jev_handoff._summarize

    async def summarize(*args):
        result = await original(*args)
        if change == "reset":
            await env.storage.increment_epoch(SOURCE)
        elif change == "append":
            await env.manager.append_message(SOURCE, "user", "A newly queued instruction.")
        else:
            entries = await env.manager.get_transcript(SOURCE)
            await env.manager.append_message(
                SOURCE, entries[0].role, "An edited instruction.", message_id=entries[0].message_id,
            )
        return result

    monkeypatch.setattr(jev_handoff, "_summarize", summarize)
    with pytest.raises(RpcHandlerError) as caught:
        await jev_handoff.create_jev_handoff(env.ctx, SOURCE, DECISION)
    assert caught.value.code == "CONFLICT"
    assert await env.storage.count_sessions() == 1


@pytest.mark.parametrize("location", ["user", "tool"])
async def test_attachments_fail_explicitly_before_summary(setup_handoff, location):
    env = setup_handoff
    if location == "user":
        await env.manager.append_message(SOURCE, "user", json.dumps({
            "text": "Read this file.",
            "attachments": [{"name": "synthetic.txt", "mime": "text/plain", "data": "eA=="}],
        }))
    else:
        await env.manager.append_message(
            SOURCE, "assistant", "Tool returned an image.", tool_calls=[
                {"type": "tool_result", "content": [{"type": "image", "data": "synthetic"}]},
            ],
        )
    before = await env.manager.get_transcript(SOURCE)
    with pytest.raises(RpcHandlerError) as caught:
        await jev_handoff.create_jev_handoff(env.ctx, SOURCE, DECISION)
    assert caught.value.details == {"reason": "handoff_attachments_unsupported"}
    assert not env.provider.calls
    assert await env.storage.count_sessions() == 1
    assert [entry.model_dump() for entry in await env.manager.get_transcript(SOURCE)] == [
        entry.model_dump() for entry in before
    ]


async def test_failed_or_partial_summary_never_creates_empty_child(setup_handoff, monkeypatch):
    env = setup_handoff
    monkeypatch.setattr(jev_handoff, "compact_context", AsyncMock(return_value=CompactionResult(
        summary="Partial summary must never become a continuation.", kept_entries=[],
        removed_count=8, chunks_processed=1, summary_source="fallback",
    )))
    with pytest.raises(RpcHandlerError) as caught:
        await jev_handoff.create_jev_handoff(env.ctx, SOURCE, DECISION)
    assert caught.value.details == {"reason": "handoff_summary_unavailable"}
    assert await env.storage.count_sessions() == 1


async def test_target_binding_conflict_never_overwrites_existing_chat(setup_handoff):
    env = setup_handoff
    source = await env.manager.get_session(SOURCE)
    target_key, _, _ = jev_handoff._handoff_identity(source, DECISION)
    await env.manager.create(target_key, display_name="An unrelated existing chat")
    await env.manager.append_message(target_key, "user", "Keep this unrelated content.")
    with pytest.raises(RpcHandlerError) as caught:
        await jev_handoff.create_jev_handoff(env.ctx, SOURCE, DECISION)
    assert caught.value.code == "CONFLICT"
    assert not env.provider.calls
    entry, = await env.manager.get_transcript(target_key)
    assert entry.content == "Keep this unrelated content."


async def test_seed_write_failure_cleans_only_empty_destination(setup_handoff, monkeypatch):
    env = setup_handoff
    original = env.manager.append_message

    async def append(key, *args, **kwargs):
        if key != SOURCE:
            raise OSError("synthetic storage failure")
        return await original(key, *args, **kwargs)

    monkeypatch.setattr(env.manager, "append_message", append)
    with pytest.raises(OSError, match="synthetic storage failure"):
        await jev_handoff.create_jev_handoff(env.ctx, SOURCE, DECISION)
    assert await env.storage.count_sessions() == 1
    assert len(await env.manager.get_transcript(SOURCE)) == 8


async def test_completed_tool_results_are_summarized_without_replaying_calls(setup_handoff):
    env = setup_handoff
    env.provider.summary = SUMMARY + "\nVerified tool result: tool_fact_42."
    await env.manager.append_message(SOURCE, "assistant", "Read the report.", tool_calls=[{
        "id": "synthetic-tool", "type": "tool_use", "name": "read_file",
        "input": {"path": "synthetic/report.txt"},
    }])
    await env.manager.append_message(
        SOURCE, "tool", "Verified total: tool_fact_42.", tool_call_id="synthetic-tool",
    )
    await env.manager.append_message(SOURCE, "assistant", "The tool round is complete.")

    result = await jev_handoff.create_jev_handoff(env.ctx, SOURCE, DECISION)

    seed, = await env.manager.get_transcript(result["targetSessionKey"])
    assert "tool_fact_42" in seed.content
    assert "tool_fact_42" in repr(env.provider.calls[0][0])
    assert seed.tool_calls is None and seed.tool_call_id is None
    source_entries = await env.manager.get_transcript(SOURCE)
    assert source_entries[-2].tool_call_id == "synthetic-tool"


async def test_outer_timeout_keeps_source_and_creates_no_child(setup_handoff, monkeypatch):
    env = setup_handoff
    started = asyncio.Event()

    async def stalled_summary(request):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(jev_handoff, "_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(jev_handoff, "compact_context", stalled_summary)
    with pytest.raises(RpcHandlerError) as caught:
        await jev_handoff.create_jev_handoff(env.ctx, SOURCE, DECISION)
    assert started.is_set()
    assert caught.value.code == "UNAVAILABLE"
    assert await env.storage.count_sessions() == 1
    assert len(await env.manager.get_transcript(SOURCE)) == 8


async def test_cancel_during_seed_settles_before_unlock_and_retry_is_idempotent(
    setup_handoff, monkeypatch,
):
    env = setup_handoff
    seed_started = asyncio.Event()
    seed_continue = asyncio.Event()
    original = env.manager.append_message

    async def append(key, *args, **kwargs):
        if key != SOURCE:
            seed_started.set()
            await seed_continue.wait()
        return await original(key, *args, **kwargs)

    monkeypatch.setattr(env.manager, "append_message", append)
    operation = asyncio.create_task(jev_handoff.create_jev_handoff(env.ctx, SOURCE, DECISION))
    await asyncio.wait_for(seed_started.wait(), timeout=2)
    operation.cancel()
    await asyncio.sleep(0)
    assert env.locks[SOURCE].locked()
    seed_continue.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert not env.locks[SOURCE].locked()
    result = await jev_handoff.create_jev_handoff(env.ctx, SOURCE, DECISION)
    assert len(await env.manager.get_transcript(result["targetSessionKey"])) == 1
    assert len(env.provider.calls) == 1


async def test_final_seed_envelope_must_fit_not_just_summary(setup_handoff, monkeypatch):
    env = setup_handoff
    original = jev_handoff.build_gateway_compaction_budget

    def summary_only_budget(*args, **kwargs):
        budget = original(*args, **kwargs)
        return replace(budget, consumer_admission=lambda summary, kept: bool(summary))

    monkeypatch.setattr(jev_handoff, "build_gateway_compaction_budget", summary_only_budget)
    with pytest.raises(RpcHandlerError) as caught:
        await jev_handoff.create_jev_handoff(env.ctx, SOURCE, DECISION)
    assert caught.value.code == "UNAVAILABLE"
    assert "容量" in caught.value.message
    assert env.provider.calls
    assert await env.storage.count_sessions() == 1
