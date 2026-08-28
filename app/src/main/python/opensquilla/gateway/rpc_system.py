"""System/messaging domain RPC handlers (Tier 2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, RpcUnavailableError, get_dispatcher
from opensquilla.gateway.rpc_memory import memory_health_from_durable_ledger
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
    "sessions.list",
    "sessions.get",
    "tools.catalog",
]


def _raise_unavailable(method: str) -> NoReturn:
    raise RpcUnavailableError(f"{method} is not available in this build")


def _repair_summary_wire(summary: Any) -> dict[str, Any]:
    return {
        "summaryId": getattr(summary, "id", None),
        "sessionKey": getattr(summary, "session_key", ""),
        "compactionId": getattr(summary, "compaction_id", None),
        "flushReceiptStatus": getattr(summary, "flush_receipt_status", "unknown"),
        "removedCount": int(getattr(summary, "removed_count", 0) or 0),
        "coveredThroughId": getattr(summary, "covered_through_id", None),
        "createdAt": getattr(summary, "created_at", None),
    }


def _raw_fallback_rows_for_manager(manager: Any) -> list[dict[str, Any]]:
    root = getattr(manager, "workspace_dir", None) or getattr(manager, "memory_dir", None)
    if root is None:
        return []
    raw_root = Path(root) / "memory" / ".raw_fallbacks"
    if not raw_root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for file_path in sorted(path for path in raw_root.glob("*.md") if path.is_file()):
        try:
            stat = file_path.stat()
        except OSError:
            continue
        rows.append(
            {
                "path": (Path("memory") / ".raw_fallbacks" / file_path.name).as_posix(),
                "sizeBytes": stat.st_size,
            }
        )
    return rows


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

    from opensquilla.gateway.rpc_config import _persist_config

    if should_persist:
        _persist_config(candidate)
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
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    params = params or {}
    deep = bool(params.get("deep", False))
    agent_id = normalize_agent_id(str(params.get("agentId") or "main"))
    memory_backend = getattr(ctx, "memory_backend", None)
    manager = (getattr(ctx, "memory_managers", None) or {}).get(agent_id)
    if memory_backend is None and manager is None:
        unavailable_payload: dict[str, Any] = {
            "backend": "none",
            "status": "unavailable",
            "entryCount": None,
            "sizeBytes": None,
            "error": "No memory backend configured",
        }
        unavailable_payload.update(
            await memory_health_from_durable_ledger(
                getattr(ctx, "session_manager", None),
                agent_id=agent_id,
            )
        )
        return unavailable_payload
    health: dict[str, Any] = {}
    try:
        if memory_backend is not None:
            health_call = getattr(memory_backend, "health", None)
            if not callable(health_call):
                health_call = getattr(memory_backend, "health_check", None)
            if callable(health_call):
                health = await health_call()
    except Exception as exc:
        health = {
            "backend": "unknown",
            "status": "error",
            "entryCount": None,
            "sizeBytes": None,
            "error": str(exc),
        }

    manager_status: dict[str, Any] = {}
    if manager is not None and callable(getattr(manager, "status", None)):
        try:
            manager_status = await manager.status()
        except Exception:
            manager_status = {
                "degraded": [
                    {
                        "component": "manager",
                        "operation": "status",
                        "error": "redacted",
                    }
                ]
            }

    degraded_rows: list[dict[str, str]] = []
    for row in manager_status.get("degraded") or []:
        if not isinstance(row, dict):
            continue
        degraded_rows.append(
            {
                "component": str(row.get("component") or ""),
                "operation": str(row.get("operation") or ""),
                "error": "redacted" if row.get("error") else "",
            }
        )

    backend_error = health.get("error")
    status_value = health.get("status", "ok")
    if degraded_rows and status_value == "ok":
        status_value = "degraded"

    payload: dict[str, Any] = {
        "backend": health.get("backend", "sqlite" if manager is not None else "unknown"),
        "status": status_value,
        "entryCount": health.get("entryCount", manager_status.get("chunk_count")),
        "sizeBytes": health.get("sizeBytes", manager_status.get("total_size_bytes")),
        "error": "redacted" if backend_error else None,
        "agentId": agent_id,
        "vecAvailable": bool(manager_status.get("vec_available", False)),
        "ftsAvailable": bool(manager_status.get("fts_available", False)),
        "sourceCounts": manager_status.get("source_counts", {}),
        "degraded": degraded_rows,
    }
    payload.update(
        await memory_health_from_durable_ledger(
            getattr(ctx, "session_manager", None),
            agent_id=agent_id,
        )
    )
    if deep:
        repair_rows: list[Any] = []
        repair_failures: list[dict[str, Any]] = []
        session_manager = getattr(ctx, "session_manager", None)
        list_degraded = getattr(session_manager, "list_degraded_compactions", None)
        if callable(list_degraded):
            try:
                repair_rows = await list_degraded(agent_id=agent_id, limit=50)
                repair_failures = [
                    _repair_summary_wire(row)
                    for row in repair_rows
                    if str(getattr(row, "flush_receipt_status", ""))
                    in {"failed_retryable", "quarantined"}
                ]
            except Exception:
                repair_rows = []
                repair_failures = []

        raw_rows = _raw_fallback_rows_for_manager(manager) if manager is not None else []
        payload.update(
            {
                "fileCount": manager_status.get("file_count"),
                "chunkCount": manager_status.get("chunk_count"),
                "totalSizeBytes": manager_status.get("total_size_bytes"),
                "memorySource": manager_status.get("memory_source"),
                "retrievalMode": manager_status.get("retrieval_mode"),
                "configuredRetrievalMode": manager_status.get("configured_retrieval_mode"),
                "embeddingRequestedProvider": manager_status.get(
                    "embedding_requested_provider"
                ),
                "embeddingEffectiveProvider": manager_status.get("embedding_effective_provider"),
                "embeddingModel": manager_status.get("embedding_model"),
                "vectorWeight": manager_status.get("vector_weight"),
                "textWeight": manager_status.get("text_weight"),
                "pendingRepairCount": len(repair_rows),
                "recentPreimages": [_repair_summary_wire(row) for row in repair_rows[:5]],
                "repairFailures": repair_failures[:5],
                "rawFallbackCount": len(raw_rows),
                "recentRawFallbacks": raw_rows[-5:],
            }
        )
    return payload
