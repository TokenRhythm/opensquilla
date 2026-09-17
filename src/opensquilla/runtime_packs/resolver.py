"""Runtime resolution independent from sandbox policy enforcement."""

from __future__ import annotations

import ntpath
import os
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from opensquilla.run_mode import RunMode, normalize_run_mode
from opensquilla.runtime_packs.manager import RuntimePackService


class RuntimePolicyProtocol(Protocol):
    """Minimal policy surface accepted from sandbox and non-sandbox callers."""

    enabled: bool
    python: bool
    node: bool
    git_bash: bool


type RuntimePolicyInput = RuntimePolicyProtocol | Mapping[str, Any] | None


@dataclass(frozen=True)
class _RuntimePolicy:
    enabled: bool = True
    python: bool = True
    node: bool = True
    git_bash: bool = True


def _policy_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"Runtime Pack policy {field} must be a boolean")
    return value


def _runtime_policy(
    value: RuntimePolicyInput,
) -> _RuntimePolicy:
    if value is None:
        return _RuntimePolicy()
    if isinstance(value, Mapping):
        return _RuntimePolicy(
            enabled=_policy_bool(value.get("enabled", True), "enabled"),
            python=_policy_bool(value.get("python", True), "python"),
            node=_policy_bool(value.get("node", True), "node"),
            git_bash=_policy_bool(
                value.get("git_bash", value.get("gitBash", True)),
                "gitBash",
            ),
        )
    return _RuntimePolicy(
        enabled=_policy_bool(value.enabled, "enabled"),
        python=_policy_bool(value.python, "python"),
        node=_policy_bool(value.node, "node"),
        git_bash=_policy_bool(value.git_bash, "gitBash"),
    )


def _enabled_components(settings: _RuntimePolicy) -> tuple[str, ...]:
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


def _dedupe_path_text(paths: Iterable[str], *, windows: bool) -> tuple[str, ...]:
    """Deduplicate PATH entries without rewriting the approved environment text."""

    result: list[str] = []
    seen: set[str] = set()
    for value in paths:
        key = ntpath.normcase(value) if windows else value
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return tuple(result)


class RuntimePackResolver:
    """Resolve only activated and integrity-checked Runtime Packs."""

    def __init__(self, service: RuntimePackService) -> None:
        self.service = service
        self.target = service.target

    def runtime_roots(
        self,
        policy: RuntimePolicyInput = None,
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
        policy: RuntimePolicyInput = None,
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
        policy: RuntimePolicyInput = None,
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
        policy: RuntimePolicyInput = None,
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
        policy: RuntimePolicyInput = None,
        require_managed: bool = False,
    ) -> dict[str, str]:
        result = dict(environment or {})
        path_key = next((key for key in result if key.casefold() == "path"), "PATH")
        host = tuple(
            part for part in result.get(path_key, "").split(os.pathsep) if part.strip()
        )
        managed = tuple(str(path) for path in self.managed_path(policy))
        if require_managed:
            combined = managed
        elif normalize_run_mode(mode) is RunMode.FULL:
            combined = (*host, *managed)
        else:
            combined = (*managed, *host)
        resolved = _dedupe_path_text(
            combined,
            windows=self.target.startswith("windows-"),
        )
        result[path_key] = os.pathsep.join(resolved)
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


__all__ = ["RuntimePackResolver", "RuntimePolicyInput", "RuntimePolicyProtocol"]
