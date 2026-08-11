"""Crash-recoverable filesystem transaction primitives for managed Skills."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opensquilla.paths import state_dir
from opensquilla.skills.hub.contracts import (
    DiagnosticPhase,
    DiagnosticSeverity,
    SkillDiagnostic,
)
from opensquilla.skills.hub.lockfile import compute_tree_sha256

_JOURNAL_VERSION = 1
_STAGING_DIRECTORY = ".opensquilla-staging"
_ROLLBACK_DIRECTORY = ".opensquilla-rollback"
_VALID_PHASES = frozenset(
    {"prepared", "old_moved", "new_moved", "lock_written", "committed"}
)
_SAFE_JOURNAL_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TRANSACTION_ID = re.compile(r"^[0-9a-f]{32}$")


def managed_root_identity(managed_dir: Path) -> str:
    """Return a stable, non-sensitive identity for one configured managed root."""

    logical = os.path.normcase(str(managed_dir.expanduser().resolve(strict=False)))
    return hashlib.sha256(logical.encode("utf-8")).hexdigest()[:24]


def default_journal_path(managed_dir: Path) -> Path:
    """Locate the transaction journal in profile state, outside the Skill layer."""

    return state_dir(
        "skill-management",
        f"{managed_root_identity(managed_dir)}.journal.json",
    )


def journal_path_for_state(managed_dir: Path, state_root: Path | None) -> Path:
    """Resolve the journal for a configured profile state root."""

    if state_root is None:
        return default_journal_path(managed_dir)
    return (
        state_root
        / "skill-management"
        / f"{managed_root_identity(managed_dir)}.journal.json"
    )


def staging_root(managed_dir: Path) -> Path:
    return managed_dir / _STAGING_DIRECTORY


def rollback_root(managed_dir: Path) -> Path:
    return managed_dir / _ROLLBACK_DIRECTORY


def path_is_occupied(path: Path) -> bool:
    """Return whether a directory entry exists without following symlinks.

    ``Path.exists()`` reports false for dangling symlinks.  Store mutations must
    still treat those entries as occupied so a broken link cannot be silently
    replaced as though the destination were unused.
    """

    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def fsync_directory(directory: Path) -> None:
    """Persist directory entries on POSIX and best-effort them on Windows."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        if os.name != "nt":
            raise


def remove_transaction_journal(journal_path: Path) -> None:
    """Remove a completed transaction journal and persist its absence."""

    journal_path.unlink(missing_ok=True)
    fsync_directory(journal_path.parent)


