from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "build_wheelhouse_zip.py"
REPO_ROOT = SCRIPT_PATH.parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "wheelhouse-release.yml"
INTERNAL_RELEASE_MARKERS = (
    "INTERNAL_ORG_NAME",
    "github.com/internal-org/opensquilla",
    ".internal/evidence",
    "INTERNAL_RELEASE_NOTE.md",
    "LOCAL_AGENT_NOTES.md",
)


def assert_executable_on_posix(path: Path) -> None:
    if os.name != "nt":
        assert path.stat().st_mode & stat.S_IXUSR


def load_script():
    spec = importlib.util.spec_from_file_location("build_wheelhouse_zip", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_build_wheel_retries_once_after_transient_uv_failure(monkeypatch, tmp_path: Path) -> None:
    module = load_script()
    wheel_path = tmp_path / "wheels" / "opensquilla-0.1.0-py3-none-any.whl"
    calls = []

    def fake_run(args, *, cwd, env):
        calls.append((args, cwd, env))
        if len(calls) == 1:
            raise subprocess.CalledProcessError(4294967295, args)

    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    monkeypatch.setattr(module, "find_built_wheel", lambda wheel_dir: wheel_path)

    assert (
        module.build_wheel(tmp_path, tmp_path / "wheels", {"UV_CACHE_DIR": "cache"})
        == wheel_path
    )
    assert len(calls) == 2


def test_build_subprocess_env_keeps_uv_cache_outside_repo_root(tmp_path: Path) -> None:
    module = load_script()
    repo_root = tmp_path / "repo"
    work_dir = repo_root / "build" / "wheelhouse-zip"
    repo_root.mkdir()

    env = module.build_subprocess_env(work_dir, repo_root=repo_root)

    uv_cache = Path(env["UV_CACHE_DIR"]).resolve()
    pip_cache = Path(env["PIP_CACHE_DIR"]).resolve()
    assert not uv_cache.is_relative_to(repo_root.resolve())
    assert not pip_cache.is_relative_to(repo_root.resolve())
    assert uv_cache.name == "uv-cache"
    assert pip_cache.name == "pip-cache"


def test_build_webui_checks_node_then_installs_and_builds(monkeypatch, tmp_path: Path) -> None:
    module = load_script()
    repo_root = tmp_path / "repo"
    webui_dir = repo_root / "opensquilla-webui"
    webui_dir.mkdir(parents=True)
    (webui_dir / ".node-version").write_text("22.12.0\n", encoding="utf-8")
    calls = []

    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda executable, *, path=None: f"/tools/{executable}",
    )

    def fake_subprocess_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout="v22.12.0\n")

    monkeypatch.setattr(module.subprocess, "run", fake_subprocess_run)

    module.build_webui(repo_root, {"PATH": "/tools"})

    assert [call[0] for call in calls] == [
        ["/tools/node", "--version"],
        ["/tools/npm", "ci"],
        ["/tools/npm", "run", "build"],
        ["/tools/npm", "run", "verify:release-dist"],
    ]
    assert calls[1][1]["cwd"] == webui_dir
    assert calls[2][1]["cwd"] == webui_dir
    assert calls[3][1]["cwd"] == webui_dir


def test_build_webui_rejects_node_older_than_pinned_minimum(
    monkeypatch, tmp_path: Path
) -> None:
    module = load_script()
    repo_root = tmp_path / "repo"
    webui_dir = repo_root / "opensquilla-webui"
    webui_dir.mkdir(parents=True)
    (webui_dir / ".node-version").write_text("22.12.0\n", encoding="utf-8")

    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda executable, *, path=None: f"/tools/{executable}",
    )
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="v20.19.0\n"),
    )

    with pytest.raises(SystemExit, match=r"requires Node\.js >= 22\.12\.0"):
        module.build_webui(repo_root, {"PATH": "/tools"})


def test_portable_python_preflight_rejects_before_webui_build(monkeypatch) -> None:
    module = load_script()
    webui_calls = []

    class Python313:
        major = 3
        minor = 13

        def __getitem__(self, key):
            return (3, 13, 0)[key]

    monkeypatch.setattr(
        module,
        "sys",
        SimpleNamespace(version_info=Python313()),
    )
    monkeypatch.setattr(module, "read_project_version", lambda repo_root: "0.1.0")
    monkeypatch.setattr(module, "build_subprocess_env", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        module,
        "build_webui",
        lambda *args, **kwargs: webui_calls.append((args, kwargs)),
    )

    with pytest.raises(SystemExit, match="require Python 3.12"):
        module.main(
            [
                "--profile",
                "core",
                "--skip-wheelhouse",
                "--bundle-python-runtime",
            ]
        )

    assert webui_calls == []


def test_release_name_records_platform_python_profile() -> None:
    module = load_script()

    wheelhouse_name = module.release_name(
        app_version="0.1.0",
        platform_tag="macos-arm64",
        python_major=3,
        python_minor=12,
        profile="recommended",
        portable=False,
    )
    portable_name = module.release_name(
        app_version="0.1.0",
        platform_tag="macos-arm64",
        python_major=3,
        python_minor=12,
        profile="recommended",
        portable=True,
    )

    assert wheelhouse_name == "OpenSquilla-0.1.0-macos-arm64-py312-recommended-wheelhouse"
    assert portable_name == "OpenSquilla-0.1.0-macos-arm64-py312-recommended-portable"


