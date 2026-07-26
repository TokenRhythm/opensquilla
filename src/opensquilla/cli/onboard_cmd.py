"""CLI: opensquilla onboard / configure."""

from __future__ import annotations

import json as _json
import tomllib
from pathlib import Path
from typing import NoReturn

import typer
from pydantic import ValidationError
from rich.table import Table

from opensquilla.cli.output import print_json
from opensquilla.cli.ui import (
    ACCENT,
    ACCENT_SOFT,
    banner_panel,
    console,
    error_console,
    markup_escape,
    warning_panel,
)
from opensquilla.onboarding.config_store import load_config, resolve_config_path
from opensquilla.onboarding.errors import UserCancelledError
from opensquilla.onboarding.flow import (
    OnboardOptions,
    run_interactive_configure,
    run_interactive_onboard,
)
from opensquilla.onboarding.legacy_data import legacy_data_payload
from opensquilla.onboarding.next_steps import (
    env_recovery_commands,
    env_reference_warnings,
    format_next_steps,
    headless_setup_commands,
    human_env_command,
    quote_cli_arg,
    setup_catalog_command,
)
from opensquilla.onboarding.redaction import redact_error_text
from opensquilla.onboarding.section_status import SECTION_STATUS_DISPLAY, SectionStatus
from opensquilla.onboarding.setup_engine import (
    AUDIO_SECTION_ALIASES,
    ENSEMBLE_SECTION_ALIASES,
    IMAGE_GENERATION_SECTION_ALIASES,
    MEMORY_EMBEDDING_SECTION_ALIASES,
    SetupEngine,
    setup_catalog_payload,
)
from opensquilla.onboarding.setup_paths import web_setup_url
from opensquilla.onboarding.status import OnboardingStatus, get_onboarding_status
from opensquilla.router_tiers import DEFAULT_TEXT_TIER, TEXT_TIERS

# Exit code for a user-initiated cancellation (Esc/Ctrl+C in the wizard).
# 130 = 128 + SIGINT, the conventional shell exit status for an interrupt;
# there is no earlier repo convention for wizard cancellation, so this pins it.
_CANCELLED_EXIT_CODE = 130

_STATUS_BLOCKING = {SectionStatus.MISSING, SectionStatus.DEGRADED, SectionStatus.UNKNOWN}
# Single source of truth for the status words lives in section_status so the
# table below and the next-steps capability summary can never diverge.
_STATUS_DISPLAY: dict[SectionStatus, str] = SECTION_STATUS_DISPLAY
_LLM_SOURCE_DISPLAY = {
    "explicit": "explicit key",
    "env": "env key visible",
    "missing_env": "env key not visible",
    "none": "not configured",
}
_IMAGE_SOURCE_DISPLAY = {
    "explicit": "explicit key",
    "env": "env key visible",
    "llm_fallback": "same provider key",
    "none": "not configured",
}


def _section_label(status: OnboardingStatus, name: str) -> str:
    detail = status.section_details.get(name, {})
    label = detail.get("label")
    return str(label) if label else name.replace("_", " ").title()


def _section_scope(status: OnboardingStatus, name: str) -> str:
    detail = status.section_details.get(name, {})
    return "Required" if detail.get("required") else "Optional"


def _status_display(state: SectionStatus) -> str:
    return _STATUS_DISPLAY.get(state, state.value)


def _section_status_display(status: OnboardingStatus, name: str) -> str:
    state = status.sections[name]
    if (
        name == "router"
        and state is SectionStatus.OK
        and _section_detail(status, name) == "uses SquillaRouter after provider setup"
    ):
        return "Provider first"
    return _status_display(state)


def _section_status_style(state: SectionStatus, display: str) -> str:
    if display == "Provider first":
        return "yellow"
    return _STATUS_STYLE.get(state, "")


def _section_detail(status: OnboardingStatus, name: str) -> str:
    detail = str(status.section_details.get(name, {}).get("detail") or "")
    if detail:
        return detail
    if name == "llm":
        return _LLM_SOURCE_DISPLAY.get(status.llm_source, status.llm_source)
    if name == "search":
        return "configured" if status.search_configured else "not configured"
    if name == "image_generation" and status.image_generation_provider:
        source = _IMAGE_SOURCE_DISPLAY.get(
            status.image_generation_source,
            status.image_generation_source,
        )
        return (
            f"{status.image_generation_provider} "
            f"({source})"
        ).strip()
    if name == "image_generation":
        return "disabled" if not status.image_generation_enabled else "not configured"
    if name == "channels":
        return f"{status.channel_count} configured"
    if name == "memory_embedding":
        return status.memory_embedding_provider
    return ""


def _print_env_reference_warnings(config) -> None:
    for warning in env_reference_warnings(config):
        console.print(warning_panel(warning))


def _print_saved_path(path: object) -> None:
    console.print(
        f"[bold {ACCENT}]◆[/] [bold]saved[/] "
        f"[dim]→[/] [{ACCENT_SOFT}]{markup_escape(path)}[/]",
        soft_wrap=True,
    )


def _format_missing_sections(status: OnboardingStatus) -> str:
    parts = [
        f"{_section_label(status, name)} ({_status_display(state)})"
        for name, state in status.sections.items()
        if state in _STATUS_BLOCKING
    ]
    return ", ".join(parts) if parts else "none"


def _optional_action_sections(status: OnboardingStatus) -> list[str]:
    return [
        name
        for name, state in status.sections.items()
        if not status.section_details.get(name, {}).get("required")
        and not status.section_details.get(name, {}).get("blocking")
        and (
            status.section_details.get(name, {}).get("actionRequired")
            or state is not SectionStatus.OK
        )
    ]


def _format_action_sections(status: OnboardingStatus, names: list[str]) -> str:
    parts = [
        f"{_section_label(status, name)} ({_status_display(status.sections[name])})"
        for name in names
    ]
    return ", ".join(parts) if parts else "none"


def _format_config_load_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if errors:
            first = errors[0]
            loc = ".".join(str(part) for part in first.get("loc", ()))
            msg = str(first.get("msg") or "invalid value")
            return f"{loc}: {msg}" if loc else msg
    return str(exc).splitlines()[0]


def _format_validation_error(exc: ValidationError) -> str:
    """Render a ValidationError as a field-naming summary.

    Never echoes pydantic's ``input_value`` dump — a mispasted secret must
    not surface on stderr. Only field paths and validator messages appear,
    and the free-text redactor runs as a final guard so a validator message
    that interpolates the offending value cannot leak a secret either
    (mirrors the channel-entry formatter in ``onboarding.mutations``).
    """
    parts: list[str] = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        loc = ".".join(str(item) for item in error.get("loc", ()) or ())
        msg = str(error.get("msg") or "invalid value")
        parts.append(f"{loc}: {msg}" if loc else msg)
    detail = "; ".join(parts) or "invalid value"
    return redact_error_text(detail, max_len=500)


def _exit_cancelled(exc: BaseException) -> NoReturn:
    """Productize a wizard cancellation (Esc/Ctrl+C) at the CLI boundary."""
    error_console.print(
        "[yellow]Setup cancelled[/yellow] "
        "[dim]— rerun `opensquilla onboard` when you are ready.[/dim]"
    )
    raise typer.Exit(code=_CANCELLED_EXIT_CODE) from exc


def _print_restart_guidance(result: object, config_path: Path | None = None) -> None:
    """Surface ``PersistResult.restart_required`` instead of discarding it."""
    if not getattr(result, "restart_required", False):
        return
    config_arg = _config_cli_arg(config_path)
    console.print(
        f"[{ACCENT_SOFT}]◆[/] [bold]restart required[/] "
        "[dim]— apply this change with[/dim] "
        f"[{ACCENT_SOFT}]opensquilla gateway restart{markup_escape(config_arg)}[/]",
        soft_wrap=True,
    )


