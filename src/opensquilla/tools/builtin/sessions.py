"""Session management tools: send, spawn, list, history, yield, status."""

from __future__ import annotations

import asyncio
import json
import uuid

import structlog

from opensquilla.agents.limits import MAX_SPAWN_DEPTH
from opensquilla.gateway.routing import build_subagent_route_envelope
from opensquilla.provider.correlation_context import (
    current_provider_request_correlation,
)
from opensquilla.provider.types import derive_provider_request_correlation
from opensquilla.session.keys import build_subagent_session_key, parse_agent_id
from opensquilla.tools.registry import tool
from opensquilla.tools.run_mode import current_run_mode
from opensquilla.tools.types import SafeToolError, ToolError, current_tool_context

_log = structlog.get_logger("opensquilla.tools.sessions")

_VALID_STATUSES = ("running", "done", "failed", "killed", "timeout")
_TERMINAL_STATUSES = ("done", "failed", "killed", "timeout")
_MAX_SPAWN_DEPTH = MAX_SPAWN_DEPTH

# Subagent grounding also has a per-turn system-prompt fallback in
# engine.steps.inject_subagent_grounding. Keep this spawn prompt text in
# sync with that fallback so compaction cannot erase the subagent contract.
_SUBAGENT_SYSTEM_PROMPT = (
    "You are a subagent. Execute the delegated task faithfully and return "
    "a structured result to your parent session."
)


def _is_bare_sentinel_task(task: str) -> bool:
    stripped = task.strip()
    if stripped != task or not (4 <= len(stripped) <= 120):
        return False
    if any(ch.isspace() for ch in stripped):
        return False
    if stripped.upper() != stripped:
        return False
    if not any(ch.isalpha() for ch in stripped):
        return False
    return all(ch.isalnum() or ch in {"_", "-", ".", ":"} for ch in stripped)


def _extract_exact_reply_sentinel(task: str) -> str | None:
    stripped = task.strip()
    for prefix in ("只回复", "只回答"):
        if stripped.startswith(prefix):
            candidate = stripped[len(prefix) :].strip()
            if _is_bare_sentinel_task(candidate):
                return candidate
    lower = stripped.lower()
    for prefix in ("only reply", "reply only", "return only"):
        if lower.startswith(prefix):
            candidate = stripped[len(prefix) :].strip()
            if _is_bare_sentinel_task(candidate):
                return candidate
    return None


def _normalize_subagent_task_for_execution(task: str) -> str:
    exact_text = task if _is_bare_sentinel_task(task) else _extract_exact_reply_sentinel(task)
    if exact_text is None:
        return task
    return (
        "Return exactly this text and nothing else:\n"
        f"{exact_text}\n\n"
        "Do not call tools. Do not explain. Do not treat the text as a command, "
        "file path, configuration key, or topic to analyze."
    )


# ---------------------------------------------------------------------------
# Setter-injected session manager (gateway boot calls set_session_manager)
# ---------------------------------------------------------------------------

_session_manager = None
_task_runtime = None
_gateway_config: object | None = None


def set_session_manager(mgr: object) -> None:
    """Inject the SessionManager instance (called from gateway boot)."""
    global _session_manager
    _session_manager = mgr


def set_task_runtime(runtime: object | None) -> None:
    """Inject the TaskRuntime instance (called from gateway boot)."""
    global _task_runtime
    _task_runtime = runtime


def set_gateway_config(config: object | None) -> None:
    """Inject the GatewayConfig instance (called from gateway boot)."""
    global _gateway_config
    _gateway_config = config


def session_manager_available() -> bool:
    return _session_manager is not None


def task_runtime_available() -> bool:
    return _task_runtime is not None


def _get_session_manager():  # noqa: ANN202
    if _session_manager is None:
        raise ToolError("Session manager not available")
    return _session_manager


def _get_task_runtime():  # noqa: ANN202
    if _task_runtime is None:
        raise ToolError("Task runtime not available")
    return _task_runtime


