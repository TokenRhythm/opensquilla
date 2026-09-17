from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.turn_runner.attachment_stage import (
    AttachmentMaterializationStats,
)
from opensquilla.engine.turn_runner.harness import _TurnRunnerPipelineExecutionAdapter
from opensquilla.engine.turn_runner.input_stage import (
    ExtraContextResolver,
    InputStage,
    InputStageInput,
)
from opensquilla.engine.turn_runner.prompt_assembler_stage import (
    MemoryFingerprintPort,
    PipelineExecutionPort,
    PromptAssemblerPort,
    PromptAssemblerStage,
    PromptAssemblerStageInput,
    PromptConfigResolverPort,
    PromptReportBuilderPort,
    RouterContextPort,
    RunPipelineRequest,
    SessionIdResolverPort,
)
from opensquilla.observability.prompt_report import PromptReport


class _NoExtraContext(ExtraContextResolver):
    def extra_context_for(self, ctx):  # noqa: ANN001
        return {}

    def merge(self, base, extra):  # noqa: ANN001
        return base if not extra else dict(extra)


@pytest.mark.asyncio
async def test_input_stage_exposes_input_normalization_metadata() -> None:
    provenance = {
        "kind": "web_message",
        "input_normalization": {
            "guard_action": "generated_text_attachment",
            "material_estimated_tokens": 45_000,
        },
    }
    stage = InputStage(extra_ctx=_NoExtraContext())

    out = await stage.run(
        InputStageInput(
            message="short semantic message",
            semantic_message=None,
            input_mode="default",
            persist_input=False,
            input_provenance=provenance,
            session_key="agent:main:s1",
            tool_context=None,
            session_append=None,
        )
    )

    assert out.normalization_metadata == provenance["input_normalization"]
    assert out.normalization_metadata is not provenance["input_normalization"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provenance",
    [
        None,
        {"kind": "web_message"},
        {"kind": "web_message", "input_normalization": "not-a-dict"},
    ],
)
async def test_input_stage_ignores_missing_or_non_dict_normalization_metadata(
    provenance: dict[str, Any] | None,
) -> None:
    stage = InputStage(extra_ctx=_NoExtraContext())

    out = await stage.run(
        InputStageInput(
            message="short semantic message",
            semantic_message=None,
            input_mode="default",
            persist_input=False,
            input_provenance=provenance,
            session_key="agent:main:s1",
            tool_context=None,
            session_append=None,
        )
    )

    assert out.normalization_metadata is None


@pytest.mark.asyncio
async def test_prompt_assembler_passes_normalization_metadata_to_pipeline() -> None:
    normalization_metadata = {
        "guard_action": "generated_text_attachment",
        "material_estimated_tokens": 45_000,
    }
    executor = _RecordingPipelineExecutor(turn=_make_turn())
    stage = _make_stage(executor=executor)

    await stage.run(
        _make_prompt_input(normalization_metadata=normalization_metadata)
    )

    assert executor.requests[0].normalization_metadata == normalization_metadata
    assert executor.requests[0].normalization_metadata is normalization_metadata


@pytest.mark.asyncio
async def test_pipeline_execution_adapter_forwards_normalization_metadata() -> None:
    runner = _RecordingRunner()
    adapter = _TurnRunnerPipelineExecutionAdapter(cast(TurnRunner, runner))
    normalization_metadata = {
        "guard_action": "generated_text_attachment",
        "material_estimated_tokens": 45_000,
    }

    await adapter.run_pipeline(
        RunPipelineRequest(
            runtime_message="runtime",
            session_key="agent:main:s1",
            provider="provider",
            cloned_selector=None,
            tool_defs=[],
            base_prompt="base",
            attachments=[],
            normalization_metadata=normalization_metadata,
        )
    )

    assert runner.calls[0]["normalization_metadata"] == normalization_metadata


@pytest.mark.asyncio
async def test_attachment_materialization_threads_into_sanitized_pipeline_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_run_pipeline(ctx, steps):  # noqa: ANN001, ARG001
        captured["metadata"] = ctx.metadata
        return ctx

    monkeypatch.setattr("opensquilla.engine.pipeline.run_pipeline", fake_run_pipeline)
    runner = TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(
            squilla_router=SimpleNamespace(routing_timeout_seconds=5.0)
        ),
    )
    stats = AttachmentMaterializationStats(
        attachment_count=3,
        estimated_tokens=45_123,
        generated_normalization_estimated_tokens=2_000,
        parse_failure_count=1,
        provider_visible_text_chars=180_000,
        image_count=1,
    )

    await runner._run_pipeline(
        message="runtime",
        session_key="agent:main:s1",
        provider=None,
        cloned_selector=None,
        tool_defs=[],
        base_prompt="base",
        attachments=[],
        attachment_materialization=stats,
    )

    assert captured["metadata"] | {
        "had_attachments": True,
        "attachment_count": 3,
        "attachment_material_estimated_tokens": 45_123,
        "attachment_generated_normalization_estimated_tokens": 2_000,
        "attachment_parse_failure_count": 1,
        "attachment_provider_visible_text_chars": 180_000,
        "attachment_image_count": 1,
    } == captured["metadata"]
    assert not any("name" in key or "content" in key for key in captured["metadata"])


