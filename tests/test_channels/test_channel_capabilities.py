from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.artifacts import ArtifactStore
from opensquilla.channels.artifact_delivery import (
    can_deliver_channel_files,
    deliver_artifacts_as_channel_files,
    strip_delivered_artifact_image_references,
)
from opensquilla.channels.contract import (
    PUBLIC_VENDOR_ADAPTERS,
    ChannelCapabilities,
    ChannelCapabilityProfile,
    ChannelPlatformCapabilityStatus,
    ChannelPlatformCategories,
    ChannelPlatformManifest,
    ChannelSendResult,
    ChannelSendStatus,
    channel_capability_evidence,
    channel_capability_profile,
    channel_platform_manifest,
    normalize_channel_send_result,
    run_channel_contract,
)
from opensquilla.channels.dingtalk import DingTalkChannel, DingTalkChannelConfig
from opensquilla.channels.discord import DiscordChannel, DiscordChannelConfig
from opensquilla.channels.feishu import FeishuChannel, FeishuChannelConfig
from opensquilla.channels.manager import ChannelManager
from opensquilla.channels.matrix import MatrixChannel, MatrixChannelConfig
from opensquilla.channels.msteams import MSTeamsChannel, MSTeamsChannelConfig
from opensquilla.channels.qq import QQChannel, QQChannelConfig
from opensquilla.channels.slack import SlackChannel
from opensquilla.channels.telegram import TelegramChannel, TelegramChannelConfig
from opensquilla.channels.types import IncomingMessage
from opensquilla.channels.wecom import WeComChannel, WeComChannelConfig
from opensquilla.gateway.routing import build_channel_route_envelope

PlatformCapabilityExpectation = dict[
    str,
    tuple[ChannelPlatformCapabilityStatus, tuple[str, ...], tuple[str, ...]],
]


def test_channel_capabilities_cover_structured_delivery_and_events() -> None:
    assert ChannelCapabilities.ARTIFACT_DELIVERY == "artifact_delivery"
    assert ChannelCapabilities.NATIVE_FILE_UPLOAD == "native_file_upload"
    assert ChannelCapabilities.MEDIA == "media"
    assert ChannelCapabilities.REACTIONS == "reactions"
    assert ChannelCapabilities.THREADS == "threads"
    assert ChannelCapabilities.EDIT == "edit"
    assert ChannelCapabilities.CARDS == "cards"
    assert ChannelCapabilities.MEMBER_EVENTS == "member_events"


@pytest.mark.parametrize("adapter_name", PUBLIC_VENDOR_ADAPTERS)
def test_public_vendor_adapters_keep_shared_channel_contract(adapter_name: str) -> None:
    module = importlib.import_module(f"opensquilla.channels.{adapter_name}")

    run_channel_contract(module)


def test_channel_capability_profile_derives_compatibility_tags() -> None:
    profile = ChannelCapabilityProfile(
        channel_type="discord",
        group_chat=True,
        mentions=True,
        typing_indicator=True,
        native_file_upload=True,
        media=True,
        reactions=True,
        threads=True,
        edit=True,
        delete=True,
        transports=("websocket",),
    )

    assert profile.supports(ChannelCapabilities.NATIVE_FILE_UPLOAD)
    assert profile.supports(ChannelCapabilities.TYPING_INDICATOR)
    assert profile.supports(ChannelCapabilities.WEBSOCKET)
    assert profile.capability_tags() >= {
        ChannelCapabilities.GROUP_CHAT,
        ChannelCapabilities.MENTIONS,
        ChannelCapabilities.TYPING_INDICATOR,
        ChannelCapabilities.NATIVE_FILE_UPLOAD,
        ChannelCapabilities.MEDIA,
        ChannelCapabilities.REACTIONS,
        ChannelCapabilities.THREADS,
        ChannelCapabilities.EDIT,
        ChannelCapabilities.WEBSOCKET,
    }


