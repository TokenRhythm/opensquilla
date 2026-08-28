"""Compatibility facade for the transactional Community Skill manager.

New composition roots should inject :class:`SkillManagementService` directly.
``SkillInstaller`` remains import-compatible for existing extensions and tests.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from opensquilla.paths import default_opensquilla_home
from opensquilla.skills.hub.contracts import (
    DiagnosticPhase,
    DiagnosticSeverity,
    SkillDiagnostic,
)
from opensquilla.skills.hub.management import InstallResult, SkillManagementService
from opensquilla.skills.hub.router import SourceRouter
from opensquilla.skills.paths import default_managed_skills_dir

INSTALLER_CAPABILITY_UNSUPPORTED = "INSTALLER_CAPABILITY_UNSUPPORTED"


def supports_keyword_argument(function: Callable[..., Any], keyword: str) -> bool:
    """Return whether ``function`` explicitly declares one keyword.

    Compatibility must be selected before a mutation starts. Catching
    ``TypeError`` around the call itself cannot distinguish an old signature
    from an exception raised after the installer has already changed state. A
    generic ``**kwargs`` sink is not proof that a safety-relevant capability is
    implemented; wrappers must expose the keyword in their public signature.
    """

    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
    parameter = parameters.get(keyword)
    return parameter is not None and parameter.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }


def supported_keyword_arguments(
    function: Callable[..., Any],
    candidates: Mapping[str, Any],
) -> dict[str, Any]:
    """Filter additive compatibility kwargs without invoking ``function``."""

    return {
        keyword: value
        for keyword, value in candidates.items()
        if supports_keyword_argument(function, keyword)
    }


def unsupported_installer_result(
    *,
    operation: str,
    capability: str,
    name: str = "",
    install_id: str = "",
) -> InstallResult:
    """Build the stable no-mutation result for a legacy installer contract gap."""

    message = (
        f"The configured Skill installer does not support {capability} for {operation}. "
        "Upgrade or restart OpenSquilla with a compatible installer, then retry."
    )
    return InstallResult(
        success=False,
        name=name,
        message=message,
        install_id=install_id,
        diagnostics=[
            SkillDiagnostic(
                code=INSTALLER_CAPABILITY_UNSUPPORTED,
                severity=DiagnosticSeverity.ERROR,
                phase=DiagnosticPhase.COMPATIBILITY,
                message=message,
                blocking=True,
                hint="Use the installer bundled with the running OpenSquilla version.",
                details={"operation": operation, "capability": capability},
            )
        ],
    )


class SkillInstaller(SkillManagementService):
    """Backward-compatible name for the shared management service."""

    def __init__(
        self,
        router: SourceRouter,
        managed_dir: Path | None = None,
        quarantine_dir: Path | None = None,
        lockfile_path: Path | None = None,
        *,
        loader: Any | None = None,
        journal_path: Path | None = None,
        offline: bool | None = None,
    ) -> None:
        selected_managed = managed_dir or default_managed_skills_dir()
        selected_lock = lockfile_path or default_opensquilla_home() / "skills-lock.json"
        # ``quarantine_dir`` remains accepted for constructor compatibility,
        # but transaction state must have one canonical path per managed root.
        # Candidate staging now lives on the managed filesystem and no runtime
        # state is written beneath the legacy quarantine directory.
        _ = quarantine_dir
        super().__init__(
            router=router,
            managed_dir=selected_managed,
            lockfile_path=selected_lock,
            loader=loader,
            journal_path=journal_path,
            offline=(loader is None) if offline is None else offline,
        )


__all__ = [
    "INSTALLER_CAPABILITY_UNSUPPORTED",
    "InstallResult",
    "SkillInstaller",
    "supported_keyword_arguments",
    "supports_keyword_argument",
    "unsupported_installer_result",
]
