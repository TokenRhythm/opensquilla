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
    DocumentGrantBinding,
    DocumentMutationGrantRegistry,
    clear_context_registry,
    document_grant_registry_for_context,
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


def _binding(
    *,
    task_id: str = "turn-1",
    sha256: str = "a" * 64,
    adapter_id: str = "html",
    adapter_version: int = 1,
) -> ArtifactRangeBinding:
    return ArtifactRangeBinding(
        task_id=task_id,
        session_key="agent:main:webchat:test",
        session_id="session-test",
        session_epoch=4,
        document_id="document-test",
        revision_id="revision-test",
        source_sha256=sha256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
    )


def _document_binding(
    *,
    task_id: str = "turn-1",
    sha256: str = "a" * 64,
    adapter_id: str = "html",
    adapter_version: int = 1,
) -> DocumentGrantBinding:
    return DocumentGrantBinding(
        task_id=task_id,
        session_key="agent:main:webchat:test",
        session_id="session-test",
        session_epoch=4,
        document_id="document-test",
        revision_id="revision-test",
        source_sha256=sha256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
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
    with pytest.raises(ArtifactRangeGrantError, match="ARTIFACT_RANGE_TOKEN_INVALID"):
        registry.reserve_ranges(
            binding=_binding(adapter_id="docx", adapter_version=1),
            source=source,
            tokens=[token],
            reservation_id="wrong-adapter",
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


def test_document_grant_binds_adapter_operation_and_target_fingerprint() -> None:
    registry = DocumentMutationGrantRegistry()
    fingerprint = "f" * 64
    locator = {"adapter-private": ("part", 7)}
    token = registry.mint_grant(
        binding=_document_binding(adapter_id="html", adapter_version=1),
        annotation_orders=(0,),
        operation="replace_text",
        target_fingerprint=fingerprint,
        adapter_locator=locator,
    )

    resolved = registry.reserve_grants(
        binding=_document_binding(adapter_id="html", adapter_version=1),
        tokens=[token],
        reservation_id="document-mutation",
    )

    assert len(resolved) == 1
    assert resolved[0].adapter_id == "html"
    assert resolved[0].adapter_version == 1
    assert resolved[0].operation == "replace_text"
    assert resolved[0].target_fingerprint == fingerprint
    assert resolved[0].annotation_orders == (0,)
    assert resolved[0].adapter_locator is locator
    assert not hasattr(resolved[0], "start")
    assert not hasattr(resolved[0], "end")
    assert not hasattr(resolved[0], "kind")


def test_document_grant_rejects_cross_scope_adapter_revision_and_reuse() -> None:
    registry = DocumentMutationGrantRegistry()
    binding = _document_binding()
    token = registry.mint_grant(
        binding=binding,
        operation="replace_text",
        target_fingerprint="e" * 64,
        annotation_orders=(1,),
        adapter_locator=("opaque", 1),
    )

    for wrong_binding in (
        _document_binding(task_id="turn-2"),
        _document_binding(sha256="b" * 64),
        _document_binding(adapter_id="docx"),
        _document_binding(adapter_version=2),
    ):
        with pytest.raises(ArtifactRangeGrantError, match="ARTIFACT_RANGE_TOKEN_INVALID"):
            registry.reserve_grants(
                binding=wrong_binding,
                tokens=[token],
                reservation_id="wrong-scope",
            )

    resolved = registry.reserve_grants(
        binding=binding,
        tokens=[token],
        reservation_id="valid",
    )
    assert resolved[0].annotation_orders == (1,)
    registry.consume_reservation("valid")
    with pytest.raises(ArtifactRangeGrantError, match="ARTIFACT_RANGE_TOKEN_INVALID"):
        registry.reserve_grants(
            binding=binding,
            tokens=[token],
            reservation_id="reused",
        )


def test_document_grant_releases_failed_proposal_for_relocation_or_retry() -> None:
    registry = DocumentMutationGrantRegistry()
    binding = _document_binding()
    token = registry.mint_grant(
        binding=binding,
        operation="set_style",
        target_fingerprint="d" * 64,
        annotation_orders=(0,),
        adapter_locator=object(),
    )
    registry.reserve_grants(
        binding=binding,
        tokens=[token],
        reservation_id="proposal-1",
    )
    registry.release_reservation("proposal-1")

    retried = registry.reserve_grants(
        binding=binding,
        tokens=[token],
        reservation_id="proposal-2",
    )

    assert retried[0].operation == "set_style"


def test_document_source_reads_are_utf8_bounded_and_total_capacity_is_exact() -> None:
    registry = DocumentMutationGrantRegistry()
    binding = _document_binding()
    multibyte_fragment = "界" * 5_461

    registry.record_source_read(
        binding=binding,
        start=0,
        end=len(multibyte_fragment),
        text=multibyte_fragment,
    )
    assert registry.candidate_range_was_read(
        binding=binding,
        start=2,
        end=5,
    )

    registry.clear()
    for index in range(8):
        fragment = f"{index:02d}" + ("x" * (15 * 1024 - 2))
        start = index * len(fragment)
        registry.record_source_read(
            binding=binding,
            start=start,
            end=start + len(fragment),
            text=fragment,
        )

    assert registry.candidate_range_was_read(
        binding=binding,
        start=(15 * 1024) - 3,
        end=(15 * 1024) + 3,
    )

    with pytest.raises(ArtifactRangeGrantError, match="ARTIFACT_RANGE_LIMIT"):
        registry.record_source_read(
            binding=binding,
            start=8 * 15 * 1024,
            end=9 * 15 * 1024,
            text="overflow" + ("y" * (15 * 1024 - len("overflow"))),
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


def test_document_registry_bounds_malformed_tool_recovery_replays() -> None:
    registry = DocumentMutationGrantRegistry()

    assert registry.reserve_tool_attempt(attempt_key="document_read:invalid_cursor") == 1
    assert registry.reserve_tool_attempt(attempt_key="document_read:invalid_cursor") == 0
    with pytest.raises(ArtifactRangeGrantError, match="ARTIFACT_RANGE_QUERY_LIMIT") as exc_info:
        registry.reserve_tool_attempt(attempt_key="document_read:invalid_cursor")
    assert "malformed document-tool recovery" in exc_info.value.user_message

    registry.clear()
    assert registry.reserve_tool_attempt(attempt_key="document_read:invalid_cursor") == 1


@pytest.mark.asyncio
async def test_agent_turn_finally_clears_range_registry_after_success() -> None:
    ctx = ToolContext(is_owner=True, session_key="agent:main:webchat:range-cleanup")
    registry_for_context(ctx)
    document_grant_registry_for_context(ctx)
    provider = _RangeLifecycleProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1),
        tool_context=ctx,
    )

    _events = [event async for event in agent.run_turn("finish")]

    assert getattr(ctx, "_artifact_range_grant_registry", None) is None
    assert getattr(ctx, "_document_mutation_grant_registry", None) is None


@pytest.mark.asyncio
async def test_agent_turn_finally_clears_range_registry_after_cancellation() -> None:
    ctx = ToolContext(is_owner=True, session_key="agent:main:webchat:range-cancel")
    registry_for_context(ctx)
    document_grant_registry_for_context(ctx)
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
    assert getattr(ctx, "_document_mutation_grant_registry", None) is None
