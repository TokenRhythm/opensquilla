"""``opensquilla code-task`` — solve a real-repo coding task with an agent.

Host mode only in v1: the repo is cloned to a disposable working directory
and the agent runs as a host subprocess (no Docker). Treat the target repo
as TRUSTED — this is not an OS isolation boundary.

This module is lazy: heavy imports happen inside the command body so
``--help`` stays cheap and importing opensquilla does not pull it in.
"""

from __future__ import annotations

import importlib
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

import typer


def _stdin_is_tty() -> bool:
    """True iff stdin is a real interactive terminal.

    Defensive against unit-test stand-ins (``StringIO``, ``BytesIO``, etc.)
    that lack an ``isatty`` attribute entirely — those should count as
    non-tty, not crash the gate. Used by the trusted-host confirm to
    fail-fast on non-interactive surfaces (background_process, cron,
    CI, daemons) where ``typer.confirm`` would otherwise block on
    ``input()`` until the parent timeout fires.
    """
    return bool(getattr(sys.stdin, "isatty", lambda: False)())

codetask_app = typer.Typer(
    help="Solve a real-repository coding task (GitHub issue or feature request).",
    no_args_is_help=True,
)


@codetask_app.callback()
def _codetask_main() -> None:
    """Solve real-repository coding tasks with an OpenSquilla agent (host mode)."""
    # Present so the single ``solve`` command is not collapsed away by Typer
    # (keeps ``opensquilla code-task solve ...`` as an explicit subcommand).


def _shell_quote_path(path: str) -> str:
    if os.name == "nt":
        return '"' + path.replace('"', '\\"') + '"'
    return shlex.quote(path)


@codetask_app.command("stage-task-file")
def stage_task_file() -> None:
    """Read task text from stdin, write a private temp file, print a quoted path."""
    stream = getattr(sys.stdin, "buffer", None)
    payload = stream.read() if stream is not None else sys.stdin.read().encode("utf-8")
    fd, raw_path = tempfile.mkstemp(prefix="codetask-task-", suffix=".txt")
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    typer.echo(_shell_quote_path(str(path)))


_DEFAULT_SMOKE_IMPORTS = (
    "joblib",
    "sklearn",
    "lightgbm",
    "tokenizers",
    "tiktoken",
    "onnxruntime",
    "mcp",
)


def _smoke_import_modules(modules: list[str] | tuple[str, ...]) -> dict[str, object]:
    ok: list[str] = []
    missing: dict[str, str] = {}
    for module in modules:
        try:
            importlib.import_module(module)
            ok.append(module)
        except Exception as exc:
            missing[module] = f"{type(exc).__name__}: {exc}"
    return {"ok": ok, "missing": missing, "success": not missing}


def _smoke_router_runtime() -> dict[str, object]:
    import asyncio

    router_module = importlib.import_module("opensquilla.squilla_router.v4_phase3")
    strategy_cls = getattr(router_module, "V4Phase3Strategy")

    async def _run() -> dict[str, object]:
        strategy = strategy_cls(require_router_runtime=True)
        tier, confidence, source, metadata = await strategy.classify(
            "Summarize this short note.",
            valid_tiers=["c0", "c1", "c2", "c3"],
        )
        success = source == "v4_phase3" and bool(strategy._available)
        return {
            "success": success,
            "available": bool(strategy._available),
            "tier": tier,
            "confidence": confidence,
            "source": source,
            "route_class": metadata.get("route_class"),
            "model_version": metadata.get("model_version"),
        }

    return asyncio.run(_run())


@codetask_app.command("smoke-imports")
def smoke_imports(
    module: list[str] | None = typer.Option(
        None,
        "--module",
        help="Module to import; defaults to desktop packaged runtime capability smoke set.",
    ),
) -> None:
    """Import optional desktop capability modules and exit non-zero on gaps."""
    modules = module or list(_DEFAULT_SMOKE_IMPORTS)
    result = _smoke_import_modules(modules)
    typer.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if not result["success"]:
        raise typer.Exit(1)


