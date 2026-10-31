from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.history import TOOL_IMAGE_NATIVE_ACK_CONTEXT_KEY
from opensquilla.engine.turn_runner.turn_finalizer_stage import (
    TurnFinalizerStage,
    TurnFinalizerStageInput,
)
from opensquilla.engine.types import DoneEvent, public_agent_event_payload
from opensquilla.provider.types import Message
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.session.turn_context import current_turn_context, turn_context_scope


@pytest.mark.asyncio
async def test_native_image_ack_is_durable_without_changing_replay_or_causal_context() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:synthetic-image-ack-context"
    await manager.create(session_key)

    async def append_message(key: str, **kwargs: Any) -> bool:
        await manager.append_message(key, **kwargs)
        return True

    async def noop(**_kwargs: Any) -> None:
        return None

    stage = TurnFinalizerStage(
        transcript_append=SimpleNamespace(append_message=append_message),
        turn_memory_capture=SimpleNamespace(capture_turn=noop),
        session_totals=SimpleNamespace(rollup=noop),
        turn_error_persist=SimpleNamespace(persist_error=noop),
    )
    replay = {
        "version": 1,
        "messages": [
            Message(role="assistant", content="The frame is blue.").model_dump(mode="json")
        ],
    }
    done = DoneEvent(
        text="The frame is blue.",
        assistant_replay=replay,
        tool_image_native_ack_indexes=[0],
    )
    inp = TurnFinalizerStageInput(
        final_text_parts=[done.text],
        turn_segments=[],
        turn_artifacts=[],
        error_message=None,
        pending_error_event=None,
        done_event=done,
        runtime_message="Inspect the frame.",
        input_mode="user",
        input_provenance=None,
        resolved_model="synthetic-vision-model",
        agent_id="main",
        session_key=session_key,
        tool_context=None,
        run_kind="default",
        heartbeat_ack_max_chars=300,
        no_memory_capture=True,
    )
    try:
        with turn_context_scope({"turn_id": "synthetic-turn"}):
            await stage.run(inp)
            assert current_turn_context() == {"turn_id": "synthetic-turn"}
        entries = await manager.get_transcript(session_key)
        assert len(entries) == 1
        assert entries[0].assistant_replay == replay
        assert entries[0].turn_context == {
            "turn_id": "synthetic-turn",
            TOOL_IMAGE_NATIVE_ACK_CONTEXT_KEY: [0],
        }
        assert "tool_image_native_ack_indexes" not in public_agent_event_payload(done)
    finally:
        await storage.close()
