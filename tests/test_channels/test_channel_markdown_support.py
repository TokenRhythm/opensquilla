"""Rich-text (markdown) outbound support across channel adapters."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from opensquilla.channels.dingtalk import DingTalkChannel, DingTalkChannelConfig
from opensquilla.channels.discord import DiscordChannel
from opensquilla.channels.feishu import FeishuChannel, FeishuChannelConfig, _TokenState
from opensquilla.channels.matrix import MatrixChannel
from opensquilla.channels.qq import QQChannel
from opensquilla.channels.slack import SlackChannel
from opensquilla.channels.telegram import (
    TelegramApiError,
    TelegramChannel,
    TelegramChannelConfig,
)
from opensquilla.channels.types import OutgoingMessage
from opensquilla.channels.wecom import WeComChannel, WeComChannelConfig


def test_markdown_capable_declarations_match_rendering_support() -> None:
    """Every adapter declares whether it can render markdown natively."""

    assert FeishuChannel.markdown_capable is True
    assert WeComChannel.markdown_capable is True
    assert DingTalkChannel.markdown_capable is True
    assert TelegramChannel.markdown_capable is True
    assert SlackChannel.markdown_capable is True
    assert DiscordChannel.markdown_capable is True
    assert MatrixChannel.markdown_capable is True
    assert QQChannel.markdown_capable is True


@pytest.mark.asyncio
async def test_feishu_send_markdown_posts_rich_message() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = await request.aread()
        payload = json.loads(body)
        assert payload["msg_type"] == "post"
        post = json.loads(payload["content"])["zh_cn"]
        assert post["content"][0][0]["tag"] == "md"
        assert post["content"][0][0]["text"] == "**bold** text"
        return httpx.Response(200, json={"code": 0})

    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="webhook")
    )
    channel._token_state = _TokenState(token="tenant-token", expires_at=999999999.0)
    channel._client = httpx.AsyncClient(
        base_url="https://open.feishu.cn/open-apis",
        transport=httpx.MockTransport(handler),
    )

    try:
        await channel.send(
            OutgoingMessage(
                content="**bold** text",
                reply_to="ou_user",
                format="markdown",
            )
        )
    finally:
        await channel.stop()

    assert len(requests) == 1
    assert requests[0].url.path == "/open-apis/im/v1/messages"


@pytest.mark.asyncio
async def test_feishu_send_text_stays_plain_when_format_is_text() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = await request.aread()
        payload = json.loads(body)
        assert payload["msg_type"] == "text"
        assert json.loads(payload["content"]) == {"text": "plain"}
        return httpx.Response(200, json={"code": 0})

    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="webhook")
    )
    channel._token_state = _TokenState(token="tenant-token", expires_at=999999999.0)
    channel._client = httpx.AsyncClient(
        base_url="https://open.feishu.cn/open-apis",
        transport=httpx.MockTransport(handler),
    )

    try:
        await channel.send(OutgoingMessage(content="plain", reply_to="ou_user", format="text"))
    finally:
        await channel.stop()

    assert len(requests) == 1


def test_wecom_webhook_builds_markdown_payload() -> None:
    channel = WeComChannel(WeComChannelConfig(name="wecom", agent_id_int=1))
    payload = channel._build_send_payload(  # noqa: SLF001
        OutgoingMessage(
            content="**bold** text",
            reply_to="user-1",
            format="markdown",
        )
    )
    assert payload["msgtype"] == "markdown"
    assert payload["markdown"] == {"content": "**bold** text"}


def test_wecom_webhook_builds_plain_text_payload_by_default() -> None:
    channel = WeComChannel(WeComChannelConfig(name="wecom", agent_id_int=1))
    payload = channel._build_send_payload(  # noqa: SLF001
        OutgoingMessage(content="plain", reply_to="user-1")
    )
    assert payload["msgtype"] == "text"
    assert payload["text"] == {"content": "plain"}


def test_telegram_builds_html_payload_for_markdown() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="bot-token", connection_mode="webhook"))
    payload = channel._build_send_payload(  # noqa: SLF001
        OutgoingMessage(
            content="*bold* text",
            metadata={"chat_id": "12345"},
            format="markdown",
        )
    )
    assert payload["parse_mode"] == "HTML"
    assert payload["text"] == "*bold* text"


def test_telegram_builds_plain_payload_without_parse_mode() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="bot-token", connection_mode="webhook"))
    payload = channel._build_send_payload(  # noqa: SLF001
        OutgoingMessage(
            content="plain",
            metadata={"chat_id": "12345"},
        )
    )
    assert "parse_mode" not in payload


def test_runtime_reply_defaults_to_markdown_format() -> None:
    """Agent replies are rendered as markdown by default across adapters."""

    from types import SimpleNamespace

    from opensquilla.channels.qq import QQChannel, QQChannelConfig
    from opensquilla.channels.types import IncomingMessage
    from opensquilla.gateway.channel_dispatch import _build_runtime_reply_message

    channel = QQChannel(QQChannelConfig(name="qq", app_id="a", app_secret="s"))
    inbound = IncomingMessage(
        sender_id="openid-1",
        channel_id="chat-1",
        content="hi",
        metadata={"chat_type": "c2c", "openid": "openid-1", "msg_id": "m-1"},
    )
    route_envelope = SimpleNamespace(
        channel_id="chat-1",
        thread_id=None,
        channel_name="qq",
        metadata={"chat_type": "c2c", "openid": "openid-1"},
    )

    reply = _build_runtime_reply_message(
        channel,
        "**bold** answer",
        inbound,
        route_envelope,
    )

    assert reply.format == "markdown"
    assert reply.content == "**bold** answer"


@pytest.mark.asyncio
async def test_qq_markdown_uses_official_markdown_message() -> None:
    """QQ markdown replies use msg_type=2 + markdown.content, no top-level content."""

    from types import SimpleNamespace

    from opensquilla.channels.qq import QQChannelConfig

    captured: dict[str, object] = {}

    class FakeHttp:
        async def request(self, route: object, json: dict | None = None) -> dict:
            captured["route"] = route
            captured["json"] = json
            return {"id": "mock-msg-id"}

    channel = QQChannel(QQChannelConfig(name="qq", app_id="a", app_secret="s"))
    channel.api = SimpleNamespace(_http=FakeHttp())
    await channel.send(
        OutgoingMessage(
            content="**bold** list",
            format="markdown",
            metadata={"chat_type": "c2c", "openid": "openid-1", "msg_id": "m-1"},
        )
    )
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["msg_type"] == 2
    assert "content" not in body  # QQ rejects top-level content (even null)
    assert body["markdown"]["content"] == "**bold** list"
    assert body["openid"] == "openid-1"
    assert body["msg_id"] == "m-1"


@pytest.mark.asyncio
async def test_telegram_markdown_parse_failure_falls_back_to_plain_text() -> None:
    """Entity-parse failures fall back to plain text instead of dropping."""

    channel = TelegramChannel(TelegramChannelConfig(token="bot-token", connection_mode="webhook"))
    sent: list[dict] = []

    async def fake_api(method: str, payload: dict | None = None) -> Any:
        sent.append(dict(payload or {}))
        if method == "sendMessage" and payload.get("parse_mode") == "HTML":
            raise TelegramApiError(
                "Bad Request: can't parse entities: character at offset 3",
                error_code=400,
            )
        return {"ok": True, "result": {"message_id": 1}}

    channel._api = fake_api  # type: ignore[method-assign]
    await channel.send(
        OutgoingMessage(
            content="unescaped _underscore_",
            format="markdown",
            metadata={"chat_id": "12345"},
        )
    )

    assert len(sent) == 2
    assert sent[0]["parse_mode"] == "HTML"
    assert sent[0]["text"].startswith("unescaped <i>")
    assert "parse_mode" not in sent[1]
    # The fallback delivers the original markdown chunk, not rendered HTML.
    assert sent[1]["text"] == "unescaped _underscore_"


@pytest.mark.asyncio
async def test_telegram_markdown_non_parse_error_is_not_masked() -> None:
    """Errors unrelated to entity parsing must surface, not fall back."""

    channel = TelegramChannel(TelegramChannelConfig(token="bot-token", connection_mode="webhook"))

    async def fake_api(method: str, payload: dict | None = None) -> Any:
        raise TelegramApiError("Bad Request: chat not found", error_code=400)

    channel._api = fake_api  # type: ignore[method-assign]
    with pytest.raises(TelegramApiError, match="chat not found"):
        await channel.send(
            OutgoingMessage(
                content="hello **world**",
                format="markdown",
                metadata={"chat_id": "12345"},
            )
        )


def test_telegram_markdown_renders_common_constructs() -> None:
    from opensquilla.channels._markdown import markdown_to_telegram_html

    assert (
        markdown_to_telegram_html("**bold** and *italic* and `code`")
        == "<b>bold</b> and <i>italic</i> and <code>code</code>"
    )
    assert (
        markdown_to_telegram_html("[docs](https://example.com?a=1&b=2)")
        == '<a href="https://example.com?a=1&amp;b=2">docs</a>'
    )


def test_telegram_markdown_escapes_text_safely() -> None:
    from opensquilla.channels._markdown import markdown_to_telegram_html

    assert markdown_to_telegram_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"
    # Markers inside code spans are literal, not markup.
    assert markdown_to_telegram_html("`**not bold**`") == "<code>**not bold**</code>"


def test_telegram_markdown_renders_nested_and_block_constructs() -> None:
    from opensquilla.channels._markdown import markdown_to_telegram_html

    assert markdown_to_telegram_html("*a **b** c*") == "<i>a <b>b</b> c</i>"
    assert (
        markdown_to_telegram_html("```python\nprint('hi')\n```")
        == "<pre><code class=\"language-python\">print('hi')</code></pre>"
    )
    # Headings render as plain bold-free lines: Telegram rejects nested <b>.
    assert "Title" in markdown_to_telegram_html("# Title\n- one\n- two\n1. three")
    assert "\u2022 one" in markdown_to_telegram_html("# Title\n- one\n- two\n1. three")
    assert markdown_to_telegram_html("> quoted text") == "<blockquote>quoted text</blockquote>"
    # Intraword underscores are not emphasis (snake_case stays literal).
    assert markdown_to_telegram_html("snake_case_name") == "snake_case_name"


def test_dingtalk_reply_markdown_matches_real_sdk_signature() -> None:
    """The adapter must call reply_markdown with the SDK's parameter shape."""

    import inspect

    from dingtalk_stream import ChatbotHandler

    params = list(inspect.signature(ChatbotHandler.reply_markdown).parameters)
    assert params == ["self", "title", "text", "incoming_message"]


