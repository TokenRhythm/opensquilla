"""Compose OpenSquilla's native tool registry for a standalone MetaSkill run.

The schemas, policy dispatcher, and handlers all come from OpenSquilla. This
module only selects tools that do not require a running Gateway or desktop.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from importlib import import_module
import os
from pathlib import Path
import re
import signal
import subprocess
from typing import Any
from uuid import uuid4

TOOL_MODULES = (
    "artifacts", "code_exec", "file_authoring", "filesystem", "git",
    "media", "patch", "shell", "tool_results", "web", "web_fetch",
)


class _BackgroundCleanupHook:
    """Remember only native sessions actually started by this tool handler."""

    name = "standalone_background_cleanup"

    def __init__(self):
        self.sessions: dict[str, Any] = {}

    def before_tool(self, _call):
        pass

    def after_tool(self, call, outcome):
        if call.tool_call.tool_name != "background_process":
            return
        # Observe the native raw outcome before tool-result projection. No
        # global process scan or inference from model-provided PIDs is used.
        content = outcome.result
        if not isinstance(content, str):
            return
        match = re.match(r"session_id=(\S+)", content)
        if match is None:
            return
        from opensquilla.tools.builtin.shell import get_bg_session

        session = get_bg_session(match.group(1))
        if session is not None:
            self.sessions[session.session_id] = session


def build_tools(workspace: Path, skills_dir: Path, *, config=None, catalog=None, session_key=None):
    """Return native tool definitions and the native policy-aware dispatcher.

    Safe mode is the default. Full mode must be selected explicitly in
    ``[tools] run_mode = "full"``. Neither mode silently approves requests.
    """
    from opensquilla.application.approval_queue import ApprovalQueue
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.integration import configure_runtime
    from opensquilla.tools.dispatch import build_tool_handler
    from opensquilla.tools.registry import ToolRegistry, get_default_registry
    from opensquilla.tools.types import CallerKind, InteractionMode, ToolContext

    config = config or {}
    options = config.get("tools", {})
    mode = options.get("run_mode", "safe")
    if mode not in {"safe", "full"}:
        raise ValueError("tools.run_mode must be 'safe' or 'full'")
    workspace = workspace.resolve()
    state = workspace / ".metaskill-state"
    state.mkdir(parents=True, exist_ok=True)
    # Native artifact and tool-result handlers require the entry point to seed
    # a session identity. CLI runs pass their invocation key; direct callers
    # receive a fresh local scope rather than sharing a user profile session.
    session_key = session_key or f"metaskill-cli:{uuid4().hex}"
    session_id = session_key.rsplit(":", 1)[-1] or uuid4().hex
    if catalog is None:
        from opensquilla.skills.loader import SkillLoader

        loader = SkillLoader(
            workspace_dir=skills_dir,
            snapshot_path=state / "skills_snapshot.json",
            lockfile_path=state / "skills-lock.json",
        )
        loader.load_all()
        catalog = loader.snapshot()
    from opensquilla.tools.builtin.skill_tools import create_skill_tools

    create_skill_tools(catalog)
    settings = SandboxSettings(
        sandbox=(mode == "safe"), run_mode=mode, security_grading=True,
        backend=options.get("sandbox_backend", "auto"), auto_setup=False,
        approvals_reviewer="user",
    )
    selected_modules = {
        import_module(f"opensquilla.tools.builtin.{name}").__name__
        for name in TOOL_MODULES
    }
    registry = ToolRegistry()
    for registered in get_default_registry().all_tools():
        if registered.handler.__module__ in selected_modules or registered.spec.name in {"skill_view", "skill_list"}:
            registry.register(registered.spec, registered.handler)
    allow = options.get("allow")
    for key in ("allow", "deny"):
        unknown = set(options.get(key) or []) - set(registry.list_names())
        if unknown:
            raise ValueError(f"Unknown standalone tools: {', '.join(sorted(unknown))}")
    context = ToolContext(
        is_owner=True, caller_kind=CallerKind.CLI,
        interaction_mode=InteractionMode.UNATTENDED,
        workspace_dir=str(workspace), run_mode=mode,
        scratch_dir=str(state / "scratch"),
        session_key=session_key,
        artifact_media_root=str(state / "artifacts"),
        artifact_session_id=session_id,
        tool_result_store_dir=str(state / "tool-results"),
        tool_result_store_session_id=session_id,
        allowed_tools=set(allow) if allow is not None else None,
        denied_tools=set(options.get("deny", [])),
        skill_catalog=catalog,
    )
    search = config.get("search", {})
    from opensquilla.tools.builtin.web import configure_search

    configure_search(
        search.get("provider", "duckduckgo"),
        max_results=search.get("max_results", 5),
        api_key=os.environ.get(search.get("api_key_env", ""), ""),
        proxy=search.get("proxy", ""),
        use_env_proxy=False, fallback_policy="off",
    )
    definitions = registry.to_tool_definitions(context)
    background_cleanup = _BackgroundCleanupHook()
    handler = build_tool_handler(
        registry, context,
        known_skill_names={skill.name for skill in getattr(catalog, "skills", ())},
        tool_hooks=(background_cleanup,),
    )
    # The unattended CLI offers only safe/full, without an interactive approval
    # surface. Native policies govern both modes; timeout=0 means no deadline,
    # not immediate denial. Keep this runtime queue local and in memory.
    queue = ApprovalQueue(default_timeout=0, db_path=":memory:")
    try:
        configure_runtime(settings, approval_queue=queue, workspace=workspace,
                          default_run_mode=mode)
    except Exception:
        queue.close()
        raise
    handler._metaskill_context = context
    handler._metaskill_registry = registry
    handler._metaskill_approval_queue = queue
    handler._metaskill_background_cleanup = background_cleanup
    return definitions, handler


def _close_approval_queue(handler: Any) -> None:
    queue = getattr(handler, "_metaskill_approval_queue", None)
    if queue is not None:
        queue.close()
        handler._metaskill_approval_queue = None


async def aclose_tools(handler: Any) -> None:
    """Finish this handler's background trees and native cleanup callbacks."""
    from opensquilla.tools.builtin import shell
    from opensquilla.tools.types import current_tool_context

    cleanup = getattr(handler, "_metaskill_background_cleanup", None)
    token = current_tool_context.set(getattr(handler, "_metaskill_context", None))
    errors = []
    async def close_session(session_id, session):
        # Native IDs are short; object identity prevents acting on an ID
        # that was removed or replaced by a different handler later.
        if shell.get_bg_session(session_id) is not session:
            cleanup.sessions.pop(session_id, None)
            return
        try:
            active_posix_group = os.name == "posix" and not session.done
            if os.name == "nt" and session.process.returncode is None:
                # The frozen native Windows API terminates PowerShell,
                # which does not itself stop its external child programs.
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(session.process.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                try:
                    await asyncio.wait_for(killer.wait(), timeout=5)
                except TimeoutError:
                    killer.kill()
                    await killer.wait()
            elif active_posix_group:
                # Native background processes have start_new_session=True.
                # Signal their group even if its shell exited before a
                # child holding inherited stdout finished.
                with suppress(ProcessLookupError):
                    os.killpg(session.process.pid, signal.SIGTERM)
            await shell.process(action="kill", session_id=session_id)
            if active_posix_group:
                with suppress(ProcessLookupError):
                    os.killpg(session.process.pid, signal.SIGKILL)
            if session.process.returncode is None:
                raise RuntimeError("Native background process did not terminate")
            await shell.process(action="remove", session_id=session_id)
            cleanup.sessions.pop(session_id, None)
        except Exception:
            errors.append(session_id)

    try:
        # Native cleanup deadlines apply per session. Drain this handler's
        # snapshot together so a batch TERM grace does not grow with its count.
        await asyncio.gather(*(close_session(session_id, session)
                               for session_id, session in list(getattr(cleanup, "sessions", {}).items())))
    finally:
        current_tool_context.reset(token)
        _close_approval_queue(handler)
    if errors:
        raise RuntimeError("Background cleanup failed for native sessions: " + ", ".join(errors))


def close_tools(handler: Any):
    """Synchronous compatibility entry; running async callers await its task."""
    cleanup = getattr(handler, "_metaskill_background_cleanup", None)
    if not getattr(cleanup, "sessions", None):
        _close_approval_queue(handler)
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(aclose_tools(handler))
    return loop.create_task(aclose_tools(handler), name="metaskill-background-cleanup")
