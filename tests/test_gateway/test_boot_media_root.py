"""The gateway service builder wires the media root into the session manager.

Fork material copy depends on ``SessionManager`` knowing where attachment/artifact
material lives. The kwarg defaults to ``None`` (a silent no-op), so a regression that
drops it from ``build_services`` would disable forked-conversation previews with no
other test failure. This pins the production wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.gateway.boot import build_services
from opensquilla.gateway.config import GatewayConfig
from opensquilla.paths import media_root_from_config
from opensquilla.sandbox.integration import reset_runtime


@pytest.fixture(autouse=True)
def _drop_sandbox_runtime():
    """build_services configures the process-wide SandboxRuntime; drop it.

    Without this, the runtime (with the config's network mode) leaks into
    every later test in the session — e.g. the search RPC tests in
    test_rpc_product_cli_gaps.py got SandboxDenied under PROXY_ALLOWLIST.
    """
    yield
    reset_runtime()


@pytest.mark.asyncio
async def test_build_services_wires_media_root_into_session_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Keep the build hermetic: redirect all state off the real user home.
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))

    def fail_background_sandbox_setup(coro):
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        raise AssertionError("unit tests must not schedule real sandbox setup")

    monkeypatch.setattr(
        "opensquilla.gateway.boot.create_background_task",
        fail_background_sandbox_setup,
    )

    media = tmp_path / "media"
    config = GatewayConfig(
        memory={"flush_enabled": False},
        attachments={"media_root": str(media)},
        sandbox={"auto_setup": False},
    )

    services = await build_services(
        config=config, session_db_path=":memory:", seed_agent_workspaces=False
    )
    try:
        assert services.session_manager is not None
        media_root = services.session_manager._media_root
        assert media_root is not None
        assert media_root == media_root_from_config(config)
    finally:
        await services.close()


@pytest.mark.asyncio
async def test_build_services_reconciles_artifact_mutations_before_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))
    calls: list[tuple[object, Path]] = []

    def reject_background_task(coro):
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        raise AssertionError("unit tests must not schedule real sandbox setup")

    async def fake_reconcile(service, store):
        calls.append((service, store.media_root))
        return type(
            "Summary",
            (),
            {
                "examined": 0,
                "applied": 0,
                "failed": 0,
                "ambiguous": 0,
                "deleted_candidates": 0,
            },
        )()

    monkeypatch.setattr(
        "opensquilla.gateway.boot.create_background_task",
        reject_background_task,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.artifact_mutation_recovery.reconcile_pending_artifact_mutations",
        fake_reconcile,
    )
    media = tmp_path / "media"
    services = await build_services(
        config=GatewayConfig(
            memory={"flush_enabled": False},
            attachments={"media_root": str(media)},
            sandbox={"auto_setup": False},
        ),
        session_db_path=":memory:",
        seed_agent_workspaces=False,
    )
    try:
        assert len(calls) == 1
        assert calls[0][1] == media
    finally:
        await services.close()
