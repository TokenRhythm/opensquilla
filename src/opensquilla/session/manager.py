"""SessionManager — high-level lifecycle operations over SessionStorage."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from opensquilla.engine.steps.inject_time_prefix import stamp as _stamp_time_prefix
from opensquilla.paths import default_opensquilla_home
from opensquilla.session.compaction import (
    CompactionConfig,
    CompactionRequest,
    CompactionResult,
    compact_context,
)
from opensquilla.session.compaction_lifecycle import new_compaction_id
from opensquilla.session.compaction_state import (
    build_structured_summary_from_text,
    extract_compaction_obligations,
)
from opensquilla.session.keys import canonicalize_session_key, normalize_agent_id
from opensquilla.session.models import (
    MemoryDurableReceipt,
    SessionContextState,
    SessionIntent,
    SessionNode,
    SessionStatus,
    SessionSummary,
    TranscriptEntry,
)
from opensquilla.session.storage import (
    CANONICAL_FORK_PROOF_SCHEMA_VERSION,
    SessionStorage,
)
from opensquilla.session.tokenizer import estimate_tokens

if TYPE_CHECKING:
    from opensquilla.provider.types import ProviderRequestCorrelation

_SANDBOX_RUN_CONTEXT_ORIGIN_KEY = "sandbox_run_context"


@dataclass(frozen=True, slots=True)
class CanonicalTranscriptPage:
    """Bounded user-visible transcript page and archive coverage metadata."""

    entries: list[TranscriptEntry]
    has_more: bool
    canonical_complete: bool


def _validate_iana_name(name: str) -> str | None:
    """Return ``name`` if it is a resolvable IANA timezone, else None."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return name


def _resolve_local_tz_name() -> str:
    """Best-effort IANA timezone name; falls back to ``"UTC"``."""
    for env_var in ("OPENSQUILLA_TIMEZONE", "TZ"):
        candidate = os.environ.get(env_var)
        if candidate and (resolved := _validate_iana_name(candidate)):
            return resolved

    local_tz = datetime.now().astimezone().tzinfo
    if local_tz is not None:
        name = getattr(local_tz, "key", None) or str(local_tz)
        if name and (resolved := _validate_iana_name(name)):
            return resolved

    try:
        link = os.readlink("/etc/localtime")
    except OSError:
        link = ""
    if "zoneinfo/" in link:
        name = link.split("zoneinfo/", 1)[1]
        if resolved := _validate_iana_name(name):
            return resolved

    try:
        import tzlocal  # type: ignore[import-not-found]

        name = tzlocal.get_localzone_name()  # type: ignore[no-untyped-call]
        if name and (resolved := _validate_iana_name(str(name))):
            return resolved
    except Exception:
        pass

    return "UTC"


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _now_iso() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class PreparedSessionIntent:
    """Pure session mutation plan consumed by the turn-acceptance transaction."""

    node: SessionNode
    action: str
    expected_epoch: int
    previous_session_id: str | None = None
    previous_node: SessionNode | None = None
    initial_transcript_entries: tuple[TranscriptEntry, ...] = ()


@contextlib.asynccontextmanager
async def _null_async_context() -> AsyncIterator[None]:
    yield


def _session_mutation_context(
    mutation_context: Callable[[], contextlib.AbstractAsyncContextManager[None]] | None,
) -> contextlib.AbstractAsyncContextManager[None]:
    return mutation_context() if mutation_context is not None else _null_async_context()


def _compaction_flush_status_for_persistence(status: str | None) -> str:
    if not status:
        return "unknown"
    if status == "unsafe":
        return "degraded_forensic"
    return status


def _archive_dir() -> Path:
    return Path(
        os.environ.get(
            "OPENSQUILLA_SESSION_ARCHIVE_DIR",
            str(default_opensquilla_home() / "session-archive"),
        )
    )


def _safe_archive_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "session"


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _compaction_entry_payloads(entries: list[TranscriptEntry]) -> list[dict[str, Any]]:
    return [
        {
            "role": e.role,
            "content": e.content or "",
            "token_count": e.token_count,
            "tool_calls": e.tool_calls,
            "tool_call_id": e.tool_call_id,
            "reasoning_content": e.reasoning_content,
            "turn_usage": e.turn_usage,
            "turn_context": e.turn_context,
        }
        for e in entries
    ]


def _transcript_preimage(entries: list[TranscriptEntry]) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            entry.id,
            entry.message_id,
            entry.role,
            entry.content,
            entry.tool_call_id,
            entry.reasoning_content,
            entry.token_count,
            _stable_json(entry.tool_calls),
            _stable_json(entry.turn_usage),
            _stable_json(entry.turn_context),
        )
        for entry in entries
    )


def _branch_origin(parent_origin: Any) -> dict[str, Any] | None:
    if not isinstance(parent_origin, dict):
        return None
    sandbox_context = parent_origin.get(_SANDBOX_RUN_CONTEXT_ORIGIN_KEY)
    if not isinstance(sandbox_context, dict):
        return None
    return {_SANDBOX_RUN_CONTEXT_ORIGIN_KEY: dict(sandbox_context)}