def _fsync_file(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        if os.name != "nt":
            raise


def fsync_staging_tree(tree: Path) -> None:
    """Persist a validated staging tree before its prepared journal is written."""

    directories = [tree]
    for path in sorted(tree.rglob("*")):
        info = path.lstat()
        junction_check = getattr(path, "is_junction", None)
        if stat.S_ISLNK(info.st_mode) or (
            callable(junction_check) and bool(junction_check())
        ):
            raise ValueError(f"staging tree contains a symlink or junction: {path}")
        if stat.S_ISREG(info.st_mode):
            _fsync_file(path)
        elif stat.S_ISDIR(info.st_mode):
            directories.append(path)
        else:
            raise ValueError(f"staging tree contains a non-ordinary entry: {path}")

    for directory in sorted(
        directories,
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        fsync_directory(directory)
    # Persist the Skill entry, transaction-id entry, and reserved-root entry.
    fsync_directory(tree.parent)
    fsync_directory(tree.parent.parent)
    fsync_directory(tree.parent.parent.parent)


def _ordinary_directory_info(path: Path, *, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"{label} is missing: {path}") from exc
    junction_check = getattr(path, "is_junction", None)
    if stat.S_ISLNK(info.st_mode) or (
        callable(junction_check) and bool(junction_check())
    ):
        raise ValueError(f"{label} must not be a symlink or junction: {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{label} must be an ordinary directory: {path}")
    return info


def ensure_safe_transaction_roots(managed_dir: Path) -> tuple[Path, Path]:
    """Create and validate the two reserved transaction directories.

    Both directories must remain ordinary direct children of the configured
    managed root and on the same filesystem so transaction renames stay atomic.
    The managed root itself may be a configured symlink; comparisons therefore
    use its resolved location while rejecting symlinks at either reserved child.
    """

    managed_dir.mkdir(parents=True, exist_ok=True)
    try:
        resolved_root = managed_dir.resolve(strict=True)
        root_info = resolved_root.stat()
    except (FileNotFoundError, OSError) as exc:
        raise ValueError(f"managed Skill root is unavailable: {managed_dir}") from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f"managed Skill root is not a directory: {managed_dir}")

    roots = (staging_root(managed_dir), rollback_root(managed_dir))
    for root in roots:
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            # Reserved roots persist across transactions; the checks below
            # verify that an existing path is still an ordinary safe directory.
            pass
        info = _ordinary_directory_info(root, label="reserved transaction root")
        try:
            resolved = root.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise ValueError(f"reserved transaction root is unavailable: {root}") from exc
        if resolved.parent != resolved_root or resolved.name != root.name:
            raise ValueError(
                f"reserved transaction root is not a direct managed-root child: {root}"
            )
        if info.st_dev != root_info.st_dev:
            raise ValueError(
                f"reserved transaction root is on a different filesystem: {root}"
            )
    return roots


def _safe_journal_component(value: str, *, label: str) -> str:
    if (
        not _SAFE_JOURNAL_COMPONENT.fullmatch(value)
        or value in {".", ".."}
        or value.endswith(".")
    ):
        raise ValueError(f"journal contains an invalid {label}")
    return value


def _logical_relative_path(path: Path, root: Path, *, label: str) -> tuple[str, str]:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"journal {label} path has an unsafe logical layout")
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"journal {label} path is outside its reserved root") from exc
    if len(relative.parts) != 2:
        raise ValueError(f"journal {label} path has an unsafe logical layout")
    transaction_id, name = relative.parts
    _safe_journal_component(transaction_id, label="transaction identifier")
    _safe_journal_component(name, label="Skill name")
    return transaction_id, name


def _validate_directory_chain(
    path: Path,
    *,
    root: Path,
    root_device: int,
    label: str,
) -> None:
    """Reject symlink/non-directory ancestors while allowing absent tail paths."""

    relative = path.relative_to(root)
    current = root
    for component in relative.parts:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        junction_check = getattr(current, "is_junction", None)
        if stat.S_ISLNK(info.st_mode) or (
            callable(junction_check) and bool(junction_check())
        ):
            raise ValueError(f"journal {label} path contains a symlink: {current}")
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(
                f"journal {label} path contains a non-directory entry: {current}"
            )
        if info.st_dev != root_device:
            raise ValueError(
                f"journal {label} path crosses to a different filesystem: {current}"
            )


def _remove_tree(path: Path) -> None:
    removed = path_is_occupied(path)
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)
    if removed:
        fsync_directory(path.parent)


def _atomic_write(path: Path, content: bytes) -> None:
    missing_parents: list[Path] = []
    current = path.parent
    while not path_is_occupied(current):
        missing_parents.append(current)
        if current.parent == current:
            break
        current = current.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    # Persist every newly-created journal-directory entry from the outside in.
    # Syncing only the new leaf directory does not make its own parent entry
    # durable across a power loss.
    for created in reversed(missing_parents):
        fsync_directory(created.parent)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@dataclass
