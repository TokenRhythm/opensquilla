"""Cold-start provider hot-apply regression tests.

Pins the fix for gateways that boot before any provider credentials exist.
``build_services`` now always constructs the ``ModelSelector`` (unconfigured
when no usable key is present) so that a Web UI / RPC config edit can bring
it live in place via ``sync_primary``. Previously the selector stayed
``None`` forever: the config file was saved, onboarding reported
``restart_required=False``, the connectivity probe passed — yet every turn
failed with "No provider available" until a full gateway restart.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.provider_runtime import sync_provider_selector
from opensquilla.gateway.provider_runtime import (
    sync_provider_selector as config_sync_provider_selector,
)
from opensquilla.gateway.rpc.registry import RpcContext, _status
from opensquilla.provider.selector import (
    ModelSelector,
    ProviderConfig,
    SelectorConfig,
)


def _cold_boot_selector() -> ModelSelector:
    """The selector shape build_services constructs when no API key exists."""
    return ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="openrouter",
                model="deepseek/deepseek-v4-flash",
                api_key="",
            )
        )
    )


def _ctx(selector: ModelSelector, config: GatewayConfig | None = None) -> RpcContext:
    return RpcContext(
        conn_id="c",
        principal=SimpleNamespace(role="operator"),
        provider_selector=selector,
        config=config,
    )


def test_unconfigured_selector_yields_clean_no_provider() -> None:
    """Before a key arrives, turns fail with no_provider — not an exception."""
    runner = TurnRunner(provider_selector=_cold_boot_selector())
    assert runner._resolve_provider() == (None, None)


def test_config_sync_brings_cold_boot_selector_live_without_restart() -> None:
    selector = _cold_boot_selector()
    runner = TurnRunner(provider_selector=selector)
    assert runner._resolve_provider() == (None, None)

    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-flash",
            "api_key": "test-key",
        }
    )
    config_sync_provider_selector(_ctx(selector, config), config)

    assert selector.is_configured is True
    provider, cloned = runner._resolve_provider()
    assert provider is not None
    assert cloned is not None
    assert cloned.active_provider_id == "openrouter"


def test_shared_runtime_sync_brings_cold_boot_selector_live_without_restart() -> None:
    selector = _cold_boot_selector()
    runner = TurnRunner(provider_selector=selector)
    assert runner._resolve_provider() == (None, None)

    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-flash",
            "api_key": "test-key",
        }
    )
    sync_provider_selector(_ctx(selector, config), config)

    assert selector.is_configured is True
    provider, _ = runner._resolve_provider()
    assert provider is not None


async def test_status_hides_provider_until_configured() -> None:
    selector = _cold_boot_selector()

    before = await _status(None, _ctx(selector))
    assert before["provider"] is None

    selector.sync_primary(
        ProviderConfig(
            provider="openrouter",
            model="deepseek/deepseek-v4-flash",
            api_key="test-key",
        )
    )

    after = await _status(None, _ctx(selector))
    assert after["provider"] == "openrouter"


async def test_build_services_constructs_unconfigured_selector_without_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keyless boot must still yield a selector for hot-apply to mutate."""
    from opensquilla.gateway.boot import build_services
    from opensquilla.provider.model_catalog import set_shared_catalog
    from opensquilla.sandbox.integration import reset_runtime

    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))
    # A developer checkout may carry a repo-local .env with real keys;
    # load_env would re-inject them mid-boot and make the selector
    # configured. Run from an empty cwd so the cold-boot state is real.
    monkeypatch.chdir(tmp_path)

    def fail_background_sandbox_setup(coro):
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        raise AssertionError("unit tests must not schedule real sandbox setup")

    monkeypatch.setattr(
        "opensquilla.gateway.boot.create_background_task",
        fail_background_sandbox_setup,
    )

    config = GatewayConfig(
        memory={"flush_enabled": False},
    )
    services = await build_services(
        config=config, session_db_path=":memory:", seed_agent_workspaces=False
    )
    try:
        selector = services.provider_selector
        assert selector is not None
        assert selector.is_configured is False

        selector.sync_primary(
            ProviderConfig(provider="openrouter", model="m", api_key="test-key")
        )
        assert selector.is_configured is True
    finally:
        await services.close()
        set_shared_catalog(None)
        reset_runtime()
