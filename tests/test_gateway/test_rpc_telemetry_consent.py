from __future__ import annotations

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
    desktop_early_spool_root,
)
from opensquilla.telemetry.growth.state import (
    gateway_growth_milestone_state_path,
    growth_cohort_state_path,
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
    real_reasons = rpc_telemetry.telemetry_scope_forced_off_reasons
    real_resolve = consent_transition.resolve_scope_consent

    def filtered_env() -> dict[str, str]:
        return {key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"}

    monkeypatch.setattr(
        rpc_telemetry,
        "telemetry_scope_forced_off_reasons",
        lambda scope, *, config: real_reasons(
            scope,
            config=config,
            env=filtered_env(),
        ),
    )
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
            product_analytics_notice_version="growth-v1",
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
    return RpcContext(
        conn_id="telemetry-consent-test",
        principal=_principal(*scopes),
        config=config,
        telemetry_consent_coordinator=coordinator,
        telemetry_consent_cleanup=cleanup,
        telemetry_growth_eligibility_cleanup=eligibility_cleanup,
    )


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


async def test_server_authors_current_notice_and_utc_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rpc_telemetry, "_utc_now", lambda: _NOW)
    config = _config(tmp_path)

    result = await _handle_telemetry_consent_set(
        {"scope": "reliability", "enabled": True},
        _context(config),
    )

    assert result == {
        "scope": "reliability",
        "enabled": True,
        "noticeVersion": "reliability-v1",
        "consentedAtUtc": _NOW,
        "changed": True,
        "cleanupPerformed": False,
        "cleanupComplete": True,
    }
    assert config.privacy.reliability_diagnostics_enabled is True
    assert config.privacy.reliability_notice_version == "reliability-v1"
    assert config.privacy.reliability_consented_at_utc == _NOW
    persisted = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert persisted["privacy"]["reliability_diagnostics_enabled"] is True
    assert persisted["privacy"]["reliability_notice_version"] == "reliability-v1"
    assert persisted["privacy"]["reliability_consented_at_utc"] == _NOW
    assert _mirror(config)["reliability"] == {
        "enabled": True,
        "notice_version": "reliability-v1",
        "consented_at_utc": _NOW,
        "forced_off": False,
    }


async def test_declined_scope_is_cleaned_before_it_can_be_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, reliability=False)
    order: list[str] = []

    async def cleanup(*, scope: TelemetryScope, config: GatewayConfig) -> None:
        assert scope is TelemetryScope.RELIABILITY
        assert config.privacy.reliability_diagnostics_enabled is False
        assert _mirror(config)["reliability"]["enabled"] is False
        order.append("cleanup")

    real_persist = rpc_telemetry._persist_config

    def persist(candidate: GatewayConfig) -> None:
        order.append("persist")
        real_persist(candidate)

    monkeypatch.setattr(rpc_telemetry, "_persist_config", persist)
    monkeypatch.setattr(rpc_telemetry, "_utc_now", lambda: _NOW)

    result = await _handle_telemetry_consent_set(
        {"scope": "reliability", "enabled": True},
        _context(config, cleanup=cleanup),
    )

    assert order == ["cleanup", "persist"]
    assert result["cleanupPerformed"] is True
    assert config.privacy.reliability_diagnostics_enabled is True


async def test_pre_enable_cleanup_failure_keeps_decline_closed(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, reliability=False)

    async def cleanup(**_: object) -> None:
        raise OSError("synthetic cleanup failure")

    with pytest.raises(RpcHandlerError) as raised:
        await _handle_telemetry_consent_set(
            {"scope": "reliability", "enabled": True},
            _context(config, cleanup=cleanup),
        )

    assert raised.value.code == "TELEMETRY_CONSENT_CLEANUP_FAILED"
    assert raised.value.accepted is False
    assert config.privacy.reliability_diagnostics_enabled is False
    assert _mirror(config)["reliability"]["enabled"] is False
    assert not (tmp_path / "config.toml").exists()


