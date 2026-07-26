"""Tests for TurnRunner harness adapters."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.turn_runner.harness import (
    _coerce_flush_triggers,
    _TurnRunnerAgentFactoryAdapter,
)
from opensquilla.provider import ProviderRequestCorrelation


def test_harness_flush_triggers_normalize_comma_delimited_aliases() -> None:
    assert _coerce_flush_triggers("reset, inline_overflow") == [
        "session_reset",
        "pre_compaction",
    ]


def test_harness_flush_triggers_reject_unknown_aliases() -> None:
    with pytest.raises(ValueError, match="unknown flush trigger"):
        _coerce_flush_triggers(["manual", "bogus"])


def test_agent_factory_adapter_passes_runner_tool_registry(monkeypatch) -> None:
    """Meta-skill execution needs the per-runner registry on constructed Agents."""

    captured: dict[str, Any] = {}

    class RecordingAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    import opensquilla.engine.agent as agent_module

    monkeypatch.setattr(agent_module, "Agent", RecordingAgent)

    registry = object()
    runner = SimpleNamespace(
        _tool_registry=registry,
        _usage_tracker=None,
        _session_flush_service=None,
    )
    adapter = _TurnRunnerAgentFactoryAdapter(runner)
    correlation = ProviderRequestCorrelation(
        session_id="session-1",
        turn_id="turn-1",
        execution_id="execution-1",
        call_kind="agent.chat",
    )

    adapter.build(
        provider=object(),
        config=object(),
        tool_definitions=[],
        tool_handler=None,
        session_key="agent:main:test",
        turn_call_logger=None,
        memory_sync_manager=None,
        tool_context=None,
        turn_id="turn-1",
        session_id="session-1",
        provider_request_correlation=correlation,
    )

    assert captured["tool_registry"] is registry
    assert captured["provider_request_correlation"] is correlation


def _catalog_runner(
    *, llm: SimpleNamespace | None, model_catalog: Any = None
) -> SimpleNamespace:
    return SimpleNamespace(
        _config=SimpleNamespace(llm=llm) if llm is not None else None,
        _model_catalog=model_catalog,
    )


def test_model_catalog_adapter_defaults_to_200k_without_override() -> None:
    from opensquilla.engine.turn_runner.harness import _TurnRunnerModelCatalogAdapter

    llm = SimpleNamespace(max_tokens=32768, temperature=None, top_p=None)
    adapter = _TurnRunnerModelCatalogAdapter(_catalog_runner(llm=llm))

    resolved = adapter.lookup("qwen3.6-flash")

    assert resolved.context_window == 200_000
    assert resolved.max_tokens == 32768


def test_model_catalog_adapter_honors_context_window_tokens_override() -> None:
    from opensquilla.engine.turn_runner.harness import _TurnRunnerModelCatalogAdapter

    llm = SimpleNamespace(
        max_tokens=32768,
        context_window_tokens=1_000_000,
        temperature=None,
        top_p=None,
    )
    adapter = _TurnRunnerModelCatalogAdapter(_catalog_runner(llm=llm))

    resolved = adapter.lookup("qwen3.6-flash")

    assert resolved.context_window == 1_000_000
    assert resolved.max_tokens == 32768


def test_model_catalog_adapter_override_beats_catalog_resolution() -> None:
    from opensquilla.engine.turn_runner.harness import _TurnRunnerModelCatalogAdapter
    from opensquilla.provider.model_catalog import ModelCatalog

    llm = SimpleNamespace(
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        max_tokens=32768,
        context_window_tokens=202_752,
        temperature=None,
        top_p=None,
    )
    adapter = _TurnRunnerModelCatalogAdapter(
        _catalog_runner(llm=llm, model_catalog=ModelCatalog())
    )

    # glm-5.1 resolves to 200_000 via the static fallback; the explicit config
    # override must win so the compaction ladder budgets against the real window.
    resolved = adapter.lookup("glm-5.1")

    assert resolved.context_window == 202_752


def test_model_catalog_adapter_per_model_override_beats_global_config() -> None:
    from opensquilla.engine.turn_runner.harness import _TurnRunnerModelCatalogAdapter
    from opensquilla.provider.model_catalog import ModelCatalog

    catalog = ModelCatalog()
    catalog.set_user_overrides({"openrouter/glm-5.1": {"context_window": 131_072}})
    llm = SimpleNamespace(
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        max_tokens=32768,
        context_window_tokens=1_000_000,
        temperature=None,
        top_p=None,
    )
    adapter = _TurnRunnerModelCatalogAdapter(_catalog_runner(llm=llm, model_catalog=catalog))

    # The [models.*] per-model window beats the global llm.context_window_tokens
    # override; the global still applies to models without a per-model row.
    assert adapter.lookup("glm-5.1").context_window == 131_072
    assert adapter.lookup("some-other-model").context_window == 1_000_000


def test_model_catalog_adapter_ignores_junk_context_window_values() -> None:
    from opensquilla.engine.turn_runner.harness import _TurnRunnerModelCatalogAdapter

    for junk in ("not-a-number", -5, 0, None):
        llm = SimpleNamespace(
            max_tokens=0,
            context_window_tokens=junk,
            temperature=None,
            top_p=None,
        )
        adapter = _TurnRunnerModelCatalogAdapter(_catalog_runner(llm=llm))
        assert adapter.lookup("some-model").context_window == 200_000
