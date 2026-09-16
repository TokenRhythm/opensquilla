"""Shared memory-status projection for Gateway read surfaces."""

from __future__ import annotations

from typing import Any

from opensquilla.gateway.memory_health import memory_health_from_durable_ledger
from opensquilla.session.keys import normalize_agent_id


async def read_memory_status(
    params: dict[str, Any] | None,
    *,
    memory_backend: Any = None,
    memory_managers: dict[str, Any] | None = None,
    session_manager: Any = None,
) -> dict[str, Any]:
    """Read one agent's memory health without depending on an RPC context."""
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    params = params or {}
    deep = bool(params.get("deep", False))
    agent_id = normalize_agent_id(str(params.get("agentId") or "main"))
    manager = (memory_managers or {}).get(agent_id)
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
                session_manager,
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
            session_manager,
            agent_id=agent_id,
        )
    )
    if deep:
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
            }
        )
    return payload


__all__ = ["read_memory_status"]
