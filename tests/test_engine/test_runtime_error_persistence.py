from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import ErrorEvent
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import ErrorEvent as ProviderError
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.session.storage import StaleEpochError
from opensquilla.session.terminal_reply import CONTEXT_PAYLOAD_TOO_LARGE_MESSAGES
from opensquilla.tools.types import CallerKind, ToolContext


class _RecordingSessionManager:
    def __init__(self) -> None:
        self.compact_calls: list[tuple[str, int]] = []
        self.messages: list[tuple[str, str, str]] = []
        self.append_calls: list[dict[str, object]] = []

    async def compact(self, session_key: str, budget: int) -> str:
        self.compact_calls.append((session_key, budget))
        return "summary"

    async def append_message(
        self,
        session_key: str,
        *,
        role: str,
        content: str,
        expected_session_id: str | None = None,
        expected_session_epoch: int | None = None,
        **kwargs: object,
    ) -> None:
        self.messages.append((session_key, role, content))
        self.append_calls.append(
            {
                "session_key": session_key,
                "role": role,
                "content": content,
                "expected_session_id": expected_session_id,
                "expected_session_epoch": expected_session_epoch,
                **kwargs,
            }
        )

    async def get_transcript(self, session_key: str) -> list[object]:
        del session_key
        return []

    async def get_session(self, session_key: str) -> SimpleNamespace:
        del session_key
        return SimpleNamespace(
            session_id="session-old",
            epoch=7,
            workspace_id=None,
            model_provider=None,
            model_override=None,
            model=None,
            provider_override=None,
        )


class _StaleAssistantSessionManager(_RecordingSessionManager):
    async def append_message(self, session_key: str, **kwargs: Any) -> None:
        await super().append_message(session_key, **kwargs)
        if kwargs.get("role") == "assistant":
            raise StaleEpochError("session owner rotated")


