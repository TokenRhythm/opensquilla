"""Private filesystem replies remain lossless while process logs stay bounded."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.sandbox import filesystem_worker
from opensquilla.sandbox.backend import seatbelt, windows_default
from opensquilla.sandbox.operation_runtime import SandboxOperation, SandboxOperationResult
from opensquilla.sandbox.types import (
    NetworkMode,
    ResourceLimits,
    SandboxPolicy,
    SandboxRequest,
    SecurityLevel,
)


class _Stream:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def read(self, size: int) -> bytes:
        chunk, self.payload = self.payload[:size], self.payload[size:]
        return chunk


class _Process:
    returncode = 0
    stdin = None

    def __init__(self, stdout: bytes, stderr: bytes) -> None:
        self.stdout = _Stream(stdout)
        self.stderr = _Stream(stderr)
        self._output = (stdout, stderr)

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return self._output

    async def wait(self) -> None:
        return None


@pytest.mark.parametrize("backend_name", ["seatbelt", "windows"])
async def test_large_applied_diff_survives_private_worker_transport(
    tmp_path: Path, monkeypatch, backend_name: str,
) -> None:
    before = "before-" + "a" * 600_000 + "\n"
    after = "after-" + "b" * 600_000 + "\n"
    path = tmp_path / "large.txt"
    path.write_text(before, encoding="utf-8")
    worker_result = filesystem_worker._edit_text({
        "path": str(path), "oldText": before, "newText": after,
    })
    assert path.read_text(encoding="utf-8") == after
    worker_stdout = json.dumps(worker_result).encode("utf-8")
    assert len(worker_stdout) > seatbelt._OUTPUT_BYTE_CAP
    stderr = b"worker diagnostic\n" * 80_000
    request = SandboxRequest(
        argv=("worker",), cwd=tmp_path, action_kind="fs.worker.edit_text",
        policy=SandboxPolicy(
            level=SecurityLevel.STANDARD, network=NetworkMode.NONE,
            mounts=(), tmp_writable=False,
            workspace_rw=True, limits=ResourceLimits(), env_allowlist=(),
            require_approval=False,
        ),
    )
    operation = SandboxOperation.filesystem(
        kind="edit_text", workspace=tmp_path, run_mode="safe", path=path,
        paths=(path,), old_text=before, new_text=after,
    )

    async def spawn(*args, **kwargs):
        return _Process(worker_stdout, stderr)

    if backend_name == "seatbelt":
        backend = seatbelt.SeatbeltBackend()
        monkeypatch.setattr(backend, "available", lambda: True)
        monkeypatch.setattr(
            seatbelt, "_filesystem_operation_launch", lambda _operation: SimpleNamespace(
                request=request, private_transport=seatbelt._SeatbeltPrivateTransport(),
            ),
        )
        monkeypatch.setattr(seatbelt, "_validate_request", lambda _request: None)
        monkeypatch.setattr(seatbelt, "_render_seatbelt_profile", lambda *args, **kwargs: "")
        monkeypatch.setattr(seatbelt, "build_seatbelt_argv", lambda *args, **kwargs: ("worker",))
        monkeypatch.setattr(seatbelt, "create_owned_subprocess_exec", spawn)
    else:
        backend = windows_default.WindowsDefaultBackend()
        monkeypatch.setattr(windows_default, "_support_ready", lambda: True)
        monkeypatch.setattr(windows_default, "_filesystem_operation_request", lambda _op: request)
        monkeypatch.setattr(windows_default, "_payload_for_request", lambda *args, **kwargs: {
            "helperNonce": "test-private-reply",
        })
        monkeypatch.setattr(windows_default, "ensure_cache_dirs", lambda _path: None)
        monkeypatch.setattr(windows_default, "create_owned_subprocess_exec", spawn)

    result = await backend.run_operation(operation)
    assert result.metadata["file_snapshot"] == worker_result["file_snapshot"]
    assert result.message == worker_result["message"]

    # The same output on the public process path remains a bounded log. Public
    # action_kind alone must not opt a request into the private JSON transport.
    public_result = await backend.run(request)
    assert public_result.truncated_stdout is True
    assert public_result.truncated_stderr is True
    assert len(public_result.stdout.encode("utf-8")) == seatbelt._OUTPUT_BYTE_CAP
    assert len(public_result.stderr.encode("utf-8")) == seatbelt._OUTPUT_BYTE_CAP


async def test_private_filesystem_stream_uses_separate_finite_stdout_budget() -> None:
    payload = b"x" * (seatbelt._OUTPUT_BYTE_CAP + 50_000)
    stdout, stderr, truncated_stdout, truncated_stderr = await seatbelt._communicate_capped(
        _Process(payload, payload), stdout_byte_cap=seatbelt.MAX_FILESYSTEM_REPLY_BYTES,
    )
    assert stdout == payload
    assert truncated_stdout is False
    assert len(stderr) == seatbelt._OUTPUT_BYTE_CAP
    assert truncated_stderr is True


async def test_private_filesystem_stream_drains_but_bounds_retained_bytes() -> None:
    process = _Process(b"x" * 4096, b"diagnostic")
    stdout, stderr, truncated, _ = await seatbelt._communicate_capped(
        process, stdout_byte_cap=128,
    )
    assert len(stdout) == 128 and truncated
    assert process.stdout.payload == b""
    assert stderr == b"diagnostic"


async def test_windows_structured_output_uses_finite_budget() -> None:
    process = _Process(b"x" * 4096, b"diagnostic")
    captured, errors = await windows_default._collect_process_output(
        process, expected_nonce="bounded", stdout_byte_cap=128,
    )
    assert len(captured.data) == 128 and captured.truncated
    assert process.stdout.payload == b""
    assert bytes(errors.data) == b"diagnostic"
