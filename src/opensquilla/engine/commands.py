"""Unified slash-command registry.

Source of truth for slash commands across chat surfaces. Per-surface adapters
in ``cli/repl/commands.py``, ``channels/command_registry.py``, and the web
frontend consume this single registry so the visible command set stays in
lockstep across surfaces.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Surface(StrEnum):
    """Chat surface that may render a slash command.

    Legacy names remain as enum aliases for existing in-process callers. Use
    :func:`parse_surface` for user/input parsing so old values such as ``web``
    and ``tui`` normalize to the canonical surface names.
    """

    WEB_CHAT = "web_chat"
    CLI_GATEWAY = "cli_gateway"
    CLI_STANDALONE = "cli_standalone"
    CHANNEL = "channel"

    WEB = "web_chat"
    TUI = "cli_gateway"
    CLI = "cli_gateway"


_SURFACE_ALIASES = {
    "web": Surface.WEB_CHAT,
    "tui": Surface.CLI_GATEWAY,
    "cli": Surface.CLI_GATEWAY,
}


def parse_surface(value: str) -> Surface:
    """Parse canonical and legacy surface names."""
    normalized = value.strip().lower()
    if normalized in _SURFACE_ALIASES:
        return _SURFACE_ALIASES[normalized]
    try:
        return Surface(normalized)
    except ValueError as exc:
        valid = ", ".join(sorted({s.value for s in Surface} | set(_SURFACE_ALIASES)))
        raise ValueError(f"unknown surface {value!r}; valid: {valid}") from exc


# Per-envelope params builder for channel-mode dispatch. Kept as a generic
# Callable to avoid a cycle with opensquilla.gateway.routing.RouteEnvelope at
# import time — the channel dispatcher passes its own envelope and we only
# require attribute access (`session_key`).
ParamsFactory = Callable[[Any], dict[str, Any]]


@dataclass(frozen=True)
class ArgumentChoice:
    """One user-visible argument option for slash-command completion."""

    value: str
    description: str


class ExecutionKind(StrEnum):
    """How a surface executes a slash command."""

    RPC = "rpc"
    LOCAL = "local"


class CommandCategory(StrEnum):
    """Product role of a slash command."""

    QUERY = "query"
    CONTROL = "control"
    NAVIGATION = "navigation"
    TURN = "turn"


class CommandBusyPolicy(StrEnum):
    """Scheduling contract when a turn is already running."""

    IMMEDIATE = "immediate"
    NEXT_TURN = "next_turn"
    REQUIRE_IDLE = "require_idle"
    ABORT_AND_RUN = "abort_and_run"
    DRAIN_AND_EXIT = "drain_and_exit"


class CommandPresentation(StrEnum):
    """Preferred UI treatment for a command and its result."""

    NOTICE = "notice"
    PICKER = "picker"
    PANEL = "panel"
    TURN = "turn"


@dataclass(frozen=True)
class CommandExecution:
    """Per-surface execution metadata for a slash command."""

    kind: ExecutionKind
    action: str
    rpc_method: str | None = None
    rpc_params: ParamsFactory | None = None
    usage: str | None = None
    description: str | None = None
    argument_choices: tuple[ArgumentChoice, ...] | None = None


@dataclass(frozen=True)
class CommandDef:
    """One slash command as visible across all surfaces it supports.

    The same `CommandDef` instance is shared by every surface that lists the
    command. Per-surface execution metadata describes whether a surface calls
    gateway RPC or handles the command locally.
    """

    name: str
    usage: str
    description: str
    execution: Mapping[Surface, CommandExecution]
    aliases: tuple[str, ...] = ()
    argument_choices: tuple[ArgumentChoice, ...] = ()
    category: CommandCategory = CommandCategory.QUERY
    busy_policy: CommandBusyPolicy = CommandBusyPolicy.IMMEDIATE
    presentation: CommandPresentation = CommandPresentation.NOTICE
    order: int = 1000
    visible_by_default: bool = True
    deprecated: bool = False

    @property
    def surfaces(self) -> frozenset[Surface]:
        """Return surfaces where this command has visible execution."""
        return frozenset(self.execution.keys())

    @property
    def rpc_method(self) -> str | None:
        """Deprecated channel RPC method compatibility projection."""
        execution = self.execution_for(Surface.CHANNEL)
        return execution.rpc_method if execution is not None else None

    @property
    def rpc_params(self) -> ParamsFactory | None:
        """Deprecated channel RPC params compatibility projection."""
        execution = self.execution_for(Surface.CHANNEL)
        return execution.rpc_params if execution is not None else None

    def execution_for(self, surface: Surface | str) -> CommandExecution | None:
        """Return execution metadata for a surface, if visible there."""
        parsed = parse_surface(surface) if isinstance(surface, str) else surface
        return self.execution.get(parsed)

    def usage_for(self, surface: Surface | str) -> str:
        """Return surface-specific usage without changing another client's contract."""
        execution = self.execution_for(surface)
        if execution is not None and execution.usage is not None:
            return execution.usage
        return self.usage

    def description_for(self, surface: Surface | str) -> str:
        """Return surface-specific help text, falling back to the shared text."""
        execution = self.execution_for(surface)
        if execution is not None and execution.description is not None:
            return execution.description
        return self.description

    def argument_choices_for(self, surface: Surface | str) -> tuple[ArgumentChoice, ...]:
        """Return argument choices that are valid on ``surface``."""
        execution = self.execution_for(surface)
        if execution is not None and execution.argument_choices is not None:
            return execution.argument_choices
        return self.argument_choices

    def words(self) -> tuple[str, ...]:
        """Return name + aliases. Used by completion machinery."""
        return (self.name, *self.aliases)