def test_capability_profile_exposes_precise_channel_features() -> None:
    profile = ChannelCapabilityProfile(
        channel_type="example",
        group_chat=True,
        mentions=True,
        native_file_upload=True,
        artifact_delivery=True,
        inbound_reactions=True,
        outbound_status_reactions=False,
        thread_messages=True,
        thread_lifecycle=False,
        interactive_cards=False,
        card_actions=False,
        member_events=True,
        transports=("websocket",),
    )

    tags = profile.capability_tags()

    assert ChannelCapabilities.GROUP_CHAT in tags
    assert ChannelCapabilities.NATIVE_FILE_UPLOAD in tags
    assert ChannelCapabilities.INBOUND_REACTIONS in tags
    assert ChannelCapabilities.OUTBOUND_STATUS_REACTIONS not in tags
    assert ChannelCapabilities.THREAD_MESSAGES in tags
    assert ChannelCapabilities.THREAD_LIFECYCLE not in tags
    assert ChannelCapabilities.CARD_ACTIONS not in tags


def test_platform_manifest_derives_honest_boundary_from_profile() -> None:
    profile = ChannelCapabilityProfile(
        channel_type="example",
        group_chat=True,
        native_file_upload=True,
        media=True,
        thread_reply=True,
        cards=True,
        scope_diagnostics=True,
    )

    manifest = ChannelPlatformManifest.from_channel_profile(
        profile,
        has_send_file=True,
        has_inbound_attachment_resolver=True,
    )

    assert manifest.supports(ChannelPlatformCategories.CHAT)
    assert manifest.supports(ChannelPlatformCategories.FILES)
    assert manifest.supports(ChannelPlatformCategories.ATTACHMENTS)
    assert manifest.supports(ChannelPlatformCategories.THREADS)
    assert manifest.supports(ChannelPlatformCategories.CARDS)
    assert manifest.supports(ChannelPlatformCategories.SCOPES)
    assert manifest.get(ChannelPlatformCategories.DOCS).status == (
        ChannelPlatformCapabilityStatus.UNSUPPORTED
    )
    assert manifest.get(ChannelPlatformCategories.PERMISSIONS).status == (
        ChannelPlatformCapabilityStatus.UNSUPPORTED
    )


@pytest.mark.parametrize(
    ("adapter_name", "channel"),
    [
        ("slack", SlackChannel(token="xoxb-token", slack_channel_id="C-default")),
        ("discord", DiscordChannel(DiscordChannelConfig(token="token"))),
        (
            "feishu",
            FeishuChannel(
                FeishuChannelConfig(
                    app_id="app",
                    app_secret="secret",
                    connection_mode="websocket",
                )
            ),
        ),
        ("dingtalk", DingTalkChannel(DingTalkChannelConfig())),
        ("wecom", WeComChannel(WeComChannelConfig())),
        ("qq", QQChannel(QQChannelConfig())),
        ("msteams", MSTeamsChannel(MSTeamsChannelConfig())),
        ("matrix", MatrixChannel(MatrixChannelConfig())),
        ("telegram", TelegramChannel(TelegramChannelConfig(transport_name="webhook"))),
    ],
)
def test_public_vendor_channels_expose_platform_manifests(
    adapter_name: str,
    channel: object,
) -> None:
    manifest = channel_platform_manifest(channel)

    assert isinstance(manifest, ChannelPlatformManifest)
    assert manifest.channel_type == adapter_name
    assert manifest.get(ChannelPlatformCategories.CHAT).status == (
        ChannelPlatformCapabilityStatus.SUPPORTED
    )
    assert manifest.get(ChannelPlatformCategories.FILES).status in {
        ChannelPlatformCapabilityStatus.SUPPORTED,
        ChannelPlatformCapabilityStatus.UNSUPPORTED,
        ChannelPlatformCapabilityStatus.CONFIG_REQUIRED,
    }
    assert manifest.get(ChannelPlatformCategories.DOCS).status in {
        ChannelPlatformCapabilityStatus.SUPPORTED,
        ChannelPlatformCapabilityStatus.UNSUPPORTED,
        ChannelPlatformCapabilityStatus.CONFIG_REQUIRED,
    }


