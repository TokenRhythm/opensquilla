"""Offline-safe diagnostics for managed Community Skills.

The doctor is deliberately observational.  It reads the managed tree, the
canonical lockfile, and (when supplied) an already-published loader snapshot.
It never refreshes the catalog, resolves a remote source, executes Skill code,
or asks an LLM to interpret third-party content.
"""

from __future__ import annotations

import codecs
import os
import re
import stat
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any

from opensquilla.skills.eligibility import (
    EligibilityContext,
    EligibilityReport,
    diagnose_eligibility,
)
from opensquilla.skills.hub.archive import normalize_relative_path
from opensquilla.skills.hub.contracts import (
    DiagnosticPhase,
    DiagnosticSeverity,
    SkillCompatibilityState,
    SkillDiagnostic,
    SkillInstallState,
    SkillInvocationCapabilities,
    SkillLifecycle,
    SkillLoadState,
    SkillReadinessState,
    SkillSelectionState,
)
from opensquilla.skills.hub.lockfile import (
    LockEntry,
    Lockfile,
    compute_sha256,
    compute_tree_sha256,
)
from opensquilla.skills.hub.source import SourceResolution
from opensquilla.skills.hub.transaction import inspect_pending_skill_transaction
from opensquilla.skills.manifest import SkillCompileProfile, compile_skill_manifest
from opensquilla.skills.types import SkillLayer, SkillSpec

if TYPE_CHECKING:
    from opensquilla.skills.loader import SkillLoader


_RESOURCE_DIRS = frozenset({"assets", "references", "scripts", "templates"})
_RESERVED_INTERNAL_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".opensquilla",
        ".opensquilla-install.json",
        ".opensquilla-provenance.json",
        ".provenance.json",
        "__macosx",
    }
)
_MAX_TREE_ENTRIES = 2_048
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_DEGRADED_CAPABILITIES_KEY = "degraded_capabilities"
_SCOPED_TOOL_PERMISSIONS_CAPABILITY = "scoped_tool_permissions"
_DYNAMIC_CONTEXT_CAPABILITY = "dynamic_context"
_UNSUPPORTED_EXECUTION_CAPABILITY = "unsupported_execution_fields"


def _diagnostic(
    code: str,
    severity: DiagnosticSeverity,
    phase: DiagnosticPhase,
    message: str,
    *,
    blocking: bool = False,
    path: str = "",
    field_name: str = "",
    hint: str = "",
    details: dict[str, Any] | None = None,
) -> SkillDiagnostic:
    return SkillDiagnostic(
        code=code,
        severity=severity,
        phase=phase,
        message=message,
        blocking=blocking,
        path=path,
        field_name=field_name,
        hint=hint,
        details=details or {},
    )


@dataclass(frozen=True)
class SkillDoctorItem:
    """One managed Skill instance and all independently observed states."""

    name: str
    install_id: str
    path: str
    status: str
    lifecycle: SkillLifecycle
    resolution: SourceResolution | None = None
    diagnostics: tuple[SkillDiagnostic, ...] = ()

    @property
    def installed(self) -> bool:
        return self.lifecycle.install_state in {
            SkillInstallState.TRACKED,
            SkillInstallState.UNTRACKED,
            SkillInstallState.DRIFTED,
        }

    @property
    def active(self) -> bool:
        return (
            self.lifecycle.load_state
            in {SkillLoadState.LOADED, SkillLoadState.SERVING_PREVIOUS}
            and self.lifecycle.selection_state is SkillSelectionState.ACTIVE
        )

    @property
    def instruction_usable(self) -> bool:
        return self.lifecycle.usable is True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "installId": self.install_id,
            "path": self.path,
            "status": self.status,
            "installed": self.installed,
            "active": self.active,
            "instruction_usable": self.instruction_usable,
            "lifecycle": self.lifecycle.to_dict(),
            "resolution": self.resolution.to_dict() if self.resolution else None,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }

    def as_dict(self) -> dict[str, Any]:
        return self.to_dict()


@dataclass(frozen=True)
class SkillDoctorReport:
    """Serializable result returned by :class:`SkillDoctor`."""

    skills: tuple[SkillDoctorItem, ...]
    diagnostics: tuple[SkillDiagnostic, ...] = ()
    catalog_generation: int | None = None
    target: str = ""
    constraints: dict[str, bool] = field(
        default_factory=lambda: {
            "network": False,
            "scripts": False,
            "llm": False,
        }
    )

    @property
    def ok(self) -> bool:
        return not any(
            item.blocking
            for item in (
                *self.diagnostics,
                *(diagnostic for skill in self.skills for diagnostic in skill.diagnostics),
            )
        )

    def to_dict(self) -> dict[str, Any]:
        statuses = {"ready": 0, "needs_setup": 0, "not_declared": 0}
        for item in self.skills:
            statuses[item.status] = statuses.get(item.status, 0) + 1
        blocking = sum(item.blocking for item in self.diagnostics) + sum(
            diagnostic.blocking
            for skill in self.skills
            for diagnostic in skill.diagnostics
        )
        return {
            "ok": self.ok,
            "target": self.target or None,
            "catalogGeneration": self.catalog_generation,
            "summary": {
                "checked": len(self.skills),
                "blocking": blocking,
                **statuses,
            },
            "constraints": dict(self.constraints),
            "skills": [item.to_dict() for item in self.skills],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }

    def as_dict(self) -> dict[str, Any]:
        return self.to_dict()


@dataclass(frozen=True)
class _StoreRecord:
    lock_name: str
    entry: LockEntry | None
    path: Path | None
    path_diagnostics: tuple[SkillDiagnostic, ...] = ()


@dataclass(frozen=True)
class _CatalogObservation:
    spec: SkillSpec | None
    load_state: SkillLoadState
    selection_state: SkillSelectionState
    generation: int | None
    diagnostics: tuple[SkillDiagnostic, ...]
    live: bool


