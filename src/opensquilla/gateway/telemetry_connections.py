"""Bounded connection identity registry for Gateway telemetry dimensions."""

from __future__ import annotations

from collections import deque

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