def test_feishu_platform_manifest_derives_from_the_profile() -> None:
    # The channel no longer bundles vendor API tools (docs/drive/wiki are
    # Feishu's own MCP server and CLI, mounted through the MCP client), so the
    # manifest is the honest derivation from the capability profile: what the
    # conversation surface itself can do.
    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="websocket")
    )

    manifest = channel_platform_manifest(channel)
    assert isinstance(manifest, ChannelPlatformManifest)

    chat = manifest.get(ChannelPlatformCategories.CHAT)
    files = manifest.get(ChannelPlatformCategories.FILES)
    docs = manifest.get(ChannelPlatformCategories.DOCS)

    assert chat.status == ChannelPlatformCapabilityStatus.SUPPORTED
    assert files.status == ChannelPlatformCapabilityStatus.SUPPORTED
    assert docs.status == ChannelPlatformCapabilityStatus.UNSUPPORTED
    assert not docs.tools


@pytest.mark.parametrize(
    ("channel", "expectations"),
    [
        (
            SlackChannel(token="xoxb-token", slack_channel_id="C-default"),
            {
                ChannelPlatformCategories.FILES: (
                    ChannelPlatformCapabilityStatus.SUPPORTED,
                    ("files.getUploadURLExternal", "files.completeUploadExternal"),
                    ("files:write",),
                ),
                ChannelPlatformCategories.THREADS: (
                    ChannelPlatformCapabilityStatus.SUPPORTED,
                    ("thread_ts",),
                    (),
                ),
            },
        ),
        (
            DiscordChannel(DiscordChannelConfig(token="token")),
            {
                ChannelPlatformCategories.FILES: (
                    ChannelPlatformCapabilityStatus.SUPPORTED,
                    ("multipart/form-data message attachments",),
                    (),
                ),
                ChannelPlatformCategories.ATTACHMENTS: (
                    ChannelPlatformCapabilityStatus.SUPPORTED,
                    ("attachment.url",),
                    (),
                ),
            },
        ),
        (
            TelegramChannel(TelegramChannelConfig(transport_name="webhook")),
            {
                ChannelPlatformCategories.FILES: (
                    ChannelPlatformCapabilityStatus.SUPPORTED,
                    ("sendDocument", "getFile"),
                    (),
                ),
                ChannelPlatformCategories.ATTACHMENTS: (
                    ChannelPlatformCapabilityStatus.SUPPORTED,
                    ("getFile",),
                    (),
                ),
            },
        ),
        (
            MatrixChannel(MatrixChannelConfig()),
            {
                ChannelPlatformCategories.FILES: (
                    ChannelPlatformCapabilityStatus.SUPPORTED,
                    ("media.upload", "room_send"),
                    (),
                ),
                ChannelPlatformCategories.ATTACHMENTS: (
                    ChannelPlatformCapabilityStatus.SUPPORTED,
                    ("media.download",),
                    (),
                ),
            },
        ),
        (
            WeComChannel(WeComChannelConfig()),
            {
                ChannelPlatformCategories.FILES: (
                    ChannelPlatformCapabilityStatus.SUPPORTED,
                    ("media/upload", "message/send:file"),
                    (),
                ),
                ChannelPlatformCategories.ATTACHMENTS: (
                    ChannelPlatformCapabilityStatus.UNSUPPORTED,
                    (),
                    (),
                ),
            },
        ),
        (
            MSTeamsChannel(MSTeamsChannelConfig()),
            {
                ChannelPlatformCategories.FILES: (
                    ChannelPlatformCapabilityStatus.UNSUPPORTED,
                    ("FileConsentCard", "Microsoft Graph file attachments"),
                    (),
                ),
                ChannelPlatformCategories.ATTACHMENTS: (
                    ChannelPlatformCapabilityStatus.UNSUPPORTED,
                    ("Bot Framework attachments",),
                    (),
                ),
            },
        ),
        (
            DingTalkChannel(DingTalkChannelConfig()),
            {
                ChannelPlatformCategories.FILES: (
                    ChannelPlatformCapabilityStatus.UNSUPPORTED,
                    (),
                    (),
                ),
                ChannelPlatformCategories.CARDS: (
                    ChannelPlatformCapabilityStatus.SUPPORTED,
                    ("MarkdownCardInstance",),
                    (),
                ),
            },
        ),
        (
            QQChannel(QQChannelConfig()),
            {
                ChannelPlatformCategories.FILES: (
                    ChannelPlatformCapabilityStatus.UNSUPPORTED,
                    (),
                    (),
                ),
                ChannelPlatformCategories.MEDIA: (
                    ChannelPlatformCapabilityStatus.UNSUPPORTED,
                    (),
                    (),
                ),
            },
        ),
    ],
)
def test_non_feishu_platform_manifests_are_provider_specific(
    channel: object,
    expectations: PlatformCapabilityExpectation,
) -> None:
    manifest = channel_platform_manifest(channel)
    assert isinstance(manifest, ChannelPlatformManifest)

    for category, (status, tools, required_scopes) in expectations.items():
        capability = manifest.get(category)
        assert capability.status == status
        assert capability.tools == tools
        assert capability.required_scopes == required_scopes
        assert capability.notes


