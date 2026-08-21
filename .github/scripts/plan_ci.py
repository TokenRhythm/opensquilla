#!/usr/bin/env python3
"""Build a deterministic, fail-closed CI suite plan from changed paths."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Final

SCHEMA_VERSION: Final = 1
DEFAULT_CONFIG: Final = Path(".github/ci/suites.v1.json")

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
_PLATFORM_TEST_PREFIXES: Final = (
    "tests/test_compat/",
    "tests/test_desktop/",
    "tests/test_migration/",
    "tests/test_migrations/",
    "tests/test_packaging/",
    "tests/test_persistence/",
    "tests/test_recovery/",
    "tests/test_sandbox/",
    "tests/test_scheduler/",
    "tests/test_session/",
    "tests/test_uninstall/",
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
_TEST_TARGET_RULES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("tests/test_provider", ("tests/test_provider", "tests/test_provider*.py")),
    (
        "tests/test_gateway",
        (
            "tests/functional/test_gateway_*_e2e.py",
            "tests/test_gateway",
            "tests/test_gateway*.py",
        ),
    ),
    ("tests/test_engine", ("tests/test_engine", "tests/test_engine*.py")),
    ("tests/test_channels/", ("tests/test_channels",)),
    ("tests/test_memory", ("tests/test_memory", "tests/test_memory*.py")),
    (
        "tests/test_skills",
        ("tests/test_meta_skill*.py", "tests/test_skills", "tests/test_skills*.py"),
    ),
    ("tests/test_cli/", ("tests/test_cli",)),
    ("tests/integration/cli/", ("tests/integration/cli", "tests/test_cli")),
    ("tests/test_onboarding/", ("tests/test_onboarding",)),
    ("tests/test_identity/", ("tests/test_identity",)),
    ("tests/test_mcp/", ("tests/test_mcp",)),
    ("tests/test_health/", ("tests/test_health",)),
    ("tests/test_observability/", ("tests/test_observability",)),
    ("tests/test_search/", ("tests/test_search",)),
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


def _desktop_groups(path: str, config: Mapping[str, Any]) -> set[str]:
    explicit = {
        str(group)
        for group, raw_group in config["desktop_groups"].items()
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in raw_group["path_patterns"])
    }
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
        if "profiles" in selected_groups:
            cells.add(("macos-latest", "profiles"))
        if selected_groups.intersection({"ownership", "workbench"}):
            cells.add(("macos-latest", "ownership-workbench"))
    if "windows-latest" in platforms:
        cells.update(("windows-latest", group) for group in selected_groups)
    return cells


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
    path: str, targets: set[str], suites: set[str], reasons: set[str]
) -> bool:
    if path.startswith(_PLATFORM_TEST_PREFIXES):
        if "python-full" not in suites:
            suites.add("python-targeted")
            targets.add(str(PurePosixPath(path).parent))
        reasons.add("python_platform_sensitive")
        return True
    for prefix, rule_targets in _TEST_TARGET_RULES:
        if path.startswith(prefix):
            if "python-full" not in suites:
                suites.add("python-targeted")
                targets.update(rule_targets)
            reasons.add("python_targeted")
            return True
    return False


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
            list(config["full_python_matrix"]["windows"])
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
    desktop_cells: set[tuple[str, str]] = set()
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
            ("src/opensquilla/uninstall/", "tests/test_packaging/")
        ):
            suites.update({"release-packaging", "windows-high-risk"})
            reasons.add("packaging_changed")
            continue

        managed_toolchain_targets = _managed_toolchain_targets(path)
        if managed_toolchain_targets is not None:
            suites.update({"managed-toolchain", "python-targeted", "windows-high-risk"})
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
                    reasons.update(f"desktop_{group}_changed" for group in groups)
                elif path.startswith("src/opensquilla/gateway/"):
                    suites.add("webui-chat-recovery")
                if python_kind == "platform":
                    if not os_scope or "macos-latest" in os_scope:
                        suites.add("macos-recovery")
                    if not os_scope or "windows-latest" in os_scope:
                        suites.add("windows-high-risk")
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
                    if "macos-latest" in os_scope:
                        suites.add("macos-recovery")
                continue
            full_fallback = True
            reasons.add("unknown_path")
            continue

        if path.startswith("tests/"):
            if _add_test_target(path, targets, suites, reasons):
                groups = _desktop_groups(path, config)
                if path.startswith(_PLATFORM_TEST_PREFIXES) or groups:
                    suites.update({"macos-recovery", "windows-high-risk"})
                    desktop_cells.update(
                        _desktop_cells(groups=groups, os_scope=os_scope, config=config)
                    )
                    if desktop_cells:
                        suites.update({"desktop-recovery-e2e", "webui-chat-recovery"})
                    reasons.update(f"desktop_{group}_changed" for group in groups)
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
