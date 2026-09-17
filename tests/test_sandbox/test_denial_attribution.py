from __future__ import annotations

import signal

import pytest

from opensquilla.sandbox import denial_attribution
from opensquilla.sandbox.denial_attribution import is_likely_sandbox_denied
from opensquilla.sandbox.types import SandboxResult


def _result(
    *,
    returncode: int,
    stdout: str = "",
    stderr: str = "",
    backend: str = "bubblewrap",
    notes: tuple[str, ...] = (),
) -> SandboxResult:
    return SandboxResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        wall_time_s=0.1,
        backend_used=backend,
        backend_notes=notes,
    )


@pytest.mark.parametrize(
    "message",
    [
        "Operation not permitted",
        "Permission denied",
        "Read-only file system",
        "blocked by seccomp",
        "sandbox rejected the operation",
        "Landlock denied access",
        "failed to write file",
    ],
)
def test_codex_denial_keywords_are_attributed(message: str) -> None:
    assert is_likely_sandbox_denied(_result(returncode=1, stderr=message)) is True


def test_structured_backend_note_is_attributed_even_for_network_zero_exit() -> None:
    result = _result(
        returncode=0,
        notes=("network.denied: outbound connection blocked",),
    )

    assert is_likely_sandbox_denied(result) is True


@pytest.mark.parametrize("returncode", [2, 126, 127])
def test_quick_reject_without_denial_evidence_is_not_attributed(returncode: int) -> None:
    assert is_likely_sandbox_denied(_result(returncode=returncode)) is False


@pytest.mark.skipif(not hasattr(signal, "SIGSYS"), reason="SIGSYS is unavailable")
def test_linux_sigsys_is_attributed() -> None:
    assert is_likely_sandbox_denied(_result(returncode=128 + int(signal.SIGSYS))) is True


def test_platform_without_sigsys_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(denial_attribution.signal, "SIGSYS", raising=False)

    assert is_likely_sandbox_denied(_result(returncode=1)) is False


def test_generic_nonzero_exit_is_never_escalated() -> None:
    assert (
        is_likely_sandbox_denied(_result(returncode=1, stderr="tests failed: 3 assertions"))
        is False
    )


def test_unsandboxed_backend_is_never_attributed_from_text() -> None:
    assert (
        is_likely_sandbox_denied(_result(returncode=1, stderr="permission denied", backend="noop"))
        is False
    )


def _frozen_python_traceback(error: str) -> str:
    return (
        "Traceback (most recent call last):\n"
        '  File "gateway-entry.py", line 205, in <module>\n'
        '  File "opensquilla\\sandbox\\runtime_launcher.py", line 230, in dispatch_internal_child\n'
        '  File "opensquilla\\sandbox\\runtime_launcher.py", line 207, in _run_python_code\n'
        '  File "opensquilla\\sandbox\\python_code_runner.py", line 27, in main\n'
        '  File "<string>", line 1\n'
        f"{error}\n"
        "[PYI-123:ERROR] Failed to execute script 'gateway-entry' due to unhandled exception!\n"
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r\r\n"])
@pytest.mark.parametrize(
    "error",
    ["ValueError: 合成普通异常", "    def broken(:\n               ^\nSyntaxError: invalid syntax"],
)
def test_frozen_python_errors_are_not_denials(error: str, newline: str) -> None:
    stderr = _frozen_python_traceback(error).replace("\n", newline)
    result = _result(returncode=1, stderr=stderr, backend="windows_default")

    assert is_likely_sandbox_denied(result) is False
    assert result.stderr == stderr


def test_unicode_python_frame_location_is_not_denial_evidence() -> None:
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "C:\\合成 空格\\sandbox\\脚本.py", line 12, in 执行\n'
        "ValueError: 合成普通异常\n"
    )

    assert is_likely_sandbox_denied(_result(returncode=1, stderr=stderr)) is False


@pytest.mark.parametrize(
    "error",
    [
        "PermissionError: [Errno 13] Permission denied: 'synthetic.txt'",
        "SandboxBlocked: synthetic policy denied",
        "    raise SandboxBlocked('denied')\nValueError: synthetic error",
    ],
)
def test_python_traceback_keeps_real_denial_and_source_text(error: str) -> None:
    assert (
        is_likely_sandbox_denied(_result(returncode=1, stderr=_frozen_python_traceback(error)))
        is True
    )


@pytest.mark.parametrize(
    "message",
    [
        'File "sandbox/file.py", line 1',
        ' File "sandbox/file.py", line 1',
        '    File "sandbox/file.py", line 1',
        '  File "sandbox/file.py", line unknown',
        '  File "sandbox/file.py", line 0',
        '  File "sandbox/file.py", line 1: Permission denied',
        '  File "sandbox/file.py", line 1, in main: Permission denied',
        '  File "sandbox/file.py", line 1, in',
        '  File "sandbox/file.py, line 1',
        'PermissionError: File "sandbox/file.py", line 1',
    ],
)
def test_noncanonical_python_frame_text_remains_denial_evidence(message: str) -> None:
    assert is_likely_sandbox_denied(_result(returncode=1, stderr=message)) is True


def test_traceback_filter_does_not_change_stdout_or_structured_notes() -> None:
    traceback = _frozen_python_traceback("ValueError: synthetic error")
    assert (
        is_likely_sandbox_denied(
            _result(returncode=1, stderr=traceback, stdout='  File "sandbox/file.py", line 1')
        )
        is True
    )
    assert (
        is_likely_sandbox_denied(
            _result(returncode=1, stderr=traceback, notes=("network.denied: blocked",))
        )
        is True
    )
