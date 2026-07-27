"""Tests for context window compaction logic."""

import pytest

from opensquilla.provider.types import ProviderRequestCorrelation
from opensquilla.session.compaction import (
    CompactionConfig,
    CompactionRequest,
    build_compaction_config_from_provider,
    call_compaction_llm,
    compact_context,
    estimate_entry_model_replay_tokens,
    estimate_entry_replay_tokens,
)
from opensquilla.session.compaction_lifecycle import (
    compaction_effect_payload,
    compaction_result_payload,
)


def _make_entries(n: int, tokens_each: int = 100) -> list[dict]:
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"message {i} " + "x" * 50,
            "token_count": tokens_each,
        }
        for i in range(n)
    ]


def test_compaction_effect_payload_marks_automatic_noop_not_user_visible():
    payload = compaction_effect_payload(
        status="skipped",
        source="automatic",
        reason="within_compaction_budget",
    )

    assert payload == {
        "applied": False,
        "durability": "none",
        "skip_reason": "within_compaction_budget",
        "user_visible": False,
    }


def test_compaction_effect_payload_surfaces_non_benign_skip_reasons():
    for reason in ("coverage_blocked", "empty_summary", "no_safe_turn_boundary"):
        payload = compaction_effect_payload(
            status="skipped",
            source="automatic",
            reason=reason,
        )

        assert payload["applied"] is False
        assert payload["durability"] == "none"
        assert payload["skip_reason"] == reason
        assert payload["user_visible"] is True


def test_compaction_effect_payload_marks_durable_completion_applied():
    payload = compaction_effect_payload(status="completed", source="automatic")

    assert payload["applied"] is True
    assert payload["durability"] == "durable"
    assert payload["user_visible"] is True


@pytest.mark.asyncio
async def test_no_compaction_needed_small_context():
    entries = _make_entries(5, tokens_each=10)  # 50 tokens total
    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=10_000,  # huge window
        )
    )
    assert result.removed_count == 0
    assert result.kept_entries == entries
    assert result.summary_source == "skipped"
    assert result.skip_reason == "within_compaction_budget"
    assert result.kept_start_index == 0


@pytest.mark.asyncio
async def test_message_count_compaction_uses_exact_forced_prefix_within_token_budget(
    monkeypatch,
):
    calls: list[str] = []

    async def fake_llm(**kwargs):
        calls.append(kwargs["chunk_text"])
        # Deliberately larger than the removed entries. Message-count recovery
        # is still useful as long as the replacement fits the token window.
        return "count recovery summary " * 40

    monkeypatch.setattr("opensquilla.session.compaction.call_compaction_llm", fake_llm)
    entries = [
        {"role": "user", "content": "old user 0", "token_count": 5},
        {"role": "assistant", "content": "old assistant 1", "token_count": 5},
        {"role": "user", "content": "protected current request", "token_count": 5},
        {"role": "assistant", "content": "protected current answer", "token_count": 5},
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="message-count",
            entries=entries,
            context_window_tokens=2_000,
            config=CompactionConfig(
                model="test/model",
                api_key="test-key",
                safety_margin=1.0,
                protected_recent_messages=2,
            ),
            forced_prefix_cut=2,
            trigger="message_count",
            reason="provider_messages_limit",
        )
    )

    assert calls
    assert "old user 0" in "\n".join(calls)
    assert "old assistant 1" in "\n".join(calls)
    assert "protected current request" not in "\n".join(calls)
    assert result.removed_count == 2
    assert result.kept_start_index == 2
    assert result.kept_entries == entries[2:]
    assert result.kept_entries[0] is entries[2]
    assert result.kept_entries[1] is entries[3]
    assert result.tokens_after >= result.tokens_before
    assert result.quality_report["fits_context_window"] is True
    assert result.quality_report["passes_structural_gate"] is True


