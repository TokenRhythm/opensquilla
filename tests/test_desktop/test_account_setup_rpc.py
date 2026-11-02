"""Manual setup RPCs must not use or change the desktop account binding."""

from __future__ import annotations

import json
import time
import tomllib
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

import opensquilla.gateway.rpc_onboarding  # noqa: F401 - register RPC handlers
from opensquilla import desktop_account as bridge
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.onboarding.probe import ProviderModelsDiscoverResult

_UAT_BASE = "https://uat.tokenrhythm.studio/v1"
_MANUAL_KEY = "synthetic-manual-tokenrhythm-key"
_DRAFT_KEY = "synthetic-draft-tokenrhythm-key"
_LEASE = "os_local_" + "x" * 43
_PRIMARY_DISCOVER = "onboarding.models.discover"
_PROFILE_DISCOVER = "onboarding.llmProfile.models.discover"


@pytest.fixture(autouse=True)
def isolate_account(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(bridge, "_endpoint", "")
    monkeypatch.setattr(bridge, "_token", "")
    monkeypatch.setattr(bridge, "_lease_authorities", {})
    monkeypatch.delenv("SYNTHETIC_MISSING_TOKENRHYTHM_KEY", raising=False)
    token = bridge.pin_account_state(None)
    try:
        yield
    finally:
        bridge.reset_account_state(token)


def _select_account(*, ready: bool = True, **updates: Any) -> dict[str, Any]:
    state = {
        "source": "account",
        "selectedAccountId": "synthetic-account",
        "accountId": "synthetic-account",
        "ready": ready,
        "credential": _LEASE if ready else "",
        "mediaSources": {"image": "inherit", "video": "inherit"},
        **updates,
    }
    bridge.pin_account_state(state)
    return deepcopy(state)


def _install_bridge_state(
    monkeypatch: pytest.MonkeyPatch, *, cached: dict[str, Any], live: dict[str, Any],
) -> list[tuple[str, str]]:
    requests: list[tuple[str, str]] = []

    class Connection:
        status = 200

        def request(self, method: str, path: str, **_kwargs: Any) -> None:
            requests.append((method, path))

        def getresponse(self) -> Connection:
            return self

        def read(self, _limit: int) -> bytes:
            return json.dumps(live).encode()

        def close(self) -> None:
            pass

    monkeypatch.setattr(bridge, "_endpoint", "http://127.0.0.1:12345")
    monkeypatch.setattr(bridge, "_token", "synthetic-control-token")
    monkeypatch.setattr(bridge, "_cached", cached)
    monkeypatch.setattr(bridge, "_cached_at", time.monotonic())
    monkeypatch.setattr(bridge, "_leases", {})
    monkeypatch.setattr(
        bridge.http.client, "HTTPConnection", lambda *_args, **_kwargs: Connection(),
    )
    return requests


def _config(tmp_path: Path, *, primary: bool = False) -> GatewayConfig:
    connection = {
        "provider": "tokenrhythm", "model": "qwen3.7-max",
        "api_key": _MANUAL_KEY, "base_url": _UAT_BASE,
    }
    return GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        llm=connection if primary else {
            "provider": "openai", "model": "gpt-4o", "api_key": "synthetic-original-key",
        },
        llm_profiles={} if primary else {"tokenrhythm": {
            key: value for key, value in connection.items() if key != "provider"
        }},
        squilla_router={"enabled": False, "preset_binding": "follow_primary"},
    )


def _context(config: GatewayConfig) -> RpcContext:
    return RpcContext(
        conn_id="desktop-account-setup",
        config=config,
        principal=Principal(
            role="operator", scopes=frozenset({"operator.admin"}),
            is_owner=True, authenticated=True,
        ),
    )


