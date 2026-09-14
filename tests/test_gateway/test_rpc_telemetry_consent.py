from __future__ import annotations

import asyncio
import json
import os
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import opensquilla.gateway.rpc_telemetry as rpc_telemetry
import opensquilla.telemetry.consent_transition as consent_transition
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.rpc_config import (
    _handle_config_apply,
    _handle_config_patch,
    _handle_config_patch_safe,
    _handle_config_reload,
    _handle_config_set,
)
from opensquilla.gateway.rpc_telemetry import (
    _handle_client_launch_record,
    _handle_product_active_record,
    _handle_telemetry_consent_set,
)
from opensquilla.gateway.scopes import METHOD_SCOPES, WRITE_SCOPE
from opensquilla.telemetry.consent import TelemetryScope
from opensquilla.telemetry.coordination import (
    ScopeConsentCoordinator,
    scope_consent_coordinator_for,
)
from opensquilla.telemetry.desktop_state import (
    desktop_consent_mirror_path,
)
from opensquilla.telemetry.growth.state import (
    growth_cohort_state_path,
    write_active_growth_cohort,
)
from opensquilla.telemetry.identity import (
    TelemetryIdentityKind,
    identity_state_path,
    load_or_create_identity,
)

_NOW = "2026-09-02T08:09:10.111Z"
_VETO_ENV = (
    "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY",
    "OPENSQUILLA_TELEMETRY_DISABLED",
    "OPENSQUILLA_PRIVACY_DISABLE_RELIABILITY_DIAGNOSTICS",
    "OPENSQUILLA_PRIVACY_DISABLE_PRODUCT_ANALYTICS",
    "DO_NOT_TRACK",
    "CI",
    "GITHUB_ACTIONS",
    "PYTEST_CURRENT_TEST",
    "OPENSQUILLA_TESTING",
)


