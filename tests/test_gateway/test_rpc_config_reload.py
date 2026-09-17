"""``config.reload`` acceptance tests — validate-then-apply-or-rollback + secrets.

Each test encodes an audited hazard of re-reading hand-edited TOML into a
running gateway:

* a broken/invalid file must never touch the live config (identity + values);
* the boot-generated ``auth.token`` (absent from disk by design) must survive
  by value AND runtime-secret marker, so a later persist cannot write it out;
* provider env keys must self-heal onto the candidate via the selector sync
  (which re-marks ``llm.api_key``) before the in-place swap;
* a hand-written on-disk ``llm.api_key`` must NOT inherit a stale runtime
  marker, or the next persist would silently delete it from disk;
* channel changes are restart-gated and excluded from ``liveApplied``;
* reload itself is read-only against the config file.

Everything below is offline and synthetic: tmp_path configs, dummy tokens,
monkeypatched env vars (tests/conftest.py strips real provider keys).
"""

from __future__ import annotations

from types import SimpleNamespace

import opensquilla.gateway.rpc_config  # noqa: F401  ensures registration
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.rpc_config import (
    _handle_config_apply,
    _handle_config_patch,
    _handle_config_reload,
    _handle_config_set,
)


class _CapturingSelector:
    def __init__(self) -> None:
        self.synced = None

    def sync_primary(self, cfg) -> None:
        self.synced = cfg


def _admin_ctx(config: GatewayConfig, selector=None) -> RpcContext:
    return RpcContext(
        conn_id="t",
        config=config,
        provider_selector=selector,
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
    )


# ---------------------------------------------------------------------------
# 1. Validation failure → rollback (config identity + values untouched)
# ---------------------------------------------------------------------------


