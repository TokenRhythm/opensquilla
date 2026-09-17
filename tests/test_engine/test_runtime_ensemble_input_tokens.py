"""TurnRunner recovery must use request size without losing ensemble billing usage."""

from __future__ import annotations

from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.usage import UsageTracker
from opensquilla.engine.usage_accounting import current_usage_accounting_scope
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink
from opensquilla.provider import (
    ChatConfig,
    DoneEvent,
    Message,
    ReasoningDeltaEvent,
    TextDeltaEvent,
    ToolDefinition,
)
from opensquilla.provider.ensemble import EnsembleMemberConfig, EnsembleProvider
from opensquilla.provider.request_proof import project_final_request_payload
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import ProviderFinalRequestProjection, StreamEvent
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.types import CallerKind, ToolContext

_ANSWER = "The requested answer."
_REQUEST_INPUT_TOKENS = 10_000


@pytest.fixture(autouse=True)
def _fixed_session_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # Transcripts sort by (created_at, id); wall-clock adjustments are unrelated
    # to recovery or usage accounting. Equal timestamps retain insertion order.
    fixed_time = datetime(2024, 1, 1, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            if tz is None:
                return fixed_time.replace(tzinfo=None)
            return fixed_time.astimezone(tz)

    monkeypatch.setattr("opensquilla.session.models.datetime", FixedDateTime)


def _visible_response(model: str, text: str = _ANSWER) -> list[StreamEvent]:
    return [
        TextDeltaEvent(text=text),
        DoneEvent(
            stop_reason="stop",
            input_tokens=_REQUEST_INPUT_TOKENS,
            output_tokens=4,
            model=model,
        ),
    ]


@dataclass
class _ProviderRegistry:
    responses: dict[str, list[list[StreamEvent]]]
    calls: list[str] = field(default_factory=list)

    def build(self, config: ProviderConfig) -> _PhysicalProvider:
        assert config.provider == "fake"
        assert config.model in self.responses
        return _PhysicalProvider(config.model, self)


class _PhysicalProvider:
    provider_name = "fake"

    def __init__(self, model: str, registry: _ProviderRegistry) -> None:
        self.model = model
        self.registry = registry

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[StreamEvent]:
        # Every physical request must pass through the real usage accounting scope.
        scope = current_usage_accounting_scope()
        assert scope is not None
        assert scope.context.session_id
        attempt = self.registry.calls.count(self.model)
        self.registry.calls.append(self.model)
        responses = self.registry.responses[self.model]
        assert attempt < len(responses), "unexpected extra physical provider request"
        for event in responses[attempt]:
            yield event

    async def list_models(self) -> list[Any]:
        return []

    def project_final_request(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: ChatConfig | None = None,
        *,
        message_limit: int | None = None,
    ) -> ProviderFinalRequestProjection:
        config = config or ChatConfig()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                message.model_dump(mode="json", exclude_none=True) for message in messages
            ],
        }
        if config.system:
            payload["system"] = config.system
        if tools:
            payload["tools"] = [tool.model_dump(mode="json", exclude_none=True) for tool in tools]
        return project_final_request_payload(
            payload,
            projection_adapter="runtime_ensemble_test",
            proof_budget=int(config.provider_request_max_chars or 0),
            active_user_message_index=config.active_user_message_index,
            message_limit=message_limit,
        )


