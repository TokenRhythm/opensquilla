"""Single-task orchestration for code-task (host mode, single-threaded).

Pipeline: resolve task -> clone repo + task branch -> probe env -> render
prompt -> run host agent -> collect change -> verify (red/green/regression)
-> assemble TaskResult. No thread pool, no resume: one call solves one task.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opensquilla.contrib.codetask import config, envprobe, workspace
from opensquilla.contrib.codetask.adapter import LocalAdapter, _kill_process_group
from opensquilla.contrib.codetask.agent_config import AgentConfigError, load_agent_config_bundle
from opensquilla.contrib.codetask.inputs import InputError, TaskSpec, render_task_md, resolve_task
from opensquilla.contrib.codetask.preflight import provider_block_reason, provider_preflight
from opensquilla.contrib.codetask.types import TaskResult, TaskState
from opensquilla.contrib.codetask.verification import verify
from opensquilla.paths import default_opensquilla_home
from opensquilla.profile_operation_lock import ProfileOperationLock

logger = logging.getLogger(__name__)

TRUSTED_HOST_WARNING = (
    "code-task runs an agent on the HOST that may install dependencies and "
    "execute repository code. This is NOT an OS sandbox. Only run it against "
    "repositories you trust."
)


def _default_run_id(slug: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"codetask-{slug}-{stamp}"


_DEFAULT_MAX_ATTEMPTS = 3
_VERIFY_RESERVE_SECONDS = 300  # keep this much of the shared budget for verify
_MIN_USEFUL_AGENT_SECONDS = 120  # do not start a retry with less budget than this
_SCAFFOLD_TIMEOUT_SECONDS = 300
_SCAFFOLD_STATUS_SECONDS = 10
# Appended to any scaffold-stage failure so a stuck/opaque bootstrap (issue
# #470) names its likely cause and the fix. The built-in scaffold shells out to
# a third-party generator that may still emit its own interactive prompt; stdin
# is closed for non-interactive automation, so an unanswerable prompt is bounded
# by the timeout rather than answered.
_SCAFFOLD_STALL_HINT = (
    "Hint: the built-in scaffold runs a third-party generator that may emit its "
    "own interactive prompt (e.g. plugin selection). stdin is closed for "
    "non-interactive automation, so such a prompt cannot be answered and the "
    "step is bounded by a timeout instead. Check the stdout tail above for a "
    "pending prompt; the durable fix is to pin/replace the scaffolder with a "
    "fully non-interactive template."
)
_RETRYABLE_STATES = frozenset({TaskState.FAILED, TaskState.INVALID_ACCEPTANCE_TEST})
_ATTEMPT_SNAPSHOT_FILES = (
    "prompt.txt",
    "change.patch",
    "agent_stdout.log",
    "agent_stderr.log",
    "transcript.jsonl",
    "usage.json",
    "agent-config.toml",
    config.VERIFICATION_MANIFEST_NAME,
)


def _aggregate_usage(acc: dict, new: dict | None) -> dict:
    """Sum numeric usage fields across retry attempts (last value wins for the
    rest, e.g. model name). With a single attempt the result equals ``new``."""
    if not acc:
        return dict(new or {})
    out = dict(acc)
    for k, v in (new or {}).items():
        if isinstance(v, (int, float)) and isinstance(out.get(k), (int, float)):
            out[k] = out[k] + v
        else:
            out[k] = v
    return out


def _snapshot_attempt(run_id: str, attempt: int) -> None:
    """Snapshot this attempt's top-level artifacts into ``attempts/0N/`` so a
    retry does not clobber the prior attempt's record. Top-level keeps the
    LATEST attempt for backward-compatible consumers."""
    src_dir = config.run_dir(run_id)
    dest = src_dir / "attempts" / f"{attempt:02d}"
    dest.mkdir(parents=True, exist_ok=True)
    for name in _ATTEMPT_SNAPSHOT_FILES:
        src = src_dir / name
        if src.is_file():
            try:
                shutil.copy2(src, dest / name)
            except OSError:
                pass


_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|passwd|access[_-]?key|authorization|bearer)"
    r"(\s*[:=]\s*|\s+)(bearer\s+)?\S+"
)


def _redact(text: str) -> str:
    """Best-effort masking of secret-looking strings before runner verification
    output is fed back into a model prompt (trusted host, but defense in depth)."""
    if not text:
        return text
    return _SECRET_RE.sub(
        lambda m: m.group(1) + m.group(2) + (m.group(3) or "") + "<redacted>", text
    )


def _summarize_failure(vout: Any) -> str:
    """Bounded, structured evidence of WHY the runner's verification failed,
    fed into the next attempt's prompt."""
    state = vout.state.value if getattr(vout, "state", None) else "unknown"
    lines = [f"Runner verification result: {state}"]
    if getattr(vout, "detail", None):
        lines.append(f"detail: {vout.detail}")
    for c in getattr(vout, "acceptance", None) or []:
        if c.after != "pass":
            lines.append(
                f"- acceptance test '{c.name}' did NOT pass after your change "
                f"(exit_code={c.green_exit_code}). command: {c.command}"
            )
            if c.green_output_tail:
                lines.append("  output (tail):")
                lines.extend("    " + ln for ln in _redact(c.green_output_tail).splitlines())
    reg = getattr(vout, "regression", None)
    if reg and reg.new_failures:
        lines.append(
            f"- regression: your change introduced {reg.new_failures} NEW failing test(s)."
        )
        if reg.raw_tail:
            lines.extend("    " + ln for ln in _redact(reg.raw_tail).splitlines())
    return "\n".join(lines)