class SessionManager:
    """
    Orchestrates session lifecycle: create, resume, append, branch, archive, prune.

    All I/O is async; callers must await every method.
    """

    def __init__(
        self,
        storage: SessionStorage,
        memory_sync_notify: Callable[[int], None] | None = None,
        *,
        inject_time_prefix: bool = True,
        time_prefix_tz: str | None = None,
        agent_registry: Any = None,
        task_runtime: Any = None,
        checkpoint_workspace_dir: str | Path | None = None,
        media_root: str | Path | None = None,
    ) -> None:
        self._storage = storage
        self._memory_sync_notify = memory_sync_notify
        self._inject_time_prefix = inject_time_prefix
        self._time_prefix_tz = time_prefix_tz
        self._agent_registry = agent_registry
        self._task_runtime = task_runtime
        self._checkpoint_workspace_dir = (
            Path(checkpoint_workspace_dir).expanduser()
            if checkpoint_workspace_dir is not None
            else None
        )
        # Attachment/artifact media root, used to carry material into forked
        # children; None disables the copy (e.g. in tests that never touch disk).
        self._media_root = Path(media_root).expanduser() if media_root is not None else None
        # In-process epoch cache so _emit_to_subscribers can
        # read the current epoch without a DB round-trip on every event.
        # Invalidated (updated) whenever increment_epoch commits a new value.
        self._epoch_cache: dict[str, int] = {}

    @property
    def storage(self) -> SessionStorage:
        """Storage service used by gateway/RPC composition without private access."""
        return self._storage

    def get_cached_epoch(self, session_key: str) -> int | None:
        """Return the in-process epoch cache value for high-frequency event emits."""
        return self._epoch_cache.get(session_key)

    def set_cached_epoch(self, session_key: str, epoch: int) -> None:
        """Update the in-process epoch cache after durable epoch changes."""
        self._epoch_cache[session_key] = epoch

    def attach_task_runtime(self, task_runtime: Any) -> None:
        """Attach the TaskRuntime so kill_session can cancel running children."""
        self._task_runtime = task_runtime

    def _resolve_time_prefix_tz(self) -> str:
        return self._time_prefix_tz or _resolve_local_tz_name()

    def _maybe_stamp_user_message(self, role: str, content: Any) -> Any:
        if not self._inject_time_prefix or role != "user":
            return content
        # JSON envelopes (attachments) — callers stamp the inner "text" themselves.
        if isinstance(content, str) and content.lstrip().startswith("{"):
            return content
        return self.stamp_user_text(content)

    def stamp_user_text(self, content: Any) -> Any:
        """Stamp raw user text with the configured time prefix."""
        if not self._inject_time_prefix:
            return content
        tz_name = self._resolve_time_prefix_tz()
        try:
            now = datetime.now(tz=ZoneInfo(tz_name))
        except (ZoneInfoNotFoundError, ValueError, OSError):
            now = datetime.now(tz=UTC)
            tz_name = "UTC"
        return _stamp_time_prefix(content, now, tz_name)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    @staticmethod
    def _build_session_node(
        session_key: str,
        *,
        agent_id: str,
        **kwargs: Any,
    ) -> SessionNode:
        now = _now_ms()
        return SessionNode(
            session_key=session_key,
            session_id=str(uuid.uuid4()),
            agent_id=agent_id,
            created_at=now,
            updated_at=now,
            started_at=now,
            status=SessionStatus.RUNNING,
            **kwargs,
        )

    @staticmethod
    def _build_reset_node(node: SessionNode) -> SessionNode:
        reset = node.model_copy(deep=True)
        reset.session_id = str(uuid.uuid4())
        reset.epoch = int(node.epoch or 0) + 1
        reset.updated_at = _now_ms()
        reset.input_tokens = 0
        reset.output_tokens = 0
        reset.total_tokens = 0
        reset.total_tokens_fresh = False
        reset.estimated_cost_usd = 0.0
        reset.total_cost_usd = 0.0
        reset.billed_cost_usd = 0.0
        reset.estimated_cost_component_usd = 0.0
        reset.cost_source = "none"
        reset.missing_cost_entries = 0
        reset.cache_read = 0
        reset.cache_write = 0
        reset.context_tokens = None
        reset.compaction_count = 0
        if reset.forked_from_parent:
            reset.schema_version = max(
                reset.schema_version,
                CANONICAL_FORK_PROOF_SCHEMA_VERSION,
            )
        return reset

    async def prepare_intent(
        self,
        session_key: str,
        intent: SessionIntent | str,
        *,
        agent_id: str = "main",
        **create_kwargs: Any,
    ) -> PreparedSessionIntent:
        """Prepare create/reset/continue state without writing durable state."""

        session_key = canonicalize_session_key(session_key)
        agent_id = normalize_agent_id(agent_id)
        resolved = SessionIntent(intent)
        existing = await self._storage.get_session(session_key)
        if resolved is SessionIntent.NEW_CHAT and existing is not None:
            raise ValueError("session_key conflict")
        if existing is None:
            node = self._build_session_node(
                session_key,
                agent_id=agent_id,
                **create_kwargs,
            )
            return PreparedSessionIntent(
                node=node,
                action="create",
                expected_epoch=int(node.epoch or 0),
            )
        if resolved is SessionIntent.RESET_SAME_KEY:
            reset = self._build_reset_node(existing)
            return PreparedSessionIntent(
                node=reset,
                action="reset",
                expected_epoch=int(reset.epoch or 0),
                previous_session_id=existing.session_id,
                previous_node=existing,
            )
        return PreparedSessionIntent(
            node=existing,
            action="continue",
            expected_epoch=int(existing.epoch or 0),
        )

    async def create(
        self,
        session_key: str,
        agent_id: str = "main",
        **kwargs: Any,
    ) -> SessionNode:
        """Create a new session entry. Raises ValueError if key already exists."""
        session_key = canonicalize_session_key(session_key)
        agent_id = normalize_agent_id(agent_id)
        existing = await self._storage.get_session(session_key)
        if existing is not None:
            raise ValueError(f"Session already exists: {session_key}")

        node = self._build_session_node(session_key, agent_id=agent_id, **kwargs)
        await self._storage.upsert_session(node)
        return node

    async def get_or_create(
        self,
        session_key: str,
        agent_id: str = "main",
        **kwargs: Any,
    ) -> tuple[SessionNode, bool]:
        """Return (session, created). created=True if a new session was made."""
        session_key = canonicalize_session_key(session_key)
        agent_id = normalize_agent_id(agent_id)
        existing = await self._storage.get_session(session_key)
        if existing is not None:
            return existing, False
        node = await self.create(session_key, agent_id=agent_id, **kwargs)
        return node, True

    async def get_session(self, session_key: str) -> SessionNode | None:
        """Return the session node for ``session_key`` without mutating it."""

        session_key = canonicalize_session_key(session_key)
        return await self._storage.get_session(session_key)

    async def get_agent_config(self, agent_id: str) -> dict[str, Any] | None:
        """Return the registry entry for ``agent_id``, or None when unavailable.

        Returns None (rather than raising) when the registry is not wired or
        the agent does not exist; callers treat None as "not configured" and
        fall back to defaults.
        """
        if self._agent_registry is None:
            return None
        list_agents = getattr(self._agent_registry, "list_agents", None)
        if not callable(list_agents):
            return None
        normalized = normalize_agent_id(agent_id)
        try:
            entries = await list_agents(include_builtin=True)
        except Exception:
            return None
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("id") or entry.get("agent_id")
            if entry_id and normalize_agent_id(str(entry_id)) == normalized:
                return entry
        return None

    async def list_sessions(
        self,
        agent_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        spawned_by: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return JSON-serializable session rows for tool/RPC consumers."""
        if agent_id is not None:
            agent_id = normalize_agent_id(agent_id)
        rows = await self._storage.list_sessions(
            agent_id=agent_id,
            status=status,
            limit=limit,
            offset=offset,
            spawned_by=spawned_by,
        )
        return [row.model_dump(mode="json") for row in rows]

    @property
    def has_agent_registry(self) -> bool:
        """True when an AgentRegistry is attached.

        Lets callers distinguish ``get_agent_config`` returning ``None``
        because no registry is wired (preserve legacy "no existence check"
        behavior) from ``None`` because the agent is genuinely unknown.
        """
        return self._agent_registry is not None

    async def read_transcript(
        self,
        session_key: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return JSON-serializable transcript entries for a session."""
        session_key = canonicalize_session_key(session_key)
        entries = await self.get_transcript(session_key, limit=limit)
        return [entry.model_dump(mode="json") for entry in entries]

    async def inject_message(
        self,
        session_key: str,
        message: str,
        provenance: str | dict[str, Any] = "inter_session",
    ) -> bool:
        """Append a user message to a session with provenance metadata."""
        if isinstance(provenance, str):
            provenance_payload: dict[str, Any] = {"kind": provenance}
        else:
            provenance_payload = provenance
        await self.append_message(
            session_key,
            role="user",
            content=message,
            provenance=provenance_payload,
        )
        return True

    async def kill_session(self, session_key: str) -> SessionNode:
        """Mark a session as killed and (when policy allows) cascade to children.

        Cascade is gated by the parent agent's
        ``subagents.cascade_on_parent_kill`` policy (default True) so workflows
        that intentionally rely on orphan children completing can opt out.
        Children are killed first so the parent's KILLED status persists.
        """
        session_key = canonicalize_session_key(session_key)
        node = await self._storage.get_session(session_key)

        if node is not None and await self._cascade_on_kill(node):
            await self._cascade_kill_children(session_key)

        return await self.finish(session_key, status=SessionStatus.KILLED)

    async def _cascade_on_kill(self, node: SessionNode) -> bool:
        """Resolve cascade_on_parent_kill for the session being killed."""
        agent_id = getattr(node, "agent_id", None) or "main"
        entry = await self.get_agent_config(agent_id)
        if isinstance(entry, dict):
            policy = entry.get("subagents")
            if isinstance(policy, dict) and "cascade_on_parent_kill" in policy:
                return bool(policy["cascade_on_parent_kill"])
        # Default: cascade. Matches AgentSubagentDefaults.cascade_on_parent_kill.
        return True

    async def _cascade_kill_children(self, parent_session_key: str) -> None:
        children: list[SessionNode] = []
        page = 0
        page_size = 100
        while True:
            try:
                batch = await self._storage.list_sessions(
                    status=str(SessionStatus.RUNNING),
                    spawned_by=parent_session_key,
                    limit=page_size,
                    offset=page * page_size,
                )
            except Exception:
                break
            if not batch:
                break
            children.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
        for child in children:
            child_key = getattr(child, "session_key", None)
            if not child_key:
                continue
            if self._task_runtime is not None:
                try:
                    await self._task_runtime.cancel(
                        session_key=child_key,
                        source="parent_session_kill",
                        reason="parent_session_kill",
                    )
                except TypeError:
                    with contextlib.suppress(Exception):
                        await self._task_runtime.cancel(session_key=child_key)
                except Exception:
                    pass
            try:
                await self.kill_session(child_key)
            except KeyError:
                # Child already gone — fine.
                continue

    async def wait_for_completion(
        self,
        session_key: str,
        poll_interval: float = 0.1,
    ) -> dict[str, Any]:
        """Poll until a session reaches a terminal lifecycle status."""
        terminal = {
            SessionStatus.DONE,
            SessionStatus.FAILED,
            SessionStatus.KILLED,
            SessionStatus.TIMEOUT,
        }
        while True:
            node = await self.get_session(session_key)
            if node is None:
                raise KeyError(f"Session not found: {session_key}")
            if node.status in terminal:
                payload = node.model_dump(mode="json")
                payload["waited"] = True
                return payload
            await asyncio.sleep(poll_interval)

    async def apply_intent(
        self,
        session_key: str,
        intent: SessionIntent | str,
        *,
        agent_id: str = "main",
        **create_kwargs: Any,
    ) -> tuple[SessionNode, bool]:
        """Apply transcript semantics for ``session_key``.

        Returns ``(node, rotated_or_created)``. ``rotated_or_created`` is true
        when a new transcript identity is created.
        """

        session_key = canonicalize_session_key(session_key)
        agent_id = normalize_agent_id(agent_id)
        resolved = SessionIntent(intent)
        existing = await self._storage.get_session(session_key)
        if resolved is SessionIntent.NEW_CHAT and existing is not None:
            raise ValueError("session_key conflict")
        if existing is None:
            node = await self.create(session_key, agent_id=agent_id, **create_kwargs)
            return node, True
        if resolved is SessionIntent.RESET_SAME_KEY:
            node = await self._rotate_session_id(existing)
            return node, True
        return existing, False

    async def _rotate_session_id(self, node: SessionNode) -> SessionNode:
        old_session_id = node.session_id
        # Bump the epoch FIRST (before archive/delete/rotate) so the
        # StaleEpochError guard in append_transcript_entry fences every
        # in-flight append that read the pre-reset node: once the epoch
        # advances, such an append's expected_epoch check fails and its blind
        # whole-node upsert can no longer roll the rotation back. Doing it here
        # covers all reset call sites (RPC and non-RPC), not just the ones that
        # separately call _increment_and_emit_epoch.
        try:
            new_epoch = await self._storage.increment_epoch(node.session_key)
            node.epoch = new_epoch
            self.set_cached_epoch(node.session_key, new_epoch)
        except Exception:  # noqa: BLE001 - reset must not fail on epoch bump
            pass
        await self._archive_session_identity(node)
        await self._storage.delete_transcript(old_session_id)
        await self._storage.delete_summaries(old_session_id)
        await self._storage.invalidate_context_states(
            node.session_key,
            reason="session_reset",
        )
        node.session_id = str(uuid.uuid4())
        node.updated_at = _now_ms()
        node.input_tokens = 0
        node.output_tokens = 0
        node.total_tokens = 0
        node.total_tokens_fresh = False
        node.estimated_cost_usd = 0.0
        node.total_cost_usd = 0.0
        node.billed_cost_usd = 0.0
        node.estimated_cost_component_usd = 0.0
        node.cost_source = "none"
        node.missing_cost_entries = 0
        node.cache_read = 0
        node.cache_write = 0
        node.context_tokens = None
        node.compaction_count = 0
        if node.forked_from_parent:
            node.schema_version = max(
                node.schema_version,
                CANONICAL_FORK_PROOF_SCHEMA_VERSION,
            )
        await self._storage.upsert_session(node)
        return node

    async def _archive_session_identity(self, node: SessionNode) -> None:
        """Best-effort raw archive before a same-key transcript reset."""

        entries, summaries = await self.capture_session_archive(node)
        await self.write_session_archive(node, entries, summaries)

    async def capture_session_archive(
        self,
        node: SessionNode,
    ) -> tuple[list[TranscriptEntry], list[SessionSummary]]:
        """Read reset archive material without creating filesystem side effects."""

        try:
            entries = await self._storage.get_canonical_transcript(node.session_id)
            summaries = await self._storage.get_all_summaries(node.session_id)
            return entries, summaries
        except Exception:
            return [], []

    async def write_session_archive(
        self,
        node: SessionNode,
        entries: list[TranscriptEntry],
        summaries: list[SessionSummary],
    ) -> None:
        """Write a previously captured reset archive after acceptance commits."""

        if not entries and not summaries:
            return
        try:
            archive_dir = _archive_dir()
            new_dir = not archive_dir.exists()
            archive_dir.mkdir(parents=True, exist_ok=True)
            # Harden only the directory this boot creates (mirrors the DB
            # migrator policy): the archive holds the full raw transcript, so it
            # must not inherit the umask default of 0755/0644.
            if new_dir and os.name != "nt":
                with contextlib.suppress(OSError):
                    os.chmod(archive_dir, 0o700)
            safe_key = _safe_archive_part(node.session_key)
            safe_id = _safe_archive_part(node.session_id)
            path = archive_dir / f"{_now_ms()}-{safe_key}-{safe_id}.json"
            payload = {
                "schema_version": 1,
                "archived_at": _now_iso(),
                "reason": "reset_same_key",
                "session_key": node.session_key,
                "session_id": node.session_id,
                "session": node.model_dump(mode="json"),
                "transcript_entries": [entry.model_dump(mode="json") for entry in entries],
                "summaries": [summary.model_dump(mode="json") for summary in summaries],
            }
            data = json.dumps(payload, ensure_ascii=False, indent=2)
            # Create the file owner-only (0600) so the raw transcript is never
            # group/other-readable, matching the sessions.db hardening.
            if os.name == "nt":
                path.write_text(data, encoding="utf-8")
            else:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(data)
        except Exception:
            return

    async def resume(self, session_key: str) -> SessionNode:
        """Load an existing session; touch updated_at."""
        session_key = canonicalize_session_key(session_key)
        node = await self._storage.get_session(session_key)
        if node is None:
            raise KeyError(f"Session not found: {session_key}")
        node.updated_at = _now_ms()
        await self._storage.upsert_session(node)
        return node

    async def update(self, session_key: str, **fields: Any) -> SessionNode:
        """Merge fields into an existing session and persist."""
        session_key = canonicalize_session_key(session_key)
        node = await self._storage.get_session(session_key)
        if node is None:
            raise KeyError(f"Session not found: {session_key}")
        for k, v in fields.items():
            if hasattr(node, k):
                setattr(node, k, v)
        node.updated_at = _now_ms()
        await self._storage.upsert_session(node)
        return node

    async def finish(
        self,
        session_key: str,
        status: str = SessionStatus.DONE,
    ) -> SessionNode:
        """Mark a session as finished; set ended_at and runtime_ms."""
        session_key = canonicalize_session_key(session_key)
        node = await self._storage.get_session(session_key)
        if node is None:
            raise KeyError(f"Session not found: {session_key}")
        now = _now_ms()
        node.status = status
        node.ended_at = now
        node.updated_at = now
        if node.started_at:
            node.runtime_ms = now - node.started_at
        await self._storage.upsert_session(node)
        self._evict_session_runtime_state(session_key)
        return node

    @staticmethod
    def _evict_session_runtime_state(session_key: str) -> None:
        """Drop in-memory subagent and routing bookkeeping for ``session_key``.

        Called from ``finish`` so terminal sessions don't leak unbounded
        entries in long-running gateway processes. Imports are local to
        avoid import cycles with engine/gateway packages.
        """
        try:
            from opensquilla.gateway.subagent_announce import _tracker as _spawn_tracker

            _spawn_tracker.evict(session_key)
        except Exception:
            pass
        try:
            from opensquilla.engine.steps.squilla_router import (
                _history_store as _routing_store,
            )

            _routing_store.evict(session_key)
        except Exception:
            pass
        try:
            from opensquilla.tools.builtin.sessions import evict_spawn_lock

            evict_spawn_lock(session_key)
        except Exception:
            pass

    async def branch(
        self,
        parent_session_key: str,
        new_session_key: str,
        fork_transcript: bool = False,
        max_fork_tokens: int | None = None,
        status: SessionStatus | str = SessionStatus.RUNNING,
        fork_before_message_id: str | None = None,
        *,
        mutation_context: Callable[[], contextlib.AbstractAsyncContextManager[None]] | None = None,
    ) -> SessionNode:
        """Create a child while optionally holding the parent's mutation lock."""
        async with _session_mutation_context(mutation_context):
            return await self._branch_locked(
                parent_session_key,
                new_session_key,
                fork_transcript=fork_transcript,
                max_fork_tokens=max_fork_tokens,
                status=status,
                fork_before_message_id=fork_before_message_id,
            )

    async def _branch_locked(
        self,
        parent_session_key: str,
        new_session_key: str,
        fork_transcript: bool = False,
        max_fork_tokens: int | None = None,
        status: SessionStatus | str = SessionStatus.RUNNING,
        fork_before_message_id: str | None = None,
    ) -> SessionNode:
        """
        Create a child session branched from parent.
        If fork_transcript=True and parent token budget permits, copy parent transcript
        as initial context in the child (forkedFromParent flag set).
        If fork_before_message_id is set, copy only the canonical transcript prefix
        before that message and skip parent compaction summaries/context states.
        ``status`` sets the child's initial lifecycle status; pass a resting
        status such as ``SessionStatus.DONE`` when the child should not appear
        as an active run until its first turn starts.
        """
        parent_session_key = canonicalize_session_key(parent_session_key)
        new_session_key = canonicalize_session_key(new_session_key)
        parent = await self._storage.get_session(parent_session_key)
        if parent is None:
            raise KeyError(f"Parent session not found: {parent_session_key}")

        now = _now_ms()
        child = SessionNode(
            session_key=new_session_key,
            session_id=str(uuid.uuid4()),
            agent_id=parent.agent_id,
            parent_session_key=parent_session_key,
            spawned_by=parent_session_key,
            spawn_depth=(parent.spawn_depth or 0) + 1,
            created_at=now,
            updated_at=now,
            started_at=now,
            status=status,
            model=parent.model,
            model_provider=parent.model_provider,
            channel=parent.channel,
            chat_type=parent.chat_type,
            origin=_branch_origin(parent.origin),
        )

        if fork_transcript:
            is_prefix_fork = bool(fork_before_message_id)
            parent_coverage = await self._storage.get_canonical_transcript_coverage(
                parent.session_id
            )
            parent_canonical_complete = parent_coverage.canonical_complete
            parent_compaction_count = parent_coverage.compaction_count
            if fork_before_message_id:
                canonical_entries = await self._storage.get_canonical_transcript(parent.session_id)
                fork_index = next(
                    (
                        index
                        for index, entry in enumerate(canonical_entries)
                        if entry.message_id == fork_before_message_id
                    ),
                    None,
                )
                if fork_index is None:
                    raise KeyError(
                        f"Transcript message not found in {parent_session_key}: "
                        f"{fork_before_message_id}"
                    )
                parent_entries = canonical_entries[:fork_index]
                parent_summaries = []
                parent_context_states = []
            else:
                parent_entries = await self._storage.get_transcript(parent.session_id)
                parent_summaries = await self._storage.get_all_summaries(parent.session_id)
                parent_context_states = await self._storage.get_context_states(parent_session_key)
            summary_tokens = sum(
                estimate_tokens(summary.summary_text) for summary in parent_summaries
            )
            parent_tokens = sum(e.token_count or 0 for e in parent_entries) + summary_tokens
            if max_fork_tokens is None or parent_tokens <= max_fork_tokens:
                if is_prefix_fork:
                    # A prefix fork rewrites every copied canonical row as active raw
                    # transcript, so a complete parent needs no inherited compaction
                    # evidence. If the parent's canonical archive is incomplete, keep
                    # a durable unmatched count so this child cannot claim completeness
                    # after the missing rows have already been discarded.
                    child.compaction_count = (
                        0
                        if parent_canonical_complete
                        else max(1, parent_compaction_count)
                    )
                else:
                    # Full forks copy summaries and compacted rows verbatim. Preserve
                    # the parent's count. If its incomplete legacy lineage has no
                    # count of its own, persist an unmatched count so the new fork's
                    # semantic version cannot accidentally certify missing history.
                    child.compaction_count = (
                        parent_compaction_count
                        if parent_canonical_complete
                        else max(1, parent_compaction_count)
                    )
                child.schema_version = max(
                    child.schema_version,
                    CANONICAL_FORK_PROOF_SCHEMA_VERSION,
                )
                # Copy entries into child session
                if not is_prefix_fork:
                    await self._storage.copy_compacted_transcript_entries(
                        source_session_id=parent.session_id,
                        target_session_id=child.session_id,
                        target_session_key=new_session_key,
                    )
                for entry in parent_entries:
                    forked = TranscriptEntry(
                        session_id=child.session_id,
                        session_key=new_session_key,
                        role=entry.role,
                        content=entry.content,
                        tool_calls=entry.tool_calls,
                        tool_call_id=entry.tool_call_id,
                        reasoning_content=entry.reasoning_content,
                        turn_usage=entry.turn_usage,
                        turn_context=entry.turn_context,
                        created_at=entry.created_at,
                        token_count=entry.token_count,
                        provenance_kind=entry.provenance_kind,
                        provenance_origin_session_id=entry.provenance_origin_session_id,
                        provenance_source_session_key=entry.provenance_source_session_key,
                        provenance_source_channel=entry.provenance_source_channel,
                        provenance_source_tool=entry.provenance_source_tool,
                    )
                    await self._storage.append_transcript_entry(forked)
                for summary in parent_summaries:
                    await self._storage.save_summary(
                        SessionSummary(
                            session_id=child.session_id,
                            session_key=new_session_key,
                            compaction_id=summary.compaction_id,
                            trigger_reason=summary.trigger_reason,
                            summary_text=summary.summary_text,
                            summary_payload=summary.summary_payload,
                            summary_format=summary.summary_format,
                            summary_source=summary.summary_source,
                            coverage_status=summary.coverage_status,
                            missing_obligations=summary.missing_obligations,
                            critical_carry_forward=summary.critical_carry_forward,
                            tokens_before=summary.tokens_before,
                            tokens_after=summary.tokens_after,
                            removed_count=summary.removed_count,
                            kept_count=summary.kept_count,
                            chunk_count=summary.chunk_count,
                            flush_receipt_status=summary.flush_receipt_status,
                            covered_through_id=summary.covered_through_id,
                            created_at=summary.created_at,
                        )
                    )
                for state in parent_context_states:
                    await self._storage.save_context_state(
                        SessionContextState(
                            session_id=child.session_id,
                            session_key=new_session_key,
                            provider=state.provider,
                            model=state.model,
                            state_kind=state.state_kind,
                            payload=state.payload,
                            covered_through_id=state.covered_through_id,
                            created_at=state.created_at,
                            expires_at=state.expires_at,
                            portable=state.portable,
                            cacheable=state.cacheable,
                            valid=state.valid,
                            invalid_reason=state.invalid_reason,
                            schema_version=state.schema_version,
                        )
                    )
                child.forked_from_parent = True
                await self._copy_fork_materials(
                    parent.session_id, child.session_id, new_session_key
                )

        await self._storage.upsert_session(child)
        return child

    async def prepare_prefix_branch(
        self,
        parent_session_key: str,
        new_session_key: str,
        *,
        fork_before_message_id: str,
        status: SessionStatus | str = SessionStatus.DONE,
    ) -> PreparedSessionIntent:
        """Prepare a WebChat prefix fork without writing the child session."""

        parent_session_key = canonicalize_session_key(parent_session_key)
        new_session_key = canonicalize_session_key(new_session_key)
        parent = await self._storage.get_session(parent_session_key)
        if parent is None:
            raise KeyError(f"Parent session not found: {parent_session_key}")
        parent_coverage = await self._storage.get_canonical_transcript_coverage(
            parent.session_id
        )
        canonical_entries = await self._storage.get_canonical_transcript(parent.session_id)
        fork_index = next(
            (
                index
                for index, entry in enumerate(canonical_entries)
                if entry.message_id == fork_before_message_id
            ),
            None,
        )
        if fork_index is None:
            raise KeyError(
                f"Transcript message not found in {parent_session_key}: "
                f"{fork_before_message_id}"
            )

        now = _now_ms()
        child = SessionNode(
            session_key=new_session_key,
            session_id=str(uuid.uuid4()),
            agent_id=parent.agent_id,
            parent_session_key=parent_session_key,
            spawned_by=parent_session_key,
            spawn_depth=(parent.spawn_depth or 0) + 1,
            created_at=now,
            updated_at=now,
            started_at=now,
            status=status,
            model=parent.model,
            model_provider=parent.model_provider,
            channel=parent.channel,
            chat_type=parent.chat_type,
            display_name=parent.display_name,
            forked_from_parent=True,
        )
        child.compaction_count = (
            0
            if parent_coverage.canonical_complete
            else max(1, parent_coverage.compaction_count)
        )
        child.schema_version = max(
            child.schema_version,
            CANONICAL_FORK_PROOF_SCHEMA_VERSION,
        )
        copied_entries = tuple(
            TranscriptEntry(
                session_id=child.session_id,
                session_key=new_session_key,
                role=entry.role,
                content=entry.content,
                tool_calls=entry.tool_calls,
                tool_call_id=entry.tool_call_id,
                reasoning_content=entry.reasoning_content,
                turn_usage=entry.turn_usage,
                turn_context=entry.turn_context,
                created_at=entry.created_at,
                token_count=entry.token_count,
                provenance_kind=entry.provenance_kind,
                provenance_origin_session_id=entry.provenance_origin_session_id,
                provenance_source_session_key=entry.provenance_source_session_key,
                provenance_source_channel=entry.provenance_source_channel,
                provenance_source_tool=entry.provenance_source_tool,
            )
            for entry in canonical_entries[:fork_index]
        )
        return PreparedSessionIntent(
            node=child,
            action="fork",
            expected_epoch=int(child.epoch or 0),
            previous_session_id=parent.session_id,
            previous_node=parent,
            initial_transcript_entries=copied_entries,
        )

    async def _copy_fork_materials(
        self,
        source_session_id: str,
        target_session_id: str,
        target_session_key: str,
    ) -> None:
        """Carry a parent session's attachment/artifact material into a forked child.

        ``branch`` copies transcript rows, but the artifact and attachment material
        stores are keyed by session id, so without this the child's generated images,
        generated files, and uploaded attachments resolve to an empty bucket and fail
        to preview or replay. Runs off the event loop and never raises: a copy failure
        must not abort the fork, whose session row is committed by the caller next.
        """
        media_root = self._media_root
        if media_root is None:
            return
        import structlog as _structlog

        _log = _structlog.get_logger(__name__)

        def _run() -> None:
            from opensquilla.artifacts import ArtifactStore
            from opensquilla.attachment_refs import copy_transcript_material

            ArtifactStore(media_root).copy_session_artifacts(
                source_session_id=source_session_id,
                target_session_id=target_session_id,
                target_session_key=target_session_key,
            )
            copy_transcript_material(
                media_root=media_root,
                source_session_id=source_session_id,
                target_session_id=target_session_id,
            )

        try:
            await asyncio.to_thread(_run)
        except Exception:
            _log.warning(
                "session.fork.material_copy_failed",
                source_session_id=source_session_id,
                target_session_id=target_session_id,
                exc_info=True,
            )

    # ── Transcript ───────────────────────────────────────────────────────────

    async def prepare_message(
        self,
        session_key: str,
        role: str,
        content: str,
        *,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        reasoning_content: str | None = None,
        turn_usage: dict[str, Any] | None = None,
        turn_context: dict[str, Any] | None = None,
        token_count: int | None = None,
        provenance: dict[str, Any] | None = None,
        session_node: SessionNode | None = None,
    ) -> tuple[TranscriptEntry, int]:
        """Build an epoch-fenced transcript entry without persisting it."""

        session_key = canonicalize_session_key(session_key)
        node = session_node
        if node is not None and canonicalize_session_key(node.session_key) != session_key:
            raise ValueError("session_node does not match session_key")
        if node is None:
            node = await self._storage.get_session(session_key)
        if node is None:
            raise KeyError(f"Session not found: {session_key}")

        content = self._maybe_stamp_user_message(role, content)

        if turn_context is None:
            from opensquilla.session.turn_context import current_turn_context

            turn_context = current_turn_context()

        entry = TranscriptEntry(
            session_id=node.session_id,
            session_key=session_key,
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            reasoning_content=reasoning_content if role == "assistant" else None,
            turn_usage=turn_usage if role == "assistant" else None,
            turn_context=dict(turn_context) if turn_context is not None else None,
            token_count=token_count,
        )

        # Apply provenance only if not already set (spec: never overwrite)
        if provenance:
            entry.provenance_kind = provenance.get("kind")
            entry.provenance_origin_session_id = provenance.get("origin_session_id")
            entry.provenance_source_session_key = provenance.get("source_session_key")
            entry.provenance_source_channel = provenance.get("source_channel")
            entry.provenance_source_tool = provenance.get("source_tool")

        # Pass the epoch we read from the node so storage can perform an
        # atomic INSERT WHERE epoch=? guard against concurrent resets.
        expected_epoch = node.epoch if node.epoch is not None else 0
        return entry, expected_epoch

    def notify_message_appended(self, entry: TranscriptEntry) -> None:
        """Notify memory capture after an entry's transaction has committed."""

        if self._memory_sync_notify is None:
            return
        content = entry.content or ""
        byte_count = len(content.encode("utf-8")) if isinstance(content, str) else 0
        self._memory_sync_notify(byte_count)

    async def append_message(
        self,
        session_key: str,
        role: str,
        content: str,
        *,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        reasoning_content: str | None = None,
        turn_usage: dict[str, Any] | None = None,
        token_count: int | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> TranscriptEntry:
        """Append a message and narrowly touch its session in one transaction."""

        entry, expected_epoch = await self.prepare_message(
            session_key,
            role,
            content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            reasoning_content=reasoning_content,
            turn_usage=turn_usage,
            token_count=token_count,
            provenance=provenance,
        )
        token_delta = token_count if token_count and turn_usage is None else 0
        await self._storage.append_transcript_entry_and_touch(
            entry,
            expected_epoch=expected_epoch,
            updated_at=_now_ms(),
            token_delta=token_delta,
            mark_total_tokens_stale=bool(token_delta),
        )
        self.notify_message_appended(entry)
        return entry

    async def remove_message(self, session_key: str, message_id: str) -> bool:
        """Remove a single transcript entry by ``message_id``.

        Used by the gateway to roll back a just-appended user turn when the
        downstream enqueue fails (e.g. ``TaskQueueFullError``). Returns True
        iff a row was actually removed; the caller uses this result to decide
        whether the failure is safe to mark retryable or whether a dirty
        orphan remains.
        """
        session_key = canonicalize_session_key(session_key)
        node = await self._storage.get_session(session_key)
        if node is None:
            return False
        return await self._storage.delete_transcript_entry(node.session_id, message_id)

    async def update_message_turn_context(
        self,
        session_key: str,
        message_id: str,
        turn_context: dict[str, Any],
    ) -> bool:
        """Persist the latest disposition for one causally identified input."""

        return await self._storage.update_transcript_turn_context(
            canonicalize_session_key(session_key),
            message_id,
            turn_context,
        )

    async def get_transcript(
        self, session_key: str, limit: int | None = None
    ) -> list[TranscriptEntry]:
        session_key = canonicalize_session_key(session_key)
        node = await self._storage.get_session(session_key)
        if node is None:
            raise KeyError(f"Session not found: {session_key}")
        return await self._storage.get_transcript(node.session_id, limit=limit)

    async def record_memory_checkpoint(
        self,
        session_key: str,
        transcript: list[TranscriptEntry] | None = None,
        *,
        turn_id: str | None = None,
        source: str = "session_manager",
    ) -> MemoryDurableReceipt:
        """Persist a durable transcript checkpoint receipt before compaction."""
        from opensquilla.memory.checkpoint import (
            append_checkpoint_events,
            build_checkpoint_events,
            checkpoint_coverage_hash,
            checkpoint_event_hash,
            checkpoint_turn_id,
            serialize_checkpoint_event,
        )

        session_key = canonicalize_session_key(session_key)
        node = await self._storage.get_session(session_key)
        if node is None:
            raise KeyError(f"Session not found: {session_key}")
        entries = (
            list(transcript)
            if transcript is not None
            else await self._storage.get_transcript(node.session_id)
        )
        if not entries:
            raise ValueError("checkpoint transcript cannot be empty")

        resolved_turn_id = turn_id or checkpoint_turn_id(entries)
        coverage_turn_id = checkpoint_turn_id(entries)
        coverage_hash = checkpoint_coverage_hash(entries)
        coverage_entry_count = len(entries)
        events = build_checkpoint_events(
            session_key=session_key,
            session_id=node.session_id,
            entries=entries,
            source=source,
            turn_id=resolved_turn_id,
        )
        workspace = self._checkpoint_workspace_dir
        event_body_hash = checkpoint_event_hash(
            "\n".join(serialize_checkpoint_event(event) for event in events)
        )
        failure_key = (
            f"checkpoint:{session_key}:{resolved_turn_id}:"
            f"{event_body_hash}"
        )
        try:
            if workspace is None:
                raise RuntimeError("checkpoint workspace_dir is not configured")
            result = await asyncio.to_thread(append_checkpoint_events, workspace, events)
        except Exception as exc:
            failure_key = (
                f"{failure_key}:failed:{checkpoint_event_hash(str(exc))[:16]}"
            )
            receipt = MemoryDurableReceipt(
                session_key=session_key,
                session_id=node.session_id,
                turn_id=resolved_turn_id,
                scope="checkpoint",
                content_hash=None,
                coverage_turn_id=coverage_turn_id,
                coverage_hash=coverage_hash,
                coverage_entry_count=coverage_entry_count,
                idempotency_key=failure_key,
                status="checkpoint_failed",
                reason=str(exc),
                attempt_count=1,
            )
            try:
                await self._storage.upsert_memory_durable_receipt(receipt)
            except Exception:
                pass
            raise

        receipt = MemoryDurableReceipt(
            session_key=session_key,
            session_id=node.session_id,
            turn_id=resolved_turn_id,
            scope="checkpoint",
            source_path=result.relative_path,
            content_hash=result.content_hash,
            coverage_turn_id=coverage_turn_id,
            coverage_hash=coverage_hash,
            coverage_entry_count=coverage_entry_count,
            idempotency_key=(
                f"checkpoint:{session_key}:{resolved_turn_id}:{result.content_hash}"
            ),
            status="checkpoint_saved",
            attempt_count=1,
        )
        return await self._storage.upsert_memory_durable_receipt(receipt)

    async def get_canonical_transcript(
        self, session_key: str, limit: int | None = None
    ) -> list[TranscriptEntry]:
        """Return archived compacted rows plus the active transcript tail."""
        session_key = canonicalize_session_key(session_key)
        node = await self._storage.get_session(session_key)
        if node is None:
            raise KeyError(f"Session not found: {session_key}")
        return await self._storage.get_canonical_transcript(node.session_id, limit=limit)

    async def get_canonical_transcript_page(
        self,
        session_key: str,
        *,
        limit: int,
        before: tuple[int, int] | None = None,
        after: tuple[int, int] | None = None,
    ) -> CanonicalTranscriptPage:
        """Return a bounded canonical page without changing provider replay."""
        session_key = canonicalize_session_key(session_key)
        node = await self._storage.get_session(session_key)
        if node is None:
            raise KeyError(f"Session not found: {session_key}")
        entries, has_more = await self._storage.get_canonical_transcript_page(
            node.session_id,
            limit=limit,
            before=before,
            after=after,
        )
        canonical_complete = await self._storage.is_canonical_transcript_complete(node.session_id)
        return CanonicalTranscriptPage(
            entries=entries,
            has_more=has_more,
            canonical_complete=canonical_complete,
        )

    async def get_summaries(self, session_key: str) -> list[SessionSummary]:
        """Return durable compaction summaries for a session key."""
        session_key = canonicalize_session_key(session_key)
        node = await self._storage.get_session(session_key)
        if node is None:
            raise KeyError(f"Session not found: {session_key}")
        return await self._storage.get_all_summaries(node.session_id)

    async def list_degraded_compactions(
        self,
        *,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> list[SessionSummary]:
        prefix = f"agent:{normalize_agent_id(agent_id)}:" if agent_id else None
        return await self._storage.list_degraded_summaries(
            session_key_prefix=prefix,
            limit=limit,
        )

    async def get_compaction_preimage(self, summary: SessionSummary) -> list[TranscriptEntry]:
        if not summary.compaction_id:
            return []
        return await self._storage.get_compacted_transcript_entries(
            session_id=summary.session_id,
            compaction_id=summary.compaction_id,
        )

    async def mark_compaction_repair_status(
        self,
        summary: SessionSummary,
        status: str,
    ) -> None:
        if summary.id is None:
            return
        await self._storage.update_summary_flush_receipt_status(summary.id, status)

    async def mark_compaction_flush_receipt_status(
        self,
        session_key: str,
        compaction_id: str,
        status: str,
    ) -> int:
        return await self._storage.update_summary_flush_receipt_status_by_compaction(
            session_key=canonicalize_session_key(session_key),
            compaction_id=compaction_id,
            status=status,
        )

    async def save_context_state(self, state: SessionContextState) -> SessionContextState:
        """Persist portable or provider-specific context state."""
        return await self._storage.save_context_state(state)

    async def get_context_states(
        self,
        session_key: str,
        *,
        provider: str | None = None,
        state_kind: str | None = None,
        valid_only: bool = True,
    ) -> list[SessionContextState]:
        """Return context states for a session key without changing replay behavior."""
        return await self._storage.get_context_states(
            session_key,
            provider=provider,
            state_kind=state_kind,
            valid_only=valid_only,
        )

    async def invalidate_context_states(
        self,
        session_key: str,
        *,
        provider: str | None = None,
        state_kind: str | None = None,
        reason: str = "invalidated",
    ) -> int:
        """Mark matching context states invalid while keeping audit history."""
        return await self._storage.invalidate_context_states(
            session_key,
            provider=provider,
            state_kind=state_kind,
            reason=reason,
        )

    @staticmethod
    def _portable_structured_summary_state(
        node: SessionNode, summary: SessionSummary | None
    ) -> SessionContextState | None:
        if (
            summary is None
            or summary.summary_format != "structured_v1"
            or summary.summary_payload is None
        ):
            return None
        payload = dict(summary.summary_payload)
        if summary.compaction_id:
            payload["compaction_id"] = summary.compaction_id
        return SessionContextState(
            session_id=node.session_id,
            session_key=node.session_key,
            provider="portable",
            model=None,
            state_kind="structured_summary_v1",
            payload=payload,
            covered_through_id=summary.covered_through_id,
            portable=True,
            cacheable=True,
        )

    # ── Compaction ───────────────────────────────────────────────────────────

    async def compact(
        self,
        session_key: str,
        context_window_tokens: int,
        config: CompactionConfig | None = None,
        custom_instructions: str | None = None,
        *,
        mutation_context: Callable[[], contextlib.AbstractAsyncContextManager[None]] | None = None,
        provider_request_correlation: ProviderRequestCorrelation | None = None,
    ) -> str:
        """
        Compact the session transcript when context is filling up.
        Summarizes older entries, keeps recent ones, stores summary out-of-band.
        Returns the summary string.
        """
        correlation_kwargs: dict[str, Any] = {}
        if provider_request_correlation is not None:
            correlation_kwargs["provider_request_correlation"] = (
                provider_request_correlation
            )
        result = await self.compact_with_result(
            session_key,
            context_window_tokens,
            config,
            custom_instructions,
            mutation_context=mutation_context,
            **correlation_kwargs,
        )
        return result.summary if result.removed_count else ""

    async def compact_with_result(
        self,
        session_key: str,
        context_window_tokens: int,
        config: CompactionConfig | None = None,
        custom_instructions: str | None = None,
        *,
        compaction_id: str | None = None,
        trigger_reason: str | None = None,
        flush_receipt_status: str | None = None,
        mutation_context: Callable[[], contextlib.AbstractAsyncContextManager[None]] | None = None,
        provider_request_correlation: ProviderRequestCorrelation | None = None,
    ) -> CompactionResult:
        """Compact the session transcript and return full compaction metadata."""

        session_key = canonicalize_session_key(session_key)
        async with _session_mutation_context(mutation_context):
            node = await self._storage.get_session(session_key)
            if node is None:
                raise KeyError(f"Session not found: {session_key}")

            entries = await self._storage.get_transcript(node.session_id)
            preimage = _transcript_preimage(entries)
            raw = _compaction_entry_payloads(entries)

        result = await compact_context(
            CompactionRequest(
                session_id=node.session_id,
                entries=raw,
                context_window_tokens=context_window_tokens,
                config=config or CompactionConfig(),
                custom_instructions=custom_instructions,
                provider_request_correlation=provider_request_correlation,
            )
        )

        if result.removed_count == 0:
            return result
        if not result.summary:
            import structlog as _structlog

            _structlog.get_logger(__name__).warning(
                "session_compaction.empty_summary_not_persisted",
                session_key=session_key,
                removed_count=result.removed_count,
            )
            return replace(result, skip_reason=result.skip_reason or "empty_summary")

        async with _session_mutation_context(mutation_context):
            current_node = await self._storage.get_session(session_key)
            if current_node is None:
                raise KeyError(f"Session not found: {session_key}")
            current_entries = await self._storage.get_transcript(current_node.session_id)
            if _transcript_preimage(current_entries) != preimage:
                import structlog as _structlog

                _structlog.get_logger(__name__).warning(
                    "session_compaction.stale_preimage_skipped",
                    session_key=session_key,
                    original_entries=len(entries),
                    current_entries=len(current_entries),
                )
                return replace(
                    result,
                    summary="",
                    kept_entries=_compaction_entry_payloads(current_entries),
                    removed_count=0,
                    chunks_processed=0,
                    summary_source="skipped",
                    skip_reason="stale_preimage",
                    tokens_after=result.tokens_before,
                    remaining_budget_tokens=max(
                        context_window_tokens - result.tokens_before,
                        0,
                    ),
                )

            removed_entries = current_entries[: len(current_entries) - len(result.kept_entries)]
            kept_entries = current_entries[len(removed_entries) :]
            persisted_compaction_id = compaction_id or new_compaction_id()
            summary_record = SessionSummary(
                session_id=current_node.session_id,
                session_key=session_key,
                compaction_id=persisted_compaction_id,
                trigger_reason=trigger_reason,
                summary_text=result.summary,
                summary_payload=result.summary_payload,
                summary_format=result.summary_format,
                summary_source=result.summary_source,
                coverage_status=result.coverage_status,
                missing_obligations=result.missing_obligations,
                critical_carry_forward=result.critical_carry_forward,
                tokens_before=result.tokens_before,
                tokens_after=result.tokens_after,
                removed_count=result.removed_count,
                kept_count=len(kept_entries),
                chunk_count=result.chunks_processed,
                flush_receipt_status=_compaction_flush_status_for_persistence(
                    flush_receipt_status
                ),
                covered_through_id=max((entry.id or 0) for entry in removed_entries)
                if removed_entries
                else 0,
            )
            current_node.compaction_count = (current_node.compaction_count or 0) + 1
            current_node.updated_at = _now_ms()
            context_state = self._portable_structured_summary_state(
                current_node,
                summary_record,
            )
            await self._storage.rewrite_compacted_session(
                node=current_node,
                summary=summary_record,
                entries=kept_entries,
                context_states=[context_state] if context_state is not None else None,
                archived_entries=removed_entries,
            )
        return result

    async def persist_compaction_result(
        self,
        session_key: str,
        summary: str,
        kept_entries: list[dict],
        *,
        compaction_id: str | None = None,
        trigger_reason: str | None = None,
        flush_receipt_status: str | None = None,
    ) -> None:
        """Persist a pre-computed compaction result directly (no LLM re-compaction).

        Called by TurnRunner when Agent emits CompactionEvent. Writes the Agent's
        actual compaction output to DB, avoiding the double-compaction bug that
        would occur if we called compact() (which re-reads DB and re-runs LLM).
        """
        session_key = canonicalize_session_key(session_key)
        import structlog as _structlog

        _log = _structlog.get_logger(__name__)

        node = await self._storage.get_session(session_key)
        if node is None:
            _log.warning("persist_compaction.session_not_found", session_key=session_key)
            return

        entries = await self._storage.get_transcript(node.session_id)
        removed_entries = entries[: max(0, len(entries) - len(kept_entries))]
        preserved_entries = entries[len(removed_entries) :]
        if removed_entries and not summary:
            _log.warning(
                "persist_compaction.empty_summary_not_persisted",
                session_key=session_key,
                removed=len(removed_entries),
                kept=len(kept_entries),
            )
            return

        # Store summary out-of-band. New compactions must not prepend a
        # transcript system marker because history loading would make that
        # marker provider-visible and cache-hostile.
        summary_record = None
        if summary:
            persisted_compaction_id = compaction_id or new_compaction_id()
            raw_removed_entries = [
                {
                    "id": entry.id,
                    "role": entry.role,
                    "content": entry.content or "",
                    "tool_calls": entry.tool_calls,
                    "tool_call_id": entry.tool_call_id,
                }
                for entry in removed_entries
            ]
            obligations = extract_compaction_obligations(raw_removed_entries)
            structured_summary, coverage = build_structured_summary_from_text(summary, obligations)
            summary_record = SessionSummary(
                session_id=node.session_id,
                session_key=session_key,
                compaction_id=persisted_compaction_id,
                trigger_reason=trigger_reason,
                summary_text=summary,
                summary_payload=structured_summary.model_dump(mode="json"),
                summary_format="structured_v1",
                coverage_status=coverage.status,
                missing_obligations=coverage.missing_obligations,
                critical_carry_forward=coverage.critical_carry_forward,
                removed_count=len(removed_entries),
                kept_count=len(kept_entries),
                flush_receipt_status=_compaction_flush_status_for_persistence(
                    flush_receipt_status
                ),
                covered_through_id=max((entry.id or 0) for entry in removed_entries)
                if removed_entries
                else 0,
            )

        # Insert kept entries, preserving original metadata where possible
        rewritten_entries: list[TranscriptEntry] = []
        for index, raw in enumerate(kept_entries):
            if index < len(preserved_entries):
                preserved = preserved_entries[index]
                if preserved.role == raw.get("role") and preserved.content == raw.get("content"):
                    rewritten_entries.append(preserved)
                    continue
            entry = TranscriptEntry(
                session_id=node.session_id,
                session_key=session_key,
                role=raw.get("role", "user"),
                content=raw.get("content", ""),
                tool_calls=raw.get("tool_calls"),
                tool_call_id=raw.get("tool_call_id"),
                turn_usage=raw.get("turn_usage"),
                turn_context=raw.get("turn_context"),
            )
            rewritten_entries.append(entry)

        node.compaction_count = (node.compaction_count or 0) + 1
        node.updated_at = _now_ms()
        context_state = self._portable_structured_summary_state(node, summary_record)
        await self._storage.rewrite_compacted_session(
            node=node,
            summary=summary_record,
            entries=rewritten_entries,
            context_states=[context_state] if context_state is not None else None,
            archived_entries=removed_entries if summary_record is not None else None,
        )
        _log.info(
            "persist_compaction.done",
            session_key=session_key,
            summary_len=len(summary),
            kept=len(kept_entries),
        )

    async def truncate(self, session_key: str, max_messages: int = 20) -> dict:
        """Truncate transcript to the most recent *max_messages* entries.

        Unlike compact() (which summarises via LLM), this is simple count-based cut.
        """
        if max_messages < 0:
            raise ValueError("max_messages must be >= 0")

        session_key = canonicalize_session_key(session_key)
        node = await self._storage.get_session(session_key)
        if node is None:
            raise KeyError(f"Session not found: {session_key}")

        entries = await self._storage.get_transcript(node.session_id)
        before_count = len(entries)

        if before_count <= max_messages:
            return {"truncated": False, "before_count": before_count, "after_count": before_count}

        recent = [] if max_messages == 0 else entries[-max_messages:]
        await self._storage.delete_transcript(node.session_id)
        for entry in recent:
            await self._storage.append_transcript_entry(entry)

        node.updated_at = _now_ms()
        await self._storage.upsert_session(node)

        return {"truncated": True, "before_count": before_count, "after_count": len(recent)}

    # ── Maintenance ──────────────────────────────────────────────────────────

    async def prune_stale(self, max_age_ms: int) -> int:
        """Delete sessions older than max_age_ms. Returns number pruned."""
        cutoff = _now_ms() - max_age_ms
        return await self._storage.prune_stale_sessions(cutoff)

    async def cap_entries(self, max_entries: int = 500) -> int:
        """Delete oldest sessions beyond max_entries. Returns number deleted."""
        total = await self._storage.count_sessions()
        if total <= max_entries:
            return 0
        sessions = await self._storage.list_sessions(limit=total)
        # sorted by updated_at asc — oldest first
        to_delete = sorted(sessions, key=lambda s: s.updated_at)[: total - max_entries]
        for s in to_delete:
            await self._storage.delete_session(s.session_key)
        return len(to_delete)

    async def archive(self, session_key: str) -> None:
        """Archive (soft-finish) a session by marking status=done."""
        session_key = canonicalize_session_key(session_key)
        await self.finish(session_key, status=SessionStatus.DONE)
