import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from opensquilla.engine.routing import jev_benchmark_worker as worker
from opensquilla.engine.routing.fixed_four_tier_v2 import FIXED_FOUR_TIER_DEPLOYMENT_SPECS, FixedFourTierDecision
from opensquilla.engine.routing.jev_model import JEV_TIER_SELECTION_MODE, JevModelClassifier


def response():
    return {"model": "typesafe-ai/jev", "answers": {
        "intent": {"type": "choice", "choice": "new_task", "confidence": 1,
                   "probabilities": {"continue": 0, "redo": 0, "new_task": 1}},
        "tier": {"type": "choice", "choice": "c2", "confidence": 1,
                 "probabilities": {"c0": 0, "c1": 0, "c2": 1, "c3": 0}}},
        "usage": {"inputTokens": 42, "outputTokens": 12},
        "providerMetadata": {"gateway": {"cost": "0", "gatewayCost": "0"}}}


def prepare(tmp_path):
    inp = {"current_request": "Write and test a small CSV processor.", "task_anchor": "", "history_user": [],
           "previous_answer": "", "previous_usage": {}, "previous_outcome": "unknown",
           "active_route_tier": None, "route_history": [], "context": {}, "tool_state": {}, "attachments": []}
    input_path = tmp_path / "input.jsonl"
    input_path.write_bytes(worker.encode({"item_id": "case-1", "input": inp}) + b"\n")
    pool = {t.upper(): {"model_id": m, "revision": rev, "definition_hash": "sha256:" + "a" * 64}
            for t, _, m, _, rev in FIXED_FOUR_TIER_DEPLOYMENT_SPECS}
    pool_path = tmp_path / "pool.json"
    worker.write_once(pool_path, pool)
    policy = {"default_new_task_tier": "c1", "intent_min_confidence": .5, "tier_min_confidence": .5, "min_margin": .05,
              "tier_selection_mode": JEV_TIER_SELECTION_MODE}
    policy_path = tmp_path / "policy.json"
    worker.write_once(policy_path, policy)
    request = {"schema_version": "jev-route-only-request.v1", "campaign_id": "example", "expected_count": 1,
               "input_path": str(input_path), "input_sha256": worker.digest(input_path.read_bytes()),
               "model_pool_path": str(pool_path), "model_pool_sha256": worker.digest(pool_path.read_bytes()),
               "policy_path": str(policy_path), "policy_sha256": worker.digest(policy_path.read_bytes()),
               "output_dir": str(tmp_path / "native"), "concurrency": 1}
    path = tmp_path / "request.json"
    worker.write_once(path, request)
    return path


def install_client(monkeypatch, handler):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-sensitive-key")
    def factory(**kwargs):
        kwargs["client"] = kwargs.get("client") or httpx.Client(transport=httpx.MockTransport(handler))
        return JevModelClassifier(**kwargs)
    factory.validate_response = JevModelClassifier.validate_response
    monkeypatch.setattr(worker, "JevModelClassifier", factory)


def test_worker_joint_heads_policy_gating_and_resume(tmp_path, monkeypatch):
    path = prepare(tmp_path)
    calls = []
    def handler(req):
        payload = json.loads(req.content)
        assert "item_id" not in payload["state"]
        assert "benchmark" not in str(payload)
        assert set(payload["questions"]) == {"intent", "tier"}
        calls.append(payload)
        return httpx.Response(200, json=response())
    install_client(monkeypatch, handler)
    evidence = worker.run(path)
    assert len(calls) == 1
    assert evidence["api_usage"]["cost_usd"] == 0
    assert evidence["api_usage"]["total_tokens"] == 54
    assert evidence["api_usage"]["intent_policy_consumed_count"] == 0
    row = json.loads((tmp_path / "native/decisions.jsonl").read_text())
    assert row["tier"] == "C2"
    assert row["trace"]["intent"]["run_status"] == "not_run"
    assert row["trace"]["tier"]["run_status"] == "ran"
    assert FixedFourTierDecision.from_trace(row["trace"]).feature_input_audit.input_contract == "canonical_router_input"
    assert worker.run(path) == evidence
    assert len(calls) == 1
    for artifact in (tmp_path / "native").rglob("*.json"):
        assert "test-sensitive-key" not in artifact.read_text()


