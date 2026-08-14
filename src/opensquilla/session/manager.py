"""SessionManager — high-level lifecycle operations over SessionStorage."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from opensquilla.contracts.turn_execution import AssistantMessageReservation
from opensquilla.engine.steps.inject_time_prefix import stamp as _stamp_time_prefix
from opensquilla.paths import default_opensquilla_home, native_io_path
from opensquilla.session.compaction import (
    CompactionConfig,
    CompactionRequest,
    CompactionResult,
    arm_compaction_deadline,
    await_compaction_phase,
    compact_context,
    compaction_remaining_seconds,
    require_compaction_time,
)
from opensquilla.session.compaction_deployment import compaction_deployment_fingerprint
from opensquilla.session.compaction_lifecycle import new_compaction_id
from opensquilla.session.compaction_state import (
    StructuredCompactionSummary,
    build_structured_summary_from_text,
    extract_compaction_obligations,
)
from opensquilla.session.context_view import (
    build_compaction_context_records,
    compaction_context_fingerprint,
    format_compaction_summary_context,
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
    ResetArchiveSnapshot,
    SessionStorage,
)
from opensquilla.session.tokenizer import estimate_tokens
from opensquilla.silent_reply import sanitize_historical_silent_reply
from opensquilla.turn_outcome_projection import (
    attach_fork_terminal_outcome_projection,
    build_fork_terminal_outcome_projection,
    extract_fork_terminal_outcome_projection,
    terminal_turn_outcome,
    turn_id_from_context,
)

if TYPE_CHECKING:
    from opensquilla.provider.types import ProviderRequestCorrelation

_SANDBOX_RUN_CONTEXT_ORIGIN_KEY = "sandbox_run_context"


@dataclass(frozen=True, slots=True)
class CanonicalTranscriptPage:
    """Bounded user-visible transcript page and archive coverage metadata."""

    entries: list[TranscriptEntry]
    has_more: bool
    canonical_complete: bool


@dataclass(frozen=True, slots=True)
class CompactionSourceSnapshot:
    """Frozen durable prefix that one in-turn compaction was derived from."""

    entries: tuple[TranscriptEntry, ...]
    preimage: tuple[tuple[Any, ...], ...]
    boundary_message_id: str | None
    boundary_entry_id: int | None


@dataclass(frozen=True, slots=True)
class _CompactionSingleflightKey:
    """Secret-free identity for one frozen durable compaction input."""

    session_key: str
    frozen_prefix_hash: str
    target_fingerprint: str


@dataclass(frozen=True, slots=True)
class _CompactionSingleflight:
    """One process-local owner task shared by equivalent waiters."""

    task: asyncio.Task[CompactionResult]
    operation_id: str


@dataclass(frozen=True, slots=True)
class _ForkTerminalOutcomeResolution:
    projections: dict[str, dict[str, Any]]
    active_turn_ids: frozenset[str]
    invalid_turn_ids: frozenset[str]


_COMPACTION_SINGLEFLIGHT_LOCK = threading.Lock()
_COMPACTION_SINGLEFLIGHTS: dict[
    tuple[asyncio.AbstractEventLoop, int, _CompactionSingleflightKey],
    _CompactionSingleflight,
] = {}


def _acquire_compaction_singleflight(
    key: _CompactionSingleflightKey,
    *,
    storage_scope: object,
    operation_id: str,
    owner_factory: Callable[[], Awaitable[CompactionResult]],
) -> tuple[_CompactionSingleflight, bool]:
    """Return the existing flight or atomically register a new owner.

    Asyncio tasks cannot be awaited across event loops, and independent storage
    instances must never share a rewrite owner. Those are implementation-level
    namespaces; the meaningful flight key remains session + frozen prefix +
    physical target fingerprint.
    """

    loop = asyncio.get_running_loop()
    registry_key = (loop, id(storage_scope), key)
    with _COMPACTION_SINGLEFLIGHT_LOCK:
        existing = _COMPACTION_SINGLEFLIGHTS.get(registry_key)
        if existing is not None and not existing.task.done():
            return existing, False

        async def run_owner() -> CompactionResult:
            return await owner_factory()

        flight = _CompactionSingleflight(
            task=loop.create_task(run_owner()),
            operation_id=operation_id,
        )
        _COMPACTION_SINGLEFLIGHTS[registry_key] = flight

    def discard(completed: asyncio.Task[CompactionResult]) -> None:
        with _COMPACTION_SINGLEFLIGHT_LOCK:
            current = _COMPACTION_SINGLEFLIGHTS.get(registry_key)
            if current is not None and current.task is completed:
                _COMPACTION_SINGLEFLIGHTS.pop(registry_key, None)

    flight.task.add_done_callback(discard)
    return flight, True


async def _await_compaction_commit_barrier(
    task: asyncio.Task[Any],
) -> tuple[Any, bool]:
    """Wait until an atomic rewrite settles, even if cancellation races it.

    Returns the task result and whether cancellation was observed. A successful
    durable commit is authoritative and therefore wins the race; if the rewrite
    fails, the pending cancellation is re-raised instead of claiming a false
    completion.
    """

    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    try:
        task.result()
    except BaseException as exc:
        if cancellation is not None:
            raise cancellation from exc
        raise
    return task.result(), cancellation is not None


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


def _fsync_directory(path: Path) -> None:
    """Make an atomically published POSIX directory entry durable."""

    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_archive_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "session"
    return safe[:64]


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _successful_submit_plan_input(
    segments: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Return the one successfully executed submit_plan input, if present."""

    if not segments:
        return None
    successful_ids: set[str] = set()
    for segment in segments:
        if (
            not isinstance(segment, dict)
            or segment.get("type") != "tool_result"
            or segment.get("name") != "submit_plan"
            or segment.get("is_error") is not False
        ):
            continue
        result = segment.get("result")
        try:
            payload = json.loads(result) if isinstance(result, str) else result
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("status") == "plan_submitted":
            successful_ids.add(str(segment.get("tool_use_id") or ""))

    submissions: list[dict[str, Any]] = []
    for segment in segments:
        if (
            not isinstance(segment, dict)
            or segment.get("type") != "tool_use"
            or segment.get("name") != "submit_plan"
            or str(segment.get("tool_use_id") or "") not in successful_ids
        ):
            continue
        submitted_input = segment.get("input")
        if isinstance(submitted_input, dict):
            submissions.append(submitted_input)
    if len(submissions) > 1:
        raise ValueError("A Plan turn may submit exactly one plan revision")
    return dict(submissions[0]) if submissions else None


def _compaction_entry_payloads(entries: list[TranscriptEntry]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for entry in entries:
        silent_reply = sanitize_historical_silent_reply(
            entry.content or "",
            entry.tool_calls,
            role=entry.role,
            turn_context=entry.turn_context,
        )
        # Preserve a strict one-to-one mapping with the durable preimage. The
        # compactor's removed_count and kept-entry suffix are positional, so a
        # fully suppressed row projects to empty content instead of being
        # deleted from this request-scoped view.
        payloads.append(
            {
                "id": entry.id,
                "message_id": entry.message_id,
                "role": entry.role,
                "content": silent_reply.content or "",
                "token_count": entry.token_count,
                "tool_calls": silent_reply.segments,
                "tool_call_id": entry.tool_call_id,
                "reasoning_content": entry.reasoning_content,
                "turn_usage": entry.turn_usage,
                "turn_context": entry.turn_context,
            }
        )
    return payloads


def _transcript_preimage(entries: list[TranscriptEntry]) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            entry.session_id,
            entry.session_key,
            entry.id,
            entry.message_id,
            entry.role,
            entry.content,
            entry.tool_call_id,
            entry.reasoning_content,
            entry.created_at,
            entry.token_count,
            entry.provenance_kind,
            entry.provenance_origin_session_id,
            entry.provenance_source_session_key,
            entry.provenance_source_channel,
            entry.provenance_source_tool,
            entry.schema_version,
            _stable_json(entry.tool_calls),
            _stable_json(entry.turn_usage),
            _stable_json(entry.turn_context),
        )
        for entry in entries
    )


