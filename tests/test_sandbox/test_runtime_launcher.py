from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from opensquilla.sandbox.runtime_launcher import (
    ChildRole,
    InternalChildDispatchError,
    apply_bundled_runtime_path,
    dispatch_internal_child,
    internal_child_argv,
)


@pytest.mark.parametrize("frozen", [False, True])
def test_internal_child_import_never_probes_writable_temp(tmp_path, frozen):
    # A fresh interpreter is essential: an already imported sandbox package
    # hides import-time gettempdir() writes in read-only/frozen children.
    source_root = Path(__file__).resolve().parents[2] / "src"
    code = f"""
import sys
import tempfile
def forbidden():
    raise AssertionError('internal child import must not probe temporary storage')
tempfile.gettempdir = forbidden
sys.frozen = {frozen!r}
from opensquilla.sandbox.runtime_launcher import dispatch_internal_child
raise SystemExit(dispatch_internal_child(['python-code', 'print("child-ready")']))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(source_root), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "child-ready"


@pytest.mark.parametrize(
    ("role", "module"),
    [
        (ChildRole.PROCESS_TREE, "opensquilla.process_tree"),
        (ChildRole.FILESYSTEM_WORKER, "opensquilla.sandbox.filesystem_worker"),
        (ChildRole.LINUX_HELPER, "opensquilla.sandbox.backend.linux_helper"),
        (ChildRole.PYTHON_CODE, "opensquilla.sandbox.python_code_runner"),
        (
            ChildRole.WINDOWS_DEFAULT_RUNNER,
            "opensquilla.sandbox.backend.windows_default_runner",
        ),
        (
            ChildRole.DIRECTORY_PICKER,
            "opensquilla.gateway.windows_directory_picker",
        ),
    ],
)
def test_source_child_uses_python_module(
    monkeypatch: pytest.MonkeyPatch,
    role: ChildRole,
    module: str,
) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", "/runtime/python")

    assert internal_child_argv(role, args=("--probe",)) == (
        "/runtime/python",
        "-m",
        module,
        "--probe",
    )


@pytest.mark.parametrize(
    "role",
    [
        ChildRole.FILESYSTEM_WORKER,
        ChildRole.PROCESS_TREE,
        ChildRole.LINUX_HELPER,
        ChildRole.WINDOWS_DEFAULT_RUNNER,
        ChildRole.DIRECTORY_PICKER,
        ChildRole.PYTHON_CODE,
    ],
)
def test_frozen_child_uses_internal_role(
    monkeypatch: pytest.MonkeyPatch,
    role: ChildRole,
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "C:\\OpenSquilla\\gateway.exe")

    assert internal_child_argv(role, args=("--probe",)) == (
        "C:\\OpenSquilla\\gateway.exe",
        "--internal-child",
        role.value,
        "--probe",
    )


def test_internal_child_argv_rejects_unregistered_role() -> None:
    with pytest.raises(ValueError, match="unknown internal child role"):
        internal_child_argv("shell")


def test_dispatch_rejects_missing_or_unknown_role() -> None:
    with pytest.raises(InternalChildDispatchError, match="missing"):
        dispatch_internal_child([])
    with pytest.raises(InternalChildDispatchError, match="unknown"):
        dispatch_internal_child(["shell"])


def test_dispatch_process_tree_child(monkeypatch: pytest.MonkeyPatch) -> None:
    from opensquilla import process_tree

    monkeypatch.setattr(process_tree, "main", lambda args: 7 if tuple(args) == ("--probe",) else 2)

    assert dispatch_internal_child(["process-tree", "--probe"]) == 7


def test_dispatch_python_code_child_preserves_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    from opensquilla.sandbox import python_code_runner

    def fail_with_requested_exit(args: list[str] | tuple[str, ...]) -> int:
        assert tuple(args) == ("raise SystemExit(7)",)
        raise SystemExit(7)

    monkeypatch.setattr(python_code_runner, "main", fail_with_requested_exit)

    with pytest.raises(SystemExit) as exc:
        dispatch_internal_child(["python-code", "raise SystemExit(7)"])

    assert exc.value.code == 7


def test_strict_runtime_path_does_not_inherit_host_when_no_pack_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/host/bin")
    monkeypatch.delenv("OPENSQUILLA_BUNDLED_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("OPENSQUILLA_RUNTIME_MANIFEST", raising=False)

    result = apply_bundled_runtime_path(
        {"PATH": "/host/bin"},
        mode="safe",
        require_bundled=True,
    )

    assert result["PATH"] == ""
