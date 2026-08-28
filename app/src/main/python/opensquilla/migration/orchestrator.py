"""Shared migration orchestration outside Typer command handlers."""

from __future__ import annotations

import contextlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

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
    _is_valid_hermes_home,
)
from opensquilla.migration.legacy_detect import detect_legacy_home
from opensquilla.migration.openclaw import (
    MIGRATION_OPTIONS as OPENCLAW_MIGRATION_OPTIONS,
)
from opensquilla.migration.openclaw import (
    MIGRATION_PRESETS as OPENCLAW_MIGRATION_PRESETS,
)
from opensquilla.migration.openclaw import (
    PERSONA_CONFLICT_MODES,
    MigrationOptions,
    OpenClawMigrator,
    _is_valid_openclaw_home,
)
from opensquilla.migration.openclaw import (
    SKILL_CONFLICT_MODES as OPENCLAW_SKILL_CONFLICT_MODES,
)
from opensquilla.migration.opensquilla_home import (
    OpenSquillaHomeMigrator,
    OpenSquillaMigrationOptions,
    _same_path,
)
from opensquilla.paths import default_opensquilla_home

SourceName = Literal["opensquilla", "openclaw", "hermes"]
# Own-data imports run before foreign ones: the whole-home opensquilla copy
# must land first so the foreign migrators merge into it, not the reverse.
SOURCE_ORDER: tuple[SourceName, ...] = ("opensquilla", "openclaw", "hermes")


class MigrationOptionError(ValueError):
    """Raised when a shared migration option is invalid for a selected source."""


@dataclass(frozen=True)
class DetectedMigrationSource:
    name: SourceName
    path: Path
    source_kind: str | None = None


@dataclass(frozen=True)
class MigrationBatchOptions:
    config: Path | None = None
    apply: bool = False
    migrate_secrets: bool = False
    overwrite: bool = False
    preset: str = "full"
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    skill_conflict: str = "skip"
    persona_conflict: str = "use-opensquilla"
    quiet: bool = True


@dataclass(frozen=True)
class MigrationBatchResult:
    selected: tuple[str, ...]
    reports: dict[str, dict[str, Any]]
    apply: bool

    @property
    def has_error(self) -> bool:
        return any(report_has_error(report) for report in self.reports.values())

    @property
    def output_dirs(self) -> dict[str, str]:
        return {
            name: str(report.get("output_dir", ""))
            for name, report in self.reports.items()
            if report.get("output_dir")
        }


def detect_default_sources() -> list[DetectedMigrationSource]:
    """Discover legacy OpenSquilla, OpenClaw, and Hermes homes in canonical order."""

    found: list[DetectedMigrationSource] = []
    legacy_home = detect_legacy_home(default_opensquilla_home())
    if legacy_home is not None:
        found.append(
            DetectedMigrationSource(
                "opensquilla",
                legacy_home.path,
                source_kind=legacy_home.kind,
            )
        )
    openclaw_home = Path.home() / ".openclaw"
    if _is_valid_openclaw_home(openclaw_home):
        found.append(DetectedMigrationSource("openclaw", openclaw_home))
    hermes_home = Path.home() / ".hermes"
    if _is_valid_hermes_home(hermes_home):
        found.append(DetectedMigrationSource("hermes", hermes_home))
    return found


def canonical_source_selection(
    selected: list[str] | tuple[str, ...],
    detected: list[DetectedMigrationSource],
) -> tuple[SourceName, ...]:
    detected_names = {source.name for source in detected}
    unknown = sorted(set(selected) - set(SOURCE_ORDER))
    if unknown:
        raise MigrationOptionError(
            f"Unknown migration source: {', '.join(unknown)} "
            f"(known: {', '.join(SOURCE_ORDER)})"
        )
    missing = sorted(set(selected) - detected_names)
    if missing:
        raise MigrationOptionError(
            f"Requested source not detected: {', '.join(missing)}. "
            f"Found: {', '.join(sorted(detected_names)) or '(none)'}"
        )
    return tuple(name for name in SOURCE_ORDER if name in selected)


def validate_batch_options(
    selected: tuple[SourceName, ...], options: MigrationBatchOptions
) -> None:
    for name in selected:
        if name == "opensquilla":
            _validate_opensquilla_options(options)
        elif name == "openclaw":
            _validate_openclaw_options(options)
        elif name == "hermes":
            _validate_hermes_options(options)
        else:
            raise MigrationOptionError(f"Unknown migration source: {name}")


def run_migration_batch(
    detected: list[DetectedMigrationSource],
    selected: list[str] | tuple[str, ...],
    options: MigrationBatchOptions,
) -> MigrationBatchResult:
    canonical = canonical_source_selection(tuple(selected), detected)
    validate_batch_options(canonical, options)
    detected_by_name = {source.name: source for source in detected}
    reports: dict[str, dict[str, Any]] = {}
    for name in canonical:
        source = detected_by_name[name]
        reports[name] = run_one_migration(
            name,
            source.path,
            options,
            source_kind=getattr(source, "source_kind", None),
        )
    return MigrationBatchResult(selected=canonical, reports=reports, apply=options.apply)


