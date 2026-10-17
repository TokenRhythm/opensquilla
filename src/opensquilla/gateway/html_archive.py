"""Bounded, request-owned HTML ZIP exports; no cache or background job."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import tempfile
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from starlette.responses import FileResponse
from starlette.types import Receive, Scope, Send

from opensquilla.application.artifact_workbench import ContentExportError, ContentMaterial
from opensquilla.artifacts import (
    DEFAULT_ARTIFACT_BUNDLE_MAX_BYTES,
    DEFAULT_ARTIFACT_BUNDLE_MAX_FILES,
    ArtifactBudgetError,
    ArtifactBundle,
    ArtifactIntegrityError,
    ArtifactPathError,
    ArtifactStore,
    _extract_bundle_references,
    _resolve_bundle_reference,
    _validate_bundle_file_set,
)

MAX_ZIP_BYTES = DEFAULT_ARTIFACT_BUNDLE_MAX_BYTES + 2 * 1024 * 1024
_HTML_MIMES = {"text/html", "application/xhtml+xml"}
_HTML_SUFFIXES = {".html", ".htm", ".xhtml"}
_JS_SUFFIXES = {".cjs", ".js", ".jsx", ".mjs", ".ts", ".tsx"}


def capture_saved_html(
    store: ArtifactStore,
    artifact_id: str,
    session_id: str,
    *,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
) -> ArtifactBundle:
    """Capture only this immutable manifest, never a storage directory walk."""
    ref = store.get_ref(artifact_id=artifact_id, session_id=session_id)
    if ref.mime not in _HTML_MIMES and Path(ref.name).suffix.lower() not in _HTML_SUFFIXES:
        raise ContentExportError("ZIP_UNSUPPORTED", 415, "Only HTML bundles can be exported")
    bundle = store.read_verified_bundle(
        artifact_id,
        session_id=session_id,
        max_bytes=DEFAULT_ARTIFACT_BUNDLE_MAX_BYTES,
    )
    if expected_sha256 is not None:
        entry = next(item for item in bundle.files if item.path == bundle.entrypoint)
        if (
            len(entry.data) != expected_size
            or hashlib.sha256(entry.data).hexdigest() != expected_sha256
        ):
            raise ArtifactIntegrityError("HTML bundle does not match the selected revision")
    return bundle


def validate_html_archive(bundle: ArtifactBundle) -> None:
    _validate_bundle_file_set(
        list(bundle.files),
        entrypoint=bundle.entrypoint,
        max_bytes=DEFAULT_ARTIFACT_BUNDLE_MAX_BYTES,
        max_files=DEFAULT_ARTIFACT_BUNDLE_MAX_FILES,
    )
    entry = next(item for item in bundle.files if item.path == bundle.entrypoint)
    if entry.mime not in _HTML_MIMES and Path(entry.path).suffix.lower() not in _HTML_SUFFIXES:
        raise ContentExportError("ZIP_UNSUPPORTED", 415, "Only HTML bundles can be exported")
    # Dynamic expressions and remote URLs stay untouched. Known missing or
    # unsafe local references must not silently turn a partial preview into an
    # apparently complete export. Recheck directory and legacy single-file
    # bundles too, because their historical manifests may contain no warnings.
    if set(bundle.warning_codes) - {"dynamic_dependency"}:
        raise ContentExportError("ZIP_INCOMPLETE", 409, "HTML bundle has unavailable dependencies")
    paths = {item.path for item in bundle.files}
    for item in bundle.files:
        references, _ = _extract_bundle_references(
            item.path,
            item.data,
            force_html=item.path == bundle.entrypoint,
        )
        for reference in references:
            dependency = _resolve_bundle_reference(reference, source_path=item.path)
            if dependency is None:
                continue
            if (
                Path(item.path).suffix.lower() in _JS_SUFFIXES
                and "/" not in reference
                and not reference.startswith(".")
                and not Path(reference).suffix
            ):
                continue  # Browser/import-map resolution; never walk packages.
            if dependency not in paths:
                raise ContentExportError(
                    "ZIP_INCOMPLETE",
                    409,
                    "HTML bundle is missing a local dependency",
                )


def _remove_archive(path: Path) -> None:
    # path is minted only by _build_archive in its own random private directory.
    shutil.rmtree(path.parent)


async def _settle(task: asyncio.Task[Any]) -> None:
    """A cancelled coroutine cannot stop a filesystem worker."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except (asyncio.CancelledError, Exception):
            continue


async def cleanup_archive(path: Path) -> None:
    task = asyncio.create_task(asyncio.to_thread(_remove_archive, path))
    try:
        await asyncio.shield(task)
    except BaseException:
        await _settle(task)
        task.result()
        raise


def _build_archive(capture: Callable[[], ArtifactBundle]) -> ContentMaterial:
    root: Path | None = None
    complete = False
    try:
        bundle = capture()
        validate_html_archive(bundle)
        root = Path(tempfile.mkdtemp(prefix="opensquilla-html-export-"))
        path = root / "bundle.zip"
        with zipfile.ZipFile(
            path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=1,
        ) as archive:
            for item in bundle.files:
                archive.writestr(item.path, item.data)
                if path.stat().st_size > MAX_ZIP_BYTES:
                    raise ArtifactBudgetError("HTML ZIP exceeds the output budget")
        if path.stat().st_size > MAX_ZIP_BYTES:
            raise ArtifactBudgetError("HTML ZIP exceeds the output budget")
        result = ContentMaterial(
            path,
            "application/zip",
            Path(bundle.entrypoint).stem + ".zip",
        )
        complete = True
        return result
    except ArtifactBudgetError as exc:
        raise ContentExportError(
            "ZIP_TOO_LARGE",
            413,
            "HTML bundle exceeds the export budget",
        ) from exc
    except ArtifactPathError as exc:
        raise ContentExportError(
            "ZIP_INCOMPLETE",
            409,
            "HTML bundle contains unsafe or unreadable dependencies",
        ) from exc
    finally:
        # A successful return hands ownership to the response. Failed captures
        # create no temp directory; failed ZIP writes are cleaned by the wrapper.
        if root is not None and not complete:
            shutil.rmtree(root)


class HtmlZipExporter:
    def __init__(self) -> None:
        self._slots = asyncio.Semaphore(2)

    async def export(
        self,
        capture: Callable[[], ArtifactBundle],
        *,
        validate: Callable[[], Awaitable[None]],
    ) -> ContentMaterial:
        await self._slots.acquire()
        transferred = False
        try:
            # Recheck authority after queueing and after capture/compression.
            await validate()
            worker = asyncio.create_task(asyncio.to_thread(_build_archive, capture))
            try:
                result = await asyncio.shield(worker)
                await validate()
                released = False

                async def cleanup() -> None:
                    nonlocal released
                    if released:
                        return
                    released = True
                    try:
                        if result.path is not None:
                            await cleanup_archive(result.path)
                    finally:
                        self._slots.release()

                transferred = True
                return replace(result, cleanup=cleanup)
            except BaseException:
                await _settle(worker)
                if not worker.cancelled() and worker.exception() is None:
                    material = worker.result()
                    if material.path is not None:
                        await cleanup_archive(material.path)
                raise
        finally:
            if not transferred:
                self._slots.release()


class TemporaryArchiveResponse(FileResponse):
    def __init__(self, material: ContentMaterial) -> None:
        assert material.path is not None and material.cleanup is not None
        self._cleanup = material.cleanup
        super().__init__(
            material.path,
            media_type=material.media_type,
            filename=material.filename,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._cleanup()
