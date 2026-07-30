"""Bounded assembly of inert proposer tool output.

Candidate-mode provider responses may contain native tool-call-shaped data even
though proposers cannot execute tools.  This builder retains that data as
host-rendered, untrusted JSON text without ever constructing executable tool
events or canonical argument dictionaries.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from opensquilla.provider.stream_assembly import (
    DEFAULT_MAX_TOOL_ARGUMENT_CHARS,
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_TOOL_NAME_CHARS,
    DEFAULT_MAX_TOOL_STREAM_EVENTS,
    DEFAULT_MAX_TOTAL_TOOL_ARGUMENT_CHARS,
)

_CANDIDATE_TOOL_IDENTITY_KEYS = frozenset(
    {"id", "call_id", "item_id", "tool_call_id", "tool_use_id"}
)
_MAX_MALFORMED_WRAPPER_DEPTH = 64
_MAX_MALFORMED_WRAPPER_NODES = 4096


class CandidateArtifactLimitError(ValueError):
    """An inert candidate artifact exceeded a response-local safety bound."""

    def __init__(
        self,
        *,
        operation: str,
        key: Any,
        reason: str,
        limit: int,
        observed: int,
    ) -> None:
        messages = {
            "too_many_calls": "candidate response exceeded the inert tool-call limit",
            "too_many_events": "candidate response exceeded the inert tool-event limit",
            "call_chars_exceeded": "one inert tool call exceeded the character limit",
            "total_chars_exceeded": "candidate response exceeded the aggregate character limit",
        }
        super().__init__(messages.get(reason, "candidate artifact exceeded a safety limit"))
        self.operation = operation
        self.key = key
        self.reason = reason
        self.limit = limit
        self.observed = observed


@dataclass
class _CandidateAction:
    name_parts: list[str] = field(default_factory=list)
    argument_parts: list[str] = field(default_factory=list)
    issues: set[str] = field(default_factory=set)
    finished: bool = False
    char_count: int = 0

    @property
    def name_text(self) -> str:
        return "".join(self.name_parts)

    @property
    def arguments_text(self) -> str:
        return "".join(self.argument_parts)

    @property
    def is_substantive(self) -> bool:
        if self.name_text.strip():
            return True
        arguments = self.arguments_text.strip()
        if not arguments:
            return False
        try:
            parsed = json.loads(
                arguments,
                parse_constant=CandidateArtifactBuilder._reject_nonfinite,
            )
        except (RecursionError, TypeError, ValueError, json.JSONDecodeError):
            # Malformed argument text is still provider-authored evidence.
            return True
        if parsed is None or parsed == "" or parsed == {} or parsed == []:
            return False
        return True


class CandidateArtifactBuilder:
    """Assemble native tool-call material into bounded, non-executable text.

    The stream key is used only for response-local assembly and is deliberately
    omitted from rendered output.  Mutations are tolerant of missing identity,
    invalid argument JSON, and late fragments; those conditions become issue
    codes instead of executable-tool protocol failures.
    """

    def __init__(
        self,
        *,
        max_calls: int = DEFAULT_MAX_TOOL_CALLS,
        max_events: int = DEFAULT_MAX_TOOL_STREAM_EVENTS,
        max_chars_per_call: int = DEFAULT_MAX_TOOL_ARGUMENT_CHARS,
        max_total_chars: int = DEFAULT_MAX_TOTAL_TOOL_ARGUMENT_CHARS,
        execution_name_limit: int = DEFAULT_MAX_TOOL_NAME_CHARS,
    ) -> None:
        if min(
            max_calls,
            max_events,
            max_chars_per_call,
            max_total_chars,
            execution_name_limit,
        ) <= 0:
            raise ValueError("candidate artifact limits must be positive")
        self._max_calls = max_calls
        self._max_events = max_events
        self._max_chars_per_call = max_chars_per_call
        self._max_total_chars = max_total_chars
        self._execution_name_limit = execution_name_limit
        self._calls: dict[Any, _CandidateAction] = {}
        self._event_count = 0
        self._char_count = 0

    def start(self, key: Any, *, name_text: object | None = None) -> None:
        """Start or revisit a keyed action and optionally append a name fragment."""
        name, issues = self._coerce_name(name_text)
        self._mutate(
            key,
            operation="start",
            name_fragment=name,
            issues=issues,
        )

    def append_or_start(
        self,
        key: Any,
        *,
        name_fragment: object | None = None,
        arguments_fragment: object | None = None,
    ) -> None:
        """Append raw fragments to a keyed action, creating it when necessary."""
        name, name_issues = self._coerce_name(name_fragment)
        arguments, argument_issues = self._coerce_arguments(arguments_fragment)
        self._mutate(
            key,
            operation="append_or_start",
            name_fragment=name,
            arguments_fragment=arguments,
            issues=name_issues | argument_issues,
        )

    def append_name(self, key: Any, fragment: object | None) -> None:
        """Append one provider-native name fragment."""
        name, issues = self._coerce_name(fragment)
        self._mutate(
            key,
            operation="append_name",
            name_fragment=name,
            issues=issues,
        )

    def append_arguments(self, key: Any, fragment: object | None) -> None:
        """Append one provider-native arguments fragment."""
        arguments, issues = self._coerce_arguments(fragment)
        self._mutate(
            key,
            operation="append_arguments",
            arguments_fragment=arguments,
            issues=issues,
        )

    def finish(self, key: Any) -> None:
        """Mark a keyed action complete without parsing it into executable args."""
        self._mutate(key, operation="finish", finish=True)

    def observe_call(
        self,
        key: Any,
        *,
        name_text: object | None = None,
        arguments: object | None = None,
    ) -> None:
        """Record one whole native call as a single bounded observation."""
        name, name_issues = self._coerce_name(name_text)
        arguments_text, argument_issues = self._coerce_arguments(arguments)
        self._mutate(
            key,
            operation="observe_call",
            name_fragment=name,
            arguments_fragment=arguments_text,
            issues=name_issues | argument_issues,
            finish=True,
        )

    def render_text(self) -> str:
        """Return deterministic host-generated JSON, or ``""`` when empty."""
        actions: list[dict[str, object]] = []
        for action in self._calls.values():
            if not action.is_substantive:
                continue
            actions.append(
                {
                    "arguments_text": action.arguments_text,
                    "issues": self._issues_for(action),
                    "name_text": action.name_text,
                }
            )
        if not actions:
            return ""
        payload = {
            "actions": actions,
            "executable": False,
            "kind": "inert_proposer_tool_output",
        }
        encoder = json.JSONEncoder(
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        # The resource contract bounds the actual artifact delivered to the
        # consumer, including JSON escaping and host-generated structure.  A
        # decoded string of control characters can expand up to sixfold when
        # encoded, so checking only retained provider fragments is insufficient.
        parts = ["\n"]
        rendered_chars = 1
        for part in encoder.iterencode(payload):
            projected_chars = rendered_chars + len(part)
            if projected_chars > self._max_total_chars:
                self._raise_limit(
                    "render",
                    "artifact",
                    "total_chars_exceeded",
                    self._max_total_chars,
                    projected_chars,
                )
            parts.append(part)
            rendered_chars = projected_chars
        return "".join(parts)

    def render(self) -> str:
        """Backward-compatible short alias for :meth:`render_text`."""
        return self.render_text()

    @property
    def has_calls(self) -> bool:
        return bool(self._calls)

    @property
    def has_content(self) -> bool:
        return any(action.is_substantive for action in self._calls.values())

    @property
    def call_count(self) -> int:
        return len(self._calls)

    @property
    def event_count(self) -> int:
        return self._event_count

    @property
    def char_count(self) -> int:
        return self._char_count

    @property
    def issue_codes(self) -> tuple[str, ...]:
        issues = {
            issue
            for action in self._calls.values()
            if action.is_substantive
            for issue in self._issues_for(action)
        }
        return tuple(sorted(issues))

    def _mutate(
        self,
        key: Any,
        *,
        operation: str,
        name_fragment: str = "",
        arguments_fragment: str = "",
        issues: set[str] | None = None,
        finish: bool = False,
    ) -> None:
        existing = self._calls.get(key)
        new_chars = len(name_fragment) + len(arguments_fragment)
        projected_events = self._event_count + 1
        if projected_events > self._max_events:
            self._raise_limit(
                operation,
                key,
                "too_many_events",
                self._max_events,
                projected_events,
            )
        if existing is None and len(self._calls) + 1 > self._max_calls:
            self._raise_limit(
                operation,
                key,
                "too_many_calls",
                self._max_calls,
                len(self._calls) + 1,
            )
        projected_call_chars = (existing.char_count if existing is not None else 0) + new_chars
        if projected_call_chars > self._max_chars_per_call:
            self._raise_limit(
                operation,
                key,
                "call_chars_exceeded",
                self._max_chars_per_call,
                projected_call_chars,
            )
        projected_total_chars = self._char_count + new_chars
        if projected_total_chars > self._max_total_chars:
            self._raise_limit(
                operation,
                key,
                "total_chars_exceeded",
                self._max_total_chars,
                projected_total_chars,
            )

        self._event_count = projected_events
        action = existing
        if action is None:
            action = _CandidateAction()
            self._calls[key] = action
        elif action.finished and (name_fragment or arguments_fragment):
            action.issues.add("late_mutation")
        if name_fragment:
            action.name_parts.append(name_fragment)
        if arguments_fragment:
            action.argument_parts.append(arguments_fragment)
        if issues:
            action.issues.update(issues)
        action.char_count = projected_call_chars
        self._char_count = projected_total_chars
        if finish:
            action.finished = True

    def _issues_for(self, action: _CandidateAction) -> list[str]:
        issues = set(action.issues)
        name = action.name_text
        arguments = action.arguments_text
        if not name.strip():
            issues.add("missing_name")
        elif len(name) > self._execution_name_limit:
            issues.add("name_over_execution_limit")
        if arguments.strip():
            try:
                parsed = json.loads(arguments, parse_constant=self._reject_nonfinite)
            except (RecursionError, TypeError, ValueError, json.JSONDecodeError):
                issues.add("invalid_arguments_json")
            else:
                if not isinstance(parsed, dict):
                    issues.add("non_object_arguments")
        if not action.finished:
            issues.add("incomplete_call")
        return sorted(issues)

    def _coerce_name(self, value: object | None) -> tuple[str, set[str]]:
        if value is None:
            return "", set()
        if isinstance(value, str):
            return value, set()
        return self._coerce_json_text(value), {"invalid_name_type"}

    def _coerce_arguments(self, value: object | None) -> tuple[str, set[str]]:
        if value is None:
            return "", set()
        if isinstance(value, str):
            return value, set()
        return self._coerce_json_text(value), set()

    def _coerce_json_text(self, value: object) -> str:
        # ``iterencode`` lets us stop retaining output once the strictest
        # artifact character limit is crossed.  The builder's mutation path
        # still raises the authoritative structured limit error before any
        # partial value is committed.
        max_chars = min(self._max_chars_per_call, self._max_total_chars)
        encoder = json.JSONEncoder(
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        parts: list[str] = []
        retained = 0
        try:
            for part in encoder.iterencode(value):
                remaining = max_chars + 1 - retained
                if remaining <= 0:
                    break
                if len(part) >= remaining:
                    parts.append(part[:remaining])
                    retained += remaining
                    break
                parts.append(part)
                retained += len(part)
        except (OverflowError, RecursionError, TypeError, ValueError):
            return f"<unserializable:{type(value).__name__}>"
        return "".join(parts)

    @staticmethod
    def _reject_nonfinite(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    @staticmethod
    def _raise_limit(
        operation: str,
        key: Any,
        reason: str,
        limit: int,
        observed: int,
    ) -> None:
        raise CandidateArtifactLimitError(
            operation=operation,
            key=key,
            reason=reason,
            limit=limit,
            observed=observed,
        )


def strip_candidate_tool_identity(value: object) -> object:
    """Boundedly remove execution identities from malformed native wrappers.

    This helper is intentionally reserved for provider envelope fragments, not
    a function's actual arguments (where a field named ``id`` may be valid
    advisory data).  Provider JSON is normally shallow and acyclic; explicit
    depth/node guards keep malformed structures from escaping candidate-mode
    resource bounds before the builder serializes them.
    """

    remaining_nodes = _MAX_MALFORMED_WRAPPER_NODES
    active_containers: set[int] = set()

    def _visit(current: object, *, depth: int) -> object:
        nonlocal remaining_nodes
        if remaining_nodes <= 0:
            return "<truncated:node_limit>"
        remaining_nodes -= 1

        if isinstance(current, Mapping):
            if depth >= _MAX_MALFORMED_WRAPPER_DEPTH:
                return "<truncated:depth_limit>"
            identity = id(current)
            if identity in active_containers:
                return "<truncated:recursive_value>"
            active_containers.add(identity)
            try:
                sanitized: dict[str, object] = {}
                for key, nested in current.items():
                    if remaining_nodes <= 0:
                        sanitized["<truncated>"] = "node_limit"
                        break
                    key_text = (
                        key
                        if isinstance(key, str)
                        else f"<non_string_key:{type(key).__name__}>"
                    )
                    if key_text.casefold() in _CANDIDATE_TOOL_IDENTITY_KEYS:
                        continue
                    sanitized[key_text] = _visit(nested, depth=depth + 1)
                return sanitized
            finally:
                active_containers.discard(identity)

        if isinstance(current, (list, tuple)):
            if depth >= _MAX_MALFORMED_WRAPPER_DEPTH:
                return "<truncated:depth_limit>"
            identity = id(current)
            if identity in active_containers:
                return "<truncated:recursive_value>"
            active_containers.add(identity)
            try:
                sanitized_items: list[object] = []
                for nested in current:
                    if remaining_nodes <= 0:
                        sanitized_items.append("<truncated:node_limit>")
                        break
                    sanitized_items.append(_visit(nested, depth=depth + 1))
                return sanitized_items
            finally:
                active_containers.discard(identity)

        if current is None or isinstance(current, (str, int, float, bool)):
            return current
        return f"<unserializable:{type(current).__name__}>"

    return _visit(value, depth=0)
