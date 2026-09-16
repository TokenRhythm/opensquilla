"""Offline runner-level E2E coverage for code-task scratch mode."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from opensquilla.contrib.codetask import config, runner, verification
from opensquilla.contrib.codetask.types import AgentOutcome, TaskState
from opensquilla.paths import default_opensquilla_home
from opensquilla.recovery.errors import ProfileLockBusyError
from opensquilla.recovery.locking import ProfileOperationLock
from opensquilla.runtime_packs import get_runtime_pack_service


class _OfflineAdapter:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def run(self, prompt, *, repo: Path, scratch_dir: Path, artifact_dir: Path):
        repo.mkdir(parents=True, exist_ok=True)
        scratch_dir.mkdir(parents=True, exist_ok=True)
        artifact_dir.mkdir(parents=True, exist_ok=True)

        (repo / "calc.py").write_text(
            "def add(a, b):\n"
            "    return a + b\n",
            encoding="utf-8",
        )
        (repo / "test_calc.py").write_text(
            "from calc import add\n\n\n"
            "def test_add():\n"
            "    assert add(1, 2) == 3\n",
            encoding="utf-8",
        )
        (scratch_dir / config.VERIFICATION_MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "testable": True,
                    "acceptance_tests": [
                        {
                            "name": "pytest",
                            "command": (
                                f"{shlex.quote(verification._bash_path_entry(Path(sys.executable)))}"
                                " -m pytest -q"
                            ),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        (artifact_dir / "agent_stdout.log").write_text(
            "offline adapter wrote calc.py and test_calc.py\n",
            encoding="utf-8",
        )
        return AgentOutcome(
            success=True,
            timeout=False,
            exit_code=0,
            finish_reason="stop",
            usage={"total_tokens": 0, "model": "offline"},
            duration_seconds=0.0,
        )


@contextmanager
def _profile_writer(home: Path):
    """Hold the profile from another process, as the Desktop Gateway does."""
    code = (
        "import sys\n"
        "from opensquilla.recovery.locking import ProfileOperationLock\n"
        "with ProfileOperationLock(sys.argv[1]):\n"
        "    print('locked', flush=True)\n"
        "    sys.stdin.read()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(home)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "locked"
        yield proc
    finally:
        proc.communicate(timeout=10)
        assert proc.returncode == 0


def test_desktop_scratch_runner_works_while_primary_profile_is_locked(monkeypatch, tmp_path):
    desktop_home = tmp_path / "Desktop Data" / "profile"
    desktop_home.mkdir(parents=True)
    config_path = desktop_home / "config.toml"
    config_path.write_text('[llm]\nprovider="ollama"\nmodel="offline-model"\n')
    (desktop_home / "existing.txt").write_text("synthetic existing data")
    nested_data = desktop_home / "existing-data" / "nested"
    nested_data.mkdir(parents=True)
    (nested_data / "original.txt").write_bytes(b"synthetic nested data")
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(desktop_home))
    monkeypatch.setenv("OPENSQUILLA_PROFILE_KIND", "desktop-primary")
    monkeypatch.setenv("OPENSQUILLA_DESKTOP", "1")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_STATE_DIR", raising=False)
    monkeypatch.delenv("OPENSQUILLA_RUNTIME_PACKS_ROOT", raising=False)
    monkeypatch.delenv("OPENSQUILLA_CODETASK_RUNS_DIR", raising=False)
    monkeypatch.setenv("OPENSQUILLA_CODETASK_SCRATCH_DIR", str(tmp_path / "scratch"))
    # Separate first-time Runtime Pack initialization from code-task writes:
    # Windows Git/Bash discovery initializes it even when selecting a host tool.
    runtime_service = get_runtime_pack_service()
    assert runtime_service.root == desktop_home / "state" / "runtime-packs" / "v1"

    def profile_snapshot() -> dict[Path, bytes | None]:
        # Directory entries stay in the snapshot so new directories also fail
        # the final comparison instead of being silently omitted.
        return {
            path.relative_to(desktop_home): None if path.is_dir() else path.read_bytes()
            for path in desktop_home.rglob("*")
        }

    before = profile_snapshot()

    class _DesktopAdapter(_OfflineAdapter):
        def run(self, prompt, *, repo, scratch_dir, artifact_dir):
            assert self.kwargs["agent_config"].source_path == str(config_path)
            assert self.kwargs["agent_config"].payload["llm"]["model"] == "offline-model"
            assert not artifact_dir.is_relative_to(desktop_home)
            with pytest.raises(ProfileLockBusyError), ProfileOperationLock(desktop_home):
                pass
            return super().run(
                prompt, repo=repo, scratch_dir=scratch_dir, artifact_dir=artifact_dir
            )

    monkeypatch.setattr(runner, "LocalAdapter", _DesktopAdapter)
    with _profile_writer(desktop_home) as writer:
        result = runner.solve(
            task="create a tested add function",
            verification_mode="scratch",
            run_id="desktop-scratch",
            timeout=600,
            max_attempts=1,
        )
        assert writer.poll() is None
        with pytest.raises(ProfileLockBusyError), ProfileOperationLock(desktop_home):
            pass

    assert result.state is TaskState.VERIFIED
    assert Path(result.artifact_dir) == desktop_home.with_name("profile-code-task") / (
        "code-task/desktop-scratch"
    )
    after = profile_snapshot()
    assert after == before


@pytest.mark.parametrize("target", ["runs", "scratch", "build_workspace", "source_repo"])
def test_desktop_explicit_profile_writes_still_require_primary_lease(
    monkeypatch, tmp_path, target
):
    desktop_home = tmp_path / "desktop-profile"
    desktop_home.mkdir()
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(desktop_home))
    monkeypatch.setenv("OPENSQUILLA_PROFILE_KIND", "desktop-primary")
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    for name in ("RUNS", "SCRATCH", "WORKSPACE"):
        monkeypatch.delenv(f"OPENSQUILLA_CODETASK_{name}_DIR", raising=False)
    repo = ""
    if target == "source_repo":
        repo = str(desktop_home / "source-repo")
    else:
        name = {"runs": "RUNS", "scratch": "SCRATCH", "build_workspace": "WORKSPACE"}[target]
        monkeypatch.setenv(f"OPENSQUILLA_CODETASK_{name}_DIR", str(desktop_home / target))

    def must_not_run(**kwargs):
        raise AssertionError("must not start profile writes without its lease")

    monkeypatch.setattr(runner, "_solve_unlocked", must_not_run)
    with _profile_writer(desktop_home):
        with pytest.raises(ProfileLockBusyError):
            runner.solve(task="synthetic task", repo=repo, verification_mode="build")
    assert list(desktop_home.iterdir()) == []


def test_scratch_runner_e2e_offline_adapter_verifies(monkeypatch, tmp_path) -> None:
    run_id = "codetask-offline-e2e"
    runs_dir = tmp_path / "runs"
    monkeypatch.setenv("OPENSQUILLA_CODETASK_RUNS_DIR", str(runs_dir))
    monkeypatch.setattr(runner, "LocalAdapter", _OfflineAdapter)

    result = runner.solve(
        task="create a tested add function",
        verification_mode="scratch",
        run_id=run_id,
        timeout=600,
        max_attempts=1,
    )

    assert result.state is TaskState.VERIFIED
    assert result.verified is True
    assert result.verification_kind == "scratch"
    assert result.attempts == 1
    assert result.files_changed >= 2
    assert result.acceptance
    assert result.acceptance[0].after == "pass"

    run_dir = runs_dir / run_id
    assert Path(result.artifact_dir or "").is_dir()
    assert result.artifact_dir == str(run_dir)
    assert Path(result.patch_path or "").is_file()
    assert (run_dir / "result.json").is_file()
    assert (run_dir / "prompt.txt").is_file()
    assert (run_dir / config.VERIFICATION_MANIFEST_NAME).is_file()
    assert (run_dir / "attempts" / "01" / "change.patch").is_file()
    assert (run_dir / "repo" / "calc.py").is_file()


def test_runner_holds_profile_lock_while_adapter_runs(monkeypatch, tmp_path) -> None:
    run_id = "codetask-lock-e2e"
    runs_dir = tmp_path / "runs"
    observed: list[str] = []
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("OPENSQUILLA_CODETASK_RUNS_DIR", str(runs_dir))
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))

    class _LockProbeAdapter(_OfflineAdapter):
        def run(self, prompt, *, repo: Path, scratch_dir: Path, artifact_dir: Path):
            result: list[str] = []

            def contend() -> None:
                try:
                    with ProfileOperationLock(default_opensquilla_home(), timeout=0.05):
                        result.append("acquired")
                except ProfileLockBusyError:
                    result.append("busy")

            thread = threading.Thread(target=contend)
            thread.start()
            thread.join(timeout=2)
            assert not thread.is_alive()
            observed.extend(result)
            return super().run(
                prompt,
                repo=repo,
                scratch_dir=scratch_dir,
                artifact_dir=artifact_dir,
            )

    monkeypatch.setattr(runner, "LocalAdapter", _LockProbeAdapter)

    result = runner.solve(
        task="create a tested add function",
        verification_mode="scratch",
        run_id=run_id,
        timeout=600,
        max_attempts=1,
    )

    assert result.state is TaskState.VERIFIED
    assert observed == ["busy"]