class SkillDoctor:
    """Read-only Doctor for one managed Skill store.

    ``loader`` is optional and is never asked to refresh.  Supplying it allows
    the report to distinguish the current winner, a shadowed candidate, and a
    last-known-good catalog.  Without it, a valid on-disk manifest is reported
    as ``validated_offline`` rather than active.
    """

    def __init__(
        self,
        *,
        managed_dir: Path,
        lockfile_path: Path,
        loader: SkillLoader | None = None,
        journal_path: Path | None = None,
        eligibility_context: EligibilityContext | None = None,
        additional_diagnostics: Iterable[SkillDiagnostic] = (),
    ) -> None:
        self._managed_dir = managed_dir
        self._lockfile_path = lockfile_path
        self._loader = loader
        self._journal_path = journal_path
        # Doctor advertises a no-script observational contract.  Preserve any
        # caller-supplied environment/cache state while forcing managed binary
        # lookup onto the passive receipt-only resolver.
        self._eligibility_context = replace(
            eligibility_context or EligibilityContext.auto(),
            passive_managed_bins=True,
        )
        self._additional_diagnostics = tuple(additional_diagnostics)

    def doctor(self, name_or_install_id: str | None = None) -> SkillDoctorReport:
        """Inspect all managed Skills, or one lock name/install id.

        The method performs filesystem and environment-presence checks only.
        In particular, it does not call ``SkillLoader.load_all()``, ``reload()``,
        any source adapter, subprocess API, or provider API.
        """

        target = str(name_or_install_id or "").strip()
        global_diagnostics: list[SkillDiagnostic] = []
        global_diagnostics.extend(self._additional_diagnostics)
        try:
            lockfile_exists = self._lockfile_path.exists()
        except OSError:
            # Lockfile.load() below owns the stable I/O diagnostic contract.
            lockfile_exists = True
        if not lockfile_exists:
            global_diagnostics.append(
                _diagnostic(
                    "LOCKFILE_NOT_PRESENT",
                    DiagnosticSeverity.INFO,
                    DiagnosticPhase.LOCK,
                    "No Community Skill lockfile is present",
                    hint="The lockfile is created after the first managed installation.",
                )
            )
        lockfile = Lockfile.load(
            self._lockfile_path,
            managed_dir=self._managed_dir,
        )
        global_diagnostics.extend(lockfile.diagnostics)
        if self._journal_path is not None:
            global_diagnostics.extend(
                inspect_pending_skill_transaction(
                    managed_dir=self._managed_dir,
                    lockfile_path=self._lockfile_path,
                    journal_path=self._journal_path,
                )
            )

        records = self._store_records(lockfile, global_diagnostics)
        items = [self._inspect_record(record) for record in records]
        if target:
            items = [
                item
                for item in items
                if item.name == target or (item.install_id and item.install_id == target)
            ]
            if not items:
                global_diagnostics.append(
                    _diagnostic(
                        "SKILL_NOT_FOUND",
                        DiagnosticSeverity.ERROR,
                        DiagnosticPhase.STORE,
                        f"No managed Skill matches {target!r}",
                        blocking=True,
                        hint="Use `opensquilla skills doctor --json` to list managed Skills.",
                        details={"target": target},
                    )
                )

        generation: int | None = None
        if self._loader is not None:
            try:
                generation = int(getattr(self._loader.snapshot(), "generation", 0))
            except (AttributeError, TypeError, ValueError):
                generation = 0
        return SkillDoctorReport(
            skills=tuple(sorted(items, key=lambda item: (item.name, item.install_id))),
            diagnostics=tuple(_deduplicate_diagnostics(global_diagnostics)),
            catalog_generation=generation,
            target=target,
        )

    def _store_records(
        self,
        lockfile: Lockfile,
        diagnostics: list[SkillDiagnostic],
    ) -> list[_StoreRecord]:
        records: list[_StoreRecord] = []
        tracked_paths: set[str] = set()
        for name, entry in sorted(lockfile.installed.items()):
            path, path_diagnostics = self._entry_path(name, entry)
            if path is not None:
                tracked_paths.add(_path_key(path))
            records.append(
                _StoreRecord(
                    lock_name=name,
                    entry=entry,
                    path=path,
                    path_diagnostics=tuple(path_diagnostics),
                )
            )

        try:
            if not self._managed_dir.exists():
                diagnostics.append(
                    _diagnostic(
                        "MANAGED_ROOT_NOT_PRESENT",
                        DiagnosticSeverity.INFO,
                        DiagnosticPhase.STORE,
                        "The managed Skill directory is not present",
                        path=str(self._managed_dir),
                    )
                )
                return records
            for child in sorted(self._managed_dir.iterdir(), key=lambda item: item.name):
                if child.name.startswith("."):
                    continue
                if child.is_symlink():
                    diagnostics.append(
                        _diagnostic(
                            "UNTRACKED_STORE_SYMLINK",
                            DiagnosticSeverity.ERROR,
                            DiagnosticPhase.SECURITY,
                            "The managed root contains an untracked symbolic link",
                            blocking=True,
                            path=str(child),
                        )
                    )
                    continue
                if not child.is_dir():
                    diagnostics.append(
                        _diagnostic(
                            "UNEXPECTED_STORE_ENTRY",
                            DiagnosticSeverity.WARNING,
                            DiagnosticPhase.STORE,
                            "The managed root contains a non-Skill entry",
                            path=str(child),
                        )
                    )
                    continue
                if _path_key(child) not in tracked_paths:
                    records.append(_StoreRecord(lock_name=child.name, entry=None, path=child))
        except OSError as exc:
            diagnostics.append(
                _diagnostic(
                    "MANAGED_ROOT_READ_FAILED",
                    DiagnosticSeverity.ERROR,
                    DiagnosticPhase.STORE,
                    f"Could not inspect the managed Skill directory: {exc}",
                    blocking=True,
                    path=str(self._managed_dir),
                )
            )
        return records

    def _entry_path(
        self,
        name: str,
        entry: LockEntry,
    ) -> tuple[Path | None, list[SkillDiagnostic]]:
        diagnostics: list[SkillDiagnostic] = []
        raw_relative = entry.relative_path or entry.directory_name or name
        relative = _safe_single_directory(raw_relative)
        if relative is None:
            diagnostics.append(
                _diagnostic(
                    "STORE_PATH_UNSAFE",
                    DiagnosticSeverity.ERROR,
                    DiagnosticPhase.STORE,
                    "The lockfile points to an unsafe managed Skill path",
                    blocking=True,
                    field_name="relative_path",
                    hint="Do not edit the store manually; restore a valid lockfile backup.",
                    details={"name": name},
                )
            )
            return None, diagnostics

        path = self._managed_dir / relative
        # The v1 writer keyed every managed directory by the lock entry name.
        # Treat that key as authoritative too: a stale absolute path may point
        # at an old profile, while a mismatched current-root child could belong
        # to a different install.
        if not entry.relative_path and not entry.directory_name and entry.path:
            legacy = Path(entry.path)
            if _path_key(legacy) != _path_key(path):
                outside_current_root = legacy.is_absolute() and not _direct_child_of(
                    legacy,
                    self._managed_dir,
                )
                diagnostics.append(
                    _diagnostic(
                        (
                            "LEGACY_PATH_RELOCATED"
                            if outside_current_root
                            else "LEGACY_PATH_MISMATCH"
                        ),
                        DiagnosticSeverity.INFO,
                        DiagnosticPhase.LOCK,
                        (
                            "Ignored a legacy absolute path outside the configured managed root"
                            if outside_current_root
                            else "Ignored a legacy path that does not match its storage key"
                        ),
                        details={"name": name, "legacyPath": str(legacy)},
                    )
                )

        if not _direct_child_of(path, self._managed_dir):
            diagnostics.append(
                _diagnostic(
                    "STORE_PATH_ESCAPE",
                    DiagnosticSeverity.ERROR,
                    DiagnosticPhase.SECURITY,
                    "The managed Skill path escapes the configured root",
                    blocking=True,
                    path=str(path),
                )
            )
            return None, diagnostics
        return path, diagnostics

    def _inspect_record(self, record: _StoreRecord) -> SkillDoctorItem:
        diagnostics = list(record.path_diagnostics)
        entry = record.entry
        path = record.path
        tracked = entry is not None
        install_state = SkillInstallState.TRACKED if tracked else SkillInstallState.UNTRACKED

        exists = False
        if path is None:
            install_state = SkillInstallState.MISSING if tracked else SkillInstallState.UNTRACKED
        else:
            try:
                exists = path.exists()
            except OSError as exc:
                diagnostics.append(
                    _diagnostic(
                        "STORE_ENTRY_READ_FAILED",
                        DiagnosticSeverity.ERROR,
                        DiagnosticPhase.STORE,
                        f"Could not inspect the installed Skill: {exc}",
                        blocking=True,
                        path=str(path),
                    )
                )
            if tracked and not exists:
                install_state = SkillInstallState.MISSING
                diagnostics.append(
                    _diagnostic(
                        "INSTALL_TREE_MISSING",
                        DiagnosticSeverity.ERROR,
                        DiagnosticPhase.STORE,
                        "The lockfile entry has no installed tree",
                        blocking=True,
                        path=str(path),
                        hint="Reinstall the Skill from its recorded source.",
                    )
                )

        static_diagnostics: list[SkillDiagnostic] = []
        if exists and path is not None:
            static_diagnostics = _scan_static_tree(path)
            diagnostics.extend(static_diagnostics)
            if tracked and any(item.blocking for item in static_diagnostics):
                install_state = SkillInstallState.DRIFTED

        expected_digest = ""
        if entry is not None:
            expected_digest = entry.tree_sha256 or entry.sha256
        if exists and path is not None and tracked:
            if expected_digest and not any(item.blocking for item in static_diagnostics):
                try:
                    actual_digest = (
                        compute_tree_sha256(path)
                        if entry is not None and entry.tree_sha256
                        else compute_sha256(path)
                    )
                except OSError as exc:
                    install_state = SkillInstallState.DRIFTED
                    diagnostics.append(
                        _diagnostic(
                            "TREE_DIGEST_FAILED",
                            DiagnosticSeverity.ERROR,
                            DiagnosticPhase.STORE,
                            f"Could not verify the installed tree digest: {exc}",
                            blocking=True,
                            path=str(path),
                        )
                    )
                else:
                    if actual_digest.casefold() != expected_digest.casefold():
                        install_state = SkillInstallState.DRIFTED
                        diagnostics.append(
                            _diagnostic(
                                "TREE_DRIFT",
                                DiagnosticSeverity.ERROR,
                                DiagnosticPhase.STORE,
                                "The installed Skill tree differs from its lockfile digest",
                                blocking=True,
                                path=str(path),
                                hint=(
                                    "Review local changes, then reinstall or explicitly "
                                    "confirm removal."
                                ),
                                details={
                                    "expected": expected_digest,
                                    "actual": actual_digest,
                                },
                            )
                        )
            elif not expected_digest:
                diagnostics.append(
                    _diagnostic(
                        "TREE_DIGEST_NOT_RECORDED",
                        DiagnosticSeverity.WARNING,
                        DiagnosticPhase.LOCK,
                        "This legacy lock entry has no installed-tree digest",
                        path=str(path),
                        hint="A successful update will record a reproducible tree digest.",
                    )
                )

        catalog = self._observe_catalog(record, exists=exists)
        diagnostics.extend(catalog.diagnostics)
        spec = catalog.spec
        display_name = spec.name if spec is not None else record.lock_name

        eligibility_report: EligibilityReport | None = None
        readiness = SkillReadinessState.UNKNOWN
        status = "not_declared"
        if spec is not None:
            try:
                eligibility_report = diagnose_eligibility(spec, self._eligibility_context)
            except (OSError, TypeError, ValueError) as exc:
                diagnostics.append(
                    _diagnostic(
                        "READINESS_CHECK_FAILED",
                        DiagnosticSeverity.ERROR,
                        DiagnosticPhase.READINESS,
                        f"Could not evaluate Skill requirements: {exc}",
                        blocking=True,
                        path=str(path or ""),
                    )
                )
            else:
                status = _status_from_report(eligibility_report)
                readiness, readiness_diagnostics = _readiness_observation(
                    spec,
                    eligibility_report,
                    path=str(path or ""),
                )
                diagnostics.extend(readiness_diagnostics)

        compatibility, compatibility_diagnostics = _compatibility_observation(
            entry,
            spec,
            catalog.load_state,
            path=str(path or ""),
        )
        diagnostics.extend(compatibility_diagnostics)

        selection = catalog.selection_state
        if eligibility_report is not None and eligibility_report.disabled:
            selection = SkillSelectionState.DISABLED
            diagnostics.append(
                _diagnostic(
                    "SKILL_DISABLED",
                    DiagnosticSeverity.WARNING,
                    DiagnosticPhase.CATALOG,
                    "The Skill is disabled by the current eligibility configuration",
                    blocking=True,
                    field_name="skills.disabled",
                    hint="Enable the Skill before attempting to invoke it.",
                )
            )

        invocation = _invocation_capabilities(
            spec=spec,
            load_state=catalog.load_state,
            selection_state=selection,
            eligibility_report=eligibility_report,
            live=catalog.live,
        )
        diagnostics.extend(
            _invocation_diagnostics(
                invocation,
                load_state=catalog.load_state,
                selection_state=selection,
            )
        )

        lifecycle = SkillLifecycle(
            install_state=install_state,
            load_state=catalog.load_state,
            selection_state=selection,
            compatibility_state=compatibility,
            readiness_state=readiness,
            invocation=invocation,
        )
        resolution = _resolution_from_entry(entry) if entry is not None else None
        if entry is not None:
            diagnostics.extend(_source_diagnostics(entry))
        elif exists:
            diagnostics.append(
                _diagnostic(
                    "INSTALL_UNTRACKED",
                    DiagnosticSeverity.WARNING,
                    DiagnosticPhase.LOCK,
                    "The managed Skill directory is not recorded in the lockfile",
                    path=str(path or ""),
                    hint=(
                        "Reinstall it through a supported Community source to make "
                        "updates recoverable."
                    ),
                )
            )

        return SkillDoctorItem(
            name=display_name,
            install_id=entry.install_id if entry is not None else "",
            path=str(path or ""),
            status=status,
            lifecycle=lifecycle,
            resolution=resolution,
            diagnostics=tuple(_deduplicate_diagnostics(diagnostics)),
        )

    def _observe_catalog(self, record: _StoreRecord, *, exists: bool) -> _CatalogObservation:
        path = record.path
        offline_spec = (
            _compile_offline(path, tracked=record.entry is not None)
            if exists and path is not None
            else None
        )
        if self._loader is None:
            if offline_spec is None:
                offline_diagnostics: tuple[SkillDiagnostic, ...] = ()
                if exists:
                    offline_diagnostics = (
                        _diagnostic(
                            "MANIFEST_REJECTED_OFFLINE",
                            DiagnosticSeverity.ERROR,
                            DiagnosticPhase.MANIFEST,
                            "The production manifest compiler rejected SKILL.md",
                            blocking=True,
                            path=str(path or ""),
                        ),
                    )
                return _CatalogObservation(
                    spec=None,
                    load_state=SkillLoadState.REJECTED if exists else SkillLoadState.NOT_DISCOVERED,
                    selection_state=SkillSelectionState.HIDDEN,
                    generation=None,
                    diagnostics=offline_diagnostics,
                    live=False,
                )
            return _CatalogObservation(
                spec=offline_spec,
                load_state=SkillLoadState.VALIDATED_OFFLINE,
                selection_state=SkillSelectionState.HIDDEN,
                generation=None,
                diagnostics=(
                    _diagnostic(
                        "CATALOG_VALIDATED_OFFLINE",
                        DiagnosticSeverity.INFO,
                        DiagnosticPhase.CATALOG,
                        "The Skill is valid on disk but no running catalog was inspected",
                        hint="It can become visible after the next Gateway start.",
                    ),
                ),
                live=False,
            )

        try:
            snapshot = self._loader.snapshot()
            generation = int(getattr(snapshot, "generation", 0))
            candidates = tuple(
                getattr(snapshot, "candidates", ()) or getattr(snapshot, "skills", ())
            )
            winners = tuple(getattr(snapshot, "skills", ()))
            errors = list(getattr(snapshot, "errors", ()))
        except (AttributeError, TypeError, ValueError) as exc:
            return _CatalogObservation(
                spec=offline_spec,
                load_state=SkillLoadState.NOT_DISCOVERED,
                selection_state=SkillSelectionState.HIDDEN,
                generation=0,
                diagnostics=(
                    _diagnostic(
                        "CATALOG_INSPECTION_FAILED",
                        DiagnosticSeverity.ERROR,
                        DiagnosticPhase.CATALOG,
                        f"Could not inspect the published Skill catalog: {exc}",
                        blocking=True,
                    ),
                ),
                live=True,
            )

        candidate = next(
            (item for item in candidates if path is not None and _spec_is_at(item, path)),
            None,
        )
        spec = candidate or offline_spec
        intended_name = (
            candidate.name
            if candidate is not None
            else (offline_spec.name if offline_spec is not None else record.lock_name)
        )
        winner = next((item for item in winners if item.name == intended_name), None)
        winner_is_record = winner is not None and path is not None and _spec_is_at(winner, path)

        matching_errors = [
            item
            for item in errors
            if _loader_error_matches(item, path=path, name=intended_name)
        ]
        refresh_result = getattr(self._loader, "_last_refresh_result", None)
        refresh_failed = bool(
            refresh_result is not None and not bool(getattr(refresh_result, "success", True))
        )
        if refresh_failed:
            matching_errors.extend(tuple(getattr(refresh_result, "errors", ())))

        diagnostics: list[SkillDiagnostic] = []
        serving_previous = refresh_failed or any(
            bool(getattr(item, "kept_previous", False)) for item in matching_errors
        )
        for error in matching_errors:
            kept_previous = bool(getattr(error, "kept_previous", False)) or refresh_failed
            diagnostics.append(
                _diagnostic(
                    "LOADER_SERVING_PREVIOUS" if kept_previous else "LOADER_REJECTED",
                    DiagnosticSeverity.WARNING if kept_previous else DiagnosticSeverity.ERROR,
                    DiagnosticPhase.CATALOG,
                    (
                        "The live catalog kept its previous Skill instance after a load error"
                        if kept_previous
                        else "The production loader rejected this Skill"
                    ),
                    blocking=True,
                    path=str(path or ""),
                    hint=(
                        "Repair the installed manifest; the previous catalog remains in service."
                        if kept_previous
                        else "Run Doctor after restoring a valid SKILL.md."
                    ),
                    details={"loaderMessage": str(getattr(error, "message", ""))},
                )
            )

        if serving_previous and (candidate is not None or winner_is_record):
            load_state = SkillLoadState.SERVING_PREVIOUS
        elif candidate is not None:
            load_state = SkillLoadState.LOADED
        elif matching_errors:
            load_state = SkillLoadState.REJECTED
        else:
            load_state = SkillLoadState.NOT_DISCOVERED
            diagnostics.append(
                _diagnostic(
                    "SKILL_NOT_DISCOVERED",
                    DiagnosticSeverity.WARNING,
                    DiagnosticPhase.CATALOG,
                    "The installed tree is not present in the live catalog",
                    blocking=True,
                    path=str(path or ""),
                    hint="Reload the catalog or restart the Gateway after validating the manifest.",
                )
            )

        if winner_is_record:
            selection = (
                SkillSelectionState.HIDDEN
                if bool(getattr(spec, "disable_model_invocation", False))
                else SkillSelectionState.ACTIVE
            )
        elif candidate is not None or winner is not None:
            selection = SkillSelectionState.SHADOWED
            diagnostics.append(
                _diagnostic(
                    "SKILL_SHADOWED",
                    DiagnosticSeverity.WARNING,
                    DiagnosticPhase.CATALOG,
                    "A higher-precedence Skill instance is the current winner",
                    blocking=True,
                    details={
                        "winnerLayer": str(getattr(getattr(winner, "layer", ""), "value", "")),
                    },
                )
            )
        else:
            # Selection would be active if loading succeeded; load_state keeps
            # this from being misreported as an invocable catalog entry.
            selection = SkillSelectionState.ACTIVE

        return _CatalogObservation(
            spec=spec,
            load_state=load_state,
            selection_state=selection,
            generation=generation,
            diagnostics=tuple(diagnostics),
            live=True,
        )