class _SingleReplyProvider:
    provider_name = "test"
    model = "fake-model"

    def chat(self, messages: list[object], tools=None, config=None) -> AsyncIterator[object]:
        del messages, tools, config
        return self._stream()

    async def _stream(self) -> AsyncIterator[object]:
        yield ProviderText(text="old task reply")
        yield ProviderDone(stop_reason="end_turn", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[object]:
        return []


class _SelectorClone:
    def __init__(self, provider: _SingleReplyProvider) -> None:
        self.provider = provider
        self.current_config = SimpleNamespace(model=provider.model)

    def resolve(self) -> _SingleReplyProvider:
        return self.provider

    def override_model(self, model: str) -> None:
        self.current_config.model = model
        self.provider.model = model


class _ProviderSelector:
    def __init__(self, provider: _SingleReplyProvider) -> None:
        self.provider = provider

    def clone(self) -> _SelectorClone:
        return _SelectorClone(self.provider)


@pytest.mark.parametrize(
    "outcome", ["loaded", "missing", "cancelled", "delayed", "cancelled_inflight"],
)
async def test_selected_skill_stream_and_history_match_actual_load(
    tmp_path, monkeypatch, outcome,
) -> None:
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.provider import ToolDefinition
    from opensquilla.skills.tree import compute_tree_sha256
    from opensquilla.skills.types import SkillLayer, SkillSpec

    monkeypatch.setattr("opensquilla.engine.runtime.log", MagicMock())
    directory = tmp_path / "report"
    directory.mkdir()
    (directory / "SKILL.md").write_text("Synthetic selected instructions", encoding="utf-8")
    spec = SkillSpec(
        "report", "Synthetic reporting", SkillLayer.PERSONAL, False, [],
        "Synthetic selected instructions", base_dir=str(directory),
        instance_id="synthetic-report", tree_digest=compute_tree_sha256(directory),
        disable_model_invocation=True,
    )
    snapshot = SimpleNamespace(skills=() if outcome == "missing" else (spec,), generation=1)
    digest_started, finish_digest = asyncio.Event(), asyncio.Event()
    loading_received = asyncio.Event()
    if outcome in {"delayed", "cancelled_inflight"}:
        original_to_thread = asyncio.to_thread

        async def delayed_digest(function, *args, **kwargs):
            if function is compute_tree_sha256:
                digest_started.set()
                await finish_digest.wait()
            return await original_to_thread(function, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", delayed_digest)
    if outcome == "cancelled":
        def cancel_digest(*_args):
            raise asyncio.CancelledError("synthetic stop")

        monkeypatch.setattr(
            "opensquilla.engine.steps.selected_skills.compute_tree_sha256", cancel_digest,
        )
    calls = []

    class RecordingProvider(_SingleReplyProvider):
        def chat(self, messages, tools=None, config=None):
            calls.append((messages, config))
            return self._stream()

    manager = _RecordingSessionManager()
    config = GatewayConfig()
    config.squilla_router.enabled = False
    config.skills.injection_mode = "system"
    runner = TurnRunner(
        provider_selector=_ProviderSelector(RecordingProvider()),
        session_manager=manager, config=config,
    )
    monkeypatch.setattr(runner, "_resolve_skill_catalog", lambda: snapshot)
    monkeypatch.setattr(
        runner, "_build_tools",
        lambda *args, **kwargs: ([ToolDefinition(
            name="skill_view", description="Read skill", input_schema={"type": "object"},
        )], None),
    )
    context = ToolContext(
        is_owner=True, caller_kind=CallerKind.WEB,
        selected_skills=({
            "name": spec.name, "instanceId": spec.instance_id, "digest": spec.tree_digest,
        },),
    )
    events = []

    async def consume():
        async for event in runner.run(
            "Synthetic request", "agent:main:webchat:selected-skill", context,
            no_memory_capture=True, input_mode="text",
        ):
            events.append(event)
            if event.kind == "skill_load" and event.content["status"] == "loading":
                loading_received.set()

    if outcome in {"delayed", "cancelled_inflight"}:
        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(digest_started.wait(), timeout=5)
            # The UI receives progress before the body check or assembly completes.
            await asyncio.wait_for(loading_received.wait(), timeout=5)
            assert not calls
            if outcome == "cancelled_inflight":
                task.cancel("synthetic stop")
                with pytest.raises(asyncio.CancelledError, match="synthetic stop"):
                    await task
            else:
                finish_digest.set()
                await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    elif outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError, match="synthetic stop"):
            await consume()
    else:
        await consume()
    loads = [event for event in events if event.kind == "skill_load"]
    loaded = outcome in {"loaded", "delayed"}
    assert [event.content["status"] for event in loads] == [
        "loading", "loaded" if loaded else "failed",
    ]
    assert all(event.content["turnId"] for event in loads)
    assert bool(calls) is loaded
    assert bool([event for event in events if event.kind == "error"]) is (outcome == "missing")
    persisted = [
        segment
        for row in manager.append_calls
        for segment in row.get("tool_calls", []) or []
        if segment.get("type") == "skill_load"
    ]
    assert [row["status"] for row in persisted] == [event.content["status"] for event in loads]
    if loaded:
        assert "Synthetic selected instructions" in str(calls[0])
    elif outcome in {"cancelled", "cancelled_inflight"}:
        assert "cancelled" in loads[-1].content["error"]
        assert any(event.kind == "control_terminal" for event in events)


@pytest.mark.asyncio
async def test_stream_failure_keeps_durable_ref_and_execution_context() -> None:
    class FailingProvider(_SingleReplyProvider):
        provider_name = "openai"

        async def _stream(self) -> AsyncIterator[object]:
            yield ProviderText(text="Partial answer")
            yield ProviderError(message="synthetic invalid api key", code="401")

    records: list[dict[str, Any]] = []

    class Writer:
        def record_error(self, record):
            records.append(record)
            return True

    manager = _RecordingSessionManager()
    runner = TurnRunner(
        provider_selector=_ProviderSelector(FailingProvider()),
        session_manager=manager,
        turn_error_writer=Writer(),
        config=SimpleNamespace(context_window_tokens=100_000),
    )
    events = [event async for event in runner.run(
        "hello", "agent:main:webchat:test",
        ToolContext(is_owner=True, caller_kind=CallerKind.WEB),
        no_memory_capture=True,
        input_mode="text",
    )]
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(errors) == len(records) == 1
    assert errors[0].failure_kind == "auth_invalid"
    assert errors[0].error_id == records[0]["error_id"]
    assert records[0]["turn_id"]
    assert records[0]["provider"] == "openai"
    assert records[0]["model"] == "fake-model"
    assert records[0]["surface"] == "text"
    assert any(f"(ref: {errors[0].error_id})" in content for _, _, content in manager.messages)


@pytest.mark.asyncio
async def test_stream_error_records_without_session_manager_and_does_not_duplicate() -> None:
    records: list[dict[str, Any]] = []

    class Writer:
        def record_error(self, record):
            records.append(record)
            return True

    runner = TurnRunner(provider_selector=None, turn_error_writer=Writer())
    event = ErrorEvent(message="Synthetic failure", code="provider_error")
    await runner._persist_turn_error(
        "agent:main:test:writer", event, turn_id="turn-1", surface="text",
        provider="test", model="fake-model", fallback_hops=2,
    )
    await runner._persist_turn_error("agent:main:test:writer", event)
    assert len(records) == 1
    assert event.error_id == records[0]["error_id"]
    assert records[0]["turn_id"] == "turn-1"
    assert records[0]["fallback_hops"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_stream_error_failed_diagnostics_do_not_create_ref(raises: bool) -> None:
    class Writer:
        def record_error(self, record):
            if raises:
                raise RuntimeError("synthetic diagnostic failure")
            return False

    manager = _RecordingSessionManager()
    runner = TurnRunner(provider_selector=None, session_manager=manager, turn_error_writer=Writer())
    event = ErrorEvent(message="Synthetic failure", code="provider_error")
    await runner._persist_turn_error("agent:main:test:writer", event)
    assert event.error_id == ""
    assert manager.messages[0][2] == "Error: Synthetic failure"


@pytest.mark.asyncio
async def test_provider_request_too_large_error_persistence_does_not_compact_transcript() -> None:
    manager = _RecordingSessionManager()
    runner = TurnRunner(
        provider_selector=None,
        session_manager=manager,
        config=SimpleNamespace(context_window_tokens=100_000),
    )

    await runner._persist_turn_error(
        "agent:main:webchat:test",
        ErrorEvent(
            message=(
                "The request is too large for the provider context window after "
                "automatic context compaction and payload reduction."
            ),
            code="provider_request_too_large",
        ),
    )

    assert manager.compact_calls == []
    assert manager.messages == [
        (
            "agent:main:webchat:test",
            "system",
            "Error: The request is too large for the provider context window after "
            "automatic context compaction and payload reduction. OpenSquilla "
            "preserved the recoverable state; retry with a narrower request "
            "or a larger-context model.",
        )
    ]


@pytest.mark.parametrize("message", CONTEXT_PAYLOAD_TOO_LARGE_MESSAGES.values())
async def test_specific_request_budget_reason_survives_error_persistence(message: str) -> None:
    manager = _RecordingSessionManager()
    runner = TurnRunner(
        provider_selector=None, session_manager=manager,
        config=SimpleNamespace(context_window_tokens=100_000),
    )

    await runner._persist_turn_error(
        "agent:main:webchat:test", ErrorEvent(message=message, code="provider_request_too_large"),
    )

    assert manager.compact_calls == []
    assert manager.messages == [("agent:main:webchat:test", "system", f"Error: {message}")]


@pytest.mark.asyncio
async def test_provider_output_truncation_error_persistence_uses_terminal_reply() -> None:
    manager = _RecordingSessionManager()
    runner = TurnRunner(
        provider_selector=None,
        session_manager=manager,
        config=SimpleNamespace(context_window_tokens=100_000),
    )

    await runner._persist_turn_error(
        "agent:main:webchat:test",
        ErrorEvent(
            message="Provider output limit reached before completion",
            code="provider_output_truncated",
        ),
    )

    assert manager.compact_calls == []
    assert manager.messages == [
        (
            "agent:main:webchat:test",
            "system",
            "The provider stopped because the output limit was reached before the task finished.",
        )
    ]


@pytest.mark.asyncio
async def test_provider_output_truncation_error_persistence_uses_message_fallback() -> None:
    manager = _RecordingSessionManager()
    runner = TurnRunner(
        provider_selector=None,
        session_manager=manager,
        config=SimpleNamespace(context_window_tokens=100_000),
    )

    with patch("opensquilla.engine.runtime.log") as log:
        await runner._persist_turn_error(
            "agent:main:webchat:test",
            ErrorEvent(
                message="Provider output limit reached before completion",
                code="agent_error",
            ),
        )

    assert manager.messages == [
        (
            "agent:main:webchat:test",
            "system",
            "The provider stopped because the output limit was reached before the task finished.",
        )
    ]
    log.info.assert_called_once()
    assert log.info.call_args.kwargs["code"] == "provider_output_truncated"
    assert log.info.call_args.kwargs["turn_outcome"]["kind"] == "partial"


@pytest.mark.asyncio
async def test_terminal_reset_error_records_row_without_duplicate_transcript_message() -> None:
    manager = _RecordingSessionManager()
    runner = TurnRunner(
        provider_selector=None,
        session_manager=manager,
        config=SimpleNamespace(context_window_tokens=100_000),
    )
    record_error = AsyncMock(return_value="error-terminal-reset")
    runner._record_turn_error = record_error

    await runner._persist_turn_error(
        "agent:main:webchat:test",
        ErrorEvent(
            message="The fallback model also failed.",
            code="ensemble_fixed_error",
            failure_kind="provider_error",
        ),
        append_transcript=False,
    )

    record_error.assert_awaited_once()
    assert manager.messages == []


@pytest.mark.asyncio
async def test_task_owned_no_provider_error_append_keeps_frozen_session_owner() -> None:
    manager = _RecordingSessionManager()
    runner = TurnRunner(
        provider_selector=None,
        session_manager=manager,
        config=SimpleNamespace(context_window_tokens=100_000),
    )

    events = [
        event
        async for event in runner.run(
            "hello",
            "agent:main:webchat:test",
            ToolContext(is_owner=True, caller_kind=CallerKind.WEB),
            history_has_persisted_user=False,
            no_memory_capture=True,
            expected_session_id="session-old",
            expected_session_epoch=7,
        )
    ]

    assert any(isinstance(event, ErrorEvent) and event.code == "no_provider" for event in events)
    assert manager.append_calls == [
        {
            "session_key": "agent:main:webchat:test",
            "role": "system",
            "content": "Error: No provider available",
            "expected_session_id": "session-old",
            "expected_session_epoch": 7,
        }
    ]


@pytest.mark.asyncio
async def test_standalone_turn_rejects_stale_owner_before_provider_dispatch() -> None:
    class ReplacementSessionManager(_RecordingSessionManager):
        async def get_session(self, session_key: str) -> SimpleNamespace:
            current = await super().get_session(session_key)
            current.session_id = "session-new"
            current.epoch = 8
            return current

    class RecordingProvider(_SingleReplyProvider):
        def __init__(self) -> None:
            self.chat_calls = 0

        def chat(self, messages, tools=None, config=None):
            self.chat_calls += 1
            return super().chat(messages, tools=tools, config=config)

    manager = ReplacementSessionManager()
    provider = RecordingProvider()
    runner = TurnRunner(
        provider_selector=_ProviderSelector(provider),
        session_manager=manager,
        config=SimpleNamespace(context_window_tokens=100_000),
    )

    with pytest.raises(StaleEpochError, match="before provider dispatch"):
        async for _event in runner.run(
            "hello",
            "agent:main:webchat:test",
            ToolContext(is_owner=True, caller_kind=CallerKind.WEB),
            history_has_persisted_user=False,
            no_memory_capture=True,
            expected_session_id="session-old",
            expected_session_epoch=7,
        ):
            pass

    assert provider.chat_calls == 0
    assert manager.append_calls == []


@pytest.mark.asyncio
async def test_task_owned_context_exhaustion_skips_compaction_and_keeps_owner() -> None:
    manager = _RecordingSessionManager()
    runner = TurnRunner(
        provider_selector=None,
        session_manager=manager,
        config=SimpleNamespace(context_window_tokens=100_000),
    )

    await runner._persist_turn_error(
        "agent:main:webchat:test",
        ErrorEvent(
            message="The accepted turn no longer fits in the current context.",
            code="current_turn_context_exhausted",
        ),
        expected_session_id="session-old",
        expected_session_epoch=7,
    )

    assert manager.compact_calls == []
    assert len(manager.append_calls) == 1
    assert manager.append_calls[0]["session_key"] == "agent:main:webchat:test"
    assert manager.append_calls[0]["role"] == "system"
    assert str(manager.append_calls[0]["content"]).startswith("Error:")
    assert manager.append_calls[0]["expected_session_id"] == "session-old"
    assert manager.append_calls[0]["expected_session_epoch"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("physical_window", [None, 1_000_000])
async def test_ownerless_context_exhaustion_never_uses_legacy_soft_budget(
    physical_window: int | None,
) -> None:
    manager = _RecordingSessionManager()
    runner = TurnRunner(
        provider_selector=None,
        session_manager=manager,
        config=SimpleNamespace(
            context_budget_tokens=100_000,
            context_window_tokens=physical_window,
        ),
    )

    await runner._persist_turn_error(
        "agent:main:webchat:test",
        ErrorEvent(
            message="The accepted turn no longer fits in the current context.",
            code="current_turn_context_exhausted",
        ),
    )

    assert manager.compact_calls == []
    assert len(manager.append_calls) == 1
    assert str(manager.append_calls[0]["content"]).startswith("Error:")


@pytest.mark.asyncio
async def test_stale_finalizer_append_does_not_fall_back_to_unfenced_error_row() -> None:
    manager = _StaleAssistantSessionManager()
    runner = TurnRunner(
        provider_selector=_ProviderSelector(_SingleReplyProvider()),
        session_manager=manager,
        config=SimpleNamespace(context_window_tokens=100_000),
    )

    with pytest.raises(StaleEpochError, match="session owner rotated"):
        async for _event in runner.run(
            "hello",
            "agent:main:webchat:test",
            ToolContext(is_owner=True, caller_kind=CallerKind.WEB),
            history_has_persisted_user=False,
            no_memory_capture=True,
            expected_session_id="session-old",
            expected_session_epoch=7,
        ):
            pass

    assert len(manager.append_calls) == 1
    assert manager.append_calls[0]["role"] == "assistant"
    assert manager.append_calls[0]["expected_session_id"] == "session-old"
    assert manager.append_calls[0]["expected_session_epoch"] == 7
    assert not str(manager.append_calls[0]["content"]).startswith("Error:")
