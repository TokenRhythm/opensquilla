"""Runner-authoritative red→green→regression verification.

The agent writes a machine-readable manifest (verification.json) into the
scratch dir declaring its acceptance tests. The runner does NOT trust the
agent's self-report: it independently re-runs each acceptance command at the
task HEAD (green) and, in a throwaway ``git worktree`` at the base commit
with the agent's test files overlaid, at the base (red). This distinguishes
a genuine fix from a no-op, and surfaces the false-negative states the
design review (#6) called out.

Manifest schema (v1):
    {
      "testable": true,
      "acceptance_tests": [
        {"name": "...", "command": "pytest tests/test_x.py::test_y",
         "test_paths": ["tests/test_x.py"]}
      ],
      "regression_command": "pytest -q",   // optional
      "assumptions": ["..."],              // optional
      "not_testable_reason": ""            // required iff testable is false
    }
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from opensquilla.contrib.codetask.config import (
    DEFAULT_ACCEPTANCE_TIMEOUT,
    DEFAULT_REGRESSION_TIMEOUT,
)
from opensquilla.contrib.codetask.types import (
    AcceptanceCheck,
    RegressionResult,
    TaskState,
)

logger = logging.getLogger(__name__)

_PYTEST_SUMMARY = re.compile(r"(\d+) (passed|failed|error|errors)")
# Failing node ids, e.g. "FAILED tests/test_x.py::test_y - AssertionError".
_PYTEST_FAILED_LINE = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)


@dataclass
class VerificationOutcome:
    state: TaskState
    acceptance: list[AcceptanceCheck]
    regression: RegressionResult | None
    assumptions: list[str]
    detail: str = ""


def load_manifest(scratch_dir: Path) -> dict | None:
    """Load and shallow-validate the agent's verification.json. None if absent."""
    from opensquilla.contrib.codetask.config import VERIFICATION_MANIFEST_NAME

    path = scratch_dir / VERIFICATION_MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _tail(text: str, *, max_lines: int = 40, max_chars: int = 4000) -> str:
    """Bounded tail of command output, for the report and retry evidence."""
    if not text:
        return ""
    tail = "\n".join(text.splitlines()[-max_lines:])
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail


def _cmd_timeout(base: int, deadline: float | None) -> int:
    """Per-command timeout bounded by a shared wall-clock deadline so the TOTAL
    verification (multiple commands) cannot run past it."""
    if deadline is None:
        return base
    return max(1, min(base, int(deadline - time.monotonic())))


