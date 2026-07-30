from __future__ import annotations

import json

from opensquilla.provider.trace_recorder import LLMTraceRecorder


def _jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_llm_trace_recorder_writes_full_payload_and_redacts_headers(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "llm_calls.jsonl"
    monkeypatch.setenv("OPENSQUILLA_LLM_TRACE_RECORDER", "full")
    monkeypatch.setenv("OPENSQUILLA_LLM_TRACE_PATH", str(path))

    recorder = LLMTraceRecorder(
        provider="dashscope",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        stream=True,
    )
    recorder.record_request(
        payload={"model": "qwen3.6-flash", "messages": [{"role": "user", "content": "hi"}]},
        headers={
            "Authorization": "Bearer secret",
            "Content-Type": "application/json",
            "X-OpenSquilla-Session-Id": "session-1",
            "X-OpenSquilla-Turn-Id": "turn-1",
            "X-OpenSquilla-Execution-Id": "execution-1",
            "X-OpenSquilla-Call-Kind": "agent.chat",
        },
    )
    recorder.record_chunk({"id": "chatcmpl-1", "choices": [{"delta": {"content": "ok"}}]})
    recorder.record_response(
        usage={"input_tokens": 3, "cached_tokens": 2},
        stop_reason="stop",
        actual_model="qwen3.6-flash",
        assistant_text="ok",
        response_ids=["chatcmpl-1"],
    )

    rows = _jsonl(path)
    assert [row["event"] for row in rows] == [
        "llm.request",
        "llm.response_chunk",
        "llm.response",
    ]
    assert rows[0]["payload"]["messages"][0]["content"] == "hi"
    assert rows[0]["headers"]["Authorization"] == "[REDACTED]"
    assert rows[0]["headers"]["X-OpenSquilla-Session-Id"] == "[PRESENT]"
    assert rows[0]["headers"]["X-OpenSquilla-Turn-Id"] == "[PRESENT]"
    assert rows[0]["headers"]["X-OpenSquilla-Execution-Id"] == "[PRESENT]"
    assert rows[0]["headers"]["X-OpenSquilla-Call-Kind"] == "[PRESENT]"
    serialized = json.dumps(rows[0]["headers"], sort_keys=True)
    assert "session-1" not in serialized
    assert "turn-1" not in serialized
    assert "execution-1" not in serialized
    assert "agent.chat" not in serialized
    assert rows[2]["usage"]["cached_tokens"] == 2


def test_llm_trace_recorder_off_does_not_write(tmp_path, monkeypatch) -> None:
    path = tmp_path / "llm_calls.jsonl"
    monkeypatch.setenv("OPENSQUILLA_LLM_TRACE_RECORDER", "off")
    monkeypatch.setenv("OPENSQUILLA_LLM_TRACE_PATH", str(path))

    recorder = LLMTraceRecorder(
        provider="openrouter",
        model="z-ai/glm-5.1",
        base_url="https://openrouter.ai/api/v1",
        endpoint="https://openrouter.ai/api/v1/chat/completions",
        stream=True,
    )
    recorder.record_request(payload={"model": "z-ai/glm-5.1"})

    assert not path.exists()
