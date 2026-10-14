"""Recover one requested, provable historical write into immutable material."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

from opensquilla.application.artifact_workbench import (
    ContentIntegrityError,
    ContentNotFoundError,
)
from opensquilla.attachment_refs import transcript_material_path, write_transcript_material
from opensquilla.chat.legacy_file_snapshot import (
    MAX_LEGACY_REPLAY_BYTES,
    legacy_write_locators,
    legacy_write_succeeded,
)
from opensquilla.content_reader import ContentReadError
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.paths import media_root_from_config, native_io_path


class LegacyFileSnapshotBusyError(RuntimeError):
    """Bounded recovery admission is occupied; the caller can retry later."""


def _verified_write_payload(replay: dict[str, Any], snapshot: dict[str, Any]) -> bytes:
    """Read only a matched tool input, never replay prose or current files."""

    if (
        type(replay.get("version")) is not int or replay.get("version") != 1
        or not isinstance(replay.get("messages"), list)
    ):
        raise ContentNotFoundError("historical file content unavailable")
    reference = snapshot["legacy_ref"]
    tool_id = reference["tool_use_id"]
    calls: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for message in replay["messages"]:
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if not isinstance(block, dict):
                continue
            if (
                message.get("role") == "assistant" and block.get("type") == "tool_use"
                and block.get("id") == tool_id
            ):
                calls.append(block)
            elif (
                message.get("role") == "user" and block.get("type") == "tool_result"
                and block.get("tool_use_id") == tool_id
            ):
                results.append(block)
    if len(calls) != 1 or len(results) != 1 or not legacy_write_succeeded(results[0]):
        raise ContentNotFoundError("historical file content unavailable")
    call = calls[0]
    arguments = call.get("input")
    text = arguments.get("content") if isinstance(arguments, dict) else None
    if (
        call.get("name") != "write_file" or not isinstance(arguments, dict)
        or arguments.get("path") != snapshot["path"]
        or not isinstance(text, str)
    ):
        raise ContentNotFoundError("historical file content unavailable")
    if len(text) != reference["original_chars"]:
        raise ContentIntegrityError("historical file integrity check failed")
    payload = text.encode("utf-8")
    if len(payload) > MAX_LEGACY_REPLAY_BYTES:
        raise ContentNotFoundError("historical file content unavailable")
    if hashlib.sha256(payload).hexdigest() != reference["sha256"]:
        raise ContentIntegrityError("historical file integrity check failed")
    return payload


def _persist_verified_write(
    replay: dict[str, Any], snapshot: dict[str, Any], *, media_root: Path,
    session_id: str, disk_budget_bytes: int | None,
) -> bool:
    payload = _verified_write_payload(replay, snapshot)
    _sha, _path, written = write_transcript_material(
        media_root=media_root, session_id=session_id, payload=payload,
        disk_budget_bytes=disk_budget_bytes,
    )
    return written


def _discard_recovered_material(material: Path) -> None:
    """Undo only this recovery's write after its session generation disappears."""

    material.unlink(missing_ok=True)
    try:
        material.parent.rmdir()
    except OSError:
        # Another recovery may still own a file here; never remove its content.
        pass


class LegacyFileSnapshotRecovery:
    """Per-Gateway concurrency budget and single flight for explicitly opened rows."""

    def __init__(self, config: GatewayConfig, session_manager: Any) -> None:
        self._config = config
        self._storage = get_session_storage(session_manager)
        self._limit = asyncio.Semaphore(2)
        self._pending: dict[tuple[str, str, str, str], asyncio.Task[None]] = {}

    async def recover(
        self, *, session_key: str, message_id: str, tool_use_id: str, sha256: str,
    ) -> None:
        if (
            self._storage is None or not session_key or not message_id or not tool_use_id
            or len(message_id) > 256 or len(tool_use_id) > 256
        ):
            raise ContentNotFoundError("historical file content unavailable")
        key = (session_key, message_id, tool_use_id, sha256)
        task = self._pending.get(key)
        if task is None:
            if len(self._pending) >= 16:
                raise LegacyFileSnapshotBusyError("historical file recovery is busy")
            task = asyncio.create_task(self._recover_one(*key))
            self._pending[key] = task
            task.add_done_callback(lambda _task: self._pending.pop(key, None))
        # A collapsed client's cancellation must not release its slot while a
        # worker is still parsing/hashing/writing, or repeated opens defeat the
        # concurrency budget. Drain this bounded single flight before exiting.
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(task)
            except Exception:
                pass
            raise

    async def _recover_one(
        self, session_key: str, message_id: str, tool_use_id: str, sha256: str,
    ) -> None:
        async with self._limit:
            storage = self._storage
            if storage is None:
                raise ContentNotFoundError("historical file content unavailable")
            session = await storage.get_session(session_key)
            if session is None:
                raise ContentNotFoundError("historical file content unavailable")
            media_root = media_root_from_config(self._config)
            material = native_io_path(transcript_material_path(
                media_root, session.session_id, sha256,
            ))
            try:
                ref = await storage.get_legacy_content_ref(
                    session.session_id, message_id, allow_pending=True,
                )
                entry = await storage.read_legacy_detail_entry(
                    ref, max_bytes=MAX_LEGACY_REPLAY_BYTES,
                )
            except ContentReadError as exc:
                raise ContentNotFoundError("historical file content unavailable") from exc
            if entry is None or entry.role != "assistant":
                raise ContentNotFoundError("historical file content unavailable")
            snapshot = legacy_write_locators(entry.tool_calls, message_id).get(tool_use_id)
            if snapshot is None or snapshot["legacy_ref"]["sha256"] != sha256:
                raise ContentNotFoundError("historical file content unavailable")
            if await asyncio.to_thread(material.is_file):
                return  # The normal attachment route verifies the saved digest.
            replay = await storage.get_canonical_transcript_replay(
                session.session_id, message_id, max_bytes=MAX_LEGACY_REPLAY_BYTES,
            )
            if replay is None:
                raise ContentNotFoundError("historical file content unavailable")
            # A session reset/deletion while waiting for a reader invalidates
            # the request; never recover material into a replacement session.
            current = await storage.get_session(session_key)
            if current is None or (current.session_id, current.epoch) != (
                session.session_id, session.epoch,
            ):
                raise ContentNotFoundError("historical file content unavailable")
            written = await asyncio.to_thread(
                _persist_verified_write, replay, snapshot, media_root=media_root,
                session_id=session.session_id,
                disk_budget_bytes=self._config.attachments.transcript_disk_budget_bytes,
            )
            # Deletion can finish its material cleanup while the worker is
            # hashing or writing. Fence again after it drains, otherwise this
            # read-only request can recreate bytes for a deleted session.
            current = await storage.get_session(session_key)
            if current is None or (current.session_id, current.epoch) != (
                session.session_id, session.epoch,
            ):
                if written and (current is None or current.session_id != session.session_id):
                    await asyncio.to_thread(_discard_recovered_material, material)
                raise ContentNotFoundError("historical file content unavailable")