class SkillTransactionJournal:
    """Persisted intent needed to restore the prior store after interruption."""

    operation: str
    phase: str
    managed_root: str
    managed_root_id: str
    name: str
    target: str
    staging: str
    rollback: str
    lockfile: str
    old_lock_exists: bool
    old_lock_b64: str
    old_target_exists: bool = False
    old_target_sha256: str = ""

    @classmethod
    def prepare(
        cls,
        *,
        operation: str,
        managed_dir: Path,
        name: str,
        target: Path,
        staging: Path,
        rollback: Path,
        lockfile_path: Path,
    ) -> SkillTransactionJournal:
        ensure_safe_transaction_roots(managed_dir)
        old_lock_exists = lockfile_path.exists()
        old_lock = lockfile_path.read_bytes() if old_lock_exists else b""
        old_target_exists = target.is_dir() and not target.is_symlink()
        old_target_sha256 = (
            compute_tree_sha256(target) if old_target_exists else ""
        )
        journal = cls(
            operation=operation,
            phase="prepared",
            managed_root=str(managed_dir.resolve(strict=False)),
            managed_root_id=managed_root_identity(managed_dir),
            name=name,
            target=str(target.resolve(strict=False)),
            staging=str(staging.resolve(strict=False)),
            rollback=str(rollback.resolve(strict=False)),
            lockfile=str(lockfile_path.resolve(strict=False)),
            old_lock_exists=old_lock_exists,
            old_lock_b64=base64.b64encode(old_lock).decode("ascii"),
            old_target_exists=old_target_exists,
            old_target_sha256=old_target_sha256,
        )
        validate_transaction_journal_paths(
            journal,
            managed_dir=managed_dir,
            lockfile_path=lockfile_path,
        )
        return journal

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": _JOURNAL_VERSION,
            "operation": self.operation,
            "phase": self.phase,
            "managed_root": self.managed_root,
            "managed_root_id": self.managed_root_id,
            "name": self.name,
            "target": self.target,
            "staging": self.staging,
            "rollback": self.rollback,
            "lockfile": self.lockfile,
            "old_lock_exists": self.old_lock_exists,
            "old_lock_b64": self.old_lock_b64,
            "old_target_exists": self.old_target_exists,
            "old_target_sha256": self.old_target_sha256,
        }

    def write(self, path: Path) -> None:
        if self.phase not in _VALID_PHASES:
            raise ValueError(f"invalid Skill transaction phase: {self.phase}")
        encoded = (
            json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        _atomic_write(path, encoded)

    def advance(self, phase: str, path: Path) -> None:
        if phase not in _VALID_PHASES:
            raise ValueError(f"invalid Skill transaction phase: {phase}")
        self.phase = phase
        self.write(path)

    @classmethod
    def load(cls, path: Path) -> SkillTransactionJournal | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        junction_check = getattr(path, "is_junction", None)
        if stat.S_ISLNK(info.st_mode) or (
            callable(junction_check) and bool(junction_check())
        ):
            raise ValueError("Skill transaction journal must not be a symlink or junction")
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Skill transaction journal must be an ordinary file")
        data = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or type(data.get("version")) is not int
            or data.get("version") != _JOURNAL_VERSION
        ):
            raise ValueError("unsupported Skill transaction journal")
        string_fields = (
            "operation",
            "phase",
            "managed_root",
            "managed_root_id",
            "name",
            "target",
            "staging",
            "rollback",
            "lockfile",
            "old_lock_b64",
            "old_target_sha256",
        )
        invalid_string = next(
            (field for field in string_fields if not isinstance(data.get(field), str)),
            None,
        )
        if invalid_string is not None:
            raise ValueError(
                f"Skill transaction journal field {invalid_string!r} must be a string"
            )
        for field in ("old_lock_exists", "old_target_exists"):
            if type(data.get(field)) is not bool:
                raise ValueError(
                    f"Skill transaction journal field {field!r} must be a boolean"
                )
        phase = data["phase"]
        if phase not in _VALID_PHASES:
            raise ValueError(f"invalid Skill transaction phase: {phase}")
        return cls(
            operation=data["operation"],
            phase=phase,
            managed_root=data["managed_root"],
            managed_root_id=data["managed_root_id"],
            name=data["name"],
            target=data["target"],
            staging=data["staging"],
            rollback=data["rollback"],
            lockfile=data["lockfile"],
            old_lock_exists=data["old_lock_exists"],
            old_lock_b64=data["old_lock_b64"],
            old_target_exists=data["old_target_exists"],
            old_target_sha256=data["old_target_sha256"],
        )


def _recovery_diagnostic(
    code: str,
    message: str,
    *,
    blocking: bool,
    details: dict[str, Any] | None = None,
    hint: str | None = None,
) -> SkillDiagnostic:
    return SkillDiagnostic(
        code=code,
        severity=DiagnosticSeverity.ERROR if blocking else DiagnosticSeverity.WARNING,
        phase=DiagnosticPhase.STORE,
        message=message,
        blocking=blocking,
        hint=hint
        or (
            "Keep the journal and managed Skill directories intact, then run "
            "`opensquilla skills doctor --json`."
            if blocking
            else "No action is required; the previous installation was restored."
        ),
        details=details or {},
    )


