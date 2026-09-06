"""Settings policy and failure ordering exercised without an RPC handler."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from opensquilla.application.app_settings import AppSettings, SettingChange
from opensquilla.gateway.adapters.app_settings import GatewayAppSettingsPort
from opensquilla.gateway.config import GatewayConfig


class RecordingRuntime(GatewayAppSettingsPort):
    """Use real model/storage behavior, replacing only live process effects."""

    def __init__(self, config: GatewayConfig) -> None:
        super().__init__(config)
        self.events: list[str] = []
        self.fail_persist = False
        self.fail_replace = False
        self.fail_selector = False

    def persist(self, config: GatewayConfig) -> None:
        self.events.append("persist")
        if self.fail_persist:
            raise OSError("synthetic persist failure")
        super().persist(config)

    def replace(self, old: GatewayConfig, new: GatewayConfig) -> None:
        self.events.append("replace")
        if self.fail_replace:
            raise RuntimeError("synthetic replace failure")
        super().replace(old, new)

    def resolve_provider(self, config):
        self.events.append("resolve")
        return super().resolve_provider(config)

    def sync_provider(self, provider) -> None:
        self.events.append("selector")
        if self.fail_selector:
            raise RuntimeError("synthetic selector failure")

    def load(self, path: Path) -> GatewayConfig:
        self.events.append("load")
        return super().load(path)

    async def notify_goal(self, previous) -> None:
        self.events.append("goal")

    async def sync_runtime(self, previous, candidate) -> None:
        self.events.append("runtime")

    async def refresh_catalog(self, previous, candidate, *, force=False) -> None:
        self.events.append("catalog:force" if force else "catalog")

    async def publish_routing(self, previous, candidate) -> None:
        self.events.append("routing")

    async def reconcile_dream(self) -> bool:
        self.events.append("dream")
        return True


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RecordingRuntime:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "config.toml"
    path.write_text("[naming]\nenabled = false\n")
    return RecordingRuntime(GatewayConfig.load(path))


async def change_enabled(settings, operation: str):
    if operation == "set":
        return await settings.set("naming.enabled", True)
    if operation == "patch":
        return await settings.patch([SettingChange("naming.enabled", True)])
    if operation == "safe":
        return await settings.patch_safe([SettingChange("naming.enabled", True)])
    if operation == "merge":
        return await settings.merge({"naming": {"enabled": True}})
    if operation == "combined":
        return await settings.patch_combined(
            {"naming": {"enabled": True}}, {"naming.enabled": False}
        )
    return await settings.apply({"naming": {"enabled": True}})


async def test_reads_full_scalar_and_null_settings_shapes(runtime) -> None:
    settings = AppSettings(runtime)
    assert (await settings.read_all())["naming"]["enabled"] is False
    assert await settings.read(" naming.enabled ") is False
    assert await settings.read("naming.missing") is None
    assert "fields" in await settings.read_effective()


@pytest.mark.parametrize("operation", ["set", "patch", "safe", "merge", "combined", "apply"])
async def test_each_write_commits_once_before_live_state_changes(runtime, operation) -> None:
    settings = AppSettings(runtime)
    before_identity = runtime.config
    await change_enabled(settings, operation)

    assert runtime.config is before_identity
    assert runtime.config.naming.enabled is True
    assert tomllib.loads(Path(runtime.config.config_path).read_text())["naming"]["enabled"] is True
    assert runtime.events[:7] == [
        "resolve",
        "persist",
        "replace",
        "goal",
        "selector",
        "runtime",
        "catalog",
    ]
    assert runtime.events.count("persist") == 1


@pytest.mark.parametrize("operation", ["set", "patch", "safe", "merge", "combined", "apply"])
async def test_failed_persistence_preserves_disk_live_values_and_provenance(
    runtime, operation
) -> None:
    before = runtime.config.model_copy(deep=True)
    path = Path(runtime.config.config_path)
    original_bytes = path.read_bytes()
    runtime.fail_persist = True

    with pytest.raises(OSError, match="synthetic persist failure"):
        await change_enabled(AppSettings(runtime), operation)

    assert path.read_bytes() == original_bytes
    assert runtime.config.model_dump() == before.model_dump()
    assert runtime.config.runtime_field_overrides() == before.runtime_field_overrides()
    assert runtime.config._runtime_secret_paths == before._runtime_secret_paths
    assert runtime.events == ["resolve", "persist"]


@pytest.mark.parametrize("operation", ["set", "patch", "safe", "merge", "combined", "apply"])
async def test_replace_failure_keeps_durable_commit_and_stops_runtime_sync(runtime, operation):
    runtime.fail_replace = True
    before = runtime.config.model_copy(deep=True)
    with pytest.raises(RuntimeError, match="synthetic replace failure"):
        await change_enabled(AppSettings(runtime), operation)

    assert tomllib.loads(Path(runtime.config.config_path).read_text())["naming"]["enabled"] is True
    assert runtime.config.model_dump() == before.model_dump()
    assert runtime.config._runtime_secret_paths == before._runtime_secret_paths
    assert runtime.events == ["resolve", "persist", "replace"]


@pytest.mark.parametrize("operation", ["set", "patch", "safe", "merge", "combined", "apply"])
async def test_post_commit_selector_failure_preserves_committed_state(runtime, operation) -> None:
    runtime.fail_selector = True
    with pytest.raises(RuntimeError, match="synthetic selector failure"):
        await change_enabled(AppSettings(runtime), operation)

    assert runtime.config.naming.enabled is True
    assert tomllib.loads(Path(runtime.config.config_path).read_text())["naming"]["enabled"] is True
    assert runtime.events == ["resolve", "persist", "replace", "goal", "selector"]


async def test_combined_patch_orders_dotted_changes_then_merge_in_one_commit(runtime) -> None:
    result = await AppSettings(runtime).patch_combined(
        {"naming": {"enabled": False}}, {"naming.enabled": True}
    )
    assert runtime.config.naming.enabled is False
    assert result["patched"] == ["naming.enabled", "(merge)"]
    assert runtime.events.count("persist") == 1


async def test_safe_policy_is_enforced_by_application_without_gateway_handler(runtime) -> None:
    with pytest.raises(ValueError, match="Path is not safe for operator.write: llm.api_key"):
        await AppSettings(runtime).patch_safe([SettingChange("llm.api_key", "synthetic-key")])
    assert runtime.events == []


async def test_readonly_policy_covers_set_and_combined_patch(runtime) -> None:
    runtime.config.auth.token = "synthetic-token"
    settings = AppSettings(runtime)
    with pytest.raises(ValueError, match="Path is read-only: auth"):
        await settings.set("auth", {})
    result = await settings.patch_combined(
        {"auth": {"token": "synthetic-replacement"}, "naming": {"enabled": True}},
        {"auth.token": "synthetic-replacement"},
    )
    assert runtime.config.auth.token == "synthetic-token"
    assert runtime.config.naming.enabled is True
    assert result["patched"] == ["auth.token", "(merge)"]
    assert runtime.events.count("persist") == 1


async def test_duplicate_invalid_and_empty_changes_fail_before_storage(runtime) -> None:
    settings = AppSettings(runtime)
    with pytest.raises(ValueError, match="duplicate settings path"):
        await settings.patch(
            [SettingChange("naming.enabled", True), SettingChange("naming.enabled", False)]
        )
    with pytest.raises(ValueError, match="non-empty dotted path"):
        await settings.patch([SettingChange("naming..enabled", True)])
    with pytest.raises(ValueError, match="must not be empty"):
        await settings.merge({})
    assert runtime.events == []


async def test_reload_synchronizes_candidate_before_swap_and_never_writes(runtime) -> None:
    path = Path(runtime.config.config_path)
    path.write_text("[naming]\nenabled = true\n")
    disk = path.read_bytes()
    result = await AppSettings(runtime).reload()
    assert result["ok"] is True
    assert runtime.config.naming.enabled is True
    assert path.read_bytes() == disk
    assert runtime.events == [
        "load",
        "resolve",
        "selector",
        "replace",
        "goal",
        "runtime",
        "catalog:force",
    ]


async def test_reload_selector_failure_keeps_old_live_config_and_disk(runtime) -> None:
    path = Path(runtime.config.config_path)
    path.write_text("[naming]\nenabled = true\n")
    disk = path.read_bytes()
    runtime.fail_selector = True
    with pytest.raises(RuntimeError, match="synthetic selector failure"):
        await AppSettings(runtime).reload()
    assert runtime.config.naming.enabled is False
    assert path.read_bytes() == disk
    assert runtime.events == ["load", "resolve", "selector"]


async def test_reload_invalid_disk_returns_failure_without_replacing_live_state(runtime) -> None:
    path = Path(runtime.config.config_path)
    path.write_text("[naming]\nenabled = [\n")
    disk = path.read_bytes()
    result = await AppSettings(runtime).reload()
    assert result["ok"] is False
    assert result["path"] == str(path)
    assert runtime.config.naming.enabled is False
    assert path.read_bytes() == disk
    assert runtime.events == ["load"]


def test_legacy_secret_imports_refer_to_the_single_policy_implementation() -> None:
    from opensquilla.application import config_secrets
    from opensquilla.gateway import config_secrets as legacy

    assert legacy.restore_redacted_values is config_secrets.restore_redacted_values
    assert legacy.inherit_then_clear_explicit is config_secrets.inherit_then_clear_explicit
