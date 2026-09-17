from __future__ import annotations

import http.client
import json
import time
from urllib.parse import urlsplit

import pytest

from scripts.long_task_fault_proxy import DeterministicFaultProxy, FaultScenario


def _request(
    proxy: DeterministicFaultProxy,
    *,
    scenario: FaultScenario | None = None,
) -> tuple[http.client.HTTPResponse, http.client.HTTPConnection]:
    parsed = urlsplit(proxy.base_url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    body = json.dumps(
        {
            "model": "synthetic/model",
            "stream": True,
            "messages": [{"role": "user", "content": "private synthetic prompt"}],
        }
    )
    headers = {"Content-Type": "application/json", "Authorization": "Bearer do-not-retain"}
    if scenario is not None:
        headers["X-OpenSquilla-Fault-Scenario"] = scenario.value
    connection.request("POST", "/v1/chat/completions", body=body, headers=headers)
    return connection.getresponse(), connection


def test_rate_limit_has_exact_retry_after_without_retaining_sensitive_input() -> None:
    with DeterministicFaultProxy() as proxy:
        response, connection = _request(proxy, scenario=FaultScenario.RATE_LIMITED)
        try:
            payload = json.loads(response.read())
        finally:
            connection.close()

        assert response.status == 429
        assert response.getheader("Retry-After") == "8"
        assert payload["error"]["type"] == "rate_limit"
        assert len(proxy.records) == 1
        record = proxy.records[0]
        assert record.scenario == FaultScenario.RATE_LIMITED.value
        assert record.model == "synthetic/model"
        assert "prompt" not in vars(record)
        assert "authorization" not in vars(record)


def test_overloaded_returns_deterministic_503() -> None:
    with DeterministicFaultProxy([FaultScenario.OVERLOADED]) as proxy:
        response, connection = _request(proxy)
        try:
            assert response.status == 503
            assert json.loads(response.read())["error"]["type"] == "overloaded"
        finally:
            connection.close()


@pytest.mark.parametrize(
    "scenario,expected_partial",
    [
        (FaultScenario.RESET_BEFORE_FIRST_TOKEN, b""),
        (FaultScenario.PARTIAL_THEN_RESET, b"synthetic partial"),
    ],
)
def test_reset_scenarios_end_with_an_incomplete_chunked_stream(
    scenario: FaultScenario,
    expected_partial: bytes,
) -> None:
    with DeterministicFaultProxy() as proxy:
        response, connection = _request(proxy, scenario=scenario)
        try:
            assert response.status == 200
            with pytest.raises(http.client.IncompleteRead) as raised:
                response.read()
            assert expected_partial in raised.value.partial
        finally:
            connection.close()


def test_reasoning_only_never_emits_visible_content() -> None:
    with DeterministicFaultProxy([FaultScenario.REASONING_ONLY]) as proxy:
        response, connection = _request(proxy)
        try:
            body = response.read().decode("utf-8")
        finally:
            connection.close()

        assert response.status == 200
        assert "reasoning_content" in body
        assert '"content"' not in body
        assert '"finish_reason":"stop"' in body
        assert "data: [DONE]" in body


def test_late_terminal_delays_only_the_terminal_event() -> None:
    delay = 0.08
    with DeterministicFaultProxy(
        [FaultScenario.LATE_TERMINAL],
        late_terminal_delay_seconds=delay,
    ) as proxy:
        started = time.monotonic()
        response, connection = _request(proxy)
        try:
            body = response.read().decode("utf-8")
        finally:
            connection.close()
        elapsed = time.monotonic() - started

        assert "synthetic complete" in body
        assert '"finish_reason":"stop"' in body
        assert elapsed >= delay * 0.8


def test_server_side_sequence_reuses_last_scenario_after_sequence_end() -> None:
    with DeterministicFaultProxy([FaultScenario.RATE_LIMITED, FaultScenario.OK]) as proxy:
        statuses: list[int] = []
        for _ in range(3):
            response, connection = _request(proxy)
            try:
                statuses.append(response.status)
                response.read()
            finally:
                connection.close()

        assert statuses == [429, 200, 200]
        assert [record.scenario for record in proxy.records] == [
            FaultScenario.RATE_LIMITED.value,
            FaultScenario.OK.value,
            FaultScenario.OK.value,
        ]


def test_long_completion_is_synthetic_bounded_and_incrementally_chunked() -> None:
    marker = "OSQ_0123456789ABCDEF"
    with DeterministicFaultProxy(
        completion_text=marker,
        completion_bytes=16 * 1024,
        completion_chunk_bytes=256,
    ) as proxy:
        response, connection = _request(proxy)
        try:
            body = response.read().decode("utf-8")
        finally:
            connection.close()

    chunks = [
        json.loads(line.removeprefix("data: "))["choices"][0]["delta"].get("content", "")
        for line in body.splitlines()
        if line.startswith("data: {")
    ]
    completion = "".join(chunks)
    assert response.status == 200
    assert body.count('"content"') >= 64
    assert len(completion.encode()) == 16 * 1024
    assert completion.count(marker) == 2
    assert "Synthetic Markdown Fixture" in completion


def test_ok_stream_can_emit_reasoning_before_visible_completion() -> None:
    with DeterministicFaultProxy(reasoning_before_completion=True) as proxy:
        response, connection = _request(proxy)
        try:
            body = response.read().decode("utf-8")
        finally:
            connection.close()

    assert body.index("reasoning_content") < body.index('"content"')
    assert "synthetic complete" in body
