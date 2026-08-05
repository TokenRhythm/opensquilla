from __future__ import annotations

import json

import pytest

from opensquilla.provider import request_proof
from opensquilla.provider.anthropic import AnthropicProvider
from opensquilla.provider.ollama import OllamaProvider
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.protocol import project_provider_final_request
from opensquilla.provider.request_proof import (
    ProviderRequestBudgetExceeded,
    _final_hard_cap_payload_once,
    project_final_request_payload,
    project_provider_payload,
    protected_tool_result_indexes,
    prove_or_compact_provider_payload,
    prove_provider_payload,
)
from opensquilla.provider.types import ChatConfig, ContentBlockToolResult, Message


@pytest.fixture(autouse=True)
def _rollback_default_safety_levers(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_RECENT_ASSISTANT",
        "OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_RECENT_RESULTS",
        "OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_ERROR_RESULTS",
        "OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_UNRESOLVED_RESULTS",
        "OPENSQUILLA_PROVIDER_COMPACTION_SKIP_PROJECTED",
        "OPENSQUILLA_PROVIDER_COMPACTION_NEVER_WORSE",
    ):
        monkeypatch.setenv(name, "0")
    monkeypatch.setattr(
        request_proof,
        "_serialized_token_estimate",
        lambda serialized: (max(1, len(serialized) // 4), "legacy_test_estimate"),
    )


def test_provider_request_proof_allows_payload_within_budget() -> None:
    proof = prove_provider_payload(
        {
            "messages": [{"role": "user", "content": "small"}],
            "tools": [{"name": "tool", "description": "desc"}],
        },
        projection_adapter="openai",
        proof_budget=10_000,
    )

    assert proof["fits"] is True
    assert proof["projection_adapter"] == "openai"
    assert proof["estimated_chars"] < 10_000
    assert proof["messages_chars"] > 0
    assert proof["tools_chars"] > 0
    assert proof["system_chars"] == 0
    assert proof["top_level_chars"] == 0
    assert proof["tool_schema_too_large"] is False


def test_provider_request_proof_blocks_oversized_payload() -> None:
    with pytest.raises(ProviderRequestBudgetExceeded) as exc_info:
        prove_provider_payload(
            {"messages": [{"role": "user", "content": "x" * 5000}]},
            projection_adapter="openai",
            proof_budget=1000,
        )

    assert exc_info.value.proof["fits"] is False
    assert exc_info.value.proof["fallback_reason"] == "provider_request_budget_exhausted"
    assert exc_info.value.proof["top_contributors"][0]["chars"] == 5000


def test_provider_request_projection_reports_overflow_without_raising() -> None:
    proof = project_provider_payload(
        {"messages": [{"role": "user", "content": "x" * 5000}]},
        projection_adapter="openai",
        proof_budget=1000,
    )

    assert proof["fits"] is False
    assert proof["fits_char_budget"] is False
    assert proof["fits_token_budget"] is False
    assert proof["fallback_reason"] == "provider_request_budget_exhausted"


def test_final_request_projection_includes_media_and_message_admission() -> None:
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64," + ("AA==" * 32)
                        },
                    }
                ],
            }
        ]
    }

    projection = project_final_request_payload(
        payload,
        projection_adapter="openai",
        proof_budget=100_000,
        status_projection_mode="content_envelope",
        message_limit=1,
    )

    assert projection.payload is payload
    assert projection.wire_message_count == 1
    assert projection.message_limit == 1
    assert projection.fits_message_count is True
    assert projection.fits is True
    assert projection.proof["media_blocks_reserved"] == 1
    assert projection.proof["media_reserve_tokens"] > 0
    assert projection.proof["estimated_tokens"] >= projection.proof["media_reserve_tokens"]
    assert projection.proof["effective_proof_budget"] < 100_000
    assert projection.proof["wire_json_bytes"] >= projection.proof["wire_json_chars"]
    assert "AA==" not in repr(projection)


def test_final_request_projection_marks_known_message_limit_overflow() -> None:
    projection = project_final_request_payload(
        {
            "messages": [
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
            ]
        },
        projection_adapter="openai",
        proof_budget=100_000,
        message_limit=1,
    )

    assert projection.proof["fits_char_budget"] is True
    assert projection.proof["fits_token_budget"] is True
    assert projection.fits_message_count is False
    assert projection.fits is False
    assert projection.proof["fallback_reason"] == "provider_request_message_limit"


