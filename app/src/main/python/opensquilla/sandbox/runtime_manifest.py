"""Pinned bundled developer-runtime manifest and PATH resolution."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from opensquilla.runtime_target import runtime_target
from opensquilla.sandbox.policy_models import RuntimePolicySettings
from opensquilla.sandbox.run_mode import RunMode, normalize_run_mode

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUNTIME_KEYS = ("python", "node", "gitBash")
_PORTABLE_RUNTIME_KEYS = ("python", "node")


class RuntimeManifestError(ValueError):
    """Raised when a runtime manifest or selected target is unsafe/incomplete."""


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeManifestError(f"{field} must be an object")
    return value


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RuntimeManifestError(f"{field} must not be empty")
    return text


def _safe_relative_path(value: Any, field: str, *, allow_dot: bool = False) -> str:
    text = _required_text(value, field).replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeManifestError(f"{field} must be a safe relative path")
    if not allow_dot and path == PurePosixPath("."):
        raise RuntimeManifestError(f"{field} must not be '.'")
    return path.as_posix()


@dataclass(frozen=True)
class RuntimeAsset:
    id: str
    version: str
    url: str
    sha256: str
    archive_type: str
    install_dir: str
    strip_components: int
    bin_dirs: tuple[str, ...]
    executables: Mapping[str, str]

    @classmethod
    def model_validate(cls, raw: Any, *, field: str = "asset") -> RuntimeAsset:
        value = _mapping(raw, field)
        url = _required_text(value.get("url"), f"{field}.url")
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"https", "file"}:
            raise RuntimeManifestError(f"{field}.url must use https or file")
        sha256 = str(value.get("sha256") or "").strip().lower()
        if not _SHA256_RE.fullmatch(sha256):
            raise RuntimeManifestError(f"{field}.sha256 must be 64 lowercase hex characters")
        archive_type = _required_text(
            value.get("archiveType", value.get("archive_type")),
            f"{field}.archiveType",
        )
        if archive_type not in {"zip", "tar.gz", "tar.xz", "7z-sfx"}:
            raise RuntimeManifestError(f"{field}.archiveType is unsupported")
        try:
            strip_components = int(
                value.get("stripComponents", value.get("strip_components", 0))
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeManifestError(
                f"{field}.stripComponents must be a non-negative integer"
            ) from exc
        if strip_components < 0:
            raise RuntimeManifestError(
                f"{field}.stripComponents must be a non-negative integer"
            )
        raw_bins = value.get("binDirs", value.get("bin_dirs"))
        if not isinstance(raw_bins, list) or not raw_bins:
            raise RuntimeManifestError(f"{field}.binDirs must be a non-empty array")
        bin_dirs = tuple(
            _safe_relative_path(item, f"{field}.binDirs", allow_dot=True)
            for item in raw_bins
        )
        raw_executables = _mapping(value.get("executables"), f"{field}.executables")
        if not raw_executables:
            raise RuntimeManifestError(f"{field}.executables must not be empty")
        executables = {
            _required_text(name, f"{field}.executables key"): _safe_relative_path(
                path,
                f"{field}.executables.{name}",
            )
            for name, path in raw_executables.items()
        }
        return cls(
            id=_required_text(value.get("id"), f"{field}.id"),
            version=_required_text(value.get("version"), f"{field}.version"),
            url=url,
            sha256=sha256,
            archive_type=archive_type,
            install_dir=_safe_relative_path(
                value.get("installDir", value.get("install_dir")),
                f"{field}.installDir",
            ),
            strip_components=strip_components,
            bin_dirs=bin_dirs,
            executables=executables,
        )


@dataclass(frozen=True)
class RuntimeManifest:
    schema_version: int
    runtime_set: str
    assets: Mapping[str, Mapping[str, RuntimeAsset]]

    @classmethod
    def model_validate(cls, raw: Any) -> RuntimeManifest:
        value = _mapping(raw, "manifest")
        schema_version = value.get("schemaVersion", value.get("schema_version"))
        if schema_version != 1:
            raise RuntimeManifestError("schemaVersion must be 1")
        raw_assets = _mapping(value.get("assets"), "assets")
        if not raw_assets:
            raise RuntimeManifestError("assets must not be empty")
        assets: dict[str, dict[str, RuntimeAsset]] = {}
        for target, raw_target_assets in raw_assets.items():
            target_name = _required_text(target, "asset target")
            target_assets = _mapping(raw_target_assets, f"assets.{target_name}")
            required_keys = (
                _RUNTIME_KEYS
                if target_name.startswith("windows-")
                else _PORTABLE_RUNTIME_KEYS
            )
            missing = [key for key in required_keys if key not in target_assets]
            if missing:
                raise RuntimeManifestError(
                    f"assets.{target_name} is missing: {', '.join(missing)}"
                )
            assets[target_name] = {
                key: RuntimeAsset.model_validate(
                    target_assets[key],
                    field=f"assets.{target_name}.{key}",
                )
                for key in _RUNTIME_KEYS
                if key in target_assets
            }
        return cls(
            schema_version=1,
            runtime_set=_required_text(
                value.get("runtimeSet", value.get("runtime_set")),
                "runtimeSet",
            ),
            assets=assets,
        )

    @classmethod
    def from_path(cls, path: str | Path) -> RuntimeManifest:
        manifest_path = Path(path)
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeManifestError(
                f"could not read runtime manifest {manifest_path}: {exc}"
            ) from exc
        return cls.model_validate(payload)


def _runtime_policy(
    value: RuntimePolicySettings | Mapping[str, Any] | None,
) -> RuntimePolicySettings:
    if value is None:
        return RuntimePolicySettings()
    if isinstance(value, RuntimePolicySettings):
        return value
    return RuntimePolicySettings.model_validate(value)


def _dedupe_paths(paths: Iterable[Path], *, windows: bool) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        candidate = Path(path)
        key = str(candidate).casefold() if windows else str(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return tuple(result)


class BundledRuntimeResolver:
    """Resolve pinned runtime paths for one packaged platform target."""

    def __init__(
        self,
        manifest: RuntimeManifest | Mapping[str, Any] | str | Path,
        *,
        resource_root: str | Path,
        platform: str | None = None,
        arch: str | None = None,
    ) -> None:
        if isinstance(manifest, RuntimeManifest):
            parsed = manifest
        elif isinstance(manifest, (str, Path)):
            parsed = RuntimeManifest.from_path(manifest)
        else:
            parsed = RuntimeManifest.model_validate(manifest)
        self.manifest = parsed
        self.resource_root = Path(resource_root)
        self.target = runtime_target(platform, arch)

    def _assets(self) -> Mapping[str, RuntimeAsset]:
        try:
            return self.manifest.assets[self.target]
        except KeyError as exc:
            raise RuntimeManifestError(
                f"runtime manifest does not contain target {self.target}"
            ) from exc

    def target_root(self) -> Path:
        return self.resource_root / self.target

    def runtime_roots(
        self,
        policy: RuntimePolicySettings | Mapping[str, Any] | None = None,
    ) -> tuple[Path, ...]:
        settings = _runtime_policy(policy)
        if not settings.enabled:
            return ()
        assets = self._assets()
        enabled = (
            ("python", settings.python),
            ("node", settings.node),
            ("gitBash", settings.git_bash),
        )
        return tuple(
            self.target_root() / assets[key].install_dir
            for key, is_enabled in enabled
            if is_enabled and key in assets
        )

    def bundled_path(
        self,
        policy: RuntimePolicySettings | Mapping[str, Any] | None = None,
    ) -> tuple[Path, ...]:
        settings = _runtime_policy(policy)
        if not settings.enabled:
            return ()
        assets = self._assets()
        enabled = (
            ("python", settings.python),
            ("node", settings.node),
            ("gitBash", settings.git_bash),
        )
        paths = (
            self.target_root() / assets[key].install_dir / bin_dir
            for key, is_enabled in enabled
            if is_enabled and key in assets
            for bin_dir in assets[key].bin_dirs
        )
        return _dedupe_paths(paths, windows=self.target.startswith("windows-"))

    def path_for(
        self,
        mode: RunMode | str,
        host_path: Iterable[str | Path],
        *,
        policy: RuntimePolicySettings | Mapping[str, Any] | None = None,
    ) -> tuple[Path, ...]:
        host = tuple(Path(entry) for entry in host_path if str(entry).strip())
        bundled = self.bundled_path(policy)
        combined = (
            (*host, *bundled)
            if normalize_run_mode(mode) is RunMode.FULL
            else (*bundled, *host)
        )
        return _dedupe_paths(combined, windows=self.target.startswith("windows-"))

    def executable_paths(
        self,
        policy: RuntimePolicySettings | Mapping[str, Any] | None = None,
    ) -> Mapping[str, Path]:
        settings = _runtime_policy(policy)
        if not settings.enabled:
            return {}
        assets = self._assets()
        enabled = {
            "python": settings.python,
            "node": settings.node,
            "gitBash": settings.git_bash,
        }
        result: dict[str, Path] = {}
        for key, asset in assets.items():
            if not enabled[key]:
                continue
            install_root = self.target_root() / asset.install_dir
            for name, relative_path in asset.executables.items():
                result[name] = install_root / relative_path
        return result


def split_path(value: str | None) -> tuple[Path, ...]:
    return tuple(Path(part) for part in (value or "").split(os.pathsep) if part.strip())


__all__ = [
    "BundledRuntimeResolver",
    "RuntimeAsset",
    "RuntimeManifest",
    "RuntimeManifestError",
    "runtime_target",
    "split_path",
]
