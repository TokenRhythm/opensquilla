"""Immutable usage ownership and task grouping, independent of live sessions.

These rows are accounting metadata, not another cost accumulator. Session
deletion/reset cannot cascade into them. Only exact durable generation links
are used; a reusable session key is never an accounting identity.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

USAGE_ATTRIBUTION_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS usage_attribution_owners (
        session_id TEXT NOT NULL, session_epoch INTEGER NOT NULL,
        task_group_id TEXT NOT NULL, root_session_id TEXT NOT NULL,
        root_session_epoch INTEGER NOT NULL,
        parent_session_id TEXT, parent_session_epoch INTEGER,
        session_kind TEXT NOT NULL, branch_kind TEXT NOT NULL,
        title TEXT NOT NULL, attribution_status TEXT NOT NULL,
        PRIMARY KEY (session_id, session_epoch)
    )""",
    """CREATE TABLE IF NOT EXISTS usage_attribution_runs (
        execution_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
        session_epoch INTEGER NOT NULL, task_group_id TEXT NOT NULL,
        parent_execution_id TEXT, relation TEXT NOT NULL,
        attribution_status TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_usage_attribution_runs_group
        ON usage_attribution_runs(task_group_id, execution_id)""",
) + tuple(
    f"""CREATE TRIGGER IF NOT EXISTS usage_revision_{table}_{operation.lower()}
        AFTER {operation} ON {table} BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END"""
    for table in ("usage_attribution_owners", "usage_attribution_runs")
    for operation in ("INSERT", "UPDATE", "DELETE")
)


def _value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping) or hasattr(source, "keys"):
        return source[name] if name in source.keys() else default
    return getattr(source, name, default)


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        decoded = json.loads(value or "{}")
        return decoded if isinstance(decoded, dict) else {}
    except (ValueError, TypeError):
        return {}


async def _one(conn: Any, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    async with conn.execute(sql, params) as cur:
        row = await cur.fetchone()
    return dict(row) if row is not None else None


def _group_id(session_id: str, epoch: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"opensquilla:usage-group:{session_id}:{epoch}"))


async def ensure_usage_owner_on_conn(
    conn: Any,
    session_id: str,
    session_epoch: int,
    *,
    parent_owner: tuple[str, int] | None = None,
    kind: str | None = None,
    _visited: frozenset[tuple[str, int]] = frozenset(),
) -> dict[str, Any]:
    """Freeze one generation's known lineage; missing lineage stays partial."""
    identity = (session_id, session_epoch)
    existing = await _one(
        conn,
        "SELECT * FROM usage_attribution_owners WHERE session_id=? AND session_epoch=?",
        identity,
    )
    if existing is not None:
        return existing
    node = await _one(
        conn, "SELECT * FROM sessions WHERE session_id=? AND epoch=? LIMIT 1", identity
    )
    origin = _object(_value(node, "origin"))
    side = _object(origin.get("sidechat"))
    status = "complete" if node is not None else "partial"
    resolved_kind = kind or "root"
    # Reset starts a fresh task, even when an older origin was copied forward.
    if session_epoch > 0:
        parent_owner, resolved_kind = None, "root"
    elif parent_owner is None and side:
        parent_id, parent_epoch = side.get("parentSessionId"), side.get("parentEpoch")
        resolved_kind = "sidechat"
        if isinstance(parent_id, str) and isinstance(parent_epoch, int):
            parent_owner = (parent_id, parent_epoch)
        else:
            status = "partial"
    elif parent_owner is None and origin.get("kind") == "subagent":
        resolved_kind = "subagent"
        parent_id, parent_epoch = (
            origin.get("parent_session_id"),
            origin.get("parent_session_epoch"),
        )
        if not isinstance(parent_id, str) or not isinstance(parent_epoch, int):
            task = await _one(
                conn, "SELECT details FROM agent_tasks WHERE task_id=?", (origin.get("task_id"),)
            )
            details = _object(_value(task, "details"))
            metadata = _object(details.get("metadata"))
            if (details.get("session_id"), details.get("session_epoch")) == identity:
                parent_id = metadata.get("parent_session_id")
                parent_epoch = metadata.get("parent_session_epoch")
        if isinstance(parent_id, str) and isinstance(parent_epoch, int):
            parent_owner = (parent_id, parent_epoch)
        else:
            status = "partial"
    if resolved_kind != "root" and parent_owner is None:
        status = "partial"
    parent = None
    if parent_owner is not None:
        if parent_owner in _visited or parent_owner == identity or len(_visited) >= 100:
            status = "partial"
            parent_owner = None
        else:
            parent = await ensure_usage_owner_on_conn(
                conn, *parent_owner, _visited=_visited | {identity}
            )
            if parent["attribution_status"] != "complete":
                status = "partial"
    branch = "self"
    if parent is not None:
        branch = (
            "sidechat"
            if resolved_kind == "sidechat" or parent["branch_kind"] == "sidechat"
            else "subsession"
        )
    elif status == "partial":
        branch = "unattributed"
    title = str(_value(node, "display_name") or _value(node, "derived_title") or "")[:240]
    owner = {
        "session_id": session_id,
        "session_epoch": session_epoch,
        "task_group_id": parent["task_group_id"] if parent else _group_id(*identity),
        "root_session_id": parent["root_session_id"] if parent else session_id,
        "root_session_epoch": parent["root_session_epoch"] if parent else session_epoch,
        "parent_session_id": parent_owner[0] if parent_owner else None,
        "parent_session_epoch": parent_owner[1] if parent_owner else None,
        "session_kind": resolved_kind,
        "branch_kind": branch,
        "title": title,
        "attribution_status": status,
    }
    columns = ",".join(owner)
    await conn.execute(
        f"INSERT INTO usage_attribution_owners ({columns}) VALUES ({','.join('?' for _ in owner)})",
        tuple(owner.values()),
    )
    return owner


