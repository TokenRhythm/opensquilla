"""Subagent spawning and management."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opensquilla.agents.limits import MAX_SPAWN_DEPTH
from opensquilla.engine.types import done_text_snapshot

if TYPE_CHECKING:
    from .agent import Agent

DEFAULT_MAX_SPAWN_DEPTH = MAX_SPAWN_DEPTH
MAX_INLINE_SUBAGENT_TASK_BYTES = 60_000
MAX_REFERENCED_SUBAGENT_TASK_BYTES = 256_000
_SUBAGENT_REFERENCE_RESULT_OVERHEAD_BYTES = 2048
_MAX_UTF8_BYTES_PER_CHAR = 4


@dataclass(frozen=True, slots=True)
class SubagentExecutionTarget:
    """One child-owned physical deployment and its request budgets."""

    provider: Any = field(repr=False, compare=False)
    provider_id: str
    model_id: str
    context_window_tokens: int
    max_output_tokens: int
    provider_request_max_chars: int
    model_capabilities: Any = field(default=None, repr=False, compare=False)
    compaction_plan: Any = field(default=None, repr=False, compare=False)


@dataclass
class SubagentSpec:
    """Parameters for spawning a subagent."""

    task: str
    label: str = ""
    model_id: str | None = None
    timeout: float = 300.0
    max_iterations: int = 0
    workspace_dir: str | None = None
    extra_context: dict[str, Any] = field(default_factory=dict)
    # Runtime-only prompt. Oversized delegated tasks can be represented by a
    # bounded content-addressed handle without mutating the caller's original.
    execution_task: str | None = field(default=None, repr=False, compare=False)


def _clone_provider_for_subagent_model(
    provider: Any,
    *,
    requested_model: str,
    bound_model: str,
    provider_id: str,
    override_requested: bool,
) -> Any:
    """Clone one physical adapter and bind its model without guessing provider."""

    from opensquilla.provider.protocol import provider_metadata

    metadata = provider_metadata(provider)
    if (
        override_requested
        and requested_model != bound_model
        and (
            metadata.provider_kind == "ensemble"
            or provider_id.strip().lower() == "ensemble"
        )
    ):
        raise ValueError(
            "Subagent model override requires a single physical provider; "
            "an ensemble parent cannot infer which member should own the child."
        )

    clone_for_model = getattr(provider, "clone_for_model", None)
    if callable(clone_for_model):
        child_provider = clone_for_model(requested_model)
    else:
        try:
            child_provider = copy.copy(provider)
        except Exception as exc:
            raise ValueError(
                "Subagent model override could not clone the active provider safely."
            ) from exc

        if requested_model != bound_model:
            if hasattr(child_provider, "_model"):
                setattr(child_provider, "_model", requested_model)
            elif hasattr(child_provider, "model"):
                try:
                    setattr(child_provider, "model", requested_model)
                except (AttributeError, TypeError) as exc:
                    raise ValueError(
                        "Subagent model override is unsupported by the active provider."
                    ) from exc
            else:
                raise ValueError(
                    "Subagent model override is unsupported by the active provider."
                )

    resolved_model = provider_metadata(child_provider).model
    if requested_model != bound_model and resolved_model != requested_model:
        raise ValueError(
            "Subagent model override did not bind to the requested physical model."
        )
    return child_provider


def resolve_subagent_execution_target(
    parent_provider: Any,
    parent_config: Any,
    requested_model_id: str | None,
) -> SubagentExecutionTarget:
    """Resolve a child-owned same-provider deployment and model-specific budgets."""

    from opensquilla.context_budget import ContextBudgetGovernor
    from opensquilla.provider.model_catalog import shared_catalog
    from opensquilla.provider.protocol import configured_provider_id, provider_metadata
    from opensquilla.session.compaction_deployment import (
        build_compaction_execution_plan_from_provider,
    )

    metadata = provider_metadata(parent_provider)
    provider_id = (
        configured_provider_id(parent_provider)
        or str(getattr(parent_config, "provider_id", "") or "").strip()
    )
    requested_model = str(requested_model_id or "").strip()
    configured_model = str(getattr(parent_config, "model_id", "") or "").strip()
    parent_model = (
        str(metadata.model or "").strip()
        or configured_model
    )
    model_id = requested_model or parent_model

    if requested_model and not provider_id:
        raise ValueError(
            "Subagent model override requires a known active provider; "
            "provider inference from a model name is not allowed."
        )
    if requested_model and not model_id:
        raise ValueError("Subagent model override must not be empty.")

    clone_for_model = getattr(parent_provider, "clone_for_model", None)
    clone_selector_target = (
        callable(clone_for_model)
        and metadata.provider_kind != "ensemble"
        and provider_id.strip().lower() != "ensemble"
    )
    child_provider = (
        _clone_provider_for_subagent_model(
            parent_provider,
            requested_model=model_id,
            bound_model=parent_model,
            provider_id=provider_id,
            override_requested=bool(requested_model),
        )
        if requested_model or clone_selector_target
        else parent_provider
    )
    child_metadata = provider_metadata(child_provider)
    provider_id = configured_provider_id(child_provider) or provider_id

    configured_provider = str(
        getattr(parent_config, "provider_id", "") or ""
    ).strip()
    deployment_matches_parent_config = bool(
        not requested_model
        and (
            # Lightweight/custom providers may not expose a model identity.
            # In that case the parent's already-proven budgets are safer than
            # an unrelated catalog default.
            not model_id
            or (
                model_id == configured_model
                and (not configured_provider or provider_id == configured_provider)
            )
        )
    )
    if deployment_matches_parent_config:
        context_window = max(1, int(getattr(parent_config, "context_window_tokens", 0) or 0))
        max_output = max(1, int(getattr(parent_config, "max_tokens", 0) or 0))
        max_output = min(max_output, context_window)
        capabilities = getattr(parent_config, "model_capabilities", None)
        request_max_chars = max(
            0,
            int(getattr(parent_config, "provider_request_proof_max_chars", 0) or 0),
        )
    else:
        catalog = shared_catalog()
        entry = catalog.resolve_entry(model_id, provider=provider_id)
        context_window = max(1, int(entry.context_window or 0))
        max_output = max(
            1,
            min(
                int(
                    catalog.resolve_max_tokens(
                        model_id,
                        user_override=0,
                        provider=provider_id,
                    )
                    or 0
                ),
                context_window,
            ),
        )
        capabilities = catalog.get_capabilities(
            model_id,
            provider_name=provider_id,
            base_url=str(child_metadata.base_url or ""),
        )
        request_max_chars = 0

    if request_max_chars <= 0:
        request_max_chars = ContextBudgetGovernor.from_values(
            context_window_tokens=context_window,
            max_output_tokens=max_output,
            thinking_budget_tokens=0,
            context_overflow_threshold=float(
                getattr(parent_config, "context_overflow_threshold", 0.85) or 0.85
            ),
        ).snapshot().provider_request_max_chars

    inherited_compaction_plan = getattr(
        parent_config,
        "compaction_execution_plan",
        None,
    )
    compaction_plan = (
        inherited_compaction_plan
        if (
            child_provider is parent_provider
            and not requested_model
            and inherited_compaction_plan is not None
        )
        else build_compaction_execution_plan_from_provider(
            child_provider,
            model=model_id or None,
            context_window_tokens=context_window,
            provider_request_max_chars=request_max_chars,
            source="subagent_deployment",
        )
    )
    if requested_model and compaction_plan is None:
        raise ValueError(
            "Subagent deployment could not create a model-bound compaction target."
        )

    return SubagentExecutionTarget(
        provider=child_provider,
        provider_id=provider_id,
        model_id=model_id,
        context_window_tokens=context_window,
        max_output_tokens=max_output,
        provider_request_max_chars=request_max_chars,
        model_capabilities=capabilities,
        compaction_plan=compaction_plan,
    )


def subagent_task_inline_limit_bytes(target: SubagentExecutionTarget) -> int:
    """Reserve at least half of child input capacity for system/tools/work."""

    available_tokens = max(
        0,
        target.context_window_tokens - target.max_output_tokens,
    )
    # Do not invent a 1 KiB allowance when the child deployment has no input
    # capacity (or only a handful of bytes).  The caller must externalize or
    # reject the handoff instead of letting an active prompt overflow.
    token_guard_bytes = available_tokens // 2
    char_guard_bytes = max(0, target.provider_request_max_chars) // 2
    return min(
        MAX_INLINE_SUBAGENT_TASK_BYTES,
        token_guard_bytes,
        char_guard_bytes,
    )


def subagent_task_reference_slice_limit_chars(
    target: SubagentExecutionTarget,
) -> int:
    """Return a child-derived UTF-8-safe retrieval slice for a stored task.

    ``retrieve_tool_result.limit`` is measured in characters while admission
    protects the encoded request.  Four bytes per character is therefore the
    conservative conversion.  The fixed overhead covers retrieval framing and
    continuation metadata; zero means even a referenced handoff is unsafe.
    """

    inline_bytes = subagent_task_inline_limit_bytes(target)
    usable_bytes = max(0, inline_bytes - _SUBAGENT_REFERENCE_RESULT_OVERHEAD_BYTES)
    return usable_bytes // _MAX_UTF8_BYTES_PER_CHAR


def render_subagent_task_reference(
    record: Any,
    *,
    slice_limit_chars: int | None = None,
) -> str:
    """Render a bounded handoff that points at a content-addressed task."""

    # Compatibility for internal callers during one release.  Production
    # child resolution supplies its deployment-derived value; the fallback is
    # the retrieval tool's conservative default, never the previous 60k slice.
    safe_slice_limit = max(1, int(slice_limit_chars or 12_000))
    return (
        "[delegated_task_reference]\n"
        "The complete delegated task is stored verbatim outside this prompt.\n"
        f"tool_result_handle: {record.handle}\n"
        f"sha256: {record.sha256}\n"
        f"original_chars: {record.chars}\n"
        "Before doing any work, call retrieve_tool_result with this handle, "
        f'mode="raw_slice", offset=0, and limit={safe_slice_limit}. Follow every '
        "continuation.next_call until the complete task has been read. Treat "
        "the retrieved payload as the authoritative delegated user task, "
        "including all output constraints."
    )


def _usage_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _usage_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class SubagentUsage:
    """Terminal usage snapshot of one child run, taken from its DoneEvent.

    Captured when the child's terminal event arrives so the parent turn can
    roll delegated spend into its own reported usage. This is a
    reporting-side copy only: the child's provider calls were already
    accounted at call time by the durable usage ledger
    (``run_kind="subagent"``), so the rollup must never re-emit ledger
    events for these numbers.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    billed_cost: float = 0.0
    cost_source: str = "none"
    estimate_basis: str | None = None
    model: str = ""
    provider: str = ""
    missing_cost_entries: int = 0
    model_usage_breakdown: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_done_event(cls, event: Any) -> SubagentUsage:
        """Build a snapshot from an engine DoneEvent (defensively coerced)."""
        estimate_basis = getattr(event, "estimate_basis", None)
        cost_source = str(getattr(event, "cost_source", "") or "none")
        model_usage_breakdown = getattr(event, "model_usage_breakdown", None)
        missing_cost_entries = _usage_int(getattr(event, "missing_cost_entries", 0))
        has_tokens = bool(
            _usage_int(getattr(event, "input_tokens", 0))
            or _usage_int(getattr(event, "output_tokens", 0))
            or _usage_int(getattr(event, "cached_tokens", 0))
            or _usage_int(getattr(event, "cache_write_tokens", 0))
        )
        if (
            missing_cost_entries == 0
            and cost_source.strip().lower() == "unavailable"
            and estimate_basis != "free"
            and has_tokens
        ):
            # Legacy child producers do not carry missing_cost_entries. Keep
            # their unavailable, non-free usage visible instead of silently
            # treating the unknown component as a complete zero-cost receipt.
            missing_cost_entries = 1
        return cls(
            input_tokens=_usage_int(getattr(event, "input_tokens", 0)),
            output_tokens=_usage_int(getattr(event, "output_tokens", 0)),
            reasoning_tokens=_usage_int(getattr(event, "reasoning_tokens", 0)),
            cached_tokens=_usage_int(getattr(event, "cached_tokens", 0)),
            cache_write_tokens=_usage_int(getattr(event, "cache_write_tokens", 0)),
            cost_usd=_usage_float(getattr(event, "cost_usd", 0.0)),
            billed_cost=_usage_float(getattr(event, "billed_cost", 0.0)),
            cost_source=cost_source,
            estimate_basis=str(estimate_basis) if estimate_basis else None,
            model=str(getattr(event, "model", "") or ""),
            provider=str(getattr(event, "provider", "") or ""),
            missing_cost_entries=missing_cost_entries,
            model_usage_breakdown=(
                tuple(dict(row) for row in model_usage_breakdown if isinstance(row, dict))
                if isinstance(model_usage_breakdown, list)
                else ()
            ),
        )

    @property
    def has_usage(self) -> bool:
        return bool(
            self.input_tokens
            or self.output_tokens
            or self.reasoning_tokens
            or self.cached_tokens
            or self.cache_write_tokens
            or self.cost_usd
            or self.billed_cost
            or self.missing_cost_entries
        )


