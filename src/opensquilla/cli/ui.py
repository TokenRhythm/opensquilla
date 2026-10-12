"""CLI compatibility import for shared terminal presentation helpers."""

from rich.panel import Panel

from opensquilla.ui import (
    ACCENT,
    ACCENT_DEEP,
    ACCENT_DIM,
    ACCENT_HEADER,
    ACCENT_INK,
    ACCENT_MARKUP,
    ACCENT_SOFT,
    banner_panel,
    console,
    error_console,
    error_panel,
    markup_escape,
    questionary_style,
    section_rule,
    warning_panel,
)

_KIND_BORDER: dict[str, str] = {
    "info": ACCENT,
    "warn": "yellow",
    "error": "red",
    "block": "red",
}

_KIND_TITLE: dict[str, str] = {
    "info": "Notice",
    "warn": "Heads up",
    "error": "Error",
    "block": "Blocked",
}


def notice_panel(
    body: str,
    *,
    kind: str,
    title: str | None = None,
    command: str | None = None,
    hint: str | None = None,
) -> Panel:
    """Return a width-clamped notice panel.

    kind ∈ {"info", "warn", "error", "block"}
    """
    border_style = _KIND_BORDER.get(kind, "yellow")
    resolved_title = title if title is not None else _KIND_TITLE.get(kind, "Notice")

    lines: list[str] = []
    if command:
        lines.append(f"[bold]Command:[/bold] {markup_escape(command)}")
    if body:
        lines.append(f"[dim]{markup_escape(body)}[/dim]")
    if hint:
        lines.append(f"[dim]{markup_escape(hint)}[/dim]")

    content = "\n".join(lines) if lines else ""
    width = min(console.width, 88)
    return Panel(
        content,
        title=resolved_title,
        border_style=border_style,
        expand=False,
        width=width,
        padding=(0, 1),
    )


__all__ = [
    "ACCENT",
    "ACCENT_DEEP",
    "ACCENT_DIM",
    "ACCENT_HEADER",
    "ACCENT_INK",
    "ACCENT_MARKUP",
    "ACCENT_SOFT",
    "banner_panel",
    "console",
    "error_console",
    "error_panel",
    "markup_escape",
    "notice_panel",
    "questionary_style",
    "section_rule",
    "warning_panel",
]