@pytest.mark.asyncio
async def test_dingtalk_markdown_reply_passes_all_sdk_arguments() -> None:
    """A markdown reply calls reply_markdown(title, text, incoming_message)."""

    from types import SimpleNamespace

    channel = DingTalkChannel(DingTalkChannelConfig(name="dingtalk"))
    raw = SimpleNamespace(
        message_id="msg-md",
        message_type="text",
        text=SimpleNamespace(content="hi"),
        sender_staff_id="staff-md",
        sender_nick="staff-md",
        conversation_id="conv-md",
        conversation_type="1",
        session_webhook="https://example.invalid/hook-md",
    )
    incoming = channel.parse_message(raw)
    assert incoming is not None
    channel._last_incoming = raw

    calls: list[tuple[Any, ...]] = []
    channel._handler = SimpleNamespace(
        reply_text=lambda *args: calls.append(("text", *args)),
        reply_markdown=lambda *args: calls.append(("markdown", *args)),
    )
    reply = channel.build_reply_message("**bold** answer", incoming)
    reply.format = "markdown"
    await channel.send(reply)

    # The title is derived from the first line (DingTalk requires a
    # non-empty markdown title), never an empty string.
    assert calls == [("markdown", "bold answer", "**bold** answer", raw)]


def _feishu_mock_channel(requests: list[tuple[str, bytes]]) -> FeishuChannel:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        requests.append((request.url.path, body))
        payload = json.loads(body)
        post = json.loads(payload["content"])["zh_cn"]
        assert post["content"][0][0]["tag"] == "md"
        assert len(body) < 30 * 1024
        return httpx.Response(200, json={"code": 0})

    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="webhook")
    )
    channel._token_state = _TokenState(token="tenant-token", expires_at=999999999.0)
    channel._client = httpx.AsyncClient(
        base_url="https://open.feishu.cn/open-apis",
        transport=httpx.MockTransport(handler),
    )
    return channel


