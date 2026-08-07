from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.skills import eligibility, runtime_env
from opensquilla.skills.hub import deps
from opensquilla.skills.meta.executors import skill_exec
from opensquilla.skills.meta.types import MetaStep
from opensquilla.skills.runtime_env import MEDIA_FONTS_DIR_ENV
from opensquilla.skills.toolchains import ActiveComponentStatus, DownloadVerificationError
from opensquilla.skills.toolchains.manager import (
    managed_toolchain_state_scope,
    toolchains_root,
)
from opensquilla.skills.types import (
    SkillInstallSpec,
    SkillLayer,
    SkillPlatformMeta,
    SkillRequires,
    SkillSpec,
)


@pytest.mark.asyncio
async def test_toolchain_dependency_installer_runs_catalog_component_in_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    progress: list[tuple[str, int, int]] = []

    def fake_install(component_id: str, *, progress_cb=None) -> SimpleNamespace:
        calls.append(component_id)
        assert progress_cb is not None
        progress_cb(25, 100)
        return SimpleNamespace(version="test-v1")

    monkeypatch.setattr(deps, "install_component", fake_install)
    spec = SkillInstallSpec(
        kind="toolchain", id="paper-tex", bins=["xelatex", "bibtex"]
    )
    results = await deps.install_deps(
        [spec],
        progress_cb=lambda item, current, total: progress.append(
            (item.id, current, total)
        ),
    )
    result = results[0]

    assert calls == ["paper-tex"]
    assert progress == [("paper-tex", 25, 100)]
    assert result.success is True
    assert result.identifier == "paper-tex"
    assert "test-v1" in result.message


@pytest.mark.asyncio
async def test_toolchain_dependency_installer_reports_verification_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_install(_component_id: str) -> None:
        raise DownloadVerificationError("digest mismatch")

    monkeypatch.setattr(deps, "install_component", fail_install)
    result = await deps.install_toolchain(
        SkillInstallSpec(kind="toolchain", id="media-ffmpeg")
    )

    assert result.success is False
    assert "integrity verification" in result.message
    assert "not activated" in result.message


@pytest.mark.asyncio
async def test_toolchain_dependency_installer_has_actionable_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow_install(_component_id: str) -> None:
        time.sleep(0.03)

    monkeypatch.setattr(deps, "install_component", slow_install)
    monkeypatch.setattr(deps, "_TOOLCHAIN_INSTALL_TIMEOUT_SECONDS", 0.001)
    result = await deps.install_toolchain(
        SkillInstallSpec(kind="toolchain", id="media-ffmpeg")
    )

    assert result.success is False
    assert "timed out" in result.message.lower()
    assert "retry" in result.message


@pytest.mark.asyncio
async def test_toolchain_dependency_installer_single_flights_same_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    release = threading.Event()

    def install_once(_component_id: str, *, progress_cb=None) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        release.wait(2)
        return SimpleNamespace(version="test-v1")

    monkeypatch.setattr(deps, "install_component", install_once)
    deps._TOOLCHAIN_INSTALL_TASKS.clear()
    spec = SkillInstallSpec(kind="toolchain", id="paper-tex")
    first = asyncio.create_task(deps.install_toolchain(spec))
    second = asyncio.create_task(deps.install_toolchain(spec))
    await asyncio.sleep(0)
    release.set()

    results = await asyncio.gather(first, second)

    assert calls == 1
    assert all(result.success for result in results)


@pytest.mark.asyncio
async def test_toolchain_dependency_installer_separates_single_flight_by_state_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[Path] = []
    calls_lock = threading.Lock()
    both_started = threading.Event()
    release = threading.Event()

    def install_once(_component_id: str, *, progress_cb=None) -> SimpleNamespace:
        with calls_lock:
            calls.append(toolchains_root())
            if len(calls) == 2:
                both_started.set()
        release.wait(2)
        return SimpleNamespace(version="test-v1")

    monkeypatch.setattr(deps, "install_component", install_once)
    deps._TOOLCHAIN_INSTALL_TASKS.clear()
    spec = SkillInstallSpec(kind="toolchain", id="paper-tex")
    states = (tmp_path / "state-a", tmp_path / "state-b")

    async def _install_under(state: Path) -> deps.DepResult:
        with managed_toolchain_state_scope(state):
            return await deps.install_toolchain(spec)

    tasks = [asyncio.create_task(_install_under(state)) for state in states]
    try:
        started_twice = await asyncio.wait_for(
            asyncio.to_thread(both_started.wait, 1),
            timeout=2,
        )
    finally:
        release.set()
    results = await asyncio.gather(*tasks)

    assert started_twice is True
    assert sorted(calls) == sorted(state / "toolchains" / "v1" for state in states)
    assert all(result.success for result in results)


def test_eligibility_accepts_receipted_managed_binary_and_rejects_manifest_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_resolve(name: str) -> Path | None:
        calls.append(name)
        return Path("/managed/bin/xelatex") if name == "xelatex" else None

    monkeypatch.setattr(eligibility.shutil, "which", lambda _name: None)
    monkeypatch.setattr(eligibility, "resolve_managed_binary", fake_resolve)
    spec = SkillSpec(
        name="paper",
        description="test",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=[],
        content="",
        metadata=SkillPlatformMeta(requires=SkillRequires(bins=["xelatex"])),
    )

    assert eligibility.check_eligibility(spec, eligibility.EligibilityContext.auto()) is True
    assert calls == ["xelatex"]
    assert (
        eligibility._has_bin("/tmp/manifest-controlled", eligibility.EligibilityContext())
        is False
    )
    assert calls == ["xelatex"]