def _logical_transaction_journal_paths(
    journal: SkillTransactionJournal,
    *,
    managed_dir: Path,
    lockfile_path: Path,
) -> tuple[Path, Path, Path, str, str]:
    """Validate persisted identities and path layout without mutating the store."""

    expected_root = managed_dir.resolve(strict=False)
    if (
        journal.managed_root_id != managed_root_identity(managed_dir)
        or Path(journal.managed_root) != expected_root
    ):
        raise ValueError("journal belongs to a different managed root")
    if (
        not Path(journal.lockfile).is_absolute()
        or ".." in Path(journal.lockfile).parts
        or Path(journal.lockfile) != lockfile_path.resolve(strict=False)
    ):
        raise ValueError("journal belongs to a different lockfile")
    if journal.operation not in {"install", "update", "uninstall"}:
        raise ValueError("journal contains an unsupported operation")

    name = _safe_journal_component(journal.name, label="Skill name")
    target = Path(journal.target)
    staging = Path(journal.staging)
    rollback = Path(journal.rollback)
    if (
        not target.is_absolute()
        or ".." in target.parts
        or target != expected_root / name
    ):
        raise ValueError("journal target is not the named direct managed-root child")

    # Validate the persisted logical shape before creating any missing reserved
    # roots.  A journal for another store must not cause directories to appear
    # in the configured store merely by being inspected.
    staging_transaction, staging_name = _logical_relative_path(
        staging,
        expected_root / _STAGING_DIRECTORY,
        label="staging",
    )
    rollback_transaction, rollback_name = _logical_relative_path(
        rollback,
        expected_root / _ROLLBACK_DIRECTORY,
        label="rollback",
    )
    if staging_name != name or rollback_name != name:
        raise ValueError("journal transaction paths do not match the Skill name")
    if staging_transaction != rollback_transaction:
        raise ValueError("journal staging and rollback transaction identifiers differ")

    return target, staging, rollback, staging_transaction, rollback_transaction


def validate_transaction_journal_paths(
    journal: SkillTransactionJournal,
    *,
    managed_dir: Path,
    lockfile_path: Path,
) -> tuple[Path, Path, Path]:
    target, staging, rollback, staging_transaction, rollback_transaction = (
        _logical_transaction_journal_paths(
            journal,
            managed_dir=managed_dir,
            lockfile_path=lockfile_path,
        )
    )
    expected_root = managed_dir.resolve(strict=False)
    name = journal.name

    safe_staging, safe_rollback = ensure_safe_transaction_roots(managed_dir)
    resolved_root = managed_dir.resolve(strict=True)
    if resolved_root != expected_root:
        raise ValueError("managed root changed while validating the transaction journal")
    resolved_staging_root = safe_staging.resolve(strict=True)
    resolved_rollback_root = safe_rollback.resolve(strict=True)
    resolved_staging_transaction, resolved_staging_name = _logical_relative_path(
        staging,
        resolved_staging_root,
        label="staging",
    )
    resolved_rollback_transaction, resolved_rollback_name = _logical_relative_path(
        rollback,
        resolved_rollback_root,
        label="rollback",
    )
    if (
        resolved_staging_transaction != staging_transaction
        or resolved_rollback_transaction != rollback_transaction
        or resolved_staging_name != name
        or resolved_rollback_name != name
    ):
        raise ValueError("journal transaction paths changed during root validation")

    root_device = resolved_root.stat().st_dev
    _validate_directory_chain(
        target,
        root=resolved_root,
        root_device=root_device,
        label="target",
    )
    _validate_directory_chain(
        staging,
        root=resolved_staging_root,
        root_device=root_device,
        label="staging",
    )
    _validate_directory_chain(
        rollback,
        root=resolved_rollback_root,
        root_device=root_device,
        label="rollback",
    )
    return target, staging, rollback


