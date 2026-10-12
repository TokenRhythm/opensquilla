"""Shared terminal presentation helpers."""

from __future__ import annotations

import sys
from typing import IO, cast

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel


class _DynamicStream:
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def _stream(self):
        return getattr(sys, self._name)

    def write(self, data: str) -> int:
        try:
            written = self._stream.write(data)
        except UnicodeEncodeError:
            written = self._stream.write(_replace_unencodable(data, self._stream))
        return len(data) if written is None else int(written)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def _replace_unencodable(data: str, stream: object) -> str:
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        return data.encode(encoding, errors="replace").decode(encoding, errors="replace")
    except LookupError:
        return data.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


console = Console(file=cast(IO[str], _DynamicStream("stdout")), highlight=False)
error_console = Console(file=cast(IO[str], _DynamicStream("stderr")), highlight=False)

ACCENT = "#F56600"
ACCENT_SOFT = "#FF8A4C"
ACCENT_DEEP = "#B0440A"
ACCENT_DIM = "#7A2C00"
ACCENT_INK = "#1a0e02"
ACCENT_HEADER = f"bold {ACCENT}"
ACCENT_MARKUP = ACCENT


def _apply_typer_help_theme() -> None:
    try:
        from typer import rich_utils
    except ImportError:
        return

    import click
    from rich import box
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    rich_utils.STYLE_OPTIONS_PANEL_BORDER = ACCENT
    rich_utils.STYLE_COMMANDS_PANEL_BORDER = ACCENT
    rich_utils.STYLE_OPTION = ACCENT_HEADER
    rich_utils.STYLE_SWITCH = ACCENT_HEADER
    rich_utils.STYLE_COMMANDS_TABLE_FIRST_COLUMN = ACCENT_HEADER
    rich_utils.STYLE_USAGE = ACCENT_SOFT
    rich_utils.STYLE_METAVAR = f"bold {ACCENT_SOFT}"

    def _is_argument(param: object) -> bool:
        return (
            isinstance(param, click.Argument)
            or getattr(param, "param_type_name", "") == "argument"
        )

    def _is_option(param: object) -> bool:
        return isinstance(param, click.Option) or getattr(param, "param_type_name", "") == "option"

    def _make_metavar(param: click.Parameter) -> str:
        metavar = getattr(param, "metavar", None)
        if metavar:
            return str(metavar)
        name = getattr(param, "name", None)
        if _is_argument(param) and name:
            return str(name).upper()
        param_type = getattr(param, "type", None)
        type_name = getattr(param_type, "name", "text")
        return str(type_name).upper()

    def _parameter_label(param: click.Parameter, ctx: click.Context) -> Text:
        if _is_argument(param):
            metavar = _make_metavar(param)
            return Text(metavar, style=rich_utils.STYLE_METAVAR)

        if not _is_option(param):
            return Text(str(getattr(param, "name", "") or ""), style=rich_utils.STYLE_OPTION)
        opt_parts = [*getattr(param, "opts", ())]
        secondary_opts = getattr(param, "secondary_opts", ())
        if secondary_opts:
            opt_parts.append("/".join(secondary_opts))
        label = Text(", ".join(opt_parts), style=rich_utils.STYLE_OPTION)

        metavar = _make_metavar(param)
        if metavar != "BOOLEAN":
            label.append(" ")
            label.append(metavar, style=rich_utils.STYLE_METAVAR)
        if getattr(param, "required", False):
            label.append(" ")
            label.append(rich_utils.REQUIRED_SHORT_STRING, style=rich_utils.STYLE_REQUIRED_SHORT)
        return label

    def _parameter_help(param: click.Parameter, ctx: click.Context) -> Text:
        if _is_option(param):
            try:
                help_record = param.get_help_record(ctx)
            except (AttributeError, TypeError):
                help_text = str(getattr(param, "help", "") or "")
                default = getattr(param, "default", None)
                if getattr(param, "show_default", False) and default not in (None, "", False):
                    help_text = f"{help_text}  [default: {default}]".strip()
                return Text(help_text, style=rich_utils.STYLE_OPTION_HELP)
            else:
                if help_record is not None:
                    return Text(help_record[1] or "", style=rich_utils.STYLE_OPTION_HELP)
        return Text(str(getattr(param, "help", "") or ""), style=rich_utils.STYLE_OPTION_HELP)

    def _print_compact_options_panel(
        *,
        name: str,
        params: list[click.Option] | list[click.Argument],
        ctx: click.Context,
        markup_mode: rich_utils.MarkupModeStrict,
        console,
    ) -> None:
        if not params:
            return

        table = Table(
            highlight=False,
            show_header=False,
            expand=True,
            box=getattr(box, rich_utils.STYLE_OPTIONS_TABLE_BOX, None),
            border_style=rich_utils.STYLE_OPTIONS_TABLE_BORDER_STYLE,
            row_styles=rich_utils.STYLE_OPTIONS_TABLE_ROW_STYLES,
            pad_edge=rich_utils.STYLE_OPTIONS_TABLE_PAD_EDGE,
            padding=rich_utils.STYLE_OPTIONS_TABLE_PADDING,
            show_lines=rich_utils.STYLE_OPTIONS_TABLE_SHOW_LINES,
            leading=rich_utils.STYLE_OPTIONS_TABLE_LEADING,
        )
        table.add_column("Option", no_wrap=True)
        table.add_column("Help", ratio=1)
        for param in params:
            table.add_row(
                _parameter_label(param, ctx),
                _parameter_help(param, ctx),
            )

        console.print(
            Panel(
                table,
                border_style=rich_utils.STYLE_OPTIONS_PANEL_BORDER,
                title=name,
                title_align=rich_utils.ALIGN_OPTIONS_PANEL,
            )
        )

    rich_utils._print_options_panel = _print_compact_options_panel  # noqa: SLF001


