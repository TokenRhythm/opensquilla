"""Contract tests for the live LLM provider probe."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from opensquilla.onboarding import probe as probe_module
from opensquilla.onboarding.probe import probe_llm_provider
from opensquilla.provider.failures import ProviderFailureKind
from opensquilla.provider.types import (
    DoneEvent,
    ErrorEvent,
    ReasoningDeltaEvent,
    TextDeltaEvent,
)


def _sse_ok_body() -> bytes:
    chunks = [
        {"choices": [{"delta": {"content": "pong"}, "finish_reason": None}]},
        {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    ]
    body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
    return body + b"data: [DONE]\n\n"


def _patch_response(monkeypatch: Any, response: httpx.Response) -> None:
    transport = httpx.MockTransport(lambda request: response)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)


def _patch_transport_error(monkeypatch: Any, exc: Exception) -> None:
    """Route provider HTTP through a transport that always fails to connect."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)


def _probe(**kwargs: Any):
    return asyncio.run(probe_llm_provider(**kwargs))


def test_probe_reports_ok_on_completed_turn(monkeypatch: Any) -> None:
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_ok_body(),
        ),
    )
    result = _probe(provider_id="openai", model="gpt-4o", api_key="sk-test")
    assert result.ok is True
    assert result.failure_kind == ""
    assert isinstance(result.first_response_ms, int)
    assert result.first_response_ms >= 0
    assert result.total_ms == result.latency_ms


@pytest.mark.parametrize(
    ("provider_id", "model"),
    [
        ("openai", "gpt-4o"),
        ("tokenrhythm", "deepseek-v4-pro"),
    ],
)
def test_probe_always_uses_one_token_completion_budget(
    monkeypatch: Any,
    provider_id: str,
    model: str,
) -> None:
    observed_max_tokens: list[int | None] = []
    observed_models: list[str] = []
    observed_thinking: list[bool | None] = []
    observed_request_caps: list[int] = []

    class _CapturingProvider:
        provider_name = provider_id

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            observed_max_tokens.append(config.max_tokens)
            observed_thinking.append(config.thinking)
            observed_request_caps.append(config.provider_request_max_chars)

            async def _gen() -> Any:
                yield DoneEvent()

            return _gen()

        async def list_models(self) -> list[Any]:
            return []

    def _build_provider(provider: str, selected_model: str, **kwargs: Any) -> Any:
        observed_models.append(selected_model)
        return _CapturingProvider()

    monkeypatch.setattr("opensquilla.onboarding.probe.build_provider", _build_provider)

    result = _probe(provider_id=provider_id, model=model, api_key="synthetic-key")

    assert result.ok is True
    assert observed_max_tokens == [1]
    assert observed_models == [model]
    assert observed_thinking == [False]
    assert observed_request_caps[0] > 0


def test_probe_defaults_to_model_mode_and_legacy_timeout(monkeypatch: Any) -> None:
    observed_timeouts: list[float] = []

    class _Provider:
        provider_name = "openai"

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            observed_timeouts.append(config.timeout)

            async def _gen() -> Any:
                yield DoneEvent()

            return _gen()

        async def list_models(self, *, raise_on_error: bool = False) -> list[Any]:
            raise AssertionError("default probe must not list models")

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _Provider(),
    )

    result = _probe(provider_id="openai", model="gpt-4o", api_key="sk-test")

    assert result.ok is True
    assert result.verification_level == "model_verified"
    assert result.failure_stage == "model"
    assert observed_timeouts == [30.0]


def test_reachability_probe_accepts_strict_live_model_listing(monkeypatch: Any) -> None:
    class _Provider:
        provider_name = "openai"

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            raise AssertionError("successful reachability probe must not generate")

        async def list_models(self, *, raise_on_error: bool = False) -> list[Any]:
            assert raise_on_error is True
            return [object()]

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _Provider(),
    )

    result = _probe(
        provider_id="openai",
        model="gpt-4o",
        api_key="sk-test",
        mode="reachability",
    )

    assert result.ok is True
    assert result.verification_level == "reachable"
    assert result.failure_stage == "reachability"
    assert result.first_response_ms is None


