from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.channels._attachment_io import (
    attachment_limit_for_mime,
    ensure_declared_size_within_limit,
    preferred_attachment_mime,
)
from opensquilla.channels.discord import DiscordChannel, DiscordChannelConfig
from opensquilla.channels.matrix import MatrixChannel, MatrixChannelConfig
from opensquilla.channels.slack import SlackChannel, _slack_event_attachments
from opensquilla.channels.telegram import TelegramChannel, TelegramChannelConfig
from opensquilla.channels.types import Attachment
from opensquilla.contracts.attachments import (
    EMAIL_ATTACHMENT_BYTES,
    MAX_STAGED_TEXT_BYTES,
    OPAQUE_ATTACHMENT_BYTES,
)
from opensquilla.gateway.attachment_ingest import (
    IMAGE_ATTACHMENT_BYTES,
    MAX_ATTACHMENT_BYTES,
    MAX_STAGED_PDF_BYTES,
)


def test_generic_download_content_type_preserves_declared_allowed_mime() -> None:
    assert preferred_attachment_mime("application/octet-stream", "text/plain") == "text/plain"
    assert preferred_attachment_mime("text/plain", "application/pdf") == "text/plain"


def test_channel_attachment_limit_uses_declared_mime_policy() -> None:
    # Channel downloads feed the staged ingest path, so text uses the staged
    # text ceiling rather than the 2MB inline cap.
    assert attachment_limit_for_mime("text/plain") == MAX_STAGED_TEXT_BYTES
    assert attachment_limit_for_mime("image/png") == IMAGE_ATTACHMENT_BYTES
    assert attachment_limit_for_mime("application/pdf") == MAX_STAGED_PDF_BYTES
    assert attachment_limit_for_mime(None) == MAX_ATTACHMENT_BYTES
    # Opaque types (archives, voice notes, video) download up to the staged
    # opaque ceiling instead of the old 5MiB unknown-type cap; email keeps the
    # inline text cap because it is never stageable.
    assert attachment_limit_for_mime("application/zip") == OPAQUE_ATTACHMENT_BYTES
    assert attachment_limit_for_mime("audio/ogg") == OPAQUE_ATTACHMENT_BYTES
    assert attachment_limit_for_mime("message/rfc822") == EMAIL_ATTACHMENT_BYTES

    ensure_declared_size_within_limit(
        6 * 1024 * 1024,
        name="report.pdf",
        limit=attachment_limit_for_mime("application/pdf"),
    )
    with pytest.raises(ValueError, match="exceeds"):
        ensure_declared_size_within_limit(
            MAX_STAGED_TEXT_BYTES + 1,
            name="large.txt",
            limit=attachment_limit_for_mime("text/plain"),
        )


def test_telegram_document_maps_to_attachment_metadata() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="t"))

    msg = channel.parse_incoming(
        {
            "message": {
                "message_id": 1,
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 456},
                "caption": "read",
                "document": {
                    "file_id": "file-1",
                    "file_name": "report.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 12,
                },
            }
        }
    )

    assert len(msg.attachments) == 1
    att = msg.attachments[0]
    assert att.name == "report.pdf"
    assert att.mime_type == "application/pdf"
    assert att.size == 12
    assert att.metadata["telegram_file_id"] == "file-1"


def test_telegram_photo_uses_largest_photo_file_id() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="t"))

    msg = channel.parse_incoming(
        {
            "message": {
                "message_id": 1,
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 456},
                "photo": [
                    {"file_id": "small", "file_unique_id": "s", "width": 10, "height": 10},
                    {"file_id": "large", "file_unique_id": "l", "width": 100, "height": 100},
                ],
            }
        }
    )

    assert msg.content == "[photo]"
    assert len(msg.attachments) == 1
    att = msg.attachments[0]
    assert att.mime_type == "image/jpeg"
    assert att.metadata["telegram_file_id"] == "large"


@pytest.mark.asyncio
async def test_matrix_media_event_creates_attachment_with_mxc_url() -> None:
    channel = MatrixChannel(MatrixChannelConfig(user_id="@bot:example.test"))
    channel._bot_user_id = "@bot:example.test"
    room = SimpleNamespace(room_id="!room:example.test", member_count=2)
    event = SimpleNamespace(
        event_id="$event",
        sender="@user:example.test",
        body="report.pdf",
        url="mxc://example.test/media",
        source={
            "content": {
                "msgtype": "m.file",
                "info": {"mimetype": "application/pdf", "size": 12},
            }
        },
    )

    await channel._on_room_message_media(room, event)
    msg = await channel.receive()

    assert msg.attachments == [
        Attachment(
            name="report.pdf",
            mime_type="application/pdf",
            url="mxc://example.test/media",
            size=12,
            metadata={"matrix_mxc_url": "mxc://example.test/media", "matrix_media_kind": "file"},
        )
    ]


