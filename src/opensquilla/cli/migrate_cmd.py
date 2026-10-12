"""CLI commands for migration from external agent runtimes."""

from __future__ import annotations

import contextlib
import io
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import typer

from opensquilla.cli.ui import console
from opensquilla.migration.hermes import (
    MIGRATION_OPTIONS as HERMES_MIGRATION_OPTIONS,
)
from opensquilla.migration.hermes import (
    MIGRATION_PRESETS as HERMES_MIGRATION_PRESETS,
)
from opensquilla.migration.hermes import (
    SKILL_CONFLICT_MODES as HERMES_SKILL_CONFLICT_MODES,
)
from opensquilla.migration.hermes import (
    HermesMigrationOptions,
    HermesMigrator,
)
from opensquilla.migration.openclaw import (
    MIGRATION_OPTIONS,
    MIGRATION_PRESETS,
    PERSONA_CONFLICT_MODES,
    SKILL_CONFLICT_MODES,
    MigrationOptions,
    OpenClawMigrator,
)
from opensquilla.migration.opensquilla_home import (
    OPENSQUILLA_SOURCE_KINDS,
    OpenSquillaHomeMigrator,
    OpenSquillaMigrationOptions,
    PortableCandidate,
    detect_desktop_home,
    detect_legacy_cli_home,
    enumerate_portable_homes,
    inspect_opensquilla_home_candidate,
    is_valid_opensquilla_home,
)
from opensquilla.migration.orchestrator import (
    DetectedMigrationSource,
    detect_default_sources,
)
from opensquilla.paths import default_opensquilla_home

migrate_app = typer.Typer(
    help=(
        "Profile transfer for a supported OpenSquilla CLI or Desktop profile, "
        "historical Windows Portable data, and external agent runtimes."
    ),
    invoke_without_command=True,
    no_args_is_help=False,
)

_AUTO_DETECT_SOURCES: tuple[str, ...] = ("opensquilla", "openclaw", "hermes")


@contextlib.contextmanager
def _guard_foreign_migration_target() -> Iterator[None]:
    """Guard and lock a Desktop target without changing ordinary CLI behavior.

    The first Desktop inspection is deliberately read-only and happens before
    the lifecycle context can create the legacy ``gateway.pid.lock`` file.
    OpenSquilla self-import does not use this context: it owns a sorted,
    multi-profile source/target lock transaction internally.
    """

    from opensquilla.recovery import guard_desktop_profile, guarded_desktop_profile

    if guard_desktop_profile() is None:
        yield
        return
    with guarded_desktop_profile():
        yield


def _split_csv(values: list[str] | None) -> tuple[str, ...]:
    parsed: list[str] = []
    for value in values or []:
        for part in value.split(","):
            normalized = part.strip()
            if normalized:
                parsed.append(normalized)
    return tuple(parsed)


def _stdin_is_tty() -> bool:
    """Indirection point so tests can simulate TTY vs non-TTY contexts.

    ``CliRunner`` swaps ``sys.stdin`` with a non-TTY buffer, so patching
    ``sys.stdin.isatty`` directly doesn't reach the callback. Tests
    monkeypatch this helper instead.
    """
    return sys.stdin.isatty()


def _detect_migration_sources() -> list[DetectedMigrationSource]:
    """Discover importable homes on disk. Order: opensquilla, openclaw, hermes.

    The shared detector preserves the OpenSquilla source kind so portable
    homes are dispatched through the matching migration contract.
    """
    detected = [
        item
        for item in detect_default_sources()
        if not (
            item.name == "opensquilla" and item.source_kind == "windows-portable"
        )
    ]
    # The legacy detector historically collapsed Portable homes to the newest
    # mtime. RC4 keeps every candidate visible and never turns that estimate
    # into a selection decision.
    target = default_opensquilla_home()
    for candidate in enumerate_portable_homes():
        try:
            if candidate.path.resolve(strict=False) == target.resolve(strict=False):
                continue
        except OSError:
            pass
        detected.append(
            DetectedMigrationSource(
                "opensquilla",
                candidate.path,
                source_kind="windows-portable",
            )
        )
    return detected


