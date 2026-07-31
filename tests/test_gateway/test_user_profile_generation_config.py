"""Configuration contract for offline user-profile production."""

from __future__ import annotations

import tomllib

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.boot import _user_profile_generation_enabled
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_config import (
    _SAFE_WRITE_PATCH_PATHS,
    _handle_config_patch_safe,
)


def _ctx(tmp_path) -> RpcContext:
    return RpcContext(
        conn_id="user-profile-config",
        config=GatewayConfig(config_path=str(tmp_path / "config.toml")),
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.write"}),
            is_owner=True,
            authenticated=True,
        ),
    )


def test_user_profile_generation_defaults_to_disabled() -> None:
    config = GatewayConfig()

    assert config.squilla_router.user_profile.enabled is False
    assert _user_profile_generation_enabled(config) is False
    assert config.to_toml_dict()["squilla_router"]["user_profile"]["enabled"] is False


def test_user_profile_generation_can_be_explicitly_enabled() -> None:
    config = GatewayConfig(
        squilla_router={"user_profile": {"enabled": True}},
    )

    assert config.squilla_router.user_profile.enabled is True
    assert _user_profile_generation_enabled(config) is True


async def test_safe_patch_enables_and_persists_user_profile_generation(tmp_path) -> None:
    assert "squilla_router.user_profile.enabled" in _SAFE_WRITE_PATCH_PATHS
    ctx = _ctx(tmp_path)

    await _handle_config_patch_safe(
        {"patches": {"squilla_router.user_profile.enabled": True}},
        ctx,
    )

    assert ctx.config.squilla_router.user_profile.enabled is True
    persisted = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert persisted["squilla_router"]["user_profile"]["enabled"] is True