@pytest.fixture
def catalog_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fetch(**kwargs: Any) -> ProviderModelsDiscoverResult:
        calls.append(kwargs)
        return ProviderModelsDiscoverResult(
            ok=True, provider_id="tokenrhythm", source="live",
            models=[{"id": "synthetic-discovered-model"}],
        )

    # Keep the real RPC, deployment resolution, and selectable-catalog policy.
    # Both final fetch paths are stubbed, so a wrong origin is observable too.
    monkeypatch.setattr("opensquilla.onboarding.probe._discover_saved_or_draft_models", fetch)
    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.discover_tokenrhythm_models", fetch,
    )
    return calls


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("method", [_PRIMARY_DISCOVER, _PROFILE_DISCOVER])
@pytest.mark.asyncio
async def test_manual_discovery_uses_saved_key_and_uat_endpoint(
    tmp_path: Path, catalog_calls: list[dict[str, Any]], ready: bool, method: str,
) -> None:
    config = _config(tmp_path, primary=method == _PRIMARY_DISCOVER)
    before = config.model_dump()
    selected = _select_account(ready=ready)
    response = await get_dispatcher().dispatch(
        "manual-discover", method,
        {"providerId": "tokenrhythm", "credentialSource": "manual", "forceRefresh": True},
        _context(config),
    )
    assert response.error is None, response.error
    assert response.payload["ok"] is True
    assert len(catalog_calls) == 1
    assert catalog_calls[0]["api_key"] == _MANUAL_KEY
    assert catalog_calls[0]["base_url"] == _UAT_BASE
    assert config.model_dump() == before
    assert not Path(config.config_path).exists()
    assert bridge.account_state() == selected
    assert _MANUAL_KEY not in repr(response.payload)


@pytest.mark.parametrize("method", [_PRIMARY_DISCOVER, _PROFILE_DISCOVER])
@pytest.mark.asyncio
async def test_default_discovery_uses_selected_runtime_account(
    tmp_path: Path, catalog_calls: list[dict[str, Any]], method: str,
) -> None:
    config = _config(tmp_path, primary=method == _PRIMARY_DISCOVER)
    selected = _select_account()
    response = await get_dispatcher().dispatch(
        "runtime-discover", method, {"providerId": "tokenrhythm"}, _context(config),
    )
    assert response.error is None, response.error
    assert response.payload["ok"] is True
    assert len(catalog_calls) == 1
    assert catalog_calls[0]["api_key"] == _LEASE
    assert catalog_calls[0]["base_url"] == bridge.OFFICIAL_BASE
    assert bridge.account_state() == selected
    assert _LEASE not in repr(response.payload)


@pytest.mark.parametrize("method", [_PRIMARY_DISCOVER, _PROFILE_DISCOVER])
@pytest.mark.asyncio
async def test_default_discovery_does_not_fall_back_when_account_is_paused(
    tmp_path: Path, catalog_calls: list[dict[str, Any]], method: str,
) -> None:
    config = _config(tmp_path, primary=method == _PRIMARY_DISCOVER)
    selected = _select_account(ready=False)
    response = await get_dispatcher().dispatch(
        "paused-discover", method, {"providerId": "tokenrhythm"}, _context(config),
    )
    if method == _PRIMARY_DISCOVER:
        assert response.error is None, response.error
        assert response.payload["ok"] is False
        assert response.payload["failureKind"] == "auth_invalid"
    else:
        assert response.error is not None
        assert response.error.code == "onboarding.llmProfile.invalid"
    assert catalog_calls == []
    assert bridge.account_state() == selected


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.asyncio
async def test_draft_profile_discovery_is_manual_without_changing_selection(
    tmp_path: Path, catalog_calls: list[dict[str, Any]], ready: bool,
) -> None:
    config = _config(tmp_path)
    before = config.model_dump()
    selected = _select_account(ready=ready)
    response = await get_dispatcher().dispatch(
        "draft-discover", "onboarding.llmProfile.draft.models.discover",
        {"providerId": "tokenrhythm", "apiKey": _DRAFT_KEY,
         "baseUrl": _UAT_BASE, "keepCurrentSecret": False},
        _context(config),
    )
    assert response.error is None, response.error
    assert response.payload["ok"] is True
    assert len(catalog_calls) == 1
    assert catalog_calls[0]["api_key"] == _DRAFT_KEY
    assert catalog_calls[0]["base_url"] == _UAT_BASE
    assert catalog_calls[0]["saved"] is False
    assert config.model_dump() == before
    assert not Path(config.config_path).exists()
    assert bridge.account_state() == selected


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.asyncio
async def test_low_level_catalog_discovery_preserves_explicit_arguments(
    monkeypatch: pytest.MonkeyPatch, ready: bool,
) -> None:
    from opensquilla.onboarding.probe import discover_selectable_provider_models
    from opensquilla.provider.openai import OpenAIProvider

    seen: list[tuple[str, str]] = []

    async def listed(self: OpenAIProvider, **_kwargs: Any) -> list[Any]:
        seen.append((self._api_key, self._base_url))
        return []

    monkeypatch.setattr(OpenAIProvider, "list_models", listed)
    selected = _select_account(ready=ready)
    result = await discover_selectable_provider_models(
        provider_id="tokenrhythm", api_key=_DRAFT_KEY, base_url=_UAT_BASE,
    )
    assert result.ok is True
    assert seen == [(_DRAFT_KEY, _UAT_BASE)]
    assert bridge.account_state() == selected


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.asyncio
async def test_primary_draft_discovery_builds_submitted_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ready: bool,
) -> None:
    from opensquilla.provider.openai import OpenAIProvider

    seen: list[tuple[str, str]] = []

    async def listed(self: OpenAIProvider, **_kwargs: Any) -> list[Any]:
        seen.append((self._api_key, self._base_url))
        return []

    monkeypatch.setattr(OpenAIProvider, "list_models", listed)
    config = _config(tmp_path, primary=True)
    selected = _select_account(ready=ready)
    response = await get_dispatcher().dispatch(
        "primary-draft", _PRIMARY_DISCOVER,
        {"providerId": "tokenrhythm", "apiKey": _DRAFT_KEY, "baseUrl": _UAT_BASE},
        _context(config),
    )
    assert response.error is None, response.error
    assert response.payload["ok"] is True
    assert seen == [(_DRAFT_KEY, _UAT_BASE)]
    assert bridge.account_state() == selected


