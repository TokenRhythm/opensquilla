"""OpenSquilla CLI adapter for SWE-bench evaluation.

Wraps `opensquilla agent -m` CLI calls with structured result handling
and timeout management.

Architecture: OpenSquilla runs INSIDE the SWE-bench Docker container via
bind-mounted standalone Python + opensquilla venv. The CLI runs in standalone
mode (no gateway daemon) — `opensquilla agent` directly builds services and
runs a single TurnRunner pass.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from opensquilla.contrib.swebench.config import (
    DEFAULT_AGENT_TIMEOUT,
    DEFAULT_MODEL,
    DEFAULT_THINKING,
    container_pythonpath,
    python_bin,
)
from opensquilla.contrib.swebench.types import AgentResult

logger = logging.getLogger(__name__)

# Container-side paths the workspace bind-mounts into.
CONTAINER_OPENSQUILLA_CONFIG = "/opt/opensquilla-config/config.toml"
CONTAINER_OPENSQUILLA_STATE = "/tmp/opensquilla-state"

SUBPROCESS_TIMEOUT_BUFFER = 120

# Regex pulling balanced JSON objects out of stdout (OpenSquilla emits
# structured logs before the JSON; the JSON envelope is always the last
# top-level object).
_JSON_OBJECT_RE = re.compile(r"\{(?:[^{}]|(?:\{[^{}]*\}))*\}")


class OpenSquillaAdapter:
    """Drives OpenSquilla CLI inside containers and returns structured results.

    OpenSquilla runs inside the container via bind-mounted standalone Python
    and opensquilla venv. Each invocation uses :memory: session DB and
    --no-memory-capture for full isolation.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        timeout: int = DEFAULT_AGENT_TIMEOUT,
        thinking: str = DEFAULT_THINKING,
    ):
        self.model = model
        self.timeout = timeout
        self.thinking = thinking

    # ------------------------------------------------------------------
    # Agent lifecycle (no-ops — OpenSquilla CLI is stateless per invocation)
    # ------------------------------------------------------------------

    def create_agent(self, agent_id: str) -> None:
        pass

    def delete_agent(self, agent_id: str) -> None:
        pass

    def backup_session(self, agent_id: str, dest: Path) -> None:
        pass

    def switch_model(self, model_name: str) -> None:
        self.model = model_name
        logger.info("Model set to %s", model_name)

    # ------------------------------------------------------------------
    # Task execution
    # ------------------------------------------------------------------

    def send_task(
        self,
        prompt: str,
        agent_id: str,
        container_name: str,
        artifact_dir: Path | None = None,
    ) -> AgentResult:
        if artifact_dir:
            artifact_dir.mkdir(parents=True, exist_ok=True)

        stdout_path = artifact_dir / "agent_stdout.log" if artifact_dir else None
        stderr_path = artifact_dir / "agent_stderr.log" if artifact_dir else None
        transcript_path = "/tmp/opensquilla-transcript.jsonl"
        usage_path = "/tmp/opensquilla-usage.json"

        # Build argv for `opensquilla agent` inside the container.
        # Aligned with upstream maintainer's recommended automation profile
        # (feature/agent-automation-controls).
        argv: list[str] = [
            "opensquilla",
            "agent",
            "--message",
            prompt,
            "--workspace",
            "/testbed",
            "--no-memory-capture",
            "--session-db-path",
            ":memory:",
            "--json",
            "--timeout",
            str(self.timeout),
            "--max-iterations",
            "300",
            "--iteration-timeout-seconds",
            "600",
            "--max-provider-retries",
            "5",
            "--transcript-path",
            transcript_path,
            "--usage-path",
            usage_path,
            # Automation controls — maintainer-recommended:
            # 1. clean-room bootstrap that keeps AGENTS.md project rules
            #    (so "minimal change, don't touch tests" discipline survives)
            "--stateless",
            "--stateless-keep-project-rules",
            # 2. write containment — block writes outside /testbed and scratch
            "--workspace-lockdown",
            # 3. scratch dir for reproducers, debug scripts, candidate patches
            "--scratch-dir",
            "/tmp/squilla-scratch",
            # 4. unattended permissions: never prompt for approval
            "--permissions",
            "bypass",
        ]
        # When self.model / self.thinking are empty, router (config.toml
        # [squilla_router]) decides per-prompt; CLI override would shadow that.
        if self.model:
            argv += ["--model", self.model]
        if self.thinking:
            argv += ["--thinking", self.thinking]

        # Build inline Python that flips argv and invokes the Typer app.
        # This bypasses the venv's recorded shebang and works regardless
        # of the venv's recorded Python path.
        py_code = f"import sys\nsys.argv = {argv!r}\nfrom opensquilla.cli.main import app\napp()\n"

        # Secrets travel via a 0600 env-file: `docker exec -e KEY=value`
        # would expose the key in the host's process list.
        secret_env_path = _write_secret_env_file()
        cmd = [
            "docker",
            "exec",
            "--env-file",
            secret_env_path,
            "-e",
            f"PYTHONPATH={container_pythonpath()}",
            "-e",
            f"OPENSQUILLA_GATEWAY_CONFIG_PATH={CONTAINER_OPENSQUILLA_CONFIG}",
            "-e",
            f"OPENSQUILLA_STATE_DIR={CONTAINER_OPENSQUILLA_STATE}",
            container_name,
            python_bin(),
            "-c",
            py_code,
        ]

        start_time = time.time()
        timed_out = False

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout + SUBPROCESS_TIMEOUT_BUFFER,
            )
            exit_code = result.returncode
            stdout = result.stdout
            stderr = result.stderr
        except subprocess.TimeoutExpired as e:
            timed_out = True
            exit_code = -1
            stdout = _decode(e.stdout)
            stderr = _decode(e.stderr)
            logger.warning(
                "OpenSquilla subprocess timed out after %ds",
                self.timeout + SUBPROCESS_TIMEOUT_BUFFER,
            )
        finally:
            try:
                os.unlink(secret_env_path)
            except OSError:
                pass

        duration = time.time() - start_time

        # Pull transcript + usage files out of the container before cleanup.
        if artifact_dir:
            _save_container_file(container_name, transcript_path, artifact_dir / "transcript.jsonl")
            _save_container_file(container_name, usage_path, artifact_dir / "usage.json")

        # Defensive cleanup of anything OpenSquilla may have dropped into
        # /testbed before patch collection.
        _cleanup_opensquilla_metadata(container_name)

        if stdout_path:
            stdout_path.write_text(stdout, encoding="utf-8")
        if stderr_path:
            stderr_path.write_text(stderr, encoding="utf-8")

        envelope = _parse_json_envelope(stdout)

        if timed_out:
            finish_reason = "timeout"
        elif exit_code != 0:
            finish_reason = "error"
        elif envelope is None:
            finish_reason = "empty"
        else:
            status = envelope.get("status")
            text = (envelope.get("text") or "").strip()
            errors = envelope.get("errors") or []
            if status == "ok" and text:
                finish_reason = "stop"
            elif errors:
                finish_reason = "error"
            else:
                finish_reason = "empty"

        usage = (envelope or {}).get("usage") or {}
        session_key = (envelope or {}).get("session_key")

        return AgentResult(
            success=finish_reason == "stop",
            timeout=timed_out,
            exit_code=exit_code,
            finish_reason=finish_reason,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            session_id=session_key,
            duration_seconds=round(duration, 1),
            usage=usage,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_secret_env_file() -> str:
    """Write provider secrets to a private (0600) temp env-file.

    Returned path is passed to ``docker exec --env-file`` and removed by
    the caller right after the agent subprocess finishes.

    The fd is opened with mode 0o600 in the same call as ``mkstemp`` so
    the file is never observable with broader permissions, and the
    payload is written with an explicit UTF-8 encoding so non-UTF-8
    locales (cp1252 on Windows) do not raise mid-write and leak the
    half-written key.
    """
    fd, path = tempfile.mkstemp(prefix="opensquilla-swebench-", suffix=".env")
    # ``fdopen`` is the operation that takes ownership of ``fd``. Track
    # that transition explicitly so a failure inside ``chmod`` /
    # ``fdopen`` / ``write`` cleans up both the descriptor and the
    # partial file rather than leaking either.
    fd_owned = False
    file_owned = True
    try:
        # Belt-and-braces: mkstemp already uses 0o600 on POSIX, but
        # tighten explicitly so a permissive umask or a non-POSIX
        # platform cannot leave the file group/world-readable while the
        # docker subprocess is launched.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd_owned = True
            fh.write(f"OPENROUTER_API_KEY={os.environ.get('OPENROUTER_API_KEY', '')}\n")
    except Exception:
        # Make sure we don't leak the fd / partial file if the write
        # fails for any reason (decode error, disk full, ...). If
        # ``fdopen`` never took ownership, close the raw descriptor
        # ourselves; otherwise we close the wrapped file handle above
        # already.
        if not fd_owned:
            try:
                os.close(fd)
            except OSError:
                pass
        if file_owned:
            try:
                os.unlink(path)
            except OSError:
                pass
        raise
    return path


def _save_container_file(container_name: str, src_in_container: str, dest: Path) -> None:
    """Copy a file out of the container; silently no-op on failure."""
    if not src_in_container:
        return
    try:
        result = subprocess.run(
            ["docker", "exec", container_name, "cat", src_in_container],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout:
            dest.write_text(result.stdout, encoding="utf-8")
    except Exception:
        pass


def _cleanup_opensquilla_metadata(container_name: str) -> None:
    """Remove runtime droppings OpenSquilla may have left under /testbed.

    Only untracked copies are removed: when the benchmark repo legitimately
    tracks a file with one of these names (e.g. its own AGENTS.md), deleting
    it would surface as a spurious deletion in the collected patch.
    """
    names = (
        "AGENTS.md HEARTBEAT.md SOUL.md TOOLS.md USER.md IDENTITY.md memory sessions .opensquilla"
    )
    script = (
        f"cd /testbed && for f in {names}; do "
        'git ls-files --error-unmatch "$f" >/dev/null 2>&1 || rm -rf "$f"; '
        "done"
    )
    try:
        subprocess.run(
            ["docker", "exec", container_name, "bash", "-c", script],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Best-effort cleanup must never fail the run; the patch cleaner
        # still strips anything unexpected later.
        logger.warning("Container metadata cleanup failed: %s", exc)


def _parse_json_envelope(stdout: str) -> dict | None:
    """OpenSquilla --json emits a single JSON envelope (usually last line)."""
    if not stdout:
        return None
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
            break
    matches = _JSON_OBJECT_RE.findall(stdout)
    for raw in reversed(matches):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict) and "status" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    return None


def _decode(data) -> str:
    if isinstance(data, bytes):
        return data.decode(errors="replace")
    return data or ""
