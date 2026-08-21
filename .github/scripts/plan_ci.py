#!/usr/bin/env python3
"""Build a deterministic, fail-closed CI suite plan from changed paths."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Final

SCHEMA_VERSION: Final = 1
DEFAULT_CONFIG: Final = Path(".github/ci/suites.v1.json")
_WINDOWS_ASSIGNMENTS_CONFIG_KEY: Final = "windows_test_assignments"
_WINDOWS_ASSIGNMENTS_PATH_KEY: Final = "_windows_test_assignments_path"
_LOADED_WINDOWS_ASSIGNMENTS_KEY: Final = "_loaded_windows_test_assignments"
_MACOS_RECOVERY_TEST_INPUTS_KEY: Final = "macos_recovery_test_inputs"

_DOC_EXACT: Final = {
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "META_SKILL_GUIDE.md",
    "MIGRATION.md",
    "README.md",
    "SECURITY.md",
    "SUPPORT.md",
    "THIRD_PARTY_NOTICES.md",
}
_DEPENDENCY_EXACT: Final = {
    ".python-version",
    "pyproject.toml",
    "uv.lock",
    "opensquilla-webui/.node-version",
    "opensquilla-webui/package.json",
    "opensquilla-webui/package-lock.json",
    "desktop/electron/package.json",
    "desktop/electron/package-lock.json",
    "src/opensquilla/cli/tui/opentui/package/.bun-version",
    "src/opensquilla/cli/tui/opentui/package/package.json",
    "src/opensquilla/cli/tui/opentui/package/bun.lock",
}
_PACKAGING_EXACT: Final = {
    "README.release.md",
    "RELEASES.md",
    "install.ps1",
    "install.sh",
    "start.ps1",
    "start.sh",
    "scripts/build_wheelhouse_zip.py",
    "scripts/install_source.ps1",
    "scripts/install_source.sh",
}
_MANAGED_TOOLCHAIN_EXACT: Final = {
    "scripts/validate_managed_toolchain_artifacts.py",
    "scripts/validate_managed_toolchain_artifacts_stdlib.py",
    "src/opensquilla/skills/runtime_env.py",
    "tests/test_skills/test_managed_toolchains.py",
}
_MANAGED_TOOLCHAIN_SHARED_TARGETS: Final = {
    "tests/test_skills/test_managed_toolchains.py",
    "tests/test_skills/test_toolchain_runtime_integration.py",
    "tests/test_skills/test_toolchain_state_scope.py",
}
_MANAGED_TOOLCHAIN_SOURCE_TARGETS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (
        "src/opensquilla/skills/bundled/meta-paper-write/",
        (
            "tests/test_skills/test_meta_paper*.py",
            "tests/test_skills/test_paper_*.py",
        ),
    ),
    (
        "src/opensquilla/skills/bundled/paper-",
        (
            "tests/test_skills/test_meta_paper*.py",
            "tests/test_skills/test_paper_*.py",
        ),
    ),
    (
        "src/opensquilla/skills/bundled/meta-short-drama/",
        ("tests/test_skills/test_meta_short_drama*.py",),
    ),
    (
        "src/opensquilla/skills/bundled/subtitle-burner/",
        ("tests/test_skills/test_subtitle_burner.py",),
    ),
    ("src/opensquilla/skills/bundled/video-still-animator/", ()),
)
_MANAGED_TOOLCHAIN_TEST_PREFIXES: Final = (
    "tests/test_skills/test_toolchain_",
    "tests/test_skills/test_meta_paper",
    "tests/test_skills/test_paper_",
    "tests/test_skills/test_meta_short_drama",
    "tests/test_skills/test_subtitle_burner",
    "tests/test_skills/test_video_still_animator",
)
_PYTHON_TARGET_RULES: Final[tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]] = (
    (
        ("src/opensquilla/provider/", "src/opensquilla/router_tiers.py"),
        (
            "tests/test_*router*.py",
            "tests/test_cross_provider_tiers.py",
            "tests/test_provider",
            "tests/test_provider*.py",
        ),
    ),
    (
        ("src/opensquilla/gateway/",),
        (
            "tests/functional/test_gateway_*_e2e.py",
            "tests/test_gateway",
            "tests/test_gateway*.py",
        ),
    ),
    (("src/opensquilla/channels/",), ("tests/test_channels",)),
    (
        ("src/opensquilla/memory/",),
        ("tests/test_memory", "tests/test_memory*.py"),
    ),
    (("src/opensquilla/scheduler/",), ("tests/test_scheduler",)),
    (
        ("src/opensquilla/skills/",),
        ("tests/test_meta_skill*.py", "tests/test_skills", "tests/test_skills*.py"),
    ),
    (
        ("src/opensquilla/cli/",),
        ("tests/integration/cli", "tests/test_cli"),
    ),
    (("src/opensquilla/identity/",), ("tests/test_identity",)),
    (
        ("src/opensquilla/mcp/", "src/opensquilla/mcp_server/"),
        ("tests/test_mcp", "tests/test_mcp_server"),
    ),
    (("src/opensquilla/health/",), ("tests/test_health",)),
    (("src/opensquilla/observability/",), ("tests/test_observability",)),
    (("src/opensquilla/search/",), ("tests/test_search",)),
    (("src/opensquilla/onboarding/",), ("tests/test_onboarding",)),
)
_FIXED_PLATFORM_MATRIX: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "workflow-lint": (("ubuntu-latest", "default"),),
    "readme-locale": (("ubuntu-latest", "default"),),
    "frontend": (("ubuntu-latest", "artifact-and-validation"),),
    "webui-chat-recovery": (("ubuntu-latest", "chromium"),),
    "tui": (("ubuntu-latest", "default"),),
    "desktop-static": (("ubuntu-latest", "default"),),
    "python-targeted": (("ubuntu-latest", "targeted"),),
    "windows-compat": (("windows-latest", "compat"),),
    "macos-recovery": (("macos-latest", "recovery"),),
    "release-packaging": (("ubuntu-latest", "default"),),
    "managed-toolchain": (
        ("ubuntu-24.04", "linux-x64"),
        ("ubuntu-24.04-arm", "linux-arm64"),
        ("ubuntu-24.04", "linux-musl-x64"),
        ("macos-15", "darwin-arm64"),
        ("macos-15-intel", "darwin-x64"),
        ("windows-2022", "windows-x64"),
    ),
}


class PlanError(ValueError):
    """The suite contract or planner input is invalid."""


def _managed_toolchain_targets(path: str) -> set[str] | None:
    """Return targeted tests when *path* belongs to the managed-toolchain domain."""

    targets: set[str] | None = None
    if (
        path in _MANAGED_TOOLCHAIN_EXACT
        or path.startswith("src/opensquilla/skills/toolchains/")
        or path.startswith(_MANAGED_TOOLCHAIN_TEST_PREFIXES)
    ):
        targets = set(_MANAGED_TOOLCHAIN_SHARED_TARGETS)
    for prefix, domain_targets in _MANAGED_TOOLCHAIN_SOURCE_TARGETS:
        if path.startswith(prefix):
            targets = set(_MANAGED_TOOLCHAIN_SHARED_TARGETS)
            targets.update(domain_targets)
            break
    if targets is not None and path.startswith("tests/"):
        targets.add(path)
    return targets


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def canonical_json(value: object) -> str:
    """Return one-line canonical JSON suitable for artifacts and digests."""

    return _canonical_bytes(value).decode("utf-8") + "\n"


def _require_string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise PlanError(f"{label} must be a list of non-empty strings")
    if len(value) != len(set(value)):
        raise PlanError(f"{label} contains duplicates")
    return list(value)


def _load_windows_test_assignments(
    path: Path, *, allowed_shards: set[str]
) -> dict[str, str]:
    """Load the governed exact test-to-shard map used by Windows CI."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"cannot read Windows test assignments {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise PlanError("unsupported Windows test assignment schema")
    baseline = value.get("baseline_assignments")
    if not isinstance(baseline, dict) or set(baseline) != allowed_shards:
        raise PlanError(
            "Windows test assignments must define every configured shard exactly once"
        )

    assignments: dict[str, str] = {}
    for shard, raw_paths in baseline.items():
        paths = _require_string_list(raw_paths, f"Windows shard {shard!r} assignments")
        for test_path in paths:
            candidate = PurePosixPath(test_path)
            if (
                candidate.as_posix() != test_path
                or not test_path.startswith("tests/")
                or not candidate.name.startswith("test_")
                or candidate.suffix != ".py"
            ):
                raise PlanError(f"invalid Windows test assignment path: {test_path!r}")
            if test_path in assignments:
                raise PlanError(f"duplicate Windows test assignment: {test_path}")
            assignments[test_path] = str(shard)

    overrides = value.get("overrides", [])
    if not isinstance(overrides, list):
        raise PlanError("Windows test assignment overrides must be a list")
    seen_overrides: set[str] = set()
    for raw_override in overrides:
        if not isinstance(raw_override, dict):
            raise PlanError("Windows test assignment override must be an object")
        test_path = raw_override.get("path")
        from_shard = raw_override.get("from")
        to_shard = raw_override.get("to")
        if not isinstance(test_path, str) or test_path in seen_overrides:
            raise PlanError("Windows test assignment override path is invalid or duplicated")
        if assignments.get(test_path) != from_shard:
            raise PlanError(f"Windows test assignment override source drifted: {test_path}")
        if to_shard not in allowed_shards or to_shard == from_shard:
            raise PlanError(f"Windows test assignment override target is invalid: {test_path}")
        assignments[test_path] = str(to_shard)
        seen_overrides.add(test_path)
    return assignments


def load_config(path: Path, *, repo: Path | None = None) -> dict[str, Any]:
    """Load and validate the v1 suite contract."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"cannot read suite contract {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise PlanError("unsupported CI suite contract schema")
    suites = value.get("suites")
    if not isinstance(suites, dict) or not suites:
        raise PlanError("suite contract must define suites")
    for suite_id, raw_suite in suites.items():
        if not isinstance(suite_id, str) or not suite_id:
            raise PlanError("suite IDs must be non-empty strings")
        if not isinstance(raw_suite, dict):
            raise PlanError(f"suite {suite_id!r} must be an object")
        _require_string_list(
            raw_suite.get("execution_inputs"), f"suite {suite_id!r} execution_inputs"
        )

    known = set(suites)
    for label in ("baseline_suites", "full_suites"):
        suite_ids = _require_string_list(value.get(label), label)
        unknown = sorted(set(suite_ids) - known)
        if unknown:
            raise PlanError(f"{label} contains unknown suites: {', '.join(unknown)}")

    matrix = value.get("full_desktop_matrix")
    if not isinstance(matrix, list) or not matrix:
        raise PlanError("full_desktop_matrix must be a non-empty list")
    seen_cells: set[tuple[str, str]] = set()
    for cell in matrix:
        if not isinstance(cell, dict) or set(cell) != {"os", "shard"}:
            raise PlanError("desktop matrix cells must contain only os and shard")
        os_name = cell.get("os")
        shard = cell.get("shard")
        if not isinstance(os_name, str) or not isinstance(shard, str):
            raise PlanError("desktop matrix os and shard must be strings")
        key = (os_name, shard)
        if key in seen_cells:
            raise PlanError("full_desktop_matrix contains duplicate cells")
        seen_cells.add(key)

    python_matrix = value.get("full_python_matrix")
    if not isinstance(python_matrix, dict) or set(python_matrix) != {
        "ubuntu",
        "windows",
    }:
        raise PlanError("full_python_matrix must define ubuntu and windows")
    for platform_name in ("ubuntu", "windows"):
        shards = _require_string_list(
            python_matrix.get(platform_name),
            f"full_python_matrix {platform_name}",
        )
        if not shards:
            raise PlanError(f"full_python_matrix {platform_name} must not be empty")

    assignments_path = value.get(_WINDOWS_ASSIGNMENTS_CONFIG_KEY)
    if (
        not isinstance(assignments_path, str)
        or not assignments_path
        or PurePosixPath(assignments_path).is_absolute()
        or PurePosixPath(assignments_path).as_posix() != assignments_path
        or ".." in PurePosixPath(assignments_path).parts
    ):
        raise PlanError(
            f"{_WINDOWS_ASSIGNMENTS_CONFIG_KEY} must be a normalized repository-relative path"
        )
    if repo is not None:
        # Parsing is intentionally lazy. Digest-only and source-only planning
        # does not need the test assignment payload; exact test planning does.
        value[_WINDOWS_ASSIGNMENTS_PATH_KEY] = repo.resolve() / assignments_path

    macos_recovery_inputs = _require_string_list(
        value.get(_MACOS_RECOVERY_TEST_INPUTS_KEY),
        _MACOS_RECOVERY_TEST_INPUTS_KEY,
    )
    for pattern in macos_recovery_inputs:
        candidate = PurePosixPath(pattern)
        if (
            "\\" in pattern
            or candidate.is_absolute()
            or candidate.as_posix() != pattern
            or not pattern.startswith("tests/")
            or ".." in candidate.parts
        ):
            raise PlanError(
                f"invalid {_MACOS_RECOVERY_TEST_INPUTS_KEY} pattern: {pattern!r}"
            )
    macos_recovery_suite = suites.get("macos-recovery")
    if not isinstance(macos_recovery_suite, Mapping):
        raise PlanError("suite 'macos-recovery' must be configured")
    missing_digest_inputs = sorted(
        set(macos_recovery_inputs)
        - set(macos_recovery_suite["execution_inputs"])
    )
    if missing_digest_inputs:
        raise PlanError(
            f"{_MACOS_RECOVERY_TEST_INPUTS_KEY} must be covered by the "
            "macos-recovery execution digest: "
            + ", ".join(missing_digest_inputs)
        )

    groups = value.get("desktop_groups")
    if not isinstance(groups, dict) or set(groups) != {
        "profiles",
        "ownership",
        "workbench",
    }:
        raise PlanError("desktop_groups must define profiles, ownership, and workbench")
    for group, raw_group in groups.items():
        if not isinstance(raw_group, dict):
            raise PlanError(f"desktop group {group!r} must be an object")
        _require_string_list(raw_group.get("keywords"), f"desktop group {group!r} keywords")
        _require_string_list(
            raw_group.get("path_patterns"),
            f"desktop group {group!r} path_patterns",
        )
    if repo is not None:
        _validate_execution_input_patterns(value, repo.resolve())
    return value


def _normalize_changed_paths(paths: Iterable[str]) -> tuple[list[str], bool]:
    normalized: set[str] = set()
    invalid = False
    for raw in paths:
        value = raw.rstrip("\r\n")
        if not value:
            continue
        candidate = PurePosixPath(value)
        if (
            "\\" in value
            or candidate.is_absolute()
            or value != candidate.as_posix()
            or any(part in {"", ".", ".."} for part in candidate.parts)
        ):
            invalid = True
            continue
        normalized.add(value)
    return sorted(normalized), invalid


def _is_docs(path: str) -> bool:
    if path in _PACKAGING_EXACT:
        return False
    name = PurePosixPath(path).name
    return (
        path.startswith("docs/")
        or path.startswith(".github/ISSUE_TEMPLATE/")
        or path == ".github/pull_request_template.md"
        or path in _DOC_EXACT
        or (name.startswith("README.") and name.endswith(".md") and "/" not in path)
    )


def _is_dependency(path: str) -> bool:
    return path in _DEPENDENCY_EXACT or path.endswith(
        ("/package.json", "/package-lock.json", "/bun.lock")
    )


def _os_scope(path: str) -> set[str]:
    lowered = f"/{path.casefold()}"
    scopes: set[str] = set()
    if path.endswith(".ps1") or any(
        token in lowered
        for token in ("/windows/", "_windows", "windows_", "/win32/", "-windows")
    ):
        scopes.add("windows-latest")
    if any(
        token in lowered
        for token in ("/macos/", "_macos", "macos_", "/darwin/", "-macos", ".plist")
    ):
        scopes.add("macos-latest")
    if any(
        token in lowered
        for token in ("/linux/", "_linux", "linux_", "-linux", "service-units/")
    ):
        scopes.add("ubuntu-latest")
    return scopes


def _add_os_reason_codes(scopes: set[str], reasons: set[str]) -> None:
    labels = {
        "ubuntu-latest": "linux_specific_changed",
        "macos-latest": "macos_specific_changed",
        "windows-latest": "windows_specific_changed",
    }
    reasons.update(labels[scope] for scope in scopes)


def _explicit_desktop_groups(path: str, config: Mapping[str, Any]) -> set[str]:
    """Return only reviewed path-pattern mappings, without keyword inference."""

    return {
        str(group)
        for group, raw_group in config["desktop_groups"].items()
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in raw_group["path_patterns"])
    }


def _desktop_groups(path: str, config: Mapping[str, Any]) -> set[str]:
    explicit = _explicit_desktop_groups(path, config)
    if explicit:
        return explicit
    lowered = path.casefold()
    groups: set[str] = set()
    for group, raw_group in config["desktop_groups"].items():
        if any(keyword.casefold() in lowered for keyword in raw_group["keywords"]):
            groups.add(str(group))
    return groups


def _desktop_cells(
    *, groups: set[str], os_scope: set[str], config: Mapping[str, Any]
) -> set[tuple[str, str]]:
    if not groups:
        return {
            (str(cell["os"]), str(cell["shard"]))
            for cell in config["full_desktop_matrix"]
            if not os_scope or str(cell["os"]) in os_scope
        }
    selected_groups = groups or {"profiles", "ownership", "workbench"}
    platforms = os_scope or {"ubuntu-latest", "macos-latest", "windows-latest"}
    cells: set[tuple[str, str]] = set()
    if "ubuntu-latest" in platforms:
        cells.update(("ubuntu-latest", group) for group in selected_groups)
    if "macos-latest" in platforms:
        cells.update(("macos-latest", group) for group in selected_groups)
    if "windows-latest" in platforms:
        cells.update(("windows-latest", group) for group in selected_groups)
    return cells


def _path_exists(repo: Path, path: str, *, ref: str | None, directory: bool) -> bool:
    """Return whether *path* is present in the selected worktree or Git tree."""

    if ref is None:
        candidate = repo / path
        return candidate.is_dir() if directory else candidate.is_file()
    completed = subprocess.run(
        ["git", "cat-file", "-t", f"{ref}:{path}"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    expected_type = "tree" if directory else "blob"
    return completed.returncode == 0 and completed.stdout.strip() == expected_type


def _safe_test_execution_target(
    path: str, *, repo: Path, ref: str | None
) -> str | None:
    """Use an exact test when present, otherwise its nearest existing test directory."""

    if _path_exists(repo, path, ref=ref, directory=False):
        return path
    candidate = PurePosixPath(path).parent
    while candidate.as_posix() == "tests" or candidate.as_posix().startswith("tests/"):
        relative = candidate.as_posix()
        if _path_exists(repo, relative, ref=ref, directory=True):
            return relative
        if relative == "tests":
            break
        candidate = candidate.parent
    return None


def _windows_test_assignments(config: Mapping[str, Any]) -> Mapping[str, str]:
    """Return the effective governed assignment map, loading it once per plan."""

    raw_assignments = config.get(_LOADED_WINDOWS_ASSIGNMENTS_KEY)
    if isinstance(raw_assignments, Mapping):
        return raw_assignments
    assignments_path = config.get(_WINDOWS_ASSIGNMENTS_PATH_KEY)
    if not isinstance(assignments_path, Path):
        raise PlanError("Windows test assignment path was not loaded with the contract")
    assignments = _load_windows_test_assignments(
        assignments_path,
        allowed_shards=set(config["full_python_matrix"]["windows"]),
    )
    if isinstance(config, dict):
        config[_LOADED_WINDOWS_ASSIGNMENTS_KEY] = assignments
    return assignments


def _is_test_module_path(path: str) -> bool:
    candidate = PurePosixPath(path)
    return (
        path.startswith("tests/")
        and candidate.name.startswith("test_")
        and candidate.suffix == ".py"
    )


def _test_module_name(path: str) -> str:
    candidate = PurePosixPath(path)
    if not _is_test_module_path(path):
        raise PlanError(f"cannot derive test module name from {path!r}")
    return ".".join((*candidate.parts[:-1], candidate.stem))


def _test_module_sources(
    repo: Path, *, ref: str | None
) -> dict[str, str]:
    """Read every current test module without executing repository code."""

    sources: dict[str, str] = {}
    if ref is None:
        for path in _repository_files_for_validation(repo):
            if not _is_test_module_path(path):
                continue
            candidate = repo / path
            if candidate.is_symlink():
                raise PlanError(f"test dependency analysis rejects symlink: {path}")
            try:
                sources[path] = candidate.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError) as exc:
                raise PlanError(f"cannot read test module {path}: {exc}") from exc
        return sources

    try:
        completed = subprocess.run(
            ["git", "archive", "--format=tar", ref, "--", "tests"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as archive:
            for member in archive.getmembers():
                path = PurePosixPath(member.name).as_posix()
                if not _is_test_module_path(path):
                    continue
                if not member.isfile():
                    raise PlanError(
                        f"test dependency analysis rejects non-file at {ref}: {path}"
                    )
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise PlanError(f"cannot read test module at {ref}: {path}")
                sources[path] = extracted.read().decode("utf-8-sig", errors="strict")
    except (OSError, subprocess.CalledProcessError, tarfile.TarError, UnicodeError) as exc:
        raise PlanError(f"cannot inspect test modules at ref {ref!r}: {exc}") from exc
    return sources


def _relative_import_name(
    importer_module: str, *, level: int, module: str | None
) -> str:
    package = importer_module.split(".")[:-1]
    keep = len(package) - (level - 1)
    if level <= 0 or keep <= 0:
        raise PlanError(f"invalid relative import in test module {importer_module}")
    parts = package[:keep]
    if module:
        parts.extend(module.split("."))
    return ".".join(parts)


def _static_string_values(value: ast.expr) -> tuple[str, ...] | None:
    """Return a static string or string sequence without evaluating code."""

    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return (value.value,)
    if isinstance(value, (ast.List, ast.Tuple)):
        values: list[str] = []
        for item in value.elts:
            if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                return None
            values.append(item.value)
        return tuple(values)
    return None


def _expression_mentions_test_module(value: ast.expr) -> bool:
    """Return whether a dynamic expression visibly names the test module tree."""

    for node in ast.walk(value):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        literal = node.value
        if literal.startswith(("tests.", "test_")) or ".test_" in literal:
            return True
    return False


def _build_test_reverse_dependencies(
    *,
    repo: Path,
    ref: str | None,
    governed_paths: Iterable[str],
) -> dict[str, frozenset[str]]:
    """Return test-module consumers keyed by the helper module they import.

    Governed paths are included in the module index even when a file was
    deleted in the selected tree. That lets a remaining consumer of a deleted
    helper participate in the safe execution closure.
    """

    sources = _test_module_sources(repo, ref=ref)
    known_paths = set(sources)
    known_paths.update(governed_paths)
    module_to_path: dict[str, str] = {}
    suffix_to_paths: dict[str, set[str]] = {}
    for path in sorted(known_paths):
        module_name = _test_module_name(path)
        previous = module_to_path.setdefault(module_name, path)
        if previous != path:
            raise PlanError(f"duplicate test module identity: {module_name}")
        parts = module_name.split(".")
        for offset in range(len(parts)):
            suffix_to_paths.setdefault(".".join(parts[offset:]), set()).add(path)

    def resolve(module_name: str) -> str | None:
        exact = module_to_path.get(module_name)
        if exact is not None:
            return exact
        matches = suffix_to_paths.get(module_name, set())
        if len(matches) == 1:
            return next(iter(matches))
        looks_like_test = module_name.rsplit(".", 1)[-1].startswith("test_")
        if len(matches) > 1 and looks_like_test:
            raise PlanError(f"ambiguous imported test module: {module_name}")
        if not matches and looks_like_test and (
            module_name.startswith("tests.") or ".test_" in module_name
        ):
            raise PlanError(f"unresolved imported test module: {module_name}")
        return None

    reverse: dict[str, set[str]] = {}
    for consumer_path, source in sorted(sources.items()):
        importer_module = _test_module_name(consumer_path)
        try:
            tree = ast.parse(source, filename=consumer_path)
        except (SyntaxError, ValueError) as exc:
            raise PlanError(f"cannot parse test module {consumer_path}: {exc}") from exc

        importlib_bindings: set[str] = set()
        import_module_bindings: set[str] = set()
        builtins_bindings: set[str] = set()
        builtin_import_bindings: set[str] = {"__import__"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "importlib":
                        importlib_bindings.add(alias.asname or "importlib")
                    elif alias.name == "builtins":
                        builtins_bindings.add(alias.asname or "builtins")
            elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
                import_module_bindings.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "import_module"
                )
            elif isinstance(node, ast.ImportFrom) and node.module == "builtins":
                builtin_import_bindings.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "__import__"
                )

        pytest_plugin_modules: list[str] = []
        for statement in tree.body:
            value: ast.expr | None = None
            owns_pytest_plugins = False
            if isinstance(statement, ast.Assign):
                owns_pytest_plugins = any(
                    isinstance(target, ast.Name) and target.id == "pytest_plugins"
                    for target in statement.targets
                )
                value = statement.value
            elif isinstance(statement, ast.AnnAssign):
                owns_pytest_plugins = (
                    isinstance(statement.target, ast.Name)
                    and statement.target.id == "pytest_plugins"
                )
                value = statement.value
            elif isinstance(statement, ast.AugAssign):
                owns_pytest_plugins = (
                    isinstance(statement.target, ast.Name)
                    and statement.target.id == "pytest_plugins"
                )
            if not owns_pytest_plugins:
                continue
            if value is None:
                raise PlanError(
                    f"cannot resolve pytest_plugins declaration in {consumer_path}"
                )
            static_plugins = _static_string_values(value)
            if static_plugins is None:
                raise PlanError(
                    f"cannot resolve pytest_plugins declaration in {consumer_path}"
                )
            pytest_plugin_modules.extend(static_plugins)

        imported_paths: set[str] = set()
        for module_name in pytest_plugin_modules:
            imported = resolve(module_name)
            if imported is not None and imported != consumer_path:
                imported_paths.add(imported)
        for node in ast.walk(tree):
            module_names: list[str] = []
            if isinstance(node, ast.Import):
                module_names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = _relative_import_name(
                        importer_module,
                        level=node.level,
                        module=node.module,
                    )
                else:
                    base = node.module or ""
                if base:
                    module_names.append(base)
                    if resolve(base) is None:
                        for alias in node.names:
                            if alias.name != "*":
                                module_names.append(f"{base}.{alias.name}")
            elif isinstance(node, ast.Call) and node.args:
                dynamic_import = False
                if isinstance(node.func, ast.Name):
                    dynamic_import = node.func.id in (
                        import_module_bindings | builtin_import_bindings
                    )
                elif isinstance(node.func, ast.Attribute) and isinstance(
                    node.func.value, ast.Name
                ):
                    dynamic_import = (
                        node.func.attr == "import_module"
                        and node.func.value.id in importlib_bindings
                    ) or (
                        node.func.attr == "__import__"
                        and node.func.value.id in builtins_bindings
                    )
                if dynamic_import:
                    static_modules = _static_string_values(node.args[0])
                    if static_modules is not None and len(static_modules) == 1:
                        module_names.extend(static_modules)
                    elif _expression_mentions_test_module(node.args[0]):
                        raise PlanError(
                            f"cannot resolve dynamic test module import in {consumer_path}"
                        )

            for module_name in module_names:
                imported = resolve(module_name)
                if imported is not None and imported != consumer_path:
                    imported_paths.add(imported)

        for imported in imported_paths:
            reverse.setdefault(imported, set()).add(consumer_path)
    return {path: frozenset(consumers) for path, consumers in reverse.items()}


def _test_dependency_closure(
    path: str, reverse_dependencies: Mapping[str, frozenset[str]]
) -> set[str]:
    selected = {path}
    pending = [path]
    while pending:
        imported = pending.pop()
        for consumer in sorted(reverse_dependencies.get(imported, ())):
            if consumer in selected:
                continue
            selected.add(consumer)
            pending.append(consumer)
    return selected


def _is_macos_recovery_test(path: str, config: Mapping[str, Any]) -> bool:
    return any(
        _matches_input(path, pattern)
        for pattern in config[_MACOS_RECOVERY_TEST_INPUTS_KEY]
    )


def _add_python_target(
    path: str, targets: set[str], suites: set[str], reasons: set[str]
) -> str | None:
    shared_prefixes = (
        "src/opensquilla/agent/",
        "src/opensquilla/agents/",
        "src/opensquilla/application/",
        "src/opensquilla/engine/",
        "src/opensquilla/safety/",
    )
    if path.startswith(shared_prefixes):
        suites.discard("python-targeted")
        suites.add("python-full")
        targets.clear()
        targets.add("tests")
        reasons.add("python_shared_core")
        return "shared"

    for prefixes, rule_targets in _PYTHON_TARGET_RULES:
        if any(path == prefix or path.startswith(prefix) for prefix in prefixes):
            if "python-full" not in suites:
                suites.add("python-targeted")
                targets.update(rule_targets)
            reasons.add("python_targeted")
            return "targeted"

    platform_prefixes = (
        "src/opensquilla/artifact_session/",
        "src/opensquilla/migration/",
        "src/opensquilla/persistence/",
        "src/opensquilla/recovery/",
        "src/opensquilla/sandbox/",
        "src/opensquilla/session/",
        "src/opensquilla/tools/",
        "src/opensquilla/uninstall/",
    )
    platform_exact_prefixes = (
        "src/opensquilla/artifact",
        "src/opensquilla/gateway_lifecycle",
        "src/opensquilla/process_ownership",
        "src/opensquilla/process_tree",
        "src/opensquilla/profile",
        "src/opensquilla/prompt_annotations",
        "src/opensquilla/shell",
        "src/opensquilla/tool_boundary",
    )
    if path.startswith(platform_prefixes) or path.startswith(platform_exact_prefixes):
        if "python-full" not in suites:
            suites.add("python-targeted")
            targets.update(
                {
                    "tests/test_desktop",
                    "tests/test_migration",
                    "tests/test_migrations",
                    "tests/test_persistence",
                    "tests/test_recovery",
                    "tests/test_sandbox",
                    "tests/test_session",
                    "tests/test_tools",
                }
            )
        reasons.add("python_platform_sensitive")
        return "platform"
    return None


def _add_test_target(
    path: str,
    targets: set[str],
    suites: set[str],
    reasons: set[str],
    windows_shards: set[str],
    assignments: Mapping[str, str],
    reverse_dependencies: Mapping[str, frozenset[str]],
    repo: Path,
    ref: str | None,
) -> set[str] | None:
    """Select one governed test plus every recursive test-module consumer."""

    if not isinstance(assignments.get(path), str):
        return None
    execution_target = _safe_test_execution_target(path, repo=repo, ref=ref)
    if execution_target is None:
        reasons.add("deleted_test_without_safe_target")
        return None

    selected_paths = _test_dependency_closure(path, reverse_dependencies)
    selected_targets = {path: execution_target}
    for selected_path in sorted(selected_paths - {path}):
        shard = assignments.get(selected_path)
        if not isinstance(shard, str):
            reasons.add("test_dependency_ungoverned")
            return None
        if not _path_exists(repo, selected_path, ref=ref, directory=False):
            reasons.add("test_dependency_missing")
            return None
        selected_targets[selected_path] = selected_path

    if "python-full" not in suites:
        suites.add("python-targeted")
        targets.update(selected_targets.values())
    suites.add("windows-high-risk")
    windows_shards.update(str(assignments[selected_path]) for selected_path in selected_paths)
    reasons.add("test_only_targeted")
    if execution_target != path:
        reasons.add("deleted_test_targeted")
    if len(selected_paths) > 1:
        reasons.add("test_dependency_closure")
    return selected_paths


def _tracked_blob_ids(repo: Path, ref: str) -> dict[str, tuple[str, str]]:
    completed = subprocess.run(
        ["git", "ls-tree", "-r", "-z", ref],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    result: dict[str, tuple[str, str]] = {}
    for raw_entry in completed.stdout.split(b"\0"):
        if not raw_entry:
            continue
        metadata, raw_path = raw_entry.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split()
        if object_type != "blob":
            continue
        result[raw_path.decode("utf-8", errors="strict")] = (mode, object_id)
    return result


def _tracked_files(repo: Path) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return sorted(
            path.relative_to(repo).as_posix()
            for path in repo.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(repo).parts
        )
    return sorted(
        item.decode("utf-8", errors="strict")
        for item in completed.stdout.split(b"\0")
        if item
    )


def _matches_input(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        return fnmatch.fnmatchcase(path, f"{pattern[:-3]}/*")
    return fnmatch.fnmatchcase(path, pattern)


def _repository_files_for_validation(repo: Path) -> list[str]:
    """Return tracked and non-ignored pending files for config validation."""

    try:
        completed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return sorted(
            path.relative_to(repo).as_posix()
            for path in repo.rglob("*")
            if (path.is_file() or path.is_symlink())
            and ".git" not in path.relative_to(repo).parts
        )
    return sorted(
        item.decode("utf-8", errors="strict")
        for item in completed.stdout.split(b"\0")
        if item
    )


def _validate_execution_input_patterns(
    config: Mapping[str, Any], repo: Path
) -> None:
    """Reject suite input patterns that cannot contribute to a repository digest."""

    files = _repository_files_for_validation(repo)
    if not files:
        raise PlanError(f"cannot validate suite execution_inputs in empty repository {repo}")

    unmatched: list[tuple[str, str]] = []
    for suite_id, raw_suite in sorted(config["suites"].items()):
        for pattern in raw_suite["execution_inputs"]:
            if not any(_matches_input(path, pattern) for path in files):
                unmatched.append((suite_id, pattern))
    if unmatched:
        details = ", ".join(
            f"{suite_id}:{pattern}" for suite_id, pattern in unmatched
        )
        raise PlanError(
            "suite execution_inputs match no repository files: " + details
        )


def _blob_digest(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_symlink():
        digest.update(b"symlink\0")
        digest.update(os.readlink(path).encode("utf-8"))
    else:
        digest.update(b"file\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def suite_execution_digests(
    suite_ids: Iterable[str],
    *,
    repo: Path,
    config: Mapping[str, Any],
    ref: str | None = None,
) -> dict[str, str]:
    """Hash each required suite's contract and matching repository inputs."""

    tracked_blobs: dict[str, tuple[str, str]] | None
    try:
        tracked_blobs = _tracked_blob_ids(repo, ref or "HEAD")
    except (OSError, subprocess.CalledProcessError, ValueError):
        if ref is not None:
            raise PlanError(f"cannot resolve suite execution digest ref {ref!r}")
        tracked_blobs = None
    files = sorted(tracked_blobs) if tracked_blobs is not None else _tracked_files(repo)
    blob_cache: dict[str, str] = {}
    result: dict[str, str] = {}
    raw_suites = config["suites"]
    for suite_id in sorted(set(suite_ids)):
        raw_suite = raw_suites[suite_id]
        patterns = raw_suite["execution_inputs"]
        matched = sorted(
            path for path in files if any(_matches_input(path, pattern) for pattern in patterns)
        )
        digest = hashlib.sha256()
        digest.update(_canonical_bytes({"schema_version": SCHEMA_VERSION, **raw_suite}))
        for relative in matched:
            encoded = relative.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
            if tracked_blobs is not None:
                mode, object_id = tracked_blobs[relative]
                digest.update(mode.encode("ascii"))
                digest.update(bytes.fromhex(object_id))
            else:
                if relative not in blob_cache:
                    blob_cache[relative] = _blob_digest(repo / relative)
                digest.update(bytes.fromhex(blob_cache[relative]))
        result[suite_id] = digest.hexdigest()
    return result


def _execution_matrices(
    required_suites: Sequence[str],
    desktop_cells: set[tuple[str, str]],
    targeted_windows_shards: set[str],
    windows_full_matrix: bool,
    config: Mapping[str, Any],
) -> tuple[dict[str, list[str]], list[dict[str, str]]]:
    """Return canonical Python and all-platform execution matrices."""

    suites = set(required_suites)
    python_matrix = {
        "ubuntu": (
            list(config["full_python_matrix"]["ubuntu"])
            if "python-full" in suites
            else []
        ),
        "windows": (
            (
                list(config["full_python_matrix"]["windows"])
                if windows_full_matrix
                else sorted(targeted_windows_shards)
            )
            if "windows-high-risk" in suites
            else []
        ),
    }
    cells: set[tuple[str, str, str]] = set()
    for suite_id in suites:
        for os_name, shard in _FIXED_PLATFORM_MATRIX.get(suite_id, ()):
            cells.add((suite_id, os_name, shard))
    if "desktop-recovery-e2e" in suites:
        cells.update(
            ("desktop-recovery-e2e", os_name, shard)
            for os_name, shard in desktop_cells
        )
    if "python-full" in suites:
        cells.update(
            ("python-full", "ubuntu-latest", shard)
            for shard in python_matrix["ubuntu"]
        )
    if "windows-high-risk" in suites:
        cells.update(
            ("windows-high-risk", "windows-latest", shard)
            for shard in python_matrix["windows"]
        )
    missing = sorted(
        suites
        - set(_FIXED_PLATFORM_MATRIX)
        - {"desktop-recovery-e2e", "python-full", "windows-high-risk"}
    )
    if missing:
        raise PlanError("suite platform matrix is missing: " + ", ".join(missing))
    platform_matrix = [
        {"suite": suite_id, "os": os_name, "shard": shard}
        for suite_id, os_name, shard in sorted(cells)
    ]
    return python_matrix, platform_matrix


def plan_changes(
    changed_paths: Iterable[str],
    *,
    repo: Path,
    config: Mapping[str, Any],
    ref: str | None = None,
) -> dict[str, object]:
    """Return a canonicalizable suite plan for *changed_paths*."""

    paths, invalid_paths = _normalize_changed_paths(changed_paths)
    suites = set(config["baseline_suites"])
    reasons: set[str] = set()
    targets: set[str] = set()
    targeted_windows_shards: set[str] = set()
    windows_full_matrix = False
    desktop_cells: set[tuple[str, str]] = set()
    test_reverse_dependencies: dict[str, frozenset[str]] | None = None
    test_dependency_analysis_failed = False
    full_fallback = False
    all_docs = bool(paths) and not invalid_paths

    if invalid_paths:
        full_fallback = True
        reasons.add("invalid_changed_path")
        all_docs = False
    if not paths:
        full_fallback = True
        reasons.add("empty_change_set")
        all_docs = False

    for path in paths:
        if _is_docs(path):
            continue
        all_docs = False

        if path == ".ci/run-all":
            full_fallback = True
            reasons.add("explicit_full")
            continue
        if _is_dependency(path):
            full_fallback = True
            reasons.add("dependency_changed")
            continue
        if path == ".github/scripts/windows_test_durations.json":
            suites.add("python-targeted")
            targets.add("tests/test_ci/test_windows_test_shards.py")
            reasons.add("scheduling_metadata_changed")
            continue
        if path == ".github/scripts/windows_test_assignments.json":
            suites.update({"python-targeted", "windows-high-risk"})
            targets.add("tests/test_ci/test_windows_test_shards.py")
            windows_full_matrix = True
            reasons.add("windows_shard_layout_changed")
            continue
        if (
            path.startswith(".github/workflows/")
            or path.startswith(".github/scripts/")
            or path.startswith(".github/ci/")
            or path.startswith("tests/test_ci/")
        ):
            full_fallback = True
            reasons.add("ci_policy_changed")
            continue

        if path.startswith("tests/"):
            assignments = _windows_test_assignments(config)
            if not isinstance(assignments.get(path), str):
                full_fallback = True
                reasons.add("unknown_path")
                continue
            if test_dependency_analysis_failed:
                full_fallback = True
                reasons.add("test_dependency_analysis_uncertain")
                continue
            if test_reverse_dependencies is None:
                try:
                    test_reverse_dependencies = _build_test_reverse_dependencies(
                        repo=repo,
                        ref=ref,
                        governed_paths=assignments,
                    )
                except PlanError:
                    test_dependency_analysis_failed = True
                    full_fallback = True
                    reasons.add("test_dependency_analysis_uncertain")
                    continue

            selected_test_paths = _add_test_target(
                path,
                targets,
                suites,
                reasons,
                targeted_windows_shards,
                assignments,
                test_reverse_dependencies,
                repo,
                ref,
            )
            if selected_test_paths is None:
                full_fallback = True
                reasons.add("test_dependency_unsafe")
                continue

            for selected_test_path in sorted(selected_test_paths):
                if selected_test_path.startswith("tests/test_packaging/") or (
                    selected_test_path == "tests/test_release_consistency.py"
                ):
                    suites.add("release-packaging")
                    reasons.add("packaging_changed")
                if _managed_toolchain_targets(selected_test_path) is not None:
                    suites.add("managed-toolchain")
                    reasons.add("toolchain_changed")
                if _is_macos_recovery_test(selected_test_path, config):
                    suites.add("macos-recovery")
                    reasons.add("macos_recovery_test_changed")

                # Test filenames often contain product-domain words such as
                # ``workbench`` or ``recovery``. Those words do not change the
                # product and must not wake native E2E. A reviewed path_patterns
                # entry remains the explicit escape hatch for a test that really
                # owns a Desktop harness contract.
                groups = _explicit_desktop_groups(selected_test_path, config)
                if groups:
                    suites.add("desktop-recovery-e2e")
                    desktop_cells.update(
                        _desktop_cells(
                            groups=groups,
                            os_scope=_os_scope(selected_test_path),
                            config=config,
                        )
                    )
                    reasons.update(
                        f"desktop_{group}_test_changed" for group in groups
                    )
            continue

        if path.startswith("opensquilla-webui/") or path.startswith(
            "src/opensquilla/gateway/static/dist/"
        ):
            suites.update({"frontend", "webui-chat-recovery"})
            reasons.add("webui_changed")
            os_scope = _os_scope(path)
            _add_os_reason_codes(os_scope, reasons)
            groups = _desktop_groups(path, config)
            if groups or "platform/desktop" in path.casefold():
                suites.update({"desktop-recovery-e2e", "desktop-static"})
                desktop_cells.update(
                    _desktop_cells(groups=groups, os_scope=os_scope, config=config)
                )
                reasons.update(f"desktop_{group}_changed" for group in groups)
            continue

        if path.startswith("desktop/"):
            os_scope = _os_scope(path)
            _add_os_reason_codes(os_scope, reasons)
            suites.update({"desktop-recovery-e2e", "desktop-static", "frontend"})
            if not os_scope or "macos-latest" in os_scope:
                suites.add("macos-recovery")
            if not os_scope or "windows-latest" in os_scope:
                suites.add("windows-high-risk")
                windows_full_matrix = True
            groups = _desktop_groups(path, config)
            if groups:
                reasons.update(f"desktop_{group}_changed" for group in groups)
            else:
                reasons.add("desktop_generic_changed")
            if not groups or "profiles" in groups:
                suites.add("webui-chat-recovery")
            desktop_cells.update(
                _desktop_cells(groups=groups, os_scope=os_scope, config=config)
            )
            continue

        if path in _PACKAGING_EXACT or path.startswith(
            ("src/opensquilla/uninstall/",)
        ):
            suites.update({"release-packaging", "windows-high-risk"})
            windows_full_matrix = True
            reasons.add("packaging_changed")
            continue

        managed_toolchain_targets = _managed_toolchain_targets(path)
        if managed_toolchain_targets is not None:
            suites.update({"managed-toolchain", "python-targeted", "windows-high-risk"})
            windows_full_matrix = True
            targets.update(managed_toolchain_targets)
            reasons.add("toolchain_changed")
            continue

        if path.startswith("src/opensquilla/cli/tui/opentui/package/") or path.startswith(
            "packages/opensquilla-tui-host/"
        ):
            suites.add("tui")
            reasons.add("tui_changed")
            continue

        os_scope = _os_scope(path)
        _add_os_reason_codes(os_scope, reasons)
        if path.startswith("src/opensquilla/"):
            python_kind = _add_python_target(path, targets, suites, reasons)
            if python_kind is not None:
                groups = _desktop_groups(path, config)
                if groups:
                    desktop_cells.update(
                        _desktop_cells(groups=groups, os_scope=os_scope, config=config)
                    )
                    suites.update({"desktop-recovery-e2e", "frontend"})
                    if "profiles" in groups:
                        suites.add("webui-chat-recovery")
                    if not os_scope or "macos-latest" in os_scope:
                        suites.add("macos-recovery")
                    if not os_scope or "windows-latest" in os_scope:
                        suites.add("windows-high-risk")
                        windows_full_matrix = True
                    reasons.update(f"desktop_{group}_changed" for group in groups)
                elif path.startswith("src/opensquilla/gateway/"):
                    suites.add("webui-chat-recovery")
                if python_kind == "platform":
                    if not os_scope or "macos-latest" in os_scope:
                        suites.add("macos-recovery")
                    if not os_scope or "windows-latest" in os_scope:
                        suites.add("windows-high-risk")
                        windows_full_matrix = True
                    if not groups:
                        desktop_cells.update(
                            _desktop_cells(groups=groups, os_scope=os_scope, config=config)
                        )
                    if desktop_cells:
                        suites.add("desktop-recovery-e2e")
                        if not groups or "profiles" in groups:
                            suites.add("webui-chat-recovery")
                elif os_scope:
                    if "windows-latest" in os_scope:
                        suites.add("windows-high-risk")
                        windows_full_matrix = True
                    if "macos-latest" in os_scope:
                        suites.add("macos-recovery")
                continue
            full_fallback = True
            reasons.add("unknown_path")
            continue

        if path.startswith("migrations/"):
            suites.update(
                {
                    "desktop-recovery-e2e",
                    "macos-recovery",
                    "python-targeted",
                    "webui-chat-recovery",
                    "windows-high-risk",
                }
            )
            windows_full_matrix = True
            targets.update({"tests/test_migration", "tests/test_migrations"})
            desktop_cells.update(
                _desktop_cells(groups={"profiles"}, os_scope=os_scope, config=config)
            )
            reasons.add("python_platform_sensitive")
            continue

        if path.startswith("service-units/"):
            suites.update({"python-targeted", "release-packaging"})
            targets.add("tests/test_packaging")
            reasons.update({"linux_specific_changed", "packaging_changed"})
            continue

        full_fallback = True
        reasons.add("unknown_path")

    if all_docs:
        reasons.add("docs_only")

    if full_fallback:
        suites = set(config["full_suites"])
        desktop_cells = {
            (str(cell["os"]), str(cell["shard"]))
            for cell in config["full_desktop_matrix"]
        }
        targets = {"tests"}
        windows_full_matrix = True
    elif "python-full" in suites:
        suites.discard("python-targeted")
        suites.discard("windows-compat")
        targets = {"tests"}

    # Both browser and Electron consumers download the verified WebUI artifact.
    # Keep this dependency closure in the planner so a selected consumer can
    # never be skipped merely because its producer was omitted.
    if suites.intersection({"webui-chat-recovery", "desktop-recovery-e2e"}):
        suites.add("frontend")

    required_suites = sorted(suites)
    python_matrix, platform_matrix = _execution_matrices(
        required_suites,
        desktop_cells,
        targeted_windows_shards,
        windows_full_matrix,
        config,
    )
    digests = suite_execution_digests(
        required_suites, repo=repo, config=config, ref=ref
    )
    payload: dict[str, object] = {
        "required_suites": required_suites,
        "desktop_matrix": [
            {"os": os_name, "shard": shard}
            for os_name, shard in sorted(desktop_cells)
        ],
        "python_matrix": python_matrix,
        "platform_matrix": platform_matrix,
        "python_targets": sorted(targets),
        "full_fallback": full_fallback,
        "reason_codes": sorted(reasons),
        "suite_execution_digests": digests,
    }
    payload["plan_digest"] = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    return payload


def _read_changed_file(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PlanError(f"cannot read changed-files list {path}: {exc}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("changed_files", type=Path)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path)
    parser.add_argument("--ref")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = args.repo.resolve()
    config_path = (
        args.config.resolve() if args.config else (repo / DEFAULT_CONFIG).resolve()
    )
    try:
        plan = plan_changes(
            _read_changed_file(args.changed_files),
            repo=repo,
            config=load_config(config_path, repo=repo),
            ref=args.ref,
        )
    except PlanError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    rendered = canonical_json(plan)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
