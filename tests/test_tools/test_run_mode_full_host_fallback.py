"""Sandbox-disabled Full Host Access fallback and its opt-out lever.

A runtime configured with ``sandbox=False`` implies Full Host Access by
default. OPENSQUILLA_SANDBOX_DISABLED_FULL_HOST=off suppresses only that
fallback so run-mode semantics come from the tool context alone and the
workspace policy layers stay active; explicit Full run mode is unaffected.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.run_mode import RunMode
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, reset_runtime
from opensquilla.tools.run_mode import (
    current_run_mode,
    effective_run_mode_for_context,
    full_host_access_active,
    full_host_access_for_context,
    trusted_sandbox_active,
)
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context

_ENV = "OPENSQUILLA_SANDBOX_DISABLED_FULL_HOST"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(_ENV, raising=False)
    yield


@pytest.fixture
def disabled_sandbox_runtime(tmp_path: Path):
    reset_runtime()
    configure_runtime(
        SandboxSettings(
            sandbox=False,
            security_grading=False,
            allow_legacy_mode=True,
        ),
        workspace=tmp_path,
    )
    yield
    reset_runtime()


@pytest.fixture
def bypass_context(tmp_path: Path):
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        workspace_dir=str(tmp_path),
        elevated="bypass",
    )
    token = current_tool_context.set(ctx)
    try:
        yield ctx
    finally:
        current_tool_context.reset(token)


def test_disabled_sandbox_grants_full_host_by_default(
    disabled_sandbox_runtime, bypass_context
):
    assert current_run_mode() == "safe"
    assert full_host_access_active() is True
    assert full_host_access_for_context(bypass_context) is True
    assert effective_run_mode_for_context(bypass_context) is RunMode.FULL


def test_disabled_sandbox_never_upgrades_guest_to_full_host(
    disabled_sandbox_runtime,
    tmp_path: Path,
) -> None:
    guest = ToolContext(
        is_owner=False,
        guest_safe=True,
        caller_kind=CallerKind.AGENT,
        workspace_dir=str(tmp_path),
        run_mode="safe",
    )

    assert full_host_access_for_context(guest) is False
    assert effective_run_mode_for_context(guest) is RunMode.SAFE


def test_effective_mode_reads_persisted_sandbox_run_context(tmp_path: Path) -> None:
    reset_runtime()
    ctx = ToolContext(
        workspace_dir=str(tmp_path),
        run_mode=None,
        sandbox_run_context=SimpleNamespace(run_mode=RunMode.FULL),
    )

    assert effective_run_mode_for_context(ctx) is RunMode.FULL


def test_effective_mode_reads_session_authorization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reset_runtime()
    queue = SimpleNamespace(get_run_mode=lambda _session_key: "full")
    monkeypatch.setattr(
        "opensquilla.gateway.approval_queue.get_approval_queue",
        lambda: queue,
    )
    ctx = ToolContext(
        workspace_dir=str(tmp_path),
        session_key="agent:main:legacy-approved",
    )

    assert effective_run_mode_for_context(ctx) is RunMode.FULL


def test_effective_mode_keeps_legacy_safe_session_over_full_runtime_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    queue = SimpleNamespace(get_run_mode=lambda _session_key: "safe")
    runtime = SimpleNamespace(
        effective=SimpleNamespace(sandbox_enabled=True),
        default_run_mode="full",
    )
    monkeypatch.setattr(
        "opensquilla.gateway.approval_queue.get_approval_queue",
        lambda: queue,
    )
    monkeypatch.setattr(
        "opensquilla.sandbox.integration.get_runtime",
        lambda: runtime,
    )
    ctx = ToolContext(
        workspace_dir=str(tmp_path),
        session_key="agent:main:legacy-safe",
    )

    assert effective_run_mode_for_context(ctx) is RunMode.SAFE
    assert ctx.run_mode == RunMode.SAFE.value


def test_guest_context_rejects_even_a_forged_full_run_mode(
    disabled_sandbox_runtime,
    tmp_path: Path,
) -> None:
    guest = ToolContext(
        is_owner=False,
        guest_safe=True,
        caller_kind=CallerKind.AGENT,
        workspace_dir=str(tmp_path),
        run_mode="full",
    )
    token = current_tool_context.set(guest)
    try:
        assert full_host_access_active() is False
    finally:
        current_tool_context.reset(token)


def test_opt_out_keeps_context_run_mode_semantics(
    monkeypatch: pytest.MonkeyPatch, disabled_sandbox_runtime, bypass_context
):
    monkeypatch.setenv(_ENV, "off")
    assert current_run_mode() == "safe"
    assert full_host_access_active() is False
    assert full_host_access_for_context(bypass_context) is False
    assert trusted_sandbox_active() is True


def test_opt_out_preserves_explicit_full_mode(
    monkeypatch: pytest.MonkeyPatch, disabled_sandbox_runtime, bypass_context
):
    monkeypatch.setenv(_ENV, "off")
    bypass_context.run_mode = "full"
    assert full_host_access_active() is True
    assert full_host_access_for_context(bypass_context) is True


def test_unrecognized_value_fails_safe_to_default(
    monkeypatch: pytest.MonkeyPatch, disabled_sandbox_runtime, bypass_context
):
    monkeypatch.setenv(_ENV, "of")
    assert full_host_access_active() is True


def test_opt_out_without_runtime_still_false(
    monkeypatch: pytest.MonkeyPatch, bypass_context
):
    monkeypatch.setenv(_ENV, "off")
    reset_runtime()
    assert full_host_access_active() is False