def test_python_runtime_asset_name_uses_platform_triple() -> None:
    module = load_script()

    macos = module.python_runtime_asset_name(
        python_version="3.12.13",
        runtime_release="20260414",
        platform_tag="macos-arm64",
    )
    windows = module.python_runtime_asset_name(
        python_version="3.12.13",
        runtime_release="20260414",
        platform_tag="windows-x64",
    )

    assert macos == (
        "cpython-3.12.13+20260414-aarch64-apple-darwin-install_only_stripped.tar.gz"
    )
    assert windows == (
        "cpython-3.12.13+20260414-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
    )


def test_cross_platform_wheelhouse_requires_target_host(tmp_path: Path) -> None:
    module = load_script()
    module.platform_tag = lambda: "linux-x64"
    wheel_path = tmp_path / "opensquilla-0.1.0-py3-none-any.whl"
    package_dir = tmp_path / "packages"

    with pytest.raises(SystemExit, match="must run on the target platform"):
        module.build_wheelhouse_command(
            package_dir,
            wheel_path,
            "recommended",
            target_platform_tag="windows-x64",
            python_major=3,
            python_minor=12,
        )


def test_portable_recommended_wheelhouse_uses_recommended_extra_only(tmp_path: Path) -> None:
    module = load_script()
    module.platform_tag = lambda: "windows-x64"
    wheel_path = tmp_path / "opensquilla-0.1.0-py3-none-any.whl"
    package_dir = tmp_path / "packages"

    command = module.build_wheelhouse_command(
        package_dir,
        wheel_path,
        "recommended",
        target_platform_tag="windows-x64",
        python_major=3,
        python_minor=12,
    )

    assert str(wheel_path) + "[recommended]" in command
    assert str(wheel_path) + "[recommended,feishu]" not in command

def test_release_wheel_allows_router_provenance_markdown() -> None:
    module = load_script()
    provenance = module.ROUTER_PROVENANCE_WHEEL_PATH
    tokenjuice_provenance = module.TOKENJUICE_PROVENANCE_WHEEL_PATH
    pptx_reference = "opensquilla/skills/bundled/pptx/references/python_pptx.md"
    unrelated_skill_reference = (
        "opensquilla/skills/bundled/example/references/private-notes.md"
    )
    skill_readme = "opensquilla/skills/bundled/filesystem/README.md"
    skill_license = "opensquilla/skills/bundled/filesystem/LICENSE.md"
    skill_card = "opensquilla/skills/bundled/filesystem/skill-card.md"
    unrelated_router_doc = (
        "opensquilla/squilla_router/models/v4.2_phase3_inference/README.md"
    )

    violations = module.forbidden_release_wheel_entries(
        (
            provenance,
            tokenjuice_provenance,
            unrelated_router_doc,
            "opensquilla/skills/bundled/example/SKILL.md",
            pptx_reference,
            unrelated_skill_reference,
            skill_readme,
            skill_license,
            skill_card,
        )
    )

    assert provenance not in violations
    assert tokenjuice_provenance not in violations
    assert "opensquilla/skills/bundled/example/SKILL.md" not in violations
    assert pptx_reference not in violations
    assert unrelated_router_doc in violations
    assert unrelated_skill_reference in violations
    assert skill_readme in violations
    assert skill_license in violations
    assert skill_card in violations


def test_pyproject_release_wheel_config_excludes_forbidden_skill_resources() -> None:
    module = load_script()
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel_config = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    excludes = set(wheel_config.get("exclude", []))
    force_includes = wheel_config.get("force-include", {})

    assert "src/opensquilla/skills/bundled/**/THIRD_PARTY_NOTICES.md" in excludes
    assert "src/opensquilla/skills/bundled/**/README.md" in excludes
    assert "src/opensquilla/skills/bundled/**/LICENSE.md" in excludes
    assert "src/opensquilla/skills/bundled/**/skill-card.md" in excludes
    assert "src/opensquilla/skills/bundled/**/references/*.md" in excludes
    assert "src/opensquilla/skills/exp/**" in excludes
    assert "src/opensquilla/skills/meta/META_SKILL_AUTHORING.md" in excludes
    assert module.forbidden_release_wheel_entries(tuple(force_includes.values())) == []


def test_required_router_assets_include_provenance_and_manifest() -> None:
    module = load_script()

    assert "v4.2_phase3_inference/PROVENANCE.md" in module.ROUTER_ASSET_RELS
    assert "v4.2_phase3_inference/artifact_manifest.json" in module.ROUTER_ASSET_RELS


def test_project_release_metadata_avoids_internal_repository_markers() -> None:
    for rel_path in ("pyproject.toml", "README.release.md", "LICENSE"):
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        for marker in INTERNAL_RELEASE_MARKERS:
            assert marker not in text


def test_public_release_docs_avoid_private_kol_language() -> None:
    for rel_path in ("README.md",):
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8").lower()
        assert "kol" not in text
        assert "private" not in text


