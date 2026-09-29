"""Existing whole-turn deadlines cover preflight before Agent.run_turn starts."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

from opensquilla.engine.cache_break_monitor import active_compaction_ids
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import ControlTerminalEvent, ControlTerminalReason, ErrorEvent
from opensquilla.engine.usage_accounting import (
    UsageAccountingBusyError,
    UsageAccountingUnavailableError,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import DoneEvent, ProviderMessageLimitProof, TextDeltaEvent
from opensquilla.provider.types import ErrorEvent as ProviderErrorEvent
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.types import CallerKind, ToolContext


@pytest_asyncio.fixture
async def deadline_turn(tmp_path, monkeypatch):
    from opensquilla import token_estimation

    monkeypatch.setattr(token_estimation, "_encoding", token_estimation._ENCODING_UNAVAILABLE)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "profile"))
    workspace = tmp_path / "w"
    workspace.mkdir()
    config = GatewayConfig(
        workspace_dir=str(workspace),
        llm={"provider": "openai", "model": "synthetic-deadline", "api_key": "unused-test",
             "base_url": "https://example.invalid/v1", "context_window_tokens": 16_000,
             "max_tokens": 1024, "thinking": "off"},
        prompt={"mode": "minimal"}, squilla_router={"enabled": False},
        llm_ensemble={"enabled": False},
    )
    config.attachments.media_root = str(tmp_path / "media")
    storage = SessionStorage(str(tmp_path / "deadline.sqlite"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False, checkpoint_workspace_dir=workspace)
    node = await manager.create("agent:main:webchat:parent-deadline")
    for index in range(12):
        await manager.append_message(
            node.session_key, "user" if index % 2 == 0 else "assistant",
            "Completed background discussion. " * 240,
        )
    selector = ModelSelector(SelectorConfig(primary=ProviderConfig(
        provider="openai", model="synthetic-deadline", api_key="unused-test",
        base_url="https://example.invalid/v1",
    )))
    runner = TurnRunner(provider_selector=selector, session_manager=manager,
                        config=config, model_catalog=ModelCatalog())
    case = SimpleNamespace(runner=runner, storage=storage, manager=manager, node=node,
                           workspace=workspace, calls=[], summary_cancelled=False)
    try:
        yield case
    finally:
        await storage.close()


async def _run(case, timeout=2):
    return [event async for event in case.runner.run(
        "Continue the current task.", case.node.session_key,
        ToolContext(is_owner=True, caller_kind=CallerKind.WEB,
                    session_key=case.node.session_key, workspace_dir=str(case.workspace)),
        persist_input=False, history_has_persisted_user=False, no_memory_capture=True,
        max_iterations=1, max_provider_retries=0, length_capped_continuations=0,
        expected_session_id=case.node.session_id, expected_session_epoch=case.node.epoch,
        timeout=timeout,
    )]


def _assert_deadline(events):
    controls = [event for event in events if isinstance(event, ControlTerminalEvent)]
    assert len(controls) == 1, events
    assert controls[0].reason == ControlTerminalReason.HARD_DEADLINE
    assert not any(isinstance(event, ErrorEvent) for event in events), events


async def _assert_clean(case):
    key = case.node.session_key
    assert active_compaction_ids(key) == ()
    assert not case.runner.get_session_lock(key).locked()
    assert key not in case.runner._compaction_failures
    assert not case.runner.has_attempted_compaction_this_turn(key)
    assert not await case.storage.list_agent_tasks(session_key=key)


async def test_expired_turn_deadline_never_starts_summary_or_ordinary_call(
    deadline_turn, monkeypatch,
):
    case = deadline_turn
    monkeypatch.setattr("opensquilla.engine.runtime._deadline_from_timeout",
                        lambda _: time.monotonic() - 1)

    async def unexpected_call(*args, **kwargs):
        case.calls.append("unexpected")
        raise AssertionError("expired turn dispatched a model request")
        yield  # pragma: no cover - async stream shape

    monkeypatch.setattr(OpenAIProvider, "chat", unexpected_call)
    _assert_deadline(await _run(case))
    assert case.calls == []
    assert (await case.manager.get_session(case.node.session_key)).compaction_count == 0
    await _assert_clean(case)


async def test_parent_timer_expiry_classifies_deadline_despite_lagging_clock(
    deadline_turn, monkeypatch,
):
    case = deadline_turn
    parent_deadline = time.monotonic() + 30
    monkeypatch.setattr("opensquilla.engine.runtime._deadline_from_timeout",
                        lambda _: parent_deadline)
    real_timeout = asyncio.timeout_at

    class ExpiredTimer:
        async def __aenter__(self):
            raise TimeoutError("Synthetic event-loop timer fired before the next clock read")

        async def __aexit__(self, *args):
            return False

        def expired(self):
            return True

    monkeypatch.setattr(asyncio, "timeout_at", lambda deadline: (
        ExpiredTimer() if deadline == parent_deadline else real_timeout(deadline)
    ))
    _assert_deadline(await _run(case))
    assert time.monotonic() < parent_deadline
    await _assert_clean(case)


@pytest.mark.ci_serial
async def test_parent_deadline_cancels_inflight_preflight_without_auxiliary_failure(
    deadline_turn, monkeypatch,
):
    case = deadline_turn

    async def waiting_summary(self, messages, tools=None, config=None):
        summary = config.candidate_output_mode == "inert_artifact"
        case.calls.append(summary)
        assert summary
        assert config.turn_deadline_at_monotonic is not None
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            case.summary_cancelled = True
            raise
        yield  # pragma: no cover - async stream shape

    monkeypatch.setattr(OpenAIProvider, "chat", waiting_summary)
    async with asyncio.timeout(6):
        events = await _run(case)
    _assert_deadline(events)
    assert case.calls == [True]
    assert case.summary_cancelled
    assert (await case.manager.get_session(case.node.session_key)).compaction_count == 0
    await _assert_clean(case)


@pytest.mark.ci_serial
async def test_parent_deadline_after_sqlite_commit_does_not_rollback_checkpoint(
    deadline_turn, monkeypatch,
):
    case = deadline_turn
    committed = asyncio.Event()
    canonical = await case.manager.get_canonical_transcript(case.node.session_key)
    rewrite = case.storage.rewrite_compacted_session

    async def committed_then_wait(**kwargs):
        result = await rewrite(**kwargs)
        assert result
        committed.set()
        # The owner must settle a started SQLite commit before unwinding a
        # cancellation. Delay its acknowledgement beyond the parent deadline,
        # while still allowing that shielded settlement to complete.
        await asyncio.sleep(max(0.0, case.parent_deadline - time.monotonic()) + 0.1)
        return result

    async def summary(self, messages, tools=None, config=None):
        case.calls.append(config.candidate_output_mode == "inert_artifact")
        case.parent_deadline = config.turn_deadline_at_monotonic
        yield TextDeltaEvent(
            text="Earlier background discussion is complete. Continue current work.",
        )
        yield DoneEvent(stop_reason="stop", input_tokens=100, output_tokens=12)

    monkeypatch.setattr(case.storage, "rewrite_compacted_session", committed_then_wait)
    monkeypatch.setattr(OpenAIProvider, "chat", summary)
    async with asyncio.timeout(15):
        events = await _run(case, timeout=10)
    _assert_deadline(events)
    assert committed.is_set()
    assert case.calls and all(case.calls)
    assert (await case.manager.get_session(case.node.session_key)).compaction_count == 1
    assert len(await case.storage.get_all_summaries(case.node.session_id)) == 1
    assert await case.manager.get_canonical_transcript(case.node.session_key) == canonical
    await _assert_clean(case)


@pytest.mark.parametrize("error_type", [UsageAccountingUnavailableError, UsageAccountingBusyError])
@pytest.mark.parametrize("entry", ["preflight", "message_count"])
async def test_summary_accounting_error_is_real_main_error_not_cancellation(
    deadline_turn, monkeypatch, error_type, entry,
):
    case = deadline_turn
    if entry != "preflight":
        async def skip_preflight(*args, **kwargs):
            return None
        monkeypatch.setattr(case.runner, "_maybe_preflight_compact", skip_preflight)

    async def unavailable(self, messages, tools=None, config=None):
        summary = config.candidate_output_mode == "inert_artifact"
        case.calls.append(summary)
        if not summary and entry == "message_count":
            projection = self.project_message_count(messages, config)
            assert projection.actual_wire_messages > 6
            yield ProviderErrorEvent(
                code="400", message="Request has too many messages",
                message_limit_proof=ProviderMessageLimitProof(
                    actual_wire_messages=projection.actual_wire_messages, limit=6,
                    logical_messages=projection.logical_messages,
                    system_messages=projection.system_messages,
                    tool_result_messages=projection.tool_result_messages,
                    provider_kind=projection.provider_kind, model=projection.model,
                    base_host=projection.base_host,
                ),
            )
            return
        assert summary
        raise error_type("Synthetic accounting admission failure")
        yield  # pragma: no cover - async stream shape

    monkeypatch.setattr(OpenAIProvider, "chat", unavailable)
    events = await _run(case, timeout=10)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(errors) == 1 and errors[0].code == error_type.code, events
    assert not any(isinstance(event, ControlTerminalEvent) for event in events)
    assert case.calls == ([True] if entry == "preflight" else [False, True])
    await _assert_clean(case)
