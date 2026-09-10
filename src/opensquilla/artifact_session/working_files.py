"""Workspace files backed by immutable document versions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import shutil
import weakref
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from opensquilla.artifacts import (
    ArtifactBundle,
    ArtifactBundleSourceFile,
    ArtifactRef,
    ArtifactSource,
    ArtifactStore,
    artifact_bundle_manifest,
    collect_artifact_bundle,
)

from .errors import ArtifactConflictError, ArtifactValidationError
from .models import (
    Actor,
    ActorKind,
    ArtifactBlobRef,
    ChangeSet,
    CommitResult,
    Revision,
    RevisionSource,
)
from .service import ArtifactSessionService

_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def _lock(document_id: str) -> asyncio.Lock:
    lock = _LOCKS.get(document_id)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[document_id] = lock
    return lock


def checked_path(root: Path, relative: str) -> Path:
    """Resolve a regular resource without traversing links or leaving its root."""
    logical = PurePosixPath(relative)
    if (
        not relative
        or logical.is_absolute()
        or ".." in logical.parts
        or "\\" in relative
        or ":" in relative
        or "\x00" in relative
    ):
        raise ArtifactValidationError("Invalid working resource path")
    path = root
    if root.is_symlink() or root.resolve() != root.absolute():
        raise ArtifactValidationError("Working directory contains a link")
    for part in logical.parts:
        path = path / part
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ArtifactValidationError("Working resource contains a link")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ArtifactValidationError("Working resource is outside its directory")
    return path


def _working_bundle_digest(bundle: ArtifactBundle) -> str:
    # Host MIME registries use equivalent names for JavaScript. Normalize only
    # for comparison, leaving persisted resource metadata and bytes intact.
    files = tuple(
        replace(item, mime="text/javascript")
        if item.mime in {"application/javascript", "application/x-javascript"}
        else item
        for item in bundle.files
    )
    return artifact_bundle_manifest(replace(bundle, files=files)).bundle_digest


@dataclass(frozen=True)
class WorkingFiles:
    document_id: str
    workspace: str
    relative_root: str
    entrypoint: str
    base_revision_id: str
    source_path: str | None = None
    bundle_mode: str | None = None
    bundle_root: str | None = None
    entry_mime: str = "text/html"

    @property
    def root(self) -> Path:
        return checked_path(Path(self.workspace), self.relative_root)

    @property
    def entry(self) -> Path:
        if self.source_path is not None:
            return checked_path(Path(self.workspace), self.source_path)
        return checked_path(self.root, self.entrypoint)

    def bundle(self) -> ArtifactBundle:
        if self.source_path is not None and self.bundle_mode == "none":
            return ArtifactBundle(entrypoint=self.entrypoint, files=(ArtifactBundleSourceFile(
                path=self.entrypoint, mime=self.entry_mime, data=self.entry.read_bytes(),
            ),))
        bundle = collect_artifact_bundle(
            self.entry,
            workspace_root=self.workspace,
            mode=self.bundle_mode or "directory",
            bundle_root=(self.root if self.bundle_mode in {None, "directory"} else None),
            entry_mime=self.entry_mime,
        )
        if bundle is None:
            raise ArtifactValidationError("Working document has no HTML entrypoint")
        return bundle

    def digest(self) -> str:
        return _working_bundle_digest(self.bundle())


async def get_working_files(
    service: ArtifactSessionService,
    document_id: str,
) -> WorkingFiles | None:
    async with service.repository._read_transaction("working_files.get") as conn:
        cursor = await conn.execute(
            "SELECT * FROM artifact_working_files WHERE document_id = ?",
            (document_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        cursor = await conn.execute(
            "SELECT source_path, bundle_mode, bundle_root FROM artifact_working_sources "
            "WHERE document_id=?", (document_id,),
        )
        source = await cursor.fetchone()
        revision = await service.repository._get_revision_on_conn(conn, row["base_revision_id"])
        return WorkingFiles(
            **dict(row), **(dict(source) if source is not None else {}),
            entry_mime=revision.media_type,
        )


def prepare_working_source(
    store: ArtifactStore, ref: ArtifactRef, workspace: str, source: ArtifactSource,
) -> dict[str, str]:
    """Translate host publication provenance without inferring identity from content or names."""
    root = Path(workspace).resolve()
    path = Path(source.path)
    if not path.is_absolute():
        raise ArtifactValidationError("Publication source must be absolute")
    relative_path = path.relative_to(root).as_posix()
    checked_path(root, relative_path)
    if source.bundle_mode not in {"auto", "none", "directory"}:
        raise ArtifactValidationError("Invalid publication collection mode")
    if source.bundle_mode == "directory":
        if source.bundle_root is None or not Path(source.bundle_root).is_absolute():
            raise ArtifactValidationError("Directory publication has no source root")
        collection_root = Path(source.bundle_root)
        relative_root = collection_root.relative_to(root).as_posix()
        checked_path(root, relative_root)
        path.relative_to(collection_root)
    else:
        if source.bundle_root is not None:
            raise ArtifactValidationError("Unexpected publication source root")
        collection_root = path.parent
        relative_root = collection_root.relative_to(root).as_posix()
    manifest = store.validate_preview_bundle(ref.id, session_id=ref.session_id)
    entrypoint = manifest.entrypoint if manifest is not None else ref.name
    if manifest is not None and path.relative_to(collection_root).as_posix() != entrypoint:
        raise ArtifactValidationError("Publication source entrypoint changed")
    return {
        "workspace": str(root), "source_path": relative_path,
        "relative_root": relative_root, "entrypoint": entrypoint,
        "bundle_mode": "none" if manifest is None else source.bundle_mode,
        "bundle_root": relative_root if source.bundle_mode == "directory" else "",
    }


async def _save_source_binding(
    conn: Any, binding: WorkingFiles,
) -> None:
    await conn.execute(
        "UPDATE artifact_working_files SET relative_root=?, entrypoint=?, base_revision_id=? "
        "WHERE document_id=? AND workspace=?",
        (binding.relative_root, binding.entrypoint, binding.base_revision_id,
         binding.document_id, binding.workspace),
    )
    await conn.execute(
        "UPDATE artifact_working_sources SET bundle_mode=?, bundle_root=? "
        "WHERE document_id=? AND workspace=? AND source_path=?",
        (binding.bundle_mode, binding.bundle_root, binding.document_id,
         binding.workspace, binding.source_path),
    )
    # A mode-only publication may reuse a revision. Its first captured collection
    # boundary remains immutable even when the current source mode changes.
    await conn.execute(
        "INSERT OR IGNORE INTO artifact_working_source_versions VALUES (?, ?, ?, ?, ?, ?)",
        (binding.base_revision_id, binding.document_id, binding.relative_root,
         binding.entrypoint, binding.bundle_mode, binding.bundle_root),
    )


async def _record_publication(
    service: ArtifactSessionService, conn: Any, *, document_id: str, revision_id: str,
    publication_key: str, artifact_id: str, actor: Actor,
) -> None:
    await conn.execute(
        "INSERT OR IGNORE INTO artifact_audit_events "
        "(event_id, document_id, event_type, actor_kind, actor_id, revision_id, "
        "payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (publication_key, document_id, "document.source_published", actor.kind.value,
         actor.actor_id, revision_id, json.dumps({"artifact_id": artifact_id}),
         service.repository._clock()),
    )


def load_version_bundle(store: ArtifactStore, artifact_id: str, session_id: str) -> ArtifactBundle:
    manifest = store.validate_preview_bundle(artifact_id, session_id=session_id)
    ref = store.get_ref(artifact_id=artifact_id, session_id=session_id)
    entrypoint = manifest.entrypoint if manifest else ref.name
    paths = [item.path for item in manifest.files] if manifest else [entrypoint]
    sources = []
    for logical_path in paths:
        resource = store.resolve_preview_resource(
            artifact_id,
            session_id=session_id,
            logical_path=logical_path,
        )
        data = resource.path.read_bytes()
        if hashlib.sha256(data).hexdigest() != resource.sha256 or len(data) != resource.size:
            raise ArtifactValidationError("Stored version failed its integrity check")
        sources.append(ArtifactBundleSourceFile(path=logical_path, mime=resource.mime, data=data))
    return ArtifactBundle(entrypoint=entrypoint, files=tuple(sources))


def _restore_source(
    binding: WorkingFiles, bundle: ArtifactBundle, previous: ArtifactBundle,
) -> Callable[[], None]:
    # A workspace source can share its directory with other projects. Preserve
    # every overwritten byte before any writes, and touch only version-owned paths.
    recovery_root = checked_path(Path(binding.workspace), "artifacts")
    if binding.bundle_mode == "directory" and recovery_root.is_relative_to(binding.root):
        recovery_root = checked_path(
            Path(binding.workspace), f"artifact-recovery-{secrets.token_hex(12)}",
        )
    recovery_root.mkdir(exist_ok=True)
    recovery = checked_path(recovery_root, f"recovered-{secrets.token_hex(12)}")
    recovery.mkdir()
    old = {item.path for item in previous.files}
    target = {item.path: item for item in bundle.files}

    def physical(logical: str, snapshot: ArtifactBundle) -> Path:
        if logical == snapshot.entrypoint:
            return binding.entry
        root = binding.entry.parents[len(PurePosixPath(snapshot.entrypoint).parts) - 1]
        checked_path(Path(binding.workspace), root.relative_to(binding.workspace).as_posix())
        return checked_path(root, logical)

    old_paths = {physical(name, previous) for name in old}
    target_paths = {name: physical(name, bundle) for name in target}
    paths = old_paths | set(target_paths.values())
    for path in sorted(paths):
        if path.exists():
            if not path.is_file():
                raise ArtifactValidationError("Restore would replace a non-file resource")
            saved = checked_path(recovery / "files", path.relative_to(binding.workspace).as_posix())
            saved.parent.mkdir(parents=True, exist_ok=True)
            with saved.open("xb") as stream:
                stream.write(path.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
    (recovery / "restore.json").write_text(json.dumps({
        "document_id": binding.document_id, "base_revision_id": binding.base_revision_id,
        "overwritten": sorted(path.relative_to(binding.workspace).as_posix() for path in paths),
        "target": sorted(target),
    }))
    def rollback() -> None:
        for path in sorted(paths):
            relative = path.relative_to(binding.workspace).as_posix()
            checked_path(Path(binding.workspace), relative)
            saved = checked_path(recovery / "files", relative)
            if saved.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(saved, path)
            else:
                path.unlink(missing_ok=True)

    try:
        for name, source in target.items():
            path = physical(name, bundle)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".restore-{secrets.token_hex(12)}")
            try:
                with temporary.open("xb") as stream:
                    stream.write(source.data)
                    stream.flush()
                    os.fsync(stream.fileno())
                physical(name, bundle)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        for path in old_paths - set(target_paths.values()):
            checked_path(Path(binding.workspace), path.relative_to(binding.workspace).as_posix())
            path.unlink(missing_ok=True)
    except BaseException:
        rollback()
        raise
    return rollback


def _materialize(
    binding: WorkingFiles, bundle: ArtifactBundle, previous: ArtifactBundle | None = None,
) -> Callable[[], None]:
    if binding.source_path is not None:
        if previous is None:
            raise ArtifactValidationError("Source restore has no prior ownership snapshot")
        return _restore_source(binding, bundle, previous)
    parent = checked_path(Path(binding.workspace), "artifacts")
    parent.mkdir(parents=True, exist_ok=True)
    staging = checked_path(parent, f".materialize-{secrets.token_hex(12)}")
    staging.mkdir()
    backup = checked_path(parent, f"recovered-{secrets.token_hex(12)}")
    moved = False
    try:
        for source in bundle.files:
            target = checked_path(staging, source.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(source.data)
                stream.flush()
                os.fsync(stream.fileno())
        if binding.root.exists():
            # Preserve interrupted edits and untracked resources when restoring a version.
            os.replace(binding.root, backup)
            moved = True
        try:
            os.replace(staging, binding.root)
        except BaseException:
            if moved:
                os.replace(backup, binding.root)
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    def rollback() -> None:
        if binding.root.exists():
            shutil.rmtree(binding.root)
        if moved:
            os.replace(backup, binding.root)

    return rollback


async def _settle_rollback(rollback: Callable[[], None]) -> None:
    operation = asyncio.create_task(asyncio.to_thread(rollback))
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            continue
    operation.result()


async def _materialize_before_unlock(
    binding: WorkingFiles, bundle: ArtifactBundle, previous: ArtifactBundle | None = None,
) -> Callable[[], None]:
    # Cancelling to_thread does not stop its filesystem writes. Keep the caller's
    # document lock until the worker settles, including repeated cancellation.
    operation = asyncio.create_task(asyncio.to_thread(_materialize, binding, bundle, previous))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not operation.cancelled() and operation.exception() is None:
            await _settle_rollback(operation.result())
        raise


async def _binding_for_version(
    conn: Any, current: WorkingFiles, revision: Revision, bundle: ArtifactBundle,
) -> WorkingFiles:
    if current.source_path is None:
        return replace(
            current, base_revision_id=revision.revision_id,
            entrypoint=bundle.entrypoint, entry_mime=revision.media_type,
        )
    source_revision = revision
    seen: set[str] = set()
    while True:
        cursor = await conn.execute(
            "SELECT relative_root, entrypoint, bundle_mode, bundle_root "
            "FROM artifact_working_source_versions WHERE document_id=? AND revision_id=?",
            (current.document_id, source_revision.revision_id),
        )
        settings = await cursor.fetchone()
        if settings is not None:
            return replace(
                current, base_revision_id=revision.revision_id,
                entry_mime=revision.media_type, **dict(settings),
            )
        copied_from = source_revision.copied_from_revision_id
        if copied_from is None or copied_from in seen:
            raise ArtifactValidationError("Stored source version has no collection metadata")
        seen.add(copied_from)
        cursor = await conn.execute(
            "SELECT * FROM artifact_revisions WHERE revision_id=? AND document_id=?",
            (copied_from, current.document_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ArtifactValidationError("Stored source version has no collection metadata")
        # Only the immutable lineage fields are needed to find the saved source layout.
        source_revision = replace(
            source_revision, revision_id=row["revision_id"],
            copied_from_revision_id=row["copied_from_revision_id"],
        )


async def ensure_working_files(
    service: ArtifactSessionService,
    store: ArtifactStore,
    *,
    document_id: str,
    session_key: str,
    session_id: str,
    workspace: str,
) -> WorkingFiles:
    """Create a full working copy, or reconcile an explicitly restored version."""
    async with _lock(document_id):
        async with service.repository._transaction("working_files.bind") as conn:
            document = await service.repository._get_document_on_conn(conn, document_id)
            revision = await service.repository._get_revision_on_conn(
                conn, document.head_revision_id
            )
            if document.session_key != session_key or document.session_id != session_id:
                raise ArtifactValidationError("Working document belongs to another session")
            cursor = await conn.execute(
                "SELECT * FROM artifact_working_files WHERE document_id = ?",
                (document_id,),
            )
            row = await cursor.fetchone()
            current = WorkingFiles(**dict(row)) if row is not None else None
            if current is not None:
                cursor = await conn.execute(
                    "SELECT source_path, bundle_mode, bundle_root FROM artifact_working_sources "
                    "WHERE document_id=?", (document_id,),
                )
                source = await cursor.fetchone()
                if source is not None:
                    current = replace(current, **dict(source))
                current_revision = await service.repository._get_revision_on_conn(
                    conn, current.base_revision_id,
                )
                current = replace(current, entry_mime=current_revision.media_type)
            resolved_workspace = str(Path(workspace).resolve())
            if current is not None and current.workspace != resolved_workspace:
                raise ArtifactConflictError("Working document belongs to another workspace")
            if current is not None and current.base_revision_id == revision.revision_id:
                current.entry  # Validate paths again; missing files are not silently overwritten.
                return current
            bundle = await asyncio.to_thread(
                load_version_bundle,
                store,
                revision.artifact_id,
                session_id,
            )
            if current is not None:
                binding = await _binding_for_version(conn, current, revision, bundle)
            else:
                binding = WorkingFiles(
                    document_id=document_id,
                    workspace=resolved_workspace,
                    relative_root=f"artifacts/{hashlib.sha256(document_id.encode()).hexdigest()[:24]}",
                    entrypoint=bundle.entrypoint,
                    base_revision_id=revision.revision_id,
                    entry_mime=revision.media_type,
                )
            previous = await asyncio.to_thread(
                load_version_bundle, store,
                (await service.repository._get_revision_on_conn(
                    conn, current.base_revision_id,
                )).artifact_id, session_id,
            ) if current is not None and current.source_path is not None else None
            await _materialize_before_unlock(binding, bundle, previous)
            await conn.execute(
                "INSERT INTO artifact_working_files VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(document_id) DO UPDATE SET workspace=excluded.workspace, "
                "relative_root=excluded.relative_root, entrypoint=excluded.entrypoint, "
                "base_revision_id=excluded.base_revision_id",
                (
                    binding.document_id,
                    binding.workspace,
                    binding.relative_root,
                    binding.entrypoint,
                    binding.base_revision_id,
                ),
            )
            if binding.source_path is not None:
                await _save_source_binding(conn, binding)
            return binding


async def restore_working_revision(
    service: ArtifactSessionService,
    store: ArtifactStore,
    *,
    document_id: str,
    session_key: str,
    session_id: str,
    target_revision_id: str,
    expected_head_revision_id: str,
    expected_state_revision: int,
    actor: Actor,
    turn_id: str,
) -> tuple[CommitResult, ChangeSet, bool]:
    """Restore existing bytes and their head together, without creating a content version."""
    async with _lock(document_id):
        current = await get_working_files(service, document_id)
        rollback: Callable[[], None] | None = None
        try:
            async with service.repository._transaction("working_files.restore") as conn:
                document = await service.repository._get_document_on_conn(conn, document_id)
                if document.session_key != session_key or document.session_id not in {
                    None, session_id,
                }:
                    raise ArtifactValidationError("Working document belongs to another session")
                async def restore(no_op: bool | None = None) -> tuple[
                    CommitResult, ChangeSet | None, bool,
                ]:
                    return await service.repository._restore_revision_on_conn(
                        conn, document_id=document_id, target_revision_id=target_revision_id,
                        expected_head_revision_id=expected_head_revision_id,
                        expected_state_revision=expected_state_revision, actor=actor,
                        turn_id=turn_id, no_op=no_op,
                    )
                cursor = await conn.execute(
                    "SELECT 1 FROM artifact_change_sets WHERE document_id=? AND turn_id=?",
                    (document_id, turn_id),
                )
                if await cursor.fetchone() is not None:
                    result, change, replayed = await restore()
                    assert change is not None and replayed
                    return result, change, replayed
                target = await service.repository._get_revision_on_conn(
                    conn, target_revision_id,
                )
                if target.document_id != document_id:
                    raise ArtifactValidationError("Target revision belongs to another document")
                if (
                    document.head_revision_id != expected_head_revision_id
                    or document.state_revision != expected_state_revision
                ):
                    raise ArtifactConflictError("Document changed before version restoration")
                unchanged = target_revision_id == document.head_revision_id
                binding = None
                bundle = None
                if current is not None:
                    bundle = await asyncio.to_thread(
                        load_version_bundle, store, target.artifact_id, session_id,
                    )
                    binding = await _binding_for_version(conn, current, target, bundle)
                    try:
                        unchanged = (
                            unchanged and current == binding
                            and binding.entry.exists() and binding.root.exists()
                            and await asyncio.to_thread(binding.digest)
                            == _working_bundle_digest(bundle)
                        )
                    except FileNotFoundError:
                        unchanged = False
                result, change, replayed = await restore(no_op=unchanged)
                assert change is not None
                if binding is not None and bundle is not None and not unchanged:
                    previous = await asyncio.to_thread(
                        load_version_bundle, store,
                        (await service.repository._get_revision_on_conn(
                            conn, current.base_revision_id,
                        )).artifact_id, session_id,
                    ) if current is not None and current.source_path is not None else None
                    rollback = await _materialize_before_unlock(binding, bundle, previous)
                    await conn.execute(
                        "UPDATE artifact_working_files SET relative_root=?, entrypoint=?, "
                        "base_revision_id=? WHERE document_id=?",
                        (binding.relative_root, binding.entrypoint,
                         binding.base_revision_id, document_id),
                    )
                    if binding.source_path is not None:
                        await _save_source_binding(conn, binding)
                return result, change, replayed
        except BaseException:
            if rollback is not None:
                # A storage commit may finish before cancellation reaches this caller.
                # Its durable receipt decides whether the working copy must stay restored.
                try:
                    check = asyncio.create_task(service.get_change_set_by_turn(
                        document_id=document_id, turn_id=turn_id,
                    ))
                    while not check.done():
                        try:
                            await asyncio.shield(check)
                        except asyncio.CancelledError:
                            continue
                    committed = check.result() is not None
                except (Exception, asyncio.CancelledError):
                    logging.getLogger(__name__).warning(
                        "Restore commit status unavailable; recovery files retained"
                    )
                else:
                    if not committed:
                        await _settle_rollback(rollback)
            raise


async def save_working_version(
    service: ArtifactSessionService,
    store: ArtifactStore,
    *,
    document_id: str,
    session_key: str,
    session_id: str,
    actor_id: str,
    published_ref: ArtifactRef | None = None,
    working_source: dict[str, str] | None = None,
    publication_id: str = "",
) -> Any | None:
    """Save current files or an explicit immutable publication without duplicating a version."""
    async with _lock(document_id):
        binding = await get_working_files(service, document_id)
        if binding is None:
            return None
        head = await service.get_document_head(document_id)
        if head.document.session_key != session_key or head.document.session_id != session_id:
            raise ArtifactValidationError("Working document belongs to another session")
        if working_source is not None and binding.source_path is not None:
            if (
                published_ref is None
                or working_source["workspace"] != binding.workspace
                or working_source["source_path"] != binding.source_path
            ):
                raise ArtifactValidationError("Publication source belongs to another working file")
            binding = replace(
                binding, relative_root=working_source["relative_root"],
                entrypoint=working_source["entrypoint"],
                bundle_mode=working_source["bundle_mode"],
                bundle_root=working_source["bundle_root"] or None,
                entry_mime=published_ref.mime,
            )
        actor = Actor(kind=ActorKind.AGENT, actor_id=actor_id)
        ref = published_ref
        publication_revision = None
        publication_key = None
        committed_publication_revision = None
        linked_publication_key = None
        occurrence_replay = False
        if published_ref is not None and publication_id:
            identity = f"{document_id}\0{publication_id}".encode()
            digest = hashlib.sha256(identity).hexdigest()
            publication_key = f"working-publish:{digest}"
            committed_publication_revision = f"rev_{digest}"
        if ref is not None:
            stored = await asyncio.to_thread(
                store.get_ref, session_id=session_id, artifact_id=ref.id
            )
            if (
                ref.session_id != session_id
                or ref.session_key != session_key
                or stored.session_id != session_id
                or stored.session_key != session_key
                or (ref.sha256, ref.name, ref.mime, ref.size)
                != (stored.sha256, stored.name, stored.mime, stored.size)
            ):
                raise ArtifactValidationError("Published version belongs to another resource")
            bundle = await asyncio.to_thread(load_version_bundle, store, ref.id, session_id)
            async with service.repository._read_transaction("working_files.publication") as conn:
                cursor = await conn.execute(
                    "SELECT document_id, revision_id, idempotency_key "
                    "FROM document_publish_attempts "
                    "WHERE session_id=? AND candidate_artifact_id=?",
                    (session_id, ref.id),
                )
                attempt = await cursor.fetchone()
                if attempt is not None:
                    if attempt["document_id"] != document_id:
                        raise ArtifactConflictError("Publication belongs to another document")
                    linked_publication_key = str(attempt["idempotency_key"])
                    if not publication_id:
                        publication_revision = str(attempt["revision_id"])
                        publication_key = linked_publication_key
                if publication_id:
                    cursor = await conn.execute(
                        "SELECT revision_id, payload_json FROM artifact_audit_events "
                        "WHERE event_id=? AND document_id=?",
                        (publication_key, document_id),
                    )
                    occurrence = await cursor.fetchone()
                    if occurrence is not None:
                        if json.loads(occurrence["payload_json"])["artifact_id"] != ref.id:
                            raise ArtifactConflictError("Publication occurrence changed resource")
                        publication_revision = str(occurrence["revision_id"])
                        occurrence_replay = True
                elif attempt is None:
                    # A prior save may have committed before publication reservation.
                    # Exact immutable identity recovers that revision, even after a later edit.
                    cursor = await conn.execute(
                        "SELECT revision_id FROM artifact_revisions "
                        "WHERE document_id=? AND " + (
                            "revision_id=?" if committed_publication_revision
                            else "artifact_id=? ORDER BY generation LIMIT 1"
                        ),
                        (document_id, committed_publication_revision or ref.id),
                    )
                    revision = await cursor.fetchone()
                    if revision is not None:
                        publication_revision = str(revision["revision_id"])
        else:
            bundle = await asyncio.to_thread(binding.bundle)

        result = None
        if publication_revision is None:
            if binding.base_revision_id != head.revision.revision_id:
                raise ArtifactConflictError("Working version changed during this turn")
            previous = await asyncio.to_thread(
                load_version_bundle, store, head.revision.artifact_id, session_id
            )
            unchanged = (
                _working_bundle_digest(bundle)
                == _working_bundle_digest(previous)
            )
            if unchanged:
                if ref is None:
                    return None
                publication_revision = head.revision.revision_id
            else:
                if ref is None:
                    ref = await asyncio.to_thread(
                        store.publish_bundle,
                        bundle,
                        session_id=session_id,
                        session_key=session_key,
                        name=head.document.name,
                        mime=head.revision.media_type,
                        source="working_files",
                        visibility="internal",
                    )
                async with service.repository._transaction("working_files.saved") as conn:
                    result = await service.repository._commit_revision_on_conn(
                        conn,
                        document_id=document_id,
                        expected_head_revision_id=head.revision.revision_id,
                        expected_state_revision=head.document.state_revision,
                        artifact=ArtifactBlobRef(
                            artifact_id=ref.id,
                            sha256=ref.sha256,
                            filename=ref.name,
                            media_type=ref.mime,
                            byte_size=ref.size,
                        ),
                        actor=actor,
                        source=RevisionSource.AGENT,
                        revision_id=committed_publication_revision,
                    )
                    await conn.execute(
                        "UPDATE artifact_working_files SET base_revision_id=? "
                        "WHERE document_id=? AND base_revision_id=?",
                        (result.revision.revision_id, document_id, binding.base_revision_id),
                    )
                    if binding.source_path is not None:
                        await _save_source_binding(
                            conn, replace(binding, base_revision_id=result.revision.revision_id),
                        )
                    if publication_id and publication_key is not None:
                        await _record_publication(
                            service, conn, document_id=document_id,
                            revision_id=result.revision.revision_id,
                            publication_key=publication_key, artifact_id=ref.id, actor=actor,
                        )
                publication_revision = result.revision.revision_id

        if (
            result is None and not occurrence_replay
            and publication_revision == head.revision.revision_id
            and (working_source is not None or publication_id)
        ):
            async with service.repository._transaction("working_files.source_mode") as conn:
                current_head = await service.repository._get_document_on_conn(conn, document_id)
                if (
                    current_head.head_revision_id != head.revision.revision_id
                    or binding.base_revision_id != head.revision.revision_id
                ):
                    raise ArtifactConflictError("Working version changed during publication")
                if working_source is not None and binding.source_path is not None:
                    await _save_source_binding(conn, binding)
                if publication_id and publication_key is not None and ref is not None:
                    await _record_publication(
                        service, conn, document_id=document_id,
                        revision_id=publication_revision, publication_key=publication_key,
                        artifact_id=ref.id, actor=actor,
                    )

        if published_ref is not None:
            assert ref is not None and publication_revision is not None
            if publication_key is None:
                identity = f"{document_id}\0{ref.id}".encode()
                publication_key = f"working-publish:{hashlib.sha256(identity).hexdigest()}"
            if linked_publication_key is not None:
                publication_key = linked_publication_key
            else:
                await service.reserve_document_publish_attempt(
                    session_key=session_key,
                    session_id=session_id,
                    idempotency_key=publication_key,
                    document_id=document_id,
                    revision_id=publication_revision,
                    candidate_artifact=ArtifactBlobRef(
                        artifact_id=ref.id,
                        sha256=ref.sha256,
                        filename=ref.name,
                        media_type=ref.mime,
                        byte_size=ref.size,
                    ),
                )
            await service.apply_document_publish_attempt(
                session_id=session_id, idempotency_key=publication_key, actor=actor
            )
            await service.mark_document_publish_promoted(
                session_id=session_id, idempotency_key=publication_key
            )
        return result
