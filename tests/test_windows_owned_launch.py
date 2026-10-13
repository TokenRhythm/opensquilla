from __future__ import annotations

import builtins
import os
import runpy
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla import _windows_owned_launch as helper

ROOT = Path(__file__).resolve().parents[1]


def test_helper_cold_import_does_not_load_parent_or_target_dependencies() -> None:
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(ROOT / 'src')!r})
        import opensquilla._windows_owned_launch
        forbidden = ('asyncio', 'subprocess', 'importlib.metadata',
                     'opensquilla.process_tree', 'opensquilla.sandbox')
        loaded = [name for name in sys.modules
                  if any(name == root or name.startswith(root + '.') for root in forbidden)]
        assert not loaded, loaded
        """,
    )
    # Skip site/customization and installed editable paths in this fresh process.
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", script],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_source_script_boots_outside_package_with_isolated_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from opensquilla import process_tree

    package_dir = tmp_path / "中文 package space"
    package_dir.mkdir()
    script_path = package_dir / "_windows_owned_launch.py"
    script_path.write_text(Path(helper.__file__).read_text(encoding="utf-8"), encoding="utf-8")
    # A direct file must not initialize even the adjacent package.
    (package_dir / "__init__.py").write_text("raise AssertionError('package loaded')\n")
    (package_dir / "ctypes.py").write_text("raise AssertionError('script path loaded')\n")
    unrelated_cwd = tmp_path / "unrelated cwd"
    unrelated_cwd.mkdir()
    (unrelated_cwd / "ctypes.py").write_text("raise AssertionError('cwd loaded')\n")
    poison_path = tmp_path / "pythonpath"
    poison_path.mkdir()
    marker = tmp_path / "site-ran"
    (poison_path / "sitecustomize.py").write_text(
        f"open({str(marker)!r}, 'w').close()\nraise AssertionError('site loaded')\n",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment.update(
        PYTHONPATH=str(poison_path), PYTHONHOME=str(tmp_path / "not-a-python-home"),
        PYTHONPYCACHEPREFIX=str(tmp_path / "ignored-pycache"),
    )
    monkeypatch.setattr(process_tree, "__file__", str(package_dir / "process_tree.py"))
    monkeypatch.setattr(process_tree.sys, "frozen", False, raising=False)
    # Invalid helper arguments exercise startup but never touch native events.
    argv = process_tree._process_tree_child_argv("--windows-owned-launch")
    assert argv == (sys.executable, "-I", "-S", "-B", str(script_path), "--windows-owned-launch")
    result = subprocess.run(
        argv, cwd=unrelated_cwd, env=environment,
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 2, result.stderr
    assert result.stdout == result.stderr == ""
    assert not marker.exists()
    assert not (package_dir / "__pycache__").exists()
    assert not (tmp_path / "ignored-pycache").exists()


class _WinFunction:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


def _kernel(monkeypatch: pytest.MonkeyPatch, *, failure: str | None = None):
    events: list[str] = []
    closed: list[int] = []

    def open_event(access, inherit, name):
        assert inherit is False
        assert (name, access) in {("gate", 0x00100000), ("ready", 0x0002)}
        events.append("open_" + name)
        return 0 if failure == "open_" + name else {"gate": 10, "ready": 20}[name]

    def set_event(handle):
        assert handle == 20
        events.append("signal_ready")
        return failure != "signal_ready"

    def wait_for_gate(handle, timeout):
        assert (handle, timeout) == (10, 0xFFFFFFFF)
        assert "signal_ready" in events
        assert "import_subprocess" not in events
        assert "spawn" not in events
        events.append("wait_gate")
        if failure == "wait_gate":
            return 0xFFFFFFFF
        events.append("gate_released")
        return 0

    def close_handle(handle):
        closed.append(handle)
        return True

    kernel = SimpleNamespace(
        OpenEventW=_WinFunction(open_event),
        SetEvent=_WinFunction(set_event),
        WaitForSingleObject=_WinFunction(wait_for_gate),
        CloseHandle=_WinFunction(close_handle),
    )

    def win_dll(name, *, use_last_error):
        assert name == "kernel32" and use_last_error is True
        return kernel

    monkeypatch.setattr(helper.ctypes, "WinDLL", win_dll, raising=False)
    monkeypatch.setattr(helper, "_windows_error", lambda: OSError(5, "synthetic Win32 error"))
    return events, closed


def _target_import(monkeypatch: pytest.MonkeyPatch, events, popen):
    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "subprocess":
            assert "gate_released" in events
            events.append("import_subprocess")
            return SimpleNamespace(Popen=popen)
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)


def test_target_import_and_execution_wait_for_gate_and_preserve_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events, closed = _kernel(monkeypatch)
    target = ["tool.exe", "中文 space", "a;b"]
    environment = {
        "SystemRoot": r"C:\Windows", "caller_value": "中文",
        "PYTHONPATH": "caller Python path", "PYTHONIOENCODING": "utf-8",
        helper._WINDOWS_HELPER_STRIP_ENV: "systemroot",
    }
    monkeypatch.setattr(helper.os, "environ", environment)

    def popen(argv, **kwargs):
        events.append("spawn")
        assert "gate_released" in events
        assert closed == [20, 10]
        assert argv == target
        # No stdio/cwd/creation flag changes accompany extraction.
        assert kwargs == {"env": {
            "caller_value": "中文", "PYTHONPATH": "caller Python path",
            "PYTHONIOENCODING": "utf-8",
        }}
        return SimpleNamespace(wait=lambda: 23)

    _target_import(monkeypatch, events, popen)
    assert helper.main(["--windows-owned-launch", "gate", "ready", "--", *target]) == 23
    assert events.index("signal_ready") < events.index("gate_released")
    assert events.index("gate_released") < events.index("import_subprocess")
    assert events.index("import_subprocess") < events.index("spawn")
    assert environment["SystemRoot"] == r"C:\Windows"


@pytest.mark.parametrize(
    ("failure", "expected_closed"),
    [("open_gate", []), ("open_ready", [10]),
     ("signal_ready", [20, 10]), ("wait_gate", [20, 10])],
)
def test_handshake_errors_never_import_or_start_target(
    monkeypatch: pytest.MonkeyPatch, failure: str, expected_closed: list[int],
) -> None:
    events, closed = _kernel(monkeypatch, failure=failure)
    _target_import(monkeypatch, events, lambda *_args, **_kwargs: pytest.fail("target started"))
    with pytest.raises(OSError, match="synthetic Win32 error"):
        helper.main(["--windows-owned-launch", "gate", "ready", "--", "target"])
    assert "import_subprocess" not in events
    assert closed == expected_closed


def test_target_spawn_error_keeps_bounded_stderr_and_127(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    events, _closed = _kernel(monkeypatch)

    def failed_spawn(*_args, **_kwargs):
        raise OSError("private target failure detail")

    _target_import(monkeypatch, events, failed_spawn)
    assert helper.main(["--windows-owned-launch", "gate", "ready", "--", "target"]) == 127
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "OpenSquilla controlled launch failed\n"


def test_empty_target_preserves_handshake_then_127_without_target_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events, closed = _kernel(monkeypatch)
    _target_import(monkeypatch, events, lambda *_args, **_kwargs: pytest.fail("target started"))
    assert helper.main(["--windows-owned-launch", "gate", "ready", "--"]) == 127
    assert "gate_released" in events
    assert "import_subprocess" not in events
    assert closed == [20, 10]


def test_target_import_failure_after_release_is_not_relabelled_as_spawn_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    events, closed = _kernel(monkeypatch)
    original_import = builtins.__import__

    def failed_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "subprocess":
            assert "gate_released" in events
            raise ImportError("synthetic target machinery import failure")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", failed_import)
    with pytest.raises(ImportError, match="synthetic target machinery import failure"):
        helper.main(["--windows-owned-launch", "gate", "ready", "--", "target"])
    assert closed == [20, 10]
    assert "spawn" not in events
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "argv", [[], ["--windows-owned-launch"], ["--other", "g", "r", "--"],
             ["--windows-owned-launch", "g", "r", "not-a-separator"]],
)
def test_invalid_arguments_do_not_enter_handshake(monkeypatch: pytest.MonkeyPatch, argv) -> None:
    monkeypatch.setattr(
        helper, "_run_windows_owned_launch",
        lambda *_args: pytest.fail("invalid arguments reached handshake"),
    )
    assert helper.main(argv) == 2


def test_environment_strip_preserves_casefold_copy_and_first_match_contract() -> None:
    original = {
        helper._WINDOWS_HELPER_STRIP_ENV.lower(): "SYSTEMROOT;;windir;missing",
        "SystemRoot": "injected", "SYSTEMROOT": "second case variant",
        "WINDIR": "injected", "ComSpec": "caller provided", "target": 7,
    }
    assert helper._windows_target_env_from_helper(original) == {
        "SYSTEMROOT": "second case variant", "ComSpec": "caller provided", "target": "7",
    }
    assert original["SystemRoot"] == "injected"
    assert helper._WINDOWS_HELPER_STRIP_ENV.lower() in original


def test_frozen_entry_routes_windows_helper_without_parent_sandbox_or_ssl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = ROOT / "desktop/electron/scripts/gateway-entry.py"
    arguments = ["--windows-owned-launch", "gate", "ready", "--", "target", "中文"]
    calls = []

    def main(argv):
        calls.append(argv)
        return 37

    monkeypatch.setattr(helper, "main", main)
    monkeypatch.setattr(sys, "argv", [str(entry), "--internal-child", "process-tree", *arguments])
    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        assert not any(
            name == root or name.startswith(root + ".")
            for root in ("asyncio", "subprocess", "ssl", "importlib.metadata",
                         "opensquilla.process_tree", "opensquilla.sandbox", "opensquilla.cli")
        ), name
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(entry), run_name="__main__")
    assert caught.value.code == 37
    assert calls == [arguments]