@pytest.mark.parametrize(
    ("adapter_name", "channel"),
    [
        ("slack", SlackChannel(token="xoxb-token", slack_channel_id="C-default")),
        ("discord", DiscordChannel(DiscordChannelConfig(token="token"))),
        (
            "feishu",
            FeishuChannel(
                FeishuChannelConfig(
                    app_id="app",
                    app_secret="secret",
                    connection_mode="websocket",
                )
            ),
        ),
        ("dingtalk", DingTalkChannel(DingTalkChannelConfig())),
        ("wecom", WeComChannel(WeComChannelConfig())),
        ("qq", QQChannel(QQChannelConfig())),
        ("msteams", MSTeamsChannel(MSTeamsChannelConfig())),
        ("matrix", MatrixChannel(MatrixChannelConfig())),
        ("telegram", TelegramChannel(TelegramChannelConfig(transport_name="webhook"))),
    ],
)
def test_public_vendor_channels_expose_typed_capability_profiles(
    adapter_name: str,
    channel: object,
) -> None:
    profile = channel_capability_profile(channel)

    assert isinstance(profile, ChannelCapabilityProfile)
    assert profile.channel_type == adapter_name


def test_slack_profile_matches_current_web_api_adapter_surface() -> None:
    channel = SlackChannel(
        token="xoxb-token",
        slack_channel_id="C-default",
        status_reactions_enabled=True,
    )

    profile = channel.capability_profile

    assert profile.supports(ChannelCapabilities.WEBHOOK)
    assert profile.supports(ChannelCapabilities.GROUP_CHAT)
    assert profile.supports(ChannelCapabilities.MENTIONS)
    assert profile.supports(ChannelCapabilities.THREADS)
    assert profile.supports(ChannelCapabilities.THREAD_REPLY)
    assert profile.supports(ChannelCapabilities.EDIT)
    assert profile.supports(ChannelCapabilities.DELETE)
    assert profile.supports(ChannelCapabilities.OUTBOUND_STATUS_REACTIONS)
    assert profile.supports(ChannelCapabilities.NATIVE_FILE_UPLOAD)
    assert not profile.supports(ChannelCapabilities.CARD_ACTIONS)