@pytest.fixture(autouse=True)
def _clear_runtime_vetoes(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _VETO_ENV:
        monkeypatch.delenv(name, raising=False)
    real_resolve = consent_transition.resolve_scope_consent

    def filtered_env() -> dict[str, str]:
        return {key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"}

    monkeypatch.setattr(
        consent_transition,
        "resolve_scope_consent",
        lambda scope, *, config: real_resolve(
            scope,
            config=config,
            env=filtered_env(),
        ),
    )


def _config(
    tmp_path: Path,
    *,
    reliability: bool | None = None,
    growth: bool | None = None,
) -> GatewayConfig:
    privacy: dict[str, object] = {
        "reliability_diagnostics_enabled": reliability,
        "product_analytics_enabled": growth,
    }
    if reliability is True:
        privacy.update(
            reliability_notice_version="reliability-v1",
            reliability_consented_at_utc=_NOW,
        )
    if growth is True:
        privacy.update(
            product_analytics_notice_version="growth-v2",
            product_analytics_consented_at_utc=_NOW,
        )
    return GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        state_dir=str(tmp_path / "state"),
        privacy=privacy,
    )


def _principal(*scopes: str) -> Principal:
    return Principal(
        role="operator",
        scopes=frozenset(scopes),
        is_owner="operator.admin" in scopes,
        authenticated=True,
    )


def _context(
    config: GatewayConfig,
    *,
    cleanup: Callable[..., object] | None = None,
    eligibility_cleanup: Callable[..., object] | None = None,
    coordinator: ScopeConsentCoordinator | None = None,
    scopes: tuple[str, ...] = ("operator.write",),
) -> RpcContext:
    context = RpcContext(
        conn_id="telemetry-consent-test",
        principal=_principal(*scopes),
        config=config,
    )
    # These are narrow host/test extension hooks resolved with getattr by the
    # telemetry boundary; they do not expand the shared request context schema.
    context.telemetry_consent_coordinator = coordinator
    context.telemetry_consent_cleanup = cleanup
    context.telemetry_growth_eligibility_cleanup = eligibility_cleanup
    return context


def _mirror(config: GatewayConfig) -> dict[str, Any]:
    path = desktop_consent_mirror_path(str(config.state_dir))
    return json.loads(path.read_text(encoding="utf-8"))


async def test_client_launch_rpc_supplies_hard_coded_dimensions(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    class Sink:
        async def record_client_launch(self, **kwargs: object) -> bool:
            calls.append(kwargs)
            return True

    context = _context(_config(tmp_path), scopes=("operator.admin",))
    context.turn_runner = type("Runner", (), {"growth_event_sink": Sink()})()

    result = await _handle_client_launch_record({}, context)

    assert result == {"recorded": True}
    assert calls == [
        {
            "surface": "tui",
            "entrypoint": "chat",
            "execution_mode": "gateway",
        }
    ]


async def test_client_launch_rpc_rejects_client_dimensions(tmp_path: Path) -> None:
    context = _context(_config(tmp_path), scopes=("operator.admin",))

    with pytest.raises(RpcHandlerError) as raised:
        await _handle_client_launch_record({"surface": "cli"}, context)

    assert raised.value.code == "INVALID_REQUEST"


@pytest.mark.parametrize("surface", ["desktop", "web", "tui", "cli"])
async def test_product_active_rpc_only_forwards_surface(tmp_path: Path, surface: str) -> None:
    calls: list[dict[str, object]] = []

    class Sink:
        async def record_product_active(self, **kwargs: object) -> bool:
            calls.append(kwargs)
            return True

    context = _context(_config(tmp_path), scopes=("operator.admin",))
    context.turn_runner = type("Runner", (), {"growth_event_sink": Sink()})()
    assert await _handle_product_active_record({"surface": surface}, context) == {"recorded": True}
    assert calls == [{"surface": surface}]
    assert METHOD_SCOPES["telemetry.product_active.record"] == WRITE_SCOPE


@pytest.mark.parametrize("params", [
    None, {}, {"surface": "unknown"}, {"surface": 1},
    {"surface": "web", "analytics_user_id": "forged"},
    {"surface": "desktop", "occurred_at_utc": _NOW},
    {"surface": "tui", "prompt": "synthetic"},
])
async def test_product_active_rpc_rejects_extra_fields(tmp_path: Path, params: Any) -> None:
    context = _context(_config(tmp_path), scopes=("operator.admin",))
    with pytest.raises(RpcHandlerError) as raised:
        await _handle_product_active_record(params, context)
    assert raised.value.code == "INVALID_REQUEST"


async def test_product_active_rpc_requires_authenticated_owner(tmp_path: Path) -> None:
    context = _context(_config(tmp_path), scopes=("operator.write",))
    with pytest.raises(RpcHandlerError) as raised:
        await _handle_product_active_record({"surface": "web"}, context)
    assert raised.value.code == "UNAUTHORIZED"


async def test_product_active_rpc_without_sink_is_noop(tmp_path: Path) -> None:
    context = _context(_config(tmp_path), scopes=("operator.admin",))
    assert await _handle_product_active_record({"surface": "web"}, context) == {"recorded": False}


@pytest.mark.parametrize(
    "params",
    [
        None,
        {},
        {"scope": "reliability"},
        {"enabled": True},
        {"scope": "unknown", "enabled": True},
        {"scope": "reliability", "enabled": 1},
        {"scope": "growth", "enabled": "true"},
        {"scope": TelemetryScope.RELIABILITY, "enabled": True},
        {"scope": "reliability", "enabled": True, "noticeVersion": "forged"},
        {"scope": "growth", "enabled": False, "consentedAtUtc": _NOW},
    ],
)
async def test_params_are_exact_and_client_cannot_submit_metadata(
    tmp_path: Path,
    params: Any,
) -> None:
    calls: list[TelemetryScope] = []

    async def cleanup(*, scope: TelemetryScope, config: GatewayConfig) -> None:
        calls.append(scope)

    config = _config(tmp_path)
    with pytest.raises(RpcHandlerError) as raised:
        await _handle_telemetry_consent_set(params, _context(config, cleanup=cleanup))

    assert raised.value.code == "INVALID_REQUEST"
    assert raised.value.accepted is False
    assert calls == []
    assert config.privacy.reliability_diagnostics_enabled is None
    assert config.privacy.product_analytics_enabled is None
    assert not desktop_consent_mirror_path(str(config.state_dir)).exists()


async def test_method_is_write_scoped_and_dispatcher_enforces_permission(
    tmp_path: Path,
) -> None:
    assert METHOD_SCOPES["telemetry.consent.set"] == WRITE_SCOPE
    entry = get_dispatcher().get_entry("telemetry.consent.set")
    assert entry is not None and entry.required_scope == WRITE_SCOPE

    config = _config(tmp_path)
    denied = await get_dispatcher().dispatch(
        "denied",
        "telemetry.consent.set",
        {"scope": "reliability", "enabled": False},
        _context(config, scopes=("operator.read",)),
    )
    assert denied.error is not None
    assert denied.error.code == "UNAUTHORIZED"
    assert config.privacy.reliability_diagnostics_enabled is None

    async def cleanup(**_: object) -> None:
        return None

    allowed = await get_dispatcher().dispatch(
        "allowed",
        "telemetry.consent.set",
        {"scope": "reliability", "enabled": False},
        _context(config, cleanup=cleanup),
    )
    assert allowed.error is None
    assert allowed.payload["enabled"] is False


async def test_rpc_uses_injected_or_process_shared_scope_coordinator(
    tmp_path: Path,
) -> None:
    injected_config = _config(tmp_path / "injected")
    injected = ScopeConsentCoordinator(
        lambda scope: consent_transition.resolve_scope_consent(
            scope,
            config=injected_config,
        )
    )
    assert injected.revision(TelemetryScope.RELIABILITY) == 0
    await _handle_telemetry_consent_set(
        {"scope": "reliability", "enabled": False},
        _context(injected_config, coordinator=injected),
    )
    assert injected.revision(TelemetryScope.RELIABILITY) == 1

    shared_config = _config(tmp_path / "shared")
    shared = scope_consent_coordinator_for(shared_config)
    before = shared.revision(TelemetryScope.GROWTH)
    await _handle_telemetry_consent_set(
        {"scope": "growth", "enabled": False},
        _context(shared_config),
    )
    assert scope_consent_coordinator_for(shared_config) is shared
    assert shared.revision(TelemetryScope.GROWTH) == before + 1


@pytest.mark.parametrize("scope", ["reliability", "growth"])
async def test_legacy_rpc_controls_both_streams_without_new_consent_metadata(
    tmp_path: Path,
    scope: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rpc_telemetry, "_utc_now", lambda: _NOW)
    config = _config(tmp_path)
    context = _context(config)
    disabled = await _handle_telemetry_consent_set({"scope": scope, "enabled": False}, context)
    assert disabled["enabled"] is False
    assert disabled["changed"] is True
    assert disabled["cleanupPerformed"] is False
    assert config.privacy.disable_network_observability
    assert _mirror(config)["reliability"]["enabled"] is False
    assert _mirror(config)["growth"]["enabled"] is False

    enabled = await _handle_telemetry_consent_set({"scope": scope, "enabled": True}, context)
    assert enabled == {
        "scope": scope,
        "enabled": True,
        "noticeVersion": "reliability-v1" if scope == "reliability" else "growth-v2",
        "consentedAtUtc": _NOW,
        "changed": True,
        "cleanupPerformed": False,
        "cleanupComplete": True,
    }
    assert not config.privacy.disable_network_observability
    persisted = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))["privacy"]
    assert persisted["disable_network_observability"] is False
    for stream in ("reliability", "growth"):
        mirror = _mirror(config)[stream]
        assert mirror["enabled"] is True
        assert mirror["consented_at_utc"] is None
    assert config.privacy.reliability_diagnostics_enabled is None
    assert config.privacy.product_analytics_enabled is None


