from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import httpx
import pytest

from opensquilla.tools.builtin import web
from opensquilla.tools.types import ToolError

HttpRequestCallable = Callable[..., Awaitable[str]]


def _original_http_request() -> HttpRequestCallable:
    return cast(HttpRequestCallable, web.http_request.__wrapped__.__wrapped__)


def _patch_response(monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> None:
    class FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def request(self, **kwargs: object) -> httpx.Response:
            return response

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)


@pytest.mark.asyncio
async def test_http_request_returns_body_base64_for_octet_stream_invalid_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"\xff\xfe\x00PDF"
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=raw,
            headers={"content-type": "application/octet-stream"},
            request=httpx.Request("GET", "https://example.test/file"),
        ),
    )

    payload = json.loads(await _original_http_request()(url="https://example.test/file"))

    assert payload["content_type"] == "application/octet-stream"
    assert payload["body"] is None
    assert base64.b64decode(payload["body_base64"]) == raw
    assert payload["body_base64_truncated"] is False


@pytest.mark.asyncio
async def test_http_request_returns_text_body_for_json_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=b'{"ok":true}',
            headers={"content-type": "application/json; charset=utf-8"},
            request=httpx.Request("GET", "https://example.test/data"),
        ),
    )

    payload = json.loads(await _original_http_request()(url="https://example.test/data"))

    assert payload["body"] == '{"ok":true}'
    assert base64.b64decode(payload["body_base64"]) == b'{"ok":true}'
    assert payload["body_truncated"] is False


@pytest.mark.asyncio
async def test_http_request_keeps_body_base64_for_misleading_text_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"\xff\xfe\x00PDF"
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=raw,
            headers={"content-type": "text/plain; charset=utf-8"},
            request=httpx.Request("GET", "https://example.test/mislabelled"),
        ),
    )

    payload = json.loads(await _original_http_request()(url="https://example.test/mislabelled"))

    assert payload["body"] is not None
    assert "\ufffd" in payload["body"]
    assert base64.b64decode(payload["body_base64"]) == raw


@pytest.mark.asyncio
async def test_http_request_uses_body_base64_when_content_type_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"\x00\x01\x02"
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=raw,
            request=httpx.Request("GET", "https://example.test/blob"),
        ),
    )

    payload = json.loads(await _original_http_request()(url="https://example.test/blob"))

    assert payload["content_type"] == ""
    assert payload["body"] is None
    assert base64.b64decode(payload["body_base64"]) == raw


@pytest.mark.asyncio
async def test_http_request_does_not_implicitly_save_large_binary_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw = b"x" * 1_000_001
    monkeypatch.chdir(tmp_path)
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=raw,
            headers={"content-type": "application/octet-stream"},
            request=httpx.Request("GET", "https://example.test/large"),
        ),
    )

    payload = json.loads(await _original_http_request()(url="https://example.test/large"))

    digest = hashlib.sha256(raw).hexdigest()
    saved_path = tmp_path / ".fetch" / f"{digest}.bin"
    assert payload["size"] == len(raw)
    assert payload["sha256"] == digest
    assert payload["body_saved"] is False
    assert payload["body"] is None
    assert payload["body_base64"] is not None
    assert payload["body_base64_truncated"] is True
    assert payload["path"] is None
    assert not saved_path.exists()


@pytest.mark.asyncio
async def test_http_request_does_not_implicitly_save_large_text_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw = b"<feed>" + (b"a" * 60_000) + b"</feed>"
    monkeypatch.chdir(tmp_path)
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=raw,
            headers={"content-type": "application/xml"},
            request=httpx.Request("GET", "https://example.test/feed"),
        ),
    )

    payload = json.loads(await _original_http_request()(url="https://example.test/feed"))

    digest = hashlib.sha256(raw).hexdigest()
    saved_path = tmp_path / ".fetch" / f"{digest}.bin"
    assert payload["size"] == len(raw)
    assert payload["sha256"] == digest
    assert payload["body_saved"] is False
    assert payload["body"].startswith("<feed>")
    assert len(payload["body"]) == 10_000
    assert payload["body_base64"] is not None
    assert payload["body_truncated"] is True
    assert payload["body_base64_truncated"] is False
    assert payload["path"] is None
    assert not saved_path.exists()


@pytest.mark.asyncio
async def test_http_request_output_path_saves_inside_fetch_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw = b'{"ok":true}'
    monkeypatch.chdir(tmp_path)
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=raw,
            headers={"content-type": "application/json"},
            request=httpx.Request("GET", "https://example.test/data"),
        ),
    )

    payload = json.loads(
        await _original_http_request()(
            url="https://example.test/data",
            output_path="raw.json",
        )
    )

    saved_path = tmp_path / ".fetch" / "raw.json"
    assert Path(payload["path"]) == saved_path
    assert saved_path.read_bytes() == raw
    assert payload["body_saved"] is True
    assert payload["body"] is None
    assert payload["body_base64"] is None


@pytest.mark.asyncio
async def test_http_request_output_path_rejects_existing_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw = b'{"ok":true}'
    monkeypatch.chdir(tmp_path)
    existing_path = tmp_path / ".fetch" / "raw.json"
    existing_path.parent.mkdir()
    existing_path.write_bytes(b"keep")
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=raw,
            headers={"content-type": "application/json"},
            request=httpx.Request("GET", "https://example.test/data"),
        ),
    )

    with pytest.raises(ToolError, match="output_path already exists"):
        await _original_http_request()(
            url="https://example.test/data",
            output_path="raw.json",
        )

    assert existing_path.read_bytes() == b"keep"


@pytest.mark.asyncio
async def test_http_request_output_path_rejects_fetch_directory_escape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=b"escape",
            headers={"content-type": "text/plain"},
            request=httpx.Request("GET", "https://example.test/data"),
        ),
    )

    with pytest.raises(ToolError, match="output_path must stay inside"):
        await _original_http_request()(
            url="https://example.test/data",
            output_path="../escape.txt",
        )
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.asyncio
async def test_http_request_output_path_rejects_foreign_posix_path_on_windows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.tools.builtin import web

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(web.os, "name", "nt")
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=b"foreign",
            headers={"content-type": "text/plain"},
            request=httpx.Request("GET", "https://example.test/data"),
        ),
    )

    with pytest.raises(ToolError, match="foreign_host_path"):
        await _original_http_request()(
            url="https://example.test/data",
            output_path="/Users/a1/Desktop/raw.txt",
        )

    assert not (tmp_path / "Users").exists()


@pytest.mark.asyncio
async def test_http_request_without_output_path_does_not_create_fetch_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw = b"x" * 60_000
    monkeypatch.chdir(tmp_path)
    _patch_response(
        monkeypatch,
        httpx.Response(
            200,
            content=raw,
            headers={"content-type": "application/octet-stream"},
            request=httpx.Request("GET", "https://example.test/blob"),
        ),
    )

    first = json.loads(await _original_http_request()(url="https://example.test/blob"))
    second = json.loads(await _original_http_request()(url="https://example.test/blob"))

    assert first["path"] is None
    assert second["path"] is None
    assert not (tmp_path / ".fetch").exists()
