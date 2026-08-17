"""One capability reached by two entry points must get one verdict.

`search.query` and the chat `web_search` tool both end at
`run_in_process_network_action`. Tool dispatch establishes a Run Context; an RPC
never enters tool dispatch, so under `NetworkMode.PROXY_ALLOWLIST` the RPC was
refused before the provider was reached while Web Chat succeeded against the
same gateway — issue #1202.

These tests pin the property rather than a posture table: whatever the chat tool
path is allowed to do, the operator RPC is allowed to do, and `search.status`
reports the verdict `search.query` will actually meet.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.gateway.rpc_tools import (
    _handle_search_query,
    _handle_search_status,
    _operator_network_tool_context,
)
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import (
    configure_runtime,
    reset_runtime,
    run_in_process_network_action,
)
from opensquilla.sandbox.run_context import RunContext
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.tools.run_mode import full_host_access_active
from opensquilla.tools.types import ToolContext, current_tool_context

_POSTURES: dict[str, SandboxSettings] = {
    "recommended": SandboxSettings(
        sandbox=True, security_grading=True, network_default="proxy_allowlist"
    ),
    "no-managed-network": SandboxSettings(
        sandbox=True, security_grading=True, network_default="none"
    ),
    "ungraded": SandboxSettings(
        sandbox=False, security_grading=False, network_default="proxy_allowlist"
    ),
    "sandbox-off": SandboxSettings(sandbox=False, security_grading=False),
}


def _configure(posture: str, workspace: Path) -> None:
    reset_runtime()
    settings = _POSTURES[posture]
    if settings is not None:
        configure_runtime(settings, workspace=workspace)


@pytest.fixture(autouse=True)
def _clean_runtime():
    reset_runtime()
    yield
    reset_runtime()


@pytest.fixture
def _search_payload(monkeypatch: pytest.MonkeyPatch):
    """Keep the provider out of it — this is about reaching the provider at all."""

    async def _payload(query, limit=None, *, provider=None):
        return {"ok": True, "query": query, "provider": provider or "duckduckgo", "results": []}

    monkeypatch.setattr("opensquilla.gateway.rpc_tools.run_web_search_payload", _payload)
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_tools.search_runtime_status",
        lambda provider=None: {"provider": provider or "duckduckgo", "ok": True},
    )


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(config=None)


async def _chat_tool_allowed(workspace: Path) -> bool:
    """What the chat `web_search` tool gets: dispatch has set a Run Context."""

    async def _callback() -> dict[str, object]:
        return {"ok": True}

    token = current_tool_context.set(
        ToolContext(
            sandbox_run_context=RunContext(run_mode=RunMode.SAFE, workspace=str(workspace)),
            workspace_dir=str(workspace),
        )
    )
    try:
        outcome = await run_in_process_network_action(
            action_kind="web.fetch",
            argv=("web_search", "opensquilla", "5", "providers=duckduckgo"),
            callback=_callback,
        )
    finally:
        current_tool_context.reset(token)
    return isinstance(outcome, dict) and outcome.get("ok") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("posture", sorted(_POSTURES))
async def test_operator_rpc_and_the_chat_tool_reach_the_same_verdict(
    posture: str, tmp_path: Path, _search_payload
) -> None:
    """The chat tool defines the expectation; the RPC must not be more restricted.

    Every posture here allows the chat tool, because supplying the Run Context
    is the whole job of the guard at this seam — a posture that refuses even
    with one would have to be built out of the approval layer below. So the
    assertion is one-directional by construction, and its regression value is
    that it fails on the pre-fix handler, where the RPC alone came back denied.
    """
    # A denial is recorded and a repeat of the same fingerprint is refused as a
    # replay, so each entry point needs its own runtime to report its own verdict.
    _configure(posture, tmp_path)
    chat_allowed = await _chat_tool_allowed(tmp_path)

    _configure(posture, tmp_path)
    result = await _handle_search_query({"query": "opensquilla github", "limit": 5}, _ctx())

    assert result["ok"] is chat_allowed


@pytest.mark.asyncio
async def test_the_reported_posture_no_longer_refuses_the_operator_query(
    tmp_path: Path, _search_payload
) -> None:
    """#1202: default DuckDuckGo, local gateway, CLI denied while Web Chat worked."""
    _configure("recommended", tmp_path)

    result = await _handle_search_query({"query": "OpenSquilla GitHub", "limit": 5}, _ctx())

    assert result["ok"] is True, result.get("error")


@pytest.mark.asyncio
@pytest.mark.parametrize("posture", sorted(_POSTURES))
async def test_status_reports_the_verdict_the_query_will_meet(
    posture: str, tmp_path: Path, _search_payload
) -> None:
    """The #1142 property, kept from the other side: readiness must not now under-report."""
    _configure(posture, tmp_path)
    status = await _handle_search_status(None, _ctx())

    _configure(posture, tmp_path)
    result = await _handle_search_query({"query": "opensquilla"}, _ctx())

    assert status["networkReady"] is result["ok"]
    if not status["networkReady"]:
        assert status["networkBlockedReason"]


@pytest.mark.asyncio
async def test_the_operator_context_carries_no_grants_of_its_own(tmp_path: Path) -> None:
    """It is the carrier for a per-action grant, not a standing authorization.

    If this context ever arrives with mounts, domains, or a public-network
    grant, an operator RPC would be reaching the network on terms the chat path
    has to ask for.
    """
    _configure("recommended", tmp_path)

    context = _operator_network_tool_context(_ctx()).sandbox_run_context

    assert isinstance(context, RunContext)
    assert context.mounts == ()
    assert context.domains == ()
    assert context.bundles == ()
    assert context.public_network == ()
    assert context.temporary_grants == ()


@pytest.mark.asyncio
async def test_the_operator_context_never_publishes_full_host_access(tmp_path: Path) -> None:
    """Safe, even when the deployment is configured Full.

    A tool context is not a description of the deployment. Every guard that
    calls `full_host_access_active()` reads the run mode off whatever context is
    current, so a `full` here stands down protections that have nothing to do
    with the network proxy. An earlier draft of this fix derived the mode from
    the gateway config; a default `GatewayConfig()` resolves to `full`, and that
    silently disabled the sensitive-payload guard below.
    """
    _configure("recommended", tmp_path)
    full_deployment = SimpleNamespace(
        config=SimpleNamespace(
            sandbox=SimpleNamespace(run_mode="full", model_fields_set={"run_mode"}),
            permissions=SimpleNamespace(default_mode="bypass"),
        )
    )

    for ctx in (_ctx(), full_deployment):
        context = _operator_network_tool_context(ctx)
        assert context.run_mode == RunMode.SAFE.value
        assert context.sandbox_run_context.run_mode is RunMode.SAFE

        token = current_tool_context.set(context)
        try:
            assert full_host_access_active() is False
        finally:
            current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_a_query_carrying_secrets_is_still_refused_before_the_provider(
    tmp_path: Path,
) -> None:
    """The Run Context must not buy the query past the sensitive-payload guard."""
    _configure("recommended", tmp_path)

    result = await _handle_search_query(
        {"query": "API_KEY=super-secret-value", "limit": 2}, _ctx()
    )

    assert result["ok"] is False
    assert result["query"] == "[redacted]"
    assert "super-secret-value" not in repr(result)
    assert result["error"]["class"] == "SensitiveInput"