@dataclass
class SubagentHandle:
    """Reference to a running subagent."""

    run_id: str
    label: str
    task: asyncio.Task[str]  # type: ignore[type-arg]
    status: str = "running"  # running | done | error | aborted | archived | orphaned
    result: str = ""
    error: str = ""
    parent_task_id: int | None = None  # id() of parent asyncio.Task for orphan tracking
    spawned_at: float = field(default_factory=time.monotonic)
    completed_at: float | None = None
    # Terminal usage snapshot from the child's DoneEvent. None until the
    # child yields a terminal event (an aborted child that never reached
    # its DoneEvent keeps None — its partial spend has no snapshot to
    # report, though the durable ledger still holds its provider calls).
    usage: SubagentUsage | None = None
    # Set once the parent turn has folded this handle's usage into its
    # reported totals.
    usage_rolled_up: bool = False


class SubagentRegistry:
    """Tracks active subagent runs for a session."""

    def __init__(self) -> None:
        self._runs: dict[str, SubagentHandle] = {}
        self._archived: dict[str, SubagentHandle] = {}
        self._parent_tasks: dict[str, asyncio.Task[Any]] = {}

    def register(
        self, handle: SubagentHandle, parent_task: asyncio.Task[Any] | None = None
    ) -> None:
        self._runs[handle.run_id] = handle
        if parent_task is not None:
            self._parent_tasks[handle.run_id] = parent_task

    def count_active(self) -> int:
        return sum(1 for h in self._runs.values() if h.status == "running")

    def get(self, run_id: str) -> SubagentHandle | None:
        return self._runs.get(run_id)

    def all_handles(self) -> list[SubagentHandle]:
        return list(self._runs.values())

    def abort(self, run_id: str) -> bool:
        """Cancel a running subagent's asyncio.Task and mark it aborted."""
        handle = self._runs.get(run_id)
        if handle is None:
            return False
        handle.task.cancel()
        handle.status = "aborted"
        handle.completed_at = time.monotonic()
        return True

    def archive(self, run_id: str) -> bool:
        """Move a handle from active to archived."""
        handle = self._runs.pop(run_id, None)
        if handle is None:
            return False
        self._archived[run_id] = handle
        self._parent_tasks.pop(run_id, None)
        return True

    def get_archived(self) -> list[SubagentHandle]:
        return list(self._archived.values())

    def get_by_status(self, status: str) -> list[SubagentHandle]:
        return [h for h in self._runs.values() if h.status == status]

    def drain_usage(self) -> list[SubagentUsage]:
        """Return captured child usage not yet rolled into a parent turn.

        Marks each returned handle consumed so every child run is reported
        at most once. Archived handles are included: archiving moves a handle
        out of the active map without settling its usage.
        """
        drained: list[SubagentUsage] = []
        for handle in list(self._runs.values()) + list(self._archived.values()):
            if handle.usage is None or handle.usage_rolled_up:
                continue
            handle.usage_rolled_up = True
            if handle.usage.has_usage:
                drained.append(handle.usage)
        return drained

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for h in self._runs.values():
            counts[h.status] = counts.get(h.status, 0) + 1
        return counts

    def cleanup_orphans(self) -> list[str]:
        """Abort handles whose parent task is done. Returns list of aborted run_ids."""
        aborted: list[str] = []
        for run_id, parent_task in list(self._parent_tasks.items()):
            if parent_task.done():
                handle = self._runs.get(run_id)
                if handle and handle.status == "running":
                    self.abort(run_id)
                    aborted.append(run_id)
        return aborted

    def save_state(self, path: Path) -> None:
        """Serialize registry metadata to JSON (no asyncio.Task objects)."""
        entries = []
        for h in self._runs.values():
            entries.append(
                {
                    "run_id": h.run_id,
                    "label": h.label,
                    "status": h.status,
                    "result": h.result,
                    "error": h.error,
                    "spawned_at": h.spawned_at,
                    "completed_at": h.completed_at,
                }
            )
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    def load_state(self, path: Path) -> dict[str, SubagentHandle]:
        """Restore registry from JSON. All loaded handles are marked 'orphaned'."""
        if not path.exists():
            return {}

        entries = json.loads(path.read_text(encoding="utf-8"))
        loaded: dict[str, SubagentHandle] = {}

        for entry in entries:
            # Create a dummy completed task as placeholder
            async def _noop() -> str:
                return ""

            task: asyncio.Task[str] = asyncio.create_task(_noop())
            task.cancel()

            handle = SubagentHandle(
                run_id=entry["run_id"],
                label=entry["label"],
                task=task,
                status="orphaned",
                result=entry.get("result", ""),
                error=entry.get("error", ""),
                spawned_at=entry.get("spawned_at", 0.0),
                completed_at=entry.get("completed_at"),
            )
            loaded[handle.run_id] = handle
            self._runs[handle.run_id] = handle

        return loaded


