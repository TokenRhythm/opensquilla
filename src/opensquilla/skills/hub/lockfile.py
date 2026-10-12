"""Versioned, loss-aware lockfile management for installed Community skills."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opensquilla.skills.file_hash import _stream_file_into_digest
from opensquilla.skills.hub.archive import normalize_relative_path
from opensquilla.skills.hub.contracts import (
    DiagnosticPhase,
    DiagnosticSeverity,
    SkillDiagnostic,
)
from opensquilla.skills.tree import compute_tree_sha256

LOCKFILE_SCHEMA_VERSION = 2

_IGNORED_FILE_PREDICATE_ERRNOS = frozenset(
    {errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP}
)
_IGNORED_FILE_PREDICATE_WINERRORS = frozenset({21, 123, 1921})

_STRING_ENTRY_FIELDS = (
    "source",
    "identifier",
    "version",
    "installed_at",
    "path",
    "sha256",
    "license",
    "upstream_url",
    "source_trust",
    "scan_verdict",
    "scan_strategy",
    "install_id",
    "manifest_name",
    "directory_name",
    "relative_path",
    "requested_identifier",
    "resolved_identifier",
    "resolved_version",
    "resolved_revision",
    "artifact_sha256",
    "tree_sha256",
    "parser_version",
    "dialect",
    "source_package_id",
)
_INTEGER_ENTRY_FIELDS = ("file_count", "total_bytes")
_KNOWN_ENTRY_FIELDS = frozenset(
    (*_STRING_ENTRY_FIELDS, *_INTEGER_ENTRY_FIELDS, "accepted_risk_override", "scan_findings")
)
_V2_ONLY_ENTRY_FIELDS = frozenset(
    {
        "install_id",
        "manifest_name",
        "directory_name",
        "relative_path",
        "requested_identifier",
        "resolved_identifier",
        "resolved_version",
        "resolved_revision",
        "artifact_sha256",
        "tree_sha256",
        "file_count",
        "total_bytes",
        "parser_version",
        "dialect",
        "source_package_id",
        "accepted_risk_override",
    }
)
_DEGRADED_IDENTITY_MARKER = "_opensquilla_identity_metadata_lost"


class LockfileMutationBlockedError(RuntimeError):
    """Raised when mutating a missing-trust or malformed lockfile would lose state."""

    def __init__(self, path: Path | str, diagnostics: list[SkillDiagnostic]) -> None:
        self.path = str(path)
        self.diagnostics = tuple(diagnostics)
        detail = diagnostics[0].message if diagnostics else "lockfile is not mutable"
        super().__init__(f"Skill lockfile mutation blocked for {self.path}: {detail}")


class LockfileIdentityAmbiguousError(LookupError):
    """Raised when a legacy runtime-name selector matches multiple installs."""

    def __init__(self, selector: str, candidates: list[str]) -> None:
        self.selector = selector
        self.candidates = tuple(candidates)
        super().__init__(
            f"Skill name {selector!r} matches multiple installs; use installId"
        )


@dataclass
class LockEntry:
    """A single installed Skill entry in the storage-keyed v2 lockfile.

    The original v1 fields remain first-class.  New source-resolution and
    validation fields are additive and default safely so existing installers can
    continue constructing ``LockEntry(source=..., identifier=...)``.
    """

    source: str = ""
    identifier: str = ""
    version: str = ""
    installed_at: str = ""
    path: str = ""
    sha256: str = ""
    license: str = ""
    upstream_url: str = ""
    source_trust: str = ""
    scan_verdict: str = ""
    scan_strategy: str = ""
    scan_findings: list[dict[str, Any]] = field(default_factory=list)

    # v2 identity and reproducibility fields. ``path`` remains for legacy
    # readers; ``relative_path`` is the portable path beneath the managed root.
    install_id: str = ""
    manifest_name: str = ""
    directory_name: str = ""
    relative_path: str = ""
    requested_identifier: str = ""
    resolved_identifier: str = ""
    resolved_version: str = ""
    resolved_revision: str = ""
    artifact_sha256: str = ""
    tree_sha256: str = ""
    file_count: int = 0
    total_bytes: int = 0
    parser_version: str = ""
    dialect: str = ""
    source_package_id: str = ""
    accepted_risk_override: bool = False

    # Unknown entry fields survive load/save so a newer writer is not silently
    # destroyed by this version when its core schema is still understood.
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    # Runtime-only guard for a schema-v2 entry that passed through the legacy
    # field-filtering writer. The persisted marker in ``extra`` keeps the guard
    # active after safe derived fields have been reconstructed and saved.
    identity_metadata_degraded: bool = field(default=False, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.extra)
        for field_name in _STRING_ENTRY_FIELDS:
            payload[field_name] = getattr(self, field_name)
        payload["file_count"] = self.file_count
        payload["total_bytes"] = self.total_bytes
        payload["accepted_risk_override"] = self.accepted_risk_override
        payload["scan_findings"] = [dict(item) for item in self.scan_findings]
        return payload

    def as_dict(self) -> dict[str, Any]:
        return self.to_dict()


@dataclass
class Lockfile:
    """Storage-keyed Skill lockfile with explicit load diagnostics.

    A malformed or unsupported file is readable as a diagnostic object but all
    mutation methods fail closed.  This prevents callers from silently replacing
    a damaged installation record with an empty lockfile.
    """

    version: int = LOCKFILE_SCHEMA_VERSION
    installed: dict[str, LockEntry] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict, repr=False)
    source_index_extensions: dict[tuple[str, str], dict[str, Any]] = field(
        default_factory=dict,
        repr=False,
    )
    loaded_version: int = LOCKFILE_SCHEMA_VERSION
    diagnostics: list[SkillDiagnostic] = field(default_factory=list, repr=False)
    mutation_blocked: bool = False
    source_path: str = field(default="", repr=False)

    @staticmethod
    def load(path: Path, *, managed_dir: Path | None = None) -> Lockfile:
        """Read v2, v1, or the historical top-level ``skills`` shape.

        Missing files are valid empty v2 lockfiles. Parse, shape, I/O, and future
        schema errors produce a fail-closed object with wire-safe diagnostics.
        """

        try:
            exists = path.exists()
        except OSError as exc:
            return _blocked_lockfile(path, "LOCKFILE_IO_ERROR", str(exc))
        if not exists:
            return Lockfile(source_path=str(path))

        try:
            raw_text = path.read_text(encoding="utf-8")
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            return _blocked_lockfile(
                path,
                "LOCKFILE_CORRUPT",
                f"Invalid JSON at line {exc.lineno}, column {exc.colno}",
            )
        except (OSError, UnicodeError) as exc:
            return _blocked_lockfile(path, "LOCKFILE_IO_ERROR", str(exc))

        if not isinstance(data, dict):
            return _blocked_lockfile(
                path,
                "LOCKFILE_INVALID_SHAPE",
                "Lockfile root must be a JSON object",
            )

        diagnostics: list[SkillDiagnostic] = []
        loaded_version = _parse_version(data.get("version", 1), path, diagnostics)
        mutation_blocked = any(item.blocking for item in diagnostics)
        if loaded_version > LOCKFILE_SCHEMA_VERSION:
            diagnostics.append(
                _diagnostic(
                    "LOCKFILE_VERSION_UNSUPPORTED",
                    (
                        f"Lockfile schema {loaded_version} is newer than supported "
                        f"schema {LOCKFILE_SCHEMA_VERSION}"
                    ),
                    path,
                    blocking=True,
                    hint="Upgrade OpenSquilla before changing installed skills.",
                )
            )
            mutation_blocked = True

        raw_entries: object
        historical = False
        if "installed" in data:
            raw_entries = data["installed"]
        elif "skills" in data:
            raw_entries = data["skills"]
            historical = True
        else:
            raw_entries = {}

        normalized = _normalize_entry_container(
            raw_entries,
            path=path,
            diagnostics=diagnostics,
            historical=historical,
        )
        entries: dict[str, LockEntry] = {}
        degraded_storage_keys: list[str] = []
        recovered_runtime_names: dict[str, str] = {}
        for name, raw_entry in normalized.items():
            entry = _parse_entry(name, raw_entry, path=path, diagnostics=diagnostics)
            if entry is not None:
                degraded = (
                    loaded_version == LOCKFILE_SCHEMA_VERSION
                    and _is_v2_identity_degraded(raw_entry, entry)
                )
                if degraded:
                    entry.identity_metadata_degraded = True
                    entry.extra[_DEGRADED_IDENTITY_MARKER] = True
                    recovered_name = _recover_derived_identity(
                        storage_key=name,
                        entry=entry,
                        lockfile_path=path,
                        managed_dir=managed_dir,
                    )
                    degraded_storage_keys.append(name)
                    if recovered_name:
                        recovered_runtime_names[name] = recovered_name
                entries[name] = entry

        if degraded_storage_keys:
            diagnostics.append(
                _diagnostic(
                    "LOCKFILE_IDENTITY_METADATA_LOST",
                    (
                        "Schema-v2 Skill identity metadata was removed by an older "
                        "lockfile writer"
                    ),
                    path,
                    blocking=False,
                    severity=DiagnosticSeverity.WARNING,
                    hint=(
                        "Keep this OpenSquilla version active, then update or reinstall "
                        "each affected Skill to restore exact source and install identity; "
                        "restore skills-lock.json.bak first if exact installId selection is "
                        "required."
                    ),
                    details={
                        "storageKeys": degraded_storage_keys,
                        "recoveredRuntimeNames": recovered_runtime_names,
                    },
                )
            )

        _validate_store_paths(entries, path=path, diagnostics=diagnostics)
        _validate_install_ids(entries, path=path, diagnostics=diagnostics)

        mutation_blocked = mutation_blocked or any(item.blocking for item in diagnostics)
        extra = {
            key: value
            for key, value in data.items()
            if key not in {"version", "installed", "skills", "source_index"}
        }
        return Lockfile(
            version=LOCKFILE_SCHEMA_VERSION,
            installed=entries,
            extra=extra,
            source_index_extensions=_source_index_extensions(data.get("source_index")),
            loaded_version=loaded_version,
            diagnostics=diagnostics,
            mutation_blocked=mutation_blocked,
            source_path=str(path),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.extra)
        payload["version"] = LOCKFILE_SCHEMA_VERSION
        payload["installed"] = {
            name: self.installed[name].to_dict() for name in sorted(self.installed)
        }
        payload["source_index"] = self.source_index
        return payload

    def as_dict(self) -> dict[str, Any]:
        return self.to_dict()

    @property
    def source_index(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Return the secondary source identity index rebuilt from ``installed``.

        Persisted index targets are never authoritative. Unknown fields attached
        to a still-valid source/identifier pair survive as additive extensions.
        """

        return _build_source_index(self.installed, self.source_index_extensions)

    def save(self, path: Path) -> None:
        """Atomically save v2 and retain the previous valid file as ``.bak``."""

        self._ensure_mutable(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        previous: bytes | None = None
        try:
            if path.exists():
                current = Lockfile.load(path)
                current._ensure_mutable(path)
                previous = path.read_bytes()
        except LockfileMutationBlockedError:
            raise
        except OSError as exc:
            raise LockfileMutationBlockedError(
                path,
                [_diagnostic("LOCKFILE_IO_ERROR", str(exc), path, blocking=True)],
            ) from exc

        if previous is not None:
            _atomic_write(lockfile_backup_path(path), previous)

        encoded = (
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        _atomic_write(path, encoded)
        self.version = LOCKFILE_SCHEMA_VERSION
        self.loaded_version = LOCKFILE_SCHEMA_VERSION
        self.source_path = str(path)

    def add(self, storage_key: str, entry: LockEntry) -> None:
        self._ensure_mutable(self.source_path or "<memory>")
        self.installed[storage_key] = entry

    def remove(self, storage_key: str) -> bool:
        self._ensure_mutable(self.source_path or "<memory>")
        if storage_key in self.installed:
            del self.installed[storage_key]
            return True
        return False

    def get(self, storage_key: str) -> LockEntry | None:
        """Return an entry by its exact persisted storage key."""

        return self.installed.get(storage_key)

    def find_by_install_id(self, install_id: str) -> tuple[str, LockEntry] | None:
        """Resolve one exact install identity without consulting runtime names."""

        if not install_id:
            return None
        matches = [
            (storage_key, entry)
            for storage_key, entry in self.installed.items()
            if entry.install_id == install_id
        ]
        return matches[0] if len(matches) == 1 else None

    def keys_for_manifest_name(self, name: str) -> list[str]:
        """Return every storage key whose runtime manifest name matches ``name``."""

        return [
            storage_key
            for storage_key, entry in self.installed.items()
            if entry.manifest_name == name
            or (
                not entry.manifest_name
                and not entry.identity_metadata_degraded
                and storage_key == name
            )
        ]

    def resolve_key(
        self,
        selector: str = "",
        *,
        install_id: str = "",
    ) -> str | None:
        """Resolve exact install id, storage key, or one unambiguous runtime name.

        Runtime-name lookup is authoritative for public name selectors and
        fails closed when two packages expose the same name. A v1 storage key
        remains a compatibility selector only while that entry has no recorded
        ``manifest_name`` and no runtime-name collision exists.
        """

        if install_id:
            match = self.find_by_install_id(install_id)
            if match is None:
                return None
            storage_key, _entry = match
            if selector:
                entry = self.installed[storage_key]
                runtime_name = entry.manifest_name or storage_key
                legacy_storage_selector = (
                    not entry.manifest_name
                    and not entry.identity_metadata_degraded
                    and selector == storage_key
                    and not self.keys_for_manifest_name(selector)
                )
                if selector != runtime_name and not legacy_storage_selector:
                    return None
            return storage_key
        if not selector:
            return None
        matches = self.keys_for_manifest_name(selector)
        legacy_exact = self.installed.get(selector)
        if (
            legacy_exact is not None
            and not legacy_exact.manifest_name
            and not legacy_exact.identity_metadata_degraded
            and selector not in matches
        ):
            matches.append(selector)
        if len(matches) > 1:
            raise LockfileIdentityAmbiguousError(selector, matches)
        return matches[0] if matches else None

    def _ensure_mutable(self, path: Path | str) -> None:
        if self.mutation_blocked:
            raise LockfileMutationBlockedError(path, self.diagnostics)


def lockfile_backup_path(path: Path) -> Path:
    """Return the single-generation backup path for ``path``."""

    return path.with_name(f"{path.name}.bak")


def _source_index_extensions(raw: object) -> dict[tuple[str, str], dict[str, Any]]:
    """Retain only unknown fields associated with structurally usable index rows."""

    if not isinstance(raw, dict):
        return {}
    extensions: dict[tuple[str, str], dict[str, Any]] = {}
    for source_id, raw_identifiers in raw.items():
        if not isinstance(source_id, str) or not isinstance(raw_identifiers, dict):
            continue
        for identifier, raw_target in raw_identifiers.items():
            if not isinstance(identifier, str) or not isinstance(raw_target, dict):
                continue
            extra = {
                key: value
                for key, value in raw_target.items()
                if key not in {"name", "manifest_name", "storage_key", "install_id"}
            }
            if extra:
                extensions[(source_id, identifier)] = extra
    return extensions


def _build_source_index(
    installed: dict[str, LockEntry],
    extensions: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    index: dict[str, dict[str, dict[str, Any]]] = {}
    for storage_key in sorted(installed):
        entry = installed[storage_key]
        source_id = entry.source
        identifier = (
            entry.resolved_identifier or entry.identifier or entry.requested_identifier
        )
        if not source_id or not identifier:
            continue
        source_entries = index.setdefault(source_id, {})
        if identifier in source_entries:
            # The storage-keyed record remains authoritative. Management services
            # prevent new collisions; old collisions resolve deterministically.
            continue
        target = dict(extensions.get((source_id, identifier), {}))
        # ``name`` was the storage-key pointer in the original v2 index. Keep
        # that meaning for older readers and expose the runtime name additively.
        target["name"] = storage_key
        target["manifest_name"] = entry.manifest_name or (
            "" if entry.identity_metadata_degraded else storage_key
        )
        target["storage_key"] = storage_key
        target["install_id"] = entry.install_id
        source_entries[identifier] = target
    return index


def _parse_version(
    raw: object,
    path: Path,
    diagnostics: list[SkillDiagnostic],
) -> int:
    if isinstance(raw, bool):
        value = 0
    elif isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.isdigit():
        value = int(raw)
    else:
        value = 0
    if value >= 1:
        return value
    diagnostics.append(
        _diagnostic(
            "LOCKFILE_INVALID_VERSION",
            "Lockfile version must be a positive integer",
            path,
            blocking=True,
            field_name="version",
        )
    )
    return 1


def _normalize_entry_container(
    raw: object,
    *,
    path: Path,
    diagnostics: list[SkillDiagnostic],
    historical: bool,
) -> dict[str, object]:
    if isinstance(raw, dict):
        return {str(name): entry for name, entry in raw.items()}
    if historical and isinstance(raw, list):
        entries: dict[str, object] = {}
        for index, item in enumerate(raw):
            if isinstance(item, str) and item:
                name = item
                entry: object = {"identifier": item}
            elif isinstance(item, dict):
                name = str(
                    item.get("name")
                    or item.get("skill_name")
                    or item.get("identifier")
                    or ""
                )
                entry = item
            else:
                name = ""
                entry = item
            if not name:
                diagnostics.append(
                    _diagnostic(
                        "LOCKFILE_INVALID_ENTRY",
                        f"Historical skills entry {index} has no usable name",
                        path,
                        blocking=True,
                        field_name=f"skills[{index}]",
                    )
                )
                continue
            if name in entries:
                diagnostics.append(
                    _diagnostic(
                        "LOCKFILE_DUPLICATE_NAME",
                        f"Historical lockfile contains duplicate Skill name {name!r}",
                        path,
                        blocking=True,
                        field_name=f"skills[{index}]",
                    )
                )
                continue
            entries[name] = entry
        return entries

    diagnostics.append(
        _diagnostic(
            "LOCKFILE_INVALID_SHAPE",
            (
                "Historical skills must be an array or object"
                if historical
                else "installed must be an object"
            ),
            path,
            blocking=True,
            field_name="skills" if historical else "installed",
        )
    )
    return {}


def _parse_entry(
    name: str,
    raw: object,
    *,
    path: Path,
    diagnostics: list[SkillDiagnostic],
) -> LockEntry | None:
    if not name:
        diagnostics.append(
            _diagnostic(
                "LOCKFILE_INVALID_ENTRY",
                "Installed Skill name must not be empty",
                path,
                blocking=True,
                field_name="installed",
            )
        )
        return None
    if isinstance(raw, str):
        raw = {"identifier": raw}
    if not isinstance(raw, dict):
        diagnostics.append(
            _diagnostic(
                "LOCKFILE_INVALID_ENTRY",
                f"Installed Skill {name!r} must be an object",
                path,
                blocking=True,
                field_name=f"installed.{name}",
            )
        )
        return None

    values: dict[str, Any] = {}
    for field_name in _STRING_ENTRY_FIELDS:
        value = raw.get(field_name, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            diagnostics.append(
                _diagnostic(
                    "LOCKFILE_INVALID_ENTRY_FIELD",
                    f"{name}.{field_name} must be a string",
                    path,
                    blocking=True,
                    field_name=f"installed.{name}.{field_name}",
                )
            )
            value = ""
        values[field_name] = value

    for field_name in _INTEGER_ENTRY_FIELDS:
        value = raw.get(field_name, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            diagnostics.append(
                _diagnostic(
                    "LOCKFILE_INVALID_ENTRY_FIELD",
                    f"{name}.{field_name} must be a non-negative integer",
                    path,
                    blocking=True,
                    field_name=f"installed.{name}.{field_name}",
                )
            )
            value = 0
        values[field_name] = value

    accepted_risk = raw.get("accepted_risk_override", False)
    if not isinstance(accepted_risk, bool):
        diagnostics.append(
            _diagnostic(
                "LOCKFILE_INVALID_ENTRY_FIELD",
                f"{name}.accepted_risk_override must be a boolean",
                path,
                blocking=True,
                field_name=f"installed.{name}.accepted_risk_override",
            )
        )
        accepted_risk = False
    values["accepted_risk_override"] = accepted_risk

    raw_findings = raw.get("scan_findings", [])
    if not isinstance(raw_findings, list) or not all(
        isinstance(item, dict) for item in raw_findings
    ):
        diagnostics.append(
            _diagnostic(
                "LOCKFILE_INVALID_ENTRY_FIELD",
                f"{name}.scan_findings must be an array of objects",
                path,
                blocking=True,
                field_name=f"installed.{name}.scan_findings",
            )
        )
        raw_findings = []
    values["scan_findings"] = [dict(item) for item in raw_findings]
    values["extra"] = {
        key: value
        for key, value in raw.items()
        if key not in _KNOWN_ENTRY_FIELDS
    }
    return LockEntry(**values)


def _is_v2_identity_degraded(raw: object, entry: LockEntry) -> bool:
    """Detect a v2 row rewritten by the v1 field-filtering serializer."""

    if entry.extra.get(_DEGRADED_IDENTITY_MARKER) is True:
        return True
    if not isinstance(raw, dict):
        return True
    return not any(field_name in raw for field_name in _V2_ONLY_ENTRY_FIELDS)


def _recover_derived_identity(
    *,
    storage_key: str,
    entry: LockEntry,
    lockfile_path: Path,
    managed_dir: Path | None,
) -> str:
    """Recover only path/runtime fields provable from the managed tree.

    Exact install ids, immutable revisions, and publisher-qualified package
    identities are deliberately left empty. Those values cannot be derived
    from local bytes after an older writer removed them.
    """

    try:
        portable = normalize_relative_path(storage_key)
    except ValueError:
        return ""
    if len(portable.parts) != 1:
        return ""

    entry.directory_name = storage_key
    entry.relative_path = storage_key

    root = managed_dir
    if root is None and lockfile_path.name == "skills-lock.json":
        root = lockfile_path.parent / "skills"
    if root is None:
        return ""

    target = root / storage_key
    manifest = target / "SKILL.md"
    try:
        resolved_root = root.resolve(strict=False)
        resolved_target = target.resolve(strict=False)
        if (
            resolved_target.parent != resolved_root
            or target.is_symlink()
            or manifest.is_symlink()
            or not manifest.is_file()
        ):
            return ""
        from opensquilla.skills.manifest import (
            SkillCompileProfile,
            compile_skill_manifest,
        )
        from opensquilla.skills.types import SkillLayer

        spec = compile_skill_manifest(
            target,
            SkillLayer.MANAGED,
            profile=SkillCompileProfile.COMMUNITY_INSTRUCTION,
        )
    except (OSError, UnicodeError, TypeError, ValueError):
        return ""
    entry.manifest_name = spec.name
    return spec.name


def _validate_store_paths(
    entries: dict[str, LockEntry],
    *,
    path: Path,
    diagnostics: list[SkillDiagnostic],
) -> None:
    """Fail closed when persisted installs alias one portable store child."""

    seen: dict[tuple[str, ...], tuple[str, str]] = {}
    for storage_key, entry in entries.items():
        relative = entry.relative_path or entry.directory_name or storage_key
        try:
            portable = normalize_relative_path(relative)
        except ValueError as exc:
            diagnostics.append(
                _diagnostic(
                    "LOCKFILE_STORE_PATH_UNSAFE",
                    f"Installed entry {storage_key!r} has an unsafe store path: {exc}",
                    path,
                    blocking=True,
                    field_name=f"installed.{storage_key}.relative_path",
                )
            )
            continue
        if len(portable.parts) != 1:
            diagnostics.append(
                _diagnostic(
                    "LOCKFILE_STORE_PATH_UNSAFE",
                    f"Installed entry {storage_key!r} is not a direct managed-root child",
                    path,
                    blocking=True,
                    field_name=f"installed.{storage_key}.relative_path",
                )
            )
            continue
        collision_key = tuple(part.casefold() for part in portable.parts)
        previous = seen.get(collision_key)
        if previous is not None:
            previous_key, previous_path = previous
            diagnostics.append(
                _diagnostic(
                    "LOCKFILE_STORE_PATH_COLLISION",
                    (
                        f"Installed entries {previous_key!r} and {storage_key!r} "
                        f"alias the portable store path {previous_path!r}/{relative!r}"
                    ),
                    path,
                    blocking=True,
                    field_name=f"installed.{storage_key}.relative_path",
                )
            )
            continue
        seen[collision_key] = (storage_key, relative)


def _validate_install_ids(
    entries: dict[str, LockEntry],
    *,
    path: Path,
    diagnostics: list[SkillDiagnostic],
) -> None:
    """Fail closed when an allegedly exact install identity is not unique."""

    seen: dict[str, str] = {}
    for storage_key, entry in entries.items():
        install_id = entry.install_id
        if not install_id:
            continue
        previous_key = seen.get(install_id)
        if previous_key is not None:
            diagnostics.append(
                _diagnostic(
                    "LOCKFILE_INSTALL_ID_COLLISION",
                    (
                        f"Installed entries {previous_key!r} and {storage_key!r} "
                        f"share install id {install_id!r}"
                    ),
                    path,
                    blocking=True,
                    field_name=f"installed.{storage_key}.install_id",
                )
            )
            continue
        seen[install_id] = storage_key


def _blocked_lockfile(path: Path, code: str, message: str) -> Lockfile:
    diagnostic = _diagnostic(code, message, path, blocking=True)
    return Lockfile(
        diagnostics=[diagnostic],
        mutation_blocked=True,
        source_path=str(path),
    )


def _diagnostic(
    code: str,
    message: str,
    path: Path,
    *,
    blocking: bool,
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR,
    field_name: str = "",
    hint: str = "Repair or restore the lockfile before changing installed skills.",
    details: dict[str, Any] | None = None,
) -> SkillDiagnostic:
    return SkillDiagnostic(
        code=code,
        severity=severity,
        phase=DiagnosticPhase.LOCK,
        message=message,
        blocking=blocking,
        path=str(path),
        field_name=field_name,
        hint=hint,
        details=dict(details or {}),
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            # Cleanup is best-effort and must not mask the atomic-write outcome.
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory durability; directory handles are unavailable on Windows."""

    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Some platforms and filesystems do not support fsync on directories.
        # The atomic file replacement has already completed, so durability here
        # remains best-effort by contract.
        pass
    finally:
        os.close(descriptor)


def compute_sha256(directory: Path) -> str:
    """Compute the legacy SHA-256 digest of all non-dotfiles in a directory."""

    hasher = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        try:
            path_info = path.lstat()
            info = path.stat() if stat.S_ISLNK(path_info.st_mode) else path_info
        except OSError as exc:
            # Match Python 3.12 ``Path.is_file()``: missing, broken, or invalid
            # paths are skipped, while permission and I/O failures propagate.
            if (
                exc.errno not in _IGNORED_FILE_PREDICATE_ERRNOS
                and getattr(exc, "winerror", None)
                not in _IGNORED_FILE_PREDICATE_WINERRORS
            ):
                raise
            continue
        except ValueError:
            # ``Path.is_file()`` also treats non-encodable paths as non-files.
            continue
        if stat.S_ISREG(info.st_mode) and not any(
            part.startswith(".") for part in relative.parts
        ):
            hasher.update(str(relative).encode())
            _stream_file_into_digest(
                path,
                hasher,
                follow_symlinks=True,
                expected_stat=info,
                expected_path_stat=path_info,
            )
    return hasher.hexdigest()


__all__ = [
    "LOCKFILE_SCHEMA_VERSION",
    "LockEntry",
    "Lockfile",
    "LockfileIdentityAmbiguousError",
    "LockfileMutationBlockedError",
    "compute_sha256",
    "compute_tree_sha256",
    "lockfile_backup_path",
]
