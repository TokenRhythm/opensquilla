"""ptc_run — Programmatic Tool Calling (PTC) executor.

Ports the deepseek-harness "Code Mode" / ``run_code`` pattern (see the
cloned reference repo at ``deepseek-harness/packages/core/tools``) to
OpenSquilla.

PTC = "write a small executable program once, then run it end-to-end".
Instead of the model emitting one tool call, reading one result, and deciding
the next step for every step of a long / repetitive workflow, the model
writes ONE async Python program that calls tools in-process through a
``tools`` namespace. The program runs to completion; only its ``print(...)``
lines and its return value re-enter the model context. Long loops
(8 regions × 10 samples → aggregate) execute as a single tool call, saving
model round-trips, tokens, and latency.

Program contract (mirrors the harness's Python SDK):
  * ``code`` is the BODY of an async function taking one argument ``tools``.
  * Call tools as ``await tools.<name>(**args)`` — exotic names via
    ``await tools["<name>"](**args)``.
  * Each call resolves to the tool's canonical value: JSON-decoded when the
    tool returns JSON, otherwise the raw string.
  * A FAILED call raises ``ToolCallError`` with ``tool_name`` and ``message``
    — ``try/except`` it to handle and continue.
  * Independent read-only calls MAY overlap with ``asyncio.gather``;
    dependent work uses ``await`` in order.
  * Emit results with ``return <value>`` and/or ``print(...)``. ONLY what you
    print or return comes back — intermediate tool results never enter the
    conversation, so curate.

Safety model (in-process execution):
  * Static preflight reuses ``code_exec``'s destructive / sensitive / network
    guards; raw network I/O is blocked with a pointer to the web tools.
  * The program may only call tools in its allowed set (``allowed_tools`` or
    the safe default), minus a hard-deny control set. Every sub-call still
    traverses the full dispatch pipeline (policy chain, approval seams, run
    budget), so a program cannot bypass any tool gate.
"""

from __future__ import annotations

import asyncio
import collections
import datetime
import functools
import io
import itertools
import json
import math
import re
import statistics
import sys
import textwrap
import time
import uuid
from typing import TYPE_CHECKING, Any

from opensquilla.tool_boundary import ToolCall
from opensquilla.tools.registry import get_default_registry, tool
from opensquilla.tools.types import ToolError, current_tool_context

if TYPE_CHECKING:
    from opensquilla.tools.registry import ToolRegistry

_PTC_MAX_TIMEOUT = 600
_PTC_DEFAULT_TIMEOUT = 120
_PTC_MAX_OUTPUT_CHARS = 50_000
_PTC_MAX_LOG_LINES = 400

#: Safe default tool set the program may call when the caller omits
#: ``allowed_tools``. Read / search / web / local execution only.
_PTC_DEFAULT_ALLOW: frozenset[str] = frozenset(
    {
        "read_file",
        "glob_search",
        "grep_search",
        "list_dir",
        "web_discover",
        "web_search",
        "web_fetch",
        "execute_code",
        "git_status",
        "git_diff",
        "git_log",
    }
)

#: Tools a program must never call, even if listed in ``allowed_tools``.
_PTC_HARD_DENY: frozenset[str] = frozenset(
    {
        "ptc_run",
        "meta_invoke",
        "router_control",
        "plan_control",
        "goal_control",
        "cron",
        "gateway",
        "admin",
        "agents_list",
        "subagents",
        "sessions",
        "session_search",
        "submit",
        "message",
        "publish_artifact",
    }
)

#: Optional registry override injected by the gateway when it builds tool
#: surfaces from a non-default registry instance. Falls back to
#: :func:`opensquilla.tools.registry.get_default_registry`.
_PTC_REGISTRY_OVERRIDE: ToolRegistry | None = None


def bind_ptc_registry(registry: ToolRegistry | None) -> None:
    """Point ``ptc_run`` sub-dispatch at a specific registry (gateway seam)."""
    global _PTC_REGISTRY_OVERRIDE  # noqa: PLW0603
    _PTC_REGISTRY_OVERRIDE = registry


def _resolve_registry() -> ToolRegistry:
    return _PTC_REGISTRY_OVERRIDE or get_default_registry()


class PTCError(Exception):
    """Program-visible error for one failed tool call (mirrors harness ToolCallError)."""

    def __init__(self, tool_name: str, message: str) -> None:
        super().__init__(f"tool {tool_name!r} failed: {message}")
        self.tool_name = tool_name
        self.message = message


ToolCallError = PTCError


