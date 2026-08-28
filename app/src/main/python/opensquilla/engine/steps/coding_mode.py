"""Pre-turn step: enforce coding mode when the operator toggle is ON.

Coding mode is an operator toggle, not an intent classifier. While it is ON,
turns with the complete code-task launch surface get a directive that steers
code changes through the code-task plugin (``opensquilla code-task solve``)
instead of letting the agent clone and hand-edit repositories itself (which
skips the runner-verified red->green proof). Restricted surfaces that cannot
launch code-task receive neither the directive nor the skill. No per-message
detection is performed.

The directive does NOT tell the agent to run a BARE ``opensquilla``: the gateway
shell tools inherit the gateway process PATH, which frequently does NOT contain
the CLI's bin (the gateway is commonly started via an absolute interpreter path
or a packaged desktop binary). A bare command then fails to resolve, and the
agent tends to "self-install" and degrade to hand-editing. So the directive
injects a PATH-independent invocation resolved from the running process (see
``resolve_code_task_command``), and falls back to a fail-loud directive when
code-task cannot be run at all.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import structlog

from opensquilla.engine.pipeline import TurnContext

log = structlog.get_logger(__name__)

_CODE_TASK_PREFLIGHT_TIMEOUT = 15.0
_PACKAGED_GATEWAY_EXECUTABLES = {"opensquilla-gateway", "opensquilla-gateway.exe"}
_CODE_TASK_REQUIRED_TOOLS = frozenset({"background_process", "exec_command", "process"})


def _runs_code_task(argv: list[str]) -> bool:
    """True iff ``<argv> code-task --help`` exits 0 — i.e. the invocation works."""
    try:
        proc = subprocess.run(
            [*argv, "code-task", "--help"],
            capture_output=True,
            timeout=_CODE_TASK_PREFLIGHT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


_resolved_command: str | None = None
_resolution_succeeded = False


def _reset_resolution_cache() -> None:
    """Test helper: drop the memoized resolution."""
    global _resolved_command, _resolution_succeeded
    _resolved_command = None
    _resolution_succeeded = False


def resolve_code_task_command() -> str | None:
    """Resolve a PATH-independent, runnable ``code-task`` command prefix.

    Returns a shell-ready prefix ending at ``code-task`` (the caller appends
    ``solve ...``), e.g. ``/opt/env/bin/opensquilla code-task``,
    ``/Applications/OpenSquilla.app/.../opensquilla-gateway code-task``, or
    ``/opt/env/bin/python -P -m opensquilla.cli.main code-task``; or ``None`` if
    code-task cannot be run here.

    A SUCCESSFUL resolution is memoized for the process lifetime (the
    interpreter / install layout is fixed while the gateway runs). A FAILURE is
    NOT cached, so a transient preflight timeout cannot permanently flip coding
    mode into the 'unavailable' directive until a restart.
    """
    global _resolved_command, _resolution_succeeded
    if _resolution_succeeded:
        return _resolved_command
    cmd = _resolve_code_task_command_uncached()
    if cmd is not None:
        _resolved_command = cmd
        _resolution_succeeded = True
    return cmd


def _quote(path: str) -> str:
    """Shell-quote a path for the HOST shell (POSIX sh vs Windows cmd/PowerShell)."""
    if os.name == "nt":
        return f'"{path}"' if any(c in path for c in ' ()&') else path
    return shlex.quote(path)


def _is_packaged_gateway_executable(path: str) -> bool:
    name = Path(path).name
    if name == path and ("\\" in path or ":" in path):
        name = PureWindowsPath(path).name
    return name.lower() in _PACKAGED_GATEWAY_EXECUTABLES


def _resolve_code_task_command_uncached() -> str | None:
    base = Path(sys.executable).parent
    # 1) The console script next to the running interpreter. On Windows it is
    #    opensquilla.exe / .cmd in the venv Scripts dir, not a bare name.
    for name in ("opensquilla", "opensquilla.exe", "opensquilla.cmd", "opensquilla.bat"):
        cand = base / name
        if cand.is_file() and os.access(cand, os.X_OK) and _runs_code_task([str(cand)]):
            return f"{_quote(str(cand))} code-task"
    # 2) Packaged desktop gateways are PyInstaller CLI binaries
    #    (opensquilla-gateway[.exe]), so sys.executable itself may own
    #    ``code-task`` even though it is not named ``opensquilla``.
    if _is_packaged_gateway_executable(sys.executable) and _runs_code_task([sys.executable]):
        return f"{_quote(sys.executable)} code-task"
    # 3) Module invocation via the EXACT interpreter the gateway runs on. ``-P``
    #    (Python 3.11+) keeps a cwd ``opensquilla`` package from shadowing the
    #    import once the agent cd's into a target repo.
    if _runs_code_task([sys.executable, "-P", "-m", "opensquilla.cli.main"]):
        return f"{_quote(sys.executable)} -P -m opensquilla.cli.main code-task"
    # 4) Whatever ``opensquilla`` is on PATH (shutil.which honors PATHEXT on
    #    Windows) — but only if it actually runs.
    on_path = shutil.which("opensquilla")
    if on_path and _runs_code_task([on_path]):
        return f"{_quote(on_path)} code-task"
    return None


_CODING_MODE_DIRECTIVE_TEMPLATE = (
    "\n\n[CODING MODE — ACTIVE]\n"
    "The operator has enabled coding mode for this session. For ANY request to "
    "WRITE or CHANGE code you MUST do the work THROUGH code-task — NEVER by "
    "typing the code directly in your reply, and never by hand-editing files. "
    "Choose the matching case. (1) The user NAMES a real repository (a "
    "filesystem path or a git URL) — fix a bug, add/implement a feature, edit a "
    "file, resolve a GitHub issue — run\n"
    "    __CODE_TASK_CMD__ solve --repo <url-or-path> "
    "(--issue N | --task-file <path>) --shallow --yes\n"
    "Use that command EXACTLY as written above — it is resolved to run in THIS "
    "environment regardless of PATH. Do NOT replace it with a bare "
    "`opensquilla`, do NOT `pip install` OpenSquilla, and if that command fails "
    "to run, STOP and report that code-task / the environment is broken instead "
    "of working around it by hand.\n"
    "For building an app or UI from scratch (e.g. an Electron + React "
    "desktop app), add --verification-mode build with NO --repo (code-task "
    "scaffolds the new app's workspace itself) so code-task verifies the "
    "app compiles and packages instead of running red->green tests.\n"
    "(2) The user asks for SELF-CONTAINED, TESTABLE code FROM SCRATCH and names "
    "NO repo (e.g. 'write a python function that maps A-Z to pitches', 'write a "
    "script that parses a log'): this STILL goes through code-task — do NOT "
    "answer by typing the code in your reply — run\n"
    "    __CODE_TASK_CMD__ solve --task-file <path> --verification-mode "
    "scratch --yes\n"
    "(no --repo); code-task scaffolds a throwaway project, writes the code plus a "
    "test, and verifies it green. ONLY answer inline (no code-task) for trivial "
    "one-liners, pseudocode, or conceptual / non-deterministic / GUI- or "
    "network-dependent requests that cannot be expressed as a quick automated "
    "test. A real function with input->output logic (like the pitch example) is "
    "NOT trivial — use case (2), do not answer it inline.\n"
    "BUILD-FROM-SCRATCH — ASK BRIEFLY WHEN THE REQUEST IS ONLY A CATEGORY: "
    "decide by whether you know WHAT THE APP SHOULD DO, not just what kind it "
    "is. If the request gives only a broad app type or goal with no concrete "
    "features, target user, or scope (e.g. 'make me an English-learning app', "
    "'build a drawing app', 'make me an app'), ASK 1-2 focused questions — most "
    "usefully its core features/screens and who it is for — and STOP this turn "
    "(do NOT call code-task until the user answers). If the request already "
    "names concrete features, scope, or target users, do NOT ask — build it, "
    "choosing sensible defaults for the rest and briefly stating your "
    "assumptions. NEVER ask about platform, framework, or styling; just default "
    "those. Keep it to at most 2 questions and never interrogate. (Concrete bug "
    "fixes or feature changes in a named repo never need this.)\n"
    "For a follow-up change to an app you already built (e.g. add a section "
    "or change the color), point --repo at that app's repo and keep "
    "--verification-mode build; code-task edits the existing app in place and "
    "applies the verified change back to the repo so the next edit builds on it.\n"
    "code-task runs for MANY minutes (often 20-40, up to ~90 on a heavy repo "
    "that must install dependencies), so it is a long-running task: ALWAYS "
    "launch it with background_process(timeout=5400) and then await it with "
    'process(action="wait", session_id=..., timeout=5400). Do NOT run '
    "code-task with a blocking exec_command — exec_command is hard-capped at "
    "600s (10 min) no matter what timeout you pass, so it would kill "
    "code-task mid-run and waste the attempt. Do not poll "
    "process(action=\"poll\") in a loop either — just wait for the result.\n"
    "code-task works in an ISOLATED run directory, NOT in the --repo source you "
    "pass it: that source stays EMPTY until a run finishes and VERIFIES, then "
    "the change is committed back. So do NOT judge progress by the source "
    "repo's files and do NOT conclude it is 'stuck' because the source looks "
    "empty. Let process(action=\"wait\") return the result — do NOT kill the "
    "run, do NOT 'clean and retry', and do NOT launch the same task again while "
    "one is still running. Decide success ONLY from the returned result (its "
    "state and build.installer_path, which point into the run directory); the "
    "run prints its run directory on startup and writes live progress to "
    "<run_dir>/status.json. That status file includes phase, pid, current_command, "
    "log_paths, last_output_at, and terminal error/final_failure_reason fields; "
    "for build mode it also reports scaffold_running/scaffold_complete, "
    "agent_running, verifying, completed, or stalled.\n"
    "If process(action=\"wait\") returns a non-zero exit, an environment_blocked "
    "state, or a build result with no installer_path for build mode, STOP and "
    "report the code-task result. Do NOT manually rebuild the app, copy files, "
    "or publish a workaround artifact from this session.\n"
    "Do NOT clone the repository yourself and do NOT hand-edit its files in "
    "this session: the file-editing tools (write_file, edit_file, apply_patch, "
    "execute_code, git_commit, create_*) are DISABLED while coding mode is on, and "
    "in-session edits skip code-task's isolation and the "
    "runner-verified red→green proof, so they are not equivalent and are not "
    "allowed while coding mode is on. Read-only requests (showing structure, "
    "explaining code) and ordinary conversation are answered normally.\n"
    "TASK-FILE STAGING — task text ALWAYS goes through --task-file. "
    "Inline `--task \"<text>\"` is FORBIDDEN in coding mode, regardless of "
    "length, language, or complexity (no escape hatch). cmd.exe on "
    "Windows truncates a command string at the first literal newline "
    "inside a quoted argument and silently drops everything after it — "
    "including --yes — so an inline multi-line --task hangs code-task at "
    "[y/N] until the parent timeout fires. Embedded \" in the text "
    "breaks argv on POSIX too. The escape hatch is to send the task "
    "content through a PIPE, which neither shell touches:\n"
    "    1. exec_command(\n"
    "         command=\"__CODE_TASK_CMD__ stage-task-file\",\n"
    "         stdin=\"<task text, any length, any chars, any newlines>\",\n"
    "       )\n"
    "    # stage-task-file creates an atomic, 0600 unique file and emits a SHELL-SAFE QUOTED "
    "TOKEN on its own stdout line — `\"<path>\"` on Windows (safe for "
    "spaces / & / non-ASCII in ordinary %TEMP% paths; cmd.exe DOES "
    "still expand %VAR% inside double quotes, but %TEMP% itself is "
    "normally free of '%' so this is robust in practice -- argv "
    "support in background_process is the long-term airtight fix) or "
    "shlex.quote(path) on POSIX (defends against $, backticks, and "
    "backslash quirks in $TMPDIR). exec_command's return is formatted "
    "as `exit_code=0\\n<stdout>`; confirm exit_code=0, then take the "
    "final stdout line — the already-shell-quoted path — and paste it "
    "verbatim as the --task-file argument.\n"
    "    2a. (Case 1, real repo) background_process(\n"
    "          command=\"__CODE_TASK_CMD__ solve --repo <url-or-path> "
    "--task-file <quoted-path-from-step-1> --shallow --yes\",\n"
    "          timeout=5400,\n"
    "        )\n"
    "    2b. (Case 2, scratch) background_process(\n"
    "          command=\"__CODE_TASK_CMD__ solve --task-file <quoted-"
    "path-from-step-1> --verification-mode scratch --yes\",\n"
    "          timeout=5400,\n"
    "        )\n"
    "    2c. (Case 3, app build from scratch) background_process(\n"
    "          command=\"__CODE_TASK_CMD__ solve --task-file <quoted-"
    "path-from-step-1> --verification-mode build --yes\",\n"
    "          timeout=5400,\n"
    "        )\n"
    "Why this works: stdin rides through a real OS pipe — neither cmd.exe "
    "nor bash touches its contents. The already-shell-quoted path the "
    "script prints is one safe argv token, so --task-file survives and "
    "--yes is honored. No trivial-case exception: even a one-line task "
    "uses the staging recipe so a future longer task in the same session "
    "cannot regress.\n"
    "Cleanup: the staged task file persists under %TEMP% / $TMPDIR (task "
    "prose can contain private issue text). After "
    "process(action='wait', ...) returns the task result, drop the "
    "file with a separate exec_command (`del <quoted-path>` on Windows, "
    "`rm <quoted-path>` on POSIX). If you forget, the OS sweeps temp on "
    "reboot."
)

_CODING_MODE_UNAVAILABLE_DIRECTIVE = (
    "\n\n[CODING MODE — ACTIVE, but code-task is UNAVAILABLE]\n"
    "The operator enabled coding mode, which requires every code change to go "
    "through the code-task plugin — but `opensquilla code-task` cannot be run "
    "in this environment (the OpenSquilla CLI is not installed or not runnable "
    "here). For ANY request that would change code, STOP and tell the user that "
    "code-task is unavailable and the environment must be fixed (install "
    "OpenSquilla so `opensquilla code-task` runs — e.g. `bash "
    "scripts/install_source.sh`, which uses uv to provision Python 3.12). "
    "Do NOT try to `pip install` OpenSquilla yourself, do NOT clone the "
    "repository, and do NOT hand-edit files via the shell as a workaround — the "
    "in-session file-editing tools are disabled and a manual workaround skips "
    "code-task's isolation and verification. Do NOT run any installation or "
    "repair commands yourself (no `pip install`, no `bash scripts/install_source.sh`, "
    "no building OpenSquilla) — surface the problem to the user/operator and let "
    "THEM fix the environment. Read-only requests (showing "
    "structure, explaining code) and ordinary conversation are answered "
    "normally."
)


def _build_coding_mode_directive() -> str:
    """The coding-mode directive with a resolved, runnable code-task command,
    or a fail-loud directive when code-task cannot be run in this environment.

    One placeholder is substituted:
    - ``__CODE_TASK_CMD__`` — the resolved code-task invocation prefix.
    """
    cmd = resolve_code_task_command()
    if cmd is None:
        return _CODING_MODE_UNAVAILABLE_DIRECTIVE
    return _CODING_MODE_DIRECTIVE_TEMPLATE.replace("__CODE_TASK_CMD__", cmd)


def _coding_mode_on(ctx: TurnContext) -> bool:
    skills_cfg = getattr(ctx.config, "skills", None) if getattr(ctx, "config", None) else None
    return bool(getattr(skills_cfg, "coding_mode", False))


def _missing_code_task_tools(ctx: TurnContext) -> set[str]:
    """Return launch tools absent from this turn's already-filtered surface."""
    available_tools = {
        str(getattr(tool, "name", ""))
        for tool in (getattr(ctx, "tool_defs", None) or [])
    }
    return set(_CODE_TASK_REQUIRED_TOOLS - available_tools)


