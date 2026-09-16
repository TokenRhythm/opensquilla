"""Redaction round-trip + D19 secret-inheritance regression tests.

Pins the behavior of the consolidated secret-inheritance service
(``opensquilla.application.config_secrets``) and proves that every config-mutation
surface routes through it identically:

* the redaction round-trip restores a redaction *marker* back to the stored
  secret without ever exposing the value;
* the D19 inherit-then-clear-explicit rule keeps an inherited secret marked
  and unmarks a path that received an explicit new value;
* no secret value ever reaches a wire response, a persisted config file, or a
  structlog event.

A single distinctive sentinel secret is used throughout so any leak is
unambiguous. These are offline, credential-free tests.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest
import structlog

import opensquilla.gateway.rpc_config  # noqa: F401  ensures config.* handlers register
from opensquilla.application import config_secrets
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.config_persistence import persist_gateway_config
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.onboarding.mutations import upsert_llm_provider
from opensquilla.onboarding.redaction import REDACTED_PLACEHOLDER

# A secret that must never appear in any wire response, persisted file, or log.
SENTINEL_SECRET = "sk-SEN71NEL-do-not-leak-Zz9"
MARKER = config_secrets.REDACTED_PUBLIC_VALUE


def _admin_ctx(config: GatewayConfig) -> RpcContext:
    return RpcContext(
        conn_id="t",
        config=config,
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
    )


# --- Part 1: the consolidated service in isolation (the D19 rule) -----------


def test_marker_constant_matches_read_side_redaction() -> None:
    # The write-side marker must equal the read-side redaction value so a
    # public config round-trips.
    from opensquilla.gateway.config import _REDACTED

    assert config_secrets.REDACTED_PUBLIC_VALUE == _REDACTED


def test_restore_redacted_values_inherits_marker_at_secret_path() -> None:
    payload = {"remote": {"api_key": MARKER, "model": "new-model"}}
    source = {"remote": {"api_key": SENTINEL_SECRET, "model": "old-model"}}

    restored, redacted_paths = config_secrets.restore_redacted_values(payload, source)

    # Marker inherited the stored secret; explicit non-secret value replaced.
    assert restored["remote"]["api_key"] == SENTINEL_SECRET
    assert restored["remote"]["model"] == "new-model"
    assert redacted_paths == {"remote.api_key"}


def test_restore_redacted_values_explicit_new_secret_passes_through() -> None:
    payload = {"remote": {"api_key": "sk-brand-new"}}
    source = {"remote": {"api_key": SENTINEL_SECRET}}

    restored, redacted_paths = config_secrets.restore_redacted_values(payload, source)

    assert restored["remote"]["api_key"] == "sk-brand-new"
    assert redacted_paths == set()


def test_restore_redacted_values_literal_marker_at_nonsecret_path_is_kept() -> None:
    # "[redacted]" at a non-secret path is a legitimate value, not a marker.
    payload = {"remote": {"model": MARKER}}
    source = {"remote": {"model": "old"}}

    restored, redacted_paths = config_secrets.restore_redacted_values(payload, source)

    assert restored["remote"]["model"] == MARKER
    assert redacted_paths == set()


def test_restore_redacted_values_marker_without_stored_secret_raises() -> None:
    with pytest.raises(ValueError, match="no existing secret"):
        config_secrets.restore_redacted_values(
            {"remote": {"api_key": MARKER}}, {"remote": {}}
        )


def test_inherit_then_clear_explicit_applies_d19_rule() -> None:
    old = GatewayConfig()
    old.mark_runtime_secret("llm.api_key")
    old.mark_runtime_secret("search_api_key")
    new = GatewayConfig()

    # The path that received an explicit new value is cleared; the inherited
    # (marker-preserved) path keeps its runtime-secret marker.
    config_secrets.inherit_then_clear_explicit(old, new, {"search_api_key"})

    assert "llm.api_key" in new._runtime_secret_paths
    assert "search_api_key" not in new._runtime_secret_paths


def test_inherit_runtime_secrets_none_source_is_noop() -> None:
    target = GatewayConfig()
    target.mark_runtime_secret("llm.api_key")
    config_secrets.inherit_runtime_secrets(None, target)
    # A None source must not wipe the target's markers.
    assert "llm.api_key" in target._runtime_secret_paths


# --- Part 2: RPC surface (config.get / config.apply) round-trip -------------


@pytest.mark.asyncio
async def test_rpc_secret_reads_back_as_marker_never_value(tmp_path) -> None:
    cfg = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        memory={"embedding": {"remote": {"api_key": SENTINEL_SECRET}}},
    )

    res = await get_dispatcher().dispatch("r1", "config.get", {}, _admin_ctx(cfg))

    assert res.error is None, res.error
    assert res.payload["memory"]["embedding"]["remote"]["api_key"] == MARKER
    assert SENTINEL_SECRET not in json.dumps(res.payload)


@pytest.mark.asyncio
async def test_rpc_marker_apply_preserves_secret_and_never_leaks(tmp_path) -> None:
    config_path = tmp_path / "c.toml"
    cfg = GatewayConfig(
        config_path=str(config_path),
        memory={"embedding": {"remote": {"api_key": SENTINEL_SECRET}}},
    )
    dispatcher = get_dispatcher()

    get_res = await dispatcher.dispatch("r1", "config.get", {}, _admin_ctx(cfg))
    assert get_res.payload["memory"]["embedding"]["remote"]["api_key"] == MARKER

    apply_res = await dispatcher.dispatch(
        "r2", "config.apply", {"config": get_res.payload}, _admin_ctx(cfg)
    )

    assert apply_res.error is None, apply_res.error
    # Marker resubmit inherited the stored secret: value unchanged.
    assert cfg.memory.embedding.remote.api_key == SENTINEL_SECRET
    # Never echoed on the wire; never written to disk as the marker string.
    assert SENTINEL_SECRET not in json.dumps(apply_res.payload)
    assert MARKER not in config_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_rpc_runtime_marked_secret_survives_and_is_not_persisted(tmp_path) -> None:
    config_path = tmp_path / "c.toml"
    cfg = GatewayConfig(
        config_path=str(config_path),
        llm={"provider": "openai", "model": "gpt-x", "api_key": SENTINEL_SECRET},
    )
    # Emulate an env-sourced runtime-secret marker (as config_store stamps).
    cfg.mark_runtime_secret("llm.api_key")
    dispatcher = get_dispatcher()

    get_res = await dispatcher.dispatch("r1", "config.get", {}, _admin_ctx(cfg))
    assert get_res.payload["llm"]["api_key"] == MARKER

    apply_res = await dispatcher.dispatch(
        "r2", "config.apply", {"config": get_res.payload}, _admin_ctx(cfg)
    )

    assert apply_res.error is None, apply_res.error
    # Secret preserved by value AND marker; marker keeps it off disk.
    assert cfg.llm.api_key == SENTINEL_SECRET
    assert "llm.api_key" in cfg._runtime_secret_paths
    assert SENTINEL_SECRET not in config_path.read_text(encoding="utf-8")
    assert SENTINEL_SECRET not in json.dumps(apply_res.payload)


@pytest.mark.asyncio
async def test_rpc_explicit_new_secret_replaces_and_clears_marker(tmp_path) -> None:
    config_path = tmp_path / "c.toml"
    cfg = GatewayConfig(
        config_path=str(config_path),
        memory={"embedding": {"remote": {"api_key": "sk-original"}}},
    )
    cfg.mark_runtime_secret("memory.embedding.remote.api_key")

    res = await get_dispatcher().dispatch(
        "r1",
        "config.set",
        {"path": "memory.embedding.remote.api_key", "value": SENTINEL_SECRET},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    # Explicit value replaced the stored secret and cleared its marker, so it
    # is now persisted (the value belongs on disk once explicitly provided).
    assert cfg.memory.embedding.remote.api_key == SENTINEL_SECRET
    assert "memory.embedding.remote.api_key" not in cfg._runtime_secret_paths
    persisted = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["memory"]["embedding"]["remote"]["api_key"] == SENTINEL_SECRET
    # The response body never carries the value.
    assert SENTINEL_SECRET not in json.dumps(res.payload)


# --- Part 3: onboarding mutation surface ------------------------------------


def test_onboarding_blank_key_inherits_stored_secret() -> None:
    res1 = upsert_llm_provider(
        GatewayConfig(), provider_id="openrouter", model="m1", api_key=SENTINEL_SECRET
    )
    assert res1.config.llm.api_key == SENTINEL_SECRET

    # Re-saving the same provider with a blank key keeps the stored secret.
    res2 = upsert_llm_provider(
        res1.config, provider_id="openrouter", model="m2", api_key=""
    )

    assert res2.config.llm.api_key == SENTINEL_SECRET
    assert res2.public_payload["api_key"] == REDACTED_PLACEHOLDER
    assert SENTINEL_SECRET not in json.dumps(res2.public_payload)


def test_onboarding_explicit_key_replaces_and_clears_marker() -> None:
    res1 = upsert_llm_provider(
        GatewayConfig(), provider_id="openrouter", model="m1", api_key="sk-old"
    )
    # Emulate a runtime-secret marker carried on the stored config.
    res1.config.mark_runtime_secret("llm.api_key")

    res2 = upsert_llm_provider(
        res1.config, provider_id="openrouter", model="m1", api_key=SENTINEL_SECRET
    )

    assert res2.config.llm.api_key == SENTINEL_SECRET
    assert "llm.api_key" not in res2.config._runtime_secret_paths
    assert SENTINEL_SECRET not in json.dumps(res2.public_payload)


# --- Part 4: no secret value ever reaches a structlog event -----------------


# --- Part 5: env-absorbed auth token must not be baked to disk -------------


async def test_config_set_does_not_bake_env_auth_token_into_toml(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENSQUILLA_AUTH_TOKEN", "tok-env-original")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[auth]\nmode = "token"\n')

    cfg = GatewayConfig.load(cfg_path)
    assert cfg.auth.token == "tok-env-original"

    res = await get_dispatcher().dispatch(
        "r1", "config.set", {"path": "port", "value": 18795}, _admin_ctx(cfg)
    )
    assert res.error is None, res.error

    text = cfg_path.read_text()
    assert tomllib.loads(text)["port"] == 18795
    assert "tok-env-original" not in text


async def test_env_auth_token_rotation_survives_config_save(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_AUTH_TOKEN", "tok-env-original")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[auth]\nmode = "token"\n')

    cfg = GatewayConfig.load(cfg_path)
    await get_dispatcher().dispatch(
        "r1", "config.set", {"path": "port", "value": 18795}, _admin_ctx(cfg)
    )

    monkeypatch.setenv("OPENSQUILLA_AUTH_TOKEN", "tok-env-rotated")
    rebooted = GatewayConfig.load(cfg_path)
    assert rebooted.auth.token == "tok-env-rotated"


@pytest.mark.skipif(os.name == "nt", reason="Windows st_mode does not represent DACLs")
def test_persist_config_fresh_file_has_owner_only_posix_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_AUTH_TOKEN", "tok-env-original")
    old_umask = os.umask(0o022)
    try:
        cfg = GatewayConfig()
        cfg_path = tmp_path / "fresh" / "config.toml"
        cfg.config_path = str(cfg_path)
        persist_gateway_config(cfg)
    finally:
        os.umask(old_umask)

    mode = Path(cfg_path).stat().st_mode & 0o777
    assert mode & 0o077 == 0


@pytest.mark.asyncio
async def test_secret_never_appears_in_any_log_event(tmp_path) -> None:
    cfg = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        memory={"embedding": {"remote": {"api_key": SENTINEL_SECRET}}},
    )
    dispatcher = get_dispatcher()

    with structlog.testing.capture_logs() as captured:
        get_res = await dispatcher.dispatch("r1", "config.get", {}, _admin_ctx(cfg))
        await dispatcher.dispatch(
            "r2", "config.apply", {"config": get_res.payload}, _admin_ctx(cfg)
        )
        # Onboarding surface in the same capture window.
        upsert_llm_provider(
            cfg, provider_id="openrouter", model="m", api_key=SENTINEL_SECRET
        )

    blob = json.dumps(captured, default=repr)
    assert SENTINEL_SECRET not in blob