def test_duck_typed_final_request_projection_missing_or_invalid_is_none() -> None:
    class _Missing:
        pass

    class _Raising:
        def project_final_request(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            raise RuntimeError("synthetic")

    class _Invalid:
        def project_final_request(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            return {"fits": True}

    messages = []
    assert project_provider_final_request(None, messages) is None
    assert project_provider_final_request(_Missing(), messages) is None
    assert project_provider_final_request(_Raising(), messages) is None
    assert project_provider_final_request(_Invalid(), messages) is None


def test_provider_request_proof_checks_serialized_tokens_as_well_as_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def _token_estimate(serialized_payload: str) -> tuple[int, str]:
        captured.append(serialized_payload)
        return 200, "synthetic_tokenizer"

    monkeypatch.setattr(request_proof, "_serialized_token_estimate", _token_estimate)
    payload = {"messages": [{"role": "user", "content": "small"}]}

    with pytest.raises(ProviderRequestBudgetExceeded) as exc_info:
        prove_provider_payload(
            payload,
            projection_adapter="openai",
            proof_budget=1000,
        )

    proof = exc_info.value.proof
    assert json.loads(captured[0]) == payload
    assert proof["fits_char_budget"] is True
    assert proof["fits_token_budget"] is False
    assert proof["provider_window_mismatch"] is True
    assert proof["estimated_text_tokens"] == 200
    assert proof["estimated_tokens"] == 200
    assert proof["effective_proof_token_budget"] < 200
    assert proof["token_estimate_source"] == "synthetic_tokenizer"


def test_provider_request_proof_adds_media_reserve_to_serialized_text_tokens() -> None:
    proof = prove_provider_payload(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64," + ("a" * 128),
                            },
                        },
                    ],
                }
            ]
        },
        projection_adapter="openai",
        proof_budget=10_000,
    )

    assert proof["estimated_tokens"] == (
        proof["projected_text_tokens"] + proof["media_reserve_tokens"]
    )
    assert proof["fits_token_budget"] is True


def test_provider_request_proof_uses_effective_budget_headroom() -> None:
    payload = {"messages": [{"role": "user", "content": "x" * 9400}]}

    with pytest.raises(ProviderRequestBudgetExceeded) as exc_info:
        prove_provider_payload(
            payload,
            projection_adapter="openrouter",
            proof_budget=10_000,
        )

    proof = exc_info.value.proof
    assert proof["fits"] is False
    assert proof["proof_budget"] == 10_000
    assert proof["raw_proof_budget"] == 10_000
    assert proof["effective_proof_budget"] < proof["raw_proof_budget"]
    assert proof["proof_headroom_chars"] > 0
    assert proof["estimated_chars"] <= proof["raw_proof_budget"]
    assert proof["estimated_chars"] > proof["effective_proof_budget"]


def test_provider_request_proof_excludes_native_image_payload_from_text_budget() -> None:
    proof = prove_provider_payload(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64," + ("a" * 5000),
                            },
                        },
                    ],
                }
            ]
        },
        projection_adapter="openrouter",
        proof_budget=10_000,
        status_projection_mode="content_envelope",
    )

    assert proof["fits"] is True
    assert proof["media_blocks_excluded"] == 1
    assert proof["media_chars_excluded"] > 5000
    assert proof["top_contributors"][0]["chars"] < 5000
    assert proof["media_blocks_reserved"] == 1
    assert proof["media_image_blocks"] == 1
    assert proof["media_pdf_blocks"] == 0
    assert proof["media_reserve_tokens"] >= 1024
    assert proof["media_reserve_chars"] == proof["media_reserve_tokens"] * 4
    assert proof["usage_source"] == "projected_text_plus_media_reserve"
    assert proof["wire_json_chars"] > proof["projected_context_chars"]
    assert proof["wire_json_bytes"] >= proof["wire_json_chars"]


def test_provider_request_proof_excludes_anthropic_base64_media_from_text_budget() -> None:
    proof = prove_provider_payload(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "summarize this"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "a" * 5000,
                            },
                        },
                    ],
                }
            ]
        },
        projection_adapter="anthropic",
        proof_budget=10_000,
        status_projection_mode="content_envelope",
    )

    assert proof["fits"] is True
    assert proof["media_blocks_excluded"] == 1
    assert proof["media_chars_excluded"] == 5000
    assert proof["top_contributors"][0]["chars"] < 5000
    assert proof["media_decoded_bytes_estimated"] > 0


def test_provider_request_proof_reserves_nonzero_budget_for_small_image() -> None:
    proof = prove_provider_payload(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64," + ("a" * 128),
                            },
                        },
                    ],
                }
            ]
        },
        projection_adapter="openai",
        proof_budget=10_000,
        status_projection_mode="content_envelope",
    )

    assert proof["fits"] is True
    assert proof["media_blocks_reserved"] == 1
    assert proof["media_reserve_tokens"] > 0
    assert proof["estimated_chars"] > proof["projected_text_chars"]


