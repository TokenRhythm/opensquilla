"""Runtime-override provenance across the RPC apply/persist lifecycle.

The sparse persister restores a field's stored value whenever it still equals
the recorded env-applied value, so boot-time env resolution never gets baked
into ``config.toml``. These tests pin the lifecycle rules that keep that
provenance state coherent on the LIVE gateway config across RPC saves,
in-place config swaps, and repeated resolves:

- an explicit operator base_url beats a boot-time env override and survives
  the save (selector sync must not clobber it right before persist);
- clearing a record on the mutation clone reaches the live config, so
  consecutive saves do not flip-flop the persisted value;
- in-place swaps (reload / config.set) drop records whose provenance no
  longer describes the new state;
- the legacy RPC empty-string sentinel is not an explicit endpoint and must
  not clear the record;
- repeated in-process resolves never chain the stored slot away from disk
  provenance.

Everything is offline and synthetic; env vars are scoped via monkeypatch.
"""

from __future__ import annotations

import tomllib

import pytest

import opensquilla.gateway.rpc_onboarding  # noqa: F401  ensures registration
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.onboarding.config_store import load_config

_ENV_URL = "https://corp-proxy.example/v1"
_USER_URL = "https://user.example/v1"


class _RecordingSelector:
    """Fake ModelSelector: sync_primary must receive resolved runtime values."""

    def __init__(self) -> None:
        self.synced: list[object] = []

    def sync_primary(self, provider_config) -> None:
        self.synced.append(provider_config)


def _admin_ctx(config=None, selector=None) -> RpcContext:
    return RpcContext(
        conn_id="t",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
        config=config,
        provider_selector=selector,
    )


def _boot_config(config_path, monkeypatch, *, body: str = ""):
    """Simulate a gateway boot: write, load, env-resolve the live config."""
    config_path.write_text(body)
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(config_path))
    cfg = load_config(config_path)
    resolve_llm_runtime_config(cfg)
    return cfg


async def _configure_provider(ctx, **overrides):
    params = {"providerId": "openai", "apiKey": "sk-dummy-provenance"}
    params.update(overrides)
    res = await get_dispatcher().dispatch(
        "r1", "onboarding.provider.configure", params, ctx
    )
    assert res.error is None, res.error
    return res


def _disk_base_url(config_path) -> str:
    data = tomllib.loads(config_path.read_text())
    return data.get("llm", {}).get("base_url", "")


@pytest.mark.asyncio
async def test_explicit_base_url_survives_env_override_and_selector_sync(
    tmp_path, monkeypatch
):
    """F1: user's explicit URL must reach disk; env URL must never be baked."""
    monkeypatch.setenv("OPENAI_BASE_URL", _ENV_URL)
    config_path = tmp_path / "config.toml"
    cfg = _boot_config(config_path, monkeypatch, body='[llm]\nprovider = "openai"\n')
    # A real selector makes _sync_provider_selector run its env resolution —
    # the exact step that used to clobber the shared llm submodel and record
    # a post-save override on the live config.
    selector = _RecordingSelector()
    ctx = _admin_ctx(config=cfg, selector=selector)

    await _configure_provider(ctx, baseUrl=_USER_URL)

    assert _disk_base_url(config_path) == _USER_URL
    assert _ENV_URL not in config_path.read_text()
    # The live config keeps serving the explicit URL too (selector sync must
    # not have clobbered the shared llm submodel with the env value).
    assert ctx.config.llm.base_url == _USER_URL
    # The selector itself still received resolved runtime values (env wins
    # for the RUNNING process; only persistence must ignore it)... unless the
    # operator's explicit URL replaced the env source of truth on the model.
    assert len(selector.synced) == 1
    # And no post-save override record may linger on the live config for the
    # explicitly-set field (that stale record is what later reverts saves).
    assert "llm.base_url" not in ctx.config.runtime_field_overrides()