def test_telegram_profile_matches_current_bot_api_adapter_surface() -> None:
    channel = TelegramChannel(TelegramChannelConfig(transport_name="webhook"))

    profile = channel.capability_profile

    assert profile.supports(ChannelCapabilities.WEBHOOK)
    assert profile.supports(ChannelCapabilities.GROUP_CHAT)
    assert profile.supports(ChannelCapabilities.MENTIONS)
    assert profile.supports(ChannelCapabilities.MEDIA)
    assert profile.supports(ChannelCapabilities.REPLY)
    assert profile.supports(ChannelCapabilities.THREAD_REPLY)
    assert profile.supports(ChannelCapabilities.EDIT)
    assert profile.supports(ChannelCapabilities.DELETE)
    assert profile.supports(ChannelCapabilities.TYPING_INDICATOR)
    assert profile.supports(ChannelCapabilities.REACTIONS)
    assert profile.supports(ChannelCapabilities.NATIVE_FILE_UPLOAD)

    evidence = channel_capability_evidence(channel)
    assert evidence[ChannelCapabilities.TYPING_INDICATOR]["methods"] == [
        "send_typing"
    ]
    assert evidence[ChannelCapabilities.REACTIONS]["methods"] == ["set_reaction"]
    assert evidence[ChannelCapabilities.GROUP_CHAT]["evidence_kind"] == "declaration"


def test_matrix_profile_matches_current_sync_adapter_surface() -> None:
    channel = MatrixChannel(MatrixChannelConfig())

    profile = channel.capability_profile

    assert not profile.supports(ChannelCapabilities.WEBSOCKET)
    assert profile.transports == ("http_sync",)
    assert profile.supports(ChannelCapabilities.GROUP_CHAT)
    assert profile.supports(ChannelCapabilities.MENTIONS)
    assert profile.supports(ChannelCapabilities.MEDIA)
    assert profile.supports(ChannelCapabilities.REPLY)
    assert profile.supports(ChannelCapabilities.EDIT)
    assert profile.supports(ChannelCapabilities.DELETE)
    assert not profile.supports(ChannelCapabilities.THREAD_REPLY)
    assert profile.supports(ChannelCapabilities.NATIVE_FILE_UPLOAD)


def test_msteams_profile_matches_current_bot_framework_adapter_surface() -> None:
    channel = MSTeamsChannel(MSTeamsChannelConfig())

    profile = channel.capability_profile

    assert profile.supports(ChannelCapabilities.WEBHOOK)
    assert profile.supports(ChannelCapabilities.GROUP_CHAT)
    assert profile.supports(ChannelCapabilities.MENTIONS)
    assert profile.supports(ChannelCapabilities.REPLY)
    assert profile.supports(ChannelCapabilities.EDIT)
    assert profile.supports(ChannelCapabilities.DELETE)
    assert not profile.supports(ChannelCapabilities.NATIVE_FILE_UPLOAD)
    assert not profile.supports(ChannelCapabilities.CARD_ACTIONS)


def test_dingtalk_profile_matches_current_stream_adapter_surface() -> None:
    channel = DingTalkChannel(DingTalkChannelConfig())

    profile = channel.capability_profile

    assert profile.supports(ChannelCapabilities.WEBSOCKET)
    assert profile.supports(ChannelCapabilities.GROUP_CHAT)
    assert profile.supports(ChannelCapabilities.MENTIONS)
    assert profile.supports(ChannelCapabilities.REPLY)
    assert profile.supports(ChannelCapabilities.CARDS)
    assert not profile.supports(ChannelCapabilities.EDIT)
    assert not profile.supports(ChannelCapabilities.DELETE)
    assert not profile.supports(ChannelCapabilities.NATIVE_FILE_UPLOAD)


def test_wecom_profile_matches_current_corp_app_adapter_surface() -> None:
    channel = WeComChannel(WeComChannelConfig())

    profile = channel.capability_profile

    assert profile.supports(ChannelCapabilities.WEBHOOK)
    assert profile.supports(ChannelCapabilities.GROUP_CHAT)
    assert profile.supports(ChannelCapabilities.MENTIONS)
    assert profile.supports(ChannelCapabilities.REPLY)
    assert not profile.supports(ChannelCapabilities.EDIT)
    assert not profile.supports(ChannelCapabilities.DELETE)
    assert profile.supports(ChannelCapabilities.NATIVE_FILE_UPLOAD)
    assert profile.supports(ChannelCapabilities.MEDIA)