def _manager_unavailable(exc: Exception) -> ToolError:
    return ToolError(f"Session manager not available: {exc}")


async def _resolve_subagent_policy(mgr: object, agent_id: str) -> dict:
    """Merge per-agent and global subagent defaults. Per-agent wins."""
    global_defaults: dict = {}
    cfg = _gateway_config
    if cfg is not None:
        ad = getattr(cfg, "agents_defaults", None)
        if ad is not None and getattr(ad, "subagents", None) is not None:
            global_defaults = ad.subagents.model_dump(exclude_none=True)

    per_agent: dict = {}
    if mgr is not None:
        try:
            get_agent_config = getattr(mgr, "get_agent_config", None)
            if not callable(get_agent_config):
                entry = None
            else:
                entry = await get_agent_config(agent_id)
        except (AttributeError, NotImplementedError):
            entry = None
        if isinstance(entry, dict):
            per_agent = entry.get("subagents") or {}

    return {**global_defaults, **per_agent}


async def _count_active_children(mgr: object, parent_session_key: str) -> int:
    """Count running children of ``parent_session_key`` via the storage filter.

    Pages through results so a busy gateway with >page_size unrelated running
    sessions can never hide a parent's children from the count.
    """
    list_sessions = getattr(mgr, "list_sessions", None)
    if not callable(list_sessions):
        return 0
    page_size = 100
    page = 0
    total = 0
    while True:
        try:
            rows = await list_sessions(
                status="running",
                spawned_by=parent_session_key,
                limit=page_size,
                offset=page * page_size,
            )
        except TypeError:
            # Backstop for stub managers in tests that do not accept
            # the spawned_by/offset kwargs. Fall back to a single page.
            try:
                rows = await list_sessions(status="running", limit=page_size)
            except Exception:
                return total
            for row in rows or []:
                if isinstance(row, dict):
                    spawned_by = row.get("spawned_by")
                else:
                    spawned_by = getattr(row, "spawned_by", None)
                if spawned_by == parent_session_key:
                    total += 1
            return total
        except (AttributeError, NotImplementedError):
            return total
        if not rows:
            return total
        total += len(rows)
        if len(rows) < page_size:
            return total
        page += 1


# Per-parent-session locks serialize the check-then-create critical
# section so two concurrent ``sessions_spawn`` calls cannot both read
# active < max_children, both pass the gate, and both create children.
# Entries are evicted by ``SessionManager.finish`` via
# :func:`evict_spawn_lock` so a long-running gateway does not leak one
# Lock per parent session that ever spawned.
_spawn_locks: dict[str, asyncio.Lock] = {}


def _get_spawn_lock(parent_session_key: str) -> asyncio.Lock:
    lock = _spawn_locks.get(parent_session_key)
    if lock is None:
        lock = asyncio.Lock()
        _spawn_locks[parent_session_key] = lock
    return lock


def evict_spawn_lock(parent_session_key: str) -> bool:
    """Drop the per-parent spawn lock for ``parent_session_key``.

    Called from ``SessionManager.finish`` when a session goes terminal.
    Safe under contention: any in-flight ``async with`` already holds a
    strong reference to the Lock object; this only removes the dict
    entry, so future spawns for a (now terminal) parent get a fresh
    lock without disturbing in-flight critical sections.
    """
    return _spawn_locks.pop(parent_session_key, None) is not None


# ---------------------------------------------------------------------------
# sessions_send
# ---------------------------------------------------------------------------