class SubagentManager:
    """Manages subagent lifecycle for a parent agent session."""

    def __init__(
        self,
        spawn_depth: int = 0,
        max_depth: int = DEFAULT_MAX_SPAWN_DEPTH,
        max_concurrent: int = 5,
    ) -> None:
        self.spawn_depth = spawn_depth
        self.max_depth = max_depth
        self.max_concurrent = max_concurrent
        self.registry = SubagentRegistry()

    def can_spawn(self) -> bool:
        """Return True if depth and concurrency limits allow spawning."""
        if self.spawn_depth >= self.max_depth:
            return False
        if self.registry.count_active() >= self.max_concurrent:
            return False
        return True

    def _check_depth(self) -> None:
        if self.spawn_depth >= self.max_depth:
            raise RuntimeError(f"Max subagent spawn depth ({self.max_depth}) exceeded")

    def _check_concurrent(self) -> None:
        if self.registry.count_active() >= self.max_concurrent:
            raise RuntimeError(f"Max concurrent subagents ({self.max_concurrent}) exceeded")

    async def spawn(
        self,
        spec: SubagentSpec,
        agent_factory: Any,  # callable: (spec, depth[, execution_id]) -> Agent
    ) -> SubagentHandle:
        """Spawn a child agent for the given spec.

        agent_factory is a callable that returns an Agent instance.
        The child runs concurrently as an asyncio task.
        """
        self._check_depth()
        self._check_concurrent()

        run_id = str(uuid.uuid4())
        depth = self.spawn_depth + 1
        try:
            inspect.signature(agent_factory).bind(spec, depth, run_id)
        except (TypeError, ValueError):
            # Compatibility for external/test factories implementing the
            # historical two-argument internal callback.
            child_agent: Agent = agent_factory(spec, depth)
        else:
            child_agent = agent_factory(spec, depth, run_id)

        # Captured outside _run so the terminal usage survives the coroutine
        # boundary even when the task is later cancelled or times out after
        # the child's DoneEvent already arrived.
        terminal_usage: list[SubagentUsage] = []
        execution_task = spec.execution_task or spec.task

        async def _run() -> str:
            collected: list[str] = []
            terminal_text_present = False
            terminal_text = ""
            stream = child_agent.run_turn(execution_task)
            try:
                async for event in stream:
                    if hasattr(event, "text") and event.kind == "text_delta":  # type: ignore[union-attr]
                        collected.append(event.text)  # type: ignore[union-attr]
                    elif event.kind == "done":  # type: ignore[union-attr]
                        terminal_usage.append(SubagentUsage.from_done_event(event))
                        terminal_text_present, terminal_text = done_text_snapshot(event)
                        break
            finally:
                close = getattr(stream, "aclose", None)
                if callable(close):
                    # Closing in the child task that iterated the stream keeps
                    # Agent.run_turn's ContextVar token reset in its creation
                    # context instead of deferring async-generator cleanup to
                    # an unrelated event-loop finalizer task.
                    await close()
            return terminal_text if terminal_text_present else "".join(collected)

        async def _run_with_timeout() -> str:
            if spec.timeout <= 0:
                return await _run()  # no external timeout; rely on configured agent budget
            try:
                return await asyncio.wait_for(_run(), timeout=spec.timeout)
            except TimeoutError:
                raise TimeoutError(f"Subagent timed out after {spec.timeout}s")

        task: asyncio.Task[str] = asyncio.create_task(
            _run_with_timeout(), name=f"subagent-{run_id}"
        )
        handle = SubagentHandle(
            run_id=run_id,
            label=spec.label or spec.task[:40],
            task=task,
            spawned_at=time.monotonic(),
        )

        def _on_done(t: asyncio.Task[str]) -> None:
            handle.completed_at = time.monotonic()
            if terminal_usage:
                handle.usage = terminal_usage[-1]
            exc = t.exception() if not t.cancelled() else None
            if t.cancelled():
                if handle.status not in ("aborted",):
                    handle.status = "aborted"
            elif exc is not None:
                handle.status = "error"
                handle.error = str(exc)
            else:
                handle.status = "done"
                handle.result = t.result()

        task.add_done_callback(_on_done)
        self.registry.register(handle)
        return handle

    def drain_completed_usage(self) -> list[SubagentUsage]:
        """Return child usage snapshots not yet rolled into a parent turn."""
        return self.registry.drain_usage()

    async def wait_all(self, timeout: float | None = None) -> None:
        """Wait for all running subagents to finish.

        Retained as a graceful-shutdown barrier even without a live caller:
        teardown paths need an awaitable "all running subagents settled"
        primitive rather than each one open-coding ``asyncio.wait``.
        """
        tasks = [h.task for h in self.registry.all_handles() if h.status == "running"]
        if not tasks:
            return
        await asyncio.wait(tasks, timeout=timeout)

    async def abort_all(self) -> int:
        """Cancel all running subagents. Returns count of aborted tasks."""
        running = self.registry.get_by_status("running")
        for handle in running:
            self.registry.abort(handle.run_id)
        return len(running)