def test_wrong_input_hash_no_call(tmp_path, monkeypatch):
    path = prepare(tmp_path)
    (tmp_path / "input.jsonl").write_text("changed\n")
    install_client(monkeypatch, lambda _: pytest.fail("must not call"))
    with pytest.raises(ValueError, match="hash"):
        worker.run(path)


def test_legacy_policy_cannot_silently_run_new_argmax(tmp_path, monkeypatch):
    path = prepare(tmp_path)
    request = json.loads(path.read_bytes())
    policy_path = tmp_path / "legacy-policy.json"
    policy = json.loads(Path(request["policy_path"]).read_bytes())
    policy.pop("tier_selection_mode")
    worker.write_once(policy_path, policy)
    request.update(policy_path=str(policy_path), policy_sha256=worker.digest(policy_path.read_bytes()))
    legacy_request = tmp_path / "legacy-request.json"
    worker.write_once(legacy_request, request)
    install_client(monkeypatch, lambda _: pytest.fail("legacy policy must not call HTTP"))
    with pytest.raises(ValueError, match="explicitly frozen"):
        worker.run(legacy_request)


def test_legacy_completed_decision_cannot_be_relabelled_as_argmax(tmp_path, monkeypatch):
    path = prepare(tmp_path)
    install_client(monkeypatch, lambda _: httpx.Response(200, json=response()))
    worker.run(path)
    completed = next((tmp_path / "native/items").glob("*/completed.json"))
    value = json.loads(completed.read_bytes())
    value["trace"]["classifier_identity"].pop("tier_selection_mode")
    completed.write_bytes(worker.encode(value))
    install_client(monkeypatch, lambda _: pytest.fail("must not call HTTP for old completed output"))
    with pytest.raises(ValueError, match="historical decisions"):
        worker.run(path)


def test_invalid_response_no_default_fallback_success(tmp_path, monkeypatch):
    path = prepare(tmp_path)
    install_client(monkeypatch, lambda _: httpx.Response(200, json={"not": "valid"}))
    with pytest.raises(Exception):
        worker.run(path)
    assert not (tmp_path / "native/evidence.json").exists()
    assert list((tmp_path / "native/items").glob("*/event-001.json"))


def test_unlisted_transport_failure_not_retried(tmp_path, monkeypatch):
    path = prepare(tmp_path)
    calls = []
    def handler(req):
        calls.append(req)
        raise httpx.LocalProtocolError("sensitive transport error test-sensitive-key")
    install_client(monkeypatch, handler)
    with pytest.raises(Exception):
        worker.run(path)
    with pytest.raises(RuntimeError, match="in_doubt"):
        worker.run(path)
    assert len(calls) == 1
    for artifact in (tmp_path / "native").rglob("*.json"):
        assert "test-sensitive-key" not in artifact.read_text()


def test_low_confidence_selects_argmax_without_c1_fallback(tmp_path, monkeypatch):
    path = prepare(tmp_path)
    body = response()
    body["answers"]["tier"].update(confidence=.4, probabilities={"c0": .1, "c1": .25, "c2": .4, "c3": .25})
    install_client(monkeypatch, lambda _: httpx.Response(200, json=body))
    worker.run(path)
    row = json.loads((tmp_path / "native/decisions.jsonl").read_text())
    assert row["tier"] == "C2"
    assert row["trace"]["tier"]["source"] == "classifier"
    assert row["trace"]["tier"]["reason"] == "classifier_argmax_selected"


def test_valid_probability_tie_uses_fixed_order_argmax_without_fallback(tmp_path, monkeypatch):
    path = prepare(tmp_path)
    body = response()
    body["answers"]["tier"].update(confidence=.2, probabilities={"c0": 0, "c1": .5, "c2": .5, "c3": 0})
    install_client(monkeypatch, lambda _: httpx.Response(200, json=body))
    evidence = worker.run(path)
    row = json.loads((tmp_path / "native/decisions.jsonl").read_text())
    assert row["tier"] == "C1"
    assert row["trace"]["tier"]["source"] == "classifier"
    assert row["valid_response_tie_policy_fallback"] is False
    assert row["valid_response_non_argmax_policy_fallback"] is False
    assert evidence["valid_response_tie_policy_fallback_count"] == 0
    assert evidence["valid_response_non_argmax_policy_fallback_count"] == 0


