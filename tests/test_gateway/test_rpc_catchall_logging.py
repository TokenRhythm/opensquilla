"""Gateway catch-alls must log tracebacks server-side, not just return str(exc)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.middleware import ErrorHandlingMiddleware
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc.registry import RpcRegistry, RpcUnavailableError
from opensquilla.skills.toolchains.manager import toolchains_root


@pytest.fixture(autouse=True)
def _default_structlog_config() -> Iterator[None]:
    """Pin structlog to its default stdout renderer for deterministic capture.

    Another test in the same process may have routed structlog through the
    stdlib bridge (or left a custom configuration behind); reset to defaults
    so error events render to ``sys.stdout`` where ``capsys`` observes them,
    then restore the prior configuration state.
    """
    was_configured = structlog.is_configured()
    old_config = structlog.get_config()
    structlog.reset_defaults()
    try:
        yield
    finally:
        if was_configured:
            structlog.configure(**old_config)
        else:
            structlog.reset_defaults()


async def test_dispatch_catchall_logs_traceback(capsys) -> None:
    registry = RpcRegistry()

    async def _boom(params, ctx):
        raise RuntimeError("synthetic dispatch explosion")

    registry.register("test.boom", _boom, "operator.read")
    ctx = RpcContext(conn_id="test", config=GatewayConfig())

    response = await registry.dispatch("req-1", "test.boom", {}, ctx)

    # Client-visible frame is unchanged: same INTERNAL_ERROR shape as before.
    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "INTERNAL_ERROR"
    assert response.error.message == "synthetic dispatch explosion"

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "rpc.dispatch_failed" in combined
    assert "synthetic dispatch explosion" in combined
    assert "Traceback" in combined


@pytest.mark.parametrize(
    ("method", "scope", "exception", "expected_code", "expected_message", "accepted"),
    [
        (
            "artifacts.synthetic",
            "operator.write",
            ValueError("private artifact validation detail"),
            "INVALID_REQUEST",
            "The request could not be completed. Check the input and try again.",
            False,
        ),
        (
            "workbench.resources.synthetic",
            "operator.write",
            KeyError("private resource identity"),
            "MUTATION_OUTCOME_PENDING",
            "The update result cannot be confirmed yet. Open the page to check before retrying.",
            None,
        ),
        (
            "documents.synthetic",
            "operator.read",
            RuntimeError("private document storage detail"),
            "INTERNAL_ERROR",
            "The operation could not be completed. Try again.",
            False,
        ),
        (
            "workbench.previews.synthetic-value",
            "operator.read",
            ValueError("private preview validation detail"),
            "INVALID_REQUEST",
            "The request could not be completed. Check the input and try again.",
            False,
        ),
        (
            "workbench.previews.synthetic-runtime",
            "operator.read",
            RuntimeError("private preview renderer detail"),
            "INTERNAL_ERROR",
            "The operation could not be completed. Try again.",
            False,
        ),
    ],
    ids=(
        "artifact-value-error",
        "resource-key-error",
        "document-exception",
        "preview-value-error",
        "preview-runtime-error",
    ),
)
async def test_artifact_product_dispatch_fallback_never_exposes_exception(
    capsys,
    method: str,
    scope: str,
    exception: Exception,
    expected_code: str,
    expected_message: str,
    accepted: bool | None,
) -> None:
    registry = RpcRegistry()

    async def _boom(params, ctx):
        raise exception

    registry.register(method, _boom, scope)
    ctx = RpcContext(conn_id="test", config=GatewayConfig())

    response = await registry.dispatch("req-artifact", method, {}, ctx)

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == expected_code
    assert response.error.message == expected_message
    assert response.error.accepted is accepted
    assert response.error.details is not None
    correlation_id = response.error.details["correlationId"]
    assert isinstance(correlation_id, str)
    assert len(correlation_id) == 32
    assert str(exception) not in response.error.message
    assert str(exception) not in str(response.error.details)

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "artifact.rpc_dispatch_failed" in combined
    assert correlation_id in combined
    assert str(exception) in combined


@pytest.mark.parametrize(
    ("scope", "expected_code", "accepted", "retryable"),
    [
        ("operator.write", "MUTATION_OUTCOME_PENDING", None, False),
        ("operator.read", "DOCUMENT_UNAVAILABLE", False, True),
    ],
    ids=("write-outcome-remains-pending", "read-remains-retryable"),
)
async def test_artifact_unavailable_preserves_unknown_write_outcome(
    scope: str,
    expected_code: str,
    accepted: bool | None,
    retryable: bool,
) -> None:
    registry = RpcRegistry()

    async def _unavailable(params, ctx):
        raise RpcUnavailableError("private transport detail")

    registry.register("documents.synthetic-unavailable", _unavailable, scope)
    response = await registry.dispatch(
        "req-artifact-unavailable",
        "documents.synthetic-unavailable",
        {},
        RpcContext(conn_id="test", config=GatewayConfig()),
    )

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == expected_code
    assert response.error.accepted is accepted
    assert response.error.retryable is retryable
    assert "private transport detail" not in response.error.message


@pytest.mark.asyncio
async def test_dispatch_binds_configured_toolchain_state_per_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry = RpcRegistry()
    fallback_state = tmp_path / "fallback-state"
    states = (tmp_path / "state-a", tmp_path / "state-b")
    both_started = asyncio.Event()
    started = 0

    monkeypatch.setenv("OPENSQUILLA_GATEWAY_STATE_DIR", str(fallback_state))

    async def _capture_root(params, ctx):
        nonlocal started
        before_wait = toolchains_root()
        started += 1
        if started == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        await asyncio.sleep(0)
        return {
            "before": str(before_wait),
            "after": str(toolchains_root()),
        }

    registry.register("test.state-root", _capture_root, "operator.read")
    contexts = [
        RpcContext(
            conn_id=f"test-{index}",
            config=SimpleNamespace(state_dir=str(state)),
        )
        for index, state in enumerate(states)
    ]

    responses = await asyncio.gather(
        *(
            registry.dispatch(f"req-{index}", "test.state-root", {}, context)
            for index, context in enumerate(contexts)
        )
    )

    assert all(response.ok for response in responses)
    assert [response.payload for response in responses] == [
        {
            "before": str(state / "toolchains" / "v1"),
            "after": str(state / "toolchains" / "v1"),
        }
        for state in states
    ]
    assert toolchains_root() == fallback_state / "toolchains" / "v1"


def test_http_catchall_logs_traceback(capsys) -> None:
    async def _boom(request):
        raise RuntimeError("synthetic http explosion")

    app = Starlette(
        routes=[Route("/boom", _boom)],
        middleware=[Middleware(ErrorHandlingMiddleware)],
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")

    # Client-visible response is unchanged: same JSON error body as before.
    assert response.status_code == 500
    assert response.json() == {"error": "synthetic http explosion", "code": "INTERNAL_ERROR"}

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "http.request_failed" in combined
    assert "synthetic http explosion" in combined
    assert "Traceback" in combined