async def test_pause_and_resume_preserve_existing_identity_cohort_and_queued_state(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    identity_path = identity_state_path(TelemetryIdentityKind.ANALYTICS_USER, config=config)
    identity = load_or_create_identity(identity_path, TelemetryIdentityKind.ANALYTICS_USER)
    cohort_path = growth_cohort_state_path(config=config)
    write_active_growth_cohort(cohort_path, activated_at_utc=_NOW)
    queued = identity_path.parent / "growth_metaskill_usage.json"
    queued.write_text('{"fixture":"retained"}', encoding="utf-8")

    async def forbidden_cleanup(**_: object) -> None:
        pytest.fail("The V1 upload switch must not invoke withdrawal cleanup")

    context = _context(config, cleanup=forbidden_cleanup, eligibility_cleanup=forbidden_cleanup)
    for enabled in (False, True):
        result = await _handle_telemetry_consent_set(
            {"scope": "growth", "enabled": enabled}, context,
        )
        assert result["cleanupPerformed"] is False
        assert load_or_create_identity(
            identity_path, TelemetryIdentityKind.ANALYTICS_USER,
        ).value == identity.value
        assert cohort_path.exists()
        assert queued.read_text(encoding="utf-8") == '{"fixture":"retained"}'


@pytest.mark.parametrize("method", ["legacy_rpc", "config_set"])
async def test_migrated_decline_can_be_reenabled_and_stays_enabled_after_reload(
    tmp_path: Path,
    method: str,
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[privacy]\nproduct_analytics_enabled = false\n'
        'product_analytics_notice_version = "growth-v1"\n',
        encoding="utf-8",
    )
    config = GatewayConfig.load(str(config_file), read_only=True)
    config.state_dir = str(tmp_path / "state")
    assert config.privacy.disable_network_observability
    if method == "legacy_rpc":
        await _handle_telemetry_consent_set(
            {"scope": "reliability", "enabled": True}, _context(config),
        )
    else:
        await _handle_config_set(
            {"path": "privacy.disable_network_observability", "value": False},
            _context(config, scopes=("operator.admin",)),
        )
    persisted = tomllib.loads(config_file.read_text(encoding="utf-8"))["privacy"]
    assert persisted == {"disable_network_observability": False}
    reloaded = GatewayConfig.load(str(config_file), read_only=True)
    assert not reloaded.privacy.disable_network_observability


async def test_persist_failure_keeps_both_live_gates_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)

    def fail_persist(_: object) -> None:
        raise OSError("synthetic persistence failure")

    monkeypatch.setattr(rpc_telemetry, "persist_gateway_config", fail_persist)
    with pytest.raises(RpcHandlerError) as raised:
        await _handle_telemetry_consent_set(
            {"scope": "growth", "enabled": False}, _context(config),
        )
    assert raised.value.code == "TELEMETRY_CONSENT_PERSIST_FAILED"
    assert raised.value.accepted is False
    assert not config.privacy.disable_network_observability
    assert _mirror(config)["reliability"]["enabled"] is False
    assert _mirror(config)["growth"]["enabled"] is False