def doctor(
    *,
    managed_dir: Path,
    lockfile_path: Path,
    name_or_install_id: str | None = None,
    loader: SkillLoader | None = None,
    journal_path: Path | None = None,
    eligibility_context: EligibilityContext | None = None,
) -> SkillDoctorReport:
    """Convenience entry point for composition roots and tests."""

    return SkillDoctor(
        managed_dir=managed_dir,
        lockfile_path=lockfile_path,
        loader=loader,
        journal_path=journal_path,
        eligibility_context=eligibility_context,
    ).doctor(name_or_install_id)


def _safe_single_directory(value: str) -> str | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    normalized = value.strip().replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or _WINDOWS_DRIVE_RE.match(normalized)
        or PureWindowsPath(value).is_absolute()
    ):
        return None
    normalized = normalized.rstrip("/")
    path = PurePosixPath(normalized)
    if len(path.parts) != 1 or path.parts[0] in {".", ".."} or path.parts[0].startswith("."):
        return None
    try:
        portable = normalize_relative_path(path.as_posix())
    except ValueError:
        return None
    if len(portable.parts) != 1:
        return None
    return path.parts[0]


def _direct_child_of(path: Path, root: Path) -> bool:
    try:
        return path.resolve(strict=False).parent == root.resolve(strict=False)
    except (OSError, ValueError):
        return False