def test_qq_profile_matches_current_official_bot_adapter_surface() -> None:
    channel = QQChannel(QQChannelConfig())

    profile = channel.capability_profile

    assert profile.supports(ChannelCapabilities.WEBSOCKET)
    assert profile.supports(ChannelCapabilities.GROUP_CHAT)
    assert profile.supports(ChannelCapabilities.MENTIONS)
    assert profile.supports(ChannelCapabilities.REPLY)
    assert not profile.supports(ChannelCapabilities.EDIT)
    assert not profile.supports(ChannelCapabilities.DELETE)
    assert not profile.supports(ChannelCapabilities.NATIVE_FILE_UPLOAD)
    assert not profile.supports(ChannelCapabilities.MEDIA)


def test_group_thread_metadata_builds_thread_session_key() -> None:
    msg = IncomingMessage(
        sender_id="user-1",
        channel_id="chat-1",
        content="hello",
        metadata={
            "is_group": True,
            "conversation_kind": "thread",
            "native_thread_id": "thread-9",
            "thread_id": "ignored-legacy-thread",
        },
    )

    key = ChannelManager._build_session_key("discord", msg)

    assert key == "agent:main:discord:group:chat-1:sender:user-1:thread:thread-9"


def test_dm_message_uses_sender_session_even_with_native_message_metadata() -> None:
    msg = IncomingMessage(
        sender_id="user-1",
        channel_id="dm-1",
        content="hello",
        metadata={
            "is_group": False,
            "native_message_id": "msg-1",
            "native_thread_id": "thread-1",
        },
    )

    key = ChannelManager._build_session_key("feishu", msg)

    assert key == "agent:main:feishu:direct:user-1"


def test_channel_send_result_normalizes_legacy_none_success() -> None:
    result = normalize_channel_send_result(
        None,
        capability=ChannelCapabilities.NATIVE_FILE_UPLOAD,
        target_id="c1",
    )

    assert result == ChannelSendResult(
        status=ChannelSendStatus.SENT,
        capability=ChannelCapabilities.NATIVE_FILE_UPLOAD,
        target_id="c1",
    )


def test_strip_delivered_artifact_image_references_removes_loose_image_lines() -> None:
    text = "Here is the image:\nimage: generated-chart.png\nDone."
    artifacts = [{"name": "generated-chart.png"}]

    assert strip_delivered_artifact_image_references(text, artifacts) == (
        "Here is the image:\nDone."
    )


