"""Shared streaming-assembly helpers for provider adapters.

Each provider keeps its own transport loop and raw-format detection (SSE vs
JSONL vs whole-response; differing field names). What they had all duplicated
is the *semantic* bookkeeping inside that loop: buffering reasoning fragments
into ``DoneEvent.reasoning_content``, and assembling streamed tool-call
fragments into the ToolUseStart/Delta/End lifecycle. That logic lives here
once.

The accumulators are deliberately format-agnostic: a provider feeds them the
fragments it has already extracted from its own wire format and yields the
events they return.  They enforce one Start per call and a stable
``tool_use_id`` across Start/Delta/End.  Adapters may emit End only after their
wire protocol supplies successful terminal evidence; a truncated or abnormal
stream must retain the call as unclosed diagnostic state and return Error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from opensquilla.provider.types import (
    ReasoningDeltaEvent,
    StreamEvent,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)


class ReasoningAccumulator:
    """Buffers reasoning fragments and emits them as streaming deltas.

    Usage in a provider loop::

        racc = ReasoningAccumulator()
        ...
        if fragment := extract_reasoning(chunk):
            event = racc.emit(fragment)
            if event is not None:
                yield event
        ...
        done = DoneEvent(reasoning_content=racc.finalize(), ...)
    """

    __slots__ = ("_parts",)

    def __init__(self) -> None:
        self._parts: list[str] = []

    def emit(self, fragment: str | None) -> ReasoningDeltaEvent | None:
        """Buffer a reasoning fragment; return an event to yield, or None.

        Empty/None fragments are ignored (no event), matching the providers'
        prior behavior of skipping empty reasoning chunks.
        """
        if not fragment:
            return None
        self._parts.append(fragment)
        return ReasoningDeltaEvent(text=fragment)

    def finalize(self) -> str | None:
        """Return the joined reasoning text, or None if nothing was buffered.

        ``None`` (not ``""``) when empty preserves the existing
        ``DoneEvent.reasoning_content`` contract, where absence of reasoning is
        represented as ``None``.
        """
        if not self._parts:
            return None
        return "".join(self._parts)

    @property
    def has_content(self) -> bool:
        return bool(self._parts)


class ToolStreamProtocolError(ValueError):
    """A provider violated one tool call's streamed lifecycle contract."""

    def __init__(
        self,
        *,
        operation: str,
        key: Any,
        tool_use_id: str,
        reason: str = "closed_mutation",
        limit: int | None = None,
        observed: int | None = None,
    ) -> None:
        messages = {
            "closed_mutation": "closed tool call received a late mutation",
            "conflicting_tool_use_id": "tool call key received a conflicting id",
            "conflicting_tool_name": "tool call key received a conflicting name",
            "duplicate_tool_use_id": "different tool call keys reused one public id",
            "invalid_tool_use_id": "tool call id must be a string",
            "invalid_tool_name": "tool name must be a string",
            "tool_use_id_too_large": "tool call id exceeded the identity limit",
            "tool_name_too_large": "tool name exceeded the identity limit",
            "invalid_arguments_fragment": "tool argument fragment must be a string",
            "invalid_tool_arguments": "tool arguments must be a finite JSON object",
            "unknown_tool_call": "tool lifecycle event referenced an unknown call",
            "incomplete_tool_identity": "tool call ended before its identity was complete",
            "too_many_tool_calls": "provider response exceeded the tool call limit",
            "tool_arguments_too_large": "one tool call exceeded the argument limit",
            "total_tool_arguments_too_large": (
                "provider response exceeded the aggregate tool argument limit"
            ),
            "too_many_tool_events": "provider response exceeded the tool event limit",
        }
        super().__init__(messages.get(reason, "invalid streamed tool call lifecycle"))
        self.operation = operation
        self.key = key
        self.tool_use_id = tool_use_id
        self.reason = reason
        self.limit = limit
        self.observed = observed


