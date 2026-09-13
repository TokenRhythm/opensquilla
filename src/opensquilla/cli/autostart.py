"""Windows logon autostart helpers for OpenSquilla init."""

from __future__ import annotations

import base64
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from opensquilla.paths import is_valid_profile_name


class AutostartError(RuntimeError):
    """Raised when logon autostart registration fails."""


class PlatformNotSupportedError(AutostartError):
    """Raised when the current platform has no supported autostart backend."""


@dataclass(frozen=True)
class AutostartResult:
    platform: str
    profile: str | None
    target: str

    def summary(self) -> str:
        if self.profile:
            return (
                f"{self.platform} autostart registered for profile "
                f"'{self.profile}' as {self.target}."
            )
        return (
            f"{self.platform} autostart registered for the default "
            f"OpenSquilla home as {self.target}."
        )


def task_name_for_profile(profile: str | None) -> str:
    if not profile:
        return "OpenSquilla"
    return f"OpenSquilla_{profile}"


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _encoded_powershell(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _resolve_executable(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise AutostartError(
            f"{name} is not on PATH; install OpenSquilla before enabling autostart."
        )
    return resolved


def _launch_script(
    *,
    opensquilla: str,
    profile: str | None,
    home: Path,
    state_dir: Path | None = None,
) -> str:
    lines = ["$ErrorActionPreference = 'Stop'"]
    args = ["gateway", "start"]
    if state_dir is not None:
        lines.append(f"$env:OPENSQUILLA_STATE_DIR = {_powershell_literal(str(state_dir))}")
    elif profile:
        if not is_valid_profile_name(profile):
            raise AutostartError(f"Invalid OpenSquilla profile name: {profile}")
        lines.append(r"Remove-Item Env:\OPENSQUILLA_STATE_DIR -ErrorAction SilentlyContinue")
        lines.append(f"$env:OPENSQUILLA_HOME = {_powershell_literal(str(home.parent))}")
        lines.append(f"$env:OPENSQUILLA_PROFILE = {_powershell_literal(profile)}")
        args = ["--profile", profile, *args]

    rendered_args = " ".join(_powershell_literal(arg) for arg in args)
    lines.append(f"& {_powershell_literal(opensquilla)} {rendered_args}")
    lines.append("exit $LASTEXITCODE")
    return "\n".join(lines)


def _registration_script(
    *,
    powershell: str,
    opensquilla: str,
    profile: str | None,
    home: Path,
    task_name: str,
    state_dir: Path | None = None,
) -> str:
    launch_script = _launch_script(
        opensquilla=opensquilla,
        profile=profile,
        home=home,
        state_dir=state_dir,
    )
    launch_args = (
        "-NoProfile -ExecutionPolicy Bypass -EncodedCommand "
        + _encoded_powershell(launch_script)
    )
    description = (
        f"Auto-start OpenSquilla profile {profile} at user logon."
        if profile
        else "Auto-start OpenSquilla gateway at user logon."
    )

    return "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            (
                "$Action = New-ScheduledTaskAction "
                f"-Execute {_powershell_literal(powershell)} "
                f"-Argument {_powershell_literal(launch_args)} "
                f"-WorkingDirectory {_powershell_literal(str(home))}"
            ),
            "$Trigger = New-ScheduledTaskTrigger -AtLogOn",
            (
                "$Principal = New-ScheduledTaskPrincipal "
                '-UserId "$env:USERDOMAIN\\$env:USERNAME" '
                "-LogonType Interactive "
                "-RunLevel Limited"
            ),
            (
                "$Settings = New-ScheduledTaskSettingsSet "
                "-MultipleInstances Parallel "
                "-ExecutionTimeLimit (New-TimeSpan -Minutes 10) "
                "-AllowStartIfOnBatteries "
                "-DontStopIfGoingOnBatteries"
            ),
            (
                "Register-ScheduledTask "
                f"-TaskName {_powershell_literal(task_name)} "
                f"-Description {_powershell_literal(description)} "
                "-Action $Action "
                "-Trigger $Trigger "
                "-Principal $Principal "
                "-Settings $Settings "
                "-Force | Out-Null"
            ),
        ]
    )


def register_logon_task(
    *, profile: str | None, home: Path, state_dir: Path | None = None
) -> AutostartResult:
    system = platform.system()
    if system != "Windows":
        raise PlatformNotSupportedError(f"Autostart registration is Windows-only for now: {system}")

    opensquilla = _resolve_executable("opensquilla")
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        raise AutostartError("powershell.exe is not on PATH; cannot register Task Scheduler entry.")

    task_name = task_name_for_profile(profile)
    script = _registration_script(
        powershell=powershell,
        opensquilla=opensquilla,
        profile=profile,
        home=home,
        state_dir=state_dir,
        task_name=task_name,
    )
    command = [
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        _encoded_powershell(script),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        suffix = f": {details}" if details else ""
        raise AutostartError(f"Register-ScheduledTask failed{suffix}")

    return AutostartResult(platform=system, profile=profile, target=task_name)