@pytest.mark.asyncio
async def test_matrix_resolve_inbound_attachment_downloads_bytes() -> None:
    class FakeBody:
        async def iter_chunked(self, chunk_size: int):
            yield b"%PDF-1.4\n"

    class FakeResponse:
        status = 200
        headers = {"content-type": "application/pdf"}
        content = FakeBody()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    class FakeSession:
        def get(self, url: str, **kwargs):
            assert url == "https://matrix.example.test/_matrix/media"
            return FakeResponse()

    class FakeClient:
        ssl = None
        client_session = FakeSession()

        def mxc_to_http(self, mxc_url: str):
            assert mxc_url == "mxc://example.test/media"
            return "https://matrix.example.test/_matrix/media"

    channel = MatrixChannel(MatrixChannelConfig(user_id="@bot:example.test"))
    channel._client = FakeClient()

    resolved = await channel.resolve_inbound_attachment(
        Attachment(
            name="report.pdf",
            mime_type="application/pdf",
            url="mxc://example.test/media",
            metadata={"matrix_mxc_url": "mxc://example.test/media"},
        )
    )

    assert resolved.data == b"%PDF-1.4\n"
    assert resolved.mime_type == "application/pdf"


@pytest.mark.asyncio
async def test_matrix_resolve_inbound_attachment_fails_closed_without_streaming() -> None:
    class FakeClient:
        async def download(self, mxc_url: str):
            raise AssertionError("unbounded Matrix download fallback must not be called")

    channel = MatrixChannel(MatrixChannelConfig(user_id="@bot:example.test"))
    channel._client = FakeClient()

    with pytest.raises(RuntimeError, match="bounded media streaming"):
        await channel.resolve_inbound_attachment(
            Attachment(
                name="report.pdf",
                mime_type="application/pdf",
                url="mxc://example.test/media",
                metadata={"matrix_mxc_url": "mxc://example.test/media"},
            )
        )


