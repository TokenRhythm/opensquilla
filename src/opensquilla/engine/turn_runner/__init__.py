"""TurnRunner stage decomposition — public exports.

Re-exports the eight TurnRunner stages, their I/O dataclasses, the
Protocol ports they accept, and the shared ``StageOutcome`` /
``TurnContext`` types. Internal adapters and private dataclasses live
in their owning submodules (``harness``, ``stream_consumer_stage``,
``agent_bootstrap_stage``) and are imported from there by code that
needs them.
"""

from __future__ import annotations

from opensquilla.engine.turn_runner.agent_bootstrap_stage import (
    AgentBootstrapStage,
    AgentBootstrapStageInput,
    AgentBootstrapStageOutput,
    AgentConfigBuilderPort,
    AgentFactoryPort,
    MemorySnapshotPort,
    ModelCatalogPort,
    TimeoutBudgetPort,
)
from opensquilla.engine.turn_runner.attachment_stage import (
    AttachmentMessageBuilderPort,
    AttachmentStage,
    AttachmentStageInput,
    AttachmentStageOutput,
)
from opensquilla.engine.turn_runner.compaction_and_history_stage import (
    CompactionAndHistoryStage,
    CompactionAndHistoryStageInput,
    CompactionAndHistoryStageOutput,
    HistoryLoaderPort,
    PreflightCompactionPort,
    RequestContextPrependPort,
    T3UpgradeCompactionPort,
)
from opensquilla.engine.turn_runner.context import TurnContext
from opensquilla.engine.turn_runner.input_stage import (
    ExtraContextResolver,
    InputStage,
    InputStageInput,
    InputStageOutput,
    SessionAppendPort,
)
from opensquilla.engine.turn_runner.outcome import StageOutcome
from opensquilla.engine.turn_runner.prompt_assembler_stage import (
    MemoryFingerprintPort,
    PipelineExecutionPort,
    PromptAssemblerPort,
    PromptAssemblerStage,
    PromptAssemblerStageInput,
    PromptAssemblerStageOutput,
    PromptConfigResolverPort,
    PromptReportBuilderPort,
    RouterContextPort,
    RunPipelineRequest,
    SessionIdResolverPort,
)
from opensquilla.engine.turn_runner.provider_and_tools_stage import (
    ProviderAndToolsStage,
    ProviderAndToolsStageInput,
    ProviderAndToolsStageOutput,
    ProviderResolverPort,
    SkillCatalogResolverPort,
    ToolBuilderPort,
)
from opensquilla.engine.turn_runner.stream_consumer_stage import (
    AgentRunPort,
    CompactionPersistPort,
    MemorySnapshotRefreshPort,
    MemorySyncNotifyPort,
    StreamConsumerStage,
    StreamConsumerStageInput,
    SystemPromptRefreshPort,
    WarningTransformer,
)
from opensquilla.engine.turn_runner.turn_finalizer_stage import (
    CostRollupResult,
    SessionTotalsPort,
    TranscriptAppendPort,
    TranscriptAppendResult,
    TurnErrorPersistPort,
    TurnFinalizerStage,
    TurnFinalizerStageInput,
    TurnFinalizerStageOutput,
    TurnMemoryCapturePort,
)

__all__ = [
    "AgentBootstrapStage",
    "AgentBootstrapStageInput",
    "AgentBootstrapStageOutput",
    "AgentConfigBuilderPort",
    "AgentFactoryPort",
    "AgentRunPort",
    "AttachmentMessageBuilderPort",
    "AttachmentStage",
    "AttachmentStageInput",
    "AttachmentStageOutput",
    "CompactionAndHistoryStage",
    "CompactionAndHistoryStageInput",
    "CompactionAndHistoryStageOutput",
    "CompactionPersistPort",
    "CostRollupResult",
    "ExtraContextResolver",
    "HistoryLoaderPort",
    "InputStage",
    "InputStageInput",
    "InputStageOutput",
    "MemoryFingerprintPort",
    "MemorySnapshotPort",
    "MemorySnapshotRefreshPort",
    "MemorySyncNotifyPort",
    "ModelCatalogPort",
    "PipelineExecutionPort",
    "PreflightCompactionPort",
    "PromptAssemblerPort",
    "PromptAssemblerStage",
    "PromptAssemblerStageInput",
    "PromptAssemblerStageOutput",
    "PromptConfigResolverPort",
    "PromptReportBuilderPort",
    "ProviderAndToolsStage",
    "ProviderAndToolsStageInput",
    "ProviderAndToolsStageOutput",
    "ProviderResolverPort",
    "SkillCatalogResolverPort",
    "RequestContextPrependPort",
    "RouterContextPort",
    "RunPipelineRequest",
    "SessionAppendPort",
    "SessionIdResolverPort",
    "SessionTotalsPort",
    "StageOutcome",
    "StreamConsumerStage",
    "StreamConsumerStageInput",
    "SystemPromptRefreshPort",
    "T3UpgradeCompactionPort",
    "TimeoutBudgetPort",
    "ToolBuilderPort",
    "TranscriptAppendPort",
    "TranscriptAppendResult",
    "TurnContext",
    "TurnErrorPersistPort",
    "TurnFinalizerStage",
    "TurnFinalizerStageInput",
    "TurnFinalizerStageOutput",
    "TurnMemoryCapturePort",
    "WarningTransformer",
]