def test_release_wheel_content_scanner_flags_internal_markers(tmp_path: Path) -> None:
    module = load_script()
    wheel_path = tmp_path / "opensquilla-0.1.0-py3-none-any.whl"

    with ZipFile(wheel_path, "w") as archive:
        archive.writestr("opensquilla/__init__.py", "__version__ = '0.1.0'\n")
        archive.writestr(
            "opensquilla-0.1.0.dist-info/METADATA",
            "\n".join(
                [
                    "Author: INTERNAL_ORG_NAME",
                    "Project-URL: Repository, https://github.com/internal-org/opensquilla",
                    "",
                ]
            ),
        )

    assert module.forbidden_release_text_hits(wheel_path) == [
        "opensquilla-0.1.0.dist-info/METADATA: INTERNAL_ORG_NAME",
        "opensquilla-0.1.0.dist-info/METADATA: github.com/internal-org/opensquilla",
    ]


def test_install_scripts_install_from_local_wheelhouse_and_run_onboarding() -> None:
    module = load_script()

    sh_script = module.render_install_sh(
        wheel_name="opensquilla-0.1.0-py3-none-any.whl",
        profile="recommended",
        python_major=3,
        python_minor=12,
    )
    ps_script = module.render_install_ps1(
        wheel_name="opensquilla-0.1.0-py3-none-any.whl",
        profile="recommended",
        python_major=3,
        python_minor=12,
    )

    assert 'PACKAGE_DIR="${SCRIPT_DIR}/packages"' in sh_script
    assert 'REQUIRED_PYTHON_MINOR=12' in sh_script
    assert "uv tool install" in sh_script
    assert '--find-links "${PACKAGE_DIR}"' in sh_script
    assert '"${PACKAGE_DIR}/opensquilla-0.1.0-py3-none-any.whl[recommended]"' in sh_script
    assert '"${OPENSQUILLA_BIN}" onboard' in sh_script
    assert '"${OPENSQUILLA_BIN}" onboard --if-needed' in sh_script
    assert "opensquilla gateway run" in sh_script

    assert "$PackageDir = Join-Path $ScriptDir 'packages'" in ps_script
    assert "$RequiredPythonMinor = 12" in ps_script
    assert "uv tool install" in ps_script
    assert "--find-links" in ps_script
    assert "opensquilla-0.1.0-py3-none-any.whl[recommended]" in ps_script
    assert "& $OpenSquillaBin onboard --if-needed" in ps_script
    assert 'throw "OpenSquilla installation failed with exit code $LASTEXITCODE."' in ps_script
    assert 'throw "OpenSquilla onboarding failed with exit code $LASTEXITCODE."' in ps_script
    assert "opensquilla gateway run" in ps_script