@pytest.mark.parametrize("vendor_confidence, probabilities", [
    (.32, {"c0": .01, "c1": .02, "c2": .49, "c3": .48}),
    (.99, {"c0": 0, "c1": 0, "c2": .9, "c3": .1}),
    (.8, {"c0": 0, "c1": .45, "c2": .45, "c3": .1}),
])
def test_non_argmax_choice_uses_probability_argmax_without_retry(
    tmp_path, monkeypatch, vendor_confidence, probabilities
):
    path = prepare(tmp_path)
    body = response()
    body["answers"]["tier"].update(choice="c3", confidence=vendor_confidence, probabilities=probabilities)
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=body)
    install_client(monkeypatch, handler)
    evidence = worker.run(path)
    result = json.loads((tmp_path / "native/decisions.jsonl").read_text())
    assert result["tier"] == max(("c0", "c1", "c2", "c3"), key=probabilities.get).upper()
    assert result["trace"]["tier"]["run_status"] == "ran"
    assert result["trace"]["tier"]["reason"] == "classifier_argmax_selected"
    assert result["trace"]["tier"]["source"] == "classifier"
    assert result["joint_tier_prediction"] == body["answers"]["tier"]
    assert result["valid_response_non_argmax_policy_fallback"] is False
    assert result["valid_response_tie_policy_fallback"] is False
    assert evidence["valid_response_non_argmax_policy_fallback_count"] == 0
    assert evidence["valid_response_tie_policy_fallback_count"] == 0
    assert evidence["api_usage"]["request_count"] == 1
    assert evidence["api_usage"]["retry_count"] == 0
    assert len(calls) == 1
    assert worker.run(path) == evidence
    assert len(calls) == 1


def test_saved_non_argmax_200_receipt_resumes_without_network(tmp_path, monkeypatch):
    path = prepare(tmp_path)
    body = response()
    body["answers"]["tier"].update(choice="c3", confidence=.32,
        probabilities={"c0": .01, "c1": .02, "c2": .49, "c3": .48})
    original_write = worker.write_once
    def interrupt_after_receipt(path, value):
        if path.name == "api.json":
            raise RuntimeError("simulate pre-completion process exit")
        return original_write(path, value)
    monkeypatch.setattr(worker, "write_once", interrupt_after_receipt)
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=body)
    install_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="pre-completion"):
        worker.run(path)
    assert len(calls) == 1
    monkeypatch.setattr(worker, "write_once", original_write)
    install_client(monkeypatch, lambda _: pytest.fail("cached HTTP200 must not trigger another request"))
    evidence = worker.run(path)
    assert evidence["valid_response_non_argmax_policy_fallback_count"] == 0
    assert json.loads((tmp_path / "native/decisions.jsonl").read_text())["tier"] == "C2"
    assert evidence["api_usage"]["request_count"] == 1


def test_invalid_probabilities_still_fail_closed_without_http_retry(tmp_path, monkeypatch):
    path = prepare(tmp_path)
    body = response()
    body["answers"]["tier"].update(choice="c3", confidence=.32,
        probabilities={"c0": .01, "c1": .02, "c2": .49, "c3": -.1})
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=body)
    install_client(monkeypatch, handler)
    with pytest.raises(Exception):
        worker.run(path)
    with pytest.raises(Exception):
        worker.run(path)
    assert len(calls) == 1
    assert not (tmp_path / "native/evidence.json").exists()


def test_cached_argmax_row_requires_explicit_no_fallback_flags(tmp_path, monkeypatch):
    path = prepare(tmp_path)
    original_write = worker.write_once
    def legacy_completed_write(path, value):
        if path.name == "completed.json":
            value = {key: v for key, v in value.items() if key not in {
                "valid_response_non_argmax_policy_fallback", "valid_response_probability_sum_policy_fallback"}}
        elif path.name == "evidence.json":
            raise RuntimeError("stop after older-format cached row")
        return original_write(path, value)
    monkeypatch.setattr(worker, "write_once", legacy_completed_write)
    install_client(monkeypatch, lambda _: httpx.Response(200, json=response()))
    with pytest.raises(RuntimeError, match="older-format"):
        worker.run(path)
    # A migration retains only immutable per-item receipts, not an incomplete
    # aggregate decisions file; emulate that using a fresh output directory.
    request = json.loads(path.read_bytes())
    fresh = tmp_path / "resumed"
    item_directory = next((tmp_path / "native/items").iterdir())
    for artifact in item_directory.iterdir():
        target = fresh / "items" / item_directory.name / artifact.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(artifact.read_bytes())
    request["output_dir"] = str(fresh)
    new_path = tmp_path / "request-resumed.json"
    original_write(new_path, request)
    monkeypatch.setattr(worker, "write_once", original_write)
    install_client(monkeypatch, lambda _: pytest.fail("completed legacy item must not call HTTP"))
    with pytest.raises(ValueError, match="fallback flags"):
        worker.run(new_path)


