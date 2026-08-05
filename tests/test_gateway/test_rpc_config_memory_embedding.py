from __future__ import annotations

import tomllib

import pytest

import opensquilla.gateway.rpc_config  # noqa: F401  ensures registration
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher


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


def _memory_remote_secret_config(config_path) -> GatewayConfig:
    return GatewayConfig(
        config_path=str(config_path),
        memory={
            "embedding": {
                "provider": "openai",
                "remote": {
                    "api_key": "mem-secret",
                    "headers": {
                        "Authorization": "Bearer header-secret",
                        "x-api-key": "header-key",
                    },
                },
            }
        },
    )


def _assert_memory_remote_secrets_preserved(
    cfg: GatewayConfig,
    config_path,
) -> None:
    assert cfg.memory.embedding.remote.api_key == "mem-secret"
    headers = cfg.memory.embedding.remote.headers
    assert headers["Authorization"] == "Bearer header-secret"
    assert headers["x-api-key"] == "header-key"
    persisted = config_path.read_text(encoding="utf-8")
    assert "[redacted]" not in persisted


@pytest.mark.asyncio
async def test_config_patch_memory_embedding_reports_restart_required(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"memory.embedding.provider": "none"}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True


@pytest.mark.asyncio
async def test_config_patch_same_memory_embedding_does_not_report_restart_required(tmp_path):
    cfg = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        memory={"embedding": {"provider": "none"}},
    )
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"memory.embedding.provider": "none"}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is False


@pytest.mark.asyncio
async def test_config_patch_permissions_default_reports_restart_required(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"permissions.default_mode": "full"}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True


@pytest.mark.asyncio
async def test_config_patch_same_permissions_default_does_not_report_restart_required(
    tmp_path,
):
    cfg = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        permissions={"default_mode": "bypass"},
    )
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"permissions.default_mode": "bypass"}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is False


