"""Subagent spawning and management."""

from __future__ import annotations

import asyncio
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

        async def _run() -> str:
            collected: list[str] = []
            terminal_text_present = False
            terminal_text = ""
            async for event in child_agent.run_turn(spec.task):
                if hasattr(event, "text") and event.kind == "text_delta":  # type: ignore[union-attr]
                    collected.append(event.text)  # type: ignore[union-attr]
                elif event.kind == "done":  # type: ignore[union-attr]
                    terminal_text_present, terminal_text = done_text_snapshot(event)
                    break
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
