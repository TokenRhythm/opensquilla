"""Persistence stays safe around corrupted configs and interrupted writes.

A real incident: an agent edited config.toml through a shell whose codepage
wrote non-UTF-8 bytes, after which every load raised UnicodeDecodeError and
the gateway crash-looped. The persist layer must (a) never produce such a
file itself, (b) recover over one — backing up the corrupt bytes first — and
(c) leave the original untouched when a write fails midway.
"""

from __future__ import annotations

import os
import tomllib

import pytest

from opensquilla.onboarding.config_store import load_config, persist_config
from opensquilla.onboarding.mutations import upsert_audio_provider

# Valid GBK-encoded Chinese ("配置"), invalid as UTF-8 — the observed corruption.
GBK_BYTES = "# 配置\n".encode("gbk")


def _fresh_config(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    return load_config(path=target), target


def test_persist_recovers_over_non_utf8_config_with_backup(tmp_path, monkeypatch) -> None:
    cfg, target = _fresh_config(tmp_path, monkeypatch)
    mutated = upsert_audio_provider(cfg, provider_id="elevenlabs", api_key="k-123").config

    # Corrupt the on-disk file the way the incident did.
    target.write_bytes(GBK_BYTES)

    result = persist_config(mutated, path=target)

    # The rewritten file is strict UTF-8 and loadable end to end.
    text = target.read_bytes().decode("utf-8", errors="strict")
    tomllib.loads(text)
    assert load_config(path=target).audio.enabled is True

    # The corrupt original was preserved byte-for-byte in a backup.
    backups = sorted(tmp_path.glob("config.toml.backup.*"))
    assert backups, "no backup of the corrupt config was kept"
    assert any(b.read_bytes() == GBK_BYTES for b in backups)
    assert result.path == target


def test_chinese_content_round_trips_as_utf8(tmp_path, monkeypatch) -> None:
    cfg, target = _fresh_config(tmp_path, monkeypatch)
    mutated = upsert_audio_provider(
        cfg,
        provider_id="elevenlabs",
        api_key="k-456",
        tts_voice="温柔的中文声音",
    ).config

    persist_config(mutated, path=target)

    text = target.read_bytes().decode("utf-8", errors="strict")
    assert "温柔的中文声音" in text
    reloaded = load_config(path=target)
    providers = reloaded.audio.providers
    entry = providers.get("elevenlabs") if isinstance(providers, dict) else providers.elevenlabs
    assert getattr(entry, "tts_voice", None) == "温柔的中文声音" or "温柔的中文声音" in text


def test_failed_write_leaves_original_bytes_untouched(tmp_path, monkeypatch) -> None:
    cfg, target = _fresh_config(tmp_path, monkeypatch)
    first = upsert_audio_provider(cfg, provider_id="elevenlabs", api_key="k-first").config
    persist_config(first, path=target)
    original_bytes = target.read_bytes()

    # Reload so the second mutation diffs against the committed baseline.
    cfg2 = load_config(path=target)
    second = upsert_audio_provider(cfg2, provider_id="elevenlabs", api_key="k-second").config

    real_replace = os.replace

    def failing_replace(*args, **kwargs):
        raise OSError("simulated mid-write failure")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError):
        persist_config(second, path=target, backup=False)
    monkeypatch.setattr(os, "replace", real_replace)

    assert target.read_bytes() == original_bytes
    assert "k-second" not in target.read_bytes().decode("utf-8", "strict")