class SlashCommandRegistry:
    """Per-surface lookup, alias resolution, and stable help generation.

    The registry is constructed once with the canonical command tuple. All
    lookups normalize the input head (lowercase, strip leading whitespace)
    so callers can pass user-typed text directly. Result lists are
    returned in surface-appropriate stable order. Terminal surfaces use the
    explicit product order; web/channel keep their historical lexical order so
    adding TUI metadata cannot reorder another client's menu.
    """

    def __init__(self, commands: tuple[CommandDef, ...]) -> None:
        self._commands: tuple[CommandDef, ...] = tuple(commands)
        self._by_word: dict[str, CommandDef] = {}
        for cmd in self._commands:
            for word in cmd.words():
                lower = word.lower()
                if lower in self._by_word:
                    raise ValueError(
                        f"duplicate slash word {word!r}: {self._by_word[lower].name} vs {cmd.name}"
                    )
                self._by_word[lower] = cmd

    def for_surface(self, surface: Surface | str) -> tuple[CommandDef, ...]:
        parsed = parse_surface(surface) if isinstance(surface, str) else surface
        commands = [c for c in self._commands if c.execution_for(parsed) is not None]
        if parsed in {Surface.CLI_GATEWAY, Surface.CLI_STANDALONE}:
            # Python's sort is stable: equal/default orders retain declaration
            # order for extension/test registries, while shipped commands use
            # unique curated values.
            commands.sort(key=lambda command: command.order)
        else:
            commands.sort(key=lambda command: command.name)
        return tuple(commands)

    def find(self, value: str, surface: Surface | str | None = None) -> CommandDef | None:
        head = value.strip().split(maxsplit=1)[0].lower() if value.strip() else ""
        if not head:
            return None
        cmd = self._by_word.get(head)
        if cmd is None:
            return None
        if surface is not None and cmd.execution_for(surface) is None:
            return None
        return cmd

    def help_lines(self, surface: Surface | str) -> list[str]:
        """Return ``["/name — description", ...]`` in surface order."""
        return [f"{c.name} — {c.description_for(surface)}" for c in self.for_surface(surface)]