@pytest.fixture
def quiet_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_refresh(_config: GatewayConfig) -> None:
        pass

    monkeypatch.setattr(
        "opensquilla.gateway.model_catalog_refresh.refresh_live_model_catalog", no_refresh,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.setup_config_runtime.sync_media_runtime", lambda _config: None,
    )


@pytest.mark.parametrize("method", [
    "onboarding.llmProfile.activate", "onboarding.llmProfile.upsertAndActivate",
])
@pytest.mark.asyncio
async def test_manual_activation_succeeds_while_account_is_paused(
    tmp_path: Path, quiet_runtime: None, method: str,
) -> None:
    from opensquilla.onboarding.config_store import load_config

    config = _config(tmp_path)
    selected = _select_account(ready=False)
    params = {"providerId": "tokenrhythm", "model": "qwen3.7-max",
              "credentialSource": "manual"}
    expected_key = _MANUAL_KEY
    if method.endswith("upsertAndActivate"):
        params.update(apiKey=_DRAFT_KEY, baseUrl=_UAT_BASE)
        expected_key = _DRAFT_KEY
    context = _context(config)
    response = await get_dispatcher().dispatch("activate-manual", method, params, context)
    assert response.error is None, response.error
    assert context.config.llm.provider == "tokenrhythm"
    assert context.config.llm.api_key == expected_key
    assert context.config.llm.base_url == _UAT_BASE
    assert context.config.llm.credential_source == "manual"
    persisted = tomllib.loads(Path(config.config_path).read_text())
    assert load_config(Path(config.config_path)).llm.provider == "tokenrhythm"
    assert persisted["llm"]["api_key"] == expected_key
    assert persisted["llm"]["base_url"] == _UAT_BASE
    assert bridge.account_state() == selected
    assert expected_key not in repr(response.payload)
    assert "os_local_" not in Path(config.config_path).read_text()


@pytest.mark.parametrize("method", [
    "onboarding.llmProfile.activate", "onboarding.llmProfile.upsertAndActivate",
])
@pytest.mark.asyncio
async def test_failed_manual_activation_preserves_live_config_and_file(
    tmp_path: Path, quiet_runtime: None, method: str,
) -> None:
    config = _config(tmp_path)
    config.llm_profiles["tokenrhythm"].api_key = ""
    config.llm_profiles["tokenrhythm"].api_key_env = "SYNTHETIC_MISSING_TOKENRHYTHM_KEY"
    path = Path(config.config_path)
    path.write_text('[llm]\nprovider = "openai"\napi_key = "synthetic-original-key"\n')
    before_file = path.read_bytes()
    before = config.model_dump()
    selected = _select_account()
    params: dict[str, Any] = {
        "providerId": "tokenrhythm", "model": "qwen3.7-max", "credentialSource": "manual",
    }
    if method.endswith("upsertAndActivate"):
        params.update(apiKey="", apiKeyEnv="SYNTHETIC_MISSING_TOKENRHYTHM_KEY",
                      keepCurrentSecret=False)
    response = await get_dispatcher().dispatch("invalid-manual", method, params, _context(config))
    assert response.error is not None
    assert config.model_dump() == before
    assert path.read_bytes() == before_file
    assert bridge.account_state() == selected


