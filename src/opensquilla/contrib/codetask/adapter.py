"""LocalAdapter: run an OpenSquilla agent as a host subprocess.

Unlike the swebench OpenSquillaAdapter (which crosses a Docker boundary via
``docker exec``), this runs ``opensquilla agent`` directly on the host with
the repo as the working directory. Provider credentials are inherited from
the runner's environment — no env-file is needed because there is no
container boundary to cross (codex review #3).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tomli_w

from opensquilla.contrib.codetask.agent_config import (
    AgentConfigBundle,
    load_agent_config_bundle,
)
from opensquilla.contrib.codetask.config import (
    DEFAULT_AGENT_TIMEOUT,
    DEFAULT_ITERATION_TIMEOUT,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MAX_PROVIDER_RETRIES,
    agent_python,
)
from opensquilla.contrib.codetask.types import AgentOutcome

logger = logging.getLogger(__name__)

SUBPROCESS_TIMEOUT_BUFFER = 120
DEFAULT_QUIET_TIMEOUT = 300
STATUS_HEARTBEAT_SECONDS = 15
POLL_INTERVAL_SECONDS = 0.5

_JSON_OBJECT_RE = re.compile(r"\{(?:[^{}]|(?:\{[^{}]*\}))*\}")
StatusCallback = Callable[[dict[str, Any]], None]

# code-task inherits the operator's runtime environment for credentials,
# proxies, PATH, and packaged-runtime discovery. Persistent/profile-scoped
# locations are different: a child agent is a separate writer and must never
# attach itself to the live Desktop/gateway profile that launched the run.
_PROFILE_SCOPED_CHILD_ENV = frozenset(
    {
        "OPENSQUILLA_DESKTOP",
        "OPENSQUILLA_DESKTOP_PROFILE_KIND",
        "OPENSQUILLA_PROFILE_KIND",
        "OPENSQUILLA_HOME",
        "OPENSQUILLA_PROFILE",
        "OPENSQUILLA_STATE_DIR",
        "OPENSQUILLA_GATEWAY_STATE_DIR",
        "OPENSQUILLA_GATEWAY_WORKSPACE_DIR",
        "OPENSQUILLA_WORKSPACE_DIR",
        "OPENSQUILLA_MEMORY_DIR",
        "OPENSQUILLA_MEMORY_DB",
        "OPENSQUILLA_SESSION_ARCHIVE_DIR",
        "OPENSQUILLA_LOG_DIR",
        "OPENSQUILLA_TURN_CALL_LOG_DIR",
        "OPENSQUILLA_LLM_TRACE_PATH",
        "OPENSQUILLA_RUNTIME_EVENTS_PATH",
        "OPENSQUILLA_PATCH_EVIDENCE_LEDGER_PATH",
        "OPENSQUILLA_SCHEDULER_DB",
        "OPENSQUILLA_META_RUNS_DB",
        "OPENSQUILLA_ROUTER_DECISIONS_DB",
    }
)


def _agent_access_arguments(bundle: AgentConfigBundle) -> list[str]:
    """Return child CLI access flags matching the effective sandbox run mode."""

    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.run_mode import RunMode, normalize_run_mode

    config = GatewayConfig(**copy.deepcopy(bundle.payload))
    run_mode = normalize_run_mode(config.effective_run_mode)
    if run_mode is RunMode.FULL:
        return ["--no-workspace-strict", "--permissions", "full"]
    return [
        "--workspace-strict",
        "--workspace-lockdown",
        "--permissions",
        "restricted",
    ]


class LocalAdapter:
    """Drives the host ``opensquilla agent`` CLI and returns a structured result."""

    def __init__(
        self,
        *,
        model: str = "",
        thinking: str = "",
        timeout: int = DEFAULT_AGENT_TIMEOUT,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        agent_config: AgentConfigBundle | None = None,
    ):
        self.model = model
        self.thinking = thinking
        self.timeout = timeout
        self.max_iterations = max_iterations
        self.agent_config = agent_config

    def run(
        self,
        prompt: str,
        *,
        repo: Path,
        scratch_dir: Path,
        artifact_dir: Path,
        status_callback: StatusCallback | None = None,
        quiet_timeout: int | None = None,
    ) -> AgentOutcome:
        """Run one agent turn with ``repo`` as the workspace.

        Writes agent_stdout.log / agent_stderr.log / transcript.jsonl /
        usage.json into ``artifact_dir``.
        """
        # Start from a clean scratch dir: a stale verification.json from an
        # earlier run that reused this run_id must never be read back (codex).
        if scratch_dir.exists():
            shutil.rmtree(scratch_dir, ignore_errors=True)
        scratch_dir.mkdir(parents=True, exist_ok=True)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = artifact_dir / "transcript.jsonl"
        usage_path = artifact_dir / "usage.json"
        stdout_path = artifact_dir / "agent_stdout.log"
        stderr_path = artifact_dir / "agent_stderr.log"
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        transcript_path.touch()
        if not usage_path.exists():
            usage_path.write_text("{}", encoding="utf-8")

        bundle = self.agent_config or load_agent_config_bundle()
        argv: list[str] = [
            "opensquilla",
            "agent",
            "--message",
            prompt,
            "--workspace",
            str(repo),
            *_agent_access_arguments(bundle),
            "--scratch-dir",
            str(scratch_dir),
            "--stateless",
            "--stateless-keep-project-rules",
            "--no-memory-capture",
            "--session-db-path",
            ":memory:",
            "--json",
            "--timeout",
            str(self.timeout),
            "--max-iterations",
            str(self.max_iterations),
            "--iteration-timeout-seconds",
            str(DEFAULT_ITERATION_TIMEOUT),
            "--max-provider-retries",
            str(DEFAULT_MAX_PROVIDER_RETRIES),
            "--transcript-path",
            str(transcript_path),
            "--usage-path",
            str(usage_path),
        ]
        if self.model:
            argv += ["--model", self.model]
        if self.thinking:
            argv += ["--thinking", self.thinking]

        py_code = f"import sys\nsys.argv = {argv!r}\nfrom opensquilla.cli.main import app\napp()\n"
        cmd = _agent_command(agent_python(), argv, py_code)
        command_label = _display_command(agent_python(), argv)

        start = time.time()
        timed_out = False
        stalled = False
        stall_error = ""
        # cwd = repo so relative tool paths and test commands resolve there.
        # env inherits provider credentials and points the agent at the per-run
        # config (OPENSQUILLA_GATEWAY_CONFIG_PATH): the operator's provider
        # sections carried in, coding-irrelevant tools denied, network kept.
        # Isolate the agent and any install/test descendants into their own
        # process group / job so a timeout can kill the WHOLE tree, not just
        # the direct python child (codex review #6). POSIX uses
        # start_new_session (setsid); Windows uses CREATE_NEW_PROCESS_GROUP.
        # Give the agent a PER-RUN tool-result/media store under scratch instead
        # of the shared global media root. The tool-result store rescans every
        # record on disk on each provider request; against the global store (all
        # sessions' accumulated history) that becomes a quadratic disk scan that
        # can saturate the event loop and burn the whole timeout doing nothing.
        # A fresh per-run dir keeps the scanned set tiny (this run only) and stops
        # code-task polluting the global store. attachments.media_root wins in
        # media_root_from_config(); tool_result_store_dir = media_root/tool-results.
        run_media_root = scratch_dir.expanduser().resolve() / "media"
        per_run_config = artifact_dir / "agent-config.toml"
        _cfg = copy.deepcopy(bundle.payload)
        _attachments = _cfg.setdefault("attachments", {})
        if not isinstance(_attachments, dict):
            raise RuntimeError("code-task agent config has invalid [attachments]")
        _attachments["media_root"] = str(run_media_root)
        # The file can carry [llm_profiles] credentials (they have no env
        # transport channel), so create it owner-only from the start — a
        # post-write chmod leaves a world-readable window under umask 022.
        # POSIX mode is honored; a no-op on Windows (best effort). Attempt
        # snapshots preserve the mode (copy2).
        _write_owner_only(per_run_config, tomli_w.dumps(_cfg))
        agent_env = _agent_environment(
            bundle=bundle,
            per_run_config=per_run_config,
            scratch_dir=scratch_dir,
        )
        popen_kwargs: dict[str, Any] = dict(
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=agent_env,
        )
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        else:
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except FileNotFoundError as exc:
            raise RuntimeError(f"could not launch agent interpreter: {exc}") from exc

        if status_callback is not None:
            status_callback(
                {
                    "pid": getattr(proc, "pid", None),
                    "current_command": command_label,
                    "log_paths": {
                        "stdout": str(stdout_path),
                        "stderr": str(stderr_path),
                        "transcript": str(transcript_path),
                        "usage": str(usage_path),
                    },
                    "started_at": datetime.now(UTC).isoformat(),
                }
            )

        if not hasattr(proc, "poll") or getattr(proc, "stdout", None) is None:
            try:
                stdout, stderr = proc.communicate(
                    timeout=self.timeout + SUBPROCESS_TIMEOUT_BUFFER
                )
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = -1
                _kill_process_group(proc)
                stdout, stderr = proc.communicate()
                logger.warning("agent subprocess timed out after %ds; killed group", self.timeout)
            stdout_path.write_text(stdout or "", encoding="utf-8")
            stderr_path.write_text(stderr or "", encoding="utf-8")
        else:
            quiet_timeout = _quiet_timeout(quiet_timeout)
            stdout, stderr, exit_code, timed_out, stalled, stall_error = _monitor_process(
                proc,
                timeout=self.timeout + SUBPROCESS_TIMEOUT_BUFFER,
                quiet_timeout=quiet_timeout,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                transcript_path=transcript_path,
                usage_path=usage_path,
                status_callback=status_callback,
                command_label=command_label,
            )

        duration = time.time() - start

        envelope = _parse_json_envelope(stdout)
        envelope_errors = [e for e in ((envelope or {}).get("errors") or []) if isinstance(e, dict)]
        if stalled:
            finish = "stalled"
        elif timed_out:
            finish = "timeout"
        elif exit_code != 0:
            finish = "error"
        elif envelope is None:
            finish = "empty"
        else:
            status = envelope.get("status")
            text = (envelope.get("text") or "").strip()
            if status == "ok" and text:
                finish = "stop"
            elif envelope_errors:
                finish = "error"
            else:
                finish = "empty"

        usage = (envelope or {}).get("usage") or {}
        return AgentOutcome(
            success=finish == "stop",
            timeout=timed_out,
            exit_code=exit_code,
            finish_reason=finish,
            duration_seconds=round(duration, 1),
            session_id=(envelope or {}).get("session_key"),
            usage=usage,
            error=stall_error or None,
            errors=envelope_errors,
        )


def _agent_environment(
    *,
    bundle: AgentConfigBundle,
    per_run_config: Path,
    scratch_dir: Path,
) -> dict[str, str]:
    """Build the isolated environment for one code-task child agent.

    The child still acquires the universal writer lock, but for a disposable
    per-attempt profile under scratch rather than the caller's live profile.
    Runtime/provider variables remain inherited; only persistent data-root
    overrides are removed.
    """

    environment = dict(os.environ)
    for name in _PROFILE_SCOPED_CHILD_ENV:
        environment.pop(name, None)
    environment.update(bundle.child_env)
    environment.update(
        {
            "OPENSQUILLA_STATE_DIR": str(
                scratch_dir.expanduser().resolve() / "profile"
            ),
            "OPENSQUILLA_GATEWAY_CONFIG_PATH": str(per_run_config),
        }
    )
    return environment


def _monitor_process(
    proc: subprocess.Popen,
    *,
    timeout: int,
    quiet_timeout: int,
    stdout_path: Path,
    stderr_path: Path,
    transcript_path: Path,
    usage_path: Path,
    status_callback: StatusCallback | None,
    command_label: str,
) -> tuple[str, str, int, bool, bool, str]:
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    lock = threading.Lock()
    last_activity = time.monotonic()
    last_output_at = datetime.now(UTC).isoformat()
    killed_for_timeout = False
    killed_for_stall = False
    stall_error = ""

    def mark_activity() -> None:
        nonlocal last_activity, last_output_at
        with lock:
            last_activity = time.monotonic()
            last_output_at = datetime.now(UTC).isoformat()

    def read_stream(pipe: Any, path: Path, parts: list[str]) -> None:
        try:
            with path.open("a", encoding="utf-8") as handle:
                while True:
                    chunk = pipe.readline()
                    if not chunk:
                        break
                    if isinstance(chunk, bytes):
                        chunk = chunk.decode(errors="replace")
                    parts.append(chunk)
                    handle.write(chunk)
                    handle.flush()
                    mark_activity()
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    threads = [
        threading.Thread(
            target=read_stream, args=(proc.stdout, stdout_path, stdout_parts), daemon=True
        ),
        threading.Thread(
            target=read_stream, args=(proc.stderr, stderr_path, stderr_parts), daemon=True
        ),
    ]
    for thread in threads:
        thread.start()

    side_mtimes = {
        transcript_path: _mtime_or_zero(transcript_path),
        usage_path: _mtime_or_zero(usage_path),
    }
    deadline = time.monotonic() + timeout
    next_status = time.monotonic() + STATUS_HEARTBEAT_SECONDS

    while True:
        rc = proc.poll()
        now = time.monotonic()
        for path, previous in list(side_mtimes.items()):
            current = _mtime_or_zero(path)
            if current > previous:
                side_mtimes[path] = current
                mark_activity()

        if rc is not None:
            break
        if now >= deadline:
            killed_for_timeout = True
            _kill_process_group(proc)
            logger.warning("agent subprocess timed out after %ds; killed group", timeout)
            break

        with lock:
            quiet_for = now - last_activity
            output_at = last_output_at
        if quiet_timeout > 0 and quiet_for >= quiet_timeout:
            killed_for_stall = True
            stall_error = (
                f"agent produced no stdout/stderr/transcript/usage updates for "
                f"{int(quiet_for)}s"
            )
            _kill_process_group(proc)
            logger.warning("%s; killed group", stall_error)
            break

        if status_callback is not None and now >= next_status:
            status_callback(
                {
                    "pid": getattr(proc, "pid", None),
                    "current_command": command_label,
                    "last_output_at": output_at,
                    "quiet_for_seconds": round(quiet_for, 1),
                    "log_paths": {
                        "stdout": str(stdout_path),
                        "stderr": str(stderr_path),
                        "transcript": str(transcript_path),
                        "usage": str(usage_path),
                    },
                }
            )
            next_status = now + STATUS_HEARTBEAT_SECONDS
        time.sleep(POLL_INTERVAL_SECONDS)

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        proc.wait(timeout=10)

    for thread in threads:
        thread.join(timeout=2)

    return (
        "".join(stdout_parts),
        "".join(stderr_parts),
        proc.returncode if proc.returncode is not None else -1,
        killed_for_timeout,
        killed_for_stall,
        stall_error,
    )


def _quiet_timeout(value: int | None) -> int:
    if value is not None:
        return value
    raw = os.environ.get("OPENSQUILLA_CODETASK_AGENT_QUIET_TIMEOUT")
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return DEFAULT_QUIET_TIMEOUT


def _mtime_or_zero(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _display_command(executable: str, argv: list[str]) -> str:
    shown: list[str]
    if _looks_like_python_interpreter(executable):
        shown = [executable, "-m", "opensquilla.cli.main", *argv[1:]]
    else:
        shown = [executable, *argv[1:]]
    sanitized: list[str] = []
    skip_next = False
    for part in shown:
        if skip_next:
            sanitized.append("<prompt>")
            skip_next = False
            continue
        sanitized.append(part)
        if part == "--message":
            skip_next = True
    return " ".join(shlex.quote(str(part)) for part in sanitized)


def _kill_process_group(proc) -> None:
    """Kill the agent subprocess and its descendants (best effort).

    On POSIX we signal the entire process group via ``killpg`` so install /
    test grandchildren die too. On Windows there is no POSIX process group;
    ``taskkill /F /T /PID <pid>`` walks the Windows parent-tree, which is
    enough for ordinary subprocess descendants but is NOT a containment
    boundary (a child that detaches, re-parents to a service, or is launched
    via a scheduler is not bound to the tree and can survive). We fall back
    to ``proc.kill()`` (direct child only) if taskkill is unavailable or
    reports failure, so the outer ``proc.communicate()`` cannot hang on a
    still-running direct child.
    """
    import os
    import signal

    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    else:
        try:
            r = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
            )
            # taskkill returns nonzero when the target is already gone or it
            # can't reach a descendant. The direct child may still be alive,
            # so we only short-circuit on a clean exit; otherwise we fall
            # through to proc.kill() so the outer proc.communicate() can't
            # block forever on a half-killed tree.
            if r.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _write_owner_only(path: Path, text: str) -> None:
    """Write ``text`` to ``path``, creating it readable only by the owner.

    ``os.open`` with mode 0o600 applies the mode at creation (subject to
    umask, which only removes bits), so credentials in the file are never
    briefly world-readable — unlike write-then-chmod. On Windows the mode
    bits are largely inert; this is best effort there.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def _agent_command(executable: str, argv: list[str], py_code: str) -> list[str]:
    """Build the agent subprocess command for source and packaged runtimes."""
    if _looks_like_python_interpreter(executable):
        return [executable, "-c", py_code]
    return [executable, *argv[1:]]


def _looks_like_python_interpreter(executable: str) -> bool:
    name = Path(executable).name.lower()
    return name.startswith("python") or name in {"py", "py.exe"}


def _parse_json_envelope(stdout: str) -> dict | None:
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
    for raw in reversed(_JSON_OBJECT_RE.findall(stdout)):
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
