from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from scripts.live_usage_matrix import (
    ENDPOINTS,
    _apply_reconciled_snapshot,
    reconcile_local,
    reconcile_videos,
    resume_video_delivery,
)


async def _accepted_case(
    tmp_path, case_id, *, owner=None, key="synthetic-key", provider="openrouter", receipt=None
):
    from opensquilla.engine.usage_accounting import (
        UsageAccountingScope,
        UsageExecutionContext,
        bind_usage_accounting_scope,
    )
    from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink
    from opensquilla.provider.media_jobs import accounted_video_submission, observe_video_job
    from opensquilla.session.models import SessionNode
    from opensquilla.session.storage import SessionStorage

    directory = tmp_path / "ledgers"
    directory.mkdir(exist_ok=True)
    storage = await SessionStorage.open(str(directory / (case_id + ".db")))
    sink = SessionUsageEventSink(storage)
    root, child = "root-" + case_id, "child-" + case_id
    root_key = "agent:main:webchat:" + root
    await storage.upsert_session(SessionNode(session_key=root_key, session_id=root))
    await storage.upsert_session(
        SessionNode(
            session_key="agent:main:webchat:" + child,
            session_id=child,
            parent_session_key=root_key,
            origin={"sidechat": {"parentSessionId": root, "parentEpoch": 0}},
        )
    )
    context = UsageExecutionContext(
        execution_id=case_id, agent_run_id=case_id, session_id=owner or child
    )

    @accounted_video_submission(provider)
    async def submit(**kwargs):
        kwargs["on_job_accepted"]("job-" + case_id)
        return "job-" + case_id

    try:
        with bind_usage_accounting_scope(UsageAccountingScope(sink=sink, context=context)):
            await submit(api_root=ENDPOINTS[provider], model="fixture", duration=2, api_key=key)
            if receipt is not None:
                await observe_video_job(
                    provider, ENDPOINTS[provider], "job-" + case_id, receipt, api_key=key
                )
        await sink.close(drain_timeout=5)
    finally:
        await storage.close()
    return {
        "id": case_id,
        "provider": provider,
        "model": "fixture",
        "kind": "video",
        "status": "in_flight",
        "reservationUsd": "1",
        "functional": False,
        "eventCount": 99,
        "parentRollupConserved": False,
    }


