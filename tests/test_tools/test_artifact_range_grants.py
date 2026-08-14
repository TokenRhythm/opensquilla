from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.provider import ChatConfig, Message
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderTextDelta
from opensquilla.tools.builtin.artifact_range_grants import (
    ArtifactRangeBinding,
    ArtifactRangeGrantError,
    ArtifactRangeGrantRegistry,
    clear_context_registry,
    registry_for_context,
)
from opensquilla.tools.types import ToolContext


class _RangeLifecycleProvider:
    provider_name = "fake"

    def __init__(self, *, block: bool = False) -> None:
        self.block = block
        self.started = asyncio.Event()

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools, config
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
            return
        yield ProviderTextDelta(text="done")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


def _binding(*, task_id: str = "turn-1", sha256: str = "a" * 64) -> ArtifactRangeBinding:
    return ArtifactRangeBinding(
        task_id=task_id,
        session_key="agent:main:webchat:test",
        session_id="session-test",
        session_epoch=4,
        document_id="document-test",
        revision_id="revision-test",
        source_sha256=sha256,
    )


def test_registry_is_turn_local_bounded_and_explicitly_clearable() -> None:
    first_ctx = SimpleNamespace(task_id="turn-1", session_key="session-key")
    second_ctx = SimpleNamespace(task_id="turn-2", session_key="session-key")
    first = registry_for_context(first_ctx)
    assert registry_for_context(first_ctx) is first
    assert registry_for_context(second_ctx) is not first

    token = first.mint_range(
        binding=_binding(),
        source="<h1>Before</h1>",
        start=4,
        end=10,
        kind="text_content",
        annotation_orders=(0,),
    )
    assert re.fullmatch(r"hrg_[A-Za-z0-9_-]{43}", token)

    clear_context_registry(first_ctx)
    assert registry_for_context(first_ctx) is not first


def test_registry_rejects_cross_turn_stale_duplicate_and_overlapping_grants() -> None:
    source = "<h1>Before</h1>"
    registry = ArtifactRangeGrantRegistry()
    binding = _binding()
    token = registry.mint_range(
        binding=binding,
        source=source,
        start=4,
        end=10,
        kind="text_content",
        annotation_orders=(0,),
    )
    with pytest.raises(ArtifactRangeGrantError, match="ARTIFACT_RANGE_DUPLICATE"):
        registry.reserve_ranges(
            binding=binding,
            source=source,
            tokens=[token, token],
            reservation_id="duplicate",
        )
    with pytest.raises(ArtifactRangeGrantError, match="ARTIFACT_RANGE_TOKEN_INVALID"):
        registry.reserve_ranges(
            binding=_binding(task_id="turn-2"),
            source=source,
            tokens=[token],
            reservation_id="wrong-turn",
        )
    with pytest.raises(ArtifactRangeGrantError, match="ARTIFACT_RANGE_STALE"):
        registry.reserve_ranges(
            binding=binding,
            source="<h1>Changed</h1>",
            tokens=[token],
            reservation_id="stale",
        )

    overlapping = registry.mint_range(
        binding=binding,
        source=source,
        start=3,
        end=11,
        kind="element_fragment",
        annotation_orders=(0,),
    )
    with pytest.raises(ArtifactRangeGrantError, match="ARTIFACT_RANGE_OVERLAP"):
        registry.reserve_ranges(
            binding=binding,
            source=source,
            tokens=[token, overlapping],
            reservation_id="overlap",
        )
    resolved = registry.reserve_ranges(
        binding=binding,
        source=source,
        tokens=[token],
        reservation_id="valid",
    )
    assert resolved[0].annotation_orders == (0,)
    registry.consume_reservation("valid")
    with pytest.raises(ArtifactRangeGrantError, match="ARTIFACT_RANGE_TOKEN_INVALID"):
        registry.reserve_ranges(
            binding=binding,
            source=source,
            tokens=[token],
            reservation_id="reuse",
        )


def test_registry_ttl_cursor_single_use_and_shared_capacity() -> None:
    now = [100.0]
    registry = ArtifactRangeGrantRegistry(
        capacity=2,
        ttl_seconds=10,
        monotonic=lambda: now[0],
    )
    binding = _binding()
    source = "abcdef"
    token = registry.mint_range(
        binding=binding,
        source=source,
        start=0,
        end=1,
        kind="literal_match",
    )
    cursor = registry.mint_cursor(binding=binding, position=3)
    with pytest.raises(ArtifactRangeGrantError, match="ARTIFACT_RANGE_LIMIT"):
        registry.mint_range(
            binding=binding,
            source=source,
            start=1,
            end=2,
            kind="literal_match",
        )
    assert registry.consume_cursor(binding=binding, token=cursor) == 3
    with pytest.raises(ArtifactRangeGrantError, match="ARTIFACT_CURSOR_INVALID"):
        registry.consume_cursor(binding=binding, token=cursor)

    now[0] = 111.0
    with pytest.raises(ArtifactRangeGrantError, match="ARTIFACT_RANGE_TOKEN_INVALID"):
        registry.reserve_ranges(
            binding=binding,
            source=source,
            tokens=[token],
            reservation_id="expired",
        )


def test_registry_enforces_one_shared_four_query_budget() -> None:
    registry = ArtifactRangeGrantRegistry()
    for _index in range(4):
        registry.consume_query_budget()

    with pytest.raises(ArtifactRangeGrantError, match="ARTIFACT_RANGE_QUERY_LIMIT"):
        registry.consume_query_budget()

    registry.clear()
    registry.consume_query_budget()


def test_registry_reuses_budget_for_an_identical_query_without_broadening_authority() -> None:
    registry = ArtifactRangeGrantRegistry()

    assert registry.consume_query_budget(query_key="annotation-0:set_style") == 3
    assert registry.consume_query_budget(query_key="annotation-0:set_style") == 3
    assert registry.consume_query_budget(query_key="annotation-1:replace_text") == 2
    assert registry.consume_query_budget(query_key="annotation-2:set_attribute:src") == 1
    assert registry.consume_query_budget(query_key="annotation-2:set_style") == 0

    with pytest.raises(ArtifactRangeGrantError, match="ARTIFACT_RANGE_QUERY_LIMIT"):
        registry.consume_query_budget(query_key="annotation-3:remove_node")


@pytest.mark.asyncio
async def test_agent_turn_finally_clears_range_registry_after_success() -> None:
    ctx = ToolContext(is_owner=True, session_key="agent:main:webchat:range-cleanup")
    registry_for_context(ctx)
    provider = _RangeLifecycleProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1),
        tool_context=ctx,
    )

    _events = [event async for event in agent.run_turn("finish")]

    assert getattr(ctx, "_artifact_range_grant_registry", None) is None


@pytest.mark.asyncio
async def test_agent_turn_finally_clears_range_registry_after_cancellation() -> None:
    ctx = ToolContext(is_owner=True, session_key="agent:main:webchat:range-cancel")
    registry_for_context(ctx)
    provider = _RangeLifecycleProvider(block=True)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1),
        tool_context=ctx,
    )

    async def run() -> list[Any]:
        return [event async for event in agent.run_turn("cancel")]

    task = asyncio.create_task(run())
    await provider.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert getattr(ctx, "_artifact_range_grant_registry", None) is None
