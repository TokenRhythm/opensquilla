from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from scripts import live_tokenrhythm_budget as module
from scripts.live_tokenrhythm_transport import (
    BudgetedTransport,
    RelayTarget,
    TransportRejectedError,
)


def _log(tmp_path, *, enabled=True):
    log = module.FunctionalRequestLog(tmp_path / "functional.sqlite", enabled=enabled)
    log.select_phase(variant="baseline", case_id="synthetic-landing-r1")
    return log


def _body(model="unpriced-model"):
    return json.dumps({
        "model": model, "messages": [{"role": "user", "content": "synthetic-private-prompt"}],
        "stream": True,
    }).encode()


def _relay(log, handler):
    return module.BudgetRelay(
        None, {}, request_log=log, api_key="synthetic-relay-only-secret",
        transport=httpx.MockTransport(handler),
    )


def test_functional_is_disabled_before_any_physical_dispatch(tmp_path):
    log = _log(tmp_path, enabled=False)
    received = []
    relay = _relay(log, lambda request: received.append(request))
    try:
        with pytest.raises(module.BudgetRejectedError, match="live_requests_disabled"):
            with relay.forward(_body()):
                pytest.fail("unexpected upstream response")
    finally:
        relay.close()
    assert received == []
    assert log.snapshot()["requests"] == []


def test_functional_rejects_budget_file_without_changing_any_bytes(tmp_path):
    path = tmp_path / "historical.sqlite"
    ledger = module.BudgetLedger(path)
    ledger.select_phase(bucket="regular", variant="baseline", case_id="old-attempt")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(module.BudgetRejectedError, match="not_a_functional_request_log"):
        module.FunctionalRequestLog(path, enabled=True)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_functional_retry_and_fallback_preserve_actual_http_without_fee_gates(
    tmp_path, monkeypatch,
):
    def forbidden(*args, **kwargs):
        pytest.fail("functional forwarding entered budget accounting")

    monkeypatch.setattr(module.ModelCostBound, "reserve_nanos", forbidden)
    monkeypatch.setattr(module.BudgetLedger, "__init__", forbidden)
    monkeypatch.setattr(module.BillingObservation, "__init__", forbidden)
    monkeypatch.setattr(module, "reviewed_catalog_bounds", forbidden)
    log = _log(tmp_path)
    received = []
    replies = [
        (503, b'{"error":{"message":"synthetic-private-upstream-detail"}}'),
        (503, b'{"error":{"message":"synthetic-private-upstream-detail"}}'),
        (200, b'data: {"choices": [{"delta":{"content":"synthetic-private-answer"}}]}\n\n'),
    ]

    def upstream(request):
        received.append(request)
        assert log.snapshot()["pendingRequests"] == 1
        status, body = replies[len(received) - 1]
        return httpx.Response(status, content=body, headers={
            "Content-Type": "text/event-stream" if status == 200 else "application/json",
            "X-Request-ID": f"synthetic-response-{len(received)}",
        })

    relay = _relay(log, upstream)
    try:
        target = RelayTarget(relay.start(), relay.client_key)
        with httpx.Client(
            transport=BudgetedTransport(target), trust_env=False, timeout=5
        ) as client:
            bodies = [_body(), _body(), _body("unpriced-fallback-model")]
            # The caller initiates each physical retry/fallback. The relay does not.
            for index, body in enumerate(bodies):
                response = client.post(
                    "https://tokenrhythm.studio/v1/chat/completions", content=body,
                    headers={"Authorization": "Bearer " + relay.client_key,
                             "X-Request-ID": f"synthetic-client-{index}"},
                )
                assert response.status_code == replies[index][0]
                assert response.content == replies[index][1]
                assert str(response.request.url) == (
                    "https://tokenrhythm.studio/v1/chat/completions"
                )
                assert len(received) == index + 1
        assert [request.content for request in received] == bodies
        assert all(request.headers["host"] == "tokenrhythm.studio" for request in received)
        assert all(request.headers["authorization"] == (
            "Bearer synthetic-relay-only-secret"
        ) for request in received)
        rows = log.snapshot()["requests"]
        assert len({row["id"] for row in rows}) == 3
        assert [row["model"] for row in rows] == [
            "unpriced-model", "unpriced-model", "unpriced-fallback-model",
        ]
        assert [row["http_status"] for row in rows] == [503, 503, 200]
        assert all(row["status"] == "completed" and row["reason"] == "http_eof" for row in rows)
        assert all(row["started"] <= row["response_headers_at"] <= row["first_chunk_at"]
                   <= row["first_event_at"] <= row["ended"] for row in rows)
        assert [row["response_bytes"] for row in rows] == [len(body) for _, body in replies]
        evidence = json.dumps(log.snapshot()) + log.path.read_bytes().decode("latin1")
        for secret in ("synthetic-private-prompt", "synthetic-private-upstream-detail",
                       "synthetic-private-answer", "synthetic-relay-only-secret", relay.client_key):
            assert secret not in evidence
    finally:
        relay.close()


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
def test_functional_transport_failure_is_recorded_and_does_not_retry(tmp_path, failure):
    log = _log(tmp_path)
    received = []

    def upstream(request):
        received.append(request)
        raise failure("synthetic-private-error", request=request)

    relay = _relay(log, upstream)
    try:
        with pytest.raises(failure):
            with relay.forward(_body()):
                pytest.fail("failed request yielded a response")
    finally:
        relay.close()
    assert len(received) == 1
    row = log.snapshot()["requests"][0]
    assert row["status"] == "interrupted"
    assert row["reason"] == (
        "upstream_timeout" if failure is httpx.ReadTimeout else "transport_error"
    )
    assert row["http_status"] is None and row["ended"] is not None
    assert "synthetic-private-error" not in json.dumps(log.snapshot())