def test_managed_skill_env_appends_bins_and_sets_verified_font_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    managed_bin = tmp_path / "managed" / "bin"
    managed_bin.mkdir(parents=True)
    font = tmp_path / "managed" / "fonts" / "NotoSansCJK-Regular.ttc"
    font.parent.mkdir(parents=True)
    font.write_bytes(b"font")

    monkeypatch.setattr(
        runtime_env,
        "managed_env",
        lambda base: {
            **base,
            "PATH": str(managed_bin) + os.pathsep + base["PATH"],
            MEDIA_FONTS_DIR_ENV: str(font.parent),
        },
    )

    result = runtime_env.managed_skill_env({"PATH": "/system/bin"})

    assert result["PATH"].split(os.pathsep) == [str(managed_bin), "/system/bin"]
    assert result[MEDIA_FONTS_DIR_ENV] == str(font.parent)


def test_managed_skill_env_preserves_operator_font_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_env, "managed_env", lambda base: dict(base))
    result = runtime_env.managed_skill_env(
        {"PATH": "/system/bin", MEDIA_FONTS_DIR_ENV: "/operator/fonts"}
    )
    assert result[MEDIA_FONTS_DIR_ENV] == "/operator/fonts"


def test_managed_skill_env_preserves_explicit_empty_font_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_env, "managed_env", lambda base: dict(base))

    result = runtime_env.managed_skill_env(
        {"PATH": "/system/bin", MEDIA_FONTS_DIR_ENV: ""}
    )

    assert result[MEDIA_FONTS_DIR_ENV] == ""


def test_toolchain_inventory_is_sanitized_and_reports_active_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    status = ActiveComponentStatus(
        component_id="media-ffmpeg",
        version="1.0",
        platform_key="test-x64",
        install_backend="archive",
        supported=True,
        active=True,
    )
    monkeypatch.setattr(
        runtime_env,
        "list_active_components",
        lambda *, root=None: (status,),
    )

    result = runtime_env.managed_toolchain_inventory(root=tmp_path / "state")

    assert result == [
        {
            "component_id": "media-ffmpeg",
            "version": "1.0",
            "platform_key": "test-x64",
            "install_backend": "archive",
            "supported": True,
            "active": True,
        }
    ]
    assert "verified-managed" not in str(result)


@pytest.mark.asyncio
async def test_skill_exec_receives_managed_runtime_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = SimpleNamespace(
        base_dir=str(tmp_path),
        entrypoint={"command": "python", "parse": "text"},
    )
    loader = SimpleNamespace(get_by_name=lambda _name: spec)
    captured: dict[str, object] = {}

    def fake_env(base: object) -> dict[str, str]:
        assert isinstance(base, dict)
        assert base is not os.environ
        assert base.get("PATH") == os.environ.get("PATH")
        assert "PYTEST_CURRENT_TEST" not in base
        return {"PATH": "/managed:/system", MEDIA_FONTS_DIR_ENV: "/managed/fonts"}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout=b"ok\n", stderr=b"")

    monkeypatch.setattr(skill_exec, "managed_skill_env", fake_env)
    monkeypatch.setattr(skill_exec.subprocess, "run", fake_run)

    output = await skill_exec.run_skill_exec_step(
        MetaStep(id="run", skill="fake", kind="skill_exec"),
        "fake",
        {},
        {},
        skill_loader=loader,
        workspace_dir=str(tmp_path),
    )

    assert output == "ok"
    expected_env = {
        "PATH": "/managed:/system",
        MEDIA_FONTS_DIR_ENV: "/managed/fonts",
    }
    if os.name == "nt":
        expected_env.update({
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        })
    assert captured["env"] == expected_env


@pytest.mark.parametrize("font_override", ["/operator/fonts", ""])
@pytest.mark.asyncio
async def test_skill_exec_preserves_explicit_operator_font_environment(
    font_override: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = SimpleNamespace(
        base_dir=str(tmp_path),
        entrypoint={"command": "python", "parse": "text"},
    )
    loader = SimpleNamespace(get_by_name=lambda _name: spec)
    captured: dict[str, str] = {}

    def passthrough_env(base: object) -> dict[str, str]:
        assert isinstance(base, dict)
        assert base[MEDIA_FONTS_DIR_ENV] == font_override
        return dict(base)

    def fake_run(_argv: list[str], **kwargs: object) -> SimpleNamespace:
        env = kwargs["env"]
        assert isinstance(env, dict)
        captured[MEDIA_FONTS_DIR_ENV] = env[MEDIA_FONTS_DIR_ENV]
        return SimpleNamespace(returncode=0, stdout=b"ok\n", stderr=b"")

    monkeypatch.setenv(MEDIA_FONTS_DIR_ENV, font_override)
    monkeypatch.setattr(skill_exec, "managed_skill_env", passthrough_env)
    monkeypatch.setattr(skill_exec.subprocess, "run", fake_run)

    output = await skill_exec.run_skill_exec_step(
        MetaStep(id="run", skill="fake", kind="skill_exec"),
        "fake",
        {},
        {},
        skill_loader=loader,
        workspace_dir=str(tmp_path),
    )

    assert output == "ok"
    assert captured[MEDIA_FONTS_DIR_ENV] == font_override


def test_skill_exec_normalizes_nested_base_dir_separators_for_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(skill_exec.os, "sep", "\\")

    assert skill_exec._normalize_base_dir_argument(
        r"C:\runtime\paper/scripts/run.py",
        r"C:\runtime\paper",
    ) == r"C:\runtime\paper\scripts\run.py"