def test_reachability_probe_accepts_empty_strict_listing_as_http_proof(
    monkeypatch: Any,
) -> None:
    class _Provider:
        provider_name = "openai"

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            raise AssertionError("a successful HTTP listing must not generate")

        async def list_models(self, *, raise_on_error: bool = False) -> list[Any]:
            assert raise_on_error is True
            return []

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _Provider(),
    )

    result = _probe(
        provider_id="openai",
        model="gpt-4o",
        api_key="sk-test",
        mode="reachability",
    )

    assert result.ok is True
    assert result.verification_level == "reachable"
    assert result.failure_stage == "reachability"


@pytest.mark.parametrize(
    "body",
    [
        b"<html>upstream login</html>",
        b'{"data": {"unexpected": "shape"}}',
    ],
)
def test_reachability_probe_keeps_2xx_malformed_listing_as_reachable_evidence(
    monkeypatch: Any,
    body: bytes,
) -> None:
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=body,
        ),
    )

    result = _probe(
        provider_id="openai",
        model="gpt-4o",
        api_key="synthetic-key",
        mode="reachability",
    )

    assert result.ok is False
    assert result.failure_kind == ProviderFailureKind.MALFORMED_RESPONSE.value
    assert result.code == "200"
    assert result.verification_level == "reachable"
    assert result.failure_stage == "reachability"
    assert "upstream login" not in result.message


@pytest.mark.parametrize(
    ("status_code", "expected_kind"),
    [
        (401, ProviderFailureKind.AUTH_INVALID),
        (429, ProviderFailureKind.RATE_LIMITED),
        (503, ProviderFailureKind.PROVIDER_OVERLOADED),
        (505, ProviderFailureKind.PROVIDER_OVERLOADED),
    ],
)
def test_reachability_probe_classifies_http_failures_as_reachable(
    monkeypatch: Any,
    status_code: int,
    expected_kind: ProviderFailureKind,
) -> None:
    request = httpx.Request("GET", "https://api.openai.com/v1/models")
    response = httpx.Response(status_code, request=request)

    class _Provider:
        provider_name = "openai"

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            raise AssertionError("classified listing failure must not generate")

        async def list_models(self, *, raise_on_error: bool = False) -> list[Any]:
            raise httpx.HTTPStatusError(
                f"HTTP {status_code}",
                request=request,
                response=response,
            )

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _Provider(),
    )

    result = _probe(
        provider_id="openai",
        model="gpt-4o",
        api_key="sk-test",
        mode="reachability",
    )

    assert result.ok is False
    assert result.failure_kind == expected_kind.value
    assert result.code == str(status_code)
    assert result.verification_level == "reachable"
    assert result.failure_stage == "reachability"


@pytest.mark.parametrize("status_code", [404, 501])
def test_reachability_probe_falls_back_to_model_on_unsupported_models_status(
    monkeypatch: Any,
    status_code: int,
) -> None:
    request = httpx.Request("GET", "https://example.test/v1/models")
    response = httpx.Response(status_code, request=request)
    calls: list[str] = []

    class _Provider:
        provider_name = "openai"

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            calls.append("chat")

            async def _gen() -> Any:
                yield DoneEvent()

            return _gen()

        async def list_models(self, *, raise_on_error: bool = False) -> list[Any]:
            calls.append("list")
            raise httpx.HTTPStatusError(
                f"HTTP {status_code}",
                request=request,
                response=response,
            )

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _Provider(),
    )

    result = _probe(
        provider_id="openai",
        model="gpt-4o",
        api_key="sk-test",
        mode="reachability",
    )

    assert result.ok is True
    assert result.verification_level == "model_verified"
    assert result.failure_stage == "model"
    assert calls == ["list", "chat"]


def test_reachability_probe_falls_back_on_openai_unsupported_models_body(
    monkeypatch: Any,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/models"):
            return httpx.Response(
                400,
                json={"error": {"message": "Models endpoint is not supported"}},
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_ok_body(),
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)

    result = _probe(
        provider_id="openai",
        model="gpt-4o",
        api_key="sk-test",
        mode="reachability",
    )

    assert result.ok is True
    assert result.verification_level == "model_verified"
    assert result.failure_stage == "model"
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/v1/models"),
        ("POST", "/v1/chat/completions"),
    ]


