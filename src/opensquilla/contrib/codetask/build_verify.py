"""Build-mode verification for code-task (app / from-scratch generation).

Red->green->regression fits "fix a bug in an existing repo". Generating/editing
an app has no such test loop, so build mode instead runs a FIXED, runner-owned
checklist that proves the app actually builds: install from the committed
lockfile, build, and PACKAGE for the host platform.

The package step is host-aware and builds the installer for whatever OS it runs
on (each platform's installer can only be built on that platform):
- macOS    -> `electron-builder --mac`   -> a .dmg (signing auto-discovery is
  disabled so an unsigned .dmg is built deterministically, no keychain prompt).
- Windows  -> `electron-builder --win`   -> an .exe (NSIS) installer.
- Linux    -> `electron-builder --linux` -> an .AppImage / .deb installer.

To collect all three, run code-task on each OS (or a CI matrix); a single host
only produces its own platform's installer.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opensquilla.contrib.codetask.adapter import _kill_process_group
from opensquilla.contrib.codetask.types import BuildCheck, BuildResult, TaskState

_TAIL_LINES = 25
_NODE_BIN_ENV_KEYS = (
    "OPENSQUILLA_NODE_BIN_DIR",
    "OPENSQUILLA_DESKTOP_NODE_BIN_DIR",
    "OPENSQUILLA_BUNDLED_NODE_BIN",
)


def _resolve_cli(name: str) -> str:
    """Resolve a node CLI shim (npm/npx) to its actual executable path.

    On Windows, ``npm``/``npx`` are ``.cmd`` shims that ``subprocess.run`` with
    ``shell=False`` cannot find by the bare name. ``shutil.which`` returns the
    fully-qualified ``npm.cmd``/``npx.cmd`` path, which Python can launch
    directly. Falls back to the bare name on POSIX (or when not found, so the
    later ``FileNotFoundError`` surfaces with a clear message).
    """
    on_path = shutil.which(name)
    if on_path:
        return on_path
    for node_bin_dir in _node_bin_dirs():
        for candidate_name in _cli_candidate_names(name):
            candidate = node_bin_dir / candidate_name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return name


def _cli_candidate_names(name: str) -> tuple[str, ...]:
    if sys.platform == "win32":
        return (name, f"{name}.cmd", f"{name}.bat", f"{name}.exe")
    return (name,)


def _node_bin_dirs() -> list[Path]:
    candidates: list[Path] = []
    for env_key in _NODE_BIN_ENV_KEYS:
        raw = os.environ.get(env_key)
        if not raw:
            continue
        for part in raw.split(os.pathsep):
            if not part:
                continue
            path = Path(part).expanduser()
            candidates.append(path.parent if path.is_file() else path)

    home = Path.home()
    if sys.platform == "win32":
        for env_key in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(env_key)
            if root:
                candidates.append(Path(root) / "nodejs")
    else:
        candidates.extend(
            [
                home / ".local" / "bin",
                home / ".npm-global" / "bin",
                Path("/opt/homebrew/bin"),
                Path("/usr/local/bin"),
            ]
        )

    seen: set[str] = set()
    existing: list[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_dir():
            existing.append(candidate)
    return existing


def _build_env() -> dict[str, str]:
    env = {**os.environ, **_PACKAGE_ENV, **_NONINTERACTIVE_ENV}
    node_dirs = [str(path) for path in _node_bin_dirs()]
    if node_dirs:
        current_path = env.get("PATH") or env.get("Path") or ""
        current_parts = [part for part in current_path.split(os.pathsep) if part]
        merged = [*node_dirs, *[part for part in current_parts if part not in node_dirs]]
        env["PATH"] = os.pathsep.join(merged)
        if sys.platform == "win32":
            env["Path"] = env["PATH"]
    return env

# Build unsigned, deterministically: never auto-discover a keychain identity
# (which can prompt/hang or sign host-dependently in an automated run).
_PACKAGE_ENV = {"CSC_IDENTITY_AUTO_DISCOVERY": "false"}

# Force every verification subprocess into non-interactive mode. npm honours
# ``CI``/``npm_config_yes``; ``DEBIAN_FRONTEND`` silences any apt-driven
# post-install prompt a build script might shell out to; disabling fund/audit
# banners keeps the captured tail focused on real failures. Combined with a
# closed stdin (see ``_run_build_check``), a prompting child gets EOF instead of
# stealing the caller's TTY and hanging.
_NONINTERACTIVE_ENV = {
    "CI": "1",
    "npm_config_yes": "true",
    "npm_config_fund": "false",
    "npm_config_audit": "false",
    "DEBIAN_FRONTEND": "noninteractive",
}


@dataclass
class _CheckRun:
    """Result of one bounded verification subprocess."""

    ran: bool
    exit_code: int | None
    timed_out: bool
    output_tail: str
    error: str | None = None  # e.g. "command not found: npm" (process never ran)


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _run_build_check(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> _CheckRun:
    """Run one verification command, bounded and non-interactive.

    stdin is closed (``DEVNULL``) so a prompting build tool gets EOF rather than
    the caller's terminal; the child runs in its own process group/session so a
    deadline overrun kills the WHOLE tree (``npm``/``npx`` spawn grandchildren
    that a plain ``proc.kill()`` would orphan — and on Windows would defeat the
    timeout entirely). Output is streamed to temp files (never PIPE) so a chatty
    build cannot deadlock on a full pipe buffer while we poll the deadline.
    """
    out_fd, out_path = tempfile.mkstemp(prefix="codetask-build-", suffix=".out")
    err_fd, err_path = tempfile.mkstemp(prefix="codetask-build-", suffix=".err")
    os.close(out_fd)
    os.close(err_fd)
    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "stdin": subprocess.DEVNULL,
        "env": env,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        with open(out_path, "w", encoding="utf-8") as out_f, open(
            err_path, "w", encoding="utf-8"
        ) as err_f:
            try:
                proc = subprocess.Popen(argv, stdout=out_f, stderr=err_f, **popen_kwargs)
            except FileNotFoundError as exc:
                return _CheckRun(False, None, False, "", error=f"command not found: {exc}")
            except OSError as exc:
                return _CheckRun(False, None, False, "", error=f"could not start: {exc}")
            deadline = time.monotonic() + max(1, timeout)
            while proc.poll() is None:
                if time.monotonic() >= deadline:
                    _kill_process_group(proc)
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        _kill_process_group(proc)
                    tail = _tail(_read_text(out_path) + _read_text(err_path))
                    return _CheckRun(
                        True,
                        None,
                        True,
                        f"TIMEOUT after {timeout}s (process tree killed)\n{tail}".rstrip(),
                    )
                time.sleep(0.2)
        tail = _tail(_read_text(out_path) + _read_text(err_path))
        return _CheckRun(True, proc.returncode, False, tail)
    finally:
        for path in (out_path, err_path):
            try:
                os.unlink(path)
            except OSError:
                pass


def _package_step() -> tuple[str, list[str]]:
    """Host-platform electron packaging command (name, argv).

    Builds ONE tooling-free installer target for the OS we are running on, so
    packaging succeeds on a clean machine without extra build tools and without
    depending on the app's own target list:
      macOS   -> dmg       (needs only macOS' built-in hdiutil)
      Windows -> nsis      (.exe; electron-builder's built-in installer)
      Linux   -> AppImage  (self-contained; no dpkg/snapcraft/rpm tooling needed)
    Pinning the target (vs a bare ``--mac``/``--win``/``--linux``) also avoids
    triggering extra targets a generated app may have configured (deb/snap/rpm),
    which would need host tooling and fail on a clean machine. Each target can
    only be built on its own platform, so to get all three, run on each OS.
    """
    npx = _resolve_cli("npx")
    if sys.platform == "darwin":
        return "package", [npx, "electron-builder", "--mac", "dmg", "--publish", "never"]
    if sys.platform == "win32":
        return "package", [npx, "electron-builder", "--win", "nsis", "--publish", "never"]
    # Linux (and other unix): AppImage is self-contained, no extra tooling.
    return "package", [npx, "electron-builder", "--linux", "AppImage", "--publish", "never"]


def _checklist() -> list[tuple[str, list[str]]]:
    # `npm ci` (NOT install) installs strictly from the committed lockfile and
    # never mutates it, so build verification leaves the collected change clean.
    npm = _resolve_cli("npm")
    return [
        ("npm_ci", [npm, "ci"]),
        ("build", [npm, "run", "build"]),
        _package_step(),
    ]


def _installer_suffixes() -> tuple[str, ...]:
    """Installer file extension(s) electron-builder emits for the HOST platform."""
    if sys.platform == "darwin":
        return (".dmg",)
    if sys.platform == "win32":
        return (".exe", ".msi")
    return (".AppImage", ".deb", ".rpm", ".snap")


def _find_installers(repo: Path) -> list[str]:
    """Produced installer artifacts for the HOST platform — the deliverables.

    build mode packages for whatever OS it runs on (macOS -> .dmg,
    Windows -> .exe, Linux -> .AppImage/.deb). electron-builder's output dir is
    configurable (``directories.output``, default ``dist``, but a generated app
    may set ``release/``), so search the whole repo tree for the host's
    installer extension(s) rather than a fixed dir — else a real, successful
    build whose installer landed elsewhere is misreported as "no installer".
    ``node_modules``/``.git`` and the unpacked app dirs (``win-unpacked`` etc.,
    which also contain a raw ``.exe``) are pruned. Multi-arch builds can emit
    more than one installer, so return all.
    """
    suffixes = _installer_suffixes()
    skip = {"node_modules", ".git"}
    found: list[str] = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [
            d for d in dirs if d not in skip and not d.endswith("-unpacked")
        ]
        found.extend(os.path.join(root, f) for f in files if f.endswith(suffixes))
    return sorted(found)


@dataclass
class BuildVerificationOutcome:
    state: TaskState
    build: BuildResult
    detail: str = ""


def _tail(text: str, n: int = _TAIL_LINES) -> str:
    return "\n".join((text or "").splitlines()[-n:])


_NODE_BUILTINS = frozenset({
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console",
    "constants", "crypto", "dgram", "diagnostics_channel", "dns", "domain",
    "events", "fs", "http", "http2", "https", "inspector", "module", "net",
    "os", "path", "perf_hooks", "process", "punycode", "querystring", "readline",
    "repl", "stream", "string_decoder", "timers", "tls", "trace_events", "tty",
    "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib",
})

_REQUIRE_RE = re.compile(
    r"""(?:require\(\s*|from\s+|import\(\s*)['"]([^'"]+)['"]"""
)


def _check_runtime_deps(repo: Path) -> BuildCheck:
    """Static check: every bare module the built MAIN process require()s must be
    in package.json ``dependencies``.

    electron-vite externalizes main/preload dependencies (required at runtime, not
    bundled) and electron-builder prunes ``devDependencies`` when packaging, so a
    runtime module left in ``devDependencies`` builds and packages cleanly yet
    makes the installed app crash on launch with ``Cannot find module``. This
    catches that whole class without launching the GUI.
    """
    chk = BuildCheck(
        name="runtime_deps",
        command="(static) main-process require()s must be in dependencies",
    )
    chk.ran = True
    main_dir = repo / "out" / "main"
    pkg = repo / "package.json"
    if not main_dir.is_dir() or not pkg.is_file():
        chk.ok = True
        chk.raw_tail = "skipped (no out/main or package.json)"
        return chk
    try:
        deps = set(json.loads(pkg.read_text(encoding="utf-8")).get("dependencies", {}))
    except (OSError, ValueError) as exc:
        chk.ok = False
        chk.raw_tail = f"cannot parse package.json: {exc}"
        return chk
    allowed = _NODE_BUILTINS | {"electron"}
    missing: set[str] = set()
    for js in main_dir.rglob("*.js"):
        try:
            text = js.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for spec in _REQUIRE_RE.findall(text):
            if spec.startswith((".", "/")):
                continue
            if spec.startswith("node:"):
                spec = spec[len("node:"):]
            base = (
                "/".join(spec.split("/")[:2])
                if spec.startswith("@")
                else spec.split("/")[0]
            )
            if base in allowed or base in deps:
                continue
            missing.add(base)
    if missing:
        chk.ok = False
        chk.raw_tail = (
            "main process require()s these at runtime but they are NOT in "
            'package.json "dependencies": ' + ", ".join(sorted(missing)) + ". "
            "electron-builder prunes devDependencies when packaging, so the "
            "installed app throws `Cannot find module`. Move them to dependencies."
        )
    else:
        chk.ok = True
        chk.raw_tail = "all main-process runtime require()s are in dependencies"
    return chk


def verify_build(
    repo: Path,
    *,
    check_timeout: int = 1800,
) -> BuildVerificationOutcome:
    """Run the fixed build checklist from the repo root and decide the state."""
    missing = [
        name
        for name in ("package.json", "package-lock.json")
        if not (repo / name).is_file()
    ]
    if missing:
        return BuildVerificationOutcome(
            state=TaskState.ENVIRONMENT_BLOCKED,
            build=BuildResult(checks=[], all_passed=False),
            detail=(
                f"missing {', '.join(missing)} — the app must be scaffolded and "
                "`npm install` run so a lockfile exists in the change"
            ),
        )

    env = _build_env()
    checklist = _checklist()
    checks: list[BuildCheck] = []
    for name, argv in checklist:
        chk = BuildCheck(name=name, command=" ".join(argv))
        start = time.monotonic()
        run = _run_build_check(argv, cwd=repo, env=env, timeout=check_timeout)
        chk.ran = run.ran
        chk.exit_code = run.exit_code
        if run.error is not None:
            chk.ok = False
            chk.raw_tail = run.error
        elif run.timed_out:
            chk.ok = False
            chk.timed_out = True
            chk.raw_tail = run.output_tail
        else:
            chk.ok = run.exit_code == 0
            chk.raw_tail = run.output_tail
        chk.duration_seconds = round(time.monotonic() - start, 1)
        checks.append(chk)
        if not chk.ok:
            break  # later checks are meaningless once one fails

    subprocess_passed = len(checks) == len(checklist) and all(c.ok for c in checks)
    # A clean build + package does NOT prove the app runs: electron-builder
    # prunes devDependencies, so a runtime module left there packages fine but
    # makes the INSTALLED app crash on launch with `Cannot find module`.
    # Statically verify every module the built main require()s is in dependencies.
    if subprocess_passed:
        dep_chk = _check_runtime_deps(repo)
        checks.append(dep_chk)
        all_passed = dep_chk.ok
    else:
        all_passed = False
    build = BuildResult(checks=checks, all_passed=all_passed)

    if all_passed:
        # The package step must yield the host platform's installer deliverable
        # (.dmg on macOS, .exe on Windows, .AppImage/.deb on Linux). A clean exit
        # with no installer (e.g. config emitted only an unpacked dir) is NOT a
        # real success.
        installers = _find_installers(repo)
        if not installers:
            build.all_passed = False
            return BuildVerificationOutcome(
                state=TaskState.FAILED,
                build=build,
                detail="packaging exited cleanly but produced no installer",
            )
        build.installer_paths = installers
        build.installer_path = installers[0]
        return BuildVerificationOutcome(state=TaskState.VERIFIED, build=build)

    failed = next((c for c in checks if not c.ok), None)
    # Deps failing to install = the environment is the blocker; build/package
    # failing = the generated app does not build.
    state = (
        TaskState.ENVIRONMENT_BLOCKED
        if failed is not None and failed.name == "npm_ci"
        else TaskState.FAILED
    )
    if failed is not None and failed.timed_out:
        detail = (
            f"build check timed out: {failed.name} exceeded {check_timeout}s and was "
            f"killed (a hung or interactive-prompting build)\n{failed.raw_tail}"
        ).rstrip()
    elif failed is not None:
        detail = f"build check failed: {failed.name}\n{failed.raw_tail}".rstrip()
    else:
        detail = "build verification did not complete"
    return BuildVerificationOutcome(state=state, build=build, detail=detail)