@pytest.mark.parametrize("probabilities", [
    {"c0": .01, "c1": .01, "c2": .01, "c3": .96},
    {"c0": .01, "c1": .02, "c2": .49, "c3": .47},
    {"c0": .01, "c1": .02, "c2": .49, "c3": .49},
])
def test_non_normalized_probabilities_use_argmax_without_resampling(tmp_path, monkeypatch, probabilities):
    path = prepare(tmp_path)
    body = response()
    body["answers"]["tier"].update(choice="c3", confidence=.99, probabilities=probabilities)
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=body)
    install_client(monkeypatch, handler)
    evidence = worker.run(path)
    result = json.loads((tmp_path / "native/decisions.jsonl").read_text())
    assert result["tier"] == max(("c0", "c1", "c2", "c3"), key=probabilities.get).upper()
    assert result["trace"]["tier"]["run_status"] == "ran"
    assert result["trace"]["tier"]["reason"] == "classifier_argmax_selected"
    assert result["trace"]["tier"]["source"] == "classifier"
    assert result["joint_tier_prediction"] == body["answers"]["tier"]
    assert result["joint_tier_prediction"]["probabilities"] == probabilities
    assert result["valid_response_probability_sum_policy_fallback"] is False
    assert result["valid_response_non_argmax_policy_fallback"] is False
    assert result["valid_response_tie_policy_fallback"] is False
    assert evidence["valid_response_probability_sum_policy_fallback_count"] == 0
    assert evidence["valid_response_non_argmax_policy_fallback_count"] == 0
    assert evidence["valid_response_tie_policy_fallback_count"] == 0
    assert evidence["api_usage"]["request_count"] == 1
    assert evidence["api_usage"]["retry_count"] == 0
    assert worker.run(path) == evidence
    assert len(calls) == 1


def test_saved_invalid_sum_200_resumes_without_additional_network(tmp_path, monkeypatch):
    path = prepare(tmp_path)
    body = response()
    body["answers"]["tier"].update(choice="c3", confidence=.99,
        probabilities={"c0": .01, "c1": .01, "c2": .01, "c3": .96})
    original_write = worker.write_once
    def interrupt_after_receipt(path, value):
        if path.name == "api.json":
            raise RuntimeError("simulate exit after durable sum-invalid HTTP200")
        return original_write(path, value)
    monkeypatch.setattr(worker, "write_once", interrupt_after_receipt)
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=body)
    install_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="sum-invalid"):
        worker.run(path)
    assert len(calls) == 1
    original_receipts = {p: p.read_bytes() for p in (tmp_path / "native/items").glob("*/event-*.json")}
    monkeypatch.setattr(worker, "write_once", original_write)
    install_client(monkeypatch, lambda _: pytest.fail("existing HTTP200 must not be resampled"))
    evidence = worker.run(path)
    assert evidence["valid_response_probability_sum_policy_fallback_count"] == 0
    assert json.loads((tmp_path / "native/decisions.jsonl").read_text())["tier"] == "C3"
    assert evidence["api_usage"]["request_count"] == 1
    assert evidence["api_usage"]["retry_count"] == 0
    assert all(p.read_bytes() == payload for p, payload in original_receipts.items())


def rejected(attempts=0):
    return {"error": {"message": "rate limited"}, "providerMetadata": {
        "gateway": {"routing": {"totalProviderAttemptCount": attempts}}}}


def service_failure(attempts=1):
    return {"error": {"type": "service_unavailable_error", "message": "temporarily unavailable"},
            "providerMetadata": {"gateway": {"routing": {
                "totalProviderAttemptCount": attempts, "modelAttemptCount": 1,
                "modelAttempts": [{"success": False, "providerAttemptCount": attempts,
                    "providerAttempts": [{"success": False, "statusCode": 503, "provider": "typesafe-ai"}
                                         for _ in range(attempts)]}]}}}}


