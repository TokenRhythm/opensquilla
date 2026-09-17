"""Readiness must agree with what the network path will actually do.

A search provider can be configured, buildable, and still refused before it is
reached, because the sandbox resolves a network mode per action. Issue #1130 is
that disagreement: Overview and ``opensquilla search status`` said DuckDuckGo was
ready while ``opensquilla search query`` came back denied.

The mode is resolved from the graded ``SecurityLevel``, not from the configured
run mode, so these tests pin the property that matters — the precondition and the
real call reach the same verdict — rather than a table of postures a second
implementation would have to keep in step.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import (
    configure_runtime,
    effective_network_mode,
    in_process_network_precondition,
    reset_runtime,
    run_in_process_network_action,
)
from opensquilla.sandbox.run_context import RunContext
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.sandbox.types import NetworkMode

_ARGV = ("web_search", "opensquilla", "5", "providers=duckduckgo")

_POSTURES = {
    "recommended": SandboxSettings(
        sandbox=True, security_grading=True, network_default="proxy_allowlist"
    ),
    "no-managed-network": SandboxSettings(
        sandbox=True, security_grading=True, network_default="none"
    ),
    "ungraded": SandboxSettings(
        sandbox=True, security_grading=False, network_default="proxy_allowlist"
    ),
    "sandbox-off": SandboxSettings(sandbox=False, security_grading=False),
}


@pytest.fixture(autouse=True)
def _clean_runtime():
    reset_runtime()
    yield
    reset_runtime()


async def _query_denied(tmp_path: Path) -> bool:
    async def _callback() -> dict[str, object]:
        return {"ok": True}

    outcome = await run_in_process_network_action(
        action_kind="web.fetch",
        argv=_ARGV,
        callback=_callback,
    )
    return not (isinstance(outcome, dict) and outcome.get("ok") is True)


@pytest.mark.asyncio
@pytest.mark.parametrize("posture", sorted(_POSTURES))
async def test_readiness_and_the_real_call_reach_the_same_verdict(
    posture: str, tmp_path: Path
) -> None:
    configure_runtime(_POSTURES[posture], workspace=tmp_path)

    blocked_by_readiness = in_process_network_precondition() is not None
    denied_by_the_call = await _query_denied(tmp_path)

    assert blocked_by_readiness == denied_by_the_call


@pytest.mark.asyncio
async def test_the_reported_posture_reports_the_reason_it_will_refuse(tmp_path: Path) -> None:
    configure_runtime(_POSTURES["recommended"], workspace=tmp_path)

    reason = in_process_network_precondition()

    assert reason is not None
    assert "Run Context grants" in reason
    assert await _query_denied(tmp_path)


@pytest.mark.asyncio
async def test_a_disabled_network_without_run_context_reports_required_grant(
    tmp_path: Path,
) -> None:
    configure_runtime(_POSTURES["no-managed-network"], workspace=tmp_path)

    reason = in_process_network_precondition()

    assert reason is not None
    assert "Network-disabled" in reason
    assert "Run Context grants" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("posture", ["recommended", "no-managed-network"])
async def test_an_established_run_context_clears_the_precondition_and_the_call_runs(
    posture: str, tmp_path: Path
) -> None:
    from opensquilla.tools.types import ToolContext, current_tool_context

    configure_runtime(_POSTURES[posture], workspace=tmp_path)
    assert in_process_network_precondition() is not None

    token = current_tool_context.set(
        ToolContext(
            sandbox_run_context=RunContext(run_mode=RunMode.SAFE, workspace=str(tmp_path)),
            session_key=None,
            workspace_dir=str(tmp_path),
        )
    )
    try:
        assert in_process_network_precondition() is None
        assert not await _query_denied(tmp_path)
    finally:
        current_tool_context.reset(token)


def test_no_runtime_yet_means_the_question_has_no_answer() -> None:
    assert effective_network_mode("web.fetch") is None
    assert in_process_network_precondition() is None


def test_the_mode_comes_from_the_graded_level_not_the_configured_run_mode(
    tmp_path: Path,
) -> None:
    # Deriving the posture from configuration alone is what made an earlier
    # attempt answer "ready" for the very configuration in the report: grading
    # decides the level, and the level decides the mode.
    configure_runtime(_POSTURES["recommended"], workspace=tmp_path)
    graded = effective_network_mode("web.fetch")

    reset_runtime()
    configure_runtime(_POSTURES["no-managed-network"], workspace=tmp_path)
    ungranted = effective_network_mode("web.fetch")

    assert graded == NetworkMode.PROXY_ALLOWLIST
    assert ungranted == NetworkMode.NONE


def test_a_non_network_action_is_not_reported_as_network_blocked(tmp_path: Path) -> None:
    configure_runtime(_POSTURES["no-managed-network"], workspace=tmp_path)

    assert in_process_network_precondition("fs.read") is None


def test_a_probe_that_cannot_resolve_reports_no_answer_instead_of_raising(
    monkeypatch, tmp_path: Path
) -> None:
    # This runs inside `search.status`, which the CLI table and the Control UI
    # Overview both reach. A posture probe that raises would turn a readiness
    # request into an error response — strictly worse than the wrong-but-quiet
    # readiness this change set out to fix.
    from opensquilla.sandbox import integration

    configure_runtime(_POSTURES["recommended"], workspace=tmp_path)

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("posture resolution unavailable on this host")

    monkeypatch.setattr(integration, "build_policy", _explode)

    assert effective_network_mode("web.fetch") is None
    assert in_process_network_precondition() is None