@pytest.mark.parametrize("proposer_input_tokens", [1_000, 30_000], ids=["low-total", "high-total"])
@pytest.mark.parametrize("response_kind", ["visible", "empty", "reasoning_length"])
async def test_ensemble_recovery_uses_terminal_request_size_and_preserves_usage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    proposer_input_tokens: int,
    response_kind: str,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENSQUILLA_RUNTIME_RECOVERY_MODE", "log")
    monkeypatch.setenv("OPENSQUILLA_REASONING_ONLY_ACT_NOW", "0")
    if response_kind == "visible":
        aggregate_responses = [_visible_response("aggregate")]
        failed_output_tokens = 0
    else:
        failed_output_tokens = 1024 if response_kind == "reasoning_length" else 0
        failure: list[StreamEvent] = []
        if response_kind == "reasoning_length":
            failure.append(ReasoningDeltaEvent(text="Considering the request."))
        failure.append(
            DoneEvent(
                stop_reason="length" if failed_output_tokens else "stop",
                input_tokens=_REQUEST_INPUT_TOKENS,
                output_tokens=failed_output_tokens,
                reasoning_tokens=failed_output_tokens,
                reasoning_content="Considering the request." if failed_output_tokens else None,
                model="aggregate",
            )
        )
        aggregate_responses = [failure, _visible_response("aggregate")]
    registry = _ProviderRegistry(
        responses={
            "draft": [[
                TextDeltaEvent(text="A candidate answer."),
                DoneEvent(
                    stop_reason="stop", input_tokens=proposer_input_tokens,
                    output_tokens=3, model="draft",
                ),
            ]],
            "aggregate": aggregate_responses,
            "fallback": [_visible_response("fallback", "A fallback answer.")],
        }
    )
    ensemble = EnsembleProvider(
        profile_name="test-profile",
        proposers=[EnsembleMemberConfig(
            provider_config=ProviderConfig(provider="fake", model="draft"),
            label="draft", thinking="high", max_tokens=1024,
        )],
        aggregator=EnsembleMemberConfig(
            provider_config=ProviderConfig(provider="fake", model="aggregate"),
            label="aggregate", thinking="high", max_tokens=1024,
        ),
        all_failed_policy="error",
        shuffle_candidates=False,
    )
    primary_config = ProviderConfig(
        provider="fake", model="aggregate", api_key="synthetic-public-dummy",
    )
    fallback_config = ProviderConfig(
        provider="fake", model="fallback", api_key="synthetic-public-dummy",
    )
    selector = ModelSelector(SelectorConfig(
        primary=primary_config,
        fallbacks=[fallback_config] if response_kind == "reasoning_length" else [],
    ))

    def build_selected_provider(config: ProviderConfig) -> EnsembleProvider | _PhysicalProvider:
        if config.model == "aggregate":
            return ensemble
        return registry.build(config)

    monkeypatch.setattr("opensquilla.provider.selector._build_provider", build_selected_provider)
    monkeypatch.setattr("opensquilla.provider.ensemble._build_provider", registry.build)
    config = GatewayConfig()
    config.squilla_router.enabled = False
    config.llm_ensemble.enabled = False
    config.llm.provider = "fake"
    config.llm.model = "aggregate"
    config.llm.api_key = "synthetic-public-dummy"
    config.llm.max_tokens = 1024
    config.llm.thinking = "high"
    config.workspace_dir = str(tmp_path)
    config.naming.enabled = False
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:ensemble-request-size"
    await manager.create(session_key)
    sink = SessionUsageEventSink(storage)
    try:
        runner = TurnRunner(
            provider_selector=selector,
            session_manager=manager,
            usage_tracker=UsageTracker(),
            usage_event_sink=sink,
            config=config,
        )
        with capture_logs() as logs:
            events = [event async for event in runner.run(
                "Give a short answer.", session_key,
                ToolContext(is_owner=True, caller_kind=CallerKind.CLI),
                history_has_persisted_user=False,
                no_memory_capture=True,
                max_provider_retries=1,
                persist_input=True,
            )]
        aggregate_calls = 1 if response_kind == "visible" else 2
        assert registry.calls == ["draft", *(["aggregate"] * aggregate_calls)]
        assert not [event for event in events if event.kind == "error"]
        done_events = [event for event in events if event.kind == "done"]
        assert len(done_events) == 1
        assert done_events[0].text == _ANSWER
        assert "".join(event.text for event in events if event.kind == "text_delta") == _ANSWER
        assert not [log for log in logs if log["event"] == "provider.large_context_visible_retry"]

        async with storage.conn.execute(
            "SELECT model, input_tokens, output_tokens, status "
            "FROM usage_events ORDER BY call_index"
        ) as cursor:
            ledger = [dict(row) for row in await cursor.fetchall()]
        assert Counter(row["model"] for row in ledger) == Counter(registry.calls)
        assert all(row["status"] == "finalized" for row in ledger)
        expected_input = proposer_input_tokens + _REQUEST_INPUT_TOKENS * aggregate_calls
        expected_output = 3 + 4 + failed_output_tokens
        assert sum(row["input_tokens"] for row in ledger) == expected_input
        assert sum(row["output_tokens"] for row in ledger) == expected_output
        assert done_events[0].input_tokens == expected_input
        assert done_events[0].output_tokens == expected_output
        session = await storage.get_session(session_key)
        assert session is not None
        assert session.input_tokens == expected_input
        assert session.output_tokens == expected_output
        transcript = await manager.get_transcript(session_key)
        assert transcript[-1].content == _ANSWER
    finally:
        await sink.close()
        await storage.close()