async def ensure_usage_attribution_on_conn(conn: Any, event: Any) -> None:
    """Called in the provider reservation transaction, including exact replay."""
    run_id = str(_value(event, "execution_id", ""))
    session_id = str(_value(event, "session_id", ""))
    epoch = int(_value(event, "session_epoch", 0))
    existing = await _one(
        conn, "SELECT * FROM usage_attribution_runs WHERE execution_id=?", (run_id,)
    )
    if existing is not None:
        if (existing["session_id"], existing["session_epoch"]) != (session_id, epoch):
            from opensquilla.session.usage_ledger import UsageLedgerConflictError

            raise UsageLedgerConflictError("usage execution owner cannot change")
        return
    parent_run_id = _value(event, "parent_turn_id")
    parent_owner = None
    task = await _one(
        conn, "SELECT details FROM agent_tasks WHERE task_id=?", (_value(event, "turn_id"),)
    )
    details = _object(_value(task, "details"))
    metadata = (
        _object(details.get("metadata"))
        if (details.get("session_id"), details.get("session_epoch")) == (session_id, epoch)
        else {}
    )
    if metadata.get("parent_task_id"):
        parent_id, parent_epoch = (
            metadata.get("parent_session_id"),
            metadata.get("parent_session_epoch"),
        )
        if isinstance(parent_id, str) and isinstance(parent_epoch, int):
            parent_owner = (parent_id, parent_epoch)
        parent_run_id = metadata["parent_task_id"]
    parent_run = None
    if parent_run_id:
        parent_run = await _one(
            conn, "SELECT * FROM usage_attribution_runs WHERE execution_id=?", (parent_run_id,)
        )
    inline = _value(event, "run_kind") == "subagent" and parent_owner in {None, (session_id, epoch)}
    durable_inline_parent = False
    if inline and parent_run is None and parent_owner == (session_id, epoch):
        # A durable parent can delegate before making its own provider call.
        # Verify the captured generation instead of requiring a parent bill.
        parent_task = await _one(
            conn, "SELECT session_key,details FROM agent_tasks WHERE task_id=?", (parent_run_id,)
        )
        parent_details = _object(_value(parent_task, "details"))
        durable_inline_parent = (
            parent_task is not None
            and metadata.get("parent_session_key") == parent_task["session_key"]
            and (parent_details.get("session_id"), parent_details.get("session_epoch"))
            == (session_id, epoch)
        )
    if inline:
        parent_owner = None
    owner = await ensure_usage_owner_on_conn(
        conn,
        session_id,
        epoch,
        parent_owner=parent_owner,
        kind="subagent" if parent_owner is not None else None,
    )
    relation = owner["branch_kind"]
    status = owner["attribution_status"]
    if inline and relation == "self":
        if durable_inline_parent or (
            parent_run is not None
            and (parent_run["session_id"], parent_run["session_epoch"]) == (session_id, epoch)
        ):
            relation = "inline_subagent"
        else:
            relation, status = "unattributed", "partial"
    await conn.execute(
        "INSERT INTO usage_attribution_runs VALUES (?,?,?,?,?,?,?)",
        (run_id, session_id, epoch, owner["task_group_id"], parent_run_id, relation, status),
    )


async def backfill_usage_attribution_on_conn(conn: Any) -> None:
    """Recover metadata only, never costs or inferred session-key ancestry."""
    async with conn.execute(
        "SELECT e.* FROM usage_events e WHERE NOT EXISTS "
        "(SELECT 1 FROM usage_attribution_runs r WHERE r.execution_id=e.execution_id) "
        "ORDER BY e.started_at_ms,e.call_index,e.event_id"
    ) as cur:
        events = await cur.fetchall()
    for event in events:
        await ensure_usage_attribution_on_conn(conn, event)


async def read_usage_attributions_on_conn(
    conn: Any, execution_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for start in range(0, len(execution_ids), 400):
        batch = list(dict.fromkeys(execution_ids[start : start + 400]))
        if not batch:
            continue
        async with conn.execute(
            "SELECT o.*,root.title AS root_title,r.execution_id,r.parent_execution_id,r.relation,"
            "r.attribution_status AS run_attribution_status FROM usage_attribution_runs r "
            "JOIN usage_attribution_owners o ON o.session_id=r.session_id "
            "AND o.session_epoch=r.session_epoch "
            "LEFT JOIN usage_attribution_owners root ON root.session_id=o.root_session_id "
            "AND root.session_epoch=o.root_session_epoch "
            f"WHERE r.execution_id IN ({','.join('?' for _ in batch)})",
            batch,
        ) as cur:
            rows = await cur.fetchall()
        result.update((str(row["execution_id"]), dict(row)) for row in rows)
    return result