def test_start_scripts_use_bundled_python_runtime() -> None:
    module = load_script()

    sh_script = module.render_start_sh()
    ps_script = module.render_start_ps1()
    cmd_script = module.render_start_cmd()

    assert sh_script.startswith('#!/bin/sh\nif [ -z "${BASH_VERSION:-}" ]; then')
    assert 'exec /usr/bin/env bash "$0" "$@"' in sh_script
    assert 'PYTHON_BIN="${SCRIPT_DIR}/runtime/python/bin/python3"' in sh_script
    assert "OPENSQUILLA_WHEEL=" in sh_script
    assert "WHEEL_HASH=" in sh_script
    assert 'VENV_DIR="${SCRIPT_DIR}/.venv-${WHEEL_HASH}"' in sh_script
    assert "--without-pip" in sh_script
    assert (
        'export PATH="${SCRIPT_DIR}:${VENV_DIR}/bin:${SCRIPT_DIR}/runtime/python/bin:${PATH}"'
        in sh_script
    )
    assert (
        'PORTABLE_DATA_DIR="${OPENSQUILLA_PORTABLE_HOME:-${DATA_BASE}/OpenSquilla/'
        'portable/${RELEASE_ID}}"' in sh_script
    )
    assert 'if [[ -z "${OPENSQUILLA_GATEWAY_CONFIG_PATH:-}" ]]; then' in sh_script
    assert (
        'export OPENSQUILLA_GATEWAY_CONFIG_PATH="${PORTABLE_DATA_DIR}/config.toml"'
        in sh_script
    )
    assert (
        'if [[ -z "${OPENSQUILLA_LLM_API_KEY:-}" && -n "${OPENROUTER_API_KEY:-}" ]]; then'
        in sh_script
    )
    assert 'export OPENSQUILLA_STATE_DIR="${PORTABLE_DATA_DIR}"' in sh_script
    assert (
        'export OPENSQUILLA_GATEWAY_STATE_DIR="${OPENSQUILLA_STATE_DIR}/state"'
        in sh_script
    )
    assert (
        'export OPENSQUILLA_GATEWAY_WORKSPACE_DIR="${OPENSQUILLA_STATE_DIR}/workspace"'
        in sh_script
    )
    assert 'mkdir -p "${OPENSQUILLA_STATE_DIR}"' in sh_script
    assert '"${PYTHON_BIN}" -m venv --without-pip "${VENV_DIR}"' in sh_script
    assert "-m pip install" not in sh_script
    assert "Installing OpenSquilla from bundled wheels..." in sh_script
    assert 'OPENSQUILLA_MODULE=( "-m" "opensquilla.cli.main" )' in sh_script
    assert (
        'if [[ ! -f "${OPENSQUILLA_GATEWAY_CONFIG_PATH}" && '
        '-n "${OPENROUTER_API_KEY:-}" ]]; then' in sh_script
    )
    assert (
        '"${OPENSQUILLA_BIN}" "${OPENSQUILLA_MODULE[@]}" onboard \\\n'
        "    --provider openrouter" in sh_script
    )
    assert "--api-key-env OPENROUTER_API_KEY" in sh_script
    assert "--minimal" in sh_script
    assert '"${OPENSQUILLA_BIN}" "${OPENSQUILLA_MODULE[@]}" onboard' in sh_script
    assert '"${OPENSQUILLA_BIN}" onboard --if-needed' not in sh_script
    assert "if [[ -t 1 ]]; then" in sh_script
    assert 'exec "${OPENSQUILLA_BIN}" "${OPENSQUILLA_MODULE[@]}" gateway run' in sh_script
    assert "else" in sh_script
    assert 'CONSOLE_LOG="${OPENSQUILLA_STATE_DIR}/logs/gateway-console.log"' in sh_script
    assert 'tee -a "${CONSOLE_LOG}"' in sh_script
    assert sh_script.index("if [[ -t 1 ]]; then") < sh_script.index(
        'tee -a "${CONSOLE_LOG}"'
    )
    assert sh_script.index(
        'export OPENSQUILLA_GATEWAY_CONFIG_PATH="${PORTABLE_DATA_DIR}/config.toml"'
    ) < sh_script.index('"${OPENSQUILLA_BIN}" "${OPENSQUILLA_MODULE[@]}" onboard')

    assert "$PythonBin = Join-Path $ScriptDir 'runtime\\python\\python.exe'" in ps_script
    assert "$OpenSquillaWheel = Get-ChildItem -Path $PackageDir" in ps_script
    assert "$WheelHashFull = -join" in ps_script
    assert "$WheelHash = $WheelHashFull.Substring(0, 12).ToLowerInvariant()" in ps_script
    assert "Get-FileHash" not in ps_script
    assert "[System.IO.File]::OpenRead($OpenSquillaWheel.FullName)" in ps_script
    assert "$Sha256.ComputeHash($WheelStream)" in ps_script
    assert "$VenvDir = Join-Path $VenvRoot $ReleaseId" in ps_script
    assert '$env:PATH = "$VenvDir\\Scripts;$env:PATH"' in ps_script
    assert 'Join-Path $VenvBase "OpenSquilla\\portable\\$ReleaseId"' in ps_script
    assert (
        "$env:OPENSQUILLA_GATEWAY_CONFIG_PATH = Join-Path $PortableDataDir 'config.toml'"
        in ps_script
    )
    assert "$env:OPENSQUILLA_LLM_API_KEY = $env:OPENROUTER_API_KEY" in ps_script
    assert "$env:OPENSQUILLA_STATE_DIR = $PortableDataDir" in ps_script
    assert (
        "$env:OPENSQUILLA_GATEWAY_STATE_DIR = Join-Path "
        "$env:OPENSQUILLA_STATE_DIR 'state'" in ps_script
    )
    assert (
        "$env:OPENSQUILLA_GATEWAY_WORKSPACE_DIR = Join-Path "
        "$env:OPENSQUILLA_STATE_DIR 'workspace'" in ps_script
    )
    assert "New-Item -ItemType Directory -Path $env:OPENSQUILLA_STATE_DIR -Force" in ps_script
    assert "& $PythonBin -m venv --without-pip $VenvDir" in ps_script
    assert "-m pip install" not in ps_script
    assert "-c $WheelInstallScript" not in ps_script
    assert "$WheelInstallScript | & $PythonBin - $PackageDir $SitePackages" in ps_script
    assert "Installing OpenSquilla from bundled wheels..." in ps_script
    assert '$OpenSquillaArgs = @("-m", "opensquilla.cli.main")' in ps_script
    assert (
        "if ((-not (Test-Path $env:OPENSQUILLA_GATEWAY_CONFIG_PATH)) "
        "-and $env:OPENROUTER_API_KEY) {" in ps_script
    )
    assert "& $VenvPython @OpenSquillaArgs onboard `" in ps_script
    assert "--provider openrouter `" in ps_script
    assert "--api-key-env OPENROUTER_API_KEY `" in ps_script
    assert "& $VenvPython @OpenSquillaArgs onboard" in ps_script
    assert "& $OpenSquillaBin onboard --if-needed" not in ps_script
    assert "OpenSquilla environment creation failed" in ps_script
    assert "OpenSquilla installation failed" not in ps_script
    assert 'throw "OpenSquilla onboarding failed with exit code $LASTEXITCODE."' in ps_script
    assert "$OutputRedirected = [Console]::IsOutputRedirected" in ps_script
    assert "if (-not $OutputRedirected) {" in ps_script
    assert "& $VenvPython @OpenSquillaArgs gateway run" in ps_script
    assert "$ConsoleLog = Join-Path $LogDir 'gateway-console.log'" in ps_script
    assert "$PreviousErrorActionPreference = $ErrorActionPreference" in ps_script
    assert "$ErrorActionPreference = \"Continue\"" in ps_script
    assert "$_ -is [System.Management.Automation.ErrorRecord]" in ps_script
    assert "Tee-Object -FilePath $ConsoleLog -Append" in ps_script
    assert ps_script.index("if (-not $OutputRedirected) {") < ps_script.index(
        "Tee-Object -FilePath $ConsoleLog -Append"
    )
    assert ps_script.index(
        "$env:OPENSQUILLA_GATEWAY_CONFIG_PATH = Join-Path $PortableDataDir 'config.toml'"
    ) < ps_script.index("& $VenvPython @OpenSquillaArgs onboard")
    assert ps_script.index(
        "$env:OPENSQUILLA_GATEWAY_STATE_DIR = Join-Path "
        "$env:OPENSQUILLA_STATE_DIR 'state'"
    ) < ps_script.index("& $VenvPython @OpenSquillaArgs onboard")

    assert cmd_script == (
        "@echo off\r\n"
        "title OpenSquilla Gateway\r\n"
        'cd /d "%~dp0"\r\n'
        'set "OSQ_POWERSHELL=powershell.exe"\r\n'
        'where pwsh.exe >nul 2>nul && set "OSQ_POWERSHELL=pwsh.exe"\r\n'
        '"%OSQ_POWERSHELL%" -NoLogo -NoExit -NoProfile -ExecutionPolicy Bypass '
        '-File "%~dp0start.ps1"\r\n'
    )