@codetask_app.command("smoke-router")
def smoke_router() -> None:
    """Initialize the bundled V4 router and run one deterministic classification."""
    try:
        result = _smoke_router_runtime()
    except Exception as exc:
        result = {
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    typer.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if not result.get("success"):
        raise typer.Exit(1)


@codetask_app.command("solve")
def solve(
    repo: str = typer.Option(
        "",
        help="Repo to work on (git URL or local path); omit for --verification-mode scratch.",
    ),
    issue: int = typer.Option(None, "--issue", help="GitHub issue number (needs `gh`)."),
    task: str = typer.Option(None, "--task", help="Free-form task / feature request text."),
    task_file: str = typer.Option(
        None, "--task-file", help="Path to a file holding the task description."
    ),
    base: str = typer.Option(None, "--base", help="Base ref to start from (default: HEAD)."),
    shallow: bool = typer.Option(False, "--shallow", help="Shallow clone (no history)."),
    model: str = typer.Option("", help="Model override; empty lets the router/config decide."),
    thinking: str = typer.Option("", help="Thinking effort; empty lets config decide."),
    timeout: int = typer.Option(
        5400,
        help="Agent timeout in seconds (default 90 min; heavy repos need time to install deps).",
    ),
    verification_mode: str = typer.Option(
        "red-green",
        "--verification-mode",
        help=(
            "How to verify: red-green (default) / build (app checklist) / "
            "scratch (from-scratch code, green-only, no --repo)."
        ),
    ),
    run_id: str = typer.Option("", help="Run identifier (auto-generated when empty)."),
    json_output: bool = typer.Option(False, "--json", help="Print the result as JSON."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the trusted-host confirmation prompt."
    ),
) -> None:
    """Clone the repo, run an agent to solve the task, and verify the result."""
    from opensquilla.contrib.codetask.inputs import InputError
    from opensquilla.contrib.codetask.report import render
    from opensquilla.contrib.codetask.runner import TRUSTED_HOST_WARNING
    from opensquilla.contrib.codetask.runner import solve as run_solve
    from opensquilla.contrib.codetask.types import PRODUCTIVE_STATES, TaskState
    from opensquilla.contrib.codetask.workspace import WorkspaceError

    given = [x for x in (issue, task, task_file) if x not in (None, "")]
    if len(given) != 1:
        typer.secho(
            "Pass exactly one of --issue, --task, or --task-file.",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)

    valid_modes = {"red-green", "build", "scratch"}
    if verification_mode not in valid_modes:
        typer.secho(
            f"Unknown --verification-mode {verification_mode!r}; expected one of: "
            "red-green, build, scratch.",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)
    if verification_mode == "scratch":
        if repo:
            typer.secho(
                "Do not pass --repo with --verification-mode scratch; it creates an empty repo.",
                err=True,
                fg=typer.colors.RED,
            )
            raise typer.Exit(2)
        if issue is not None:
            typer.secho(
                "--verification-mode scratch supports --task or --task-file, not --issue.",
                err=True,
                fg=typer.colors.RED,
            )
            raise typer.Exit(2)
    elif verification_mode == "build" and not repo:
        if issue is not None:
            typer.secho(
                "--verification-mode build from scratch uses --task/--task-file, not --issue.",
                err=True,
                fg=typer.colors.RED,
            )
            raise typer.Exit(2)
    elif not repo:
        typer.secho(
            "Pass --repo unless using --verification-mode scratch or a from-scratch build.",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)

    # Trusted-host gate. A non-interactive surface — --json, or any caller
    # whose stdin is not a TTY (background_process, cron, CI, ssh non-tty,
    # daemon) — must carry an explicit --yes. Otherwise typer.confirm()
    # would block on input() until the parent timeout fires (observed: a
    # background_process launch with --task containing embedded newlines
    # eats --yes via cmd.exe quoting, then waits the full 5400s timeout
    # at "[y/N]"). Fail-fast with a clear exit 2 instead.
    if not yes:
        if json_output or not _stdin_is_tty():
            reason = (
                "(stdout is JSON)" if json_output else "(stdin is not a terminal)"
            )
            typer.secho(
                f"Refusing to run without --yes on a non-interactive surface "
                f"{reason}. code-task runs an agent on the host (not a sandbox); "
                "pass --yes to confirm the repo is trusted.",
                err=True,
                fg=typer.colors.RED,
            )
            raise typer.Exit(2)
        typer.secho(TRUSTED_HOST_WARNING, fg=typer.colors.YELLOW)
        target = "a scratch repo" if verification_mode == "scratch" else repo
        if not typer.confirm(f"Run code-task against {target}?", default=False):
            raise typer.Exit(1)

    # Resolve the run id here so we can announce the run directory on STDOUT's
    # sibling stream (stderr) before the long run, and stamp a terminal status
    # if the run crashes unexpectedly. The --json result stays the only thing on
    # stdout, so a consumer reading stdout alone still gets clean JSON.
    from opensquilla.contrib.codetask import config as ct_config
    from opensquilla.contrib.codetask.runner import _default_run_id
    from opensquilla.recovery.errors import ProfileLockBusyError

    rid = run_id or _default_run_id("task")
    run_dir = ct_config.run_dir(rid)
    typer.echo(
        f"[code-task] run started: run_id={rid} artifact_dir={run_dir} "
        f"status={run_dir / 'status.json'} "
        "(work happens in the run dir, NOT in the --repo source)",
        err=True,
    )

    try:
        result = run_solve(
            repo=repo,
            issue=issue,
            task=task or None,
            task_file=task_file or None,
            base_ref=base or None,
            shallow=shallow,
            model=model,
            thinking=thinking,
            timeout=timeout,
            verification_mode=verification_mode,
            run_id=rid,
        )
    except InputError as exc:
        typer.secho(str(exc), err=True, fg=typer.colors.RED)
        raise typer.Exit(2) from exc
    except WorkspaceError as exc:
        typer.secho(f"workspace error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    except ProfileLockBusyError as exc:
        typer.secho(f"profile is busy: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    if json_output:
        from opensquilla.contrib.codetask.runner import _result_to_dict

        typer.echo(json.dumps(_result_to_dict(result), ensure_ascii=False))
    else:
        typer.echo(render(result))

    # Exit non-zero on clearly unproductive outcomes so scripts can branch.
    if result.state in (
        TaskState.FAILED,
        TaskState.ENVIRONMENT_BLOCKED,
        TaskState.INVALID_ACCEPTANCE_TEST,
    ):
        raise typer.Exit(1)
    _ = PRODUCTIVE_STATES  # documented states for consumers