@pytest.mark.asyncio
async def test_consecutive_saves_do_not_flip_flop_the_persisted_base_url(
    tmp_path, monkeypatch
):
    """F2: clearing the record on the clone reaches the live config."""
    monkeypatch.setenv("OPENAI_BASE_URL", _ENV_URL)
    config_path = tmp_path / "config.toml"
    cfg = _boot_config(config_path, monkeypatch, body='[llm]\nprovider = "openai"\n')
    ctx = _admin_ctx(config=cfg)

    await _configure_provider(ctx, baseUrl=_USER_URL)
    # The clone's clear_runtime_override must reach the live config: a stale
    # record here is what made later saves flip the URL back to the env value.
    assert "llm.base_url" not in ctx.config.runtime_field_overrides()
    assert _disk_base_url(config_path) == _USER_URL

    # A key rotation without baseUrl follows the pinned legacy RPC contract
    # (absent -> "" -> derive the spec default; see the R18 contract tests) —
    # deterministic, and never the env URL from a resurrected stale record.
    await _configure_provider(ctx, apiKey="sk-dummy-rotated")
    rotated = _disk_base_url(config_path)
    assert rotated != _ENV_URL
    await _configure_provider(ctx, apiKey="sk-dummy-rotated-2")
    assert _disk_base_url(config_path) == rotated  # stable, no flip-flop


@pytest.mark.asyncio
async def test_in_place_swap_drops_stale_records_before_later_saves(
    tmp_path, monkeypatch
):
    """F3: hand-edit + reload-style swap must not let an unrelated save revert."""
    monkeypatch.setenv("OPENAI_BASE_URL", _ENV_URL)
    config_path = tmp_path / "config.toml"
    cfg = _boot_config(config_path, monkeypatch, body='[llm]\nprovider = "openai"\n')
    assert "llm.base_url" in cfg.runtime_field_overrides()

    # Operator hand-edits the file, then the gateway applies the new state
    # in place (the reload / config.set path).
    from opensquilla.gateway.adapters.app_settings import _update_config_in_place

    config_path.write_text(
        f'[llm]\nprovider = "openai"\nbase_url = "{_USER_URL}"\n'
    )
    fresh = load_config(config_path)
    _update_config_in_place(cfg, fresh)

    # An unrelated onboarding save must keep the hand-edited URL on disk.
    res = await get_dispatcher().dispatch(
        "r1",
        "onboarding.search.configure",
        {"providerId": "duckduckgo"},
        _admin_ctx(config=cfg),
    )
    assert res.error is None, res.error
    assert _disk_base_url(config_path) == _USER_URL


@pytest.mark.asyncio
async def test_legacy_empty_base_url_re_save_does_not_bake_env(tmp_path, monkeypatch):
    """F5: baseUrl='' is the legacy reset sentinel, not an explicit endpoint."""
    monkeypatch.setenv("OPENAI_BASE_URL", _ENV_URL)
    config_path = tmp_path / "config.toml"
    cfg = _boot_config(config_path, monkeypatch, body='[llm]\nprovider = "openai"\n')
    ctx = _admin_ctx(config=cfg)

    # Web-UI style key rotation sends the legacy default baseUrl="", which
    # the pinned RPC contract maps to "derive the spec default" — the write
    # must be that deterministic default, never the boot-time env URL.
    await _configure_provider(ctx, baseUrl="")

    from opensquilla.onboarding.provider_specs import get_provider_setup_spec

    assert _disk_base_url(config_path) == get_provider_setup_spec("openai").default_base_url
    assert _ENV_URL not in config_path.read_text()


def test_repeated_resolves_keep_original_stored_slot(tmp_path, monkeypatch):
    """F8: record_runtime_override must not chain across in-process resolves."""
    monkeypatch.setenv("OPENAI_BASE_URL", _ENV_URL)
    config_path = tmp_path / "config.toml"
    cfg = _boot_config(config_path, monkeypatch, body='[llm]\nprovider = "openai"\n')

    stored_first, applied_first = cfg.runtime_field_overrides()["llm.base_url"]
    # Disk carried no base_url, so "stored" is the model default the loaded
    # config held before env application — that is what a restore must write
    # back (and the sparse differ then omits it as default-equal).
    assert stored_first == type(cfg).model_fields["llm"].default_factory().base_url
    assert applied_first == _ENV_URL

    # Second in-process resolve (selector sync, reload paths) sees the field
    # already env-applied; the stored slot must keep disk provenance instead
    # of chaining to the env value the first resolve wrote into the model.
    resolve_llm_runtime_config(cfg)
    stored_second, applied_second = cfg.runtime_field_overrides()["llm.base_url"]

    assert stored_second == stored_first
    assert stored_second != _ENV_URL
    assert applied_second == _ENV_URL
