"""Dependency summary analyzer for Skills UI diagnostics."""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from opensquilla.skills.eligibility import (
    EligibilityContext,
    EligibilityReport,
    _has_bin,
    diagnose_eligibility,
)
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.types import SkillSpec

_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_BASE_URL", "_ENDPOINT")
_STD_LIB_MODULES = set(getattr(sys, "stdlib_module_names", ()))
_PACKAGE_IMPORT_ALIASES: dict[str, set[str]] = {
    "beautifulsoup4": {"bs4"},
    "dingtalk-stream": {"dingtalk_stream"},
    "lark-oapi": {"lark_oapi"},
    "matrix-nio": {"nio"},
    "pydantic-settings": {"pydantic_settings"},
    "python-docx": {"docx"},
    "python-multipart": {"multipart"},
    "python-pptx": {"pptx"},
    "python-telegram-bot": {"telegram"},
    "qq-botpy": {"botpy"},
    "readability-lxml": {"readability"},
    "sqlite-vec": {"sqlite_vec"},
    "yoyo-migrations": {"yoyo"},
}


def build_dependency_summary(
    spec: SkillSpec,
    *,
    loader: SkillLoader | None = None,
    ctx: EligibilityContext | None = None,
    report: EligibilityReport | None = None,
    _seen: set[str] | None = None,
) -> dict[str, Any]:
    """Build a manifest-led dependency summary with advisory static hints."""
    ctx = ctx or EligibilityContext.auto()
    seen = set(_seen or ())
    if spec.name in seen:
        summary = _empty_summary()
        summary["inferred"]["scan_errors"].append(f"Cyclic sub-skill reference: {spec.name}")
        return summary
    seen.add(spec.name)

    requires = spec.metadata.requires if spec.metadata and spec.metadata.requires else None
    report = report or diagnose_eligibility(spec, ctx)

    declared_any_bins = list(requires.any_bins) if requires else []
    declared_all_bins = list(requires.bins) if requires else []
    declared_all_env = list(requires.env) if requires else []
    declared_any_env = list(requires.env_any) if requires else []
    declared_python_packages = _declared_python_packages(spec)

    inferred_python_imports, inferred_api_env, scan_errors = _scan_inferred_dependencies(
        spec,
        declared_python_packages=declared_python_packages,
        declared_api_env=set(declared_all_env) | set(declared_any_env),
    )
    sub_skill_dependencies = _build_sub_skill_dependencies(
        spec,
        loader=loader,
        ctx=ctx,
        seen=seen,
    )

    summary = _empty_summary()
    summary["declared"]["binaries"]["all"] = declared_all_bins
    summary["declared"]["binaries"]["any"] = declared_any_bins
    summary["declared"]["python_packages"] = declared_python_packages
    summary["declared"]["api_env"]["all"] = declared_all_env
    summary["declared"]["api_env"]["any"] = declared_any_env
    summary["missing"]["binaries"]["all"] = [
        binary for binary in declared_all_bins if binary in report.missing_bins
    ]
    summary["missing"]["binaries"]["any"] = _missing_any_bin_groups(declared_any_bins, ctx=ctx)
    summary["missing"]["api_env"]["all"] = list(report.missing_env)
    summary["missing"]["api_env"]["any"] = [list(group) for group in report.missing_env_any]
    summary["missing"]["count"] = (
        len(summary["missing"]["binaries"]["all"])
        + len(summary["missing"]["binaries"]["any"])
        + len(summary["missing"]["api_env"]["all"])
        + len(summary["missing"]["api_env"]["any"])
    )
    summary["inferred"]["python_imports"] = inferred_python_imports
    summary["inferred"]["api_env"] = inferred_api_env
    summary["inferred"]["scan_errors"] = scan_errors
    summary["sub_skill_dependencies"] = sub_skill_dependencies
    summary["declaration_quality"] = _declaration_quality(
        report_declared=report.declared,
        inferred_python_imports=inferred_python_imports,
        inferred_api_env=inferred_api_env,
        sub_skill_dependencies=sub_skill_dependencies,
    )
    return summary


def _empty_summary() -> dict[str, Any]:
    return {
        "declared": {
            "binaries": {"all": [], "any": []},
            "python_packages": [],
            "api_env": {"all": [], "any": []},
        },
        "missing": {
            "binaries": {"all": [], "any": []},
            "api_env": {"all": [], "any": []},
            "count": 0,
        },
        "inferred": {
            "python_imports": [],
            "api_env": [],
            "scan_errors": [],
        },
        "sub_skill_dependencies": {
            "skills": [],
            "missing_count": 0,
            "inferred_count": 0,
            "missing_references": [],
        },
        "declaration_quality": "none",
    }


def _declared_python_packages(spec: SkillSpec) -> list[dict[str, str]]:
    if spec.metadata is None:
        return []
    out: list[dict[str, str]] = []
    for install in spec.metadata.install:
        if install.kind != "uv":
            continue
        out.append(
            {
                "install_id": install.id,
                "label": install.label,
                "package": install.package,
                "module": install.module,
            }
        )
    return out


