"""System/messaging domain RPC handlers (Tier 2)."""

from __future__ import annotations

from typing import Any, NoReturn

from opensquilla.contracts.generated.v4.sessions_list_metadata import (
    SESSIONS_LIST_METHOD,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.config_persistence import persist_gateway_config
from opensquilla.gateway.memory_status_runtime import read_memory_status
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, RpcUnavailableError, get_dispatcher
from opensquilla.session.keys import normalize_agent_id

_d = get_dispatcher()

_AGENT_WAIT_SUPPORTED_PARAMS = [
    "agentId",
    "agent_id",
    "sessionKey",
    "session_key",
    "timeoutMs",
    "timeout_ms",
]
_AGENT_WAIT_AVAILABLE_METHODS = [
    "agents.list",
    "agents.files.list",
    SESSIONS_LIST_METHOD,
    "sessions.get",
    "tools.catalog",
]


def _raise_unavailable(method: str) -> NoReturn:
    raise RpcUnavailableError(f"{method} is not available in this build")


@_d.method("wake", scope="operator.write")
async def _handle_wake(params: dict | None, ctx: RpcContext) -> None:
    if not isinstance(params, dict) or "text" not in params:
        raise ValueError("params.text is required")
    _raise_unavailable("wake")


@_d.method("send", scope="operator.write")
async def _handle_send(params: dict | None, ctx: RpcContext) -> None:
    if not isinstance(params, dict):
        raise ValueError("params required: text, sessionKey")
    if "text" not in params:
        raise ValueError("params.text is required")
    if "sessionKey" not in params:
        raise ValueError("params.sessionKey is required")
    _raise_unavailable("send")


@_d.method("agent", scope="operator.write")
async def _handle_agent(params: dict | None, ctx: RpcContext) -> None:
    if not isinstance(params, dict) or "message" not in params:
        raise ValueError("params.message is required")
    _raise_unavailable("agent")


@_d.method("agent.wait", scope="operator.write")
async def _handle_agent_wait(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    agent_id = params.get("agentId", params.get("agent_id"))
    session_key = params.get("sessionKey", params.get("session_key"))
    timeout_ms = params.get("timeoutMs", params.get("timeout_ms"))
    if agent_id is None and session_key is None:
        raise ValueError("params.agentId or params.sessionKey is required")

    accepted_params: dict[str, Any] = {}
    if agent_id is not None:
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("params.agentId must be a non-empty string")
        accepted_params["agentId"] = normalize_agent_id(agent_id)
    if session_key is not None:
        if not isinstance(session_key, str) or not session_key.strip():
            raise ValueError("params.sessionKey must be a non-empty string")
        accepted_params["sessionKey"] = session_key.strip()
    if timeout_ms is not None:
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 0:
            raise ValueError("params.timeoutMs must be a non-negative integer")
        accepted_params["timeoutMs"] = timeout_ms

    raise RpcHandlerError(
        "agent.unavailable",
        "agent.wait parameters are accepted, but no agent runtime bridge is available.",
        details={
            "reason": "runtime_bridge_unavailable",
            "acceptedParams": accepted_params,
            "supportedParams": _AGENT_WAIT_SUPPORTED_PARAMS,
            "availableRpcMethods": _AGENT_WAIT_AVAILABLE_METHODS,
        },
        retryable=False,
    )


@_d.method("system-presence", scope="operator.read")
async def _handle_system_presence(params: dict | None, ctx: RpcContext) -> None:
    if not isinstance(params, dict) or "status" not in params:
        raise ValueError("params.status is required")
    _raise_unavailable("system-presence")


@_d.method("system-event", scope="operator.admin")
async def _handle_system_event(params: dict | None, ctx: RpcContext) -> None:
    if not isinstance(params, dict) or "text" not in params:
        raise ValueError("params.text is required")
    _raise_unavailable("system-event")


@_d.method("set-heartbeats", scope="operator.admin")
async def _handle_set_heartbeats(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    should_persist = ctx.config is not None
    if ctx.config is None:
        ctx.config = GatewayConfig()
    if not hasattr(ctx.config, "heartbeat"):
        raise ValueError("No heartbeat config available")

    # Validate and mutate a clone; the live config is only touched after
    # every parameter validated AND the persist succeeded, so a mid-way
    # ValueError or a failed write can never leave live state diverged
    # from disk (or a half-applied heartbeat section in memory).
    candidate = (
        ctx.config.model_copy(deep=True)
        if hasattr(ctx.config, "model_copy")
        else ctx.config
    )
    heartbeat = candidate.heartbeat

    if "enabled" in params:
        enabled = params["enabled"]
        if not isinstance(enabled, bool):
            raise ValueError("params.enabled must be a boolean")
        heartbeat.enabled = enabled

    if "intervalMs" in params:
        interval_ms = params["intervalMs"]
        if isinstance(interval_ms, bool) or not isinstance(interval_ms, int) or interval_ms <= 0:
            raise ValueError("params.intervalMs must be a positive integer")
        heartbeat.interval_ms = interval_ms

    if "target" in params:
        target = params["target"]
        if not isinstance(target, str) or not target.strip():
            raise ValueError("params.target must be a non-empty string")
        heartbeat.target = target.strip()

    if "to" in params:
        to = params["to"]
        if to is not None and not isinstance(to, str):
            raise ValueError("params.to must be a string or null")
        heartbeat.to = to or ""

    if "accountId" in params:
        account_id = params["accountId"]
        if account_id is not None and not isinstance(account_id, str):
            raise ValueError("params.accountId must be a string or null")
        heartbeat.account_id = account_id or ""

    if "threadId" in params:
        thread_id = params["threadId"]
        if thread_id is not None and not isinstance(thread_id, str):
            raise ValueError("params.threadId must be a string or null")
        heartbeat.thread_id = thread_id or ""

    if "prompt" in params:
        prompt = params["prompt"]
        if prompt is not None and not isinstance(prompt, str):
            raise ValueError("params.prompt must be a string or null")
        heartbeat.prompt = prompt

    if "ackMaxChars" in params:
        ack_max_chars = params["ackMaxChars"]
        if (
            isinstance(ack_max_chars, bool)
            or not isinstance(ack_max_chars, int)
            or ack_max_chars < 0
        ):
            raise ValueError("params.ackMaxChars must be a non-negative integer")
        heartbeat.ack_max_chars = ack_max_chars

    if "lightContext" in params:
        light_context = params["lightContext"]
        if not isinstance(light_context, bool):
            raise ValueError("params.lightContext must be a boolean")
        heartbeat.light_context = light_context

    if should_persist:
        persist_gateway_config(candidate)
    if candidate is not ctx.config:
        ctx.config.heartbeat = heartbeat
        if hasattr(ctx.config, "inherit_persist_provenance"):
            ctx.config.inherit_persist_provenance(candidate)

    heartbeat_loop = getattr(ctx, "heartbeat_loop", None)
    if heartbeat_loop is not None and hasattr(heartbeat_loop, "nudge"):
        heartbeat_loop.nudge()

    return {
        "enabled": heartbeat.enabled,
        "intervalMs": heartbeat.interval_ms,
        "target": heartbeat.target,
        "to": heartbeat.to,
        "accountId": heartbeat.account_id,
        "threadId": heartbeat.thread_id,
        "prompt": heartbeat.prompt,
        "ackMaxChars": heartbeat.ack_max_chars,
        "lightContext": heartbeat.light_context,
    }


@_d.method("doctor.memory.status", scope="operator.read")
async def _handle_doctor_memory_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    return await read_memory_status(
        params,
        memory_backend=getattr(ctx, "memory_backend", None),
        memory_managers=getattr(ctx, "memory_managers", None),
        session_manager=getattr(ctx, "session_manager", None),
    )
