"""SKILL.md frontmatter parser and multi-layer skill loader."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import structlog
import yaml

from opensquilla.paths import default_opensquilla_home
from opensquilla.skills.meta.sop_compiler import (
    SOPCompileError,
)
from opensquilla.skills.meta.sop_compiler import (
    compile as _sop_compile,
)
from opensquilla.skills.types import (
    SkillInstallSpec,
    SkillLayer,
    SkillPlatformMeta,
    SkillProvenance,
    SkillRequires,
    SkillSpec,
)

log = structlog.get_logger(__name__)

MAX_SKILL_FILE_BYTES = 256_000  # 256KB per SKILL.md
MAX_SKILLS_PER_SOURCE = 200  # per layer cap

# Bump when on-disk snapshot fields change so stale caches are invalidated
# instead of silently losing new fields. v12 uses nanosecond mtimes and stores
# the versioned catalog metadata used by hot reload.
_SNAPSHOT_SCHEMA_VERSION = 12
_COMPAT_PROBE_INTERVAL_SECONDS = 0.250

Manifest = dict[str, dict[str, int]]


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

    def load_all(self) -> list[SkillSpec]:
        """Return this generation without probing the live filesystem."""
        return list(self.skills)

    def get_by_name(self, name: str) -> SkillSpec | None:
        """Resolve a skill from this generation only."""
        return next((skill for skill in self.skills if skill.name == name), None)

    def list_meta_specs(self) -> list[SkillSpec]:
        """Return compiled meta skills from this generation only."""
        return [skill for skill in self.skills if skill.kind == "meta"]


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
        return [skill for skill in self.load_all() if skill.kind == "meta"]

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


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _validated_skill_name(value: object) -> str:
    """Return a usable catalog key or reject malformed source/cache data."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("skill name must be a non-empty string")
    return value


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from SKILL.md.

    Returns (frontmatter_dict, body_content).
    Handles both simple and nested metadata formats.
    """
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not match:
        return {}, text

    fm_text, body = match.groups()
    try:
        frontmatter = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return {}, text

    if not isinstance(frontmatter, dict):
        return {}, text

    return frontmatter, body.strip()


def _resolve_metadata(frontmatter: dict) -> SkillPlatformMeta | None:
    """Extract platform metadata from frontmatter."""
    raw_meta = frontmatter.get("metadata", {})
    if isinstance(raw_meta, dict):
        # Namespace fallback: platform > openclaw > clawdbot > top-level.
        # `opensquilla` overlays advisory fields such as risk/capabilities
        # without erasing platform dependency metadata kept at the top level
        # or in an upstream namespace.
        base_meta = raw_meta.get(
            "platform",
            raw_meta.get("openclaw", raw_meta.get("clawdbot", raw_meta)),
        )
        if not isinstance(base_meta, dict):
            base_meta = {}
        merged_meta = dict(base_meta)
        opensquilla_meta = raw_meta.get("opensquilla", {})
        if isinstance(opensquilla_meta, dict):
            for key in (
                "emoji",
                "skillKey",
                "primaryEnv",
                "homepage",
                "always",
                "os",
                "requires",
                "install",
                "risk",
                "risk_level",
                "riskLevel",
                "capabilities",
            ):
                if key in opensquilla_meta:
                    merged_meta[key] = opensquilla_meta[key]
        raw_meta = merged_meta
    if not isinstance(raw_meta, dict):
        return None

    requires = None
    raw_req = raw_meta.get("requires", {})
    if isinstance(raw_req, dict):
        # ClawHub frontmatter sometimes uses requires.commands instead of requires.bins.
        bins_value = raw_req.get("bins")
        if bins_value is None:
            bins_value = raw_req.get("commands", [])
        requires = SkillRequires(
            bins=bins_value if isinstance(bins_value, list) else [],
            any_bins=raw_req.get("anyBins", []),
            env=raw_req.get("env", []),
            env_any=raw_req.get("envAny", []),
            config=raw_req.get("config", []),
        )

    install_specs: list[SkillInstallSpec] = []
    for item in raw_meta.get("install", []):
        if isinstance(item, dict):
            install_specs.append(
                SkillInstallSpec(
                    kind=item.get("kind", ""),
                    id=item.get("id", ""),
                    label=item.get("label", ""),
                    bins=item.get("bins", []),
                    os=item.get("os", []),
                    formula=item.get("formula", ""),
                    package=item.get("package", ""),
                    module=item.get("module", ""),
                    url=item.get("url", ""),
                )
            )

    always_val = raw_meta.get("always")
    return SkillPlatformMeta(
        emoji=raw_meta.get("emoji", ""),
        skill_key=raw_meta.get("skillKey", ""),
        primary_env=raw_meta.get("primaryEnv", ""),
        homepage=raw_meta.get("homepage", ""),
        always=bool(always_val) if always_val is not None else None,
        os=_string_list(raw_meta.get("os", [])),
        requires=requires,
        install=install_specs,
        risk_level=str(
            raw_meta.get("risk")
            or raw_meta.get("risk_level")
            or raw_meta.get("riskLevel")
            or ""
        ).strip().lower(),
        capabilities=_string_list(raw_meta.get("capabilities", [])),
    )


def _resolve_provenance(frontmatter: dict) -> SkillProvenance:
    """Extract provenance metadata from top-level frontmatter."""
    raw = frontmatter.get("provenance", {})
    if not isinstance(raw, dict):
        raw = {}
    return SkillProvenance(
        origin=str(raw.get("origin") or "unknown"),
        license=str(raw.get("license") or "unknown"),
        upstream_url=str(raw.get("upstream_url") or ""),
        maintained_by=str(raw.get("maintained_by") or "OpenSquilla"),
    )


def _snapshot_provenance(raw: object) -> SkillProvenance:
    if not isinstance(raw, dict):
        return SkillProvenance()
    return SkillProvenance(
        origin=str(raw.get("origin") or "unknown"),
        license=str(raw.get("license") or "unknown"),
        upstream_url=str(raw.get("upstream_url") or ""),
        maintained_by=str(raw.get("maintained_by") or "OpenSquilla"),
    )


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
    ) -> None:
        self._bundled_dir = bundled_dir
        self._workspace_dir = workspace_dir
        self._managed_dir = managed_dir
        self._personal_agents_dir = personal_agents_dir
        self._project_agents_dir = project_agents_dir
        self._extra_dirs = extra_dirs or []
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

    def invalidate_cache(self) -> None:
        """Compatibility alias: make the next access rebuild the catalog."""
        self.mark_dirty("invalidate_cache")

    def snapshot(self) -> SkillCatalogSnapshot:
        """Return the current catalog snapshot without touching the filesystem."""
        return self._catalog

    def mark_dirty(self, reason: str = "mutation") -> None:
        """Mark a known successful filesystem mutation for the next access."""
        with self._refresh_lock:
            self._dirty = True
            self._dirty_reason = reason

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
        manifest: Manifest = {}
        for dir_path, _layer in self._get_layer_dirs():
            if not dir_path.exists():
                continue
            for skill_dir in sorted(dir_path.iterdir()):
                if skill_dir.is_dir() and not skill_dir.name.startswith("."):
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists():
                        stat = skill_file.stat()
                        manifest[str(skill_file.resolve())] = {
                            "mtime_ns": stat.st_mtime_ns,
                            "size": stat.st_size,
                        }
        return manifest

    def save_snapshot(self) -> None:
        """Save loaded skills to disk cache for fast cold starts."""
        self.load_all()
        self._write_snapshot(self._catalog)

    def _write_snapshot(self, catalog: SkillCatalogSnapshot) -> None:
        """Persist an already-published catalog without probing or rebuilding."""
        skills = catalog.skills
        data = {
            "version": _SNAPSHOT_SCHEMA_VERSION,
            "generation": catalog.generation,
            "manifest": catalog.manifest,
            "source_digests": catalog.source_digests,
            "errors": [error.to_dict() for error in catalog.errors],
            "skills": [
                {
                    "name": s.name,
                    "description": s.description,
                    "layer": s.layer.value,
                    "always": s.always,
                    "triggers": s.triggers,
                    "content": s.content,
                    "file_path": s.file_path,
                    "base_dir": s.base_dir,
                    "user_invocable": s.user_invocable,
                    "disable_model_invocation": s.disable_model_invocation,
                    "homepage": s.homepage,
                    "provenance": {
                        "origin": s.provenance.origin,
                        "license": s.provenance.license,
                        "upstream_url": s.provenance.upstream_url,
                        "maintained_by": s.provenance.maintained_by,
                    },
                    "metadata": {
                        "os": s.metadata.os if s.metadata else [],
                        "emoji": s.metadata.emoji if s.metadata else "",
                        "skill_key": s.metadata.skill_key if s.metadata else "",
                        "primary_env": s.metadata.primary_env if s.metadata else "",
                        "homepage": s.metadata.homepage if s.metadata else "",
                        "always": s.metadata.always if s.metadata else None,
                        "risk_level": s.metadata.risk_level if s.metadata else "",
                        "capabilities": s.metadata.capabilities if s.metadata else [],
                        "requires_bins": s.metadata.requires.bins
                        if s.metadata and s.metadata.requires
                        else [],
                        "requires_any_bins": s.metadata.requires.any_bins
                        if s.metadata and s.metadata.requires
                        else [],
                        "requires_env": s.metadata.requires.env
                        if s.metadata and s.metadata.requires
                        else [],
                        "requires_env_any": s.metadata.requires.env_any
                        if s.metadata and s.metadata.requires
                        else [],
                        "install": [
                            {
                                "kind": i.kind,
                                "id": i.id,
                                "label": i.label,
                                "bins": i.bins,
                                "os": i.os,
                                "formula": i.formula,
                                "package": i.package,
                                "module": i.module,
                                "url": i.url,
                            }
                            for i in (s.metadata.install if s.metadata else [])
                        ],
                    }
                    if s.metadata
                    else None,
                    "requires_tools": s.requires_tools,
                    "fallback_for_toolsets": s.fallback_for_toolsets,
                    "kind": s.kind,
                    "meta_priority": s.meta_priority,
                    "composition_raw": s.composition_raw,
                    "final_text_mode": s.final_text_mode,
                    "request_template": s.request_template,
                    "output_contract": s.output_contract,
                    "eval_prompts": s.eval_prompts,
                    "preference_keys": s.preference_keys,
                    "policy_tags": s.policy_tags,
                    "entrypoint": s.entrypoint,
                }
                for s in skills
            ],
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

        if not isinstance(data, dict) or data.get("version") != _SNAPSHOT_SCHEMA_VERSION:
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

    def _restore_snapshot_skills(self, data: dict) -> list[SkillSpec]:
        skills = []
        for s in data.get("skills", []):
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
                    ),
                    install=install_specs,
                    risk_level=str(raw_meta.get("risk_level", "")).strip().lower(),
                    capabilities=raw_meta.get("capabilities", []),
                )

            skills.append(
                SkillSpec(
                    name=name,
                    description=s.get("description", ""),
                    layer=SkillLayer(s.get("layer", "bundled")),
                    always=s.get("always", False),
                    triggers=s.get("triggers", []),
                    content=s.get("content", ""),
                    path=Path(s.get("base_dir", "")),
                    file_path=s.get("file_path", ""),
                    base_dir=s.get("base_dir", ""),
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
                    entrypoint=(
                        s["entrypoint"]
                        if isinstance(s.get("entrypoint"), dict)
                        else None
                    ),
                )
            )
        return skills

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
        return list(self._catalog.skills)

    def refresh_if_changed(self, reason: str = "access") -> SkillReloadResult:
        """Probe the filesystem once and rebuild only when it changed."""
        return self._refresh(force=False, reason=reason)

    def reload(self, force: bool = True, reason: str = "manual") -> SkillReloadResult:
        """Explicitly rescan all sources, even when the manifest is unchanged."""
        return self._refresh(force=force, reason=reason)

    def _refresh(self, *, force: bool, reason: str) -> SkillReloadResult:
        observed_epoch = self._refresh_epoch
        with self._refresh_lock:
            # A caller that arrived while another rebuild was in flight shares
            # its result instead of immediately repeating the same full scan.
            # A force reload may only share another force reload: otherwise a
            # concurrent lightweight probe could swallow its full-rescan
            # guarantee when content changed without a manifest delta.
            if observed_epoch != self._refresh_epoch and (
                not force or self._last_refresh_was_force
            ):
                return self._last_refresh_result
            result = self._refresh_impl(force=force, reason=reason)
            self._refresh_epoch += 1
            self._last_refresh_result = result
            self._last_refresh_was_force = force
            return result

    def _refresh_impl(self, *, force: bool, reason: str) -> SkillReloadResult:
        started = time.monotonic()
        observed_generation = self._catalog.generation
        with self._refresh_lock:
            self._last_probe_at = time.monotonic()
            old = self._catalog
            if (
                self._initialized
                and observed_generation != old.generation
                and not self._dirty
            ):
                return self._last_refresh_result
            if self._mutation_depth:
                return self._unchanged_result(old)

            try:
                manifest = self._build_manifest()
            except OSError as exc:
                return self._failed_refresh(old, reason, exc, started)

            dirty = self._dirty
            effective_reason = self._dirty_reason or reason
            if self._initialized and not force and not dirty and manifest == old.manifest:
                return self._unchanged_result(old)

            if not self._initialized and not force and not dirty:
                disk_data = self._read_snapshot_data(manifest)
                catalog: SkillCatalogSnapshot | None = None
                if disk_data is not None:
                    try:
                        disk_skills = tuple(self._restore_snapshot_skills(disk_data))
                        disk_digests = {
                            str(path): str(digest)
                            for path, digest in dict(
                                disk_data.get("source_digests", {})
                            ).items()
                        }
                        disk_errors = tuple(
                            SkillLoadError(
                                name=str(item.get("name", "")),
                                path=str(item.get("path", "")),
                                message=str(item.get("message", "")),
                                kept_previous=bool(item.get("kept_previous", False)),
                            )
                            for item in disk_data.get("errors", [])
                            if isinstance(item, dict)
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
                    skills, digests, errors = self._build_catalog(old)
                    after = self._build_manifest()
                except OSError as exc:
                    return self._failed_refresh(old, effective_reason, exc, started)
                if after == manifest:
                    break
                manifest = after
                if attempt == 1:
                    unstable_error = OSError(
                        "skill sources changed during both catalog scans"
                    )
                    return self._failed_refresh(
                        old, effective_reason, unstable_error, started
                    )
            else:  # pragma: no cover - loop always breaks or returns
                raise AssertionError("unreachable catalog rebuild state")

            added, removed, modified = self._diff(old, skills, digests)
            errors_tuple = tuple(errors)
            catalog_changed = (
                not self._initialized
                or manifest != old.manifest
                or bool(added or removed or modified)
                or errors_tuple != old.errors
            )
            if not catalog_changed:
                self._dirty = False
                self._dirty_reason = ""
                return self._unchanged_result(old)

            candidate = SkillCatalogSnapshot(
                generation=old.generation + 1,
                manifest=dict(manifest),
                skills=tuple(skills),
                source_digests=dict(digests),
                errors=errors_tuple,
            )
            return self._publish(
                old,
                candidate,
                effective_reason,
                started,
                diff=(added, removed, modified),
                initial=not self._initialized,
            )

    def _build_catalog(
        self, old: SkillCatalogSnapshot
    ) -> tuple[list[SkillSpec], dict[str, str], list[SkillLoadError]]:
        """Build a complete candidate without mutating the published catalog."""
        merged: dict[str, SkillSpec] = {}
        digests: dict[str, str] = {}
        errors: list[SkillLoadError] = []
        old_by_path = {skill.file_path: skill for skill in old.skills if skill.file_path}
        for dir_path, layer in self._get_layer_dirs():
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
                    if not skill_dir.resolve().is_relative_to(dir_path.resolve()):
                        errors.append(
                            SkillLoadError(
                                name=skill_dir.name,
                                path=str(skill_file),
                                message=f"skill directory escapes layer root {dir_path}",
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
                file_path = str(skill_file.resolve())
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
                                message=(
                                    f"SKILL.md exceeds {MAX_SKILL_FILE_BYTES} bytes"
                                ),
                                kept_previous=previous is not None,
                            )
                        )
                        if previous is None:
                            continue
                        spec = previous
                        old_digest = old.source_digests.get(file_path)
                        if old_digest:
                            digests[file_path] = old_digest
                        merged[spec.name] = spec
                        layer_count += 1
                        continue

                    digests[file_path] = hashlib.sha256(skill_bytes).hexdigest()
                    spec = self._load_skill(
                        skill_dir,
                        layer,
                        root=dir_path,
                        skill_bytes=skill_bytes,
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
                    else:
                        merged[sop_name] = previous
                    self._build_local.skills = tuple(merged.values())
        finally:
            del self._build_local.skills

        return list(merged.values()), digests, errors

    @staticmethod
    def _skill_source_key(skill: SkillSpec, digests: dict[str, str]) -> tuple[str, str]:
        return skill.file_path, digests.get(skill.file_path, "")

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
        try:
            self._write_snapshot(catalog)
        except (OSError, TypeError, ValueError):
            if structlog.is_configured():
                log.debug(
                    "skill_catalog.snapshot_write_failed", path=str(self._snapshot_path)
                )
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
    ) -> SkillSpec | None:
        """Load a single skill from its directory."""
        # Symlink containment: reject skills that escape the layer root
        if root is not None:
            try:
                real = skill_dir.resolve()
                if not real.is_relative_to(root.resolve()):
                    log.warning("skill %s escapes root %s, skipping", skill_dir.name, root)
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
            if len(skill_bytes) > MAX_SKILL_FILE_BYTES:
                log.warning(
                    "skill %s exceeds %d bytes, skipping",
                    skill_dir.name,
                    MAX_SKILL_FILE_BYTES,
                )
                return None
            text = skill_bytes.decode("utf-8")
            frontmatter, body = _parse_frontmatter(text)

            if not frontmatter or "name" not in frontmatter:
                return None

            name = _validated_skill_name(frontmatter["name"])
            description = frontmatter.get("description", "")

            # Simple fields
            always_raw = frontmatter.get("always", False)
            always = bool(always_raw) if always_raw is not None else False

            triggers = frontmatter.get("triggers", [])
            if not isinstance(triggers, list):
                triggers = [str(triggers)]

            # Platform metadata fields
            metadata = _resolve_metadata(frontmatter)
            provenance = _resolve_provenance(frontmatter)
            # metadata.always overrides top-level always if set
            if metadata and metadata.always is not None:
                always = metadata.always

            user_invocable = frontmatter.get("user-invocable", True)
            disable_model_invocation = frontmatter.get(
                "disable-model-invocation",
                False,
            )
            homepage = frontmatter.get("homepage", "")

            # Conditional activation fields
            activation_meta: dict[str, Any] = {}
            raw_meta_dict = frontmatter.get("metadata", {})
            if isinstance(raw_meta_dict, dict):
                raw_activation_meta = raw_meta_dict.get("opensquilla", {})
                if isinstance(raw_activation_meta, dict):
                    activation_meta = cast(dict[str, Any], raw_activation_meta)
            requires_tools = activation_meta.get("requires_tools", [])
            fallback_for_toolsets = activation_meta.get("fallback_for_toolsets", [])

            # Meta-Skill fields (MVP): kind, meta_priority, composition_raw.
            # Non-meta skills get the defaults; behavior unchanged.
            kind_raw = frontmatter.get("kind", "skill")
            kind = str(kind_raw) if isinstance(kind_raw, str) else "skill"
            meta_priority_raw = frontmatter.get("meta_priority", 0)
            try:
                meta_priority = int(meta_priority_raw) if meta_priority_raw is not None else 0
            except (TypeError, ValueError):
                meta_priority = 0
            composition_raw = frontmatter.get("composition")
            if not isinstance(composition_raw, dict):
                composition_raw = None

            entrypoint_raw = frontmatter.get("entrypoint")
            entrypoint = entrypoint_raw if isinstance(entrypoint_raw, dict) else None

            # final_text_mode is a meta-skill-only optional field; non-meta
            # skills keep the default "auto" but never consume it.
            final_text_mode_raw = frontmatter.get("final_text_mode", "auto")
            final_text_mode = (
                str(final_text_mode_raw).strip() if final_text_mode_raw else "auto"
            ) or "auto"
            request_template_raw = frontmatter.get("request_template")
            request_template = (
                dict(request_template_raw)
                if isinstance(request_template_raw, dict)
                else {}
            )
            output_contract_raw = frontmatter.get("output_contract")
            output_contract = (
                dict(output_contract_raw)
                if isinstance(output_contract_raw, dict)
                else {}
            )
            eval_prompts_raw = frontmatter.get("eval_prompts")
            eval_prompts = (
                [dict(item) for item in eval_prompts_raw if isinstance(item, dict)]
                if isinstance(eval_prompts_raw, list)
                else []
            )
            preference_keys = _string_list(frontmatter.get("preference_keys", []))
            policy_tags = _string_list(frontmatter.get("policy_tags", []))

            return SkillSpec(
                name=name,
                description=description,
                layer=layer,
                always=always,
                triggers=triggers,
                content=body,
                path=skill_dir,
                metadata=metadata,
                provenance=provenance,
                user_invocable=user_invocable,
                disable_model_invocation=disable_model_invocation,
                homepage=homepage,
                file_path=str(skill_file.resolve()),
                base_dir=str(skill_dir.resolve()),
                requires_tools=requires_tools if isinstance(requires_tools, list) else [],
                fallback_for_toolsets=fallback_for_toolsets
                if isinstance(fallback_for_toolsets, list)
                else [],
                kind=kind,
                meta_priority=meta_priority,
                composition_raw=composition_raw,
                final_text_mode=final_text_mode,
                request_template=request_template,
                output_contract=output_contract,
                eval_prompts=eval_prompts,
                preference_keys=preference_keys,
                policy_tags=policy_tags,
                entrypoint=entrypoint,
            )
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
        """Return all loaded specs with kind == 'meta'.

        Note: loader Pass 2 compiles authored 'meta_sop' specs into
        'meta' shape before they reach this function, so meta_sop authors
        ARE included. The helper exists to centralize that contract — do
        not filter against 'meta_sop' here.
        """
        return [spec for spec in self.load_all() if spec.kind == "meta"]
