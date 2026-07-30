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
    assert is_likely_sandbox_denied(
        _result(returncode=128 + int(signal.SIGSYS))
    ) is True


def test_platform_without_sigsys_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(denial_attribution.signal, "SIGSYS", raising=False)

    assert is_likely_sandbox_denied(_result(returncode=1)) is False


def test_generic_nonzero_exit_is_never_escalated() -> None:
    assert is_likely_sandbox_denied(
        _result(returncode=1, stderr="tests failed: 3 assertions")
    ) is False


def test_unsandboxed_backend_is_never_attributed_from_text() -> None:
    assert is_likely_sandbox_denied(
        _result(returncode=1, stderr="permission denied", backend="noop")
    ) is False