def fake_clock(monkeypatch):
    now = [100.0]
    sleeps = []
    def sleep(seconds):
        assert 0 < seconds <= 30
        sleeps.append(seconds)
        now[0] += seconds
    monkeypatch.setattr(worker.time, "sleep", sleep)
    monkeypatch.setattr(worker.time, "monotonic", lambda: now[0])
    return sleeps


def test_proven_unexecuted_429_retries_once_and_preserves_all_receipts(tmp_path, monkeypatch):
    path = prepare(tmp_path)
    sleeps = fake_clock(monkeypatch)
    calls = []
    def handler(req):
        calls.append(json.loads(req.content))
        return httpx.Response(429, json=rejected()) if len(calls) == 1 else httpx.Response(200, json=response())
    install_client(monkeypatch, handler)
    evidence = worker.run(path)
    assert calls[0] == calls[1]
    assert sleeps == [30]
    usage = evidence["api_usage"]
    assert usage["request_count"] == 2
    assert usage["successful_count"] == 1
    assert usage["joint_intent_head_count"] == usage["joint_tier_head_count"] == 1
    assert usage["rate_limit_rejection_count"] == usage["retry_count"] == 1
    assert usage["total_tokens"] == 54 and usage["cost_usd"] == 0
    assert usage["rejected_requests"]["cost_usd"] is None
    assert usage["rejected_requests"]["input_tokens"] is None
    assert usage["rejected_requests"]["proven_provider_attempt_count"] == 0
    assert evidence["transport_policy"] == worker.TRANSPORT_POLICY
    item_dir = next((tmp_path / "native/items").iterdir())
    api = json.loads((item_dir / "api.json").read_text())
    assert [e.get("status") for e in api["events"] if e["event"] == "response"] == [429, 200]
    assert len(list(item_dir.glob("event-*.json"))) == 4
    assert worker.run(path) == evidence
    assert len(calls) == 2


@pytest.mark.parametrize("attempts", [None, 1, False, "0"])
def test_429_without_zero_provider_proof_is_accounted_as_unverified(tmp_path, monkeypatch, attempts):
    path = prepare(tmp_path)
    fake_clock(monkeypatch)
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(429, json=rejected(attempts)) if len(calls) == 1 else httpx.Response(200, json=response())
    install_client(monkeypatch, handler)
    evidence = worker.run(path)
    assert evidence["api_usage"]["unverified_gateway_failure_count"] == 1
    assert evidence["api_usage"]["rate_limit_rejection_count"] == 0
    assert evidence["api_usage"]["unverified_failures"]["cost_usd"] is None
    assert worker.run(path) == evidence
    assert len(calls) == 2


def test_attempt_limit_is_durable_and_sleep_is_bounded(tmp_path, monkeypatch):
    path = prepare(tmp_path)
    sleeps = fake_clock(monkeypatch)
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(429, json=rejected())
    install_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="attempt limit"):
        worker.run(path)
    assert len(calls) == 6
    assert sleeps == [30] * 9
    with pytest.raises(RuntimeError, match="attempt limit"):
        worker.run(path)
    assert len(calls) == 6
    assert len(list((tmp_path / "native/items").glob("*/event-*.json"))) == 12


def test_global_throttle_shared_across_threads(monkeypatch):
    sleeps = fake_clock(monkeypatch)
    throttle = worker._RequestThrottle()
    with worker.ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: throttle.wait(), range(8)))
    assert sleeps == [2] * 7