async def test_reload_broken_toml_leaves_config_untouched(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[llm\nprovider =", encoding="utf-8")
    cfg = GatewayConfig(
        config_path=str(path),
        llm={"provider": "openai", "api_key": "", "base_url": ""},
    )
    before_dump = cfg.model_dump(mode="python")
    ctx = SimpleNamespace(config=cfg)

    res = await _handle_config_reload(None, ctx)

    assert res["ok"] is False
    assert res["error"]
    assert ctx.config is cfg  # same object identity — nothing was swapped
    assert ctx.config.model_dump(mode="python") == before_dump


async def test_reload_invalid_field_value_leaves_config_untouched(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[squilla_router]\nvisual_mode = "bogus"\n', encoding="utf-8")
    cfg = GatewayConfig(config_path=str(path))
    before_dump = cfg.model_dump(mode="python")
    ctx = SimpleNamespace(config=cfg)

    res = await _handle_config_reload(None, ctx)

    assert res["ok"] is False
    assert "visual_mode" in res["error"]
    assert ctx.config is cfg
    assert ctx.config.model_dump(mode="python") == before_dump


async def test_reload_hot_applies_context_compaction_settings(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            (
                "context_budget_tokens = 75000",
                'context_overflow_policy = "refuse"',
                "preflight_compact_ratio = 0.7",
            )
        ),
        encoding="utf-8",
    )
    cfg = GatewayConfig(config_path=str(path))
    ctx = SimpleNamespace(config=cfg)

    res = await _handle_config_reload(None, ctx)

    assert res["ok"] is True
    assert ctx.config is cfg
    assert ctx.config.context_budget_tokens == 75_000
    assert ctx.config.context_overflow_policy.value == "refuse"
    assert ctx.config.preflight_compact_ratio == 0.7


# ---------------------------------------------------------------------------
# 2. Boot-generated auth token survives by value AND marker
# ---------------------------------------------------------------------------


async def test_reload_preserves_boot_generated_auth_token(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[auth]\nmode = "token"\n', encoding="utf-8")
    cfg = GatewayConfig(
        config_path=str(path),
        auth={"mode": "token", "token": "dummy-boot-token"},
    )
    cfg.mark_runtime_secret("auth.token")  # as gateway boot does
    ctx = SimpleNamespace(config=cfg)

    res = await _handle_config_reload(None, ctx)

    assert res["ok"] is True
    # Value survives even though the file omits it...
    assert ctx.config.auth.token == "dummy-boot-token"
    # ...and it is STILL MARKED, so a subsequent persist never writes it out.
    assert "auth.token" in ctx.config._runtime_secret_paths
    dumped = ctx.config.to_toml_dict()
    assert "token" not in dumped.get("auth", {})


# ---------------------------------------------------------------------------
# 3. Provider env key self-heals onto the candidate (sync order)
# ---------------------------------------------------------------------------


async def test_reload_resolves_provider_env_key_and_marks_it(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-anthropic-key")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    path = tmp_path / "config.toml"
    path.write_text(
        '[llm]\nprovider = "anthropic"\nmodel = "claude-test-model"\n',
        encoding="utf-8",
    )
    cfg = GatewayConfig(
        config_path=str(path),
        llm={"provider": "openai", "api_key": "", "base_url": ""},
    )
    selector = _CapturingSelector()
    ctx = SimpleNamespace(config=cfg, provider_selector=selector)

    res = await _handle_config_reload(None, ctx)

    assert res["ok"] is True
    assert selector.synced is not None
    assert selector.synced.provider == "anthropic"
    assert selector.synced.api_key == "dummy-anthropic-key"
    assert ctx.config.llm.api_key == "dummy-anthropic-key"
    # Marked as runtime secret so the env key can never be persisted to disk.
    assert "llm.api_key" in ctx.config._runtime_secret_paths
    assert "api_key" not in ctx.config.to_toml_dict()["llm"]


# ---------------------------------------------------------------------------
# 4. Hand-written explicit llm.api_key: live, NOT marked, survives persist view
# ---------------------------------------------------------------------------


async def test_reload_hand_written_api_key_is_live_and_persists(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = tmp_path / "config.toml"
    path.write_text(
        '[llm]\nprovider = "openai"\napi_key = "dummy-hand-key"\n',
        encoding="utf-8",
    )
    cfg = GatewayConfig(
        config_path=str(path),
        llm={"provider": "openai", "api_key": "dummy-stale-runtime-key", "base_url": ""},
    )
    # Stale marker from an earlier env-based resolve. Blanket marker
    # inheritance would carry it over and make the next persist DELETE the
    # operator's newly hand-written key — markers must be recomputed instead.
    cfg.mark_runtime_secret("llm.api_key")
    ctx = SimpleNamespace(config=cfg)

    res = await _handle_config_reload(None, ctx)

    assert res["ok"] is True
    assert ctx.config.llm.api_key == "dummy-hand-key"
    assert "llm.api_key" not in ctx.config._runtime_secret_paths
    assert ctx.config.to_toml_dict()["llm"]["api_key"] == "dummy-hand-key"


# ---------------------------------------------------------------------------
# 5. Restart gating + liveApplied honesty
# ---------------------------------------------------------------------------


async def test_reload_channels_are_restart_gated_and_excluded_from_live_applied(
    tmp_path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                "[naming]",
                "enabled = false",
                "",
                "[[channels.channels]]",
                'name = "team"',
                'type = "telegram"',
                'token = "dummy-telegram-token"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    cfg = GatewayConfig(config_path=str(path))
    ctx = SimpleNamespace(config=cfg)

    res = await _handle_config_reload(None, ctx)

    assert res["ok"] is True
    assert res["restartRequired"] is True
    assert res["restartSections"] == ["channels"]
    assert "naming" in res["liveApplied"]
    assert "channels" not in res["liveApplied"]
    # The non-gated section really did hot-apply in place.
    assert ctx.config.naming.enabled is False
    assert len(ctx.config.channels.channels) == 1


async def test_reload_no_disk_change_reports_nothing(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[naming]\nenabled = false\n', encoding="utf-8")
    cfg = GatewayConfig(config_path=str(path), naming={"enabled": False})
    ctx = SimpleNamespace(config=cfg)

    res = await _handle_config_reload(None, ctx)

    assert res["ok"] is True
    assert res["restartRequired"] is False
    assert res["restartSections"] == []
    assert res["liveApplied"] == []


# ---------------------------------------------------------------------------
# 6. Reload is read-only against the config file
# ---------------------------------------------------------------------------


async def test_reload_never_writes_the_config_file(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[naming]\nenabled = false\n', encoding="utf-8")
    before_bytes = path.read_bytes()
    before_mtime_ns = path.stat().st_mtime_ns
    cfg = GatewayConfig(config_path=str(path))
    ctx = SimpleNamespace(config=cfg)

    res = await _handle_config_reload(None, ctx)

    assert res["ok"] is True
    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_mtime_ns


# ---------------------------------------------------------------------------
# Scope wiring: config.reload dispatches for admin via the real registry
# ---------------------------------------------------------------------------


async def test_config_reload_dispatches_as_admin(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[naming]\nenabled = false\n', encoding="utf-8")
    cfg = GatewayConfig(config_path=str(path))

    res = await get_dispatcher().dispatch(
        "r1", "config.reload", {}, _admin_ctx(cfg)
    )

    assert res.error is None, res.error
    assert res.payload["ok"] is True
    assert res.payload["liveApplied"] == ["naming"]


async def test_config_reload_denied_without_admin_scope(tmp_path) -> None:
    cfg = GatewayConfig(config_path=str(tmp_path / "config.toml"))
    ctx = RpcContext(
        conn_id="t",
        config=cfg,
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.write", "operator.read"}),
            is_owner=False,
            authenticated=True,
        ),
    )

    res = await get_dispatcher().dispatch("r1", "config.reload", {}, ctx)

    assert res.error is not None
    assert "config.reload" in res.error.message


# ---------------------------------------------------------------------------
# liveApplied on the existing write responses (additive keys)
# ---------------------------------------------------------------------------


async def test_config_set_reports_live_applied_sections(tmp_path) -> None:
    cfg = GatewayConfig(config_path=str(tmp_path / "config.toml"))
    ctx = SimpleNamespace(config=cfg)

    res = await _handle_config_set({"path": "naming.enabled", "value": False}, ctx)

    assert res["restartRequired"] is False
    assert res["restartSections"] == []
    assert res["liveApplied"] == ["naming"]


async def test_config_patch_excludes_gated_sections_from_live_applied(
    tmp_path,
) -> None:
    cfg = GatewayConfig(config_path=str(tmp_path / "config.toml"))
    ctx = SimpleNamespace(config=cfg)

    res = await _handle_config_patch(
        {
            "patches": {
                "permissions.default_mode": "full",
                "naming.enabled": False,
            }
        },
        ctx,
    )

    assert res["restartRequired"] is True
    assert "permissions" in res["restartSections"]
    assert res["liveApplied"] == ["naming"]


async def test_config_apply_reports_live_applied_sections(tmp_path) -> None:
    cfg = GatewayConfig(config_path=str(tmp_path / "config.toml"))
    ctx = SimpleNamespace(config=cfg)
    payload = cfg.model_dump(mode="python")
    payload["naming"]["enabled"] = False

    res = await _handle_config_apply({"config": payload}, ctx)

    assert res["restartRequired"] is False
    assert res["liveApplied"] == ["naming"]