@pytest.mark.asyncio
async def test_feishu_markdown_over_post_budget_splits_into_multiple_posts() -> None:
    requests: list[tuple[str, bytes]] = []
    channel = _feishu_mock_channel(requests)
    content = "**bold** text " * 2200  # ~29 KB of UTF-8, over the post budget

    try:
        await channel.send(OutgoingMessage(content=content, reply_to="ou_user", format="markdown"))
    finally:
        await channel.stop()

    assert len(requests) >= 2
    joined = "".join(
        json.loads(json.loads(body)["content"])["zh_cn"]["content"][0][0]["text"]
        for _path, body in requests
    )
    assert joined == content


@pytest.mark.asyncio
async def test_feishu_markdown_under_post_budget_stays_single_post() -> None:
    requests: list[tuple[str, bytes]] = []
    channel = _feishu_mock_channel(requests)

    try:
        await channel.send(
            OutgoingMessage(
                content="**bold** text " * 1000,  # ~13 KB, under the budget
                reply_to="ou_user",
                format="markdown",
            )
        )
    finally:
        await channel.stop()

    assert len(requests) == 1


@pytest.mark.asyncio
async def test_feishu_markdown_cjk_stays_under_post_cap_on_the_wire() -> None:
    requests: list[tuple[str, bytes]] = []
    channel = _feishu_mock_channel(requests)
    content = "内容" * 9900  # ~29.7 KB of UTF-8, over the post budget

    try:
        await channel.send(OutgoingMessage(content=content, reply_to="ou_user", format="markdown"))
    finally:
        await channel.stop()

    assert len(requests) >= 2
    assert all(len(body) < 30 * 1024 for _path, body in requests)


