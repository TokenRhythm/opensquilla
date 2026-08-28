"""Recoverable journaled writes across fixed profile roots."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import stat
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

from opensquilla.memory.profile_import.errors import (
    ProfileImportStalePreviewError,
    ProfileImportWriteError,
)
from opensquilla.memory.profile_import.files import (
    _is_link_or_reparse,
    ensure_safe_parent,
    image_hash,
    read_text_image,
    target_path,
)
from opensquilla.memory.profile_import.models import (
    ImportStatus,
    InternalFilePlan,
    InternalPreviewRecord,
    ProfileImportPaths,
    ProfileImportReceipt,
    PublishedFileIdentity,
    TransactionJournal,
)
from opensquilla.memory.profile_import.store import ProfileImportStore
from opensquilla.profile_import_io import (
    ConfigSnapshot,
    chmod_open_file,
    copy_macos_config_metadata,
    copy_windows_config_dacl,
    native_io_path,
    native_move_no_replace,
)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(native_io_path(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _current_matches(paths: ProfileImportPaths, plan: InternalFilePlan, *, after: bool) -> bool:
    root, path = target_path(paths, plan)
    exists, content, _mode = read_text_image(root, path)
    expected = plan.after_hash if after else plan.before_hash
    return image_hash(exists=exists, content=content) == expected


def assert_plan_baselines(paths: ProfileImportPaths, plans: list[InternalFilePlan]) -> None:
    for plan in plans:
        if not _current_matches(paths, plan, after=False):
            raise ProfileImportStalePreviewError(
                f"{plan.relative_path} changed after the profile import preview"
            )


def all_after_images_match(paths: ProfileImportPaths, plans: list[InternalFilePlan]) -> bool:
    return all(_current_matches(paths, plan, after=True) for plan in plans)


def _published_identity(
    journal: TransactionJournal,
    plan: InternalFilePlan,
) -> PublishedFileIdentity | None:
    return next(
        (
            identity
            for identity in journal.publication_identities
            if identity.target == plan.target
        ),
        None,
    )


def _snapshot_publication_identity(
    plan: InternalFilePlan,
    snapshot: ConfigSnapshot,
) -> PublishedFileIdentity:
    identity = snapshot.identity
    if identity is None:
        raise ProfileImportWriteError(
            f"{plan.relative_path} candidate disappeared before publication"
        )
    return PublishedFileIdentity(
        target=plan.target,
        device=identity.device,
        inode=identity.inode,
        mode=identity.mode,
        size=identity.size,
        modified_at_ns=identity.modified_at_ns,
        reparse_tag=identity.reparse_tag,
        link_target=identity.link_target,
    )


def _path_matches_publication_identity(
    path: Path,
    expected: PublishedFileIdentity,
) -> bool:
    try:
        snapshot = ConfigSnapshot.capture(path)
    except Exception:
        return False
    identity = snapshot.identity
    if identity is None:
        return False
    return identity.metadata_tuple() == (
        expected.device,
        expected.inode,
        expected.mode,
        expected.size,
        expected.modified_at_ns,
        expected.reparse_tag,
        expected.link_target,
    )


def _all_published_images_owned(
    paths: ProfileImportPaths,
    journal: TransactionJournal,
) -> bool:
    if not all_after_images_match(paths, journal.plans):
        return False
    for plan in journal.plans:
        if not plan.after_exists:
            continue
        expected = _published_identity(journal, plan)
        if expected is None:
            return False
        _root, target = target_path(paths, plan)
        if not _path_matches_publication_identity(target, expected):
            return False
    return True


def _capture_target_snapshot(
    paths: ProfileImportPaths,
    plan: InternalFilePlan,
) -> ConfigSnapshot:
    root, target = target_path(paths, plan)
    if not _current_matches(paths, plan, after=False):
        raise ProfileImportStalePreviewError(
            f"{plan.relative_path} changed before profile import publication"
        )
    try:
        snapshot = ConfigSnapshot.capture(target)
    except Exception as exc:
        raise ProfileImportStalePreviewError(
            f"{plan.relative_path} changed while profile import opened it"
        ) from exc
    expected_data = plan.before_content.encode("utf-8") if plan.before_exists else b""
    if (snapshot.identity is not None) != plan.before_exists or snapshot.data != expected_data:
        raise ProfileImportStalePreviewError(
            f"{plan.relative_path} changed while profile import read it"
        )
    # target_path already validates the configured root; retain the local name
    # to make that boundary explicit for future refactors.
    del root
    return snapshot


def _assert_snapshot_current(
    snapshot: ConfigSnapshot,
    plan: InternalFilePlan,
) -> None:
    try:
        snapshot.assert_current()
    except Exception as exc:
        raise ProfileImportStalePreviewError(
            f"{plan.relative_path} changed before profile import publication"
        ) from exc


def _transaction_temporary(target: Path, transaction_id: str) -> Path:
    token = hashlib.sha256(
        f"{transaction_id}\0{target}".encode("utf-8", "surrogatepass")
    ).hexdigest()[:24]
    return target.with_name(f".{target.name}.profile-import-{token}.tmp")


def _transaction_backup(target: Path, transaction_id: str) -> Path:
    token = hashlib.sha256(
        f"{transaction_id}\0{target}".encode("utf-8", "surrogatepass")
    ).hexdigest()[:24]
    return target.with_name(f".{target.name}.profile-import-{token}.before")


def _transaction_after_image(target: Path, transaction_id: str) -> Path:
    token = hashlib.sha256(
        f"{transaction_id}\0{target}".encode("utf-8", "surrogatepass")
    ).hexdigest()[:24]
    return target.with_name(f".{target.name}.profile-import-{token}.after")


def _move_no_replace(source: Path, destination: Path) -> None:
    try:
        native_move_no_replace(source, destination)
        _fsync_directory(destination.parent)
    except Exception as exc:
        raise ProfileImportWriteError(
            f"cannot move profile import file without replacement: {source.name}"
        ) from exc


def _path_matches_image(
    path: Path,
    *,
    exists: bool,
    content: str,
) -> bool:
    try:
        snapshot = ConfigSnapshot.capture(path)
    except Exception:
        return False
    if snapshot.identity is None:
        return not exists
    if not exists:
        return False
    return snapshot.data == content.encode("utf-8")


def _path_matches_snapshot(path: Path, snapshot: ConfigSnapshot) -> bool:
    try:
        current = ConfigSnapshot.capture(path)
    except Exception:
        return False
    if snapshot.identity is None or current.identity is None:
        return snapshot.identity is None and current.identity is None
    return (
        current.identity.metadata_tuple() == snapshot.identity.metadata_tuple()
        and current.data == snapshot.data
    )


def _restore_unexpected_parked_file(parked: Path, target: Path) -> None:
    """Put an unexpectedly moved source back without replacing a later edit."""

    if not os.path.lexists(native_io_path(parked)):
        return
    if os.path.lexists(native_io_path(target)):
        raise ProfileImportWriteError(
            f"profile import target changed while its prior image was parked: {target}"
        )
    _move_no_replace(parked, target)


def _remove_transaction_temporary(path: Path) -> None:
    if not os.path.lexists(native_io_path(path)):
        return
    try:
        value = os.lstat(native_io_path(path))
        if _is_link_or_reparse(value) or not stat.S_ISREG(value.st_mode):
            raise ProfileImportWriteError(
                f"profile import temporary path is unsafe: {path}"
            )
        os.unlink(native_io_path(path))
        _fsync_directory(path.parent)
    except ProfileImportWriteError:
        raise
    except OSError as exc:
        raise ProfileImportWriteError(
            f"cannot remove profile import temporary file: {path}"
        ) from exc


def _cleanup_transaction_temporaries(
    paths: ProfileImportPaths,
    plans: list[InternalFilePlan],
    transaction_id: str,
) -> None:
    for plan in plans:
        _root, target = target_path(paths, plan)
        _remove_transaction_temporary(
            _transaction_temporary(target, transaction_id)
        )


def _remove_owned_image(path: Path, *, exists: bool, content: str) -> None:
    if not os.path.lexists(native_io_path(path)):
        return
    if not _path_matches_image(path, exists=exists, content=content):
        return
    try:
        os.unlink(native_io_path(path))
        _fsync_directory(path.parent)
    except OSError:
        return


def _cleanup_transaction_artifacts(
    paths: ProfileImportPaths,
    plans: list[InternalFilePlan],
    transaction_id: str,
) -> None:
    _cleanup_transaction_temporaries(paths, plans, transaction_id)
    for plan in plans:
        _root, target = target_path(paths, plan)
        _remove_owned_image(
            _transaction_backup(target, transaction_id),
            exists=plan.before_exists,
            content=plan.before_content,
        )
        _remove_owned_image(
            _transaction_after_image(target, transaction_id),
            exists=plan.after_exists,
            content=plan.after_content,
        )


def _rollback_plan(
    paths: ProfileImportPaths,
    plan: InternalFilePlan,
    *,
    transaction_id: str,
    published_identity: PublishedFileIdentity | None = None,
) -> None:
    """Restore one plan with no-replace moves, never overwriting a later edit."""

    _root, target = target_path(paths, plan)
    backup = _transaction_backup(target, transaction_id)
    after_image = _transaction_after_image(target, transaction_id)
    temporary = _transaction_temporary(target, transaction_id)

    if _current_matches(paths, plan, after=False):
        _remove_owned_image(
            backup,
            exists=plan.before_exists,
            content=plan.before_content,
        )
        _remove_owned_image(
            after_image,
            exists=plan.after_exists,
            content=plan.after_content,
        )
        _remove_transaction_temporary(temporary)
        return

    # A process can stop after the before-image is atomically parked but
    # before the candidate is published. The empty canonical name plus our
    # exact backup is a recoverable state, not an external-edit conflict.
    if (
        plan.before_exists
        and not os.path.lexists(native_io_path(target))
        and _path_matches_image(
            backup,
            exists=True,
            content=plan.before_content,
        )
        and (
            _path_matches_image(
                temporary,
                exists=plan.after_exists,
                content=plan.after_content,
            )
            or _path_matches_image(
                after_image,
                exists=plan.after_exists,
                content=plan.after_content,
            )
        )
    ):
        _move_no_replace(backup, target)
        if not _current_matches(paths, plan, after=False):
            raise ProfileImportWriteError(
                f"{plan.relative_path} rollback could not restore its parked image"
            )
        _remove_owned_image(
            after_image,
            exists=plan.after_exists,
            content=plan.after_content,
        )
        _remove_transaction_temporary(temporary)
        return

    # A still-present owned candidate proves that a same-byte canonical file
    # was created by somebody else after we parked the before-image. Hash
    # equality is not ownership and must never authorize removing that file.
    if (
        plan.after_exists
        and os.path.lexists(native_io_path(target))
        and _path_matches_image(
            temporary,
            exists=True,
            content=plan.after_content,
        )
    ):
        raise ProfileImportStalePreviewError(
            f"{plan.relative_path} was recreated before candidate publication"
        )

    if _current_matches(paths, plan, after=True):
        if plan.after_exists:
            if published_identity is not None and not _path_matches_publication_identity(
                target,
                published_identity,
            ):
                raise ProfileImportStalePreviewError(
                    f"{plan.relative_path} identity changed after publication"
                )
            if os.path.lexists(native_io_path(after_image)):
                raise ProfileImportStalePreviewError(
                    f"{plan.relative_path} rollback quarantine is occupied"
                )
            _move_no_replace(target, after_image)
            if not _path_matches_image(
                after_image,
                exists=True,
                content=plan.after_content,
            ):
                _restore_unexpected_parked_file(after_image, target)
                raise ProfileImportStalePreviewError(
                    f"{plan.relative_path} changed while rollback parked it"
                )
    else:
        raise ProfileImportStalePreviewError(
            f"{plan.relative_path} changed before profile import rollback"
        )

    if plan.before_exists:
        if not _path_matches_image(
            backup,
            exists=True,
            content=plan.before_content,
        ):
            raise ProfileImportWriteError(
                f"{plan.relative_path} rollback backup is unavailable"
            )
        if os.path.lexists(native_io_path(target)):
            raise ProfileImportStalePreviewError(
                f"{plan.relative_path} was recreated before rollback"
            )
        _move_no_replace(backup, target)
        if not _current_matches(paths, plan, after=False):
            raise ProfileImportWriteError(
                f"{plan.relative_path} rollback could not verify its prior image"
            )
    elif os.path.lexists(native_io_path(target)):
        raise ProfileImportStalePreviewError(
            f"{plan.relative_path} was recreated before rollback"
        )

    _remove_owned_image(
        after_image,
        exists=plan.after_exists,
        content=plan.after_content,
    )
    _remove_transaction_temporary(temporary)


def _replace_target(
    paths: ProfileImportPaths,
    plan: InternalFilePlan,
    *,
    transaction_id: str = "",
    checkpoint_publication: Callable[[PublishedFileIdentity], None] | None = None,
) -> None:
    root, target = target_path(paths, plan)
    ensure_safe_parent(root, target)
    snapshot = _capture_target_snapshot(paths, plan)
    operation_id = transaction_id or uuid.uuid4().hex
    backup = _transaction_backup(target, operation_id)
    after_image = _transaction_after_image(
        target,
        operation_id,
    )
    if os.path.lexists(native_io_path(backup)) or os.path.lexists(
        native_io_path(after_image)
    ):
        raise ProfileImportWriteError(
            f"profile import transaction artifacts already exist for {target}"
        )
    existing_mode = snapshot.mode if snapshot.identity is not None else 0o600
    temporary = _transaction_temporary(
        target,
        operation_id,
    )
    _remove_transaction_temporary(temporary)
    if not plan.after_exists:
        if snapshot.identity is None:
            _assert_snapshot_current(snapshot, plan)
            return
        _assert_snapshot_current(snapshot, plan)
        _move_no_replace(target, backup)
        if not _path_matches_snapshot(backup, snapshot):
            _restore_unexpected_parked_file(backup, target)
            raise ProfileImportStalePreviewError(
                f"{plan.relative_path} changed while profile import parked it"
            )
        return

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    parked_before_image = False
    try:
        descriptor = os.open(native_io_path(temporary), flags, existing_mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            if snapshot.identity is not None:
                try:
                    shutil.copystat(
                        native_io_path(target),
                        native_io_path(temporary),
                        follow_symlinks=False,
                    )
                    copy_macos_config_metadata(snapshot, handle.fileno())
                except Exception as exc:
                    raise ProfileImportWriteError(
                        f"cannot preserve profile import target metadata: {target}"
                    ) from exc
            chmod_open_file(handle.fileno(), existing_mode)
            handle.write(plan.after_content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            os.chmod(native_io_path(temporary), existing_mode)
        try:
            copy_windows_config_dacl(snapshot, temporary)
        except Exception as exc:
            raise ProfileImportWriteError(
                f"cannot preserve profile import target security metadata: {target}"
            ) from exc
        candidate_snapshot = ConfigSnapshot.capture(temporary)
        if candidate_snapshot.data != plan.after_content.encode("utf-8"):
            raise ProfileImportWriteError(
                f"{plan.relative_path} candidate changed before publication"
            )
        publication_identity = _snapshot_publication_identity(
            plan,
            candidate_snapshot,
        )
        if checkpoint_publication is not None:
            checkpoint_publication(publication_identity)
        # Park the live before-image with an atomic no-replace rename. The
        # candidate is then published only into an empty name, so a concurrent
        # editor can make the operation fail but can never be overwritten.
        ensure_safe_parent(root, target)
        _assert_snapshot_current(snapshot, plan)
        if snapshot.identity is not None:
            _move_no_replace(target, backup)
            parked_before_image = True
            if not _path_matches_snapshot(backup, snapshot):
                _restore_unexpected_parked_file(backup, target)
                raise ProfileImportStalePreviewError(
                    f"{plan.relative_path} changed while profile import parked it"
                )
        _move_no_replace(temporary, target)
        if not _current_matches(paths, plan, after=True):
            raise ProfileImportWriteError(
                f"{plan.relative_path} publication could not be verified"
            )
    except ProfileImportWriteError:
        raise
    except OSError as exc:
        raise ProfileImportWriteError(f"cannot publish profile import target: {target}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not parked_before_image:
            with contextlib.suppress(OSError):
                os.unlink(native_io_path(temporary))


def _snapshot_plans(
    store: ProfileImportStore,
    plans: list[InternalFilePlan],
    *,
    batch_id: str,
    undo: bool,
) -> None:
    for plan in plans:
        store.write_snapshot(
            batch_id,
            target=plan.target.value,
            undo=undo,
            payload={
                "schemaVersion": 1,
                "target": plan.target.value,
                "relativePath": plan.relative_path,
                "exists": plan.before_exists,
                "content": plan.before_content,
                "hash": plan.before_hash,
            },
        )


def execute_transaction(
    *,
    paths: ProfileImportPaths,
    store: ProfileImportStore,
    preview: InternalPreviewRecord,
    receipt: ProfileImportReceipt,
    plans: list[InternalFilePlan],
    operation: Literal["profile-import-apply", "profile-import-undo"],
    now: datetime,
) -> TransactionJournal:
    """Apply all plans or restore every touched before-image on failure."""

    undo = operation == "profile-import-undo"
    assert_plan_baselines(paths, plans)
    _snapshot_plans(store, plans, batch_id=preview.batch_id, undo=undo)
    journal = TransactionJournal(
        operation=operation,
        transaction_id=uuid.uuid4().hex,
        phase="applying",
        preview_id=preview.preview_id,
        receipt_id=receipt.receipt_id,
        batch_id=preview.batch_id,
        plans=plans,
        receipt=receipt,
        updated_at=now,
    )
    store.save_journal(journal, undo=undo)

    try:
        for plan in plans:
            journal.active_target = plan.target
            journal.updated_at = now
            store.save_journal(journal, undo=undo)

            def checkpoint_publication(identity: PublishedFileIdentity) -> None:
                journal.publication_identities = [
                    item
                    for item in journal.publication_identities
                    if item.target != plan.target
                ]
                journal.publication_identities.append(identity)
                journal.updated_at = now
                store.save_journal(journal, undo=undo)

            _replace_target(
                paths,
                plan,
                transaction_id=journal.transaction_id,
                checkpoint_publication=checkpoint_publication,
            )
            journal.completed_targets.append(plan.target)
            journal.active_target = None
            store.save_journal(journal, undo=undo)
        if not _all_published_images_owned(paths, journal):
            raise ProfileImportStalePreviewError(
                "profile import files changed before the batch could be finalized"
            )
        journal.phase = "published"
        store.save_journal(journal, undo=undo)
        store.save_receipt(receipt)
        if not undo or preview.operation == "undo_review":
            preview.status = ImportStatus.APPLIED
            preview.receipt_id = receipt.receipt_id
            preview.applied_at = receipt.undone_at or receipt.applied_at
            store.update_preview(preview)
        journal.phase = "committed"
        store.save_journal(journal, undo=undo)
        with contextlib.suppress(ProfileImportWriteError):
            _cleanup_transaction_artifacts(
                paths,
                plans,
                journal.transaction_id,
            )
        return journal
    except BaseException as exc:
        if journal.phase in {"published", "committed"}:
            raise ProfileImportWriteError(
                "profile import files were published; durable recovery must finalize metadata"
            ) from exc
        journal.phase = "rolling_back"
        store.save_journal(journal, undo=undo)
        rollback_error: BaseException | None = None
        touched = set(journal.completed_targets)
        if journal.active_target is not None:
            touched.add(journal.active_target)
        for plan in reversed(plans):
            if plan.target not in touched:
                continue
            try:
                _rollback_plan(
                    paths,
                    plan,
                    transaction_id=journal.transaction_id,
                    published_identity=_published_identity(journal, plan),
                )
            except BaseException as restore_exc:  # preserve the durable recovery journal
                rollback_error = restore_exc
        journal.phase = "rollback_failed" if rollback_error is not None else "rolled_back"
        journal.updated_at = now
        store.save_journal(journal, undo=undo)
        if rollback_error is not None:
            raise ProfileImportWriteError(
                "profile import failed and automatic rollback requires recovery"
            ) from rollback_error
        if isinstance(exc, ProfileImportStalePreviewError):
            raise
        if isinstance(exc, ProfileImportWriteError):
            raise
        raise ProfileImportWriteError("profile import transaction failed") from exc


def recover_transaction(
    *,
    paths: ProfileImportPaths,
    store: ProfileImportStore,
    journal: TransactionJournal,
    undo: bool,
    now: datetime,
) -> str | None:
    """Recover one interrupted journal without guessing over external edits."""

    if journal.phase == "recovery_conflict":
        _cleanup_transaction_temporaries(paths, journal.plans, journal.transaction_id)
        raise ProfileImportWriteError(
            "profile import recovery conflicts with later file changes"
        )
    if journal.phase == "rolled_back":
        _cleanup_transaction_artifacts(
            paths,
            journal.plans,
            journal.transaction_id,
        )
        return None
    if journal.phase == "committed":
        # The receipt was durably saved before the committed phase. Do not
        # replay this older journal copy: later undo/index metadata may have
        # legitimately advanced the live receipt.
        _cleanup_transaction_artifacts(
            paths,
            journal.plans,
            journal.transaction_id,
        )
        return None
    if journal.phase == "published":
        if not _all_published_images_owned(paths, journal):
            journal.phase = "recovery_conflict"
            journal.updated_at = now
            store.save_journal(journal, undo=undo)
            raise ProfileImportWriteError(
                "profile import recovery found files changed after publication"
            )
        store.save_receipt(journal.receipt)
        if not undo or journal.preview_id != journal.receipt.preview_id:
            preview = store.load_preview(journal.preview_id)
            preview.status = ImportStatus.APPLIED
            preview.receipt_id = journal.receipt.receipt_id
            preview.applied_at = (
                journal.receipt.undone_at or journal.receipt.applied_at
            )
            store.update_preview(preview)
        journal.phase = "committed"
        journal.updated_at = now
        store.save_journal(journal, undo=undo)
        _cleanup_transaction_artifacts(
            paths,
            journal.plans,
            journal.transaction_id,
        )
        return journal.batch_id

    touched = set(journal.completed_targets)
    if journal.active_target is not None:
        touched.add(journal.active_target)
    conflict = False
    for plan in reversed(journal.plans):
        if plan.target not in touched:
            continue
        # A target already at its before image needs no restoration. Restore
        # only our exact after image; never overwrite a later local edit.
        try:
            _rollback_plan(
                paths,
                plan,
                transaction_id=journal.transaction_id,
                published_identity=_published_identity(journal, plan),
            )
        except (ProfileImportStalePreviewError, ProfileImportWriteError):
            conflict = True
    journal.phase = "recovery_conflict" if conflict else "rolled_back"
    journal.updated_at = now
    store.save_journal(journal, undo=undo)
    if not conflict:
        _cleanup_transaction_artifacts(
            paths,
            journal.plans,
            journal.transaction_id,
        )
    if conflict:
        raise ProfileImportWriteError(
            "profile import recovery conflicts with later file changes"
        )
    return journal.batch_id