# ---------------------------------------------------------------------------
# Canonical registry: every slash command shipped today across the three
# surfaces. Its surface adapters are:
#   - cli/repl/commands.py (TUI)
#   - channels/command_registry.py (channel)
#   - opensquilla-webui/src/composables/chat/useChatSlashCommands.ts (web,
#     loaded through the commands.list_for_surface RPC)
# Where canonical name diverges (TUI's /clear vs web/channel's /reset),
# we pick the cross-surface name and demote the other to alias.
# ---------------------------------------------------------------------------


def _key(envelope: Any) -> dict[str, str]:
    return {"key": envelope.session_key}


def _session_key(envelope: Any) -> dict[str, str]:
    return {"sessionKey": envelope.session_key}


def _sandbox_session_key(envelope: Any) -> dict[str, str]:
    return {"sessionKey": envelope.session_key}


def _empty(_envelope: Any) -> dict[str, Any]:
    return {}


_W = Surface.WEB_CHAT
_T = Surface.CLI_GATEWAY
_S = Surface.CLI_STANDALONE
_C = Surface.CHANNEL


def _local(
    action: str,
    *,
    usage: str | None = None,
    description: str | None = None,
    argument_choices: tuple[ArgumentChoice, ...] | None = None,
) -> CommandExecution:
    return CommandExecution(
        kind=ExecutionKind.LOCAL,
        action=action,
        usage=usage,
        description=description,
        argument_choices=argument_choices,
    )


def _rpc(method: str, params: ParamsFactory | None = None) -> CommandExecution:
    return CommandExecution(
        kind=ExecutionKind.RPC,
        action=method,
        rpc_method=method,
        rpc_params=params,
    )