@pytest.mark.asyncio
async def test_config_apply_legacy_sandbox_posture_does_not_require_restart(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    payload = cfg.model_dump(mode="python")
    payload["sandbox"]["sandbox"] = False
    payload["sandbox"]["security_grading"] = False
    payload["permissions"]["default_mode"] = "off"

    res = await get_dispatcher().dispatch(
        "r1",
        "config.apply",
        {"config": payload},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is False


@pytest.mark.asyncio
async def test_config_patch_memory_retrieval_mode_reports_restart_required(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"memory.retrieval_mode": "fts_only"}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True


@pytest.mark.asyncio
async def test_config_apply_memory_retrieval_mode_reports_restart_required(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    payload = cfg.model_dump(mode="python")
    payload["memory"]["retrieval_mode"] = "fts_only"

    res = await get_dispatcher().dispatch(
        "r1",
        "config.apply",
        {"config": payload},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True


@pytest.mark.asyncio
async def test_config_get_redacts_memory_remote_api_key(tmp_path):
    cfg = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        memory={"embedding": {"remote": {"api_key": "mem-secret"}}},
    )

    res = await get_dispatcher().dispatch("r1", "config.get", {}, _admin_ctx(cfg))

    assert res.error is None, res.error
    assert res.payload["memory"]["embedding"]["remote"]["api_key"] == "[redacted]"


@pytest.mark.asyncio
async def test_config_get_does_not_redact_empty_memory_remote_api_key(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))

    res = await get_dispatcher().dispatch("r1", "config.get", {}, _admin_ctx(cfg))

    assert res.error is None, res.error
    assert res.payload["memory"]["embedding"]["remote"]["api_key"] is None


@pytest.mark.asyncio
async def test_config_get_redacts_memory_remote_header_api_key(tmp_path):
    cfg = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        memory={
            "embedding": {
                "remote": {
                    "headers": {
                        "Authorization": "Bearer secret",
                        "x-api-key": "header-secret",
                    }
                }
            }
        },
    )

    res = await get_dispatcher().dispatch("r1", "config.get", {}, _admin_ctx(cfg))

    assert res.error is None, res.error
    headers = res.payload["memory"]["embedding"]["remote"]["headers"]
    assert headers["Authorization"] == "[redacted]"
    assert headers["x-api-key"] == "[redacted]"


@pytest.mark.asyncio
async def test_config_apply_preserves_redacted_memory_remote_secrets(tmp_path):
    config_path = tmp_path / "c.toml"
    cfg = _memory_remote_secret_config(config_path)
    dispatcher = get_dispatcher()

    get_res = await dispatcher.dispatch("r1", "config.get", {}, _admin_ctx(cfg))
    assert get_res.error is None, get_res.error
    assert get_res.payload["memory"]["embedding"]["remote"]["api_key"] == "[redacted]"

    apply_res = await dispatcher.dispatch(
        "r2",
        "config.apply",
        {"config": get_res.payload},
        _admin_ctx(cfg),
    )

    assert apply_res.error is None, apply_res.error
    _assert_memory_remote_secrets_preserved(cfg, config_path)
    persisted = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert "network_observability_disabled_effective" not in persisted.get("privacy", {})


@pytest.mark.asyncio
async def test_config_patch_preserves_redacted_memory_object_secrets(tmp_path):
    config_path = tmp_path / "c.toml"
    cfg = _memory_remote_secret_config(config_path)
    dispatcher = get_dispatcher()

    get_res = await dispatcher.dispatch("r1", "config.get", {}, _admin_ctx(cfg))
    assert get_res.error is None, get_res.error

    memory_payload = get_res.payload["memory"]
    assert memory_payload["embedding"]["remote"]["api_key"] == "[redacted]"
    patch_res = await dispatcher.dispatch(
        "r2",
        "config.patch",
        {"patches": {"memory": memory_payload}},
        _admin_ctx(cfg),
    )

    assert patch_res.error is None, patch_res.error
    _assert_memory_remote_secrets_preserved(cfg, config_path)


@pytest.mark.asyncio
async def test_config_apply_keeps_literal_redacted_for_non_secret_memory_fields(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    payload = cfg.model_dump(mode="python")
    payload["memory"]["embedding"]["remote"]["model"] = "[redacted]"

    res = await get_dispatcher().dispatch(
        "r1",
        "config.apply",
        {"config": payload},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert cfg.memory.embedding.remote.model == "[redacted]"


@pytest.mark.asyncio
async def test_config_patch_keeps_literal_redacted_for_non_secret_memory_fields(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))

    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"memory.embedding.remote.model": "[redacted]"}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert cfg.memory.embedding.remote.model == "[redacted]"


@pytest.mark.asyncio
async def test_config_patch_rejects_direct_redacted_secret_marker(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))

    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"memory.embedding.remote.api_key": "[redacted]"}},
        _admin_ctx(cfg),
    )

    assert res.error is not None
    assert res.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_config_set_rejects_direct_redacted_secret_marker(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))

    res = await get_dispatcher().dispatch(
        "r1",
        "config.set",
        {"path": "memory.embedding.remote.api_key", "value": "[redacted]"},
        _admin_ctx(cfg),
    )

    assert res.error is not None
    assert res.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_config_set_rejects_redacted_secret_marker_inside_object(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))

    res = await get_dispatcher().dispatch(
        "r1",
        "config.set",
        {
            "path": "memory.embedding.remote",
            "value": {"api_key": "[redacted]"},
        },
        _admin_ctx(cfg),
    )

    assert res.error is not None
    assert res.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_config_set_preserves_redacted_memory_remote_object_secrets(tmp_path):
    config_path = tmp_path / "c.toml"
    cfg = _memory_remote_secret_config(config_path)
    dispatcher = get_dispatcher()

    get_res = await dispatcher.dispatch("r1", "config.get", {}, _admin_ctx(cfg))
    assert get_res.error is None, get_res.error

    remote_payload = get_res.payload["memory"]["embedding"]["remote"]
    remote_payload["base_url"] = "https://embeddings.example/v1"
    set_res = await dispatcher.dispatch(
        "r2",
        "config.set",
        {"path": "memory.embedding.remote", "value": remote_payload},
        _admin_ctx(cfg),
    )

    assert set_res.error is None, set_res.error
    assert cfg.memory.embedding.remote.base_url == "https://embeddings.example/v1"
    _assert_memory_remote_secrets_preserved(cfg, config_path)