async def test_persist_failure_does_not_open_gate_or_run_withdrawal_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, reliability=True)
    cleanup_calls: list[TelemetryScope] = []

    async def cleanup(*, scope: TelemetryScope, config: GatewayConfig) -> None:
        cleanup_calls.append(scope)

    def fail_persist(_: GatewayConfig) -> None:
        raise OSError("synthetic persist failure")

    monkeypatch.setattr(rpc_telemetry, "_persist_config", fail_persist)
    with pytest.raises(RpcHandlerError) as raised:
        await _handle_telemetry_consent_set(
            {"scope": "reliability", "enabled": False},
            _context(config, cleanup=cleanup),
        )

    assert raised.value.code == "TELEMETRY_CONSENT_PERSIST_FAILED"
    assert raised.value.accepted is False
    assert config.privacy.reliability_diagnostics_enabled is True
    assert cleanup_calls == []
    # Desktop remains conservatively closed after the failed config commit.
    assert _mirror(config)["reliability"]["enabled"] is False


async def test_withdrawal_persists_and_hot_closes_before_cleanup(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, reliability=True)
    observed: list[str] = []

    async def cleanup(*, scope: TelemetryScope, config: GatewayConfig) -> None:
        assert scope is TelemetryScope.RELIABILITY
        assert config.privacy.reliability_diagnostics_enabled is False
        assert config.privacy.reliability_notice_version is None
        assert config.privacy.reliability_consented_at_utc is None
        persisted = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
        assert persisted["privacy"]["reliability_diagnostics_enabled"] is False
        assert "reliability_notice_version" not in persisted["privacy"]
        assert "reliability_consented_at_utc" not in persisted["privacy"]
        observed.append("closed-before-cleanup")

    result = await _handle_telemetry_consent_set(
        {"scope": "reliability", "enabled": False},
        _context(config, cleanup=cleanup),
    )

    assert observed == ["closed-before-cleanup"]
    assert result["changed"] is True
    assert result["cleanupComplete"] is True


async def test_cleanup_failure_after_withdrawal_leaves_durable_gate_closed(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, reliability=True)

    async def cleanup(*, config: GatewayConfig, **_: object) -> None:
        assert config.privacy.reliability_diagnostics_enabled is False
        raise OSError("synthetic cleanup failure")

    with pytest.raises(RpcHandlerError) as raised:
        await _handle_telemetry_consent_set(
            {"scope": "reliability", "enabled": False},
            _context(config, cleanup=cleanup),
        )

    assert raised.value.code == "TELEMETRY_CONSENT_CLEANUP_FAILED"
    assert raised.value.accepted is True
    assert raised.value.details["cleanupComplete"] is False
    assert config.privacy.reliability_diagnostics_enabled is False
    persisted = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert persisted["privacy"]["reliability_diagnostics_enabled"] is False


async def test_repeated_withdrawal_retries_cleanup_without_rewriting_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, reliability=True)
    cleanup_calls = 0
    persist_calls = 0

    async def cleanup(**_: object) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    real_persist = rpc_telemetry._persist_config

    def persist(candidate: GatewayConfig) -> None:
        nonlocal persist_calls
        persist_calls += 1
        real_persist(candidate)

    monkeypatch.setattr(rpc_telemetry, "_persist_config", persist)
    context = _context(config, cleanup=cleanup)
    first = await _handle_telemetry_consent_set({"scope": "reliability", "enabled": False}, context)
    second = await _handle_telemetry_consent_set(
        {"scope": "reliability", "enabled": False}, context
    )

    assert first["changed"] is True
    assert second["changed"] is False
    assert cleanup_calls == 2
    assert persist_calls == 1