def cleanup_empty_transaction_directories(
    journal: SkillTransactionJournal,
    *,
    managed_dir: Path,
    lockfile_path: Path,
) -> tuple[Path, ...]:
    """Remove this transaction's empty staging and rollback directories only.

    The leaf paths and their shared transaction identifier are validated before
    cleanup.  ``rmdir`` is deliberately used instead of recursive deletion so
    unexpected files or directories are preserved for inspection.
    """

    _, staging, rollback = validate_transaction_journal_paths(
        journal,
        managed_dir=managed_dir,
        lockfile_path=lockfile_path,
    )
    retained: list[Path] = []
    for transaction_dir in (staging.parent, rollback.parent):
        try:
            transaction_dir.rmdir()
            fsync_directory(transaction_dir.parent)
        except FileNotFoundError:
            continue
        except OSError as remove_error:
            # Revalidate after the failed mutation so a symlink/ancestor swap
            # cannot be mistaken for an ordinary non-empty reservation.
            validate_transaction_journal_paths(
                journal,
                managed_dir=managed_dir,
                lockfile_path=lockfile_path,
            )
            try:
                next(transaction_dir.iterdir())
            except FileNotFoundError:
                continue
            except StopIteration:
                raise remove_error
            retained.append(transaction_dir)
    return tuple(retained)


def _restore_lockfile(journal: SkillTransactionJournal, lockfile_path: Path) -> None:
    if journal.old_lock_exists:
        try:
            content = base64.b64decode(journal.old_lock_b64, validate=True)
        except ValueError as exc:
            raise ValueError("journal contains invalid prior lockfile bytes") from exc
        _atomic_write(lockfile_path, content)
    else:
        removed = path_is_occupied(lockfile_path)
        lockfile_path.unlink(missing_ok=True)
        if removed:
            fsync_directory(lockfile_path.parent)


def _validate_removable_reservation_tree(
    path: Path,
    *,
    root_device: int,
    label: str,
) -> None:
    info = _ordinary_directory_info(path, label=label)
    if info.st_dev != root_device:
        raise ValueError(f"{label} is on a different filesystem: {path}")
    for descendant in sorted(path.rglob("*")):
        child_info = descendant.lstat()
        junction_check = getattr(descendant, "is_junction", None)
        if stat.S_ISLNK(child_info.st_mode) or (
            callable(junction_check) and bool(junction_check())
        ):
            raise ValueError(f"{label} contains a symlink or junction: {descendant}")
        if not (stat.S_ISREG(child_info.st_mode) or stat.S_ISDIR(child_info.st_mode)):
            raise ValueError(f"{label} contains a non-ordinary entry: {descendant}")
        if child_info.st_dev != root_device:
            raise ValueError(f"{label} crosses to a different filesystem: {descendant}")


def cleanup_staging_transaction_reservation(
    *,
    managed_dir: Path,
    transaction_id: str,
    report_recovered: bool = False,
) -> list[SkillDiagnostic]:
    """Safely remove one pre-journal transaction reservation.

    Only UUID-shaped reservations created by the current publisher are eligible.
    Staging trees must contain ordinary files/directories and the matching rollback
    reservation must be empty, so cleanup can never discard a previous Skill tree.
    """

    if not _TRANSACTION_ID.fullmatch(transaction_id):
        return [
            _recovery_diagnostic(
                "TRANSACTION_CLEANUP_PENDING",
                "Ignored an invalid pre-journal transaction reservation",
                blocking=False,
                details={"transactionId": transaction_id},
                hint="Inspect the reserved transaction directory before removing it.",
            )
        ]

    stage = staging_root(managed_dir) / transaction_id
    rollback = rollback_root(managed_dir) / transaction_id
    if not path_is_occupied(stage) and not path_is_occupied(rollback):
        return []

    try:
        safe_staging, safe_rollback = ensure_safe_transaction_roots(managed_dir)
        root_device = managed_dir.resolve(strict=True).stat().st_dev
        if path_is_occupied(stage):
            _validate_removable_reservation_tree(
                stage,
                root_device=root_device,
                label="pre-journal staging reservation",
            )
        if path_is_occupied(rollback):
            _validate_removable_reservation_tree(
                rollback,
                root_device=root_device,
                label="pre-journal rollback reservation",
            )
            try:
                next(rollback.iterdir())
            except StopIteration:
                pass
            else:
                return [
                    _recovery_diagnostic(
                        "RECOVERY_REQUIRED",
                        (
                            "A Skill rollback reservation contains data but its "
                            "transaction journal is missing"
                        ),
                        blocking=True,
                        details={
                            "transactionId": transaction_id,
                            "staging": str(stage),
                            "rollback": str(rollback),
                        },
                        hint=(
                            "Keep the retained reservation intact and inspect whether "
                            "it contains the previous installed Skill before recovering "
                            "the managed store."
                        ),
                    )
                ]

        if path_is_occupied(stage):
            shutil.rmtree(stage)
            fsync_directory(safe_staging)
        if path_is_occupied(rollback):
            rollback.rmdir()
            fsync_directory(safe_rollback)
        fsync_directory(managed_dir.resolve(strict=True))
    except Exception as exc:
        return [
            _recovery_diagnostic(
                "TRANSACTION_CLEANUP_PENDING",
                f"Pre-journal transaction cleanup is pending: {exc}",
                blocking=False,
                details={
                    "transactionId": transaction_id,
                    "staging": str(stage),
                    "rollback": str(rollback),
                },
                hint="Inspect the retained reservation; unexpected content was not deleted.",
            )
        ]

    if not report_recovered:
        return []
    return [
        _recovery_diagnostic(
            "ORPHAN_STAGING_RECOVERED",
            "Removed a pre-journal Skill staging reservation left by an interrupted writer",
            blocking=False,
            details={"transactionId": transaction_id},
        )
    ]