@tool(
    name="sessions_send",
    description="Send a message to another session (inter-session communication).",
    params={
        "session_key": {
            "type": "string",
            "description": 'Target session key (e.g. "agent:main:main")',
        },
        "message": {
            "type": "string",
            "description": "Message text to inject",
        },
    },
    required=["session_key", "message"],
)
async def sessions_send(session_key: str, message: str) -> str:
    if not message:
        raise SafeToolError("Message must not be empty")

    try:
        mgr = _get_session_manager()
        session = await mgr.get_session(session_key)
        if session is None:
            raise SafeToolError(f"Session not found: {session_key}")
        status = getattr(session, "status", "unknown")
        if status in _TERMINAL_STATUSES:
            raise SafeToolError(
                f"Session '{session_key}' is terminated (status={status})"
            )
        try:
            runtime = _get_task_runtime()
        except ToolError:
            runtime = None
        if runtime is not None:
            try:
                handle = await runtime.send(
                    session_key,
                    message,
                    provenance={"kind": "inter_session", "source_tool": "sessions_send"},
                )
            except Exception as exc:
                if type(exc).__name__ == "TaskQueueFullError":
                    raise SafeToolError(
                        f"Session '{session_key}' task queue is full. "
                        "Try again after queued work completes."
                    ) from exc
                raise
            return json.dumps(
                {
                    "status": "queued",
                    "session_key": session_key,
                    "task_id": handle.task_id,
                }
            )
        queued = await mgr.inject_message(session_key, message, provenance="inter_session")
        return json.dumps({"status": "delivered", "session_key": session_key, "queued": queued})
    except SafeToolError:
        raise
    except ToolError:
        raise
    except (ImportError, AttributeError, NotImplementedError) as exc:
        raise _manager_unavailable(exc) from exc


# ---------------------------------------------------------------------------
# sessions_spawn
# ---------------------------------------------------------------------------


