"""Native platform adapter for fixed-semantics Artifact Workbench open operations."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from opensquilla.application.artifact_workbench import (
    ArtifactContentPort,
    ArtifactContentQuery,
    ContentMaterial,
    NativeArtifactOpen,
    NativeArtifactOpenError,
    NativeArtifactOpenPort,
    NativeArtifactUnsupportedError,
)
from opensquilla.paths import native_io_path

_OPENABLE_HTML_MIMES = frozenset({"text/html", "application/xhtml+xml"})
_OPENABLE_HTML_SUFFIXES = frozenset({".html", ".htm", ".xhtml"})
_MIME_EXTENSION_FALLBACKS = {
    "text/html": ".html",
    "application/xhtml+xml": ".xhtml",
}
_OPEN_CACHE_MAX_AGE_SECONDS = 60 * 60
_UNSAFE_OPEN_FILENAME_RE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]+')


def _normalized_mime(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _is_html_material(material: ContentMaterial) -> bool:
    if _normalized_mime(material.media_type) in _OPENABLE_HTML_MIMES:
        return True
    return Path(material.filename or "").suffix.lower() in _OPENABLE_HTML_SUFFIXES


def _safe_open_filename(name: str) -> str:
    base = Path(str(name or "artifact")).name.strip().replace("\\", "_")
    cleaned = _UNSAFE_OPEN_FILENAME_RE.sub("_", base).strip()
    return cleaned or "artifact"


def _extension_for_open_name(name: str, mime: str) -> str:
    if Path(name).suffix:
        return ""
    return _MIME_EXTENSION_FALLBACKS.get(_normalized_mime(mime), "")


def _artifact_open_cache_dir() -> Path:
    root = Path(tempfile.gettempdir()) / "opensquilla-artifacts"
    try:
        root.mkdir(parents=True, exist_ok=False, mode=0o700)
    except FileExistsError:
        pass
    if root.is_symlink() or not root.is_dir():
        raise OSError("unsafe artifact open temp directory")
    if sys.platform != "win32":
        uid = getattr(os, "getuid", lambda: None)()
        stat_result = root.stat()
        if uid is not None and getattr(stat_result, "st_uid", uid) != uid:
            raise OSError("artifact open temp directory is owned by another user")
        if stat_result.st_mode & 0o077:
            root.chmod(0o700)
            stat_result = root.stat()
            if stat_result.st_mode & 0o077:
                raise OSError("artifact open temp directory permissions are too broad")
    return root


def _prune_artifact_open_cache(root: Path) -> None:
    try:
        now = time.time()
        for entry in root.iterdir():
            try:
                if now - entry.stat().st_mtime > _OPEN_CACHE_MAX_AGE_SECONDS:
                    entry.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _materialize_artifact_for_open(material: ContentMaterial) -> Path:
    root = _artifact_open_cache_dir()
    _prune_artifact_open_cache(root)
    name = _safe_open_filename(material.filename or "artifact")
    suffix = _extension_for_open_name(name, material.media_type)
    destination = root / f"{uuid4()}-{name}{suffix}"
    shutil.copyfile(native_io_path(material.path), destination)
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return destination


def _open_path_with_default_app(path: Path) -> str | None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
            return None
        command = ("open", str(path)) if sys.platform == "darwin" else ("xdg-open", str(path))
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if process.poll() not in (None, 0):
            return "system opener failed"
        return None
    except Exception:
        return "system opener failed"


class GatewayNativeArtifactOpenPort(NativeArtifactOpenPort):
    def __init__(
        self,
        content: ArtifactContentPort,
        *,
        materialize: Callable[[ContentMaterial], Path] = _materialize_artifact_for_open,
        open_path: Callable[[Path], str | None] = _open_path_with_default_app,
    ) -> None:
        self._content = content
        self._materialize = materialize
        self._open_path = open_path

    async def open_artifact(self, command: NativeArtifactOpen) -> None:
        material = await self._content.artifact_content(
            ArtifactContentQuery(command.session_key, command.artifact_id)
        )
        if not _is_html_material(material):
            raise NativeArtifactUnsupportedError("artifact type is not supported for native open")
        try:
            open_path = self._materialize(material)
        except OSError as exc:
            raise NativeArtifactOpenError("artifact open failed") from exc
        if self._open_path(open_path):
            raise NativeArtifactOpenError("artifact open failed")


__all__ = [
    "GatewayNativeArtifactOpenPort",
    "_artifact_open_cache_dir",
    "_materialize_artifact_for_open",
    "_open_path_with_default_app",
]