def test_provider_request_proof_blocks_media_only_request_when_reserve_exceeds_budget() -> None:
    with pytest.raises(ProviderRequestBudgetExceeded) as exc_info:
        prove_provider_payload(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64," + ("a" * 500_000),
                                },
                            },
                        ],
                    }
                ]
            },
            projection_adapter="openai",
            proof_budget=7000,
            status_projection_mode="content_envelope",
        )

    proof = exc_info.value.proof
    assert proof["fits"] is False
    assert proof["media_blocks_reserved"] == 1
    assert proof["media_reserve_chars"] > proof["effective_proof_budget"]
    assert proof["top_contributors"][0]["path"] == "$.__media_token_equivalent_reserve"


def test_provider_request_proof_uses_larger_reserve_for_pdf_than_image() -> None:
    encoded = "a" * 4096
    image_proof = prove_provider_payload(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": encoded,
                            },
                        }
                    ],
                }
            ]
        },
        projection_adapter="anthropic",
        proof_budget=100_000,
    )
    pdf_proof = prove_provider_payload(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": encoded,
                            },
                        }
                    ],
                }
            ]
        },
        projection_adapter="anthropic",
        proof_budget=100_000,
    )

    assert image_proof["media_image_blocks"] == 1
    assert image_proof["media_pdf_blocks"] == 0
    assert pdf_proof["media_image_blocks"] == 0
    assert pdf_proof["media_pdf_blocks"] == 1
    assert pdf_proof["media_reserve_tokens"] > image_proof["media_reserve_tokens"]


def test_provider_request_proof_still_blocks_large_text_next_to_native_media() -> None:
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "x" * 5000},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64," + ("a" * 5000),
                        },
                    },
                ],
            }
        ]
    }

    with pytest.raises(ProviderRequestBudgetExceeded) as exc_info:
        prove_provider_payload(
            payload,
            projection_adapter="openrouter",
            proof_budget=1000,
            status_projection_mode="content_envelope",
        )

    proof = exc_info.value.proof
    assert proof["fits"] is False
    assert proof["media_blocks_excluded"] == 1
    assert proof["top_contributors"][0]["chars"] == 5000


def test_provider_request_proof_compacts_tool_payload_once() -> None:
    payload = {
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": "x" * 5000},
        ]
    }

    compacted, proof = prove_or_compact_provider_payload(
        payload,
        projection_adapter="openai",
        proof_budget=2000,
        status_projection_mode="content_envelope",
    )

    assert proof is not None
    assert proof["fits"] is True
    assert proof["retry_count"] == 1
    assert len(compacted["messages"][1]["content"]) < 2000


def test_logical_unresolved_tool_result_stays_raw_after_wire_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_UNRESOLVED_RESULTS",
        "1",
    )
    unresolved = "unresolved-" + ("x" * 3000)
    logical_messages = [
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="background-1",
                    content=unresolved,
                    execution_status={
                        "version": 1,
                        "status": "unknown",
                        "exit_code": None,
                        "timed_out": False,
                        "truncated": False,
                        "reason": "background_running",
                        "source": "tool_runtime",
                        "preservation_class": "ephemeral",
                    },
                )
            ],
        )
    ]
    protected_indexes = protected_tool_result_indexes(logical_messages)
    payload = {
        "messages": [
            {"role": "tool", "tool_call_id": "background-1", "content": unresolved},
            {"role": "tool", "tool_call_id": "done-1", "content": "a" * 3000},
            {"role": "tool", "tool_call_id": "done-2", "content": "b" * 3000},
            {"role": "tool", "tool_call_id": "done-3", "content": "c" * 3000},
        ]
    }

    compacted, proof = prove_or_compact_provider_payload(
        payload,
        projection_adapter="openai",
        proof_budget=8500,
        status_projection_mode="content_envelope",
        protected_tool_result_indexes=protected_indexes,
    )

    assert protected_indexes == frozenset({0})
    assert proof is not None
    assert proof["fits"] is True
    assert proof["protected_tool_result_count"] == 1
    assert compacted["messages"][0]["content"] == unresolved
    assert len(compacted["messages"][1]["content"]) < 3000


@pytest.mark.parametrize(
    "provider",
    [
        OpenAIProvider(api_key="test", model="gpt-test"),
        AnthropicProvider(api_key="test", model="claude-test"),
        OllamaProvider(model="llama-test"),
    ],
)
def test_adapter_projection_keeps_unresolved_protection_out_of_band(
    monkeypatch: pytest.MonkeyPatch,
    provider: object,
) -> None:
    monkeypatch.setenv(
        "OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_UNRESOLVED_RESULTS",
        "1",
    )
    messages = [
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="background-1",
                    content="still running",
                    execution_status={
                        "version": 1,
                        "status": "unknown",
                        "exit_code": None,
                        "timed_out": False,
                        "truncated": False,
                        "reason": "background_running",
                        "source": "tool_runtime",
                        "preservation_class": "ephemeral",
                    },
                )
            ],
        )
    ]

    projection = project_provider_final_request(
        provider,
        messages,
        config=ChatConfig(provider_request_max_chars=100_000),
    )

    assert projection is not None
    assert projection.proof["protected_tool_result_count"] == 1
    assert "background_running" not in json.dumps(
        projection.payload,
        ensure_ascii=False,
    )


