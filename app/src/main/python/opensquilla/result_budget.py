"""Shared tool runtime/result guard helpers.

The runtime guards apply at tool boundaries, not at skill boundaries, so
installed skills cannot bypass explicit per-call resource caps. Model-context
budgeting is handled later in the agent/provider request view.

Lives at the top level (rather than inside ``opensquilla.tools``) so that the
engine layer can import these helpers without triggering the tool-registry
side effect in ``opensquilla.tools.__init__``. See
``tests/test_public_tool_surface.py::test_engine_types_import_does_not_register_builtin_tools``.
"""

from __future__ import annotations

import asyncio
import json
import shlex
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeGuard

from opensquilla.search.normalize import canonicalize_query_key
from opensquilla.search_tool_outcome import (
    WebRetrievalSemanticKey,
    parse_web_tool_outcome,
    web_retrieval_semantic_key,
)

WEB_FETCH_MIN_MAX_CHARS = 100
WEB_SEARCH_MIN_MAX_CHARS_PER_SOURCE = 200
RetrievalKey = tuple[str, str, str, str]


class ToolResultBudgetClass(StrEnum):
    EXTERNAL = "external"
    LOCAL = "local"
    ARTIFACT = "artifact"
    ERROR = "error"
    CONTROL = "control"
    UNKNOWN = "unknown"


EXTERNAL_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "http_request",
        "web_discover",
        "web_fetch",
        "web_search",
    }
)

CONTROL_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "sessions_yield",
    }
)

EXECUTION_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "background_process",
        "exec_command",
        "execute_code",
        "process",
    }
)

_SEMANTIC_RESULT_PRESERVE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "git_diff",
        "read_file",
    }
)
_SOURCE_CONTEXT_EXEC_MAX_CHARS = 200_000
_SOURCE_CONTEXT_FILE_SUFFIXES = (
    ".bash",
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".gradle",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".lua",
    ".m",
    ".mjs",
    ".md",
    ".mm",
    ".php",
    ".proto",
    ".py",
    ".rb",
    ".rs",
    ".scala",
    ".scss",
    ".sh",
    ".sql",
    ".svelte",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
)
_SOURCE_CONTEXT_FILE_BASENAMES = frozenset(
    {
        "build.gradle",
        "cargo.toml",
        "cmakelists.txt",
        "dockerfile",
        "gemfile",
        "go.mod",
        "makefile",
        "package.json",
        "pom.xml",
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "tsconfig.json",
    }
)


@dataclass(frozen=True)
class ToolResultBudgetPolicy:
    max_single_tool_result_chars: int | None = None
    max_single_external_result_chars: int | None = None
    max_single_execution_result_chars: int | None = None
    max_tool_result_chars_per_turn: int | None = None
    max_external_tool_result_chars_per_turn: int | None = None
    max_execution_tool_result_chars_per_turn: int | None = None
    min_error_result_chars: int = 512
    min_control_result_chars: int = 512


DEFAULT_TOOL_RESULT_BUDGET_POLICY = ToolResultBudgetPolicy()


@dataclass(frozen=True)
class ToolRunBudgetPolicy:
    max_web_search_calls_per_turn: int | None = None
    max_web_fetch_calls_per_turn: int | None = None
    max_external_text_chars_per_turn: int | None = None
    max_single_fetch_chars: int | None = 50_000
    max_web_search_results: int | None = 10
    max_web_search_fetch_top_k: int | None = 3
    max_web_search_chars_per_source: int | None = 1500
    max_repeated_retrievals_per_turn: int | None = 2


DEFAULT_TOOL_RUN_BUDGET_POLICY = ToolRunBudgetPolicy()