_COMMANDS: tuple[CommandDef, ...] = (
    # ---- Cross-surface (web + tui + channel where applicable) -------------
    CommandDef(
        name="/new",
        usage="/new [title]",
        description="Start a new chat session.",
        execution={
            _W: _rpc("sessions.reset", _key),
            _T: _local("session.new"),
            _S: _local("session.new"),
            _C: _rpc("sessions.reset", _key),
        },
        category=CommandCategory.NAVIGATION,
        busy_policy=CommandBusyPolicy.REQUIRE_IDLE,
        presentation=CommandPresentation.NOTICE,
        order=40,
    ),
    CommandDef(
        name="/reset",
        usage="/reset",
        description="Clear the current conversation context.",
        execution={
            _W: _rpc("sessions.reset", _key),
            _T: _local("session.reset"),
            _S: _local("session.reset"),
            _C: _rpc("sessions.reset", _key),
        },
        aliases=("/clear",),
        category=CommandCategory.CONTROL,
        busy_policy=CommandBusyPolicy.ABORT_AND_RUN,
        presentation=CommandPresentation.NOTICE,
        order=170,
    ),
    CommandDef(
        name="/compact",
        usage="/compact",
        description="Compact older context in the current session.",
        execution={
            _W: _rpc("sessions.contextCompact", _key),
            _T: _local("session.compact"),
            _S: _local("session.compact"),
            _C: _rpc("sessions.contextCompact", _key),
        },
        aliases=("/cmp",),
        category=CommandCategory.CONTROL,
        busy_policy=CommandBusyPolicy.ABORT_AND_RUN,
        presentation=CommandPresentation.NOTICE,
        order=70,
    ),
    # ---- TUI + Channel ----------------------------------------------------
    CommandDef(
        name="/help",
        usage="/help",
        description="Show available commands.",
        execution={_T: _local("help.show"), _S: _local("help.show"), _C: _rpc("status", _empty)},
        category=CommandCategory.QUERY,
        busy_policy=CommandBusyPolicy.IMMEDIATE,
        presentation=CommandPresentation.PANEL,
        order=100,
    ),
    CommandDef(
        name="/keys",
        usage="/keys",
        description="Show keyboard shortcuts.",
        execution={_T: _local("keys.show"), _S: _local("keys.show")},
        aliases=("/shortcuts",),
        category=CommandCategory.QUERY,
        busy_policy=CommandBusyPolicy.IMMEDIATE,
        presentation=CommandPresentation.PANEL,
        order=101,
    ),
    CommandDef(
        name="/theme",
        usage="/theme [name]",
        description="List or switch the OpenTUI color theme.",
        execution={_T: _local("theme.set"), _S: _local("theme.set")},
        category=CommandCategory.CONTROL,
        busy_policy=CommandBusyPolicy.IMMEDIATE,
        presentation=CommandPresentation.PICKER,
        order=90,
    ),
    CommandDef(
        name="/strategy",
        usage="/strategy [direct|router|ensemble|status]",
        description="Choose or inspect the shared model strategy.",
        execution={_T: _local("model.routing.strategy")},
        argument_choices=(
            ArgumentChoice("direct", "Use the selected model directly from the next turn."),
            ArgumentChoice("router", "Use Squilla Router from the next turn."),
            ArgumentChoice("ensemble", "Use Model Ensemble from the next turn."),
            ArgumentChoice("status", "Show the canonical Gateway strategy."),
        ),
        category=CommandCategory.CONTROL,
        busy_policy=CommandBusyPolicy.NEXT_TURN,
        presentation=CommandPresentation.PICKER,
        order=20,
    ),
    CommandDef(
        name="/router",
        usage="/router [on|off|status]",
        description="Inspect or switch the shared model strategy.",
        execution={_T: _local("model.routing.router")},
        argument_choices=(
            ArgumentChoice("on", "Use Squilla Router from the next turn."),
            ArgumentChoice("off", "Switch to direct model selection."),
            ArgumentChoice("status", "Show the canonical Gateway strategy."),
        ),
        category=CommandCategory.CONTROL,
        busy_policy=CommandBusyPolicy.NEXT_TURN,
        presentation=CommandPresentation.PICKER,
        order=210,
        visible_by_default=False,
    ),
    CommandDef(
        name="/ensemble",
        usage="/ensemble [on|off|status]",
        description="Inspect or switch the shared model strategy.",
        execution={_T: _local("model.routing.ensemble")},
        argument_choices=(
            ArgumentChoice("on", "Use Model Ensemble from the next turn."),
            ArgumentChoice("off", "Switch to direct model selection."),
            ArgumentChoice("status", "Show the canonical Gateway strategy."),
        ),
        category=CommandCategory.CONTROL,
        busy_policy=CommandBusyPolicy.NEXT_TURN,
        presentation=CommandPresentation.PICKER,
        order=220,
        visible_by_default=False,
    ),
    CommandDef(
        name="/status",
        usage="/status",
        description="Show current session, model, and mode.",
        execution={
            _T: _local("status.show"),
            _S: _local("status.show"),
            _C: _rpc("status", _empty),
        },
        aliases=("/session",),
        category=CommandCategory.QUERY,
        busy_policy=CommandBusyPolicy.IMMEDIATE,
        presentation=CommandPresentation.PANEL,
        order=60,
    ),
    CommandDef(
        name="/model",
        usage="/model [name]",
        description="List available models.",
        execution={
            _T: _local(
                "model.select",
                usage="/model [auto|status|name]",
                description="Choose or inspect the session model.",
                argument_choices=(
                    ArgumentChoice("auto", "Clear the session model pin; let the strategy choose."),
                    ArgumentChoice("status", "Show the session model pin and effective model."),
                ),
            ),
            _S: _local(
                "model.select",
                usage="/model [auto|status|name]",
                description="Choose or inspect the session model.",
                argument_choices=(
                    ArgumentChoice("auto", "Clear the session model pin; let the strategy choose."),
                    ArgumentChoice("status", "Show the session model pin and effective model."),
                ),
            ),
            _C: _rpc("models.list", _empty),
        },
        category=CommandCategory.CONTROL,
        busy_policy=CommandBusyPolicy.IMMEDIATE,
        presentation=CommandPresentation.PICKER,
        order=10,
    ),
    # ---- TUI only ---------------------------------------------------------
    CommandDef(
        name="/models",
        usage="/models",
        description="List available Gateway models.",
        execution={_T: _local("models.list")},
        category=CommandCategory.QUERY,
        busy_policy=CommandBusyPolicy.IMMEDIATE,
        presentation=CommandPresentation.PANEL,
        order=900,
        visible_by_default=False,
        deprecated=True,
    ),
    CommandDef(
        name="/cost",
        usage="/cost",
        description="Show current REPL session usage.",
        execution={_T: _local("usage.cost"), _S: _local("usage.cost")},
        category=CommandCategory.QUERY,
        busy_policy=CommandBusyPolicy.IMMEDIATE,
        presentation=CommandPresentation.PANEL,
        order=190,
    ),
    CommandDef(
        name="/usage",
        usage="/usage",
        description="Show gateway aggregate usage.",
        execution={
            _W: _rpc("usage.status"),
            _T: _rpc("usage.status"),
            _C: _rpc("usage.status", _empty),
        },
        category=CommandCategory.QUERY,
        busy_policy=CommandBusyPolicy.IMMEDIATE,
        presentation=CommandPresentation.PANEL,
        order=80,
    ),
    CommandDef(
        name="/file",
        usage="/file <path> [prompt]",
        description="Upload a local file from this CLI machine.",
        execution={_T: _local("cli.file")},
        category=CommandCategory.TURN,
        busy_policy=CommandBusyPolicy.NEXT_TURN,
        presentation=CommandPresentation.TURN,
        order=120,
    ),
    CommandDef(
        name="/save",
        usage="/save [file]",
        description="Export the current REPL transcript as markdown.",
        execution={_T: _local("transcript.save"), _S: _local("transcript.save")},
        category=CommandCategory.QUERY,
        busy_policy=CommandBusyPolicy.IMMEDIATE,
        presentation=CommandPresentation.NOTICE,
        order=200,
    ),
    CommandDef(
        name="/image",
        usage="/image <path> [prompt]",
        description="Attach an image and send a prompt.",
        execution={_T: _local("image.attach"), _S: _local("image.attach")},
        category=CommandCategory.TURN,
        busy_policy=CommandBusyPolicy.NEXT_TURN,
        presentation=CommandPresentation.TURN,
        order=130,
    ),
    CommandDef(
        name="/path",
        usage="/path <path> [prompt]",
        description=(
            "Analyze a local path without uploading bytes; sends the path string "
            "as prompt text."
        ),
        execution={_T: _local("path.analyze"), _S: _local("path.analyze")},
        category=CommandCategory.TURN,
        busy_policy=CommandBusyPolicy.NEXT_TURN,
        presentation=CommandPresentation.TURN,
        order=140,
    ),
    CommandDef(
        name="/approvals",
        usage="/approvals [reset]",
        description="Show or reset approval state.",
        execution={_T: _local("approvals.show")},
        category=CommandCategory.CONTROL,
        busy_policy=CommandBusyPolicy.IMMEDIATE,
        presentation=CommandPresentation.PANEL,
        order=180,
    ),
    CommandDef(
        name="/permissions",
        usage="/permissions [mode]",
        description="Show or set the session permission override.",
        execution={_T: _local("permissions.show")},
        aliases=("/elevated",),
        argument_choices=(
            ArgumentChoice("off", "Clear session override; configured default resumes."),
            ArgumentChoice("on", "Host exec, approvals required."),
            ArgumentChoice(
                "bypass",
                "Host exec, approvals auto-granted; sensitive paths still blocked.",
            ),
            ArgumentChoice("full", "Host exec, approvals skipped; sensitive paths bypassed."),
            ArgumentChoice("status", "Show current session permissions override."),
        ),
        category=CommandCategory.CONTROL,
        busy_policy=CommandBusyPolicy.IMMEDIATE,
        presentation=CommandPresentation.PICKER,
        order=50,
    ),
    CommandDef(
        name="/forget",
        usage="/forget [target]",
        description="Compatibility no-op for removed approval cache.",
        execution={_T: _local("approvals.forget")},
        category=CommandCategory.QUERY,
        busy_policy=CommandBusyPolicy.IMMEDIATE,
        presentation=CommandPresentation.NOTICE,
        order=910,
        visible_by_default=False,
        deprecated=True,
    ),
    CommandDef(
        name="/sessions",
        usage="/sessions [limit]",
        description="List recent sessions.",
        execution={_T: _local("sessions.list")},
        category=CommandCategory.NAVIGATION,
        busy_policy=CommandBusyPolicy.IMMEDIATE,
        presentation=CommandPresentation.PICKER,
        order=30,
    ),
    CommandDef(
        name="/resume",
        usage="/resume <id>",
        description="Resume an existing session.",
        execution={_T: _local("sessions.resume")},
        category=CommandCategory.NAVIGATION,
        busy_policy=CommandBusyPolicy.REQUIRE_IDLE,
        presentation=CommandPresentation.PICKER,
        order=150,
    ),
    CommandDef(
        name="/delete",
        usage="/delete <id>",
        description="Delete a session.",
        execution={_T: _local("sessions.delete")},
        category=CommandCategory.NAVIGATION,
        busy_policy=CommandBusyPolicy.REQUIRE_IDLE,
        presentation=CommandPresentation.NOTICE,
        order=160,
    ),
    CommandDef(
        name="/exit",
        usage="/exit",
        description="Exit the REPL.",
        execution={_T: _local("repl.exit"), _S: _local("repl.exit")},
        aliases=("/quit",),
        category=CommandCategory.NAVIGATION,
        busy_policy=CommandBusyPolicy.DRAIN_AND_EXIT,
        presentation=CommandPresentation.NOTICE,
        order=110,
    ),
    # ---- Channel only -----------------------------------------------------
    CommandDef(
        name="/abort",
        usage="/abort",
        description="Abort the in-progress turn.",
        execution={_C: _rpc("sessions.abort", _key)},
    ),
    CommandDef(
        name="/history",
        usage="/history",
        description="Show recent chat history.",
        execution={_C: _rpc("chat.history", _session_key)},
    ),
    CommandDef(
        name="/memory",
        usage="/memory",
        description="Show memory subsystem status.",
        execution={_C: _rpc("doctor.memory.status", _empty)},
    ),
    CommandDef(
        name="/sandbox",
        usage="/sandbox <standard|trusted|full>",
        description="Set the channel session sandbox mode.",
        execution={_C: _rpc("sandbox.run_context.set", _sandbox_session_key)},
        argument_choices=(
            ArgumentChoice("standard", "Use Standard-Sandbox for this channel session."),
            ArgumentChoice("trusted", "Use Managed Execution for this channel session."),
            ArgumentChoice("full", "Use Full Host Access; channel admin only."),
        ),
    ),
    CommandDef(
        name="/meta",
        usage="/meta [skill-name]",
        description="List meta-skills, or run one with /meta <skill-name>.",
        execution={
            _W: _local("meta.menu"),
            _T: _local("meta.menu"),
            _C: _rpc("meta.list", _empty),
        },
        category=CommandCategory.TURN,
        busy_policy=CommandBusyPolicy.NEXT_TURN,
        presentation=CommandPresentation.TURN,
        order=230,
    ),
    CommandDef(
        name="/skills",
        usage="/skills",
        description="List loaded skills.",
        execution={_C: _rpc("skills.list", _empty)},
    ),
)


DEFAULT_REGISTRY = SlashCommandRegistry(_COMMANDS)


__all__ = [
    "ArgumentChoice",
    "CommandBusyPolicy",
    "CommandCategory",
    "CommandDef",
    "CommandExecution",
    "CommandPresentation",
    "DEFAULT_REGISTRY",
    "ExecutionKind",
    "ParamsFactory",
    "SlashCommandRegistry",
    "Surface",
    "parse_surface",
]