def _exit_config_load_error(exc: Exception, path: str | Path | None = None) -> NoReturn:
    target, _ = resolve_config_path(path)
    config_arg = _config_cli_arg(target)
    error_console.print(
        f"[red]OpenSquilla config error:[/red] {markup_escape(str(target))}"
    )
    error_console.print(
        f"[dim]Reason: {markup_escape(_format_config_load_error(exc))}[/dim]"
    )
    error_console.print("[dim]Fix: edit or move this config, then rerun onboarding:[/dim]")
    error_console.print(
        f"  [{ACCENT_SOFT}]opensquilla onboard --if-needed"
        f"{markup_escape(config_arg)}[/]"
    )
    raise typer.Exit(code=2) from exc


def _load_config_for_cli(path: str | Path | None = None):
    try:
        return load_config(path)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
        _exit_config_load_error(exc, path)


def _has_stored_setup_state(cfg) -> bool:
    """True when the loaded config already carries provider or router state.

    Gates the keep-current default of ``onboard --router``: a re-save over an
    install with explicit ``[llm]`` or ``[squilla_router]`` state must not
    re-apply a router profile, while a fresh install keeps today's
    recommended-profile first-run behavior. Env-absorbed credentials (marked
    runtime secrets) do not count — a key exported in the shell says nothing
    about provider/model/router preferences.
    """
    secret_paths: set[str] = getattr(cfg, "_runtime_secret_paths", set()) or set()
    llm_fields = {
        name for name in cfg.llm.model_fields_set if f"llm.{name}" not in secret_paths
    }
    return bool(llm_fields or cfg.squilla_router.model_fields_set)


def _format_section_names(status: OnboardingStatus, names: list[str]) -> str:
    return ", ".join(_section_label(status, name) for name in names) if names else "none"


def _status_cockpit_summary(status: OnboardingStatus) -> str:
    blocking = [
        name
        for name in status.sections
        if status.section_details.get(name, {}).get("blocking")
    ]
    optional_later = [
        name
        for name, state in status.sections.items()
        if not status.section_details.get(name, {}).get("required")
        and not status.section_details.get(name, {}).get("blocking")
        and state is not SectionStatus.OK
    ]
    return (
        f"Blocking setup: {_format_section_names(status, blocking)}"
        f" · Optional later: {_format_section_names(status, optional_later)}"
    )


def _config_cli_arg(config_path: Path | None) -> str:
    if config_path is None:
        return ""
    # Platform-appropriate quoting: PowerShell on Windows, POSIX elsewhere.
    return f" --config {quote_cli_arg(config_path)}"


def _headless_section_paths(
    names: list[str],
    config_arg: str,
) -> list[tuple[str, str, str]]:
    paths: list[tuple[str, str, str]] = []
    seen_commands: set[str] = set()
    for name in names:
        entries = headless_setup_commands(name)
        if not entries:
            continue
        for label, command in entries:
            if command in seen_commands:
                continue
            seen_commands.add(command)
            paths.append((label, f"{command}{config_arg}", ""))
    return paths


def _missing_env_paths(status: OnboardingStatus) -> list[tuple[str, str, str]]:
    return [
        (str(entry["label"]), human_env_command(str(entry["command"])), "")
        for entry in env_recovery_commands(status)
    ]


def _has_blocking_env_recovery(status: OnboardingStatus) -> bool:
    for entry in env_recovery_commands(status):
        section = str(entry.get("section") or "")
        if status.section_details.get(section, {}).get("blocking"):
            return True
    return False


def _status_setup_paths(
    status: OnboardingStatus,
    cfg,
    config_path: Path | None,
) -> list[tuple[str, str, str]]:
    config_arg = _config_cli_arg(config_path)
    paths = _missing_env_paths(status)
    paths.append(
        (
            "Guided CLI",
            f"opensquilla onboard --if-needed{config_arg}",
            "",
        ),
    )
    setup_url = web_setup_url(cfg)
    if setup_url:
        web_label = (
            "Web UI after env fix" if _has_blocking_env_recovery(status) else "Web UI"
        )
        paths.append(
            (
                web_label,
                f"opensquilla gateway run{config_arg}",
                f" -> {setup_url}",
            )
        )
    catalog_label, catalog_command = setup_catalog_command(config_arg)
    paths.append((catalog_label, catalog_command, ""))
    blocking = [
        name
        for name, state in status.sections.items()
        if state in _STATUS_BLOCKING
        or status.section_details.get(name, {}).get("blocking")
    ]
    paths.extend(_headless_section_paths(blocking, config_arg))
    return paths


def _optional_setup_paths(
    status: OnboardingStatus,
    cfg,
    config_path: Path | None,
) -> list[tuple[str, str, str]]:
    config_arg = _config_cli_arg(config_path)
    paths: list[tuple[str, str, str]] = []
    setup_url = web_setup_url(cfg)
    if setup_url:
        paths.append(
            (
                "Web UI",
                f"opensquilla gateway run{config_arg}",
                f" -> {setup_url}",
            )
        )
    catalog_label, catalog_command = setup_catalog_command(config_arg)
    paths.append((catalog_label, catalog_command, ""))
    paths.extend(_headless_section_paths(_optional_action_sections(status), config_arg))
    return paths


def _ready_setup_paths(
    cfg,
    config_path: Path | None,
) -> list[tuple[str, str, str]]:
    config_arg = _config_cli_arg(config_path)
    setup_url = web_setup_url(cfg)
    return [
        (
            "Start gateway",
            f"opensquilla gateway run{config_arg}",
            f" -> {setup_url}" if setup_url else "",
        ),
        (
            "Reconfigure later",
            f"opensquilla onboard configure <section>{config_arg}",
            "",
        ),
    ]


def _print_status_path(label: str, command: str, suffix: str = "") -> None:
    console.print(
        f"  [dim]{label}:[/] "
        f"[{ACCENT_SOFT}]{markup_escape(command)}[/]"
        f"[dim]{markup_escape(suffix)}[/]",
        soft_wrap=True,
    )


def _print_optional_action_handoff(
    status: OnboardingStatus,
    cfg,
    config_path: Path | None,
) -> None:
    actions = _optional_action_sections(status)
    console.print(
        f"[{ACCENT_SOFT}]◆[/] [bold]core setup is ready[/]; "
        "[bold]optional capabilities need action:[/] "
        f"{markup_escape(_format_action_sections(status, actions))}",
        soft_wrap=True,
    )
    console.print("[bold]Optional next moves:[/]")
    for label, command, suffix in _optional_setup_paths(status, cfg, config_path):
        _print_status_path(label, command, suffix)


def _probe_saved_provider(cfg) -> bool:
    """Run a small live probe against the just-saved provider config.

    Only invoked behind ``--probe`` on the non-interactive provider path so CI
    can gate on real credentials; the default path never touches the network.
    Any failure (classified or unexpected) prints a redacted detail and
    returns False so the caller can exit non-zero.
    """
    import asyncio

    from opensquilla.onboarding.probe import probe_llm_provider

    llm = cfg.llm
    console.print(f"[{ACCENT_SOFT}]◆[/] Checking the connection…")
    try:
        result = asyncio.run(
            probe_llm_provider(
                provider_id=str(getattr(llm, "provider", "") or ""),
                model=str(getattr(llm, "model", "") or ""),
                api_key=str(getattr(llm, "api_key", "") or ""),
                api_key_env=str(getattr(llm, "api_key_env", "") or ""),
                base_url=str(getattr(llm, "base_url", "") or ""),
                proxy=str(getattr(llm, "proxy", "") or ""),
            )
        )
    except Exception as exc:  # noqa: BLE001 - probe outcome maps to exit code
        error_console.print(f"[red]Probe failed:[/red] {markup_escape(str(exc))}")
        return False
    if result.ok:
        console.print(
            f"[bold {ACCENT}]◆[/] [bold]Probe OK[/] "
            f"[dim]·[/] [{ACCENT_SOFT}]{markup_escape(result.provider_id)}[/]"
            f"[dim]/[/]{markup_escape(result.model)}"
        )
        return True
    kind = result.failure_kind or "error"
    detail = result.message or "the provider did not accept the request"
    error_console.print(
        f"[red]Probe failed ({markup_escape(kind)}):[/red] {markup_escape(detail)}"
    )
    return False