def _path_key(path: Path) -> str:
    try:
        return os.path.normcase(str(path.resolve(strict=False)))
    except (OSError, ValueError):
        return os.path.normcase(str(path.absolute()))


def _spec_is_at(spec: object, path: Path) -> bool:
    base_dir = str(getattr(spec, "base_dir", "") or "")
    file_path = str(getattr(spec, "file_path", "") or "")
    if base_dir and _path_key(Path(base_dir)) == _path_key(path):
        return True
    return bool(file_path) and _path_key(Path(file_path).parent) == _path_key(path)


def _loader_error_matches(error: object, *, path: Path | None, name: str) -> bool:
    error_path = str(getattr(error, "path", "") or "")
    if error_path and path is not None:
        try:
            error_parent = Path(error_path).resolve(strict=False).parent
            if error_parent == path.resolve(strict=False):
                return True
        except (OSError, ValueError):
            # An unresolvable diagnostic path cannot identify this installed candidate.
            return False
        return False
    return str(getattr(error, "name", "") or "") in {name, "catalog"}


def _compile_offline(path: Path | None, *, tracked: bool) -> SkillSpec | None:
    if path is None:
        return None
    try:
        return compile_skill_manifest(
            path,
            SkillLayer.MANAGED,
            profile=(
                SkillCompileProfile.COMMUNITY_INSTRUCTION
                if tracked
                else SkillCompileProfile.TRUSTED
            ),
        )
    except (OSError, UnicodeError, TypeError, ValueError):
        return None