def verify(
    *,
    repo: Path,
    base_commit: str,
    scratch_dir: Path,
    acceptance_timeout: int = DEFAULT_ACCEPTANCE_TIMEOUT,
    regression_timeout: int = DEFAULT_REGRESSION_TIMEOUT,
    deadline: float | None = None,
) -> VerificationOutcome:
    """Run the full verification protocol and decide the task state."""
    manifest = load_manifest(scratch_dir)
    if manifest is None:
        return VerificationOutcome(
            state=TaskState.INVALID_ACCEPTANCE_TEST,
            acceptance=[],
            regression=None,
            assumptions=[],
            detail="agent did not emit a valid verification.json manifest",
        )

    assumptions = [str(a) for a in manifest.get("assumptions", []) if str(a).strip()]

    if manifest.get("testable") is False:
        reason = str(manifest.get("not_testable_reason", "")).strip()
        return VerificationOutcome(
            state=TaskState.NOT_TESTABLE,
            acceptance=[],
            regression=None,
            assumptions=assumptions,
            detail=reason or "agent reported the task is not automatically testable",
        )

    raw_tests = manifest.get("acceptance_tests") or []
    checks: list[AcceptanceCheck] = []
    for entry in raw_tests:
        if not isinstance(entry, dict):
            continue
        cmd = str(entry.get("command", "")).strip()
        if not cmd:
            continue
        checks.append(
            AcceptanceCheck(
                name=str(entry.get("name") or cmd)[:80],
                command=cmd,
                expected="pass",
            )
        )
    test_paths_by_index = [
        [str(p) for p in (e.get("test_paths") or [])] if isinstance(e, dict) else []
        for e in raw_tests
    ]

    if not checks:
        return VerificationOutcome(
            state=TaskState.INVALID_ACCEPTANCE_TEST,
            acceptance=[],
            regression=None,
            assumptions=assumptions,
            detail="manifest declared testable but listed no runnable acceptance tests",
        )

    # GREEN: run each acceptance command at the current (post-change) tree.
    for check in checks:
        rc, out = _run_shell(
            check.command,
            cwd=repo,
            timeout=_cmd_timeout(acceptance_timeout, deadline),
            repo=repo,
        )
        check.after = "pass" if rc == 0 else "fail"
        check.green_exit_code = rc
        check.green_output_tail = _tail(out)

    # RED: re-run each acceptance command at base, with the agent's test files
    # overlaid, in a throwaway worktree. ``before`` is left None whenever we
    # could not establish the red state, and the reason is recorded so the
    # state machine can fail CLOSED rather than claim an unprovable VERIFIED.
    red_unprovable: str | None = None
    try:
        with _BaseWorktree(repo, base_commit) as wt:
            for check, paths in zip(checks, test_paths_by_index, strict=False):
                safe = _safe_rel_paths(paths)
                if not safe:
                    check.before = None  # no usable test paths -> cannot prove red
                    red_unprovable = red_unprovable or "missing_test_paths"
                    continue
                if not _overlay_paths(repo, wt, safe):
                    check.before = None
                    red_unprovable = red_unprovable or "missing_test_paths"
                    continue
                # Rewrite any hardcoded task-repo path so a ``cd /abs/repo`` in
                # the agent's command cannot teleport the red check back into
                # the already-fixed task repo.
                wt_command = _localize_command(check.command, repo, wt)
                rc, out = _run_shell(
                    wt_command,
                    cwd=wt,
                    timeout=_cmd_timeout(acceptance_timeout, deadline),
                    repo=repo,
                )
                check.before = "fail" if rc != 0 else "pass"
                check.red_exit_code = rc
                check.red_output_tail = _tail(out)
    except _WorktreeError as exc:
        logger.warning("base worktree unavailable, red phase skipped: %s", exc)
        red_unprovable = "worktree_failed"

    regression = _run_regression(
        manifest.get("regression_command"),
        repo=repo,
        base_commit=base_commit,
        timeout=regression_timeout,
        deadline=deadline,
    )

    state, detail = _decide_state(checks, regression, red_unprovable)
    return VerificationOutcome(
        state=state,
        acceptance=checks,
        regression=regression,
        assumptions=assumptions,
        detail=detail,
    )


def verify_scratch(
    *,
    repo: Path,
    scratch_dir: Path,
    acceptance_timeout: int = DEFAULT_ACCEPTANCE_TIMEOUT,
    deadline: float | None = None,
) -> VerificationOutcome:
    """Green-only verification for from-scratch standalone code.

    The agent's acceptance tests must pass on the produced code. There is no
    red phase on an empty base and no regression suite because there is no
    pre-existing project.
    """
    manifest = load_manifest(scratch_dir)
    if manifest is None:
        return VerificationOutcome(
            state=TaskState.INVALID_ACCEPTANCE_TEST,
            acceptance=[],
            regression=None,
            assumptions=[],
            detail="agent did not emit a valid verification.json manifest",
        )
    assumptions = [str(a) for a in manifest.get("assumptions", []) if str(a).strip()]
    if manifest.get("testable") is False:
        reason = str(manifest.get("not_testable_reason", "")).strip()
        return VerificationOutcome(
            state=TaskState.NOT_TESTABLE,
            acceptance=[],
            regression=None,
            assumptions=assumptions,
            detail=reason or "agent reported the task is not testable",
        )
    checks: list[AcceptanceCheck] = []
    for entry in manifest.get("acceptance_tests") or []:
        if not isinstance(entry, dict):
            continue
        cmd = str(entry.get("command", "")).strip()
        if not cmd:
            continue
        checks.append(
            AcceptanceCheck(
                name=str(entry.get("name") or cmd)[:80],
                command=cmd,
                expected="pass",
            )
        )
    if not checks:
        return VerificationOutcome(
            state=TaskState.INVALID_ACCEPTANCE_TEST,
            acceptance=[],
            regression=None,
            assumptions=assumptions,
            detail="manifest declared testable but listed no runnable acceptance tests",
        )
    all_pass = True
    for check in checks:
        rc, out = _run_shell(
            check.command,
            cwd=repo,
            timeout=_cmd_timeout(acceptance_timeout, deadline),
            repo=repo,
        )
        check.after = "pass" if rc == 0 else "fail"
        check.green_exit_code = rc
        check.green_output_tail = _tail(out)
        if rc != 0:
            all_pass = False
    return VerificationOutcome(
        state=TaskState.VERIFIED if all_pass else TaskState.FAILED,
        acceptance=checks,
        regression=None,
        assumptions=assumptions,
        detail="" if all_pass else "the from-scratch acceptance test did not pass (green-only)",
    )