@pytest.mark.parametrize(
    ("status_code", "message"),
    [
        (404, "404 Not Found"),
        (400, "Models endpoint is not supported"),
    ],
)
def test_reachability_probe_preserves_http_proof_when_model_times_out(
    monkeypatch: Any,
    status_code: int,
    message: str,
) -> None:
    request = httpx.Request("GET", "https://example.test/v1/models")
    response = httpx.Response(status_code, request=request)

    class _Stream:
        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> Any:
            await asyncio.Event().wait()
            raise StopAsyncIteration

        async def aclose(self) -> None:
            return None

    class _Provider:
        provider_name = "openai"

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            return _Stream()

        async def list_models(self, *, raise_on_error: bool = False) -> list[Any]:
            raise httpx.HTTPStatusError(
                message,
                request=request,
                response=response,
            )

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _Provider(),
    )

    result = _probe(
        provider_id="openai",
        model="gpt-4o",
        api_key="sk-test",
        mode="reachability",
        timeout=0.01,
    )

    assert result.ok is False
    assert result.failure_kind == "probe_timeout"
    assert result.verification_level == "reachable"
    assert result.failure_stage == "model"


@pytest.mark.parametrize(
    ("status_code", "message"),
    [
        (404, "404 Not Found"),
        (400, "Models endpoint is not supported"),
    ],
)
def test_reachability_probe_preserves_http_proof_without_fallback_model(
    monkeypatch: Any,
    status_code: int,
    message: str,
) -> None:
    request = httpx.Request("GET", "https://example.test/v1/models")
    response = httpx.Response(status_code, request=request)

    class _Provider:
        provider_name = "openai"

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            raise AssertionError("a missing model must not start generation")

        async def list_models(self, *, raise_on_error: bool = False) -> list[Any]:
            raise httpx.HTTPStatusError(
                message,
                request=request,
                response=response,
            )

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _Provider(),
    )

    result = _probe(
        provider_id="openai",
        model="",
        api_key="sk-test",
        mode="reachability",
    )

    assert result.ok is False
    assert result.failure_kind == ProviderFailureKind.UNSUPPORTED_FEATURE.value
    assert result.verification_level == "reachable"
    assert result.failure_stage == "reachability"


def test_reachability_probe_falls_back_when_strict_listing_is_unavailable(
    monkeypatch: Any,
) -> None:
    calls: list[str] = []

    class _Provider:
        provider_name = "anthropic"

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            calls.append("chat")

            async def _gen() -> Any:
                yield DoneEvent()

            return _gen()

        async def list_models(self) -> list[Any]:
            raise AssertionError("non-strict listing must not be called")

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _Provider(),
    )

    result = _probe(
        provider_id="anthropic",
        model="claude-test",
        api_key="sk-test",
        mode="reachability",
    )

    assert result.ok is True
    assert result.verification_level == "model_verified"
    assert calls == ["chat"]


def test_reachability_probe_without_http_proof_keeps_timeout_unverified(
    monkeypatch: Any,
) -> None:
    class _Stream:
        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> Any:
            await asyncio.Event().wait()
            raise StopAsyncIteration

        async def aclose(self) -> None:
            return None

    class _Provider:
        provider_name = "anthropic"

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            return _Stream()

        async def list_models(self) -> list[Any]:
            raise AssertionError("non-strict listing must not be called")

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _Provider(),
    )

    result = _probe(
        provider_id="anthropic",
        model="claude-test",
        api_key="sk-test",
        mode="reachability",
        timeout=0.01,
    )

    assert result.ok is False
    assert result.failure_kind == "probe_timeout"
    assert result.verification_level == "none"
    assert result.failure_stage == "model"