def test_provider_request_proof_blocks_after_all_reduction_tiers_fail() -> None:
    payload = {"messages": [{"role": "tool", "content": "x" * 5000}]}

    with pytest.raises(ProviderRequestBudgetExceeded) as exc_info:
        prove_or_compact_provider_payload(
            payload,
            projection_adapter="openai",
            proof_budget=100,
            status_projection_mode="content_envelope",
        )

    assert exc_info.value.proof["fits"] is False
    assert exc_info.value.proof["retry_count"] == 4


def test_provider_request_proof_compacts_large_tool_args_preserving_protocol() -> None:
    large_arguments = json.dumps(
        {
            "cmd": "python build_report.py",
            "script": "print('start')\n" + ("x = 1\n" * 500) + "print('end')",
        }
    )
    payload = {
        "messages": [
            {"role": "system", "content": "system"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "exec_command",
                            "arguments": large_arguments,
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ]
    }

    compacted, proof = prove_or_compact_provider_payload(
        payload,
        projection_adapter="openrouter",
        proof_budget=2200,
        status_projection_mode="content_envelope",
    )

    assert proof is not None
    assert proof["fits"] is True
    assert proof["retry_count"] == 2
    assistant_message = compacted["messages"][1]
    tool_calls = assistant_message["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["id"] == "call_1"
    assert tool_calls[0]["function"]["name"] == "exec_command"
    compacted_arguments = tool_calls[0]["function"]["arguments"]
    assert compacted_arguments != large_arguments
    parsed_arguments = json.loads(compacted_arguments)
    assert parsed_arguments["cmd"] == "python build_report.py"
    assert "provider_request_tool_input_compacted" in parsed_arguments["script"]
    tool_result_message = compacted["messages"][2]
    assert tool_result_message["role"] == "tool"
    assert tool_result_message["tool_call_id"] == "call_1"
    assert tool_result_message["content"] == "ok"
    serialized = json.dumps(compacted, ensure_ascii=False)
    assert "_opensquilla_compacted_tool_arguments" not in serialized
    assert "_invalid_provider_context_arguments" not in serialized
    assert "Historical tool call omitted" not in serialized
    assert payload["messages"][1]["tool_calls"][0]["function"]["arguments"] == large_arguments


def test_provider_request_proof_preserves_aggregate_tool_call_protocol() -> None:
    tool_calls = []
    original_arguments: list[str] = []
    for index in range(36):
        arguments = json.dumps(
            {
                "path": f"generated/file-{index}.html",
                "content": "x" * 520,
            },
            separators=(",", ":"),
        )
        assert len(arguments) < 640
        original_arguments.append(arguments)
        tool_calls.append(
            {
                "id": f"call_{index}",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": arguments,
                },
            }
        )

    payload = {
        "messages": [
            {"role": "user", "content": "build the app"},
            {
                "role": "assistant",
                "tool_calls": tool_calls,
            },
        ]
    }

    compacted, proof = prove_or_compact_provider_payload(
        payload,
        projection_adapter="openrouter",
        proof_budget=13_000,
        status_projection_mode="content_envelope",
    )

    assert proof is not None
    assert proof["fits"] is True
    assert proof["compact_needed"] is True
    assert proof["aggregate_tool_arguments_compacted"] is True
    assert set(compacted) == {"messages"}
    compacted_arguments = [
        call["function"]["arguments"]
        for call in compacted["messages"][1].get("tool_calls", [])
    ]
    assert len(compacted_arguments) == 36
    assert compacted_arguments[0] != original_arguments[0]
    parsed_first = json.loads(compacted_arguments[0])
    assert parsed_first["path"] == "generated/file-0.html"
    assert "provider_request_tool_input_compacted" in parsed_first["content"]
    assistant_message = compacted["messages"][1]
    assert "tool_calls" in assistant_message
    assert [call["id"] for call in assistant_message["tool_calls"][:3]] == [
        "call_0",
        "call_1",
        "call_2",
    ]
    serialized = json.dumps(compacted, ensure_ascii=False)
    assert "_opensquilla_compacted_tool_arguments" not in serialized
    assert "_invalid_provider_context_arguments" not in serialized
    assert "Historical tool call omitted" not in serialized
    assert payload["messages"][1]["tool_calls"][0]["function"]["arguments"] == original_arguments[0]


def test_provider_request_proof_compacts_leaked_tool_argument_projections() -> None:
    projection = (
        "[tool_use_argument_projection]\n"
        "tool: write_file\n"
        "tool_use_id: call_original\n"
        "field: content\n"
        "path: generated/app.css\n"
        "original_chars: 20000\n"
        "original_input_chars: 20500\n"
        "sha256: 1234567890abcdef\n"
        "tool_argument_handle: tr-1234567890abcdef\n"
        "omitted_chars: 20000\n"
        "reason: large tool argument compacted for provider context budget.\n"
        "head:\n"
        + ("x" * 700)
        + "\n...\ntail:\n"
        + ("y" * 200)
    )
    original_arguments = json.dumps(
        {
            "path": "generated/app.css",
            "content": projection,
        },
        separators=(",", ":"),
    )
    payload = {
        "messages": [
            {"role": "user", "content": "continue the app"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_projected",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": original_arguments,
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_projected", "content": "error"},
        ]
    }

    compacted, proof = prove_or_compact_provider_payload(
        payload,
        projection_adapter="openrouter",
        proof_budget=2200,
        status_projection_mode="content_envelope",
    )

    assert proof is not None
    compacted_arguments = compacted["messages"][1]["tool_calls"][0]["function"][
        "arguments"
    ]
    assert "_invalid_provider_context_arguments" in compacted_arguments
    assert "_opensquilla_compacted_tool_arguments" not in compacted_arguments
    assert "[tool_use_argument_projection]" not in compacted_arguments
    assert "tool_argument_handle: tr-1234567890abcdef" not in compacted_arguments
    assert "head:" not in compacted_arguments
    assert payload["messages"][1]["tool_calls"][0]["function"]["arguments"] == original_arguments


def test_provider_request_proof_compacts_leaked_provider_compacted_tool_arguments() -> None:
    original_arguments = json.dumps(
        {
            "_opensquilla_compacted_tool_arguments": True,
            "original_chars": 549,
            "sha256": "0" * 64,
            "argument_keys": ["command", "timeout"],
        },
        separators=(",", ":"),
    )
    payload = {
        "messages": [
            {"role": "user", "content": "open in chrome"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_compacted",
                        "type": "function",
                        "function": {
                            "name": "exec_command",
                            "arguments": original_arguments,
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_compacted", "content": "error"},
        ]
    }

    compacted, proof = prove_or_compact_provider_payload(
        payload,
        projection_adapter="openrouter",
        proof_budget=2200,
        status_projection_mode="content_envelope",
    )

    assert proof is not None
    compacted_arguments = compacted["messages"][1]["tool_calls"][0]["function"][
        "arguments"
    ]
    assert "_opensquilla_compacted_tool_arguments" not in compacted_arguments
    assert "command" not in compacted_arguments
    assert payload["messages"][1]["tool_calls"][0]["function"]["arguments"] == original_arguments


def test_provider_request_proof_compacts_string_provider_context_markers() -> None:
    original_arguments = json.dumps(
        {
            "_opensquilla_compacted_tool_arguments": "true",
            "original_chars": "549",
            "sha256": "0" * 64,
            "argument_keys": '["command", "timeout"]',
        },
        separators=(",", ":"),
    )
    payload = {
        "messages": [
            {"role": "user", "content": "open in chrome"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_compacted",
                        "type": "function",
                        "function": {
                            "name": "exec_command",
                            "arguments": original_arguments,
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_compacted", "content": "error"},
        ]
    }

    compacted, proof = prove_or_compact_provider_payload(
        payload,
        projection_adapter="openrouter",
        proof_budget=2200,
        status_projection_mode="content_envelope",
    )

    assert proof is not None
    compacted_arguments = compacted["messages"][1]["tool_calls"][0]["function"][
        "arguments"
    ]
    parsed = json.loads(compacted_arguments)
    assert parsed["_invalid_provider_context_arguments"] is True
    assert "_opensquilla_compacted_tool_arguments" not in compacted_arguments
    assert "command" not in compacted_arguments
    assert payload["messages"][1]["tool_calls"][0]["function"]["arguments"] == original_arguments


def test_provider_request_proof_compacts_leaked_tool_input_projections() -> None:
    projection = (
        "[tool_use_argument_projection]\n"
        "tool: write_file\n"
        "tool_use_id: call_input\n"
        "field: content\n"
        "path: generated/app.html\n"
        "original_chars: 25000\n"
        "sha256: abcdef1234567890\n"
        "tool_argument_handle: tr-abcdef1234567890\n"
        "reason: large tool argument compacted for provider context budget.\n"
        "head:\n"
        + ("h" * 800)
        + "\n...\ntail:\n"
        + ("t" * 200)
    )
    payload = {
        "messages": [
            {"role": "user", "content": "continue the app"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_input",
                        "name": "write_file",
                        "input": {
                            "path": "generated/app.html",
                            "content": projection,
                        },
                    }
                ],
            },
            {"role": "user", "content": "finish"},
        ]
    }

    compacted, proof = prove_or_compact_provider_payload(
        payload,
        projection_adapter="anthropic",
        proof_budget=2200,
        status_projection_mode="content_envelope",
    )

    assert proof is not None
    compacted_input = compacted["messages"][1]["content"][0]["input"]
    compacted_dump = json.dumps(compacted_input)
    assert "_invalid_provider_context_arguments" in compacted_dump
    assert "_opensquilla_compacted_tool_input" not in compacted_dump
    assert "[tool_use_argument_projection]" not in compacted_dump
    assert "tool_argument_handle: tr-abcdef1234567890" not in compacted_dump
    assert "head:" not in compacted_dump
    assert payload["messages"][1]["content"][0]["input"]["content"] == projection


def test_provider_request_proof_compacts_assistant_reasoning_content() -> None:
    payload = {
        "messages": [
            {"role": "user", "content": "continue"},
            {
                "role": "assistant",
                "content": "I will call a tool.",
                "reasoning_content": "thinking\n" + ("details\n" * 400),
            },
        ]
    }

    compacted, proof = prove_or_compact_provider_payload(
        payload,
        projection_adapter="openrouter",
        proof_budget=2200,
        status_projection_mode="content_envelope",
    )

    assert proof is not None
    assert proof["fits"] is True
    assert proof["retry_count"] == 2
    reasoning = compacted["messages"][1]["reasoning_content"]
    assert "[provider_request_reasoning_content_compacted:" in reasoning
    assert reasoning != payload["messages"][1]["reasoning_content"]


def test_provider_request_proof_compacts_segmented_assistant_text_tail() -> None:
    payload = {
        "messages": [
            {"role": "system", "content": "system prompt\n" + ("s" * 6400)},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "previous result\n" + ("x" * 20_000)}
                ],
            },
            {"role": "user", "content": "每过五分钟提醒我喝水"},
        ]
    }

    compacted, proof = prove_or_compact_provider_payload(
        payload,
        projection_adapter="openrouter",
        proof_budget=12_000,
        status_projection_mode="content_envelope",
    )

    assert proof is not None
    assert proof["fits"] is True
    assert proof["retry_count"] == 2
    assert proof["recent_tail_too_large"] is False
    assistant_text = compacted["messages"][1]["content"][0]["text"]
    assert "[provider_request_text_block_compacted:" in assistant_text
    assert assistant_text != payload["messages"][1]["content"][0]["text"]


def test_provider_request_proof_reports_recent_tail_after_tail_compaction_fails() -> None:
    payload = {
        "messages": [
            {"role": "system", "content": "x" * 5000},
            {"role": "user", "content": "hello"},
        ]
    }

    with pytest.raises(ProviderRequestBudgetExceeded) as exc_info:
        prove_or_compact_provider_payload(
            payload,
            projection_adapter="openrouter",
            proof_budget=1000,
            status_projection_mode="content_envelope",
        )

    proof = exc_info.value.proof
    assert proof["fits"] is False
    assert proof["retry_count"] == 4
    assert proof["recent_tail_too_large"] is True
    # All 4 escalating compaction tiers were exhausted before this raise.
    assert proof["compaction_tier"] == 4


def test_provider_request_proof_emergency_compacts_many_current_turn_tool_results() -> None:
    payload = {
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "research"},
            *[
                {"role": "tool", "tool_call_id": f"call_{index}", "content": "x" * 5000}
                for index in range(80)
            ],
        ]
    }

    compacted, proof = prove_or_compact_provider_payload(
        payload,
        projection_adapter="openrouter",
        proof_budget=96_000,
        status_projection_mode="content_envelope",
    )

    assert proof is not None
    assert proof["fits"] is True
    assert proof["retry_count"] == 3
    assert proof["emergency_current_turn_compacted"] is True
    assert proof["recent_tail_too_large"] is False
    assert compacted["messages"][2]["content"] != payload["messages"][2]["content"]