@pytest.mark.parametrize("already_scoped", [False, True])
def test_setup_status_refreshes_source_once_unless_operation_is_already_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, already_scoped: bool,
) -> None:
    from opensquilla.gateway.adapters.setup_workflow import setup_status

    selected = _select_account()
    if not already_scoped:
        bridge.pin_account_state(None)
    live = {**selected, "source": "manual"}
    requests = _install_bridge_state(monkeypatch, cached=selected, live=live)
    result = setup_status(_config(tmp_path, primary=True), is_owner=True)

    if already_scoped:
        assert result["llmCredentialStatus"]["source"] == "account"
        assert requests == []
        assert bridge._turn_state.get() == selected
    else:
        assert result["llmCredentialStatus"]["source"] == "explicit"
        assert requests == [("GET", "/state")]
        assert bridge._turn_state.get() is None


@pytest.mark.asyncio
async def test_active_remove_promotes_manual_replacement_while_account_is_paused(
    tmp_path: Path, quiet_runtime: None,
) -> None:
    from opensquilla.onboarding.config_store import load_config

    config = _config(tmp_path)
    selected = _select_account(ready=False)
    context = _context(config)
    response = await get_dispatcher().dispatch(
        "remove-active", "onboarding.llmProfile.active.remove",
        {"providerId": "openai", "replacementProviderId": "tokenrhythm",
         "replacementModel": "qwen3.7-max", "credentialSource": "manual"},
        context,
    )
    assert response.error is None, response.error
    assert context.config.llm.provider == "tokenrhythm"
    assert context.config.llm.api_key == _MANUAL_KEY
    assert context.config.llm.base_url == _UAT_BASE
    assert "openai" not in context.config.llm_profiles
    persisted = tomllib.loads(Path(config.config_path).read_text())
    assert load_config(Path(config.config_path)).llm.provider == "tokenrhythm"
    assert persisted["llm"]["api_key"] == _MANUAL_KEY
    assert "openai" not in persisted.get("llm_profiles", {})
    assert bridge.account_state() == selected
    assert _MANUAL_KEY not in repr(response.payload)


def _account_activation_params(method: str) -> dict[str, Any]:
    if method.endswith("active.remove"):
        return {
            "providerId": "openai", "replacementProviderId": "tokenrhythm",
            "replacementModel": "qwen3.7-max", "credentialSource": "account",
        }
    params: dict[str, Any] = {
        "providerId": "tokenrhythm", "model": "qwen3.7-max", "credentialSource": "account",
    }
    if method.endswith("upsertAndActivate"):
        params["keepCurrentSecret"] = True
    return params


@pytest.mark.parametrize("method", [
    "onboarding.llmProfile.activate", "onboarding.llmProfile.upsertAndActivate",
    "onboarding.llmProfile.active.remove",
])
@pytest.mark.asyncio
async def test_account_activation_validates_current_account_without_rebinding_old_selection(
    tmp_path: Path, quiet_runtime: None, method: str,
) -> None:
    from opensquilla.onboarding.config_store import load_config

    config = _config(tmp_path)
    profile = config.llm_profiles["tokenrhythm"]
    profile.credential_source = "account"
    profile.api_key = ""
    profile.base_url = bridge.OFFICIAL_BASE
    selected = _select_account(selectedAccountId="previous-account", accountId="current-account")
    assert not bridge.account_credential("tokenrhythm").ready
    response = await get_dispatcher().dispatch(
        "validate-current-account", method, _account_activation_params(method), _context(config),
    )
    assert response.error is None, response.error
    assert config.llm.provider == "tokenrhythm"
    assert config.llm.credential_source == "account"
    assert config.llm.api_key == ""
    assert load_config(Path(config.config_path)).llm.provider == "tokenrhythm"
    assert "os_local_" not in Path(config.config_path).read_text()
    assert bridge.account_state() == selected
    assert not bridge.account_credential("tokenrhythm").ready


