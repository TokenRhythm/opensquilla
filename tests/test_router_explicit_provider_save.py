"""Explicit Router deployments survive saves and execute on their own endpoint."""

from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path
from typing import Any

import httpx
import pytest
import tomli_w

from opensquilla.engine.selector_override import (
    apply_model_override,
    cross_provider_tier_config,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.config_migration import LATEST_CONFIG_VERSION
from opensquilla.gateway.provider_runtime import resolve_provider_selector_config
from opensquilla.onboarding.config_store import load_config, persist_config
from opensquilla.onboarding.mutations import upsert_router
from opensquilla.onboarding.router_policy import RouterProviderConflictError
from opensquilla.provider.selector import ModelSelector, SelectorConfig
from opensquilla.provider.types import ChatConfig, DoneEvent, ErrorEvent, Message

PRIMARY_KEY = "synthetic-primary-key"
SECONDARY_KEY = "synthetic-secondary-key"
PRIMARY_ENDPOINT = "https://primary-router.test/v1"
SECONDARY_ENDPOINT = "https://secondary-router.test/api/v1"
FOREIGN_MODEL = "moonshotai/kimi-k3.1-pro"


def _saved_config(
    path: Path,
    *,
    cross_provider: bool | None,
    foreign_c3: bool = False,
    enabled: bool = True,
    config_version: int = LATEST_CONFIG_VERSION,
) -> GatewayConfig:
    router: dict[str, Any] = {
        "enabled": enabled,
        "preset_binding": "custom",
        "rollout_phase": "full",
        "tiers": {
            f"c{index}": {"provider": "tokenrhythm", "model": f"primary-c{index}"}
            for index in range(4)
        },
    }
    if cross_provider is not None:
        router["cross_provider_tiers"] = cross_provider
    if foreign_c3:
        router["tiers"]["c3"] = {"provider": "openrouter", "model": FOREIGN_MODEL}
    path.write_text(
        tomli_w.dumps({
            "config_version": config_version,
            "llm": {
                "provider": "tokenrhythm",
                "model": "primary-direct",
                "api_key": PRIMARY_KEY,
                "base_url": PRIMARY_ENDPOINT,
            },
            "llm_profiles": {
                "openrouter": {"api_key": SECONDARY_KEY, "base_url": SECONDARY_ENDPOINT},
            },
            "squilla_router": router,
            "llm_ensemble": {"enabled": False, "selection_mode": "static_openrouter_b5"},
        }),
        encoding="utf-8",
    )
    return load_config(path)


def _foreign_tiers(config: GatewayConfig) -> dict[str, Any]:
    tiers = copy.deepcopy(config.squilla_router.tiers)
    tiers["c3"] = {"provider": "openrouter", "model": FOREIGN_MODEL}
    return tiers


@pytest.mark.parametrize("saved_flag", [None, False, True], ids=["missing", "false", "true"])
@pytest.mark.parametrize("was_enabled", [False, True], ids=["enable", "edit-enabled"])
def test_explicit_foreign_deployment_save_persists_across_reload(
    tmp_path: Path, saved_flag: bool | None, was_enabled: bool,
) -> None:
    path = tmp_path / "config.toml"
    config = _saved_config(path, cross_provider=saved_flag, enabled=was_enabled)
    original_primary = config.llm.model_dump()
    original_router = config.squilla_router.model_dump()

    result = upsert_router(config, mode="custom", tiers=_foreign_tiers(config))

    assert result.config.squilla_router.cross_provider_tiers is True
    assert result.public_payload["cross_provider_tiers"] is True
    assert result.config.llm.model_dump() == original_primary
    assert config.squilla_router.model_dump() == original_router
    persist_config(result.config, path=path)

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    assert raw["squilla_router"]["cross_provider_tiers"] is True
    restored = load_config(path)
    assert restored.squilla_router.cross_provider_tiers is True
    assert restored.squilla_router.enabled is True
    assert restored.squilla_router.preset_binding == "custom"
    assert restored.squilla_router.tiers["c3"]["provider"] == "openrouter"
    assert restored.squilla_router.tiers["c3"]["model"] == FOREIGN_MODEL
    assert restored.llm.model_dump() == original_primary


@pytest.mark.parametrize("saved_flag", [None, False, True], ids=["missing", "false", "true"])
def test_upgraded_config_enables_explicit_foreign_route_without_changing_load_semantics(
    tmp_path: Path, saved_flag: bool | None,
) -> None:
    path = tmp_path / "config.toml"
    config = _saved_config(path, cross_provider=saved_flag, config_version=1)
    assert config.squilla_router.cross_provider_tiers is (saved_flag is True)
    migrated = tomllib.loads(path.read_text(encoding="utf-8"))
    assert migrated["config_version"] == LATEST_CONFIG_VERSION

    result = upsert_router(config, mode="custom", tiers=_foreign_tiers(config))
    persist_config(result.config, path=path)

    restored = load_config(path)
    assert restored.squilla_router.cross_provider_tiers is True
    assert restored.squilla_router.tiers["c3"]["provider"] == "openrouter"
    assert restored.squilla_router.tiers["c3"]["model"] == FOREIGN_MODEL


@pytest.mark.parametrize("change_model", [False, True], ids=["same-table", "new-model"])
def test_explicit_legacy_foreign_table_adopts_its_selected_provider(
    tmp_path: Path, change_model: bool,
) -> None:
    config = _saved_config(tmp_path / "config.toml", cross_provider=False, foreign_c3=True)
    tiers = copy.deepcopy(config.squilla_router.tiers)
    if change_model:
        tiers["c3"]["model"] = "openrouter/replacement-model"

    result = upsert_router(config, mode="custom", tiers=tiers)

    assert result.config.squilla_router.cross_provider_tiers is True
    assert result.config.squilla_router.tiers["c3"]["model"] == tiers["c3"]["model"]


def test_unrelated_default_tier_save_preserves_legacy_execution(tmp_path: Path) -> None:
    config = _saved_config(tmp_path / "config.toml", cross_provider=False, foreign_c3=True)

    result = upsert_router(config, mode="custom", default_tier="c2")

    assert result.config.squilla_router.cross_provider_tiers is False
    for tier_name in ("c0", "c1", "c2", "c3"):
        for field in ("provider", "model"):
            assert (
                result.config.squilla_router.tiers[tier_name][field]
                == config.squilla_router.tiers[tier_name][field]
            )


def test_enabling_legacy_foreign_router_without_explicit_tiers_still_conflicts(
    tmp_path: Path,
) -> None:
    config = _saved_config(
        tmp_path / "config.toml", cross_provider=False, foreign_c3=True, enabled=False,
    )

    with pytest.raises(RouterProviderConflictError):
        upsert_router(config, mode="custom")


def test_explicit_execution_opt_out_is_not_overridden(tmp_path: Path) -> None:
    config = _saved_config(tmp_path / "config.toml", cross_provider=False)

    with pytest.raises(RouterProviderConflictError):
        upsert_router(
            config, mode="custom", tiers=_foreign_tiers(config), cross_provider_tiers=False,
        )


@pytest.mark.parametrize("selection_mode", ["static_openrouter_b5", "router_dynamic"])
def test_shared_c3_only_enables_cross_provider_when_it_consumes_the_tier(
    tmp_path: Path, selection_mode: str,
) -> None:
    config = _saved_config(tmp_path / "config.toml", cross_provider=False)
    config.llm_ensemble.selection_mode = selection_mode
    tiers = _foreign_tiers(config)
    tiers["c3"]["ensemble_enabled"] = True

    result = upsert_router(config, mode="custom", tiers=tiers)

    assert result.config.squilla_router.cross_provider_tiers is (
        selection_mode == "router_dynamic"
    )


def test_legacy_image_draft_does_not_enable_cross_provider_execution(tmp_path: Path) -> None:
    config = _saved_config(tmp_path / "config.toml", cross_provider=False)
    tiers = copy.deepcopy(config.squilla_router.tiers)
    tiers["image_model"] = {"provider": "openrouter", "model": "unused-image-model"}

    result = upsert_router(config, mode="custom", tiers=tiers)

    assert result.config.squilla_router.cross_provider_tiers is False


@pytest.mark.parametrize("saved_flag", [None, False, True], ids=["missing", "false", "true"])
async def test_reloaded_router_sends_each_tier_to_its_selected_endpoint_and_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, saved_flag: bool | None,
) -> None:
    path = tmp_path / "config.toml"
    config = _saved_config(path, cross_provider=saved_flag)
    result = upsert_router(config, mode="custom", tiers=_foreign_tiers(config))
    persist_config(result.config, path=path)
    restored = load_config(path)
    captured: list[tuple[str, str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        captured.append((str(request.url), request.headers["authorization"], model))
        chunks = [
            {"model": model, "choices": [{"delta": {"content": "ok"}, "finish_reason": None}]},
            {
                "model": model,
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        ]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=body + "data: [DONE]\n\n",
        )

    transport = httpx.MockTransport(respond)
    real_async_client = httpx.AsyncClient

    def mock_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        kwargs["proxy"] = None
        kwargs["trust_env"] = False
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", mock_client)

    for tier_name in ("c1", "c3"):
        primary = resolve_provider_selector_config(restored)
        assert primary is not None
        selector = ModelSelector(SelectorConfig(primary=primary))
        tier = restored.squilla_router.tiers[tier_name]
        metadata: dict[str, Any] = {
            "routing_applied": True,
            "routed_tier": tier_name,
            "routed_provider": tier["provider"],
            "routed_model": tier["model"],
        }
        deployment = cross_provider_tier_config(
            restored, metadata, tier["model"], active_provider_id=primary.provider,
        )
        provider = apply_model_override(
            selector, tier["model"], turn_metadata=metadata,
            realign_routed_model=False, tier_provider_config=deployment,
        )
        events = [
            event async for event in provider.chat(
                [Message(role="user", content="synthetic routing check")],
                config=ChatConfig(max_tokens=16),
            )
        ]
        assert not any(isinstance(event, ErrorEvent) for event in events)
        assert any(isinstance(event, DoneEvent) for event in events)
        assert metadata["executed_provider"] == tier["provider"]
        assert metadata["executed_model"] == tier["model"]

    assert captured == [
        (f"{PRIMARY_ENDPOINT}/chat/completions", f"Bearer {PRIMARY_KEY}", "primary-c1"),
        (f"{SECONDARY_ENDPOINT}/chat/completions", f"Bearer {SECONDARY_KEY}", FOREIGN_MODEL),
    ]