def cleanup_orphan_staging_reservations(
    *,
    managed_dir: Path,
) -> list[SkillDiagnostic]:
    """Sweep crash leftovers before a Gateway loader or offline writer starts."""

    stage_root = staging_root(managed_dir)
    rollback_directory = rollback_root(managed_dir)
    if not path_is_occupied(stage_root) and not path_is_occupied(rollback_directory):
        return []
    try:
        safe_staging, safe_rollback = ensure_safe_transaction_roots(managed_dir)
        children = {
            child.name
            for root in (safe_staging, safe_rollback)
            for child in root.iterdir()
        }
    except Exception as exc:
        return [
            _recovery_diagnostic(
                "RECOVERY_REQUIRED",
                f"Could not inspect reserved Skill transaction directories: {exc}",
                blocking=True,
            )
        ]

    diagnostics: list[SkillDiagnostic] = []
    for transaction_id in sorted(children):
        diagnostics.extend(
            cleanup_staging_transaction_reservation(
                managed_dir=managed_dir,
                transaction_id=transaction_id,
                report_recovered=True,
            )
        )
    return diagnostics


def guard_retained_recovery_journal(
    diagnostics: list[SkillDiagnostic],
    *,
    journal_path: Path,
) -> list[SkillDiagnostic]:
    """Promote a retained recovery journal to a mutation-blocking diagnostic."""

    guarded = list(diagnostics)
    if path_is_occupied(journal_path) and not any(item.blocking for item in guarded):
        guarded.append(
            _recovery_diagnostic(
                "RECOVERY_REQUIRED",
                "A prior Skill transaction journal is retained for cleanup",
                blocking=True,
                details={"journal": str(journal_path)},
                hint=(
                    "Inspect the retained transaction reservation and restart OpenSquilla "
                    "before changing installed Skills."
                ),
            )
        )
    return guarded


def inspect_pending_skill_transaction(
    *,
    managed_dir: Path,
    lockfile_path: Path,
    journal_path: Path,
) -> list[SkillDiagnostic]:
    """Read and validate a pending journal without creating or changing any path."""

    if not path_is_occupied(journal_path):
        return []
    try:
        journal = SkillTransactionJournal.load(journal_path)
        if journal is None:  # pragma: no cover - guarded by lstat above
            return []
        _logical_transaction_journal_paths(
            journal,
            managed_dir=managed_dir,
            lockfile_path=lockfile_path,
        )
    except Exception as exc:
        return [
            _recovery_diagnostic(
                "RECOVERY_REQUIRED",
                f"Skill transaction journal is invalid: {exc}",
                blocking=True,
                details={"journal": str(journal_path)},
            )
        ]

    code = (
        "TRANSACTION_CLEANUP_PENDING"
        if journal.phase == "committed"
        else "TRANSACTION_PENDING"
    )
    message = (
        f"Committed Skill {journal.operation} cleanup is pending for {journal.name!r}"
        if journal.phase == "committed"
        else (
            f"Interrupted Skill {journal.operation} for {journal.name!r} "
            f"is pending recovery at phase {journal.phase!r}"
        )
    )
    return [
        _recovery_diagnostic(
            code,
            message,
            blocking=True,
            details={
                "journal": str(journal_path),
                "name": journal.name,
                "operation": journal.operation,
                "phase": journal.phase,
            },
            hint=(
                "Keep the journal and reserved directories intact, then restart OpenSquilla "
                "to run recovery before loading Skills."
            ),
        )
    ]


