"""Inner-stage process execution for the Linux sandbox helper."""

from __future__ import annotations

import asyncio
import base64
import os
import signal
import time
from pathlib import Path
from typing import Any

from opensquilla.sandbox.backend.linux_payload import HelperPayload
from opensquilla.sandbox.backend.linux_preexec import process_preexec_from_policy

OUTPUT_BYTE_CAP = 1_048_576
TERMINATE_GRACE_S = 2.0


async def run_process_payload(payload: HelperPayload) -> dict[str, Any]:
    if payload.process is None:
        raise ValueError("process payload is required")
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *payload.process.argv,
        cwd=Path(payload.cwd),
        env={**os.environ, **payload.env},
        stdin=asyncio.subprocess.PIPE if payload.process.stdin_base64 else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=process_preexec_from_policy(payload.policy),
        start_new_session=True,
    )
    timed_out = False
    stdin = (
        base64.b64decode(payload.process.stdin_base64)
        if payload.process.stdin_base64
        else None
    )
    try:
        stdout_raw, stderr_raw = await asyncio.wait_for(
            proc.communicate(input=stdin),
            timeout=float(payload.policy.get("wallTimeoutS", 60.0)),
        )
    except TimeoutError:
        timed_out = True
        stdout_raw, stderr_raw = await _terminate_process_group(proc)
    elapsed = time.monotonic() - started
    stdout, truncated_stdout = _decode_capped(stdout_raw)
    stderr, truncated_stderr = _decode_capped(stderr_raw)
    if timed_out:
        stderr = stderr or "linux sandbox process timed out"
    return {
        "returncode": 124 if timed_out else proc.returncode if proc.returncode is not None else -1,
        "stdout": stdout,
        "stderr": stderr,
        "wallTimeS": elapsed,
        "timedOut": timed_out,
        "truncatedStdout": truncated_stdout,
        "truncatedStderr": truncated_stderr,
    }


def _decode_capped(raw: bytes | None) -> tuple[str, bool]:
    if not raw:
        return "", False
    if len(raw) <= OUTPUT_BYTE_CAP:
        return raw.decode("utf-8", errors="replace"), False
    return raw[:OUTPUT_BYTE_CAP].decode("utf-8", errors="replace"), True


async def _terminate_process_group(proc: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=TERMINATE_GRACE_S)
    except TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await proc.wait()
        except ProcessLookupError:
            pass
    return b"", b""