def _failure_signature(vout: Any) -> tuple:
    """A coarse, stable signature of a verification failure: (state, failing test
    names, regression new-failure count). Identical signatures across attempts
    mean the retry made NO progress, so we stop instead of thrashing."""
    failing = tuple(
        sorted(
            (c.name, c.green_exit_code)
            for c in (getattr(vout, "acceptance", None) or [])
            if c.after != "pass"
        )
    )
    reg = getattr(vout, "regression", None)
    new_fail = reg.new_failures if reg else None
    return (getattr(vout, "state", None), failing, new_fail)


def _render_retry_prompt(
    base_prompt: str, attempt: int, max_attempts: int, failure_summary: str
) -> str:
    """Original task prompt + concrete failure evidence. The agent runs on the
    SAME repo where its prior change is already applied; it must CORRECT it, not
    start over, and must NOT weaken the acceptance tests to pass."""
    return (
        base_prompt
        + f"\n\n[RETRY {attempt}/{max_attempts} \u2014 the runner's INDEPENDENT verification "
        "of your previous attempt FAILED]\n"
        "Your previous change is ALREADY applied in the working tree \u2014 do NOT start over; "
        "diagnose and CORRECT it. The runner re-ran your acceptance tests itself and they did "
        "not prove the fix. Do NOT delete, weaken, or trivially rewrite the acceptance tests to "
        "make them pass \u2014 fix the underlying CODE so the SAME tests pass.\n\n"
        + failure_summary
        + "\n\nThen re-emit the verification manifest (verification.json) as before."
    )