async def test_growth_default_cleanup_removes_identity_spool_and_calls_eligibility_hook(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, reliability=True, growth=True)
    identity_path = identity_state_path(TelemetryIdentityKind.ANALYTICS_USER, config=config)
    load_or_create_identity(identity_path, TelemetryIdentityKind.ANALYTICS_USER)
    cohort_path = growth_cohort_state_path(config=config)
    milestone_path = gateway_growth_milestone_state_path(config=config)
    cohort_path.write_text("{}", encoding="utf-8")
    milestone_path.write_text("{}", encoding="utf-8")
    spool_root = desktop_early_spool_root(str(config.state_dir))
    growth_spool = spool_root / "growth"
    reliability_spool = spool_root / "reliability"
    growth_spool.mkdir(parents=True)
    reliability_spool.mkdir()
    for name in ("one.ready", "two.processing.7", ".three.7.tmp"):
        (growth_spool / name).write_text("synthetic", encoding="utf-8")
    other_scope = reliability_spool / "other.ready"
    other_scope.write_text("keep", encoding="utf-8")
    eligibility_calls = 0

    async def clear_eligibility(*, config: GatewayConfig) -> None:
        nonlocal eligibility_calls
        assert config.privacy.product_analytics_enabled is False
        eligibility_calls += 1

    await _handle_telemetry_consent_set(
        {"scope": "growth", "enabled": False},
        _context(config, eligibility_cleanup=clear_eligibility),
    )

    assert not identity_path.exists()
    assert not cohort_path.exists()
    assert not milestone_path.exists()
    assert not growth_spool.exists()
    assert other_scope.exists()
    assert eligibility_calls == 1
    mirror = _mirror(config)
    assert mirror["growth"]["enabled"] is False
    assert mirror["reliability"]["enabled"] is True


