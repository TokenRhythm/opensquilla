from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / "scripts" / "supervisor"


def test_supervisor_scripts_are_windows_profile_scoped() -> None:
    scripts = {
        "lib.ps1",
        "start-all.ps1",
        "stop-all.ps1",
        "status.ps1",
        "install-autostart.ps1",
        "uninstall-autostart.ps1",
    }
    for name in scripts:
        assert (SUPERVISOR / name).is_file()

    combined = "\n".join((SUPERVISOR / name).read_text(encoding="utf-8") for name in scripts)
    assert r"D:\ai\opensquilla" not in combined
    assert "OPENSQUILLA_HOME" in combined
    assert "--profile" in combined
    assert "^[a-z0-9][a-z0-9_-]{0,63}$" in combined


def test_task_scheduler_registration_uses_encoded_command() -> None:
    text = (SUPERVISOR / "install-autostart.ps1").read_text(encoding="utf-8")

    assert "-EncodedCommand" in text
    assert "ConvertTo-PowerShellSingleQuotedLiteral" in text
    assert "ConvertTo-XmlEscapedText" in text


def test_supervisor_profile_name_match_is_case_sensitive() -> None:
    lib = (SUPERVISOR / "lib.ps1").read_text(encoding="utf-8")

    assert "-cmatch $Script:PROFILE_NAME_PATTERN" in lib
    assert "-match $Script:PROFILE_NAME_PATTERN" not in lib


def test_supervisor_explicit_repo_takes_precedence_over_installed_binary() -> None:
    lib = (SUPERVISOR / "lib.ps1").read_text(encoding="utf-8")

    repo_branch = "if ($Repo) {"
    installed_lookup = "$installed = Get-Command 'opensquilla'"
    explicit_error = 'throw "OpenSquilla repo lacks pyproject.toml: $resolvedRepo"'
    auto_detect = "Get-OpensquillaRoot -Override $null"
    assert repo_branch in lib
    assert installed_lookup in lib
    assert explicit_error in lib
    assert auto_detect in lib
    assert lib.index(repo_branch) < lib.index(explicit_error) < lib.index(installed_lookup)
    assert lib.index(installed_lookup) < lib.index(auto_detect)


def test_supervisor_profile_invocation_masks_inherited_state_dir() -> None:
    lib = (SUPERVISOR / "lib.ps1").read_text(encoding="utf-8")

    save_state_dir = "$previousStateDir = $env:OPENSQUILLA_STATE_DIR"
    clear_state_dir = r"Remove-Item Env:\OPENSQUILLA_STATE_DIR -ErrorAction SilentlyContinue"
    restore_state_dir = "$env:OPENSQUILLA_STATE_DIR = $previousStateDir"
    set_profile = "$env:OPENSQUILLA_PROFILE = $profileLeaf"
    resolve_command = "$cmd = Get-OpensquillaCommand -Repo $Repo"

    assert save_state_dir in lib
    assert clear_state_dir in lib
    assert restore_state_dir in lib
    assert (
        lib.index(save_state_dir)
        < lib.index(set_profile)
        < lib.index(clear_state_dir)
        < lib.index(resolve_command)
    )
    assert lib.index(resolve_command) < lib.index(restore_state_dir)


def test_supervisor_skip_running_checks_gateway_state_not_exit_code() -> None:
    start_all = (SUPERVISOR / "start-all.ps1").read_text(encoding="utf-8")

    assert "ConvertFrom-Json -ErrorAction SilentlyContinue" in start_all
    assert "[string]$parsed.state -eq 'running'" in start_all
    assert "if ($status.ExitCode -eq 0)" not in start_all


def test_supervisor_ports_are_persisted_per_profile() -> None:
    lib = (SUPERVISOR / "lib.ps1").read_text(encoding="utf-8")

    assert "$Script:PORT_FILE_NAME = 'supervisor-port.txt'" in lib
    assert "Get-ProfilePortFile" in lib
    assert "Get-UsedProfilePorts" in lib
    assert "Set-Content -LiteralPath $portFile" in lib
    assert "$BasePort + $index" not in lib