def _decide_state(
    checks: list[AcceptanceCheck],
    regression: RegressionResult | None,
    red_unprovable: str | None,
) -> tuple[TaskState, str]:
    """Decide the task state, failing CLOSED when proof is incomplete."""
    all_green = all(c.after == "pass" for c in checks)
    if not all_green:
        return TaskState.FAILED, "an acceptance test did not pass after the change"

    regressed = bool(regression and regression.ran and (regression.new_failures or 0) > 0)
    if regressed:
        return TaskState.FAILED, "the change introduced new regression failures"

    # All acceptance green. We may only claim VERIFIED if every check was
    # independently proven red on the base commit.
    befores = [c.before for c in checks]
    if all(b == "fail" for b in befores):
        return TaskState.VERIFIED, ""
    if all(b == "pass" for b in befores):
        return TaskState.ALREADY_SATISFIED, "expected behavior already held on the base commit"

    # Green but red not (fully) established -> cannot prove the change matters.
    if red_unprovable == "worktree_failed":
        return (
            TaskState.ENVIRONMENT_BLOCKED,
            "acceptance passed but the base worktree could not be built to prove the red state",
        )
    return (
        TaskState.INVALID_ACCEPTANCE_TEST,
        "acceptance passed but its red state could not be proven (declare test_paths so the "
        "runner can re-create the failing state on the original code)",
    )


def _run_regression(
    command,
    *,
    repo: Path,
    base_commit: str,
    timeout: int,
    deadline: float | None = None,
) -> RegressionResult | None:
    if not command or not str(command).strip():
        return None
    cmd = str(command).strip()
    result = RegressionResult(command=cmd, ran=True)

    head_rc, head_out = _run_shell(
        cmd, cwd=repo, timeout=_cmd_timeout(timeout, deadline), repo=repo
    )
    head_fail = _parse_failures(head_out, head_rc)
    head_names = _failing_names(head_out)
    result.passed = _parse_passes(head_out)
    result.failed = head_fail
    result.raw_tail = "\n".join(head_out.splitlines()[-15:])

    # Differential: run the same command at base to avoid penalizing
    # pre-existing failures.
    base_rc: int | None = None
    base_fail: int | None = None
    base_names: set[str] | None = None
    try:
        with _BaseWorktree(repo, base_commit) as wt:
            base_rc, base_out = _run_shell(
                _localize_command(cmd, repo, wt),
                cwd=wt,
                timeout=_cmd_timeout(timeout, deadline),
                repo=repo,
            )
            base_fail = _parse_failures(base_out, base_rc)
            base_names = _failing_names(base_out)
    except _WorktreeError:
        pass

    # Prefer a set difference of named failures (a new failure cannot be
    # masked by a fixed pre-existing one — codex review #4).
    if head_names is not None and base_names is not None:
        result.new_failures = len(head_names - base_names)
    elif base_fail is not None and head_fail is not None:
        result.new_failures = max(0, head_fail - base_fail)
    elif head_fail is not None:
        # No usable baseline: any head failure counts as new (conservative).
        result.new_failures = head_fail
    elif head_rc != 0:
        # Nonzero exit we could not parse: fail CLOSED, treat as regressed
        # (codex review #3 — do NOT silently report clean).
        result.new_failures = 1
    else:
        result.new_failures = 0
    return result


# ---------------------------------------------------------------------------
# Shell + git worktree helpers
# ---------------------------------------------------------------------------
class _WorktreeError(RuntimeError):
    pass