def _status_from_report(report: EligibilityReport) -> str:
    if not report.eligible:
        return "needs_setup"
    if report.declared:
        return "ready"
    return "not_declared"


def _readiness_observation(
    spec: SkillSpec,
    report: EligibilityReport,
    *,
    path: str,
) -> tuple[SkillReadinessState, list[SkillDiagnostic]]:
    diagnostics: list[SkillDiagnostic] = []
    if report.wrong_os:
        diagnostics.append(
            _diagnostic(
                "OS_REQUIREMENT_UNMET",
                DiagnosticSeverity.WARNING,
                DiagnosticPhase.READINESS,
                "The current operating system is not supported by this Skill",
                blocking=True,
                path=path,
                details={"running": "unknown"},
            )
        )
    for binary in dict.fromkeys(str(item) for item in report.missing_bins):
        diagnostics.append(
            _diagnostic(
                "BINARY_REQUIREMENT_UNMET",
                DiagnosticSeverity.WARNING,
                DiagnosticPhase.READINESS,
                f"Required binary {binary!r} is not available",
                blocking=True,
                field_name=binary,
                hint="Install the dependency outside OpenSquilla, then rerun Doctor.",
            )
        )
    for env_name in dict.fromkeys(str(item) for item in report.missing_env):
        diagnostics.append(
            _diagnostic(
                "ENV_REQUIREMENT_UNMET",
                DiagnosticSeverity.WARNING,
                DiagnosticPhase.READINESS,
                f"Required environment variable {env_name!r} is not set",
                blocking=True,
                field_name=env_name,
                hint="Configure the environment variable without placing its value in SKILL.md.",
            )
        )
    for group in report.missing_env_any:
        names = [str(item) for item in group]
        diagnostics.append(
            _diagnostic(
                "ENV_ANY_REQUIREMENT_UNMET",
                DiagnosticSeverity.WARNING,
                DiagnosticPhase.READINESS,
                "None of the alternative environment variables is set",
                blocking=True,
                details={"alternatives": names},
            )
        )

    requires = spec.metadata.requires if spec.metadata and spec.metadata.requires else None
    config_requirements = list(requires.config) if requires else []
    for config_name in dict.fromkeys(str(item) for item in config_requirements):
        diagnostics.append(
            _diagnostic(
                "REQUIREMENT_UNSUPPORTED",
                DiagnosticSeverity.WARNING,
                DiagnosticPhase.READINESS,
                f"Configuration requirement {config_name!r} cannot be verified",
                blocking=True,
                field_name=config_name,
                hint="OpenSquilla does not map Community requires.config names to runtime config.",
            )
        )

    dependency_missing = bool(
        report.wrong_os or report.missing_bins or report.missing_env or report.missing_env_any
    )
    if dependency_missing:
        return SkillReadinessState.NEEDS_SETUP, diagnostics
    if config_requirements:
        return SkillReadinessState.UNKNOWN, diagnostics
    return SkillReadinessState.READY, diagnostics