async def test_preference_can_be_saved_while_environment_still_vetoes_uploads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, growth=False)
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    result = await _handle_telemetry_consent_set(
        {"scope": "growth", "enabled": True}, _context(config),
    )
    assert result["enabled"] is True
    assert not config.privacy.disable_network_observability
    for stream in ("reliability", "growth"):
        assert _mirror(config)[stream]["forced_off"] is True


async def test_unified_change_waits_for_both_scope_boundaries(tmp_path: Path) -> None:
    config = _config(tmp_path)
    coordinator = scope_consent_coordinator_for(config)
    async with coordinator.transition(TelemetryScope.GROWTH):
        change = asyncio.create_task(_handle_telemetry_consent_set(
            {"scope": "reliability", "enabled": False}, _context(config),
        ))
        await asyncio.sleep(0)
        assert not change.done()
        assert not config.privacy.disable_network_observability
    await change
    assert config.privacy.disable_network_observability
    assert coordinator.revision(TelemetryScope.RELIABILITY) == 1
    assert coordinator.revision(TelemetryScope.GROWTH) == 2


async def test_unsafe_mirror_fails_before_config_changes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = desktop_consent_mirror_path(str(config.state_dir))
    target.parent.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("unchanged", encoding="utf-8")
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(RpcHandlerError) as raised:
        await _handle_telemetry_consent_set(
            {"scope": "reliability", "enabled": False}, _context(config),
        )
    assert raised.value.code == "TELEMETRY_CONSENT_MIRROR_FAILED"
    assert raised.value.accepted is False
    assert not config.privacy.disable_network_observability
    assert outside.read_text(encoding="utf-8") == "unchanged"


async def test_final_mirror_failure_can_retry_without_config_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, growth=False)
    real_write = consent_transition.write_desktop_consent_mirror
    writes = 0

    def flaky_write(*args: Any, **kwargs: Any) -> Path:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("synthetic final mirror failure")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(consent_transition, "write_desktop_consent_mirror", flaky_write)
    with pytest.raises(RpcHandlerError) as raised:
        await _handle_telemetry_consent_set(
            {"scope": "growth", "enabled": True}, _context(config),
        )
    assert raised.value.accepted is True
    assert not config.privacy.disable_network_observability
    assert _mirror(config)["growth"]["enabled"] is False
    retry = await _handle_telemetry_consent_set(
        {"scope": "growth", "enabled": True}, _context(config),
    )
    assert retry["changed"] is False
    assert _mirror(config)["growth"]["enabled"] is True


async def test_generic_config_mutations_cannot_restore_retired_consent_records(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    context = _context(config, scopes=("operator.admin",))
    with pytest.raises(ValueError, match="read-only"):
        await _handle_config_set(
            {"path": "privacy.reliability_diagnostics_enabled", "value": False}, context,
        )
    with pytest.raises(ValueError, match="not safe"):
        await _handle_config_patch_safe(
            {"patches": {"privacy.product_analytics_enabled": True}}, context,
        )
    await _handle_config_patch(
        {"patches": {"privacy.product_analytics_notice_version": "forged-v9"}}, context,
    )
    replacement = config.model_dump(mode="python")
    replacement["privacy"]["product_analytics_enabled"] = True
    replacement["privacy"]["product_analytics_consented_at_utc"] = _NOW
    await _handle_config_apply({"config": replacement}, context)
    assert config.privacy.product_analytics_enabled is None
    assert config.privacy.product_analytics_consented_at_utc is None
    assert not config.privacy.disable_network_observability


async def test_config_reload_migrates_legacy_decline_into_global_preference(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    (tmp_path / "config.toml").write_text(
        "[privacy]\nreliability_diagnostics_enabled = false\n", encoding="utf-8",
    )
    result = await _handle_config_reload(None, _context(config, scopes=("operator.admin",)))
    assert result["ok"] is True
    assert config.privacy.disable_network_observability
    assert config.privacy.reliability_diagnostics_enabled is None