@pytest.mark.asyncio
async def test_compaction_request_passes_explicit_correlation_to_every_chunk(
    monkeypatch,
) -> None:
    observed: list[ProviderRequestCorrelation | None] = []

    async def fake_llm(**kwargs):
        observed.append(kwargs.get("provider_request_correlation"))
        return "bounded historical summary"

    monkeypatch.setattr("opensquilla.session.compaction.call_compaction_llm", fake_llm)
    correlation = ProviderRequestCorrelation(
        session_id="session-1",
        turn_id="turn-1",
        execution_id="compaction-1",
        call_kind="auxiliary.compaction",
    )
    await compact_context(
        CompactionRequest(
            session_id="session-1",
            entries=_make_entries(12, tokens_each=20),
            context_window_tokens=100,
            config=CompactionConfig(
                model="test/model",
                api_key="test-key",
                safety_margin=1.0,
            ),
            provider_request_correlation=correlation,
        )
    )

    assert observed
    assert all(item is correlation for item in observed)


@pytest.mark.asyncio
async def test_message_count_compaction_summarizes_large_prefix_with_one_llm_call(
    monkeypatch,
):
    calls: list[str] = []

    async def fake_llm(**kwargs):
        calls.append(kwargs["chunk_text"])
        return "one bounded historical summary"

    monkeypatch.setattr("opensquilla.session.compaction.call_compaction_llm", fake_llm)
    entries = _make_entries(104, tokens_each=1)

    result = await compact_context(
        CompactionRequest(
            session_id="message-count-large-prefix",
            entries=entries,
            context_window_tokens=128_000,
            config=CompactionConfig(
                model="test/model",
                api_key="test-key",
                protected_recent_messages=86,
            ),
            forced_prefix_cut=18,
            trigger="message_count",
            reason="provider_request_message_limit",
        )
    )

    assert len(calls) == 1
    assert "message 0" in calls[0]
    assert "message 17" in calls[0]
    assert "message 18" not in calls[0]
    assert result.chunks_processed == 1
    assert result.removed_count == 18
    assert result.kept_start_index == 18
    assert result.kept_entries == entries[18:]


@pytest.mark.asyncio
async def test_token_trigger_still_rejects_forced_summary_that_does_not_reduce_tokens(
    monkeypatch,
):
    async def fake_llm(**kwargs):
        return "larger replacement summary " * 40

    monkeypatch.setattr("opensquilla.session.compaction.call_compaction_llm", fake_llm)
    entries = _make_entries(4, tokens_each=5)

    result = await compact_context(
        CompactionRequest(
            session_id="token-trigger",
            entries=entries,
            context_window_tokens=2_000,
            config=CompactionConfig(
                model="test/model",
                api_key="test-key",
                safety_margin=1.0,
            ),
            forced_prefix_cut=2,
        )
    )

    assert result.removed_count == 0
    assert result.kept_start_index == 0
    assert result.kept_entries == entries
    assert result.skip_reason == "quality_gate_failed"
    assert result.quality_report["fits_context_window"] is True
    assert result.quality_report["passes_structural_gate"] is False


@pytest.mark.asyncio
async def test_forced_prefix_cut_refuses_protected_tail_overlap():
    entries = _make_entries(4, tokens_each=5)

    result = await compact_context(
        CompactionRequest(
            session_id="protected-tail",
            entries=entries,
            context_window_tokens=2_000,
            config=CompactionConfig(protected_recent_messages=2),
            forced_prefix_cut=3,
            trigger="message_count",
        )
    )

    assert result.removed_count == 0
    assert result.kept_start_index == 0
    assert result.kept_entries == entries
    assert result.skip_reason == "forced_prefix_cut_overlaps_protected_tail"


@pytest.mark.asyncio
async def test_forced_prefix_cut_refuses_split_tool_segment():
    entries = [
        {"role": "user", "content": "old context", "token_count": 5},
        {
            "role": "assistant",
            "content": "calling tool",
            "tool_calls": [{"id": "call_1", "type": "function"}],
            "token_count": 5,
        },
        {
            "role": "tool",
            "content": "tool result",
            "tool_call_id": "call_1",
            "token_count": 5,
        },
        {"role": "user", "content": "current request", "token_count": 5},
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="tool-boundary",
            entries=entries,
            context_window_tokens=2_000,
            forced_prefix_cut=2,
            trigger="message_count",
        )
    )

    assert result.removed_count == 0
    assert result.kept_start_index == 0
    assert result.kept_entries == entries
    assert result.skip_reason == "forced_prefix_cut_splits_tool_segment"


