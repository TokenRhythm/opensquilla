"""Result-blind Jev diagnostic worker using probability-argmax tier selection.

Only approved public inputs, the fixed deployment pool and policy are accepted.
No results, scores, benchmark identifiers or credentials enter the API payload.
Completed item receipts resume without new calls. Recorded transient failures
have bounded read-only retries; a bare request with no terminal receipt stops.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import sys
import threading
import time

import httpx

from .benchmark_worker import _iter_input_rows, _routing_request, _validate_input_bundle
from .fixed_four_tier_v2 import FIXED_FOUR_TIER_DEPLOYMENT_SPECS, TIERS, FixedFourTierV2Router
from .jev_model import JEV_TIER_SELECTION_MODE, JevModelClassifier


TRANSPORT_POLICY = {
    "schema_version": "jev-transport-policy.v3",
    "min_request_interval_seconds": 2.0,
    "max_http_attempts_per_item": 6,
    "retry_backoff_seconds": [30.0, 60.0],
    "max_single_sleep_seconds": 30.0,
    "retry_condition": (
        "Bounded read-only classification retries for recorded HTTP 408/429/500/502/503/504 "
        "without answers or a recorded whitelisted transport_error, even when execution and "
        "billing are unknown. Every attempt requires a complete request/terminal-receipt pair. "
        "Known zero-provider 429 and strictly proven unsuccessful provider attempts retain separate accounting."
    ),
    "ambiguous_transport_retry": True,
    "bare_request_retry": False,
    "retryable_http_statuses": [408, 429, 500, 502, 503, 504],
    "retryable_transport_error_types": [
        "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout",
        "ReadError", "WriteError", "ConnectError", "CloseError",
        "RemoteProtocolError", "NetworkError", "TimeoutException",
    ],
    "failed_provider_retries_may_be_billed": True,
}


def _sleep_bounded(seconds: float) -> None:
    while seconds > 0:
        step = min(seconds, TRANSPORT_POLICY["max_single_sleep_seconds"])
        time.sleep(step)
        seconds -= step


class _RequestThrottle:
    """One request-start clock shared by every worker thread in this run."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last_request = None

    def wait(self) -> None:
        with self._lock:
            if self._last_request is not None:
                delay = self._last_request + TRANSPORT_POLICY["min_request_interval_seconds"] - time.monotonic()
                _sleep_bounded(max(0, delay))
            self._last_request = time.monotonic()


def _status(event: dict) -> object:
    return event.get("status_code", event.get("status"))


def _unexecuted_rate_limit(event: dict) -> bool:
    """A gateway receipt proving zero provider attempts has known accounting."""
    body = event.get("response")
    if (event.get("event") != "response" or _status(event) != 429
            or not isinstance(body, dict) or "answers" in body):
        return False
    metadata = body.get("providerMetadata")
    gateway = metadata.get("gateway") if isinstance(metadata, dict) else None
    routing = gateway.get("routing") if isinstance(gateway, dict) else None
    attempts = routing.get("totalProviderAttemptCount") if isinstance(routing, dict) else None
    return type(attempts) is int and attempts == 0


def _transient_service_failure(event: dict) -> bool:
    """A known unsuccessful read-only call may retry, but may still be billed."""
    body = event.get("response")
    if (event.get("event") != "response" or _status(event) not in (502, 503, 504)
            or not isinstance(body, dict) or "answers" in body
            or not isinstance(body.get("error"), dict) or not body["error"]):
        return False
    metadata = body.get("providerMetadata")
    gateway = metadata.get("gateway") if isinstance(metadata, dict) else None
    routing = gateway.get("routing") if isinstance(gateway, dict) else None
    if not isinstance(routing, dict):
        return False
    attempt_count = routing.get("totalProviderAttemptCount")
    models = routing.get("modelAttempts")
    if (type(attempt_count) is not int or attempt_count < 0 or not isinstance(models, list) or not models
            or type(routing.get("modelAttemptCount")) is not int
            or routing["modelAttemptCount"] != len(models)):
        return False
    providers = []
    for model in models:
        if (not isinstance(model, dict) or model.get("success") is not False
                or not isinstance(model.get("providerAttempts"), list)
                or type(model.get("providerAttemptCount")) is not int
                or model["providerAttemptCount"] != len(model["providerAttempts"])):
            return False
        providers.extend(model["providerAttempts"])
    return (len(providers) == attempt_count
            and all(isinstance(p, dict) and p.get("success") is False for p in providers))