@pytest.mark.parametrize("method", [
    "onboarding.llmProfile.activate", "onboarding.llmProfile.upsertAndActivate",
    "onboarding.llmProfile.active.remove",
])
@pytest.mark.asyncio
async def test_failed_account_activation_preserves_prior_binding_and_config(
    tmp_path: Path, quiet_runtime: None, method: str,
) -> None:
    config = _config(tmp_path)
    profile = config.llm_profiles["tokenrhythm"]
    profile.credential_source = "account"
    profile.api_key = ""
    profile.api_key_env_pool = ["SYNTHETIC_MISSING_TOKENRHYTHM_KEY"]
    profile.base_url = bridge.OFFICIAL_BASE
    selected = _select_account(selectedAccountId="previous-account", accountId="current-account")
    before = config.model_dump()
    path = Path(config.config_path)
    path.write_text('[llm]\nprovider = "openai"\napi_key = "synthetic-original-key"\n')
    before_file = path.read_bytes()
    response = await get_dispatcher().dispatch(
        "reject-current-account", method, _account_activation_params(method), _context(config),
    )
    assert response.error is not None
    assert config.model_dump() == before
    assert path.read_bytes() == before_file
    assert bridge.account_state() == selected
    assert not bridge.account_credential("tokenrhythm").ready


@pytest.mark.asyncio
async def test_account_upsert_and_activate_creates_reference_without_existing_profile(
    tmp_path: Path, quiet_runtime: None,
) -> None:
    config = _config(tmp_path)
    config.llm_profiles.clear()
    selected = _select_account(selectedAccountId="previous-account", accountId="current-account")
    response = await get_dispatcher().dispatch(
        "new-account-reference", "onboarding.llmProfile.upsertAndActivate",
        {"providerId": "tokenrhythm", "model": "qwen3.7-max",
         "baseUrl": bridge.OFFICIAL_BASE, "credentialSource": "account"},
        _context(config),
    )
    assert response.error is None, response.error
    assert config.llm.provider == "tokenrhythm"
    assert config.llm.credential_source == "account"
    assert config.llm.api_key == config.llm.api_key_env == ""
    persisted = tomllib.loads(Path(config.config_path).read_text())
    assert persisted["llm"]["credential_source"] == "account"
    assert "os_local_" not in Path(config.config_path).read_text()
    assert bridge.account_state() == selected


@pytest.mark.parametrize("method", [_PRIMARY_DISCOVER, _PROFILE_DISCOVER])
@pytest.mark.asyncio
async def test_omitted_discovery_source_refreshes_stale_bridge_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    catalog_calls: list[dict[str, Any]], method: str,
) -> None:
    selected = _select_account()
    bridge.pin_account_state(None)
    requests = _install_bridge_state(
        monkeypatch, cached={**selected, "source": "manual"}, live=selected,
    )
    config = _config(tmp_path, primary=method == _PRIMARY_DISCOVER)
    response = await get_dispatcher().dispatch(
        "fresh-source", method, {"providerId": "tokenrhythm"}, _context(config),
    )
    assert response.error is None, response.error
    assert response.payload["ok"] is True
    assert len(catalog_calls) == 1
    assert catalog_calls[0]["api_key"] == _LEASE
    assert catalog_calls[0]["base_url"] == bridge.OFFICIAL_BASE
    assert requests == [("GET", "/state")]
    assert bridge._turn_state.get() is None


@pytest.fixture
def probe_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from opensquilla.onboarding.probe import ProviderProbeResult

    calls: list[dict[str, Any]] = []

    async def probe(**kwargs: Any) -> ProviderProbeResult:
        calls.append(kwargs)
        return ProviderProbeResult(
            ok=True, provider_id=kwargs["provider_id"], model=kwargs["model"],
        )

    monkeypatch.setattr("opensquilla.onboarding.probe.probe_llm_provider", probe)
    return calls