def build_web_retrieval_tool_run_budget_policy(
    *,
    max_web_search_calls_per_turn: int | None = None,
    max_web_fetch_calls_per_turn: int | None = None,
    max_external_text_chars_per_turn: int | None = None,
    max_single_fetch_chars: int | None = 50_000,
    max_web_search_results: int | None = 10,
    max_web_search_fetch_top_k: int | None = 3,
    max_web_search_chars_per_source: int | None = 1500,
    max_repeated_retrievals_per_turn: int | None = 2,
) -> ToolRunBudgetPolicy:
    """Build an explicit web retrieval budget policy for benchmark/profile use.

    The defaults intentionally match the normal runtime: no per-turn call-count
    caps and no aggregate external-text cap. Callers must opt in to tighter
    limits by passing concrete values.
    """
    return ToolRunBudgetPolicy(
        max_web_search_calls_per_turn=max_web_search_calls_per_turn,
        max_web_fetch_calls_per_turn=max_web_fetch_calls_per_turn,
        max_external_text_chars_per_turn=max_external_text_chars_per_turn,
        max_single_fetch_chars=max_single_fetch_chars,
        max_web_search_results=max_web_search_results,
        max_web_search_fetch_top_k=max_web_search_fetch_top_k,
        max_web_search_chars_per_source=max_web_search_chars_per_source,
        max_repeated_retrievals_per_turn=max_repeated_retrievals_per_turn,
    )


class ToolRunBudgetExceededError(RuntimeError):
    """Raised when a tool call would exceed the per-turn run budget."""

    def __init__(self, tool_name: str, message: str) -> None:
        super().__init__(message)
        self.tool_name = tool_name


class DuplicateRetrievalInFlightError(RuntimeError):
    """Raised when the same semantic search is already executing this turn."""

    def __init__(self, tool_name: str) -> None:
        super().__init__(
            f"Tool '{tool_name}' skipped a duplicate retrieval already in flight."
        )
        self.tool_name = tool_name


class TerminalRetrievalReplayError(RuntimeError):
    """Raised when a semantic search already ended in a terminal failure."""

    def __init__(self, tool_name: str, *, error_kind: str) -> None:
        super().__init__(
            f"Tool '{tool_name}' skipped a retrieval that already failed "
            "without retry permission this turn."
        )
        self.tool_name = tool_name
        self.error_kind = error_kind


@dataclass(frozen=True)
class ToolRunBudgetReservation:
    tool_name: str
    arguments: dict[str, Any]
    reserved_external_text_chars: int = 0
    counted_as_fetch: bool = False
    counted_as_search: bool = False
    counted_as_external_text: bool = False
    retrieval_key: RetrievalKey | None = None
    semantic_retrieval_key: WebRetrievalSemanticKey | None = None