def _path_variants(p: Path) -> list[str]:
    """All plausible string forms an agent may use for an absolute path.

    On Windows the agent runs under Git Bash (so ``_run_shell`` invokes
    bash), but may write any of three equivalent forms:
      - native:     ``C:\\src\\opensquilla``
      - forward:    ``C:/src/opensquilla``
      - MSYS/posix: ``/c/src/opensquilla``

    On POSIX, native and forward forms collapse to the same string.
    """
    native = str(p)
    forms = {native}
    if os.name == "nt":
        forward = native.replace("\\", "/")
        forms.add(forward)
        # Drive-letter -> MSYS form: "C:/foo" -> "/c/foo".
        if len(forward) >= 2 and forward[1] == ":":
            drive = forward[0].lower()
            forms.add(f"/{drive}{forward[2:]}")
    return list(forms)


def _localize_command(command: str, repo: Path, target: Path) -> str:
    """Redirect any absolute reference to the task repo onto ``target``.

    The agent writes acceptance/regression commands while standing inside the
    task repo, so they frequently hardcode its absolute path (e.g.
    ``cd /abs/repo && PYTHONPATH=src pytest ...``). When the runner re-runs
    such a command in the base worktree to establish the red state, that
    absolute ``cd`` would teleport execution back into the agent-fixed task
    repo and silently contaminate the check (the test passes against the fix,
    so the runner wrongly concludes the behavior was already satisfied).

    Rewriting the task-repo path to the worktree path keeps the command inside
    the intended tree regardless of how the agent wrote it. Longest match
    first so ``/abs/repo/src`` is rewritten before ``/abs/repo``. We match
    every plausible form (native, forward-slash, MSYS) since ``_run_shell``
    invokes bash on Windows. On Windows the match is case-insensitive
    because the underlying file system is.

    The repo path is only rewritten when it is followed by ``/`` (a subpath),
    end-of-string, or an unambiguous shell path-terminator (whitespace, quote,
    or a shell metacharacter). Any other following character — including the
    many filename-legal chars like ``-+@=,~.`` and digits — is treated as a
    SIBLING path (e.g. ``/abs/repo-fixture``, ``/abs/repo+x``) and left intact,
    rather than trying to enumerate the near-infinite set of filename chars.
    """
    sources: set[str] = set()
    for base in {repo, repo.resolve()}:
        sources.update(_path_variants(base))
    candidates = sorted(sources, key=len, reverse=True)

    # Use a forward-slash form of the target so the rewritten command stays
    # bash-friendly on Windows (backslashes inside double-quoted shell args
    # are escapes).
    target_str = str(target)
    if os.name == "nt":
        target_str = target_str.replace("\\", "/")

    flags = re.IGNORECASE if os.name == "nt" else 0
    out = command
    for src in candidates:
        # Positive lookahead for a real path boundary: a path separator
        # ('/' on POSIX, also '\\' on Windows), whitespace, end-of-string, or
        # a shell word terminator.
        pattern = re.escape(src) + r"(?=[/\\]|\s|$|[" + re.escape("'\"`:;&|<>()") + r"])"
        out = re.sub(pattern, lambda _m: target_str, out, flags=flags)
    return out


_BASH_PROBE_SENTINEL = "__opensquilla_bash_probe_ok__"
_BASH_PROBE_TIMEOUT = 5.0
# Probe classification: native bash (usable as-is), a WSL launcher (real bash,
# but a Linux userland — last-resort fallback only), or unusable (stub,
# busybox, broken).
_BASH_KIND_NATIVE = "native"
_BASH_KIND_WSL = "wsl"
_BASH_KIND_UNUSABLE = "unusable"
# Exit code the probe script reserves for "real bash, but Linux userland".
_BASH_PROBE_WSL_EXIT = 42
# Process-level cache: bash location is stable for the gateway's lifetime.
# Tuple form (resolved, path) so a cached "not found" doesn't keep re-probing.
_BASH_RESOLVED: bool = False
_BASH_CACHED: str | None = None