def test_provider_request_proof_hard_caps_many_tool_results_after_emergency_compaction() -> None:
    payload = {
        "messages": [
            {"role": "system", "content": "system prompt\n" + ("s" * 8_000)},
            {"role": "user", "content": "research several current agent papers"},
            {
                "role": "assistant",
                "content": "I will search and fetch sources.",
                "tool_calls": [
                    {
                        "id": f"call_{index}",
                        "type": "function",
                        "function": {
                            "name": "web_fetch",
                            "arguments": json.dumps(
                                {
                                    "url": f"https://example.com/paper-{index}",
                                    "note": "x" * 700,
                                },
                                separators=(",", ":"),
                            ),
                        },
                    }
                    for index in range(306)
                ],
            },
            *[
                {
                    "role": "tool",
                    "tool_call_id": f"call_{index}",
                    "content": "paper result\n" + ("long source excerpt\n" * 320),
                }
                for index in range(306)
            ],
            {"role": "user", "content": "write the brief"},
        ]
    }

    compacted, proof = prove_or_compact_provider_payload(
        payload,
        projection_adapter="openrouter",
        proof_budget=96_000,
        status_projection_mode="content_envelope",
    )

    assert proof is not None
    assert proof["fits"] is True
    assert proof["final_hard_cap_compacted"] is True
    assert proof["recent_tail_too_large"] is False
    assert compacted["messages"][-1]["content"] == "write the brief"
    assert compacted["messages"][3]["content"] != payload["messages"][3]["content"]


