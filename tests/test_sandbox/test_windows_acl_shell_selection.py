from __future__ import annotations

import base64
import json

import pytest

from opensquilla.sandbox import upgrade_migration as module

WINPS_SYSTEM32 = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
WINPS_SYSNATIVE = r"C:\Windows\SysNative\WindowsPowerShell\v1.0\powershell.exe"
PWSH = r"C:\Program Files\PowerShell\7\pwsh.exe"


def _patch_exists(monkeypatch: pytest.MonkeyPatch, *, system32: bool, sysnative: bool) -> None:
    def fake_exists(path: str) -> bool:
        if system32 and path == WINPS_SYSTEM32:
            return True
        if sysnative and path == WINPS_SYSNATIVE:
            return True
        return False

    monkeypatch.setattr(module.os.path, "exists", fake_exists)


def _patch_which(monkeypatch: pytest.MonkeyPatch, *, pwsh: bool) -> None:
    def fake_which(name: str) -> str | None:
        if pwsh and name == "pwsh":
            return PWSH
        return None

    monkeypatch.setattr(module.shutil, "which", fake_which)


def test_selects_system32_windows_powershell_on_64bit(monkeypatch: pytest.MonkeyPatch) -> None:
    # 64-bit process: the SysNative redirector is invisible, so System32 wins.
    _patch_exists(monkeypatch, system32=True, sysnative=False)
    _patch_which(monkeypatch, pwsh=True)
    assert module._windows_powershell_exe() == WINPS_SYSTEM32
    assert module._windows_acl_shells() == (WINPS_SYSTEM32, PWSH)


def test_selects_sysnative_windows_powershell_on_32bit(monkeypatch: pytest.MonkeyPatch) -> None:
    # 32-bit process: SysNative redirects to the native 64-bit directory.
    _patch_exists(monkeypatch, system32=False, sysnative=True)
    _patch_which(monkeypatch, pwsh=True)
    assert module._windows_powershell_exe() == WINPS_SYSNATIVE
    assert module._windows_acl_shells() == (WINPS_SYSNATIVE, PWSH)


def test_missing_system_paths_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Neither canonical location exists. We must NOT silently resolve a bare
    # "powershell" from PATH, which a co-installed pwsh can shadow.
    _patch_exists(monkeypatch, system32=False, sysnative=False)
    _patch_which(monkeypatch, pwsh=False)
    assert module._windows_powershell_exe() is None


def test_path_only_pwsh_falls_back_to_pwsh(monkeypatch: pytest.MonkeyPatch) -> None:
    # Windows PowerShell 5.1 absent, only pwsh on PATH: pwsh is the fallback.
    _patch_exists(monkeypatch, system32=False, sysnative=False)
    _patch_which(monkeypatch, pwsh=True)
    assert module._windows_powershell_exe() is None
    assert module._windows_acl_shells() == (PWSH,)


def test_both_unavailable_raises_diagnosable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # No trusted Windows PowerShell 5.1 and no pwsh: fail loudly, not silently.
    _patch_exists(monkeypatch, system32=False, sysnative=False)
    _patch_which(monkeypatch, pwsh=False)
    with pytest.raises(RuntimeError, match="Windows PowerShell 5.1"):
        module._windows_acl_shells()


def test_preferred_shell_failure_falls_back_at_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # The runtime loop must try the preferred Windows PowerShell 5.1 first and
    # fall back to pwsh only when the preferred invocation fails for a
    # retryable reason, then succeed.
    entry = tmp_path / "snapshot"
    entry.mkdir()
    monkeypatch.setattr(module, "_windows_acl_shells", lambda: (WINPS_SYSTEM32, PWSH))

    calls: list[str] = []

    class _Completed:
        def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **_kwargs):
        calls.append(cmd[0])
        if cmd[0] == WINPS_SYSTEM32:
            return _Completed(1, stdout=b"", stderr=b"securityprivilege required")
        entries = ((entry, True),)
        result = {
            "count": len(entries),
            "ids": [str(i) for i in range(len(entries))],
            "pathUtf8Base64": [
                base64.b64encode(str(p).encode("utf-8")).decode("ascii") for p, _ in entries
            ],
            "pathHashes": [module._acl_path_hash(p) for p, _ in entries],
        }
        return _Completed(0, stdout=json.dumps(result).encode("ascii"))

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module._protect_windows_acl_batch(((entry, True),), windows_user_sid="S-1-5-21-1")
    assert calls == [WINPS_SYSTEM32, PWSH]