@pytest.mark.asyncio
async def test_config_patch_rejects_explicit_remote_without_memory_api_key(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))

    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"memory.embedding.provider": "openai"}},
        _admin_ctx(cfg),
    )

    assert res.error is not None
    assert res.error.code == "INVALID_REQUEST"
    assert "memory.embedding.remote.api_key" in res.error.message


@pytest.mark.asyncio
async def test_config_apply_rejects_explicit_remote_without_memory_api_key(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    payload = cfg.model_dump(mode="python")
    payload["memory"]["embedding"]["provider"] = "openai"

    res = await get_dispatcher().dispatch(
        "r1",
        "config.apply",
        {"config": payload},
        _admin_ctx(cfg),
    )

    assert res.error is not None
    assert res.error.code == "INVALID_REQUEST"
    assert "memory.embedding.remote.api_key" in res.error.message


def _slack_channel_entry(name: str = "work", token: str = "xoxb-test-token") -> dict:
    return {
        "type": "slack",
        "name": name,
        "enabled": True,
        "agent_id": "main",
        "debounce_window_s": 0.0,
        "status_reactions_enabled": False,
        "token": token,
        "slack_channel_id": "C001",
        "signing_secret": None,
        "reply_in_thread": False,
    }


@pytest.mark.asyncio
async def test_config_patch_channels_reports_restart_required(tmp_path):
    cfg = GatewayConfig(config_path=str(tmp_path / "c.toml"))
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"channels.channels": [_slack_channel_entry()]}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True


@pytest.mark.asyncio
async def test_config_patch_same_channels_does_not_report_restart_required(tmp_path):
    cfg = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        channels={"channels": [_slack_channel_entry()]},
    )
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"channels.channels": [_slack_channel_entry()]}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is False


@pytest.mark.asyncio
async def test_config_apply_channels_token_change_reports_restart_required(tmp_path):
    cfg = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        channels={"channels": [_slack_channel_entry(token="xoxb-old")]},
    )
    payload = cfg.model_dump(mode="python")
    payload["channels"]["channels"][0]["token"] = "xoxb-new"

    res = await get_dispatcher().dispatch(
        "r1",
        "config.apply",
        {"config": payload},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True


@pytest.mark.asyncio
async def test_config_apply_unchanged_channels_does_not_report_restart_required(tmp_path):
    cfg = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        channels={"channels": [_slack_channel_entry()]},
    )
    payload = cfg.model_dump(mode="python")

    res = await get_dispatcher().dispatch(
        "r1",
        "config.apply",
        {"config": payload},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is False


@pytest.mark.asyncio
async def test_config_set_channel_token_rotation_reports_restart_required(tmp_path):
    cfg = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        channels={"channels": [_slack_channel_entry(token="xoxb-old")]},
    )
    res = await get_dispatcher().dispatch(
        "r1",
        "config.set",
        {
            "path": "channels.channels",
            "value": [_slack_channel_entry(token="xoxb-new")],
        },
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True


@pytest.mark.asyncio
async def test_config_patch_disabling_channel_reports_restart_required(tmp_path):
    cfg = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        channels={"channels": [_slack_channel_entry()]},
    )
    disabled_entry = {**_slack_channel_entry(), "enabled": False}
    res = await get_dispatcher().dispatch(
        "r1",
        "config.patch",
        {"patches": {"channels.channels": [disabled_entry]}},
        _admin_ctx(cfg),
    )

    assert res.error is None, res.error
    assert res.payload["restartRequired"] is True
