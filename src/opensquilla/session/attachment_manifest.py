"""Durable attachment occurrence indexing for session history.

The transcript is the authority for user-visible content.  This module keeps
the smaller, machine-readable index needed to find an attachment after a
compaction moved its source row out of the active transcript.  The index is
stored as a ``SessionContextState`` snapshot rather than a second SQL schema;
that makes it safe to deploy alongside older databases and lets the existing
session fork/reset code carry the state forward.

No image bytes are stored in the manifest.  Inline legacy envelopes are only
decoded long enough to derive a content hash and a deterministic occurrence
identifier.  Request-time image projection belongs to the engine layer and
must never mutate this index.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from opensquilla.session.keys import canonicalize_session_key
from opensquilla.session.models import SessionContextState

ATTACHMENT_MANIFEST_STATE_KIND = "attachment_manifest_v1"
ATTACHMENT_MANIFEST_PROVIDER = "portable"
ATTACHMENT_MANIFEST_SCHEMA_VERSION = 1

MATERIAL_AVAILABLE = "available"
MATERIAL_MISSING = "missing"
MATERIAL_INVALID = "invalid"
MATERIAL_STATES = frozenset(
    {MATERIAL_AVAILABLE, MATERIAL_MISSING, MATERIAL_INVALID}
)

_ATTACHMENT_ID_RE = re.compile(r"^att_[A-Za-z0-9_-]{8,160}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_NAME_BYTES = 160
_MAX_MIME_BYTES = 120
_MAX_REASON_BYTES = 256
_MAX_MESSAGE_ID_BYTES = 512
_MAX_MANIFEST_OCCURRENCES = 100_000
_MAX_INLINE_BYTES = 64 * 1024 * 1024


class AttachmentManifestError(ValueError):
    """Raised when an attachment occurrence cannot be indexed safely."""


class AttachmentManifestStorage(Protocol):
    """Small storage surface used by :class:`AttachmentManifestStore`.

    ``SessionStorage`` and ``SessionManager`` both expose these methods.  A
    protocol keeps this module independent of either high-level owner and
    makes the persistence adapter straightforward to exercise with a fake.
    """

    async def save_context_state(
        self, state: SessionContextState
    ) -> SessionContextState: ...

    async def get_context_states(
        self,
        session_key: str,
        *,
        provider: str | None = None,
        state_kind: str | None = None,
        valid_only: bool = True,
    ) -> list[SessionContextState]: ...

    async def get_canonical_transcript(
        self,
        session_id: str,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[object]: ...


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _field(value: object, name: str, default: object = None) -> object:
    """Read a field from either a mapping or a SQLModel/dataclass row."""

    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _bounded_text(value: object, *, fallback: str, max_bytes: int) -> str:
    if not isinstance(value, str):
        return fallback
    normalized = " ".join(value.strip().split())
    if not normalized:
        return fallback
    # Slice by encoded bytes, not code points, so the serialized state stays
    # bounded for non-ASCII filenames and MIME-like values.
    encoded = normalized.encode("utf-8")
    if len(encoded) <= max_bytes:
        return normalized
    return encoded[:max_bytes].decode("utf-8", errors="ignore") or fallback


def normalize_attachment_name(value: object, *, fallback: str = "attachment") -> str:
    """Return a bounded display name safe for a model-visible descriptor."""

    return _bounded_text(value, fallback=fallback, max_bytes=_MAX_NAME_BYTES)


def normalize_attachment_mime(value: object) -> str:
    """Normalize a MIME value without retaining parameters or control chars."""

    if not isinstance(value, str):
        return "application/octet-stream"
    normalized = value.split(";", 1)[0].strip().lower()
    if "/" not in normalized or any(char in normalized for char in "\r\n"):
        return "application/octet-stream"
    return _bounded_text(
        normalized,
        fallback="application/octet-stream",
        max_bytes=_MAX_MIME_BYTES,
    )


def valid_sha256(value: object) -> str | None:
    """Return a canonical lower-case SHA-256 digest, or ``None``."""

    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        return None
    return value.lower()


def valid_attachment_id(value: object) -> str | None:
    """Return a valid persisted occurrence ID, or ``None`` for legacy data."""

    if not isinstance(value, str) or _ATTACHMENT_ID_RE.fullmatch(value) is None:
        return None
    return value


def legacy_attachment_id(
    *,
    session_id: str,
    message_id: str,
    index: int,
    sha256: str | None = None,
) -> str:
    """Derive the stable ID used when an old envelope has no occurrence ID.

    The input includes the logical session, source message, ordinal and
    content hash.  It therefore remains stable across compaction and process
    restarts while keeping the raw material out of the identifier.
    """

    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise AttachmentManifestError("attachment index must be a non-negative integer")
    digest = hashlib.sha256(
        f"{session_id}\0{message_id}\0{index}\0{sha256 or ''}".encode()
    ).digest()[:18]
    token = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"att_legacy_{token}"


# Name used by a few call sites that describe this as a deterministic rather
# than legacy identity.  Keep both names public so migration code need not
# duplicate the algorithm.
deterministic_attachment_id = legacy_attachment_id


def _decode_inline_data(value: object) -> bytes | None:
    if isinstance(value, bytes):
        if len(value) > _MAX_INLINE_BYTES:
            return None
        return value
    if not isinstance(value, str):
        return None
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError, TypeError):
        return None
    if len(decoded) > _MAX_INLINE_BYTES:
        return None
    return decoded


def _declared_size(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _entry_id(value: object) -> int | None:
    raw = value
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        try:
            parsed = int(raw.strip())
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _message_id(value: object, *, source_entry_id: int | None, fallback_index: int) -> str:
    raw = value if isinstance(value, str) else ""
    normalized = raw.strip()
    if not normalized:
        normalized = (
            f"entry-{source_entry_id}"
            if source_entry_id is not None
            else f"legacy-entry-{fallback_index}"
        )
    encoded = normalized.encode("utf-8")
    if len(encoded) > _MAX_MESSAGE_ID_BYTES:
        normalized = encoded[:_MAX_MESSAGE_ID_BYTES].decode("utf-8", errors="ignore")
    return normalized


def _envelope_from_content(content: object) -> Mapping[str, Any] | None:
    if isinstance(content, Mapping):
        return cast(Mapping[str, Any], content)
    if not isinstance(content, str):
        return None
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, Mapping):
        return None
    return cast(Mapping[str, Any], parsed)


def _occurrence_from_item(
    item: Mapping[str, Any],
    *,
    session_id: str,
    source_message_id: str,
    source_entry_id: int | None,
    ordinal: int,
    created_at: int,
) -> AttachmentOccurrence:
    explicit_sha = item.get("sha256_ref")
    if explicit_sha is None:
        # A few pre-envelope writers used ``sha256``/``material_id``.  Read
        # those aliases but never emit them in the new manifest payload.
        explicit_sha = item.get("sha256") or item.get("material_id")
    sha256_ref = valid_sha256(explicit_sha)
    raw_data = item.get("data")
    decoded = _decode_inline_data(raw_data)
    has_data_field = "data" in item

    material_state = MATERIAL_MISSING
    missing_reason: str | None = None
    if isinstance(item.get("missing_reason"), str) and item["missing_reason"].strip():
        material_state = MATERIAL_MISSING
        missing_reason = _bounded_text(
            item["missing_reason"],
            fallback="attachment unavailable",
            max_bytes=_MAX_REASON_BYTES,
        )
    elif explicit_sha is not None and sha256_ref is None:
        material_state = MATERIAL_INVALID
        missing_reason = "invalid sha256 reference"
    elif has_data_field and decoded is None:
        material_state = MATERIAL_INVALID
        missing_reason = "invalid inline attachment data"
    elif sha256_ref is not None or decoded is not None:
        material_state = MATERIAL_AVAILABLE

    computed_sha = hashlib.sha256(decoded).hexdigest() if decoded is not None else None
    if sha256_ref is None and computed_sha is not None:
        sha256_ref = computed_sha
    elif sha256_ref is not None and computed_sha is not None and sha256_ref != computed_sha:
        material_state = MATERIAL_INVALID
        missing_reason = "attachment hash mismatch"

    declared = _declared_size(item.get("size"))
    actual_size = len(decoded) if decoded is not None else None
    size = declared if declared is not None else actual_size
    if declared is not None and actual_size is not None and declared != actual_size:
        material_state = MATERIAL_INVALID
        missing_reason = "attachment size mismatch"

    # A missing/invalid item still needs a stable identity.  Empty SHA is
    # intentional: source message + ordinal distinguish occurrences, while
    # malformed bytes never become part of an identifier.
    attachment_id = valid_attachment_id(item.get("attachment_id"))
    if attachment_id is None:
        attachment_id = legacy_attachment_id(
            session_id=session_id,
            message_id=source_message_id,
            index=ordinal,
            sha256=sha256_ref,
        )

    return AttachmentOccurrence(
        attachment_id=attachment_id,
        source_entry_id=source_entry_id,
        source_message_id=source_message_id,
        ordinal=ordinal,
        sha256_ref=sha256_ref,
        name=normalize_attachment_name(item.get("name")),
        mime=normalize_attachment_mime(
            item.get("mime") or item.get("type") or item.get("media_type")
        ),
        size=size,
        material_state=material_state,
        created_at=created_at,
        missing_reason=missing_reason,
    )


def extract_attachment_occurrences_from_envelope(
    envelope: object,
    *,
    session_id: str,
    source_message_id: str,
    source_entry_id: int | None = None,
    created_at: int = 0,
) -> tuple[AttachmentOccurrence, ...]:
    """Extract attachment occurrences from one canonical envelope.

    The function accepts either the persisted JSON string or an already parsed
    mapping, which is useful during legacy backfill and in storage tests.
    Invalid/non-object attachment list members are ignored because they do not
    identify a material occurrence; malformed material inside an object is
    retained as ``material_state='invalid'`` for deterministic degradation.
    """

    parsed = _envelope_from_content(envelope)
    if parsed is None:
        return ()
    raw_attachments = parsed.get("attachments")
    if not isinstance(raw_attachments, (list, tuple)):
        return ()
    result: list[AttachmentOccurrence] = []
    for ordinal, raw_item in enumerate(raw_attachments):
        if not isinstance(raw_item, Mapping):
            continue
        result.append(
            _occurrence_from_item(
                cast(Mapping[str, Any], raw_item),
                session_id=session_id,
                source_message_id=source_message_id,
                source_entry_id=source_entry_id,
                ordinal=ordinal,
                created_at=created_at,
            )
        )
    return tuple(result)


def extract_attachment_occurrences(
    entries: Iterable[object],
    *,
    session_id: str | None = None,
    include_roles: Iterable[str] = ("user",),
) -> tuple[AttachmentOccurrence, ...]:
    """Extract occurrences from active and/or compacted transcript rows.

    ``SessionStorage.get_canonical_transcript`` already merges the active and
    archived tables.  Passing that result here therefore gives one lookup
    path for both states.  By default only user rows are considered, matching
    the canonical attachment envelope contract and avoiding accidental index
    entries from arbitrary assistant/tool metadata.
    """

    allowed_roles = {str(role).strip().lower() for role in include_roles}
    result: list[AttachmentOccurrence] = []
    for fallback_index, entry in enumerate(entries):
        role = _field(entry, "role", "user")
        if isinstance(role, str) and role.strip().lower() not in allowed_roles:
            continue
        entry_session_id = _field(entry, "session_id", "")
        resolved_session_id = session_id or (
            entry_session_id if isinstance(entry_session_id, str) else ""
        )
        physical_id = _entry_id(_field(entry, "id"))
        message_id = _message_id(
            _field(entry, "message_id"),
            source_entry_id=physical_id,
            fallback_index=fallback_index,
        )
        created_at_raw = _field(entry, "created_at", 0)
        created_at = _entry_id(created_at_raw) or 0
        content = _field(entry, "content")
        result.extend(
            extract_attachment_occurrences_from_envelope(
                content,
                session_id=resolved_session_id,
                source_message_id=message_id,
                source_entry_id=physical_id,
                created_at=created_at,
            )
        )
    return tuple(result)


@dataclass(frozen=True, slots=True)
class AttachmentOccurrence:
    """One logical attachment occurrence in canonical session history."""

    attachment_id: str
    source_message_id: str
    ordinal: int
    sha256_ref: str | None = None
    name: str = "attachment"
    mime: str = "application/octet-stream"
    size: int | None = None
    material_state: str = MATERIAL_MISSING
    source_entry_id: int | None = None
    created_at: int = 0
    missing_reason: str | None = None

    @property
    def message_id(self) -> str:
        """Compatibility alias used by existing attachment resource code."""

        return self.source_message_id

    @property
    def index(self) -> int:
        """Compatibility alias for the envelope ordinal."""

        return self.ordinal

    @property
    def sha256(self) -> str | None:
        """Compatibility alias for the canonical content reference."""

        return self.sha256_ref

    def to_payload(self) -> dict[str, Any]:
        """Serialize metadata only; never include inline bytes or paths."""

        payload: dict[str, Any] = {
            "attachment_id": self.attachment_id,
            "source_entry_id": self.source_entry_id,
            "source_message_id": self.source_message_id,
            "ordinal": self.ordinal,
            "sha256_ref": self.sha256_ref,
            "name": self.name,
            "mime": self.mime,
            "size": self.size,
            "material_state": self.material_state,
            "created_at": self.created_at,
        }
        if self.missing_reason:
            payload["missing_reason"] = self.missing_reason
        return payload

    @classmethod
    def from_payload(cls, raw: object) -> AttachmentOccurrence:
        if not isinstance(raw, Mapping):
            raise AttachmentManifestError("attachment occurrence payload must be an object")
        attachment_id = raw.get("attachment_id")
        source_message_id = raw.get("source_message_id")
        ordinal = raw.get("ordinal")
        if not isinstance(attachment_id, str) or not attachment_id:
            raise AttachmentManifestError("attachment occurrence ID is missing")
        if not isinstance(source_message_id, str) or not source_message_id:
            raise AttachmentManifestError("attachment source message ID is missing")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
            raise AttachmentManifestError("attachment ordinal is invalid")
        state = raw.get("material_state", MATERIAL_MISSING)
        if state not in MATERIAL_STATES:
            raise AttachmentManifestError("attachment material state is invalid")
        source_entry_id = _entry_id(raw.get("source_entry_id"))
        size = _declared_size(raw.get("size"))
        created_at = _entry_id(raw.get("created_at")) or 0
        missing_reason_raw = raw.get("missing_reason")
        missing_reason = (
            _bounded_text(
                missing_reason_raw,
                fallback="attachment unavailable",
                max_bytes=_MAX_REASON_BYTES,
            )
            if isinstance(missing_reason_raw, str) and missing_reason_raw.strip()
            else None
        )
        return cls(
            attachment_id=attachment_id,
            source_entry_id=source_entry_id,
            source_message_id=source_message_id,
            ordinal=ordinal,
            sha256_ref=valid_sha256(raw.get("sha256_ref")),
            name=normalize_attachment_name(raw.get("name")),
            mime=normalize_attachment_mime(raw.get("mime")),
            size=size,
            material_state=str(state),
            created_at=created_at,
            missing_reason=missing_reason,
        )


def _logical_identity(occurrence: AttachmentOccurrence) -> tuple[object, ...]:
    """Identity independent of a physical transcript row ID.

    Forked sessions preserve message IDs and attachment IDs but allocate new
    database row IDs.  Consequently ``source_entry_id`` is deliberately not
    part of this identity.
    """

    source = occurrence.source_message_id or f"entry:{occurrence.source_entry_id}"
    # The hash is intentionally excluded here.  A legacy snapshot may have
    # indexed an occurrence before its inline bytes were decoded; a later
    # canonical rebuild should be able to fill in that hash.  If both sides do
    # carry a hash, ``_merge_occurrence`` checks that they agree.
    return (source, occurrence.ordinal)


def _merge_occurrence(
    old: AttachmentOccurrence,
    new: AttachmentOccurrence,
) -> AttachmentOccurrence:
    if _logical_identity(old) != _logical_identity(new):
        raise AttachmentManifestError(
            f"attachment ID collision for {old.attachment_id}"
        )
    if (
        old.sha256_ref is not None
        and new.sha256_ref is not None
        and old.sha256_ref != new.sha256_ref
    ):
        raise AttachmentManifestError(
            f"attachment ID collision for {old.attachment_id}"
        )
    # Prefer a usable material record over a degraded one, while retaining the
    # oldest source location for deterministic ordering.
    old_rank = (
        2
        if old.material_state == MATERIAL_AVAILABLE
        else 1
        if old.material_state == MATERIAL_INVALID
        else 0
    )
    new_rank = (
        2
        if new.material_state == MATERIAL_AVAILABLE
        else 1
        if new.material_state == MATERIAL_INVALID
        else 0
    )
    preferred = new if new_rank > old_rank else old
    return replace(
        preferred,
        source_entry_id=(
            old.source_entry_id
            if old.source_entry_id is not None
            else new.source_entry_id
        ),
        created_at=min(old.created_at, new.created_at),
        name=(old.name if old.name != "attachment" else new.name),
        mime=(old.mime if old.mime != "application/octet-stream" else new.mime),
    )


def merge_attachment_occurrences(
    existing: Iterable[AttachmentOccurrence],
    incoming: Iterable[AttachmentOccurrence],
) -> tuple[AttachmentOccurrence, ...]:
    """Merge occurrence snapshots, de-duplicating exact logical repeats."""

    by_id: dict[str, AttachmentOccurrence] = {}
    order: list[str] = []
    for occurrence in (*tuple(existing), *tuple(incoming)):
        if occurrence.attachment_id in by_id:
            by_id[occurrence.attachment_id] = _merge_occurrence(
                by_id[occurrence.attachment_id], occurrence
            )
        else:
            by_id[occurrence.attachment_id] = occurrence
            order.append(occurrence.attachment_id)
        if len(order) > _MAX_MANIFEST_OCCURRENCES:
            raise AttachmentManifestError("attachment manifest is too large")
    return tuple(by_id[attachment_id] for attachment_id in order)


@dataclass(frozen=True, slots=True)
class AttachmentManifest:
    """Portable attachment occurrence snapshot for one session identity."""

    session_id: str
    session_key: str
    occurrences: tuple[AttachmentOccurrence, ...] = ()
    covered_through_id: int = 0
    schema_version: int = ATTACHMENT_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.covered_through_id < 0:
            raise AttachmentManifestError("manifest coverage ID cannot be negative")
        if self.schema_version != ATTACHMENT_MANIFEST_SCHEMA_VERSION:
            raise AttachmentManifestError("unsupported attachment manifest schema")
        # Validate duplicate IDs at construction time as well as during merge;
        # callers often construct a snapshot directly from a migration.
        merge_attachment_occurrences((), self.occurrences)

    def by_id(self, attachment_id: str) -> AttachmentOccurrence | None:
        """Return one occurrence by exact logical ID."""

        for occurrence in self.occurrences:
            if occurrence.attachment_id == attachment_id:
                return occurrence
        return None

    def by_ids(self, attachment_ids: Iterable[str]) -> tuple[AttachmentOccurrence, ...]:
        """Return occurrences in caller-supplied ID order, skipping misses."""

        lookup = {occurrence.attachment_id: occurrence for occurrence in self.occurrences}
        return tuple(
            lookup[attachment_id]
            for attachment_id in attachment_ids
            if attachment_id in lookup
        )

    def merge(
        self,
        incoming: Iterable[AttachmentOccurrence],
        *,
        covered_through_id: int | None = None,
    ) -> AttachmentManifest:
        return replace(
            self,
            occurrences=merge_attachment_occurrences(self.occurrences, incoming),
            covered_through_id=max(
                self.covered_through_id,
                covered_through_id if covered_through_id is not None else 0,
            ),
        )

    def to_payload(self) -> dict[str, Any]:
        """Return the JSON-compatible context-state payload."""

        return {
            "schema_version": self.schema_version,
            "covered_through_id": self.covered_through_id,
            "occurrences": [occurrence.to_payload() for occurrence in self.occurrences],
        }

    @classmethod
    def from_payload(
        cls,
        payload: object,
        *,
        session_id: str,
        session_key: str,
    ) -> AttachmentManifest:
        if not isinstance(payload, Mapping):
            raise AttachmentManifestError("attachment manifest payload must be an object")
        schema_version = payload.get("schema_version", ATTACHMENT_MANIFEST_SCHEMA_VERSION)
        if schema_version != ATTACHMENT_MANIFEST_SCHEMA_VERSION:
            raise AttachmentManifestError("unsupported attachment manifest schema")
        covered = _entry_id(payload.get("covered_through_id")) or 0
        raw_occurrences = payload.get("occurrences", [])
        if not isinstance(raw_occurrences, (list, tuple)):
            raise AttachmentManifestError("attachment manifest occurrences must be an array")
        if len(raw_occurrences) > _MAX_MANIFEST_OCCURRENCES:
            raise AttachmentManifestError("attachment manifest is too large")
        parsed = tuple(AttachmentOccurrence.from_payload(item) for item in raw_occurrences)
        return cls(
            session_id=session_id,
            session_key=canonicalize_session_key(session_key),
            occurrences=merge_attachment_occurrences((), parsed),
            covered_through_id=covered,
            schema_version=ATTACHMENT_MANIFEST_SCHEMA_VERSION,
        )


def build_attachment_manifest(
    entries: Iterable[object],
    *,
    session_id: str,
    session_key: str,
    covered_through_id: int | None = None,
) -> AttachmentManifest:
    """Build a manifest from canonical active+archived transcript entries."""

    occurrences = extract_attachment_occurrences(entries, session_id=session_id)
    inferred_coverage = max(
        (occurrence.source_entry_id or 0 for occurrence in occurrences),
        default=0,
    )
    return AttachmentManifest(
        session_id=session_id,
        session_key=canonicalize_session_key(session_key),
        occurrences=merge_attachment_occurrences((), occurrences),
        covered_through_id=max(inferred_coverage, covered_through_id or 0),
    )


def lookup_attachment_occurrence(
    entries: Iterable[object],
    attachment_id: str,
    *,
    session_id: str | None = None,
) -> AttachmentOccurrence | None:
    """Look up an attachment in a canonical active/archive entry sequence.

    This pure helper is useful during lazy migration when a session has not
    acquired a manifest snapshot yet.  Callers normally pass the result of
    ``SessionStorage.get_canonical_transcript`` so both active and archived
    rows are covered in one deterministic scan.
    """

    occurrences = extract_attachment_occurrences(entries, session_id=session_id)
    for occurrence in occurrences:
        if occurrence.attachment_id == attachment_id:
            return occurrence
    return None


def manifest_context_state(
    manifest: AttachmentManifest,
    *,
    created_at: int | None = None,
) -> SessionContextState:
    """Create a portable context-state row for atomic compaction writes."""

    return SessionContextState(
        session_id=manifest.session_id,
        session_key=canonicalize_session_key(manifest.session_key),
        provider=ATTACHMENT_MANIFEST_PROVIDER,
        model=None,
        state_kind=ATTACHMENT_MANIFEST_STATE_KIND,
        payload=manifest.to_payload(),
        covered_through_id=manifest.covered_through_id,
        created_at=created_at if created_at is not None else _now_ms(),
        portable=True,
        cacheable=True,
        valid=True,
        schema_version=ATTACHMENT_MANIFEST_SCHEMA_VERSION,
    )


def attachment_manifest_from_context_state(
    state: SessionContextState,
) -> AttachmentManifest:
    """Decode and validate one stored context-state row."""

    if state.provider != ATTACHMENT_MANIFEST_PROVIDER:
        raise AttachmentManifestError("context state provider is not portable")
    if state.state_kind != ATTACHMENT_MANIFEST_STATE_KIND:
        raise AttachmentManifestError("context state is not an attachment manifest")
    return AttachmentManifest.from_payload(
        state.payload,
        session_id=state.session_id,
        session_key=state.session_key,
    ).merge((), covered_through_id=state.covered_through_id)


class AttachmentManifestStore:
    """Persistence/query adapter backed by ``SessionContextState`` snapshots."""

    def __init__(self, storage: AttachmentManifestStorage) -> None:
        self._storage = storage

    async def load(
        self,
        session_key: str,
        *,
        session_id: str | None = None,
    ) -> AttachmentManifest:
        """Load the newest valid snapshot, tolerating a corrupt newest row."""

        canonical_key = canonicalize_session_key(session_key)
        states = await self._storage.get_context_states(
            canonical_key,
            provider=ATTACHMENT_MANIFEST_PROVIDER,
            state_kind=ATTACHMENT_MANIFEST_STATE_KIND,
            valid_only=True,
        )
        # ``latest_context_state`` gives stable created_at/id ordering.  Walk
        # backwards if a partially written/old payload is malformed so one bad
        # migration row does not hide a previous usable snapshot.
        ordered = sorted(
            states,
            key=lambda state: (
                int(state.created_at or 0),
                int(state.id or 0),
            ),
        )
        for state in reversed(ordered):
            if session_id is not None and state.session_id != session_id:
                continue
            try:
                manifest = attachment_manifest_from_context_state(state)
            except AttachmentManifestError:
                continue
            return manifest
        return AttachmentManifest(
            session_id=session_id or "",
            session_key=canonical_key,
            occurrences=(),
        )

    async def save(self, manifest: AttachmentManifest) -> SessionContextState:
        """Append a new immutable snapshot and return its stored row."""

        return await self._storage.save_context_state(manifest_context_state(manifest))

    async def rebuild(
        self,
        *,
        session_id: str,
        session_key: str,
        entries: Iterable[object],
        covered_through_id: int | None = None,
    ) -> AttachmentManifest:
        """Replace the logical snapshot with a canonical transcript rebuild."""

        manifest = build_attachment_manifest(
            entries,
            session_id=session_id,
            session_key=session_key,
            covered_through_id=covered_through_id,
        )
        await self.save(manifest)
        return manifest

    async def rebuild_from_canonical(
        self,
        *,
        session_id: str,
        session_key: str,
        covered_through_id: int | None = None,
    ) -> AttachmentManifest:
        """Rebuild and persist a snapshot from active plus archived rows."""

        entries = await self._storage.get_canonical_transcript(session_id)
        return await self.rebuild(
            session_id=session_id,
            session_key=session_key,
            entries=entries,
            covered_through_id=covered_through_id,
        )

    async def merge_entries(
        self,
        *,
        session_id: str,
        session_key: str,
        entries: Iterable[object],
        covered_through_id: int | None = None,
    ) -> AttachmentManifest:
        """Merge newly observed entries into the latest durable snapshot."""

        current = await self.load(session_key, session_id=session_id)
        incoming = extract_attachment_occurrences(entries, session_id=session_id)
        merged = current.merge(incoming, covered_through_id=covered_through_id)
        await self.save(merged)
        return merged

    async def lookup(
        self,
        session_key: str,
        attachment_id: str,
        *,
        session_id: str | None = None,
    ) -> AttachmentOccurrence | None:
        """Find one occurrence by exact ID in the latest valid snapshot."""

        return (await self.load(session_key, session_id=session_id)).by_id(attachment_id)

    async def lookup_many(
        self,
        session_key: str,
        attachment_ids: Iterable[str],
        *,
        session_id: str | None = None,
    ) -> tuple[AttachmentOccurrence, ...]:
        """Find multiple occurrences while preserving requested order."""

        return (await self.load(session_key, session_id=session_id)).by_ids(attachment_ids)


__all__ = [
    "ATTACHMENT_MANIFEST_PROVIDER",
    "ATTACHMENT_MANIFEST_SCHEMA_VERSION",
    "ATTACHMENT_MANIFEST_STATE_KIND",
    "MATERIAL_AVAILABLE",
    "MATERIAL_INVALID",
    "MATERIAL_MISSING",
    "AttachmentManifest",
    "AttachmentManifestError",
    "AttachmentManifestStore",
    "AttachmentOccurrence",
    "attachment_manifest_from_context_state",
    "build_attachment_manifest",
    "deterministic_attachment_id",
    "extract_attachment_occurrences",
    "extract_attachment_occurrences_from_envelope",
    "legacy_attachment_id",
    "lookup_attachment_occurrence",
    "manifest_context_state",
    "merge_attachment_occurrences",
    "normalize_attachment_mime",
    "normalize_attachment_name",
    "valid_attachment_id",
    "valid_sha256",
]
