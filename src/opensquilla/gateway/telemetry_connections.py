"""Bounded connection identity registry for Gateway telemetry dimensions."""

from __future__ import annotations

from collections import deque
from typing import Any

from pydantic import ValidationError

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.protocol import ClientInfo

_LIMIT = 1024
_order: deque[str] = deque()
_ids: set[str] = set()


def is_registered_tui_connection(conn_id: str) -> bool:
    return conn_id in _ids


def register_tui_connection(conn_id: str) -> None:
    if conn_id in _ids:
        return
    _ids.add(conn_id)
    _order.append(conn_id)
    while len(_order) > _LIMIT:
        _ids.discard(_order.popleft())


def register_tui_handshake(conn_id: str, client: Any, principal: Principal) -> None:
    """Classify an owner TUI before telemetry or user input can arrive."""

    if not principal.is_owner or principal.auth_state != "authenticated":
        return
    try:
        info = ClientInfo.model_validate(client, strict=True)
    except ValidationError:
        return
    if info.id == "opensquilla-cli" and info.mode == "tui":
        register_tui_connection(conn_id)


def unregister_tui_connection(conn_id: str) -> None:
    if conn_id not in _ids:
        return
    _ids.discard(conn_id)
    _order.remove(conn_id)
