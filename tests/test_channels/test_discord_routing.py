from __future__ import annotations

from opensquilla.channels.contract import ChannelCapabilities
from opensquilla.channels.discord import DiscordChannel, DiscordChannelConfig
from opensquilla.channels.manager import ChannelManager


def test_discord_profile_declares_precise_implemented_capabilities() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token"))

    assert channel.capability_profile.supports(ChannelCapabilities.INBOUND_REACTIONS)
    assert not channel.capability_profile.supports(ChannelCapabilities.THREAD_MESSAGES)
    assert channel.capability_profile.supports(ChannelCapabilities.GROUP_DM)
    assert not channel.capability_profile.supports(ChannelCapabilities.THREAD_LIFECYCLE)
    assert not channel.capability_profile.supports(ChannelCapabilities.CARD_ACTIONS)


def test_discord_parse_event_distinguishes_dm_group_dm_and_guild_group() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token"))

    dm_msg = channel.parse_event(
        {
            "id": "msg-dm",
            "channel_id": "dm-channel",
            "channel_type": 1,
            "author": {"id": "user-1"},
            "content": "dm",
        }
    )
    group_dm_msg = channel.parse_event(
        {
            "id": "msg-group-dm",
            "channel_id": "group-dm-channel",
            "channel_type": 3,
            "author": {"id": "user-2"},
            "content": "group dm",
        }
    )
    guild_msg = channel.parse_event(
        {
            "id": "msg-guild",
            "channel_id": "guild-channel",
            "guild_id": "guild-1",
            "channel_type": 0,
            "author": {"id": "user-3"},
            "content": "guild",
        }
    )

    assert dm_msg.metadata["conversation_kind"] == "dm"
    assert dm_msg.metadata["is_group"] is False
    assert ChannelManager._build_session_key("discord", dm_msg) == (
        "agent:main:discord:direct:user-1"
    )

    assert group_dm_msg.metadata["conversation_kind"] == "group_dm"
    assert group_dm_msg.metadata["is_group"] is True
    assert ChannelManager._build_session_key("discord", group_dm_msg) == (
        "agent:main:discord:group:group-dm-channel:sender:user-2"
    )

    assert guild_msg.metadata["conversation_kind"] == "group"
    assert guild_msg.metadata["is_group"] is True
    assert ChannelManager._build_session_key("discord", guild_msg) == (
        "agent:main:discord:group:guild-channel:sender:user-3"
    )


def test_discord_parse_event_preserves_thread_native_metadata() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token"))

    msg = channel.parse_event(
        {
            "id": "msg-thread",
            "channel_id": "thread-channel",
            "guild_id": "guild-1",
            "channel_type": 11,
            "author": {"id": "user-1"},
            "content": "thread reply",
            "message_reference": {
                "message_id": "parent-message",
                "channel_id": "parent-channel",
                "guild_id": "guild-1",
            },
        }
    )

    assert msg.metadata["conversation_kind"] == "thread"
    assert msg.metadata["message_id"] == "msg-thread"
    assert msg.metadata["channel_id"] == "thread-channel"
    assert msg.metadata["guild_id"] == "guild-1"
    assert msg.metadata["channel_type"] == 11
    assert msg.metadata["native_message_id"] == "msg-thread"
    assert msg.metadata["native_chat_id"] == "thread-channel"
    assert msg.metadata["native_thread_id"] == "thread-channel"
    assert msg.metadata["native_parent_id"] == "parent-message"
    assert msg.metadata["native_root_id"] == "parent-message"
    assert msg.metadata["reply_target_id"] == "msg-thread"
    assert msg.metadata["referenced_message_id"] == "parent-message"
    assert ChannelManager._build_session_key("discord", msg) == (
        "agent:main:discord:group:thread-channel:sender:user-1:thread:thread-channel"
    )


def test_discord_streaming_reply_kwargs_target_inbound_channel() -> None:
    channel = DiscordChannel(
        DiscordChannelConfig(token="token", default_channel_id="static-channel")
    )

    msg = channel.parse_event(
        {
            "id": "msg-1",
            "channel_id": "origin-channel",
            "guild_id": "guild-1",
            "author": {"id": "user-1"},
            "content": "hello",
        }
    )

    assert channel.streaming_reply_kwargs(msg) == {"channel_id": "origin-channel"}


def test_discord_build_reply_message_targets_inbound_channel() -> None:
    channel = DiscordChannel(
        DiscordChannelConfig(token="token", default_channel_id="static-channel")
    )

    msg = channel.parse_event(
        {
            "id": "msg-1",
            "channel_id": "origin-channel",
            "guild_id": "guild-1",
            "author": {"id": "user-1"},
            "content": "hello",
        }
    )

    reply = channel.build_reply_message("answer", msg)

    assert reply.reply_to == "origin-channel"
    assert reply.metadata["reply_to_message_id"] == "msg-1"


async def test_discord_gateway_channel_create_cache_classifies_group_dm() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token"))

    await channel._handle_dispatch(
        "CHANNEL_CREATE",
        {"id": "group-dm-channel", "type": 3},
    )
    await channel._handle_dispatch(
        "MESSAGE_CREATE",
        {
            "id": "msg-1",
            "channel_id": "group-dm-channel",
            "author": {"id": "user-1"},
            "content": "hello group dm",
        },
    )

    msg = await channel.receive()

    assert msg.metadata["conversation_kind"] == "group_dm"
    assert msg.metadata["is_group"] is True
    assert ChannelManager._build_session_key("discord", msg) == (
        "agent:main:discord:group:group-dm-channel:sender:user-1"
    )


