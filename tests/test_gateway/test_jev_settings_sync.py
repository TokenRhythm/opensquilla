"""Profile JEV settings replace legacy chat preferences without a restart."""

from types import SimpleNamespace

import pytest

from opensquilla.application.app_settings import AppSettings, SettingChange
from opensquilla.gateway.adapters.app_settings import GatewayAppSettingsPort
from opensquilla.gateway.config import GatewayConfig
from opensquilla.jev_service import JevService


class LocalSettingsRuntime(GatewayAppSettingsPort):
    """Keep real config persistence and JEV state, omitting unrelated services."""

    def resolve_provider(self, config):
        return None

    def sync_provider(self, provider):
        pass

    async def refresh_catalog(self, previous, candidate, *, force=False):
        pass

    async def publish_routing(self, previous, candidate):
        pass


@pytest.fixture
def config(tmp_path):
    return GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        state_dir=str(tmp_path / "state"),
        memory={"retrieval_mode": "fts_only"},
        jev_router={"enabled": True, "mode": "suggest"},
    )


@pytest.mark.asyncio
async def test_profile_save_applies_mode_to_existing_and_new_sessions(config):
    service = JevService()
    runner = SimpleNamespace(jev_service=service)
    runtime = LocalSettingsRuntime(config, turn_runner=runner)
    await service.configure("legacy-chat", {"mode": "trust", "budgetPerHour": 1}, config.jev_router)
    try:
        await AppSettings(runtime).patch([SettingChange("jev_router.max_prompts_per_hour", 5)])

        existing = await service.settings("legacy-chat", config.jev_router)
        fresh = await service.settings("new-chat", config.jev_router)
        assert existing["mode"] == fresh["mode"] == "suggest"
        assert existing["max_prompts_per_hour"] == fresh["max_prompts_per_hour"] == 5
        assert GatewayConfig.load(config.config_path).jev_router.max_prompts_per_hour == 5
        assert service.defaults_provider().mode == "suggest"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_unrelated_save_preserves_legacy_preferences(config):
    service = JevService()
    runner = SimpleNamespace(jev_service=service)
    runtime = LocalSettingsRuntime(config, turn_runner=runner)
    await service.configure("legacy-chat", {"mode": "trust"}, config.jev_router)
    try:
        await AppSettings(runtime).patch([SettingChange("naming.enabled", False)])
        assert (await service.settings("legacy-chat", config.jev_router))["mode"] == "trust"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_same_value_explicit_profile_save_supersedes_legacy_mode(config):
    service = JevService()
    runner = SimpleNamespace(jev_service=service)
    runtime = LocalSettingsRuntime(config, turn_runner=runner)
    await service.configure("legacy-chat", {"mode": "trust"}, config.jev_router)
    try:
        await AppSettings(runtime).patch([SettingChange("jev_router.mode", "suggest")])
        assert (await service.settings("legacy-chat", config.jev_router))["mode"] == "suggest"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_profile_save_updates_stored_preferences_before_first_chat(config, tmp_path):
    stored = JevService(tmp_path / "jev.db")
    await stored.configure("legacy-chat", {"mode": "trust"}, config.jev_router)
    await stored.close()
    runner = SimpleNamespace()
    manager = SimpleNamespace(storage=SimpleNamespace(_db_path=tmp_path / "sessions.db"))
    runtime = LocalSettingsRuntime(config, turn_runner=runner, session_manager=manager)
    try:
        await AppSettings(runtime).patch([SettingChange("jev_router.mode", "suggest")])
        assert (await runner.jev_service.settings("legacy-chat", config.jev_router))["mode"] == (
            "suggest"
        )
    finally:
        if getattr(runner, "jev_service", None) is not None:
            await runner.jev_service.close()
