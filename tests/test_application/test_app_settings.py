"""Tests for the transport-independent AppSettings Module."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from opensquilla.application.app_settings import AppSettings, SettingChange


class SettingsPort:
    def __init__(self) -> None:
        self.public = {"llm": {"provider": "openai"}, "nullable": None}
        self.effective = {
            "fields": {"llm.provider": {"value": "openai", "source": "config"}}
        }
        self.mutations: list[tuple[str, dict[str, Any]]] = []

    async def read_public_settings(self) -> Mapping[str, Any]:
        return self.public

    async def read_effective_settings(self) -> Mapping[str, Any]:
        return self.effective

    async def patch_settings(
        self, changes: Mapping[str, Any], *, safe: bool
    ) -> Mapping[str, Any]:
        operation = "safe" if safe else "patch"
        self.mutations.append((operation, dict(changes)))
        return {"patched": list(changes), "restartRequired": False}

    async def merge_settings(self, patch: Mapping[str, Any]) -> Mapping[str, Any]:
        self.mutations.append(("merge", dict(patch)))
        return {"patched": ["(merge)"]}


@pytest.mark.asyncio
async def test_reads_full_scalar_and_null_settings_shapes() -> None:
    port = SettingsPort()
    settings = AppSettings(port, port)

    assert await settings.read_all() == port.public
    assert await settings.read("llm.provider") == "openai"
    assert await settings.read("nullable") is None
    assert await settings.read("llm.missing") is None
    assert await settings.read_effective() == port.effective


@pytest.mark.asyncio
async def test_patch_preserves_domain_changes_and_safe_intent() -> None:
    port = SettingsPort()
    settings = AppSettings(port, port)
    changes = [SettingChange("llm.model", "gpt-5"), SettingChange("audio.enabled", True)]

    assert (await settings.patch(changes))["patched"] == ["llm.model", "audio.enabled"]
    await settings.patch_safe([SettingChange("control_ui.default_locale", "zh-CN")])

    assert port.mutations == [
        ("patch", {"llm.model": "gpt-5", "audio.enabled": True}),
        ("safe", {"control_ui.default_locale": "zh-CN"}),
    ]


@pytest.mark.asyncio
async def test_patch_rejects_duplicate_or_invalid_paths_before_the_port() -> None:
    port = SettingsPort()
    settings = AppSettings(port, port)

    with pytest.raises(ValueError, match="duplicate settings path"):
        await settings.patch([SettingChange("llm.model", "a"), SettingChange("llm.model", "b")])
    with pytest.raises(ValueError, match="non-empty dotted path"):
        await settings.patch([SettingChange("llm..model", "a")])

    assert port.mutations == []


@pytest.mark.asyncio
async def test_merge_is_a_distinct_domain_operation() -> None:
    port = SettingsPort()
    settings = AppSettings(port, port)

    assert await settings.merge({"llm": {"model": "gpt-5"}}) == {"patched": ["(merge)"]}
    with pytest.raises(ValueError, match="must not be empty"):
        await settings.merge({})
