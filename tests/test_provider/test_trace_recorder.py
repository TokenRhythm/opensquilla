from __future__ import annotations

import json
from pathlib import Path

from opensquilla.provider.trace_recorder import LLMTraceRecorder


def test_llm_trace_recorder_redacts_secret_values_in_strings(
    monkeypatch, tmp_path: Path
) -> None:
    trace_path = tmp_path / "llm_calls.jsonl"
    secret = "sk-or-v1-abcdefghijklmnopqrstuvwxyz"
    monkeypatch.setenv("OPENSQUILLA_LLM_TRACE_RECORDER", "full")
    monkeypatch.setenv("OPENSQUILLA_LLM_TRACE_PATH", str(trace_path))

    recorder = LLMTraceRecorder(
        provider="dashscope",
        model="qwen3.6-flash",
        base_url="https://example.invalid",
        endpoint="/chat/completions",
        stream=True,
    )
    recorder.record_request(
        payload={
            "messages": [
                {"role": "tool", "content": f"env.OPENROUTER_API_KEY={secret}"}
            ]
        },
        headers={"Authorization": f"Bearer {secret}"},
    )
    recorder.record_response(
        assistant_text=f"debug DASHSCOPE_API_KEY={secret}",
        response={"choices": [{"message": {"content": f"token={secret}"}}]},
    )
    recorder.record_response_headers(response_ids=[f"gen-{secret}"])
    recorder.record_error(
        code="bad",
        message=f"failed with {secret} and RAW_UPSTREAM_DETAIL",
        response_body=f"OPENROUTER_API_KEY={secret}; RAW_UPSTREAM_BODY",
    )

    text = trace_path.read_text(encoding="utf-8")
    assert secret not in text
    rows = [json.loads(line) for line in text.splitlines()]
    assert rows[0]["payload"]["messages"][0]["content"] == (
        "env.OPENROUTER_API_KEY=[REDACTED]"
    )
    assert rows[0]["headers"]["Authorization"] == "[REDACTED]"
    assert rows[1]["assistant_text"] == "debug DASHSCOPE_API_KEY=[REDACTED]"
    assert secret not in json.dumps(rows[2], sort_keys=True)
    assert rows[3]["message"] == "Provider request failed"
    assert rows[3]["code"] == "provider_error"
    assert rows[3]["code_chars"] == len("bad")
    assert rows[3]["message_chars"] == len(
        f"failed with {secret} and RAW_UPSTREAM_DETAIL"
    )
    assert rows[3]["response_body"] is None
    assert rows[3]["response_body_chars"] == len(
        f"OPENROUTER_API_KEY={secret}; RAW_UPSTREAM_BODY"
    )
    assert "RAW_UPSTREAM_DETAIL" not in text
    assert "RAW_UPSTREAM_BODY" not in text
