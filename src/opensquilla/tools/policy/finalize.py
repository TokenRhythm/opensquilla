"""Single execution-status finalisation point.

:func:`finalize` is the only place the new pipeline mints execution status
for a tool result. It branches on the four mutually exclusive outcomes from
the orchestrator — exception, approval-pending on an unsupported surface,
denial payload, and success — and always routes through
:func:`normalize_execution_status` exactly once.

The function preserves the budget-bypass behaviour: when
artifacts were published the raw content is returned unchanged; otherwise
the result is normalised through the budget tracker.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

import structlog

from opensquilla.engine.tool_result_store import ToolResultStore, ToolResultStoreBudgetError
from opensquilla.execution_status import (
    derive_is_error,
    execution_status_for_tool_result,
    mark_execution_status_truncated,
    normalize_execution_status,
)
from opensquilla.paths import default_opensquilla_home
from opensquilla.result_budget import (
    ToolResultBudgetTracker,
    ToolRunBudgetExceededError,
    resolve_budget_class,
)
from opensquilla.router_control import router_control_payload_terminates_turn
from opensquilla.safety.secret_redaction import redact_secret_text, redact_secret_value
from opensquilla.tool_boundary import ToolCall, ToolResult
from opensquilla.tools.envelope import build_tool_failure_envelope, is_denial_payload
from opensquilla.tools.types import CallerKind, InteractionMode, ToolContext

log = structlog.get_logger("opensquilla.tools.dispatch")

_PENDING_APPROVAL_STATUSES: frozenset[str] = frozenset(
    {"approval_required", "approval_pending"}
)
_MAX_TERMINAL_RESPONSE_CHARS = 2_000

_DIRECT_FILE_RESULT_TOOLS = frozenset({"read_file", "read_source"})
_SHELL_RC_BASENAMES = frozenset(
    {
        ".bash_login",
        ".bash_profile",
        ".bashrc",
        ".profile",
        ".zlogin",
        ".zprofile",
        ".zshenv",
        ".zshrc",
    }
)
_ENV_DUMP_COMMANDS = frozenset({"declare", "env", "export", "printenv", "set"})
_FILE_READ_COMMANDS = frozenset(
    {
        "awk",
        "bat",
        "batcat",
        "cat",
        "grep",
        "get-content",
        "head",
        "less",
        "more",
        "nl",
        "sed",
        "tac",
        "tail",
        "type",
        "view",
        "zcat",
    }
)
_PATTERN_FIRST_COMMANDS = frozenset({"awk", "grep", "sed"})
_OPENSQUILLA_HOME_PREFIXES = (
    "$OPENSQUILLA_HOME/",
    "${OPENSQUILLA_HOME}/",
    "$OPENSQUILLA_STATE_DIR/",
    "${OPENSQUILLA_STATE_DIR}/",
)
_HOME_PREFIXES = ("$HOME/", "${HOME}/")
_GREP_MATCH_RE = re.compile(
    r"^(?P<prefix>(?P<path>.+?)(?::\d+)?: )"
    r"(?P<content>[^\r\n]*)(?P<ending>\r?\n)?$"
)


_DISPATCH_TRUNCATION_RETRIEVE_HINT = (
    "This tool result was truncated before entering model context. "
    "Use retrieve_tool_result with handle=<tool_result_handle> to inspect the original raw output."
)


def _command_segments(command: str) -> list[str]:
    """Split shell pipelines and sequences without splitting quoted text."""
    segments: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    for character in command:
        if quote:
            buffer.append(character)
            if character == quote:
                quote = None
            continue
        if character in "'\"":
            quote = character
            buffer.append(character)
            continue
        if character in "|;&\r\n":
            segment = "".join(buffer).strip()
            if segment:
                segments.append(segment)
            buffer = []
            continue
        buffer.append(character)
    segment = "".join(buffer).strip()
    if segment:
        segments.append(segment)
    return segments


def _is_opensquilla_config(
    path: str,
    parts: list[str],
    *,
    opensquilla_home: bool = False,
) -> bool:
    if not parts or parts[-1] != "config.toml":
        return False
    if opensquilla_home or ".opensquilla" in parts[:-1]:
        return True
    try:
        candidate = Path(path.strip("\"'")).expanduser().resolve(strict=False)
        home = default_opensquilla_home().expanduser().resolve(strict=False)
        candidate.relative_to(home)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _is_secret_file_arg(argument: object) -> bool:
    if not isinstance(argument, str) or not argument.strip():
        return False
    path = argument.strip("\"'").replace("\\", "/")
    opensquilla_home = False
    for prefix in _OPENSQUILLA_HOME_PREFIXES:
        if path.startswith(prefix):
            path = path[len(prefix) :]
            opensquilla_home = True
            break
    for prefix in _HOME_PREFIXES:
        if path.startswith(prefix):
            path = path[len(prefix) :]
            break
    if "$" in path:
        return False
    parts = [part.lower() for part in path.split("/") if part]
    if not parts:
        return False
    basename = parts[-1]
    if (
        basename == ".env"
        or basename.startswith(".env.")
        or basename == ".envrc"
        or basename in _SHELL_RC_BASENAMES
    ):
        return True
    return _is_opensquilla_config(
        path,
        parts,
        opensquilla_home=opensquilla_home,
    )


def _is_env_dump_command(command: str) -> bool:
    for segment in _command_segments(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        if tokens and tokens[0].rsplit("/", 1)[-1].lower() in _ENV_DUMP_COMMANDS:
            return True
    return False


def _command_file_read_classification(command: str) -> tuple[bool, bool, bool]:
    """Return ordinary-read, secret-read, and unknown-segment flags."""
    reads_file = False
    reads_secret_file = False
    has_unknown_segment = False
    for segment in _command_segments(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        if not tokens:
            continue
        reader = tokens[0].rsplit("/", 1)[-1].lower()
        if reader not in _FILE_READ_COMMANDS:
            has_unknown_segment = True
            continue
        positional = [argument for argument in tokens[1:] if not argument.startswith("-")]
        if reader in _PATTERN_FIRST_COMMANDS:
            positional = positional[1:]
        if not positional:
            continue
        if any(_is_secret_file_arg(argument) for argument in positional):
            reads_secret_file = True
        else:
            reads_file = True
    return reads_file, reads_secret_file, has_unknown_segment


def _redact_grep_search_result(value: Any) -> Any:
    """Redact each grep match using the path emitted with that match."""
    if not isinstance(value, str):
        return redact_secret_value(value)
    redacted_lines: list[str] = []
    for line in value.splitlines(keepends=True):
        match = _GREP_MATCH_RE.match(line)
        if match is None:
            redacted_lines.append(redact_secret_text(line))
            continue
        secret_file = _is_secret_file_arg(match.group("path"))
        redacted_content = redact_secret_text(
            match.group("content"),
            code_file=not secret_file,
            secret_file=secret_file,
        )
        redacted_lines.append(
            match.group("prefix") + redacted_content + (match.group("ending") or "")
        )
    return "".join(redacted_lines)


def _tool_result_redaction_options(call: ToolCall) -> dict[str, bool]:
    """Choose assignment redaction from the source that produced the result."""
    if call.tool_name in _DIRECT_FILE_RESULT_TOOLS:
        secret_file = _is_secret_file_arg(call.arguments.get("path"))
        return {"code_file": not secret_file, "secret_file": secret_file}
    if call.tool_name == "source_symbols":
        return {"code_file": True, "secret_file": False}
    if call.tool_name == "exec_command":
        command = call.arguments.get("command", "")
        command = command if isinstance(command, str) else ""
        reads_file, reads_secret_file, has_unknown_segment = (
            _command_file_read_classification(command)
        )
        if _is_env_dump_command(command) or reads_secret_file:
            return {"code_file": False, "secret_file": True}
        if reads_file and not has_unknown_segment:
            return {"code_file": True, "secret_file": False}
        return {}
    return {}


def _registered_terminates_turn(registered: Any) -> bool:
    return bool(getattr(getattr(registered, "spec", None), "terminates_turn", False))


def _registered_terminal_response_text(
    registered: Any,
    content: Any,
    *,
    is_error: bool,
) -> str | None:
    """Extract one bounded completion receipt from an opted-in terminal tool."""

    spec = getattr(registered, "spec", None)
    field = str(getattr(spec, "terminal_response_field", "") or "").strip()
    if is_error or not field or not _registered_terminates_turn(registered):
        return None
    try:
        payload = json.loads(content) if isinstance(content, str) else content
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get(field)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _MAX_TERMINAL_RESPONSE_CHARS:
        return None
    return text


def _plan_checkpoint_terminates_turn(tool_name: str, content: Any) -> bool:
    """A blocked checkpoint is a hard execution boundary."""

    if tool_name != "plan_run_checkpoint":
        return False
    try:
        payload = json.loads(content) if isinstance(content, str) else content
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    run = payload.get("plan_run")
    return isinstance(run, dict) and run.get("status") == "blocked"


def _user_input_terminates_turn(tool_name: str, content: Any) -> bool:
    """Fallback surfaces without a deferred broker stop at the request."""

    if tool_name != "request_user_input":
        return False
    try:
        payload = json.loads(content) if isinstance(content, str) else content
    except (json.JSONDecodeError, TypeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("status") == "input_required"
        and payload.get("kind") == "user_input"
        and payload.get("paused") is True
    )


def _store_dispatch_truncated_snapshot(
    *,
    ctx: ToolContext | None,
    call: ToolCall,
    content: str,
) -> dict[str, Any] | None:
    """Persist raw output that dispatch-level result budgets truncated."""
    if (
        ctx is None
        or not ctx.tool_result_store_dir
        or not ctx.tool_result_retrieval_available
    ):
        return None

    session_id = (
        ctx.tool_result_store_session_id
        or ctx.artifact_session_id
        or ctx.session_key
    )
    session_key = ctx.session_key or session_id
    agent_id = ctx.agent_id or "main"
    if not session_id or not session_key or not agent_id:
        return None

    try:
        record = ToolResultStore(ctx.tool_result_store_dir).write(
            content,
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            session_id=session_id,
            session_key=session_key,
            agent_id=agent_id,
        )
    except ToolResultStoreBudgetError as exc:
        log.info(
            "dispatch.truncated_raw_snapshot_skipped",
            tool=call.tool_name,
            tool_use_id=call.tool_use_id,
            reason=str(exc),
        )
        return None
    except Exception as exc:  # pragma: no cover - tracing must not break tools
        log.warning(
            "dispatch.truncated_raw_snapshot_failed",
            tool=call.tool_name,
            tool_use_id=call.tool_use_id,
            error=str(exc),
        )
        return None

    return {
        "tool_result_handle": record.handle,
        "tool_result_sha256": record.sha256,
        "tool_result_storage_encoding": record.storage_encoding,
        "tool_result_stored_size_bytes": record.stored_size_bytes,
        "retrieve_hint": _DISPATCH_TRUNCATION_RETRIEVE_HINT,
    }


def _attach_dispatch_truncated_snapshot(
    *,
    content: str,
    snapshot: dict[str, Any] | None,
) -> str:
    if not snapshot:
        return content
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return content
    if not isinstance(payload, dict) or payload.get("result_truncated") is not True:
        return content
    payload.update(snapshot)
    return json.dumps(payload, ensure_ascii=False)


def _extract_pending_approval(content: Any) -> dict[str, Any] | None:
    """Return the payload when ``content`` carries a pending-approval status."""
    if isinstance(content, dict):
        payload = content
    elif isinstance(content, str):
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
    else:
        return None
    return payload if payload.get("status") in _PENDING_APPROVAL_STATUSES else None

def _denial_reason(content: Any) -> str:
    payload: Any = content
    if isinstance(content, str):
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return "denied"
    if isinstance(payload, dict) and payload.get("status") == "approval_denied":
        return "approval_denied"
    return "denied"

def _has_live_approval_surface(ctx: ToolContext | None) -> bool:
    return (
        ctx is None
        or ctx.interaction_mode is InteractionMode.INTERACTIVE
        # Channel turns are unattended from the process perspective, but the
        # channel approval notifier delivers a card/text decision back to the
        # originating sender. Treat that as an approval-capable surface.
        or ctx.caller_kind is CallerKind.CHANNEL
    )


def _uses_automatic_review(payload: dict[str, Any]) -> bool:
    approval_id = payload.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id:
        return False
    try:
        from opensquilla.gateway.approval_queue import get_approval_queue

        entry = get_approval_queue().get(approval_id)
    except (KeyError, RuntimeError):
        return False
    return bool(
        entry.namespace == "exec"
        and entry.params.get("reviewer") == "auto_review"
        and entry.params.get("humanActionable") is False
    )


async def finalize(
    call: ToolCall,
    ctx: ToolContext | None,
    raw_result: Any,
    exception: BaseException | None,
    artifact_start: int,
    budget_tracker: ToolResultBudgetTracker,
    registered: Any,
) -> ToolResult:
    """Build the canonical :class:`ToolResult` for one dispatched call.

    Branches on the orchestrator-provided outcome state. ``exception``
    takes precedence — when set, ``raw_result`` is ignored and a runtime
    error envelope is returned. With no exception, an
    ``approval_required`` payload returned to an unattended surface
    short-circuits to the approval-pending envelope. Otherwise the result
    flows through the budget tracker (unless artifacts were published)
    and execution-status pipeline.
    """
    # ---------------- Exception branch ----------------
    if exception is not None:
        if isinstance(exception, ToolRunBudgetExceededError):
            payload = {
                "status": "control",
                "tool": call.tool_name,
                "reason": "tool_run_budget_exhausted",
                "user_message": (
                    "The tool was skipped by a runtime resource guard. Continue with "
                    "available evidence or choose a smaller request."
                ),
                "retry_allowed": False,
            }
            status = {
                "version": 1,
                "status": "unknown",
                "exit_code": None,
                "timed_out": False,
                "truncated": False,
                "reason": "tool_run_budget_exhausted",
                "source": "tool_runtime",
                "preservation_class": "ephemeral",
            }
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content=json.dumps(payload),
                is_error=False,
                execution_status=normalize_execution_status(status),
                terminates_turn=False,
            )

        envelope = redact_secret_value(
            build_tool_failure_envelope(exception, call.tool_name)
        )
        log.warning(
            "dispatch.tool_failed",
            tool=call.tool_name,
            tool_use_id=call.tool_use_id,
            agent_id=ctx.agent_id if ctx else None,
            session_key=ctx.session_key if ctx else None,
            error_class=envelope["error_class"],
            retry_allowed=envelope["retry_allowed"],
            # ``finalize`` runs from the dispatcher's ``finally`` block after the
            # ``except`` clause has already handled the exception, so
            # ``sys.exc_info()`` is empty here — pass the exception object
            # explicitly so the traceback reaches debug.log.
            exc_info=exception,
        )
        status = {
            "version": 1,
            "status": "error",
            "exit_code": None,
            "timed_out": False,
            "truncated": False,
            "reason": "runtime_error",
            "source": "tool_runtime",
            "preservation_class": "diagnostic",
        }
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content=json.dumps(envelope),
            is_error=True,
            execution_status=normalize_execution_status(status),
            terminates_turn=False,
        )

    if call.tool_name == "grep_search":
        result = _redact_grep_search_result(raw_result)
    else:
        redaction_options = _tool_result_redaction_options(call)
        result = redact_secret_value(
            raw_result,
            code_file=redaction_options.get("code_file", False),
            secret_file=redaction_options.get("secret_file", False),
        )

    # ---------------- Approval-on-unsupported-surface branch ----------------
    if not _has_live_approval_surface(ctx):
        pending = _extract_pending_approval(result)
        if pending is not None and not _uses_automatic_review(pending):
            surface = ctx.caller_kind.value if ctx else "unknown"
            log.warning(
                "dispatch.approval_required_unsupported_surface",
                tool=call.tool_name,
                surface=surface,
                approval_id=pending.get("approval_id"),
                tool_use_id=call.tool_use_id,
                agent_id=ctx.agent_id if ctx else None,
                session_key=ctx.session_key if ctx else None,
            )
            user_message = (
                f"Tool '{call.tool_name}' requires human approval, but the {surface} "
                "surface has no interactive approval path. Re-run with --interactive "
                "or from an interactive operator surface."
            )
            envelope = build_tool_failure_envelope(
                ValueError("approval required"),
                call.tool_name,
                policy_denial=True,
                error_class_override="UnsupportedSurface",
                user_message_override=user_message,
            )
            status = {
                "version": 1,
                "status": "unknown",
                "exit_code": None,
                "timed_out": False,
                "truncated": False,
                "reason": "approval_pending",
                "source": "tool_runtime",
                "preservation_class": "ephemeral",
            }
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content=json.dumps(envelope),
                is_error=False,
                execution_status=normalize_execution_status(status),
                terminates_turn=False,
            )

    # ---------------- Standard branch (success or denial payload) ----------------
    denial = is_denial_payload(result)
    denial_reason = _denial_reason(result) if denial else None
    execution_status = execution_status_for_tool_result(call.tool_name, result)
    if execution_status is None:
        pending = _extract_pending_approval(result)
        if pending is not None:
            execution_status = {
                "version": 1,
                "status": "unknown",
                "exit_code": None,
                "timed_out": False,
                "truncated": False,
                "reason": "approval_pending",
                "source": "tool_runtime",
                "preservation_class": "ephemeral",
            }
    if execution_status is None and denial:
        execution_status = {
            "version": 1,
            "status": "error",
            "exit_code": None,
            "timed_out": False,
            "truncated": False,
            "reason": denial_reason or "denied",
            "source": "tool_runtime",
            "preservation_class": "diagnostic",
        }
    if execution_status is not None:
        execution_status = normalize_execution_status(execution_status)
        log.debug(
            "tool.execution_status_normalized",
            tool=call.tool_name,
            status=execution_status["status"],
            reason=execution_status["reason"],
            source=execution_status["source"],
        )

    status_is_error = derive_is_error(execution_status) if execution_status else False
    is_error = denial or status_is_error

    artifacts = (
        list(ctx.published_artifacts[artifact_start:]) if ctx is not None else []
    )
    if artifacts:
        content = result
    else:
        budget_class = resolve_budget_class(
            call.tool_name,
            registered.spec.result_budget_class,
        )
        raw_budget_content = result if isinstance(result, str) else str(result)
        budgeted = await budget_tracker.normalize(
            tool_name=call.tool_name,
            content=raw_budget_content,
            budget_class=budget_class,
            is_error=is_error,
            arguments=call.arguments,
        )
        snapshot = (
            _store_dispatch_truncated_snapshot(
                ctx=ctx,
                call=call,
                content=raw_budget_content,
            )
            if budgeted.changed
            else None
        )
        content = (
            _attach_dispatch_truncated_snapshot(
                content=budgeted.content,
                snapshot=snapshot,
            )
            if snapshot is not None
            else budgeted.content
        )
        # Dispatch limits are hard safety budgets. When retrieval is visible,
        # attach a Store handle to make the bounded result recoverable. When it
        # is unavailable, keep the bounded result's explicit truncation marker
        # rather than inventing a handle the model cannot use.
        if budgeted.changed and execution_status is not None:
            execution_status = mark_execution_status_truncated(execution_status)
    terminates_turn = (
        (_registered_terminates_turn(registered) and not is_error)
        or _plan_checkpoint_terminates_turn(call.tool_name, content)
        or _user_input_terminates_turn(call.tool_name, content)
        or (
            call.tool_name == "router_control"
            and router_control_payload_terminates_turn(content)
        )
    )
    return ToolResult(
        tool_use_id=call.tool_use_id,
        tool_name=call.tool_name,
        content=content,
        is_error=is_error,
        artifacts=artifacts,
        execution_status=execution_status,
        terminates_turn=terminates_turn,
        terminal_response_text=(
            _registered_terminal_response_text(
                registered,
                content,
                is_error=is_error,
            )
            if terminates_turn
            else None
        ),
    )