def _windows_bash_candidates() -> list[str]:
    """Ordered candidate paths to try as ``bash`` on Windows.

    Walks every ``bash.<PATHEXT>`` hit on PATH, then the standard
    Git-for-Windows install locations as a last-resort fallback. The
    ``OPENSQUILLA_BASH`` env override (if set) is yielded first so an
    operator can force a specific shell without rearranging PATH.
    Caller probes each in order; first one that passes wins.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(p: str | None) -> None:
        if not p:
            return
        key = os.path.normcase(os.path.abspath(p))
        if key in seen:
            return
        seen.add(key)
        out.append(p)

    _add(os.environ.get("OPENSQUILLA_BASH"))

    pathext = [
        e for e in os.environ.get("PATHEXT", ".EXE").split(os.pathsep) if e
    ] or [".EXE"]
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        for ext in pathext:
            cand = os.path.join(d, "bash" + ext)
            if os.path.isfile(cand):
                _add(cand)

    for base in (
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("LOCALAPPDATA"),
    ):
        if not base:
            continue
        for rel in (
            r"Git\usr\bin\bash.exe",
            r"Git\bin\bash.exe",
            r"Programs\Git\usr\bin\bash.exe",
            r"Programs\Git\bin\bash.exe",
        ):
            cand = os.path.join(base, rel)
            if os.path.isfile(cand):
                _add(cand)
    return out


def _probe_bash_kind(path: str) -> str:
    """Classify ``path``: native bash, a WSL launcher, or unusable.

    Runs ``-lc`` with a bash-specific invariant (``$BASH_VERSION`` is set
    by every real bash and unset by busybox, dash, and a ``bash.cmd`` wrapper
    that calls ``exit /b``). A real bash whose ``uname -s`` reports Linux is
    the WSL-launcher class on Windows: real bash, but without native Windows
    path and ``-lc`` command-line variable semantics, so it is only eligible
    as a loudly-warned last resort (see :func:`_resolve_bash`). Native
    requires both returncode 0 AND the sentinel in stdout so a stub that
    happens to exit 0 but echoes nothing — or echoes garbage — is rejected.
    """
    try:
        proc = subprocess.run(
            [
                path,
                "-lc",
                (
                    'test -n "$BASH_VERSION" && '
                    'case "$(uname -s 2>/dev/null)" in Linux*) '
                    f"exit {_BASH_PROBE_WSL_EXIT};; esac && "
                    f"echo {_BASH_PROBE_SENTINEL}"
                ),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_BASH_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return _BASH_KIND_UNUSABLE
    if proc.returncode == 0 and _BASH_PROBE_SENTINEL in (proc.stdout or ""):
        return _BASH_KIND_NATIVE
    if proc.returncode == _BASH_PROBE_WSL_EXIT:
        return _BASH_KIND_WSL
    return _BASH_KIND_UNUSABLE


def _probe_bash(path: str) -> bool:
    """True iff ``path`` is native bash (not a stub, busybox, or WSL)."""
    return _probe_bash_kind(path) == _BASH_KIND_NATIVE


def _resolve_bash() -> str | None:
    """Find a working ``bash`` (or None).

    POSIX is bit-equivalent to the original ``shutil.which("bash")`` lookup —
    called fresh on every invocation, no cache, no probe — because the
    busybox/WSL-stub class of failure is Windows-only and any caching layer
    would diverge from the old behavior if ``PATH`` changes mid-process.
    Windows enumerates candidates, probes each (see
    :func:`_windows_bash_candidates`, :func:`_probe_bash_kind`), and memoizes
    the result for the gateway's lifetime so per-shell-call probe cost stays
    at one round-trip total. Native bash is preferred regardless of candidate
    order; when no native candidate probes successfully, a WSL bash launcher
    is used as a loudly-warned last resort so Windows setups whose
    verification commands ran under WSL keep working.
    """
    if os.name != "nt":
        return shutil.which("bash")
    global _BASH_RESOLVED, _BASH_CACHED
    if _BASH_RESOLVED:
        return _BASH_CACHED
    wsl_fallback: str | None = None
    for cand in _windows_bash_candidates():
        kind = _probe_bash_kind(cand)
        if kind == _BASH_KIND_NATIVE:
            _BASH_CACHED = cand
            _BASH_RESOLVED = True
            return cand
        if kind == _BASH_KIND_WSL and wsl_fallback is None:
            wsl_fallback = cand
    if wsl_fallback is not None:
        logger.warning(
            "code-task verification found no native Windows bash; falling "
            "back to the WSL bash launcher at %s. Windows paths and "
            "`bash -lc` variable semantics may not behave natively under "
            "WSL — install Git Bash (or point OPENSQUILLA_BASH at a native "
            "bash.exe) for full support.",
            wsl_fallback,
        )
    _BASH_CACHED = wsl_fallback
    _BASH_RESOLVED = True
    return _BASH_CACHED


def _reset_bash_cache() -> None:
    """Test helper: drop the memoized bash resolution."""
    global _BASH_RESOLVED, _BASH_CACHED
    _BASH_RESOLVED = False
    _BASH_CACHED = None


def _repo_venv_python_candidates(repo: Path) -> tuple[Path, ...]:
    return (
        repo / ".venv" / "bin" / "python",
        repo / ".venv" / "Scripts" / "python.exe",
        repo / ".venv" / "Scripts" / "python",
    )


def _bash_path_entry(path: Path) -> str:
    raw = str(path)
    if os.name == "nt":
        raw = raw.replace("\\", "/")
        if len(raw) >= 2 and raw[1] == ":":
            raw = f"/{raw[0].lower()}{raw[2:]}"
    return raw


def _write_python_shim(shim_dir: Path, name: str, venv_python: Path) -> None:
    target = shim_dir / name
    try:
        target.symlink_to(venv_python)
        return
    except OSError:
        pass
    script = (
        "#!/usr/bin/env bash\n"
        f'exec {shlex.quote(_bash_path_entry(venv_python))} "$@"\n'
    )
    target.write_text(script, encoding="utf-8", newline="\n")
    try:
        target.chmod(0o755)
    except OSError:
        pass


def _run_shell(
    command: str, *, cwd: Path, timeout: int, repo: Path | None = None
) -> tuple[int, str]:
    """Run a manifest acceptance/regression command in a POSIX-flavored shell.

    Verification commands the agent records use POSIX shell semantics
    (``VAR=val command``, pipelines, ``&&``). We require ``bash`` to honor
    them. On Windows the user should install Git Bash; when only a WSL bash
    launcher is available it is used as a loudly-warned last resort (it does
    not provide native Windows path and ``-lc`` variable semantics, but it is
    the shell such setups already ran their commands under). We surface a
    clear error instead of an opaque ``FileNotFoundError`` if no usable bash
    is available at all.
    """
    bash = _resolve_bash()
    if bash is None:
        if os.name == "nt":
            hint = (
                "no working bash found (install Git Bash, or set "
                "OPENSQUILLA_BASH to a working bash.exe; fake bash.cmd "
                "stubs and a broken or unconfigured WSL launcher are "
                "skipped automatically)"
            )
        else:
            # Bit-equivalent to the original message on Linux/Mac.
            hint = "bash not found on PATH"
        return -1, f"OSERROR: {hint}"
    shim: Path | None = None
    prefixed = command
    if repo is not None:
        repo = Path(repo)
        exports = ""
        # uv project: point `uv run` at THIS run repo's project + .venv even when
        # cwd is the base worktree (which has no venv of its own), via UV_PROJECT.
        # Without it a manifest `uv run ...` re-run from the worktree would build
        # a SEPARATE wt/.venv (slow), and "deps missing" there could masquerade
        # as a valid red. UV_PROJECT keeps the one-venv reuse while ensuring deps.
        if (repo / "uv.lock").exists():
            exports += f"export UV_PROJECT={shlex.quote(str(repo))}; "
        # Make BOTH `python` and `python3` resolve to the run repo's venv, for
        # bare-interpreter manifests re-run in a plain (non-activated) shell and
        # in the base worktree. uv venvs often expose only `.venv/bin/python`,
        # so a small shim covers `python3` too. Exports run AFTER `bash -lc`
        # startup files, so they win over any login-profile PATH.
        venv_python = next((p for p in _repo_venv_python_candidates(repo) if p.exists()), None)
        if venv_python is not None:
            try:
                shim = Path(tempfile.mkdtemp(prefix="codetask-pyshim-"))
                for _name in ("python", "python3"):
                    _write_python_shim(shim, _name, venv_python)
                _vbin = shlex.quote(
                    f"{_bash_path_entry(shim)}:{_bash_path_entry(venv_python.parent)}"
                )
                exports += f'export PATH={_vbin}:"$PATH"; '
            except OSError:
                shim = None
        if exports:
            prefixed = exports + command
    try:
        proc = subprocess.run(
            [bash, "-lc", prefixed],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"
    except OSError as exc:
        return -1, f"OSERROR: {exc}"
    finally:
        if shim is not None:
            shutil.rmtree(shim, ignore_errors=True)


class _BaseWorktree:
    """Context manager: a detached worktree at base_commit, auto-removed."""

    def __init__(self, repo: Path, base_commit: str):
        self.repo = repo
        self.base_commit = base_commit
        self._dir: Path | None = None

    def __enter__(self) -> Path:
        import shutil

        tmp = Path(tempfile.mkdtemp(prefix="codetask-base-"))
        try:
            r = subprocess.run(
                ["git", "worktree", "add", "--detach", str(tmp), self.base_commit],
                cwd=str(self.repo),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            shutil.rmtree(tmp, ignore_errors=True)
            raise _WorktreeError(str(exc)) from exc
        if r.returncode != 0:
            # Do not leak the mkdtemp dir when the worktree was never added
            # (codex review #9).
            shutil.rmtree(tmp, ignore_errors=True)
            raise _WorktreeError((r.stderr or "").strip()[-200:])
        self._dir = tmp
        return tmp

    def __exit__(self, *exc) -> None:
        if self._dir is not None:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(self._dir)],
                cwd=str(self.repo),
                capture_output=True,
                timeout=60,
            )


def _safe_rel_paths(paths: list[str]) -> list[str]:
    """Keep only repo-relative paths (reject absolute and parent-escaping).

    Agent-declared test_paths are untrusted input; an absolute path or one
    containing ``..`` could read/overlay files outside the repo (codex
    review #7).
    """
    safe: list[str] = []
    for rel in paths:
        # Accept either separator — an agent running on Windows may emit
        # ``tests\test_x.py``; normalize to POSIX before validating so the
        # downstream copy still works.
        p = str(rel).strip().replace("\\", "/")
        if not p:
            continue
        # Reject POSIX-absolute, UNC ("//server/share"), and Windows
        # drive-letter absolute ("C:/...") paths.
        if p.startswith("/") or (len(p) >= 2 and p[1] == ":"):
            continue
        if any(part == ".." for part in PurePosixPath(p).parts):
            continue
        safe.append(p)
    return safe


def _overlay_paths(repo: Path, worktree: Path, paths: list[str]) -> bool:
    """Copy the given files from the task tree into the base worktree.

    Used so a NEW acceptance test (which does not exist at base) can be run
    against base source. Paths are assumed pre-validated by
    :func:`_safe_rel_paths`. Returns False if any path is missing or a copy
    fails (so the caller leaves ``before`` unproven rather than crashing).
    """
    import shutil

    for rel in paths:
        src = (repo / rel).resolve()
        try:
            src.relative_to(repo.resolve())
        except ValueError:
            return False  # symlink/escape outside the repo
        if not src.is_file():
            return False
        dest = worktree / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        except OSError:
            return False
    return True


def _failing_names(output: str) -> set[str] | None:
    """Extract failing test node ids from pytest-style output.

    Returns None when no ``FAILED <nodeid>`` lines are present (so the caller
    falls back to counts). Enables a set difference that a count cannot do.
    """
    names = set(_PYTEST_FAILED_LINE.findall(output))
    return names or None


def _parse_failures(output: str, returncode: int) -> int | None:
    counts = {kind: int(n) for n, kind in _PYTEST_SUMMARY.findall(output)}
    if counts:
        return counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
    if returncode == 0:
        return 0
    return None  # nonzero but unparseable: unknown count


def _parse_passes(output: str) -> int | None:
    counts = {kind: int(n) for n, kind in _PYTEST_SUMMARY.findall(output)}
    return counts.get("passed") if counts else None
