"""Slash-command registry helpers for the terminal chat frontend."""

from __future__ import annotations

from dataclasses import dataclass

from rich.markup import escape
from rich.table import Table

from opensquilla.cli.chat.commands import (
    BARE_EXIT_WORDS,
)
from opensquilla.cli.ui import ACCENT_HEADER
from opensquilla.engine.commands import (
    DEFAULT_REGISTRY,
    ArgumentChoice,
    CommandBusyPolicy,
    CommandCategory,
    CommandDef,
    CommandPresentation,
    Surface,
)

DEFAULT_SURFACE = Surface.CLI_GATEWAY


@dataclass(frozen=True)
class SlashCommand:
    """TUI-side view of a unified :class:`CommandDef`."""

    name: str
    usage: str
    description: str
    aliases: tuple[str, ...] = ()
    argument_choices: tuple[ArgumentChoice, ...] = ()
    category: CommandCategory = CommandCategory.QUERY
    busy_policy: CommandBusyPolicy = CommandBusyPolicy.IMMEDIATE
    presentation: CommandPresentation = CommandPresentation.NOTICE
    order: int = 1000
    visible_by_default: bool = True
    deprecated: bool = False

    @property
    def words(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


def _to_shim(cmd: CommandDef, surface: Surface | str) -> SlashCommand:
    return SlashCommand(
        name=cmd.name,
        usage=cmd.usage_for(surface),
        description=cmd.description_for(surface),
        aliases=cmd.aliases,
        argument_choices=cmd.argument_choices_for(surface),
        category=cmd.category,
        busy_policy=cmd.busy_policy,
        presentation=cmd.presentation,
        order=cmd.order,
        visible_by_default=cmd.visible_by_default,
        deprecated=cmd.deprecated,
    )


def registry_for_surface(surface: Surface | str = DEFAULT_SURFACE) -> tuple[SlashCommand, ...]:
    return tuple(_to_shim(cmd, surface) for cmd in DEFAULT_REGISTRY.for_surface(surface))


REGISTRY: tuple[SlashCommand, ...] = registry_for_surface(DEFAULT_SURFACE)

_BARE_EXIT_WORDS = BARE_EXIT_WORDS


def slash_words(surface: Surface | str = DEFAULT_SURFACE) -> list[str]:
    words: list[str] = [word for command in registry_for_surface(surface) for word in command.words]
    words.extend(_BARE_EXIT_WORDS)
    return words


def is_exit_command(value: str, surface: Surface | str = DEFAULT_SURFACE) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    if stripped.lower() in _BARE_EXIT_WORDS:
        return True
    cmd = DEFAULT_REGISTRY.find(stripped, surface=surface)
    return (
        cmd is not None
        and cmd.name == "/exit"
        and stripped in cmd.words()
    )


def find_command(value: str, surface: Surface | str = DEFAULT_SURFACE) -> SlashCommand | None:
    head = value.strip().split(maxsplit=1)[0].lower() if value.strip() else ""
    if not head:
        return None
    if head in _BARE_EXIT_WORDS:
        cmd = DEFAULT_REGISTRY.find("/exit", surface=surface)
        return _to_shim(cmd, surface) if cmd is not None else None
    cmd = DEFAULT_REGISTRY.find(head, surface=surface)
    return _to_shim(cmd, surface) if cmd is not None else None


def render_help_table(surface: Surface | str = DEFAULT_SURFACE) -> Table:
    table = Table(title="OpenSquilla Chat Commands", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("Command", style="bold")
    table.add_column("Description")
    for command in registry_for_surface(surface):
        if not command.visible_by_default or command.deprecated:
            continue
        cell = command.usage
        if command.aliases:
            cell += f"  (alias: {', '.join(command.aliases)})"
        table.add_row(escape(cell), command.description)
    return table


@dataclass(frozen=True)
class KeyBinding:
    """One ``/keys`` cheatsheet row plus the host-source literals that prove it.

    ``js_literals`` are exact substrings of the keyboard-handler source in
    ``package/src/<js_file>``. The cheatsheet is a hand-maintained mirror of
    those imperative handlers, so each row pins the condition it documents;
    ``tests/unit/cli/tui/test_keys_cheatsheet.py`` fails when a binding is
    removed or renamed without updating this table (the same pattern that pins
    ``THEME_NAMES`` to ``theme.mjs``).
    """

    keys: str
    action: str
    js_literals: tuple[str, ...] = ()
    js_file: str = "composer.mjs"


OPENTUI_KEY_BINDINGS: tuple[KeyBinding, ...] = (
    KeyBinding(
        "Enter",
        "Send the message — while a turn runs, steer it",
        ('key.name === "return"',),
    ),
    KeyBinding(
        "Shift+Enter / Alt+Enter",
        "Insert a newline",
        ("key.shift || key.option || key.meta || key.alt",),
    ),
    KeyBinding(
        "Tab",
        "Queue the draft for the next turn (while one runs)",
        ('key.name === "tab" && turnActive',),
    ),
    KeyBinding("Esc", "Cancel the running turn", ('key.name === "escape"',)),
    KeyBinding(
        "Ctrl+C",
        "Clear the input — when empty, cancel the turn",
        ('key.ctrl && key.name === "c"',),
    ),
    KeyBinding("Ctrl+D", "Exit chat", ('key.ctrl && key.name === "d"',)),
    KeyBinding(
        "Ctrl+O",
        "Expand or collapse thinking and tool details",
        ('key?.name !== "o"',),
        js_file="main.mjs",
    ),
    KeyBinding(
        "PageUp / PageDown",
        "Scroll the transcript",
        ('key.name === "pageup"', 'key.name === "pagedown"'),
    ),
    KeyBinding(
        "Ctrl+G / Ctrl+End",
        "Jump back to the latest output",
        ('key.ctrl && key.name === "g"', 'key.ctrl && key.name === "end"'),
    ),
    KeyBinding("Ctrl+L", "Redraw the screen", ('key.ctrl && key.name === "l"',)),
    KeyBinding(
        "Ctrl+A / Ctrl+E · Home / End",
        "Go to the start / end of the line",
        ('key.ctrl && key.name === "a"', 'key.ctrl && key.name === "e"'),
    ),
    KeyBinding(
        "Ctrl+U / Ctrl+K",
        "Cut to the line start / end",
        ('key.name === "u" || key.name === "k" || key.name === "w"',),
    ),
    KeyBinding(
        "Ctrl+W / Alt+Backspace · Alt+D",
        "Cut the previous / next word",
        (
            '(key.meta || key.alt || key.option) && key.name === "backspace"',
            '(key.meta || key.alt || key.option) && key.name === "d"',
        ),
    ),
    KeyBinding("Ctrl+Y", "Paste the last cut text", ('key.ctrl && key.name === "y"',)),
    KeyBinding(
        "Alt+B / Alt+F · Ctrl+←/→",
        "Move back / forward one word",
        ('key.ctrl && key.name === "left"', 'key.ctrl && key.name === "right"'),
    ),
    KeyBinding("@ · /", "Complete a file path · a command (at line start)"),
)

# The plain (native) backend keeps the standard terminal line editor; only the
# bindings its runtime actually handles are documented, mirroring the launch
# banner wording.
NATIVE_KEY_BINDINGS: tuple[KeyBinding, ...] = (
    KeyBinding("Enter", "Send the message"),
    KeyBinding("Ctrl+C", "Clear the input, or cancel the running turn"),
    KeyBinding("Ctrl+D", "Exit chat"),
)


def render_keys_table(*, opentui: bool = True) -> Table:
    table = Table(title="Keyboard Shortcuts", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("Keys", style="bold")
    table.add_column("Action")
    for binding in OPENTUI_KEY_BINDINGS if opentui else NATIVE_KEY_BINDINGS:
        table.add_row(escape(binding.keys), binding.action)
    if not opentui:
        table.caption = (
            "Plain-mode keys. The full-screen TUI adds editing, scroll, and detail shortcuts."
        )
    return table