def _scan_inferred_dependencies(
    spec: SkillSpec,
    *,
    declared_python_packages: list[dict[str, str]],
    declared_api_env: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    python_imports: set[tuple[str, str]] = set()
    script_env_map: dict[str, set[str]] = {}
    scan_errors: list[str] = []
    script_paths = _script_paths(spec)
    declared_modules = _declared_python_modules(declared_python_packages)
    project_modules = _project_modules()

    for script_path in script_paths:
        try:
            tree = ast.parse(script_path.read_text(encoding="utf-8"), filename=str(script_path))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            scan_errors.append(f"{_relative_skill_path(spec, script_path)}: {exc}")
            continue

        for module_name in _imports_from_tree(tree):
            if module_name in _STD_LIB_MODULES:
                continue
            if module_name == "opensquilla":
                continue
            if _is_local_import(module_name, spec=spec, script_path=script_path):
                continue
            if module_name in declared_modules or module_name in project_modules:
                continue
            python_imports.add((module_name, _relative_skill_path(spec, script_path)))

        env_names = _env_get_calls_from_tree(tree)
        if env_names:
            script_env_map.setdefault(_relative_skill_path(spec, script_path), set()).update(
                env_names - declared_api_env
            )

    inferred_python_imports = [
        {
            "module": module_name,
            "source": source,
            "not_enforced": True,
        }
        for module_name, source in sorted(python_imports)
    ]
    inferred_api_env = _merge_inferred_api_env(
        script_env_map=script_env_map,
        markdown_names=_markdown_env_candidates(spec) - declared_api_env,
    )
    return inferred_python_imports, inferred_api_env, scan_errors


def _script_paths(spec: SkillSpec) -> list[Path]:
    skill_dir = _skill_dir(spec)
    if skill_dir is None:
        return []
    scripts_dir = skill_dir / "scripts"
    if not scripts_dir.exists():
        return []
    return sorted(path for path in scripts_dir.rglob("*.py") if path.is_file())


def _missing_any_bin_groups(
    declared_any_bins: list[str],
    *,
    ctx: EligibilityContext,
) -> list[list[str]]:
    if not declared_any_bins:
        return []
    if any(_has_bin(binary, ctx) for binary in declared_any_bins):
        return []
    return [list(declared_any_bins)]


def _skill_dir(spec: SkillSpec) -> Path | None:
    if spec.path is not None:
        return spec.path
    base_dir = getattr(spec, "base_dir", "")
    if not base_dir:
        return None
    return Path(base_dir)


def _relative_skill_path(spec: SkillSpec, path: Path) -> str:
    skill_dir = _skill_dir(spec)
    if skill_dir is None:
        return path.name
    try:
        return path.relative_to(skill_dir).as_posix()
    except ValueError:
        return path.name


def _is_local_import(module_name: str, *, spec: SkillSpec, script_path: Path) -> bool:
    skill_dir = _skill_dir(spec)
    if skill_dir is None:
        return False
    scripts_dir = skill_dir / "scripts"
    for base in (script_path.parent, scripts_dir, skill_dir):
        if _module_exists_under(base, module_name):
            return True
    return False


def _module_exists_under(base: Path, module_name: str) -> bool:
    module_dir = base / module_name
    module_file = base / f"{module_name}.py"
    return module_dir.is_dir() or module_file.is_file()


def _imports_from_tree(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root:
                    modules.add(root)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module:
                root = node.module.split(".", 1)[0]
                if root:
                    modules.add(root)
    return modules


def _env_get_calls_from_tree(tree: ast.AST) -> set[str]:
    env_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_os_environ_get(node):
            continue
        if not node.args:
            continue
        first_arg = node.args[0]
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            if first_arg.value:
                env_names.add(first_arg.value)
    return env_names


def _is_os_environ_get(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "get":
        return False
    value = func.value
    if not isinstance(value, ast.Attribute) or value.attr != "environ":
        return False
    base = value.value
    return isinstance(base, ast.Name) and base.id == "os"


def _merge_inferred_api_env(
    *,
    script_env_map: dict[str, set[str]],
    markdown_names: set[str],
) -> list[dict[str, Any]]:
    merged: dict[str, set[str]] = {}
    for source, names in script_env_map.items():
        for name in names:
            merged.setdefault(name, set()).add(source)
    for name in markdown_names:
        merged.setdefault(name, set()).add("SKILL.md")
    return [
        {
            "name": name,
            "sources": sorted(sources),
            "not_enforced": True,
        }
        for name, sources in sorted(merged.items())
    ]


def _markdown_env_candidates(spec: SkillSpec) -> set[str]:
    skill_dir = _skill_dir(spec)
    skill_text = spec.content
    if skill_dir is not None:
        skill_file = skill_dir / "SKILL.md"
        if skill_file.exists():
            try:
                skill_text = skill_file.read_text(encoding="utf-8")
            except OSError:
                pass
    pattern = r"\b[A-Z][A-Z0-9_]*(?:_API_KEY|_TOKEN|_BASE_URL|_ENDPOINT)\b"
    return {
        match.group(0)
        for match in re.finditer(pattern, skill_text)
        if match.group(0).endswith(_ENV_SUFFIXES)
    }


def _build_sub_skill_dependencies(
    spec: SkillSpec,
    *,
    loader: SkillLoader | None,
    ctx: EligibilityContext,
    seen: set[str],
) -> dict[str, Any]:
    referenced_skills = _referenced_skill_names(spec)
    skills: list[dict[str, Any]] = []
    missing_references: list[str] = []
    missing_count = 0
    inferred_count = 0

    for name in referenced_skills:
        child = loader.get_by_name(name) if loader is not None else None
        if child is None:
            missing_references.append(name)
            continue
        child_summary = build_dependency_summary(child, loader=loader, ctx=ctx, _seen=seen)
        skills.append({"name": name, "summary": child_summary})
        if child_summary["missing"]["count"] > 0:
            missing_count += 1
        if (
            child_summary["inferred"]["python_imports"]
            or child_summary["inferred"]["api_env"]
            or child_summary["inferred"]["scan_errors"]
            or child_summary["sub_skill_dependencies"]["inferred_count"] > 0
        ):
            inferred_count += 1

    return {
        "skills": skills,
        "missing_count": missing_count,
        "inferred_count": inferred_count,
        "missing_references": missing_references,
    }


def _referenced_skill_names(spec: SkillSpec) -> list[str]:
    composition = spec.composition_raw
    if not isinstance(composition, dict):
        return []
    steps = composition.get("steps")
    if not isinstance(steps, list):
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        _append_skill_ref(step.get("skill"), seen, ordered)
        routes = step.get("routes")
        if not isinstance(routes, list):
            continue
        for route in routes:
            if isinstance(route, dict):
                _append_skill_ref(route.get("skill"), seen, ordered)
    return ordered


def _append_skill_ref(raw: object, seen: set[str], ordered: list[str]) -> None:
    if not isinstance(raw, str) or not raw or raw in seen:
        return
    seen.add(raw)
    ordered.append(raw)


def _declaration_quality(
    *,
    report_declared: bool,
    inferred_python_imports: list[dict[str, Any]],
    inferred_api_env: list[dict[str, Any]],
    sub_skill_dependencies: dict[str, Any],
) -> str:
    has_inferred = bool(
        inferred_python_imports
        or inferred_api_env
        or sub_skill_dependencies["inferred_count"]
        or sub_skill_dependencies["missing_references"]
    )
    if report_declared and has_inferred:
        return "partial"
    if report_declared:
        return "declared"
    if has_inferred:
        return "undeclared_inferred"
    return "none"


def _declared_python_modules(
    declared_python_packages: list[dict[str, str]],
) -> set[str]:
    modules: set[str] = set()
    for package in declared_python_packages:
        for candidate in (
            package.get("module", ""),
            package.get("package", ""),
            package.get("install_id", ""),
        ):
            modules.update(_module_name_candidates(candidate))
    return modules


@lru_cache(maxsize=1)
def _project_modules() -> set[str]:
    repo_root = Path(__file__).resolve().parents[3]
    pyproject_path = repo_root / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return set()

    requirements: list[str] = []
    project = data.get("project")
    if isinstance(project, dict):
        requirements.extend(_string_list(project.get("dependencies")))
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for values in optional.values():
                requirements.extend(_string_list(values))
    dep_groups = data.get("dependency-groups")
    if isinstance(dep_groups, dict):
        for values in dep_groups.values():
            requirements.extend(_string_list(values))

    modules: set[str] = set()
    for requirement in requirements:
        modules.update(_module_name_candidates(_requirement_name(requirement)))
    return modules


def _string_list(raw: object) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


def _requirement_name(requirement: str) -> str:
    requirement = requirement.strip()
    if not requirement:
        return ""
    end = len(requirement)
    for marker in ("[", "<", ">", "=", "!", "~", ";", " "):
        marker_index = requirement.find(marker)
        if marker_index != -1:
            end = min(end, marker_index)
    return requirement[:end]


def _module_name_candidates(raw_name: str) -> set[str]:
    if not raw_name:
        return set()
    normalized = _normalize_name(raw_name)
    candidates = {
        normalized.replace("-", "_"),
        normalized.replace("-", ""),
    }
    alias_candidates = _PACKAGE_IMPORT_ALIASES.get(normalized, set())
    candidates.update(alias_candidates)
    return {candidate for candidate in candidates if candidate}


def _normalize_name(raw_name: str) -> str:
    return raw_name.strip().lower().replace("_", "-")
