"""Boot-time live-catalog warm gating (``_warm_model_catalog_and_pricing``).

The live-listing fetch is keyless, so credential stripping alone cannot keep
the default offline suite off the network — the warm must be gated on the
primary provider's resolved credential. This guards that invariant: a boot
without an API key performs zero live-catalog fetches.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from opensquilla.gateway.boot import build_services
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider.model_catalog import set_shared_catalog
from opensquilla.sandbox.integration import reset_runtime


@pytest.fixture(autouse=True)
def _clear_shared_catalog():
    set_shared_catalog(None)
    yield
    set_shared_catalog(None)


@pytest.fixture(autouse=True)
def _drop_sandbox_runtime():
    yield
    reset_runtime()


def _deny_background_sandbox_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_background_sandbox_setup(coro):
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        raise AssertionError("unit tests must not schedule real sandbox setup")

    monkeypatch.setattr(
        "opensquilla.gateway.boot.create_background_task",
        fail_background_sandbox_setup,
    )


@pytest.mark.asyncio
async def test_keyless_boot_never_fetches_live_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))

    _deny_background_sandbox_setup(monkeypatch)

    fetches: list[tuple[Any, ...]] = []

    async def recording_fetch(*args: Any, **kwargs: Any) -> dict:
        fetches.append(args)
        return {}

    # warm_live_provider_catalogs resolves the fetch through its module
    # global, so this interception observes any attempted listing fetch.
    monkeypatch.setattr(
        "opensquilla.provider.live_catalog.fetch_live_catalog_entries",
        recording_fetch,
    )

    # tokenrhythm's spec names a live catalog URL, but conftest strips all
    # provider credentials — the boot warm must therefore skip it entirely.
    config = GatewayConfig(
        llm={"provider": "tokenrhythm", "model": "deepseek-v4-pro"},
        memory={"flush_enabled": False},
        sandbox={"auto_setup": False},
    )

    services = await build_services(
        config=config, session_db_path=":memory:", seed_agent_workspaces=False
    )
    try:
        assert fetches == []
        # Budgets fall back to the packaged corrections rows, which mirror
        # the platform listing, so keyless boots still budget correctly.
        assert services.model_catalog is not None
        window = services.model_catalog.resolve_context_window(
            "deepseek-v4-pro", "tokenrhythm"
        )
        assert window == 1_000_000
    finally:
        await services.close()


@pytest.mark.asyncio
async def test_configured_boot_ingests_live_qwen_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))
    _deny_background_sandbox_setup(monkeypatch)

    fetches: list[tuple[Any, ...]] = []

    async def fake_fetch(*args: Any, **kwargs: Any) -> dict:
        fetches.append(args)
        return {
            "qwen3.7-max": {
                "context_window": 1_000_000,
                "max_output_tokens": 131_072,
            }
        }

    monkeypatch.setattr(
        "opensquilla.provider.live_catalog.fetch_live_catalog_entries",
        fake_fetch,
    )
    config = GatewayConfig(
        llm={
            "provider": "tokenrhythm",
            "model": "qwen3.7-max",
            "api_key": "dummy-tokenrhythm-key",
        },
        memory={"flush_enabled": False},
        sandbox={"auto_setup": False},
    )

    services = await build_services(
        config=config, session_db_path=":memory:", seed_agent_workspaces=False
    )
    try:
        assert len(fetches) == 1
        assert services.model_catalog is not None
        entry = services.model_catalog.resolve_entry("qwen3.7-max", provider="tokenrhythm")
        assert entry.source == "live"
        assert services.model_catalog.resolve_max_tokens(
            "qwen3.7-max", provider="tokenrhythm"
        ) == 131_072
    finally:
        await services.close()


@pytest.mark.asyncio
async def test_desktop_deferred_warm_uses_key_saved_after_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENSQUILLA_DESKTOP_FAST_START", "1")
    _deny_background_sandbox_setup(monkeypatch)

    fetches: list[tuple[Any, ...]] = []

    async def fake_fetch(*args: Any, **kwargs: Any) -> dict:
        fetches.append(args)
        return {"qwen3.7-max": {"max_output_tokens": 131_072}}

    monkeypatch.setattr(
        "opensquilla.provider.live_catalog.fetch_live_catalog_entries",
        fake_fetch,
    )
    config = GatewayConfig(
        llm={"provider": "tokenrhythm", "model": "qwen3.7-max"},
        memory={"flush_enabled": False},
        sandbox={"auto_setup": False},
    )

    services = await build_services(
        config=config, session_db_path=":memory:", seed_agent_workspaces=False
    )
    try:
        assert fetches == []
        assert services.model_catalog is not None
        assert services.model_catalog.resolve_max_tokens(
            "qwen3.7-max", provider="tokenrhythm"
        ) == 131_072

        # Simulate the Web UI saving the key after desktop first paint but
        # before the deferred warmup runs.
        config.llm.api_key = "dummy-saved-after-build"
        warmup = next(
            item
            for item in services.deferred_warmups
            if getattr(item, "__name__", "") == "_warm_model_catalog_and_pricing"
        )
        await warmup()

        assert len(fetches) == 1
        assert services.model_catalog.resolve_entry(
            "qwen3.7-max", provider="tokenrhythm"
        ).source == "live"
    finally:
        await services.close()