def test_provider_request_proof_hard_cap_compacts_leaked_tool_arguments() -> None:
    projection = (
        "[tool_use_argument_projection]\n"
        "tool: write_file\n"
        "tool_use_id: call_projected\n"
        "field: content\n"
        "path: generated/app.js\n"
        "original_chars: 30000\n"
        "sha256: fedcba0987654321\n"
        "tool_argument_handle: tr-fedcba0987654321\n"
        "head:\n"
        + ("j" * 700)
        + "\n...\ntail:\n"
        + ("k" * 200)
    )
    projected_arguments = json.dumps(
        {"path": "generated/app.js", "content": projection},
        separators=(",", ":"),
    )
    payload = {
        "messages": [
            {"role": "system", "content": "system prompt\n" + ("s" * 8_000)},
            {"role": "user", "content": "build the app"},
            {
                "role": "assistant",
                "content": "writing files",
                "tool_calls": [
                    {
                        "id": "call_projected",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": projected_arguments,
                        },
                    },
                    *[
                        {
                            "id": f"call_{index}",
                            "type": "function",
                            "function": {
                                "name": "web_fetch",
                                "arguments": json.dumps(
                                    {
                                        "url": f"https://example.com/{index}",
                                        "note": "x" * 700,
                                    },
                                    separators=(",", ":"),
                                ),
                            },
                        }
                        for index in range(306)
                    ],
                ],
            },
            {"role": "tool", "tool_call_id": "call_projected", "content": "error"},
            *[
                {
                    "role": "tool",
                    "tool_call_id": f"call_{index}",
                    "content": "paper result\n" + ("long source excerpt\n" * 320),
                }
                for index in range(306)
            ],
            {"role": "user", "content": "continue"},
        ]
    }

    compacted, proof = prove_or_compact_provider_payload(
        payload,
        projection_adapter="openrouter",
        proof_budget=96_000,
        status_projection_mode="content_envelope",
    )

    assert proof is not None
    assert proof["fits"] is True
    assert proof["final_hard_cap_compacted"] is True
    assistant_message = compacted["messages"][2]
    tool_calls = assistant_message["tool_calls"]
    assert len(tool_calls) == 307
    assert tool_calls[0]["id"] == "call_projected"
    assert tool_calls[0]["function"]["name"] == "write_file"
    projected_compacted_arguments = json.loads(
        tool_calls[0]["function"]["arguments"]
    )
    assert projected_compacted_arguments["_invalid_provider_context_arguments"] is True
    assert compacted["messages"][3]["role"] == "tool"
    assert compacted["messages"][3]["tool_call_id"] == "call_projected"
    serialized = json.dumps(compacted, ensure_ascii=False)
    assert "[tool_use_argument_projection]" not in serialized
    assert "tool_argument_handle: tr-fedcba0987654321" not in serialized
    assert "head:" not in serialized
    assert "_opensquilla_compacted_tool_arguments" not in serialized