async def test_growth_cleanup_with_unmanaged_spool_is_accepted_but_incomplete(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, growth=True)
    identity_path = identity_state_path(TelemetryIdentityKind.ANALYTICS_USER, config=config)
    load_or_create_identity(identity_path, TelemetryIdentityKind.ANALYTICS_USER)
    spool_root = desktop_early_spool_root(str(config.state_dir))
    growth_spool = spool_root / "growth"
    growth_spool.mkdir(parents=True)
    (growth_spool / "one.ready").write_text("synthetic", encoding="utf-8")
    (growth_spool / "keep.local").write_text("keep", encoding="utf-8")

    with pytest.raises(RpcHandlerError) as raised:
        await _handle_telemetry_consent_set(
            {"scope": "growth", "enabled": False},
            _context(config),
        )

    assert raised.value.code == "TELEMETRY_CONSENT_CLEANUP_FAILED"
    assert raised.value.accepted is True
    assert raised.value.details["cleanupComplete"] is False
    assert config.privacy.product_analytics_enabled is False
    assert not identity_path.exists()
    quarantines = tuple(spool_root.glob(".revoked-growth-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "keep.local").read_text(encoding="utf-8") == "keep"
    assert not (quarantines[0] / "one.ready").exists()


async def test_forced_off_policy_allows_revoke_but_blocks_new_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    declined = _config(tmp_path / "declined", reliability=False)
    with pytest.raises(RpcHandlerError) as blocked:
        await _handle_telemetry_consent_set(
            {"scope": "reliability", "enabled": True},
            _context(declined),
        )
    assert blocked.value.code == "TELEMETRY_CONSENT_FORCED_OFF"
    assert declined.privacy.reliability_diagnostics_enabled is False

    granted = _config(tmp_path / "granted", reliability=True)

    async def cleanup(**_: object) -> None:
        return None

    result = await _handle_telemetry_consent_set(
        {"scope": "reliability", "enabled": False},
        _context(granted, cleanup=cleanup),
    )
    assert result["enabled"] is False
    assert granted.privacy.reliability_diagnostics_enabled is False


async def test_unsafe_mirror_fails_before_config_or_cleanup_changes(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, reliability=True)
    target = desktop_consent_mirror_path(str(config.state_dir))
    target.parent.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("unchanged", encoding="utf-8")
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")
    cleanup_calls = 0

    async def cleanup(**_: object) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    with pytest.raises(RpcHandlerError) as raised:
        await _handle_telemetry_consent_set(
            {"scope": "reliability", "enabled": False},
            _context(config, cleanup=cleanup),
        )

    assert raised.value.code == "TELEMETRY_CONSENT_MIRROR_FAILED"
    assert raised.value.accepted is False
    assert config.privacy.reliability_diagnostics_enabled is True
    assert cleanup_calls == 0
    assert outside.read_text(encoding="utf-8") == "unchanged"


async def test_final_mirror_failure_is_accepted_and_retryable_without_config_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(rpc_telemetry, "_utc_now", lambda: _NOW)
    real_write = consent_transition.write_desktop_consent_mirror
    mirror_writes = 0

    def flaky_write(*args: object, **kwargs: object) -> Path:
        nonlocal mirror_writes
        mirror_writes += 1
        if mirror_writes == 2:
            raise OSError("synthetic final mirror failure")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(
        consent_transition,
        "write_desktop_consent_mirror",
        flaky_write,
    )
    with pytest.raises(RpcHandlerError) as raised:
        await _handle_telemetry_consent_set(
            {"scope": "reliability", "enabled": True},
            _context(config),
        )
    assert raised.value.code == "TELEMETRY_CONSENT_MIRROR_FAILED"
    assert raised.value.accepted is True
    assert config.privacy.reliability_diagnostics_enabled is True
    assert _mirror(config)["reliability"]["enabled"] is False

    retried = await _handle_telemetry_consent_set(
        {"scope": "reliability", "enabled": True},
        _context(config),
    )
    assert retried["changed"] is False
    assert _mirror(config)["reliability"]["enabled"] is True


async def test_generic_config_mutations_cannot_change_consent_records(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, reliability=True, growth=False)
    admin = _context(config, scopes=("operator.admin",))

    with pytest.raises(ValueError, match="read-only"):
        await _handle_config_set(
            {"path": "privacy.reliability_diagnostics_enabled", "value": False},
            admin,
        )
    with pytest.raises(ValueError, match="not safe"):
        await _handle_config_patch_safe(
            {"patches": {"privacy.product_analytics_enabled": True}},
            admin,
        )

    await _handle_config_patch(
        {
            "patches": {
                "privacy.reliability_notice_version": "forged-v9",
                "privacy.product_analytics_enabled": True,
            },
            "patch": {
                "privacy": {
                    "reliability_consented_at_utc": "1999-01-01T00:00:00Z",
                    "product_analytics_notice_version": "forged-v9",
                }
            },
        },
        admin,
    )
    assert config.privacy.reliability_diagnostics_enabled is True
    assert config.privacy.reliability_notice_version == "reliability-v1"
    assert config.privacy.reliability_consented_at_utc == _NOW
    assert config.privacy.product_analytics_enabled is False
    assert config.privacy.product_analytics_notice_version is None

    replacement = config.model_dump(mode="python")
    replacement["privacy"]["reliability_diagnostics_enabled"] = False
    replacement["privacy"]["product_analytics_enabled"] = True
    replacement["privacy"]["product_analytics_notice_version"] = "forged-v9"
    replacement["privacy"]["product_analytics_consented_at_utc"] = _NOW
    await _handle_config_apply({"config": replacement}, admin)
    assert config.privacy.reliability_diagnostics_enabled is True
    assert config.privacy.product_analytics_enabled is False


async def test_config_reload_preserves_live_server_owned_consent(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, reliability=True, growth=False)
    (tmp_path / "config.toml").write_text(
        "\n".join(
            (
                "[privacy]",
                "reliability_diagnostics_enabled = false",
                "product_analytics_enabled = true",
                'product_analytics_notice_version = "growth-v1"',
                f'product_analytics_consented_at_utc = "{_NOW}"',
                "",
            )
        ),
        encoding="utf-8",
    )

    result = await _handle_config_reload(None, _context(config, scopes=("operator.admin",)))

    assert result["ok"] is True
    assert config.privacy.reliability_diagnostics_enabled is True
    assert config.privacy.reliability_notice_version == "reliability-v1"
    assert config.privacy.product_analytics_enabled is False
    assert config.privacy.product_analytics_notice_version is None