def test_reachability_probe_reports_its_own_timeout(monkeypatch: Any) -> None:
    class _Provider:
        provider_name = "openai"

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            raise AssertionError("timed-out listing must not generate")

        async def list_models(self, *, raise_on_error: bool = False) -> list[Any]:
            await asyncio.sleep(10)
            return [object()]

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _Provider(),
    )

    result = _probe(
        provider_id="openai",
        model="gpt-4o",
        api_key="sk-test",
        mode="reachability",
        reachability_timeout=0.01,
    )

    assert result.ok is False
    assert result.failure_kind == "probe_timeout"
    assert result.code == "probe_timeout"
    assert result.verification_level == "none"
    assert result.failure_stage == "reachability"


def test_model_probe_outer_timeout_closes_stream(monkeypatch: Any) -> None:
    closed = False

    class _Stream:
        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> Any:
            await asyncio.Event().wait()
            raise StopAsyncIteration

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    class _Provider:
        provider_name = "openai"

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            return _Stream()

        async def list_models(self) -> list[Any]:
            return []

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _Provider(),
    )

    result = _probe(
        provider_id="openai",
        model="gpt-4o",
        api_key="sk-test",
        timeout=0.01,
    )

    assert result.ok is False
    assert result.failure_kind == "probe_timeout"
    assert result.failure_stage == "model"
    assert closed is True


@pytest.mark.asyncio
async def test_model_probe_timeout_returns_while_blocked_stream_close_is_supervised(
    monkeypatch: Any,
) -> None:
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    close_finished = asyncio.Event()

    class _Stream:
        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> Any:
            await asyncio.Event().wait()
            raise StopAsyncIteration

        async def aclose(self) -> None:
            close_started.set()
            try:
                await release_close.wait()
            finally:
                close_finished.set()

    class _Provider:
        provider_name = "openai"

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            return _Stream()

        async def list_models(self) -> list[Any]:
            return []

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _Provider(),
    )

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    result = await asyncio.wait_for(
        probe_llm_provider(
            provider_id="openai",
            model="gpt-4o",
            api_key="sk-test",
            timeout=0.01,
        ),
        timeout=0.5,
    )
    elapsed = loop.time() - started_at

    assert result.ok is False
    assert result.failure_kind == "probe_timeout"
    assert elapsed < 0.25
    await asyncio.wait_for(close_started.wait(), timeout=0.25)
    assert close_finished.is_set() is False

    release_close.set()
    await asyncio.wait_for(close_finished.wait(), timeout=0.25)
    for _ in range(10):
        if probe_module.active_provider_probe_cleanup_tasks() == 0:
            break
        await asyncio.sleep(0)
    assert probe_module.active_provider_probe_cleanup_tasks() == 0


@pytest.mark.asyncio
async def test_model_probe_cancel_during_blocked_stream_close_supervises_close_task(
    monkeypatch: Any,
) -> None:
    close_started = asyncio.Event()
    close_cancelled = asyncio.Event()
    release_close = asyncio.Event()
    close_finished = asyncio.Event()

    class _Stream:
        def __init__(self) -> None:
            self._sent_done = False

        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> Any:
            if self._sent_done:
                raise StopAsyncIteration
            self._sent_done = True
            return DoneEvent()

        async def aclose(self) -> None:
            close_started.set()
            try:
                await release_close.wait()
            except asyncio.CancelledError:
                close_cancelled.set()
                await release_close.wait()
            finally:
                close_finished.set()

    class _Provider:
        provider_name = "openai"

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            return _Stream()

        async def list_models(self) -> list[Any]:
            return []

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _Provider(),
    )

    assert probe_module.active_provider_probe_cleanup_tasks() == 0
    probe_task = asyncio.create_task(
        probe_llm_provider(
            provider_id="openai",
            model="gpt-4o",
            api_key="sk-test",
            timeout=60,
        )
    )
    await asyncio.wait_for(close_started.wait(), timeout=0.25)

    try:
        probe_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(probe_task, timeout=0.25)
        await asyncio.wait_for(close_cancelled.wait(), timeout=0.25)
        for _ in range(10):
            if probe_module.active_provider_probe_cleanup_tasks() == 1:
                break
            await asyncio.sleep(0)
        assert probe_module.active_provider_probe_cleanup_tasks() == 1
    finally:
        release_close.set()
        await asyncio.wait_for(close_finished.wait(), timeout=0.25)

    for _ in range(10):
        if probe_module.active_provider_probe_cleanup_tasks() == 0:
            break
        await asyncio.sleep(0)
    assert probe_module.active_provider_probe_cleanup_tasks() == 0