# These are response-local safety bounds, not provider/request token budgets.
# They keep a hostile or broken stream from retaining an unbounded number of
# calls or one-character argument deltas before terminal validation.  The
# constructor accepts narrower limits for deterministic adapter tests.
DEFAULT_MAX_TOOL_CALLS = 256
DEFAULT_MAX_TOOL_USE_ID_CHARS = 1_024
DEFAULT_MAX_TOOL_NAME_CHARS = 256
DEFAULT_MAX_TOOL_ARGUMENT_CHARS = 256_000
DEFAULT_MAX_TOTAL_TOOL_ARGUMENT_CHARS = 1_024_000
# Token-sized provider deltas are commonly only a few characters.  Keep the
# event bound high enough that the 256k per-call character cap remains the
# practical limit for ordinary streamed JSON, while still bounding list/event
# overhead for a pathological one-character-per-frame response.
DEFAULT_MAX_TOOL_STREAM_EVENTS = 65_536


@dataclass
class _PendingToolCall:
    tool_use_id: str
    tool_name: str
    wire_id: str | None = None
    json_parts: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    start_emitted: bool = False
    argument_chars: int = 0


class ToolStreamAccumulator:
    """Assembles streamed tool-call fragments into Start/Delta/End events.

    Calls are keyed by the provider's *stream-local* identifier — the
    ``tool_calls[].index`` int for OpenAI Chat, the content-block index for
    Anthropic, the output-item id string for the Responses API. The key is
    distinct from the public ``tool_use_id``: the public id is frozen the
    moment ``ToolUseStartEvent`` is emitted, so Start/Delta/End always agree
    even when the upstream reveals its real id only in a later chunk (the
    late id is retained as ``wire_id`` for key matching only).

    The three provider grammars map onto the operations:

    - identity-first (Anthropic ``content_block_start``): ``start`` then
      ``append`` then adapter validation and ``finish_with_arguments``.
    - identity-on-first-delta (OpenAI Chat): ``append_or_start`` per chunk,
      then adapter validation and ``finish_with_arguments`` at the successful
      response terminal (Chat has no per-call stop event).
    - whole-call (Ollama, Responses, non-stream fallbacks): ``start`` +
      ``append`` + ``finish_with_arguments``.

    Closing always requires an adapter-supplied canonical argument object.
    The accumulator deliberately has no parse-and-close shortcut: transport
    completion alone must never promote malformed fragments into an executable
    ``ToolUseEndEvent``.
    """

    def __init__(
        self,
        *,
        max_calls: int = DEFAULT_MAX_TOOL_CALLS,
        max_tool_use_id_chars: int = DEFAULT_MAX_TOOL_USE_ID_CHARS,
        max_tool_name_chars: int = DEFAULT_MAX_TOOL_NAME_CHARS,
        max_argument_chars: int = DEFAULT_MAX_TOOL_ARGUMENT_CHARS,
        max_total_argument_chars: int = DEFAULT_MAX_TOTAL_TOOL_ARGUMENT_CHARS,
        max_events: int = DEFAULT_MAX_TOOL_STREAM_EVENTS,
    ) -> None:
        if min(
            max_calls,
            max_tool_use_id_chars,
            max_tool_name_chars,
            max_argument_chars,
            max_total_argument_chars,
            max_events,
        ) < 1:
            raise ValueError("tool stream limits must be positive")
        self._calls: dict[Any, _PendingToolCall] = {}
        self._closed: set[Any] = set()
        # Public ids and late provider wire ids share one response-local
        # namespace: either can be used to resolve later index-less deltas, so
        # allowing cross-namespace reuse would make routing ambiguous.
        self._identity_keys: dict[str, Any] = {}
        self._next_int_key = 0
        self._total_argument_chars = 0
        self._event_count = 0
        self._pending_unemitted_events = 0
        self._pending_unemitted_chars = 0
        self._max_calls = max_calls
        self._max_tool_use_id_chars = max_tool_use_id_chars
        self._max_tool_name_chars = max_tool_name_chars
        self._max_argument_chars = max_argument_chars
        self._max_total_argument_chars = max_total_argument_chars
        self._max_events = max_events

    # -- queries ----------------------------------------------------------

    @property
    def has_calls(self) -> bool:
        """True once any tool call was started (open or closed)."""
        return bool(self._calls)

    def has_key(self, key: Any) -> bool:
        """Return whether a stream-local tool slot has already been opened."""

        return key in self._calls

    def find_key_for_tool_call_id(self, tool_call_id: str) -> Any | None:
        """Return the key of the call matching a provider tool-call id."""
        return self._identity_keys.get(tool_call_id)

    def single_key(self) -> Any | None:
        """Return the only key when exactly one call is being assembled."""
        if len(self._calls) == 1:
            return next(iter(self._calls))
        return None

    def next_int_key(self) -> int:
        """Allocate a monotonically increasing key for index-less calls.

        Allocation is O(1); scanning all prior keys for every call made a
        response containing many index-less calls quadratic.
        """

        key = self._next_int_key
        self._next_int_key += 1
        return key

    def first_metadata(self, name: str) -> Any | None:
        """Return the first call's metadata value for ``name``, if any."""
        for call in self._calls.values():
            value = call.metadata.get(name)
            if value is not None:
                return value
        return None

    @property
    def pending_unemitted_event_count(self) -> int:
        """Logical Start-plus-delta events held behind incomplete identity."""

        return self._pending_unemitted_events

    @property
    def pending_unemitted_char_count(self) -> int:
        """Characters retained for events held behind incomplete identity."""

        return self._pending_unemitted_chars

    def pending_raw_arguments(self) -> list[tuple[Any, str, str, str]]:
        """Return ``(key, tool_use_id, tool_name, raw_argument_text)`` per open call.

        Providers that post-process argument JSON before closing (e.g.
        dialect-specific repair of malformed fragments) read the accumulated
        raw text here and close each call via ``finish_with_arguments``,
        instead of reaching into private state.
        """
        return [
            (key, call.tool_use_id, call.tool_name, "".join(call.json_parts))
            for key, call in self._calls.items()
            if key not in self._closed
        ]

    def _reject_closed_mutation(self, key: Any, *, operation: str) -> None:
        if key not in self._closed:
            return
        call = self._calls.get(key)
        raise ToolStreamProtocolError(
            operation=operation,
            key=key,
            tool_use_id=call.tool_use_id if call is not None else "",
        )

    @staticmethod
    def _has_identity_value(value: object) -> bool:
        return isinstance(value, str) and bool(value.strip())

    def _raise_limit(
        self,
        *,
        operation: str,
        key: Any,
        tool_use_id: str,
        reason: str,
        limit: int,
        observed: int,
    ) -> None:
        raise ToolStreamProtocolError(
            operation=operation,
            key=key,
            tool_use_id=tool_use_id,
            reason=reason,
            limit=limit,
            observed=observed,
        )

    def _reserve_event(
        self,
        *,
        operation: str,
        key: Any,
        tool_use_id: str,
    ) -> None:
        observed = self._event_count + 1
        if observed > self._max_events:
            self._raise_limit(
                operation=operation,
                key=key,
                tool_use_id=tool_use_id,
                reason="too_many_tool_events",
                limit=self._max_events,
                observed=observed,
            )
        self._event_count = observed

    def _reserve_identity(
        self,
        *,
        operation: str,
        key: Any,
        tool_use_id: str,
    ) -> None:
        if len(tool_use_id) > self._max_tool_use_id_chars:
            self._raise_limit(
                operation=operation,
                key=key,
                tool_use_id="",
                reason="tool_use_id_too_large",
                limit=self._max_tool_use_id_chars,
                observed=len(tool_use_id),
            )
        if not self._has_identity_value(tool_use_id):
            return
        sentinel = object()
        existing_key = self._identity_keys.get(tool_use_id, sentinel)
        if existing_key is not sentinel and existing_key != key:
            raise ToolStreamProtocolError(
                operation=operation,
                key=key,
                tool_use_id=tool_use_id,
                reason="duplicate_tool_use_id",
            )
        self._identity_keys[tool_use_id] = key

    def _register_key(self, key: Any) -> None:
        if type(key) is int and key >= self._next_int_key:
            self._next_int_key = key + 1

    def _validate_tool_name(
        self,
        *,
        operation: str,
        key: Any,
        tool_use_id: str,
        tool_name: str,
    ) -> None:
        if len(tool_name) > self._max_tool_name_chars:
            self._raise_limit(
                operation=operation,
                key=key,
                tool_use_id=tool_use_id,
                reason="tool_name_too_large",
                limit=self._max_tool_name_chars,
                observed=len(tool_name),
            )

    def _append_fragment(
        self,
        *,
        operation: str,
        key: Any,
        call: _PendingToolCall,
        fragment: str,
    ) -> None:
        if not isinstance(fragment, str):
            raise ToolStreamProtocolError(
                operation=operation,
                key=key,
                tool_use_id=call.tool_use_id,
                reason="invalid_arguments_fragment",
            )
        per_call_observed = call.argument_chars + len(fragment)
        if per_call_observed > self._max_argument_chars:
            self._raise_limit(
                operation=operation,
                key=key,
                tool_use_id=call.tool_use_id,
                reason="tool_arguments_too_large",
                limit=self._max_argument_chars,
                observed=per_call_observed,
            )
        total_observed = self._total_argument_chars + len(fragment)
        if total_observed > self._max_total_argument_chars:
            self._raise_limit(
                operation=operation,
                key=key,
                tool_use_id=call.tool_use_id,
                reason="total_tool_arguments_too_large",
                limit=self._max_total_argument_chars,
                observed=total_observed,
            )
        self._reserve_event(
            operation=operation,
            key=key,
            tool_use_id=call.tool_use_id,
        )
        call.json_parts.append(fragment)
        call.argument_chars = per_call_observed
        self._total_argument_chars = total_observed

    def _raise_identity_conflict(
        self,
        *,
        operation: str,
        key: Any,
        call: _PendingToolCall,
        reason: str,
    ) -> None:
        raise ToolStreamProtocolError(
            operation=operation,
            key=key,
            tool_use_id=call.tool_use_id,
            reason=reason,
        )

    def _merge_tool_name(
        self,
        *,
        operation: str,
        key: Any,
        call: _PendingToolCall,
        tool_name: str,
    ) -> None:
        if not self._has_identity_value(tool_name):
            return
        if self._has_identity_value(call.tool_name) and call.tool_name != tool_name:
            self._raise_identity_conflict(
                operation=operation,
                key=key,
                call=call,
                reason="conflicting_tool_name",
            )
        if not self._has_identity_value(call.tool_name):
            if not call.start_emitted:
                self._pending_unemitted_chars += len(tool_name) - len(call.tool_name)
            call.tool_name = tool_name

    def _register_pending_call(self, call: _PendingToolCall) -> None:
        self._pending_unemitted_events += 1 + len(call.json_parts)
        self._pending_unemitted_chars += (
            len(call.tool_use_id)
            + len(call.tool_name)
            + sum(len(fragment) for fragment in call.json_parts)
        )

    def _emit_start_if_ready(self, call: _PendingToolCall) -> list[StreamEvent]:
        """Emit one identity-complete Start, followed by any held deltas."""

        if (
            call.start_emitted
            or not self._has_identity_value(call.tool_use_id)
            or not self._has_identity_value(call.tool_name)
        ):
            return []
        self._pending_unemitted_events -= 1 + len(call.json_parts)
        self._pending_unemitted_chars -= (
            len(call.tool_use_id)
            + len(call.tool_name)
            + sum(len(fragment) for fragment in call.json_parts)
        )
        call.start_emitted = True
        events: list[StreamEvent] = [
            ToolUseStartEvent(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
            )
        ]
        events.extend(
            ToolUseDeltaEvent(
                tool_use_id=call.tool_use_id,
                json_fragment=fragment,
            )
            for fragment in call.json_parts
        )
        return events

    # -- lifecycle --------------------------------------------------------

    def start(
        self,
        key: Any,
        *,
        tool_use_id: str,
        tool_name: str,
    ) -> list[StreamEvent]:
        """Open a call whose identity arrived before any argument delta."""
        self._reject_closed_mutation(key, operation="start")
        if not isinstance(tool_use_id, str):
            raise ToolStreamProtocolError(
                operation="start",
                key=key,
                tool_use_id="",
                reason="invalid_tool_use_id",
            )
        if not isinstance(tool_name, str):
            raise ToolStreamProtocolError(
                operation="start",
                key=key,
                tool_use_id=tool_use_id,
                reason="invalid_tool_name",
            )
        self._validate_tool_name(
            operation="start",
            key=key,
            tool_use_id=tool_use_id,
            tool_name=tool_name,
        )
        call = self._calls.get(key)
        if call is None:
            observed_calls = len(self._calls) + 1
            if observed_calls > self._max_calls:
                self._raise_limit(
                    operation="start",
                    key=key,
                    tool_use_id=tool_use_id,
                    reason="too_many_tool_calls",
                    limit=self._max_calls,
                    observed=observed_calls,
                )
            self._reserve_identity(
                operation="start",
                key=key,
                tool_use_id=tool_use_id,
            )
            self._reserve_event(
                operation="start",
                key=key,
                tool_use_id=tool_use_id,
            )
            call = _PendingToolCall(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                wire_id=tool_use_id or None,
            )
            self._calls[key] = call
            self._register_key(key)
            self._register_pending_call(call)
        else:
            if self._has_identity_value(tool_use_id):
                if (
                    self._has_identity_value(call.tool_use_id)
                    and call.tool_use_id != tool_use_id
                ):
                    self._raise_identity_conflict(
                        operation="start",
                        key=key,
                        call=call,
                        reason="conflicting_tool_use_id",
                    )
                if not self._has_identity_value(call.tool_use_id):
                    self._reserve_identity(
                        operation="start",
                        key=key,
                        tool_use_id=tool_use_id,
                    )
                    if not call.start_emitted:
                        self._pending_unemitted_chars += len(tool_use_id) - len(
                            call.tool_use_id
                        )
                    call.tool_use_id = tool_use_id
                    call.wire_id = tool_use_id
            self._merge_tool_name(
                operation="start",
                key=key,
                call=call,
                tool_name=tool_name,
            )
        return self._emit_start_if_ready(call)

    def append_or_start(
        self,
        key: Any,
        *,
        tool_call_id: str | None = None,
        tool_name: str = "",
        fragment: str = "",
    ) -> list[StreamEvent]:
        """Feed one OpenAI-Chat-style delta where identity may arrive late.

        Creates the call on first sight (synthesizing a public id when the
        chunk carries none). A missing name holds Start and any argument
        deltas until a later chunk completes the identity. Later chunks may
        fill ``wire_id`` and an absent name, but conflicting nonempty identity
        is a protocol error; the already-selected public id never changes.
        """
        self._reject_closed_mutation(key, operation="append_or_start")
        if tool_call_id is not None and not isinstance(tool_call_id, str):
            raise ToolStreamProtocolError(
                operation="append_or_start",
                key=key,
                tool_use_id="",
                reason="invalid_tool_use_id",
            )
        if not isinstance(tool_name, str):
            raise ToolStreamProtocolError(
                operation="append_or_start",
                key=key,
                tool_use_id=tool_call_id or "",
                reason="invalid_tool_name",
            )
        self._validate_tool_name(
            operation="append_or_start",
            key=key,
            tool_use_id=tool_call_id or "",
            tool_name=tool_name,
        )
        events: list[StreamEvent] = []
        call = self._calls.get(key)
        if call is None:
            observed_calls = len(self._calls) + 1
            if observed_calls > self._max_calls:
                self._raise_limit(
                    operation="append_or_start",
                    key=key,
                    tool_use_id=tool_call_id or "",
                    reason="too_many_tool_calls",
                    limit=self._max_calls,
                    observed=observed_calls,
                )
            public_id = tool_call_id or f"call_{uuid4().hex[:12]}"
            self._reserve_identity(
                operation="append_or_start",
                key=key,
                tool_use_id=public_id,
            )
            self._reserve_event(
                operation="append_or_start",
                key=key,
                tool_use_id=public_id,
            )
            call = _PendingToolCall(
                tool_use_id=public_id,
                tool_name=tool_name,
                wire_id=tool_call_id,
            )
            self._calls[key] = call
            self._register_key(key)
            self._register_pending_call(call)
        else:
            if tool_call_id:
                if call.wire_id and call.wire_id != tool_call_id:
                    self._raise_identity_conflict(
                        operation="append_or_start",
                        key=key,
                        call=call,
                        reason="conflicting_tool_use_id",
                    )
                self._reserve_identity(
                    operation="append_or_start",
                    key=key,
                    tool_use_id=tool_call_id,
                )
                call.wire_id = tool_call_id
            self._merge_tool_name(
                operation="append_or_start",
                key=key,
                call=call,
                tool_name=tool_name,
            )
        events.extend(self._emit_start_if_ready(call))
        if fragment:
            self._append_fragment(
                operation="append_or_start",
                key=key,
                call=call,
                fragment=fragment,
            )
            if call.start_emitted:
                events.append(
                    ToolUseDeltaEvent(
                        tool_use_id=call.tool_use_id,
                        json_fragment=fragment,
                    )
                )
            else:
                self._pending_unemitted_events += 1
                self._pending_unemitted_chars += len(fragment)
        return events

    def append(self, key: Any, fragment: str) -> list[StreamEvent]:
        """Append an argument fragment to an already-started call.

        Unknown keys are a protocol error. Silently dropping a fragment can
        otherwise turn a malformed stream into a successful empty response.
        """
        self._reject_closed_mutation(key, operation="append")
        call = self._calls.get(key)
        if call is None:
            raise ToolStreamProtocolError(
                operation="append",
                key=key,
                tool_use_id="",
                reason="unknown_tool_call",
            )
        self._append_fragment(
            operation="append",
            key=key,
            call=call,
            fragment=fragment,
        )
        if not call.start_emitted:
            self._pending_unemitted_events += 1
            self._pending_unemitted_chars += len(fragment)
            return []
        return [ToolUseDeltaEvent(tool_use_id=call.tool_use_id, json_fragment=fragment)]

    def set_metadata(self, key: Any, name: str, value: Any) -> None:
        """Attach provider-opaque per-call state (e.g. thought signatures)."""
        self._reject_closed_mutation(key, operation="set_metadata")
        call = self._calls.get(key)
        if call is None:
            raise ToolStreamProtocolError(
                operation="set_metadata",
                key=key,
                tool_use_id="",
                reason="unknown_tool_call",
            )
        call.metadata[name] = value

    def finish_with_arguments(self, key: Any, arguments: dict[str, Any]) -> list[StreamEvent]:
        """Close one call with authoritative already-parsed arguments."""
        call = self._calls.get(key)
        if call is None:
            raise ToolStreamProtocolError(
                operation="finish_with_arguments",
                key=key,
                tool_use_id="",
                reason="unknown_tool_call",
            )
        if key in self._closed:
            return []
        if not call.start_emitted:
            raise ToolStreamProtocolError(
                operation="finish_with_arguments",
                key=key,
                tool_use_id=call.tool_use_id,
                reason="incomplete_tool_identity",
            )
        if not isinstance(arguments, dict):
            raise ToolStreamProtocolError(
                operation="finish_with_arguments",
                key=key,
                tool_use_id=call.tool_use_id,
                reason="invalid_tool_arguments",
            )
        try:
            canonical_arguments = json.dumps(
                arguments,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (RecursionError, TypeError, ValueError) as exc:
            raise ToolStreamProtocolError(
                operation="finish_with_arguments",
                key=key,
                tool_use_id=call.tool_use_id,
                reason="invalid_tool_arguments",
            ) from exc
        effective_chars = max(call.argument_chars, len(canonical_arguments))
        if effective_chars > self._max_argument_chars:
            self._raise_limit(
                operation="finish_with_arguments",
                key=key,
                tool_use_id=call.tool_use_id,
                reason="tool_arguments_too_large",
                limit=self._max_argument_chars,
                observed=effective_chars,
            )
        added_chars = effective_chars - call.argument_chars
        total_observed = self._total_argument_chars + added_chars
        if total_observed > self._max_total_argument_chars:
            self._raise_limit(
                operation="finish_with_arguments",
                key=key,
                tool_use_id=call.tool_use_id,
                reason="total_tool_arguments_too_large",
                limit=self._max_total_argument_chars,
                observed=total_observed,
            )
        self._reserve_event(
            operation="finish_with_arguments",
            key=key,
            tool_use_id=call.tool_use_id,
        )
        call.argument_chars = effective_chars
        self._total_argument_chars = total_observed
        self._closed.add(key)
        return [
            ToolUseEndEvent(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                arguments=arguments,
            )
        ]