@pytest.mark.parametrize("source", [None, "manual"])
@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.asyncio
async def test_primary_probe_defaults_to_manual_without_rebinding(
    tmp_path: Path, probe_calls: list[dict[str, Any]], source: str | None, ready: bool,
) -> None:
    config = _config(tmp_path, primary=True)
    selected = _select_account(ready=ready)
    params = {"providerId": "tokenrhythm", "model": "qwen3.7-max", "mode": "model"}
    if source is not None:
        params["credentialSource"] = source
    response = await get_dispatcher().dispatch(
        "manual-probe", "onboarding.provider.probe", params, _context(config),
    )
    assert response.error is None, response.error
    assert response.payload["ok"] is True
    assert len(probe_calls) == 1
    assert probe_calls[0]["api_key"] == _MANUAL_KEY
    assert probe_calls[0]["base_url"] == _UAT_BASE
    assert bridge.account_state() == selected


@pytest.mark.parametrize("primary", [False, True])
@pytest.mark.parametrize("method", ["onboarding.provider.probe", _PRIMARY_DISCOVER])
@pytest.mark.asyncio
async def test_explicit_account_probe_and_discovery_ignore_manual_connection_overrides(
    tmp_path: Path, probe_calls: list[dict[str, Any]], catalog_calls: list[dict[str, Any]],
    primary: bool, method: str,
) -> None:
    config = _config(tmp_path, primary=primary)
    before = config.model_dump()
    selected = _select_account(source="manual", selectedAccountId="previous-account")
    response = await get_dispatcher().dispatch(
        "account-connection", method,
        {"providerId": "tokenrhythm", "credentialSource": "account", "model": "qwen3.7-max",
         "apiKey": _DRAFT_KEY, "apiKeyEnv": "TOKENRHYTHM_API_KEY",
         "baseUrl": "https://untrusted.example/v1", "proxy": "http://untrusted.example:8888"},
        _context(config),
    )
    assert response.error is None, response.error
    assert response.payload["ok"] is True
    calls = probe_calls if method.endswith(".probe") else catalog_calls
    assert len(calls) == 1
    assert calls[0]["api_key"] == _LEASE
    assert calls[0]["base_url"] == bridge.OFFICIAL_BASE
    assert calls[0]["proxy"] == ""
    if method.endswith(".probe"):
        assert calls[0]["api_key_env"] == ""
        assert calls[0]["allow_default_api_key_env"] is False
    assert bridge.account_state() == selected
    assert config.model_dump() == before
    assert not Path(config.config_path).exists()
    assert _LEASE not in repr(response.payload)


@pytest.mark.parametrize("missing_bridge", [False, True])
@pytest.mark.parametrize("method", ["onboarding.provider.probe", _PRIMARY_DISCOVER])
@pytest.mark.asyncio
async def test_explicit_account_operation_never_falls_back_to_manual_when_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    probe_calls: list[dict[str, Any]], catalog_calls: list[dict[str, Any]],
    missing_bridge: bool, method: str,
) -> None:
    config = _config(tmp_path, primary=True)
    before = config.model_dump()
    selected = None if missing_bridge else _select_account(source="manual", ready=False)
    monkeypatch.setenv("TOKENRHYTHM_API_KEY", "synthetic-env-key")
    response = await get_dispatcher().dispatch(
        "unavailable-account", method,
        {"providerId": "tokenrhythm", "credentialSource": "account", "model": "qwen3.7-max",
         "apiKey": _DRAFT_KEY, "baseUrl": _UAT_BASE},
        _context(config),
    )
    if missing_bridge:
        assert response.error is not None
        assert response.error.code == "UNAVAILABLE"
    else:
        assert response.error is None, response.error
        assert response.payload["ok"] is False
        assert response.payload["failureKind"] == "auth_invalid"
    assert probe_calls == catalog_calls == []
    assert config.model_dump() == before
    assert bridge.account_state() == selected


@pytest.mark.parametrize("method", ["onboarding.provider.probe", _PRIMARY_DISCOVER])
@pytest.mark.asyncio
async def test_account_source_rejects_non_account_provider(
    tmp_path: Path, probe_calls: list[dict[str, Any]], catalog_calls: list[dict[str, Any]],
    method: str,
) -> None:
    _select_account()
    response = await get_dispatcher().dispatch(
        "wrong-account-provider", method,
        {"providerId": "openai", "credentialSource": "account", "model": "gpt-4o"},
        _context(_config(tmp_path)),
    )
    assert response.error is not None
    assert response.error.code == "INVALID_REQUEST"
    assert probe_calls == catalog_calls == []