@pytest.mark.asyncio
async def test_artifact_delivery_honors_profile_without_native_file_upload(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    ref = store.publish_bytes(
        b"report",
        session_id="session-1",
        session_key="agent:main:channel:session-1",
        name="report.txt",
        mime="text/plain",
        source="test",
    )

    class TextOnlyChannel:
        capability_profile = ChannelCapabilityProfile(
            channel_type="text_only",
            native_file_upload=False,
            media=False,
        )
        send_file_called = False

        async def send_file(self, *_args: object, **_kwargs: object) -> None:
            self.send_file_called = True

    channel = TextOnlyChannel()
    msg = IncomingMessage(
        sender_id="u1",
        channel_id="c1",
        content="",
        metadata={"is_group": False},
    )
    config = SimpleNamespace(attachments=SimpleNamespace(media_root=str(tmp_path)))

    assert can_deliver_channel_files(channel) is False
    undelivered = await deliver_artifacts_as_channel_files(
        channel,
        msg,
        [ref.to_dict()],
        config,
    )

    assert undelivered == [ref.to_dict()]
    assert channel.send_file_called is False


@pytest.mark.asyncio
async def test_artifact_delivery_preserves_fallback_on_structured_failure(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    ref = store.publish_bytes(
        b"report",
        session_id="session-1",
        session_key="agent:main:channel:session-1",
        name="report.txt",
        mime="text/plain",
        source="test",
    )

    class FailingFileChannel:
        capability_profile = ChannelCapabilityProfile(
            channel_type="files",
            native_file_upload=True,
            media=True,
        )

        async def send_file(self, channel_id: str, file_path: str) -> ChannelSendResult:
            assert channel_id == "c1"
            assert Path(file_path).is_file()
            return ChannelSendResult.failed(
                capability=ChannelCapabilities.NATIVE_FILE_UPLOAD,
                target_id=channel_id,
                reason="simulated",
            )

    msg = IncomingMessage(
        sender_id="u1",
        channel_id="c1",
        content="",
        metadata={"is_group": False},
    )
    config = SimpleNamespace(attachments=SimpleNamespace(media_root=str(tmp_path)))

    undelivered = await deliver_artifacts_as_channel_files(
        FailingFileChannel(),
        msg,
        [ref.to_dict()],
        config,
    )

    assert undelivered == [ref.to_dict()]


def test_feishu_profile_and_inbound_group_metadata() -> None:
    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="websocket")
    )

    assert channel.capability_profile.supports(ChannelCapabilities.WEBSOCKET)
    assert channel.capability_profile.supports(ChannelCapabilities.NATIVE_FILE_UPLOAD)
    assert channel.capability_profile.supports(ChannelCapabilities.REPLY)
    assert not channel.capability_profile.supports(ChannelCapabilities.INBOUND_REACTIONS)
    assert not channel.capability_profile.supports(ChannelCapabilities.THREAD_MESSAGES)
    assert channel.capability_profile.supports(ChannelCapabilities.THREAD_REPLY)
    assert channel.capability_profile.supports(ChannelCapabilities.SCOPE_DIAGNOSTICS)
    assert not channel.capability_profile.supports(ChannelCapabilities.INTERACTIVE_CARDS)
    assert not channel.capability_profile.supports(ChannelCapabilities.CARD_ACTIONS)

    webhook_channel = FeishuChannel(
        FeishuChannelConfig(
            app_id="app",
            app_secret="secret",
            connection_mode="webhook",
        )
    )
    assert webhook_channel.capability_profile.supports(ChannelCapabilities.INTERACTIVE_CARDS)

    group_msg = channel.parse_event(
        {
            "header": {"event_id": "evt-group"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_group",
                    "chat_id": "oc_group",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": '{"text":"hi"}',
                },
            },
        }
    )
    direct_msg = channel.parse_event(
        {
            "header": {"event_id": "evt-dm"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_dm",
                    "chat_id": "ou_user",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": '{"text":"hi"}',
                },
            },
        }
    )

    assert group_msg.metadata["is_group"] is True
    assert direct_msg.metadata["is_group"] is False
    reply = channel.build_reply_message("hello", group_msg)
    assert reply.reply_to == "oc_group"
    assert reply.metadata["reply_message_id"] == "om_group"


def test_feishu_parse_event_preserves_native_conversation_metadata() -> None:
    channel = FeishuChannel(
        FeishuChannelConfig(
            app_id="app",
            app_secret="secret",
            connection_mode="websocket",
            status_reactions_enabled=True,
        )
    )

    assert channel.capability_profile.supports(ChannelCapabilities.OUTBOUND_STATUS_REACTIONS)

    msg = channel.parse_event(
        {
            "header": {"event_id": "evt-thread", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_message",
                    "root_id": "om_root",
                    "parent_id": "om_parent",
                    "thread_id": "omt_thread",
                    "chat_id": "oc_group",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": '{"text":"@_user_1 hello"}',
                    "mentions": [
                        {"key": "@_user_1", "id": {"open_id": "ou_bot"}},
                        {"key": "@_user_2", "id": {"open_id": "ou_user_2"}},
                    ],
                },
            },
        }
    )

    assert msg.metadata["conversation_kind"] == "thread"
    assert msg.metadata["message_id"] == "om_message"
    assert msg.metadata["chat_id"] == "oc_group"
    assert msg.metadata["root_id"] == "om_root"
    assert msg.metadata["parent_id"] == "om_parent"
    assert "thread_id" not in msg.metadata
    assert msg.metadata["native_message_id"] == "om_message"
    assert msg.metadata["native_chat_id"] == "oc_group"
    assert msg.metadata["native_root_id"] == "om_root"
    assert msg.metadata["native_parent_id"] == "om_parent"
    assert msg.metadata["native_thread_id"] == "omt_thread"
    assert msg.metadata["reply_target_id"] == "om_message"
    assert msg.metadata["mentions"] == [
        {"key": "@_user_1", "id": {"open_id": "ou_bot"}},
        {"key": "@_user_2", "id": {"open_id": "ou_user_2"}},
    ]
    assert msg.metadata["mention_map"] == {
        "@_user_1": "ou_bot",
        "@_user_2": "ou_user_2",
    }
    channel.bot_open_id = "ou_bot"
    assert channel.is_group_mentioned(msg) is True

    envelope = build_channel_route_envelope(
        msg,
        session_key="agent:main:feishu:group:oc_group:thread:omt_thread",
        session_prefix="feishu",
    )

    assert envelope.channel_id == "oc_group"
    assert envelope.thread_id is None
    assert envelope.reply_target is not None
    assert envelope.reply_target.to == "oc_group"
    assert envelope.reply_target.thread_id is None


