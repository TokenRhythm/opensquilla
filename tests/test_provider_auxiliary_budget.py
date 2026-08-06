from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.provider import auxiliary_budget
from opensquilla.provider.auxiliary_budget import (
    AuxiliaryRequestTooLargeError,
    ensure_auxiliary_text_fits,
    resolve_auxiliary_request_budget,
)
from opensquilla.provider.protocol import ProviderMetadata
from opensquilla.provider.types import ChatConfig, DoneEvent, Message, TextDeltaEvent


class _Catalog:
    def __init__(self, *, context_window: int = 8192) -> None:
        self.context_window = context_window

    def resolve_context_window_with_source(
        self,
        model_id: str,
        *,
        provider: str = "",
    ) -> tuple[int, str]:
        del model_id, provider
        return self.context_window, "catalog"

    def resolve_context_window(self, model_id: str, provider: str = "") -> int:
        del model_id, provider
        return self.context_window

    def resolve_max_tokens(
        self,
        model_id: str,
        user_override: int = 0,
        provider: str = "",
    ) -> int:
        del model_id, provider
        return max(1, user_override or 256)


class _ChatProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.config: ChatConfig | None = None

    def provider_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_id="test",
            provider_name="test",
            model="test-model",
        )

    def chat(
        self,
        messages: list[Message],
        *,
        tools: Any = None,
        config: ChatConfig,
    ) -> AsyncIterator[Any]:
        del messages, tools
        self.calls += 1
        self.config = config
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield TextDeltaEvent(text="ok")
        yield DoneEvent(input_tokens=1, output_tokens=1)


@pytest.fixture
def small_catalog(monkeypatch: pytest.MonkeyPatch) -> _Catalog:
    catalog = _Catalog()
    monkeypatch.setattr(auxiliary_budget, "shared_catalog", lambda: catalog)
    return catalog


def test_auxiliary_budget_binds_model_window_and_never_widens_explicit_cap(
    small_catalog: _Catalog,
) -> None:
    provider = _ChatProvider()

    budget = resolve_auxiliary_request_budget(
        provider,
        max_output_tokens=128,
        provider_request_max_chars=1200,
    )

    assert budget.provider_id == "test"
    assert budget.model == "test-model"
    assert budget.context_window_tokens == small_catalog.context_window
    assert budget.max_output_tokens == 128
    assert budget.max_input_tokens > 0
    assert budget.provider_request_max_chars == 1200


def test_auxiliary_budget_prefers_physical_metadata_over_stale_hints(
    small_catalog: _Catalog,
) -> None:
    provider = _ChatProvider()

    budget = resolve_auxiliary_request_budget(
        provider,
        provider_id="stale-provider",
        model="stale-model",
        context_window_tokens=1_000_000,
        max_output_tokens=128,
        provider_request_max_chars=1200,
    )

    assert budget.provider_id == "test"
    assert budget.model == "test-model"
    assert budget.context_window_tokens == small_catalog.context_window
    assert budget.provider_request_max_chars == 1200


def test_auxiliary_text_preflight_rejects_before_physical_call() -> None:
    with pytest.raises(AuxiliaryRequestTooLargeError, match="resolved deployment budget"):
        ensure_auxiliary_text_fits(
            [Message(role="user", content="x" * 101)],
            max_chars=100,
        )


def test_auxiliary_text_preflight_rejects_token_dense_text() -> None:
    with pytest.raises(AuxiliaryRequestTooLargeError, match="tokens"):
        ensure_auxiliary_text_fits(
            [Message(role="user", content="中文🙂" * 100)],
            max_chars=10_000,
            max_tokens=10,
        )