onboard_app = typer.Typer(
    help=(
        "OpenSquilla setup cockpit for providers, SquillaRouter, "
        "channels, search, images, and memory."
    ),
    invoke_without_command=True,
    no_args_is_help=False,
)


@onboard_app.callback(invoke_without_command=True)
def onboard_command(
    ctx: typer.Context,
    provider: str = typer.Option("", "--provider", help="Provider id to configure."),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Model id for the provider (unchanged if omitted on a re-save).",
    ),
    api_key: str = typer.Option("", "--api-key", help="Provider key to store in config."),
    api_key_env: str = typer.Option(
        "",
        "--api-key-env",
        help="Read the provider key from this environment variable.",
    ),
    base_url: str | None = typer.Option(
        None,
        "--base-url",
        help="Custom provider base URL (unchanged if omitted on a re-save).",
    ),
    proxy: str | None = typer.Option(
        None,
        "--proxy",
        help="Explicit HTTP proxy URL for upstream calls (unchanged if omitted on a re-save).",
    ),
    router: str | None = typer.Option(
        None,
        "--router",
        metavar="MODE",
        help=(
            "Router profile: recommended, openrouter-mix, or disabled. "
            "Unchanged if omitted on a re-save; recommended on first setup."
        ),
    ),
    minimal: bool = typer.Option(False, "--minimal", help="Keep interactive setup to core fields."),
    skip_channels: bool = typer.Option(
        False,
        "--skip-channels",
        help="Leave channel setup for later.",
    ),
    skip_search: bool = typer.Option(
        False,
        "--skip-search",
        help="Leave optional Web search setup for later.",
    ),
    skip_image_generation: bool = typer.Option(
        False,
        "--skip-image-generation",
        help="Leave optional image generation setup for later.",
    ),
    skip_migration: bool = typer.Option(
        False,
        "--skip-migration",
        help="Skip legacy OpenClaw/Hermes import prompts.",
    ),
    if_needed: bool = typer.Option(
        False,
        "--if-needed",
        help="Only run the wizard when required setup is incomplete.",
    ),
    probe: bool = typer.Option(
        False,
        "--probe",
        help=(
            "With --provider: verify the saved provider with a small live "
            "probe and exit non-zero on probe failure (CI-friendly)."
        ),
    ),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    """Run first-run onboarding (interactive or non-interactive)."""
    if ctx.invoked_subcommand is not None:
        # ``opensquilla onboard <subcommand>`` was invoked; let the subcommand
        # handler take over instead of running the interactive flow.
        return
    if if_needed:
        cfg = _load_config_for_cli(config_path)
        status = get_onboarding_status(cfg)
        if status.has_config and not status.needs_onboarding:
            if _optional_action_sections(status):
                _print_optional_action_handoff(status, cfg, config_path)
                raise typer.Exit(code=0)
            console.print(
                f"[{ACCENT_SOFT}]◆[/] [bold]onboarding already complete[/]"
                " [dim]— nothing to do[/dim]"
            )
            raise typer.Exit(code=0)
        # Tell the operator what is still pending so it is obvious why the
        # idempotent gate did not short-circuit.
        if status.has_config:
            console.print(
                f"[{ACCENT_SOFT}]◆[/] [bold]onboarding has unfinished sections:[/] "
                f"{markup_escape(_format_missing_sections(status))}"
            )

    if provider:
        # Productize config-file load failures up front so a corrupt config
        # surfaces as the standard config-error handoff instead of leaking a
        # mutation-shaped error (or a raw traceback) below. The loaded config
        # feeds the SetupEngine directly, so this path parses the file once
        # before the save instead of loading, discarding, and loading again.
        cfg_before = _load_config_for_cli(config_path)
        # Keep-current router semantics: an omitted --router never touches
        # the stored router state on an already-configured install (a key
        # rotation must not re-enable a disabled router or rewrite a custom
        # tier ladder/model); on a fresh install it keeps today's first-run
        # behavior and applies the recommended profile.
        router_mode = router
        if router_mode is None and not _has_stored_setup_state(cfg_before):
            router_mode = "recommended"
        try:
            engine = SetupEngine(cfg_before, path=config_path)
            engine.apply(
                "provider",
                {
                    "providerId": provider,
                    "model": model,
                    "apiKey": api_key,
                    "apiKeyEnv": api_key_env,
                    "baseUrl": base_url,
                    "proxy": proxy,
                },
            )
            if router_mode:
                engine.apply("router", {"mode": router_mode})
            result = engine.persist()
        except ValidationError as exc:
            # Handle before ValueError (its base class): pydantic's message
            # embeds input_value, which can echo a secret pasted into the
            # wrong field. Print a redacted, field-naming summary instead.
            error_console.print(
                "[red]Error:[/red] invalid provider configuration: "
                f"{markup_escape(_format_validation_error(exc))}"
            )
            raise typer.Exit(code=2) from exc
        except (KeyError, TypeError, ValueError) as exc:
            error_console.print(f"[red]Error:[/red] {markup_escape(str(exc))}")
            if "model is required" in str(exc):
                error_console.print(
                    "[dim]Hint: pass --model <model-id> for providers without "
                    "a router default, or run `opensquilla onboard` for guided "
                    "prompts.[/dim]"
                )
            raise typer.Exit(code=2) from exc
        console.print(
            banner_panel(
                "OpenSquilla Setup Handoff",
                provider,
            )
        )
        _print_restart_guidance(result, config_path)
        cfg = _load_config_for_cli(result.path)
        _print_env_reference_warnings(cfg)
        console.print(
            format_next_steps(cfg, config_path=result.path),
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
        if probe and not _probe_saved_provider(cfg):
            raise typer.Exit(code=1)
        return

    # Pre-validate the config load so a corrupt or unreadable file surfaces
    # as the standard config-error handoff BEFORE the wizard collects any
    # input; the wizard-phase handlers below then never have to guess
    # whether an I/O failure was a load or a write problem. The --if-needed
    # gate above already load-validated the config on its way here.
    if not if_needed:
        _load_config_for_cli(config_path)
    options = OnboardOptions(
        skip_channels=skip_channels,
        skip_search=skip_search,
        skip_image_generation=skip_image_generation,
        if_needed=if_needed,
        provider_id=provider or None,
        model=model or None,
        api_key=api_key or None,
        api_key_env=api_key_env or None,
        base_url=base_url or None,
        proxy=proxy or None,
        # None = keep-current router on a re-save; the wizard resolves the
        # first-run default itself.
        router_mode=router,
        minimal=minimal,
        skip_migration=skip_migration,
        config_path=config_path,
    )
    try:
        result = run_interactive_onboard(options)
    except (UserCancelledError, KeyboardInterrupt, EOFError) as exc:
        # EOFError covers Ctrl+D / exhausted piped stdin at a prompt — the
        # same operator intent as Esc/Ctrl+C, so the same productized exit.
        _exit_cancelled(exc)
    except (tomllib.TOMLDecodeError, ValidationError) as exc:
        # Corrupt config discovered mid-wizard (e.g. re-loaded after an
        # out-of-band edit): route through the same productized handoff as
        # `--if-needed`/`status` instead of a raw traceback.
        _exit_config_load_error(exc, config_path)
    except OSError as exc:
        # The config loaded fine above, so an OSError out of the wizard is a
        # runtime I/O failure (disk full at persist time, permissions revoked
        # mid-wizard) — not a config-load problem, and the load-error "edit
        # or move this config" recovery advice would be wrong here.
        error_console.print(
            f"[red]Error:[/red] setup hit a file I/O error: {markup_escape(str(exc))}"
        )
        error_console.print(
            "[dim]Check disk space and permissions for the config directory, "
            "then rerun `opensquilla onboard`.[/dim]"
        )
        raise typer.Exit(code=2) from exc
    except (KeyError, TypeError, ValueError) as exc:
        # Mutation-level validation failures (e.g. "model is required") must
        # exit like the headless path does, not as a raw traceback after the
        # operator already typed the credentials.
        error_console.print(f"[red]Error:[/red] {markup_escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    if "tty_required" in result.warnings:
        raise typer.Exit(code=2)
    console.print(
        banner_panel(
            "OpenSquilla Setup Handoff",
            str(result.path),
        )
    )
    _print_restart_guidance(result, config_path)
    cfg = _load_config_for_cli(result.path)
    _print_env_reference_warnings(cfg)
    console.print(
        format_next_steps(cfg, config_path=result.path),
        markup=False,
        highlight=False,
        soft_wrap=True,
    )


_STATUS_STYLE: dict[SectionStatus, str] = {
    SectionStatus.OK: "green",
    SectionStatus.OPTIONAL: "dim",
    SectionStatus.MISSING: "yellow",
    SectionStatus.DEGRADED: "yellow",
    SectionStatus.UNKNOWN: "red",
}


def _status_payload(status: OnboardingStatus) -> dict:
    """Machine-readable ``onboard status --json`` payload.

    Superset contract: every key of the RPC ``onboarding.status`` payload
    (contract-frozen in tests/test_contracts/test_onboarding_status.py) must
    appear here under the same name so scripts can consume either surface;
    ``sectionAliases`` plus the ``provider`` alias entries are the only
    CLI-side additions. Pinned by tests/test_cli/test_onboard_status_json.py.
    """
    sections = {name: state.value for name, state in status.sections.items()}
    section_details = {name: dict(detail) for name, detail in status.section_details.items()}
    if "llm" in sections:
        sections["provider"] = sections["llm"]
    if "llm" in section_details:
        section_details["provider"] = dict(section_details["llm"])

    return {
        "configPath": status.config_path,
        "hasConfig": status.has_config,
        "needsOnboarding": status.needs_onboarding,
        "sections": sections,
        "sectionDetails": section_details,
        "sectionAliases": {"llm": "provider"},
        "llmConfigured": status.llm_configured,
        "llmSource": status.llm_source,
        "llmEnvKey": status.llm_env_key,
        "llmCredentialStatus": dict(status.llm_credential_status),
        "llmProfileStatus": [dict(row) for row in status.llm_profile_status],
        "searchConfigured": status.search_configured,
        "searchProvider": status.search_provider,
        "searchSource": status.search_source,
        "searchEnvKey": status.search_env_key,
        "imageGenerationConfigured": status.image_generation_configured,
        "imageGenerationEnabled": status.image_generation_enabled,
        "imageGenerationSource": status.image_generation_source,
        "imageGenerationProvider": status.image_generation_provider,
        "imageGenerationPrimary": status.image_generation_primary,
        "imageGenerationEnvKey": status.image_generation_env_key,
        "audioConfigured": status.audio_configured,
        "audioEnabled": status.audio_enabled,
        "audioSource": status.audio_source,
        "audioProvider": status.audio_provider,
        "audioEnvKey": status.audio_env_key,
        "memoryEmbeddingConfigured": status.memory_embedding_configured,
        "memoryEmbeddingProvider": status.memory_embedding_provider,
        "memoryEmbeddingSource": status.memory_embedding_source,
        "memoryEmbeddingEnvKey": status.memory_embedding_env_key,
        "ensembleCredentialStatus": list(status.ensemble_credential_status),
        "envRecoveryCommands": env_recovery_commands(status),
        "channelCount": status.channel_count,
        "channelsConfigured": status.channels_configured,
        "warnings": list(status.warnings),
        # Shared with the RPC payload as a frozen compatibility key. Migration
        # discovery is settings-only, so this remains null.
        "legacyData": legacy_data_payload(),
    }


def _catalog_count(value: object) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict) and isinstance(value.get("profiles"), list):
        return len(value["profiles"])
    return 1


_CATALOG_COMMANDS = {
    "providers": (
        "opensquilla onboard configure provider --provider <id> --model <model> "
        "--api-key-env <ENV_NAME>"
    ),
    "routerProfiles": (
        "opensquilla onboard configure router --router recommended "
        f"--default-tier {DEFAULT_TEXT_TIER}"
    ),
    "searchProviders": (
        "opensquilla onboard configure search --search-provider <provider> "
        "--api-key-env <ENV_NAME>"
    ),
    "channels": (
        "opensquilla onboard configure channels --channel-type <type> --name <name> "
        "--field key=value"
    ),
    "imageGenerationProviders": (
        "opensquilla onboard configure image --image-provider <provider> "
        "--primary <model> --api-key-env <ENV_NAME>"
    ),
    # Voice audio has no headless configure recipe yet; the Web UI setup page
    # is the supported configuration surface.
    "audioProviders": "opensquilla gateway run",
    "memoryEmbeddingProviders": (
        "opensquilla onboard configure memory --memory-provider <provider> "
        "--model <model> --api-key-env <ENV_NAME>"
    ),
}

_CATALOG_SECTION_COMMANDS = {
    "providers": "opensquilla onboard catalog providers",
    "routerProfiles": "opensquilla onboard catalog router",
    "searchProviders": "opensquilla onboard catalog search",
    "channels": "opensquilla onboard catalog channels",
    "imageGenerationProviders": "opensquilla onboard catalog image",
    "audioProviders": "opensquilla onboard catalog audio",
    "memoryEmbeddingProviders": "opensquilla onboard catalog memory",
}

_CATALOG_TITLES = {
    "providers": "Text providers",
    "routerProfiles": "SquillaRouter profiles",
    "searchProviders": "Web search providers",
    "channels": "Channel types",
    "imageGenerationProviders": "Image generation providers",
    "audioProviders": "Voice audio providers",
    "memoryEmbeddingProviders": "Memory embedding providers",
}


def _catalog_value(row: dict[str, object], key: str, fallback: str = "") -> str:
    value = row.get(key)
    if value is None or value == "":
        return fallback
    return str(value)


def _catalog_key_requirement(row: dict[str, object]) -> str:
    if row.get("requiresApiKey"):
        return _catalog_value(row, "envKey", "API key")
    return "No key"


def _catalog_runtime(row: dict[str, object]) -> str:
    if row.get("runtimeSupported") is False:
        return "metadata only"
    return "ready"


def _catalog_field_summary(row: dict[str, object]) -> str:
    fields = row.get("fields")
    if not isinstance(fields, list):
        return ""
    required: list[str] = []
    for field in fields:
        if not isinstance(field, dict) or not field.get("required"):
            continue
        label = str(field.get("label") or field.get("name") or "")
        name = str(field.get("name") or "")
        required.append(f"{label} ({name})" if name else label)
    return ", ".join(required) if required else "No required fields"


def _print_catalog_line(text: str) -> None:
    console.print(text, soft_wrap=True)


def _catalog_command(name: str, config_arg: str = "") -> str:
    command = _CATALOG_COMMANDS.get(name, "")
    return f"{command}{config_arg}" if command else ""


def _catalog_section_command(name: str, config_arg: str = "") -> str:
    command = _CATALOG_SECTION_COMMANDS.get(name, "")
    return f"{command}{config_arg}" if command else ""


def _catalog_try_command(
    name: str,
    row: dict[str, object],
    config_arg: str = "",
) -> str:
    if name == "providers":
        provider_id = _catalog_value(row, "providerId", "<id>")
        model = _catalog_value(row, "defaultDirectModel", "<model>")
        command = (
            "opensquilla onboard configure provider "
            f"--provider {provider_id} --model {model}"
        )
        if row.get("requiresApiKey"):
            command += f" --api-key-env {_catalog_value(row, 'envKey', '<ENV_NAME>')}"
        if row.get("requiresBaseUrl"):
            command += f" --base-url {_catalog_value(row, 'defaultBaseUrl', '<base-url>')}"
        return f"{command}{config_arg}"

    if name == "searchProviders":
        if row.get("runtimeSupported") is False:
            return ""
        provider_id = _catalog_value(row, "providerId", "<provider>")
        command = f"opensquilla onboard configure search --search-provider {provider_id}"
        if row.get("requiresApiKey"):
            command += f" --api-key-env {_catalog_value(row, 'envKey', '<ENV_NAME>')}"
        return f"{command}{config_arg}"

    if name == "channels":
        channel_type = _catalog_value(row, "type", "<type>")
        command = (
            "opensquilla onboard configure channels "
            f"--channel-type {channel_type} --name <name>"
        )
        fields = row.get("fields")
        if isinstance(fields, list):
            for field in fields:
                if not isinstance(field, dict) or not field.get("required"):
                    continue
                field_name = str(field.get("name") or "")
                if not field_name or field_name == "name":
                    continue
                if field_name == "token":
                    command += " --token <token>"
                else:
                    command += f" --field {field_name}=<value>"
        return f"{command}{config_arg}"

    if name == "imageGenerationProviders":
        provider_id = _catalog_value(row, "providerId", "<provider>")
        model = _catalog_value(row, "defaultModel", "<model>")
        command = (
            "opensquilla onboard configure image "
            f"--image-provider {provider_id} --primary {model}"
        )
        if row.get("requiresApiKey"):
            command += f" --api-key-env {_catalog_value(row, 'envKey', '<ENV_NAME>')}"
        if row.get("requiresBaseUrl"):
            command += f" --base-url {_catalog_value(row, 'defaultBaseUrl', '<base-url>')}"
        return f"{command}{config_arg}"

    if name == "audioProviders":
        # No headless configure recipe exists for voice audio yet; point at
        # the gateway so the Web UI setup page can finish the job.
        return f"opensquilla gateway run{config_arg}"

    if name == "memoryEmbeddingProviders":
        provider_id = _catalog_value(row, "providerId", "<provider>")
        command = (
            "opensquilla onboard configure memory "
            f"--memory-provider {provider_id}"
        )
        if row.get("requiresApiKey"):
            command += f" --api-key-env {_catalog_value(row, 'envKey', '<ENV_NAME>')}"
        if provider_id == "openai-compatible":
            command += " --base-url <base-url> --model <model>"
        elif provider_id == "ollama":
            command += " --model <model>"
        return f"{command}{config_arg}"

    return _catalog_command(name, config_arg)


def _print_catalog_try_command(
    name: str,
    row: dict[str, object],
    config_arg: str = "",
) -> None:
    command = _catalog_try_command(name, row, config_arg)
    if command:
        _print_catalog_line(f"  Try: {command}")
    else:
        _print_catalog_line("  Try: not configurable in this build")


def _print_catalog_recipe_hint() -> None:
    console.print("Copy a Try line; key flags appear only when that option needs them.")


def _catalog_provider_route(row: dict[str, object]) -> str:
    return "SquillaRouter ready" if row.get("routerSupported") else "Direct only"


def _print_list_catalog(
    name: str,
    rows: list[dict[str, object]],
    config_arg: str = "",
) -> None:
    if name == "providers":
        console.print("[bold]OpenSquilla text provider options[/bold]")
        _print_catalog_recipe_hint()
        for row in rows:
            _print_catalog_line(
                f"- {_catalog_value(row, 'providerId')}: {_catalog_value(row, 'label')}"
                f" | route {_catalog_provider_route(row)}"
                f" | key {_catalog_key_requirement(row)}"
                f" | default {_catalog_value(row, 'defaultDirectModel', 'custom')}"
            )
            _print_catalog_try_command(name, row, config_arg)
        return

    if name == "searchProviders":
        console.print("[bold]OpenSquilla Web search provider options[/bold]")
        _print_catalog_recipe_hint()
        for row in rows:
            _print_catalog_line(
                f"- {_catalog_value(row, 'providerId')}: {_catalog_value(row, 'label')}"
                f" | {_catalog_runtime(row)}"
                f" | key {_catalog_key_requirement(row)}"
                f" | {_catalog_value(row, 'deployment')}"
            )
            _print_catalog_try_command(name, row, config_arg)
        return

    if name == "channels":
        console.print("[bold]OpenSquilla channel type options[/bold]")
        _print_catalog_recipe_hint()
        for row in rows:
            channel_type = _catalog_value(row, "type")
            _print_catalog_line(
                f"- {channel_type}: {_catalog_value(row, 'label')}"
                f" | {_catalog_value(row, 'transport')}"
                f" | fields {_catalog_field_summary(row)}"
                f" | guide opensquilla channels describe {channel_type} --json"
            )
            _print_catalog_try_command(name, row, config_arg)
        return

    if name == "imageGenerationProviders":
        console.print("[bold]OpenSquilla image generation provider options[/bold]")
        _print_catalog_recipe_hint()
        for row in rows:
            _print_catalog_line(
                f"- {_catalog_value(row, 'providerId')}: {_catalog_value(row, 'label')}"
                f" | key {_catalog_key_requirement(row)}"
                f" | default {_catalog_value(row, 'defaultModel', 'custom')}"
            )
            _print_catalog_try_command(name, row, config_arg)
        return

    if name == "audioProviders":
        console.print("[bold]OpenSquilla voice audio provider options[/bold]")
        console.print(
            "Configure voice audio from the Web UI setup page after starting "
            "the gateway with the Try command."
        )
        for row in rows:
            _print_catalog_line(
                f"- {_catalog_value(row, 'providerId')}: {_catalog_value(row, 'label')}"
                f" | {_catalog_value(row, 'deployment')}"
                f" | key {_catalog_key_requirement(row)}"
                f" | tts {_catalog_value(row, 'defaultTtsModel', 'custom')}"
            )
            _print_catalog_try_command(name, row, config_arg)
        return

    if name == "memoryEmbeddingProviders":
        console.print("[bold]OpenSquilla memory embedding provider options[/bold]")
        _print_catalog_recipe_hint()
        for row in rows:
            _print_catalog_line(
                f"- {_catalog_value(row, 'providerId')}: {_catalog_value(row, 'label')}"
                f" | {_catalog_value(row, 'deployment')}"
                f" | key {_catalog_key_requirement(row)}"
            )
            _print_catalog_try_command(name, row, config_arg)
        return


def _router_tier_summary(profile: dict[str, object]) -> str:
    tiers = profile.get("tiers")
    if not isinstance(tiers, dict):
        return ""
    summary: list[str] = []
    for tier in TEXT_TIERS:
        tier_spec = tiers.get(tier)
        if isinstance(tier_spec, dict):
            summary.append(f"{tier}: {tier_spec.get('model', '')}")
    return "; ".join(summary)


def _print_router_catalog(catalog: dict[str, object], config_arg: str = "") -> None:
    modes = catalog.get("modes")
    if isinstance(modes, list):
        console.print("[bold]OpenSquilla router modes[/bold]")
        for row in modes:
            if not isinstance(row, dict):
                continue
            _print_catalog_line(
                f"- {_catalog_value(row, 'mode')}: {_catalog_value(row, 'label')}"
                f" | {_catalog_value(row, 'description')}"
            )

    profiles = catalog.get("profiles")
    if isinstance(profiles, list):
        console.print("[bold]OpenSquilla provider tier profiles[/bold]")
        for row in profiles:
            if not isinstance(row, dict):
                continue
            _print_catalog_line(
                f"- {_catalog_value(row, 'profileId')}: {_catalog_value(row, 'label')}"
                f" | {_router_tier_summary(row)}"
            )
    console.print("Copy a Try line to keep the default OpenSquilla router profile.")
    _print_catalog_line(f"  Try: {_catalog_command('routerProfiles', config_arg)}")


def _print_focused_catalog(name: str, value: object, config_arg: str = "") -> None:
    if name == "routerProfiles" and isinstance(value, dict):
        _print_router_catalog(value, config_arg)
        return
    if isinstance(value, list):
        rows = [row for row in value if isinstance(row, dict)]
        _print_list_catalog(name, rows, config_arg)
        return


@onboard_app.command("catalog")
def onboard_catalog_command(
    section: str = typer.Argument(
        "",
        metavar="SECTION",
        help=(
            "Optional section: providers, router, search, channels, "
            "image (alias for image-generation), or memory "
            "(alias for memory-embedding)."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    config_path: Path | None = typer.Option(
        None,
        "--config",
        help="Accepted for copyable setup paths.",
    ),
) -> None:
    """List onboarding setup options for every configurable section.

    Aliases: image (alias for image-generation), memory (alias for memory-embedding).
    """
    config_arg = _config_cli_arg(config_path)
    try:
        payload = setup_catalog_payload(section or None)
    except ValueError as exc:
        error_console.print(f"[red]Error:[/red] {markup_escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    if json_output:
        print_json(payload)
        return

    if section and len(payload) == 1:
        name, value = next(iter(payload.items()))
        _print_focused_catalog(name, value, config_arg)
        return

    table = Table(title="OpenSquilla setup catalog")
    table.add_column("Section")
    table.add_column("Options", justify="right")
    table.add_column("Open section", overflow="fold")
    for name, value in payload.items():
        table.add_row(
            _CATALOG_TITLES.get(name, name),
            str(_catalog_count(value)),
            _catalog_section_command(name, config_arg),
        )
    console.print(table)
    console.print("Open a section for option-specific Try commands:")
    for name in payload:
        title = _CATALOG_TITLES.get(name, name)
        _print_catalog_line(f"  {title}: {_catalog_section_command(name, config_arg)}")
    console.print(
        "Tip: start with `opensquilla onboard catalog providers` for the required "
        "text provider."
    )


@onboard_app.command("status")
def onboard_status_command(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    """Print readiness of every onboarding section without mutating state."""
    cfg = _load_config_for_cli(config_path)
    status = get_onboarding_status(cfg)

    if json_output:
        typer.echo(_json.dumps(_status_payload(status), ensure_ascii=False))
        return

    console.print(banner_panel("OpenSquilla Setup Cockpit", _status_cockpit_summary(status)))
    table = Table(title="OpenSquilla setup readiness", show_header=True)
    table.add_column("Section")
    table.add_column("Scope")
    table.add_column("Status")
    table.add_column("Detail")
    for name, state in status.sections.items():
        display = _section_status_display(status, name)
        style = _section_status_style(state, display)
        table.add_row(
            _section_label(status, name),
            _section_scope(status, name),
            f"[{style}]{display}[/]" if style else display,
            _section_detail(status, name),
        )
    console.print(table)
    console.print("[dim]Action guide:[/]")
    console.print("[dim]  Fix: blocked or action-required[/]")
    console.print("[dim]  Review: ready[/]")
    console.print("[dim]  Configure: optional later[/]")
    console.print(
        f"[bold]OpenSquilla ready:[/] "
        f"{'no' if status.needs_onboarding else 'yes'}"
    )
    if status.needs_onboarding:
        paths = _status_setup_paths(status, cfg, config_path)
        fix_paths = _missing_env_paths(status)
        if fix_paths:
            console.print("[bold]Fix now:[/]")
            for label, command, suffix in fix_paths:
                _print_status_path(label, command, suffix)
            setup_paths = paths[len(fix_paths) :]
            if setup_paths:
                console.print("[bold]Setup paths:[/]")
                for label, command, suffix in setup_paths:
                    _print_status_path(label, command, suffix)
        else:
            recommended_label, recommended_command, recommended_suffix = paths[0]
            console.print("[bold]Recommended next move:[/]")
            _print_status_path(
                recommended_label,
                recommended_command,
                recommended_suffix,
            )
            alternatives = paths[1:]
            if alternatives:
                console.print("[bold]Setup paths:[/]")
                for label, command, suffix in alternatives:
                    _print_status_path(label, command, suffix)
        console.print(
            f"  [dim]Addressing:[/] "
            f"{markup_escape(_format_missing_sections(status))}"
        )
    elif _optional_action_sections(status):
        console.print("[bold]Optional next moves:[/]")
        for label, command, suffix in _optional_setup_paths(status, cfg, config_path):
            _print_status_path(label, command, suffix)
    else:
        console.print("[bold]Ready next moves:[/]")
        for label, command, suffix in _ready_setup_paths(cfg, config_path):
            _print_status_path(label, command, suffix)


# Canonical wizard slug per accepted configure section spelling. Anything
# outside this map is not configurable via `onboard configure` and exits 2.
_CONFIGURE_SECTION_SLUGS: dict[str, str] = {
    "provider": "provider",
    "providers": "provider",
    "router": "router",
    **{alias: "ensemble" for alias in ENSEMBLE_SECTION_ALIASES},
    "channel": "channels",
    "channels": "channels",
    "search": "search",
    **{alias: "image-generation" for alias in IMAGE_GENERATION_SECTION_ALIASES},
    **{alias: "memory-embedding" for alias in MEMORY_EMBEDDING_SECTION_ALIASES},
}
_CONFIGURE_SECTION_HINT = (
    "provider, router, ensemble, channels, search, image (alias for "
    "image-generation), or memory (alias for memory-embedding)"
)


def _exit_unknown_configure_section(selected: str, normalized: str) -> NoReturn:
    if normalized in AUDIO_SECTION_ALIASES:
        error_console.print(
            "[red]Error:[/red] voice audio has no headless configure recipe yet; "
            "view options with `opensquilla onboard catalog audio` and configure "
            "it from the Web UI setup page."
        )
    else:
        error_console.print(
            f"[red]Error:[/red] unknown configure section: {markup_escape(repr(selected))}"
        )
        error_console.print(f"[dim]Sections: {_CONFIGURE_SECTION_HINT}.[/dim]")
    raise typer.Exit(code=2)


def _missing_headless_gate_flags(
    normalized: str,
    given: dict[str, bool],
) -> list[str]:
    """Name the gate flag(s) a headless `configure <section>` call still needs.

    Called only when explicit flags were passed but no headless branch
    matched — silently dropping those flags (the pre-fix behavior) made
    `configure router --default-tier c2` a no-op with exit 0.
    """
    if normalized in {"provider", "providers"}:
        return ["--provider"]
    if normalized == "router":
        return ["--router"]
    if normalized in ENSEMBLE_SECTION_ALIASES:
        return ["--enabled/--disabled (or another ensemble flag)"]
    if normalized == "search":
        return ["--search-provider"]
    if normalized in {"channel", "channels"}:
        return [flag for flag in ("--channel-type", "--name") if not given[flag]]
    if normalized in IMAGE_GENERATION_SECTION_ALIASES:
        return ["--image-provider"]
    if normalized in MEMORY_EMBEDDING_SECTION_ALIASES:
        return ["--memory-provider"]
    return []


def _exit_incomplete_headless_flags(normalized: str, given: dict[str, bool]) -> NoReturn:
    missing = _missing_headless_gate_flags(normalized, given)
    flags = " and ".join(missing) if missing else "its gate flag"
    error_console.print(
        f"[red]Error:[/red] `configure {markup_escape(normalized)}` received "
        f"explicit flags but is missing {markup_escape(flags)}; no changes were "
        "made. Add the missing flag(s), or rerun without flags for the guided "
        "wizard."
    )
    raise typer.Exit(code=2)


@onboard_app.command("configure")
def configure_command(
    section_arg: str = typer.Argument(
        "",
        metavar="SECTION",
        help=(
            "Section to configure: provider, router, ensemble, channels, search, "
            "image (alias for image-generation), or memory "
            "(alias for memory-embedding)."
        ),
    ),
    section: str = typer.Option(
        "",
        "--section",
        help=(
            "Section to configure: provider, router, ensemble, channels, search, "
            "image (alias for image-generation), or memory "
            "(alias for memory-embedding)."
        ),
        rich_help_panel="Target section",
    ),
    provider: str = typer.Option(
        "",
        "--provider",
        help="Text provider id for provider setup.",
        rich_help_panel="Text provider",
    ),
    preset: str = typer.Option(
        "",
        "--preset",
        help="Explicitly apply this router preset id when saving the provider.",
        rich_help_panel="Text provider",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Model id for provider or remote memory embedding (unchanged if omitted).",
        rich_help_panel="Shared keys and endpoints",
    ),
    api_key: str = typer.Option(
        "",
        "--api-key",
        help="API key for provider, search, image generation, or memory embedding.",
        rich_help_panel="Shared keys and endpoints",
    ),
    api_key_env: str = typer.Option(
        "",
        "--api-key-env",
        help="Read that capability key from this environment variable.",
        rich_help_panel="Shared keys and endpoints",
    ),
    base_url: str | None = typer.Option(
        None,
        "--base-url",
        help=(
            "Custom upstream base URL for provider, image, or remote memory "
            "(unchanged if omitted)."
        ),
        rich_help_panel="Shared keys and endpoints",
    ),
    proxy: str | None = typer.Option(
        None,
        "--proxy",
        help=(
            "Explicit HTTP proxy URL for provider or search upstream calls "
            "(unchanged if omitted)."
        ),
        rich_help_panel="Shared keys and endpoints",
    ),
    router: str = typer.Option(
        "",
        "--router",
        help="recommended | openrouter-mix | disabled",
        rich_help_panel="Router",
    ),
    default_tier: str = typer.Option(
        "",
        "--default-tier",
        help=f"Default router text tier: {', '.join(TEXT_TIERS)}.",
        rich_help_panel="Router",
    ),
    enabled: bool | None = typer.Option(
        None,
        "--enabled/--disabled",
        help="Enable or disable the LLM ensemble (omit to keep current).",
        rich_help_panel="LLM ensemble",
    ),
    selection_mode: str = typer.Option(
        "",
        "--selection-mode",
        help=(
            "Ensemble selection mode: router_dynamic, static_openrouter_b5, "
            "static_tokenrhythm_b5, or custom_b5."
        ),
        rich_help_panel="LLM ensemble",
    ),
    model_options: str = typer.Option(
        "",
        "--model-options",
        help="Comma-separated ensemble model ids (omit to keep current).",
        rich_help_panel="LLM ensemble",
    ),
    min_successful_proposers: int | None = typer.Option(
        None,
        "--min-successful-proposers",
        help="Minimum successful proposers for the ensemble (omit to keep current).",
        rich_help_panel="LLM ensemble",
    ),
    all_failed_policy: str = typer.Option(
        "",
        "--all-failed-policy",
        help="Policy when all proposers fail: fallback_single or error.",
        rich_help_panel="LLM ensemble",
    ),
    search_provider: str = typer.Option(
        "",
        "--search-provider",
        help="Search provider id.",
        rich_help_panel="Search",
    ),
    max_results: int | None = typer.Option(
        None,
        "--max-results",
        help="Default Web search result limit (unchanged if omitted).",
        rich_help_panel="Search",
    ),
    use_env_proxy: bool | None = typer.Option(
        None,
        "--use-env-proxy/--no-use-env-proxy",
        help=(
            "Let Web search use HTTP(S)_PROXY from the gateway environment "
            "(unchanged if omitted)."
        ),
        rich_help_panel="Search",
    ),
    fallback_policy: str = typer.Option(
        "",
        "--fallback-policy",
        help="Search fallback policy: off or network (unchanged if omitted).",
        rich_help_panel="Search",
    ),
    diagnostics: bool | None = typer.Option(
        None,
        "--diagnostics/--no-diagnostics",
        help=(
            "Include search provider attempt/error details for troubleshooting "
            "(unchanged if omitted)."
        ),
        rich_help_panel="Search",
    ),
    channel_type: str = typer.Option(
        "",
        "--channel-type",
        help="Channel type such as slack, discord, feishu.",
        rich_help_panel="Channels",
    ),
    name: str = typer.Option(
        "",
        "--name",
        help="Channel instance name.",
        rich_help_panel="Channels",
    ),
    token: str = typer.Option(
        "",
        "--token",
        help="Channel token or bot secret.",
        rich_help_panel="Channels",
    ),
    fields: list[str] = typer.Option(
        [],
        "--field",
        "-f",
        help="Repeatable key=value channel field.",
        rich_help_panel="Channels",
    ),
    image_provider: str = typer.Option(
        "",
        "--image-provider",
        help="Image provider id.",
        rich_help_panel="Image generation",
    ),
    image_enabled: bool | None = typer.Option(
        None,
        "--image-enabled/--no-image-enabled",
        help="Enable or disable image generation (unchanged if omitted).",
        rich_help_panel="Image generation",
    ),
    primary: str = typer.Option(
        "",
        "--primary",
        help="Image model id.",
        rich_help_panel="Image generation",
    ),
    memory_provider: str = typer.Option(
        "",
        "--memory-provider",
        help="Memory embedding provider.",
        rich_help_panel="Memory embedding",
    ),
    onnx_dir: str = typer.Option(
        "",
        "--onnx-dir",
        help="Local embedding ONNX model directory.",
        rich_help_panel="Memory embedding",
    ),
    config_path: Path | None = typer.Option(
        None,
        "--config",
        help="Override config path.",
        rich_help_panel="Global",
    ),
) -> None:
    """Reconfigure provider, router, ensemble, channels, search, image generation, or memory."""
    selected = section or section_arg
    # Which headless flags the operator explicitly passed (None/""/[] means
    # "not given" — the None-defaulted options keep stored values downstream).
    given = {
        "--provider": bool(provider),
        "--preset": bool(preset),
        "--model": model is not None,
        "--api-key": bool(api_key),
        "--api-key-env": bool(api_key_env),
        "--base-url": base_url is not None,
        "--proxy": proxy is not None,
        "--router": bool(router),
        "--default-tier": bool(default_tier),
        "--enabled/--disabled": enabled is not None,
        "--selection-mode": bool(selection_mode),
        "--model-options": bool(model_options),
        "--min-successful-proposers": min_successful_proposers is not None,
        "--all-failed-policy": bool(all_failed_policy),
        "--search-provider": bool(search_provider),
        "--max-results": max_results is not None,
        "--use-env-proxy/--no-use-env-proxy": use_env_proxy is not None,
        "--fallback-policy": bool(fallback_policy),
        "--diagnostics/--no-diagnostics": diagnostics is not None,
        "--channel-type": bool(channel_type),
        "--name": bool(name),
        "--token": bool(token),
        "--field": bool(fields),
        "--image-provider": bool(image_provider),
        "--image-enabled/--no-image-enabled": image_enabled is not None,
        "--primary": bool(primary),
        "--memory-provider": bool(memory_provider),
        "--onnx-dir": bool(onnx_dir),
    }
    any_flag_given = any(given.values())
    wizard_section: str | None = None
    if selected:
        normalized = selected.strip().lower()
        wizard_section = _CONFIGURE_SECTION_SLUGS.get(normalized)
        if wizard_section is None:
            _exit_unknown_configure_section(selected, normalized)
        from opensquilla.onboarding.setup_engine import SetupEngine

        try:
            if normalized in {"provider", "providers"} and provider:
                engine = SetupEngine(path=config_path)
                engine.apply(
                    "provider",
                    {
                        "providerId": provider,
                        "model": model,
                        "apiKey": api_key,
                        "apiKeyEnv": api_key_env,
                        "baseUrl": base_url,
                        "proxy": proxy,
                        "presetId": preset,
                    },
                )
                result = engine.persist()
                _print_saved_path(result.path)
                _print_restart_guidance(result, config_path)
                _print_env_reference_warnings(_load_config_for_cli(result.path))
                return
            if normalized == "router" and router:
                engine = SetupEngine(path=config_path)
                engine.apply("router", {"mode": router, "defaultTier": default_tier})
                result = engine.persist()
                _print_saved_path(result.path)
                _print_restart_guidance(result, config_path)
                _print_env_reference_warnings(_load_config_for_cli(result.path))
                return
            ensemble_flags_given = (
                enabled is not None
                or bool(selection_mode)
                or bool(model_options)
                or min_successful_proposers is not None
                or bool(all_failed_policy)
            )
            if normalized in ENSEMBLE_SECTION_ALIASES and ensemble_flags_given:
                # Keep-current semantics: only flags the operator actually
                # passed reach the mutation; omitted flags map to None and
                # never touch the stored [llm_ensemble] values.
                parsed_model_options = [
                    piece.strip()
                    for piece in model_options.split(",")
                    if piece.strip()
                ]
                engine = SetupEngine(path=config_path)
                engine.apply(
                    "ensemble",
                    {
                        "enabled": enabled,
                        "selectionMode": selection_mode or None,
                        "modelOptions": parsed_model_options or None,
                        "minSuccessfulProposers": min_successful_proposers,
                        "allFailedPolicy": all_failed_policy or None,
                    },
                )
                result = engine.persist()
                _print_saved_path(result.path)
                _print_restart_guidance(result, config_path)
                return
            if normalized == "search" and search_provider:
                engine = SetupEngine(path=config_path)
                engine.apply(
                    "search",
                    {
                        "providerId": search_provider,
                        "apiKey": api_key,
                        "apiKeyEnv": api_key_env,
                        # None = keep the stored global search settings.
                        "maxResults": max_results,
                        "proxy": proxy,
                        "useEnvProxy": use_env_proxy,
                        "fallbackPolicy": fallback_policy or None,
                        "diagnostics": diagnostics,
                    },
                )
                result = engine.persist()
                _print_saved_path(result.path)
                _print_restart_guidance(result, config_path)
                _print_env_reference_warnings(_load_config_for_cli(result.path))
                return
            if normalized in {"channel", "channels"} and channel_type and name:
                from opensquilla.cli.channel_fields import (
                    apply_channel_token,
                    parse_channel_field_pairs,
                )

                engine = SetupEngine(path=config_path)
                entry = {"type": channel_type, "name": name}
                apply_channel_token(entry, channel_type, token)
                entry.update(parse_channel_field_pairs(fields, channel_type))
                engine.apply("channel", {"entry": entry})
                result = engine.persist()
                _print_saved_path(result.path)
                _print_restart_guidance(result, config_path)
                return
            if normalized in IMAGE_GENERATION_SECTION_ALIASES and (
                image_provider or image_enabled is False
            ):
                engine = SetupEngine(path=config_path)
                engine.apply(
                    "image-generation",
                    {
                        "providerId": image_provider,
                        "primary": primary,
                        "apiKey": api_key,
                        "apiKeyEnv": api_key_env,
                        "baseUrl": base_url,
                        # None = keep the stored enabled flag (a deliberate
                        # enabled=false survives a key rotation).
                        "enabled": image_enabled,
                    },
                )
                result = engine.persist()
                _print_saved_path(result.path)
                _print_restart_guidance(result, config_path)
                _print_env_reference_warnings(_load_config_for_cli(result.path))
                return
            if normalized in MEMORY_EMBEDDING_SECTION_ALIASES and memory_provider:
                engine = SetupEngine(path=config_path)
                engine.apply(
                    "memory-embedding",
                    {
                        "providerId": memory_provider,
                        "model": model,
                        "apiKey": api_key,
                        "apiKeyEnv": api_key_env,
                        "baseUrl": base_url,
                        "onnxDir": onnx_dir,
                    },
                )
                result = engine.persist()
                _print_saved_path(result.path)
                _print_restart_guidance(result, config_path)
                _print_env_reference_warnings(_load_config_for_cli(result.path))
                return
            if any_flag_given:
                # Explicit flags without the section's gate flag: refuse
                # instead of silently forwarding nothing to the wizard.
                _exit_incomplete_headless_flags(normalized, given)
        except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
            _exit_config_load_error(exc, config_path)
        except (KeyError, TypeError, ValueError) as exc:
            error_console.print(f"[red]Error:[/red] {markup_escape(exc)}")
            raise typer.Exit(code=2) from exc
    elif any_flag_given:
        error_console.print(
            "[red]Error:[/red] configuration flags need a target section, e.g. "
            "`opensquilla onboard configure router --router recommended`; "
            "no changes were made."
        )
        error_console.print(f"[dim]Sections: {_CONFIGURE_SECTION_HINT}.[/dim]")
        raise typer.Exit(code=2)

    try:
        interactive_result = run_interactive_configure(
            wizard_section, config_path=config_path
        )
    except (UserCancelledError, KeyboardInterrupt, EOFError) as exc:
        # EOFError covers Ctrl+D / exhausted piped stdin at a prompt — the
        # same operator intent as Esc/Ctrl+C, so the same productized exit.
        _exit_cancelled(exc)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
        _exit_config_load_error(exc, config_path)
    except (KeyError, TypeError, ValueError) as exc:
        # Mutation-level validation failures reachable from the wizard (e.g.
        # a blank required channel secret) must surface like the headless
        # boundary above — productized message, exit 2, no traceback.
        error_console.print(f"[red]Error:[/red] {markup_escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    if interactive_result is not None:
        _print_saved_path(interactive_result.path)
        _print_restart_guidance(interactive_result, config_path)
        return
    if not selected:
        # Bare interactive `onboard configure` can exit the hub without changes
        # via "Exit (nothing changed)"; that is a successful no-op, not the
        # non-TTY error path below.
        from opensquilla.onboarding.flow import _is_tty

        if _is_tty():
            return
    # ``run_interactive_configure`` returns ``None`` only after printing the
    # non-TTY hint; exit 2 like bare `onboard` so scripts see the failure.
    raise typer.Exit(code=2)
