"""The agent-facing audio_config tool: safe persistence, hot apply, no leaks.

This is the supported alternative to the shell fallbacks that have corrupted
config.toml before (non-UTF-8 writes, subprocess-only env vars). It reuses the
onboarding mutation + persist path, so the file stays valid UTF-8, the running
gateway is updated in place, and no restart is needed.
"""

from __future__ import annotations

import json
import logging
import tomllib

import pytest

import opensquilla.tools.builtin.admin as admin_mod
import opensquilla.tools.builtin.media as media_mod
from opensquilla.onboarding.audio_specs import get_audio_provider_setup_spec
from opensquilla.onboarding.config_store import load_config
from opensquilla.onboarding.mutations import upsert_audio_provider
from opensquilla.tools.builtin.admin import audio_config as audio_config_tool
from opensquilla.tools.types import ToolError

SECRET = "elevenlabs-key-1a2b3c4d5e6f"


@pytest.fixture()
def live_config(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    cfg = load_config(path=target)
    admin_mod.set_gateway_config(cfg)
    yield cfg, target
    admin_mod.set_gateway_config(None)


@pytest.mark.asyncio
async def test_configures_elevenlabs_without_restart_and_without_leaking(
    live_config, caplog
) -> None:
    cfg, target = live_config
    hot_applied: list[object] = []
    real_configure_audio = media_mod.configure_audio
    media_mod.configure_audio = lambda audio_cfg: hot_applied.append(audio_cfg)
    try:
        with caplog.at_level(logging.DEBUG):
            raw = await audio_config_tool(
                provider="elevenlabs",
                api_key=SECRET,
                enabled=True,
                tts_voice="中文声音",
                tts_model="eleven_turbo_v2_5",
                language_code="zh-CN",
            )
    finally:
        media_mod.configure_audio = real_configure_audio

    result = json.loads(raw)
    # ElevenLabs config never needs a gateway restart.
    assert result["restartRequired"] is False
    assert result["changed"] is True
    assert result["configPath"] == str(target)

    # The key is stored but never echoed: not in the tool result, not in logs.
    assert SECRET not in raw
    assert SECRET not in caplog.text

    # On-disk artifact is strict UTF-8, valid TOML, and a loadable config —
    # including the non-ASCII voice name round-tripping intact.
    data = target.read_bytes()
    text = data.decode("utf-8", errors="strict")
    assert "中文声音" in text
    parsed = tomllib.loads(text)
    assert parsed["audio"]["providers"]["elevenlabs"]["api_key"] == SECRET
    reloaded = load_config(path=target)
    assert reloaded.audio.enabled is True
    assert (
        reloaded.audio.providers.elevenlabs.base_url
        == get_audio_provider_setup_spec("elevenlabs").default_base_url
    )

    # Hot apply: the running config was updated in place and pushed into the
    # audio tool layer without a restart.
    assert cfg.audio.enabled is True
    assert hot_applied, "configure_audio() was not invoked for the live config"
    assert hot_applied[-1] is cfg.audio or getattr(hot_applied[-1], "enabled", None) is True


@pytest.mark.asyncio
async def test_validation_errors_are_actionable_and_secret_free(live_config, caplog) -> None:
    _cfg, target = live_config
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ToolError) as exc_info:
            await audio_config_tool(
                provider="elevenlabs",
                api_key=SECRET,
                api_key_env="ELEVENLABS_API_KEY",
            )
    message = str(exc_info.value)
    assert "api_key" in message
    assert SECRET not in message
    assert SECRET not in caplog.text
    # A refused configuration writes nothing.
    assert not target.exists() or SECRET not in target.read_bytes().decode("utf-8", "replace")


@pytest.mark.asyncio
async def test_unsupported_provider_is_refused(live_config) -> None:
    with pytest.raises(ToolError):
        await audio_config_tool(provider="not-a-provider")


@pytest.mark.asyncio
async def test_refuses_unregistered_credential_environment_variable(live_config) -> None:
    _cfg, target = live_config

    with pytest.raises(ToolError) as exc_info:
        await audio_config_tool(provider="elevenlabs", api_key_env="OPENAI_API_KEY")

    assert "ELEVENLABS_API_KEY" in str(exc_info.value)
    assert not target.exists()


@pytest.mark.asyncio
async def test_resets_operator_managed_endpoint_before_storing_agent_key(live_config) -> None:
    cfg, target = live_config
    custom = upsert_audio_provider(
        cfg,
        provider_id="elevenlabs",
        base_url="https://audio-proxy.example.invalid/v1",
        enabled=False,
    ).config
    admin_mod.set_gateway_config(custom)

    await audio_config_tool(provider="elevenlabs", api_key=SECRET)

    text = target.read_text(encoding="utf-8")
    assert "audio-proxy.example.invalid" not in text
    reloaded = load_config(path=target)
    assert (
        reloaded.audio.providers.elevenlabs.base_url
        == get_audio_provider_setup_spec("elevenlabs").default_base_url
    )


@pytest.mark.asyncio
async def test_requires_a_wired_gateway_config() -> None:
    admin_mod.set_gateway_config(None)
    with pytest.raises(ToolError):
        await audio_config_tool(provider="elevenlabs", api_key=SECRET)