def _prompt_source_selection(detected: list[DetectedMigrationSource]) -> list[str]:
    """Interactive multi-select for which detected sources to migrate.

    Returns the list of selected source ids. Empty list means the user
    cancelled or selected nothing.
    """
    import questionary

    target = default_opensquilla_home()
    choices = [
        questionary.Choice(
            title=(
                _describe_portable_candidate(metadata)
                if source.name == "opensquilla"
                and (
                    metadata := inspect_opensquilla_home_candidate(
                        source.path,
                        kind=source.source_kind or "cli-home",
                        target=target,
                    )
                ) is not None
                else f"{source.name} ({source.path})"
            ),
            value=source.name,
            checked=source.name != "opensquilla",
        )
        for source in detected
    ]
    answer = questionary.checkbox(
        "Which migration sources should be imported into OpenSquilla?",
        choices=choices,
    ).ask()
    return list(answer or [])


def _detected_source_payload(source: DetectedMigrationSource) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": source.name, "path": str(source.path)}
    if source.name != "opensquilla":
        return payload
    candidate = inspect_opensquilla_home_candidate(
        source.path,
        kind=source.source_kind or "cli-home",
        target=default_opensquilla_home(),
    )
    if candidate is None:
        return payload
    return {"name": source.name, **candidate.as_payload()}


def _run_one_migration(
    name: str,
    source_path: Path,
    *,
    source_kind: str | None,
    config: Path | None,
    apply: bool,
    migrate_secrets: bool,
    overwrite: bool,
    replace_target: bool,
    confirm_replace_target: Path | None,
    preset: str,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    skill_conflict: str,
    persona_conflict: str,
    json_output: bool,
) -> dict[str, Any]:
    """Run a single migrator. Validation errors raise typer.Exit(2)."""
    if name == "opensquilla":
        _reject_invalid_opensquilla_options(
            config=config,
            preset=preset,
            include=include,
            exclude=exclude,
        )
        opensquilla_options = OpenSquillaMigrationOptions(
            source=source_path,
            kind=source_kind or "cli-home",
            config_path=config,
            apply=apply,
            replace_target=replace_target,
            confirm_replace_target=confirm_replace_target,
            overwrite=overwrite,
        )
        migrator: Any = OpenSquillaHomeMigrator(opensquilla_options)
    elif name == "openclaw":
        _reject_invalid_options(
            preset=preset,
            include=include,
            exclude=exclude,
            skill_conflict=skill_conflict,
            persona_conflict=persona_conflict,
        )
        options = MigrationOptions(
            source=source_path,
            config_path=config,
            apply=apply,
            migrate_secrets=migrate_secrets,
            overwrite=overwrite,
            preset=preset,
            include=include,
            exclude=exclude,
            skill_conflict=skill_conflict,  # type: ignore[arg-type]
            persona_conflict=persona_conflict,  # type: ignore[arg-type]
        )
        migrator = OpenClawMigrator(options)
    elif name == "hermes":
        _reject_invalid_hermes_options(
            preset=preset,
            include=include,
            exclude=exclude,
            skill_conflict=skill_conflict,
        )
        hermes_options = HermesMigrationOptions(
            source=source_path,
            config_path=config,
            apply=apply,
            migrate_secrets=migrate_secrets,
            overwrite=overwrite,
            preset=preset,
            include=include,
            exclude=exclude,
            skill_conflict=skill_conflict,  # type: ignore[arg-type]
        )
        migrator = HermesMigrator(hermes_options)
    else:  # pragma: no cover - guarded earlier
        raise typer.Exit(2)

    def execute() -> dict[str, Any]:
        if json_output:
            with contextlib.redirect_stdout(io.StringIO()):
                return cast(dict[str, Any], migrator.migrate())
        return cast(dict[str, Any], migrator.migrate())

    if name == "opensquilla":
        return execute()
    with _guard_foreign_migration_target():
        return execute()


