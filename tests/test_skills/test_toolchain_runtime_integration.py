from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.git_runtime import GitCapability, GitCapabilityState
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


class _FakeOwnedProcess:
    pid = 4242
    returncode = 0

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        assert input is None
        return b"ok\n", b""


def _mock_skill_exec_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def fake_spawn(*argv: str, **kwargs: Any) -> _FakeOwnedProcess:
        captured.update(argv=argv, kwargs=kwargs)
        return _FakeOwnedProcess()

    monkeypatch.setattr(skill_exec, "create_owned_subprocess_exec", fake_spawn)
    return captured


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

    def fake_env(base: object) -> dict[str, str]:
        assert isinstance(base, dict)
        assert base is not os.environ
        assert base.get("PATH") == os.environ.get("PATH")
        assert "PYTEST_CURRENT_TEST" not in base
        return {"PATH": "/managed:/system", MEDIA_FONTS_DIR_ENV: "/managed/fonts"}

    monkeypatch.setattr(skill_exec, "managed_skill_env", fake_env)
    spawned = _mock_skill_exec_launcher(monkeypatch)

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
    assert spawned["argv"] == (sys.executable,)
    assert spawned["kwargs"]["env"] == expected_env


@pytest.mark.asyncio
async def test_skill_exec_pins_resolved_git_ahead_of_apple_shim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = SimpleNamespace(
        base_dir=str(tmp_path),
        entrypoint={"command": "python", "parse": "text"},
        metadata=SimpleNamespace(requires=SimpleNamespace(bins=["git"])),
    )
    loader = SimpleNamespace(get_by_name=lambda _name: spec)
    safe_git = Path("/opt/homebrew/bin/git")
    monkeypatch.setattr(
        skill_exec,
        "resolve_git_capability",
        lambda: GitCapability(
            state=GitCapabilityState.AVAILABLE,
            executable=safe_git,
            source="host",
        ),
    )
    monkeypatch.setattr(
        skill_exec,
        "managed_skill_env",
        lambda _base: {"PATH": f"/usr/bin{os.pathsep}{safe_git.parent}"},
    )
    spawned = _mock_skill_exec_launcher(monkeypatch)

    output = await skill_exec.run_skill_exec_step(
        MetaStep(id="run", skill="fake", kind="skill_exec"),
        "fake",
        {},
        {},
        skill_loader=loader,
        workspace_dir=str(tmp_path),
    )

    assert output == "ok"
    assert spawned["kwargs"]["env"]["PATH"] == (
        f"{safe_git.parent}{os.pathsep}/usr/bin"
    )


@pytest.mark.asyncio
async def test_git_skill_exec_fails_before_spawn_when_git_becomes_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = SimpleNamespace(
        base_dir=str(tmp_path),
        entrypoint={"command": "python", "parse": "text"},
        metadata=SimpleNamespace(requires=SimpleNamespace(bins=["git"])),
    )
    loader = SimpleNamespace(get_by_name=lambda _name: spec)
    monkeypatch.setattr(
        skill_exec,
        "resolve_git_capability",
        lambda: GitCapability(
            state=GitCapabilityState.UNAVAILABLE,
            reason="git_not_found",
        ),
    )
    monkeypatch.setattr(
        skill_exec,
        "create_owned_subprocess_exec",
        lambda *_args, **_kwargs: pytest.fail("unavailable Git must not spawn a skill"),
    )

    with pytest.raises(RuntimeError, match=r"^GIT_UNAVAILABLE:"):
        await skill_exec.run_skill_exec_step(
            MetaStep(id="run", skill="fake", kind="skill_exec"),
            "fake",
            {},
            {},
            skill_loader=loader,
            workspace_dir=str(tmp_path),
        )


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

    def passthrough_env(base: object) -> dict[str, str]:
        assert isinstance(base, dict)
        assert base[MEDIA_FONTS_DIR_ENV] == font_override
        return dict(base)

    monkeypatch.setenv(MEDIA_FONTS_DIR_ENV, font_override)
    monkeypatch.setattr(skill_exec, "managed_skill_env", passthrough_env)
    spawned = _mock_skill_exec_launcher(monkeypatch)

    output = await skill_exec.run_skill_exec_step(
        MetaStep(id="run", skill="fake", kind="skill_exec"),
        "fake",
        {},
        {},
        skill_loader=loader,
        workspace_dir=str(tmp_path),
    )

    assert output == "ok"
    env = spawned["kwargs"]["env"]
    assert isinstance(env, dict)
    assert env[MEDIA_FONTS_DIR_ENV] == font_override


def test_skill_exec_normalizes_nested_base_dir_separators_for_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(skill_exec.os, "sep", "\\")

    assert skill_exec._normalize_base_dir_argument(
        r"C:\runtime\paper/scripts/run.py",
        r"C:\runtime\paper",
    ) == r"C:\runtime\paper\scripts\run.py"
