"""``[models.*]`` config overrides wired into the shared model catalog.

``ModelOverrideConfig`` (schema, ``gateway/config.py``) is flattened by
``model_override_entries`` into the ``ModelCatalog.set_user_overrides`` key
shape and installed onto the shared catalog at gateway boot
(``build_services``) and re-applied on every config hot-apply path
(``config.set``/``patch``/``apply``/``reload`` — see
``rpc_config._sync_model_catalog_overrides``), so ``resolve_model_price`` and
``get_capabilities`` honor operator cost/metadata overrides without a
restart.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.engine.pricing import PriceEntry, resolve_model_price
from opensquilla.gateway.boot import (
    apply_model_catalog_overrides,
    build_services,
    model_override_entries,
)
from opensquilla.gateway.config import GatewayConfig, ModelOverrideConfig
from opensquilla.gateway.rpc_config import _sync_model_catalog_overrides
from opensquilla.provider.model_catalog import ModelCatalog, set_shared_catalog, shared_catalog
from opensquilla.sandbox.integration import reset_runtime


@pytest.fixture(autouse=True)
def _clear_shared_catalog():
    set_shared_catalog(None)
    yield
    set_shared_catalog(None)


# ---------------------------------------------------------------------------
# model_override_entries — pure flattening helper
# ---------------------------------------------------------------------------


def test_model_override_entries_shape() -> None:
    cfg = GatewayConfig()
    cfg.models = {
        "deepseek": {
            "deepseek-v4-pro": ModelOverrideConfig(
                input_cost_per_mtok=0.2, cache_read_cost_per_mtok=0.002
            )
        }
    }
    entries = model_override_entries(cfg)
    assert entries == {
        "deepseek/deepseek-v4-pro": {
            "input_cost_per_mtok": 0.2,
            "cache_read_cost_per_mtok": 0.002,
        }
    }


def test_model_override_entries_drops_thinking_level_map() -> None:
    cfg = GatewayConfig()
    cfg.models = {
        "openrouter": {
            "z-ai/glm-5.2": ModelOverrideConfig(
                input_cost_per_mtok=0.5,
                thinking_level_map={"high": "high"},
            )
        }
    }
    entries = model_override_entries(cfg)
    assert entries == {"openrouter/z-ai/glm-5.2": {"input_cost_per_mtok": 0.5}}


def test_model_override_entries_skips_empty_overrides() -> None:
    """An all-None override (or one that only sets thinking_level_map) must
    not produce an empty entry — resolve_entry treats an empty dict as
    "no override" the same as no key at all, but skipping it here keeps
    set_user_overrides' installed map free of dead keys."""
    cfg = GatewayConfig()
    cfg.models = {"custom": {"idle-model": ModelOverrideConfig()}}
    assert model_override_entries(cfg) == {}


def test_model_override_entries_lowercases_keys() -> None:
    cfg = GatewayConfig()
    cfg.models = {"OpenRouter": {"Z-AI/GLM-5.2": ModelOverrideConfig(input_cost_per_mtok=0.5)}}
    entries = model_override_entries(cfg)
    assert entries == {"openrouter/z-ai/glm-5.2": {"input_cost_per_mtok": 0.5}}


# ---------------------------------------------------------------------------
# apply_model_catalog_overrides — resilient installation onto a catalog
# ---------------------------------------------------------------------------


def test_apply_model_catalog_overrides_installs_onto_catalog() -> None:
    cfg = GatewayConfig()
    cfg.models = {
        "deepseek": {
            "deepseek-v4-pro": ModelOverrideConfig(
                input_cost_per_mtok=0.2,
                output_cost_per_mtok=0.4,
                cache_read_cost_per_mtok=0.002,
            )
        }
    }
    catalog = ModelCatalog()

    apply_model_catalog_overrides(catalog, cfg)

    entry = catalog.resolve_entry("deepseek-v4-pro", provider="deepseek")
    assert entry.source == "user"
    assert entry.input_cost_per_mtok == pytest.approx(0.2)
    assert entry.output_cost_per_mtok == pytest.approx(0.4)
    assert entry.cache_read_cost_per_mtok == pytest.approx(0.002)