_apply_typer_help_theme()


def error_panel(message: str, *, title: str = "Error") -> Panel:
    """Return a compact operator-facing error panel."""
    return Panel(f"[red]{markup_escape(message)}[/red]", title=title, border_style="red")


def warning_panel(message: str, *, title: str = "Warning") -> Panel:
    """Return a brand-tinted warning panel for recoverable setup gaps."""
    body = f"[bold {ACCENT}]▌ {markup_escape(title)}[/bold {ACCENT}]"
    body += f"\n[dim]{markup_escape(message)}[/dim]"
    return Panel(body, border_style=ACCENT_SOFT, padding=(0, 2))


def markup_escape(value: object) -> str:
    """Escape dynamic text before interpolating it into Rich markup."""
    return escape(str(value))


def banner_panel(title: str, subtitle: str = "") -> Panel:
    """Brand-tinted header panel used by onboarding / setup surfaces."""
    body = f"[bold {ACCENT}]▌ {markup_escape(title)}[/bold {ACCENT}]"
    if subtitle:
        body += f"\n[dim]{markup_escape(subtitle)}[/dim]"
    return Panel(
        body,
        border_style=ACCENT,
        padding=(0, 2),
    )


def section_rule(label: str) -> str:
    """A compact rule string with the brand accent for inline section markers."""
    return (
        f"[bold {ACCENT}]┄┄┄ {markup_escape(label)} "
        f"[/bold {ACCENT}][{ACCENT_DIM}]"
        + "─" * 6
        + "[/]"
    )


def questionary_style():
    """Build a questionary Style aligned with the WebUI brand orange.

    Returns ``None`` if questionary is unavailable or stubbed in tests.
    """
    try:
        from questionary import Style
    except (ImportError, AttributeError):
        return None

    return Style(
        [
            ("qmark", f"fg:{ACCENT} bold"),
            ("question", "bold"),
            ("answer", f"fg:{ACCENT_SOFT} bold"),
            ("pointer", f"fg:{ACCENT} bold noreverse"),
            ("highlighted", f"fg:{ACCENT} bold noreverse"),
            ("selected", f"fg:{ACCENT_SOFT} noreverse"),
            ("separator", f"fg:{ACCENT_DIM}"),
            ("instruction", "fg:#7a7a7a"),
            ("text", ""),
            ("disabled", "fg:#666666 italic"),
        ]
    )
