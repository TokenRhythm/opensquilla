"""SKILL.md frontmatter parser and multi-layer skill loader."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from opensquilla.paths import default_opensquilla_home
from opensquilla.skills.file_hash import _TreeChangedDuringHashError
from opensquilla.skills.manifest import (
    MAX_SKILL_FILE_BYTES,
    SkillCompileProfile,
    _string_list,
    _validated_skill_name,
    compile_skill_manifest,
    skill_instance_id,
)
from opensquilla.skills.meta.sop_compiler import (
    SOPCompileError,
)
from opensquilla.skills.meta.sop_compiler import (
    compile as _sop_compile,
)
from opensquilla.skills.tree import compute_tree_sha256, compute_tree_state
from opensquilla.skills.types import (
    SkillLayer,
    SkillProvenance,
    SkillSpec,
)

log = structlog.get_logger(__name__)

MAX_SKILLS_PER_SOURCE = 200  # per layer cap

# Bump when on-disk snapshot fields change so stale caches are invalidated
# instead of silently losing new fields. v12 uses nanosecond mtimes and stores
# the versioned catalog metadata used by hot reload. v13 adds description_zh;
# v14 adds stable instance identities, full-tree digests, and the complete
# candidate/shadow view. v15 records the managed lock profile so Community
# instruction projection can never be restored from a stale trusted snapshot.
_SNAPSHOT_SCHEMA_VERSION = 15
_SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS = frozenset({_SNAPSHOT_SCHEMA_VERSION})
_COMPAT_PROBE_INTERVAL_SECONDS = 0.250

Manifest = dict[str, dict[str, int | str]]


@dataclass(frozen=True)
class SkillLoadError:
    """A single source error encountered while rebuilding the catalog."""

    name: str
    path: str
    message: str
    kept_previous: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.path,
            "message": self.message,
            "kept_previous": self.kept_previous,
        }

    def as_dict(self) -> dict[str, object]:
        """Compatibility alias for callers using dataclass-style naming."""
        return self.to_dict()


@dataclass(frozen=True)
class SkillCatalogSnapshot:
    """Read-only loader view of one successfully published catalog."""

    generation: int
    manifest: Manifest
    skills: tuple[SkillSpec, ...]
    source_digests: dict[str, str]
    errors: tuple[SkillLoadError, ...]
    # Additive inspection surfaces. ``skills`` remains the active winner set
    # and therefore preserves every existing loader consumer contract.
    candidates: tuple[SkillSpec, ...] = ()
    shadowed: tuple[SkillSpec, ...] = ()
    diagnostics: tuple[SkillLoadError, ...] = ()

    def __post_init__(self) -> None:
        # Snapshots constructed by older tests/callers only provided ``skills``
        # and ``errors``. Make their additive views useful without changing the
        # positional constructor contract.
        if not self.candidates and self.skills:
            object.__setattr__(self, "candidates", self.skills)
        if not self.diagnostics and self.errors:
            object.__setattr__(self, "diagnostics", self.errors)

    def load_all(self) -> list[SkillSpec]:
        """Return this generation without probing the live filesystem."""
        return list(self.skills)

    def get_by_name(self, name: str) -> SkillSpec | None:
        """Resolve a skill from this generation only."""
        return next((skill for skill in self.skills if skill.name == name), None)

    def get_candidate_by_instance_id(self, instance_id: str) -> SkillSpec | None:
        """Resolve one physical candidate without changing winner semantics."""
        return next(
            (skill for skill in self.candidates if skill.instance_id == instance_id),
            None,
        )

    def list_meta_specs(self) -> list[SkillSpec]:
        """Return invokable compiled meta skills from this generation only."""
        return [
            skill
            for skill in self.skills
            if skill.kind == "meta" and not skill.disable_model_invocation
        ]


class PinnedSkillLoader:
    """Loader-compatible read view pinned to one catalog generation.

    Non-catalog attributes (for example configured roots used by runtime
    validation) delegate to the live loader. All catalog reads stay pinned,
    even if a delegated mutation marks the live loader dirty mid-turn.
    """

    def __init__(self, catalog: Any, live_loader: Any) -> None:
        self._catalog = catalog
        self._live_loader = live_loader

    def snapshot(self) -> Any:
        return self._catalog

    def load_all(self) -> list[SkillSpec]:
        return list(getattr(self._catalog, "skills", ()))

    def get_by_name(self, name: str) -> SkillSpec | None:
        return next((skill for skill in self.load_all() if skill.name == name), None)

    def list_meta_specs(self) -> list[SkillSpec]:
        return [
            skill
            for skill in self.load_all()
            if skill.kind == "meta" and not skill.disable_model_invocation
        ]

    def find_by_trigger(self, text: str) -> list[SkillSpec]:
        text_lower = text.lower()
        return [
            skill
            for skill in self._catalog.skills
            if any(trigger.lower() in text_lower for trigger in skill.triggers)
        ]

    def get_always_skills(self) -> list[SkillSpec]:
        return [skill for skill in self._catalog.skills if skill.always]

    def get_user_invocable(self) -> list[SkillSpec]:
        return [skill for skill in self._catalog.skills if skill.user_invocable]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._live_loader, name)


@dataclass(frozen=True)
class SkillReloadResult:
    """Stable result returned by automatic and explicit catalog refreshes."""

    success: bool
    changed: bool
    partial: bool
    generation: int
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    errors: tuple[SkillLoadError, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "changed": self.changed,
            "partial": self.partial,
            "generation": self.generation,
            "added": list(self.added),
            "removed": list(self.removed),
            "modified": list(self.modified),
            "errors": [error.to_dict() for error in self.errors],
        }

    def as_dict(self) -> dict[str, object]:
        """Compatibility alias for callers using dataclass-style naming."""
        return self.to_dict()


class _CatalogPublicationBarrier:
    """Keep a management transaction's provisional catalog invisible.

    A verified reload still needs to build the production snapshot before the
    store transaction can commit.  While this barrier is open, ordinary
    readers continue to receive the catalog that was live at entry.  Calling
    :meth:`commit` makes the verified snapshot visible when the barrier closes;
    every other exit restores the entry snapshot.
    """

    def __init__(self, loader: SkillLoader, *, reason: str) -> None:
        self._loader = loader
        self._reason = reason
        self._baseline: SkillCatalogSnapshot | None = None
        self._entered = False
        self._committed = False

    def __enter__(self) -> _CatalogPublicationBarrier:
        loader = self._loader
        with loader._refresh_lock:
            if loader._publication_barrier_depth:
                raise RuntimeError("a catalog publication barrier is already active")
            self._baseline = loader._catalog
            loader._publication_barrier_snapshot = self._baseline
            loader._publication_barrier_depth += 1
            loader._mutation_depth += 1
            self._entered = True
        return self

    def commit(self) -> None:
        """Reveal the verified catalog when the barrier closes."""

        if not self._entered:
            raise RuntimeError("catalog publication barrier is not active")
        self._committed = True

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self._entered:
            return
        loader = self._loader
        baseline = self._baseline
        try:
            if (exc_type is not None or not self._committed) and baseline is not None:
                with loader._refresh_lock:
                    needs_restore = loader._catalog is not baseline or loader._dirty
                if needs_restore:
                    loader.restore_snapshot(
                        baseline,
                        reason=f"{self._reason}.not_committed",
                    )
        finally:
            with loader._refresh_lock:
                loader._publication_barrier_depth -= 1
                loader._mutation_depth -= 1
                if loader._publication_barrier_depth == 0:
                    loader._publication_barrier_snapshot = None
            self._entered = False


def _snapshot_provenance(raw: object) -> SkillProvenance:
    if not isinstance(raw, dict):
        return SkillProvenance()
    return SkillProvenance(
        origin=str(raw.get("origin") or "unknown"),
        license=str(raw.get("license") or "unknown"),
        upstream_url=str(raw.get("upstream_url") or ""),
        maintained_by=str(raw.get("maintained_by") or "OpenSquilla"),
    )


def _skill_to_snapshot(skill: SkillSpec) -> dict[str, object]:
    """Serialize one active or shadowed spec using the v14 catalog shape."""

    metadata = skill.metadata
    requires = metadata.requires if metadata else None
    return {
        "name": skill.name,
        "description": skill.description,
        "description_zh": skill.description_zh,
        "layer": skill.layer.value,
        "always": skill.always,
        "triggers": skill.triggers,
        "content": skill.content,
        "file_path": skill.file_path,
        "base_dir": skill.base_dir,
        "instance_id": skill.instance_id,
        "tree_digest": skill.tree_digest,
        "user_invocable": skill.user_invocable,
        "disable_model_invocation": skill.disable_model_invocation,
        "homepage": skill.homepage,
        "provenance": {
            "origin": skill.provenance.origin,
            "license": skill.provenance.license,
            "upstream_url": skill.provenance.upstream_url,
            "maintained_by": skill.provenance.maintained_by,
        },
        "metadata": (
            {
                "os": metadata.os,
                "emoji": metadata.emoji,
                "skill_key": metadata.skill_key,
                "primary_env": metadata.primary_env,
                "homepage": metadata.homepage,
                "always": metadata.always,
                "risk_level": metadata.risk_level,
                "capabilities": metadata.capabilities,
                "requires_bins": requires.bins if requires else [],
                "requires_any_bins": requires.any_bins if requires else [],
                "requires_env": requires.env if requires else [],
                "requires_env_any": requires.env_any if requires else [],
                "requires_config": requires.config if requires else [],
                "install": [
                    {
                        "kind": item.kind,
                        "id": item.id,
                        "label": item.label,
                        "bins": item.bins,
                        "os": item.os,
                        "formula": item.formula,
                        "package": item.package,
                        "module": item.module,
                        "url": item.url,
                    }
                    for item in metadata.install
                ],
            }
            if metadata
            else None
        ),
        "requires_tools": skill.requires_tools,
        "fallback_for_toolsets": skill.fallback_for_toolsets,
        "kind": skill.kind,
        "meta_priority": skill.meta_priority,
        "composition_raw": skill.composition_raw,
        "final_text_mode": skill.final_text_mode,
        "request_template": skill.request_template,
        "output_contract": skill.output_contract,
        "eval_prompts": skill.eval_prompts,
        "preference_keys": skill.preference_keys,
        "policy_tags": skill.policy_tags,
        "entrypoint": skill.entrypoint,
    }


# Layer ordering: low precedence → high precedence
_LAYER_ORDER = [
    SkillLayer.EXTRA,
    SkillLayer.BUNDLED,
    SkillLayer.MANAGED,
    SkillLayer.PERSONAL,
    SkillLayer.PROJECT,
    SkillLayer.WORKSPACE,
]


class SkillLoader:
    """Loads and manages skills from multiple layered directories."""

    def __init__(
        self,
        bundled_dir: Path | None = None,
        workspace_dir: Path | None = None,
        managed_dir: Path | None = None,
        personal_agents_dir: Path | None = None,
        project_agents_dir: Path | None = None,
        extra_dirs: list[Path] | None = None,
        snapshot_path: Path | None = None,
        lockfile_path: Path | None = None,
    ) -> None:
        self._bundled_dir = bundled_dir
        self._workspace_dir = workspace_dir
        self._managed_dir = managed_dir
        self._personal_agents_dir = personal_agents_dir
        self._project_agents_dir = project_agents_dir
        self._extra_dirs = extra_dirs or []
        self._lockfile_path = (
            lockfile_path
            if lockfile_path is not None
            else default_opensquilla_home() / "skills-lock.json"
        )
        self._snapshot_path = (
            snapshot_path or default_opensquilla_home() / "cache" / "skills_snapshot.json"
        )
        self._catalog = SkillCatalogSnapshot(0, {}, (), {}, ())
        self._initialized = False
        self._cached: list[SkillSpec] | None = None
        self._refresh_lock = threading.RLock()
        self._build_local = threading.local()
        self._dirty = False
        self._dirty_reason = ""
        self._last_probe_at = 0.0
        self._mutation_depth = 0
        self._publication_barrier_depth = 0
        self._publication_barrier_snapshot: SkillCatalogSnapshot | None = None
        # ``None`` means the managed layer is healthy and may be scanned.
        # A tuple means recovery has quarantined that layer: cold start uses
        # an empty tuple, while a runtime failure retains only the managed
        # candidates from the last published catalog.  The other five layers
        # continue to refresh normally.
        self._managed_recovery_candidates: tuple[SkillSpec, ...] | None = None
        self._managed_recovery_digests: dict[str, str] = {}
        self._managed_recovery_manifest: Manifest = {}
        self._last_refresh_result = SkillReloadResult(
            success=True,
            changed=False,
            partial=False,
            generation=0,
        )
        self._last_refresh_was_force = False
        self._refresh_epoch = 0

    @property
    def workspace_dir(self) -> Path | None:
        """Public accessor for workspace skill directory."""
        return self._workspace_dir

    @property
    def managed_dir(self) -> Path | None:
        """Public accessor for managed Community-installed skills."""
        return self._managed_dir

    def bind_managed_lockfile(self, path: Path) -> None:
        """Bind the authoritative lock used to classify managed artifacts.

        Custom composition roots may keep their lock outside the default
        profile home. The management service calls this before publishing a
        transaction so tracked Community artifacts receive the same projection
        in production and embedded/test configurations.
        """

        with self._refresh_lock:
            if self._lockfile_path == path:
                return
            self._lockfile_path = path
            if self._initialized:
                self._dirty = True
                self._dirty_reason = "skill.management.lockfile-bound"

    def invalidate_cache(self) -> None:
        """Compatibility alias: make the next access rebuild the catalog."""
        self.mark_dirty("invalidate_cache")

    def snapshot(self) -> SkillCatalogSnapshot:
        """Return the current catalog snapshot without touching the filesystem."""
        with self._refresh_lock:
            return self._publication_barrier_snapshot or self._catalog

    def snapshot_for_turn(self, reason: str = "turn") -> SkillCatalogSnapshot:
        """Return the catalog visible to one turn, probing only when needed.

        A published, clean generation is immutable, so ordinary turns can pin
        it without touching Skill roots. Initial loads and known invalidations
        retain the existing full manifest/tree verification path.
        """
        started = time.monotonic()
        with self._refresh_lock:
            visible = self._publication_barrier_snapshot or self._catalog
            if self._publication_barrier_depth or self._mutation_depth:
                self._log_turn_resolution(visible, started, "barrier")
                return visible
            if self._initialized and not self._dirty:
                self._log_turn_resolution(visible, started, "snapshot")
                return visible
        result = self.refresh_if_changed(reason=reason)
        visible = self.snapshot()
        self._log_turn_resolution(
            visible,
            started,
            "rebuild" if result.changed else "probe",
        )
        return visible

    @staticmethod
    def _log_turn_resolution(
        snapshot: SkillCatalogSnapshot,
        started: float,
        phase: str,
    ) -> None:
        if structlog.is_configured():
            log.debug(
                "skill_catalog.turn_resolved",
                catalog_resolve_ms=round((time.monotonic() - started) * 1000, 3),
                phase=phase,
                generation=snapshot.generation,
                skills=len(snapshot.skills),
                entries=len(snapshot.manifest),
            )

    def freeze_catalog_for_recovery(self, *, reason: str = "recovery-required") -> None:
        """Quarantine managed bytes while retaining their published LKG.

        A failed rollback can leave uncommitted bytes on disk. Ordinary turn,
        RPC, and compatibility refreshes must not publish those bytes merely
        because the transaction's temporary publication barrier has closed.
        Recovery is scoped to the managed layer: bundled, personal, project,
        workspace, and extra Skills remain available and refreshable.
        """

        with self._refresh_lock:
            baseline = self._publication_barrier_snapshot or self._catalog
            if self._managed_recovery_candidates is None:
                self._managed_recovery_candidates = tuple(
                    skill for skill in baseline.candidates if skill.layer is SkillLayer.MANAGED
                )
                managed_paths = {
                    skill.file_path
                    for skill in self._managed_recovery_candidates
                    if skill.file_path
                }
                managed_paths.add(os.path.abspath(self._lockfile_path))
                self._managed_recovery_digests = {
                    path: digest
                    for path, digest in baseline.source_digests.items()
                    if path in managed_paths
                }
                self._managed_recovery_manifest = {
                    path: state
                    for path, state in baseline.manifest.items()
                    if path in managed_paths
                }
            self._dirty = True
            self._dirty_reason = reason

    def clear_catalog_recovery_freeze(self) -> None:
        """Release managed-layer quarantine after the journal is recovered."""

        with self._refresh_lock:
            if self._managed_recovery_candidates is not None:
                self._managed_recovery_candidates = None
                self._managed_recovery_digests = {}
                self._managed_recovery_manifest = {}
                self._dirty = True
                self._dirty_reason = "skill.management.recovery-cleared"

    def restore_snapshot(
        self,
        snapshot: SkillCatalogSnapshot,
        *,
        reason: str = "rollback",
    ) -> None:
        """Restore a previously published generation after a failed mutation.

        The management transaction restores the matching files and lockfile
        before calling this method. Keeping this operation explicit prevents a
        failed postflight from turning a transient candidate generation into
        the live catalog or incrementing the old generation during rollback.
        """

        with self._refresh_lock:
            self._catalog = snapshot
            self._cached = list(snapshot.skills)
            self._initialized = bool(snapshot.generation or snapshot.manifest or snapshot.skills)
            self._dirty = False
            self._dirty_reason = ""
            self._refresh_epoch += 1
            self._last_refresh_was_force = False
            self._last_refresh_result = SkillReloadResult(
                success=True,
                changed=False,
                partial=bool(snapshot.errors),
                generation=snapshot.generation,
                errors=snapshot.errors,
            )
            self._last_probe_at = time.monotonic()
            try:
                self._write_snapshot(snapshot)
            except (OSError, TypeError, ValueError):
                if structlog.is_configured():
                    log.warning(
                        "skill_catalog.rollback_snapshot_write_failed",
                        reason=reason,
                        generation=snapshot.generation,
                    )

    def mark_dirty(self, reason: str = "mutation") -> None:
        """Mark a known successful filesystem mutation for the next access."""
        with self._refresh_lock:
            self._dirty = True
            self._dirty_reason = reason
        if structlog.is_configured():
            log.debug("skill_catalog.invalidated", reason=reason)

    def watch_roots(self) -> tuple[Path, ...]:
        """Return configured Skill roots without probing their contents."""
        with self._refresh_lock:
            roots: list[Path] = []
            seen: set[str] = set()
            for root, _layer in self._get_layer_dirs():
                key = os.path.normcase(os.path.abspath(os.fspath(root)))
                if key in seen:
                    continue
                seen.add(key)
                roots.append(root)
            return tuple(roots)

    @contextmanager
    def mutation_guard(self, reason: str = "mutation") -> Iterator[None]:
        """Hide in-progress writes and dirty the catalog only after success.

        The guard deliberately does not hold the refresh lock while the caller
        writes. Concurrent readers therefore keep receiving the last-known-good
        snapshot instead of observing a half-written source tree.
        """
        with self._refresh_lock:
            self._mutation_depth += 1
        try:
            yield
        except BaseException:
            raise
        else:
            self.mark_dirty(reason)
        finally:
            with self._refresh_lock:
                self._mutation_depth -= 1

    def catalog_publication_barrier(
        self,
        reason: str = "management",
    ) -> _CatalogPublicationBarrier:
        """Return a fail-closed barrier for a full management transaction."""

        return _CatalogPublicationBarrier(self, reason=reason)

    def _get_layer_dirs(self) -> list[tuple[Path, SkillLayer]]:
        layer_dirs: list[tuple[Path, SkillLayer]] = []
        for d in self._extra_dirs:
            layer_dirs.append((d, SkillLayer.EXTRA))
        if self._bundled_dir:
            layer_dirs.append((self._bundled_dir, SkillLayer.BUNDLED))
        if self._managed_dir:
            layer_dirs.append((self._managed_dir, SkillLayer.MANAGED))
        if self._personal_agents_dir:
            layer_dirs.append((self._personal_agents_dir, SkillLayer.PERSONAL))
        if self._project_agents_dir:
            layer_dirs.append((self._project_agents_dir, SkillLayer.PROJECT))
        if self._workspace_dir:
            layer_dirs.append((self._workspace_dir, SkillLayer.WORKSPACE))
        return layer_dirs

    def _build_manifest(self) -> Manifest:
        """Build a manifest of all SKILL.md files with mtime and size."""
        manifest: Manifest = dict(self._managed_recovery_manifest)
        for dir_path, layer in self._get_layer_dirs():
            if layer is SkillLayer.MANAGED and self._managed_recovery_candidates is not None:
                continue
            if not dir_path.exists():
                continue
            for skill_dir in sorted(dir_path.iterdir()):
                if skill_dir.is_dir() and not skill_dir.name.startswith("."):
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists():
                        stat = skill_file.stat()
                        manifest[os.path.abspath(skill_file)] = {
                            "mtime_ns": stat.st_mtime_ns,
                            "size": stat.st_size,
                            "tree_state": compute_tree_state(skill_dir),
                        }
        if (
            self._managed_dir is not None
            and self._managed_recovery_candidates is None
        ):
            try:
                lock_bytes = self._lockfile_path.read_bytes()
            except FileNotFoundError:
                pass
            else:
                manifest[os.path.abspath(self._lockfile_path)] = {
                    # Transaction rollback may atomically restore identical lock
                    # bytes with a new mtime. Catalog identity follows content,
                    # not publication metadata, so that recovery is generation
                    # neutral while real lock/profile changes still invalidate.
                    "mtime_ns": 0,
                    "size": len(lock_bytes),
                    "tree_state": hashlib.sha256(lock_bytes).hexdigest(),
                }
        return manifest

    def save_snapshot(self) -> None:
        """Save loaded skills to disk cache for fast cold starts."""
        self.load_all()
        self._write_snapshot(self.snapshot())

    def _write_snapshot(self, catalog: SkillCatalogSnapshot) -> None:
        """Persist an already-published catalog without probing or rebuilding."""
        data = {
            "version": _SNAPSHOT_SCHEMA_VERSION,
            "generation": catalog.generation,
            "manifest": catalog.manifest,
            "source_digests": catalog.source_digests,
            "errors": [error.to_dict() for error in catalog.errors],
            "diagnostics": [error.to_dict() for error in catalog.diagnostics],
            "skills": [_skill_to_snapshot(skill) for skill in catalog.skills],
            "candidates": [_skill_to_snapshot(skill) for skill in catalog.candidates],
            "shadowed": [_skill_to_snapshot(skill) for skill in catalog.shadowed],
        }
        self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self._snapshot_path.parent,
                prefix=f".{self._snapshot_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                json.dump(data, handle)
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
            os.replace(temp_path, self._snapshot_path)
        finally:
            try:
                if temp_path is not None and temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass

    def _read_snapshot_data(self, current_manifest: Manifest | None = None) -> dict | None:
        if not self._snapshot_path.exists():
            return None
        try:
            data = json.loads(self._snapshot_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        if (
            not isinstance(data, dict)
            or data.get("version") not in _SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS
            or not all(
                isinstance(data.get(key), list)
                for key in ("skills", "candidates", "shadowed", "diagnostics")
            )
        ):
            return None
        saved_manifest = data.get("manifest", {})
        if not isinstance(saved_manifest, dict):
            return None
        if current_manifest is None:
            try:
                current_manifest = self._build_manifest()
            except OSError:
                return None
        if saved_manifest != current_manifest:
            return None
        return data

    def load_snapshot(self) -> list[SkillSpec] | None:
        """Load from snapshot if manifest matches. Returns None on miss."""
        data = self._read_snapshot_data()
        if data is None:
            return None
        try:
            return self._restore_snapshot_skills(data)
        except (AttributeError, KeyError, TypeError, ValueError):
            return None

    def _restore_snapshot_skills(
        self,
        data: dict,
        *,
        key: str = "skills",
    ) -> list[SkillSpec]:
        skills = []
        for s in data.get(key, []):
            name = _validated_skill_name(s.get("name"))
            # Restore metadata from snapshot
            meta = None
            raw_meta = s.get("metadata")
            if raw_meta:
                from opensquilla.skills.types import (
                    SkillInstallSpec,
                    SkillPlatformMeta,
                    SkillRequires,
                )

                install_specs = [
                    SkillInstallSpec(
                        kind=i.get("kind", ""),
                        id=i.get("id", ""),
                        label=i.get("label", ""),
                        bins=i.get("bins", []),
                        os=i.get("os", []),
                        formula=i.get("formula", ""),
                        package=i.get("package", ""),
                        module=i.get("module", ""),
                        url=i.get("url", ""),
                    )
                    for i in raw_meta.get("install", [])
                ]
                meta = SkillPlatformMeta(
                    emoji=raw_meta.get("emoji", ""),
                    skill_key=raw_meta.get("skill_key", ""),
                    primary_env=raw_meta.get("primary_env", ""),
                    homepage=raw_meta.get("homepage", ""),
                    always=raw_meta.get("always"),
                    os=raw_meta.get("os", []),
                    requires=SkillRequires(
                        bins=raw_meta.get("requires_bins", []),
                        any_bins=raw_meta.get("requires_any_bins", []),
                        env=raw_meta.get("requires_env", []),
                        env_any=raw_meta.get("requires_env_any", []),
                        config=raw_meta.get("requires_config", []),
                    ),
                    install=install_specs,
                    risk_level=str(raw_meta.get("risk_level", "")).strip().lower(),
                    capabilities=raw_meta.get("capabilities", []),
                )

            layer = SkillLayer(s.get("layer", "bundled"))
            file_path = str(s.get("file_path", "") or "")
            base_dir = str(s.get("base_dir", "") or "")
            raw_instance_id = s.get("instance_id")
            instance_id = (
                raw_instance_id
                if isinstance(raw_instance_id, str) and raw_instance_id
                else skill_instance_id(
                    layer=layer,
                    file_path=file_path or str(Path(base_dir) / "SKILL.md"),
                )
            )
            raw_tree_digest = s.get("tree_digest")
            base_path = Path(base_dir) if base_dir else None
            tree_digest = (
                raw_tree_digest
                if isinstance(raw_tree_digest, str) and raw_tree_digest
                else (
                    compute_tree_sha256(base_path)
                    if base_path is not None and base_path.is_dir()
                    else ""
                )
            )
            skills.append(
                SkillSpec(
                    name=name,
                    description=s.get("description", ""),
                    description_zh=s.get("description_zh", "") or "",
                    layer=layer,
                    always=s.get("always", False),
                    triggers=s.get("triggers", []),
                    content=s.get("content", ""),
                    path=Path(base_dir),
                    file_path=file_path,
                    base_dir=base_dir,
                    instance_id=instance_id,
                    tree_digest=tree_digest,
                    user_invocable=s.get("user_invocable", True),
                    disable_model_invocation=s.get("disable_model_invocation", False),
                    homepage=s.get("homepage", ""),
                    metadata=meta,
                    provenance=_snapshot_provenance(s.get("provenance")),
                    requires_tools=s.get("requires_tools", []),
                    fallback_for_toolsets=s.get("fallback_for_toolsets", []),
                    kind=s.get("kind", "skill"),
                    meta_priority=int(s.get("meta_priority", 0) or 0),
                    composition_raw=s.get("composition_raw"),
                    final_text_mode=str(s.get("final_text_mode", "auto") or "auto"),
                    request_template=(
                        dict(s.get("request_template") or {})
                        if isinstance(s.get("request_template"), dict)
                        else {}
                    ),
                    output_contract=(
                        dict(s.get("output_contract") or {})
                        if isinstance(s.get("output_contract"), dict)
                        else {}
                    ),
                    eval_prompts=(
                        [dict(item) for item in s.get("eval_prompts", []) if isinstance(item, dict)]
                        if isinstance(s.get("eval_prompts", []), list)
                        else []
                    ),
                    preference_keys=_string_list(s.get("preference_keys", [])),
                    policy_tags=_string_list(s.get("policy_tags", [])),
                    entrypoint=(s["entrypoint"] if isinstance(s.get("entrypoint"), dict) else None),
                )
            )
        return skills

    @staticmethod
    def _restore_snapshot_errors(
        data: dict,
        *,
        key: str = "errors",
    ) -> tuple[SkillLoadError, ...]:
        return tuple(
            SkillLoadError(
                name=str(item.get("name", "")),
                path=str(item.get("path", "")),
                message=str(item.get("message", "")),
                kept_previous=bool(item.get("kept_previous", False)),
            )
            for item in data.get(key, [])
            if isinstance(item, dict)
        )

    def load_all(self) -> list[SkillSpec]:
        """Load all skills with layer precedence (high overrides low).

        This compatibility entry point probes at most every 250ms. Turn and RPC
        boundaries call :meth:`refresh_if_changed` directly and are not
        throttled.
        """
        building = getattr(self._build_local, "skills", None)
        if building is not None:
            return list(building)

        now = time.monotonic()
        if (
            not self._initialized
            or self._dirty
            or now - self._last_probe_at >= _COMPAT_PROBE_INTERVAL_SECONDS
        ):
            self.refresh_if_changed(reason="load_all")
        return list(self.snapshot().skills)

    def refresh_if_changed(self, reason: str = "access") -> SkillReloadResult:
        """Probe the filesystem once and rebuild only when it changed."""
        return self._refresh(force=False, reason=reason)

    def reload(self, force: bool = True, reason: str = "manual") -> SkillReloadResult:
        """Explicitly rescan all sources, even when the manifest is unchanged."""
        return self._refresh(force=force, reason=reason)

    def reload_verified(
        self,
        verifier: Callable[[SkillCatalogSnapshot], None],
        *,
        reason: str = "verified",
    ) -> SkillReloadResult:
        """Build, verify, and publish one catalog while readers remain on LKG."""

        return self._refresh(force=True, reason=reason, verifier=verifier)

    def _refresh(
        self,
        *,
        force: bool,
        reason: str,
        verifier: Callable[[SkillCatalogSnapshot], None] | None = None,
    ) -> SkillReloadResult:
        observed_epoch = self._refresh_epoch
        with self._refresh_lock:
            # A management reload may already have built a provisional catalog
            # and advanced the refresh epoch while its durable journal commit
            # is still pending.  Never share that provisional result with an
            # ordinary reader; report the same visible baseline as snapshot().
            if self._publication_barrier_depth and verifier is None:
                return self._unchanged_result(
                    self._publication_barrier_snapshot or self._catalog,
                )
            # A caller that arrived while another rebuild was in flight shares
            # its result instead of immediately repeating the same full scan.
            # A force reload may only share another force reload: otherwise a
            # concurrent lightweight probe could swallow its full-rescan
            # guarantee when content changed without a manifest delta.
            if (
                verifier is None
                and observed_epoch != self._refresh_epoch
                and (not force or self._last_refresh_was_force)
            ):
                return self._last_refresh_result
            result = self._refresh_impl(
                force=force,
                reason=reason,
                verifier=verifier,
            )
            self._refresh_epoch += 1
            self._last_refresh_result = result
            self._last_refresh_was_force = force
            return result

    def _refresh_impl(
        self,
        *,
        force: bool,
        reason: str,
        verifier: Callable[[SkillCatalogSnapshot], None] | None = None,
    ) -> SkillReloadResult:
        started = time.monotonic()
        observed_generation = self._catalog.generation
        with self._refresh_lock:
            old = self._catalog
            if self._initialized and observed_generation != old.generation and not self._dirty:
                return self._last_refresh_result
            if self._mutation_depth and verifier is None:
                return self._unchanged_result(
                    self._publication_barrier_snapshot or old,
                )

            try:
                manifest = self._build_manifest()
            except OSError as exc:
                return self._failed_refresh(old, reason, exc, started)

            dirty = self._dirty
            effective_reason = self._dirty_reason or reason
            if self._initialized and not force and not dirty and manifest == old.manifest:
                self._last_probe_at = time.monotonic()
                return self._unchanged_result(old)

            if (
                not self._initialized
                and not force
                and not dirty
                and self._managed_recovery_candidates is None
            ):
                disk_data = self._read_snapshot_data(manifest)
                catalog: SkillCatalogSnapshot | None = None
                if disk_data is not None:
                    try:
                        disk_skills = tuple(self._restore_snapshot_skills(disk_data))
                        disk_candidates = tuple(
                            self._restore_snapshot_skills(
                                disk_data,
                                key="candidates",
                            )
                        )
                        active_instance_ids = {skill.instance_id for skill in disk_skills}
                        disk_shadowed = tuple(
                            skill
                            for skill in disk_candidates
                            if skill.instance_id not in active_instance_ids
                        )
                        disk_digests = {
                            str(path): str(digest)
                            for path, digest in dict(disk_data.get("source_digests", {})).items()
                        }
                        disk_errors = self._restore_snapshot_errors(disk_data)
                        disk_diagnostics = self._restore_snapshot_errors(
                            disk_data,
                            key=("diagnostics" if "diagnostics" in disk_data else "errors"),
                        )
                        after = self._build_manifest()
                        if after == manifest:
                            generation = max(1, int(disk_data.get("generation", 1)))
                            catalog = SkillCatalogSnapshot(
                                generation=generation,
                                manifest=dict(manifest),
                                skills=disk_skills,
                                source_digests=disk_digests,
                                errors=disk_errors,
                                candidates=disk_candidates,
                                shadowed=disk_shadowed,
                                diagnostics=disk_diagnostics,
                            )
                    except (AttributeError, KeyError, TypeError, ValueError, OSError):
                        disk_data = None
                    if catalog is not None:
                        return self._publish(
                            old,
                            catalog,
                            effective_reason,
                            started,
                            initial=True,
                        )
                    if disk_data is not None:
                        manifest = after

            for attempt in range(2):
                try:
                    skills, digests, errors, candidates, shadowed = self._build_catalog(old)
                    after = self._build_manifest()
                except OSError as exc:
                    return self._failed_refresh(old, effective_reason, exc, started)
                if after == manifest:
                    break
                manifest = after
                if attempt == 1:
                    unstable_error = OSError("skill sources changed during both catalog scans")
                    return self._failed_refresh(old, effective_reason, unstable_error, started)
            else:  # pragma: no cover - loop always breaks or returns
                raise AssertionError("unreachable catalog rebuild state")

            added, removed, modified = self._diff(old, skills, digests)
            errors_tuple = tuple(errors)
            candidates_tuple = tuple(candidates)
            shadowed_tuple = tuple(shadowed)
            catalog_changed = (
                not self._initialized
                or manifest != old.manifest
                or bool(added or removed or modified)
                or errors_tuple != old.errors
                or candidates_tuple != old.candidates
                or shadowed_tuple != old.shadowed
            )
            if not catalog_changed:
                self._dirty = False
                self._dirty_reason = ""
                self._last_probe_at = time.monotonic()
                return self._unchanged_result(old)

            candidate = SkillCatalogSnapshot(
                generation=old.generation + 1,
                manifest=dict(manifest),
                skills=tuple(skills),
                source_digests=dict(digests),
                errors=errors_tuple,
                candidates=candidates_tuple,
                shadowed=shadowed_tuple,
                diagnostics=errors_tuple,
            )
            if verifier is not None:
                try:
                    verifier(candidate)
                except Exception as exc:
                    return SkillReloadResult(
                        success=False,
                        changed=False,
                        partial=False,
                        generation=old.generation,
                        errors=(
                            SkillLoadError(
                                name="catalog",
                                path="",
                                message=str(exc) or type(exc).__name__,
                                kept_previous=True,
                            ),
                        ),
                    )
            return self._publish(
                old,
                candidate,
                effective_reason,
                started,
                diff=(added, removed, modified),
                initial=not self._initialized,
            )

    def _managed_compile_profiles(
        self,
    ) -> tuple[dict[str, SkillCompileProfile], tuple[str, ...]]:
        """Resolve trusted lock records to exact managed directories.

        The managed layer also contains user-accepted local Meta Skills, so the
        layer itself is not a Community trust signal. Only a supported parser
        profile in a structurally valid lockfile, bound to an exact direct child
        of the configured managed root, activates instruction projection.
        """

        if self._managed_dir is None:
            return {}, ()
        from opensquilla.skills.hub.lockfile import Lockfile

        lockfile = Lockfile.load(
            self._lockfile_path,
            managed_dir=self._managed_dir,
        )
        if lockfile.mutation_blocked:
            if structlog.is_configured():
                log.warning(
                    "skill_catalog.managed_profile_unavailable",
                    lockfile=str(self._lockfile_path),
                    diagnostics=[item.to_dict() for item in lockfile.diagnostics],
                )
            return {}, tuple(item.message for item in lockfile.diagnostics)
        try:
            root = self._managed_dir.resolve(strict=False)
        except (OSError, ValueError):
            return {}, ("Managed Skill root could not be resolved safely",)

        profiles: dict[str, SkillCompileProfile] = {}
        path_errors: list[str] = []
        for lock_name, entry in lockfile.installed.items():
            # v2 records use an explicit relative path. Historical records are
            # keyed by their managed direct-child directory; their absolute
            # ``path`` value may still point at an old profile after a move.
            # Keep the persisted storage key authoritative so loader, Doctor,
            # update, and uninstall all resolve the same bytes.
            relative = entry.relative_path or entry.directory_name or lock_name
            relative_path = Path(relative)
            target: Path | None = None
            if (
                not relative_path.is_absolute()
                and len(relative_path.parts) == 1
                and relative_path.name not in {"", ".", ".."}
            ):
                target = root / relative_path
            else:
                path_errors.append(
                    f"Tracked Skill {lock_name!r} has no usable managed path"
                )
            if target is None:
                continue
            try:
                resolved_target = target.resolve(strict=False)
            except (OSError, ValueError):
                path_errors.append(
                    f"Tracked Skill {lock_name!r} path could not be resolved safely"
                )
                continue
            if resolved_target.parent != root:
                path_errors.append(
                    f"Tracked Skill {lock_name!r} escapes the managed root"
                )
                continue
            # A lock entry is itself the Community trust marker. Parser
            # versions describe how it was produced; unknown, future, or empty
            # values must never upgrade third-party bytes to trusted execution.
            profiles[str(resolved_target)] = SkillCompileProfile.COMMUNITY_INSTRUCTION
        return profiles, tuple(path_errors)

    def _build_catalog(
        self, old: SkillCatalogSnapshot
    ) -> tuple[
        list[SkillSpec],
        dict[str, str],
        list[SkillLoadError],
        list[SkillSpec],
        list[SkillSpec],
    ]:
        """Build a complete candidate without mutating the published catalog."""
        merged: dict[str, SkillSpec] = {}
        digests: dict[str, str] = {}
        errors: list[SkillLoadError] = []
        candidates: list[SkillSpec] = []
        candidate_indexes: dict[str, int] = {}

        def record_candidate(spec: SkillSpec) -> None:
            existing_index = candidate_indexes.get(spec.instance_id)
            if existing_index is None:
                candidate_indexes[spec.instance_id] = len(candidates)
                candidates.append(spec)
            else:
                candidates[existing_index] = spec

        def forget_candidate(instance_id: str) -> None:
            existing_index = candidate_indexes.pop(instance_id, None)
            if existing_index is None:
                return
            candidates.pop(existing_index)
            candidate_indexes.clear()
            candidate_indexes.update(
                (candidate.instance_id, index) for index, candidate in enumerate(candidates)
            )

        old_by_path = {skill.file_path: skill for skill in old.skills if skill.file_path}
        managed_profiles, managed_profile_errors = self._managed_compile_profiles()
        old_managed_candidates = tuple(
            skill for skill in old.candidates if skill.layer is SkillLayer.MANAGED
        )
        for dir_path, layer in self._get_layer_dirs():
            if layer is SkillLayer.MANAGED and self._managed_recovery_candidates is not None:
                for frozen_spec in self._managed_recovery_candidates:
                    record_candidate(frozen_spec)
                    merged[frozen_spec.name] = frozen_spec
                    old_digest = self._managed_recovery_digests.get(frozen_spec.file_path)
                    if old_digest:
                        digests[frozen_spec.file_path] = old_digest
                continue
            if layer is SkillLayer.MANAGED and managed_profile_errors:
                for frozen_spec in old_managed_candidates:
                    record_candidate(frozen_spec)
                    merged[frozen_spec.name] = frozen_spec
                    old_digest = old.source_digests.get(frozen_spec.file_path)
                    if old_digest:
                        digests[frozen_spec.file_path] = old_digest
                errors.append(
                    SkillLoadError(
                        name="managed",
                        path=str(self._lockfile_path),
                        message="; ".join(managed_profile_errors),
                        kept_previous=bool(old_managed_candidates),
                    )
                )
                continue
            if not dir_path.exists():
                continue
            layer_count = 0
            for skill_dir in sorted(dir_path.iterdir()):
                if layer_count >= MAX_SKILLS_PER_SOURCE:
                    log.warning(
                        "layer %s has %d+ skills, truncating",
                        layer.value,
                        MAX_SKILLS_PER_SOURCE,
                    )
                    break
                if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                    continue
                skill_file = skill_dir / "SKILL.md"
                if not skill_file.exists():
                    continue
                try:
                    resolved_root = dir_path.resolve()
                    if not skill_dir.resolve().is_relative_to(resolved_root):
                        errors.append(
                            SkillLoadError(
                                name=skill_dir.name,
                                path=str(skill_file),
                                message=f"skill directory escapes layer root {dir_path}",
                            )
                        )
                        continue
                    if not skill_file.resolve().is_relative_to(resolved_root):
                        errors.append(
                            SkillLoadError(
                                name=skill_dir.name,
                                path=str(skill_file),
                                message=f"skill manifest escapes layer root {dir_path}",
                            )
                        )
                        continue
                except (OSError, ValueError) as exc:
                    errors.append(
                        SkillLoadError(
                            name=skill_dir.name,
                            path=str(skill_file),
                            message=str(exc),
                        )
                    )
                    continue
                file_path = os.path.abspath(skill_file)
                spec: SkillSpec | None
                try:
                    with skill_file.open("rb") as handle:
                        skill_bytes = handle.read(MAX_SKILL_FILE_BYTES + 1)
                except OSError as exc:
                    previous = old_by_path.get(file_path)
                    errors.append(
                        SkillLoadError(
                            name=previous.name if previous else skill_dir.name,
                            path=file_path,
                            message=str(exc),
                            kept_previous=previous is not None,
                        )
                    )
                    if previous is None:
                        continue
                    spec = previous
                    old_digest = old.source_digests.get(file_path)
                    if old_digest:
                        digests[file_path] = old_digest
                else:
                    previous = old_by_path.get(file_path)
                    if len(skill_bytes) > MAX_SKILL_FILE_BYTES:
                        errors.append(
                            SkillLoadError(
                                name=previous.name if previous else skill_dir.name,
                                path=file_path,
                                message=(f"SKILL.md exceeds {MAX_SKILL_FILE_BYTES} bytes"),
                                kept_previous=previous is not None,
                            )
                        )
                        if previous is None:
                            continue
                        spec = previous
                        old_digest = old.source_digests.get(file_path)
                        if old_digest:
                            digests[file_path] = old_digest
                        record_candidate(spec)
                        merged[spec.name] = spec
                        layer_count += 1
                        continue

                    digests[file_path] = hashlib.sha256(skill_bytes).hexdigest()
                    spec = self._load_skill(
                        skill_dir,
                        layer,
                        root=dir_path,
                        skill_bytes=skill_bytes,
                        profile=(
                            managed_profiles.get(str(skill_dir.resolve(strict=False)))
                            if layer is SkillLayer.MANAGED
                            else None
                        ),
                    )
                    if spec is None:
                        errors.append(
                            SkillLoadError(
                                name=previous.name if previous else skill_dir.name,
                                path=file_path,
                                message="invalid or unreadable SKILL.md",
                                kept_previous=previous is not None,
                            )
                        )
                        if previous is None:
                            continue
                        spec = previous

                assert spec is not None
                record_candidate(spec)

                prev = merged.get(spec.name)
                if prev is not None and prev.kind != spec.kind:
                    log.warning(
                        "skill.kind_override",
                        name=spec.name,
                        prev_kind=prev.kind,
                        new_kind=spec.kind,
                        prev_layer=prev.layer.value,
                        new_layer=spec.layer.value,
                        prev_path=str(getattr(prev, "base_dir", "")),
                        new_path=str(getattr(spec, "base_dir", "")),
                    )
                merged[spec.name] = spec
                layer_count += 1

        self._build_local.skills = tuple(merged.values())
        try:
            for sop_name in [name for name, spec in merged.items() if spec.kind == "meta_sop"]:
                sop_spec = merged[sop_name]
                try:
                    merged[sop_name] = _sop_compile(sop_spec, skill_loader=self)
                    record_candidate(merged[sop_name])
                    self._build_local.skills = tuple(merged.values())
                except SOPCompileError as exc:
                    previous = old_by_path.get(sop_spec.file_path)
                    kept_previous = previous is not None
                    errors.append(
                        SkillLoadError(
                            name=sop_name,
                            path=sop_spec.file_path,
                            message=str(exc),
                            kept_previous=kept_previous,
                        )
                    )
                    log.warning("sop_compile_failed", skill=sop_name, error=str(exc))
                    if previous is None:
                        del merged[sop_name]
                        forget_candidate(sop_spec.instance_id)
                    else:
                        merged[sop_name] = previous
                        record_candidate(previous)
                    self._build_local.skills = tuple(merged.values())
        finally:
            del self._build_local.skills

        active_instance_ids = {spec.instance_id for spec in merged.values()}
        shadowed = [spec for spec in candidates if spec.instance_id not in active_instance_ids]
        return list(merged.values()), digests, errors, candidates, shadowed

    @staticmethod
    def _skill_source_key(
        skill: SkillSpec,
        digests: dict[str, str],
    ) -> tuple[str, str, str]:
        return (
            skill.file_path,
            digests.get(skill.file_path, ""),
            skill.tree_digest,
        )

    def _diff(
        self,
        old: SkillCatalogSnapshot,
        skills: list[SkillSpec] | tuple[SkillSpec, ...],
        digests: dict[str, str],
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        old_by_name = {skill.name: skill for skill in old.skills}
        new_by_name = {skill.name: skill for skill in skills}
        added = tuple(sorted(new_by_name.keys() - old_by_name.keys()))
        removed = tuple(sorted(old_by_name.keys() - new_by_name.keys()))
        modified = tuple(
            sorted(
                name
                for name in old_by_name.keys() & new_by_name.keys()
                if self._skill_source_key(old_by_name[name], old.source_digests)
                != self._skill_source_key(new_by_name[name], digests)
            )
        )
        return added, removed, modified

    def _publish(
        self,
        old: SkillCatalogSnapshot,
        catalog: SkillCatalogSnapshot,
        reason: str,
        started: float,
        *,
        diff: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None = None,
        initial: bool = False,
    ) -> SkillReloadResult:
        if diff is None:
            diff = self._diff(old, catalog.skills, catalog.source_digests)
        self._catalog = catalog
        self._cached = list(catalog.skills)
        self._initialized = True
        self._dirty = False
        self._dirty_reason = ""
        self._last_probe_at = time.monotonic()
        try:
            self._write_snapshot(catalog)
        except (OSError, TypeError, ValueError):
            if structlog.is_configured():
                log.debug("skill_catalog.snapshot_write_failed", path=str(self._snapshot_path))
        added, removed, modified = diff
        elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        # An unconfigured structlog PrintLogger writes to stdout. Standalone
        # skill entrypoints reserve stdout for their machine-readable result,
        # so emit catalog diagnostics only after a host configures logging.
        if structlog.is_configured():
            log.info(
                "skill_catalog.refreshed",
                reason=reason,
                old_generation=old.generation,
                new_generation=catalog.generation,
                added=len(added),
                removed=len(removed),
                modified=len(modified),
                errors=len(catalog.errors),
                skills=len(catalog.skills),
                entries=len(catalog.manifest),
                elapsed_ms=elapsed_ms,
                initial=initial,
            )
        result = SkillReloadResult(
            success=True,
            changed=True,
            partial=bool(catalog.errors),
            generation=catalog.generation,
            added=added,
            removed=removed,
            modified=modified,
            errors=catalog.errors,
        )
        self._last_refresh_result = result
        return result

    @staticmethod
    def _unchanged_result(catalog: SkillCatalogSnapshot) -> SkillReloadResult:
        return SkillReloadResult(
            success=True,
            changed=False,
            partial=bool(catalog.errors),
            generation=catalog.generation,
            errors=catalog.errors,
        )

    def _failed_refresh(
        self,
        old: SkillCatalogSnapshot,
        reason: str,
        exc: OSError,
        started: float,
    ) -> SkillReloadResult:
        error = SkillLoadError(
            name="catalog",
            path="",
            message=str(exc),
            kept_previous=self._initialized,
        )
        if structlog.is_configured():
            log.warning(
                "skill_catalog.refresh_failed",
                reason=reason,
                generation=old.generation,
                errors=1,
                elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                error=str(exc),
            )
        result = SkillReloadResult(
            success=False,
            changed=False,
            partial=False,
            generation=old.generation,
            errors=(error,),
        )
        self._last_refresh_result = result
        return result

    def _load_skill(
        self,
        skill_dir: Path,
        layer: SkillLayer,
        root: Path | None = None,
        *,
        skill_bytes: bytes | None = None,
        profile: SkillCompileProfile | None = None,
    ) -> SkillSpec | None:
        """Load a single skill from its directory."""
        # Symlink containment: reject skills that escape the layer root
        if root is not None:
            try:
                real = skill_dir.resolve()
                resolved_root = root.resolve()
                if not real.is_relative_to(resolved_root):
                    log.warning("skill %s escapes root %s, skipping", skill_dir.name, root)
                    return None
                if not (skill_dir / "SKILL.md").resolve().is_relative_to(resolved_root):
                    log.warning("skill manifest %s escapes root %s, skipping", skill_dir.name, root)
                    return None
            except (OSError, ValueError):
                return None

        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            return None

        try:
            if skill_bytes is None:
                with skill_file.open("rb") as handle:
                    skill_bytes = handle.read(MAX_SKILL_FILE_BYTES + 1)
            # Keep the runtime loader's historical newline-normalization
            # contract while delegating all manifest semantics to the shared
            # tolerant compiler.  Source and tree digests remain based on the
            # original on-disk bytes.
            normalized_bytes = (
                skill_bytes.decode("utf-8")
                .replace("\r\n", "\n")
                .replace("\r", "\n")
                .encode("utf-8")
            )
            skill = compile_skill_manifest(
                skill_dir,
                layer,
                skill_bytes=normalized_bytes,
                profile=profile or SkillCompileProfile.TRUSTED,
            )
            skill.tree_digest = compute_tree_sha256(skill_dir)
            return skill
        except _TreeChangedDuringHashError:
            # A tree digest is an integrity boundary, not a per-Skill parse
            # concern.  Publishing a catalog that silently omits this Skill
            # could make a metadata-only race permanent because the cheap
            # manifest probe may see no subsequent change.  Let the catalog
            # refresh fail atomically so cold starts retry and warm loaders
            # retain their complete last-known-good snapshot.
            raise
        except Exception as exc:
            log.debug("skill.load_failed", dir=str(skill_dir), error=str(exc))
            return None

    def filter_by_tools(self, available_tools: set[str]) -> list[SkillSpec]:
        """Return skills whose requires_tools are all present in available_tools.

        Skills with no requires_tools pass unconditionally.
        """
        result = []
        for s in self.load_all():
            if s.requires_tools and not all(t in available_tools for t in s.requires_tools):
                continue
            result.append(s)
        return result

    def find_by_trigger(self, text: str) -> list[SkillSpec]:
        """Find skills matching triggers in the given text."""
        text_lower = text.lower()
        matches: list[SkillSpec] = []
        for skill in self.load_all():
            for trigger in skill.triggers:
                if trigger.lower() in text_lower:
                    matches.append(skill)
                    break
        return matches

    def get_always_skills(self) -> list[SkillSpec]:
        """Get all skills with always=True."""
        return [skill for skill in self.load_all() if skill.always]

    def get_user_invocable(self) -> list[SkillSpec]:
        """Get all skills that are user-invocable."""
        return [skill for skill in self.load_all() if skill.user_invocable]

    def get_by_name(self, name: str) -> SkillSpec | None:
        """Get a skill by exact name."""
        for skill in self.load_all():
            if skill.name == name:
                return skill
        return None

    def list_meta_specs(self) -> list[SkillSpec]:
        """Return invokable loaded specs with kind == 'meta'.

        Note: loader Pass 2 compiles authored 'meta_sop' specs into
        'meta' shape before they reach this function, so meta_sop authors
        ARE included. The helper exists to centralize that contract — do
        not filter against 'meta_sop' here. Compatibility definitions with
        ``disable-model-invocation: true`` stay addressable through
        :meth:`get_by_name` for persisted-run recovery, but are not part of
        fresh-run discovery.
        """
        return [
            spec
            for spec in self.load_all()
            if spec.kind == "meta" and not spec.disable_model_invocation
        ]