def _solve_unlocked(
    *,
    repo: str = "",
    issue: int | None = None,
    task: str | None = None,
    task_file: str | None = None,
    base_ref: str | None = None,
    shallow: bool = False,
    model: str = "",
    thinking: str = "",
    timeout: int = config.DEFAULT_AGENT_TIMEOUT,
    verification_mode: str = "red-green",
    run_id: str | None = None,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
) -> TaskResult:
    """Run one code-task end-to-end and return a structured TaskResult."""
    # 1. Resolve the task text first (cheap), so a bad task fails before cloning.
    #    Issue mode may need the clone to resolve the repo, so clone first when
    #    --issue is used.
    valid_modes = {"red-green", "build", "scratch"}
    if verification_mode not in valid_modes:
        raise InputError(
            f"Unknown verification_mode {verification_mode!r}; expected one of: "
            "red-green, build, scratch."
        )
    if verification_mode == "scratch":
        if repo:
            raise InputError(
                "Do not pass repo with verification_mode='scratch'; it creates an empty repo."
            )
        if issue is not None:
            raise InputError("verification_mode='scratch' supports task or task_file, not issue.")
    elif verification_mode == "build" and not repo:
        # From-scratch app build: no repo needed; we scaffold a workspace repo.
        if issue is not None:
            raise InputError(
                "verification_mode='build' from scratch uses task or task_file, not issue."
            )
    elif not repo:
        raise InputError(
            "Pass repo unless using verification_mode='scratch' or a from-scratch 'build'."
        )

    spec: TaskSpec
    run_id = run_id or _default_run_id("task")
    from_scratch_build = verification_mode == "build" and not repo

    # Assemble the subagent config up front (operator provider sections over
    # the policy template) so a broken operator config fails loud in
    # milliseconds — BEFORE the potentially minutes-long clone — with the
    # actionable reason in result.json/status.json.
    try:
        agent_config = load_agent_config_bundle()
    except AgentConfigError as exc:
        detail = str(exc)
        blocked = TaskResult(
            task_slug="task",
            run_id=run_id,
            state=TaskState.ENVIRONMENT_BLOCKED,
            repo=repo,
            base_ref="",
            branch="",
            source="config",
            artifact_dir=str(config.run_dir(run_id)),
            error=detail,
        )
        blocked.final_failure_reason = detail
        config.run_dir(run_id).mkdir(parents=True, exist_ok=True)
        _persist(blocked)
        return blocked

    # Credential preflight: one throwaway request against the SAME provider the
    # subagent will use, before the minutes-long clone/install, so a missing or
    # rejected key fails loud and actionable now instead of opaquely mid-run.
    config.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    _write_status(run_id, "provider_preflight", repo=repo)
    preflight_ok, preflight_reason = provider_preflight(agent_config, model)
    if not preflight_ok:
        blocked = TaskResult(
            task_slug="task",
            run_id=run_id,
            state=TaskState.ENVIRONMENT_BLOCKED,
            repo=repo,
            base_ref="",
            branch="",
            source="config",
            artifact_dir=str(config.run_dir(run_id)),
            error=preflight_reason,
        )
        blocked.final_failure_reason = preflight_reason
        _write_status(run_id, "provider_preflight_failed", repo=repo, error=preflight_reason)
        _persist(blocked)
        return blocked

    if verification_mode == "scratch":
        # No repo: write self-contained code from scratch in an empty git repo.
        spec = resolve_task(task_text=task, task_file=task_file)
        run_id_final = run_id or _default_run_id(spec.slug)
        prepared = workspace.prepare_scratch_repo(run_id_final, slug=spec.slug)
    elif verification_mode == "build" and not repo:
        # From-scratch app build: scaffold into a DURABLE workspace repo so the
        # verified app persists somewhere a follow-up edit can --repo at.
        spec = resolve_task(task_text=task, task_file=task_file)
        run_id_final = run_id or _default_run_id(spec.slug)
        repo = str(workspace.ensure_build_workspace(spec.slug))
        prepared = workspace.prepare_repo(
            run_id_final, repo, base_ref=base_ref, shallow=shallow, slug=spec.slug
        )
    elif issue is not None:
        prepared = workspace.prepare_repo(
            run_id, repo, base_ref=base_ref, shallow=shallow, slug="task"
        )
        spec = resolve_task(issue_number=issue, repo_dir=prepared.path)
        # Re-slug the run/branch off the resolved issue title for readability.
        run_id_final = run_id
    else:
        spec = resolve_task(task_text=task, task_file=task_file)
        run_id_final = run_id or _default_run_id(spec.slug)
        prepared = workspace.prepare_repo(
            run_id_final, repo, base_ref=base_ref, shallow=shallow, slug=spec.slug
        )

    rid = run_id_final
    artifact_dir = config.run_dir(rid)
    scratch = config.scratch_dir(rid)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    # Work happens in this isolated run dir, NOT in the --repo source (which
    # stays empty until a VERIFIED change is persisted back). The CLI announces
    # the run dir on startup; here we start the status heartbeat so an observer
    # watches progress in the run dir instead of misreading the empty source as
    # "stuck".
    _write_status(rid, "preparing", repo=repo, run_dir=str(artifact_dir))

    # 2. Persist task.md.
    task_md = render_task_md(
        spec, repo=repo, base_ref=prepared.base_ref, commit=prepared.base_commit
    )
    config.artifact_path(rid, "task.md").write_text(task_md, encoding="utf-8")

    result = TaskResult(
        task_slug=spec.slug,
        run_id=rid,
        state=TaskState.FAILED,
        repo=repo,
        base_ref=prepared.base_ref,
        branch=prepared.branch,
        source=spec.source,
        artifact_dir=str(artifact_dir),
    )
    result.verification_kind = {"build": "build", "scratch": "scratch"}.get(
        verification_mode, "red_green"
    )

    # 3. Build-from-scratch gets a runner-owned scaffold before the agent.
    # This removes the observed silent hang where a model-driven `npx create-*`
    # process waits forever with no log/status visibility.
    runner_scaffolded = False
    if from_scratch_build and not _repo_has_app(prepared.path):
        ok, detail = _scaffold_build_app(rid, prepared.path, artifact_dir)
        if not ok:
            result.state = TaskState.ENVIRONMENT_BLOCKED
            result.error = detail
            result.final_failure_reason = detail
            _persist(result)
            return result
        runner_scaffolded = True

    # 4. Probe environment + render prompt.
    probe = envprobe.probe(prepared.path)
    is_edit = (
        verification_mode == "build"
        and _repo_has_app(prepared.path)
        and not runner_scaffolded
    )
    base_prompt = _render_prompt(task_md, probe.as_hints(), scratch, verification_mode, is_edit)

    # 5. Verify->fix loop. Re-run the agent on the SAME prepared repo (clone +
    #    built venv + the prior attempt's changes all preserved) when the
    #    runner's authoritative verification fails, feeding the concrete failure
    #    back so the retry CORRECTS the prior attempt instead of re-cloning and
    #    re-exploring from scratch. prepare_repo + env probe (above) run ONCE.
    #    The total time budget is SHARED across attempts (never multiplied).
    #    Build mode is single-attempt in v1.
    max_attempts = 1 if verification_mode == "build" else max(1, max_attempts)
    result.max_attempts = max_attempts
    deadline = time.monotonic() + max(1, timeout)
    patch = ""
    failure_summary = ""
    prev_failure_sig: tuple | None = None
    prev_files = 0
    prev_patch_bytes = 0
    diff_exploded = False
    attempt = 0
    while attempt < max_attempts:
        # Budget + attempt-count decided BEFORE running, so a retry skipped for
        # lack of shared budget is NOT counted. Attempt 1 always runs and honors
        # the full ``timeout``; a retry gets the remaining shared budget minus a
        # verify reserve and is skipped (not counted) when too little remains.
        if attempt >= 1:  # this iteration would be a RETRY (attempt incremented below)
            usable = (deadline - time.monotonic()) - _VERIFY_RESERVE_SECONDS
            if usable < _MIN_USEFUL_AGENT_SECONDS:
                result.final_failure_reason = (
                    "stopped retrying: shared time budget exhausted"
                )
                result.retry_exhausted = True
                break
            agent_budget = int(min(timeout, usable))
        else:
            # First attempt: adaptively reserve verify time so the whole run
            # stays within the shared deadline; never crush a small timeout.
            _reserve = min(_VERIFY_RESERVE_SECONDS, timeout // 4)
            agent_budget = int(max(1, timeout - _reserve))
        attempt += 1
        result.attempts = attempt

        # Clear stale top-level artifacts so THIS attempt owns them: a retry that
        # reverts to an empty diff / emits no manifest must not leave the prior
        # attempt's patch or manifest behind to be snapshotted.
        for _stale in ("change.patch", config.VERIFICATION_MANIFEST_NAME):
            _sp = config.artifact_path(rid, _stale)
            if _sp.exists():
                try:
                    _sp.unlink()
                except OSError:
                    pass
        result.patch_path = None
        _scratch_manifest = scratch / config.VERIFICATION_MANIFEST_NAME
        if _scratch_manifest.exists():
            try:
                _scratch_manifest.unlink()
            except OSError:
                pass

        prompt = (
            base_prompt
            if attempt == 1
            else _render_retry_prompt(base_prompt, attempt, max_attempts, failure_summary)
        )
        config.artifact_path(rid, "prompt.txt").write_text(prompt, encoding="utf-8")

        _write_status(
            rid, "agent_running", repo=repo, run_dir=str(artifact_dir),
            attempt=attempt, max_attempts=max_attempts,
        )
        adapter = LocalAdapter(
            model=model, thinking=thinking, timeout=agent_budget, agent_config=agent_config
        )
        agent_status_base = {
            "repo": repo,
            "run_dir": str(artifact_dir),
            "attempt": attempt,
            "max_attempts": max_attempts,
        }

        def _agent_status(update: dict[str, Any]) -> None:
            _write_status(rid, "agent_running", **agent_status_base, **update)

        try:
            try:
                outcome = adapter.run(
                    prompt,
                    repo=prepared.path,
                    scratch_dir=scratch,
                    artifact_dir=artifact_dir,
                    status_callback=_agent_status,
                )
            except TypeError as exc:
                if "status_callback" not in str(exc):
                    raise
                outcome = adapter.run(
                    prompt,
                    repo=prepared.path,
                    scratch_dir=scratch,
                    artifact_dir=artifact_dir,
                )
        except RuntimeError as exc:
            result.error = str(exc)
            break
        result.usage = _aggregate_usage(result.usage, outcome.usage)
        result.duration_seconds = (result.duration_seconds or 0.0) + (
            outcome.duration_seconds or 0.0
        )
        outcome_finish_reason = getattr(outcome, "finish_reason", "")
        outcome_error = getattr(outcome, "error", None)

        _write_status(rid, "collecting_change", run_dir=str(artifact_dir))
        files_changed, diffstat, patch = workspace.collect_change(
            prepared.path, prepared.base_commit
        )
        result.files_changed = files_changed
        result.diffstat = diffstat
        result.commits = workspace.count_commits(prepared.path, prepared.base_commit)
        # Guardrail: a retry whose cumulative diff balloons is piling on changes,
        # not converging -- stop retrying after this attempt.
        patch_bytes = len(patch.encode("utf-8"))
        if attempt > 1 and (
            files_changed > max(10, 2 * prev_files)
            or patch_bytes > max(200_000, 2 * prev_patch_bytes)
        ):
            diff_exploded = True
        prev_files = files_changed
        prev_patch_bytes = patch_bytes
        if patch.strip():
            patch_path = config.artifact_path(rid, "change.patch")
            patch_path.write_text(patch, encoding="utf-8")
            result.patch_path = str(patch_path)
        _archive_manifest(scratch, rid)
        _snapshot_attempt(rid, attempt)

        if outcome.timeout:
            result.state = TaskState.ENVIRONMENT_BLOCKED
            result.error = "agent timed out before finishing"
            _write_status(
                rid,
                "stalled" if outcome_finish_reason == "stalled" else "agent_timeout",
                run_dir=str(artifact_dir),
                attempt=attempt,
                max_attempts=max_attempts,
                error=result.error,
            )
            break
        if outcome_finish_reason == "stalled":
            result.state = TaskState.ENVIRONMENT_BLOCKED
            result.error = outcome_error or "agent stalled with no output"
            _write_status(
                rid,
                "stalled",
                run_dir=str(artifact_dir),
                attempt=attempt,
                max_attempts=max_attempts,
                error=result.error,
            )
            break
        # A credential/config-class provider error is not retryable: stop with
        # the real reason instead of falling through to verification (which
        # would report a misleading "no valid manifest" and burn more attempts).
        credential_block = provider_block_reason(getattr(outcome, "errors", []))
        if outcome_finish_reason == "error" and credential_block is not None:
            result.state = TaskState.ENVIRONMENT_BLOCKED
            result.error = credential_block
            result.final_failure_reason = credential_block
            _write_status(
                rid,
                "provider_error",
                run_dir=str(artifact_dir),
                attempt=attempt,
                max_attempts=max_attempts,
                error=credential_block,
            )
            break

        _write_status(
            rid, "verifying", mode=verification_mode, run_dir=str(artifact_dir),
            attempt=attempt, max_attempts=max_attempts,
        )
        if verification_mode == "build":
            from opensquilla.contrib.codetask.build_verify import verify_build

            bout = verify_build(prepared.path)
            result.state = bout.state
            result.build = bout.build
            result.verified = bout.state == TaskState.VERIFIED
            if bout.detail and not result.error:
                result.error = bout.detail
            break  # build mode: single attempt in v1

        if verification_mode == "scratch":
            from opensquilla.contrib.codetask.verification import verify_scratch

            vout = verify_scratch(
                repo=prepared.path, scratch_dir=scratch, deadline=deadline
            )
        else:
            # Bound the WHOLE verification by the shared deadline (per-command,
            # re-checked between commands) so multiple commands cannot overrun it.
            vout = verify(
                repo=prepared.path,
                base_commit=prepared.base_commit,
                scratch_dir=scratch,
                deadline=deadline,
            )
        result.state = vout.state
        result.acceptance = vout.acceptance
        result.regression = vout.regression
        result.assumptions = vout.assumptions
        result.verified = vout.state == TaskState.VERIFIED
        result.error = vout.detail or None

        if vout.state not in _RETRYABLE_STATES:
            break
        # Record this attempt's failure evidence FIRST, so an attempt stopped by a
        # guardrail below still has verify_failure.txt, then decide whether to retry.
        failure_summary = _summarize_failure(vout)
        try:
            (config.run_dir(rid) / "attempts" / f"{attempt:02d}" / "verify_failure.txt").write_text(
                failure_summary, encoding="utf-8"
            )
        except OSError:
            pass
        # Guardrail: no progress -- the retry produced the SAME failure signature.
        sig = _failure_signature(vout)
        if sig == prev_failure_sig:
            result.final_failure_reason = (
                "stopped retrying: no progress (identical failure across attempts)"
            )
            break
        # Guardrail: the change grew past the diff cap on this retry.
        if diff_exploded:
            result.final_failure_reason = (
                "stopped retrying: change grew beyond the retry diff cap"
            )
            break
        prev_failure_sig = sig

    # Finalize retry metadata. relaunch is never recommended once internal
    # retries are exhausted (the gateway must surface the result, not re-launch).
    _terminal_ok = result.state in (
        TaskState.VERIFIED,
        TaskState.ALREADY_SATISFIED,
        TaskState.NOT_TESTABLE,
    )
    result.retry_exhausted = result.retry_exhausted or (
        attempt >= max_attempts and not _terminal_ok and result.state in _RETRYABLE_STATES
    )
    result.relaunch_recommended = False
    if not _terminal_ok and not result.final_failure_reason:
        result.final_failure_reason = result.error or (
            result.state.value if result.state else None
        )

    # Build mode: promote a VERIFIED change back onto the stable LOCAL source
    # repo so follow-up edits iterate on the same app (URLs are not touched).
    if verification_mode == "build" and result.state == TaskState.VERIFIED and patch.strip():
        _is_local = ("://" not in repo) and not repo.startswith("git@")
        _src = Path(repo).expanduser()
        if _is_local and _src.exists() and (_src.resolve() / ".git").is_dir():
            ok, info = workspace.persist_to_source(
                _src.resolve(), prepared.base_commit, patch, f"code-task: {spec.slug}"
            )
            result.persisted = ok
            if ok:
                result.source_repo = str(_src.resolve())
            elif not result.error:
                result.error = f"verified but not persisted to source: {info}"

    _persist(result)
    return result


def solve(
    *,
    repo: str = "",
    issue: int | None = None,
    task: str | None = None,
    task_file: str | None = None,
    base_ref: str | None = None,
    shallow: bool = False,
    model: str = "",
    thinking: str = "",
    timeout: int = config.DEFAULT_AGENT_TIMEOUT,
    verification_mode: str = "red-green",
    run_id: str | None = None,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
) -> TaskResult:
    """Run one code-task while excluding concurrent profile lifecycle mutations."""

    with ProfileOperationLock(default_opensquilla_home()):
        try:
            return _solve_unlocked(
                repo=repo,
                issue=issue,
                task=task,
                task_file=task_file,
                base_ref=base_ref,
                shallow=shallow,
                model=model,
                thinking=thinking,
                timeout=timeout,
                verification_mode=verification_mode,
                run_id=run_id,
                max_attempts=max_attempts,
            )
        except (InputError, workspace.WorkspaceError):
            raise
        except Exception as exc:
            if run_id:
                _write_status(run_id, "crashed", error=str(exc)[:500])
            raise


def _archive_manifest(scratch: Path, run_id: str) -> None:
    """Copy the agent's verification.json from scratch into the run dir."""
    src = scratch / config.VERIFICATION_MANIFEST_NAME
    if not src.is_file():
        return
    try:
        config.artifact_path(run_id, config.VERIFICATION_MANIFEST_NAME).write_text(
            src.read_text(encoding="utf-8"), encoding="utf-8"
        )
    except OSError:
        pass


def _repo_has_app(repo: Path) -> bool:
    """True if the cloned repo already contains a JS app (edit, not scaffold)."""
    return (repo / "package.json").is_file() and (repo / "src").is_dir()


def _scaffold_build_app(run_id: str, repo: Path, artifact_dir: Path) -> tuple[bool, str]:
    """Create the fixed Electron/Vite scaffold under runner control.

    The sub-agent still owns product implementation, but the fragile
    interactive template bootstrap is bounded, logged, and reflected in
    status.json by the runner.
    """
    from opensquilla.contrib.codetask.build_verify import _build_env, _resolve_cli

    timeout = _scaffold_timeout()
    scaffold_root = artifact_dir / "scaffold"
    generated = scaffold_root / "app"
    stdout_path = artifact_dir / "scaffold_stdout.log"
    stderr_path = artifact_dir / "scaffold_stderr.log"
    npm_stdout_path = artifact_dir / "scaffold_npm_stdout.log"
    npm_stderr_path = artifact_dir / "scaffold_npm_stderr.log"
    if scaffold_root.exists():
        shutil.rmtree(scaffold_root, ignore_errors=True)
    scaffold_root.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")

    env = _build_env()
    env.update(
        {
            "CI": "1",
            "YES": "1",
            "npm_config_yes": "true",
            "npm_config_fund": "false",
            "npm_config_audit": "false",
        }
    )
    # @quick-start/create-electron uses --skip, not npm's --yes, to disable
    # its own updater/mirror prompts.
    cmd = [
        _resolve_cli("npm"),
        "create",
        "@quick-start/electron@latest",
        "app",
        "--",
        "--template",
        "react-ts",
        "--skip",
    ]
    ok, detail = _run_logged_step(
        run_id,
        "scaffold_running",
        cmd,
        cwd=scaffold_root,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        timeout=timeout,
        env=env,
    )
    if not ok:
        detail = f"{detail}\n\n{_SCAFFOLD_STALL_HINT}"
        _write_status(
            run_id,
            "scaffold_failed",
            run_dir=str(artifact_dir),
            error=detail,
            log_paths={"stdout": str(stdout_path), "stderr": str(stderr_path)},
        )
        return False, detail
    if not (generated / "package.json").is_file():
        detail = (
            "scaffold command completed but did not create app/package.json\n\n"
            f"{_SCAFFOLD_STALL_HINT}"
        )
        _write_status(
            run_id,
            "scaffold_failed",
            run_dir=str(artifact_dir),
            error=detail,
            log_paths={"stdout": str(stdout_path), "stderr": str(stderr_path)},
        )
        return False, detail

    _write_status(run_id, "scaffold_copying", run_dir=str(artifact_dir))
    _copy_scaffold_contents(generated, repo)
    if not (repo / "package-lock.json").is_file():
        npm = _resolve_cli("npm")
        npm_stdout_path.write_text("", encoding="utf-8")
        npm_stderr_path.write_text("", encoding="utf-8")
        ok, detail = _run_logged_step(
            run_id,
            "scaffold_lockfile",
            [npm, "install", "--package-lock-only"],
            cwd=repo,
            stdout_path=npm_stdout_path,
            stderr_path=npm_stderr_path,
            timeout=timeout,
            env=env,
        )
        if not ok:
            _write_status(
                run_id,
                "scaffold_failed",
                run_dir=str(artifact_dir),
                error=detail,
                log_paths={
                    "stdout": str(npm_stdout_path),
                    "stderr": str(npm_stderr_path),
                },
            )
            return False, detail

    _write_status(
        run_id,
        "scaffold_complete",
        run_dir=str(artifact_dir),
        log_paths={
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            "npm_stdout": str(npm_stdout_path),
            "npm_stderr": str(npm_stderr_path),
        },
    )
    return True, "scaffold complete"


def _scaffold_timeout() -> int:
    raw = os.environ.get("OPENSQUILLA_CODETASK_SCAFFOLD_TIMEOUT")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return _SCAFFOLD_TIMEOUT_SECONDS


def _run_logged_step(
    run_id: str,
    phase: str,
    cmd: list[str],
    *,
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout: int,
    env: dict[str, str],
) -> tuple[bool, str]:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    command = _command_label(cmd)
    try:
        with stdout_path.open("a", encoding="utf-8") as out, stderr_path.open(
            "a", encoding="utf-8"
        ) as err:
            popen_kwargs: dict[str, Any] = {
                "cwd": str(cwd),
                "stdin": subprocess.DEVNULL,
                "stdout": out,
                "stderr": err,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "env": env,
            }
            if os.name == "posix":
                popen_kwargs["start_new_session"] = True
            else:
                popen_kwargs["creationflags"] = getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                )
            proc = subprocess.Popen(cmd, **popen_kwargs)
            _write_status(
                run_id,
                phase,
                pid=proc.pid,
                cwd=str(cwd),
                current_command=command,
                timeout_seconds=timeout,
                log_paths={"stdout": str(stdout_path), "stderr": str(stderr_path)},
            )
            deadline = time.monotonic() + timeout
            next_status = time.monotonic() + _SCAFFOLD_STATUS_SECONDS
            while proc.poll() is None:
                now = time.monotonic()
                if now >= deadline:
                    _kill_process_group(proc)
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        _kill_process_group(proc)
                    detail = (
                        f"{phase} timed out after {timeout}s while running: {command}\n"
                        f"stdout tail:\n{_tail_file(stdout_path)}\n"
                        f"stderr tail:\n{_tail_file(stderr_path)}"
                    )
                    return False, detail
                if now >= next_status:
                    _write_status(
                        run_id,
                        phase,
                        pid=proc.pid,
                        cwd=str(cwd),
                        current_command=command,
                        elapsed_seconds=round(now - (deadline - timeout), 1),
                        log_paths={
                            "stdout": str(stdout_path),
                            "stderr": str(stderr_path),
                        },
                    )
                    next_status = now + _SCAFFOLD_STATUS_SECONDS
                time.sleep(0.5)
            if proc.returncode != 0:
                detail = (
                    f"{phase} failed with exit {proc.returncode}: {command}\n"
                    f"stdout tail:\n{_tail_file(stdout_path)}\n"
                    f"stderr tail:\n{_tail_file(stderr_path)}"
                )
                return False, detail
    except FileNotFoundError as exc:
        return False, f"{phase} command not found: {exc}"
    except OSError as exc:
        return False, f"{phase} could not start: {exc}"
    return True, f"{phase} complete"