@pytest.mark.parametrize("status", [429, 503, 500, "ReadTimeout"])
def test_known_failure_resumes_in_a_fresh_process_without_repeating_failure(tmp_path, monkeypatch, status):
    path = prepare(tmp_path)
    calls = []
    def handler(req):
        calls.append(req)
        if status == "ReadTimeout":
            raise httpx.ReadTimeout("interrupted read")
        if status == 500:
            return httpx.Response(500, text="<html>upstream unavailable</html>")
        return httpx.Response(status, json=rejected() if status == 429 else service_failure())
    install_client(monkeypatch, handler)
    def stop_during_backoff(_):
        raise KeyboardInterrupt("simulate process interruption after durable rejection")
    monkeypatch.setattr(worker.time, "sleep", stop_during_backoff)
    with pytest.raises(KeyboardInterrupt):
        worker.run(path)
    assert len(calls) == 1
    events_before = {p: p.read_bytes() for p in (tmp_path / "native/items").glob("*/event-*.json")}
    program = '''
import json, sys
from pathlib import Path
import httpx
from opensquilla.engine.routing import jev_benchmark_worker as worker
from opensquilla.engine.routing.jev_model import JevModelClassifier
body = json.loads(sys.argv[2])
calls = []
def handler(request):
    calls.append(request)
    return httpx.Response(200, json=body)
def factory(**kwargs):
    kwargs["client"] = kwargs.get("client") or httpx.Client(transport=httpx.MockTransport(handler))
    return JevModelClassifier(**kwargs)
factory.validate_response = JevModelClassifier.validate_response
worker.JevModelClassifier = factory
worker.time.sleep = lambda _: None
evidence = worker.run(Path(sys.argv[1]))
assert len(calls) == 1
assert evidence["api_usage"]["request_count"] == 2
assert evidence["api_usage"]["rate_limit_rejection_count"] == int(sys.argv[3] == "429")
assert evidence["api_usage"]["transient_service_failure_count"] == int(sys.argv[3] == "503")
assert evidence["api_usage"]["unverified_gateway_failure_count"] == int(sys.argv[3] == "500")
assert evidence["api_usage"]["transport_error_count"] == int(sys.argv[3] == "ReadTimeout")
assert worker.run(Path(sys.argv[1])) == evidence
assert len(calls) == 1
'''
    completed = subprocess.run([sys.executable, "-c", program, str(path), json.dumps(response()), str(status)],
                               capture_output=True, text=True, env=dict(os.environ), timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert all(p.read_bytes() == data for p, data in events_before.items())
    assert len(list((tmp_path / "native/items").glob("*/event-*.json"))) == 4


@pytest.mark.parametrize("status", [502, 503, 504])
def test_explicit_transient_failure_retries_and_preserves_unknown_cost(tmp_path, monkeypatch, status):
    path = prepare(tmp_path)
    sleeps = fake_clock(monkeypatch)
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(status, json=service_failure()) if len(calls) == 1 else httpx.Response(200, json=response())
    install_client(monkeypatch, handler)
    usage = worker.run(path)["api_usage"]
    assert len(calls) == 2 and sleeps == [30]
    assert usage["request_count"] == 2 and usage["successful_count"] == 1
    assert usage["transient_service_failure_count"] == 1
    assert usage["rate_limit_rejection_count"] == 0
    assert usage["retry_count"] == usage["failed_or_retried_requests"] == 1
    assert usage["provider_failed"]["request_count"] == 1
    assert usage["provider_failed"]["proven_provider_attempt_count"] == 1
    assert usage["provider_failed"]["cost_usd"] is None
    assert usage["provider_failed"]["input_tokens"] is None
    assert usage["rejected_before_provider"]["request_count"] == 0
    assert usage["reported_success"]["request_count"] == 1
    assert usage["reported_success"]["cost_usd"] == 0
    worker.run(path)
    assert len(calls) == 2


def test_transient_gateway_failure_before_provider_is_a_separate_accounting_group(tmp_path, monkeypatch):
    path = prepare(tmp_path)
    fake_clock(monkeypatch)
    calls = []
    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(503, json=service_failure(attempts=0))
        return httpx.Response(200, json=response())
    install_client(monkeypatch, handler)
    usage = worker.run(path)["api_usage"]
    assert usage["transient_service_failure_count"] == 1
    assert usage["rejected_before_provider"]["request_count"] == 1
    assert usage["rejected_before_provider"]["cost_usd"] is None
    assert usage["provider_failed"]["request_count"] == 0


def test_failed_provider_reported_billing_is_not_dropped_or_mixed_with_success(tmp_path, monkeypatch):
    path = prepare(tmp_path)
    fake_clock(monkeypatch)
    calls = []
    failed = service_failure()
    failed["providerMetadata"]["gateway"]["cost"] = "0.02"
    failed["usage"] = {"inputTokens": 12, "outputTokens": 3}
    def handler(req):
        calls.append(req)
        return httpx.Response(503, json=failed) if len(calls) == 1 else httpx.Response(200, json=response())
    install_client(monkeypatch, handler)
    usage = worker.run(path)["api_usage"]
    assert usage["provider_failed"]["cost_usd"] == .02
    assert usage["provider_failed"]["input_tokens"] == 12
    assert usage["provider_failed"]["output_tokens"] == 3
    assert usage["reported_success"]["cost_usd"] == usage["cost_usd"] == 0
    assert usage["total_tokens"] == 54


@pytest.mark.parametrize("invalid", ["500", "answers", "no_error", "model_success", "provider_success",
                                     "missing_providers", "missing_models", "wrong_count", "boolean_count",
                                     "wrong_model_count", "wrong_provider_count"])
def test_unproven_service_failure_accounting_never_claims_proven_failure(tmp_path, monkeypatch, invalid):
    path = prepare(tmp_path)
    fake_clock(monkeypatch)
    body = service_failure()
    routing = body["providerMetadata"]["gateway"]["routing"]
    model = routing["modelAttempts"][0]
    if invalid == "answers":
        body["answers"] = {}
    elif invalid == "no_error":
        del body["error"]
    elif invalid == "model_success":
        model["success"] = True
    elif invalid == "provider_success":
        model["providerAttempts"][0]["success"] = True
    elif invalid == "missing_providers":
        del model["providerAttempts"]
    elif invalid == "missing_models":
        routing["modelAttempts"] = []
    elif invalid == "wrong_count":
        routing["totalProviderAttemptCount"] = 2
    elif invalid == "boolean_count":
        routing["totalProviderAttemptCount"] = True
    elif invalid == "wrong_model_count":
        routing["modelAttemptCount"] = 2
    elif invalid == "wrong_provider_count":
        model["providerAttemptCount"] = 2
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(500 if invalid == "500" else 503, json=body) if len(calls) == 1 else httpx.Response(200, json=response())
    install_client(monkeypatch, handler)
    if invalid == "answers":
        with pytest.raises(Exception):
            worker.run(path)
        with pytest.raises(RuntimeError, match="no automatic retry"):
            worker.run(path)
        assert len(calls) == 1
        assert not (tmp_path / "native/evidence.json").exists()
    else:
        evidence = worker.run(path)
        assert evidence["api_usage"]["unverified_gateway_failure_count"] == 1
        assert evidence["api_usage"]["transient_service_failure_count"] == 0
        assert evidence["api_usage"]["unverified_failures"]["proven_provider_attempt_count"] is None
        assert len(calls) == 2


def test_mixed_retries_share_one_persistent_attempt_limit(tmp_path, monkeypatch):
    path = prepare(tmp_path)
    sleeps = fake_clock(monkeypatch)
    calls = []
    def handler(req):
        calls.append(req)
        if len(calls) % 2:
            return httpx.Response(429, json=rejected())
        return httpx.Response(503, json=service_failure())
    install_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="attempt limit"):
        worker.run(path)
    with pytest.raises(RuntimeError, match="attempt limit"):
        worker.run(path)
    assert len(calls) == 6 and sleeps == [30] * 9


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_html_gateway_failure_retries_but_accounting_remains_unknown(tmp_path, monkeypatch, status):
    path = prepare(tmp_path)
    fake_clock(monkeypatch)
    calls = []
    html = "<html>unknown gateway failure</html>"
    def handler(req):
        calls.append(req)
        return httpx.Response(status, text=html) if len(calls) == 1 else httpx.Response(200, json=response())
    install_client(monkeypatch, handler)
    evidence = worker.run(path)
    usage = evidence["api_usage"]
    assert usage["request_count"] == usage["http_response_count"] == 2
    assert usage["successful_count"] == 1
    assert usage["unverified_gateway_failure_count"] == 1
    assert usage["transport_error_count"] == usage["unresolved_request_count"] == usage["unresolved_item_count"] == 0
    assert usage["unverified_failures"]["cost_usd"] is None
    assert usage["unverified_failures"]["input_tokens"] is None
    assert usage["unverified_failures"]["provider_attempt_count"] is None
    api = json.loads(next((tmp_path / "native/items").glob("*/api.json")).read_bytes())
    assert api["events"][1]["response_text"] == html
    assert [e["event"] for e in api["events"]] == ["request", "response", "request", "response"]
    assert worker.run(path) == evidence
    assert len(calls) == 2


