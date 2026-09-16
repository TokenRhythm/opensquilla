"""Offline provider for tests of successful durable compaction."""

from opensquilla.provider.types import DoneEvent, TextDeltaEvent
from opensquilla.session.compaction import CompactionConfig
from opensquilla.session.compaction_deployment import (
    CompactionExecutionPlan,
    CompactionExecutionTarget,
)


class SyntheticCompactionProvider:
    def __init__(self, summary: str = "Earlier work completed; continue the current task.") -> None:
        self.summary = summary
        self.calls: list = []

    async def chat(self, messages, tools=None, config=None):
        self.calls.append((messages, tools, config))
        yield TextDeltaEvent(text=self.summary)
        yield DoneEvent(stop_reason="end_turn")


def synthetic_compaction_config(
    *, summary: str = "Earlier work completed; continue the current task.", **kwargs,
) -> CompactionConfig:
    kwargs.pop("model", None)
    kwargs.pop("api_key", None)
    return CompactionConfig(
        llm_plan=CompactionExecutionPlan(candidates=(CompactionExecutionTarget(
            provider=SyntheticCompactionProvider(summary),
            provider_id="synthetic", model="synthetic-summary",
            context_window_tokens=100_000,
        ),)),
        **kwargs,
    )