def _compatibility_observation(
    entry: LockEntry | None,
    spec: SkillSpec | None,
    load_state: SkillLoadState,
    *,
    path: str,
) -> tuple[SkillCompatibilityState, list[SkillDiagnostic]]:
    diagnostics: list[SkillDiagnostic] = []
    if load_state is SkillLoadState.REJECTED and spec is None:
        return SkillCompatibilityState.UNSUPPORTED, diagnostics

    dialect = str(entry.dialect if entry is not None else "").strip().casefold()
    compatibility = (
        SkillCompatibilityState.NATIVE
        if dialect in {"native", "opensquilla", "opensquilla-v1"}
        else SkillCompatibilityState.INSTRUCTION_ONLY
    )
    degraded_capabilities: set[str] = set()
    if entry is not None:
        raw_capabilities = entry.extra.get(_DEGRADED_CAPABILITIES_KEY, [])
        if isinstance(raw_capabilities, list):
            degraded_capabilities = {str(item) for item in raw_capabilities}
    if degraded_capabilities.intersection(
        {
            _DYNAMIC_CONTEXT_CAPABILITY,
            _SCOPED_TOOL_PERMISSIONS_CAPABILITY,
            _UNSUPPORTED_EXECUTION_CAPABILITY,
        }
    ):
        compatibility = SkillCompatibilityState.DEGRADED
    if _SCOPED_TOOL_PERMISSIONS_CAPABILITY in degraded_capabilities:
        diagnostics.append(
            _diagnostic(
                "TOOL_PREAPPROVAL_IGNORED",
                DiagnosticSeverity.WARNING,
                DiagnosticPhase.COMPATIBILITY,
                (
                    "This Skill requested tool preapproval, but OpenSquilla keeps "
                    "its normal tool approval policy"
                ),
                path=path,
                field_name="allowed-tools",
                hint=(
                    "The Skill instructions remain usable; matching tools still "
                    "require the normal approval flow."
                ),
            )
        )
    if _UNSUPPORTED_EXECUTION_CAPABILITY in degraded_capabilities:
        diagnostics.append(
            _diagnostic(
                "DIALECT_FIELD_UNSUPPORTED",
                DiagnosticSeverity.WARNING,
                DiagnosticPhase.COMPATIBILITY,
                "Unsupported host execution fields were ignored during installation",
                path=path,
                hint="The portable instruction body remains available.",
            )
        )
    if _DYNAMIC_CONTEXT_CAPABILITY in degraded_capabilities:
        diagnostics.append(
            _diagnostic(
                "DYNAMIC_CONTEXT_UNSUPPORTED",
                DiagnosticSeverity.WARNING,
                DiagnosticPhase.COMPATIBILITY,
                (
                    "This Skill requests dynamic shell context, but OpenSquilla "
                    "keeps the command as instruction text"
                ),
                path=path,
                field_name="body.dynamic-context",
                hint=(
                    "Run the command through the normal tool flow when its output "
                    "is needed."
                ),
            )
        )
    if spec is not None:
        requires = spec.metadata.requires if spec.metadata and spec.metadata.requires else None
        if requires and requires.config:
            compatibility = SkillCompatibilityState.DEGRADED
        is_community = entry is not None and entry.source in {"clawhub", "github"}
        unsupported_execution = bool(
            spec.entrypoint or spec.composition_raw or spec.kind not in {"", "skill"}
        )
        if is_community and unsupported_execution:
            compatibility = SkillCompatibilityState.UNSUPPORTED
            diagnostics.append(
                _diagnostic(
                    "DIALECT_FIELD_UNSUPPORTED",
                    DiagnosticSeverity.ERROR,
                    DiagnosticPhase.COMPATIBILITY,
                    "This Community Skill declares executable or orchestration behavior",
                    blocking=True,
                    path=path,
                    hint=(
                        "Install an instruction-first Skill without hooks, commands, "
                        "or orchestration."
                    ),
                )
            )
    return compatibility, diagnostics