async def enforce_coding_mode(ctx: TurnContext) -> TurnContext:
    """Inject and pin code-task when coding mode and its launch tools are available."""
    if not _coding_mode_on(ctx):
        return ctx

    missing_tools = _missing_code_task_tools(ctx)
    if missing_tools:
        # `filter_skills` independently gates the manifest on the same three
        # tools. Do not leave a conflicting, mandatory code-task instruction
        # on restricted callers such as ordinary channel users.
        ctx.metadata["enforce_coding_mode__applied"] = False
        log.info("coding_mode.skipped", missing_tools=sorted(missing_tools))
        return ctx

    # Resolve the code-task invocation off the event loop (cached after first).
    directive = await asyncio.to_thread(_build_coding_mode_directive)

    sp = getattr(ctx, "system_prompt", None)
    if sp is not None:
        # Append to the uncached suffix slot so upstream cache breakpoints stay
        # stable across turns (same shape handling as meta_resolution).
        if isinstance(sp, str):
            base, suffix = sp, ""
        else:
            base, suffix = sp
        new_suffix = f"{suffix}{directive}" if suffix else directive
        ctx.system_prompt = (base, new_suffix)

    # Pin code-task so a relevance filter (when filter_enabled) cannot drop its
    # description from <available_skills>; filter_skills honors this metadata.
    ctx.metadata["pinned_skills"] = list({*ctx.metadata.get("pinned_skills", []), "code-task"})
    ctx.metadata["coding_mode"] = True
    log.info("coding_mode.enforced")
    return ctx
