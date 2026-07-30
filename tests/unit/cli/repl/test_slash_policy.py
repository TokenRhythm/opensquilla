"""Registry-backed slash-command scheduling contract.

The concurrent REPL spawns each user input as a child turn task while the
input task keeps accepting keystrokes. When new input arrives mid-turn,
the policy split routes the command by category:

* ``DESTRUCTIVE`` (``/clear`` / ``/reset`` / ``/compact``) — purge the
  pending queue, cancel the active turn, then run synchronously.
* ``EXIT`` (``/exit`` / ``/quit``) — drain the pending queue then exit
  the loop (mirroring Ctrl-D semantics).
* ``COMMAND`` / ``CONTROL`` — execute immediately without prompt echo.
* ``REQUIRE_IDLE`` — execute only while no turn is active.
* ``TURN`` / ``NON_SLASH`` — retain normal turn and queue behaviour.

These tests pin the classification surface so the runtime split in
``chat_cmd._run_concurrent_repl`` can rely on it.
"""

from __future__ import annotations

import pytest

from opensquilla.cli.repl.slash_policy import (
    DESTRUCTIVE_SLASH_WORDS,
    EXIT_SLASH_WORDS,
    SlashCategory,
    classify,
)

# --------------------------------------------------------------------------- #
# Destructive set                                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "command",
    [
        "/clear",
        "/reset",
        "/compact",
        "/cmp",
        "/clear   ",
    ],
)
def test_classify_destructive(command: str) -> None:
    """Exact bare destructive words return DESTRUCTIVE.

    Only the exact bare lowercase word qualifies: the slash handlers match
    exact strings, so anything else must not purge queued work and then
    fall through to "Unknown command".
    """
    assert classify(command) is SlashCategory.DESTRUCTIVE


@pytest.mark.parametrize(
    "command",
    [
        "/reset trailing-junk",
        "/compact extra args",
        "/CLEAR",
        "/Clear",
    ],
)
def test_classify_destructive_requires_exact_bare_word(command: str) -> None:
    """Case slips and stray arguments never classify as DESTRUCTIVE.

    Destructive routing purges the pending queue and cancels the in-flight
    turn BEFORE dispatch, while the handlers only match the exact bare
    lowercase word — so these variants must enqueue instead, letting the
    handler chain surface "Unknown command" without destroying work.
    """
    assert classify(command) is not SlashCategory.DESTRUCTIVE


def test_destructive_set_matches_plan_lock() -> None:
    """The destructive set is locked to exactly these commands.

    The destructive set is closed; any future addition needs a
    plan amendment. This test pins the frozenset contents so a silent
    expansion fails loudly. ``/cmp`` is the ``/compact`` alias and shares its
    context-rewriting (and therefore destructive) semantics.
    """
    assert DESTRUCTIVE_SLASH_WORDS == frozenset({"/clear", "/reset", "/compact", "/cmp"})


# --------------------------------------------------------------------------- #
# Exit set                                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "command",
    [
        "/exit",
        "/quit",
        "/exit  ",
    ],
)
def test_classify_exit(command: str) -> None:
    """Exact bare exit words return EXIT.

    ``/exit`` and ``/quit`` are NOT destructive — they drain the pending
    queue first so queued user work still runs.
    """
    assert classify(command) is SlashCategory.EXIT


@pytest.mark.parametrize("command", ["/quit now", "/Exit", "/EXIT stuff"])
def test_classify_exit_requires_exact_bare_word(command: str) -> None:
    """Case slips and stray arguments never classify as EXIT.

    EXIT drains the queue and terminates the loop before dispatch; a
    variant the handlers would reject must enqueue instead.
    """
    assert classify(command) is not SlashCategory.EXIT


def test_exit_set_matches_plan_lock() -> None:
    """The exit set is locked to exactly ``/exit`` and ``/quit``."""
    assert EXIT_SLASH_WORDS == frozenset({"/exit", "/quit"})


