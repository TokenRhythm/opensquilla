import json

from opensquilla.observability.trace_lookup import find_turn_traces


def test_turn_trace_lookup_preserves_attempts_and_requires_exact_identity(tmp_path) -> None:
    records = [
        {"trace_id": "attempt-a", "kind": "turn_start", "ts": "2026-01-01T00:00:01Z"},
        {"trace_id": "attempt-a", "kind": "turn_error", "ts": "2026-01-01T00:00:02Z"},
        {"trace_id": "attempt-b", "kind": "turn_start", "ts": "2026-01-01T00:00:03Z"},
        {"trace_id": "other-session", "kind": "turn_start", "session_key": "other"},
        {"trace_id": "other-turn", "kind": "turn_start", "turn_id": "turn-other"},
    ]
    (tmp_path / "traces-20260101.jsonl").write_text(
        "\n".join(
            json.dumps({"session_key": "session-test", "turn_id": "turn-test", **record})
            for record in records
        ) + '\n{"trace_id":',
        encoding="utf-8",
    )

    traces = find_turn_traces("session-test", "turn-test", trace_dir=tmp_path)

    assert [trace["trace_id"] for trace in traces] == ["attempt-a", "attempt-b"]
    assert traces[0]["complete"] is True
    assert traces[0]["status"] == "error"
    assert traces[1]["complete"] is False
    assert traces[1]["status"] == "running"
    assert traces[1]["raw_available"] is False


def test_turn_trace_lookup_only_reads_raw_directory_when_authorized(tmp_path) -> None:
    record = {
        "trace_id": "raw-attempt",
        "session_key": "session-test",
        "turn_id": "turn-test",
        "kind": "turn_end",
        "ts": "2026-01-01T00:00:00Z",
        "payload": {"messages": ["synthetic-content"]},
    }
    (tmp_path / "turn-calls-20260101.jsonl").write_text(json.dumps(record), encoding="utf-8")

    assert find_turn_traces("session-test", "turn-test", trace_dir=tmp_path) == []
    [trace] = find_turn_traces(
        "session-test", "turn-test", trace_dir=tmp_path, raw_dir=tmp_path
    )
    assert trace["raw_available"] is True
    assert trace["complete"] is True
    assert trace["status"] == "success"
    assert "synthetic-content" not in json.dumps(trace)