def _invocation_capabilities(
    *,
    spec: SkillSpec | None,
    load_state: SkillLoadState,
    selection_state: SkillSelectionState,
    eligibility_report: EligibilityReport | None,
    live: bool,
) -> SkillInvocationCapabilities:
    if spec is None or not live or load_state not in {
        SkillLoadState.LOADED,
        SkillLoadState.SERVING_PREVIOUS,
    }:
        return SkillInvocationCapabilities()
    if selection_state in {SkillSelectionState.SHADOWED, SkillSelectionState.DISABLED}:
        return SkillInvocationCapabilities()

    winner_reachable = selection_state in {
        SkillSelectionState.ACTIVE,
        SkillSelectionState.HIDDEN,
    }
    dependency_eligible = eligibility_report is not None and eligibility_report.eligible
    return SkillInvocationCapabilities(
        model_catalog=(
            winner_reachable
            and selection_state is SkillSelectionState.ACTIVE
            and dependency_eligible
            and not bool(spec.disable_model_invocation)
        ),
        skill_view=winner_reachable,
        user_completion=winner_reachable and bool(spec.user_invocable),
        direct_command=False,
        argument_substitution=False,
        scoped_tool_permissions=False,
        sandbox_execution="unknown",
    )


def _invocation_diagnostics(
    invocation: SkillInvocationCapabilities,
    *,
    load_state: SkillLoadState,
    selection_state: SkillSelectionState,
) -> list[SkillDiagnostic]:
    if load_state is SkillLoadState.VALIDATED_OFFLINE:
        return [
            _diagnostic(
                "INVOCATION_AVAILABLE_NEXT_START",
                DiagnosticSeverity.INFO,
                DiagnosticPhase.INVOCATION,
                "Invocation reachability cannot be claimed without a running catalog",
                hint="Start the Gateway and rerun Doctor to inspect live invocation surfaces.",
            )
        ]
    if selection_state is SkillSelectionState.HIDDEN and invocation.skill_view:
        return [
            _diagnostic(
                "MODEL_CATALOG_HIDDEN",
                DiagnosticSeverity.INFO,
                DiagnosticPhase.INVOCATION,
                "The Skill is deliberately hidden from the model catalog",
            )
        ]
    if not (invocation.model_catalog or invocation.skill_view or invocation.user_completion):
        return [
            _diagnostic(
                "INVOCATION_UNREACHABLE",
                DiagnosticSeverity.WARNING,
                DiagnosticPhase.INVOCATION,
                "No supported invocation surface can reach this Skill instance",
                blocking=load_state not in {
                    SkillLoadState.NOT_DISCOVERED,
                    SkillLoadState.REJECTED,
                },
            )
        ]
    return []


def _resolution_from_entry(entry: LockEntry) -> SourceResolution:
    requested = entry.requested_identifier or entry.identifier
    canonical = entry.resolved_identifier or entry.identifier
    return SourceResolution(
        source_id=entry.source,
        requested_identifier=requested,
        canonical_identifier=canonical,
        immutable=bool(entry.resolved_revision),
        revision=entry.resolved_revision,
        expected_digest=entry.artifact_sha256,
        trust_state=entry.source_trust,
        version=entry.resolved_version or entry.version,
        upstream_url=entry.upstream_url,
    )


def _source_diagnostics(entry: LockEntry) -> list[SkillDiagnostic]:
    diagnostics: list[SkillDiagnostic] = []
    if not entry.source:
        diagnostics.append(
            _diagnostic(
                "SOURCE_NOT_RECORDED",
                DiagnosticSeverity.WARNING,
                DiagnosticPhase.SOURCE,
                "The lock entry does not identify its source",
                hint="Reinstall through a supported source before updating.",
            )
        )
        return diagnostics
    if not (entry.resolved_identifier or entry.identifier):
        diagnostics.append(
            _diagnostic(
                "SOURCE_IDENTIFIER_NOT_RECORDED",
                DiagnosticSeverity.WARNING,
                DiagnosticPhase.SOURCE,
                "The lock entry has no canonical source identifier",
            )
        )
    if not entry.resolved_revision:
        diagnostics.append(
            _diagnostic(
                "SOURCE_REVISION_NOT_IMMUTABLE",
                DiagnosticSeverity.WARNING,
                DiagnosticPhase.SOURCE,
                "The recorded source cannot be proven immutable offline",
                hint="Update the Skill to resolve and record an immutable revision.",
            )
        )
    if not entry.artifact_sha256:
        diagnostics.append(
            _diagnostic(
                "ARTIFACT_DIGEST_NOT_RECORDED",
                DiagnosticSeverity.WARNING,
                DiagnosticPhase.SOURCE,
                "The upstream artifact digest was not recorded",
                hint="A successful update will record both artifact and installed-tree digests.",
            )
        )
    return diagnostics


