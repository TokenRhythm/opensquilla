"""Application tests for Platform setup mutation boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.application.provider_setup import (
    ConfigurePrimaryProvider,
    ProviderSetup,
)
from opensquilla.application.setup_mutations import commit_setup_mutation


class FakeConfigPort:
    def __init__(self, events: list[str], *, fail_persist: bool = False) -> None:
        self.events = events
        self.fail_persist = fail_persist
        self.config = SimpleNamespace(name="active")

    def active_config(self) -> Any:
        return self.config

    def persist_candidate(self, candidate: Any, **_kwargs: Any) -> str:
        self.events.append("persist")
        if self.fail_persist:
            raise RuntimeError("disk unavailable")
        return "/tmp/config.toml"

    def install_candidate(self, candidate: Any) -> Any:
        self.events.append("install")
        self.config = candidate
        return candidate


class FakeRuntimePort:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def sync_primary_provider(self, _config: Any) -> None:
        self.events.append("sync-primary")

    async def sync_media(self, _config: Any) -> None:
        self.events.append("sync-media")

    async def sync_search(self, _config: Any) -> None:
        self.events.append("sync-search")

    async def refresh_model_catalog(self, _config: Any) -> None:
        self.events.append("refresh-catalog")

    async def broadcast_model_routing(self, _config: Any, *, source: str) -> None:
        self.events.append(f"broadcast:{source}")

    async def discard_profile_credentials(self, provider_id: str) -> None:
        self.events.append(f"discard:{provider_id}")

    async def reconcile_profile_transition(
        self,
        _previous_config: Any,
        _current_config: Any,
        *,
        provider_id: str,
    ) -> None:
        self.events.append(f"reconcile:{provider_id}")


class FakeProviderProbePort:
    async def probe_primary(self, _command: Any) -> dict[str, Any]:
        return {"ok": True}

    async def discover_primary_models(self, _command: Any) -> dict[str, Any]:
        return {"ok": True, "models": []}

    async def discover_image_models(self, provider_id: str) -> dict[str, Any]:
        return {"ok": True, "providerId": provider_id, "models": []}


class FakePrimaryProviderMutationPort:
    def __init__(self, events: list[str], candidate: Any) -> None:
        self.events = events
        self.candidate = candidate

    def configure_primary(
        self, _config: Any, _command: ConfigurePrimaryProvider
    ) -> SimpleNamespace:
        self.events.append("build")
        return mutation_result(self.candidate)


def mutation_result(candidate: Any) -> SimpleNamespace:
    return SimpleNamespace(
        config=candidate,
        restart_required=False,
        changed=True,
        public_payload={"api_key": "[redacted]"},
        warnings=(),
    )


@pytest.mark.asyncio
async def test_commit_setup_mutation_persists_before_install_and_effects() -> None:
    events: list[str] = []
    config = FakeConfigPort(events)
    candidate = SimpleNamespace(name="candidate")

    async def effect(_config: Any) -> None:
        events.append("effect")

    result = await commit_setup_mutation(
        mutation_result(candidate),
        config_port=config,
        effects=(effect,),
    )

    assert events == ["persist", "install", "effect"]
    assert result.config_path == "/tmp/config.toml"
    assert result.entry == {"api_key": "[redacted]"}


@pytest.mark.asyncio
async def test_commit_setup_mutation_persist_failure_leaves_runtime_unchanged() -> None:
    events: list[str] = []
    config = FakeConfigPort(events, fail_persist=True)

    async def effect(_config: Any) -> None:
        events.append("effect")

    with pytest.raises(RuntimeError, match="disk unavailable"):
        await commit_setup_mutation(
            mutation_result(SimpleNamespace(name="candidate")),
            config_port=config,
            effects=(effect,),
        )

    assert events == ["persist"]
    assert config.active_config().name == "active"


@pytest.mark.asyncio
async def test_provider_setup_reconciles_only_after_candidate_install() -> None:
    events: list[str] = []
    config = FakeConfigPort(events)
    runtime = FakeRuntimePort(events)
    candidate = SimpleNamespace(name="candidate")

    result = await ProviderSetup(
        config,
        runtime,
        FakeProviderProbePort(),
        FakePrimaryProviderMutationPort(events, candidate),
    ).configure_primary(ConfigurePrimaryProvider(provider_id="openai"))

    assert result.changed is True
    assert events == [
        "build",
        "persist",
        "install",
        "sync-primary",
        "sync-media",
        "refresh-catalog",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "persist", "pool", "router", "already_active"])
async def test_profile_save_and_activate_commits_once_before_all_runtime_effects(failure):
    from opensquilla.application.profile_lifecycle import ProfileLifecycle, UpsertAndActivateProfile
    from opensquilla.gateway.adapters.setup_mutations import OnboardingSetupMutationPort
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.onboarding.mutations import LlmProfileActivationError

    events = []
    config = FakeConfigPort(events, fail_persist=failure == "persist")
    config.config = GatewayConfig(
        llm={"provider": "openai", "api_key": "old-key"},
        squilla_router={"preset_binding": "custom", "tier_profile": "openai"}
        if failure == "router"
        else {"preset_binding": "follow_primary"},
    )
    before = config.config.model_dump()
    lifecycle = ProfileLifecycle(
        config, FakeRuntimePort(events), None, OnboardingSetupMutationPort()
    )
    command = UpsertAndActivateProfile(
        provider_id="openai" if failure == "already_active" else "deepseek",
        api_key="draft-key",
        api_key_env_pool=["POOL_A"] if failure == "pool" else None,
    )
    if failure:
        error = RuntimeError if failure == "persist" else LlmProfileActivationError
        with pytest.raises(error):
            await lifecycle.upsert_and_activate(command)
        assert events == (["persist"] if failure == "persist" else [])
        assert config.config.model_dump() == before
    else:
        result = await lifecycle.upsert_and_activate(command)
        assert events == [
            "persist",
            "install",
            "discard:deepseek",
            "reconcile:deepseek",
            "sync-primary",
            "sync-media",
            "refresh-catalog",
        ]
        assert result.entry["active"] is True
        assert config.config.llm.provider == "deepseek"
        assert config.config.llm.api_key == "draft-key"
        assert "deepseek" not in config.config.llm_profiles