@migrate_app.callback()
def migrate_root(
    ctx: typer.Context,
    source: list[str] | None = typer.Option(
        None,
        "--source",
        help=(
            "Comma-separated source ids to migrate when auto-detecting: "
            "opensquilla, openclaw, hermes. Required when several are found "
            "and stdin is not a TTY."
        ),
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        help="OpenSquilla config path to write or preview.",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Apply the migration. Without this flag, only a dry-run report is produced.",
    ),
    migrate_secrets: bool = typer.Option(
        False,
        "--migrate-secrets",
        help="Copy recognized secrets. Defaults to false.",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help=(
            "Foreign migrations: overwrite item conflicts. OpenSquilla imports: "
            "deprecated alias for --replace-target; exact target confirmation is still required."
        ),
    ),
    replace_target: bool = typer.Option(
        False,
        "--replace-target",
        help="Replace a non-empty OpenSquilla target as one whole-profile transaction.",
    ),
    confirm_replace_target: Path | None = typer.Option(
        None,
        "--confirm-replace-target",
        help="Exact resolved target path required for a non-interactive profile replacement.",
    ),
    preset: str = typer.Option(
        "full",
        "--preset",
        help="Migration preset: user-data or full.",
    ),
    include: list[str] | None = typer.Option(
        None,
        "--include",
        help="Comma-separated migration option ids to include.",
    ),
    exclude: list[str] | None = typer.Option(
        None,
        "--exclude",
        help="Comma-separated migration option ids to exclude.",
    ),
    skill_conflict: str = typer.Option(
        "skip",
        "--skill-conflict",
        help="Skill conflict behavior: skip, overwrite, or rename.",
    ),
    persona_conflict: str = typer.Option(
        "prompt",
        "--persona-conflict",
        help=(
            "How to resolve SOUL/USER/AGENTS conflicts (openclaw only). "
            "Options: prompt (default), use-opensquilla, use-openclaw, "
            "merge, or skip."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Auto-detect importable homes under the user's home and migrate them.

    Subcommands (``migrate opensquilla``, ``migrate openclaw``,
    ``migrate hermes``) still work as before with explicit source paths.
    Calling ``opensquilla migrate`` with no subcommand scans the legacy
    ``~/.opensquilla`` CLI home (only when it is not the active home),
    ``~/.openclaw``, and ``~/.hermes``, then either prompts the user to
    pick which to import (TTY) or prints the discovered sources and asks
    for ``--source`` (non-TTY, ``--json``).
    """
    if ctx.invoked_subcommand is not None:
        if ctx.invoked_subcommand in {"openclaw", "hermes"}:
            lifecycle = _guard_foreign_migration_target()
            lifecycle.__enter__()
            ctx.call_on_close(lambda: lifecycle.__exit__(None, None, None))
        return

    detected = _detect_migration_sources()
    detected_names: list[str] = [item.name for item in detected]

    if not detected:
        payload = {
            "detected": [],
            "message": (
                "No migration source detected. Checked the default OpenSquilla CLI, "
                "desktop, and portable locations, plus "
                f"{Path.home() / '.openclaw'} and {Path.home() / '.hermes'}. "
                "Use `opensquilla migrate opensquilla --source <path>`, "
                "`opensquilla migrate openclaw --source <path>`, or "
                "`opensquilla migrate hermes --source <path>` to point "
                "at a non-default home."
            ),
        }
        if json_output:
            typer.echo(json.dumps(payload, ensure_ascii=False))
        else:
            console.print(payload["message"])
        raise typer.Exit(0)

    source_filter = _split_csv(source)
    portable_candidates = [
        item
        for item in detected
        if item.name == "opensquilla" and item.source_kind == "windows-portable"
    ]
    nonportable_opensquilla = any(
        item.name == "opensquilla" and item.source_kind != "windows-portable"
        for item in detected
    )
    if portable_candidates and not source_filter:
        portable_selection_payload = {
            "detected": [_detected_source_payload(item) for item in detected],
            "message": (
                "Portable OpenSquilla homes require an explicit path. Re-run with "
                "`opensquilla migrate opensquilla --kind windows-portable "
                "--home <path>` after reviewing the candidates."
            ),
        }
        if json_output:
            typer.echo(json.dumps(portable_selection_payload, ensure_ascii=False))
        else:
            console.print(portable_selection_payload["message"])
            for item in portable_candidates:
                candidate = inspect_opensquilla_home_candidate(
                    item.path,
                    kind=item.source_kind or "windows-portable",
                    target=default_opensquilla_home(),
                )
                console.print(
                    f"  - {_describe_portable_candidate(candidate)}"
                    if candidate is not None
                    else f"  - {item.path}"
                )
        raise typer.Exit(0)
    if source_filter:
        unknown = sorted(set(source_filter) - set(_AUTO_DETECT_SOURCES))
        if unknown:
            typer.echo(
                f"Unknown migration source: {', '.join(unknown)} "
                f"(known: {', '.join(_AUTO_DETECT_SOURCES)})"
            )
            raise typer.Exit(2)
        if (
            "opensquilla" in source_filter
            and portable_candidates
            and not nonportable_opensquilla
        ):
            typer.echo(
                "--source opensquilla does not choose among Portable homes. Use "
                "`opensquilla migrate opensquilla --kind windows-portable --home <path>`."
            )
            raise typer.Exit(2)
        missing = sorted(set(source_filter) - set(detected_names))
        if missing:
            typer.echo(
                f"Requested source not detected: {', '.join(missing)}. "
                f"Found: {', '.join(detected_names) or '(none)'}"
            )
            raise typer.Exit(2)
        selected = [name for name in _AUTO_DETECT_SOURCES if name in source_filter]
    elif len(detected) == 1 and detected[0].name != "opensquilla":
        # Preserve the established convenience for foreign runtimes. A
        # same-product profile import is different: it replaces/copies identity,
        # memory, sessions and configuration, so even a single CLI home must be
        # shown and explicitly confirmed.
        selected = detected_names
    else:
        # Multiple sources, no explicit filter. TTY: prompt. Non-TTY: list and exit.
        stdin_is_tty = _stdin_is_tty()
        if not stdin_is_tty or json_output:
            selection_message = (
                "An OpenSquilla profile was detected. Re-run with "
                "`--source opensquilla` after explicitly confirming the displayed path."
                if len(detected) == 1 and detected[0].name == "opensquilla"
                else (
                    "Multiple migration sources detected. Re-run with "
                    "`--source <names>` to select. Example: "
                    f"`opensquilla migrate --source {','.join(detected_names)} --apply`"
                )
            )
            selection_payload: dict[str, Any] = {
                "detected": [
                    _detected_source_payload(item)
                    for item in detected
                ],
                "message": selection_message,
            }
            if json_output:
                typer.echo(json.dumps(selection_payload, ensure_ascii=False))
            else:
                console.print(selection_payload["message"])
                console.print("[dim]Detected sources:[/dim]")
                for item in detected:
                    metadata = (
                        inspect_opensquilla_home_candidate(
                            item.path,
                            kind=item.source_kind or "cli-home",
                            target=default_opensquilla_home(),
                        )
                        if item.name == "opensquilla"
                        else None
                    )
                    console.print(
                        f"  - {_describe_portable_candidate(metadata)}"
                        if metadata is not None
                        else f"  - {item.name}: {item.path}"
                    )
            raise typer.Exit(0)
        selected = _prompt_source_selection(detected)
        if not selected:
            console.print("No source selected; nothing to do.")
            raise typer.Exit(0)

    include_options = _split_csv(include)
    exclude_options = _split_csv(exclude)
    # Validate every selected migrator's options BEFORE running any of
    # them, so a bad ``--include`` flag for hermes never half-applies
    # openclaw first and then bails out partway through the batch.
    for name in selected:
        if name == "opensquilla":
            _reject_invalid_opensquilla_options(
                config=config,
                preset=preset,
                include=include_options,
                exclude=exclude_options,
            )
        elif name == "openclaw":
            _reject_invalid_options(
                preset=preset,
                include=include_options,
                exclude=exclude_options,
                skill_conflict=skill_conflict,
                persona_conflict=persona_conflict,
            )
        elif name == "hermes":
            _reject_invalid_hermes_options(
                preset=preset,
                include=include_options,
                exclude=exclude_options,
                skill_conflict=skill_conflict,
            )

    detected_by_name: dict[str, DetectedMigrationSource] = {}
    for item in detected:
        detected_by_name.setdefault(item.name, item)
    reports: dict[str, dict[str, Any]] = {}
    has_error = False
    for name in selected:
        detected_source = detected_by_name[name]
        report = _run_one_migration(
            name,
            detected_source.path,
            source_kind=detected_source.source_kind,
            config=config,
            apply=apply,
            migrate_secrets=migrate_secrets,
            overwrite=overwrite,
            replace_target=replace_target,
            confirm_replace_target=confirm_replace_target,
            preset=preset,
            include=include_options,
            exclude=exclude_options,
            skill_conflict=skill_conflict,
            persona_conflict=persona_conflict,
            json_output=json_output,
        )
        reports[name] = report
        if any(item.get("status") == "error" for item in report.get("items", [])):
            has_error = True

    if json_output:
        typer.echo(
            json.dumps(
                {"selected": selected, "reports": reports},
                ensure_ascii=False,
            )
        )
    else:
        mode = "applied" if apply else "dry-run"
        for name in selected:
            console.print(f"[green]{name} migration complete[/green] ({mode})")
            output_dir = str(reports[name].get("output_dir") or "")
            if output_dir:
                console.print(f"[dim]Report:[/dim] {output_dir}")

    if has_error:
        raise typer.Exit(1)


def _reject_invalid_options(
    *,
    preset: str,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    skill_conflict: str,
    persona_conflict: str | None = None,
) -> None:
    if preset not in MIGRATION_PRESETS:
        typer.echo(f"Unknown migration preset: {preset}")
        raise typer.Exit(2)
    unknown_include = sorted(set(include) - MIGRATION_OPTIONS)
    if unknown_include:
        typer.echo(f"Unknown migration option in include: {', '.join(unknown_include)}")
        raise typer.Exit(2)
    unknown_exclude = sorted(set(exclude) - MIGRATION_OPTIONS)
    if unknown_exclude:
        typer.echo(f"Unknown migration option in exclude: {', '.join(unknown_exclude)}")
        raise typer.Exit(2)
    if skill_conflict not in SKILL_CONFLICT_MODES:
        typer.echo(f"Unknown skill conflict behavior: {skill_conflict}")
        raise typer.Exit(2)
    if persona_conflict is not None and persona_conflict not in PERSONA_CONFLICT_MODES:
        typer.echo(f"Unknown persona conflict behavior: {persona_conflict}")
        raise typer.Exit(2)


def _reject_invalid_opensquilla_options(
    *,
    config: Path | None,
    preset: str,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
) -> None:
    # The self-migration is a whole-home copy with no per-item option
    # surface; only the shared defaults are accepted silently.
    if preset not in ("", "full") or include or exclude:
        typer.echo("opensquilla source does not take preset/include/exclude")
        raise typer.Exit(2)
    if config is not None:
        typer.echo(
            "--config is not supported for OpenSquilla self-migration. "
            "Set OPENSQUILLA_STATE_DIR to the target home and re-run without --config."
        )
        raise typer.Exit(2)


def _reject_invalid_hermes_options(
    *,
    preset: str,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    skill_conflict: str,
) -> None:
    if preset not in HERMES_MIGRATION_PRESETS:
        typer.echo(f"Unknown Hermes migration preset: {preset}")
        raise typer.Exit(2)
    unknown_include = sorted(set(include) - HERMES_MIGRATION_OPTIONS)
    if unknown_include:
        typer.echo(f"Unknown Hermes migration option in include: {', '.join(unknown_include)}")
        raise typer.Exit(2)
    unknown_exclude = sorted(set(exclude) - HERMES_MIGRATION_OPTIONS)
    if unknown_exclude:
        typer.echo(f"Unknown Hermes migration option in exclude: {', '.join(unknown_exclude)}")
        raise typer.Exit(2)
    if skill_conflict not in HERMES_SKILL_CONFLICT_MODES:
        typer.echo(f"Unknown Hermes skill conflict behavior: {skill_conflict}")
        raise typer.Exit(2)


@migrate_app.command("verify-opensquilla-import", hidden=True)
def verify_opensquilla_import_command(
    source: Path = typer.Option(..., "--source", help="Exact imported profile source root."),
    target: Path = typer.Option(..., "--target", help="Exact Desktop target profile root."),
    source_kind: str = typer.Option(..., "--source-kind", help="Recorded OpenSquilla source kind."),
    transaction_id: str | None = typer.Option(
        None,
        "--transaction-id",
        help="Optional exact transaction id reported by the import child.",
    ),
    exclude_transaction_id: list[str] | None = typer.Option(
        None,
        "--exclude-transaction-id",
        help="Receipt ids that existed before this import attempt.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the fixed JSON protocol."),
) -> None:
    """Internal, offline verification for a committed whole-profile import."""

    from opensquilla.migration.opensquilla_home import verify_committed_profile_import
    from opensquilla.recovery import RecoveryError

    normalized_source = source.expanduser().absolute()
    normalized_target = target.expanduser().absolute()
    try:
        payload = verify_committed_profile_import(
            normalized_source,
            normalized_target,
            source_kind=source_kind,
            transaction_id=transaction_id,
            excluded_transaction_ids=tuple(exclude_transaction_id or ()),
        )
    except RecoveryError as exc:
        payload = {
            "schema_version": 1,
            "outcome": "unsafe",
            "stable_code": exc.stable_code,
            "source": str(normalized_source),
            "source_kind": source_kind,
            "target": str(normalized_target),
            "transaction_id": "",
            "matching_transaction_ids": [],
            "provider_connection": None,
            "report": None,
        }
    except Exception:
        payload = {
            "schema_version": 1,
            "outcome": "unsafe",
            "stable_code": "profile_import_receipt_verification_failed",
            "source": str(normalized_source),
            "source_kind": source_kind,
            "target": str(normalized_target),
            "transaction_id": "",
            "matching_transaction_ids": [],
            "provider_connection": None,
            "report": None,
        }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    typer.echo(f"{payload['outcome']}: {payload['stable_code']}")


def _describe_portable_candidate(candidate: PortableCandidate) -> str:
    version = candidate.version or "unknown version"
    activity = candidate.estimated_activity_at or "unknown"
    sessions = (
        f"{candidate.session_count} sessions"
        if candidate.session_count is not None
        else "session count unavailable"
    )
    size = (
        f"{candidate.size_bytes} bytes"
        if candidate.size_bytes is not None
        else "size unavailable"
    )
    imported = ", previously imported" if candidate.previously_imported else ""
    return (
        f"{candidate.path} (version {version}, estimated recent activity {activity}, "
        f"{sessions}, {size}{imported})"
    )


def _prompt_opensquilla_home(
    candidates: list[PortableCandidate],
    *,
    prompt: str,
) -> Path:
    import questionary

    choices = [
        questionary.Choice(
            title=f"{index}. {_describe_portable_candidate(candidate)}",
            value=str(candidate.path),
        )
        for index, candidate in enumerate(candidates, start=1)
    ]
    answer = questionary.select(
        prompt,
        choices=choices,
    ).ask()
    if not answer:
        raise typer.Exit(0)
    return Path(answer)


def _resolve_portable_source(home: Path | None, *, json_output: bool) -> Path:
    """Resolve the portable source home for ``--kind windows-portable``."""
    candidates = enumerate_portable_homes(target=default_opensquilla_home())
    if home is not None:
        selected = Path(home).expanduser()
        for candidate in candidates:
            try:
                if candidate.path.resolve() == selected.resolve():
                    return candidate.path
            except OSError:
                continue
        if is_valid_opensquilla_home(selected):
            return selected
        typer.echo(f"--home does not point at a portable OpenSquilla home: {selected}")
        raise typer.Exit(2)
    if not candidates:
        typer.echo(
            "No portable OpenSquilla homes were found under LOCALAPPDATA/TEMP. "
            "Pass --source <path> to point at one explicitly."
        )
        raise typer.Exit(2)
    if json_output or not _stdin_is_tty():
        message = "Portable OpenSquilla homes found; explicitly confirm one with --home <path>."
        if json_output:
            typer.echo(json.dumps({
                "requires_selection": True,
                "candidates": [candidate.as_payload() for candidate in candidates],
                "message": message,
            }, ensure_ascii=False))
        else:
            lines = [message]
            lines.extend(
                f"  - {_describe_portable_candidate(candidate)}" for candidate in candidates
            )
            typer.echo("\n".join(lines))
        raise typer.Exit(2)
    return _prompt_opensquilla_home(
        candidates,
        prompt="Which portable OpenSquilla home should be imported?",
    )


def _resolve_implicit_opensquilla_source(*, kind: str, json_output: bool) -> Path:
    """Show the detected same-product source and require explicit confirmation."""

    target = default_opensquilla_home()
    detected = (
        detect_legacy_cli_home(target)
        if kind == "cli-home"
        else detect_desktop_home()
    )
    candidate = (
        inspect_opensquilla_home_candidate(detected, kind=kind, target=target)
        if detected is not None
        else None
    )
    if candidate is None:
        typer.echo(
            f"No {kind} OpenSquilla home was found. Pass --source <path> explicitly."
        )
        raise typer.Exit(2)
    message = (
        "An OpenSquilla profile was found; explicitly confirm it with "
        f"--source {candidate.path}."
    )
    if json_output or not _stdin_is_tty():
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "requires_selection": True,
                        "candidates": [candidate.as_payload()],
                        "message": message,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            typer.echo(f"{message}\n  - {_describe_portable_candidate(candidate)}")
        raise typer.Exit(2)
    return _prompt_opensquilla_home(
        [candidate],
        prompt="Confirm the OpenSquilla profile to import.",
    )


@migrate_app.command("opensquilla")
def migrate_opensquilla(
    source: Path | None = typer.Option(
        None,
        "--source",
        help=(
            "Supported OpenSquilla CLI or Desktop profile, or historical Windows "
            "Portable profile directory."
        ),
    ),
    kind: str = typer.Option(
        "cli-home",
        "--kind",
        help="Source kind: cli-home, windows-portable, or desktop-home.",
    ),
    home: Path | None = typer.Option(
        None,
        "--home",
        help="Select one enumerated portable home (windows-portable only).",
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        help="OpenSquilla config path to write or preview.",
    ),
    apply: bool = typer.Option(
        False,
        "--apply/--dry-run",
        help="Apply the migration. Without this flag, only a dry-run report is produced.",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help=(
            "Deprecated alias for --replace-target. Apply still requires "
            "--confirm-replace-target with the exact resolved target path."
        ),
    ),
    replace_target: bool = typer.Option(
        False,
        "--replace-target",
        help="Back up and replace a non-empty target as a whole profile (never merge).",
    ),
    confirm_replace_target: Path | None = typer.Option(
        None,
        "--confirm-replace-target",
        help="Exact resolved target path required when replacing a non-empty target.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    inspect_candidate: bool = typer.Option(
        False,
        "--inspect-candidate",
        help=(
            "Read bounded, privacy-safe candidate metadata without planning or "
            "applying an import."
        ),
    ),
) -> None:
    """Import a supported CLI/Desktop profile or historical Windows Portable data."""

    if config is not None:
        typer.echo(
            "--config is not supported for OpenSquilla self-migration. "
            "Set OPENSQUILLA_STATE_DIR to the target home and re-run without --config."
        )
        raise typer.Exit(2)
    if kind not in OPENSQUILLA_SOURCE_KINDS:
        typer.echo(
            f"Unknown source kind: {kind} (known: {', '.join(OPENSQUILLA_SOURCE_KINDS)})"
        )
        raise typer.Exit(2)
    if home is not None and source is not None:
        typer.echo("Pass either --source or --home, not both.")
        raise typer.Exit(2)
    if home is not None and kind != "windows-portable":
        typer.echo("--home applies to --kind windows-portable only; use --source instead.")
        raise typer.Exit(2)
    source_path = source
    if source_path is None and kind == "windows-portable" and home is not None:
        source_path = _resolve_portable_source(home, json_output=json_output)
    if inspect_candidate:
        if source_path is None:
            typer.echo("--inspect-candidate requires --source or --home.")
            raise typer.Exit(2)
        candidate = inspect_opensquilla_home_candidate(
            Path(source_path).expanduser(),
            kind=kind,
            target=default_opensquilla_home(),
        )
        if candidate is None:
            payload = {
                "candidate": None,
                "error": "The selected path is not a plain OpenSquilla profile home.",
            }
            typer.echo(json.dumps(payload, ensure_ascii=False) if json_output else payload["error"])
            raise typer.Exit(2)
        if json_output:
            typer.echo(json.dumps({"candidate": candidate.as_payload()}, ensure_ascii=False))
        else:
            console.print(_describe_portable_candidate(candidate))
        raise typer.Exit(0)
    if source_path is None:
        source_path = (
            _resolve_portable_source(home, json_output=json_output)
            if kind == "windows-portable"
            else _resolve_implicit_opensquilla_source(
                kind=kind,
                json_output=json_output,
            )
        )

    options = OpenSquillaMigrationOptions(
        source=source_path,
        kind=kind,
        config_path=config,
        apply=apply,
        replace_target=replace_target,
        confirm_replace_target=confirm_replace_target,
        overwrite=overwrite,
    )
    if json_output:
        with contextlib.redirect_stdout(io.StringIO()):
            report = OpenSquillaHomeMigrator(options).migrate()
    else:
        report = OpenSquillaHomeMigrator(options).migrate()
    has_error = any(item.get("status") == "error" for item in report.get("items", []))
    if json_output:
        typer.echo(json.dumps(report, ensure_ascii=False))
    else:
        mode = "applied" if apply else "dry-run"
        counts: dict[str, int] = {}
        for item in report.get("items", []):
            status = str(item.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        if has_error:
            console.print(f"[red]OpenSquilla self-migration failed[/red] ({mode})")
        else:
            console.print(f"[green]OpenSquilla self-migration complete[/green] ({mode})")
        if counts:
            summary = ", ".join(f"{status}: {count}" for status, count in sorted(counts.items()))
            console.print(f"[dim]Items:[/dim] {summary}")
        paused = report.get("paused_jobs") or []
        if paused:
            console.print(
                f"[dim]Scheduler:[/dim] {len(paused)} imported job(s) arrive paused; "
                "review them with `opensquilla cron list`."
            )
        output_dir = str(report.get("output_dir") or "")
        if output_dir:
            console.print(f"[dim]Report:[/dim] {output_dir}")
    if has_error:
        raise typer.Exit(1)


@migrate_app.command("openclaw")
def migrate_openclaw(
    source: Path = typer.Option(
        Path.home() / ".openclaw",
        "--source",
        help="OpenClaw home directory.",
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        help="OpenSquilla config path to write or preview.",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Apply the migration. Without this flag, only a dry-run report is produced.",
    ),
    migrate_secrets: bool = typer.Option(
        False,
        "--migrate-secrets",
        help="Copy recognized secrets. Defaults to false.",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Overwrite target workspace files after making item-level backups.",
    ),
    preset: str = typer.Option(
        "full",
        "--preset",
        help="Migration preset: user-data or full.",
    ),
    include: list[str] | None = typer.Option(
        None,
        "--include",
        help="Comma-separated migration option ids to include.",
    ),
    exclude: list[str] | None = typer.Option(
        None,
        "--exclude",
        help="Comma-separated migration option ids to exclude.",
    ),
    skill_conflict: str = typer.Option(
        "skip",
        "--skill-conflict",
        help="Skill conflict behavior: skip, overwrite, or rename.",
    ),
    persona_conflict: str = typer.Option(
        "prompt",
        "--persona-conflict",
        help=(
            "How to resolve SOUL/USER/AGENTS conflicts when the destination "
            "already holds real user content: prompt (interactive, default), "
            "use-opensquilla, use-openclaw, merge, or skip."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Migrate OpenClaw state into OpenSquilla-native files."""

    include_options = _split_csv(include)
    exclude_options = _split_csv(exclude)
    _reject_invalid_options(
        preset=preset,
        include=include_options,
        exclude=exclude_options,
        skill_conflict=skill_conflict,
        persona_conflict=persona_conflict,
    )
    options = MigrationOptions(
        source=source,
        config_path=config,
        apply=apply,
        migrate_secrets=migrate_secrets,
        overwrite=overwrite,
        preset=preset,
        include=include_options,
        exclude=exclude_options,
        skill_conflict=skill_conflict,  # type: ignore[arg-type]
        persona_conflict=persona_conflict,  # type: ignore[arg-type]
    )
    if json_output:
        with contextlib.redirect_stdout(io.StringIO()):
            report = OpenClawMigrator(options).migrate()
    else:
        report = OpenClawMigrator(options).migrate()
    has_error = any(item.get("status") == "error" for item in report.get("items", []))
    if json_output:
        typer.echo(json.dumps(report, ensure_ascii=False))
    else:
        mode = "applied" if apply else "dry-run"
        console.print(f"[green]OpenClaw migration complete[/green] ({mode})")
        console.print(f"[dim]Report:[/dim] {report['output_dir']}")
    if has_error:
        raise typer.Exit(1)


@migrate_app.command("hermes")
def migrate_hermes(
    source: Path | None = typer.Option(
        None,
        "--source",
        help="Hermes home directory.",
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Hermes profile name under ~/.hermes/profiles.",
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        help="OpenSquilla config path to write or preview.",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Apply the migration. Without this flag, only a dry-run report is produced.",
    ),
    migrate_secrets: bool = typer.Option(
        False,
        "--migrate-secrets",
        help="Copy recognized secrets. Defaults to false.",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Overwrite target workspace files after making item-level backups.",
    ),
    preset: str = typer.Option(
        "full",
        "--preset",
        help="Migration preset: user-data or full.",
    ),
    include: list[str] | None = typer.Option(
        None,
        "--include",
        help="Comma-separated migration option ids to include.",
    ),
    exclude: list[str] | None = typer.Option(
        None,
        "--exclude",
        help="Comma-separated migration option ids to exclude.",
    ),
    skill_conflict: str = typer.Option(
        "skip",
        "--skill-conflict",
        help="Skill conflict behavior: skip, overwrite, or rename.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Migrate Hermes Agent state into OpenSquilla-native files."""

    include_options = _split_csv(include)
    exclude_options = _split_csv(exclude)
    _reject_invalid_hermes_options(
        preset=preset,
        include=include_options,
        exclude=exclude_options,
        skill_conflict=skill_conflict,
    )
    options = HermesMigrationOptions(
        source=source,
        profile=profile,
        config_path=config,
        apply=apply,
        migrate_secrets=migrate_secrets,
        overwrite=overwrite,
        preset=preset,
        include=include_options,
        exclude=exclude_options,
        skill_conflict=skill_conflict,  # type: ignore[arg-type]
    )
    if json_output:
        with contextlib.redirect_stdout(io.StringIO()):
            report = HermesMigrator(options).migrate()
    else:
        report = HermesMigrator(options).migrate()
    has_error = any(item.get("status") == "error" for item in report.get("items", []))
    if json_output:
        typer.echo(json.dumps(report, ensure_ascii=False))
    else:
        mode = "applied" if apply else "dry-run"
        console.print(f"[green]Hermes migration complete[/green] ({mode})")
        console.print(f"[dim]Report:[/dim] {report['output_dir']}")
    if has_error:
        raise typer.Exit(1)
