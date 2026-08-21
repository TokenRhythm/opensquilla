from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.types import ToolCall
from opensquilla.tools.builtin.document_format_adapters import DocumentMutationError
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolContext, ToolSpec


class _Controller:
    def __init__(self) -> None:
        self.active: str | None = None
        self.committed: set[str] = set()
        self.status = "reserved"
        self.proposal_rejection_count = 0

    async def observe_intent(self, tool_use_id: str) -> SimpleNamespace:
        self.active = tool_use_id
        return SimpleNamespace(created=True, attempt_number=1)

    async def reject_proposal(self, tool_use_id: str) -> None:
        self.proposal_rejection_count += 1
        if self.active == tool_use_id:
            self.active = None

    def owns_commit(self, tool_use_id: str) -> bool:
        return tool_use_id in self.committed

    async def reconcile(self, _tool_use_id: str) -> SimpleNamespace:
        return SimpleNamespace(status=SimpleNamespace(value=self.status))

    async def mark_failed(self, _tool_use_id: str, _code: str) -> SimpleNamespace:
        self.status = "failed"
        return await self.reconcile(_tool_use_id)

    async def mark_ambiguous(self, *_args: object) -> SimpleNamespace:
        self.status = "ambiguous"
        return await self.reconcile("")


def _registry(handler: Any) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="document_apply",
            description="test document writer",
            parameters={"mutations": {"type": "array"}},
            required=["mutations"],
            runtime_only_arguments=frozenset({"_tool_use_id"}),
            exposed_by_default=False,
        ),
        handler,
    )
    return registry


def _context(controller: _Controller) -> ToolContext:
    return ToolContext(
        is_owner=True,
        session_key="agent:main:webchat:dispatch-outcomes",
        task_id="turn-dispatch-outcomes",
        exclusive_tools={"document_apply"},
        allowed_tools={"document_apply"},
        surfaced_tools={"document_apply"},
        artifact_mutation_attempt_controller=controller,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation_retry", "expected_retry", "expected_loop"),
    [
        ("correctable", "same_turn", "continue"),
        ("refresh", "refresh", "finalize_without_tools"),
        ("forbidden", "never", "finalize_without_tools"),
    ],
)
async def test_typed_precommit_failure_controls_agent_loop_without_durable_attempt(
    mutation_retry: str,
    expected_retry: str,
    expected_loop: str,
) -> None:
    controller = _Controller()

    async def writer(mutations: list[object], _tool_use_id: str) -> str:
        del mutations, _tool_use_id
        raise DocumentMutationError(
            "DOCUMENT_TEST_FAILURE",
            "The proposal was rejected.",
            retry_policy=mutation_retry,  # type: ignore[arg-type]
        )

    handler = build_tool_handler(_registry(writer), _context(controller))
    call = ToolCall(
        tool_use_id=f"apply-{mutation_retry}",
        tool_name="document_apply",
        arguments={"mutations": []},
    )
    await controller.observe_intent(call.tool_use_id)

    result = await handler(call)

    assert result.is_error is True
    assert result.effect_outcome is not None
    assert result.effect_outcome.effect_state == "none"
    assert result.effect_outcome.retry_policy == expected_retry
    assert result.effect_outcome.loop_action == expected_loop
    assert result.effect_outcome.outcome_code == "DOCUMENT_TEST_FAILURE"
    assert controller.committed == set()


@pytest.mark.asyncio
async def test_only_identical_correctable_proposal_digest_is_no_progress() -> None:
    controller = _Controller()

    async def writer(mutations: list[object], _tool_use_id: str) -> str:
        del mutations, _tool_use_id
        raise DocumentMutationError(
            "DOCUMENT_MUTATIONS_INVALID",
            "Fix the mutation shape.",
            retry_policy="correctable",
        )

    handler = build_tool_handler(_registry(writer), _context(controller))
    results = []
    for index, mutations in enumerate(([], [{"input": "different"}], []), start=1):
        call = ToolCall(
            tool_use_id=f"apply-{index}",
            tool_name="document_apply",
            arguments={"mutations": mutations},
        )
        await controller.observe_intent(call.tool_use_id)
        results.append(await handler(call))

    assert [result.effect_outcome.loop_action for result in results] == [
        "continue",
        "continue",
        "finalize_without_tools",
    ]
    assert results[-1].effect_outcome.outcome_code == "document_proposal_no_progress"


@pytest.mark.asyncio
async def test_commit_conflict_is_authoritative_refresh_outcome() -> None:
    controller = _Controller()

    async def writer(mutations: list[object], _tool_use_id: str) -> str:
        del mutations
        controller.committed.add(_tool_use_id)
        raise DocumentMutationError(
            "DOCUMENT_MUTATION_CONFLICT",
            "The document head changed.",
            retry_policy="refresh",
        )

    handler = build_tool_handler(_registry(writer), _context(controller))
    call = ToolCall(
        tool_use_id="apply-conflict",
        tool_name="document_apply",
        arguments={"mutations": []},
    )
    await controller.observe_intent(call.tool_use_id)

    result = await handler(call)

    assert result.effect_outcome is not None
    assert result.effect_outcome.effect_state == "started"
    assert result.effect_outcome.retry_policy == "refresh"
    outcome = result.effect_outcome.safe_details["documentMutationOutcome"]
    assert outcome["status"] == "conflict"
    assert outcome["refreshRequired"] is True


@pytest.mark.asyncio
async def test_known_commit_failure_requires_a_new_turn() -> None:
    controller = _Controller()

    async def writer(mutations: list[object], _tool_use_id: str) -> str:
        del mutations
        controller.committed.add(_tool_use_id)
        raise RuntimeError("synthetic commit failure")

    handler = build_tool_handler(_registry(writer), _context(controller))
    call = ToolCall(
        tool_use_id="apply-failed",
        tool_name="document_apply",
        arguments={"mutations": []},
    )
    await controller.observe_intent(call.tool_use_id)

    result = await handler(call)

    assert result.effect_outcome is not None
    assert result.effect_outcome.effect_state == "started"
    assert result.effect_outcome.retry_policy == "new_turn"
    assert result.effect_outcome.loop_action == "finalize_without_tools"
    outcome = result.effect_outcome.safe_details["documentMutationOutcome"]
    assert outcome["status"] == "not_applied"
    assert outcome["phase"] == "commit"


@pytest.mark.asyncio
async def test_applied_outcome_records_prior_proposal_correction() -> None:
    controller = _Controller()
    controller.proposal_rejection_count = 1
    controller.status = "applied"

    async def writer(mutations: list[object], _tool_use_id: str) -> str:
        del mutations
        controller.committed.add(_tool_use_id)
        return '{"status":"applied"}'

    handler = build_tool_handler(_registry(writer), _context(controller))
    call = ToolCall(
        tool_use_id="apply-corrected",
        tool_name="document_apply",
        arguments={"mutations": []},
    )
    await controller.observe_intent(call.tool_use_id)

    result = await handler(call)

    outcome = result.effect_outcome.safe_details["documentMutationOutcome"]
    assert outcome["status"] == "applied"
    assert outcome["corrected"] is True
    assert outcome["proposalAttempts"] == 2