@pytest.mark.asyncio
async def test_feishu_reply_markdown_splits_over_budget() -> None:
    requests: list[tuple[str, bytes]] = []
    channel = _feishu_mock_channel(requests)
    content = "reply text " * 3000  # ~33 KB, over the post budget

    try:
        await channel.send(
            OutgoingMessage(
                content=content,
                metadata={"reply_message_id": "msg-1"},
                format="markdown",
            )
        )
    finally:
        await channel.stop()

    assert len(requests) >= 2
    assert all(path.endswith("/messages/msg-1/reply") for path, _body in requests)


@pytest.mark.asyncio
async def test_telegram_markdown_render_overflow_falls_back_to_plain_text() -> None:
    """A chunk whose HTML rendering exceeds 4096 units ships as plain text."""

    channel = TelegramChannel(TelegramChannelConfig(token="bot-token", connection_mode="webhook"))
    sent: list[dict] = []

    async def fake_api(method: str, payload: dict | None = None) -> Any:
        sent.append(dict(payload or {}))
        return {"ok": True, "result": {"message_id": 1}}

    channel._api = fake_api  # type: ignore[method-assign]
    ampersands = "&" * 3000  # renders to 15000 UTF-16 units, over the cap
    await channel.send(
        OutgoingMessage(
            content=ampersands,
            format="markdown",
            metadata={"chat_id": "12345"},
        )
    )

    assert len(sent) == 1
    assert "parse_mode" not in sent[0]
    assert sent[0]["text"] == ampersands


def test_telegram_markdown_double_underscore_bold_renders() -> None:
    from opensquilla.channels._markdown import markdown_to_telegram_html

    assert markdown_to_telegram_html("__bold__ text") == "<b>bold</b> text"
    # Intraword underscores are still not emphasis.
    assert markdown_to_telegram_html("keep_snake_case") == "keep_snake_case"


def test_feishu_markdown_chunks_serialized_size_always_fits() -> None:
    """JSON escaping must not push any post chunk past Feishu's 30 KB cap."""

    from opensquilla.channels.feishu import (
        _feishu_markdown_body_size,
        _feishu_markdown_chunks,
    )

    for content in (
        '"' * 20000,  # every char needs JSON escaping
        "\\" * 12000,
        "\n" * 15000,
        '\t\r"\\\\ ' * 4000,
    ):
        chunks = _feishu_markdown_chunks(content)
        assert chunks
        assert all(_feishu_markdown_body_size(chunk) <= 30 * 1024 for chunk in chunks)


@pytest.mark.asyncio
async def test_feishu_markdown_split_chunks_use_unique_delivery_keys() -> None:
    """Split posts must not share an idempotency key or Feishu dedupes them."""

    uuids: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        uuids.append(str(request.url.params.get("uuid", "")))
        await request.aread()
        return httpx.Response(200, json={"code": 0})

    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="webhook")
    )
    channel._token_state = _TokenState(token="tenant-token", expires_at=999999999.0)
    channel._client = httpx.AsyncClient(
        base_url="https://open.feishu.cn/open-apis",
        transport=httpx.MockTransport(handler),
    )

    try:
        await channel.send(
            OutgoingMessage(
                content="**bold** text " * 3000,
                metadata={"delivery_id": "delivery-1"},
                reply_to="ou_user",
                format="markdown",
            )
        )
    finally:
        await channel.stop()

    assert len(uuids) >= 2
    assert len(set(uuids)) == len(uuids)


