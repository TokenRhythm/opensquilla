"""Offline HTTP adapter -> Agent -> Gateway -> SQLite/history contract tests.

Only the HTTP transport is replaced. These tests exercise real classification,
retry decisions, public stream projection, task settlement and history recovery;
they do not contact a live provider or claim packaged/browser coverage.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.gateway.boot import _emit_task_runtime_stream_events
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_chat import _handle_chat_history
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import AgentTaskStatus
from opensquilla.session.storage import SessionStorage

_PRIVATE_DETAIL = "PRIVATE_HTTP_PROVIDER_DETAIL"
_SESSION_KEY = "agent:main:webchat:http-error-integration"


@pytest.mark.parametrize(
    ("status", "body", "expected_kind", "recovers", "expected_calls"),
    [
        pytest.param(403, "Forbidden", "unknown", False, 1, id="bare-403"),
        pytest.param(404, "Not Found", "unknown", False, 1, id="bare-404"),
        pytest.param(
            403, {"error": {"code": "invalid_api_key", "message": "Invalid API key"}},
            "auth_invalid", False, 1, id="403-with-auth-evidence",
        ),
        pytest.param(
            404, {"error": {"code": "model_not_found", "message": "Model not found"}},
            "model_not_found", False, 1, id="404-with-model-evidence",
        ),
        pytest.param(
            403, {"error": {"code": "invalid_api_key", "message": "Access denied"}},
            "auth_invalid", False, 1, id="403-with-auth-machine-code",
        ),
        pytest.param(
            404, {"error": {"code": "model_not_found", "message": "Resource unavailable"}},
            "model_not_found", False, 1, id="404-with-model-machine-code",
        ),
        pytest.param(
            429, {"error": {"code": "rate_limit_exceeded", "message": "Rate limit exceeded"}},
            "rate_limited", False, 2, id="429-rate-limit-exhausted",
        ),
        pytest.param(
            429, {"error": {"code": "insufficient_quota", "message": "Quota exhausted"}},
            "insufficient_credits", False, 1, id="429-quota-no-retry",
        ),
        pytest.param(502, "Bad Gateway", "provider_overloaded", True, 2, id="502-recovers"),
        pytest.param(503, "Service Unavailable", "provider_overloaded", True, 2, id="503-recovers"),
        pytest.param(502, "Bad Gateway", "provider_overloaded", False, 2, id="502-exhausted"),
        pytest.param(
            503, "Service Unavailable", "provider_overloaded", False, 2, id="503-exhausted",
        ),
    ],
)
async def test_http_failure_keeps_safe_cause_through_live_and_reopened_history(
    tmp_path, monkeypatch, status, body, expected_kind, recovers, expected_calls,
) -> None:
    calls = []
    emitted = []
    original_client = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "provider.test"
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["stream"] is True
        calls.append(payload)
        # Retrying must remain an active state: no early failed task/error card.
        assert not any(name in {"session.event.error", "task.failed"} for name, _ in emitted)
        if recovers and len(calls) > 1:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=(
                    b'data: {"choices":[{"delta":{"content":"Recovered answer"},'
                    b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
                ),
            )
        if isinstance(body, dict):
            error = {**body["error"]}
            error["message"] = f"{error['message']}: {_PRIVATE_DETAIL}"
            return httpx.Response(status, json={**body, "error": error})
        return httpx.Response(status, text=f"{body}: {_PRIVATE_DETAIL}")

    def client(*args, **kwargs):
        # Also bypass configured proxy mounts: no request may reach the network.
        kwargs.pop("proxy", None)
        kwargs["transport"] = httpx.MockTransport(respond)
        kwargs["trust_env"] = False
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    provider = OpenAIProvider(
        api_key="synthetic-integration-key", model="synthetic-model",
        base_url="https://provider.test", provider_kind="openai",
    )
    agent = Agent(provider, AgentConfig(
        max_provider_retries=1, retry_base_backoff_ms=0, retry_max_backoff_ms=0,
        workspace_dir=str(tmp_path),
    ))
    database_path = str(tmp_path / "http-error-history.sqlite")
    storage = await SessionStorage.open(database_path)
    manager = SessionManager(storage, inject_time_prefix=False)

    async def emit(_session_key, name, payload):
        emitted.append((name, payload))

    async def handler(run):
        # TaskRuntime supplies the production turn context for this transcript row.
        await manager.append_message(_SESSION_KEY, "user", "Synthetic request")
        await _emit_task_runtime_stream_events(
            agent.run_turn("Synthetic request"), _SESSION_KEY, emit,
            task_id=run.task_id, session_id=session.session_id,
            session_epoch=session.epoch, heartbeat_interval=0.0, idle_timeout=2.0,
        )

    runtime = TaskRuntime(storage=storage, turn_handler=handler, event_emitter=emit)
    try:
        session = await manager.create(_SESSION_KEY)
        handle = await runtime.enqueue(RouteEnvelope(
            source_kind=SourceKind.WEB, source_name="http-error-integration",
            agent_id="main", session_key=_SESSION_KEY, session_id=session.session_id,
            session_epoch=session.epoch, input_provenance={"kind": "test"},
        ), "Synthetic request")
        record = await runtime.wait(handle.task_id, timeout=5.0)
    finally:
        await runtime.shutdown(cancel=False)
        await storage.close()

    assert len(calls) == expected_calls
    expected_status = AgentTaskStatus.SUCCEEDED if recovers else AgentTaskStatus.FAILED
    assert record.status == expected_status
    errors = [payload for name, payload in emitted if name == "session.event.error"]
    failures = [payload for name, payload in emitted if name == "task.failed"]
    activities = [payload for name, payload in emitted if name == "session.event.provider_activity"]
    retry_phases = [
        item["phase"] for item in activities if item["phase"] in {"retry_wait", "retrying"}
    ]
    assert retry_phases == (["retry_wait", "retrying"] if expected_calls == 2 else [])
    assert _PRIVATE_DETAIL not in json.dumps(emitted)
    assert len(errors) == len(failures) == (0 if recovers else 1)
    if recovers:
        done = [payload for name, payload in emitted if name == "session.event.done"]
        assert len(done) == 1
        assert done[0]["text"] == "Recovered answer"
    else:
        assert record.details["turn_outcome"]["failure_kind"] == expected_kind
        for payload in [errors[0], failures[0]]:
            assert payload["turn_outcome"]["failure_kind"] == expected_kind
            assert payload["turn_outcome"].get("replay_safe") is not True
            assert not payload.get("error_id")

    reopened = await SessionStorage.open(database_path)
    try:
        recovered = await reopened.get_agent_task(handle.task_id)
        assert recovered is not None
        assert recovered.status == expected_status
        assert recovered.details["turn_outcome"] == record.details["turn_outcome"]
        history = await _handle_chat_history(
            {"sessionKey": _SESSION_KEY, "limit": 10},
            RpcContext(
                conn_id="http-error-integration", principal=SimpleNamespace(role="operator"),
                session_manager=SessionManager(reopened, inject_time_prefix=False),
            ),
        )
        assert _PRIVATE_DETAIL not in json.dumps(history)
        assert len(history["turn_outcomes"]) == 1
        restored = history["turn_outcomes"][0]
        assert restored["task_id"] == handle.task_id
        assert restored["status"] == expected_status.value
        if recovers:
            assert "terminal_message" not in restored
        else:
            assert restored["outcome"]["failure_kind"] == expected_kind
            assert restored["code"] == errors[0]["code"]
            assert restored["terminal_message"] == errors[0]["terminal_message"]
    finally:
        await reopened.close()