def _copy_scaffold_contents(src: Path, dest: Path) -> None:
    skip = {"node_modules", "dist", "out", "build", ".git"}
    for child in src.iterdir():
        if child.name in skip:
            continue
        target = dest / child.name
        if target.exists():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if child.is_dir():
            shutil.copytree(child, target, symlinks=True)
        else:
            shutil.copy2(child, target)


def _tail_file(path: Path, *, lines: int = 25) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def _command_label(cmd: list[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in cmd)


def _render_prompt(
    task_md: str,
    hints: str,
    scratch: Path,
    verification_mode: str = "red-green",
    is_edit: bool = False,
) -> str:
    template = config.prompt_template_path(verification_mode, is_edit).read_text(encoding="utf-8")
    hints_block = f"\n{hints}\n" if hints else ""
    return template.format(
        task=task_md,
        env_hints=hints_block,
        scratch_dir=str(scratch),
        manifest_name=config.VERIFICATION_MANIFEST_NAME,
    )


def _result_to_dict(result: TaskResult) -> dict[str, Any]:
    data = asdict(result)
    data["state"] = result.state.value
    return data


def _write_status(run_id: str, phase: str, **extra: Any) -> None:
    """Best-effort in-progress heartbeat at ``<run_dir>/status.json``.

    code-task works in an isolated run dir, and the ``--repo`` source stays
    empty until a VERIFIED change is persisted back — so an observer watching
    the source repo cannot tell a healthy run from a stuck one. This heartbeat
    gives the launching agent a correct place to see live progress without
    touching the source. Never raises (status is advisory, not load-bearing).
    """
    try:
        d = config.run_dir(run_id)
        d.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "run_id": run_id,
            "phase": phase,
            "updated": datetime.now(UTC).isoformat(),
            **extra,
        }
        # Atomic write: a concurrent reader sees either the old or the new
        # status.json, never a half-written one (tmp is in the same dir, so the
        # replace is a same-filesystem rename).
        tmp = d / "status.json.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(d / "status.json")
    except OSError:
        pass


def _persist(result: TaskResult) -> None:
    config.artifact_path(result.run_id, "result.json").write_text(
        json.dumps(_result_to_dict(result), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    # Terminal heartbeat so a watcher sees the run is done (and its outcome)
    # without parsing result.json.
    _write_status(
        result.run_id,
        "completed",
        state=result.state.value,
        verified=result.verified,
        error=result.error,
        final_failure_reason=result.final_failure_reason,
        installer_path=(result.build.installer_path if result.build else None),
    )
