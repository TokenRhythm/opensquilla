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
import os
import sys

assert sys.prefix == sys.argv[1], 'storage import probe must retain the test venv'
print(f'storage-import-pid={os.getpid()}', flush=True)

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
    environment = {**os.environ, "PYTHONPATH": str(root / "src")}
    interpreter = sys.executable
    if os.name == "nt":
        # Own the importing interpreter so the timeout also closes its pipes.
        interpreter = sys._base_executable
        environment["__PYVENV_LAUNCHER__"] = sys.executable
    process = subprocess.Popen(
        [interpreter, "-c", script, sys.prefix],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=20)
    except BaseException as error:
        try:
            if process.poll() is None:
                process.kill()
        except OSError as cleanup_error:
            error.add_note(f"storage import child kill failed: {cleanup_error!r}")
        try:
            stdout, stderr = process.communicate(timeout=5)
        except BaseException as cleanup_error:
            error.add_note(f"storage import child cleanup failed: {cleanup_error!r}")
        else:
            error.add_note(
                f"storage import child pid={process.pid}, returncode={process.returncode}; "
                f"stdout={stdout!r}; stderr={stderr!r}"
            )
        raise
    assert process.returncode == 0, stderr
    assert f"storage-import-pid={process.pid}" in stdout.splitlines(), (
        f"storage import watchdog must own the interpreter: {stdout!r}"
    )


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
