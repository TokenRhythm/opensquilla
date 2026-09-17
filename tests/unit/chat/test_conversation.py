from __future__ import annotations

from opensquilla.chat.conversation import ChatSendRequest, sessions_send_params
from opensquilla.chat.source import chat_source_metadata


def test_web_chat_source_metadata_matches_existing_rpc_shape() -> None:
    source = chat_source_metadata(
        caller_kind="web",
        channel_kind="webchat",
        channel_id="webchat:webchat:main",
        sender_id="operator",
        source_kind="webui",
        source_name="WebChat",
    )

    assert source["caller_kind"] == "web"
    assert source["channel_kind"] == "webchat"
    assert source["channel_id"] == "webchat:webchat:main"
    assert source["sender_id"] == "operator"
    assert source["source_kind"] == "webui"
    assert source["source_name"] == "WebChat"


def test_web_chat_source_metadata_preserves_allowed_elevation_hint() -> None:
    source = chat_source_metadata(
        caller_kind="web",
        channel_kind="webchat",
        channel_id="webchat:webchat:main",
        sender_id="operator",
        source_kind="webui",
        source_name="WebChat",
        elevated="bypass",
    )

    assert source["elevated"] == "bypass"


def test_web_chat_source_metadata_preserves_run_mode_hint() -> None:
    source = chat_source_metadata(
        caller_kind="web",
        channel_kind="webchat",
        channel_id="webchat:webchat:main",
        sender_id="operator",
        source_kind="webui",
        source_name="WebChat",
        run_mode="full",
    )

    assert source["runMode"] == "full"


def test_web_chat_source_metadata_decodes_legacy_mode_to_canonical_safe() -> None:
    source = chat_source_metadata(
        caller_kind="web",
        channel_kind="webchat",
        channel_id="webchat:webchat:main",
        sender_id="operator",
        source_kind="webui",
        source_name="WebChat",
        run_mode="trusted",
    )

    assert source["runMode"] == "safe"


def test_chat_send_request_preserves_message_attachments_and_intent() -> None:
    request = ChatSendRequest(
        session_key="webchat:main",
        message="hello",
        attachments=[{"type": "text/plain", "data": "x"}],
        display_text="hello.txt",
        intent="new_chat",
        extra={"runKind": "manual"},
    )

    params = sessions_send_params(
        request,
        chat_source_metadata(
            caller_kind="web",
            channel_kind="webchat",
            channel_id="webchat:webchat:main",
            sender_id="operator",
            source_kind="webui",
            source_name="WebChat",
        ),
    )

    assert params["key"] == "webchat:main"
    assert params["message"] == "hello"
    assert params["attachments"] == [{"type": "text/plain", "data": "x"}]
    assert params["displayText"] == "hello.txt"
    assert params["intent"] == "new_chat"
    assert params["runKind"] == "manual"