class _ToolsNamespace:
    """The ``tools`` object exposed to a PTC program.

    Every attribute access / subscript returns an async partial that, when
    awaited with keyword arguments, dispatches one nested tool call through
    the full tool pipeline and resolves to the tool's canonical value.
    """

    __slots__ = ("_handler", "_allowed")

    def __init__(self, *, handler: Any, allowed: frozenset[str]) -> None:
        self._handler = handler
        self._allowed = allowed

    def list(self) -> list[str]:
        """Return the sorted allowed tool names (program introspection)."""
        return sorted(self._allowed)

    async def _invoke(self, name: str, **kwargs: Any) -> Any:
        if name not in self._allowed:
            raise PTCError(
                name,
                f"tool {name!r} is not in the program's allowed tool set: {sorted(self._allowed)}",
            )
        call = ToolCall(
            tool_use_id=f"ptc_{uuid.uuid4().hex[:12]}",
            tool_name=name,
            arguments=kwargs,
            origin_trace="ptc_run",
        )
        result = await self._handler(call)
        if getattr(result, "is_error", False):
            raise PTCError(
                name,
                str(getattr(result, "content", "") or "tool call failed"),
            )
        content = str(getattr(result, "content", "") or "")
        try:
            return json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError):
            return content

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return functools.partial(self._invoke, name)

    def __getitem__(self, name: str) -> Any:
        return functools.partial(self._invoke, name)


def _json_safe(value: Any) -> Any:
    """Return a JSON-serializable projection of a program's return value."""
    if value is None or isinstance(value, (str, int, float, bool, list, dict)):
        try:
            json.dumps(value)
            return value
        except (TypeError, ValueError):
            pass
    text = repr(value)
    if len(text) > _PTC_MAX_OUTPUT_CHARS:
        text = text[:_PTC_MAX_OUTPUT_CHARS] + "...<truncated>"
    return text


def _split_logs(text: str) -> list[str]:
    lines = text.splitlines()
    if len(lines) > _PTC_MAX_LOG_LINES:
        lines = lines[:_PTC_MAX_LOG_LINES] + ["...<truncated>"]
    return lines


_CODE_FENCE_RE = re.compile(r"^```[A-Za-z0-9+_-]*\s*$")
_FUNC_DEF_RE = re.compile(
    r"^\s*(?:async\s+)?def\s+(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^)]*)\)\s*:\s*$"
)


def _is_main_def(match: re.Match[str]) -> bool:
    """Whether a leading def line is the entry function rather than a helper."""
    if match.group("name") in {"main", "_ptc_main"}:
        return True
    first_param = match.group("params").split(",", 1)[0].strip().split(":")[0].strip()
    return first_param == "tools"


def _normalize_program_body(code: str) -> str:
    """Normalize a model-emitted program body before it is wrapped.

    Correct input is unchanged. Handles the common corruption modes:
      * a surrounding ```python ... ``` markdown fence,
      * a leading entry-function line the model was told not to emit, and
      * uniform extra indentation across the whole body.
    Mixed indentation inside the body is intentionally left intact so a real
    syntax error surfaces in the compile message with its source line.
    """
    lines = code.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and _CODE_FENCE_RE.match(lines[0].strip()):
        lines.pop(0)
    if lines and _CODE_FENCE_RE.match(lines[-1].strip()):
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines:
        match = _FUNC_DEF_RE.match(lines[0])
        if match and _is_main_def(match):
            lines = lines[1:]
    return textwrap.dedent("\n".join(lines)).strip("\n")


async def _execute_program(
    code: str,
    ns: _ToolsNamespace,
    timeout: float,
) -> tuple[list[str], Any, int, str, str]:
    """Run one PTC program body; returns (logs, value, elapsed_ms, status, message)."""
    body = _normalize_program_body(code)
    wrapped = "async def _ptc_main(tools):\n" + textwrap.indent(body, "    ")
    globals_dict: dict[str, Any] = {
        "asyncio": asyncio,
        "ToolCallError": ToolCallError,
        "tools": ns,
        "json": json,
        "re": re,
        "textwrap": textwrap,
        "functools": functools,
        "time": time,
        "collections": collections,
        "datetime": datetime,
        "itertools": itertools,
        "math": math,
        "statistics": statistics,
    }
    try:
        exec(compile(wrapped, "<ptc_run>", "exec"), globals_dict)  # noqa: S102 - model code execution is the tool's contract
    except SyntaxError as exc:
        detail = f"line {exc.lineno or '?'}: {exc.msg}"
        if exc.text:
            detail += f" | {exc.text.strip()}"
        return [], None, 0, "error", f"program failed to compile: {detail}"
    except Exception as exc:  # noqa: BLE001 - surfaced to the model for self-correction
        return [], None, 0, "error", f"program failed to compile: {exc}"
    main = globals_dict.get("_ptc_main")
    if main is None:
        return [], None, 0, "error", "program did not define the async main body"

    real_stdout = sys.stdout
    buffer = io.StringIO()
    started = time.monotonic()
    status = "ok"
    message = ""
    value: Any = None
    try:
        sys.stdout = buffer
        try:
            value = await asyncio.wait_for(main(ns), timeout=timeout)
        except TimeoutError:
            status = "error"
            message = f"program timed out after {timeout:g}s"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the model for self-correction
            status = "error"
            message = f"{type(exc).__name__}: {exc}"
    finally:
        sys.stdout = real_stdout
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return _split_logs(buffer.getvalue()), value, elapsed_ms, status, message