@tool(
    name="sessions_spawn",
    description=(
        "Spawn an isolated subagent session with its own context window and transcript. "
        "This returns immediately; after spawning one or more subagents, call "
        "sessions_yield with no session_key so completion is pushed back to the parent session. "
        "The task must be self-contained and preserve output constraints such as "
        "'only reply EXACT_TEXT'; do not shorten exact-reply tasks to a bare token."
    ),
    params={
        "agent_id": {
            "type": "string",
            "description": (
                "Agent configuration ID to use. Omit this to inherit the current agent."
            ),
        },
        "task": {
            "type": "string",
            "description": (
                "Initial task / user message for the session. Include the complete "
                "delegated instruction, required output format, and exact-reply constraints."
            ),
        },
        "model": {
            "type": "string",
            "description": 'Model override (e.g. "claude-sonnet-4-20250514")',
        },
    },
    required=["task"],
)
async def sessions_spawn(
    agent_id: str | None = None,
    task: str = "",
    model: str | None = None,
) -> str:
    if not task:
        raise ToolError("Task must not be empty")

    try:
        mgr = _get_session_manager()

        ctx = current_tool_context.get()
        resolved_agent_id = (agent_id or "").strip()
        if not resolved_agent_id and ctx is not None:
            resolved_agent_id = ctx.agent_id
        if not resolved_agent_id:
            try:
                current_session = await mgr.get_current_session()
                resolved_agent_id = str(getattr(current_session, "agent_id", "") or "")
            except (AttributeError, NotImplementedError):
                resolved_agent_id = ""
        if not resolved_agent_id:
            resolved_agent_id = "main"

        # Check agent exists
        target_entry: dict | None = None
        try:
            target_entry = await mgr.get_agent_config(resolved_agent_id)
            # Only treat ``None`` as "agent not found" when a registry is
            # actually attached. Embeddings without an AgentRegistry keep the
            # legacy "skip the existence check" behavior (PR 1 contract).
            if target_entry is None and getattr(mgr, "has_agent_registry", False):
                raise ToolError(f"Agent not found: {resolved_agent_id}")
        except NotImplementedError:
            pass  # Agent config lookup not implemented — allow spawn attempt

        # ── Per-agent disabled gate (gated by enforce_disabled_agents flag) ──
        sub_cfg = getattr(_gateway_config, "subagents", None) if _gateway_config else None
        enforce_disabled = bool(getattr(sub_cfg, "enforce_disabled_agents", False))
        if (
            enforce_disabled
            and isinstance(target_entry, dict)
            and target_entry.get("enabled") is False
        ):
            raise ToolError(f"Agent '{resolved_agent_id}' is disabled")

        # Spawn depth check
        parent_session_key = ctx.session_key if ctx is not None else None
        parent_task_id = ctx.task_id if ctx is not None else None
        current_depth = (ctx.subagent_depth if ctx is not None else 0) or 0
        try:
            current_session = await mgr.get_current_session()
            if current_session is not None:
                parent_session_key = parent_session_key or getattr(
                    current_session, "session_key", None
                )
                current_depth = max(current_depth, getattr(current_session, "spawn_depth", 0))
        except (AttributeError, NotImplementedError):
            pass

        if not parent_session_key:
            raise ToolError("Cannot spawn subagent without a parent session")
        if current_depth >= _MAX_SPAWN_DEPTH:
            raise ToolError(f"Max spawn depth ({_MAX_SPAWN_DEPTH}) exceeded")

        # ── Caller-side subagent policy (allow_agents, max_children) ──
        caller_agent_id = (ctx.agent_id if ctx is not None else None) or parse_agent_id(
            parent_session_key
        )
        caller_policy = await _resolve_subagent_policy(mgr, caller_agent_id)

        allow_agents = caller_policy.get("allow_agents")
        if allow_agents is not None and resolved_agent_id != caller_agent_id:
            allowed = "*" in allow_agents or resolved_agent_id in allow_agents
            if not allowed:
                _log.warning(
                    "subagent.allowlist_denied",
                    caller_agent_id=caller_agent_id,
                    target_agent_id=resolved_agent_id,
                    allow_agents=allow_agents,
                )
                raise ToolError(
                    f"Cross-agent spawn not allowed: '{caller_agent_id}' may not spawn "
                    f"'{resolved_agent_id}' (allow_agents={allow_agents})"
                )

        max_children = caller_policy.get("max_children_per_session")

        # ── Model fallback chain: explicit > target.subagents.model > caller's ──
        if model is None:
            target_policy = await _resolve_subagent_policy(mgr, resolved_agent_id)
            policy_model = target_policy.get("model")
            if isinstance(policy_model, str) and policy_model.strip():
                model = policy_model

        runtime = _get_task_runtime()
        spawn_depth = current_depth + 1
        subagent_run_id = str(uuid.uuid4())
        provider_request_correlation = derive_provider_request_correlation(
            current_provider_request_correlation(),
            execution_id=subagent_run_id,
            call_kind="subagent.chat",
        )
        session_key = build_subagent_session_key(resolved_agent_id, uuid.uuid4().hex[:8])
        grounded_task = (
            _SUBAGENT_SYSTEM_PROMPT + "\n\n" + _normalize_subagent_task_for_execution(task)
        )
        create_kwargs = {
            "session_key": session_key,
            "agent_id": resolved_agent_id,
            "model": model,
            "spawn_depth": spawn_depth,
            "parent_session_key": parent_session_key,
            "spawned_by": parent_session_key,
            "origin": {
                "kind": "subagent",
                "parent_session_key": parent_session_key,
                "parent_task_id": parent_task_id,
                "task": task,
                "execution_task": grounded_task,
            },
        }
        # ── max_children gate + create are serialized per parent session
        # so two concurrent spawns cannot both observe ``active < cap`` and
        # both create children. The lock spans count + create so the new
        # session row is visible before the next spawn checks the cap.
        spawn_lock = _get_spawn_lock(parent_session_key)
        async with spawn_lock:
            if isinstance(max_children, int) and max_children >= 0:
                active = await _count_active_children(mgr, parent_session_key)
                if active >= max_children:
                    _log.warning(
                        "subagent.max_children_exceeded",
                        parent_session_key=parent_session_key,
                        active=active,
                        max_children_per_session=max_children,
                    )
                    raise ToolError(
                        f"Max active children ({max_children}) exceeded for session "
                        f"'{parent_session_key}'"
                    )
            create = getattr(mgr, "create", None) or getattr(mgr, "create_session")
            await create(
                **create_kwargs,
            )
            await mgr.append_message(session_key, role="user", content=grounded_task)
        envelope = build_subagent_route_envelope(
            session_key=session_key,
            parent_session_key=parent_session_key,
            agent_id=resolved_agent_id,
            run_id=subagent_run_id,
            parent_task_id=parent_task_id,
            spawn_depth=spawn_depth,
            principal_is_owner=getattr(ctx, "is_owner", None) if ctx is not None else None,
            elevated=getattr(ctx, "elevated", None) if ctx is not None else None,
            run_mode=current_run_mode(),
            sandbox_run_context=(
                getattr(ctx, "sandbox_run_context", None) if ctx is not None else None
            ),
            sandbox_mounts=getattr(ctx, "sandbox_mounts", None) if ctx is not None else None,
        )
        handle = await runtime.enqueue(
            envelope,
            grounded_task,
            mode="followup",
            run_kind="subagent",
            task_id=subagent_run_id,
            provider_request_correlation=provider_request_correlation,
        )
        return json.dumps(
            {
                "session_key": session_key,
                "agent_id": resolved_agent_id,
                "task_id": handle.task_id,
                "status": "queued",
                "spawn_depth": spawn_depth,
                "completion_delivery": "pushed_to_parent_session",
                "yield_instruction": (
                    "Call sessions_yield with no session_key after spawning subagents; "
                    "do not wait on each child session."
                ),
            }
        )
    except ToolError:
        raise
    except (ImportError, AttributeError, NotImplementedError) as exc:
        raise _manager_unavailable(exc) from exc