@pytest.mark.asyncio
async def test_discord_resolve_inbound_attachment_fetches_url_bytes() -> None:
    class FakeResponse:
        headers = {"content-type": "text/plain; charset=utf-8"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        async def aiter_bytes(self):
            yield b"hello"

    class FakeClient:
        def stream(self, method: str, url: str):
            assert method == "GET"
            assert url == "https://cdn.discordapp.test/a.txt"
            return FakeResponse()

    channel = DiscordChannel(DiscordChannelConfig(token="t"))
    channel._client = FakeClient()

    resolved = await channel.resolve_inbound_attachment(
        Attachment(
            name="a.txt",
            mime_type=None,
            url="https://cdn.discordapp.test/a.txt",
            size=5,
        )
    )

    assert resolved.data == b"hello"
    assert resolved.mime_type == "text/plain"
    assert resolved.metadata["source_url"] == "https://cdn.discordapp.test/a.txt"


@pytest.mark.asyncio
async def test_discord_oversize_content_length_is_rejected_before_body_read() -> None:
    class FakeResponse:
        headers = {"content-length": str(MAX_ATTACHMENT_BYTES + 1)}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        async def aiter_bytes(self):
            raise AssertionError("oversize response body should not be read")
            yield b""

    class FakeClient:
        def stream(self, method: str, url: str):
            assert method == "GET"
            return FakeResponse()

    channel = DiscordChannel(DiscordChannelConfig(token="t"))
    channel._client = FakeClient()

    with pytest.raises(ValueError, match="exceeds"):
        await channel.resolve_inbound_attachment(
            Attachment(name="huge.bin", url="https://cdn.discordapp.test/huge.bin")
        )


@pytest.mark.asyncio
async def test_telegram_oversize_declared_attachment_skips_get_file() -> None:
    class NoApiTelegram(TelegramChannel):
        async def _api(self, method: str, payload=None):
            raise AssertionError("oversize Telegram attachment should not call getFile")

    channel = NoApiTelegram(TelegramChannelConfig(token="t"))

    with pytest.raises(ValueError, match="exceeds"):
        await channel.resolve_inbound_attachment(
            Attachment(
                name="huge.txt",
                mime_type="text/plain",
                size=MAX_STAGED_TEXT_BYTES + 1,
                metadata={"telegram_file_id": "file-1"},
            )
        )


def test_slack_event_attachments_extracts_files() -> None:
    attachments = _slack_event_attachments(
        {
            "files": [
                {
                    "name": "report.pdf",
                    "mimetype": "application/pdf",
                    "url_private": "https://files.slack.com/files/T1/report.pdf",
                    "size": 2048,
                },
                {"name": "no-url.txt", "mimetype": "text/plain"},
                "not-a-dict",
            ]
        }
    )
    assert len(attachments) == 1
    assert attachments[0].name == "report.pdf"
    assert attachments[0].mime_type == "application/pdf"
    assert attachments[0].url == "https://files.slack.com/files/T1/report.pdf"
    assert attachments[0].size == 2048


def test_slack_event_without_files_yields_no_attachments() -> None:
    assert _slack_event_attachments({"text": "hello"}) == []


@pytest.mark.asyncio
async def test_slack_resolve_inbound_attachment_downloads_with_token() -> None:
    import httpx

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer xoxb-test"
        return httpx.Response(200, content=b"pdf-bytes")

    channel = SlackChannel(token="xoxb-test", slack_channel_id="C123")
    channel._client = httpx.AsyncClient(
        base_url="https://slack.com/api",
        headers={"Authorization": "Bearer xoxb-test"},
        transport=httpx.MockTransport(handler),
    )
    resolved = await channel.resolve_inbound_attachment(
        Attachment(
            name="report.pdf",
            mime_type="application/pdf",
            url="https://files.slack.com/files/T1/report.pdf",
            size=9,
        )
    )
    assert resolved.data == b"pdf-bytes"
    assert resolved.size == 9


@pytest.mark.asyncio
async def test_matrix_oversize_declared_attachment_skips_download() -> None:
    class FakeClient:
        async def download(self, mxc_url: str):
            raise AssertionError("oversize Matrix attachment should not download")

    channel = MatrixChannel(MatrixChannelConfig(user_id="@bot:example.test"))
    channel._client = FakeClient()

    with pytest.raises(ValueError, match="exceeds"):
        await channel.resolve_inbound_attachment(
            Attachment(
                name="huge.txt",
                mime_type="text/plain",
                url="mxc://example.test/media",
                size=MAX_STAGED_TEXT_BYTES + 1,
                metadata={"matrix_mxc_url": "mxc://example.test/media"},
            )
        )


@pytest.mark.asyncio
async def test_qq_inbound_resolver_downloads_bytes(monkeypatch) -> None:
    """QQ image URL is downloaded into bytes for the shared ingest path."""

    import httpx

    from opensquilla.channels.qq import QQChannel, QQChannelConfig

    channel = QQChannel(QQChannelConfig(name="qq", app_id="a", app_secret="s"))
    attachment = Attachment(
        name="photo.png",
        mime_type="image/png",
        url="https://cdn.example.com/photo.png",
        size=4,
    )
    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, content=b"img!", headers={"content-type": "image/png"})
        )
    )
    monkeypatch.setattr("httpx.AsyncClient", lambda **_: mock_client)
    resolved = await channel.resolve_inbound_attachment(attachment)
    assert resolved.data == b"img!"
    assert resolved.mime_type == "image/png"


@pytest.mark.asyncio
async def test_wecom_inbound_resolver_downloads_bytes(monkeypatch) -> None:
    """WeCom PicUrl is downloaded into bytes with the shared size bound."""

    import httpx

    from opensquilla.channels.wecom import WeComChannel, WeComChannelConfig

    channel = WeComChannel(WeComChannelConfig(name="wecom"))
    attachment = Attachment(
        name="wecom-image",
        mime_type="image/jpeg",
        url="https://qcdn.example.com/pic.jpg",
        size=4,
    )
    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, content=b"pic!", headers={"content-type": "image/jpeg"})
        )
    )
    monkeypatch.setattr("httpx.AsyncClient", lambda **_: mock_client)
    resolved = await channel.resolve_inbound_attachment(attachment)
    assert resolved.data == b"pic!"
    assert resolved.mime_type == "image/jpeg"


@pytest.mark.asyncio
async def test_download_attachment_bytes_streams_with_size_bound(monkeypatch) -> None:
    """Shared downloader streams bytes and enforces the MIME size limit."""

    import httpx

    from opensquilla.channels._attachment_io import (
        RemoteAttachmentTooLargeError,
        download_attachment_bytes,
    )
    from opensquilla.channels.types import Attachment

    attachment = Attachment(
        name="pic.png",
        mime_type="image/png",
        url="https://cdn.example.com/pic.png",
    )
    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                content=b"x" * 4096,
                headers={"content-type": "image/png"},
            )
        )
    )
    monkeypatch.setattr("httpx.AsyncClient", lambda **_: mock_client)
    payload = await download_attachment_bytes(attachment)
    assert len(payload) == 4096

    # Oversized (declared) attachment is rejected before download.
    big = Attachment(
        name="big.png",
        mime_type="image/png",
        url="https://cdn.example.com/big.png",
        size=IMAGE_ATTACHMENT_BYTES + 1,
    )
    with pytest.raises(RemoteAttachmentTooLargeError):
        await download_attachment_bytes(big)