def test_model_probe_classifies_adapter_timeout_event_separately(monkeypatch: Any) -> None:
    class _Provider:
        provider_name = "openai"

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            async def _gen() -> Any:
                yield ErrorEvent(message="Request timed out", code="timeout")

            return _gen()

        async def list_models(self) -> list[Any]:
            return []

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _Provider(),
    )

    result = _probe(provider_id="openai", model="gpt-4o", api_key="sk-test")

    assert result.ok is False
    assert result.failure_kind == "probe_timeout"
    assert result.failure_stage == "model"


def test_model_probe_classifies_raised_read_timeout_separately(monkeypatch: Any) -> None:
    closed = False

    class _Provider:
        provider_name = "openai"

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            async def _gen() -> Any:
                nonlocal closed
                try:
                    raise httpx.ReadTimeout("upstream read timed out")
                    yield  # pragma: no cover - async-generator marker
                finally:
                    closed = True

            return _gen()

        async def list_models(self) -> list[Any]:
            return []

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _Provider(),
    )

    result = _probe(provider_id="openai", model="gpt-4o", api_key="sk-test")

    assert result.ok is False
    assert result.failure_kind == "probe_timeout"
    assert result.failure_stage == "model"
    assert closed is True


def test_probe_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="Probe mode"):
        _probe(
            provider_id="openai",
            model="gpt-4o",
            api_key="sk-test",
            mode="invalid",
        )


def test_probe_classifies_bad_key_as_auth_invalid(monkeypatch: Any) -> None:
    _patch_response(
        monkeypatch,
        httpx.Response(
            401,
            headers={"content-type": "application/json"},
            content=b'{"error": {"message": "Incorrect API key provided"}}',
        ),
    )
    result = _probe(provider_id="openai", model="gpt-4o", api_key="sk-bad")
    assert result.ok is False
    assert result.failure_kind == ProviderFailureKind.AUTH_INVALID.value
    assert result.code == "401"
    assert "Incorrect API key" in result.message


def test_probe_classifies_unknown_model_as_model_not_found(monkeypatch: Any) -> None:
    _patch_response(
        monkeypatch,
        httpx.Response(
            404,
            headers={"content-type": "application/json"},
            content=b'{"error": {"message": "The model does not exist"}}',
        ),
    )
    result = _probe(provider_id="openai", model="gpt-nope", api_key="sk-test")
    assert result.ok is False
    assert result.failure_kind == ProviderFailureKind.MODEL_NOT_FOUND.value


def test_probe_reports_missing_key_without_network(monkeypatch: Any) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = _probe(provider_id="openai", model="gpt-4o")
    assert result.ok is False
    assert result.failure_kind == ProviderFailureKind.AUTH_INVALID.value
    assert "OPENAI_API_KEY" in result.message
    # The probe never reached the network, so no round-trip time is reported.
    assert result.latency_ms == 0
    assert result.total_ms == 0
    assert result.first_response_ms is None


def test_probe_rejects_unknown_provider_as_validation_error() -> None:
    with pytest.raises(ValueError, match="Unknown provider"):
        _probe(provider_id="no-such-provider", model="m")


def test_probe_requires_model() -> None:
    with pytest.raises(ValueError, match="Model is required"):
        _probe(provider_id="openai", model="", api_key="sk-test")


def test_probe_classifies_connection_failure_as_transport_transient(monkeypatch: Any) -> None:
    _patch_transport_error(monkeypatch, httpx.ConnectError("connection refused"))
    result = _probe(provider_id="openai", model="gpt-4o", api_key="sk-test")
    assert result.ok is False
    assert result.failure_kind == ProviderFailureKind.TRANSPORT_TRANSIENT.value