def test_feishu_markdown_chunks_preserve_content_across_splits() -> None:
    from opensquilla.channels.feishu import _feishu_markdown_chunks

    content = "内容 *" + "*" * 6000 + "* 内容"
    chunks = _feishu_markdown_chunks(content)
    assert "".join(chunks) == content


def test_telegram_markdown_renders_fence_without_language() -> None:
    from opensquilla.channels._markdown import markdown_to_telegram_html

    assert markdown_to_telegram_html("```\ncode\n```") == "<pre><code>code</code></pre>"
    assert markdown_to_telegram_html("# Heading with **bold**") == "Heading with <b>bold</b>"


def test_telegram_markdown_unbalanced_markers_escape_literally() -> None:
    from opensquilla.channels._markdown import markdown_to_telegram_html

    # An unmatched opener must not swallow the rest of the message.
    assert markdown_to_telegram_html("**bold never closed") == "**bold never closed"
    assert markdown_to_telegram_html("a *b* c * d") == "a <i>b</i> c * d"


def test_slack_markdown_converts_common_constructs() -> None:
    from opensquilla.channels._markdown import markdown_to_slack_mrkdwn

    assert (
        markdown_to_slack_mrkdwn("**bold** and *italic* and `code`")
        == "*bold* and _italic_ and `code`"
    )
    assert (
        markdown_to_slack_mrkdwn("[docs](https://example.com?a=1&b=2)")
        == "<https://example.com?a=1&amp;b=2|docs>"
    )
    assert markdown_to_slack_mrkdwn("~~gone~~") == "~gone~"


def test_slack_markdown_blocks_and_code_fences() -> None:
    from opensquilla.channels._markdown import markdown_to_slack_mrkdwn

    result = markdown_to_slack_mrkdwn("# Title\n- one\n- two\n1. three\n```\n**raw**\n```")
    assert result.startswith("*Title*\n")
    assert "\u2022 one" in result
    assert "1. three" in result
    assert "```\n**raw**\n```" in result  # code fence untouched
    assert markdown_to_slack_mrkdwn("`**not bold**`") == "`**not bold**`"


def test_slack_markdown_nested_and_unbalanced() -> None:
    from opensquilla.channels._markdown import markdown_to_slack_mrkdwn

    assert markdown_to_slack_mrkdwn("*a **b** c*") == "_a *b* c_"
    assert markdown_to_slack_mrkdwn("**never closed") == "**never closed"


@pytest.mark.asyncio
async def test_slack_send_converts_markdown_to_mrkdwn() -> None:
    from opensquilla.channels.slack import SlackChannel

    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads((await request.aread()).decode())
        requests.append(body)
        return httpx.Response(200, json={"ok": True, "ts": "123"})

    channel = SlackChannel(token="bot-token", slack_channel_id="C123")
    channel._client = httpx.AsyncClient(
        base_url="https://slack.com/api",
        transport=httpx.MockTransport(handler),
    )

    try:
        await channel.send(
            OutgoingMessage(
                content="**bold** reply",
                reply_to="C123",
                format="markdown",
            )
        )
    finally:
        await channel.stop()

    assert requests == [{"channel": "C123", "text": "*bold* reply"}]


def test_dingtalk_markdown_title_is_never_empty() -> None:
    from opensquilla.channels.dingtalk import _dingtalk_markdown_title

    assert _dingtalk_markdown_title("**bold** answer", "dingtalk") == "bold answer"
    assert _dingtalk_markdown_title("# Heading line\nbody", "dingtalk") == "Heading line"
    assert _dingtalk_markdown_title("", "dingtalk") == "dingtalk"
    # A code-fence first line strips to empty, so the fallback name is used.
    assert _dingtalk_markdown_title("```\ncode\n```", "dingtalk") == "dingtalk"
    assert len(_dingtalk_markdown_title("x" * 100, "dingtalk")) <= 20


