from __future__ import annotations

import json

import httpx
import pytest

from opensquilla import desktop_account as bridge
from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageExecutionContext,
    bind_usage_accounting_scope,
)
from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink
from opensquilla.provider.media_jobs import accounted_video_submission, observe_video_job
from opensquilla.provider.tokenrhythm_video_generation import refresh_tokenrhythm_video_billing
from opensquilla.session.storage import SessionStorage

BASE = "https://tokenrhythm.studio/v1"
ORIGIN = "https://tokenrhythm.studio"
LEASE_A = "os_local_" + "a" * 43
LEASE_B = "os_local_" + "b" * 43


@pytest.fixture
def account_bridge(monkeypatch):
    """Exercise the production bridge decoder without binding a local port."""
    current = {}

    class Connection:
        def __init__(self, host, port, *, timeout):
            assert (host, port, timeout) == ("127.0.0.1", 12345, 3)

        def request(self, method, path, *, headers):
            assert (method, path) == ("GET", "/state")

        def getresponse(self):
            return self

        status = 200

        def read(self, limit):
            return json.dumps(current).encode()

        def close(self):
            pass

    monkeypatch.setattr(bridge, "_endpoint", "http://127.0.0.1:12345")
    monkeypatch.setattr(bridge, "_token", "c" * 43)
    monkeypatch.setattr(bridge, "_cached", None)
    monkeypatch.setattr(bridge, "_cached_at", 0.0)
    monkeypatch.setattr(bridge, "_leases", {})
    monkeypatch.setattr(bridge, "_lease_authorities", {}, raising=False)
    monkeypatch.setattr(bridge.http.client, "HTTPConnection", Connection)
    token = bridge.pin_account_state(None)

    def activate(lease, *, account_id="fixture-account", origin=ORIGIN):
        current.clear()
        current.update({
            "source": "account", "ready": True, "credential": lease,
            "accountId": account_id, "selectedAccountId": account_id, "origin": origin,
        })
        return bridge.account_state(force=True)

    try:
        yield activate
    finally:
        bridge.reset_account_state(token)


def accounting_scope(storage):
    return bind_usage_accounting_scope(UsageAccountingScope(
        sink=SessionUsageEventSink(storage),
        context=UsageExecutionContext(
            execution_id="fixture-run", agent_run_id="fixture-run", session_id="fixture-session"
        ),
    ))


@pytest.mark.parametrize("reopen_ledger", [False, True])
async def test_delayed_video_bill_survives_account_lease_change(
    tmp_path, monkeypatch, account_bridge, reopen_ledger
):
    database = tmp_path / "usage.db"
    storage = SessionStorage(str(database))
    await storage.connect()
    requests = []
    submissions = []

    def handler(request):
        assert request.method == "GET"
        assert str(request.url) == "http://127.0.0.1:12345/v1/videos/fixture-job"
        assert request.headers["authorization"] == f"Bearer {LEASE_B}"
        requests.append(request)
        return httpx.Response(200, json={
            "id": "fixture-job", "status": "success",
            "billing": {"pending": False, "total_cost_cny": "1.15"},
        })

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handler))

    @accounted_video_submission("tokenrhythm")
    async def submit(**kwargs):
        submissions.append("fixture-job")
        kwargs["on_job_accepted"]("fixture-job")

    try:
        account_bridge(LEASE_A)
        with accounting_scope(storage):
            await submit(api_root=BASE, api_key=LEASE_A, model="fixture-video", duration=2)
            await observe_video_job("tokenrhythm", BASE, "fixture-job", {
                "billing": {"pending": True, "total_cost_cny": "1.10"},
            }, api_key=LEASE_A)
        [event] = await storage.query_usage_events(None, None)
        if reopen_ledger:
            await storage.close()
            storage = SessionStorage(str(database))
            await storage.connect()
            bridge._leases.clear()
            bridge._lease_authorities.clear()
        account_bridge(LEASE_B)
        with accounting_scope(storage):
            await refresh_tokenrhythm_video_billing(
                base_url=BASE, api_key=LEASE_B, job_id="fixture-job"
            )
            [settlement] = await storage.query_usage_settlements([event.event_id])
            assert (settlement["status"], settlement["native_amount"]) == ("final", "1.15")
            assert settlement["authority"] == "provider_confirmed"
            await refresh_tokenrhythm_video_billing(
                base_url=BASE, api_key=LEASE_B, job_id="fixture-job"
            )
        assert len(requests) == len(submissions) == 1
        assert len(await storage.query_usage_events(None, None)) == 1
        assert event.session_id == "fixture-session"
        sidecar = database.with_name(database.name + ".media-jobs.sqlite3").read_bytes()
        assert LEASE_A.encode() not in sidecar and LEASE_B.encode() not in sidecar
    finally:
        await storage.close()


@pytest.mark.parametrize("different_identity", ["account", "origin", "manual-key", "unknown-lease"])
async def test_video_billing_cannot_cross_credential_identity(
    tmp_path, monkeypatch, account_bridge, different_identity
):
    storage = SessionStorage(str(tmp_path / "usage.db"))
    await storage.connect()
    original = "fixture-manual-key" if different_identity == "manual-key" else LEASE_A
    other = "other-manual-key" if different_identity == "manual-key" else LEASE_B
    if different_identity == "unknown-lease":
        other = "os_local_" + "z" * 43
    requests = []
    original_client = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "fixture-job", "status": "success",
            "billing": {"pending": False, "total_cost_cny": "1.15"},
        })

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handler))

    def client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        return original_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)

    @accounted_video_submission("tokenrhythm")
    async def submit(**kwargs):
        kwargs["on_job_accepted"]("fixture-job")

    try:
        account_bridge(LEASE_A)
        with accounting_scope(storage):
            await submit(api_root=BASE, api_key=original, model="fixture-video", duration=2)
            await observe_video_job("tokenrhythm", BASE, "fixture-job", {
                "billing": {"pending": True, "total_cost_cny": "1.10"},
            }, api_key=original)
            [event] = await storage.query_usage_events(None, None)
            account_bridge(
                LEASE_B,
                account_id=(
                    "other-account" if different_identity == "account" else "fixture-account"
                ),
                origin=(
                    "https://gpt.tokenrhythm.studio" if different_identity == "origin" else ORIGIN
                ),
            )
            await refresh_tokenrhythm_video_billing(
                base_url=BASE, api_key=other, job_id="fixture-job"
            )
            await observe_video_job("tokenrhythm", BASE, "fixture-job", {
                "billing": {"pending": False, "total_cost_cny": "9.99"},
            }, api_key=other)
            [pending] = await storage.query_usage_settlements([event.event_id])
            assert (pending["status"], pending["native_amount"]) == ("pending", "1.10")
            assert not requests
            # The original authority remains bound to its lease even after the
            # bridge starts reporting another account. Do not read mutable state
            # to reassign an in-flight job to the currently logged-in account.
            await refresh_tokenrhythm_video_billing(
                base_url=BASE, api_key=original, job_id="fixture-job"
            )
            [settlement] = await storage.query_usage_settlements([event.event_id])
            assert (settlement["status"], settlement["native_amount"]) == ("final", "1.15")
            assert len(requests) == 1
    finally:
        await storage.close()