def test_install_script_reexecs_under_bash_before_pipefail() -> None:
    module = load_script()

    script = module.render_install_sh(
        wheel_name="opensquilla-0.1.0-py3-none-any.whl",
        profile="recommended",
        python_major=3,
        python_minor=12,
    )

    assert script.startswith('#!/bin/sh\nif [ -z "${BASH_VERSION:-}" ]; then')
    assert 'exec /usr/bin/env bash "$0" "$@"' in script
    assert script.index('exec /usr/bin/env bash "$0" "$@"') < script.index(
        "set -euo pipefail"
    )


def test_render_readme_is_platform_specific_for_windows_portable() -> None:
    module = load_script()

    readme = module.render_readme(
        app_version="0.1.0",
        profile="recommended",
        platform_tag="windows-x64",
        python_major=3,
        python_minor=12,
        portable=True,
    )

    assert "## Windows" in readme
    assert "# OpenSquilla 0.1.0 Portable Release" in readme
    assert "Wheelhouse Release" not in readme
    assert "Right-click `Start OpenSquilla.cmd`" in readme
    assert "Run as administrator" in readme
    assert "Smart App Control" in readme
    assert ".\\start.ps1" in readme
    assert "## macOS / Linux" not in readme
    assert "bash start.sh" not in readme
    assert "Python is bundled in this zip." in readme
    assert "Complete onboarding." in readme
    assert "Feishu" not in readme
    assert "Advanced portable usage" in readme
    assert "OPENROUTER_API_KEY" in readme
    assert "writes an OpenRouter env-reference config" in readme
    assert "supported portable launch\n  path is administrator launch" in readme
    assert "Microsoft documents that SmartScreen checks downloaded apps" in readme
    assert "skip setup when it is complete" not in readme
    assert "does not install a global `opensquilla` command" in readme
    assert (
        "Config, workspace, logs, memory, and runtime state use the normal "
        "user-level OpenSquilla directory." in readme
    )


def test_render_readme_is_platform_specific_for_macos_portable() -> None:
    module = load_script()

    readme = module.render_readme(
        app_version="0.1.0",
        profile="recommended",
        platform_tag="macos-arm64",
        python_major=3,
        python_minor=12,
        portable=True,
    )

    assert "## macOS / Linux" in readme
    assert "# OpenSquilla 0.1.0 Portable Release" in readme
    assert "Wheelhouse Release" not in readme
    assert "bash start.sh" in readme
    assert "## Windows PowerShell" not in readme
    assert ".\\start.ps1" not in readme
    assert "Python is bundled in this zip." in readme
    assert "Complete onboarding." in readme
    assert "Feishu" not in readme
    assert "later starts let you review or change the config" in readme
    assert "skip setup when it is complete" not in readme
    assert "does not install a global `opensquilla` command" not in readme
    assert (
        "Config, workspace, logs, memory, and runtime state use the normal "
        "user-level OpenSquilla directory." in readme
    )
    assert ".opensquilla/config.toml" not in readme


def test_prepare_release_tree_writes_user_surface_and_manifest(tmp_path: Path) -> None:
    module = load_script()
    release_root = tmp_path / "OpenSquilla-0.1.0-macos-arm64-py312-recommended-wheelhouse"
    wheel_path = tmp_path / "opensquilla-0.1.0-py3-none-any.whl"
    wheel_path.write_bytes(b"wheel")

    bundled_wheel = module.prepare_release_tree(
        release_root,
        wheel_path,
        app_version="0.1.0",
        profile="recommended",
        platform_tag="macos-arm64",
        python_major=3,
        python_minor=12,
        include_router_assets=True,
        portable=False,
        runtime_release="",
        runtime_asset="",
    )

    assert bundled_wheel == release_root / "packages" / wheel_path.name
    assert bundled_wheel.read_bytes() == b"wheel"
    assert (release_root / "README.md").is_file()
    assert (release_root / "install.sh").is_file()
    assert (release_root / "install.ps1").is_file()
    assert (release_root / "LICENSE").is_file()
    assert (release_root / "THIRD_PARTY_NOTICES.md").is_file()
    assert not (release_root / "runtime").exists()
    assert not (release_root / "start.sh").exists()
    assert not (release_root / "start.ps1").exists()
    assert (release_root / "manifest.json").is_file()
    assert_executable_on_posix(release_root / "install.sh")
    readme = (release_root / "README.md").read_text(encoding="utf-8")
    manifest = (release_root / "manifest.json").read_text(encoding="utf-8")
    assert "bash install.sh" in readme
    assert ".\\install.ps1" not in readme
    assert "Build target:" in readme
    assert "Configuration" not in readme
    assert "Notes" not in readme
    assert "Git repository" not in readme
    assert '"platform_tag": "macos-arm64"' in manifest
    assert '"profile": "recommended"' in manifest
    assert '"include_router_assets": true' in manifest


