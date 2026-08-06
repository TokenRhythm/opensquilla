"""Offline E2E for setup-gated manual meta-skill launch.

The test keeps the production boundaries from the bundled skill loader through
the gateway RPCs, managed installer, activation receipt, readiness re-check,
and one-shot launch marker. Only the external HTTPS response and executable
behavior are synthetic, so the test is deterministic and credential-free.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.steps.meta_command import (
    pending_meta_launch_peek,
    pending_meta_launch_pop,
)
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError
from opensquilla.gateway.rpc_meta_runs import (
    _META_SETUP_JOBS,
    _META_SETUP_LATEST,
    _META_SETUP_TASKS,
    _handle_meta_run,
    _handle_meta_setup_install,
    _handle_meta_setup_status,
)
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.paths import default_bundled_skills_dir
from opensquilla.skills.toolchains import invalidate_probe_cache, probe_component
from opensquilla.skills.toolchains import manager as toolchain_manager
from opensquilla.skills.toolchains import registry as toolchain_registry
from opensquilla.skills.toolchains import runtime as toolchain_runtime
from opensquilla.skills.toolchains.registry import ToolchainDescriptor

_META_NAME = "meta-paper-write"
_SESSION_KEY = "agent:test:meta-setup-e2e"
_ACTION_ID = f"{_META_NAME}:paper-tex"


class _OfflineResponse(io.BytesIO):
    """Minimal urllib response backed by a pinned in-memory archive."""

    def __init__(self, payload: bytes, url: str) -> None:
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload))}
        self._url = url

    def geturl(self) -> str:
        return self._url


class _StableShutil:
    """Delegate filesystem work while making binary discovery deterministic."""

    def __init__(self, real_module: Any) -> None:
        self._real = real_module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    @staticmethod
    def which(name: str, mode: int = os.F_OK | os.X_OK, path: str | None = None) -> str | None:
        del mode
        if path is None:
            # Python-backed bundled helpers remain available without consulting
            # the host PATH. TeX is deliberately absent until managed activation.
            return "/synthetic/python" if name in {"python", "python3"} else None

        suffixes = ("", ".exe", ".bat", ".cmd") if os.name == "nt" else ("",)
        for directory in path.split(os.pathsep):
            if not directory:
                continue
            for suffix in suffixes:
                candidate = Path(directory) / f"{name}{suffix}"
                if candidate.is_file():
                    return str(candidate)
        return None


def _add_tar_member(archive: tarfile.TarFile, name: str, data: bytes, mode: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = 0
    archive.addfile(info, io.BytesIO(data))


def _build_synthetic_tex_archive(path: Path) -> bytes:
    """Create a safe TinyTeX-shaped payload with two executable probe stubs."""

    with tarfile.open(path, mode="w:xz") as archive:
        for directory in ("TinyTeX", "TinyTeX/bin", "TinyTeX/bin/e2e"):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = 0
            archive.addfile(info)
        executable = b"offline managed-toolchain probe\n"
        _add_tar_member(archive, "TinyTeX/bin/e2e/xelatex", executable, 0o755)
        _add_tar_member(archive, "TinyTeX/bin/e2e/bibtex", executable, 0o755)
    return path.read_bytes()


def _descriptor(payload: bytes) -> ToolchainDescriptor:
    return ToolchainDescriptor(
        component_id="paper-tex",
        display_name="Offline paper toolchain fixture",
        version="e2e-1",
        platform_key="offline-e2e",
        supported=True,
        unsupported_reason=None,
        url="https://fixtures.invalid/paper-tex.tar.xz",
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        install_backend="archive",
        brew_formula=None,
        archive_type="tar.xz",
        archive_root="TinyTeX",
        bin_relpaths=("TinyTeX/bin/e2e",),
        probe_commands=(("xelatex", "--version"), ("bibtex", "--version")),
        post_install=None,
        package_closure=(),
        auxiliary_assets=(),
        license="test-only",
        license_url="https://fixtures.invalid/license",
        source="https://fixtures.invalid/source",
        closure_source=None,
        notes="Synthetic offline E2E fixture; never shipped.",
    )


@pytest.mark.asyncio
async def test_meta_paper_setup_gates_launch_until_confirmed_offline_install_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launch marker appears only after confirmed setup and verified readiness."""

    _META_SETUP_JOBS.clear()
    _META_SETUP_LATEST.clear()
    _META_SETUP_TASKS.clear()
    pending_meta_launch_pop(_SESSION_KEY)
    invalidate_probe_cache()

    toolchain_root = tmp_path / "managed-toolchains"
    archive_payload = _build_synthetic_tex_archive(tmp_path / "paper-tex.tar.xz")
    descriptor = _descriptor(archive_payload)
    original_describe = toolchain_registry.describe_component

    def describe(component_id: str, **kwargs: Any) -> ToolchainDescriptor:
        if component_id == "paper-tex":
            return descriptor
        return original_describe(component_id, **kwargs)

    def use_test_root(root: Path | None = None) -> Path:
        return Path(root) if root is not None else toolchain_root

    downloads: list[str] = []

    def urlopen(request: Any, timeout: float) -> _OfflineResponse:
        assert timeout == 60
        assert request.full_url == descriptor.url
        downloads.append(request.full_url)
        return _OfflineResponse(archive_payload, descriptor.url or "")

    real_subprocess = subprocess

    def run_probe(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        assert Path(command[0]).name.lower() in {"xelatex", "bibtex"}
        return real_subprocess.CompletedProcess(command, 0, b"offline probe ok\n", b"")

    probe_subprocess = SimpleNamespace(
        run=run_probe,
        SubprocessError=real_subprocess.SubprocessError,
    )
    stable_shutil = _StableShutil(toolchain_manager.shutil)

    monkeypatch.setattr(toolchain_registry, "describe_component", describe)
    monkeypatch.setattr("opensquilla.skills.toolchains.describe_component", describe)
    monkeypatch.setattr(toolchain_manager, "toolchains_root", use_test_root)
    monkeypatch.setattr(toolchain_runtime, "toolchains_root", use_test_root)
    monkeypatch.setattr(toolchain_manager.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(toolchain_manager, "subprocess", probe_subprocess)
    monkeypatch.setattr(toolchain_manager, "shutil", stable_shutil)
    monkeypatch.setattr(toolchain_runtime, "shutil", stable_shutil)

    loader = SkillLoader(
        bundled_dir=default_bundled_skills_dir(),
        snapshot_path=tmp_path / "bundled-skills-snapshot.json",
    )
    spec = loader.get_by_name(_META_NAME)
    assert spec is not None and spec.kind == "meta"
    assert spec.metadata is not None
    assert any(item.id == "paper-tex" for item in spec.metadata.install)

    config = SimpleNamespace(
        meta_skill=SimpleNamespace(enabled=True, auto_trigger=False),
    )
    ctx = RpcContext(conn_id="offline-meta-setup-e2e", config=config, skill_loader=loader)

    try:
        capability_before = probe_component("paper-tex")
        assert capability_before.ready is False

        blocked = await _handle_meta_run(
            {"name": _META_NAME, "sessionKey": _SESSION_KEY},
            ctx,
        )
        assert blocked["code"] == "META_SKILL_SETUP_REQUIRED"
        assert blocked["readiness"]["missing_bins"] == ["bibtex", "xelatex"]
        assert [action["id"] for action in blocked["readiness"]["setup_actions"]] == [
            _ACTION_ID
        ]
        assert pending_meta_launch_peek(_SESSION_KEY) is None

        with pytest.raises(RpcHandlerError, match="confirmed=true"):
            await _handle_meta_setup_install(
                {
                    "name": _META_NAME,
                    "sessionKey": _SESSION_KEY,
                    "confirmed": False,
                    "action_ids": [_ACTION_ID],
                },
                ctx,
            )

        assert _META_SETUP_JOBS == {}
        assert _META_SETUP_LATEST == {}
        assert not list(toolchain_root.rglob(".opensquilla-toolchain.json"))
        assert pending_meta_launch_peek(_SESSION_KEY) is None

        started = await _handle_meta_setup_install(
            {
                "name": _META_NAME,
                "sessionKey": _SESSION_KEY,
                "confirmed": True,
                "action_ids": [_ACTION_ID],
            },
            ctx,
        )
        assert started["ok"] is True
        assert started["reused"] is False
        assert started["job"]["status"] in {"queued", "running"}
        assert pending_meta_launch_peek(_SESSION_KEY) is None

        setup_tasks = tuple(_META_SETUP_TASKS)
        assert len(setup_tasks) == 1
        await asyncio.gather(*setup_tasks)

        status = await _handle_meta_setup_status(
            {
                "jobId": started["job"]["job_id"],
                "sessionKey": _SESSION_KEY,
            },
            ctx,
        )
        assert status["job"]["status"] == "completed"
        assert status["job"]["phase"] == "completed"
        assert status["job"]["completed_actions"] == [_ACTION_ID]
        assert status["job"]["readiness"]["ready"] is True
        assert status["job"]["readiness"]["missing_bins"] == []
        assert pending_meta_launch_peek(_SESSION_KEY) is None

        capability_after = probe_component("paper-tex")
        assert capability_after.ready is True
        assert set(capability_after.binaries) == {"bibtex", "xelatex"}
        assert downloads == [descriptor.url]
        assert (toolchain_root / "active" / "paper-tex.json").is_file()
        assert len(list(toolchain_root.rglob(".opensquilla-toolchain.json"))) == 1

        launched = await _handle_meta_run(
            {"name": _META_NAME, "sessionKey": _SESSION_KEY},
            ctx,
        )
        assert launched == {
            "ok": True,
            "name": _META_NAME,
            "sessionKey": _SESSION_KEY,
        }
        assert pending_meta_launch_pop(_SESSION_KEY) == _META_NAME
        assert pending_meta_launch_pop(_SESSION_KEY) is None
    finally:
        for task in tuple(_META_SETUP_TASKS):
            if not task.done():
                task.cancel()
        if _META_SETUP_TASKS:
            await asyncio.gather(
                *tuple(_META_SETUP_TASKS),
                return_exceptions=True,
            )
        _META_SETUP_JOBS.clear()
        _META_SETUP_LATEST.clear()
        _META_SETUP_TASKS.clear()
        pending_meta_launch_pop(_SESSION_KEY)
        invalidate_probe_cache()
