"""A real Gateway task queue survives a proven non-applied compaction writer."""
from __future__ import annotations

import asyncio
import inspect
import sqlite3

import pytest

from opensquilla.engine.cache_break_monitor import active_compaction_ids, add_compaction_listener
from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.adapters.session_maintenance import (
    build_gateway_session_maintenance_adapter,
)
from opensquilla.gateway.boot import (
    _make_task_session_lifecycle_listener,
    dispatch_task_runtime_turn,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.event_bridge import EventBridge
from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.gateway.rpc_sessions import _task_state_summary
from opensquilla.gateway.session_streams import get_session_streams
from opensquilla.gateway.websocket import ConnectionRegistry, SubscriptionManager
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import DoneEvent, TextDeltaEvent
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import AgentTaskStatus, SessionStatus
from opensquilla.session.storage import StorageBusyError
from tests.test_gateway.test_compaction_persisted_continuation import _checkpoint_rows, _rows
from tests.test_gateway.test_turn_ingress_rpc import SESSION_KEY, _open_real_stack


@pytest.mark.parametrize("failure", ["none", "preflight_read", "begin_lock"])
async def test_known_writer_refusal_releases_gateway_queue_and_next_turn_compacts(
    tmp_path, monkeypatch, failure,
):
    from opensquilla import token_estimation

    monkeypatch.setattr(token_estimation, "_encoding", token_estimation._ENCODING_UNAVAILABLE)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "profile"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = "synthetic-writer-queue"
    config = GatewayConfig(
        workspace_dir=str(workspace),
        llm={"provider": "openai", "model": model, "api_key": "synthetic-unused",
             "base_url": "https://example.invalid/v1", "context_window_tokens": 32_768,
             "max_tokens": 1024, "thinking": "off"},
        compaction={"protected_recent_messages": 0},
        prompt={"mode": "minimal"}, squilla_router={"enabled": False},
        llm_ensemble={"enabled": False}, naming={"enabled": False},
        agent_stream_heartbeat_interval_seconds=0,
    )
    config.attachments.media_root = str(tmp_path / "media")
    selector = ModelSelector(SelectorConfig(primary=ProviderConfig(
        provider="openai", model=model, api_key="synthetic-unused",
        base_url="https://example.invalid/v1",
    )))
    first_request = asyncio.Event()
    release_first = asyncio.Event()
    ordinary_requests = []
    summary_requests = []
    wire_events = []
    compactions = []
    writer_results = []
    first_prompt = "Continue FIRST_QUEUE_TURN. Preserve VIOLET-731."
    next_prompt = "Continue NEXT_QUEUE_TURN after the first response. Preserve VIOLET-731."

    async def provider_reply(self, messages, tools=None, config=None):
        projection = self.project_final_request(messages, tools, config)
        assert projection.fits and projection.proof.get("fits", True)
        text = "\n".join(str(message.content) for message in messages)
        if config.candidate_output_mode == "inert_artifact":
            summary_requests.append(text)
            yield TextDeltaEvent(text=(
                "Persisted fact: VIOLET-731. Earlier completed work remains recorded. "
                "Continue the current user request."
            ))
        else:
            assert "VIOLET-731" in text
            ordinary_requests.append(text)
            if len(ordinary_requests) == 1:
                assert first_prompt in text and next_prompt not in text
                first_request.set()
                await release_first.wait()
            else:
                assert next_prompt in text
            yield TextDeltaEvent(text=f"CONTINUED_{len(ordinary_requests)} VIOLET-731")
        yield DoneEvent(stop_reason="stop", input_tokens=100, output_tokens=40)

    # The request is genuinely assembled and admitted; only external transport
    # output is replaced. No network provider, tool, or running client is used.
    monkeypatch.setattr(OpenAIProvider, "chat", provider_reply)
    async with _open_real_stack(tmp_path / "queue.sqlite") as stack:
        stack.manager = SessionManager(
            stack.storage, inject_time_prefix=False, checkpoint_workspace_dir=workspace,
        )
        stack.context.session_manager = stack.manager
        stack.context.config = config
        stack.context.provider_selector = selector
        runner = TurnRunner(
            provider_selector=selector, session_manager=stack.manager, config=config,
            model_catalog=ModelCatalog(),
            session_lock_provider=stack.runtime._get_session_lock_for_turn,
        )
        stack.context.turn_runner = runner
        bridge = EventBridge(SubscriptionManager(), ConnectionRegistry())

        async def emit(key, event, payload):
            wire_events.append((key, event, payload))
            await bridge.emit(key, event, payload)

        stack.runtime._event_emitter = emit
        stack.runtime.set_lifecycle_listener(_make_task_session_lifecycle_listener(
            session_manager=stack.manager, event_emitter=emit,
        ))

        async def handle(run):
            stack.received_runs.append(run)
            stack.handler_started.set()
            await dispatch_task_runtime_turn(
                run, config=config, session_manager=stack.manager,
                turn_runner=runner, event_emitter=emit,
            )

        stack.runtime._turn_handler = handle
        node = await stack.manager.get_session(SESSION_KEY)
        for index in range(8):
            await stack.manager.append_message(
                SESSION_KEY, "user" if index % 2 == 0 else "assistant",
                f"VIOLET-731 initial source {index}. " + "completed earlier work " * 100,
            )
        adapter = build_gateway_session_maintenance_adapter(stack.context)
        seeded = await adapter.compact({"key": SESSION_KEY})
        assert seeded["applied"] is True and seeded["durability"] == "durable"
        checkpoints = await _checkpoint_rows(stack.storage, node)
        assert len(checkpoints) == 1
        sources = [
            f"HIDDEN_HISTORY_{index:02d}. " + "completed background details " * 240
            for index in range(12)
        ]
        assert token_estimation.estimate_tokens("\n".join(sources)) > 32_768
        for index, source in enumerate(sources):
            await stack.manager.append_message(
                SESSION_KEY, "user" if index % 2 == 0 else "assistant", source,
            )
        canonical = await _rows(stack.manager, SESSION_KEY)
        original_write = stack.storage.rewrite_compacted_session
        original_read = stack.storage._read_transcript_rows
        armed = failure != "none"

        async def read(*args, **kwargs):
            if inspect.currentframe().f_back.f_code.co_name == "rewrite_compacted_session":
                monkeypatch.setattr(stack.storage, "_read_transcript_rows", original_read)
                error = sqlite3.OperationalError("database is locked")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error
            return await original_read(*args, **kwargs)

        async def write(**kwargs):
            nonlocal armed
            blocker = None
            if armed:
                armed = False
                if failure == "preflight_read":
                    monkeypatch.setattr(stack.storage, "_read_transcript_rows", read)
                else:
                    assert stack.storage._busy_budget_seconds == 2.0
                    blocker = sqlite3.connect(str(stack.db_path), timeout=0)
                    blocker.execute("BEGIN IMMEDIATE")
            try:
                result = await original_write(**kwargs)
                writer_results.append(("applied", result))
                return result
            except StorageBusyError as error:
                writer_results.append(("refused", error.operation, error.stage))
                raise
            finally:
                # Model a transient external writer: only the compaction
                # transaction is refused, ordinary finalization is writable.
                if blocker is not None:
                    blocker.rollback()
                    blocker.close()

        monkeypatch.setattr(stack.storage, "rewrite_compacted_session", write)
        remove_listener = add_compaction_listener(
            lambda key, payload: compactions.append(payload) if key == SESSION_KEY else None,
        )
        try:
            first = await get_dispatcher().dispatch("first", "chat.send", {
                "key": SESSION_KEY, "sessionKey": SESSION_KEY, "message": first_prompt,
                "clientRequestId": "writer-first", "clientMessageId": "writer-first-message",
                "_source": {"noMemoryCapture": True},
            }, stack.context)
            assert first.ok, first.error
            first_id = first.payload["task_id"]
            await asyncio.wait_for(first_request.wait(), timeout=30)
            if failure == "none":
                assert writer_results == [("applied", True)]
                assert len(await _checkpoint_rows(stack.storage, node)) == len(checkpoints) + 1
                assert not any(e.get("status") == "emergency_ephemeral" for e in compactions)
                assert "Temporary history window" not in ordinary_requests[0]
            else:
                assert writer_results == [(
                    "refused", "rewrite_compacted_session",
                    "preflight_read" if failure == "preflight_read" else "begin",
                )]
                assert await _checkpoint_rows(stack.storage, node) == checkpoints
                assert any(e.get("status") == "emergency_ephemeral" for e in compactions)
                assert not any(e.get("status") == "completed" for e in compactions)
                assert "Temporary history window" in ordinary_requests[0]

            second = await asyncio.wait_for(get_dispatcher().dispatch("second", "chat.send", {
                "key": SESSION_KEY, "sessionKey": SESSION_KEY, "message": next_prompt,
                "clientRequestId": "writer-next", "clientMessageId": "writer-next-message",
                "queueMode": "followup", "_source": {"noMemoryCapture": True},
            }, stack.context), timeout=30)
            assert second.ok, second.error
            second_id = second.payload["task_id"]
            queued = await stack.runtime.session_task_snapshot(SESSION_KEY)
            assert queued.running_task_id == first_id
            assert queued.queued_task_ids == (second_id,)
            assert len(stack.received_runs) == 1
            release_first.set()
            first_done = await stack.wait_for_terminal(first_id, timeout=30)
            second_done = await stack.wait_for_terminal(second_id, timeout=30)
            assert first_done.status == second_done.status == AgentTaskStatus.SUCCEEDED
            assert [run.task_id for run in stack.received_runs] == [first_id, second_id]
            assert len(ordinary_requests) == 2
            assert "Temporary history window" not in ordinary_requests[1]
            assert writer_results == (
                [("applied", True)] if failure == "none"
                else [writer_results[0], ("applied", True)]
            )
            assert len(await _checkpoint_rows(stack.storage, node)) == len(checkpoints) + 1
            assert (await stack.manager.get_session(SESSION_KEY)).compaction_count == 2
            after = await _rows(stack.manager, SESSION_KEY)
            assert after[:len(canonical)] == canonical
            for marker in (first_prompt, next_prompt, "CONTINUED_1", "CONTINUED_2"):
                assert sum(marker in str(row["content"]) for row in after) == 1
            assert not [event for _, event, _ in wire_events if event == "session.event.error"]
            assert sum(event == "session.event.done" for _, event, _ in wire_events) == 2
            snapshot = await stack.runtime.session_task_snapshot(SESSION_KEY)
            assert snapshot.running_task_id is None and not snapshot.queued_task_ids
            status = _task_state_summary(
                await stack.storage.list_agent_tasks(session_key=SESSION_KEY),
            )
            assert status["run_status"] == "idle" and status["active_task"] is None
            assert (await stack.manager.get_session(SESSION_KEY)).status == SessionStatus.DONE
            assert get_session_streams().live_snapshot(SESSION_KEY).task_id is None
            changes = [payload for _, event, payload in wire_events if event == "sessions.changed"]
            assert changes[-1]["run_status"] == "idle"
            # The v4 wire builder omits None-valued fields, including idle active_task.
            assert changes[-1].get("active_task") is None
            assert active_compaction_ids(SESSION_KEY) == ()
            assert not runner.get_session_lock(SESSION_KEY).locked()
            assert not stack.storage._operation_lock.locked()
        finally:
            release_first.set()
            remove_listener()
