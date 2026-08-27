"""Safe Git capability discovery and subprocess execution.

The macOS ``/usr/bin/git`` executable is an Apple developer-tool shim.  On a
machine without Xcode or the Command Line Tools, executing that shim can open an
installer prompt.  Capability discovery in this module is deliberately passive:
it never executes a Git candidate and, on macOS, resolves the selected developer
tool to its real absolute path before returning it.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import platform
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from opensquilla.run_mode import RunMode, normalize_run_mode
from opensquilla.subprocess_encoding import decode_subprocess_output

_CAPABILITY_CACHE_TTL_SECONDS = 5.0
_XCODE_SELECT_TIMEOUT_SECONDS = 1.0
_APPLE_GIT_SHIM = Path("/usr/bin/git")
_XCODE_SELECT = Path("/usr/bin/xcode-select")


class GitCapabilityState(StrEnum):
    """Whether a safe-to-launch Git executable is available."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class GitRunState(StrEnum):
    """Outcome of one Git invocation."""

    OK = "ok"
    UNAVAILABLE = "unavailable"
    NOT_REPOSITORY = "not_repository"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


@dataclass(frozen=True)
class GitCapability:
    """Resolved Git executable, or a reason that one cannot be used safely."""

    state: GitCapabilityState
    executable: Path | None = None
    source: str | None = None
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.state is GitCapabilityState.AVAILABLE and self.executable is not None


@dataclass(frozen=True)
class GitRunResult:
    """Structured result that keeps missing Git distinct from an empty result."""

    state: GitRunState
    returncode: int | None
    stdout: bytes
    stderr: bytes
    capability: GitCapability

    @property
    def ok(self) -> bool:
        return self.state is GitRunState.OK

    @property
    def stdout_text(self) -> str:
        return decode_subprocess_output(self.stdout)

    @property
    def stderr_text(self) -> str:
        return decode_subprocess_output(self.stderr)


type _CapabilityCacheKey = tuple[str, str, str, str, str, str]

_capability_cache: dict[_CapabilityCacheKey, tuple[float, GitCapability]] = {}
_capability_cache_lock = threading.Lock()
_ACTIVE_GIT_RUN_MODE: contextvars.ContextVar[RunMode | None] = contextvars.ContextVar(
    "opensquilla_git_run_mode",
    default=None,
)


def clear_git_capability_cache() -> None:
    """Discard all process-local Git capability entries."""

    with _capability_cache_lock:
        _capability_cache.clear()


@contextlib.contextmanager
def git_run_mode_scope(run_mode: RunMode | str | None):
    """Bind the Safe/Full resolver preference for one logical turn."""

    token = _ACTIVE_GIT_RUN_MODE.set(normalize_run_mode(run_mode))
    try:
        yield
    finally:
        _ACTIVE_GIT_RUN_MODE.reset(token)


def _environment_value(environment: Mapping[str, str], name: str) -> str:
    wanted = name.casefold()
    for key, value in environment.items():
        if key.casefold() == wanted:
            return str(value)
    return ""


def _platform_name() -> str:
    return platform.system().strip().casefold()