@pytest.mark.asyncio
async def test_compaction_occurs_when_over_budget():
    entries = _make_entries(20, tokens_each=200)  # 4000 tokens
    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=1600,  # tight enough to compact, large enough for the result
            config=CompactionConfig(safety_margin=1.0),
        )
    )
    assert result.removed_count > 0
    assert result.summary != ""
    assert result.chunks_processed >= 1
    assert result.summary_source == "fallback"
    assert result.tokens_before == 4000
    assert result.tokens_after < result.tokens_before
    assert result.remaining_budget_tokens >= 0


def _make_tool_heavy_entries(
    turns: int = 10, pairs: int = 4, result_chars: int = 2000
) -> list[dict]:
    line = "drwxr-xr-x staff 4096 synthetic/file.txt "
    result_text = (line * (result_chars // len(line) + 1))[:result_chars]
    entries: list[dict] = []
    for turn in range(turns):
        tool_calls: list[dict] = []
        for pair in range(pairs):
            tool_id = f"tool-{turn}-{pair}"
            tool_calls.append(
                {
                    "type": "tool_use",
                    "tool_use_id": tool_id,
                    "name": "exec_shell",
                    "input": {"command": f"ls batch_{turn}/{pair}"},
                }
            )
            tool_calls.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "name": "exec_shell",
                    "result": result_text,
                    "is_error": False,
                }
            )
        entries.append({"role": "user", "content": f"inspect batch {turn}", "token_count": None})
        entries.append(
            {
                "role": "assistant",
                "content": f"inspected batch {turn}",
                "token_count": 120,
                "tool_calls": tool_calls,
            }
        )
    return entries


@pytest.mark.asyncio
async def test_budget_check_counts_full_tool_call_replay_not_summarized_previews():
    entries = _make_tool_heavy_entries()
    window = 16_000
    summarized = sum(estimate_entry_replay_tokens(e) for e in entries)
    model_replay = sum(estimate_entry_model_replay_tokens(e) for e in entries)
    # The summarized estimate looks within budget while the model replay overflows.
    assert summarized * 1.2 <= window < model_replay

    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=window,
            config=CompactionConfig(model=None, api_key=""),
        )
    )

    assert result.skip_reason != "within_compaction_budget"
    assert result.removed_count > 0


def test_replay_token_estimate_uses_tool_payload_summary_not_raw_arguments():
    large_content = "x" * 80_000
    entry = {
        "role": "assistant",
        "content": "wrote file",
        "token_count": 1,
        "tool_calls": [
            {
                "type": "tool_use",
                "tool_use_id": "write-large",
                "name": "write_file",
                "input": {"path": "index.html", "content": large_content},
            }
        ],
        "reasoning_content": "private reasoning " + ("r" * 20_000),
    }

    tokens = estimate_entry_replay_tokens(entry)

    assert tokens < 500


def test_provider_config_preserves_profile_when_compaction_llm_disabled():
    cfg = build_compaction_config_from_provider(
        None,
        compaction_config=type(
            "CompactionSettings",
            (),
            {
                "enabled": False,
                "compaction_profile": "coding",
                "protected_recent_messages": 6,
            },
        )(),
    )

    assert cfg.model is None
    assert cfg.api_key == ""
    assert cfg.compaction_profile == "coding"
    assert cfg.protected_recent_messages == 6


@pytest.mark.asyncio
async def test_compaction_source_is_llm_when_all_chunks_use_llm(monkeypatch):
    calls: list[str] = []

    async def fake_llm(**kwargs):
        calls.append(kwargs["chunk_text"])
        return "LLM summary"

    monkeypatch.setattr("opensquilla.session.compaction.call_compaction_llm", fake_llm)
    entries = _make_entries(12, tokens_each=200)

    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=500,
            config=CompactionConfig(model="test/model", api_key="test-key"),
        )
    )

    assert calls
    assert result.removed_count > 0
    assert result.summary_source == "llm"


@pytest.mark.asyncio
async def test_compaction_source_is_mixed_when_llm_partly_falls_back(monkeypatch):
    responses = ["LLM summary", None]

    async def fake_llm(**kwargs):
        return responses.pop(0) if responses else "LLM summary"

    monkeypatch.setattr("opensquilla.session.compaction.call_compaction_llm", fake_llm)
    entries = _make_entries(12, tokens_each=200)

    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=500,
            config=CompactionConfig(model="test/model", api_key="test-key"),
        )
    )

    assert result.removed_count > 0
    assert result.summary_source == "mixed"