def test_probe_classifies_raised_stream_exception_as_transport_transient(
    monkeypatch: Any,
) -> None:
    """An exception escaping the adapter's stream hits the probe's own guard."""

    class _ExplodingProvider:
        provider_name = "openai"

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            async def _gen() -> Any:
                raise RuntimeError("socket closed unexpectedly")
                yield  # pragma: no cover - makes _gen an async generator

            return _gen()

        async def list_models(self) -> list[Any]:
            return []

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _ExplodingProvider(),
    )
    result = _probe(provider_id="openai", model="gpt-4o", api_key="sk-test")
    assert result.ok is False
    assert result.failure_kind == ProviderFailureKind.TRANSPORT_TRANSIENT.value
    assert "socket closed" in result.message


def test_probe_classifies_truncated_stream_as_malformed_response(monkeypatch: Any) -> None:
    """A stream that dies before its completion event is a malformed response."""

    class _TruncatedProvider:
        provider_name = "openai"

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            async def _gen() -> Any:
                yield TextDeltaEvent(text="pa")  # then the stream just stops

            return _gen()

        async def list_models(self) -> list[Any]:
            return []

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _TruncatedProvider(),
    )
    result = _probe(provider_id="openai", model="gpt-4o", api_key="sk-test")
    assert result.ok is False
    assert result.failure_kind == ProviderFailureKind.MALFORMED_RESPONSE.value
    assert "without a completion event" in result.message
    assert isinstance(result.first_response_ms, int)
    assert result.total_ms == result.latency_ms


def test_probe_redacts_key_material_echoed_by_auth_errors(monkeypatch: Any) -> None:
    """Provider 401 bodies can echo the bad key; the probe must never repeat it."""
    leaked = "sk-verysecretsynthetictoken123"
    _patch_response(
        monkeypatch,
        httpx.Response(
            401,
            headers={"content-type": "application/json"},
            content=json.dumps(
                {"error": {"message": f"Incorrect API key provided: {leaked}"}}
            ).encode(),
        ),
    )
    result = _probe(provider_id="openai", model="gpt-4o", api_key="sk-bad")
    assert result.ok is False
    assert result.failure_kind == ProviderFailureKind.AUTH_INVALID.value
    assert leaked not in result.message
    assert "***" in result.message


def _delayed_provider(events: list[Any], delay_s: float = 0.02) -> Any:
    """Fake provider whose stream sleeps once, so latency is provably > 0.

    The reported integer can be slightly shorter than the requested sleep on
    event loops with coarse timer resolution, so callers only rely on it being
    positive.
    """

    class _DelayedProvider:
        provider_name = "openai"

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            async def _gen() -> Any:
                await asyncio.sleep(delay_s)
                for event in events:
                    yield event

            return _gen()

        async def list_models(self) -> list[Any]:
            return []

    return _DelayedProvider()


def test_probe_reports_latency_on_ok_path(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _delayed_provider([DoneEvent()], delay_s=0.02),
    )
    result = _probe(provider_id="openai", model="gpt-4o", api_key="sk-test")
    assert result.ok is True
    assert isinstance(result.latency_ms, int)
    assert result.latency_ms > 0
    assert result.first_response_ms is None
    assert result.total_ms == result.latency_ms


def test_probe_reports_latency_on_classified_error_path(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _delayed_provider(
            [ErrorEvent(message="Incorrect API key provided", code="401")], delay_s=0.02
        ),
    )
    result = _probe(provider_id="openai", model="gpt-4o", api_key="sk-bad")
    assert result.ok is False
    assert result.failure_kind == ProviderFailureKind.AUTH_INVALID.value
    assert isinstance(result.latency_ms, int)
    assert result.latency_ms > 0
    assert result.first_response_ms is None
    assert result.total_ms == result.latency_ms


def test_probe_records_first_non_empty_model_response_before_done(monkeypatch: Any) -> None:
    class _StreamingProvider:
        provider_name = "openai"

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            async def _gen() -> Any:
                yield TextDeltaEvent(text="")
                await asyncio.sleep(0.02)
                yield ReasoningDeltaEvent(text="thinking")
                await asyncio.sleep(0.02)
                yield TextDeltaEvent(text="answer")
                await asyncio.sleep(0.02)
                yield DoneEvent()

            return _gen()

        async def list_models(self) -> list[Any]:
            return []

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _StreamingProvider(),
    )

    result = _probe(provider_id="openai", model="gpt-4o", api_key="sk-test")

    assert result.ok is True
    assert isinstance(result.first_response_ms, int)
    assert result.first_response_ms > 0
    assert result.total_ms == result.latency_ms
    assert result.total_ms > result.first_response_ms


