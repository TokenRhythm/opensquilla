"""Independent, launch-scoped cleanup for Desktop-owned POSIX task processes."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opensquilla.process_tree import _PersistedOwnerRecord, _TaskProcessScope

PROTOCOL = "opensquilla-desktop-process-cleanup-v1"
_NONCE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_MAX_MANIFEST_BYTES = 16 * 1024


@dataclass(frozen=True)
class _DesktopProcessScope:
    state_dir: Path
    launch_digest: str
    pid: int


_active_scope: _DesktopProcessScope | None = None


def _launch_digest(nonce: str) -> str:
    if _NONCE.fullmatch(nonce) is None:
        raise ValueError("Invalid Desktop process ownership authority")
    return hashlib.sha256(f"{PROTOCOL}\0{nonce}".encode("ascii")).hexdigest()


def _signed(payload: dict[str, Any], nonce: str) -> dict[str, Any]:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    proof = hmac.new(nonce.encode("ascii"), encoded, hashlib.sha256).hexdigest()
    return {**payload, "proof": proof}


def _manifest_path(ownership_dir: Path, digest: str) -> Path:
    return ownership_dir / f"process-launch-{digest}.json"


def _read_manifest(path: Path, nonce: str) -> dict[str, Any] | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_size > _MAX_MANIFEST_BYTES
        ):
            raise ValueError("Unsafe Desktop process ownership manifest")
        raw = stream.read(_MAX_MANIFEST_BYTES + 1)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Invalid Desktop process ownership manifest")
    payload = {key: item for key, item in value.items() if key != "proof"}
    proof = value.get("proof")
    if (
        not isinstance(proof, str)
        or not hmac.compare_digest(proof, _signed(payload, nonce)["proof"])
        or payload.get("protocol") != PROTOCOL
        or payload.get("launch_digest") != _launch_digest(nonce)
        or not isinstance(payload.get("closed"), bool)
        or not (
            payload.get("state_dir") is None
            or isinstance(payload.get("state_dir"), str)
            and Path(payload["state_dir"]).is_absolute()
        )
    ):
        raise ValueError("Desktop process ownership manifest could not be verified")
    return payload


def _write_manifest(path: Path, payload: dict[str, Any], nonce: str, *, initial: bool) -> None:
    import secrets

    temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(_signed(payload, nonce), stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        if initial:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


@contextlib.contextmanager
def _launch_lock(ownership_dir: Path, digest: str, deadline: float) -> Iterator[None]:
    import fcntl

    from opensquilla.process_tree import _prepare_private_directory, _prepare_private_file

    _prepare_private_directory(ownership_dir)
    path = ownership_dir / f"process-launch-{digest}.lock"
    _prepare_private_file(path)
    fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Desktop process ownership lock timed out")
                time.sleep(min(0.01, remaining))
        yield
    finally:
        os.close(fd)


def prepare_launch_registry(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS desktop_process_launches "
        "(launch_digest TEXT PRIMARY KEY, closed INTEGER NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS desktop_process_launch_owners "
        "(owner_id TEXT PRIMARY KEY, launch_digest TEXT NOT NULL, "
        "record_json TEXT NOT NULL, status TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_desktop_process_launch_owners_launch "
        "ON desktop_process_launch_owners(launch_digest)"
    )


def configure_desktop_process_cleanup(
    *, ownership_dir: Path, instance_nonce: str, state_dir: str | Path | None,
) -> None:
    """Bind launches before service startup without exposing the Desktop nonce to tools."""
    global _active_scope
    if os.name != "posix":
        return
    from opensquilla.process_tree import _connect_owner_database, _owner_database_path

    digest = _launch_digest(instance_nonce)
    root = Path(state_dir).expanduser().resolve() if state_dir is not None else (
        ownership_dir / "process-state" / digest
    ).resolve()
    scope = _DesktopProcessScope(root, digest, os.getpid())
    if _active_scope is not None and _active_scope != scope:
        raise RuntimeError("Desktop process ownership is already active")
    path = _manifest_path(ownership_dir, digest)
    with _launch_lock(ownership_dir, digest, time.monotonic() + 5.0):
        existing = _read_manifest(path, instance_nonce)
        if existing is not None:
            raise RuntimeError("Desktop process launch has already been initialized or closed")
        _write_manifest(path, {
            "protocol": PROTOCOL, "launch_digest": digest,
            "state_dir": str(root), "closed": False,
        }, instance_nonce, initial=True)
        try:
            with _connect_owner_database(_owner_database_path(root)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT OR IGNORE INTO desktop_process_launches VALUES (?, 0)", (digest,),
                )
                row = connection.execute(
                    "SELECT closed FROM desktop_process_launches WHERE launch_digest = ?",
                    (digest,),
                ).fetchone()
                if row is None or row["closed"] != 0:
                    raise RuntimeError("Desktop process launch is closing")
                connection.execute("COMMIT")
        except BaseException:
            # The manifest is only useful once the durable admission row exists.
            # Remove it when registry initialization fails so a retry with the
            # same launch authority is not mistaken for an active instance.
            with contextlib.suppress(OSError):
                path.unlink()
            raise
        _active_scope = scope


def release_desktop_process_cleanup(instance_nonce: str) -> None:
    global _active_scope
    if _active_scope is not None and _active_scope.launch_digest == _launch_digest(instance_nonce):
        _active_scope = None


def abort_desktop_process_cleanup(ownership_dir: Path, instance_nonce: str) -> None:
    """Roll back a launch that failed before Desktop ownership was acquired.

    ``configure_desktop_process_cleanup`` runs before the gateway ownership
    record is written so that no tool can launch outside the Desktop fence.
    If that later record write fails, the launch never became observable and
    its admission row and manifest must not strand a permanently open scope.
    """
    global _active_scope
    digest = _launch_digest(instance_nonce)
    deadline = time.monotonic() + 5.0
    from opensquilla.process_tree import _connect_owner_database, _owner_database_path

    try:
        with _launch_lock(ownership_dir, digest, deadline):
            path = _manifest_path(ownership_dir, digest)
            payload = _read_manifest(path, instance_nonce)
            if payload is None:
                return
            if payload.get("closed"):
                return
            state_dir_raw = payload.get("state_dir")
            if not isinstance(state_dir_raw, str):
                raise RuntimeError("Desktop process ownership state is invalid")
            database = _owner_database_path(state_dir_raw)
            with _connect_owner_database(
                database, timeout_seconds=max(0.001, deadline - time.monotonic()),
            ) as connection:
                connection.execute("BEGIN IMMEDIATE")
                owner_count = connection.execute(
                    "SELECT COUNT(*) FROM desktop_process_launch_owners "
                    "WHERE launch_digest = ?", (digest,),
                ).fetchone()[0]
                if owner_count:
                    connection.execute("ROLLBACK")
                    raise RuntimeError("Desktop process launch already owns a process")
                connection.execute(
                    "DELETE FROM desktop_process_launches WHERE launch_digest = ?",
                    (digest,),
                )
                connection.execute("COMMIT")
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
    finally:
        if _active_scope is not None and _active_scope.launch_digest == digest:
            _active_scope = None


def desktop_process_scope(scope: _TaskProcessScope | None) -> _TaskProcessScope | None:
    active = _active_scope
    if active is None or active.pid != os.getpid():
        return scope
    from opensquilla.process_tree import ProcessTreeOwnershipError, _owner_digest, _TaskProcessScope

    if scope is not None:
        if scope.state_dir.expanduser().resolve() != active.state_dir:
            raise ProcessTreeOwnershipError(
                "Desktop task process state location changed during launch"
            )
        return replace(scope, desktop_launch_digest=active.launch_digest)
    return _TaskProcessScope(
        state_dir=active.state_dir,
        session_digest=_owner_digest("desktop-session", active.launch_digest),
        task_digest=_owner_digest("desktop-task", active.launch_digest),
        desktop_launch_digest=active.launch_digest,
    )


def register_launch_owner(
    connection: sqlite3.Connection, scope: _TaskProcessScope, record: _PersistedOwnerRecord,
) -> None:
    """Run inside the owner insertion transaction, before any target is released."""
    active = _active_scope
    digest = scope.desktop_launch_digest
    if digest is None and active is not None and active.pid == os.getpid():
        digest = active.launch_digest
    if digest is None:
        return
    from opensquilla.process_tree import ProcessTreeOwnershipError

    if (
        active is not None and active.launch_digest == digest
        and scope.state_dir.expanduser().resolve() != active.state_dir
    ):
        raise ProcessTreeOwnershipError("Desktop process registry does not match its launch")
    row = connection.execute(
        "SELECT closed FROM desktop_process_launches WHERE launch_digest = ?", (digest,),
    ).fetchone()
    if row is None or row["closed"] != 0:
        raise ProcessTreeOwnershipError("Desktop process launch is closing")
    connection.execute(
        "INSERT INTO desktop_process_launch_owners VALUES (?, ?, ?, 'pending')",
        (record.owner_id, digest, json.dumps(asdict(record), sort_keys=True)),
    )


def settle_launch_owner(connection: sqlite3.Connection, owner_id: str, complete: bool) -> None:
    # Once uncertainty is recorded, a racing deletion cannot turn it into success.
    connection.execute(
        "UPDATE desktop_process_launch_owners SET status = ? "
        "WHERE owner_id = ? AND status = 'pending'",
        ("complete" if complete else "incomplete", owner_id),
    )


def _close_launch(ownership_dir: Path, nonce: str, deadline: float) -> Path | None:
    digest = _launch_digest(nonce)
    with _launch_lock(ownership_dir, digest, deadline):
        path = _manifest_path(ownership_dir, digest)
        payload = _read_manifest(path, nonce)
        initial = payload is None
        if payload is None:
            payload = {
                "protocol": PROTOCOL, "launch_digest": digest,
                "state_dir": None, "closed": True,
            }
        else:
            payload["closed"] = True
        _write_manifest(path, payload, nonce, initial=initial)
        state_dir = payload["state_dir"]
        return Path(state_dir) if state_dir is not None else None


def cleanup_desktop_processes(ownership_dir: Path, nonce: str, *, deadline: float) -> bool:
    """Close one launch and prove its registered trees have finished."""
    if os.name != "posix":
        return False
    from opensquilla import process_tree

    state_dir = _close_launch(ownership_dir, nonce, deadline)
    if state_dir is None:
        return True  # The tombstone prevents an in-flight initializer from starting work.
    database = process_tree._owner_database_path(state_dir)
    digest = _launch_digest(nonce)
    with process_tree._connect_owner_database(
        database, timeout_seconds=max(0.001, deadline - time.monotonic()),
    ) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT closed FROM desktop_process_launches WHERE launch_digest = ?", (digest,),
        ).fetchone()
        if row is None:
            raise ValueError("Desktop process launch registry is missing")
        connection.execute(
            "UPDATE desktop_process_launches SET closed = 1 WHERE launch_digest = ?", (digest,),
        )
        rows = connection.execute(
            "SELECT owner_id, record_json, status FROM desktop_process_launch_owners "
            "WHERE launch_digest = ?", (digest,),
        ).fetchall()
        connection.execute("COMMIT")
    records = []
    for row in rows:
        if row["status"] == "complete":
            continue
        if row["status"] != "pending":
            return False
        record = process_tree._PersistedOwnerRecord(**json.loads(row["record_json"]))
        if not process_tree._valid_owner_record(record) or row["owner_id"] != record.owner_id:
            return False
        records.append(process_tree._PersistedOwnerRef(database, record))

    # Each exact controller can be serviced independently. Threads are daemonized so the
    # helper's deadline never acquires an unbounded executor shutdown wait.
    outcomes: list[bool | None] = [None] * len(records)
    def terminate(index: int, reference: process_tree._PersistedOwnerRef) -> None:
        claim = None
        try:
            while time.monotonic() < deadline:
                with process_tree._connect_owner_database(
                    database, timeout_seconds=max(0.001, deadline - time.monotonic()),
                ) as connection:
                    row = connection.execute(
                        "SELECT status FROM desktop_process_launch_owners WHERE owner_id = ?",
                        (reference.record.owner_id,),
                    ).fetchone()
                if row is None or row["status"] == "incomplete":
                    outcomes[index] = False
                    return
                if row["status"] == "complete":
                    outcomes[index] = True
                    return
                claim = process_tree._claim_owner_record(reference)
                if claim is not None:
                    outcomes[index] = process_tree._terminate_persisted_posix_owner_sync(reference)
                    return
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
            outcomes[index] = False
        except Exception:
            outcomes[index] = False
        finally:
            if claim is not None:
                process_tree._release_owner_claim(claim)

    for index, reference in enumerate(records):
        threading.Thread(target=terminate, args=(index, reference), daemon=True).start()
    while any(value is None for value in outcomes):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))
    with process_tree._connect_owner_database(
        database, timeout_seconds=max(0.001, deadline - time.monotonic()),
    ) as connection:
        connection.execute("BEGIN IMMEDIATE")
        remaining = connection.execute(
            "SELECT COUNT(*) FROM desktop_process_launch_owners "
            "WHERE launch_digest = ? AND status != 'complete'", (digest,),
        ).fetchone()[0]
        if remaining == 0:
            # Keep the closed admission fence, but retire successful per-tool
            # evidence once the entire launch has been confirmed empty.
            connection.execute(
                "DELETE FROM desktop_process_launch_owners WHERE launch_digest = ?", (digest,),
            )
        connection.execute("COMMIT")
    return remaining == 0 and time.monotonic() <= deadline


def cleanup_main() -> int:
    complete = False
    try:
        raw = sys.stdin.buffer.read(_MAX_MANIFEST_BYTES + 1)
        request = json.loads(raw)
        if not isinstance(request, dict) or set(request) != {
            "ownership_dir", "instance_nonce", "timeout_ms",
        }:
            raise ValueError("Invalid Desktop cleanup request")
        timeout = request["timeout_ms"]
        if (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 5000
        ):
            raise ValueError("Invalid Desktop cleanup timeout")
        root = request["ownership_dir"]
        nonce = request["instance_nonce"]
        if not isinstance(root, str) or not Path(root).is_absolute() or not isinstance(nonce, str):
            raise ValueError("Invalid Desktop cleanup authority")
        complete = cleanup_desktop_processes(
            Path(root), nonce, deadline=time.monotonic() + timeout / 1000,
        )
    except Exception:
        complete = False
    print(
        json.dumps({"protocol": PROTOCOL, "complete": complete}, separators=(",", ":")),
        flush=True,
    )
    return 0 if complete else 1
