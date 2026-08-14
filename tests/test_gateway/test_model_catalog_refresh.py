"""TokenRhythm gateway snapshot/refresh contract tests."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

import pytest

from opensquilla.gateway.config import GatewayConfig, LlmProviderProfile
from opensquilla.gateway.model_catalog_refresh import TokenRhythmCatalogCoordinator
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.tokenrhythm_catalog import (
    parse_tokenrhythm_declared,
    parse_tokenrhythm_published,
)


class FakeClock:
    def __init__(self, value: float = 1_800_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _config(tmp_path: Path, *, key: str = "dummy-tokenrhythm-key") -> GatewayConfig:
    config = GatewayConfig(state_dir=str(tmp_path))
    config.llm.provider = "tokenrhythm"
    config.llm.model = "qwen3.8-max"
    config.llm.api_key = key
    config.llm.base_url = "https://tokenrhythm.studio/v1"
    config.llm.proxy = ""
    return config


def _profile_config(
    tmp_path: Path,
    *,
    key: str = "dummy-tokenrhythm-profile-key",
    proxy: str = "",
) -> GatewayConfig:
    return GatewayConfig(
        state_dir=str(tmp_path),
        llm={"provider": "openrouter", "model": "openai/gpt-test"},
        llm_profiles={
            "tokenrhythm": {
                "model": "qwen3.8-max",
                "api_key": key,
                "base_url": "https://tokenrhythm.studio/v1",
                "proxy": proxy,
            }
        },
    )


def _published(*, include_public_only: bool = False):
    rows = [
        {
            "id": "qwen3.8-max",
            "name": "Qwen 3.8 Max",
            "providerDisplayName": "Qwen",
            "type": "chat",
            "status": "online",
            "contextWindow": 1_000_000,
            "maxOutputTokens": 131_072,
            "capabilities": {
                "tools": True,
                "reasoning": True,
                "vision": False,
                "streaming": True,
            },
        }
    ]
    if include_public_only:
        rows.append(
            {
                "id": "public-only",
                "name": "Public only",
                "type": "chat",
                "status": "online",
                "contextWindow": 128_000,
                "maxOutputTokens": 8_000,
            }
        )
    return parse_tokenrhythm_published({"data": rows})


def _declared(*, empty: bool = False):
    rows = []
    if not empty:
        rows.append(
            {
                "id": "qwen3.8-max",
                "name": "Qwen 3.8 Max",
                "context_length": 1_000_000,
                # Top-level must win over the deliberately conflicting legacy field.
                "max_completion_tokens": 131_072,
                "top_provider": {"max_completion_tokens": 8_192},
                "supports_tools": False,
            }
        )
    return parse_tokenrhythm_declared({"data": rows})


def _patch_fetches(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    async def fetch_published(**_kwargs):
        calls.append("published")
        return _published(include_public_only=True)

    async def fetch_declared(*_args, **_kwargs):
        calls.append("declared")
        return _declared()

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        fetch_published,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        fetch_declared,
    )


def _profile_request(config: GatewayConfig):
    module = __import__(
        "opensquilla.gateway.model_catalog_refresh", fromlist=["_profile_requests"]
    )
    requests = module._profile_requests(config, "tokenrhythm")
    assert len(requests) == 1
    return next(iter(requests.values()))


@pytest.mark.asyncio
async def test_profile_identity_cleanup_is_local_and_preserves_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog())
    previous = _profile_config(tmp_path)
    await coordinator.hydrate(previous, activate=False)
    request = _profile_request(previous)
    await coordinator.discover(
        request,
        force=True,
        persist_entitlement=True,
        activate=False,
    )
    assert calls == ["published", "declared"]

    current = previous.model_copy(deep=True)
    current.llm_profiles["tokenrhythm"].api_key = ""

    async def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("profile cleanup must not access the network")

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        unexpected_fetch,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        unexpected_fetch,
    )
    await coordinator.reconcile_profile_transition(
        previous,
        current,
        provider_id="tokenrhythm",
    )

    assert coordinator._published.models
    assert coordinator._entitlements == {}
    assert coordinator._ephemeral_entitlements == {}
    payload = json.loads(
        (tmp_path / "model_catalog" / "tokenrhythm-v1.json").read_text()
    )
    assert payload["published"]["models"]
    assert payload["entitlements"] == {}


@pytest.mark.asyncio
async def test_active_provider_removal_cleans_all_historical_authorities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import opensquilla.gateway.model_catalog_refresh as refresh_module

    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    previous = _config(tmp_path, key="synthetic-active-key-a")
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog())
    await coordinator.refresh_active(previous)
    historical = refresh_module._tokenrhythm_request(
        provider="tokenrhythm",
        base_url="https://tokenrhythm.studio/v1",
        api_key="synthetic-historical-key-b",
        proxy="",
    )
    assert historical is not None
    await coordinator.discover(
        historical,
        force=True,
        persist_entitlement=True,
        activate=False,
    )
    assert len(coordinator._entitlements) == 2

    current = GatewayConfig(
        state_dir=str(tmp_path),
        llm={
            "provider": "openrouter",
            "model": "openai/gpt-test",
            "api_key": "synthetic-openrouter-key",
        },
    )

    async def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("authority cleanup must remain network-free")

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        unexpected_fetch,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        unexpected_fetch,
    )
    await coordinator.reconcile_profile_transition(
        previous,
        current,
        provider_id="tokenrhythm",
    )

    assert coordinator._entitlements == {}
    payload = json.loads(
        (tmp_path / "model_catalog" / "tokenrhythm-v1.json").read_text()
    )
    assert payload["published"]["models"]
    assert payload["entitlements"] == {}


@pytest.mark.asyncio
async def test_each_active_transition_prunes_unreachable_historical_authorities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import opensquilla.gateway.model_catalog_refresh as refresh_module

    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog())
    first = _config(tmp_path, key="synthetic-active-key-a")
    second = _config(tmp_path, key="synthetic-active-key-b")
    first_request = refresh_module._request_from_config(first)
    second_request = refresh_module._request_from_config(second)
    assert first_request is not None
    assert second_request is not None

    await coordinator.refresh_active(first)
    await coordinator.refresh_active(second, force=True)

    assert first_request.authority_identity not in coordinator._entitlements
    assert second_request.authority_identity in coordinator._entitlements

    switched = GatewayConfig(
        state_dir=str(tmp_path),
        llm={
            "provider": "openrouter",
            "model": "openai/gpt-test",
            "api_key": "synthetic-openrouter-key",
        },
    )
    await coordinator.refresh_active(switched)

    assert coordinator._published.models
    assert coordinator._entitlements == {}
    payload = json.loads(
        (tmp_path / "model_catalog" / "tokenrhythm-v1.json").read_text()
    )
    assert payload["published"]["models"]
    assert payload["entitlements"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_source", ["direct", "environment"])
async def test_restart_prunes_entitlement_when_credential_disappeared_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_source: str,
) -> None:
    env_name = "OPENSQUILLA_TEST_TOKENRHYTHM_RESTART_KEY"
    monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    config = _config(
        tmp_path,
        key=("synthetic-restart-direct-key" if credential_source == "direct" else ""),
    )
    if credential_source == "environment":
        monkeypatch.setenv(env_name, "synthetic-restart-environment-key")
        config.llm.api_key_env = env_name

    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    first = TokenRhythmCatalogCoordinator(ModelCatalog())
    await first.refresh_active(config)
    assert calls == ["published", "declared"]

    cleared = config.model_copy(deep=True)
    if credential_source == "direct":
        cleared.llm.api_key = ""
    else:
        monkeypatch.delenv(env_name)

    async def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("restart hydration after credential clear must be offline")

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        unexpected_fetch,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        unexpected_fetch,
    )
    restarted = TokenRhythmCatalogCoordinator(ModelCatalog())
    await restarted.hydrate(cleared, activate=False)
    assert len(restarted._entitlements) == 1
    await restarted.hydrate(cleared)

    assert restarted._published.models
    assert restarted._entitlements == {}
    assert restarted.cached(cleared) == []
    payload = json.loads(
        (tmp_path / "model_catalog" / "tokenrhythm-v1.json").read_text()
    )
    assert payload["published"]["models"]
    assert payload["entitlements"] == {}


@pytest.mark.asyncio
async def test_profile_proxy_change_keeps_lkg_and_fences_late_old_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog())
    previous = _profile_config(tmp_path, proxy="http://127.0.0.1:8001")
    await coordinator.hydrate(previous, activate=False)
    request = _profile_request(previous)
    await coordinator.discover(
        request,
        force=True,
        persist_entitlement=True,
        activate=False,
    )
    original = coordinator._entitlements[request.authority_identity]

    started = asyncio.Event()

    async def late_declared(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return _declared(empty=True)

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        late_declared,
    )
    refresh = asyncio.create_task(
        coordinator.discover(
            request,
            force=True,
            persist_entitlement=True,
            activate=False,
        )
    )
    await started.wait()
    current = previous.model_copy(deep=True)
    current.llm_profiles["tokenrhythm"].proxy = "http://127.0.0.1:8002"
    await coordinator.reconcile_profile_transition(
        previous,
        current,
        provider_id="tokenrhythm",
    )
    await refresh

    retained = coordinator._entitlements[request.authority_identity]
    assert retained.models == original.models
    assert retained.transport_fingerprint == original.transport_fingerprint


@pytest.mark.asyncio
async def test_profile_credential_clear_fences_late_declared_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog())
    previous = _profile_config(tmp_path)
    await coordinator.hydrate(previous, activate=False)
    request = _profile_request(previous)
    started = asyncio.Event()

    async def fetch_published(**_kwargs):
        return _published()

    async def late_declared(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return _declared()

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        fetch_published,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        late_declared,
    )
    refresh = asyncio.create_task(
        coordinator.discover(
            request,
            force=True,
            persist_entitlement=True,
            activate=False,
        )
    )
    await started.wait()
    current = previous.model_copy(deep=True)
    current.llm_profiles["tokenrhythm"].api_key = ""
    await coordinator.reconcile_profile_transition(
        previous,
        current,
        provider_id="tokenrhythm",
    )
    await refresh

    assert request.authority_identity not in coordinator._entitlements
    assert request.authority_identity not in coordinator._ephemeral_entitlements


@pytest.mark.asyncio
async def test_credential_clear_fences_refresh_queued_before_impl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import opensquilla.gateway.model_catalog_refresh as refresh_module

    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)

    class PausingCoordinator(TokenRhythmCatalogCoordinator):
        def __init__(self, catalog: ModelCatalog) -> None:
            super().__init__(catalog)
            self.queued = asyncio.Event()
            self.release = asyncio.Event()

        async def _refresh_impl(self, request, **kwargs):
            self.queued.set()
            await self.release.wait()
            return await super()._refresh_impl(request, **kwargs)

    coordinator = PausingCoordinator(ModelCatalog())
    previous = _config(tmp_path, key="synthetic-queued-old-key")
    queued = asyncio.create_task(coordinator.refresh_active(previous))
    await coordinator.queued.wait()

    cleared = previous.model_copy(deep=True)
    cleared.llm.api_key = ""
    await coordinator.refresh_active(cleared)
    assert coordinator._active_authority == ""
    assert coordinator._entitlements == {}
    assert calls == []

    coordinator.release.set()
    assert await queued == {"tokenrhythm": 0}
    assert coordinator._active_authority == ""
    assert coordinator._entitlements == {}
    assert calls == []
    payload = json.loads(
        (tmp_path / "model_catalog" / "tokenrhythm-v1.json").read_text()
    )
    assert payload["entitlements"] == {}

    # A caller cancellation before the child reaches its implementation must
    # also release the provisional transport-to-authority registration.
    request = refresh_module._request_from_config(previous)
    assert request is not None
    cancelled_coordinator = PausingCoordinator(ModelCatalog())
    cancelled = asyncio.create_task(
        cancelled_coordinator.discover(
            request,
            force=True,
            persist_entitlement=False,
            activate=False,
        )
    )
    await cancelled_coordinator.queued.wait()
    assert (
        cancelled_coordinator._declared_transport_authorities[
            request.transport_fingerprint
        ]
        == request.authority_identity
    )
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert request.transport_fingerprint not in (
        cancelled_coordinator._declared_transport_authorities
    )
    assert cancelled_coordinator._declared_operation_counts == {}
    await coordinator.close()
    await cancelled_coordinator.close()


@pytest.mark.asyncio
async def test_profile_cleanup_never_removes_matching_active_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog())
    active = _config(tmp_path, key="shared-dummy-key")
    active.llm_profiles["tokenrhythm"] = LlmProviderProfile(
        model="qwen3.8-max",
        api_key="shared-dummy-key",
        base_url="https://tokenrhythm.studio/v1",
    )
    await coordinator.refresh_active(active)
    request = _profile_request(active)

    current = active.model_copy(deep=True)
    current.llm_profiles.pop("tokenrhythm")
    await coordinator.reconcile_profile_transition(
        active,
        current,
        provider_id="tokenrhythm",
    )

    assert request.authority_identity in coordinator._entitlements
    assert [info.model_id for info in coordinator.cached(current)] == ["qwen3.8-max"]


@pytest.mark.asyncio
async def test_ttl_boundary_and_public_only_never_grants_entitlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    config = _config(tmp_path)

    assert await coordinator.refresh_active(config) == {"tokenrhythm": 1}
    assert calls == ["published", "declared"]
    infos = coordinator.cached(config)
    assert [info.model_id for info in infos] == ["qwen3.8-max"]
    assert infos[0].max_output_tokens == 131_072
    assert infos[0].supports_tools is False

    clock.value += 3599
    assert await coordinator.refresh_active(config) == {"tokenrhythm": 1}
    assert calls == ["published", "declared"]

    clock.value += 1
    assert await coordinator.refresh_active(config) == {"tokenrhythm": 1}
    assert calls == ["published", "declared", "published", "declared"]


@pytest.mark.asyncio
async def test_auth_only_unknown_limits_fall_back_to_safe_catalog_corrections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fetch_published(**_kwargs):
        return parse_tokenrhythm_published({"data": []})

    async def fetch_declared(*_args, **_kwargs):
        return parse_tokenrhythm_declared({"data": [{"id": "qwen3.8-max"}]})

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        fetch_published,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        fetch_declared,
    )
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog())
    config = _config(tmp_path)

    await coordinator.refresh_active(config)

    [info] = coordinator.cached(config)
    assert info.model_id == "qwen3.8-max"
    assert info.context_window == 1_000_000
    assert info.max_output_tokens == 131_072
    # Published/declared reasoning metadata may be true, but the legacy
    # top-level flag only advertises a request dialect OpenSquilla can execute.
    assert info.supports_reasoning is False
    assert info.supports_tools is True
    assert info.supports_vision is True
    assert info.metadata is not None
    assert info.metadata["published"] is None


@pytest.mark.asyncio
async def test_force_failure_uses_independent_last_good_and_marks_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    config = _config(tmp_path)
    await coordinator.refresh_active(config)

    async def fail_published(**_kwargs):
        calls.append("published-failed")
        raise OSError("synthetic public failure")

    async def fail_declared(*_args, **_kwargs):
        calls.append("declared-failed")
        raise OSError("synthetic auth failure")

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        fail_published,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        fail_declared,
    )
    clock.value += 10
    result = await coordinator.refresh_active(config, force=True)

    assert result == {"tokenrhythm": 1}
    assert [info.model_id for info in coordinator.cached(config)] == ["qwen3.8-max"]
    request = __import__(
        "opensquilla.gateway.model_catalog_refresh", fromlist=["_request_from_config"]
    )._request_from_config(config)
    assert request is not None
    view = await coordinator.discover(
        request,
        force=False,
        persist_entitlement=True,
        activate=True,
    )
    assert view.catalog["stale"] is True
    # The failed attempt is in the fixed five-minute backoff window.
    assert calls == [
        "published",
        "declared",
        "published-failed",
        "declared-failed",
    ]
    clock.value += 299
    await coordinator.discover(
        request,
        force=False,
        persist_entitlement=True,
        activate=True,
    )
    assert calls[-2:] == ["published-failed", "declared-failed"]
    clock.value += 1
    await coordinator.discover(
        request,
        force=False,
        persist_entitlement=True,
        activate=True,
    )
    assert calls[-2:] == ["published-failed", "declared-failed"]
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_public_failure_backoff_prevents_new_authority_alignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    first = _config(tmp_path, key="synthetic-alignment-key-a")
    await coordinator.refresh_active(first)

    async def fail_published(**_kwargs):
        raise OSError("synthetic public failure")

    async def fetch_declared(*_args, **_kwargs):
        return _declared()

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        fail_published,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        fetch_declared,
    )
    clock.value += 10
    await coordinator.refresh_active(first, force=True)
    second = _config(tmp_path, key="synthetic-alignment-key-b")
    refresh_module = __import__(
        "opensquilla.gateway.model_catalog_refresh",
        fromlist=["_request_from_config"],
    )
    request = refresh_module._request_from_config(second)
    assert request is not None
    view = await coordinator.discover(
        request,
        force=False,
        persist_entitlement=True,
        activate=False,
    )

    assert view.declared_available is True
    assert view.catalog == {"lastSyncedAt": None, "stale": True}


@pytest.mark.asyncio
async def test_switching_away_from_active_tokenrhythm_clears_its_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    catalog = ModelCatalog()
    coordinator = TokenRhythmCatalogCoordinator(catalog)
    active = _config(tmp_path, key="synthetic-active-key-to-remove")
    await coordinator.refresh_active(active)
    refresh_module = __import__(
        "opensquilla.gateway.model_catalog_refresh",
        fromlist=["_request_from_config"],
    )
    request = refresh_module._request_from_config(active)
    assert request is not None

    async def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("provider switch cleanup must be local")

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        unexpected_fetch,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        unexpected_fetch,
    )
    switched = active.model_copy(deep=True)
    switched.llm.provider = "openrouter"
    switched.llm.api_key = "synthetic-openrouter-key"
    switched.llm.base_url = "https://openrouter.ai/api/v1"
    await coordinator.refresh_active(switched)

    assert request.authority_identity not in coordinator._entitlements
    assert request.authority_identity not in coordinator._aligned_at
    assert catalog.tokenrhythm_declared_for_authority(
        "qwen3.8-max",
        request.authority_identity,
    ) is None
    payload = json.loads(
        (tmp_path / "model_catalog" / "tokenrhythm-v1.json").read_text()
    )
    assert payload["entitlements"] == {}


@pytest.mark.asyncio
async def test_new_key_cannot_reuse_another_authoritys_entitlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    first = _config(tmp_path, key="dummy-tokenrhythm-key-one")
    await coordinator.refresh_active(first)

    async def fail_declared(*_args, **_kwargs):
        raise OSError("synthetic auth failure")

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        fail_declared,
    )
    second = _config(tmp_path, key="dummy-tokenrhythm-key-two")
    assert await coordinator.refresh_active(second) == {"tokenrhythm": 0}
    assert coordinator.cached(second) == []
    # A replaced direct key is no longer reachable from durable config, so its
    # isolated LKG must be removed rather than retained as historical state.
    assert coordinator.cached(first) == []


@pytest.mark.asyncio
async def test_saved_nonactive_authority_is_immediately_available_and_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fetch_published(**_kwargs):
        return parse_tokenrhythm_published({"data": []})

    async def fetch_declared(*_args, api_key: str, **_kwargs):
        if api_key.endswith("-a"):
            return parse_tokenrhythm_declared(
                {
                    "data": [
                        {
                            "id": "shared/model",
                            "context_length": 1_000_000,
                            "max_completion_tokens": 131_072,
                            "supports_tools": True,
                        },
                        {
                            "id": "missing/model",
                            "context_length": 900_000,
                            "max_completion_tokens": 77_777,
                            "supports_tools": False,
                        },
                    ]
                }
            )
        return parse_tokenrhythm_declared(
            {
                "data": [
                    {
                        "id": "shared/model",
                        "context_length": 64_000,
                        "max_completion_tokens": 8_192,
                        "supports_tools": False,
                    },
                    {"id": "missing/model"},
                ]
            }
        )

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        fetch_published,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        fetch_declared,
    )
    catalog = ModelCatalog()
    coordinator = TokenRhythmCatalogCoordinator(catalog)
    config_a = _config(tmp_path, key="synthetic-authority-a")
    config_b = _config(tmp_path, key="synthetic-authority-b")
    await coordinator.refresh_active(config_a, force=True)
    refresh_module = __import__(
        "opensquilla.gateway.model_catalog_refresh",
        fromlist=["_request_from_config"],
    )
    request_b = refresh_module._request_from_config(config_b)
    assert request_b is not None
    await coordinator.discover(
        request_b,
        force=True,
        persist_entitlement=True,
        activate=False,
    )

    limits_b = catalog.resolve_deployment_limits(
        "shared/model",
        provider="tokenrhythm",
        api_key="synthetic-authority-b",
        base_url="https://tokenrhythm.studio/v1",
    )
    assert (limits_b.context_window, limits_b.max_output_tokens) == (64_000, 8_192)
    infos_a = {info.model_id: info for info in coordinator.cached(config_a)}
    infos_b = {info.model_id: info for info in coordinator.cached(config_b)}
    assert infos_a["missing/model"].context_window == 900_000
    assert infos_a["missing/model"].max_output_tokens == 77_777
    assert infos_a["missing/model"].supports_tools is False
    # B's missing fields fall back to key-independent local defaults, not A.
    assert infos_b["missing/model"].context_window == 200_000
    assert infos_b["missing/model"].max_output_tokens == 16_384
    assert infos_b["missing/model"].supports_tools is True


@pytest.mark.asyncio
async def test_late_old_authority_result_cannot_overwrite_new_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_started = asyncio.Event()
    release_old = asyncio.Event()

    async def fetch_published(**_kwargs):
        return parse_tokenrhythm_published({"data": []})

    async def fetch_declared(*_args, api_key: str, **_kwargs):
        if api_key.endswith("-one"):
            old_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Simulate a transport that cannot abort immediately and
                # still produces a late response after the key changed.
                await release_old.wait()
            return parse_tokenrhythm_declared({"data": [{"id": "old-only"}]})
        return parse_tokenrhythm_declared({"data": [{"id": "new-only"}]})

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        fetch_published,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        fetch_declared,
    )
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog())
    first = _config(tmp_path, key="dummy-tokenrhythm-key-one")
    second = _config(tmp_path, key="dummy-tokenrhythm-key-two")
    old_refresh = asyncio.create_task(coordinator.refresh_active(first))
    await old_started.wait()

    assert await coordinator.refresh_active(second) == {"tokenrhythm": 1}
    release_old.set()
    assert await old_refresh == {"tokenrhythm": 0}

    assert [info.model_id for info in coordinator.cached(second)] == ["new-only"]
    assert coordinator.cached(first) == []


@pytest.mark.asyncio
async def test_official_origin_and_v1_share_authority_and_cached_entitlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import opensquilla.gateway.model_catalog_refresh as refresh_module

    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog())
    versioned = _config(tmp_path)
    await coordinator.refresh_active(versioned)

    origin = _config(tmp_path)
    origin.llm.base_url = "https://tokenrhythm.studio"
    versioned_request = refresh_module._request_from_config(versioned)
    origin_request = refresh_module._request_from_config(origin)
    assert versioned_request is not None
    assert origin_request is not None
    assert origin_request.base_url == "https://tokenrhythm.studio/v1"
    assert origin_request.authority_identity == versioned_request.authority_identity
    assert origin_request.transport_fingerprint == versioned_request.transport_fingerprint

    assert [info.model_id for info in coordinator.cached(origin)] == ["qwen3.8-max"]
    assert await coordinator.refresh_active(origin) == {"tokenrhythm": 1}
    assert calls == ["published", "declared"]


@pytest.mark.parametrize(
    "base_url",
    [
        "https://example.invalid/v1",
        "https://catalog.tokenrhythm.studio/v1",
        "https://tokenrhythm.studio:8443/v1",
    ],
)
@pytest.mark.asyncio
async def test_non_official_endpoint_never_uses_dual_source_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog())
    config = _config(tmp_path)
    config.llm.base_url = base_url

    assert await coordinator.refresh_active(config) == {}
    assert calls == []


@pytest.mark.asyncio
async def test_persisted_official_public_lkg_is_not_projected_into_custom_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    catalog = ModelCatalog()
    coordinator = TokenRhythmCatalogCoordinator(catalog)
    official = _config(tmp_path)
    await coordinator.refresh_active(official)
    assert catalog.resolve_entry(
        "qwen3.8-max", provider="tokenrhythm"
    ).source == "live"

    custom = official.model_copy(deep=True)
    custom.llm.base_url = "https://example.invalid/v1"
    assert await coordinator.refresh_active(custom) == {}
    assert calls == ["published", "declared"]
    assert coordinator._published.models
    assert catalog.provider_model_metadata("tokenrhythm") == {}
    assert catalog.resolve_entry(
        "qwen3.8-max", provider="tokenrhythm"
    ).source == "corrections"

    restarted_catalog = ModelCatalog()
    restarted = TokenRhythmCatalogCoordinator(restarted_catalog)
    await restarted.hydrate(custom)
    assert restarted._published.models
    assert restarted_catalog.provider_model_metadata("tokenrhythm") == {}
    assert restarted.cached(custom) == []


@pytest.mark.asyncio
async def test_custom_tokenrhythm_fallback_profile_disables_official_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    catalog = ModelCatalog()
    coordinator = TokenRhythmCatalogCoordinator(catalog)
    await coordinator.refresh_active(_config(tmp_path))

    fallback_config = _profile_config(tmp_path)
    fallback_config.llm_profiles["tokenrhythm"].base_url = (
        "https://mirror.example/v1"
    )
    assert await coordinator.refresh_active(fallback_config) == {}
    assert calls == ["published", "declared"]
    assert coordinator._published.models
    assert catalog.provider_model_metadata("tokenrhythm") == {}
    assert catalog.resolve_entry(
        "qwen3.8-max", provider="tokenrhythm"
    ).source == "corrections"


@pytest.mark.asyncio
async def test_legal_empty_auth_response_clears_entitlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    config = _config(tmp_path)
    await coordinator.refresh_active(config)

    async def fetch_empty(*_args, **_kwargs):
        return _declared(empty=True)

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        fetch_empty,
    )
    clock.value += 1
    assert await coordinator.refresh_active(config, force=True) == {"tokenrhythm": 0}
    assert coordinator.cached(config) == []


@pytest.mark.asyncio
async def test_singleflight_only_coalesces_concurrent_identical_refreshes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"published": 0, "declared": 0}

    async def fetch_published(**_kwargs):
        calls["published"] += 1
        started.set()
        await release.wait()
        return _published()

    async def fetch_declared(*_args, **_kwargs):
        calls["declared"] += 1
        await release.wait()
        return _declared()

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        fetch_published,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        fetch_declared,
    )
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    config = _config(tmp_path)
    first = asyncio.create_task(coordinator.refresh_active(config))
    await started.wait()
    second = asyncio.create_task(coordinator.refresh_active(config))
    await asyncio.sleep(0)
    release.set()

    assert await first == {"tokenrhythm": 1}
    assert await second == {"tokenrhythm": 1}
    assert calls == {"published": 1, "declared": 1}


@pytest.mark.asyncio
async def test_proxy_change_keeps_entitlement_lkg_but_forces_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    config = _config(tmp_path)
    await coordinator.refresh_active(config)

    async def fail_published(**_kwargs):
        calls.append("proxy-public")
        raise OSError("synthetic proxy failure")

    async def fail_declared(*_args, **_kwargs):
        calls.append("proxy-auth")
        raise OSError("synthetic proxy failure")

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        fail_published,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        fail_declared,
    )
    config.llm.proxy = "http://127.0.0.1:9999"
    clock.value += 1

    assert await coordinator.refresh_active(config) == {"tokenrhythm": 1}
    assert [info.model_id for info in coordinator.cached(config)] == ["qwen3.8-max"]
    assert calls[-2:] == ["proxy-public", "proxy-auth"]


@pytest.mark.asyncio
async def test_credential_clear_is_zero_network_and_removes_persisted_entitlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    config = _config(tmp_path)
    await coordinator.refresh_active(config)
    config.llm.api_key = ""

    assert await coordinator.refresh_active(config) == {}
    assert calls == ["published", "declared"]
    assert coordinator.cached(config) == []
    payload = json.loads(
        (tmp_path / "model_catalog" / "tokenrhythm-v1.json").read_text()
    )
    assert payload["entitlements"] == {}


@pytest.mark.asyncio
async def test_snapshot_is_private_normalized_and_hydrates_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    config = _config(tmp_path)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    await coordinator.refresh_active(config)
    path = tmp_path / "model_catalog" / "tokenrhythm-v1.json"
    raw = path.read_text()
    assert "dummy-tokenrhythm-key" not in raw
    assert "Authorization" not in raw
    assert "proxy" not in raw.lower()
    if os.name != "nt":
        assert stat_mode(path) == 0o600
        assert stat_mode(path.parent) == 0o700

    hydrated = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    await hydrated.hydrate(config)
    assert [info.model_id for info in hydrated.cached(config)] == ["qwen3.8-max"]
    assert calls == ["published", "declared"]


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows long-path behavior")
async def test_snapshot_uses_native_windows_long_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opensquilla.paths import native_io_path

    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    long_state = tmp_path
    for index in range(18):
        long_state /= f"catalog-long-path-segment-{index:02d}"
    config = _config(long_state)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog())

    assert await coordinator.refresh_active(config) == {"tokenrhythm": 1}
    snapshot = long_state / "model_catalog" / "tokenrhythm-v1.json"
    assert native_io_path(snapshot).is_file()

    hydrated = TokenRhythmCatalogCoordinator(ModelCatalog())
    await hydrated.hydrate(config)
    assert [info.model_id for info in hydrated.cached(config)] == ["qwen3.8-max"]


@pytest.mark.asyncio
async def test_future_timestamps_are_stale_and_corrupt_or_symlink_files_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    config = _config(tmp_path)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    await coordinator.refresh_active(config)
    path = tmp_path / "model_catalog" / "tokenrhythm-v1.json"
    payload = json.loads(path.read_text())
    payload["published"]["successAt"] = clock.value + 100
    for entitlement in payload["entitlements"].values():
        entitlement["successAt"] = clock.value + 100
    path.write_text(json.dumps(payload))

    future = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    await future.refresh_active(config)
    assert calls == ["published", "declared", "published", "declared"]

    path.write_text("not json")
    corrupt = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    await corrupt.hydrate(config)
    assert corrupt.cached(config) == []

    path.write_text(json.dumps({"schemaVersion": 2, "published": {}}))
    future_schema = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    await future_schema.hydrate(config)
    assert future_schema.cached(config) == []

    for invalid_schema in (True, 1.0, "1"):
        path.write_text(
            json.dumps({"schemaVersion": invalid_schema, "published": {}})
        )
        invalid = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
        await invalid.hydrate(config)
        assert invalid.cached(config) == []

    path.write_text("[" * 2_000 + "0" + "]" * 2_000)
    nested_corrupt = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    await nested_corrupt.hydrate(config)
    assert nested_corrupt.cached(config) == []

    if os.name != "nt":
        path.unlink()
        target = tmp_path / "outside.json"
        target.write_text("{}")
        path.symlink_to(target)
        symlinked = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
        await symlinked.hydrate(config)
        assert symlinked.cached(config) == []


@pytest.mark.asyncio
async def test_key_change_refreshes_declared_without_refetching_fresh_public_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import opensquilla.gateway.model_catalog_refresh as refresh_module

    clock = FakeClock()
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    catalog = ModelCatalog()
    coordinator = TokenRhythmCatalogCoordinator(catalog, clock=clock)
    refresh_module.install_tokenrhythm_catalog_coordinator(coordinator)
    first = _config(tmp_path, key="synthetic-key-change-a")
    try:
        await coordinator.refresh_active(first)
        previous = refresh_module.live_catalog_refresh_fingerprint(first)

        second = first.model_copy(deep=True)
        second.llm.api_key = "synthetic-key-change-b"
        await refresh_module.refresh_live_model_catalog_if_changed(
            previous,
            second,
            catalog=catalog,
        )

        assert calls == ["published", "declared", "declared"]
        first_request = refresh_module._request_from_config(first)
        second_request = refresh_module._request_from_config(second)
        assert first_request is not None
        assert second_request is not None
        assert first_request.authority_identity not in coordinator._entitlements
        assert second_request.authority_identity in coordinator._entitlements
    finally:
        refresh_module.install_tokenrhythm_catalog_coordinator(None)


@pytest.mark.parametrize(
    "corruption",
    [
        "empty_declared_model_id",
        "non_mapping_published_model",
        "missing_declared_required_field",
    ],
)
@pytest.mark.asyncio
async def test_semantically_corrupt_snapshot_is_ignored_atomically_and_refetched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    clock = FakeClock()
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    config = _config(tmp_path)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    await coordinator.refresh_active(config)
    assert calls == ["published", "declared"]

    path = tmp_path / "model_catalog" / "tokenrhythm-v1.json"
    payload = json.loads(path.read_text())
    entitlement = next(iter(payload["entitlements"].values()))
    if corruption == "empty_declared_model_id":
        declared = entitlement["models"].pop("qwen3.8-max")
        entitlement["models"][""] = declared
    elif corruption == "non_mapping_published_model":
        payload["published"]["models"]["qwen3.8-max"] = "not-a-model-record"
    else:
        entitlement["models"]["qwen3.8-max"].pop("capabilities")
    path.write_text(json.dumps(payload))

    calls.clear()
    hydrated = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    await hydrated.hydrate(config)

    # A single malformed row invalidates the complete atomic document: no
    # valid sibling source is partially exposed and fresh timestamps cannot
    # suppress the next bounded refresh.
    assert hydrated._published.models == {}
    assert hydrated._entitlements == {}
    assert hydrated.cached(config) == []
    assert await hydrated.refresh_active(config) == {"tokenrhythm": 1}
    assert calls == ["published", "declared"]


@pytest.mark.asyncio
async def test_unsaved_discovery_does_not_persist_entitlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import opensquilla.gateway.model_catalog_refresh as refresh_module

    clock = FakeClock()
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    config = _config(tmp_path)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    await coordinator.hydrate(config)
    request = refresh_module._request_from_config(config)
    assert request is not None

    view = await coordinator.discover(
        request,
        force=True,
        persist_entitlement=False,
        activate=False,
    )
    assert view.declared_available is True
    assert view.catalog["stale"] is False
    repeated = await coordinator.discover(
        request,
        force=False,
        persist_entitlement=False,
        activate=False,
    )
    assert repeated.declared_available is True
    assert calls == ["published", "declared"]
    assert coordinator.cached(config) == []
    payload = json.loads(
        (tmp_path / "model_catalog" / "tokenrhythm-v1.json").read_text()
    )
    assert payload["entitlements"] == {}


@pytest.mark.asyncio
async def test_saved_identity_promotes_matching_ephemeral_lkg_without_refetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import opensquilla.gateway.model_catalog_refresh as refresh_module

    clock = FakeClock()
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    config = _config(tmp_path)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    await coordinator.hydrate(config, activate=False)
    request = refresh_module._request_from_config(config)
    assert request is not None
    await coordinator.discover(
        request,
        force=False,
        persist_entitlement=False,
        activate=False,
    )

    assert await coordinator.refresh_active(config) == {"tokenrhythm": 1}
    assert calls == ["published", "declared"]
    payload = json.loads(
        (tmp_path / "model_catalog" / "tokenrhythm-v1.json").read_text()
    )
    assert len(payload["entitlements"]) == 1


@pytest.mark.asyncio
async def test_close_cancels_and_awaits_inflight_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()
    never = asyncio.Event()

    async def blocking_fetch(*_args, **_kwargs):
        started.set()
        await never.wait()
        return {}

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        blocking_fetch,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        blocking_fetch,
    )
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog())
    operation = asyncio.create_task(coordinator.refresh_active(_config(tmp_path)))
    await started.wait()

    await coordinator.close()

    assert operation.done()
    assert await operation == {"tokenrhythm": 0}
    with pytest.raises(RuntimeError, match="closed"):
        await coordinator.refresh_active(_config(tmp_path))


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="symlink setup differs on Windows")
async def test_symlinked_snapshot_parent_is_never_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "model_catalog").symlink_to(outside, target_is_directory=True)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog())

    assert await coordinator.refresh_active(_config(tmp_path)) == {"tokenrhythm": 1}
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_refresh_cancelled_before_registration_never_leaks_child_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import opensquilla.gateway.model_catalog_refresh as refresh_module

    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog())
    request = refresh_module._request_from_config(_config(tmp_path))
    assert request is not None

    await coordinator._lock.acquire()
    try:
        operation = asyncio.create_task(
            coordinator.discover(
                request,
                force=True,
                persist_entitlement=False,
                activate=False,
            )
        )
        await asyncio.sleep(0)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
    finally:
        coordinator._lock.release()

    await asyncio.sleep(0)
    assert calls == []
    assert coordinator._operations == set()


@pytest.mark.asyncio
async def test_last_cancelled_waiter_releases_declared_source_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import opensquilla.gateway.model_catalog_refresh as refresh_module

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fetch_published(**_kwargs):
        return _published()

    async def blocking_declared(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(
        refresh_module,
        "fetch_tokenrhythm_published",
        fetch_published,
    )
    monkeypatch.setattr(
        refresh_module,
        "fetch_tokenrhythm_declared",
        blocking_declared,
    )
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog())
    config = _config(tmp_path)
    await coordinator.hydrate(config, activate=False)
    request = refresh_module._request_from_config(config)
    assert request is not None

    caller = asyncio.create_task(
        coordinator.discover(
            request,
            force=True,
            persist_entitlement=False,
            activate=False,
        )
    )
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await cancelled.wait()

    assert request.transport_fingerprint not in (
        coordinator._declared_transport_authorities
    )
    assert coordinator._declared_operation_counts == {}
    assert ("declared", request.transport_fingerprint) not in coordinator._inflight
    await coordinator.close()


@pytest.mark.asyncio
async def test_close_drains_source_removed_by_identity_fence_after_caller_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import opensquilla.gateway.model_catalog_refresh as refresh_module

    started = asyncio.Event()
    cancel_seen = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def fetch_published(**_kwargs):
        return _published()

    async def cancellation_resistant_declared(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancel_seen.set()
            await release.wait()
            finished.set()
            return _declared()

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        fetch_published,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        cancellation_resistant_declared,
    )
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog())
    original = _config(tmp_path, key="dummy-original-key")
    await coordinator.hydrate(original, activate=False)
    request = refresh_module._request_from_config(original)
    assert request is not None

    caller = asyncio.create_task(
        coordinator.discover(
            request,
            force=True,
            persist_entitlement=True,
            activate=False,
        )
    )
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    replacement = _config(tmp_path, key="dummy-replacement-key")
    await coordinator.hydrate(replacement)
    await cancel_seen.wait()
    assert coordinator._inflight == {}

    closing = asyncio.create_task(coordinator.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await closing
    assert finished.is_set()
    await coordinator.close()


@pytest.mark.asyncio
async def test_total_deadline_does_not_wait_for_cancellation_resistant_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import opensquilla.gateway.model_catalog_refresh as refresh_module

    release = asyncio.Event()
    cancellation_seen = asyncio.Event()

    async def cancellation_resistant_fetch(*_args, **_kwargs):
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
        return {}

    monkeypatch.setattr(
        refresh_module,
        "fetch_tokenrhythm_published",
        cancellation_resistant_fetch,
    )
    monkeypatch.setattr(
        refresh_module,
        "fetch_tokenrhythm_declared",
        cancellation_resistant_fetch,
    )
    monkeypatch.setattr(refresh_module, "TOKENRHYTHM_PUBLIC_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(refresh_module, "TOKENRHYTHM_AUTH_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(refresh_module, "TOKENRHYTHM_REFRESH_DEADLINE_SECONDS", 0.02)

    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog())
    config = _config(tmp_path)
    await coordinator.hydrate(config, activate=False)
    request = refresh_module._request_from_config(config)
    assert request is not None
    caller = asyncio.create_task(
        coordinator.discover(
            request,
            force=True,
            persist_entitlement=False,
            activate=False,
        )
    )
    returned_before_release = False
    view = None
    try:
        done, _pending = await asyncio.wait({caller}, timeout=0.2)
        returned_before_release = caller in done
        if returned_before_release:
            view = caller.result()
    finally:
        release.set()
        await coordinator.close()

    assert cancellation_seen.is_set()
    assert returned_before_release
    assert view is not None
    assert view.declared_available is False
    assert view.catalog["stale"] is True


@pytest.mark.asyncio
async def test_ephemeral_promotion_persists_lkg_when_proxy_refresh_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import opensquilla.gateway.model_catalog_refresh as refresh_module

    clock = FakeClock()
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    config = _config(tmp_path)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    await coordinator.hydrate(config, activate=False)
    request = refresh_module._request_from_config(config)
    assert request is not None
    await coordinator.discover(
        request,
        force=False,
        persist_entitlement=False,
        activate=False,
    )

    async def fail_published(**_kwargs):
        raise OSError("synthetic public failure")

    async def fail_declared(*_args, **_kwargs):
        raise OSError("synthetic auth failure")

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        fail_published,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        fail_declared,
    )
    config.llm.proxy = "http://127.0.0.1:9999"
    clock.value += 1

    assert await coordinator.refresh_active(config) == {"tokenrhythm": 1}
    payload = json.loads(
        (tmp_path / "model_catalog" / "tokenrhythm-v1.json").read_text()
    )
    assert len(payload["entitlements"]) == 1

    hydrated = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    await hydrated.hydrate(config)
    assert [info.model_id for info in hydrated.cached(config)] == ["qwen3.8-max"]


@pytest.mark.asyncio
async def test_close_fences_keyless_refresh_between_hydrate_and_publish(
    tmp_path: Path,
) -> None:
    hydrated = asyncio.Event()
    release = asyncio.Event()

    class PausingCoordinator(TokenRhythmCatalogCoordinator):
        async def hydrate(self, config, *, activate: bool = True) -> None:
            await super().hydrate(config, activate=activate)
            hydrated.set()
            await release.wait()

    config = _config(tmp_path, key="")
    coordinator = PausingCoordinator(ModelCatalog())
    operation = asyncio.create_task(coordinator.refresh_active(config))
    await hydrated.wait()

    await coordinator.close()
    release.set()

    with pytest.raises(RuntimeError, match="closed"):
        await operation


@pytest.mark.asyncio
async def test_close_waits_for_uncancellable_snapshot_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    write_started = threading.Event()
    release_write = threading.Event()
    write_finished = threading.Event()

    def blocking_write(*_args, **_kwargs) -> None:
        write_started.set()
        release_write.wait(timeout=5)
        write_finished.set()

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh._write_snapshot_file",
        blocking_write,
    )
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog())
    operation = asyncio.create_task(coordinator.refresh_active(_config(tmp_path)))
    for _ in range(200):
        if write_started.is_set():
            break
        await asyncio.sleep(0.005)
    assert write_started.is_set()

    operation.cancel()
    closing = asyncio.create_task(coordinator.close())
    await asyncio.sleep(0.02)
    assert not closing.done()

    release_write.set()
    await closing
    assert write_finished.is_set()
    with pytest.raises(asyncio.CancelledError):
        await operation


@pytest.mark.asyncio
async def test_close_flushes_cleanup_cancelled_while_waiting_for_persist_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _patch_fetches(monkeypatch, calls)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog())
    previous = _config(tmp_path, key="synthetic-cleanup-persist-key")
    await coordinator.refresh_active(previous)
    assert coordinator._entitlements

    current = GatewayConfig(
        state_dir=str(tmp_path),
        llm={
            "provider": "openrouter",
            "model": "openai/gpt-test",
            "api_key": "synthetic-openrouter-key",
        },
    )
    await coordinator._persist_lock.acquire()
    cleanup = asyncio.create_task(
        coordinator.reconcile_profile_transition(
            previous,
            current,
            provider_id="tokenrhythm",
        )
    )
    for _ in range(200):
        if not coordinator._entitlements and coordinator._pending_persist:
            break
        await asyncio.sleep(0)
    assert coordinator._entitlements == {}
    assert coordinator._pending_persist is True

    cleanup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    closing = asyncio.create_task(coordinator.close())
    await asyncio.sleep(0)
    assert not closing.done()

    coordinator._persist_lock.release()
    await closing
    payload = json.loads(
        (tmp_path / "model_catalog" / "tokenrhythm-v1.json").read_text()
    )
    assert payload["published"]["models"]
    assert payload["entitlements"] == {}
    assert coordinator._pending_persist is False


@pytest.mark.asyncio
async def test_future_success_then_failure_honors_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    initial_calls: list[str] = []
    _patch_fetches(monkeypatch, initial_calls)
    config = _config(tmp_path)
    initial = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    await initial.refresh_active(config)
    path = tmp_path / "model_catalog" / "tokenrhythm-v1.json"
    payload = json.loads(path.read_text())
    payload["published"]["successAt"] = clock.value + 100
    for entitlement in payload["entitlements"].values():
        entitlement["successAt"] = clock.value + 100
    path.write_text(json.dumps(payload))

    failed_calls: list[str] = []

    async def fail_published(**_kwargs):
        failed_calls.append("published")
        raise OSError("synthetic public failure")

    async def fail_declared(*_args, **_kwargs):
        failed_calls.append("declared")
        raise OSError("synthetic auth failure")

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        fail_published,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        fail_declared,
    )
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    await coordinator.refresh_active(config)
    assert failed_calls == ["published", "declared"]

    await coordinator.refresh_active(config)
    clock.value += 299
    await coordinator.refresh_active(config)
    assert failed_calls == ["published", "declared"]

    clock.value += 1
    await coordinator.refresh_active(config)
    assert failed_calls == ["published", "declared", "published", "declared"]


@pytest.mark.asyncio
async def test_clock_rollback_does_not_extend_in_memory_failure_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    calls: list[str] = []

    async def fail_published(**_kwargs):
        calls.append("published")
        raise OSError("synthetic public failure")

    async def fail_declared(*_args, **_kwargs):
        calls.append("declared")
        raise OSError("synthetic auth failure")

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        fail_published,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        fail_declared,
    )
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    config = _config(tmp_path)

    assert await coordinator.refresh_active(config) == {"tokenrhythm": 0}
    clock.value -= 60
    assert await coordinator.refresh_active(config) == {"tokenrhythm": 0}
    assert calls == ["published", "declared", "published", "declared"]


@pytest.mark.asyncio
async def test_force_refresh_joins_running_auth_only_source_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import opensquilla.gateway.model_catalog_refresh as refresh_module

    clock = FakeClock()
    initial_calls: list[str] = []
    _patch_fetches(monkeypatch, initial_calls)
    config = _config(tmp_path)
    coordinator = TokenRhythmCatalogCoordinator(ModelCatalog(), clock=clock)
    await coordinator.refresh_active(config)
    request = refresh_module._request_from_config(config)
    assert request is not None
    entitlement = coordinator._entitlements[request.authority_identity]
    entitlement.success_at = clock.value - 3600

    auth_started = asyncio.Event()
    release_auth = asyncio.Event()
    calls = {"published": 0, "declared": 0}

    async def fetch_published(**_kwargs):
        calls["published"] += 1
        return _published()

    async def fetch_declared(*_args, **_kwargs):
        calls["declared"] += 1
        auth_started.set()
        await release_auth.wait()
        return _declared()

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_published",
        fetch_published,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.fetch_tokenrhythm_declared",
        fetch_declared,
    )
    ordinary = asyncio.create_task(
        coordinator.discover(
            request,
            force=False,
            persist_entitlement=True,
            activate=True,
        )
    )
    await auth_started.wait()
    forced = asyncio.create_task(
        coordinator.discover(
            request,
            force=True,
            persist_entitlement=True,
            activate=True,
        )
    )
    for _ in range(100):
        if calls["published"] == 1:
            break
        await asyncio.sleep(0)
    assert calls == {"published": 1, "declared": 1}

    release_auth.set()
    await asyncio.gather(ordinary, forced)
    assert calls == {"published": 1, "declared": 1}