def _compaction_target_fingerprint(config: CompactionConfig) -> str:
    """Hash the frozen physical target chain without retaining credentials."""

    plan = config.llm_plan
    if plan is not None:
        target_payload = {
            "max_calls": plan.max_calls,
            "candidates": [
                {
                    "deployment": candidate.deployment_fingerprint,
                    "context_window_tokens": candidate.context_window_tokens,
                    "max_output_tokens": candidate.max_output_tokens,
                    "provider_request_max_chars": candidate.provider_request_max_chars,
                }
                for candidate in plan.candidates
            ],
        }
    else:
        target_payload = {
            "deployment": compaction_deployment_fingerprint(
                provider=config.provider,
                model=str(config.model or ""),
                api_key=config.api_key,
                base_url=config.base_url,
            ),
        }
    return hashlib.sha256(_stable_json(target_payload).encode("utf-8")).hexdigest()


def _frozen_compaction_prefix_hash(
    *,
    preimage: tuple[tuple[Any, ...], ...],
    previous_summary: str,
    context_window_tokens: int,
    context_window_chars: int | None,
    custom_instructions: str | None,
    flush_receipt_status: str | None,
    config: CompactionConfig,
    consumer_admission_fingerprint: str = "",
) -> str:
    """Hash the frozen source plus output- and persistence-affecting settings."""

    digest = hashlib.sha256()
    for row in preimage:
        digest.update(_stable_json(row).encode("utf-8"))
        digest.update(b"\n")
    request_shape = {
        "previous_summary_sha256": hashlib.sha256(
            previous_summary.encode("utf-8")
        ).hexdigest(),
        "custom_instructions_sha256": hashlib.sha256(
            (custom_instructions or "").encode("utf-8")
        ).hexdigest(),
        "flush_receipt_status": flush_receipt_status,
        "context_window_tokens": context_window_tokens,
        "context_window_chars": context_window_chars,
        "base_chunk_ratio": config.base_chunk_ratio,
        "min_chunk_ratio": config.min_chunk_ratio,
        "safety_margin": config.safety_margin,
        "default_parts": config.default_parts,
        "identifier_policy": config.identifier_policy,
        "coverage_blocking": config.coverage_blocking,
        "compaction_profile": config.compaction_profile,
        "protected_recent_messages": config.protected_recent_messages,
        "consumer_admission_fingerprint": consumer_admission_fingerprint,
    }
    digest.update(_stable_json(request_shape).encode("utf-8"))
    return digest.hexdigest()


def _durable_summary_replay(summary: str) -> str:
    """Render one checkpoint as the request-context path will consume it."""

    rendered = format_compaction_summary_context([summary]) or ""
    # Existing request context, when present, is separated from the prepended
    # checkpoint by two newlines. Reserving them unconditionally is harmless
    # and keeps the no-existing-context path conservative.
    return f"{rendered}\n\n" if rendered else ""


def _branch_origin(parent_origin: Any) -> dict[str, Any] | None:
    if not isinstance(parent_origin, dict):
        return None
    sandbox_context = parent_origin.get(_SANDBOX_RUN_CONTEXT_ORIGIN_KEY)
    if not isinstance(sandbox_context, dict):
        return None
    return {_SANDBOX_RUN_CONTEXT_ORIGIN_KEY: dict(sandbox_context)}


def _entry_turn_id(entry: TranscriptEntry) -> str | None:
    return turn_id_from_context(entry.turn_context)


def _is_promoted_turn_entry(entry: TranscriptEntry) -> bool:
    return isinstance(entry.turn_context, dict) and (
        entry.turn_context.get("disposition") == "promoted"
    )