@pytest.mark.asyncio
async def test_get_only_recovery_handles_failure_then_exact_bill_and_local_budget(
    tmp_path, monkeypatch
):
    from opensquilla.gateway import usage_query
    from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink

    cases = [await _accepted_case(tmp_path, name) for name in ("denied", "good")]
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({"budgetUsedUsd": "2", "results": cases}))
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("job-denied"):
            return httpx.Response(
                403,
                json={"error": {"code": "MODEL_ACCESS_DENIED", "message": "synthetic-key private"}},
            )
        return httpx.Response(
            200,
            content=b'{"status":"completed","usage":{"cost":0.123456789012345678,"output_seconds":2}}',
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    original_close = SessionUsageEventSink.close
    original_query = usage_query.query_usage_ledger

    async def close(sink, **kwargs):
        await original_close(sink, **kwargs)
        sink._storage._test_drained = True

    async def query(storage, *args, **kwargs):
        assert storage._test_drained
        return await original_query(storage, *args, **kwargs)

    monkeypatch.setattr(SessionUsageEventSink, "close", close)
    monkeypatch.setattr(usage_query, "query_usage_ledger", query)
    result = await reconcile_videos({"openrouter": "synthetic-key"}, tmp_path)
    denied, good = result["videoRecovery"]
    assert denied["httpStatus"] == 403 and denied["errorCode"] == "MODEL_ACCESS_DENIED"
    assert good["recoveryStatus"] == "reconciled" and good["restartReplayIdempotent"]
    assert len(requests) == 3
    saved = json.loads(report_path.read_text())
    recovered = saved["results"][1]
    assert recovered["jobId"] == "job-good"
    assert recovered["eventCount"] == 1 and recovered["parentRollupConserved"]
    assert recovered["replayIdempotent"] is True and recovered["functional"] is False
    assert recovered["totals"]["billedCostUsdExact"] == "0.123456789012345678"
    assert recovered["budgetAccountedUsd"] == "1"
    assert "private" not in (tmp_path / "video-reconciliation.json").read_text()
    monkeypatch.setattr(usage_query, "query_usage_ledger", original_query)
    local = await reconcile_local({"openrouter": "synthetic-key"}, tmp_path, "50")
    assert local["reconciledLocalLedgers"] == 2
    assert Decimal(local["budgetReservedOrSpentUsd"]) == Decimal("1.123456789012345678")
    assert json.loads(report_path.read_text())["results"][1]["functional"] is False


@pytest.mark.asyncio
async def test_recovery_rejects_foreign_owner_and_payer_without_any_request(tmp_path, monkeypatch):
    cases = [
        await _accepted_case(tmp_path, "wrong-owner", owner="foreign-owner"),
        await _accepted_case(tmp_path, "wrong-payer", key="other-synthetic-key"),
    ]
    (tmp_path / "report.json").write_text(json.dumps({"budgetUsedUsd": "2", "results": cases}))

    def client(**kwargs):
        pytest.fail("a foreign job must never reach HTTP")

    monkeypatch.setattr(httpx, "AsyncClient", client)
    result = await reconcile_videos({"openrouter": "synthetic-key"}, tmp_path)
    assert [row["recoveryStatus"] for row in result["videoRecovery"]] == [
        "durable_job_owner_missing_or_ambiguous",
        "durable_job_payer_mismatch",
    ]


@pytest.mark.asyncio
async def test_recovery_transport_failure_does_not_stop_next_case(tmp_path, monkeypatch):
    cases = [await _accepted_case(tmp_path, name) for name in ("timeout", "pending")]
    (tmp_path / "report.json").write_text(json.dumps({"budgetUsedUsd": "2", "results": cases}))
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("job-timeout"):
            raise httpx.ReadTimeout("synthetic-key private")
        return httpx.Response(200, json={"status": "processing"})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    result = await reconcile_videos({"openrouter": "synthetic-key"}, tmp_path)
    assert len(requests) == 3
    assert result["videoRecovery"][0]["errorType"] == "ReadTimeout"
    pending = result["videoRecovery"][1]
    assert pending["jobOutcome"] == "pending" and pending["totals"]["missingCostEntries"] == 1
    assert "private" not in (tmp_path / "video-reconciliation.json").read_text()


def test_snapshot_recomputes_event_and_parent_evidence_without_inventing_replay():
    totals = {"costNanos": 1, "billedCostNanos": 1, "estimatedCostNanos": 0, "eventCount": 1}
    snapshot = {
        "totals": totals,
        "coverage": {},
        "taskGroups": [
            {"rootSessionId": "root-a", "totals": totals, "branches": {"sideChats": totals}}
        ],
    }
    row = {
        "id": "a",
        "functional": True,
        "status": "passed",
        "eventCount": 99,
        "parentRollupConserved": False,
    }
    _apply_reconciled_snapshot(row, snapshot, [SimpleNamespace(status="finalized")])
    assert row["eventCount"] == 1 and row["parentRollupConserved"] is True
    assert row["status"] == "accounting_unverified" and "replayIdempotent" not in row
    assert row["functional"] is True


@pytest.mark.asyncio
async def test_local_missing_database_marks_old_inflight_interrupted_without_releasing_hold(
    tmp_path,
):
    active = {"id": "missing", "status": "in_flight", "reservationUsd": "1.25"}
    skipped = {"id": "skipped", "status": "skipped_budget", "reservationUsd": "4"}
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({"budgetUsedUsd": "1.25", "results": [active, skipped]}))
    result = await reconcile_local({}, tmp_path, "50")
    saved = json.loads(report_path.read_text())
    assert result["reconciledLocalLedgers"] == 0
    assert saved["budgetUsedUsd"] == "1.25"
    assert saved["results"] == [{**active, "status": "interrupted"}, skipped]
    assert not (tmp_path / "ledgers").exists()


def _delivery_receipt(case_id):
    return {
        "id": "job-" + case_id,
        "status": "completed",
        "billing": {"pending": False, "total_cost_cny": "1.2"},
        "usage": {"output_seconds": 2},
        "result": {"url": "https://media.example.invalid/fixture.mp4"},
    }


async def _delivery_case(tmp_path, case_id="delivery", **kwargs):
    row = await _accepted_case(
        tmp_path, case_id, provider="tokenrhythm", receipt=_delivery_receipt(case_id), **kwargs
    )
    row.update(
        jobId="job-" + case_id,
        videoRecovery={"recoveryStatus": "reconciled", "jobOutcome": "completed"},
        replayIdempotent=True,
        budgetAccountedUsd="0.172043010752688172",
    )
    (tmp_path / "report.json").write_text(
        json.dumps({"budgetUsedUsd": "7.2", "budgetLimitUsd": "200", "results": [row]})
    )
    return row


