"""Turn-scoped opaque grants for document mutations.

The model receives only an unpredictable token. Each token is bound to one
adapter/version, immutable revision, semantic operation, selection and target
fingerprint. Format-specific coordinates may exist only inside an adapter-owned
opaque locator; this registry stores but never interprets that object.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Protocol

_GRANT_PREFIX = "hrg_"
_CURSOR_PREFIX = "hcur_"
_TOKEN_RE = re.compile(r"^(?:hrg|hcur)_[A-Za-z0-9_-]{43}$")
_REGISTRY_ATTRIBUTE = "_artifact_range_grant_registry"
_DOCUMENT_REGISTRY_ATTRIBUTE = "_document_mutation_grant_registry"

MAX_RANGE_GRANTS_PER_TURN = 64
MAX_RANGE_GRANT_TTL_SECONDS = 15 * 60
MAX_RANGE_QUERIES_PER_TURN = 4
MAX_IDENTICAL_DOCUMENT_TOOL_CALLS_PER_TURN = 2
MAX_RECORDED_SOURCE_FRAGMENT_BYTES = 16 * 1024
MAX_RECORDED_SOURCE_BYTES_PER_TURN = 128 * 1024


class RangeGrantContext(Protocol):
    """Narrow context shape needed for lazy per-turn registry ownership."""

    task_id: str | None
    session_key: str | None


@dataclass(frozen=True, slots=True)
class ArtifactRangeBinding:
    task_id: str
    session_key: str
    session_id: str
    session_epoch: int
    document_id: str
    revision_id: str
    source_sha256: str
    adapter_id: str = "html"
    adapter_version: int = 1

    @property
    def key(self) -> tuple[object, ...]:
        return (
            self.task_id,
            self.session_key,
            self.session_id,
            self.session_epoch,
            self.document_id,
            self.revision_id,
            self.source_sha256,
            self.adapter_id,
            self.adapter_version,
        )


@dataclass(frozen=True, slots=True)
class DocumentGrantBinding:
    """Immutable authority boundary for one adapter-owned document revision."""

    task_id: str
    session_key: str
    session_id: str
    session_epoch: int
    document_id: str
    revision_id: str
    source_sha256: str
    adapter_id: str
    adapter_version: int

    @property
    def key(self) -> tuple[object, ...]:
        return (
            self.task_id,
            self.session_key,
            self.session_id,
            self.session_epoch,
            self.document_id,
            self.revision_id,
            self.source_sha256,
            self.adapter_id,
            self.adapter_version,
        )


@dataclass(frozen=True, slots=True)
class ResolvedDocumentGrant:
    """Format-neutral grant whose locator remains process-local and adapter-owned."""

    token: str
    adapter_id: str
    adapter_version: int
    operation: str
    target_fingerprint: str
    annotation_orders: tuple[int, ...]
    adapter_locator: object = dataclass_field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ResolvedRangeGrant:
    token: str
    start: int
    end: int
    kind: str
    annotation_orders: tuple[int, ...]
    adapter_id: str
    adapter_version: int
    operation: str
    target_fingerprint: str


@dataclass(slots=True)
class _RangeEntry:
    token: str
    binding_key: tuple[object, ...]
    context_nonce: str
    start: int
    end: int
    expected_sha256: str
    kind: str
    annotation_orders: tuple[int, ...]
    adapter_id: str
    adapter_version: int
    operation: str
    target_fingerprint: str
    expires_at: float
    state: str = "fresh"
    reservation_id: str | None = None


@dataclass(slots=True)
class _DocumentGrantEntry:
    token: str
    binding_key: tuple[object, ...]
    adapter_id: str
    adapter_version: int
    operation: str
    target_fingerprint: str
    annotation_orders: tuple[int, ...]
    adapter_locator: object = dataclass_field(repr=False, compare=False)
    expires_at: float
    state: str = "fresh"
    reservation_id: str | None = None


@dataclass(slots=True)
class _CursorEntry:
    token: str
    binding_key: tuple[object, ...]
    context_nonce: str
    position: int
    expires_at: float
    state: str = "fresh"


@dataclass(frozen=True, slots=True)
class _SourceReadSpan:
    start: int
    end: int
    byte_size: int


class ArtifactRangeGrantError(ValueError):
    """Stable, sanitized range-grant failure."""

    def __init__(self, code: str, user_message: str) -> None:
        super().__init__(code)
        self.code = code
        self.user_message = user_message


class ArtifactRangeGrantRegistry:
    """Bounded, concurrency-safe range and paging authority for one turn."""

    def __init__(
        self,
        *,
        capacity: int = MAX_RANGE_GRANTS_PER_TURN,
        ttl_seconds: float = MAX_RANGE_GRANT_TTL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1 or capacity > MAX_RANGE_GRANTS_PER_TURN:
            raise ValueError("range grant capacity is invalid")
        if ttl_seconds <= 0 or ttl_seconds > MAX_RANGE_GRANT_TTL_SECONDS:
            raise ValueError("range grant ttl is invalid")
        self._capacity = int(capacity)
        self._ttl_seconds = float(ttl_seconds)
        self._monotonic = monotonic
        self._ranges: dict[str, _RangeEntry] = {}
        self._cursors: dict[str, _CursorEntry] = {}
        self._context_nonces: dict[tuple[object, ...], str] = {}
        self._query_count = 0
        self._query_keys: set[str] = set()
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._ranges.clear()
            self._cursors.clear()
            self._context_nonces.clear()
            self._query_count = 0
            self._query_keys.clear()

    def consume_query_budget(self, *, query_key: str | None = None) -> int:
        """Reserve one unique locate/search query and return the remaining budget.

        Repeating an identical, already-admitted lookup cannot broaden the
        model's authority, so it reuses the original budget slot. Distinct
        targets or operations remain bounded by the turn-wide ceiling.
        """

        with self._lock:
            if query_key is not None and query_key in self._query_keys:
                return MAX_RANGE_QUERIES_PER_TURN - self._query_count
            if self._query_count >= MAX_RANGE_QUERIES_PER_TURN:
                raise ArtifactRangeGrantError(
                    "ARTIFACT_RANGE_QUERY_LIMIT",
                    "This turn has reached the source range query limit.",
                )
            self._query_count += 1
            if query_key is not None:
                self._query_keys.add(query_key)
            return MAX_RANGE_QUERIES_PER_TURN - self._query_count

    def mint_range(
        self,
        *,
        binding: ArtifactRangeBinding,
        source: str,
        start: int,
        end: int,
        kind: str,
        annotation_orders: tuple[int, ...] = (),
        operation: str = "source_patch",
        target_fingerprint: str | None = None,
    ) -> str:
        if start < 0 or end <= start or end > len(source):
            raise ArtifactRangeGrantError(
                "ARTIFACT_RANGE_INVALID",
                "The requested source range could not be verified.",
            )
        normalized_orders = tuple(sorted(set(annotation_orders)))
        expected_sha256 = hashlib.sha256(source[start:end].encode("utf-8")).hexdigest()
        adapter_id = str(binding.adapter_id or "").strip().lower()
        adapter_version = binding.adapter_version
        normalized_operation = str(operation or "").strip().lower()
        if (
            not adapter_id
            or isinstance(adapter_version, bool)
            or not isinstance(adapter_version, int)
            or adapter_version < 1
            or not normalized_operation
        ):
            raise ArtifactRangeGrantError(
                "ARTIFACT_RANGE_BINDING_INVALID",
                "The mutation grant binding is invalid.",
            )
        if target_fingerprint is None:
            digest = hashlib.sha256()
            digest.update(
                f"{adapter_id}\0{adapter_version}\0{normalized_operation}\0".encode()
            )
            digest.update(f"{start}\0{end}\0{kind}\0{expected_sha256}".encode())
            normalized_fingerprint = digest.hexdigest()
        else:
            normalized_fingerprint = str(target_fingerprint).strip().lower()
            if re.fullmatch(r"[0-9a-f]{64}", normalized_fingerprint) is None:
                raise ArtifactRangeGrantError(
                    "ARTIFACT_RANGE_FINGERPRINT_INVALID",
                    "The mutation target fingerprint is invalid.",
                )
        now = self._monotonic()
        with self._lock:
            self._purge_locked(now)
            for entry in self._ranges.values():
                if (
                    entry.binding_key == binding.key
                    and entry.start == start
                    and entry.end == end
                    and entry.expected_sha256 == expected_sha256
                    and entry.kind == kind
                    and entry.annotation_orders == normalized_orders
                    and entry.adapter_id == adapter_id
                    and entry.adapter_version == adapter_version
                    and entry.operation == normalized_operation
                    and entry.target_fingerprint == normalized_fingerprint
                    and entry.state == "fresh"
                ):
                    return entry.token
            self._require_capacity_locked()
            nonce = self._context_nonces.setdefault(
                binding.key, secrets.token_urlsafe(32)
            )
            token = self._new_token_locked(_GRANT_PREFIX)
            self._ranges[token] = _RangeEntry(
                token=token,
                binding_key=binding.key,
                context_nonce=nonce,
                start=start,
                end=end,
                expected_sha256=expected_sha256,
                kind=kind,
                annotation_orders=normalized_orders,
                adapter_id=adapter_id,
                adapter_version=adapter_version,
                operation=normalized_operation,
                target_fingerprint=normalized_fingerprint,
                expires_at=now + self._ttl_seconds,
            )
            return token

    def mint_cursor(self, *, binding: ArtifactRangeBinding, position: int) -> str:
        if position < 0:
            raise ValueError("cursor position must be non-negative")
        now = self._monotonic()
        with self._lock:
            self._purge_locked(now)
            self._require_capacity_locked()
            nonce = self._context_nonces.setdefault(
                binding.key, secrets.token_urlsafe(32)
            )
            token = self._new_token_locked(_CURSOR_PREFIX)
            self._cursors[token] = _CursorEntry(
                token=token,
                binding_key=binding.key,
                context_nonce=nonce,
                position=position,
                expires_at=now + self._ttl_seconds,
            )
            return token

    def consume_cursor(self, *, binding: ArtifactRangeBinding, token: str) -> int:
        now = self._monotonic()
        with self._lock:
            self._purge_locked(now)
            entry = self._cursors.get(token)
            if (
                not _TOKEN_RE.fullmatch(token)
                or entry is None
                or entry.binding_key != binding.key
                or entry.state != "fresh"
            ):
                raise ArtifactRangeGrantError(
                    "ARTIFACT_CURSOR_INVALID",
                    "The source cursor is invalid or expired. Read the source again.",
                )
            entry.state = "consumed"
            return entry.position

    def reserve_ranges(
        self,
        *,
        binding: ArtifactRangeBinding,
        source: str,
        tokens: list[str],
        reservation_id: str,
    ) -> tuple[ResolvedRangeGrant, ...]:
        if not tokens or len(tokens) != len(set(tokens)):
            raise ArtifactRangeGrantError(
                "ARTIFACT_RANGE_DUPLICATE",
                "Every source range must be present exactly once.",
            )
        now = self._monotonic()
        reserved: list[_RangeEntry] = []
        with self._lock:
            self._purge_locked(now)
            try:
                for token in tokens:
                    entry = self._ranges.get(token)
                    if (
                        not _TOKEN_RE.fullmatch(token)
                        or entry is None
                        or entry.binding_key != binding.key
                    ):
                        raise ArtifactRangeGrantError(
                            "ARTIFACT_RANGE_TOKEN_INVALID",
                            "A source range is invalid or expired. "
                            "Locate the current source again.",
                        )
                    if entry.state != "fresh":
                        raise ArtifactRangeGrantError(
                            "ARTIFACT_RANGE_TOKEN_USED",
                            "A source range is already in use or was consumed.",
                        )
                    actual = hashlib.sha256(
                        source[entry.start : entry.end].encode("utf-8")
                    ).hexdigest()
                    if actual != entry.expected_sha256:
                        raise ArtifactRangeGrantError(
                            "ARTIFACT_RANGE_STALE",
                            "The source changed after the range was located.",
                        )
                    entry.state = "reserved"
                    entry.reservation_id = reservation_id
                    reserved.append(entry)
            except ArtifactRangeGrantError:
                for entry in reserved:
                    entry.state = "fresh"
                    entry.reservation_id = None
                raise

        resolved = tuple(
            ResolvedRangeGrant(
                token=entry.token,
                start=entry.start,
                end=entry.end,
                kind=entry.kind,
                annotation_orders=entry.annotation_orders,
                adapter_id=entry.adapter_id,
                adapter_version=entry.adapter_version,
                operation=entry.operation,
                target_fingerprint=entry.target_fingerprint,
            )
            for entry in reserved
        )
        ordered = sorted(resolved, key=lambda value: (value.start, value.end))
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current.start < previous.end:
                self.release_reservation(reservation_id)
                raise ArtifactRangeGrantError(
                    "ARTIFACT_RANGE_OVERLAP",
                    "Source ranges must not overlap in one atomic edit.",
                )
        return resolved

    def release_reservation(self, reservation_id: str) -> None:
        with self._lock:
            for entry in self._ranges.values():
                if entry.state == "reserved" and entry.reservation_id == reservation_id:
                    entry.state = "fresh"
                    entry.reservation_id = None

    def consume_reservation(self, reservation_id: str) -> None:
        with self._lock:
            for entry in self._ranges.values():
                if entry.state == "reserved" and entry.reservation_id == reservation_id:
                    entry.state = "consumed"
                    entry.reservation_id = None

    def _purge_locked(self, now: float) -> None:
        self._ranges = {
            token: entry
            for token, entry in self._ranges.items()
            if entry.expires_at > now and entry.state != "consumed"
        }
        self._cursors = {
            token: entry
            for token, entry in self._cursors.items()
            if entry.expires_at > now and entry.state != "consumed"
        }
        active_keys = {entry.binding_key for entry in self._ranges.values()}
        active_keys.update(entry.binding_key for entry in self._cursors.values())
        self._context_nonces = {
            key: nonce for key, nonce in self._context_nonces.items() if key in active_keys
        }

    def _require_capacity_locked(self) -> None:
        if len(self._ranges) + len(self._cursors) >= self._capacity:
            raise ArtifactRangeGrantError(
                "ARTIFACT_RANGE_LIMIT",
                "This turn has reached the source range limit.",
            )

    def _new_token_locked(self, prefix: str) -> str:
        while True:
            token = f"{prefix}{secrets.token_urlsafe(32)}"
            if token not in self._ranges and token not in self._cursors:
                return token


class DocumentMutationGrantRegistry:
    """Turn-local semantic authority without assumptions about document bytes."""

    def __init__(
        self,
        *,
        capacity: int = MAX_RANGE_GRANTS_PER_TURN,
        ttl_seconds: float = MAX_RANGE_GRANT_TTL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1 or capacity > MAX_RANGE_GRANTS_PER_TURN:
            raise ValueError("document grant capacity is invalid")
        if ttl_seconds <= 0 or ttl_seconds > MAX_RANGE_GRANT_TTL_SECONDS:
            raise ValueError("document grant ttl is invalid")
        self._capacity = int(capacity)
        self._ttl_seconds = float(ttl_seconds)
        self._monotonic = monotonic
        self._entries: dict[str, _DocumentGrantEntry] = {}
        self._query_count = 0
        self._query_keys: set[str] = set()
        self._tool_attempts: dict[str, int] = {}
        self._contextual_candidates: dict[int, str] = {}
        self._source_reads: dict[tuple[object, ...], list[_SourceReadSpan]] = {}
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._query_count = 0
            self._query_keys.clear()
            self._tool_attempts.clear()
            self._contextual_candidates.clear()
            self._source_reads.clear()

    def reserve_tool_attempt(self, *, attempt_key: str) -> int:
        """Bound identical inspection or locate calls while allowing one safe replay.

        A second identical call covers a provider retry or a lost result without
        broadening authority. Further calls cannot reveal anything new within the
        immutable turn binding, so rejecting them prevents an Agent from looping on
        a deterministic outcome.
        """

        if not isinstance(attempt_key, str) or not attempt_key:
            raise ArtifactRangeGrantError(
                "ARTIFACT_RANGE_BINDING_INVALID",
                "The document tool attempt is invalid.",
            )
        with self._lock:
            attempts = self._tool_attempts.get(attempt_key, 0)
            if attempts >= MAX_IDENTICAL_DOCUMENT_TOOL_CALLS_PER_TURN:
                raise ArtifactRangeGrantError(
                    "ARTIFACT_RANGE_QUERY_LIMIT",
                    "This document inspection or target query was already repeated. "
                    "Reuse the earlier result, leave an unsupported item unchanged, "
                    "and finish without calling it again.",
                )
            attempts += 1
            self._tool_attempts[attempt_key] = attempts
            return MAX_IDENTICAL_DOCUMENT_TOOL_CALLS_PER_TURN - attempts

    def record_source_read(
        self,
        *,
        binding: DocumentGrantBinding,
        start: int,
        end: int,
        text: str,
    ) -> None:
        """Remember a bounded canonical-source interval returned this turn."""

        if (
            not isinstance(text, str)
            or isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 0
            or end < start
            or len(text) != end - start
        ):
            raise ArtifactRangeGrantError(
                "ARTIFACT_RANGE_BINDING_INVALID",
                "The document source read could not be recorded safely.",
            )
        text_size = len(text.encode("utf-8"))
        if text_size > MAX_RECORDED_SOURCE_FRAGMENT_BYTES:
            raise ArtifactRangeGrantError(
                "ARTIFACT_RANGE_BINDING_INVALID",
                "The document source read could not be recorded safely.",
            )
        with self._lock:
            spans = self._source_reads.setdefault(binding.key, [])
            span = _SourceReadSpan(start=start, end=end, byte_size=text_size)
            if span not in spans:
                recorded_size = sum(item.byte_size for item in spans)
                if recorded_size + text_size > MAX_RECORDED_SOURCE_BYTES_PER_TURN:
                    raise ArtifactRangeGrantError(
                        "ARTIFACT_RANGE_LIMIT",
                        "This turn has reached the document source read limit.",
                    )
                spans.append(span)

    def candidate_range_was_read(
        self,
        *,
        binding: DocumentGrantBinding,
        start: int,
        end: int,
    ) -> bool:
        """Return whether read intervals fully cover a candidate source range."""

        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 0
            or end <= start
        ):
            return False
        with self._lock:
            covered_until = start
            for span in sorted(
                self._source_reads.get(binding.key, ()),
                key=lambda item: (item.start, item.end),
            ):
                if span.end <= covered_until:
                    continue
                if span.start > covered_until:
                    return False
                covered_until = span.end
                if covered_until >= end:
                    return True
            return False

    def bind_contextual_candidate(
        self,
        *,
        annotation_order: int,
        candidate_fingerprint: str,
    ) -> None:
        """Allow one distinct candidate per contextual annotation and turn."""

        if (
            isinstance(annotation_order, bool)
            or not isinstance(annotation_order, int)
            or annotation_order < 0
            or re.fullmatch(r"[0-9a-f]{64}", candidate_fingerprint) is None
        ):
            raise ArtifactRangeGrantError(
                "ARTIFACT_RANGE_BINDING_INVALID",
                "The contextual document target is invalid.",
            )
        with self._lock:
            existing = self._contextual_candidates.get(annotation_order)
            if existing is not None and existing != candidate_fingerprint:
                raise ArtifactRangeGrantError(
                    "ARTIFACT_RANGE_QUERY_LIMIT",
                    "This annotation already used a different contextual target this turn.",
                )
            self._contextual_candidates[annotation_order] = candidate_fingerprint

    def consume_query_budget(self, *, query_key: str | None = None) -> int:
        with self._lock:
            if query_key is not None and query_key in self._query_keys:
                return MAX_RANGE_QUERIES_PER_TURN - self._query_count
            if self._query_count >= MAX_RANGE_QUERIES_PER_TURN:
                raise ArtifactRangeGrantError(
                    "ARTIFACT_RANGE_QUERY_LIMIT",
                    "This turn has reached the document target query limit.",
                )
            self._query_count += 1
            if query_key is not None:
                self._query_keys.add(query_key)
            return MAX_RANGE_QUERIES_PER_TURN - self._query_count

    def mint_grant(
        self,
        *,
        binding: DocumentGrantBinding,
        operation: str,
        target_fingerprint: str,
        annotation_orders: tuple[int, ...],
        adapter_locator: object,
    ) -> str:
        adapter_id = str(binding.adapter_id or "").strip().lower()
        operation = str(operation or "").strip().lower()
        fingerprint = str(target_fingerprint or "").strip().lower()
        adapter_version = binding.adapter_version
        if (
            not adapter_id
            or not operation
            or isinstance(adapter_version, bool)
            or not isinstance(adapter_version, int)
            or adapter_version < 1
            or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
        ):
            raise ArtifactRangeGrantError(
                "ARTIFACT_RANGE_BINDING_INVALID",
                "The document mutation grant binding is invalid.",
            )
        normalized_orders = tuple(sorted(set(annotation_orders)))
        now = self._monotonic()
        with self._lock:
            self._purge_locked(now)
            for entry in self._entries.values():
                if (
                    entry.binding_key == binding.key
                    and entry.adapter_id == adapter_id
                    and entry.adapter_version == adapter_version
                    and entry.operation == operation
                    and entry.target_fingerprint == fingerprint
                    and entry.annotation_orders == normalized_orders
                    and entry.state == "fresh"
                ):
                    return entry.token
            if len(self._entries) >= self._capacity:
                raise ArtifactRangeGrantError(
                    "ARTIFACT_RANGE_LIMIT",
                    "This turn has reached the document mutation grant limit.",
                )
            token = self._new_token_locked()
            self._entries[token] = _DocumentGrantEntry(
                token=token,
                binding_key=binding.key,
                adapter_id=adapter_id,
                adapter_version=adapter_version,
                operation=operation,
                target_fingerprint=fingerprint,
                annotation_orders=normalized_orders,
                adapter_locator=adapter_locator,
                expires_at=now + self._ttl_seconds,
            )
            return token

    def reserve_grants(
        self,
        *,
        binding: DocumentGrantBinding,
        tokens: list[str],
        reservation_id: str,
    ) -> tuple[ResolvedDocumentGrant, ...]:
        if not tokens or len(tokens) != len(set(tokens)):
            raise ArtifactRangeGrantError(
                "ARTIFACT_RANGE_DUPLICATE",
                "Every document mutation grant must be present exactly once.",
            )
        now = self._monotonic()
        reserved: list[_DocumentGrantEntry] = []
        with self._lock:
            self._purge_locked(now)
            try:
                for token in tokens:
                    entry = self._entries.get(token)
                    if (
                        not _TOKEN_RE.fullmatch(token)
                        or entry is None
                        or entry.binding_key != binding.key
                    ):
                        raise ArtifactRangeGrantError(
                            "ARTIFACT_RANGE_TOKEN_INVALID",
                            "A document mutation grant is invalid or expired. "
                            "Locate the current target again.",
                        )
                    if entry.state != "fresh":
                        raise ArtifactRangeGrantError(
                            "ARTIFACT_RANGE_TOKEN_USED",
                            "A document mutation grant is already in use or was consumed.",
                        )
                    entry.state = "reserved"
                    entry.reservation_id = reservation_id
                    reserved.append(entry)
            except ArtifactRangeGrantError:
                for entry in reserved:
                    entry.state = "fresh"
                    entry.reservation_id = None
                raise
        return tuple(
            ResolvedDocumentGrant(
                token=entry.token,
                adapter_id=entry.adapter_id,
                adapter_version=entry.adapter_version,
                operation=entry.operation,
                target_fingerprint=entry.target_fingerprint,
                annotation_orders=entry.annotation_orders,
                adapter_locator=entry.adapter_locator,
            )
            for entry in reserved
        )

    def release_reservation(self, reservation_id: str) -> None:
        with self._lock:
            for entry in self._entries.values():
                if entry.state == "reserved" and entry.reservation_id == reservation_id:
                    entry.state = "fresh"
                    entry.reservation_id = None

    def consume_reservation(self, reservation_id: str) -> None:
        with self._lock:
            for entry in self._entries.values():
                if entry.state == "reserved" and entry.reservation_id == reservation_id:
                    entry.state = "consumed"
                    entry.reservation_id = None

    def _purge_locked(self, now: float) -> None:
        self._entries = {
            token: entry
            for token, entry in self._entries.items()
            if entry.expires_at > now and entry.state != "consumed"
        }

    def _new_token_locked(self) -> str:
        while True:
            token = f"{_GRANT_PREFIX}{secrets.token_urlsafe(32)}"
            if token not in self._entries:
                return token


def registry_for_context(ctx: RangeGrantContext) -> ArtifactRangeGrantRegistry:
    registry = getattr(ctx, _REGISTRY_ATTRIBUTE, None)
    if isinstance(registry, ArtifactRangeGrantRegistry):
        return registry
    registry = ArtifactRangeGrantRegistry()
    setattr(ctx, _REGISTRY_ATTRIBUTE, registry)
    callbacks = getattr(ctx, "turn_cleanup_callbacks", None)
    if isinstance(callbacks, list):
        callbacks.append(lambda context=ctx: clear_context_registry(context))
    return registry


def document_grant_registry_for_context(
    ctx: RangeGrantContext,
) -> DocumentMutationGrantRegistry:
    registry = getattr(ctx, _DOCUMENT_REGISTRY_ATTRIBUTE, None)
    if isinstance(registry, DocumentMutationGrantRegistry):
        return registry
    registry = DocumentMutationGrantRegistry()
    setattr(ctx, _DOCUMENT_REGISTRY_ATTRIBUTE, registry)
    callbacks = getattr(ctx, "turn_cleanup_callbacks", None)
    if isinstance(callbacks, list):
        callbacks.append(lambda context=ctx: clear_context_registry(context))
    return registry


def clear_context_registry(ctx: RangeGrantContext | None) -> None:
    registry = getattr(ctx, _REGISTRY_ATTRIBUTE, None)
    if isinstance(registry, ArtifactRangeGrantRegistry):
        registry.clear()
    try:
        delattr(ctx, _REGISTRY_ATTRIBUTE)
    except AttributeError:
        pass
    document_registry = getattr(ctx, _DOCUMENT_REGISTRY_ATTRIBUTE, None)
    if isinstance(document_registry, DocumentMutationGrantRegistry):
        document_registry.clear()
    try:
        delattr(ctx, _DOCUMENT_REGISTRY_ATTRIBUTE)
    except AttributeError:
        pass


# Format-neutral document aliases. The token wire format remains ``hrg_``
# because grants are process-local capabilities rather than persisted public
# identifiers.
DocumentGrantError = ArtifactRangeGrantError


__all__ = [
    "ArtifactRangeBinding",
    "ArtifactRangeGrantError",
    "ArtifactRangeGrantRegistry",
    "DocumentGrantBinding",
    "DocumentGrantError",
    "DocumentMutationGrantRegistry",
    "ResolvedRangeGrant",
    "ResolvedDocumentGrant",
    "clear_context_registry",
    "document_grant_registry_for_context",
    "registry_for_context",
]
