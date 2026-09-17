"""Immutable, application-owned Runtime Pack catalog.

The catalog deliberately contains asset names and digests, never arbitrary URLs.
Download origins are fixed in :mod:`opensquilla.runtime_packs.sources` so a remotely
mutable document cannot turn Runtime Pack installation into an arbitrary download
surface.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

_COMPONENT_IDS = ("python", "node", "gitBash")
_TARGET_COMPONENTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "darwin-arm64": ("python", "node"),
        "darwin-x64": ("python", "node"),
        "windows-arm64": ("python", "node", "gitBash"),
        "windows-x64": ("python", "node", "gitBash"),
        "linux-arm64": ("python", "node"),
        "linux-x64": ("python", "node"),
    }
)
_SAFE_TEXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_SAFE_ASSET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,191}\.tar\.xz$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_DOWNLOAD_BYTES = 2 * 1024**3
_MAX_UNPACKED_BYTES = 4 * 1024**3
_MAX_TRUSTED_ARCHIVE_DIGESTS = 4


class RuntimePackCatalogError(ValueError):
    """Raised when an application-owned Runtime Pack catalog is unsafe."""


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimePackCatalogError(f"{field} must be an object")
    return cast(Mapping[str, Any], value)


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text or not _SAFE_TEXT_RE.fullmatch(text):
        raise RuntimePackCatalogError(f"{field} is invalid")
    return text


def _positive_int(value: Any, field: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimePackCatalogError(f"{field} must be a positive integer")
    if value > maximum:
        raise RuntimePackCatalogError(f"{field} exceeds the safety limit")
    return int(value)


def _trusted_archive_digests(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise RuntimePackCatalogError(f"{field} must be an array")
    result: list[str] = []
    for raw_digest in value:
        digest = str(raw_digest or "").strip()
        if not _SHA256_RE.fullmatch(digest):
            raise RuntimePackCatalogError(
                f"{field} entries must be 64 lowercase hex characters"
            )
        if digest not in result:
            result.append(digest)
        if len(result) > _MAX_TRUSTED_ARCHIVE_DIGESTS:
            raise RuntimePackCatalogError(
                f"{field} exceeds the reviewed history limit"
            )
    return tuple(result)


@dataclass(frozen=True)
class RuntimePackDescriptor:
    """One pinned Runtime Pack archive for a concrete component and target."""

    component_id: str
    target: str
    asset: str
    archive_type: str
    version: str
    size_bytes: int
    unpacked_size_bytes: int
    sha256: str
    trusted_archive_sha256: tuple[str, ...]

    @classmethod
    def model_validate(
        cls,
        raw: Any,
        *,
        component_id: str,
        target: str,
    ) -> RuntimePackDescriptor:
        value = _mapping(raw, f"targets.{target}.{component_id}")
        if "url" in value or "urls" in value:
            raise RuntimePackCatalogError("Runtime Pack catalog entries must not contain URLs")
        asset = str(value.get("asset") or "").strip()
        if not _SAFE_ASSET_RE.fullmatch(asset) or Path(asset).name != asset:
            raise RuntimePackCatalogError(
                f"targets.{target}.{component_id}.asset must be a safe tar.xz filename"
            )
        archive_type = str(
            value.get("archiveType", value.get("archive_type")) or ""
        ).strip()
        if archive_type != "tar.xz":
            raise RuntimePackCatalogError(
                f"targets.{target}.{component_id}.archiveType must be tar.xz"
            )
        digest = str(value.get("sha256") or "").strip().lower()
        if not _SHA256_RE.fullmatch(digest):
            raise RuntimePackCatalogError(
                f"targets.{target}.{component_id}.sha256 must be 64 lowercase hex characters"
            )
        return cls(
            component_id=component_id,
            target=target,
            asset=asset,
            archive_type=archive_type,
            version=_required_text(
                value.get("version"), f"targets.{target}.{component_id}.version"
            ),
            size_bytes=_positive_int(
                value.get("sizeBytes", value.get("size_bytes")),
                f"targets.{target}.{component_id}.sizeBytes",
                maximum=_MAX_DOWNLOAD_BYTES,
            ),
            unpacked_size_bytes=_positive_int(
                value.get("unpackedSizeBytes", value.get("unpacked_size_bytes")),
                f"targets.{target}.{component_id}.unpackedSizeBytes",
                maximum=_MAX_UNPACKED_BYTES,
            ),
            sha256=digest,
            trusted_archive_sha256=_trusted_archive_digests(
                value.get("trustedArchiveSha256"),
                f"targets.{target}.{component_id}.trustedArchiveSha256",
            ),
        )


@dataclass(frozen=True)
class RuntimePackCatalog:
    """Parsed immutable Runtime Pack catalog shipped with one app version."""

    schema_version: int
    catalog_version: str
    release_tag: str
    finalized: bool
    targets: Mapping[str, Mapping[str, RuntimePackDescriptor]]

    @classmethod
    def model_validate(cls, raw: Any) -> RuntimePackCatalog:
        value = _mapping(raw, "catalog")
        if value.get("schemaVersion", value.get("schema_version")) != 1:
            raise RuntimePackCatalogError("schemaVersion must be 1")
        finalized = value.get("finalized")
        if not isinstance(finalized, bool):
            raise RuntimePackCatalogError("finalized must be a boolean")
        raw_targets = _mapping(value.get("targets"), "targets")
        targets: dict[str, Mapping[str, RuntimePackDescriptor]] = {}
        for raw_target, raw_components in raw_targets.items():
            target = str(raw_target).strip()
            if target not in _TARGET_COMPONENTS:
                raise RuntimePackCatalogError(f"unsupported Runtime Pack target: {target}")
            components = _mapping(raw_components, f"targets.{target}")
            unknown = set(components) - set(_COMPONENT_IDS)
            if unknown:
                raise RuntimePackCatalogError(
                    f"targets.{target} contains unknown components: {', '.join(sorted(unknown))}"
                )
            parsed = {
                component_id: RuntimePackDescriptor.model_validate(
                    component,
                    component_id=component_id,
                    target=target,
                )
                for component_id, component in components.items()
            }
            targets[target] = MappingProxyType(parsed)
        digest_owners: dict[str, tuple[str, str]] = {}
        for target, components in targets.items():
            for component_id, descriptor in components.items():
                owner = (target, component_id)
                for digest in (descriptor.sha256, *descriptor.trusted_archive_sha256):
                    existing = digest_owners.setdefault(digest, owner)
                    if existing != owner or digest == descriptor.sha256 and (
                        digest in descriptor.trusted_archive_sha256
                    ):
                        raise RuntimePackCatalogError(
                            "Runtime Pack archive digests must identify one target component"
                        )
        return cls(
            schema_version=1,
            catalog_version=_required_text(
                value.get("catalogVersion", value.get("catalog_version")),
                "catalogVersion",
            ),
            release_tag=_required_text(
                value.get("releaseTag", value.get("release_tag")),
                "releaseTag",
            ),
            finalized=finalized,
            targets=MappingProxyType(targets),
        )

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        *,
        require_complete: bool = False,
    ) -> RuntimePackCatalog:
        catalog_path = Path(path)
        try:
            raw = json.loads(catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimePackCatalogError(
                f"could not read Runtime Pack catalog {catalog_path}: {exc}"
            ) from exc
        catalog = cls.model_validate(raw)
        if require_complete:
            catalog.validate_release_matrix()
        return catalog

    def validate_release_matrix(self) -> None:
        """Fail unless this is the complete, finalized v1 platform matrix."""

        if not self.finalized:
            raise RuntimePackCatalogError("Runtime Pack catalog is not finalized")
        missing_targets = set(_TARGET_COMPONENTS) - set(self.targets)
        extra_targets = set(self.targets) - set(_TARGET_COMPONENTS)
        if missing_targets or extra_targets:
            raise RuntimePackCatalogError(
                "Runtime Pack catalog does not contain the complete release target matrix"
            )
        for target, required_components in _TARGET_COMPONENTS.items():
            if set(self.targets[target]) != set(required_components):
                raise RuntimePackCatalogError(
                    f"targets.{target} does not contain its complete component matrix"
                )

    def descriptor(self, target: str, component_id: str) -> RuntimePackDescriptor | None:
        if component_id not in _COMPONENT_IDS:
            raise RuntimePackCatalogError(f"unknown Runtime Pack component: {component_id}")
        return self.targets.get(target, {}).get(component_id)


def discover_catalog_path() -> Path | None:
    """Find the immutable catalog in a packaged app or source checkout."""

    packaged_wheel = Path(__file__).with_name("runtime-pack-catalog.json")
    if packaged_wheel.is_file():
        return packaged_wheel

    # A frozen desktop Gateway receives the same canonical catalog as an
    # external Electron resource. Only the two supported Gateway layouts are
    # considered; a missing application-owned catalog must not fall through to
    # an unrelated ancestor-owned catalog.
    if bool(getattr(sys, "frozen", False)):
        executable = Path(sys.executable).absolute()
        executable_parent = executable.parent
        runtime_root: Path | None = None
        if (
            executable_parent.name == "opensquilla-gateway"
            and executable_parent.parent.name == "gateway"
            and executable_parent.parent.parent.name == "runtime"
        ):
            runtime_root = executable_parent.parent.parent
        elif (
            executable_parent.name == "gateway"
            and executable_parent.parent.name == "runtime"
        ):
            runtime_root = executable_parent.parent
        if runtime_root is not None:
            external = runtime_root / "runtime-pack-catalog.json"
            if external.is_file():
                return external

    source_checkout = (
        Path(__file__).resolve().parents[3]
        / "desktop"
        / "electron"
        / "runtime"
        / "runtime-pack-catalog.json"
    )
    return source_checkout if source_checkout.is_file() else None


def load_default_catalog(*, require_complete: bool = False) -> RuntimePackCatalog:
    path = discover_catalog_path()
    if path is None:
        raise RuntimePackCatalogError("Runtime Pack catalog is unavailable")
    return RuntimePackCatalog.from_path(path, require_complete=require_complete)


def component_ids() -> tuple[str, ...]:
    return _COMPONENT_IDS


__all__ = [
    "RuntimePackCatalog",
    "RuntimePackCatalogError",
    "RuntimePackDescriptor",
    "component_ids",
    "discover_catalog_path",
    "load_default_catalog",
]