@pytest.mark.asyncio
async def test_delivery_default_selection_does_nothing_even_without_report(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: pytest.fail("no HTTP expected"))
    assert await resume_video_delivery({}, tmp_path, {}) == {"deliveryRecovery": []}
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("download_status", [200, 403])
async def test_production_delivery_resume_is_get_only_and_preserves_original_bill(
    tmp_path, monkeypatch, download_status
):
    from opensquilla.provider import tokenrhythm_video_generation, video_download
    from opensquilla.session.storage import SessionStorage

    old = await _delivery_case(tmp_path)
    requests = []
    binary = b"\x00\x00\x00\x18ftypisom" + bytes(12)

    class Bytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield binary

    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        if request.url.host == "tokenrhythm.studio":
            assert request.headers.get("Authorization") == "Bearer synthetic-key"
            return httpx.Response(200, json=_delivery_receipt("delivery"))
        assert "Authorization" not in request.headers
        return httpx.Response(
            download_status, headers={"content-type": "video/mp4"}, stream=Bytes()
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    monkeypatch.setattr(
        video_download, "_client_options", lambda *args: {"follow_redirects": False}
    )
    original_resume = tokenrhythm_video_generation.resume_tokenrhythm_video

    async def checked_resume(**kwargs):
        from opensquilla.engine.usage_accounting import current_usage_accounting_scope

        assert kwargs["timeout_seconds"] == 90 and kwargs["max_bytes"] == 16 * 1024 * 1024
        assert current_usage_accounting_scope().context.session_id.startswith("delivery-observer-")
        return await original_resume(**kwargs)

    monkeypatch.setattr(tokenrhythm_video_generation, "resume_tokenrhythm_video", checked_resume)
    result = await resume_video_delivery(
        {"tokenrhythm": "synthetic-key"}, tmp_path, {"case_ids": ["delivery"]}
    )
    [evidence] = result["deliveryRecovery"]
    assert len(requests) == 2
    saved = json.loads((tmp_path / "report.json").read_text())
    assert saved["budgetUsedUsd"] == "7.2" and saved["budgetLimitUsd"] == "200"
    [row] = saved["results"]
    assert row["budgetAccountedUsd"] == old["budgetAccountedUsd"]
    if download_status == 200:
        assert evidence["status"] == "delivered" and evidence["ledgerUnchanged"]
        assert row["functional"] and row["bytes"] == len(binary)
        assert row["eventCount"] == 1 and row["parentRollupConserved"]
        assert (tmp_path / evidence["artifact"]).read_bytes() == binary
    else:
        assert evidence["status"] == "failed" and row["functional"] is False
        assert {k: v for k, v in row.items() if k != "deliveryRecovery"} == old
    storage = await SessionStorage.open(str(tmp_path / "ledgers" / "delivery.db"))
    try:
        [event] = await storage.query_usage_events(None, None)
        [settlement] = await storage.query_usage_settlements([event.event_id])
        assert event.session_id == "child-delivery"
        assert (
            settlement["native_amount"] == "1.2" and settlement["authority"] == "provider_confirmed"
        )
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("constraint", ["owner", "payer", "reconciliation"])
async def test_delivery_rejects_unverified_identity_without_http(tmp_path, monkeypatch, constraint):
    options = (
        {"owner": "foreign"}
        if constraint == "owner"
        else {"key": "other-key"}
        if constraint == "payer"
        else {}
    )
    row = await _delivery_case(tmp_path, **options)
    if constraint == "reconciliation":
        row.pop("videoRecovery")
        (tmp_path / "report.json").write_text(
            json.dumps({"budgetUsedUsd": "7.2", "results": [row]})
        )
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: pytest.fail("invalid identity must not dispatch")
    )
    result = await resume_video_delivery(
        {"tokenrhythm": "synthetic-key"}, tmp_path, {"case_ids": ["delivery"]}
    )
    assert (
        result["deliveryRecovery"][0]["status"]
        == {
            "owner": "durable_job_owner_missing_or_ambiguous",
            "payer": "durable_job_payer_mismatch",
            "reconciliation": "case_not_reconciled_tokenrhythm_video",
        }[constraint]
    )