@pytest.mark.asyncio
async def test_run_pipeline_seeds_turn_context_with_normalization_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_run_pipeline(ctx, steps):  # noqa: ANN001, ARG001
        captured["metadata"] = ctx.metadata
        return ctx

    monkeypatch.setattr("opensquilla.engine.pipeline.run_pipeline", fake_run_pipeline)
    runner = TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(
            squilla_router=SimpleNamespace(routing_timeout_seconds=5.0)
        ),
    )
    normalization_metadata = {
        "guard_action": "generated_text_attachment",
        "material_estimated_tokens": 45_000,
    }

    await runner._run_pipeline(
        message="runtime",
        session_key="agent:main:s1",
        provider=None,
        cloned_selector=None,
        tool_defs=[],
        base_prompt="base",
        attachments=[],
        normalization_metadata=normalization_metadata,
    )

    assert captured["metadata"]["input_normalization"] == normalization_metadata
    assert captured["metadata"]["input_normalization"] is not normalization_metadata
    assert captured["metadata"]["material_estimated_tokens"] == 45_000


@pytest.mark.asyncio
async def test_run_pipeline_ignores_non_positive_material_token_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_run_pipeline(ctx, steps):  # noqa: ANN001, ARG001
        captured["metadata"] = ctx.metadata
        return ctx

    monkeypatch.setattr("opensquilla.engine.pipeline.run_pipeline", fake_run_pipeline)
    runner = TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(
            squilla_router=SimpleNamespace(routing_timeout_seconds=5.0)
        ),
    )

    await runner._run_pipeline(
        message="runtime",
        session_key="agent:main:s1",
        provider=None,
        cloned_selector=None,
        tool_defs=[],
        base_prompt="base",
        attachments=[],
        normalization_metadata={"material_estimated_tokens": True},
    )

    assert captured["metadata"]["input_normalization"] == {
        "material_estimated_tokens": True
    }
    assert "material_estimated_tokens" not in captured["metadata"]


@dataclass
class _RecordingPipelineExecutor(PipelineExecutionPort):
    turn: Any
    provider: Any = None
    requests: list[RunPipelineRequest] = field(default_factory=list)

    async def run_pipeline(self, request: RunPipelineRequest):
        self.requests.append(request)
        return self.turn, self.provider


class _RecordingPromptAssembler(PromptAssemblerPort):
    def assemble_prompt(
        self,
        agent_id,  # noqa: ANN001, ARG002
        tool_defs,  # noqa: ANN001, ARG002
        *,
        session_key,  # noqa: ANN001, ARG002
        semantic_message,  # noqa: ANN001, ARG002
        extra_context,  # noqa: ANN001, ARG002
        prompt_metadata,
        bootstrap_context_mode,  # noqa: ANN001, ARG002
        fresh_user_session=False,  # noqa: ANN001, ARG002
        workspace_dir=None,  # noqa: ANN001, ARG002
    ):
        prompt_metadata["prompt_key"] = "prompt_value"
        return "base"


class _RecordingRouterContext(RouterContextPort):
    async def fetch_router_context(
        self,
        session_key,
        *,
        exclude_last_user,
        bound_user_message_id=None,
        include_capacity=False,  # noqa: ANN001, ARG002
    ):
        return {}


class _RecordingPromptConfigResolver(PromptConfigResolverPort):
    def resolve_prompt_config(self, turn):  # noqa: ANN001
        return "final", None, None


class _RecordingPromptReportBuilder(PromptReportBuilderPort):
    def build_prompt_report(self, **kwargs):  # noqa: ANN003
        return PromptReport(
            turn_id=kwargs["turn_id"],
            session_key=kwargs["session_key"],
            session_id=kwargs["session_id"],
            agent_id=kwargs["agent_id"],
            system_chars=len(kwargs["system_prompt"]),
            tool_count=len(kwargs["tool_defs"]),
            tool_profile=kwargs["tool_profile"],
        )


class _RecordingSessionIdResolver(SessionIdResolverPort):
    async def resolve_session_id_for_log(self, session_key):  # noqa: ANN001, ARG002
        return "session-id"


class _NoMemoryFingerprint(MemoryFingerprintPort):
    def memory_mode_fingerprint(self):
        return None


class _RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def _run_pipeline(
        self,
        message,
        session_key,
        provider,
        cloned_selector,
        tool_defs,
        base_prompt,
        attachments,
        **kwargs,
    ):
        self.calls.append(
            {
                "message": message,
                "session_key": session_key,
                "provider": provider,
                "cloned_selector": cloned_selector,
                "tool_defs": tool_defs,
                "base_prompt": base_prompt,
                "attachments": attachments,
                **kwargs,
            }
        )
        return "turn", provider


def _make_turn():
    return SimpleNamespace(message="runtime", metadata={}, tool_defs=[], model="")


def _make_stage(*, executor: PipelineExecutionPort) -> PromptAssemblerStage:
    return PromptAssemblerStage(
        prompt_assembler=_RecordingPromptAssembler(),
        pipeline_executor=executor,
        router_context=_RecordingRouterContext(),
        prompt_config_resolver=_RecordingPromptConfigResolver(),
        prompt_report_builder=_RecordingPromptReportBuilder(),
        session_id_resolver=_RecordingSessionIdResolver(),
        memory_fingerprint=_NoMemoryFingerprint(),
    )


def _make_prompt_input(
    *,
    normalization_metadata: dict[str, Any] | None,
) -> PromptAssemblerStageInput:
    return PromptAssemblerStageInput(
        runtime_message="runtime",
        semantic_input="semantic",
        extra_prompt_context=None,
        provider=None,
        cloned_selector=None,
        tool_defs=[],
        effective_tool_context=None,
        tool_metadata={},
        session_key="agent:main:s1",
        agent_id="agent:main",
        turn_id="turn-1",
        attachments=[],
        bootstrap_context_mode=None,
        model=None,
        history_has_persisted_user=False,
        persist_input=False,
        ingress_pipeline_steps=None,
        normalization_metadata=normalization_metadata,
    )