def run_one_migration(
    name: str,
    source_path: Path,
    options: MigrationBatchOptions,
    *,
    source_kind: str | None = None,
) -> dict[str, Any]:
    if name == "opensquilla":
        config_path = options.config
        if config_path is not None and _same_path(
            config_path, default_opensquilla_home() / "config.toml"
        ):
            config_path = None
        opensquilla_options = OpenSquillaMigrationOptions(
            source=source_path,
            kind=source_kind or "cli-home",
            config_path=config_path,
            apply=options.apply,
            overwrite=options.overwrite,
        )
        migrator: Any = OpenSquillaHomeMigrator(opensquilla_options)
    elif name == "openclaw":
        migration_options = MigrationOptions(
            source=source_path,
            config_path=options.config,
            apply=options.apply,
            migrate_secrets=options.migrate_secrets,
            overwrite=options.overwrite,
            preset=options.preset,
            include=options.include,
            exclude=options.exclude,
            skill_conflict=options.skill_conflict,  # type: ignore[arg-type]
            persona_conflict=options.persona_conflict,  # type: ignore[arg-type]
        )
        migrator = OpenClawMigrator(migration_options)
    elif name == "hermes":
        hermes_options = HermesMigrationOptions(
            source=source_path,
            config_path=options.config,
            apply=options.apply,
            migrate_secrets=options.migrate_secrets,
            overwrite=options.overwrite,
            preset=options.preset,
            include=options.include,
            exclude=options.exclude,
            skill_conflict=options.skill_conflict,  # type: ignore[arg-type]
        )
        migrator = HermesMigrator(hermes_options)
    else:
        raise MigrationOptionError(f"Unknown migration source: {name}")

    if options.quiet:
        with contextlib.redirect_stdout(io.StringIO()):
            return cast(dict[str, Any], migrator.migrate())
    return cast(dict[str, Any], migrator.migrate())


def report_has_error(report: dict[str, Any]) -> bool:
    return any(item.get("status") == "error" for item in report.get("items", []))


def report_status_counts(report: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in report.get("items", []):
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _validate_opensquilla_options(options: MigrationBatchOptions) -> None:
    # The self-migration is a whole-home copy: there is no per-item option
    # surface. The onboarding wizard passes the shared defaults
    # (preset="full", empty include/exclude), which are accepted silently.
    if options.preset not in ("", "full") or options.include or options.exclude:
        raise MigrationOptionError(
            "opensquilla source does not take preset/include/exclude"
        )
    if options.config is not None:
        active_config = default_opensquilla_home() / "config.toml"
        if not _same_path(options.config, active_config):
            raise MigrationOptionError(
                "--config is not supported for OpenSquilla self-migration unless it is "
                "the active home's config.toml. Set OPENSQUILLA_STATE_DIR to the target "
                "home and re-run without a custom --config."
            )


def _validate_openclaw_options(options: MigrationBatchOptions) -> None:
    if options.preset not in OPENCLAW_MIGRATION_PRESETS:
        raise MigrationOptionError(f"Unknown migration preset: {options.preset}")
    unknown_include = sorted(set(options.include) - OPENCLAW_MIGRATION_OPTIONS)
    if unknown_include:
        raise MigrationOptionError(
            f"Unknown migration option in include: {', '.join(unknown_include)}"
        )
    unknown_exclude = sorted(set(options.exclude) - OPENCLAW_MIGRATION_OPTIONS)
    if unknown_exclude:
        raise MigrationOptionError(
            f"Unknown migration option in exclude: {', '.join(unknown_exclude)}"
        )
    if options.skill_conflict not in OPENCLAW_SKILL_CONFLICT_MODES:
        raise MigrationOptionError(
            f"Unknown skill conflict behavior: {options.skill_conflict}"
        )
    if options.persona_conflict not in PERSONA_CONFLICT_MODES:
        raise MigrationOptionError(
            f"Unknown persona conflict behavior: {options.persona_conflict}"
        )


def _validate_hermes_options(options: MigrationBatchOptions) -> None:
    if options.preset not in HERMES_MIGRATION_PRESETS:
        raise MigrationOptionError(f"Unknown Hermes migration preset: {options.preset}")
    unknown_include = sorted(set(options.include) - HERMES_MIGRATION_OPTIONS)
    if unknown_include:
        raise MigrationOptionError(
            f"Unknown Hermes migration option in include: {', '.join(unknown_include)}"
        )
    unknown_exclude = sorted(set(options.exclude) - HERMES_MIGRATION_OPTIONS)
    if unknown_exclude:
        raise MigrationOptionError(
            f"Unknown Hermes migration option in exclude: {', '.join(unknown_exclude)}"
        )
    if options.skill_conflict not in HERMES_SKILL_CONFLICT_MODES:
        raise MigrationOptionError(
            f"Unknown Hermes skill conflict behavior: {options.skill_conflict}"
        )