def _fork_material_references(
    entries: Sequence[TranscriptEntry],
) -> tuple[set[str], set[str]]:
    """Return attachment hashes and artifact ids reachable from copied rows."""

    attachment_hashes: set[str] = set()
    artifact_ids: set[str] = set()

    def _valid_sha256(value: Any) -> str | None:
        if not isinstance(value, str) or len(value) != 64:
            return None
        lowered = value.lower()
        if any(ch not in "0123456789abcdef" for ch in lowered):
            return None
        return lowered

    def _visit(value: Any, *, depth: int = 0) -> None:
        if isinstance(value, dict):
            sha = _valid_sha256(value.get("sha256_ref"))
            if sha is None and value.get("kind") == "attachment_ref":
                sha = _valid_sha256(value.get("sha256") or value.get("material_id"))
            if sha is not None:
                attachment_hashes.add(sha)

            artifact_id = value.get("id")
            if (
                isinstance(artifact_id, str)
                and artifact_id
                and (
                    value.get("kind") == "artifact_ref"
                    or value.get("store") == "artifacts"
                )
            ):
                artifact_ids.add(artifact_id)
            artifacts = value.get("artifacts")
            if isinstance(artifacts, list):
                for artifact in artifacts:
                    if isinstance(artifact, dict):
                        artifact_id = artifact.get("id")
                        if isinstance(artifact_id, str) and artifact_id:
                            artifact_ids.add(artifact_id)

            for item in value.values():
                _visit(item, depth=depth + 1)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                _visit(item, depth=depth + 1)
            return
        if isinstance(value, str):
            stripped = value.lstrip()
            if not stripped.startswith(("{", "[")):
                return
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                return
            _visit(parsed, depth=depth + 1)

    try:
        for entry in entries:
            _visit(entry.content)
            _visit(entry.tool_calls)
    except RecursionError as exc:
        raise ValueError(
            "Cannot fork history whose material-reference JSON is nested too deeply"
        ) from exc

    return attachment_hashes, artifact_ids


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
        # A reset starts a new task epoch. Collaboration state and its active
        # immutable plan belong to the archived epoch and must never leak into
        # the fresh transcript.
        reset.collaboration_mode = "default"
        reset.collaboration_revision = 0
        reset.active_plan_revision_id = None
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
        old_epoch = int(node.epoch or 0)
        reset = self._build_reset_node(node)

        async def archive_writer(snapshot: ResetArchiveSnapshot) -> None:
            await self.write_session_archive(
                snapshot.node,
                list(snapshot.entries),
                list(snapshot.summaries),
            )

        # The storage transaction takes the write lock before re-reading the
        # old identity and transcript. Appends committed before the lock are
        # archived; stale appends waiting behind it are fenced by the committed
        # epoch change. Cache and caller-visible state change only after commit.
        await self._storage.reset_session(
            reset,
            expected_session_id=old_session_id,
            expected_epoch=old_epoch,
            archive_writer=archive_writer,
        )
        self.set_cached_epoch(reset.session_key, int(reset.epoch or 0))
        return reset

    async def _archive_session_identity(self, node: SessionNode) -> None:
        """Persist the raw archive before a same-key transcript reset."""

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
            raise

    async def write_session_archive(
        self,
        node: SessionNode,
        entries: list[TranscriptEntry],
        summaries: list[SessionSummary],
    ) -> None:
        """Write a reset archive before destructive state changes commit."""

        if not entries and not summaries:
            return
        archive_dir = _archive_dir()
        native_archive_dir = native_io_path(archive_dir)
        new_dir = not native_archive_dir.exists()
        native_archive_dir.mkdir(parents=True, exist_ok=True)
        # Harden only the directory this boot creates (mirrors the DB
        # migrator policy): the archive holds the full raw transcript, so it
        # must not inherit the umask default of 0755/0644.
        if new_dir and os.name != "nt":
            with contextlib.suppress(OSError):
                os.chmod(native_archive_dir, 0o700)
        safe_key = _safe_archive_part(node.session_key)
        safe_id = _safe_archive_part(node.session_id)
        path = archive_dir / (f"{_now_ms()}-{safe_key}-{safe_id}-{uuid.uuid4().hex}.json")
        native_path = native_io_path(path)
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

        # Publish only a complete, flushed owner-only file. A failure leaves the
        # SQLite reset transaction untouched and the temporary file is removed.
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=os.fspath(native_archive_dir),
        )
        temporary_path = Path(temporary_name)
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
            fd = -1
            with handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, native_path)
            _fsync_directory(native_archive_dir)
        finally:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(temporary_path)

    async def resume(self, session_key: str) -> SessionNode:
        """Load an existing session; touch updated_at."""
        session_key = canonicalize_session_key(session_key)
        node = await self._storage.get_session(session_key)
        if node is None:
            raise KeyError(f"Session not found: {session_key}")
        node.updated_at = _now_ms()
        await self._storage.upsert_session(
            node,
            expected_session_id=node.session_id,
        )
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
        await self._storage.upsert_session(
            node,
            expected_session_id=node.session_id,
        )
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
        await self._storage.upsert_session(
            node,
            expected_session_id=node.session_id,
        )
        self.evict_session_runtime_state(
            session_key,
            session_id=node.session_id,
        )
        return node

    def evict_session_runtime_state(
        self,
        session_key: str,
        *,
        session_id: str | None = None,
    ) -> None:
        """Drop in-memory state owned by one retired ``session_key``.

        History deletion calls this while its runtime/admission fences are
        still held. ``session_id`` identifies the deleted generation for
        generation-scoped caches. Session-key caches are evicted regardless of
        whether that identifier is available. Imports are local to avoid cycles
        with engine/gateway packages.
        """
        session_key = canonicalize_session_key(session_key)
        self._epoch_cache.pop(session_key, None)
        goal_service = getattr(self._task_runtime, "goal_service", None)
        revoke_goal_lease = getattr(goal_service, "revoke_session", None)
        if callable(revoke_goal_lease):
            revoke_goal_lease(session_key, session_id=session_id)
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
        try:
            from opensquilla.engine.cache_break_monitor import (
                evict_cache_break_state,
            )

            evict_cache_break_state(session_key)
        except Exception:
            pass
        if session_id:
            try:
                from opensquilla.engine.steps.meta_resolution import (
                    evict_meta_sticky,
                )

                evict_meta_sticky(session_id)
            except Exception:
                pass

    async def _task_owner_is_verified_fork_ancestor(
        self,
        source: SessionNode,
        task_session_key: str,
    ) -> bool:
        """Accept task ownership only from the source or its live fork ancestry."""

        owner_key = canonicalize_session_key(task_session_key)
        current = source
        visited: set[str] = set()
        for _ in range(256):
            current_key = canonicalize_session_key(current.session_key)
            if current_key == owner_key:
                return True
            if current_key in visited:
                return False
            visited.add(current_key)
            if not current.forked_from_parent or not current.parent_session_key:
                return False
            parent_key = canonicalize_session_key(current.parent_session_key)
            if parent_key in visited:
                return False
            if not current.spawned_by or canonicalize_session_key(current.spawned_by) != parent_key:
                return False
            ancestor = await self._storage.get_session(parent_key)
            if ancestor is None:
                return False
            if int(ancestor.spawn_depth or 0) + 1 != int(current.spawn_depth or 0):
                return False
            current = ancestor
        return False

    @staticmethod
    def _has_child_owned_outcome_authority(source: SessionNode) -> bool:
        """Return whether this row is a durable, server-created fork identity."""

        if (
            not source.forked_from_parent
            or int(source.schema_version or 0) < CANONICAL_FORK_PROOF_SCHEMA_VERSION
            or int(source.spawn_depth or 0) <= 0
            or not source.parent_session_key
            or not source.spawned_by
        ):
            return False
        try:
            parent_key = canonicalize_session_key(source.parent_session_key)
            spawned_by = canonicalize_session_key(source.spawned_by)
        except (TypeError, ValueError):
            return False
        return parent_key == spawned_by

    async def _resolve_fork_terminal_outcomes(
        self,
        source: SessionNode,
        entries: Sequence[TranscriptEntry],
        *,
        target_session_id: str,
        target_session_key: str,
    ) -> _ForkTerminalOutcomeResolution:
        """Resolve terminal turns and bind their snapshots to the target child."""

        turn_ids = sorted(
            {
                turn_id
                for entry in entries
                if (turn_id := _entry_turn_id(entry)) is not None
            }
        )
        if not turn_ids:
            return _ForkTerminalOutcomeResolution({}, frozenset(), frozenset())

        source_projections: dict[str, dict[str, Any]] = {}
        invalid_turn_ids: set[str] = set()
        if self._has_child_owned_outcome_authority(source):
            for entry in entries:
                turn_id = _entry_turn_id(entry)
                if turn_id is None:
                    continue
                projection = extract_fork_terminal_outcome_projection(
                    entry.turn_context,
                    session_id=source.session_id,
                    session_key=source.session_key,
                    turn_id=turn_id,
                )
                if projection is None:
                    continue
                previous = source_projections.get(turn_id)
                if previous is not None and previous != projection:
                    source_projections.pop(turn_id, None)
                    invalid_turn_ids.add(turn_id)
                    continue
                if turn_id not in invalid_turn_ids:
                    source_projections[turn_id] = projection

        exact_tasks = getattr(self._storage, "get_agent_tasks_by_ids", None)
        get_task = getattr(self._storage, "get_agent_task", None)
        if callable(exact_tasks):
            rows = await exact_tasks(turn_ids)
        elif callable(get_task):
            rows = [
                row
                for turn_id in turn_ids
                if (row := await get_task(turn_id)) is not None
            ]
        else:
            rows = []
        tasks_by_id = {
            task_id: row
            for row in rows
            if isinstance((task_id := getattr(row, "task_id", None)), str)
            and task_id
        }

        projections: dict[str, dict[str, Any]] = {}
        active_turn_ids: set[str] = set()
        owner_checks: dict[str, bool] = {}
        for turn_id in turn_ids:
            row = tasks_by_id.get(turn_id)
            snapshot: dict[str, Any] | None = None
            if row is not None:
                task_session_key = getattr(row, "session_key", None)
                if not isinstance(task_session_key, str) or not task_session_key:
                    invalid_turn_ids.add(turn_id)
                    continue
                owner_verified = owner_checks.get(task_session_key)
                if owner_verified is None:
                    owner_verified = await self._task_owner_is_verified_fork_ancestor(
                        source,
                        task_session_key,
                    )
                    owner_checks[task_session_key] = owner_verified
                if not owner_verified:
                    # A deleted intermediate ancestor makes the live task owner
                    # unverifiable, but does not invalidate a snapshot already
                    # bound to this exact child identity.
                    snapshot = source_projections.get(turn_id)
                    if snapshot is None:
                        invalid_turn_ids.add(turn_id)
                        continue
                if snapshot is not None:
                    projections[turn_id] = build_fork_terminal_outcome_projection(
                        session_id=target_session_id,
                        session_key=target_session_key,
                        turn_id=turn_id,
                        task_id=str(snapshot["task_id"]),
                        status=str(snapshot["status"]),
                        started_at=snapshot.get("started_at"),
                        finished_at=snapshot.get("finished_at"),
                        outcome=snapshot["outcome"],
                    )
                    continue

                details = getattr(row, "details", None)
                details = details if isinstance(details, dict) else {}
                details_turn_id = details.get("turn_id")
                if (
                    isinstance(details_turn_id, str)
                    and details_turn_id.strip()
                    and details_turn_id.strip() != turn_id
                ):
                    invalid_turn_ids.add(turn_id)
                    continue
                status = getattr(row, "status", None)
                status = str(getattr(status, "value", status) or "")
                outcome = terminal_turn_outcome(status, details.get("turn_outcome"))
                if outcome is None:
                    active_turn_ids.add(turn_id)
                    continue
                snapshot = {
                    "turn_id": turn_id,
                    "task_id": str(getattr(row, "task_id", turn_id)),
                    "status": status,
                    "started_at": getattr(row, "started_at", None),
                    "finished_at": getattr(row, "finished_at", None),
                    "outcome": outcome,
                }
            elif turn_id not in invalid_turn_ids:
                snapshot = source_projections.get(turn_id)

            if snapshot is None:
                continue
            projections[turn_id] = build_fork_terminal_outcome_projection(
                session_id=target_session_id,
                session_key=target_session_key,
                turn_id=turn_id,
                task_id=str(snapshot["task_id"]),
                status=str(snapshot["status"]),
                started_at=snapshot.get("started_at"),
                finished_at=snapshot.get("finished_at"),
                outcome=snapshot["outcome"],
            )

        return _ForkTerminalOutcomeResolution(
            projections=projections,
            active_turn_ids=frozenset(active_turn_ids),
            invalid_turn_ids=frozenset(invalid_turn_ids),
        )

    async def branch(
        self,
        parent_session_key: str,
        new_session_key: str,
        fork_transcript: bool = False,
        max_fork_tokens: int | None = None,
        status: SessionStatus | str = SessionStatus.RUNNING,
        fork_before_message_id: str | None = None,
        fork_through_turn_id: str | None = None,
        display_name: str | None = None,
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
                fork_through_turn_id=fork_through_turn_id,
                display_name=display_name,
            )

    async def _branch_locked(
        self,
        parent_session_key: str,
        new_session_key: str,
        fork_transcript: bool = False,
        max_fork_tokens: int | None = None,
        status: SessionStatus | str = SessionStatus.RUNNING,
        fork_before_message_id: str | None = None,
        fork_through_turn_id: str | None = None,
        display_name: str | None = None,
    ) -> SessionNode:
        """
        Create a child session branched from parent.
        If fork_transcript=True and parent token budget permits, copy parent transcript
        as initial context in the child (forkedFromParent flag set).
        If fork_before_message_id is set, copy only the canonical transcript prefix
        before that message and skip parent compaction summaries/context states.
        If fork_through_turn_id is set, copy the complete canonical prefix through
        that terminal turn (inclusive) and likewise skip summaries/context states.
        ``status`` sets the child's initial lifecycle status; pass a resting
        status such as ``SessionStatus.DONE`` when the child should not appear
        as an active run until its first turn starts.
        """
        parent_session_key = canonicalize_session_key(parent_session_key)
        new_session_key = canonicalize_session_key(new_session_key)
        if fork_through_turn_id is not None and not fork_through_turn_id.strip():
            raise ValueError("fork_through_turn_id must not be empty")
        fork_through_turn_id = (fork_through_turn_id or "").strip() or None
        if fork_before_message_id and fork_through_turn_id:
            raise ValueError(
                "fork_before_message_id and fork_through_turn_id are mutually exclusive"
            )
        if fork_through_turn_id and not fork_transcript:
            raise ValueError("fork_through_turn_id requires fork_transcript=True")
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
            display_name=display_name,
            origin=_branch_origin(parent.origin),
            workspace_id=parent.workspace_id,
        )

        if fork_transcript:
            is_prefix_fork = bool(fork_before_message_id or fork_through_turn_id)
            parent_coverage = await self._storage.get_canonical_transcript_coverage(
                parent.session_id
            )
            parent_canonical_complete = parent_coverage.canonical_complete
            parent_compaction_count = parent_coverage.compaction_count
            parent_summaries: list[SessionSummary]
            parent_context_states: list[SessionContextState]
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
                outcome_entries = parent_entries
                parent_summaries = []
                parent_context_states = []
            elif fork_through_turn_id:
                if not parent_canonical_complete:
                    raise ValueError(
                        "Cannot fork through a turn because canonical transcript history "
                        "is incomplete"
                    )
                canonical_entries = await self._storage.get_canonical_transcript(parent.session_id)
                matching_indexes = [
                    index
                    for index, entry in enumerate(canonical_entries)
                    if _entry_turn_id(entry) == fork_through_turn_id
                ]
                if not matching_indexes:
                    raise KeyError(
                        f"Transcript turn not found in {parent_session_key}: "
                        f"{fork_through_turn_id}"
                    )
                first_index = matching_indexes[0]
                last_index = matching_indexes[-1]
                # The inclusive boundary is the last row with positive causal
                # ownership by this turn. Trailing rows without a turn id are
                # deliberately excluded; assigning them by physical adjacency
                # would make legacy/system maintenance rows leak across turns.
                earlier_turn_ids = {
                    turn_id
                    for entry in canonical_entries[:first_index]
                    if (turn_id := _entry_turn_id(entry)) is not None
                }
                parent_entries = []
                for index, entry in enumerate(canonical_entries[: last_index + 1]):
                    turn_id = _entry_turn_id(entry)
                    if first_index < index < last_index and turn_id is None:
                        raise ValueError(
                            "Cannot fork through a turn with unscoped canonical rows"
                        )
                    if index >= first_index and turn_id not in {
                        None,
                        fork_through_turn_id,
                    }:
                        # A promoted input is persisted while its predecessor is
                        # still producing output. Its physical position is before
                        # the predecessor's terminal row, but causally it belongs
                        # to a later turn and must not leak into that earlier fork.
                        if _is_promoted_turn_entry(entry):
                            continue
                        # Rows from a turn already underway before the selected
                        # promoted input are earlier causal history and remain in
                        # the prefix. A newly appearing unrelated turn is
                        # ambiguous, so reject instead of guessing its order.
                        if turn_id not in earlier_turn_ids:
                            raise ValueError(
                                "Cannot fork through a turn with interleaved canonical rows"
                            )
                    parent_entries.append(entry)
                outcome_entries = parent_entries
                parent_summaries = []
                parent_context_states = []
            else:
                parent_entries = await self._storage.get_transcript(parent.session_id)
                outcome_entries = await self._storage.get_canonical_transcript(
                    parent.session_id
                )
                parent_summaries = await self._storage.get_all_summaries(parent.session_id)
                parent_context_states = await self._storage.get_context_states(parent_session_key)
            outcome_resolution = await self._resolve_fork_terminal_outcomes(
                parent,
                outcome_entries,
                target_session_id=child.session_id,
                target_session_key=new_session_key,
            )
            if fork_through_turn_id in outcome_resolution.active_turn_ids:
                raise ValueError(
                    f"Cannot fork through active transcript turn: {fork_through_turn_id}"
                )
            if (
                fork_through_turn_id is not None
                and fork_through_turn_id not in outcome_resolution.projections
            ):
                raise KeyError(
                    f"Completion state not found for transcript turn: "
                    f"{fork_through_turn_id}"
                )
            summary_tokens = sum(
                estimate_tokens(summary.summary_text) for summary in parent_summaries
            )
            parent_tokens = sum(e.token_count or 0 for e in parent_entries) + summary_tokens
            if max_fork_tokens is None or parent_tokens <= max_fork_tokens:
                material_references = (
                    _fork_material_references(parent_entries)
                    if fork_through_turn_id
                    else None
                )
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
                        terminal_outcome_projections=outcome_resolution.projections,
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
                        turn_context=attach_fork_terminal_outcome_projection(
                            entry.turn_context,
                            outcome_resolution.projections.get(_entry_turn_id(entry) or ""),
                        ),
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
                    parent.session_id,
                    child.session_id,
                    new_session_key,
                    material_references=material_references,
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
            origin=_branch_origin(parent.origin),
            workspace_id=parent.workspace_id,
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
        prefix_entries = canonical_entries[:fork_index]
        outcome_resolution = await self._resolve_fork_terminal_outcomes(
            parent,
            prefix_entries,
            target_session_id=child.session_id,
            target_session_key=new_session_key,
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
                turn_context=attach_fork_terminal_outcome_projection(
                    entry.turn_context,
                    outcome_resolution.projections.get(_entry_turn_id(entry) or ""),
                ),
                created_at=entry.created_at,
                token_count=entry.token_count,
                provenance_kind=entry.provenance_kind,
                provenance_origin_session_id=entry.provenance_origin_session_id,
                provenance_source_session_key=entry.provenance_source_session_key,
                provenance_source_channel=entry.provenance_source_channel,
                provenance_source_tool=entry.provenance_source_tool,
            )
            for entry in prefix_entries
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
        *,
        material_references: tuple[set[str], set[str]] | None = None,
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

        attachment_hashes: set[str] | None = None
        artifact_ids: set[str] | None = None
        if material_references is not None:
            attachment_hashes, artifact_ids = material_references

        def _run() -> None:
            from opensquilla.artifacts import ArtifactStore
            from opensquilla.attachment_refs import copy_transcript_material

            ArtifactStore(media_root).copy_session_artifacts(
                source_session_id=source_session_id,
                target_session_id=target_session_id,
                target_session_key=target_session_key,
                artifact_ids=artifact_ids,
            )
            copy_transcript_material(
                media_root=media_root,
                source_session_id=source_session_id,
                target_session_id=target_session_id,
                material_ids=attachment_hashes,
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

    @staticmethod
    def reserve_assistant_message(
        turn_id: str,
        assistant_message_id: str,
        session_key: str,
        channel_id: str | None = None,
    ) -> AssistantMessageReservation:
        """Reserve an assistant identity without creating a transcript row.

        The reservation is intentionally just a value object.  Materialization
        happens on the first visible/final assistant write using the same id.
        """

        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ValueError("turn_id must be a non-empty string")
        if not isinstance(assistant_message_id, str) or not assistant_message_id.strip():
            raise ValueError("assistant_message_id must be a non-empty string")
        return AssistantMessageReservation(
            turn_id=turn_id,
            assistant_message_id=assistant_message_id,
            session_key=canonicalize_session_key(session_key),
            channel_id=channel_id,
        )

    async def prepare_message(
        self,
        session_key: str,
        role: str,
        content: str,
        *,
        message_id: str | None = None,
        assistant_message_id: str | None = None,
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

        if message_id is not None and assistant_message_id is not None:
            if message_id != assistant_message_id:
                raise ValueError("message_id and assistant_message_id must match")
        message_id = assistant_message_id or message_id
        content = self._maybe_stamp_user_message(role, content)

        if message_id is not None and (
            not isinstance(message_id, str) or not message_id.strip()
        ):
            raise ValueError("message_id must be a non-empty string when supplied")

        if turn_context is None:
            from opensquilla.session.turn_context import current_turn_context

            turn_context = current_turn_context()

        entry = TranscriptEntry(
            session_id=node.session_id,
            session_key=session_key,
            message_id=message_id or str(uuid.uuid4()),
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
        message_id: str | None = None,
        assistant_message_id: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        reasoning_content: str | None = None,
        turn_usage: dict[str, Any] | None = None,
        token_count: int | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> TranscriptEntry:
        """Append a message and narrowly touch its session in one transaction."""

        if message_id is not None and assistant_message_id is not None:
            if message_id != assistant_message_id:
                raise ValueError("message_id and assistant_message_id must match")
        message_id = assistant_message_id or message_id
        entry, expected_epoch = await self.prepare_message(
            session_key,
            role,
            content,
            message_id=message_id,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            reasoning_content=reasoning_content,
            turn_usage=turn_usage,
            token_count=token_count,
            provenance=provenance,
        )
        token_delta = token_count if token_count and turn_usage is None else 0
        submitted_plan = (
            _successful_submit_plan_input(tool_calls)
            if role == "assistant"
            else None
        )
        if submitted_plan is None:
            if message_id is None:
                await self._storage.append_transcript_entry_and_touch(
                    entry,
                    expected_epoch=expected_epoch,
                    updated_at=_now_ms(),
                    token_delta=token_delta,
                    mark_total_tokens_stale=bool(token_delta),
                )
            else:
                await self._storage.upsert_transcript_entry_and_touch(
                    entry,
                    expected_epoch=expected_epoch,
                    updated_at=_now_ms(),
                    token_delta=token_delta,
                    mark_total_tokens_stale=bool(token_delta),
                )
        else:
            node = await self._storage.get_session(session_key)
            if node is None:
                raise KeyError(f"Session not found: {session_key}")
            if node.epoch != expected_epoch:
                raise RuntimeError("Session changed before plan submission")
            parent_revision_id = node.active_plan_revision_id
            parent = (
                await self._storage.get_plan_revision(parent_revision_id)
                if parent_revision_id
                else None
            )
            if parent_revision_id and parent is None:
                raise RuntimeError("Active plan revision no longer exists")
            from opensquilla.session.plans import new_plan_revision

            submitted_title = submitted_plan.get("title")
            submitted_markdown = submitted_plan.get("markdown")
            submitted_steps = submitted_plan.get("steps")
            if not isinstance(submitted_title, str):
                raise ValueError("submit_plan title must be a string")
            if not isinstance(submitted_markdown, str):
                raise ValueError("submit_plan markdown must be a string")
            if not isinstance(submitted_steps, list):
                raise ValueError("submit_plan steps must be an array")
            revision = new_plan_revision(
                source_session_key=entry.session_key,
                source_session_id=entry.session_id,
                source_epoch=expected_epoch,
                parent=parent,
                source_turn_id=(
                    str(entry.turn_context.get("turn_id"))
                    if isinstance(entry.turn_context, dict)
                    and entry.turn_context.get("turn_id")
                    else None
                ),
                source_message_id=entry.message_id,
                title=submitted_title,
                markdown=submitted_markdown,
                steps=submitted_steps,
            )
            from opensquilla.session.plans import plan_revision_snapshot

            entry.tool_calls = [
                *(entry.tool_calls or []),
                {
                    "type": "plan",
                    "snapshot": plan_revision_snapshot(revision, current=True),
                },
            ]
            entry.turn_context = {
                **(entry.turn_context or {}),
                "plan_revision_id": revision.revision_id,
                "plan_parent_revision_id": parent_revision_id,
            }
            await self._storage.append_plan_revision(
                entry,
                revision,
                expected_epoch=expected_epoch,
                expected_parent_revision_id=parent_revision_id,
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

    async def capture_compaction_source(
        self,
        session_key: str,
        *,
        boundary_message_id: str | None = None,
    ) -> CompactionSourceSnapshot:
        """Freeze the exact durable prefix visible to the active turn.

        A bound user message cuts off already-persisted queued prompts.  An
        empty snapshot is an explicit fail-closed marker when the requested
        boundary cannot be found.
        """

        transcript = await self.get_transcript(session_key)
        source_entries = transcript
        if boundary_message_id is not None:
            boundary_index = next(
                (
                    index
                    for index, entry in enumerate(transcript)
                    if entry.message_id == boundary_message_id
                ),
                None,
            )
            source_entries = (
                transcript[: boundary_index + 1]
                if boundary_index is not None
                else []
            )
        boundary = source_entries[-1] if source_entries else None
        return CompactionSourceSnapshot(
            entries=tuple(source_entries),
            preimage=_transcript_preimage(source_entries),
            boundary_message_id=boundary.message_id if boundary is not None else None,
            boundary_entry_id=boundary.id if boundary is not None else None,
        )

    async def record_memory_checkpoint(
        self,
        session_key: str,
        transcript: list[TranscriptEntry] | None = None,
        *,
        turn_id: str | None = None,
        source: str = "session_manager",
        compaction_config: CompactionConfig | None = None,
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
        node_call = self._storage.get_session(session_key)
        node = (
            await await_compaction_phase(
                node_call,
                compaction_config,
                phase="checkpointing",
            )
            if compaction_config is not None
            else await node_call
        )
        if node is None:
            raise KeyError(f"Session not found: {session_key}")
        if transcript is not None:
            entries = list(transcript)
        else:
            entries_call = self._storage.get_transcript(node.session_id)
            entries = (
                await await_compaction_phase(
                    entries_call,
                    compaction_config,
                    phase="checkpointing",
                )
                if compaction_config is not None
                else await entries_call
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
        checkpoint_started = time.monotonic()
        try:
            if workspace is None:
                raise RuntimeError("checkpoint workspace_dir is not configured")
            checkpoint_worker = asyncio.create_task(
                asyncio.to_thread(append_checkpoint_events, workspace, events)
            )

            def _consume_late_checkpoint(done: asyncio.Task[Any]) -> None:
                with contextlib.suppress(BaseException):
                    done.result()

            checkpoint_worker.add_done_callback(_consume_late_checkpoint)
            if compaction_config is None:
                result = await checkpoint_worker
            else:
                result = await await_compaction_phase(
                    asyncio.shield(checkpoint_worker),
                    compaction_config,
                    phase="checkpointing",
                )
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
                persist_failure = self._storage.upsert_memory_durable_receipt(
                    receipt,
                    expected_session_id=node.session_id,
                )
                if compaction_config is None:
                    await persist_failure
                elif (compaction_remaining_seconds(compaction_config) or 0.0) > 0:
                    await await_compaction_phase(
                        persist_failure,
                        compaction_config,
                        phase="checkpointing",
                    )
                else:
                    close = getattr(persist_failure, "close", None)
                    if callable(close):
                        close()
            except Exception:
                pass
            import structlog as _structlog

            _structlog.get_logger(__name__).warning(
                "session_compaction.checkpoint_failed",
                compaction_id=resolved_turn_id,
                duration_ms=max(0, int((time.monotonic() - checkpoint_started) * 1000)),
                error=type(exc).__name__,
            )
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
        persisted_call = self._storage.upsert_memory_durable_receipt(
            receipt,
            expected_session_id=node.session_id,
        )
        persisted = (
            await await_compaction_phase(
                persisted_call,
                compaction_config,
                phase="checkpointing",
            )
            if compaction_config is not None
            else await persisted_call
        )
        import structlog as _structlog

        _structlog.get_logger(__name__).info(
            "session_compaction.checkpoint_completed",
            compaction_id=resolved_turn_id,
            duration_ms=max(0, int((time.monotonic() - checkpoint_started) * 1000)),
        )
        return persisted

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
        context_window_chars: int | None = None,
        mutation_context: Callable[[], contextlib.AbstractAsyncContextManager[None]] | None = None,
        provider_request_correlation: ProviderRequestCorrelation | None = None,
        consumer_admission: Callable[[str, list[dict[str, Any]]], Any] | None = None,
        consumer_admission_fingerprint: str = "",
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
            context_window_chars=context_window_chars,
            mutation_context=mutation_context,
            consumer_admission=consumer_admission,
            consumer_admission_fingerprint=consumer_admission_fingerprint,
            **correlation_kwargs,
        )
        return (
            result.summary
            if result.removed_count or result.replaced_previous_summary
            else ""
        )

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
        context_window_chars: int | None = None,
        mutation_context: Callable[[], contextlib.AbstractAsyncContextManager[None]] | None = None,
        provider_request_correlation: ProviderRequestCorrelation | None = None,
        consumer_admission: Callable[[str, list[dict[str, Any]]], Any] | None = None,
        consumer_admission_fingerprint: str = "",
    ) -> CompactionResult:
        """Compact the session transcript and return full compaction metadata."""

        session_key = canonicalize_session_key(session_key)
        # Runtime counters/deadlines belong to one logical operation. Public
        # callers may reuse a config object, so isolate it before arming; a
        # concurrent waiter must never reset the owner's deadline or call cap.
        effective_config = replace(config) if config is not None else CompactionConfig()
        persisted_compaction_id = compaction_id or new_compaction_id()
        arm_compaction_deadline(
            effective_config,
            operation_id=persisted_compaction_id,
        )
        require_compaction_time(effective_config, phase="snapshotting")
        async with _session_mutation_context(mutation_context):
            node = await await_compaction_phase(
                self._storage.get_session(session_key),
                effective_config,
                phase="snapshotting",
            )
            if node is None:
                raise KeyError(f"Session not found: {session_key}")

            entries = await await_compaction_phase(
                self._storage.get_transcript(node.session_id),
                effective_config,
                phase="snapshotting",
            )
            summaries = await await_compaction_phase(
                self._storage.get_all_summaries(node.session_id),
                effective_config,
                phase="snapshotting",
            )
            context_states = await await_compaction_phase(
                self._storage.get_context_states(session_key),
                effective_config,
                phase="snapshotting",
            )
            previous_context_records = build_compaction_context_records(
                context_states=context_states,
                summaries=summaries,
            )
            previous_summary = "\n\n".join(
                record.text.strip()
                for record in previous_context_records
                if record.text.strip()
            )
            previous_covered_through_id = max(
                (
                    int(record.covered_through_id or 0)
                    for record in previous_context_records
                ),
                default=0,
            )
            previous_context_fingerprint = compaction_context_fingerprint(
                context_states=context_states,
                summaries=summaries,
            )
            preimage = _transcript_preimage(entries)
            raw = _compaction_entry_payloads(entries)

        singleflight_key = _CompactionSingleflightKey(
            session_key=session_key,
            frozen_prefix_hash=_frozen_compaction_prefix_hash(
                preimage=preimage,
                previous_summary=previous_summary,
                context_window_tokens=context_window_tokens,
                context_window_chars=context_window_chars,
                custom_instructions=custom_instructions,
                flush_receipt_status=flush_receipt_status,
                config=effective_config,
                consumer_admission_fingerprint=consumer_admission_fingerprint,
            ),
            target_fingerprint=_compaction_target_fingerprint(effective_config),
        )
        flight, is_owner = _acquire_compaction_singleflight(
            singleflight_key,
            storage_scope=self._storage,
            operation_id=persisted_compaction_id,
            owner_factory=lambda: self._compact_snapshot_with_result(
                session_key=session_key,
                node=node,
                entries=entries,
                previous_summary=previous_summary,
                previous_context_fingerprint=previous_context_fingerprint,
                previous_covered_through_id=previous_covered_through_id,
                preimage=preimage,
                raw=raw,
                context_window_tokens=context_window_tokens,
                context_window_chars=context_window_chars,
                effective_config=effective_config,
                custom_instructions=custom_instructions,
                persisted_compaction_id=persisted_compaction_id,
                trigger_reason=trigger_reason,
                flush_receipt_status=flush_receipt_status,
                mutation_context=mutation_context,
                provider_request_correlation=provider_request_correlation,
                consumer_admission=consumer_admission,
            ),
        )
        if is_owner:
            return await flight.task

        import structlog as _structlog

        _structlog.get_logger(__name__).info(
            "session_compaction.singleflight_joined",
            compaction_id=persisted_compaction_id,
            owner_compaction_id=flight.operation_id,
            session_key=session_key,
            frozen_prefix_hash=singleflight_key.frozen_prefix_hash,
            target_fingerprint=singleflight_key.target_fingerprint,
        )
        return await await_compaction_phase(
            asyncio.shield(flight.task),
            effective_config,
            phase="singleflight_waiting",
        )

    async def _compact_snapshot_with_result(
        self,
        *,
        session_key: str,
        node: SessionNode,
        entries: list[TranscriptEntry],
        previous_summary: str,
        previous_context_fingerprint: str,
        previous_covered_through_id: int,
        preimage: tuple[tuple[Any, ...], ...],
        raw: list[dict[str, Any]],
        context_window_tokens: int,
        context_window_chars: int | None,
        effective_config: CompactionConfig,
        custom_instructions: str | None,
        persisted_compaction_id: str,
        trigger_reason: str | None,
        flush_receipt_status: str | None,
        mutation_context: Callable[[], contextlib.AbstractAsyncContextManager[None]] | None,
        provider_request_correlation: ProviderRequestCorrelation | None,
        consumer_admission: Callable[[str, list[dict[str, Any]]], Any] | None,
    ) -> CompactionResult:
        """Generate and atomically install one frozen compaction candidate."""

        result = await compact_context(
            CompactionRequest(
                session_id=node.session_id,
                entries=raw,
                context_window_tokens=context_window_tokens,
                context_window_chars=context_window_chars,
                config=effective_config,
                custom_instructions=custom_instructions,
                previous_summary=previous_summary or None,
                summary_replay_renderer=_durable_summary_replay,
                consumer_admission=consumer_admission,
                provider_request_correlation=provider_request_correlation,
            )
        )

        if result.removed_count == 0 and not result.replaced_previous_summary:
            return result
        if not result.summary:
            import structlog as _structlog

            _structlog.get_logger(__name__).warning(
                "session_compaction.empty_summary_not_persisted",
                session_key=session_key,
                removed_count=result.removed_count,
            )
            return replace(result, skip_reason=result.skip_reason or "empty_summary")

        require_compaction_time(effective_config, phase="validating")
        from opensquilla.session.compaction import (
            compaction_replay_summary,
            consumer_admission_accepts,
        )

        if not consumer_admission_accepts(
            consumer_admission,
            compaction_replay_summary(result),
            result.kept_entries,
        ):
            return replace(
                result,
                summary="",
                kept_entries=raw,
                removed_count=0,
                replaced_previous_summary=False,
                chunks_processed=0,
                summary_source="skipped",
                skip_reason="consumer_admission_stale_or_failed",
                tokens_after=result.tokens_before,
                remaining_budget_tokens=max(
                    context_window_tokens - result.tokens_before,
                    0,
                ),
            )
        async with _session_mutation_context(mutation_context):
            current_node = await await_compaction_phase(
                self._storage.get_session(session_key),
                effective_config,
                phase="validating",
            )
            if current_node is None:
                raise KeyError(f"Session not found: {session_key}")
            current_entries = await await_compaction_phase(
                self._storage.get_transcript(current_node.session_id),
                effective_config,
                phase="validating",
            )
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
                    replaced_previous_summary=False,
                    chunks_processed=0,
                    summary_source="skipped",
                    skip_reason="stale_preimage",
                    tokens_after=result.tokens_before,
                    remaining_budget_tokens=max(
                        context_window_tokens - result.tokens_before,
                        0,
                    ),
                )

            current_summaries = await await_compaction_phase(
                self._storage.get_all_summaries(current_node.session_id),
                effective_config,
                phase="validating",
            )
            current_context_states = await await_compaction_phase(
                self._storage.get_context_states(session_key),
                effective_config,
                phase="validating",
            )
            current_context_fingerprint = compaction_context_fingerprint(
                context_states=current_context_states,
                summaries=current_summaries,
            )
            if current_context_fingerprint != previous_context_fingerprint:
                import structlog as _structlog

                _structlog.get_logger(__name__).warning(
                    "session_compaction.stale_context_state_skipped",
                    session_key=session_key,
                )
                return replace(
                    result,
                    summary="",
                    kept_entries=_compaction_entry_payloads(current_entries),
                    removed_count=0,
                    replaced_previous_summary=False,
                    chunks_processed=0,
                    summary_source="skipped",
                    skip_reason="stale_context_state",
                    tokens_after=result.tokens_before,
                    remaining_budget_tokens=max(
                        context_window_tokens - result.tokens_before,
                        0,
                    ),
                )

            removed_entries = current_entries[: len(current_entries) - len(result.kept_entries)]
            kept_entries = current_entries[len(removed_entries) :]
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
                covered_through_id=max(
                    previous_covered_through_id,
                    max((entry.id or 0) for entry in removed_entries)
                    if removed_entries
                    else 0,
                ),
            )
            current_node.compaction_count = (current_node.compaction_count or 0) + 1
            current_node.updated_at = _now_ms()
            context_state = self._portable_structured_summary_state(
                current_node,
                summary_record,
            )
            # Cancellation/deadline wins until this point. Once the atomic
            # SQLite rewrite starts, wait for its real outcome so a committed
            # summary can never be reported as cancelled.
            require_compaction_time(effective_config, phase="committing")
            commit_started = time.monotonic()
            import structlog as _structlog

            _structlog.get_logger(__name__).info(
                "session_compaction.commit_started",
                compaction_id=persisted_compaction_id,
            )
            boundary = current_entries[-1] if current_entries else None
            commit_task = asyncio.create_task(
                self._storage.rewrite_compacted_session(
                    node=current_node,
                    summary=summary_record,
                    entries=kept_entries,
                    context_states=[context_state] if context_state is not None else None,
                    archived_entries=removed_entries,
                    expected_source_entries=current_entries,
                    expected_source_preimage=preimage,
                    expected_source_boundary_message_id=(
                        boundary.message_id if boundary is not None else None
                    ),
                    expected_source_boundary_entry_id=(
                        boundary.id if boundary is not None else None
                    ),
                    expected_context_fingerprint=previous_context_fingerprint,
                )
            )
            installed, cancellation_reconciled = await _await_compaction_commit_barrier(
                commit_task
            )
            if installed is False:
                return replace(
                    result,
                    summary="",
                    kept_entries=_compaction_entry_payloads(current_entries),
                    removed_count=0,
                    replaced_previous_summary=False,
                    chunks_processed=0,
                    summary_source="skipped",
                    skip_reason="stale_preimage",
                    tokens_after=result.tokens_before,
                    remaining_budget_tokens=max(
                        context_window_tokens - result.tokens_before,
                        0,
                    ),
                )
            _structlog.get_logger(__name__).info(
                "session_compaction.commit_completed",
                compaction_id=persisted_compaction_id,
                cancellation_reconciled=cancellation_reconciled,
                duration_ms=max(0, int((time.monotonic() - commit_started) * 1000)),
            )
        return result

    async def persist_compaction_result(
        self,
        session_key: str,
        summary: str,
        kept_entries: list[dict],
        *,
        summary_payload: dict[str, Any] | None = None,
        summary_format: str = "text",
        coverage_status: str = "unknown",
        missing_obligations: list[str] | None = None,
        critical_carry_forward: list[str] | None = None,
        compaction_id: str | None = None,
        trigger_reason: str | None = None,
        flush_receipt_status: str | None = None,
        compaction_deadline_at_monotonic: float | None = None,
        compaction_timeout_seconds: float | None = None,
        removed_count: int | None = None,
        source_entries: Sequence[TranscriptEntry] | None = None,
        source_preimage: Sequence[Sequence[Any]] | None = None,
        source_boundary_message_id: str | None = None,
        source_boundary_entry_id: int | None = None,
    ) -> bool:
        """Persist a pre-computed compaction result directly (no LLM re-compaction).

        Called by TurnRunner when Agent emits CompactionEvent. Writes the Agent's
        actual compaction output to DB, avoiding the double-compaction bug that
        would occur if we called compact() (which re-reads DB and re-runs LLM).
        """
        session_key = canonicalize_session_key(session_key)
        import structlog as _structlog

        _log = _structlog.get_logger(__name__)
        persisted_compaction_id = compaction_id or new_compaction_id()
        deadline_config: CompactionConfig | None = None
        if compaction_deadline_at_monotonic is not None:
            try:
                deadline = float(compaction_deadline_at_monotonic)
                total_timeout = float(compaction_timeout_seconds or 120.0)
            except (TypeError, ValueError):
                deadline = 0.0
                total_timeout = 120.0
            deadline_config = CompactionConfig(
                total_timeout_seconds=total_timeout if total_timeout > 0 else 120.0,
                deadline_at_monotonic=deadline,
                operation_id=persisted_compaction_id,
            )
            require_compaction_time(deadline_config, phase="snapshotting")

        node_call = self._storage.get_session(session_key)
        node = (
            await await_compaction_phase(
                node_call,
                deadline_config,
                phase="snapshotting",
            )
            if deadline_config is not None
            else await node_call
        )
        if node is None:
            _log.warning("persist_compaction.session_not_found", session_key=session_key)
            return False

        expected_source_entries: list[TranscriptEntry] | None = None
        expected_source_preimage: tuple[tuple[Any, ...], ...] | None = None
        if source_entries is not None:
            expected_source_entries = list(source_entries)
            expected_source_preimage = tuple(tuple(item) for item in (source_preimage or ()))
            actual_source_preimage = _transcript_preimage(expected_source_entries)
            boundary = expected_source_entries[-1] if expected_source_entries else None
            source_is_valid = (
                bool(expected_source_entries)
                and expected_source_preimage == actual_source_preimage
                and boundary is not None
                and (
                    source_boundary_message_id is None
                    or boundary.message_id == source_boundary_message_id
                )
                and (
                    source_boundary_entry_id is None
                    or boundary.id == source_boundary_entry_id
                )
                and isinstance(removed_count, int)
                and not isinstance(removed_count, bool)
                and 0 <= removed_count <= len(expected_source_entries)
            )
            if not source_is_valid:
                _log.warning(
                    "persist_compaction.invalid_source_boundary_skipped",
                    session_key=session_key,
                    source_count=len(expected_source_entries),
                    removed_count=removed_count,
                )
                return False
            assert isinstance(removed_count, int) and not isinstance(removed_count, bool)
            exact_removed_count = removed_count
            removed_entries = expected_source_entries[:exact_removed_count]
            preserved_entries = expected_source_entries[exact_removed_count:]
        else:
            # Compatibility path for older embedders that do not yet carry a
            # frozen source boundary. Keep its historical behavior, while all
            # TurnRunner calls use the exact-boundary branch above.
            entries_call = self._storage.get_transcript(node.session_id)
            entries = (
                await await_compaction_phase(
                    entries_call,
                    deadline_config,
                    phase="snapshotting",
                )
                if deadline_config is not None
                else await entries_call
            )
            removed_entries = entries[: max(0, len(entries) - len(kept_entries))]
            preserved_entries = entries[len(removed_entries) :]
        if removed_entries and not summary:
            _log.warning(
                "persist_compaction.empty_summary_not_persisted",
                session_key=session_key,
                removed=len(removed_entries),
                kept=len(kept_entries),
            )
            return False

        persisted_kept_count = (
            len(preserved_entries)
            if expected_source_entries is not None
            else len(kept_entries)
        )

        # Store summary out-of-band. New compactions must not prepend a
        # transcript system marker because history loading would make that
        # marker provider-visible and cache-hostile.
        summary_record = None
        if summary:
            if deadline_config is not None:
                require_compaction_time(deadline_config, phase="validating")
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
            if summary_format == "structured_v1" and summary_payload is not None:
                structured_summary = StructuredCompactionSummary.model_validate(
                    summary_payload
                )
                resolved_coverage_status = coverage_status
                resolved_missing_obligations = list(missing_obligations or ())
                resolved_critical_carry_forward = list(
                    critical_carry_forward
                    if critical_carry_forward is not None
                    else structured_summary.critical_carry_forward
                )
            else:
                obligations = extract_compaction_obligations(raw_removed_entries)
                structured_summary, coverage = build_structured_summary_from_text(
                    summary,
                    obligations,
                )
                resolved_coverage_status = coverage.status
                resolved_missing_obligations = coverage.missing_obligations
                resolved_critical_carry_forward = coverage.critical_carry_forward

            summary_record = SessionSummary(
                session_id=node.session_id,
                session_key=session_key,
                compaction_id=persisted_compaction_id,
                trigger_reason=trigger_reason,
                summary_text=summary,
                summary_payload=structured_summary.model_dump(mode="json"),
                summary_format="structured_v1",
                coverage_status=resolved_coverage_status,
                missing_obligations=resolved_missing_obligations,
                critical_carry_forward=resolved_critical_carry_forward,
                removed_count=len(removed_entries),
                kept_count=persisted_kept_count,
                flush_receipt_status=_compaction_flush_status_for_persistence(
                    flush_receipt_status
                ),
                covered_through_id=max((entry.id or 0) for entry in removed_entries)
                if removed_entries
                else 0,
            )

        if expected_source_entries is not None:
            # Only rewrite the durable source projection. ``kept_entries`` may
            # also contain current-turn skills, attachments, tool traffic, and
            # retry scaffolding. Those are finalized through the normal turn
            # persistence path and must never become synthetic transcript rows.
            rewritten_entries = list(preserved_entries)
        else:
            # Insert kept entries, preserving original metadata where possible.
            rewritten_entries = []
            for index, raw in enumerate(kept_entries):
                if index < len(preserved_entries):
                    preserved = preserved_entries[index]
                    # Agent compaction uses a flattened text projection only
                    # for summary generation. Prefix-only tail rows remain
                    # canonical; preserve their structured metadata by
                    # position.
                    if preserved.role == raw.get("role"):
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
        if deadline_config is not None:
            require_compaction_time(deadline_config, phase="committing")
        commit_started = time.monotonic()
        rewrite_kwargs: dict[str, Any] = {}
        if expected_source_entries is not None:
            rewrite_kwargs = {
                "expected_source_entries": expected_source_entries,
                "expected_source_preimage": expected_source_preimage,
                "expected_source_boundary_message_id": source_boundary_message_id,
                "expected_source_boundary_entry_id": source_boundary_entry_id,
            }
        commit_task = asyncio.create_task(
            self._storage.rewrite_compacted_session(
                node=node,
                summary=summary_record,
                entries=rewritten_entries,
                context_states=[context_state] if context_state is not None else None,
                archived_entries=removed_entries if summary_record is not None else None,
                **rewrite_kwargs,
            )
        )
        installed, cancellation_reconciled = await _await_compaction_commit_barrier(commit_task)
        if installed is False:
            _log.warning(
                "persist_compaction.stale_preimage_skipped",
                compaction_id=persisted_compaction_id,
                session_key=session_key,
            )
            return False
        _log.info(
            "persist_compaction.done",
            compaction_id=persisted_compaction_id,
            cancellation_reconciled=cancellation_reconciled,
            commit_ms=max(0, int((time.monotonic() - commit_started) * 1000)),
            session_key=session_key,
            summary_len=len(summary),
            kept=persisted_kept_count,
        )
        return True

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
        stale = await self._storage.prune_stale_session_records(cutoff)
        for session in stale:
            self.evict_session_runtime_state(
                session.session_key,
                session_id=session.session_id,
            )
        return len(stale)

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
            self.evict_session_runtime_state(
                s.session_key,
                session_id=s.session_id,
            )
        return len(to_delete)

    async def archive(self, session_key: str) -> None:
        """Archive (soft-finish) a session by marking status=done."""
        session_key = canonicalize_session_key(session_key)
        await self.finish(session_key, status=SessionStatus.DONE)