@tool(
    name="ptc_run",
    description=(
        "Execute ONE async Python program that calls tools in-process via a "
        "`tools` namespace, and return only the program's printed lines and "
        "return value. Use this when a task is a long or repetitive sequence "
        "of tool calls (for example sampling 8 regions x 10 times, then "
        "aggregating) that should run as a single program instead of many "
        "model round-trips — the Programmatic Tool Calling (PTC) pattern. "
        "The `code` argument is the BODY of an async function taking one "
        "argument `tools`; call tools as `await tools.<name>(**args)` per the "
        "declarations in the calling prompt. Only what the program prints or "
        "returns comes back, so curate it."
    ),
    params={
        "code": {
            "type": "string",
            "description": (
                "The BODY of an async function taking one argument `tools`. "
                "Call tools as `await tools.name(**args)`; a failed call "
                "raises ToolCallError(tool_name, message) — catch it to "
                "handle and continue. Use `print(...)` and/or `return "
                "<value>`; only those re-enter the conversation."
            ),
        },
        "description": {
            "type": "string",
            "description": (
                "Short summary of what the program does in active voice, "
                "5-10 words (shown in the UI)."
            ),
        },
        "timeout": {
            "type": "number",
            "description": (
                f"Execution timeout in seconds (1-{_PTC_MAX_TIMEOUT}, "
                f"default {_PTC_DEFAULT_TIMEOUT})."
            ),
        },
        "allowed_tools": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Optional allowlist of tool names the program may call. "
                "When omitted, a safe read/search/web/execute_code default "
                "set is used. Control tools are always denied."
            ),
        },
    },
    required=["code", "description"],
    execution_timeout_argument="timeout",
    execution_timeout_padding=10.0,
)
async def ptc_run(
    code: str,
    description: str,  # noqa: ARG001 - surfaced in the result payload
    timeout: float = _PTC_DEFAULT_TIMEOUT,
    allowed_tools: list[str] | None = None,
) -> str:
    """Run a PTC program against the registered tools; returns a JSON envelope."""
    if not str(code or "").strip():
        raise ToolError("ptc_run: code must not be empty")
    if not str(description or "").strip():
        raise ToolError("ptc_run: description must not be empty")
    timeout = max(1.0, min(float(timeout or _PTC_DEFAULT_TIMEOUT), _PTC_MAX_TIMEOUT))

    # ── Static safety preflight (reuse code_exec guards) ──────────────
    from opensquilla.tools.builtin import code_exec as _code_exec
    from opensquilla.tools.run_mode import full_host_access_active

    if not full_host_access_active():
        external = _code_exec._check_code_sensitive_external_transfer(code)
        if external is not None:
            return json.dumps(
                {
                    "status": "blocked",
                    "reason": "sensitive_external_transfer",
                    "tool": "ptc_run",
                    "sensitive_reference": external,
                },
                ensure_ascii=False,
            )
        sensitive = _code_exec._check_code_sensitive_access(code)
        if sensitive is not None:
            reason, marker = sensitive
            return json.dumps(
                {
                    "status": "blocked",
                    "reason": reason,
                    "tool": "ptc_run",
                    "sensitive_reference": marker,
                },
                ensure_ascii=False,
            )
        destructive = _code_exec._check_code_destructive(code)
        if destructive is not None:
            return json.dumps(
                {
                    "status": "blocked",
                    "reason": "destructive_code",
                    "tool": "ptc_run",
                    "detail": destructive,
                },
                ensure_ascii=False,
            )
        if _code_exec._code_needs_network(code):
            return json.dumps(
                {
                    "status": "blocked",
                    "reason": "raw_network_in_program",
                    "tool": "ptc_run",
                    "message": (
                        "Raw network I/O is not allowed inside a PTC program. "
                        "Use `await tools.web_search(...)` / "
                        "`await tools.web_fetch(...)` instead, and aggregate "
                        "the returned values in the program."
                    ),
                },
                ensure_ascii=False,
            )

    # ── Registry + allowlist resolution ──────────────────────────────
    registry = _resolve_registry()
    registered_names = frozenset(registry.list_names())
    requested = (
        frozenset(str(item) for item in (allowed_tools or ()) if str(item).strip())
        if allowed_tools
        else _PTC_DEFAULT_ALLOW
    )
    unknown = requested - registered_names
    if unknown:
        raise ToolError(
            "ptc_run: unknown tool names in allowed_tools: " + ", ".join(sorted(unknown)),
        )
    allowed = (requested - _PTC_HARD_DENY) & registered_names

    from opensquilla.tools.dispatch import build_tool_handler

    handler = build_tool_handler(registry, current_tool_context.get())
    namespace = _ToolsNamespace(handler=handler, allowed=allowed)
    logs, value, elapsed_ms, status, message = await _execute_program(
        code,
        namespace,
        timeout,
    )

    payload: dict[str, Any] = {
        "status": status,
        "description": str(description or ""),
        "elapsed_ms": elapsed_ms,
        "logs": logs,
    }
    if status == "ok":
        payload["result"] = _json_safe(value)
    else:
        payload["error"] = message
    return json.dumps(payload, ensure_ascii=False)


__all__ = ["PTCError", "ToolCallError", "bind_ptc_registry", "ptc_run"]
