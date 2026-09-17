from __future__ import annotations

import json

import pytest
from starlette.websockets import WebSocketState

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.protocol import ResFrame
from opensquilla.gateway.websocket import ConnectionRegistry, WsConnection
from opensquilla.sandbox.legacy_codec import encode_payload_for_protocol


class _Socket:
    client_state = WebSocketState.CONNECTED

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


def _connection(protocol: int) -> tuple[WsConnection, _Socket]:
    socket = _Socket()
    connection = WsConnection(
        conn_id=f"p{protocol}",
        ws=socket,  # type: ignore[arg-type]
        protocol=protocol,
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.read"}),
            is_owner=False,
            authenticated=True,
            capabilities=frozenset({"host.execute"}),
        ),
    )
    return connection, socket


@pytest.mark.asyncio
async def test_broadcast_encodes_mode_per_connection() -> None:
    registry = ConnectionRegistry()
    legacy, legacy_socket = _connection(3)
    canonical, canonical_socket = _connection(4)
    registry.register(legacy)
    registry.register(canonical)

    await registry.broadcast(
        "sandbox.mode.changed",
        {"runMode": "safe", "nested": {"effectiveMode": "safe"}},
    )

    legacy_payload = json.loads(legacy_socket.sent[-1])["payload"]
    canonical_payload = json.loads(canonical_socket.sent[-1])["payload"]
    assert legacy_payload["runMode"] == "trusted"
    assert legacy_payload["nested"]["effectiveMode"] == "trusted"
    assert canonical_payload["runMode"] == "safe"
    assert canonical_payload["nested"]["effectiveMode"] == "safe"


def test_codec_encodes_allowed_modes_without_mutating_input() -> None:
    payload = {
        "runModePolicy": {
            "allowedRunModes": ["safe", "full"],
            "defaultRunMode": "safe",
        }
    }

    encoded = encode_payload_for_protocol(payload, protocol=3)

    assert encoded["runModePolicy"]["allowedRunModes"] == ["trusted", "full"]
    assert encoded["runModePolicy"]["defaultRunMode"] == "trusted"
    assert payload["runModePolicy"]["allowedRunModes"] == ["safe", "full"]


@pytest.mark.asyncio
async def test_rpc_response_is_encoded_for_legacy_connection() -> None:
    connection, socket = _connection(3)

    await connection.send_res(
        ResFrame(
            id="request",
            ok=True,
            payload={"runMode": "safe"},
        )
    )

    assert json.loads(socket.sent[-1])["payload"]["runMode"] == "trusted"