def _unverified_gateway_failure(event: dict) -> bool:
    if event.get("event") != "response" or _status(event) not in TRANSPORT_POLICY["retryable_http_statuses"]:
        return False
    body = event.get("response")
    if isinstance(body, dict) and "answers" in body:
        return False
    return not (_unexecuted_rate_limit(event) or _transient_service_failure(event))


def _transient_transport_error(event: dict) -> bool:
    return (event.get("event") == "transport_error"
            and event.get("error_type") in TRANSPORT_POLICY["retryable_transport_error_types"])


def _retryable_failure(event: dict) -> bool:
    return (_unexecuted_rate_limit(event) or _transient_service_failure(event)
            or _unverified_gateway_failure(event) or _transient_transport_error(event))


def _receipts(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """Validate the durable journal before deciding whether any call is safe."""
    successes, rejections = [], []
    first_request = None
    if len(events) % 2:
        raise RuntimeError("in_doubt Jev request: explicit reconciliation required; no automatic retry")
    for request, response in zip(events[::2], events[1::2]):
        if request.get("event") != "request":
            raise ValueError("invalid Jev request journal sequence")
        identity = (request.get("input_hash"), request.get("request"))
        if first_request is None:
            first_request = identity
        if identity != first_request or response.get("input_hash") != request.get("input_hash"):
            raise ValueError("Jev request identity changed between attempts")
        if response.get("event") == "transport_error" and not _transient_transport_error(response):
            raise RuntimeError("in_doubt Jev request: explicit reconciliation required; no automatic retry")
        if response.get("event") not in ("response", "transport_error"):
            raise ValueError("invalid Jev response journal sequence")
        if successes:
            raise ValueError("a successful Jev response must never be requested again")
        if response.get("event") == "response" and _status(response) == 200 and isinstance(response.get("response"), dict):
            successes.append(response)
        elif _retryable_failure(response):
            rejections.append(response)
        else:
            raise RuntimeError("previous Jev request failed; original evidence retained; no automatic retry")
    if len(events) // 2 > TRANSPORT_POLICY["max_http_attempts_per_item"]:
        raise ValueError("persisted Jev attempt limit exceeded")
    return successes, rejections


def encode(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def write_once(path: Path, value: object) -> None:
    data = encode(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"immutable artifact conflict: {path.name}")
        return
    # A temp file and exclusive hard link prevent publication of partial JSON.
    temp = path.with_name(path.name + f".tmp-{os.getpid()}-{time.time_ns()}")
    with temp.open("xb") as out:
        os.chmod(temp, 0o600)
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    try:
        os.link(temp, path)
    except FileExistsError:
        if path.read_bytes() != data:
            raise ValueError(f"immutable artifact conflict: {path.name}")
    finally:
        temp.unlink()


def sources() -> dict[str, str]:
    root = Path(__file__).parent
    return {name: digest((root / name).read_bytes()) for name in (
        "jev_benchmark_worker.py", "jev_model.py", "fixed_four_tier_v2.py", "benchmark_worker.py"
    )}


def load_hashed(path: str, expected: str) -> bytes:
    p = Path(path)
    if not p.is_absolute() or p.is_symlink() or not p.is_file():
        raise ValueError("public artifact must be an absolute regular file")
    data = p.read_bytes()
    if digest(data) != expected:
        raise ValueError("public artifact hash mismatch")
    return data


def _gateway_cost(response: dict) -> float | None:
    metadata = response.get("providerMetadata")
    meta = metadata.get("gateway") if isinstance(metadata, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    # Gateway's cost is the model request cost; gatewayCost is a separate
    # accounting field, not added twice. Keep the original response as evidence.
    value = meta.get("cost")
    try:
        result = float(value)
        return result if result >= 0 and result < float("inf") else None
    except (TypeError, ValueError):
        return None


def run(request_path: Path) -> dict:
    request_data = request_path.read_bytes()
    req = json.loads(request_data)
    keys = {"schema_version", "campaign_id", "input_path", "input_sha256", "model_pool_path",
            "model_pool_sha256", "policy_path", "policy_sha256", "output_dir", "concurrency", "expected_count"}
    if set(req) != keys or req["schema_version"] != "jev-route-only-request.v1":
        raise ValueError("unsupported Jev worker request")
    concurrency = req["concurrency"]
    if type(concurrency) is not int or not 1 <= concurrency <= 16:
        raise ValueError("concurrency must be in [1,16]")
    inputs = load_hashed(req["input_path"], req["input_sha256"])
    pool = json.loads(load_hashed(req["model_pool_path"], req["model_pool_sha256"]))
    policy = json.loads(load_hashed(req["policy_path"], req["policy_sha256"]))
    expected_pool = {tier.upper(): model for tier, _, model, _, _ in FIXED_FOUR_TIER_DEPLOYMENT_SPECS}
    if set(pool) != set(expected_pool) or any(pool[k]["model_id"] != v for k, v in expected_pool.items()):
        raise ValueError("fixed model pool changed")
    with io.BytesIO(inputs) as stream:
        count = _validate_input_bundle(stream, routing_session_mode="independent")
        rows = list(_iter_input_rows(stream, routing_session_mode="independent"))
    if count != req["expected_count"]:
        raise ValueError("approved item count mismatch")
    if policy.get("tier_selection_mode") != JEV_TIER_SELECTION_MODE:
        raise ValueError("Jev probability-argmax policy must be explicitly frozen; prepare a new run")
    options = {k: policy[k] for k in ("default_new_task_tier", "intent_min_confidence", "tier_min_confidence", "min_margin")}
    if options != dict(default_new_task_tier="c1", intent_min_confidence=0.5, tier_min_confidence=0.5, min_margin=0.05):
        raise ValueError("historical policy thresholds differ")
    out = Path(req["output_dir"])
    if not out.is_absolute():
        raise ValueError("absolute output directory required")
    source_hashes = sources()
    binding = {"request_sha256": digest(request_data), "source_hashes": source_hashes,
               "tier_selection_mode": JEV_TIER_SELECTION_MODE,
               "input_sha256": req["input_sha256"], "policy_sha256": req["policy_sha256"],
               "transport_policy": TRANSPORT_POLICY}
    write_once(out / "run_binding.json", binding)
    throttle = _RequestThrottle()

    def route(row):
        item_hash = hashlib.sha256(row.item_id.encode()).hexdigest()
        item_dir = out / "items" / item_hash
        completed = item_dir / "completed.json"
        if completed.exists():
            result = json.loads(completed.read_bytes())
            if result.get("trace", {}).get("classifier_identity", {}).get("tier_selection_mode") != JEV_TIER_SELECTION_MODE:
                raise ValueError("cached Jev tier policy differs; historical decisions cannot become argmax results")
            if any(result.get(f"valid_response_{kind}_policy_fallback") is not False
                   for kind in ("tie", "non_argmax", "probability_sum")):
                raise ValueError("cached Jev argmax decision has incomplete or incompatible fallback flags")
            if result["item_id"] != row.item_id or result["input_row_sha256"] != "sha256:" + row.row_sha256.removeprefix("sha256:"):
                raise ValueError("cached item input mismatch")
            api = result["api_call_ref"]
            load_hashed(str(out / api["path"]), api["sha256"])
            return result
        events = []
        for p in sorted(item_dir.glob("event-*.json")):
            events.append(json.loads(p.read_bytes()))
        while True:
            responses, rejections = _receipts(events)
            replayed = bool(responses)
            client = None
            if replayed:
                # A received success completes policy execution without network.
                body = responses[0]["response"]
                client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
            else:
                if len(rejections) >= TRANSPORT_POLICY["max_http_attempts_per_item"]:
                    raise RuntimeError("known retryable failure exhausted the durable HTTP attempt limit")
                if rejections:
                    delays = TRANSPORT_POLICY["retry_backoff_seconds"]
                    _sleep_bounded(delays[min(len(rejections) - 1, len(delays) - 1)])

            def journal(event):
                if replayed:
                    return
                event = dict(event)
                if event.get("event") == "request":
                    if events and (event.get("input_hash"), event.get("request")) != (
                        events[0].get("input_hash"), events[0].get("request")
                    ):
                        raise ValueError("retried Jev input differs from its durable request")
                    # This callback runs immediately before the adapter's POST.
                    # The shared throttle also applies to every retry.
                    throttle.wait()
                if "latency_seconds" in event:
                    event["latency_ms"] = event["latency_seconds"] * 1000
                write_once(item_dir / f"event-{len(events):03d}.json", event)
                events.append(event)

            attempt_start_event_count = len(events)
            classifier = JevModelClassifier(timeout_seconds=60, journal=journal, client=client)
            router = FixedFourTierV2Router(
                intent_classifier=classifier, tier_classifier=classifier, **options,
                route_id_factory=lambda: "jev-route-" + item_hash,
                task_id_factory=lambda: "jev-task-" + item_hash,
                clock_ms=lambda: 0,
                policy_config={**policy, "classifier": dict(classifier.identity)},
            )
            try:
                request = _routing_request(row, inference_run_id=req["campaign_id"] + "-jev-v1")
                decision, _ = router.decide(request, None)
            except Exception:
                # Unknown outcomes and every other error propagate. The journal
                # must prove a newly received retryable failure before retrying.
                if (not replayed and len(events) == attempt_start_event_count + 2
                        and _retryable_failure(events[-1])):
                    _receipts(events)
                    continue
                raise
            finally:
                router.close()
                if client is not None:
                    client.close()
            break

        if decision.intent.final != "new_task" or decision.intent.run_status != "not_run":
            raise RuntimeError("independent first-turn intent policy changed")
        response_events, _ = _receipts(events)
        if len(response_events) != 1:
            raise ValueError("one joint Jev response is required per item")
        response_event = response_events[0]
        validated = JevModelClassifier.validate_response(response_event["response"])
        probabilities = validated["tier"].probabilities
        expected_tier = max(TIERS, key=lambda label: probabilities[label])
        if (
            decision.classifier_backend != "jev"
            or decision.tier.run_status != "ran"
            or decision.tier.source != "classifier"
            or decision.tier.reason != "classifier_argmax_selected"
            or decision.final_tier != expected_tier
        ):
            raise RuntimeError("Jev tier must be the probability argmax without policy fallback")
        api_path = item_dir / "api.json"
        api_record = {"events": events, "joint_heads": ["intent", "tier"], "intent_policy_consumed": False}
        write_once(api_path, api_record)
        tier = decision.final_tier.upper()
        result = {
            "item_id": row.item_id, "tier": tier, "model_id": pool[tier]["model_id"],
            "input_row_sha256": "sha256:" + row.row_sha256.removeprefix("sha256:"),
            "trace": decision.trace(provider="openrouter", model=pool[tier]["model_id"]),
            "api_call_ref": {"path": str(api_path.relative_to(out)), "sha256": digest(api_path.read_bytes())},
            "joint_intent_prediction": response_event["response"]["answers"]["intent"],
            "joint_tier_prediction": response_event["response"]["answers"]["tier"],
            "valid_response_tie_policy_fallback": False,
            "valid_response_non_argmax_policy_fallback": False,
            "valid_response_probability_sum_policy_fallback": False,
        }
        write_once(completed, result)
        return result

    decisions = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        pending = {executor.submit(route, row): row.item_id for row in rows}
        try:
            for future in as_completed(pending):
                decisions.append(future.result())
                if len(decisions) % 20 == 0 or len(decisions) == count:
                    print(json.dumps({"completed": len(decisions), "expected": count, "campaign": req["campaign_id"]}), flush=True)
        except BaseException:
            for future in pending:
                future.cancel()
            raise
    decisions.sort(key=lambda x: x["item_id"])
    if sources() != source_hashes:
        raise ValueError("worker source changed during inference")
    output = b"".join(encode(row) + b"\n" for row in decisions)
    path = out / "decisions.jsonl"
    if path.exists() and path.read_bytes() != output:
        raise ValueError("frozen decisions changed")
    if not path.exists():
        with path.open("xb") as f:
            f.write(output)
            f.flush()
            os.fsync(f.fileno())
    totals = {"input_tokens": [], "output_tokens": [], "cost_usd": [], "latency_ms": []}
    rejected_totals = {k: [] for k in totals}
    provider_failed_totals = {k: [] for k in totals}
    unverified_totals = {k: [] for k in totals}
    request_count = rejection_count = service_failure_count = 0
    unverified_gateway_count = transport_error_count = 0
    before_provider_count = provider_failed_count = failed_provider_attempt_count = 0
    for row in decisions:
        api = json.loads((out / row["api_call_ref"]["path"]).read_bytes())
        successful, rejected = _receipts(api["events"])
        if len(successful) != 1:
            raise ValueError("every completed item requires exactly one successful HTTP response")
        request_count += len(successful) + len(rejected)
        rejection_count += sum(_unexecuted_rate_limit(e) for e in rejected)
        service_failure_count += sum(_transient_service_failure(e) for e in rejected)
        unverified_gateway_count += sum(_unverified_gateway_failure(e) for e in rejected)
        transport_error_count += sum(_transient_transport_error(e) for e in rejected)
        before_provider, provider_failed, unverified = [], [], []
        for event in rejected:
            if _unverified_gateway_failure(event) or _transient_transport_error(event):
                unverified.append(event)
                continue
            attempts = event["response"]["providerMetadata"]["gateway"]["routing"]["totalProviderAttemptCount"]
            if attempts == 0:
                before_provider.append(event)
            else:
                provider_failed.append(event)
                failed_provider_attempt_count += attempts
        before_provider_count += len(before_provider)
        provider_failed_count += len(provider_failed)
        for group, target in ((successful, totals), (before_provider, rejected_totals),
                              (provider_failed, provider_failed_totals), (unverified, unverified_totals)):
            for event in group:
                response = event.get("response")
                response = response if isinstance(response, dict) else {}
                usage = response.get("usage")
                usage = usage if isinstance(usage, dict) else {}
                target["input_tokens"].append(usage.get("inputTokens", usage.get("input_tokens")))
                target["output_tokens"].append(usage.get("outputTokens", usage.get("output_tokens")))
                target["cost_usd"].append(_gateway_cost(response))
                target["latency_ms"].append(event.get("latency_ms"))
    def aggregate(values):
        return {k: sum(v) if all(isinstance(x, (float, int)) and not isinstance(x, bool) for x in v) else None for k, v in values.items()}
    api_usage = aggregate(totals)
    accounting = "Reported gateway fields only; missing amounts are not imputed to zero."
    rejected_usage = {**aggregate(rejected_totals), "request_count": before_provider_count,
                      "proven_provider_attempt_count": 0, "accounting": accounting}
    observed_unverified_costs = [value for value in unverified_totals["cost_usd"] if value is not None]
    unverified_usage = {**aggregate(unverified_totals),
                        "request_count": unverified_gateway_count + transport_error_count,
                        "gateway_response_count": unverified_gateway_count,
                        "transport_error_count": transport_error_count,
                        "provider_attempt_count": None, "proven_provider_attempt_count": None,
                        "observed_cost_usd": sum(observed_unverified_costs) if observed_unverified_costs else None,
                        "cost_observed_request_count": len(observed_unverified_costs),
                        "accounting": "Execution and billing may have occurred; missing amounts are unknown, never zero-imputed."}
    api_usage.update({"request_count": request_count, "successful_count": count,
                      "joint_intent_head_count": count, "joint_tier_head_count": count,
                      "intent_policy_consumed_count": 0, "failed_or_retried_requests": request_count - count,
                      "rate_limit_rejection_count": rejection_count, "retry_count": request_count - count,
                      "transient_service_failure_count": service_failure_count,
                      "unverified_gateway_failure_count": unverified_gateway_count,
                      "transport_error_count": transport_error_count,
                      "http_response_count": request_count - transport_error_count,
                      "unresolved_item_count": 0, "unresolved_request_count": transport_error_count,
                      "accounting_basis": "Top-level token/cost fields cover successful classifications only.",
                      "reported_success": {**aggregate(totals), "request_count": count, "accounting": accounting},
                      "rejected_before_provider": rejected_usage,
                      "rejected_requests": rejected_usage,
                      "unverified_failures": unverified_usage,
                      "provider_failed": {**aggregate(provider_failed_totals), "request_count": provider_failed_count,
                          "proven_provider_attempt_count": failed_provider_attempt_count,
                          "accounting": accounting}})
    api_usage["total_tokens"] = (api_usage["input_tokens"] + api_usage["output_tokens"]
                                  if api_usage["input_tokens"] is not None and api_usage["output_tokens"] is not None else None)
    evidence = {**binding, "schema_version": "jev-route-only-evidence.v1",
        "model_pool_sha256": req["model_pool_sha256"], "decision_sha256": digest(output), "item_count": count,
        "classifier_backend": "jev", "source_hashes": source_hashes, "api_usage": api_usage,
        "classifier_fallback_decision_count": sum(r["trace"]["tier"]["source"] == "fallback" for r in decisions),
        "valid_response_tie_policy_fallback_count": sum(r["valid_response_tie_policy_fallback"] for r in decisions),
        "valid_response_non_argmax_policy_fallback_count": sum(r.get("valid_response_non_argmax_policy_fallback", False) for r in decisions),
        "valid_response_probability_sum_policy_fallback_count": sum(r.get("valid_response_probability_sum_policy_fallback", False) for r in decisions),
        "environment": {"python": platform.python_version(), "httpx": httpx.__version__},
        "release_gate_eligible": False, "router_entrypoint": "FixedFourTierV2Router.decide",
        "limitations": ["Hosted model alias is not an immutable model revision.",
                        "Result-blind process protocol is not an OS filesystem sandbox.",
                        "Both Jev heads evaluated; first-turn intent remains policy-gated.",
                        "Tier is probability argmax; confidence, margin and provider choice do not gate selection."]}
    write_once(out / "evidence.json", evidence)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    try:
        evidence = run(args.request)
        print(json.dumps({"status": "complete", "items": evidence["item_count"]}), flush=True)
        return 0
    except Exception as exc:
        # Do not print transport exception strings or credentials to worker.log.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "message": "Inspect per-item receipts; transient receipt-backed failures have bounded retries, bare requests do not."}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
