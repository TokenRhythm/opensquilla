"""Smoke test: every channel adapter must be importable with only base deps.

After 0.1.0's refactor each vendor SDK (lark-oapi / python-telegram-bot /
dingtalk-stream / qq-botpy / cryptography) lives in base ``dependencies``
rather than in an opt-in extra. A bare ``pip install opensquilla`` must
therefore be enough to ``import`` any of the in-tree channel adapters
without raising ``ImportError``.

These tests guard against the regression where someone moves an SDK back
into an extra and silently breaks the "everything works out of the box"
guarantee from the install plan.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_storage_import_does_not_load_adapters_or_gateway(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    script = """
import importlib.abc
import sys

class StorageImportBoundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith((
            'opensquilla.gateway', 'opensquilla.tools', 'opensquilla.provider',
            'opensquilla.channels.manager', 'opensquilla.channels.discord',
            'opensquilla.channels.feishu', 'opensquilla.channels.slack',
            'opensquilla.channels.telegram', 'opensquilla.channels.terminal',
            'opensquilla.channels.websocket',
        )):
            raise AssertionError('storage imported unrelated runtime: ' + fullname)

sys.meta_path.insert(0, StorageImportBoundary())
from opensquilla.channels.delivery_store import ChannelDeliveryStore
from opensquilla.channels.storage_worker import AsyncChannelDeliveryStore
from opensquilla.channels.types import IncomingMessage
assert ChannelDeliveryStore.__module__ == 'opensquilla.channels.delivery_store'
assert AsyncChannelDeliveryStore.__module__ == 'opensquilla.channels.storage_worker'
message = IncomingMessage(sender_id='user', channel_id='channel', content='payload')
assert message.content == 'payload'
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_public_channel_exports_preserve_identity() -> None:
    import opensquilla.channels as channels

    expected = {
        **dict.fromkeys(
            ("Channel", "ManagedChannel", "ChannelHealth", "ChannelMeta",
             "IncomingMessage", "OutgoingMessage", "Attachment"),
            "types",
        ),
        "ChannelManager": "manager",
        "TerminalChannel": "terminal",
        "WebSocketChannel": "websocket",
        "SlackChannel": "slack",
        "FeishuChannel": "feishu",
        "DiscordChannel": "discord",
        "TelegramChannel": "telegram",
        "TelegramChannelConfig": "telegram",
    }
    assert channels.__all__ == list(expected)
    assert set(expected) <= set(dir(channels))
    for name, module_name in expected.items():
        owner = importlib.import_module(f"opensquilla.channels.{module_name}")
        assert getattr(channels, name) is getattr(owner, name)
        assert vars(channels)[name] is getattr(owner, name)

    namespace = {}
    exec("from opensquilla.channels import ChannelManager, IncomingMessage", namespace)
    assert namespace["ChannelManager"] is channels.ChannelManager
    assert namespace["IncomingMessage"] is channels.IncomingMessage
    exec("from opensquilla.channels import *", namespace)
    assert all(namespace[name] is getattr(channels, name) for name in expected)
    with pytest.raises(AttributeError, match="has no attribute 'missing'"):
        _ = channels.missing


def test_feishu_module_importable() -> None:
    importlib.import_module("opensquilla.channels.feishu")


def test_telegram_module_importable() -> None:
    importlib.import_module("opensquilla.channels.telegram")


def test_dingtalk_module_importable() -> None:
    importlib.import_module("opensquilla.channels.dingtalk")


def test_qq_module_importable() -> None:
    importlib.import_module("opensquilla.channels.qq")


def test_wecom_module_importable() -> None:
    importlib.import_module("opensquilla.channels.wecom")