def test_functional_unconsumed_stream_and_shutdown_keep_distinct_outcomes(tmp_path):
    log = _log(tmp_path)
    relay = _relay(log, lambda request: httpx.Response(200, content=b'data: {}\n\n'))
    with relay.forward(_body()) as response:
        assert response.status_code == 200
    assert log.snapshot()["requests"][0]["reason"] == "stream_not_exhausted"
    with relay.forward(_body()) as response:
        assert next(response.chunks) == b'data: {}\n\n'
        assert log.snapshot()["pendingRequests"] == 1
        relay.close()
    assert log.snapshot()["requests"][1]["reason"] == "relay_shutdown"
    assert log.snapshot()["requests"][1]["response_bytes"] == len(b'data: {}\n\n')
    assert log.snapshot()["pendingRequests"] == 0
    with pytest.raises(module.BudgetRejectedError, match="relay_closed"):
        with relay.forward(_body()):
            pytest.fail("closed relay sent another request")


def test_functional_restart_retains_inflight_and_concurrent_physical_identity(tmp_path):
    log = _log(tmp_path)
    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(
            lambda _: log.start_request(model="same-model", request_bytes=7), range(8)
        ))
    assert len(set(ids)) == 8
    reopened = module.FunctionalRequestLog(log.path, enabled=True)
    assert reopened.snapshot()["pendingRequests"] == 8
    with pytest.raises(module.BudgetRejectedError, match="requests_still_in_flight"):
        reopened.select_phase(variant="new", case_id="new-case")
    for request_id in ids:
        reopened.finish_request(request_id, completed=True, reason="http_eof", response_bytes=4)
    reopened.select_phase(variant="new", case_id="new-case")
    assert reopened.snapshot()["pendingRequests"] == 0
    assert {row["case_id"] for row in reopened.snapshot()["requests"]} == {"synthetic-landing-r1"}


def test_functional_loopback_auth_host_and_redirect_boundaries_are_preserved(tmp_path):
    log = _log(tmp_path)
    received = []

    def upstream(request):
        received.append(request)
        return httpx.Response(307, headers={"Location": "https://unreviewed.invalid/steal"})

    relay = _relay(log, upstream)
    try:
        url = relay.start()
        with httpx.Client(trust_env=False, timeout=5) as client:
            assert client.post(url + "/chat/completions", content=_body()).status_code == 403
            assert client.post(url + "/other", content=_body()).status_code == 404
        assert received == []
        with httpx.Client(transport=BudgetedTransport(RelayTarget(url, relay.client_key)),
                          trust_env=False, timeout=5, follow_redirects=True) as client:
            with pytest.raises(TransportRejectedError):
                client.post("https://unreviewed.invalid/v1/chat/completions", content=_body(),
                            headers={"Authorization": "Bearer " + relay.client_key})
            with pytest.raises(TransportRejectedError):
                client.post("https://tokenrhythm.studio/v1/chat/completions", content=_body(),
                            headers={"Authorization": "Bearer " + relay.client_key})
        assert len(received) == 1
        assert log.snapshot()["requests"][0]["http_status"] == 307
    finally:
        relay.close()


def test_functional_cli_requires_explicit_enable_and_excludes_legacy_ledger(tmp_path, capsys):
    ready = tmp_path / "ready.json"
    assert module.main(["--mode", "functional", "--ready-file", str(ready)]) == 0
    assert json.loads(ready.read_text()) == {"enabled": False, "mode": "functional"}
    with pytest.raises(SystemExit) as caught:
        module.main(["--enable-live", "--mode", "functional", "--ledger", "old.sqlite"])
    assert caught.value.code == 2
    assert "does not accept budget" in capsys.readouterr().err


def test_functional_cli_ready_and_receipt_use_only_the_independent_log(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENRHYTHM_API_KEY", "synthetic-isolated-secret")
    monkeypatch.setattr(module.BudgetRelay, "start", lambda self: "http://127.0.0.1:12345/v1")
    # Exercise normal SIGTERM cleanup without changing this pytest process's handlers.
    monkeypatch.setattr(module.signal, "signal", lambda signum, callback: callback(signum, None))
    ready, receipt, request_log = [tmp_path / name for name in (
        "ready.json", "receipt.json", "requests.sqlite",
    )]
    assert module.main([
        "--enable-live", "--mode", "functional", "--request-log", str(request_log),
        "--ready-file", str(ready), "--receipt-file", str(receipt),
        "--variant", "baseline", "--case-id", "synthetic-once",
    ]) == 0
    value = json.loads(ready.read_text())
    assert value["mode"] == "functional"
    assert value["request_log"] == str(request_log.resolve())
    assert value["client_key"].startswith("live-budget-placeholder-")
    summary = json.loads(receipt.read_text())
    assert summary["phase"] == {"variant": "baseline", "case_id": "synthetic-once"}
    assert summary["requests"] == [] and summary["pendingRequests"] == 0
    assert "synthetic-isolated-secret" not in ready.read_text() + receipt.read_text()