@pytest.mark.parametrize("provider", ["custom", "custom_anthropic"])
def test_apply_model_catalog_overrides_enables_custom_price_estimate(provider: str) -> None:
    config = GatewayConfig()
    config.models = {
        provider: {
            "vendor/priced-model": ModelOverrideConfig(
                input_cost_per_mtok=0.5,
                output_cost_per_mtok=2.0,
            )
        }
    }
    catalog = ModelCatalog()
    apply_model_catalog_overrides(catalog, config)
    set_shared_catalog(catalog)

    resolved = resolve_model_price("vendor/priced-model", provider=provider)

    assert resolved.source == "user_override"
    assert resolved.entry == PriceEntry(0.5, 2.0)


def test_apply_model_catalog_overrides_survives_bad_value_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ValueError from ModelCatalog.set_user_overrides (unknown field,
    type mismatch, ...) must not propagate — boot/hot-apply stays up — and
    must be logged rather than dropped silently."""

    class _ExplodingCatalog(ModelCatalog):
        def set_user_overrides(self, overrides):
            raise ValueError("invalid model catalog override for 'x/y': boom")

    cfg = GatewayConfig()
    cfg.models = {"x": {"y": ModelOverrideConfig(input_cost_per_mtok=0.1)}}
    catalog = _ExplodingCatalog()

    import structlog

    with structlog.testing.capture_logs() as captured:
        apply_model_catalog_overrides(catalog, cfg)  # must not raise

    warnings = [entry for entry in captured if entry["log_level"] == "warning"]
    assert warnings, "expected a structlog warning naming the bad override"
    assert any("boom" in str(entry.get("error", "")) for entry in warnings)


def test_apply_model_catalog_overrides_noop_for_empty_config() -> None:
    catalog = ModelCatalog()
    apply_model_catalog_overrides(catalog, GatewayConfig())
    entry = catalog.resolve_entry("some-model", provider="deepseek")
    assert entry.source != "user"


# ---------------------------------------------------------------------------
# Boot wiring — build_services installs overrides onto the shared catalog.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _drop_sandbox_runtime():
    yield
    reset_runtime()


@pytest.mark.asyncio
async def test_build_services_wires_model_overrides_into_shared_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    config = GatewayConfig(
        memory={"flush_enabled": False},
        sandbox={"auto_setup": False},
    )
    config.models = {
        "deepseek": {
            "deepseek-v4-pro": ModelOverrideConfig(
                input_cost_per_mtok=0.2, cache_write_cost_per_mtok=0.6
            )
        }
    }

    services = await build_services(
        config=config, session_db_path=":memory:", seed_agent_workspaces=False
    )
    try:
        assert services.model_catalog is not None
        assert shared_catalog() is services.model_catalog
        entry = services.model_catalog.resolve_entry("deepseek-v4-pro", provider="deepseek")
        assert entry.source == "user"
        assert entry.input_cost_per_mtok == pytest.approx(0.2)
        assert entry.cache_write_cost_per_mtok == pytest.approx(0.6)
    finally:
        await services.close()


# ---------------------------------------------------------------------------
# Hot-apply re-application (config.set/patch/apply/reload → rpc_config).
# ---------------------------------------------------------------------------


def test_sync_model_catalog_overrides_reapplies_onto_shared_catalog() -> None:
    catalog = ModelCatalog()
    set_shared_catalog(catalog)

    cfg = GatewayConfig()
    cfg.models = {
        "deepseek": {
            "deepseek-v4-pro": ModelOverrideConfig(input_cost_per_mtok=0.3),
        }
    }

    _sync_model_catalog_overrides(cfg)

    entry = shared_catalog().resolve_entry("deepseek-v4-pro", provider="deepseek")
    assert entry.source == "user"
    assert entry.input_cost_per_mtok == pytest.approx(0.3)
