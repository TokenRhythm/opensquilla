from __future__ import annotations

import asyncio
import json

import pytest

from opensquilla.cli.tui.opentui.transport import (
    HOST_PROTOCOL_VERSION,
    HostConnection,
)


async def _connect_and_auth(
    connection: HostConnection,
    *,
    token: str,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    env = connection.environment
    reader, writer = await asyncio.open_connection(
        env["OPENSQUILLA_OPENTUI_IPC_HOST"],
        int(env["OPENSQUILLA_OPENTUI_IPC_PORT"]),
    )
    writer.write(
        (
            json.dumps({"type": "auth", "token": token, "protocol": HOST_PROTOCOL_VERSION}) + "\n"
        ).encode()
    )
    await writer.drain()
    return reader, writer


@pytest.mark.asyncio
async def test_loopback_transport_rejects_bad_token_then_accepts_one_client() -> None:
    connection = HostConnection()
    await connection.listen()
    token = connection.environment["OPENSQUILLA_OPENTUI_IPC_TOKEN"]

    bad_reader, bad_writer = await _connect_and_auth(connection, token="wrong")
    bad_response = json.loads(await bad_reader.readline())
    assert bad_response["type"] == "auth.error"
    bad_writer.close()
    await bad_writer.wait_closed()

    reader, writer = await _connect_and_auth(connection, token=token)
    auth_response = json.loads(await reader.readline())
    assert auth_response == {"type": "auth.ok", "protocol": HOST_PROTOCOL_VERSION}
    await connection.wait_for_client(timeout=1.0)

    await connection.send_frame('{"type":"shutdown"}\n')
    assert await reader.readline() == b'{"type":"shutdown"}\n'
    writer.write(b'{"type":"ready"}\n')
    await writer.drain()
    assert await connection.readline() == '{"type":"ready"}\n'
    await connection.close()


@pytest.mark.asyncio
async def test_loopback_transport_rejects_non_object_auth_json() -> None:
    connection = HostConnection()
    await connection.listen()
    env = connection.environment
    reader, writer = await asyncio.open_connection(
        env["OPENSQUILLA_OPENTUI_IPC_HOST"],
        int(env["OPENSQUILLA_OPENTUI_IPC_PORT"]),
    )

    try:
        writer.write(b"[]\n")
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), timeout=1.0))
        assert response["type"] == "auth.error"
        assert await asyncio.wait_for(reader.readline(), timeout=1.0) == b""
    finally:
        writer.close()
        await writer.wait_closed()
        await connection.close()


@pytest.mark.asyncio
async def test_loopback_listener_is_closed_after_authenticated_client() -> None:
    connection = HostConnection()
    await connection.listen()
    env = dict(connection.environment)
    reader, writer = await _connect_and_auth(
        connection,
        token=env["OPENSQUILLA_OPENTUI_IPC_TOKEN"],
    )
    assert json.loads(await reader.readline())["type"] == "auth.ok"
    await connection.wait_for_client(timeout=1.0)

    with pytest.raises(OSError):
        await asyncio.open_connection(
            env["OPENSQUILLA_OPENTUI_IPC_HOST"],
            int(env["OPENSQUILLA_OPENTUI_IPC_PORT"]),
        )

    writer.close()
    await writer.wait_closed()
    await connection.close()


@pytest.mark.asyncio
async def test_readline_replaces_invalid_utf8_before_json_validation() -> None:
    connection = HostConnection()
    reader = asyncio.StreamReader()
    reader.feed_data(b"\xff\xfe invalid utf-8 bytes\n")
    reader.feed_eof()
    connection._reader = reader

    assert await connection.readline() == "\ufffd\ufffd invalid utf-8 bytes\n"


@pytest.mark.asyncio
async def test_send_frame_backslash_escapes_lone_surrogates() -> None:
    written: list[bytes] = []

    class _Writer:
        def write(self, data: bytes) -> None:
            written.append(data)

        async def drain(self) -> None:
            return None

    connection = HostConnection()
    connection._writer = _Writer()

    await connection.send_frame('{"text":"file_\udc80.txt"}\n')

    assert written == [b'{"text":"file_\\udc80.txt"}\n']
