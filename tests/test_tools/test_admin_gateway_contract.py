"""The gateway tool's advertised contract matches what it can actually do.

Historically the tool advertised restart/config_get/config_set while restart
always raised and config_set depended on a GatewayConfig.patch() that does not
exist. Agents that hit those dead ends fell back to shell commands that
corrupted config.toml and killed the gateway. The contract is now: config_get
only (with credential redaction), and the retired actions return guidance
pointing at the safe paths instead of a bare failure.
"""

from __future__ import annotations

import json

import pytest

import opensquilla.tools.builtin.admin as admin_mod
from opensquilla.onboarding.config_store import load_config
from opensquilla.onboarding.mutations import upsert_audio_provider
from opensquilla.tools.builtin.admin import gateway as gateway_tool
from opensquilla.tools.types import ToolError

SECRET = "elevenlabs-key-9z8y7x6w"


@pytest.fixture()
def wired_config(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    cfg = load_config(path=target)
    cfg = upsert_audio_provider(cfg, provider_id="elevenlabs", api_key=SECRET).config
    admin_mod.set_gateway_config(cfg)
    yield cfg
    admin_mod.set_gateway_config(None)


@pytest.mark.asyncio
async def test_restart_reports_unavailable_without_supervisor_guidance(wired_config) -> None:
    with pytest.raises(ToolError) as exc_info:
        await gateway_tool(action="restart")
    message = str(exc_info.value)
    assert "not available" in message
    assert "audio_config" in message
    assert "supervisor" not in message


@pytest.mark.asyncio
async def test_config_set_returns_safe_path_guidance(wired_config) -> None:
    with pytest.raises(ToolError) as exc_info:
        await gateway_tool(action="config_set", key="audio.enabled", value="true")
    message = str(exc_info.value)
    assert "audio_config" in message
    assert "supervisor" not in message


@pytest.mark.asyncio
async def test_unknown_actions_advertise_only_config_get(wired_config) -> None:
    with pytest.raises(ToolError) as exc_info:
        await gateway_tool(action="mystery")
    message = str(exc_info.value)
    assert "config_get" in message
    assert "restart" not in message
    assert "config_set" not in message


@pytest.mark.asyncio
async def test_config_get_redacts_credentials_at_the_leaf(wired_config) -> None:
    raw = await gateway_tool(action="config_get", key="audio.providers.elevenlabs.api_key")
    payload = json.loads(raw)
    assert payload["value"] == "[redacted]"
    assert SECRET not in raw


@pytest.mark.asyncio
async def test_config_get_redacts_credentials_inside_sections(wired_config) -> None:
    raw = await gateway_tool(action="config_get", key="audio")
    assert SECRET not in raw
    payload = json.loads(raw)
    providers = payload["value"]["providers"]["elevenlabs"]
    assert providers["api_key"] == "[redacted]"


@pytest.mark.asyncio
async def test_config_get_uses_canonical_channel_crypto_redaction(wired_config) -> None:
    class ConfigWithChannelCrypto:
        @staticmethod
        def to_toml_dict():
            return {
                "channels": {
                    "feishu": {"encrypt_key": "feishu-encryption-key"},
                    "wecom": {"encoding_aes_key": "wecom-encryption-key"},
                }
            }

    admin_mod.set_gateway_config(ConfigWithChannelCrypto())

    section = json.loads(await gateway_tool(action="config_get", key="channels"))["value"]
    assert section["feishu"]["encrypt_key"] == "[redacted]"
    assert section["wecom"]["encoding_aes_key"] == "[redacted]"

    leaf = json.loads(
        await gateway_tool(action="config_get", key="channels.feishu.encrypt_key")
    )["value"]
    assert leaf == "[redacted]"


@pytest.mark.asyncio
async def test_config_get_still_returns_ordinary_values(wired_config) -> None:
    raw = await gateway_tool(action="config_get", key="audio.enabled")
    payload = json.loads(raw)
    assert payload["value"] is True
