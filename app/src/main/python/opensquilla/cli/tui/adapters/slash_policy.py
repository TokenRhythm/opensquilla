"""Registry-backed slash-command scheduling for the concurrent TUI runtime.

Command meaning belongs to :mod:`opensquilla.engine.commands`.  This adapter
projects the canonical busy/presentation metadata into the small set of input
kinds understood by the TUI loop:

* turn presentations keep the ordinary prompt/steer/queue contract;
* immediate and next-turn control commands dispatch out of band;
* query/navigation commands dispatch through the command plane without a
  prompt echo;
* require-idle commands are explicitly rejected by the runtime while busy;
* abort-and-run and drain-and-exit retain their existing lifecycle semantics.

Unknown slash input is also command-plane input.  Dispatching it immediately
lets the canonical handler report ``Unknown command`` without first rendering
or queueing it as a user message.
"""

from __future__ import annotations

from enum import Enum

from opensquilla.engine.commands import (
    DEFAULT_REGISTRY,
    CommandBusyPolicy,
    CommandCategory,
    CommandPresentation,
    Surface,
)


class SlashCategory(Enum):
    """Runtime routing category for one submitted composer value."""

    DESTRUCTIVE = "destructive"
    EXIT = "exit"
    NON_SLASH = "non_slash"
    LOCAL = "local"
    CONTROL = "control"
    COMMAND = "command"
    REQUIRE_IDLE = "require_idle"
    TURN = "turn"

    # Compatibility aliases for callers that imported the former hand-written
    # taxonomy. Both categories now mean deterministic command-plane input.
    PURE_INFO = "command"
    STATE_MUTATION = "command"


_TUI_COMMANDS = DEFAULT_REGISTRY.for_surface(Surface.CLI_GATEWAY)


def _words_for(*, busy_policy: CommandBusyPolicy | None = None) -> frozenset[str]:
    return frozenset(
        word
        for command in _TUI_COMMANDS
        if busy_policy is None or command.busy_policy is busy_policy
        for word in command.words()
    )


# Deprecated compatibility projections. Their contents are derived from the
# canonical registry rather than maintained as a second semantic source.
DESTRUCTIVE_SLASH_WORDS = _words_for(busy_policy=CommandBusyPolicy.ABORT_AND_RUN)
EXIT_SLASH_WORDS = _words_for(busy_policy=CommandBusyPolicy.DRAIN_AND_EXIT)
REQUIRE_IDLE_SLASH_WORDS = _words_for(busy_policy=CommandBusyPolicy.REQUIRE_IDLE)
TURN_SLASH_WORDS = frozenset(
    word
    for command in _TUI_COMMANDS
    if command.presentation is CommandPresentation.TURN
    for word in command.words()
)


def _execution_action(command: object, surface: Surface = Surface.CLI_GATEWAY) -> str | None:
    execution_for = getattr(command, "execution_for", None)
    if not callable(execution_for):
        return None
    execution = execution_for(surface)
    return getattr(execution, "action", None)


# /theme is the one host-only command preserved as LOCAL for compatibility.
# The projection keys off its canonical action rather than a second word list.
LOCAL_SLASH_WORDS = frozenset(
    word
    for command in _TUI_COMMANDS
    if _execution_action(command) == "theme.set"
    for word in command.words()
)
CONTROL_SLASH_WORDS = frozenset(
    word
    for command in _TUI_COMMANDS
    if command.category is CommandCategory.CONTROL
    and command.presentation is not CommandPresentation.TURN
    and command.busy_policy
    in {CommandBusyPolicy.IMMEDIATE, CommandBusyPolicy.NEXT_TURN}
    and _execution_action(command) != "theme.set"
    for word in command.words()
)
PURE_INFO_SLASH_WORDS = frozenset(
    word
    for command in _TUI_COMMANDS
    if command.category is CommandCategory.QUERY
    and command.presentation is not CommandPresentation.TURN
    for word in command.words()
)
STATE_MUTATION_SLASH_WORDS = frozenset(
    word
    for command in _TUI_COMMANDS
    for word in command.words()
    if command.presentation is not CommandPresentation.TURN
    and command.category is not CommandCategory.QUERY
    and word not in DESTRUCTIVE_SLASH_WORDS
    and word not in EXIT_SLASH_WORDS
    and word not in LOCAL_SLASH_WORDS
)


def _head_word(input_text: str) -> str:
    stripped = input_text.strip()
    if not stripped:
        return ""
    return stripped.split(maxsplit=1)[0].lower()


def classify(
    input_text: str,
    *,
    surface: Surface = Surface.CLI_GATEWAY,
) -> SlashCategory:
    """Project canonical command metadata into a TUI scheduling category."""
    stripped = input_text.strip()
    head = _head_word(input_text)
    if not head or not head.startswith("/"):
        return SlashCategory.NON_SLASH

    command = DEFAULT_REGISTRY.find(head, surface)
    if command is None:
        return SlashCategory.COMMAND

    surface_words = command.words()

    # The handlers accept only the exact bare destructive/exit spelling. Keep
    # lifecycle effects case-sensitive and exact so malformed input can report
    # an error without cancelling work or terminating the session.
    if (
        command.busy_policy is CommandBusyPolicy.ABORT_AND_RUN
        and stripped in surface_words
    ):
        return SlashCategory.DESTRUCTIVE
    if (
        command.busy_policy is CommandBusyPolicy.DRAIN_AND_EXIT
        and stripped in surface_words
    ):
        return SlashCategory.EXIT

    if command.presentation is CommandPresentation.TURN:
        return SlashCategory.TURN
    if _execution_action(command, surface) == "theme.set":
        return SlashCategory.LOCAL
    if command.busy_policy is CommandBusyPolicy.REQUIRE_IDLE:
        return SlashCategory.REQUIRE_IDLE
    if command.category is CommandCategory.CONTROL:
        return SlashCategory.CONTROL
    return SlashCategory.COMMAND


__all__ = [
    "CONTROL_SLASH_WORDS",
    "DESTRUCTIVE_SLASH_WORDS",
    "EXIT_SLASH_WORDS",
    "LOCAL_SLASH_WORDS",
    "PURE_INFO_SLASH_WORDS",
    "REQUIRE_IDLE_SLASH_WORDS",
    "STATE_MUTATION_SLASH_WORDS",
    "TURN_SLASH_WORDS",
    "SlashCategory",
    "classify",
]
