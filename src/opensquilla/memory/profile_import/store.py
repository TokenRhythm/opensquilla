"""Durable private storage for profile import drafts, previews, and receipts."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from opensquilla.memory.profile_import.errors import (
    ProfileImportNotFoundError,
    ProfileImportWriteError,
)
from opensquilla.memory.profile_import.files import (
    _is_link_or_reparse,
    assert_safe_target,
    ensure_safe_parent,
    restrict_private_path,
)
from opensquilla.memory.profile_import.models import (
    InternalPreviewRecord,
    ProfileImportJobRecord,
    ProfileImportPaths,
    ProfileImportReceipt,
    TransactionJournal,
)
from opensquilla.profile_import_io import native_io_path

_OPAQUE_ID_LENGTH = 32


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(native_io_path(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes, *, root: Path, mode: int = 0o600) -> None:
    ensure_safe_parent(root, path, private=True)
    temporary = path.with_name(
        f".{path.name}.profile-import-{uuid.uuid4().hex}.tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(native_io_path(temporary), flags, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        restrict_private_path(temporary, directory=False)
        if os.path.lexists(native_io_path(path)):
            value = os.lstat(native_io_path(path))
            if _is_link_or_reparse(value) or not stat.S_ISREG(value.st_mode):
                raise ProfileImportWriteError(
                    f"private import record is not a regular file: {path}"
                )
        os.replace(native_io_path(temporary), native_io_path(path))
        _fsync_directory(path.parent)
    except OSError as exc:
        raise ProfileImportWriteError(
            f"cannot persist private profile import state: {path}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(OSError):
            os.unlink(native_io_path(temporary))


def _write_json(path: Path, payload: dict[str, Any], *, root: Path) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    _atomic_write(path, encoded, root=root)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = os.lstat(native_io_path(path))
        if _is_link_or_reparse(value) or not stat.S_ISREG(value.st_mode):
            raise ProfileImportWriteError(f"private import record is not a regular file: {path}")
        raw = Path(native_io_path(path)).read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except FileNotFoundError as exc:
        raise ProfileImportNotFoundError("profile import preview is no longer available") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileImportWriteError(f"cannot read private profile import state: {path}") from exc
    if not isinstance(parsed, dict):
        raise ProfileImportWriteError(f"private profile import record is invalid: {path}")
    return parsed


def _key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def _validate_opaque_id(value: str) -> str:
    if len(value) != _OPAQUE_ID_LENGTH or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ProfileImportNotFoundError("profile import preview is no longer available")
    return value


def _unlink_private_regular(path: Path, *, root: Path) -> bool:
    assert_safe_target(root, path)
    if not os.path.lexists(native_io_path(path)):
        return False
    try:
        value = os.lstat(native_io_path(path))
        if _is_link_or_reparse(value) or not stat.S_ISREG(value.st_mode):
            raise ProfileImportWriteError(
                f"private import record is not a regular file: {path}"
            )
        os.unlink(native_io_path(path))
        _fsync_directory(path.parent)
        return True
    except ProfileImportWriteError:
        raise
    except OSError as exc:
        raise ProfileImportWriteError(
            f"cannot remove private profile import state: {path}"
        ) from exc


def _remove_private_tree(path: Path, *, root: Path) -> int:
    """Remove one validated private batch tree without following links."""

    assert_safe_target(root, path)
    if not os.path.lexists(native_io_path(path)):
        return 0
    try:
        value = os.lstat(native_io_path(path))
        if _is_link_or_reparse(value) or not stat.S_ISDIR(value.st_mode):
            raise ProfileImportWriteError(
                f"private import batch is not a real directory: {path}"
            )
        removed = 0
        with os.scandir(native_io_path(path)) as entries:
            children = [path / entry.name for entry in entries]
        for child in children:
            child_value = os.lstat(native_io_path(child))
            if _is_link_or_reparse(child_value):
                raise ProfileImportWriteError(
                    f"private import batch contains a link or reparse point: {child}"
                )
            if stat.S_ISDIR(child_value.st_mode):
                removed += _remove_private_tree(child, root=root)
            elif stat.S_ISREG(child_value.st_mode):
                os.unlink(native_io_path(child))
                removed += 1
            else:
                raise ProfileImportWriteError(
                    f"private import batch contains an unsafe object: {child}"
                )
        os.rmdir(native_io_path(path))
        _fsync_directory(path.parent)
        return removed
    except ProfileImportWriteError:
        raise
    except OSError as exc:
        raise ProfileImportWriteError(
            f"cannot remove private profile import batch: {path}"
        ) from exc


def harden_profile_import_private_state(state_dir: Path) -> int:
    """Migrate an existing private import tree to owner-only permissions."""

    state_root = state_dir.expanduser().resolve(strict=False)
    root = state_root / "profile-imports"
    if not os.path.lexists(native_io_path(root)):
        return 0
    assert_safe_target(state_root, root)
    hardened = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            value = os.lstat(native_io_path(directory))
            if _is_link_or_reparse(value) or not stat.S_ISDIR(value.st_mode):
                raise ProfileImportWriteError(
                    f"private profile import state contains an unsafe directory: {directory}"
                )
            restrict_private_path(directory, directory=True)
            hardened += 1
            with os.scandir(native_io_path(directory)) as entries:
                children = [directory / entry.name for entry in entries]
            for child in children:
                child_value = os.lstat(native_io_path(child))
                if _is_link_or_reparse(child_value):
                    raise ProfileImportWriteError(
                        f"private profile import state contains a link: {child}"
                    )
                if stat.S_ISDIR(child_value.st_mode):
                    pending.append(child)
                elif stat.S_ISREG(child_value.st_mode):
                    restrict_private_path(child, directory=False)
                    hardened += 1
                else:
                    raise ProfileImportWriteError(
                        f"private profile import state contains an unsafe object: {child}"
                    )
        except ProfileImportWriteError:
            raise
        except OSError as exc:
            raise ProfileImportWriteError(
                f"cannot harden private profile import state: {directory}"
            ) from exc
    return hardened


def cleanup_expired_profile_import_raw(
    state_dir: Path,
    now: datetime,
) -> int:
    """Sweep old raw inputs and abandoned atomic drafts without following links."""

    root = state_dir.expanduser().resolve(strict=False) / "profile-imports"
    if not os.path.lexists(native_io_path(root)):
        return 0
    try:
        root_value = os.lstat(native_io_path(root))
        if _is_link_or_reparse(root_value) or not stat.S_ISDIR(root_value.st_mode):
            raise ProfileImportWriteError(
                "profile import state root is not a real directory"
            )
    except ProfileImportWriteError:
        raise
    except OSError as exc:
        raise ProfileImportWriteError(
            "cannot inspect private profile import state"
        ) from exc

    cutoff = now - timedelta(hours=24)
    cleaned = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(native_io_path(directory)) as entries:
                children = [directory / entry.name for entry in entries]
        except OSError:
            continue
        for candidate in children:
            try:
                value = os.lstat(native_io_path(candidate))
            except OSError:
                continue
            if _is_link_or_reparse(value):
                continue
            if stat.S_ISDIR(value.st_mode):
                pending.append(candidate)
                continue
            if not stat.S_ISREG(value.st_mode):
                continue
            relative = candidate.relative_to(root)
            is_batch_raw = (
                candidate.name == "raw.txt"
                and len(relative.parts) == 3
                and not relative.parts[0].startswith("_")
                and len(relative.parts[1]) == _OPAQUE_ID_LENGTH
                and all(
                    character in "0123456789abcdef"
                    for character in relative.parts[1]
                )
            )
            is_atomic_draft = (
                candidate.name.startswith(".")
                and ".profile-import-" in candidate.name
                and candidate.name.endswith(".tmp")
            )
            if not is_batch_raw and not is_atomic_draft:
                continue
            try:
                modified = datetime.fromtimestamp(value.st_mtime, tz=now.tzinfo)
            except OSError:
                continue
            if modified > cutoff:
                continue
            try:
                os.unlink(native_io_path(candidate))
                _fsync_directory(candidate.parent)
                cleaned += 1
            except OSError:
                continue
    return cleaned


def _locator_path(state_dir: Path, kind: str, opaque_id: str) -> Path:
    _validate_opaque_id(opaque_id)
    if kind not in {"jobs", "previews", "receipts"}:
        raise ValueError("unsupported profile import locator kind")
    return state_dir / "profile-imports" / "_locators" / kind / f"{opaque_id}.json"


def lookup_preview_agent(state_dir: Path, preview_id: str) -> str | None:
    return _lookup_agent(state_dir, "previews", preview_id)


def lookup_receipt_agent(state_dir: Path, receipt_id: str) -> str | None:
    return _lookup_agent(state_dir, "receipts", receipt_id)


def lookup_job_agent(state_dir: Path, job_id: str) -> str | None:
    return _lookup_agent(state_dir, "jobs", job_id)


def _lookup_agent(state_dir: Path, kind: str, opaque_id: str) -> str | None:
    path = _locator_path(state_dir.expanduser().absolute(), kind, opaque_id)
    if not os.path.lexists(native_io_path(path)):
        return None
    try:
        payload = _read_json(path)
    except ProfileImportNotFoundError:
        return None
    agent_id = payload.get("agentId")
    object_id = payload.get("id")
    if not isinstance(agent_id, str) or object_id != opaque_id:
        return None
    return agent_id


class ProfileImportStore:
    """Agent-scoped view over the shared private import state root."""

    def __init__(self, paths: ProfileImportPaths) -> None:
        self.paths = paths
        self.root = paths.agent_import_state_dir

    def batch_dir(self, batch_id: str) -> Path:
        _validate_opaque_id(batch_id)
        return self.root / batch_id

    def raw_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "raw.txt"

    def preview_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "preview.json"

    def job_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "job.json"

    def receipt_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "receipt.json"

    def journal_path(self, batch_id: str, *, undo: bool = False) -> Path:
        return self.batch_dir(batch_id) / ("undo-journal.json" if undo else "journal.json")

    def snapshot_dir(self, batch_id: str, *, undo: bool = False) -> Path:
        name = "undo-snapshots" if undo else "snapshots"
        return self.batch_dir(batch_id) / name

    def _index_path(self, kind: str, key: str) -> Path:
        if kind not in {"requests", "reuse", "job_requests", "job_reuse"}:
            raise ValueError("unsupported profile import index")
        return self.root / "_indexes" / kind / f"{_key(key)}.json"

    def write_raw(self, batch_id: str, raw_text: str) -> None:
        path = self.raw_path(batch_id)
        assert_safe_target(self.paths.state_dir, path)
        _atomic_write(path, raw_text.encode("utf-8"), root=self.paths.state_dir)

    def read_raw(self, batch_id: str) -> str:
        path = self.raw_path(batch_id)
        assert_safe_target(self.paths.state_dir, path)
        try:
            value = os.lstat(native_io_path(path))
            if _is_link_or_reparse(value) or not stat.S_ISREG(value.st_mode):
                raise ProfileImportWriteError(
                    f"private import raw input is not a regular file: {path}"
                )
            return Path(native_io_path(path)).read_text(encoding="utf-8")
        except ProfileImportWriteError:
            raise
        except (OSError, UnicodeDecodeError) as exc:
            raise ProfileImportWriteError(
                "cannot read private profile import raw input"
            ) from exc

    def save_job(self, record: ProfileImportJobRecord) -> None:
        self._assert_agent(record.agent_id)
        _write_json(
            self.job_path(record.batch_id),
            record.model_dump(mode="json", by_alias=True),
            root=self.paths.state_dir,
        )
        self._write_locator("jobs", record.job_id, record.batch_id)
        self._write_job_index("job_requests", record.client_request_id, record)
        self._write_job_index("job_reuse", record.reuse_key, record)

    def update_job(self, record: ProfileImportJobRecord) -> None:
        self._assert_agent(record.agent_id)
        _write_json(
            self.job_path(record.batch_id),
            record.model_dump(mode="json", by_alias=True),
            root=self.paths.state_dir,
        )

    def load_job(self, job_id: str) -> ProfileImportJobRecord:
        locator = self._read_locator("jobs", job_id)
        batch_id = str(locator["batchId"])
        try:
            return ProfileImportJobRecord.model_validate(
                _read_json(self.job_path(batch_id))
            )
        except ValidationError as exc:
            raise ProfileImportWriteError("stored profile import job is invalid") from exc

    def find_job_by_request(self, request_key: str) -> ProfileImportJobRecord | None:
        return self._find_job_index("job_requests", request_key)

    def find_job_by_reuse_key(self, reuse_key: str) -> ProfileImportJobRecord | None:
        return self._find_job_index("job_reuse", reuse_key)

    def latest_draft_job(self) -> ProfileImportJobRecord | None:
        if not self.root.exists():
            return None
        terminal = {"applied", "discarded"}
        latest: ProfileImportJobRecord | None = None
        for path in self.root.glob("*/job.json"):
            try:
                record = ProfileImportJobRecord.model_validate(_read_json(path))
            except (ValidationError, ProfileImportWriteError, ProfileImportNotFoundError):
                continue
            if record.agent_id != self.paths.agent_id or record.status.value in terminal:
                continue
            if latest is None or record.updated_at > latest.updated_at:
                latest = record
        return latest

    def find_job_by_preview(self, preview_id: str) -> ProfileImportJobRecord | None:
        if not self.root.exists():
            return None
        for path in self.root.glob("*/job.json"):
            try:
                record = ProfileImportJobRecord.model_validate(_read_json(path))
            except (ValidationError, ProfileImportWriteError, ProfileImportNotFoundError):
                continue
            if (
                record.agent_id == self.paths.agent_id
                and record.preview_id == preview_id
            ):
                return record
        return None

    def iter_jobs(self) -> list[ProfileImportJobRecord]:
        if not self.root.exists():
            return []
        jobs: list[ProfileImportJobRecord] = []
        for path in self.root.glob("*/job.json"):
            try:
                record = ProfileImportJobRecord.model_validate(_read_json(path))
            except (ValidationError, ProfileImportWriteError, ProfileImportNotFoundError):
                continue
            if record.agent_id == self.paths.agent_id:
                jobs.append(record)
        return jobs

    def purge_job(self, record: ProfileImportJobRecord) -> int:
        self._assert_agent(record.agent_id)
        owned = (
            (_locator_path(self.paths.state_dir, "jobs", record.job_id), "id", record.job_id),
            (
                self._index_path("job_requests", record.client_request_id),
                "jobId",
                record.job_id,
            ),
            (
                self._index_path("job_reuse", record.reuse_key),
                "jobId",
                record.job_id,
            ),
        )
        removed = 0
        for path, field, expected in owned:
            if not os.path.lexists(native_io_path(path)):
                continue
            try:
                payload = _read_json(path)
            except ProfileImportNotFoundError:
                continue
            if payload.get(field) == expected:
                removed += int(_unlink_private_regular(path, root=self.paths.state_dir))
        if os.path.lexists(native_io_path(self.preview_path(record.batch_id))):
            preview = InternalPreviewRecord.model_validate(
                _read_json(self.preview_path(record.batch_id))
            )
            removed += self.purge_preview(preview)
        else:
            removed += _remove_private_tree(
                self.batch_dir(record.batch_id),
                root=self.paths.state_dir,
            )
        return removed

    def delete_raw(self, batch_id: str) -> None:
        _unlink_private_regular(
            self.raw_path(batch_id),
            root=self.paths.state_dir,
        )

    def purge_preview(self, record: InternalPreviewRecord) -> int:
        """Forget a non-applied draft, its indexes, and private candidates."""

        self._assert_agent(record.agent_id)
        if record.status.value == "applied":
            raise ProfileImportWriteError("an applied profile import cannot be purged")
        if os.path.lexists(native_io_path(self.receipt_path(record.batch_id))):
            raise ProfileImportWriteError(
                "a profile import draft with a durable receipt cannot be purged"
            )
        owned_records = (
            (
                _locator_path(self.paths.state_dir, "previews", record.preview_id),
                "id",
                record.preview_id,
            ),
            (
                self._index_path("requests", record.client_request_id),
                "previewId",
                record.preview_id,
            ),
            (
                self._index_path("reuse", record.reuse_key),
                "previewId",
                record.preview_id,
            ),
        )
        removed = 0
        for path, field, expected in owned_records:
            if not os.path.lexists(native_io_path(path)):
                continue
            try:
                payload = _read_json(path)
            except ProfileImportNotFoundError:
                continue
            if payload.get(field) != expected:
                continue
            removed += int(
                _unlink_private_regular(path, root=self.paths.state_dir)
            )
        removed += _remove_private_tree(
            self.batch_dir(record.batch_id),
            root=self.paths.state_dir,
        )
        return removed

    def save_preview(self, record: InternalPreviewRecord) -> None:
        self._assert_agent(record.agent_id)
        path = self.preview_path(record.batch_id)
        _write_json(
            path,
            record.model_dump(mode="json", by_alias=True),
            root=self.paths.state_dir,
        )
        self._write_locator("previews", record.preview_id, record.batch_id)
        self._write_index("requests", record.client_request_id, record)
        self._write_index("reuse", record.reuse_key, record)

    def update_preview(self, record: InternalPreviewRecord) -> None:
        self._assert_agent(record.agent_id)
        _write_json(
            self.preview_path(record.batch_id),
            record.model_dump(mode="json", by_alias=True),
            root=self.paths.state_dir,
        )

    def load_preview(self, preview_id: str) -> InternalPreviewRecord:
        locator = self._read_locator("previews", preview_id)
        batch_id = str(locator["batchId"])
        try:
            return InternalPreviewRecord.model_validate(_read_json(self.preview_path(batch_id)))
        except ValidationError as exc:
            raise ProfileImportWriteError("stored profile import preview is invalid") from exc

    def find_preview_by_request(self, client_request_id: str) -> InternalPreviewRecord | None:
        return self._find_index("requests", client_request_id)

    def find_preview_by_reuse_key(self, reuse_key: str) -> InternalPreviewRecord | None:
        return self._find_index("reuse", reuse_key)

    def save_receipt(self, receipt: ProfileImportReceipt) -> None:
        self._assert_agent(receipt.agent_id)
        _write_json(
            self.receipt_path(receipt.batch_id),
            receipt.model_dump(mode="json", by_alias=True),
            root=self.paths.state_dir,
        )
        self._write_locator("receipts", receipt.receipt_id, receipt.batch_id)

    def load_receipt(self, receipt_id: str) -> ProfileImportReceipt:
        locator = self._read_locator("receipts", receipt_id)
        batch_id = str(locator["batchId"])
        try:
            return ProfileImportReceipt.model_validate(_read_json(self.receipt_path(batch_id)))
        except ValidationError as exc:
            raise ProfileImportWriteError("stored profile import receipt is invalid") from exc

    def load_receipt_by_batch(self, batch_id: str) -> ProfileImportReceipt | None:
        path = self.receipt_path(batch_id)
        if not os.path.lexists(native_io_path(path)):
            return None
        try:
            receipt = ProfileImportReceipt.model_validate(_read_json(path))
        except ValidationError as exc:
            raise ProfileImportWriteError(
                "stored profile import receipt is invalid"
            ) from exc
        self._assert_agent(receipt.agent_id)
        return receipt

    def latest_receipt(self) -> ProfileImportReceipt | None:
        if not self.root.exists():
            return None
        latest: ProfileImportReceipt | None = None
        try:
            candidates = list(self.root.glob("*/receipt.json"))
        except OSError as exc:
            raise ProfileImportWriteError("cannot enumerate profile import receipts") from exc
        for path in candidates:
            if path.parent.name.startswith("_"):
                continue
            try:
                receipt = ProfileImportReceipt.model_validate(_read_json(path))
            except (ValidationError, ProfileImportWriteError, ProfileImportNotFoundError):
                continue
            if receipt.agent_id != self.paths.agent_id:
                continue
            if latest is None or receipt.applied_at > latest.applied_at:
                latest = receipt
        return latest

    def find_applied_receipt_by_raw_hash(self, raw_hash: str) -> ProfileImportReceipt | None:
        if not self.root.exists():
            return None
        matches: list[ProfileImportReceipt] = []
        for path in self.root.glob("*/receipt.json"):
            try:
                receipt = ProfileImportReceipt.model_validate(_read_json(path))
            except (ValidationError, ProfileImportWriteError, ProfileImportNotFoundError):
                continue
            if (
                receipt.agent_id == self.paths.agent_id
                and receipt.status == "applied"
                and receipt.raw_hash == raw_hash
            ):
                matches.append(receipt)
        return max(matches, key=lambda item: item.applied_at) if matches else None

    def save_journal(self, journal: TransactionJournal, *, undo: bool = False) -> None:
        _write_json(
            self.journal_path(journal.batch_id, undo=undo),
            journal.model_dump(mode="json", by_alias=True),
            root=self.paths.state_dir,
        )

    def load_journal(self, batch_id: str, *, undo: bool = False) -> TransactionJournal | None:
        path = self.journal_path(batch_id, undo=undo)
        if not os.path.lexists(native_io_path(path)):
            return None
        try:
            return TransactionJournal.model_validate(_read_json(path))
        except ValidationError as exc:
            raise ProfileImportWriteError(
                "stored profile import transaction journal is invalid"
            ) from exc

    def iter_journals(self) -> list[tuple[TransactionJournal, bool]]:
        if not self.root.exists():
            return []
        result: list[tuple[TransactionJournal, bool]] = []
        for undo, name in ((False, "journal.json"), (True, "undo-journal.json")):
            try:
                candidates = list(self.root.glob(f"*/{name}"))
            except OSError as exc:
                raise ProfileImportWriteError("cannot enumerate profile import journals") from exc
            for path in candidates:
                try:
                    journal = TransactionJournal.model_validate(_read_json(path))
                except ValidationError as exc:
                    raise ProfileImportWriteError(
                        f"profile import journal is invalid: {path}"
                    ) from exc
                result.append((journal, undo))
        return result

    def write_snapshot(
        self,
        batch_id: str,
        *,
        target: str,
        payload: dict[str, Any],
        undo: bool = False,
    ) -> None:
        directory = self.snapshot_dir(batch_id, undo=undo)
        path = directory / f"{target.lower()}.json"
        _write_json(path, payload, root=self.paths.state_dir)

    def cleanup_expired_raw(self, now: datetime) -> int:
        if not self.root.exists():
            return 0
        cleaned = 0
        for job in self.iter_jobs():
            if job.status.value in {"applied", "discarded"} or job.expires_at > now:
                continue
            if self.load_receipt_by_batch(job.batch_id) is not None:
                continue
            active_journal = any(
                journal is not None and journal.phase != "rolled_back"
                for journal in (
                    self.load_journal(job.batch_id, undo=False),
                    self.load_journal(job.batch_id, undo=True),
                )
            )
            if not active_journal:
                cleaned += self.purge_job(job)
        try:
            candidates = list(self.root.glob("*/preview.json"))
        except OSError as exc:
            raise ProfileImportWriteError("cannot enumerate profile import drafts") from exc
        for path in candidates:
            try:
                record = InternalPreviewRecord.model_validate(_read_json(path))
            except (ValidationError, ProfileImportWriteError, ProfileImportNotFoundError):
                continue
            if (
                record.status.value not in {"preview", "discarded"}
                or (
                    record.status.value == "preview"
                    and record.expires_at > now
                )
            ):
                continue
            receipt_path = self.receipt_path(record.batch_id)
            if os.path.lexists(native_io_path(receipt_path)):
                continue
            active_journal = False
            for undo in (False, True):
                journal = self.load_journal(record.batch_id, undo=undo)
                if journal is not None and journal.phase != "rolled_back":
                    active_journal = True
                    break
            if active_journal:
                continue
            cleaned += self.purge_preview(record)
        cleaned += cleanup_expired_profile_import_raw(
            self.paths.state_dir,
            now,
        )
        return cleaned

    def _write_locator(self, kind: str, opaque_id: str, batch_id: str) -> None:
        path = _locator_path(self.paths.state_dir, kind, opaque_id)
        _write_json(
            path,
            {
                "schemaVersion": 1,
                "id": opaque_id,
                "agentId": self.paths.agent_id,
                "batchId": batch_id,
            },
            root=self.paths.state_dir,
        )

    def _read_locator(self, kind: str, opaque_id: str) -> dict[str, Any]:
        path = _locator_path(self.paths.state_dir, kind, opaque_id)
        payload = _read_json(path)
        if payload.get("id") != opaque_id or payload.get("agentId") != self.paths.agent_id:
            raise ProfileImportNotFoundError("profile import preview is no longer available")
        batch_id = payload.get("batchId")
        if not isinstance(batch_id, str):
            raise ProfileImportWriteError("stored profile import locator is invalid")
        _validate_opaque_id(batch_id)
        return payload

    def _write_index(
        self,
        kind: str,
        key: str,
        record: InternalPreviewRecord,
    ) -> None:
        _write_json(
            self._index_path(kind, key),
            {
                "schemaVersion": 1,
                "keyHash": _key(key),
                "previewId": record.preview_id,
                "batchId": record.batch_id,
                "agentId": record.agent_id,
            },
            root=self.paths.state_dir,
        )

    def _write_job_index(
        self,
        kind: str,
        key: str,
        record: ProfileImportJobRecord,
    ) -> None:
        _write_json(
            self._index_path(kind, key),
            {
                "schemaVersion": 1,
                "keyHash": _key(key),
                "jobId": record.job_id,
                "batchId": record.batch_id,
                "agentId": record.agent_id,
            },
            root=self.paths.state_dir,
        )

    def _find_job_index(self, kind: str, key: str) -> ProfileImportJobRecord | None:
        path = self._index_path(kind, key)
        if not os.path.lexists(native_io_path(path)):
            return None
        payload = _read_json(path)
        if (
            payload.get("keyHash") != _key(key)
            or payload.get("agentId") != self.paths.agent_id
        ):
            raise ProfileImportWriteError("stored profile import job index is invalid")
        job_id = payload.get("jobId")
        if not isinstance(job_id, str):
            raise ProfileImportWriteError("stored profile import job index is invalid")
        try:
            record = self.load_job(job_id)
        except ProfileImportNotFoundError:
            return None
        expected = (
            record.client_request_id
            if kind == "job_requests"
            else record.reuse_key
        )
        return record if expected == key else None

    def _find_index(self, kind: str, key: str) -> InternalPreviewRecord | None:
        path = self._index_path(kind, key)
        if not os.path.lexists(native_io_path(path)):
            return None
        try:
            payload = _read_json(path)
            if payload.get("keyHash") != _key(key) or payload.get("agentId") != self.paths.agent_id:
                return None
            preview_id = payload.get("previewId")
            if not isinstance(preview_id, str):
                return None
            return self.load_preview(preview_id)
        except (ProfileImportNotFoundError, ProfileImportWriteError):
            return None

    def _assert_agent(self, agent_id: str) -> None:
        if agent_id != self.paths.agent_id:
            raise ProfileImportWriteError("profile import record belongs to another agent")