def _scan_static_tree(skill_dir: Path) -> list[SkillDiagnostic]:
    diagnostics: list[SkillDiagnostic] = []
    if skill_dir.is_symlink():
        return [
            _diagnostic(
                "SKILL_ROOT_SYMLINK_UNSAFE",
                DiagnosticSeverity.ERROR,
                DiagnosticPhase.SECURITY,
                "The installed Skill root is a symbolic link",
                blocking=True,
                path=str(skill_dir),
            )
        ]
    if not skill_dir.is_dir():
        return [
            _diagnostic(
                "SKILL_ROOT_NOT_DIRECTORY",
                DiagnosticSeverity.ERROR,
                DiagnosticPhase.STORE,
                "The installed Skill path is not a directory",
                blocking=True,
                path=str(skill_dir),
            )
        ]

    stack = [skill_dir]
    seen_collision_keys: dict[tuple[str, ...], Path] = {}
    entry_count = 0
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            diagnostics.append(
                _diagnostic(
                    "RESOURCE_DIRECTORY_READ_FAILED",
                    DiagnosticSeverity.ERROR,
                    DiagnosticPhase.SECURITY,
                    f"Could not inspect Skill resources: {exc}",
                    blocking=True,
                    path=str(directory),
                )
            )
            continue
        for entry in entries:
            entry_count += 1
            path = Path(entry.path)
            try:
                relative = path.relative_to(skill_dir)
            except ValueError:
                diagnostics.append(
                    _diagnostic(
                        "RESOURCE_PATH_ESCAPE",
                        DiagnosticSeverity.ERROR,
                        DiagnosticPhase.SECURITY,
                        "A resource path escapes the installed Skill root",
                        blocking=True,
                        path=str(path),
                    )
                )
                continue
            if entry_count > _MAX_TREE_ENTRIES:
                diagnostics.append(
                    _diagnostic(
                        "RESOURCE_ENTRY_LIMIT_EXCEEDED",
                        DiagnosticSeverity.ERROR,
                        DiagnosticPhase.SECURITY,
                        f"The installed tree exceeds {_MAX_TREE_ENTRIES} entries",
                        blocking=True,
                        path=str(skill_dir),
                    )
                )
                return diagnostics

            collision_key = tuple(
                unicodedata.normalize("NFC", part).casefold() for part in relative.parts
            )
            previous = seen_collision_keys.get(collision_key)
            if previous is not None and previous != relative:
                diagnostics.append(
                    _diagnostic(
                        "RESOURCE_PATH_COLLISION",
                        DiagnosticSeverity.ERROR,
                        DiagnosticPhase.SECURITY,
                        "Resource paths collide across case or Unicode normalization",
                        blocking=True,
                        path=relative.as_posix(),
                        details={"other": previous.as_posix()},
                    )
                )
            else:
                seen_collision_keys[collision_key] = relative

            hidden_parts = [part for part in relative.parts if part.startswith(".")]
            reserved_parts = [
                part for part in relative.parts if part.casefold() in _RESERVED_INTERNAL_NAMES
            ]
            if reserved_parts:
                diagnostics.append(
                    _diagnostic(
                        "RESOURCE_INTERNAL_FILE_HIDDEN",
                        DiagnosticSeverity.INFO,
                        DiagnosticPhase.SECURITY,
                        "An internal provenance or repository file is not viewable as a resource",
                        path=relative.as_posix(),
                    )
                )
            elif hidden_parts:
                diagnostics.append(
                    _diagnostic(
                        "RESOURCE_DOTFILE_HIDDEN",
                        DiagnosticSeverity.INFO,
                        DiagnosticPhase.SECURITY,
                        "A dotfile is not viewable through the Skill resource surface",
                        path=relative.as_posix(),
                    )
                )

            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                diagnostics.append(
                    _diagnostic(
                        "RESOURCE_STAT_FAILED",
                        DiagnosticSeverity.ERROR,
                        DiagnosticPhase.SECURITY,
                        f"Could not inspect a resource entry: {exc}",
                        blocking=True,
                        path=relative.as_posix(),
                    )
                )
                continue
            mode = metadata.st_mode
            if stat.S_ISLNK(mode):
                try:
                    destination = path.resolve(strict=False)
                    escaped = not destination.is_relative_to(skill_dir.resolve(strict=False))
                except (OSError, ValueError):
                    escaped = True
                diagnostics.append(
                    _diagnostic(
                        "RESOURCE_SYMLINK_ESCAPE" if escaped else "RESOURCE_SYMLINK_UNSAFE",
                        DiagnosticSeverity.ERROR,
                        DiagnosticPhase.SECURITY,
                        (
                            "A resource symbolic link escapes the installed Skill"
                            if escaped
                            else "Resource symbolic links are unsupported"
                        ),
                        blocking=True,
                        path=relative.as_posix(),
                    )
                )
                continue
            if stat.S_ISDIR(mode):
                stack.append(path)
                continue
            if not stat.S_ISREG(mode):
                diagnostics.append(
                    _diagnostic(
                        "RESOURCE_SPECIAL_FILE_UNSAFE",
                        DiagnosticSeverity.ERROR,
                        DiagnosticPhase.SECURITY,
                        "The installed tree contains a non-regular file",
                        blocking=True,
                        path=relative.as_posix(),
                    )
                )
                continue
            if getattr(metadata, "st_nlink", 1) > 1:
                diagnostics.append(
                    _diagnostic(
                        "RESOURCE_HARDLINK_UNSAFE",
                        DiagnosticSeverity.ERROR,
                        DiagnosticPhase.SECURITY,
                        "The installed tree contains a hard-linked file",
                        blocking=True,
                        path=relative.as_posix(),
                    )
                )
            if relative.parts and relative.parts[0] in _RESOURCE_DIRS:
                text_state = _utf8_text_state(path)
                if text_state is False:
                    diagnostics.append(
                        _diagnostic(
                            "RESOURCE_NOT_TEXT",
                            DiagnosticSeverity.INFO,
                            DiagnosticPhase.INVOCATION,
                            "This binary resource is not readable through skill_view",
                            path=relative.as_posix(),
                        )
                    )
                elif text_state is None:
                    diagnostics.append(
                        _diagnostic(
                            "RESOURCE_READ_FAILED",
                            DiagnosticSeverity.WARNING,
                            DiagnosticPhase.INVOCATION,
                            "A static resource could not be read",
                            path=relative.as_posix(),
                        )
                    )
    return diagnostics


def _utf8_text_state(path: Path) -> bool | None:
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                if b"\x00" in chunk:
                    return False
                decoder.decode(chunk)
            decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    except OSError:
        return None
    return True


def _deduplicate_diagnostics(
    diagnostics: list[SkillDiagnostic],
) -> list[SkillDiagnostic]:
    result: list[SkillDiagnostic] = []
    seen: set[tuple[str, str, str]] = set()
    for item in diagnostics:
        key = (item.code, item.path, item.field_name)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


__all__ = [
    "SkillDoctor",
    "SkillDoctorItem",
    "SkillDoctorReport",
    "doctor",
]
