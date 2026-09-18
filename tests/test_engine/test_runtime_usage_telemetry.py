"""The shared turn loop keeps daily usage and V2 result reporting active together."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla import token_estimation
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import DoneEvent, ErrorEvent
from opensquilla.gateway.config import GatewayConfig
from opensquilla.observability import install_telemetry, network_policy
from opensquilla.provider import ChatConfig, Message
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.telemetry.contracts.common import ResultOutcome
from opensquilla.telemetry.runtime_facts import TurnReliabilityFacts
from opensquilla.tools.types import CallerKind, ToolContext


class _MeteredProvider:
    provider_name = "fake"

    async def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        yield ProviderText(text="A synthetic ")
        yield ProviderText(text="answer.")
        yield ProviderDone(
            input_tokens=120,
            output_tokens=24,
            cached_tokens=40,
            cache_write_tokens=6,
        )


class _SingleProviderSelector:
    active_provider_id = "fake"

    def __init__(self) -> None:
        self.current_config = SimpleNamespace(model="test-model")

    def clone(self) -> _SingleProviderSelector:
        return _SingleProviderSelector()

    def resolve(self) -> _MeteredProvider:
        return _MeteredProvider()

    def override_model(self, model: str) -> None:
        self.current_config = SimpleNamespace(model=model)


def _enable_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    # Collection stays local; avoid optional tokenizer downloads in the turn loop.
    monkeypatch.setattr(token_estimation, "_get_encoding", lambda: None)
    for name in (
        network_policy.NETWORK_OBSERVABILITY_DISABLED_ENV,
        install_telemetry.TELEMETRY_DISABLED_ENV,
        network_policy.LEGACY_UPDATE_CHECK_DISABLED_ENV,
        network_policy.DO_NOT_TRACK_ENV,
        network_policy.PRODUCT_ANALYTICS_DISABLED_ENV,
        "PYTEST_CURRENT_TEST",
        "GITHUB_ACTIONS",
        "OPENSQUILLA_TESTING",
        "CI",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("run_kind", ["default", "web_turn", "channel_turn"])
async def test_completed_turn_records_daily_usage_and_v2_result(
    tmp_path, monkeypatch: pytest.MonkeyPatch, run_kind: str
) -> None:
    _enable_telemetry(monkeypatch)
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    manager = SessionManager(storage)
    session_key = "agent:main:telemetry-test"
    await manager.create(session_key)
    facts: list[TurnReliabilityFacts] = []
    runner = TurnRunner(
        provider_selector=_SingleProviderSelector(),
        session_manager=manager,
        config=GatewayConfig(
            workspace_dir=str(tmp_path),
            state_dir=str(tmp_path / "state"),
            squilla_router={"enabled": False},
        ),
        turn_reliability_sink=facts.append,
    )
    try:
        for turn_number in range(1, 3):
            events = [
                event
                async for event in runner.run(
                    "A synthetic question.",
                    session_key,
                    tool_context=ToolContext(session_key=session_key, caller_kind=CallerKind.CLI),
                    run_kind=run_kind,
                    history_has_persisted_user=False,
                    no_memory_capture=True,
                )
            ]

            assert not [event for event in events if isinstance(event, ErrorEvent)]
            assert len([event for event in events if isinstance(event, DoneEvent)]) == 1
            rows = await storage.list_pending_daily_usage(before_day="9999-12-31")
            assert len(rows) == 1
            assert rows[0]["conversation_turns"] == turn_number
            assert rows[0]["input_tokens"] == 120 * turn_number
            assert rows[0]["output_tokens"] == 24 * turn_number
            assert rows[0]["cached_tokens"] == 40 * turn_number
            assert rows[0]["cache_write_tokens"] == 6 * turn_number
            assert rows[0]["uploaded_at"] is None
            assert len(facts) == turn_number
            assert all(fact.outcome is ResultOutcome.SUCCESS for fact in facts)
    finally:
        await storage.close()


async def test_daily_usage_storage_failure_preserves_turn_and_v2_result(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_telemetry(monkeypatch)
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    manager = SessionManager(storage)
    session_key = "agent:main:telemetry-failure-test"
    await manager.create(session_key)
    facts: list[TurnReliabilityFacts] = []
    record_attempts = 0

    async def fail_record(**kwargs: Any) -> None:
        nonlocal record_attempts
        record_attempts += 1
        raise RuntimeError("Synthetic daily telemetry storage failure")

    monkeypatch.setattr(storage, "record_daily_usage", fail_record)
    runner = TurnRunner(
        provider_selector=_SingleProviderSelector(),
        session_manager=manager,
        config=GatewayConfig(
            workspace_dir=str(tmp_path),
            state_dir=str(tmp_path / "state"),
            squilla_router={"enabled": False},
        ),
        turn_reliability_sink=facts.append,
    )
    try:
        events = [
            event
            async for event in runner.run(
                "A synthetic question.",
                session_key,
                tool_context=ToolContext(session_key=session_key, caller_kind=CallerKind.CLI),
                history_has_persisted_user=False,
                no_memory_capture=True,
            )
        ]

        assert record_attempts == 1
        assert not [event for event in events if isinstance(event, ErrorEvent)]
        done = [event for event in events if isinstance(event, DoneEvent)]
        assert len(done) == 1
        assert done[0].text == "A synthetic answer."
        assert len(facts) == 1
        assert facts[0].outcome is ResultOutcome.SUCCESS
        session = await manager.get_session(session_key)
        assert session is not None
        assert session.input_tokens == 120
        assert session.output_tokens == 24
    finally:
        await storage.close()


async def test_ephemeral_turn_keeps_only_daily_counters_across_restart(tmp_path, monkeypatch):
    from opensquilla.observability.daily_usage_store import DailyUsageStore
    from opensquilla.observability.usage_telemetry import (
        StandaloneUsageTelemetry,
        standalone_daily_usage_path,
    )

    _enable_telemetry(monkeypatch)
    storage = await SessionStorage.open(":memory:")
    manager = SessionManager(storage)
    session_key = "agent:main:ephemeral-usage"
    await manager.create(session_key)
    config = GatewayConfig(
        workspace_dir=str(tmp_path / "workspace"), state_dir=str(tmp_path / "state"),
        squilla_router={"enabled": False},
    )
    usage = StandaloneUsageTelemetry(config=config, legacy_storage=storage)
    runner = TurnRunner(
        provider_selector=_SingleProviderSelector(), session_manager=manager,
        config=config, usage_telemetry=usage,
    )
    try:
        events = [event async for event in runner.run(
            "A synthetic question.", session_key,
            tool_context=ToolContext(session_key=session_key, caller_kind=CallerKind.CLI),
            no_memory_capture=True, history_has_persisted_user=False,
        )]
        assert not [event for event in events if isinstance(event, ErrorEvent)]
        assert len([event for event in events if isinstance(event, DoneEvent)]) == 1
        assert await storage.list_pending_daily_usage(before_day="9999-12-31") == []
    finally:
        # The day remains open, so close must not start a usage HTTP request.
        await usage.close()
        await storage.close()
    restored = await DailyUsageStore.open(standalone_daily_usage_path(config))
    try:
        rows = await restored.list_pending_daily_usage(before_day="9999-12-31")
        assert len(rows) == 1
        assert rows[0]["conversation_turns"] == 1
        assert rows[0]["input_tokens"] == 120
        assert rows[0]["output_tokens"] == 24
        assert rows[0]["cached_tokens"] == 40
        assert rows[0]["cache_write_tokens"] == 6
        assert not (tmp_path / "state" / "sessions.db").exists()
    finally:
        await restored.close()
