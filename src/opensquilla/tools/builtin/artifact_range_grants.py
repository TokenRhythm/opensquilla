"""Turn-scoped opaque grants for source-preserving Artifact HTML edits.

The model never receives source offsets.  Read/locate/search tools mint an
unpredictable token for one exact range in one immutable ArtifactSession head;
the writer resolves those tokens again immediately before the atomic splice.
The registry is attached lazily to ``ToolContext`` so its lifetime cannot
outlive the turn that created it.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

_GRANT_PREFIX = "hrg_"
_CURSOR_PREFIX = "hcur_"
_TOKEN_RE = re.compile(r"^(?:hrg|hcur)_[A-Za-z0-9_-]{43}$")
_REGISTRY_ATTRIBUTE = "_artifact_range_grant_registry"

MAX_RANGE_GRANTS_PER_TURN = 64
MAX_RANGE_GRANT_TTL_SECONDS = 15 * 60
MAX_RANGE_QUERIES_PER_TURN = 4


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
        )


@dataclass(frozen=True, slots=True)
class ResolvedRangeGrant:
    token: str
    start: int
    end: int
    kind: str
    annotation_orders: tuple[int, ...]


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
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._ranges.clear()
            self._cursors.clear()
            self._context_nonces.clear()
            self._query_count = 0

    def consume_query_budget(self) -> None:
        """Reserve one shared locate/search query before it can mint any grants."""

        with self._lock:
            if self._query_count >= MAX_RANGE_QUERIES_PER_TURN:
                raise ArtifactRangeGrantError(
                    "ARTIFACT_RANGE_QUERY_LIMIT",
                    "This turn has reached the source range query limit.",
                )
            self._query_count += 1

    def mint_range(
        self,
        *,
        binding: ArtifactRangeBinding,
        source: str,
        start: int,
        end: int,
        kind: str,
        annotation_orders: tuple[int, ...] = (),
    ) -> str:
        if start < 0 or end <= start or end > len(source):
            raise ArtifactRangeGrantError(
                "ARTIFACT_RANGE_INVALID",
                "The requested source range could not be verified.",
            )
        normalized_orders = tuple(sorted(set(annotation_orders)))
        expected_sha256 = hashlib.sha256(source[start:end].encode("utf-8")).hexdigest()
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


def clear_context_registry(ctx: RangeGrantContext | None) -> None:
    registry = getattr(ctx, _REGISTRY_ATTRIBUTE, None)
    if isinstance(registry, ArtifactRangeGrantRegistry):
        registry.clear()
    try:
        delattr(ctx, _REGISTRY_ATTRIBUTE)
    except AttributeError:
        pass


# Format-neutral document aliases. The token wire format remains ``hrg_``
# because grants are process-local capabilities rather than persisted public
# identifiers.
DocumentGrantBinding = ArtifactRangeBinding
DocumentGrantError = ArtifactRangeGrantError
DocumentMutationGrantRegistry = ArtifactRangeGrantRegistry
ResolvedDocumentGrant = ResolvedRangeGrant
document_grant_registry_for_context = registry_for_context


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