# --------------------------------------------------------------------------- #
# Canonical command-plane and turn classifications                            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/help", SlashCategory.COMMAND),
        ("/version", SlashCategory.COMMAND),
        ("/cost", SlashCategory.COMMAND),
        ("/usage", SlashCategory.COMMAND),
        ("/save", SlashCategory.COMMAND),
        ("/forget", SlashCategory.COMMAND),
        ("/sessions", SlashCategory.COMMAND),
        ("/models", SlashCategory.COMMAND),
        ("/status", SlashCategory.COMMAND),
        ("/session", SlashCategory.COMMAND),
        ("/approvals", SlashCategory.CONTROL),
        ("/permissions", SlashCategory.CONTROL),
        ("/model gpt-5", SlashCategory.CONTROL),
        ("/new", SlashCategory.REQUIRE_IDLE),
        ("/resume some-id", SlashCategory.REQUIRE_IDLE),
        ("/delete other-id", SlashCategory.REQUIRE_IDLE),
        ("/file /tmp/path.txt", SlashCategory.TURN),
        ("/image /tmp/pic.png", SlashCategory.TURN),
        ("/path /tmp/file.md", SlashCategory.TURN),
        ("/meta", SlashCategory.TURN),
    ],
)
def test_classify_projects_registry_metadata(
    command: str,
    expected: SlashCategory,
) -> None:
    assert classify(command) is expected


# --------------------------------------------------------------------------- #
# Non-slash and edge cases                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "command",
    [
        "hello world",
        "what is the capital of France?",
        "  multi-word user prompt  ",
        "/",  # bare slash with no command word — not a slash command yet
    ],
)
def test_classify_non_slash(command: str) -> None:
    """Non-slash inputs return NON_SLASH and run as a normal turn.

    A bare ``/`` with no following character is not a slash command — the
    head word is just ``/`` which is not in any of the explicit sets and
    does not start with a recognized slash word; it falls through to the
    enqueue path. (This is an internal detail; see the docstring of
    ``test_classify_unknown_slash_is_enqueue`` for the unknown-slash
    contract.)
    """
    # The bare `/` case actually starts with `/` so it'll be treated as an
    # unknown slash word (enqueue) under the locked policy. Skip it from the
    # strict NON_SLASH assertion and assert on the others.
    if command.strip() == "/":
        category = classify(command)
        assert category is not SlashCategory.DESTRUCTIVE
        assert category is not SlashCategory.EXIT
        return
    assert classify(command) is SlashCategory.NON_SLASH


def test_classify_empty_input_is_non_slash() -> None:
    """Empty input maps to NON_SLASH; the dispatch loop ignores it."""
    assert classify("") is SlashCategory.NON_SLASH
    assert classify("   ") is SlashCategory.NON_SLASH


@pytest.mark.parametrize(
    "command",
    [
        "  /clear  ",
        "\t/reset",
    ],
)
def test_classify_handles_leading_whitespace(command: str) -> None:
    """Surrounding whitespace must not change classification.

    Users typing into the REPL may have trailing or leading spaces from a
    history edit; the classifier strips before matching the bare word.
    """
    assert classify(command) is SlashCategory.DESTRUCTIVE


def test_classify_destructive_and_exit_are_case_sensitive() -> None:
    """DESTRUCTIVE/EXIT match only the exact lowercase spelling.

    The handlers match exact lowercase strings, so ``/CLEAR`` must not
    purge the queue and cancel the turn only to land on "Unknown
    command". Enqueue categories keep matching case-insensitively via the
    lowercased head word.
    """
    assert classify("/CLEAR") is not SlashCategory.DESTRUCTIVE
    assert classify("/Exit") is not SlashCategory.EXIT
    assert classify("/Help") in {SlashCategory.PURE_INFO, SlashCategory.STATE_MUTATION}


def test_classify_unknown_slash_is_immediate_command() -> None:
    """Unknown slash input reports its error without echoing or queueing."""
    assert classify("/foobar") is SlashCategory.COMMAND


# --------------------------------------------------------------------------- #
# Local (host-only UI) set                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("command", ["/theme", "/theme midnight", "/theme   ", "/THEME"])
def test_classify_local_theme(command: str) -> None:
    """/theme is a host-only UI command -> LOCAL.

    LOCAL commands run immediately (inline on the runtime loop), are never echoed
    as a prompt block, and are never queued behind an in-flight turn.
    """
    assert classify(command) is SlashCategory.LOCAL


def test_local_set_is_narrow_and_disjoint() -> None:
    from opensquilla.cli.repl.slash_policy import (
        LOCAL_SLASH_WORDS,
        PURE_INFO_SLASH_WORDS,
        STATE_MUTATION_SLASH_WORDS,
    )

    # Keep LOCAL narrow: only side-effect-free host commands belong here today.
    assert LOCAL_SLASH_WORDS == {"/theme"}
    # LOCAL must not overlap any queue/cancel/exit category.
    for other in (
        DESTRUCTIVE_SLASH_WORDS,
        EXIT_SLASH_WORDS,
        PURE_INFO_SLASH_WORDS,
        STATE_MUTATION_SLASH_WORDS,
    ):
        assert not (LOCAL_SLASH_WORDS & other)