def test_provider_request_proof_emergency_compacts_oversized_request_context() -> None:
    request_context = (
        "[Request context for this turn]\n"
        "This request-scoped context is not a user request and is not transcript history.\n"
        + ("workspace context\n" * 5000)
    )
    payload = {
        "messages": [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": request_context},
            {"role": "user", "content": "hi"},
        ],
        "tools": [{"type": "function", "function": {"name": "noop", "description": "x"}}],
    }

    compacted, proof = prove_or_compact_provider_payload(
        payload,
        projection_adapter="openrouter",
        proof_budget=12_000,
        status_projection_mode="content_envelope",
    )

    assert proof is not None
    assert proof["fits"] is True
    assert proof["emergency_current_turn_compacted"] is True
    assert proof["recent_tail_too_large"] is False
    assert compacted["messages"][1]["content"] != request_context
    assert compacted["messages"][2]["content"] == "hi"


def test_provider_request_proof_emergency_compacts_old_user_tail_but_keeps_latest_user() -> None:
    old_user_message = "old channel transcript\n" + ("previous user request\n" * 4000)
    payload = {
        "messages": [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": old_user_message},
            {"role": "assistant", "content": "previous answer"},
            {"role": "user", "content": "hi"},
        ]
    }

    compacted, proof = prove_or_compact_provider_payload(
        payload,
        projection_adapter="openrouter",
        proof_budget=12_000,
        status_projection_mode="content_envelope",
    )

    assert proof is not None
    assert proof["fits"] is True
    assert proof["emergency_current_turn_compacted"] is True
    assert compacted["messages"][1]["content"] != old_user_message
    assert compacted["messages"][3]["content"] == "hi"