def recover_pending_skill_transaction(
    *,
    managed_dir: Path,
    lockfile_path: Path,
    journal_path: Path | None = None,
    sweep_orphan_staging: bool = False,
) -> list[SkillDiagnostic]:
    """Restore an interrupted mutation before the production loader scans.

    Recovery is intentionally filesystem-only: it never imports a Skill, runs a
    third-party script, performs network I/O, or calls an LLM.
    """

    selected_journal = journal_path or default_journal_path(managed_dir)
    try:
        journal = SkillTransactionJournal.load(selected_journal)
        if journal is None:
            return (
                cleanup_orphan_staging_reservations(managed_dir=managed_dir)
                if sweep_orphan_staging
                else []
            )
        target, staging, rollback = validate_transaction_journal_paths(
            journal,
            managed_dir=managed_dir,
            lockfile_path=lockfile_path,
        )

        if journal.phase == "committed":
            _remove_tree(staging)
            _remove_tree(rollback)
        else:
            # Restore the prior lock first. If filesystem recovery is then
            # interrupted, the retained journal and rollback tree make the next
            # attempt idempotent; the production loader has not scanned yet.
            _restore_lockfile(journal, lockfile_path)
            if journal.operation == "install":
                if journal.old_target_exists:
                    raise OSError("install journal unexpectedly records an old target")
                _remove_tree(target)
                _remove_tree(rollback)
            else:
                if not journal.old_target_exists:
                    raise OSError("update/uninstall journal has no prior target")
                if rollback.exists():
                    # Handles both rename→phase crash windows: target may be
                    # absent (old move) or contain the unpublished new tree.
                    _remove_tree(target)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(rollback, target)
                    fsync_directory(rollback.parent)
                    fsync_directory(target.parent)
                    fsync_directory(rollback.parent.parent)
                elif not target.exists():
                    raise OSError("both target and rollback directory are missing")
                elif (
                    journal.old_target_sha256
                    and compute_tree_sha256(target) != journal.old_target_sha256
                ):
                    raise OSError("rollback directory is missing and target is not the old tree")
            _remove_tree(staging)

        retained = cleanup_empty_transaction_directories(
            journal,
            managed_dir=managed_dir,
            lockfile_path=lockfile_path,
        )
        recovered = [
            _recovery_diagnostic(
                "TRANSACTION_RECOVERED",
                f"Recovered interrupted Skill {journal.operation} for {journal.name!r}",
                blocking=False,
                details={"phase": journal.phase, "name": journal.name},
            )
        ]
        if retained:
            recovered.append(
                _recovery_diagnostic(
                    "TRANSACTION_CLEANUP_PENDING",
                    "Recovered the Skill transaction, but its reservation is not empty",
                    blocking=False,
                    details={"paths": [str(path) for path in retained]},
                    hint=(
                        "Inspect the retained transaction directory; unrelated content was "
                        "not deleted."
                    ),
                )
            )
            return recovered
        remove_transaction_journal(selected_journal)
        return recovered
    except Exception as exc:
        return [
            _recovery_diagnostic(
                "RECOVERY_REQUIRED",
                f"Skill store recovery could not complete: {exc}",
                blocking=True,
                details={"journal": str(selected_journal)},
            )
        ]


__all__ = [
    "SkillTransactionJournal",
    "cleanup_empty_transaction_directories",
    "cleanup_orphan_staging_reservations",
    "cleanup_staging_transaction_reservation",
    "default_journal_path",
    "ensure_safe_transaction_roots",
    "fsync_directory",
    "fsync_staging_tree",
    "guard_retained_recovery_journal",
    "inspect_pending_skill_transaction",
    "journal_path_for_state",
    "managed_root_identity",
    "path_is_occupied",
    "recover_pending_skill_transaction",
    "remove_transaction_journal",
    "rollback_root",
    "staging_root",
    "validate_transaction_journal_paths",
]