def test_prepare_portable_release_tree_includes_runtime_and_start_scripts(tmp_path: Path) -> None:
    module = load_script()
    release_root = tmp_path / "OpenSquilla-0.1.0-macos-arm64-py312-recommended-portable"
    wheel_path = tmp_path / "opensquilla-0.1.0-py3-none-any.whl"
    runtime_root = tmp_path / "runtime"
    (runtime_root / "bin").mkdir(parents=True)
    (runtime_root / "bin" / "python3").write_text("python", encoding="utf-8")
    (runtime_root / "Lib" / "__pycache__").mkdir(parents=True)
    (runtime_root / "Lib" / "module.py").write_text("print('ok')\n", encoding="utf-8")
    (runtime_root / "Lib" / "__pycache__" / "module.cpython-312.pyc").write_bytes(b"cache")
    wheel_path.write_bytes(b"wheel")

    module.prepare_release_tree(
        release_root,
        wheel_path,
        app_version="0.1.0",
        profile="recommended",
        platform_tag="macos-arm64",
        python_major=3,
        python_minor=12,
        include_router_assets=True,
        portable=True,
        runtime_release="20260414",
        runtime_asset="cpython-3.12.13+20260414-aarch64-apple-darwin-install_only_stripped.tar.gz",
        runtime_root=runtime_root,
    )

    assert (release_root / "runtime" / "python" / "bin" / "python3").is_file()
    assert (release_root / "runtime" / "python" / "Lib" / "module.py").is_file()
    assert not (release_root / "runtime" / "python" / "Lib" / "__pycache__").exists()
    assert (release_root / "start.sh").is_file()
    assert (release_root / "start.ps1").is_file()
    assert "opensquilla.cli.main" in (release_root / "start.sh").read_text(encoding="utf-8")
    assert "opensquilla.cli.main" in (release_root / "start.ps1").read_text(
        encoding="utf-8"
    )
    assert not (release_root / "Start OpenSquilla.cmd").exists()
    assert (release_root / "LICENSE").is_file()
    assert (release_root / "THIRD_PARTY_NOTICES.md").is_file()
    assert not (release_root / "install.sh").exists()
    assert not (release_root / "install.ps1").exists()
    assert_executable_on_posix(release_root / "start.sh")
    manifest = (release_root / "manifest.json").read_text(encoding="utf-8")
    assert '"portable": true' in manifest
    assert '"runtime_release": "20260414"' in manifest
    assert "install_only_stripped.tar.gz" in manifest