def _managed_git_path(platform_name: str) -> Path | None:
    if platform_name != "windows":
        return None
    try:
        from opensquilla.runtime_packs import resolve_component_binary
    except ImportError:
        return None
    try:
        from opensquilla.sandbox.integration import active_sandbox_policy

        runtime_policy = active_sandbox_policy().runtimes
    except (ImportError, RuntimeError, TypeError, ValueError):
        # During early composition there may be no policy facade yet. Preserve
        # the long-standing default-enabled Runtime Pack behavior in that case.
        runtime_policy = None
    if runtime_policy is not None and (
        not runtime_policy.enabled or not runtime_policy.git_bash
    ):
        return None
    try:
        return resolve_component_binary("gitBash", "git", allow_host=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _is_executable_file(path: Path, *, windows: bool) -> bool:
    try:
        return path.is_file() and (windows or os.access(path, os.X_OK))
    except OSError:
        return False


def _absolute_path(path: Path) -> Path:
    return path.expanduser().absolute()


def _windows_executable_names(pathext: str) -> tuple[str, ...]:
    extensions = [part.strip() for part in pathext.split(";") if part.strip()]
    if not extensions:
        extensions = [".COM", ".EXE", ".BAT", ".CMD"]
    names = ["git"]
    for extension in extensions:
        suffix = extension if extension.startswith(".") else f".{extension}"
        names.append(f"git{suffix}")
    return tuple(dict.fromkeys(names))


def _host_git_candidates(
    environment: Mapping[str, str],
    *,
    platform_name: str,
) -> tuple[Path, ...]:
    path_value = _environment_value(environment, "PATH")
    if not path_value:
        return ()
    windows = platform_name == "windows"
    separator = ";" if windows else os.pathsep
    names = (
        _windows_executable_names(_environment_value(environment, "PATHEXT"))
        if windows
        else ("git",)
    )
    candidates: list[Path] = []
    seen: set[str] = set()
    for raw_directory in path_value.split(separator):
        raw_directory = raw_directory.strip().strip('"')
        if not raw_directory:
            continue
        directory = Path(raw_directory).expanduser()
        # Relative and empty PATH entries are controlled by the process cwd and
        # are unsuitable for application-owned background Git operations.
        if not directory.is_absolute():
            continue
        for name in names:
            candidate = _absolute_path(directory / name)
            key = os.path.normcase(str(candidate)) if windows else str(candidate)
            if key in seen:
                continue
            seen.add(key)
            if _is_executable_file(candidate, windows=windows):
                candidates.append(candidate)
    return tuple(candidates)


def _is_apple_git_shim(path: Path) -> bool:
    candidate = _absolute_path(path)
    shim = _absolute_path(_APPLE_GIT_SHIM)
    if candidate == shim:
        return True
    try:
        return candidate.samefile(shim)
    except OSError:
        return False


def _unavailable(reason: str) -> GitCapability:
    return GitCapability(
        state=GitCapabilityState.UNAVAILABLE,
        executable=None,
        source=None,
        reason=reason,
    )


def _available(path: Path, source: str) -> GitCapability:
    return GitCapability(
        state=GitCapabilityState.AVAILABLE,
        executable=_absolute_path(path),
        source=source,
        reason=None,
    )


def _resolve_apple_developer_git(environment: Mapping[str, str]) -> GitCapability:
    if not _is_executable_file(_XCODE_SELECT, windows=False):
        return _unavailable("xcode_select_unavailable")
    try:
        completed = subprocess.run(
            [str(_XCODE_SELECT), "--print-path"],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=_XCODE_SELECT_TIMEOUT_SECONDS,
            env=dict(environment),
        )
    except subprocess.TimeoutExpired:
        return _unavailable("xcode_select_timed_out")
    except OSError:
        return _unavailable("xcode_select_unavailable")
    if completed.returncode != 0:
        return _unavailable("apple_developer_tools_unavailable")
    raw_path = decode_subprocess_output(completed.stdout).strip()
    if not raw_path or "\x00" in raw_path:
        return _unavailable("apple_developer_directory_invalid")
    developer_directory = Path(raw_path).expanduser()
    if not developer_directory.is_absolute():
        return _unavailable("apple_developer_directory_invalid")
    candidate = developer_directory / "usr" / "bin" / "git"
    if not _is_executable_file(candidate, windows=False) or _is_apple_git_shim(candidate):
        return _unavailable("apple_developer_git_unavailable")
    return _available(candidate, "apple_developer")


def _resolve_uncached(
    environment: Mapping[str, str],
    *,
    mode: RunMode,
    platform_name: str,
    managed_path: Path | None,
) -> GitCapability:
    managed = (
        _available(managed_path, "managed")
        if managed_path is not None
        and _is_executable_file(managed_path, windows=platform_name == "windows")
        else None
    )
    host_candidates = _host_git_candidates(environment, platform_name=platform_name)

    if platform_name == "windows":
        host = _available(host_candidates[0], "host") if host_candidates else None
        ordered = (host, managed) if mode is RunMode.FULL else (managed, host)
        return next((candidate for candidate in ordered if candidate is not None), None) or (
            _unavailable("git_not_found")
        )

    if platform_name != "darwin":
        if host_candidates:
            return _available(host_candidates[0], "host")
        return _unavailable("git_not_found")

    apple_capability: GitCapability | None = None
    for candidate in host_candidates:
        if _is_apple_git_shim(candidate):
            if apple_capability is None:
                apple_capability = _resolve_apple_developer_git(environment)
            if apple_capability.available:
                return apple_capability
            continue
        return _available(candidate, "host")
    if apple_capability is not None:
        return apple_capability
    return _unavailable("git_not_found")


def resolve_git_capability(
    environment: Mapping[str, str] | None = None,
    run_mode: RunMode | str | None = None,
    force_refresh: bool = False,
) -> GitCapability:
    """Resolve a safe absolute Git executable without executing Git itself."""

    effective_environment = dict(os.environ if environment is None else environment)
    mode = normalize_run_mode(
        run_mode if run_mode is not None else _ACTIVE_GIT_RUN_MODE.get()
    )
    platform_name = _platform_name()
    managed_path = _managed_git_path(platform_name)
    key: _CapabilityCacheKey = (
        platform_name,
        mode.value,
        _environment_value(effective_environment, "PATH"),
        _environment_value(effective_environment, "PATHEXT"),
        _environment_value(effective_environment, "DEVELOPER_DIR"),
        str(managed_path or ""),
    )
    now = time.monotonic()
    if not force_refresh:
        with _capability_cache_lock:
            cached = _capability_cache.get(key)
            if cached is not None and now - cached[0] < _CAPABILITY_CACHE_TTL_SECONDS:
                return cached[1]
    capability = _resolve_uncached(
        effective_environment,
        mode=mode,
        platform_name=platform_name,
        managed_path=managed_path,
    )
    with _capability_cache_lock:
        _capability_cache[key] = (now, capability)
    return capability


def _environment_with_resolved_git(
    environment: Mapping[str, str],
    executable: Path,
) -> dict[str, str]:
    result = dict(environment)
    path_key = next((key for key in result if key.casefold() == "path"), "PATH")
    existing_path = result.get(path_key, "")
    executable_directory = str(executable.parent)
    executable_directory_key = os.path.normcase(executable_directory)
    existing_parts = [part for part in existing_path.split(os.pathsep) if part]
    remaining_parts = [
        part
        for part in existing_parts
        if os.path.normcase(part) != executable_directory_key
    ]
    result[path_key] = os.pathsep.join(
        (executable_directory, *remaining_parts)
    )
    return result


def _noninteractive_environment(
    environment: Mapping[str, str],
    executable: Path,
) -> dict[str, str]:
    result = _environment_with_resolved_git(environment, executable)
    result["GIT_TERMINAL_PROMPT"] = "0"
    result["GCM_INTERACTIVE"] = "Never"
    result["GIT_ASKPASS"] = ""
    result["SSH_ASKPASS"] = ""
    result["SSH_ASKPASS_REQUIRE"] = "never"
    result["GIT_PAGER"] = ""
    result["PAGER"] = ""
    result["LC_ALL"] = "C"
    result["LANG"] = "C"
    return result


def _exception_bytes(value: object | None) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8", errors="replace")


_NOT_REPOSITORY_MARKERS = (
    "not a git repository",
    "this operation must be run in a work tree",
)


def _failed_run_state(stdout: bytes, stderr: bytes) -> GitRunState:
    message = decode_subprocess_output(stderr + b"\n" + stdout).casefold()
    if any(marker in message for marker in _NOT_REPOSITORY_MARKERS):
        return GitRunState.NOT_REPOSITORY
    return GitRunState.FAILED


def run_git(
    args: Sequence[str],
    cwd: str | Path | None = None,
    timeout: float = 2.0,
    environment: Mapping[str, str] | None = None,
    run_mode: RunMode | str | None = None,
    input_bytes: bytes | None = None,
    allow_user_interaction: bool = False,
) -> GitRunResult:
    """Run Git by absolute path and return a structured, non-raising result.

    Application-owned probes are non-interactive by default. Explicit user
    workflows may opt in to inherited credential helpers and stdin while still
    using the safely resolved absolute executable.
    """

    effective_environment = dict(os.environ if environment is None else environment)
    capability = resolve_git_capability(effective_environment, run_mode)
    if not capability.available or capability.executable is None:
        return GitRunResult(
            state=GitRunState.UNAVAILABLE,
            returncode=None,
            stdout=b"",
            stderr=(capability.reason or "git_unavailable").encode(),
            capability=capability,
        )
    command = [str(capability.executable), *(str(argument) for argument in args)]
    child_environment = (
        _environment_with_resolved_git(effective_environment, capability.executable)
        if allow_user_interaction
        else _noninteractive_environment(effective_environment, capability.executable)
    )
    kwargs: dict[str, Any] = {
        "cwd": str(Path(cwd).expanduser()) if cwd is not None else None,
        "env": child_environment,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "check": False,
        "timeout": timeout,
    }
    if input_bytes is None and not allow_user_interaction:
        kwargs["stdin"] = subprocess.DEVNULL
    elif input_bytes is not None:
        kwargs["input"] = input_bytes
    try:
        completed = subprocess.run(command, **kwargs)
    except subprocess.TimeoutExpired as exc:
        return GitRunResult(
            state=GitRunState.TIMED_OUT,
            returncode=None,
            stdout=_exception_bytes(exc.stdout),
            stderr=_exception_bytes(exc.stderr),
            capability=capability,
        )
    except (OSError, ValueError) as exc:
        return GitRunResult(
            state=GitRunState.FAILED,
            returncode=None,
            stdout=b"",
            stderr=str(exc).encode("utf-8", errors="replace"),
            capability=capability,
        )
    stdout = _exception_bytes(completed.stdout)
    stderr = _exception_bytes(completed.stderr)
    state = (
        GitRunState.OK
        if completed.returncode == 0
        else _failed_run_state(stdout, stderr)
    )
    return GitRunResult(
        state=state,
        returncode=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        capability=capability,
    )


def probe_git_repository(
    cwd: str | Path,
    *,
    timeout: float = 2.0,
    environment: Mapping[str, str] | None = None,
    run_mode: RunMode | str | None = None,
) -> GitRunState:
    """Return whether *cwd* is an accessible Git work tree."""

    result = run_git(
        ("rev-parse", "--is-inside-work-tree"),
        cwd=cwd,
        timeout=timeout,
        environment=environment,
        run_mode=run_mode,
    )
    if result.state is not GitRunState.OK:
        return result.state
    if result.stdout_text.strip().casefold() == "true":
        return GitRunState.OK
    return GitRunState.NOT_REPOSITORY


__all__ = [
    "GitCapability",
    "GitCapabilityState",
    "GitRunResult",
    "GitRunState",
    "clear_git_capability_cache",
    "git_run_mode_scope",
    "probe_git_repository",
    "resolve_git_capability",
    "run_git",
]