def test_probe_preserves_first_response_when_stream_later_raises(monkeypatch: Any) -> None:
    class _FailingAfterResponseProvider:
        provider_name = "openai"

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            async def _gen() -> Any:
                await asyncio.sleep(0.01)
                yield TextDeltaEvent(text="partial")
                await asyncio.sleep(0.01)
                raise RuntimeError("stream closed")

            return _gen()

        async def list_models(self) -> list[Any]:
            return []

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _FailingAfterResponseProvider(),
    )

    result = _probe(provider_id="openai", model="gpt-4o", api_key="sk-test")

    assert result.ok is False
    assert result.failure_kind == ProviderFailureKind.TRANSPORT_TRANSIENT.value
    assert isinstance(result.first_response_ms, int)
    # Coarse Windows runner clocks can quantize a real first response to 0 ms;
    # ``None`` is the contract sentinel for no response being observed.
    assert result.first_response_ms >= 0
    assert result.total_ms == result.latency_ms
    assert result.total_ms >= result.first_response_ms


def test_probe_preserves_first_response_when_error_event_follows(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _delayed_provider(
            [
                ReasoningDeltaEvent(text="partial reasoning"),
                ErrorEvent(message="upstream unavailable", code="503"),
            ],
            delay_s=0.01,
        ),
    )

    result = _probe(provider_id="openai", model="gpt-4o", api_key="sk-test")

    assert result.ok is False
    assert result.failure_kind == ProviderFailureKind.PROVIDER_OVERLOADED.value
    assert isinstance(result.first_response_ms, int)
    assert result.total_ms == result.latency_ms
    assert result.total_ms >= result.first_response_ms


def test_probe_redacts_exact_resolved_key_from_error_event_fields(monkeypatch: Any) -> None:
    """Exact-key masking also covers short keys with no recognizable prefix."""
    secret = "synthKey42"
    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _delayed_provider(
            [
                ErrorEvent(
                    message=f"Invalid API key: {secret}",
                    code=f"AUTH_{secret}",
                )
            ],
            delay_s=0,
        ),
    )

    result = _probe(provider_id="openai", model="gpt-4o", api_key=secret)

    assert result.ok is False
    assert secret not in result.message
    assert secret not in result.code
    assert "***" in result.message
    assert "***" in result.code


def test_probe_redacts_exact_resolved_key_from_stream_exception(monkeypatch: Any) -> None:
    secret = "synthKey43"

    class _LeakingProvider:
        provider_name = "openai"

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            async def _gen() -> Any:
                raise RuntimeError(f"upstream rejected {secret}")
                yield  # pragma: no cover - makes _gen an async generator

            return _gen()

        async def list_models(self) -> list[Any]:
            return []

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: _LeakingProvider(),
    )

    result = _probe(provider_id="openai", model="gpt-4o", api_key=secret)

    assert result.ok is False
    assert result.failure_kind == ProviderFailureKind.TRANSPORT_TRANSIENT.value
    assert secret not in result.message
    assert "***" in result.message


def test_probe_redacts_exact_resolved_key_from_provider_build_error(monkeypatch: Any) -> None:
    from opensquilla.provider.selector import ProviderBuildError

    secret = "synthKey44"

    def fail_build(*args: Any, **kwargs: Any) -> Any:
        raise ProviderBuildError(f"cannot configure credential {secret}")

    monkeypatch.setattr("opensquilla.onboarding.probe.build_provider", fail_build)

    result = _probe(provider_id="openai", model="gpt-4o", api_key=secret)

    assert result.ok is False
    assert result.failure_kind == ProviderFailureKind.BAD_REQUEST.value
    assert secret not in result.message
    assert "***" in result.message