@pytest.mark.parametrize("error_type", worker.TRANSPORT_POLICY["retryable_transport_error_types"])
def test_recorded_transport_error_retries_with_unknown_attempt_count_and_billing(tmp_path, monkeypatch, error_type):
    path = prepare(tmp_path)
    fake_clock(monkeypatch)
    calls = []
    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            raise getattr(httpx, error_type)("temporary network error test-sensitive-key")
        return httpx.Response(200, json=response())
    install_client(monkeypatch, handler)
    evidence = worker.run(path)
    usage = evidence["api_usage"]
    assert usage["request_count"] == 2 and usage["http_response_count"] == 1
    assert usage["transport_error_count"] == usage["unresolved_request_count"] == 1
    assert usage["unresolved_item_count"] == 0
    assert usage["unverified_gateway_failure_count"] == 0
    assert usage["unverified_failures"]["request_count"] == 1
    assert usage["unverified_failures"]["provider_attempt_count"] is None
    assert usage["unverified_failures"]["cost_usd"] is None
    assert usage["unverified_failures"]["input_tokens"] is None
    api = json.loads(next((tmp_path / "native/items").glob("*/api.json")).read_bytes())
    assert [e["event"] for e in api["events"]] == ["request", "transport_error", "request", "response"]
    assert "test-sensitive-key" not in json.dumps(api)
    assert worker.run(path) == evidence
    assert len(calls) == 2


