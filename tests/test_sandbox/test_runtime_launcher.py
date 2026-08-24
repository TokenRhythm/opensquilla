from __future__ import annotations

import sys

import pytest

from opensquilla.sandbox.runtime_launcher import (
    ChildRole,
    InternalChildDispatchError,
    apply_bundled_runtime_path,
    dispatch_internal_child,
    internal_child_argv,
)


@pytest.mark.parametrize(
    ("role", "module"),
    [
        (ChildRole.PROCESS_TREE, "opensquilla.process_tree"),
        (ChildRole.FILESYSTEM_WORKER, "opensquilla.sandbox.filesystem_worker"),
        (ChildRole.LINUX_HELPER, "opensquilla.sandbox.backend.linux_helper"),
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
