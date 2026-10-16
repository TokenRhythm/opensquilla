"""Typed RPC handlers for connection-owned interactive terminal sessions."""

from __future__ import annotations

from typing import Any

from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.terminal_sessions import (
    DEFAULT_COLS,
    DEFAULT_ROWS,
    MAX_COLS,
    MAX_ROWS,
    TerminalSessionError,
    _command,
    _dimension,
    get_terminal_manager,
)
from opensquilla.gateway.terminal_targets import TerminalTargetResolver, parse_terminal_target
from opensquilla.gateway.workspace_files import WorkspaceFileUnavailableError
from opensquilla.sandbox.run_mode_policy import principal_has_host_execute

_d = get_dispatcher()


_MANAGER = get_terminal_manager()


def _params(params: dict | None) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise RpcHandlerError("INVALID_REQUEST", "params must be an object", accepted=False)
    return params


def _require_host_execute(ctx: RpcContext) -> None:
    if not principal_has_host_execute(ctx.principal):
        raise RpcHandlerError(
            "UNAUTHORIZED",
            "Interactive terminals require the host.execute capability.",
            accepted=False,
        )


def _error(exc: TerminalSessionError) -> RpcHandlerError:
    return RpcHandlerError(exc.code, exc.message, retryable=exc.retryable, accepted=False)


async def _handle_terminal_create(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    _require_host_execute(ctx)
    if (
        not isinstance(ctx.conn_id, str)
        or not ctx.conn_id
        or ctx.conn_id in {"http", "_http"}
    ):
        raise RpcHandlerError(
            "INVALID_REQUEST", "Interactive terminals require a WebSocket connection"
        )
    values = _params(params)
    try:
        target = parse_terminal_target(values)
    except ValueError as exc:
        raise RpcHandlerError("INVALID_REQUEST", str(exc), accepted=False) from exc
    cwd = values.get("cwd", "")
    if cwd is None:
        cwd = ""
    if not isinstance(cwd, str) or len(cwd) > 4096 or "\x00" in cwd:
        raise RpcHandlerError("INVALID_REQUEST", "cwd must be a bounded relative path")
    # An absolute cwd would permit the shell to start outside the authoritative
    # workspace. WorkspaceFiles._directory enforces the same boundary for all
    # relative path segments; reject absolute spellings before it is called.
    from pathlib import Path

    normalized_cwd = cwd.replace("\\", "/")
    if (
        Path(cwd).is_absolute()
        or normalized_cwd.startswith("/")
        or (len(cwd) >= 2 and cwd[1] == ":")
    ):
        raise RpcHandlerError("INVALID_REQUEST", "cwd must be relative to the workspace")
    try:
        cols = _dimension(values.get("cols"), DEFAULT_COLS, MAX_COLS, "cols")
        rows = _dimension(values.get("rows"), DEFAULT_ROWS, MAX_ROWS, "rows")
        command = values.get("command", values.get("shell"))
        if command is not None:
            command = _command(command)
        service = TerminalTargetResolver(ctx.config, ctx.session_manager, target)
        result = await _MANAGER.create(
            conn_id=ctx.conn_id,
            session_key=target.session_key,
            workspace_service=service,
            target=target.payload(),
            cwd=cwd,
            command=command,
            cols=cols,
            rows=rows,
        )
        from opensquilla.gateway.websocket import get_registry

        connection = get_registry().get(ctx.conn_id)
        if connection is None or connection.principal != ctx.principal:
            # A connection can disappear while a native PTY is being spawned.
            _MANAGER.schedule_close_connection(ctx.conn_id)
            raise RpcHandlerError("UNAUTHORIZED", "Connection is no longer active")
        if _MANAGER.claim_connection_cleanup(ctx.conn_id):
            conn_id = ctx.conn_id

            def close_connection_terminals() -> None:
                _MANAGER.on_connection_cleanup(conn_id)

            connection.add_transport_cleanup(close_connection_terminals)
    except TerminalSessionError as exc:
        raise _error(exc) from exc
    except WorkspaceFileUnavailableError as exc:
        raise RpcHandlerError(
            "WORKSPACE_UNAVAILABLE",
            "The terminal working directory is unavailable",
            accepted=False,
        ) from exc
    except (ValueError, OSError) as exc:
        raise RpcHandlerError("INVALID_REQUEST", str(exc), accepted=False) from exc
    return result


async def _handle_terminal_write(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    _require_host_execute(ctx)
    values = _params(params)
    terminal_id = values.get("terminalId")
    if not isinstance(terminal_id, str) or not terminal_id:
        raise RpcHandlerError("INVALID_REQUEST", "terminalId is required")
    data = values.get("data")
    if not isinstance(data, str) or not data:
        raise RpcHandlerError(
            "INVALID_REQUEST", "data must be a non-empty string", accepted=False
        )
    try:
        return await _MANAGER.write(
            terminal_id=terminal_id,
            conn_id=ctx.conn_id,
            data=data,
        )
    except TerminalSessionError as exc:
        raise _error(exc) from exc


async def _handle_terminal_resize(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    _require_host_execute(ctx)
    values = _params(params)
    terminal_id = values.get("terminalId")
    if not isinstance(terminal_id, str) or not terminal_id:
        raise RpcHandlerError("INVALID_REQUEST", "terminalId is required")
    try:
        cols = _dimension(values.get("cols"), DEFAULT_COLS, MAX_COLS, "cols")
        rows = _dimension(values.get("rows"), DEFAULT_ROWS, MAX_ROWS, "rows")
        return await _MANAGER.resize(
            terminal_id=terminal_id,
            conn_id=ctx.conn_id,
            cols=cols,
            rows=rows,
        )
    except TerminalSessionError as exc:
        raise _error(exc) from exc


async def _handle_terminal_close(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    _require_host_execute(ctx)
    values = _params(params)
    terminal_id = values.get("terminalId")
    if not isinstance(terminal_id, str) or not terminal_id:
        raise RpcHandlerError("INVALID_REQUEST", "terminalId is required")
    reason = values.get("reason", "client_closed")
    if not isinstance(reason, str) or not reason or len(reason) > 128:
        raise RpcHandlerError("INVALID_REQUEST", "reason must be a bounded string")
    try:
        return await _MANAGER.close(terminal_id=terminal_id, conn_id=ctx.conn_id, reason=reason)
    except TerminalSessionError as exc:
        raise _error(exc) from exc


_d.register("terminal.create", _handle_terminal_create, "operator.write")
_d.register("terminal.write", _handle_terminal_write, "operator.write")
_d.register("terminal.resize", _handle_terminal_resize, "operator.write")
_d.register("terminal.close", _handle_terminal_close, "operator.write")


__all__ = [
    "_MANAGER",
    "_handle_terminal_close",
    "_handle_terminal_create",
    "_handle_terminal_resize",
    "_handle_terminal_write",
]
