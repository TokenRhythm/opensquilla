"""Default paths for the skills subsystem.

Centralizes path resolution so the loader, installer, CLI, and gateway all
agree on where managed skills, taps, and related state live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from opensquilla.paths import default_opensquilla_home


def default_managed_skills_dir() -> Path:
    """Return the default managed-skills directory.

    Installer writes here; loader scans it as the MANAGED layer.
    """
    return default_opensquilla_home() / "skills"


def default_taps_file() -> Path:
    """Return the tap-registry file path.

    Kept outside the scanned managed-skills directory so the loader never has
    to filter it out during enumeration.
    """
    return default_opensquilla_home() / "skills-taps.json"


def legacy_taps_file() -> Path:
    """Return the pre-migration taps path (still inside the scan dir)."""
    return default_opensquilla_home() / "skills" / "taps.json"


def resolve_managed_skills_dir(config_value: str | None) -> Path | None:
    """Resolve the managed-skills directory.

    Precedence: explicit ``config_value`` > :func:`default_managed_skills_dir`.
    The default path is returned even before it exists so a long-running gateway
    can see skills installed later without rebuilding its ``SkillLoader``.
    """
    if config_value:
        return Path(config_value).expanduser()
    return default_managed_skills_dir()


def default_bundled_skills_dir() -> Path:
    """Directory that ships skills as part of the opensquilla install."""
    return Path(__file__).parent / "bundled"


@dataclass(frozen=True)
class SkillLayerDirs:
    """Resolved directories for every skill layer, ready for ``SkillLoader``.

    Gateway and CLI must agree on this mapping so ``opensquilla skills list`` shows
    the same inventory the agent actually loads.
    """

    bundled_dir: Path | None = None
    workspace_dir: Path | None = None
    managed_dir: Path | None = None
    personal_agents_dir: Path | None = None
    project_agents_dir: Path | None = None
    extra_dirs: list[Path] = field(default_factory=list)


def resolve_skill_layer_dirs(
    *,
    allow_bundled: bool = True,
    workspace_root: Path | None = None,
    workspace_override: Path | None = None,
    managed_override: str | None = None,
    extra_dirs: list[Path] | None = None,
) -> SkillLayerDirs:
    """Resolve every skill-layer dir from config-derived inputs.

    Callers (gateway boot and the ``opensquilla skills`` CLI) pass the same config
    values so both end up with the same inventory. Candidate directories are
    preserved before they exist so a running gateway can observe later writes.

    Args:
        allow_bundled: Honor the BUNDLED layer (config.skills.allow_bundled).
        workspace_root: Active workspace root (config.workspace_dir).
        workspace_override: Explicit WORKSPACE dir override
            (config.skills.workspace_dir).
        managed_override: Explicit MANAGED dir override
            (config.skills.managed_dir).
        extra_dirs: Low-precedence EXTRA dirs (config.skills.extra_dirs).
    """
    bundled_candidate = default_bundled_skills_dir()
    bundled_dir = (
        bundled_candidate if allow_bundled and bundled_candidate.is_dir() else None
    )

    # Preserve every writable candidate even when it does not exist yet;
    # skill_create and external writers may create it after gateway boot.
    if workspace_override is not None:
        workspace_dir: Path | None = workspace_override
    elif workspace_root is not None:
        workspace_dir = workspace_root / "skills"
    else:
        workspace_dir = Path.cwd() / "skills"

    managed_dir = resolve_managed_skills_dir(managed_override)

    personal_agents = Path.home() / ".agents" / "skills"
    personal_agents_dir = personal_agents

    project_root = workspace_root if workspace_root is not None else Path.cwd()
    project_agents = project_root / ".agents" / "skills"
    project_agents_dir = project_agents

    return SkillLayerDirs(
        bundled_dir=bundled_dir,
        workspace_dir=workspace_dir,
        managed_dir=managed_dir,
        personal_agents_dir=personal_agents_dir,
        project_agents_dir=project_agents_dir,
        extra_dirs=list(extra_dirs or []),
    )