def test_active_user_anchor_wins_over_later_synthetic_user_message() -> None:
    active_prompt = "ACTIVE REQUEST " + ("u" * 2000)
    synthetic_reminder = (
        "[Current user request reminder]\n"
        "This is the active user request for this same turn, not a new request.\n"
        + ("r" * 2000)
    )
    payload = {
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": active_prompt},
            {"role": "assistant", "content": "working"},
            {"role": "user", "content": synthetic_reminder},
        ]
    }

    compacted = _final_hard_cap_payload_once(
        payload,
        active_user_message_index=1,
    )

    assert compacted["messages"][1]["content"] == active_prompt
    assert compacted["messages"][3]["content"] != synthetic_reminder


def test_active_user_inference_ignores_ensemble_aggregator_bundle() -> None:
    active_prompt = "ACTIVE REQUEST " + ("u" * 2000)
    aggregator_bundle = (
        "You are the aggregator in a multi-model B5 fusion experiment.\n"
        + ("candidate bundle\n" * 300)
    )
    payload = {
        "messages": [
            {"role": "user", "content": active_prompt},
            {"role": "user", "content": aggregator_bundle},
        ]
    }

    compacted = _final_hard_cap_payload_once(payload)

    assert compacted["messages"][0]["content"] == active_prompt
    assert compacted["messages"][1]["content"] != aggregator_bundle


def test_provider_proof_reports_explicit_active_user_anchor() -> None:
    proof = prove_provider_payload(
        {"messages": [{"role": "user", "content": "active"}]},
        projection_adapter="openai",
        proof_budget=10_000,
        active_user_message_index=0,
    )

    assert proof["active_user_message_index"] == 0
    assert proof["active_user_anchor_source"] == "explicit"


def test_provider_request_proof_rejects_instead_of_rewriting_oversized_latest_user() -> None:
    huge_current_message = "please answer the LONG_CURRENT_INPUT marker\n" + ("x" * 500_000)
    payload = {
        "messages": [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": huge_current_message},
        ]
    }

    with pytest.raises(ProviderRequestBudgetExceeded) as exc_info:
        prove_or_compact_provider_payload(
            payload,
            projection_adapter="openrouter",
            proof_budget=12_000,
            status_projection_mode="content_envelope",
        )

    proof = exc_info.value.proof
    assert proof["fits"] is False
    assert proof["final_hard_cap_compacted"] is True
    assert proof["recent_tail_too_large"] is True
    assert proof["top_contributors"][0]["chars"] == len(huge_current_message)


def test_provider_request_proof_does_not_exclude_nested_tool_argument_images() -> None:
    nested_image = "x" * 5000
    payload = {
        "messages": [
            {
                "role": "assistant",
                "content": "calling tool",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "inspect",
                            "arguments": {"images": [nested_image]},
                        },
                    }
                ],
            }
        ]
    }

    with pytest.raises(ProviderRequestBudgetExceeded) as exc_info:
        prove_provider_payload(
            payload,
            projection_adapter="ollama",
            proof_budget=1000,
        )

    proof = exc_info.value.proof
    assert proof["estimated_chars"] > len(nested_image)
    assert "media_blocks_excluded" not in proof
    assert "media_blocks_reserved" not in proof


def test_provider_request_proof_final_hard_cap_preserves_critical_tool_result() -> None:
    critical_tool_result = json.dumps(
        {
            "execution_status": {"status": "error", "reason": "runtime_error"},
            "output": "BOUNDARY_FAILURE_DETAIL " + ("e" * 1800),
        },
        ensure_ascii=False,
    )
    payload = {
        "messages": [
            {"role": "user", "content": "old context\n" + ("u" * 8000)},
            {"role": "assistant", "content": "old answer\n" + ("a" * 8000)},
            {"role": "user", "content": "run the failing tool"},
            {
                "role": "assistant",
                "content": "calling tool",
                "tool_calls": [
                    {
                        "id": "call-critical",
                        "type": "function",
                        "function": {
                            "name": "exec_command",
                            "arguments": json.dumps(
                                {"cmd": "x" * 5000},
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-critical",
                "content": critical_tool_result,
            },
        ]
    }

    compacted, proof = prove_or_compact_provider_payload(
        payload,
        projection_adapter="openrouter",
        proof_budget=2_200,
        status_projection_mode="content_envelope",
    )

    assert proof is not None
    assert proof["fits"] is True
    assert proof["final_hard_cap_compacted"] is True
    tool_content = compacted["messages"][4]["content"]
    assert "BOUNDARY_FAILURE_DETAIL" in tool_content
    assert "[opensquilla_compacted:tool_result" not in tool_content
