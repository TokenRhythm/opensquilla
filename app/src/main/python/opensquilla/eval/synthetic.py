"""Offline synthetic provider for the ensemble benchmark's dry-run mode.

``SyntheticProvider`` is an ``LLMProvider``-shaped stand-in that replays one
fixed successful turn with no network or credentials. It powers
``opensquilla ensemble bench --dry-run`` and is layered under a
:class:`~opensquilla.provider.types.FailureInjector` so scripted failures can
be mixed in exactly the way the runtime consults the injector. Nothing on any
live path constructs one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from opensquilla.provider.types import (
    ChatConfig,
    DoneEvent,
    Message,
    ModelInfo,
    StreamEvent,
    TextDeltaEvent,
    ToolDefinition,
)


class SyntheticProvider:
    """Deterministic offline provider that always streams one success turn.

    Every ``chat`` call yields the same shape: an optional text delta followed
    by a terminal :class:`DoneEvent` carrying the configured token counts,
    billed cost, model id, and (for the ensemble arm) a synthetic
    ``ensemble_trace``. ``provider_name`` defaults to a registered provider id
    (``"openai"``) so an injected synthetic ``ErrorEvent`` round-trips through
    the shared failure taxonomy.
    """

    provider_name: str

    def __init__(
        self,
        *,
        model: str = "synthetic-model",
        provider_name: str = "openai",
        input_tokens: int = 1200,
        output_tokens: int = 400,
        billed_cost: float = 0.0,
        cost_source: str = "synthetic",
        text: str = "synthetic answer",
        ensemble_trace: dict[str, Any] | None = None,
    ) -> None:
        self.provider_name = provider_name
        self._model = model
        self._input_tokens = int(input_tokens)
        self._output_tokens = int(output_tokens)
        self._billed_cost = float(billed_cost)
        self._cost_source = cost_source
        self._text = text
        self._ensemble_trace = dict(ensemble_trace) if ensemble_trace is not None else None

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[StreamEvent]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[StreamEvent]:
        if self._text:
            yield TextDeltaEvent(text=self._text)
        yield DoneEvent(
            stop_reason="end_turn",
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            billed_cost=self._billed_cost,
            model=self._model,
            cost_source=self._cost_source,
            ensemble_trace=dict(self._ensemble_trace) if self._ensemble_trace else None,
        )

    async def list_models(self) -> list[ModelInfo]:
        return []
