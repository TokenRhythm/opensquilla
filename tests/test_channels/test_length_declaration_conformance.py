"""The length declarations in capability profiles must match adapter reality.

Central dispatch trusts these declarations: ``splits_natively=True`` makes it
skip an adapter entirely, and ``max_message_len`` is enforced in the declared
unit. A declaration that drifts from the adapter's actual behavior either
double-splits or ships over-cap messages — these tripwires make the drift a
test failure instead of a production surprise.
"""

from __future__ import annotations

import importlib
import inspect

import pytest

from opensquilla.channels.contract import ChannelLengthUnit, channel_capability_profile
from opensquilla.channels.dingtalk import DingTalkChannel, DingTalkChannelConfig
from opensquilla.channels.discord import DiscordChannel, DiscordChannelConfig
from opensquilla.channels.feishu import FeishuChannel, FeishuChannelConfig
from opensquilla.channels.matrix import MatrixChannel, MatrixChannelConfig
from opensquilla.channels.msteams import MSTeamsChannel, MSTeamsChannelConfig
from opensquilla.channels.qq import QQChannel, QQChannelConfig
from opensquilla.channels.slack import SlackChannel
from opensquilla.channels.telegram import TelegramChannel, TelegramChannelConfig
from opensquilla.channels.wecom import WeComChannel, WeComChannelConfig

_ADAPTERS = [
    ("slack", lambda: SlackChannel(token="xoxb-token", slack_channel_id="C-default")),
    ("discord", lambda: DiscordChannel(DiscordChannelConfig(token="token"))),
    (
        "feishu",
        lambda: FeishuChannel(
            FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="websocket")
        ),
    ),
    ("dingtalk", lambda: DingTalkChannel(DingTalkChannelConfig())),
    ("wecom", lambda: WeComChannel(WeComChannelConfig())),
    ("qq", lambda: QQChannel(QQChannelConfig())),
    ("msteams", lambda: MSTeamsChannel(MSTeamsChannelConfig())),
    ("matrix", lambda: MatrixChannel(MatrixChannelConfig())),
    ("telegram", lambda: TelegramChannel(TelegramChannelConfig(transport_name="webhook"))),
]


@pytest.mark.parametrize(("adapter_name", "make"), _ADAPTERS)
def test_length_declaration_is_well_formed(adapter_name: str, make) -> None:
    profile = channel_capability_profile(make())
    assert profile is not None
    assert profile.max_message_len >= 0
    assert isinstance(profile.length_unit, ChannelLengthUnit)
    if profile.splits_natively:
        # An adapter may only opt out of central splitting if it declares the
        # cap it splits to.
        assert profile.max_message_len > 0


@pytest.mark.parametrize(("adapter_name", "make"), _ADAPTERS)
def test_native_splitter_declarations_are_backed_by_a_split_call(adapter_name: str, make) -> None:
    # splits_natively=True makes central dispatch skip the adapter, so a
    # declaration without an actual split in the adapter ships over-cap
    # messages the platform rejects wholesale.
    profile = channel_capability_profile(make())
    assert profile is not None
    if not profile.splits_natively:
        return
    module = importlib.import_module(f"opensquilla.channels.{adapter_name}")
    assert "split_text_for_channel" in inspect.getsource(module), (
        f"{adapter_name} declares splits_natively=True but never calls the "
        "shared splitter; either split in send() or drop the declaration so "
        "central dispatch handles it"
    )


@pytest.mark.parametrize(
    ("adapter_name", "make", "constant_name"),
    [
        ("telegram", _ADAPTERS[8][1], "_TELEGRAM_MAX_MESSAGE_CHARS"),
        ("discord", _ADAPTERS[1][1], "_DISCORD_MAX_MESSAGE_CHARS"),
        ("slack", _ADAPTERS[0][1], "_SLACK_MAX_MESSAGE_CHARS"),
    ],
)
def test_native_splitter_cap_matches_the_declared_profile_cap(
    adapter_name: str, make, constant_name: str
) -> None:
    # The cap lives twice for native splitters: the module constant the
    # adapter splits with, and the profile declaration everything else reads.
    # If they drift, the contract advertises a limit the adapter won't honor.
    module = importlib.import_module(f"opensquilla.channels.{adapter_name}")
    profile = channel_capability_profile(make())
    assert profile is not None
    assert profile.max_message_len == getattr(module, constant_name)


def test_wecom_profiles_declare_identical_length_handling() -> None:
    # WeCom builds a different profile per connection mode; the platform cap
    # does not change with the transport.
    websocket = WeComChannel(WeComChannelConfig(connection_mode="websocket"))
    webhook = WeComChannel(WeComChannelConfig())
    ws_profile = channel_capability_profile(websocket)
    wh_profile = channel_capability_profile(webhook)
    assert ws_profile is not None and wh_profile is not None
    assert (
        ws_profile.max_message_len,
        ws_profile.length_unit,
        ws_profile.splits_natively,
    ) == (
        wh_profile.max_message_len,
        wh_profile.length_unit,
        wh_profile.splits_natively,
    )