def test_bare_request_with_no_terminal_receipt_is_still_not_retried(tmp_path, monkeypatch):
    path = prepare(tmp_path)
    calls = []
    def handler(req):
        calls.append(req)
        raise KeyboardInterrupt("simulate abrupt termination during request")
    install_client(monkeypatch, handler)
    with pytest.raises(KeyboardInterrupt):
        worker.run(path)
    with pytest.raises(RuntimeError, match="in_doubt"):
        worker.run(path)
    assert len(calls) == 1
    assert len(list((tmp_path / "native/items").glob("*/event-*.json"))) == 1


@pytest.mark.parametrize("kind", ["html", "transport"])
def test_unverified_failures_and_transport_errors_have_durable_six_attempt_cap(tmp_path, monkeypatch, kind):
    path = prepare(tmp_path)
    sleeps = fake_clock(monkeypatch)
    calls = []
    def handler(req):
        calls.append(req)
        if kind == "transport":
            raise httpx.ReadTimeout("no HTTP response")
        return httpx.Response(500, text="<html>still unavailable</html>")
    install_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="attempt limit"):
        worker.run(path)
    with pytest.raises(RuntimeError, match="attempt limit"):
        worker.run(path)
    assert len(calls) == 6 and sleeps == [30] * 9


def test_unknown_failures_preserve_partial_billing_without_claiming_full_total(tmp_path, monkeypatch):
    path = prepare(tmp_path)
    fake_clock(monkeypatch)
    calls = []
    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(500, json={"error": "failure", "usage": {"inputTokens": 5, "outputTokens": 1},
                "providerMetadata": {"gateway": {"cost": ".02"}}})
        if len(calls) == 2:
            raise httpx.ReadTimeout("no response or accounting")
        return httpx.Response(200, json=response())
    install_client(monkeypatch, handler)
    usage = worker.run(path)["api_usage"]
    assert usage["request_count"] == 3 and usage["http_response_count"] == 2
    assert usage["unverified_gateway_failure_count"] == usage["transport_error_count"] == 1
    unknown = usage["unverified_failures"]
    assert unknown["request_count"] == 2
    assert unknown["cost_usd"] is None and unknown["input_tokens"] is None
    assert unknown["observed_cost_usd"] == .02 and unknown["cost_observed_request_count"] == 1
    assert usage["reported_success"]["cost_usd"] == usage["cost_usd"] == 0
    assert usage["successful_count"] + usage["rate_limit_rejection_count"] + usage["transient_service_failure_count"] + usage["unverified_gateway_failure_count"] + usage["transport_error_count"] == usage["request_count"]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 200])
def test_other_http_status_or_invalid_200_never_retries(tmp_path, monkeypatch, status):
    path = prepare(tmp_path)
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(status, text="<html>not a usable answer</html>")
    install_client(monkeypatch, handler)
    with pytest.raises(Exception):
        worker.run(path)
    with pytest.raises(Exception):
        worker.run(path)
    assert len(calls) == 1


def test_contradictory_zero_provider_429_with_answers_never_retries(tmp_path, monkeypatch):
    path = prepare(tmp_path)
    body = rejected()
    body["answers"] = response()["answers"]
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(429, json=body)
    install_client(monkeypatch, handler)
    with pytest.raises(Exception):
        worker.run(path)
    with pytest.raises(RuntimeError, match="no automatic retry"):
        worker.run(path)
    assert len(calls) == 1