class ToolRunBudgetTracker:
    """Concurrency-safe per-turn accounting for tool calls and raw text."""

    def __init__(self, policy: ToolRunBudgetPolicy | None = None) -> None:
        self.policy = policy or DEFAULT_TOOL_RUN_BUDGET_POLICY
        self._lock = asyncio.Lock()
        self._web_search_calls_used = 0
        self._web_fetch_calls_used = 0
        self._external_text_chars_used = 0
        self._external_text_chars_reserved = 0
        self._retrieval_keys_used: dict[RetrievalKey, int] = {}
        self._inflight_retrieval_keys: set[WebRetrievalSemanticKey] = set()
        self._terminal_retrieval_keys: dict[WebRetrievalSemanticKey, str] = {}

    async def reserve_tool_call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolRunBudgetReservation:
        args = dict(arguments)
        if tool_name in {"web_search", "web_discover"}:
            async with self._lock:
                semantic_key = web_retrieval_semantic_key(args)
                terminal_error_kind = self._terminal_retrieval_keys.get(semantic_key)
                if terminal_error_kind is not None:
                    raise TerminalRetrievalReplayError(
                        tool_name,
                        error_kind=terminal_error_kind,
                    )
                if semantic_key in self._inflight_retrieval_keys:
                    raise DuplicateRetrievalInFlightError(tool_name)
                self._check_call_budget(
                    tool_name=tool_name,
                    used=self._web_search_calls_used,
                    limit=self.policy.max_web_search_calls_per_turn,
                )
                self._check_external_text_available(tool_name)
                retrieval_key = self._reserve_retrieval_key(tool_name, args)
                self._inflight_retrieval_keys.add(semantic_key)
                self._web_search_calls_used += 1
            return ToolRunBudgetReservation(
                tool_name=tool_name,
                arguments=args,
                counted_as_search=True,
                counted_as_external_text=True,
                retrieval_key=retrieval_key,
                semantic_retrieval_key=semantic_key,
            )

        if tool_name not in EXTERNAL_TOOL_NAMES:
            return ToolRunBudgetReservation(tool_name=tool_name, arguments=args)

        async with self._lock:
            self._check_call_budget(
                tool_name=tool_name,
                used=self._web_fetch_calls_used,
                limit=self.policy.max_web_fetch_calls_per_turn,
            )
            reserved = self._reserve_external_text_budget(tool_name, args)
            self._web_fetch_calls_used += 1
            return ToolRunBudgetReservation(
                tool_name=tool_name,
                arguments=args,
                reserved_external_text_chars=reserved,
                counted_as_fetch=True,
                counted_as_external_text=True,
            )

    async def commit_tool_result(
        self,
        reservation: ToolRunBudgetReservation,
        content: Any,
    ) -> None:
        if not reservation.counted_as_external_text:
            return
        text = content if isinstance(content, str) else str(content)
        web_outcome = parse_web_tool_outcome(reservation.tool_name, content)
        async with self._lock:
            self._release_external_reservation(reservation)
            self._external_text_chars_used += len(text)
            semantic_key = reservation.semantic_retrieval_key
            if semantic_key is not None:
                self._inflight_retrieval_keys.discard(semantic_key)
                if web_outcome is not None and web_outcome.retry_allowed is False:
                    self._terminal_retrieval_keys[semantic_key] = web_outcome.error_kind

    async def abort_tool_result(self, reservation: ToolRunBudgetReservation) -> None:
        if (
            not reservation.counted_as_fetch
            and not reservation.counted_as_search
            and not reservation.counted_as_external_text
        ):
            return
        async with self._lock:
            self._release_external_reservation(reservation)
            if reservation.semantic_retrieval_key is not None:
                self._inflight_retrieval_keys.discard(
                    reservation.semantic_retrieval_key
                )
            if reservation.counted_as_fetch:
                self._web_fetch_calls_used = max(0, self._web_fetch_calls_used - 1)
            if reservation.counted_as_search and reservation.tool_name in {
                "web_search",
                "web_discover",
            }:
                self._web_search_calls_used = max(0, self._web_search_calls_used - 1)
            self._release_retrieval_key(reservation.retrieval_key)

    async def snapshot(self) -> dict[str, object]:
        async with self._lock:
            return {
                "web_search_calls_used": self._web_search_calls_used,
                "web_fetch_calls_used": self._web_fetch_calls_used,
                "external_text_chars_used": self._external_text_chars_used,
                "external_text_chars_reserved": self._external_text_chars_reserved,
                "retrieval_loop_guard": [
                    {
                        "tool_name": tool_name,
                        "query": query,
                        "provider": provider,
                        "mode": mode,
                        "count": count,
                    }
                    for (
                        tool_name,
                        query,
                        provider,
                        mode,
                    ), count in sorted(self._retrieval_keys_used.items())
                ],
            }

    def _reserve_external_text_budget(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> int:
        cap = self.policy.max_single_fetch_chars
        remaining = self._external_text_remaining()
        if remaining is not None:
            if remaining <= 0:
                self._raise_external_text_exhausted(tool_name)
            cap = remaining if cap is None else min(cap, remaining)
        if cap is None:
            return 0
        cap = max(0, int(cap))
        if tool_name == "web_fetch":
            if cap < WEB_FETCH_MIN_MAX_CHARS:
                raise ToolRunBudgetExceededError(
                    tool_name,
                    (
                        "web_fetch cannot enforce the remaining run budget below "
                        f"{WEB_FETCH_MIN_MAX_CHARS} characters."
                    ),
                )
            requested = arguments.get("max_chars")
            try:
                requested_int = int(requested) if requested is not None else None
            except (TypeError, ValueError):
                requested_int = None
            if requested_int is None or requested_int > cap:
                arguments["max_chars"] = cap
        self._external_text_chars_reserved += cap
        return cap

    def _external_text_remaining(self) -> int | None:
        total = self.policy.max_external_text_chars_per_turn
        if total is None:
            return None
        return total - self._external_text_chars_used - self._external_text_chars_reserved

    def _check_external_text_available(self, tool_name: str) -> None:
        remaining = self._external_text_remaining()
        if remaining is not None and remaining <= 0:
            self._raise_external_text_exhausted(tool_name)

    def _raise_external_text_exhausted(self, tool_name: str) -> None:
        total = self.policy.max_external_text_chars_per_turn
        raise ToolRunBudgetExceededError(
            tool_name,
            f"Tool '{tool_name}' exceeded the external text run budget ({total}).",
        )

    def _release_external_reservation(
        self,
        reservation: ToolRunBudgetReservation,
    ) -> None:
        if reservation.reserved_external_text_chars:
            self._external_text_chars_reserved = max(
                0,
                self._external_text_chars_reserved
                - reservation.reserved_external_text_chars,
            )

    def _reserve_retrieval_key(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> RetrievalKey | None:
        limit = self.policy.max_repeated_retrievals_per_turn
        if limit is None:
            return None
        key = self._retrieval_key(tool_name, arguments)
        used = self._retrieval_keys_used.get(key, 0)
        if used >= limit:
            raise ToolRunBudgetExceededError(
                tool_name,
                f"Tool '{tool_name}' blocked repeated retrieval for the same request key.",
            )
        self._retrieval_keys_used[key] = used + 1
        return key

    def _release_retrieval_key(self, key: RetrievalKey | None) -> None:
        if key is None:
            return
        used = self._retrieval_keys_used.get(key, 0)
        if used <= 1:
            self._retrieval_keys_used.pop(key, None)
        else:
            self._retrieval_keys_used[key] = used - 1

    @staticmethod
    def _retrieval_key(tool_name: str, arguments: dict[str, Any]) -> RetrievalKey:
        query = canonicalize_query_key(str(arguments.get("query") or ""))
        provider = str(arguments.get("provider") or "auto").strip().lower() or "auto"
        mode = str(arguments.get("mode") or "auto").strip().lower() or "auto"
        return (tool_name, query, provider, mode)

    @staticmethod
    def _check_call_budget(
        *,
        tool_name: str,
        used: int,
        limit: int | None,
    ) -> None:
        if limit is not None and used >= limit:
            raise ToolRunBudgetExceededError(
                tool_name,
                f"Tool '{tool_name}' exceeded the per-turn call budget ({limit}).",
            )


def _safe_shell_tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _command_segments(command: str) -> list[str]:
    normalized = command.replace("\n", ";")
    for separator in ("&&", "||", "|"):
        normalized = normalized.replace(separator, ";")
    return [segment.strip() for segment in normalized.split(";") if segment.strip()]


def _token_looks_like_source_path(token: str) -> bool:
    value = token.strip().strip("\"'")
    if not value or value.startswith("-"):
        return False
    if any(char in value for char in "*?[]{}"):
        return False
    value = value.rstrip(",:;")
    if ":" in value:
        value = value.rsplit(":", 1)[0]
    lowered = value.lower()
    basename = lowered.rsplit("/", 1)[-1]
    return basename in _SOURCE_CONTEXT_FILE_BASENAMES or lowered.endswith(
        _SOURCE_CONTEXT_FILE_SUFFIXES
    )


def _exec_command_invokes_git_diff(command: str | None) -> bool:
    if not command:
        return False
    lowered = command.lower()
    return (
        lowered.strip().startswith("git diff")
        or " git diff " in f" {lowered} "
        or "\ngit diff " in lowered
        or "&& git diff " in lowered
        or "; git diff " in lowered
    )


def exec_command_invokes_git_diff(command: str | None) -> bool:
    """Return true when an exec command includes a focused git diff read."""
    return _exec_command_invokes_git_diff(command)


def _exec_command_invokes_source_context_read(
    command: str | None,
    *,
    content: str,
    content_chars: int | None = None,
) -> bool:
    content_length = len(content) if content_chars is None else content_chars
    if not command or content_length > _SOURCE_CONTEXT_EXEC_MAX_CHARS:
        return False
    for segment in _command_segments(command.lower()):
        tokens = _safe_shell_tokens(segment)
        if not tokens:
            continue
        executable = tokens[0].rsplit("/", 1)[-1]
        has_source_path = any(_token_looks_like_source_path(token) for token in tokens[1:])
        if executable == "cat" and has_source_path:
            return True
        if executable == "sed":
            has_print_range = "-n" in tokens and any("p" in token for token in tokens[1:])
            if has_print_range and has_source_path:
                return True
        if executable in {"rg", "grep"}:
            has_line_numbers = any(
                token == "-n" or (token.startswith("-") and "n" in token)
                for token in tokens[1:]
            )
            if has_line_numbers and has_source_path:
                return True
    return False


def exec_command_invokes_source_context_read(
    command: str | None,
    *,
    content: str,
    content_chars: int | None = None,
) -> bool:
    """Return true when an exec command reads bounded source/config context."""
    return _exec_command_invokes_source_context_read(
        command,
        content=content,
        content_chars=content_chars,
    )


def _exec_command_preserves_full_result(
    arguments: dict[str, Any] | None,
    *,
    content: str,
) -> bool:
    command = arguments.get("command") if isinstance(arguments, dict) else None
    command_text = command if isinstance(command, str) else None
    return _exec_command_invokes_git_diff(
        command_text
    ) or _exec_command_invokes_source_context_read(
        command_text,
        content=content,
    )


@dataclass(frozen=True)
class ToolResultBudgetDecision:
    content: str
    changed: bool
    original_chars: int
    returned_chars: int
    budget_class: ToolResultBudgetClass


class ToolResultBudgetTracker:
    """Concurrency-safe per-turn accounting for normalized result previews."""

    def __init__(self, policy: ToolResultBudgetPolicy | None = None) -> None:
        self.policy = policy or DEFAULT_TOOL_RESULT_BUDGET_POLICY
        self._lock = asyncio.Lock()
        self._tool_chars_used = 0
        self._external_chars_used = 0
        self._execution_chars_used = 0

    async def normalize(
        self,
        *,
        tool_name: str,
        content: str,
        budget_class: ToolResultBudgetClass,
        is_error: bool = False,
        arguments: dict[str, Any] | None = None,
    ) -> ToolResultBudgetDecision:
        if not isinstance(content, str):
            content = str(content)
        if is_error and budget_class is not ToolResultBudgetClass.CONTROL:
            budget_class = ToolResultBudgetClass.ERROR
        if budget_class is ToolResultBudgetClass.ARTIFACT:
            return ToolResultBudgetDecision(
                content=content,
                changed=False,
                original_chars=len(content),
                returned_chars=len(content),
                budget_class=budget_class,
            )
        if budget_class is ToolResultBudgetClass.EXTERNAL:
            single_limit = self.policy.max_single_external_result_chars
        else:
            single_limit = self.policy.max_single_tool_result_chars

        original_chars = len(content)
        execution_budget_applies = self._execution_budget_applies(
            tool_name=tool_name,
            content=content,
            is_error=is_error,
            arguments=arguments,
        )
        async with self._lock:
            limits: list[int] = []
            if single_limit is not None:
                limits.append(max(0, int(single_limit)))
            if (
                execution_budget_applies
                and self.policy.max_single_execution_result_chars is not None
            ):
                limits.append(max(0, int(self.policy.max_single_execution_result_chars)))
            if self.policy.max_tool_result_chars_per_turn is not None:
                limits.append(
                    max(
                        0,
                        int(self.policy.max_tool_result_chars_per_turn)
                        - self._tool_chars_used,
                    )
                )
            if (
                execution_budget_applies
                and self.policy.max_execution_tool_result_chars_per_turn is not None
            ):
                limits.append(
                    max(
                        0,
                        int(self.policy.max_execution_tool_result_chars_per_turn)
                        - self._execution_chars_used,
                    )
                )
            if (
                budget_class is ToolResultBudgetClass.EXTERNAL
                and self.policy.max_external_tool_result_chars_per_turn is not None
            ):
                limits.append(
                    max(
                        0,
                        int(self.policy.max_external_tool_result_chars_per_turn)
                        - self._external_chars_used,
                    )
                )
            if not limits:
                return ToolResultBudgetDecision(
                    content=content,
                    changed=False,
                    original_chars=original_chars,
                    returned_chars=original_chars,
                    budget_class=budget_class,
                )

            allowed = max(0, min(limits))
            if budget_class is ToolResultBudgetClass.ERROR:
                floor = self.policy.min_error_result_chars
                if single_limit is not None:
                    floor = min(int(single_limit), floor)
                allowed = max(allowed, floor)
            elif budget_class is ToolResultBudgetClass.CONTROL:
                floor = self.policy.min_control_result_chars
                if single_limit is not None:
                    floor = min(int(single_limit), floor)
                allowed = max(allowed, floor)
            if original_chars <= allowed:
                self._tool_chars_used += original_chars
                if budget_class is ToolResultBudgetClass.EXTERNAL:
                    self._external_chars_used += original_chars
                if execution_budget_applies:
                    self._execution_chars_used += original_chars
                return ToolResultBudgetDecision(
                    content=content,
                    changed=False,
                    original_chars=original_chars,
                    returned_chars=original_chars,
                    budget_class=budget_class,
                )

            compacted = compact_tool_result_content(
                tool_name=tool_name,
                content=content,
                max_preview_chars=allowed,
                budget_class=budget_class,
                is_error=is_error,
            )
            returned_chars = _preview_chars(compacted)
            self._tool_chars_used += returned_chars
            if budget_class is ToolResultBudgetClass.EXTERNAL:
                self._external_chars_used += returned_chars
            if execution_budget_applies:
                self._execution_chars_used += returned_chars
            return ToolResultBudgetDecision(
                content=compacted,
                changed=True,
                original_chars=original_chars,
                returned_chars=returned_chars,
                budget_class=budget_class,
            )

    @staticmethod
    def _execution_budget_applies(
        *,
        tool_name: str,
        content: str,
        is_error: bool,
        arguments: dict[str, Any] | None,
    ) -> bool:
        if is_error:
            return False
        if tool_name in _SEMANTIC_RESULT_PRESERVE_TOOL_NAMES:
            return False
        if tool_name not in EXECUTION_TOOL_NAMES:
            return False
        if tool_name == "exec_command" and _exec_command_preserves_full_result(
            arguments,
            content=content,
        ):
            return False
        return True


def resolve_budget_class(tool_name: str, explicit: Any = None) -> ToolResultBudgetClass:
    if isinstance(explicit, ToolResultBudgetClass):
        return explicit
    if isinstance(explicit, str):
        try:
            return ToolResultBudgetClass(explicit)
        except ValueError:
            pass
    if tool_name in CONTROL_TOOL_NAMES:
        return ToolResultBudgetClass.CONTROL
    if tool_name in EXTERNAL_TOOL_NAMES:
        return ToolResultBudgetClass.EXTERNAL
    return ToolResultBudgetClass.UNKNOWN


def clamp_tool_arguments(
    tool_name: str,
    arguments: dict[str, Any],
    policy: ToolRunBudgetPolicy,
) -> dict[str, Any]:
    next_args = dict(arguments)
    if tool_name == "web_fetch":
        requested = next_args.get("max_chars")
        cap = policy.max_single_fetch_chars
        if _is_plain_int(requested):
            value = max(100, requested)
            next_args["max_chars"] = min(value, cap) if cap is not None else value
        elif requested is None and cap is not None:
            next_args["max_chars"] = cap
    elif tool_name == "web_discover":
        # ``max_web_search_results`` is a pure ceiling: only clamp an explicit
        # value down. When the caller omits ``max_results`` we leave it absent so
        # the runtime default (the configured ``search_max_results``) governs,
        # rather than overriding it with the cap.
        requested = next_args.get("max_results")
        cap = policy.max_web_search_results
        if _is_plain_int(requested):
            value = max(1, requested)
            next_args["max_results"] = min(value, cap) if cap is not None else value
    elif tool_name == "web_search":
        requested_results = next_args.get("max_results")
        results_cap = policy.max_web_search_results
        if _is_plain_int(requested_results):
            value = max(1, requested_results)
            next_args["max_results"] = (
                min(value, results_cap) if results_cap is not None else value
            )

        requested_fetch_top_k = next_args.get("fetch_top_k")
        fetch_top_k_cap = policy.max_web_search_fetch_top_k
        if _is_plain_int(requested_fetch_top_k):
            value = max(0, requested_fetch_top_k)
            next_args["fetch_top_k"] = (
                min(value, fetch_top_k_cap) if fetch_top_k_cap is not None else value
            )
        elif requested_fetch_top_k is None and fetch_top_k_cap is not None:
            next_args["fetch_top_k"] = fetch_top_k_cap

        requested_chars = next_args.get("max_chars_per_source")
        chars_cap = policy.max_web_search_chars_per_source
        if _is_plain_int(requested_chars):
            value = max(WEB_SEARCH_MIN_MAX_CHARS_PER_SOURCE, requested_chars)
            next_args["max_chars_per_source"] = (
                min(value, chars_cap) if chars_cap is not None else value
            )
        elif requested_chars is None and chars_cap is not None:
            next_args["max_chars_per_source"] = chars_cap
    return next_args


def _is_plain_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def compact_tool_result_content(
    *,
    tool_name: str,
    content: str,
    max_preview_chars: int,
    budget_class: ToolResultBudgetClass,
    is_error: bool = False,
) -> str:
    max_preview_chars = max(0, max_preview_chars)
    original_chars = len(content)
    if budget_class is ToolResultBudgetClass.CONTROL:
        return _compact_control_json(
            tool_name=tool_name,
            content=content,
            max_preview_chars=max_preview_chars,
            original_chars=original_chars,
            budget_class=budget_class,
        )
    if max_preview_chars <= 0:
        preview = ""
        tail = ""
    else:
        head_chars = max(1, int(max_preview_chars * 0.65))
        tail_chars = max(0, max_preview_chars - head_chars)
        if tail_chars <= 0:
            preview = content[:max_preview_chars]
            tail = ""
        else:
            preview = content[:head_chars]
            tail = content[-tail_chars:]
    payload: dict[str, Any] = {
        "result_truncated": True,
        "result_original_chars": original_chars,
        "result_omitted_chars": max(0, original_chars - len(preview) - len(tail)),
        "tool": tool_name,
        "is_error": bool(is_error),
        "preview": preview,
    }
    if tail:
        payload["tail"] = tail
    return json.dumps(payload, ensure_ascii=False)


def _compact_control_json(
    *,
    tool_name: str,
    content: str,
    max_preview_chars: int,
    original_chars: int,
    budget_class: ToolResultBudgetClass,
) -> str:
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        preview = content[:max_preview_chars]
        return json.dumps(
            {
                "result_truncated": True,
                "result_original_chars": original_chars,
                "tool": tool_name,
                "preview": preview,
            },
            ensure_ascii=False,
        )
    if not isinstance(payload, dict):
        preview = content[:max_preview_chars]
        return json.dumps(
            {
                "result_truncated": True,
                "result_original_chars": original_chars,
                "tool": tool_name,
                "preview": preview,
            },
            ensure_ascii=False,
        )

    compacted = dict(payload)
    for key, value in list(compacted.items()):
        if isinstance(value, str) and len(value) > max_preview_chars:
            compacted[key] = value[:max_preview_chars]
    compacted["result_truncated"] = True
    compacted["result_original_chars"] = original_chars
    return json.dumps(compacted, ensure_ascii=False)


def _preview_chars(rendered: str) -> int:
    try:
        payload = json.loads(rendered)
    except (TypeError, ValueError):
        return len(rendered)
    if isinstance(payload, dict):
        preview = payload.get("preview")
        if isinstance(preview, str):
            tail = payload.get("tail")
            return len(preview) + (len(tail) if isinstance(tail, str) else 0)
        head = payload.get("head")
        tail = payload.get("tail")
        if isinstance(head, str) or isinstance(tail, str):
            return len(head or "") + len(tail or "")
        if payload.get("result_truncated") is True:
            return _string_value_chars(payload)
    return len(rendered)


def _string_value_chars(payload: dict[str, Any]) -> int:
    total = 0
    for value in payload.values():
        if isinstance(value, str):
            total += len(value)
    return total