async def test_discord_gateway_thread_create_cache_classifies_thread_message() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token"))

    await channel._handle_dispatch(
        "THREAD_CREATE",
        {
            "id": "thread-channel",
            "type": 11,
            "parent_id": "parent-channel",
            "guild_id": "guild-1",
        },
    )
    await channel._handle_dispatch(
        "MESSAGE_CREATE",
        {
            "id": "msg-1",
            "channel_id": "thread-channel",
            "guild_id": "guild-1",
            "author": {"id": "user-1"},
            "content": "hello thread",
        },
    )

    msg = await channel.receive()

    assert msg.metadata["conversation_kind"] == "thread"
    assert msg.metadata["native_thread_id"] == "thread-channel"
    assert msg.metadata["native_parent_channel_id"] == "parent-channel"
    assert ChannelManager._build_session_key("discord", msg) == (
        "agent:main:discord:group:thread-channel:sender:user-1:thread:thread-channel"
    )


async def test_discord_thread_list_sync_cache_classifies_existing_thread_message() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token"))

    await channel._handle_dispatch(
        "THREAD_LIST_SYNC",
        {
            "guild_id": "guild-1",
            "threads": [
                {
                    "id": "existing-thread",
                    "type": 11,
                    "parent_id": "parent-channel",
                }
            ],
        },
    )
    await channel._handle_dispatch(
        "MESSAGE_CREATE",
        {
            "id": "msg-existing-thread",
            "channel_id": "existing-thread",
            "guild_id": "guild-1",
            "author": {"id": "user-1"},
            "content": "hello existing thread",
        },
    )

    msg = await channel.receive()

    assert msg.metadata["conversation_kind"] == "thread"
    assert msg.metadata["native_thread_id"] == "existing-thread"
    assert msg.metadata["native_parent_channel_id"] == "parent-channel"
    assert ChannelManager._build_session_key("discord", msg) == (
        "agent:main:discord:group:existing-thread:sender:user-1:thread:existing-thread"
    )


async def test_discord_guild_create_cache_classifies_active_thread_message() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token"))

    await channel._handle_dispatch(
        "GUILD_CREATE",
        {
            "id": "guild-1",
            "channels": [{"id": "parent-channel", "type": 0}],
            "threads": [
                {
                    "id": "startup-thread",
                    "type": 11,
                    "parent_id": "parent-channel",
                }
            ],
        },
    )
    await channel._handle_dispatch(
        "MESSAGE_CREATE",
        {
            "id": "msg-startup-thread",
            "channel_id": "startup-thread",
            "guild_id": "guild-1",
            "author": {"id": "user-1"},
            "content": "hello startup thread",
        },
    )

    msg = await channel.receive()

    assert msg.metadata["conversation_kind"] == "thread"
    assert msg.metadata["native_thread_id"] == "startup-thread"
    assert msg.metadata["native_parent_channel_id"] == "parent-channel"
    assert ChannelManager._build_session_key("discord", msg) == (
        "agent:main:discord:group:startup-thread:sender:user-1:thread:startup-thread"
    )


async def test_discord_thread_reaction_uses_cached_thread_session() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token"))

    await channel._handle_dispatch(
        "THREAD_CREATE",
        {
            "id": "thread-channel",
            "type": 11,
            "parent_id": "parent-channel",
            "guild_id": "guild-1",
        },
    )
    await channel._handle_dispatch(
        "MESSAGE_REACTION_ADD",
        {
            "message_id": "msg-thread",
            "channel_id": "thread-channel",
            "guild_id": "guild-1",
            "user_id": "user-1",
            "emoji": {"name": "thumbsup"},
        },
    )

    msg = await channel.receive()

    assert msg.metadata["conversation_kind"] == "thread"
    assert msg.metadata["native_thread_id"] == "thread-channel"
    assert msg.metadata["native_parent_channel_id"] == "parent-channel"
    assert ChannelManager._build_session_key("discord", msg) == (
        "agent:main:discord:group:thread-channel:sender:user-1:thread:thread-channel"
    )


async def test_discord_group_dm_interaction_uses_cached_group_session() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token"))

    await channel._handle_dispatch(
        "CHANNEL_CREATE",
        {"id": "group-dm-channel", "type": 3},
    )
    await channel._handle_dispatch(
        "INTERACTION_CREATE",
        {
            "id": "interaction-1",
            "channel_id": "group-dm-channel",
            "user": {"id": "user-1"},
            "data": {
                "name": "hello",
                "options": [{"value": "world"}],
            },
        },
    )

    msg = await channel.receive()

    assert msg.metadata["conversation_kind"] == "group_dm"
    assert msg.metadata["is_group"] is True
    assert msg.metadata["native_message_id"] == "interaction-1"
    assert msg.metadata["reply_target_id"] == "interaction-1"
    assert ChannelManager._build_session_key("discord", msg) == (
        "agent:main:discord:group:group-dm-channel:sender:user-1"
    )
