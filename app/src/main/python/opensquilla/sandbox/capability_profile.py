"""Capability-oriented profiles for sandbox development operations."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, replace
from enum import StrEnum

from opensquilla.sandbox.operation_profile import (
    OperationProfile,
    classify_command,
    package_bundle_for_manager,
)


class Capability(StrEnum):
    CREATE_PROJECT_DIR = "create_project_dir"
    CREATE_ENV = "create_env"
    INSTALL_PACKAGES = "install_packages"
    RUN_BUILD_SCRIPTS = "run_build_scripts"
    VERIFY_IMPORT_OR_BUILD = "verify_import_or_build"
    FETCH_SOURCE = "fetch_source"
    EXTRACT_ARCHIVE = "extract_archive"


class NetworkIntent(StrEnum):
    PACKAGE_REGISTRY = "package_registry"
    SOURCE_FETCH = "source_fetch"
    EXPLICIT_PUBLIC_URL = "explicit_public_url"
    UNKNOWN_PUBLIC = "unknown_public"
    PRIVATE_OR_LOCAL = "private_or_local"
    METADATA_OR_LINK_LOCAL = "metadata_or_link_local"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class CapabilityProfile:
    capabilities: frozenset[Capability] = frozenset()
    package_ecosystem: str | None = None
    package_bundles: tuple[str, ...] = ()
    network_intent: NetworkIntent | None = None
    read_paths: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()
    may_run_build_scripts: bool = False
    sensitive_path_touch: bool = False
    confidence: Confidence = Confidence.LOW
    evidence: tuple[str, ...] = ()

    @property
    def needs_network(self) -> bool:
        return self.network_intent is not None

    @property
    def is_development_operation(self) -> bool:
        return bool(
            self.capabilities
            & {
                Capability.CREATE_PROJECT_DIR,
                Capability.CREATE_ENV,
                Capability.INSTALL_PACKAGES,
                Capability.RUN_BUILD_SCRIPTS,
                Capability.VERIFY_IMPORT_OR_BUILD,
                Capability.FETCH_SOURCE,
                Capability.EXTRACT_ARCHIVE,
            }
        )


def capability_profile_for_command(argv: tuple[str, ...] | list[str]) -> CapabilityProfile:
    operation = classify_command(argv)
    profile = capability_profile_from_operation(operation)
    return _inject_simple_venv_plus_install_capabilities(argv, operation, profile)


def capability_profile_from_operation(operation: OperationProfile) -> CapabilityProfile:
    capabilities: set[Capability] = set()
    network_intent: NetworkIntent | None = None
    package_bundles: list[str] = []
    evidence: list[str] = [operation.name]

    if operation.name == "package_install":
        capabilities.add(Capability.INSTALL_PACKAGES)
        capabilities.add(Capability.RUN_BUILD_SCRIPTS)
        network_intent = NetworkIntent.PACKAGE_REGISTRY
        bundle = package_bundle_for_manager(operation.package_manager)
        if bundle is not None:
            package_bundles.append(bundle)
            evidence.append(bundle)
    elif operation.name == "package_query":
        capabilities.add(Capability.INSTALL_PACKAGES)
        network_intent = NetworkIntent.PACKAGE_REGISTRY
        bundle = package_bundle_for_manager(operation.package_manager)
        if bundle is not None:
            package_bundles.append(bundle)
            evidence.append(bundle)
    elif operation.name == "url_fetch":
        capabilities.add(Capability.FETCH_SOURCE)
        network_intent = NetworkIntent.EXPLICIT_PUBLIC_URL
    elif operation.name == "create_env":
        capabilities.add(Capability.CREATE_ENV)
    elif operation.name == "verify_dependency":
        capabilities.add(Capability.VERIFY_IMPORT_OR_BUILD)

    confidence = Confidence.HIGH if capabilities else Confidence.LOW
    return CapabilityProfile(
        capabilities=frozenset(capabilities),
        package_ecosystem=operation.package_manager,
        package_bundles=tuple(package_bundles),
        network_intent=network_intent,
        read_paths=operation.requested_paths,
        write_paths=operation.requested_write_paths,
        may_run_build_scripts=Capability.RUN_BUILD_SCRIPTS in capabilities,
        confidence=confidence,
        evidence=tuple(evidence),
    )


def _inject_simple_venv_plus_install_capabilities(
    argv: tuple[str, ...] | list[str],
    operation: OperationProfile,
    profile: CapabilityProfile,
) -> CapabilityProfile:
    # Temporary compatibility bridge for the common `python -m venv ... && ... pip install ...`
    # pattern until Task 2 migrates env creation into OperationProfile.
    if operation.name != "package_install" or profile.package_ecosystem != "python":
        return profile

    venv_path = _parse_venv_creation_path(argv)
    if not venv_path:
        return profile

    updated_capabilities = set(profile.capabilities)
    updated_capabilities.add(Capability.CREATE_ENV)

    updated_write_paths = list(profile.write_paths)
    if venv_path not in updated_write_paths:
        updated_write_paths.append(venv_path)

    return replace(
        profile,
        capabilities=frozenset(updated_capabilities),
        write_paths=tuple(updated_write_paths),
    )


def _parse_venv_creation_path(argv: tuple[str, ...] | list[str]) -> str | None:
    command_parts = tuple(str(part) for part in argv)
    if len(command_parts) < 3:
        return None

    wrapper, option = command_parts[0], command_parts[1]
    if wrapper not in {"sh", "bash", "dash", "zsh", "fish", "ksh"}:
        return None
    if option not in {"-c", "-lc"}:
        return None

    script = command_parts[2]
    script_commands = tuple(piece.strip() for piece in script.split("&&"))
    if len(script_commands) < 2:
        return None

    first_command = _tokenize_shell_command(script_commands[0])
    if not first_command:
        return None
    if not _is_python_venv_create(first_command):
        return None

    venv_path = _parse_venv_path(first_command)
    if not venv_path:
        return None

    for command in script_commands[1:]:
        command_parts = _tokenize_shell_command(command)
        if not command_parts:
            continue
        if classify_command(tuple(command_parts)).name == "package_install":
            return venv_path
    return None


def _tokenize_shell_command(command: str) -> tuple[str, ...]:
    try:
        return tuple(shlex.split(command))
    except ValueError:
        return tuple(command.split())


def _is_python_venv_create(parts: tuple[str, ...]) -> bool:
    if len(parts) < 4:
        return False
    if parts[1:3] != ("-m", "venv"):
        return False
    return _command_name(parts[0]).startswith("python")


def _parse_venv_path(parts: tuple[str, ...]) -> str | None:
    for token in parts[3:]:
        if token.startswith("-"):
            continue
        return token
    return None


def _command_name(value: str) -> str:
    name = value.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return name.removesuffix(".exe").lower()


__all__ = [
    "Capability",
    "CapabilityProfile",
    "Confidence",
    "NetworkIntent",
    "capability_profile_for_command",
    "capability_profile_from_operation",
]