@pytest.mark.asyncio
async def test_compaction_keeps_recent_entries():
    entries = _make_entries(20, tokens_each=200)
    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=1500,
        )
    )
    # kept entries should be a tail of the original
    if result.kept_entries:
        last_kept = result.kept_entries[-1]
        assert last_kept in entries[-len(result.kept_entries) :]


@pytest.mark.asyncio
async def test_coding_profile_preserves_configured_recent_tail():
    entries = _make_entries(30, tokens_each=250)
    protected_tail = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"active task message {i}",
            "token_count": 5,
        }
        for i in range(4)
    ]
    entries.extend(protected_tail)

    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=1500,
            config=CompactionConfig(
                safety_margin=1.0,
                compaction_profile="coding",
                protected_recent_messages=4,
            ),
        )
    )

    assert result.removed_count > 0
    assert result.kept_entries[-4:] == protected_tail
    assert result.quality_report["profile"] == "coding"
    assert result.quality_report["protected_recent_messages"] == 4
    assert result.quality_report["protected_tail_preserved"] is True
    assert result.quality_report["fits_context_window"] is True
    assert result.quality_report["passes_structural_gate"] is True
    assert compaction_result_payload(result)["quality_report"][
        "passes_structural_gate"
    ] is True


@pytest.mark.asyncio
async def test_quality_report_marks_compaction_that_still_exceeds_window():
    entries = [
        {"role": "user", "content": "old context", "token_count": 10_000},
        *[
            {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"large protected tail {i}",
                "token_count": 500,
            }
            for i in range(5)
        ],
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=1000,
            config=CompactionConfig(
                safety_margin=1.0,
                protected_recent_messages=5,
            ),
        )
    )

    assert result.removed_count > 0
    assert result.quality_report["fits_context_window"] is False
    assert result.quality_report["passes_structural_gate"] is True
    assert compaction_result_payload(result)["quality_report"][
        "fits_context_window"
    ] is False


@pytest.mark.asyncio
async def test_protected_tail_retreats_to_tool_boundary():
    entries = [
        {"role": "user", "content": "old context", "token_count": 300},
        {
            "role": "assistant",
            "content": "[Used tool: read_file]",
            "token_count": 5,
        },
        {
            "role": "user",
            "content": "[Tool result (toolu_1): file contents]",
            "token_count": 5,
        },
        {"role": "user", "content": "next question", "token_count": 5},
        {"role": "assistant", "content": "answer", "token_count": 5},
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=100,
            config=CompactionConfig(
                safety_margin=1.0,
                protected_recent_messages=3,
            ),
        )
    )

    assert result.removed_count > 0
    assert result.kept_entries[0]["content"] == "[Used tool: read_file]"
    assert result.kept_entries[1]["content"].startswith("[Tool result ")
    assert result.quality_report["protected_tail_preserved"] is True


@pytest.mark.asyncio
async def test_protected_tail_retreats_over_multi_result_tool_segment():
    entries = [
        {"role": "user", "content": "old context", "token_count": 300},
        {
            "role": "assistant",
            "content": "calling tool",
            "tool_calls": [{"id": "call_1", "type": "function"}],
            "token_count": 5,
        },
        {
            "role": "tool",
            "content": "first result",
            "tool_call_id": "call_1",
            "token_count": 5,
        },
        {
            "role": "tool",
            "content": "second result",
            "tool_call_id": "call_1",
            "token_count": 5,
        },
        {"role": "user", "content": "next question", "token_count": 5},
        {"role": "assistant", "content": "answer", "token_count": 5},
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=120,
            config=CompactionConfig(
                safety_margin=1.0,
                protected_recent_messages=3,
            ),
        )
    )

    assert result.removed_count > 0
    assert result.kept_entries[0]["role"] == "assistant"
    assert result.kept_entries[1]["content"] == "first result"
    assert result.kept_entries[2]["content"] == "second result"
    assert result.quality_report["protected_tail_preserved"] is True


