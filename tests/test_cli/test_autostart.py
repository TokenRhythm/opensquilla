from __future__ import annotations

import base64
from pathlib import Path
from unittest import mock

import pytest

from opensquilla.cli.autostart import (
    AutostartError,
    PlatformNotSupportedError,
    _encoded_powershell,
    _launch_script,
    _registration_script,
    register_logon_task,
    task_name_for_profile,
)


def _decode_powershell(encoded: str) -> str:
    return base64.b64decode(encoded.encode("ascii")).decode("utf-16-le")


def _extract_encoded_after(script: str, marker: str) -> str:
    return script.split(marker, 1)[1].split("'", 1)[0]


def test_task_name_for_profile() -> None:
    assert task_name_for_profile(None) == "OpenSquilla"
    assert task_name_for_profile("coder") == "OpenSquilla_coder"


def test_launch_script_for_profile_sets_profile_env(tmp_path: Path) -> None:
    home = tmp_path / "profiles" / "coder"
    script = _launch_script(opensquilla="C:/Tools/opensquilla.exe", profile="coder", home=home)

    assert "OPENSQUILLA_HOME" in script
    assert "OPENSQUILLA_PROFILE" in script
    assert "'--profile' 'coder' 'gateway' 'start'" in script


def test_launch_script_for_profile_masks_inherited_state_dir(tmp_path: Path) -> None:
    home = tmp_path / "profiles" / "coder"
    script = _launch_script(opensquilla="C:/Tools/opensquilla.exe", profile="coder", home=home)

    clear_state_dir = r"Remove-Item Env:\OPENSQUILLA_STATE_DIR -ErrorAction SilentlyContinue"
    set_home = "$env:OPENSQUILLA_HOME"
    set_profile = "$env:OPENSQUILLA_PROFILE"

    assert clear_state_dir in script
    assert script.index(clear_state_dir) < script.index(set_home) < script.index(set_profile)


def test_launch_script_without_profile_uses_legacy_gateway_start(tmp_path: Path) -> None:
    script = _launch_script(opensquilla="C:/Tools/opensquilla.exe", profile=None, home=tmp_path)

    assert "OPENSQUILLA_PROFILE" not in script
    assert "'gateway' 'start'" in script
    assert "--profile" not in script


def test_launch_script_with_state_dir_sets_state_override(tmp_path: Path) -> None:
    home = tmp_path / "state-home"
    script = _launch_script(
        opensquilla="C:/Tools/opensquilla.exe",
        profile=None,
        home=home,
        state_dir=home,
    )

    assert "OPENSQUILLA_STATE_DIR" in script
    assert str(home) in script
    assert "--profile" not in script


def test_registration_script_uses_encoded_inner_launch_command(tmp_path: Path) -> None:
    home = tmp_path / "profiles with space" / "coder"
    script = _registration_script(
        powershell="C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
        opensquilla="C:/Program Files/OpenSquilla/opensquilla.exe",
        profile="coder",
        home=home,
        task_name="OpenSquilla_coder",
    )

    assert "New-ScheduledTaskAction" in script
    assert "Register-ScheduledTask" in script
    assert "OpenSquilla_coder" in script
    marker = "-EncodedCommand "
    encoded = script.split(marker, 1)[1].split("'", 1)[0]
    inner = _decode_powershell(encoded)
    assert "C:/Program Files/OpenSquilla/opensquilla.exe" in inner
    assert "--profile" in inner


def test_registration_script_uses_valid_limited_runlevel(tmp_path: Path) -> None:
    script = _registration_script(
        powershell="C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
        opensquilla="C:/Program Files/OpenSquilla/opensquilla.exe",
        profile=None,
        home=tmp_path,
        task_name="OpenSquilla",
    )

    assert "-RunLevel Limited" in script
    assert "LeastPrivilege" not in script


def test_register_logon_task_windows_dispatches_encoded_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "profiles" / "coder"
    home.mkdir(parents=True)
    monkeypatch.setattr("opensquilla.cli.autostart.platform.system", lambda: "Windows")

    def fake_which(name: str) -> str | None:
        if name == "opensquilla":
            return "C:/Tools/opensquilla.exe"
        if name in {"powershell.exe", "powershell"}:
            return "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        return None

    monkeypatch.setattr("opensquilla.cli.autostart.shutil.which", fake_which)
    with mock.patch("opensquilla.cli.autostart.subprocess.run") as run_mock:
        run_mock.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        result = register_logon_task(profile="coder", home=home)

    assert result.platform == "Windows"
    assert result.profile == "coder"
    assert result.target == "OpenSquilla_coder"
    cmd = run_mock.call_args.args[0]
    assert cmd[:4] == [
        "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
    ]
    assert cmd[4] == "-EncodedCommand"
    outer = _decode_powershell(cmd[5])
    assert "Register-ScheduledTask" in outer
    assert "OpenSquilla_coder" in outer


def test_register_logon_task_state_dir_reaches_nested_launch_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "state-home"
    home.mkdir(parents=True)
    monkeypatch.setattr("opensquilla.cli.autostart.platform.system", lambda: "Windows")

    def fake_which(name: str) -> str | None:
        if name == "opensquilla":
            return "C:/Tools/opensquilla.exe"
        if name in {"powershell.exe", "powershell"}:
            return "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        return None

    monkeypatch.setattr("opensquilla.cli.autostart.shutil.which", fake_which)
    with mock.patch("opensquilla.cli.autostart.subprocess.run") as run_mock:
        run_mock.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        result = register_logon_task(profile=None, home=home, state_dir=home)

    assert result.profile is None
    cmd = run_mock.call_args.args[0]
    outer = _decode_powershell(cmd[5])
    inner_encoded = _extract_encoded_after(outer, "-EncodedCommand ")
    inner = _decode_powershell(inner_encoded)
    assert "OPENSQUILLA_STATE_DIR" in inner
    assert str(home) in inner
    assert "--profile" not in inner


def test_register_logon_task_rejects_unsupported_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("opensquilla.cli.autostart.platform.system", lambda: "Darwin")

    with pytest.raises(PlatformNotSupportedError, match="Windows-only"):
        register_logon_task(profile="coder", home=tmp_path)


def test_register_logon_task_raises_when_opensquilla_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("opensquilla.cli.autostart.platform.system", lambda: "Windows")
    monkeypatch.setattr("opensquilla.cli.autostart.shutil.which", lambda name: None)

    with pytest.raises(AutostartError, match="opensquilla is not on PATH"):
        register_logon_task(profile="coder", home=tmp_path)


def test_encoded_powershell_round_trips() -> None:
    script = "$env:OPENSQUILLA_PROFILE = 'coder'"
    assert _decode_powershell(_encoded_powershell(script)) == script
