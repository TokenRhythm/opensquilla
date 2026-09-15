"""First-run configuration wizard."""

from __future__ import annotations

import os
from pathlib import Path

import questionary
import tomli_w
import typer

from opensquilla.cli import autostart
from opensquilla.cli.ui import console
from opensquilla.paths import default_opensquilla_home, default_profile_name


def _default_model_for_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    if normalized == "tokenrhythm":
        return "deepseek-v4-pro-0813"
    if normalized == "openrouter":
        return "deepseek/deepseek-v4-pro"
    if normalized == "deepseek":
        return "deepseek-v4-flash"
    return "openai/gpt-4o-mini"


def _autostart_context(home: Path) -> tuple[str | None, Path | None]:
    if os.environ.get("OPENSQUILLA_STATE_DIR", "").strip():
        return None, home
    if os.environ.get("OPENSQUILLA_PROFILE", "").strip():
        return default_profile_name(), None
    if os.environ.get("OPENSQUILLA_HOME", "").strip():
        return default_profile_name(), None
    return None, None


def _maybe_register_autostart(home: Path) -> None:
    profile, state_dir = _autostart_context(home)
    try:
        result = autostart.register_logon_task(
            profile=profile,
            home=home,
            state_dir=state_dir,
        )
    except autostart.AutostartError as exc:
        console.print(f"[yellow]Autostart was not registered:[/yellow] {exc}")
        return
    console.print(f"[green]{result.summary()}[/green]")


def run_init(autostart_register: bool = False) -> None:
    """Create a basic OpenSquilla home with env and config files."""
    home = default_opensquilla_home()
    env_path = home / ".env"
    config_path = home / "config.toml"
    home.mkdir(parents=True, exist_ok=True)
    (home / "state").mkdir(parents=True, exist_ok=True)

    provider = questionary.select(
        "Choose provider:",
        choices=["tokenrhythm", "openrouter", "openai", "anthropic", "deepseek", "custom"],
        default="tokenrhythm",
    ).ask()
    if not provider:
        raise typer.Exit(1)

    api_key = questionary.password("API key:").ask()
    if api_key is None:
        raise typer.Exit(1)

    default_model = questionary.text(
        "Default model:",
        default=_default_model_for_provider(provider),
    ).ask()
    if not default_model:
        raise typer.Exit(1)

    key_name = f"{provider.upper()}_API_KEY" if provider != "custom" else "OPENSQUILLA_LLM_API_KEY"
    existing_env = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = [line for line in existing_env.splitlines() if not line.startswith(f"{key_name}=")]
    lines.append(f"{key_name}={api_key}")
    env_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    config = {
        "llm": {
            "provider": provider,
            "model": default_model,
        },
        "state_dir": str(home / "state"),
    }
    config_path.write_text(tomli_w.dumps(config), encoding="utf-8")

    console.print(f"[green]Wrote[/green] {env_path}")
    console.print(f"[green]Wrote[/green] {config_path}")
    if autostart_register:
        _maybe_register_autostart(home)
    console.print("[dim]Tip: enable shell completion with `opensquilla --install-completion`[/dim]")


def init_command(
    autostart_register: bool = typer.Option(
        False,
        "--autostart/--no-autostart",
        help="Register Windows logon autostart after writing the init files.",
    ),
) -> None:
    """Initialize a workspace.

    Deprecated: prefer ``opensquilla onboard`` for full provider/channel setup.
    Kept for compatibility with older scripts.
    """
    run_init(autostart_register=autostart_register)