def test_feishu_topic_group_thread_remains_group_thread_session() -> None:
    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="websocket")
    )

    msg = channel.parse_event(
        {
            "header": {"event_id": "evt-topic", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_topic",
                    "thread_id": "omt_topic",
                    "chat_id": "oc_topic",
                    "chat_type": "topic_group",
                    "message_type": "text",
                    "content": '{"text":"topic hello"}',
                },
            },
        }
    )

    assert msg.metadata["conversation_kind"] == "topic"
    assert msg.metadata["is_group"] is True
    assert msg.metadata["native_thread_id"] == "omt_topic"
    assert "thread_id" not in msg.metadata
    assert ChannelManager._build_session_key("feishu", msg) == (
        "agent:main:feishu:group:oc_topic:sender:ou_user:thread:omt_topic"
    )


def test_discord_profile_and_inbound_group_metadata() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token"))

    assert channel.capability_profile.supports(ChannelCapabilities.WEBSOCKET)
    assert channel.capability_profile.supports(ChannelCapabilities.TYPING_INDICATOR)
    assert channel.capability_profile.supports(ChannelCapabilities.NATIVE_FILE_UPLOAD)

    group_msg = channel.parse_event(
        {
            "id": "m1",
            "channel_id": "c1",
            "guild_id": "g1",
            "author": {"id": "u1"},
            "content": "hello",
        }
    )
    direct_msg = channel.parse_event(
        {
            "id": "m2",
            "channel_id": "dm1",
            "author": {"id": "u1"},
            "content": "hello",
        }
    )

    assert group_msg.metadata["is_group"] is True
    assert direct_msg.metadata["is_group"] is False


def test_slack_parse_event_sets_explicit_group_metadata() -> None:
    channel = SlackChannel(token="xoxb-token", slack_channel_id="C-default")

    group_msg = channel.parse_event(
        {
            "user": "U1",
            "channel": "C-general",
            "channel_type": "channel",
            "text": "hello",
        }
    )
    direct_msg = channel.parse_event(
        {
            "user": "U1",
            "channel": "D-user",
            "channel_type": "im",
            "text": "hello",
        }
    )

    assert group_msg.metadata["is_group"] is True
    assert direct_msg.metadata["is_group"] is False


@pytest.mark.asyncio
async def test_discord_typing_targets_active_channel_before_default() -> None:
    requests: list[str] = []

    class FakeClient:
        async def post(self, path: str, **_kwargs: object) -> object:
            requests.append(path)
            return object()

    channel = DiscordChannel(
        DiscordChannelConfig(token="token", default_channel_id="default-channel")
    )
    channel._client = FakeClient()

    await channel.send_typing(channel_id="active-channel")
    await channel.send_typing()

    assert requests == [
        "/channels/active-channel/typing",
        "/channels/default-channel/typing",
    ]