@pytest.mark.asyncio
async def test_empty_entries():
    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=[],
            context_window_tokens=1000,
        )
    )
    assert result.removed_count == 0
    assert result.kept_entries == []
    assert result.summary == ""
    assert result.skip_reason == "no_entries"


@pytest.mark.asyncio
async def test_custom_config():
    entries = _make_entries(20, tokens_each=200)
    cfg = CompactionConfig(
        base_chunk_ratio=0.3,
        min_chunk_ratio=0.1,
        safety_margin=1.0,
        default_parts=3,
        identifier_policy="off",
    )
    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=1000,
            config=cfg,
        )
    )
    assert result.removed_count > 0


@pytest.mark.asyncio
async def test_strict_identifier_policy_in_summary():
    entries = _make_entries(10, tokens_each=200)
    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=500,
            config=CompactionConfig(identifier_policy="strict"),
        )
    )
    if result.summary:
        assert "identifier" in result.summary.lower() or "IMPORTANT" in result.summary


@pytest.mark.asyncio
async def test_chunks_processed_count():
    entries = _make_entries(30, tokens_each=200)
    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=500,
        )
    )
    assert result.chunks_processed >= 1


@pytest.mark.asyncio
async def test_call_compaction_llm_adds_openrouter_app_attribution(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "summary"}}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def post(self, url, *, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(
        "opensquilla.session.compaction.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )

    result = await call_compaction_llm(
        chunk_text="old conversation",
        identifier_instruction="",
        model="openai/gpt-4o-mini",
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        timeout=10.0,
    )

    assert result == "summary"
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["headers"] == {
        "Authorization": "Bearer test-key",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://opensquilla.ai",
        "X-Title": "OpenSquilla",
    }


@pytest.mark.asyncio
async def test_call_compaction_llm_adds_tokenrhythm_app_attribution(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "summary"}}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def post(self, url, *, json, headers):
            captured["url"] = url
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(
        "opensquilla.session.compaction.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )

    result = await call_compaction_llm(
        chunk_text="old conversation",
        identifier_instruction="",
        model="deepseek-v4-flash",
        api_key="test-key",
        base_url="https://tokenrhythm.studio/v1",
        provider="tokenrhythm",
        provider_request_correlation=ProviderRequestCorrelation(
            session_id="session-1",
            turn_id="turn-1",
            execution_id="compaction-1",
            call_kind="auxiliary.compaction",
        ),
    )

    assert result == "summary"
    assert captured["url"] == "https://tokenrhythm.studio/v1/chat/completions"
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["HTTP-Referer"] == "https://opensquilla.ai"
    assert headers["X-OpenSquilla-Session-Id"] == "session-1"
    assert headers["X-OpenSquilla-Turn-Id"] == "turn-1"
    assert headers["X-OpenSquilla-Execution-Id"] == "compaction-1"
    assert headers["X-OpenSquilla-Call-Kind"] == "auxiliary.compaction"
    assert headers["X-Title"] == "OpenSquilla"


@pytest.mark.asyncio
async def test_call_compaction_llm_timeout_returns_none(monkeypatch) -> None:
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def post(self, url, *, json, headers):
            raise TimeoutError("summary timed out")

    monkeypatch.setattr(
        "opensquilla.session.compaction.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )

    result = await call_compaction_llm(
        chunk_text="old conversation",
        identifier_instruction="",
        model="openai/gpt-4o-mini",
        api_key="test-key",
        timeout=0.01,
    )

    assert result is None


@pytest.mark.asyncio
async def test_custom_instructions_are_user_scoped_and_identifier_policy_stays_system(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "summary"}}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def post(self, url, *, json, headers):
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(
        "opensquilla.session.compaction.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )

    await call_compaction_llm(
        chunk_text="old conversation",
        identifier_instruction="Preserve exact IDs.",
        model="openai/gpt-4o-mini",
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        timeout=10.0,
        custom_instructions="Focus on deployment decisions.",
    )

    messages = captured["json"]["messages"]
    assert messages[0]["role"] == "system"
    assert "Preserve exact IDs." in messages[0]["content"]
    assert "Focus on deployment decisions." not in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert "Focus on deployment decisions." in messages[1]["content"]