def test_prune_portable_runtime_removes_packaging_tools_and_bytecode(
    tmp_path: Path,
) -> None:
    module = load_script()
    runtime_root = tmp_path / "runtime"
    site_packages = runtime_root / "Lib" / "site-packages"
    long_license_path = (
        site_packages
        / "pip-26.0.1.dist-info"
        / "licenses"
        / "src"
        / "pip"
        / "_vendor"
        / "dependency_groups"
    )
    long_license_path.mkdir(parents=True)
    (long_license_path / "LICENSE.txt").write_text("license\n", encoding="utf-8")
    for name in ("pip", "setuptools", "wheel", "_distutils_hack", "pkg_resources"):
        package = site_packages / name
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
    for name in ("setuptools-80.0.0.dist-info", "wheel-0.45.0.dist-info"):
        dist_info = site_packages / name
        dist_info.mkdir(parents=True)
        (dist_info / "METADATA").write_text("Name: test\n", encoding="utf-8")
    (site_packages / "opensquilla_runtime_dep").mkdir(parents=True)
    (site_packages / "opensquilla_runtime_dep" / "__init__.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    pycache = runtime_root / "Lib" / "__pycache__"
    pycache.mkdir(parents=True)
    (pycache / "module.cpython-312.pyc").write_bytes(b"cache")

    module.prune_portable_runtime(runtime_root)

    assert not (site_packages / "pip").exists()
    assert not (site_packages / "pip-26.0.1.dist-info").exists()
    assert not (site_packages / "setuptools").exists()
    assert not (site_packages / "setuptools-80.0.0.dist-info").exists()
    assert not (site_packages / "wheel").exists()
    assert not (site_packages / "wheel-0.45.0.dist-info").exists()
    assert not (site_packages / "_distutils_hack").exists()
    assert not (site_packages / "pkg_resources").exists()
    assert not pycache.exists()
    assert (site_packages / "opensquilla_runtime_dep" / "__init__.py").is_file()


def test_prepare_windows_portable_release_tree_includes_double_click_launcher(
    tmp_path: Path,
) -> None:
    module = load_script()
    release_root = tmp_path / "OpenSquilla-0.1.0-windows-x64-py312-recommended-portable"
    wheel_path = tmp_path / "opensquilla-0.1.0-py3-none-any.whl"
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    (runtime_root / "python.exe").write_text("python", encoding="utf-8")
    wheel_path.write_bytes(b"wheel")

    module.prepare_release_tree(
        release_root,
        wheel_path,
        app_version="0.1.0",
        profile="recommended",
        platform_tag="windows-x64",
        python_major=3,
        python_minor=12,
        include_router_assets=True,
        portable=True,
        runtime_release="20260414",
        runtime_asset="cpython-3.12.13+20260414-x86_64-pc-windows-msvc-install_only_stripped.tar.gz",
        runtime_root=runtime_root,
    )

    launcher = release_root / "Start OpenSquilla.cmd"
    assert launcher.is_file()
    assert launcher.read_bytes() == module.render_start_cmd().encode("utf-8")
    cli = release_root / "opensquilla.cmd"
    assert cli.is_file()
    cli_text = cli.read_text(encoding="utf-8")
    assert "start.ps1\" -Cli %*" in cli_text
    shell = release_root / "OpenSquilla Shell.cmd"
    assert shell.is_file()
    shell_text = shell.read_text(encoding="utf-8")
    assert "function global:opensquilla" in shell_text
    assert "opensquilla.cmd" in shell_text
    readme = (release_root / "README.md").read_text(encoding="utf-8")
    assert "Right-click `Start OpenSquilla.cmd`" in readme
    assert "Run as administrator" in readme
    assert "Smart App Control" in readme
    assert "run\n`OpenSquilla Shell.cmd`" in readme
    assert ".\\opensquilla.cmd onboard" in readme
    assert "Closing it stops the gateway." in readme
    assert "Advanced portable usage" in readme
    start_ps1 = (release_root / "start.ps1").read_text(encoding="utf-8")
    assert "Test-WindowsVCRedistInstalled" in start_ps1
    assert "RuntimeInformation]::IsOSPlatform" in start_ps1
    assert "$RequiresRouterRuntime = $true" in start_ps1
    assert '"opensquilla[recommended,feishu]" -notmatch' not in start_ps1
    assert "OPENSQUILLA_SKIP_VC_REDIST" in start_ps1
    assert "Microsoft.VCRedist.2015+.x64" in start_ps1
    assert "https://aka.ms/vs/17/release/vc_redist.x64.exe" in start_ps1
    assert "safe router fallback" in start_ps1
    assert "If automatic installation fails, install it manually" in start_ps1
    assert "After installing, reopen PowerShell and restart OpenSquilla" in start_ps1
    assert "$env:PYTHONUTF8 = '1'" in start_ps1
    assert "$env:PYTHONIOENCODING = 'utf-8:replace'" in start_ps1


def test_install_portable_wheelhouse_preinstalls_into_bundled_python(
    tmp_path: Path,
) -> None:
    module = load_script()
    release_root = tmp_path / "release"
    package_dir = release_root / "packages"
    site_packages = release_root / "runtime" / "python" / "Lib" / "site-packages"
    package_dir.mkdir(parents=True)
    site_packages.mkdir(parents=True)
    wheel_path = package_dir / "demo-0.1.0-py3-none-any.whl"
    with ZipFile(wheel_path, "w") as wheel:
        wheel.writestr("demo_pkg/__init__.py", "VALUE = 1\n")
        wheel.writestr("demo-0.1.0.dist-info/METADATA", "Name: demo\n")
        wheel.writestr("demo-0.1.0.data/purelib/demo_extra.py", "EXTRA = 2\n")
        wheel.writestr("demo-0.1.0.data/scripts/demo-script.py", "print('skip')\n")

    module.install_portable_wheelhouse(release_root)

    assert (site_packages / "demo_pkg" / "__init__.py").read_text(encoding="utf-8") == (
        "VALUE = 1\n"
    )
    assert (site_packages / "demo_extra.py").read_text(encoding="utf-8") == "EXTRA = 2\n"
    assert not (site_packages / "demo-script.py").exists()


def test_create_zip_contains_release_directory_and_preserves_install_mode(tmp_path: Path) -> None:
    module = load_script()
    release_root = tmp_path / "OpenSquilla-0.1.0-macos-arm64-py312-recommended-wheelhouse"
    packages = release_root / "packages"
    packages.mkdir(parents=True)
    (packages / "opensquilla-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
    install_script = release_root / "install.sh"
    install_script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    install_script.chmod(0o755)
    (release_root / "install.ps1").write_text("Write-Host ok\n", encoding="utf-8")
    (release_root / "README.md").write_text("readme\n", encoding="utf-8")
    (release_root / "manifest.json").write_text("{}\n", encoding="utf-8")
    zip_path = tmp_path / "release.zip"

    module.create_zip(release_root, zip_path)

    with ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        install_info = archive.getinfo(
            "OpenSquilla-0.1.0-macos-arm64-py312-recommended-wheelhouse/install.sh"
        )

    assert names == {
        "OpenSquilla-0.1.0-macos-arm64-py312-recommended-wheelhouse/README.md",
        "OpenSquilla-0.1.0-macos-arm64-py312-recommended-wheelhouse/install.ps1",
        "OpenSquilla-0.1.0-macos-arm64-py312-recommended-wheelhouse/install.sh",
        "OpenSquilla-0.1.0-macos-arm64-py312-recommended-wheelhouse/manifest.json",
        "OpenSquilla-0.1.0-macos-arm64-py312-recommended-wheelhouse/packages/opensquilla-0.1.0-py3-none-any.whl",
    }
    assert stat.S_IMODE(install_info.external_attr >> 16) & stat.S_IXUSR


def test_create_zip_preserves_runtime_executable_mode(tmp_path: Path) -> None:
    module = load_script()
    release_root = tmp_path / "OpenSquilla-0.1.0-macos-arm64-py312-recommended-portable"
    python_bin = release_root / "runtime" / "python" / "bin" / "python3"
    python_bin.parent.mkdir(parents=True)
    python_bin.write_bytes(b"python")
    python_bin.chmod(0o755)
    (release_root / "start.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (release_root / "manifest.json").write_text("{}\n", encoding="utf-8")
    zip_path = tmp_path / "release.zip"

    module.create_zip(release_root, zip_path)

    with ZipFile(zip_path) as archive:
        python_info = archive.getinfo(
            "OpenSquilla-0.1.0-macos-arm64-py312-recommended-portable/"
            "runtime/python/bin/python3"
        )

    assert stat.S_IMODE(python_info.external_attr >> 16) & stat.S_IXUSR


def test_create_zip_can_use_short_archive_root(tmp_path: Path) -> None:
    module = load_script()
    release_root = tmp_path / "OpenSquilla-0.1.0-windows-x64-py312-recommended-portable"
    (release_root / "runtime" / "python").mkdir(parents=True)
    (release_root / "runtime" / "python" / "python.exe").write_bytes(b"python")
    zip_path = tmp_path / "release.zip"

    module.create_zip(release_root, zip_path, archive_root="OpenSquilla-0.1.0")

    with ZipFile(zip_path) as archive:
        assert archive.namelist() == ["OpenSquilla-0.1.0/runtime/python/python.exe"]


def test_write_sha256s_records_all_release_zips(tmp_path: Path) -> None:
    module = load_script()
    first = tmp_path / "OpenSquilla-0.1.0-linux-x64-py312-recommended-portable.zip"
    second = tmp_path / "OpenSquilla-0.1.0-linux-x64-py312-recommended-wheelhouse.zip"
    first.write_bytes(b"portable")
    second.write_bytes(b"wheelhouse")

    checksum_path = module.write_sha256s((second, first), tmp_path / "SHA256SUMS")

    expected = [
        f"{module.sha256_digest(first)}  {first.name}",
        f"{module.sha256_digest(second)}  {second.name}",
    ]
    assert checksum_path == tmp_path / "SHA256SUMS"
    assert checksum_path.read_text(encoding="utf-8").splitlines() == expected


def test_release_workflow_publishes_wheel_and_electron_assets_without_portable() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "concurrency:" in workflow
    assert "release-assets-${{" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "timeout-minutes: 90" in workflow
    assert workflow.count("timeout-minutes: 150") == 2
    assert workflow.count("timeout-minutes: 75") == 2
    assert "timeout-minutes: 20" in workflow
    assert "build-desktop-macos:" in workflow
    assert "build-desktop-windows:" in workflow
    assert "Validate workflow inputs" in workflow
    assert "python_runtime_release" not in workflow
    assert "python_runtime_version" not in workflow
    assert "persist-credentials: false" in workflow
    assert "bundle_python_runtime:" not in workflow
    assert "--platform-tag windows-x64" not in workflow
    assert "platform_tag: macos-arm64" not in workflow
    assert "platform_tag: linux-x64" not in workflow
    assert "for mode in portable wheelhouse" not in workflow
    assert "--bundle-python-runtime" not in workflow
    assert "uv build --wheel --out-dir dist" in workflow
    assert "expected one versioned portable zip" not in workflow
    assert "expected one versioned wheel" in workflow
    assert "manifest[\"portable\"] is True" not in workflow
    assert "SHA256SUMS" in workflow
    assert "manifest.version" not in workflow
    assert "GH_REPO: ${{ github.repository }}" in workflow
    assert "0.5+ release assets must not include Windows portable zips" in workflow
    assert "if not is_prerelease:" not in workflow
    assert "OpenSquilla-windows-x64-portable.zip" not in workflow
    assert "desktop_asset_version" in workflow
    assert "OpenSquilla-{desktop_version}-mac-arm64.dmg" in workflow
    assert "OpenSquilla-{desktop_version}-win-x64.exe" in workflow
    assert "opensquilla-latest-py3-none-any.whl" not in workflow
    assert "gh release upload \"${TAG}\" dist/* --clobber" in workflow
    assert "dist/*.zip dist/*.zip.sha256 dist/SHA256SUMS" not in workflow
    assert "Git LFS pointer leaked into wheel" in workflow
    assert "Verify GitHub Release assets" in workflow
    assert "release\", \"delete-asset\", tag, name, \"--yes\"" in workflow
    assert "name.endswith(\".sha256\")" in workflow
    assert '["gh", "release", "view", tag, "--json", "assets"]' in workflow
    assert "Unexpected GitHub Release assets" in workflow
    assert "\"unexpected\": unexpected" in workflow
    assert "zip_path.stem" not in workflow
    assert "archive_roots =" not in workflow
    assert "root = archive_roots[0] + \"/\"" not in workflow


def test_release_workflow_publishes_from_version_tags() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "tags:" in workflow
    assert '- "v*"' in workflow
    assert "contents: write" in workflow
    assert "RELEASE_TAG:" in workflow
    assert "RELEASE_PROFILE:" in workflow
    assert "github.ref_name" in workflow
    assert "github.event.inputs.tag" in workflow
    assert "github.event_name == 'push' || github.event.inputs.tag != ''" in workflow
    assert "TAG: ${{ env.RELEASE_TAG }}" in workflow