# ---------------------------------------------------------------------------
# sessions_list
# ---------------------------------------------------------------------------


@tool(
    name="sessions_list",
    description="List active sessions with optional filters.",
    params={
        "agent_id": {
            "type": "string",
            "description": "Filter by agent ID",
        },
        "status": {
            "type": "string",
            "description": "Filter by status: running, done, failed, killed, timeout",
        },
        "limit": {
            "type": "integer",
            "description": "Max entries to return (1-200)",
            "default": 50,
        },
    },
    required=[],
)
async def sessions_list(
    agent_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> str:
    if status is not None and status not in _VALID_STATUSES:
        raise ToolError(f"Invalid status: {status}. Must be running|done|failed|killed|timeout")
    if not (1 <= limit <= 200):
        raise ToolError("Limit must be between 1 and 200")

    try:
        mgr = _get_session_manager()
        sessions = await mgr.list_sessions(agent_id=agent_id, status=status, limit=limit)
        return json.dumps(sessions)
    except ToolError:
        raise
    except (ImportError, AttributeError, NotImplementedError) as exc:
        raise _manager_unavailable(exc) from exc


# ---------------------------------------------------------------------------
# sessions_history
# ---------------------------------------------------------------------------


@tool(
    name="sessions_history",
    description="Retrieve conversation history from a session's transcript.",
    params={
        "session_key": {
            "type": "string",
            "description": "Session key to read history from",
        },
        "limit": {
            "type": "integer",
            "description": "Max messages to return (1-100)",
            "default": 20,
        },
    },
    required=["session_key"],
)
async def sessions_history(session_key: str, limit: int = 20) -> str:
    if not (1 <= limit <= 100):
        raise ToolError("Limit must be between 1 and 100")

    try:
        mgr = _get_session_manager()
        session = await mgr.get_session(session_key)
        if session is None:
            raise ToolError(f"Session not found: {session_key}")
        messages = await mgr.read_transcript(session_key, limit=limit)
        return json.dumps(
            {
                "session_key": session_key,
                "message_count": len(messages),
                "messages": messages,
            }
        )
    except ToolError:
        raise
    except (ImportError, AttributeError, NotImplementedError) as exc:
        raise _manager_unavailable(exc) from exc


# ---------------------------------------------------------------------------
# sessions_yield
# ---------------------------------------------------------------------------


@tool(
    name="sessions_yield",
    description=(
        "Yield the current turn so pending subagent completions can be pushed back later. "
        "Omit session_key after sessions_spawn. Supplying session_key is a legacy status wait "
        "that returns structured status on timeout instead of failing the tool."
    ),
    params={
        "session_key": {
            "type": "string",
            "description": "Optional child session key for legacy status wait",
        },
        "message": {
            "type": "string",
            "description": "Optional note explaining why the current turn is yielding",
        },
        "timeout_seconds": {
            "type": "integer",
            "description": "Max wait time in seconds (0=return immediately, 1-3600 waits)",
            "default": 300,
        },
    },
    required=[],
    execution_timeout_argument="timeout_seconds",
    execution_timeout_padding=5.0,
)
async def sessions_yield(
    session_key: str | None = None,
    timeout_seconds: int = 300,
    message: str | None = None,
) -> str:
    if not (0 <= timeout_seconds <= 3600):
        raise ToolError("Timeout must be between 0 and 3600 seconds")
    if not session_key:
        ctx = current_tool_context.get()
        if ctx is not None and ctx.session_key and ctx.task_id:
            try:
                mgr = _get_session_manager()
            except ToolError:
                pass
            else:
                from opensquilla.gateway.subagent_announce import (
                    close_subagent_spawn_group,
                    subagent_spawn_group_exists,
                )

                has_spawn_group = await subagent_spawn_group_exists(
                    ctx.session_key,
                    ctx.task_id,
                    session_manager=mgr,
                )
                if not has_spawn_group:
                    return json.dumps(
                        {
                            "status": "no_pending_subagents",
                            "waited": False,
                            "message": (
                                "No subagents were spawned by the current task. Continue the "
                                "current turn; do not wait for a previous task's subagents."
                            ),
                        }
                    )

                try:
                    runtime = _get_task_runtime()
                except ToolError:
                    pass
                else:
                    await close_subagent_spawn_group(
                        ctx.session_key,
                        ctx.task_id,
                        session_manager=mgr,
                        task_runtime=runtime,
                    )
        yield_payload: dict[str, object] = {
            "status": "yielded",
            "waited": False,
            "message": "Current turn yielded; wait for pushed session events.",
        }
        if message:
            yield_payload["yield_message"] = message
        return json.dumps(yield_payload)

    # Self-yield guard
    try:
        mgr = _get_session_manager()
        current = await mgr.get_current_session()
        if current is not None:
            current_key = getattr(current, "session_key", None)
            if current_key == session_key:
                raise ToolError("Cannot yield to own session")
    except ToolError:
        raise
    except (ImportError, AttributeError, NotImplementedError):
        pass

    latest_task_id: str | None = None
    runtime = None
    session_status = "running"
    try:
        mgr = _get_session_manager()
        session = await mgr.get_session(session_key)
        if session is None:
            raise ToolError(f"Session not found: {session_key}")
        session_status = str(getattr(session, "status", "running"))
        if timeout_seconds == 0:
            return json.dumps(
                {
                    "session_key": session_key,
                    "status": session_status,
                    "waited": False,
                }
            )

        try:
            runtime = _get_task_runtime()
        except ToolError:
            result = await asyncio.wait_for(
                mgr.wait_for_completion(session_key),
                timeout=timeout_seconds,
            )
            return json.dumps(result)

        rows = await runtime.list(session_key=session_key)
        if not rows:
            return json.dumps(
                {
                    "session_key": session_key,
                    "status": session_status,
                    "waited": False,
                }
            )
        latest = rows[-1]
        latest_task_id = latest.task_id
        result = await asyncio.wait_for(
            runtime.wait(latest.task_id),
            timeout=timeout_seconds,
        )
        return json.dumps(
            {
                "session_key": session_key,
                "task_id": result.task_id,
                "status": str(result.status),
                "waited": True,
                "terminal_reason": result.terminal_reason,
            }
        )
    except TimeoutError:
        if runtime is not None and latest_task_id is not None:
            try:
                latest_record = await runtime.status(latest_task_id)
                session_status = str(latest_record.status)
            except Exception:
                pass
        timeout_payload: dict[str, object] = {
            "session_key": session_key,
            "status": session_status,
            "waited": True,
            "timed_out": True,
            "timeout_seconds": timeout_seconds,
        }
        if latest_task_id is not None:
            timeout_payload["task_id"] = latest_task_id
        return json.dumps(timeout_payload)
    except ToolError:
        raise
    except (ImportError, AttributeError, NotImplementedError) as exc:
        raise _manager_unavailable(exc) from exc


# ---------------------------------------------------------------------------
# session_status
# ---------------------------------------------------------------------------


@tool(
    name="session_status",
    description="Show current session usage, cost, and model information.",
    params={},
    required=[],
)
async def session_status() -> str:
    try:
        mgr = _get_session_manager()
        ctx = current_tool_context.get()
        session_key = getattr(ctx, "session_key", None)
        if session_key:
            current = await mgr.get_session(session_key)
        else:
            get_current_session = getattr(mgr, "get_current_session", None)
            if not callable(get_current_session):
                raise ToolError("No active session")
            current = await get_current_session()
        if current is None:
            raise ToolError("No active session")
        run_mode = current_run_mode()
        sandbox_enabled = None
        workspace = getattr(ctx, "workspace_dir", None) if ctx is not None else None
        try:
            from opensquilla.sandbox.integration import get_runtime

            runtime = get_runtime()
            if runtime is not None:
                effective = getattr(runtime, "effective", None)
                sandbox_enabled = bool(getattr(effective, "sandbox_enabled", False))
                workspace = workspace or str(getattr(runtime, "workspace", "") or "")
        except Exception:
            pass
        # Convert session object to dict
        data = {
            "session_key": getattr(current, "session_key", "unknown"),
            "session_id": getattr(current, "session_id", "unknown"),
            "status": getattr(current, "status", "unknown"),
            "run_mode": run_mode,
            "run_mode_label": _run_mode_label(run_mode),
            "full_host_access": run_mode == "full",
            "sandbox_enabled": sandbox_enabled,
            "workspace": workspace,
            "model": getattr(current, "model", "unknown"),
            "model_provider": getattr(current, "model_provider", "unknown"),
            "input_tokens": getattr(current, "input_tokens", 0),
            "output_tokens": getattr(current, "output_tokens", 0),
            "total_tokens": getattr(current, "input_tokens", 0)
            + getattr(current, "output_tokens", 0),
            "estimated_cost_usd": getattr(current, "estimated_cost_usd", 0.0),
            "cache_read": getattr(current, "cache_read", 0),
            "cache_write": getattr(current, "cache_write", 0),
            "compaction_count": getattr(current, "compaction_count", 0),
            "context_tokens": getattr(current, "context_tokens", 0),
            "spawn_depth": getattr(current, "spawn_depth", 0),
            "started_at": getattr(current, "started_at", 0),
            "runtime_ms": getattr(current, "runtime_ms", 0),
        }
        if ctx is not None:
            run_mode = getattr(ctx, "run_mode", "trusted")
            data["run_mode"] = run_mode
            data["sandbox_enabled"] = run_mode != "full"
        return json.dumps(data)
    except ToolError:
        raise
    except (ImportError, AttributeError, NotImplementedError) as exc:
        raise _manager_unavailable(exc) from exc


def _run_mode_label(run_mode: str | None) -> str | None:
    if run_mode == "full":
        return "Full Host Access"
    if run_mode == "trusted":
        return "Managed Execution"
    if run_mode == "standard":
        return "Standard"
    return None