def test_telegram_markdown_link_with_surrounding_text() -> None:
    from opensquilla.channels._markdown import markdown_to_telegram_html

    assert (
        markdown_to_telegram_html("Visit [Google](https://google.com) for details")
        == 'Visit <a href="https://google.com">Google</a> for details'
    )
    assert (
        markdown_to_telegram_html("Check [one](https://1.com) and [two](https://2.com) here")
        == 'Check <a href="https://1.com">one</a> and <a href="https://2.com">two</a> here'
    )


def test_telegram_markdown_multiline_blockquote_preserves_lines() -> None:
    from opensquilla.channels._markdown import markdown_to_telegram_html

    assert (
        markdown_to_telegram_html("> line one\n> line two with **bold**")
        == "<blockquote>line one\nline two with <b>bold</b></blockquote>"
    )


@pytest.mark.asyncio
async def test_qq_send_streaming_emits_markdown_passive_message() -> None:
    from collections.abc import AsyncIterator
    from types import SimpleNamespace

    from opensquilla.channels.qq import QQChannel, QQChannelConfig

    captured: dict[str, object] = {}

    class FakeHttp:
        async def request(self, route: object, json: dict | None = None) -> dict:
            captured["route"] = route
            captured["json"] = json
            return {"id": "mock-msg-id"}

    channel = QQChannel(QQChannelConfig(name="qq", app_id="a", app_secret="s"))
    channel.api = SimpleNamespace(_http=FakeHttp())

    async def chunks() -> AsyncIterator[str]:
        yield "# Title\n"
        yield "**bold** answer"

    await channel.send_streaming(
        chunks(),
        chat_type="c2c",
        target="openid-1",
        msg_id="m-1",
    )

    body = captured.get("json")
    assert isinstance(body, dict)
    assert body["msg_type"] == 2
    assert "content" not in body
    assert body["markdown"]["content"] == "# Title\n**bold** answer"


@pytest.mark.asyncio
async def test_feishu_send_streaming_emits_markdown_post() -> None:
    from collections.abc import AsyncIterator

    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = await request.aread()
        payload = json.loads(body)
        assert payload["msg_type"] == "post"
        post = json.loads(payload["content"])["zh_cn"]
        assert post["content"][0][0]["tag"] == "md"
        assert post["content"][0][0]["text"] == "**streamed** bold"
        return httpx.Response(200, json={"code": 0})

    channel = FeishuChannel(
        FeishuChannelConfig(app_id="app", app_secret="secret", connection_mode="webhook")
    )
    channel._token_state = _TokenState(token="tenant-token", expires_at=999999999.0)
    channel._client = httpx.AsyncClient(
        base_url="https://open.feishu.cn/open-apis",
        transport=httpx.MockTransport(handler),
    )

    async def chunks() -> AsyncIterator[str]:
        yield "**streamed** "
        yield "bold"

    try:
        await channel.send_streaming(chunks(), chat_id="ou_user")
    finally:
        await channel.stop()

    assert len(requests) == 1


@pytest.mark.asyncio
async def test_wecom_send_streaming_emits_markdown() -> None:
    from collections.abc import AsyncIterator

    channel = WeComChannel(WeComChannelConfig(name="wecom", agent_id_int=1))
    sent_messages: list[OutgoingMessage] = []

    async def fake_send(msg: OutgoingMessage) -> None:
        sent_messages.append(msg)

    channel.send = fake_send  # type: ignore[method-assign]

    async def chunks() -> AsyncIterator[str]:
        yield "**streamed**"

    await channel.send_streaming(chunks(), reply_to="user-1")

    assert len(sent_messages) == 1
    assert sent_messages[0].format == "markdown"
    assert sent_messages[0].content == "**streamed**"


@pytest.mark.asyncio
async def test_slack_send_streaming_converts_to_mrkdwn() -> None:
    from collections.abc import AsyncIterator

    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads((await request.aread()).decode())
        requests.append(body)
        return httpx.Response(200, json={"ok": True, "ts": "123"})

    channel = SlackChannel(token="bot-token", slack_channel_id="C123")
    channel._client = httpx.AsyncClient(
        base_url="https://slack.com/api",
        transport=httpx.MockTransport(handler),
    )

    async def chunks() -> AsyncIterator[str]:
        yield "**streamed** answer"

    try:
        await channel.send_streaming(chunks(), channel="C123")
    finally:
        await channel.stop()

    assert requests
    assert requests[0]["text"] == "*streamed* answer"
