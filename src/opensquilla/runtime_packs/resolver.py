"""Runtime resolution independent from sandbox policy enforcement."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from opensquilla.runtime_packs.manager import RuntimePackService
from opensquilla.sandbox.policy_models import RuntimePolicySettings
from opensquilla.sandbox.run_mode import RunMode, normalize_run_mode


def _runtime_policy(
    value: RuntimePolicySettings | Mapping[str, Any] | None,
) -> RuntimePolicySettings:
    if value is None:
        return RuntimePolicySettings()
    if isinstance(value, RuntimePolicySettings):
        return value
    return RuntimePolicySettings.model_validate(value)


def _enabled_components(settings: RuntimePolicySettings) -> tuple[str, ...]:
    if not settings.enabled:
        return ()
    enabled = (
        ("python", settings.python),
        ("node", settings.node),
        ("gitBash", settings.git_bash),
    )
    return tuple(component_id for component_id, active in enabled if active)


def _dedupe(paths: Iterable[str | Path], *, windows: bool) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[str] = set()
    for value in paths:
        path = Path(value)
        key = str(path).casefold() if windows else str(path)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(path)
    return tuple(result)


class RuntimePackResolver:
    """Resolve only activated and integrity-checked Runtime Packs."""

    def __init__(self, service: RuntimePackService) -> None:
        self.service = service
        self.target = service.target

    def runtime_roots(
        self,
        policy: RuntimePolicySettings | Mapping[str, Any] | None = None,
    ) -> tuple[Path, ...]:
        settings = _runtime_policy(policy)
        roots = []
        for component_id in _enabled_components(settings):
            active = self.service.active_runtime(component_id)
            if active is not None:
                roots.append(active.package / "payload")
        return _dedupe(roots, windows=self.target.startswith("windows-"))

    def managed_path(
        self,
        policy: RuntimePolicySettings | Mapping[str, Any] | None = None,
    ) -> tuple[Path, ...]:
        settings = _runtime_policy(policy)
        paths: list[Path] = []
        for component_id in _enabled_components(settings):
            active = self.service.active_runtime(component_id)
            if active is not None:
                paths.extend(active.bin_dirs)
        return _dedupe(paths, windows=self.target.startswith("windows-"))

    # Compatibility vocabulary used by the old packaged-runtime integration.
    bundled_path = managed_path

    def executable_paths(
        self,
        policy: RuntimePolicySettings | Mapping[str, Any] | None = None,
    ) -> Mapping[str, Path]:
        settings = _runtime_policy(policy)
        result: dict[str, Path] = {}
        for component_id in _enabled_components(settings):
            active = self.service.active_runtime(component_id)
            if active is not None:
                result.update(active.executables)
        return result

    def path_for(
        self,
        mode: RunMode | str,
        host_path: Iterable[str | Path],
        *,
        policy: RuntimePolicySettings | Mapping[str, Any] | None = None,
        require_managed: bool = False,
    ) -> tuple[Path, ...]:
        host = tuple(Path(value) for value in host_path if str(value).strip())
        managed = self.managed_path(policy)
        if require_managed:
            combined = managed
        elif normalize_run_mode(mode) is RunMode.FULL:
            combined = (*host, *managed)
        else:
            combined = (*managed, *host)
        return _dedupe(combined, windows=self.target.startswith("windows-"))

    def apply_environment(
        self,
        environment: Mapping[str, str] | None,
        *,
        mode: RunMode | str,
        policy: RuntimePolicySettings | Mapping[str, Any] | None = None,
        require_managed: bool = False,
    ) -> dict[str, str]:
        result = dict(environment or {})
        path_key = next((key for key in result if key.casefold() == "path"), "PATH")
        host = tuple(
            Path(part) for part in result.get(path_key, "").split(os.pathsep) if part.strip()
        )
        resolved = self.path_for(
            mode,
            host,
            policy=policy,
            require_managed=require_managed,
        )
        result[path_key] = os.pathsep.join(str(path) for path in resolved)
        return result

    def resolve_component_binary(
        self,
        component_id: str,
        name: str,
        *,
        allow_host: bool = False,
    ) -> Path | None:
        active = self.service.active_runtime(component_id)
        if active is not None:
            candidate = active.executables.get(name)
            if candidate is not None:
                return candidate
        if allow_host:
            host = shutil.which(name)
            return Path(host) if host else None
        return None


__all__ = ["RuntimePackResolver"]
