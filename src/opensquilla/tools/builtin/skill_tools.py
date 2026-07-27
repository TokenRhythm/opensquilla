"""Skill tools — agent-accessible skill discovery, viewing, and management.

Registered at boot time when a SkillLoader is available.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog

from opensquilla.skills.hub.defaults import (
    build_default_skill_installer,
    get_default_skill_router,
    installed_skill_names,
)
from opensquilla.skills.types import SkillInstallSpec, SkillLayer, SkillSpec
from opensquilla.tools.registry import tool
from opensquilla.tools.types import ToolError, current_tool_context

if TYPE_CHECKING:
    from opensquilla.skills.loader import SkillLoader

logger = structlog.get_logger(__name__)

# Module-level reference set at boot
_loader: SkillLoader | None = None
# Layers that user may mutate — workspace only
_MUTABLE_LAYERS = frozenset({SkillLayer.WORKSPACE})


class _NoCatalogMutationError(Exception):
    """Leave a mutation guard without dirtying after a rejected install."""

    def __init__(self, result: Any) -> None:
        super().__init__()
        self.result = result


def _skill_available(name: str) -> bool:
    """Whether ``name`` may be surfaced/invoked under the live operator config.

    Delegates to the shared eligibility gate (single source of truth) so the
    skill_list / skill_view paths honor exactly the same coding-mode / disabled
    rules as the pre-turn filter and the meta-skill executors.
    """
    from opensquilla.skills.eligibility import is_skill_available_live

    return is_skill_available_live(name)


def _active_catalog() -> Any | None:
    """Return the catalog pinned to the current agent turn, if any."""
    ctx = current_tool_context.get()
    return getattr(ctx, "skill_catalog", None) if ctx is not None else None


def _active_skills() -> list[Any]:
    """Read the pinned generation, or refresh at a standalone tool boundary."""
    catalog = _active_catalog()
    if catalog is not None:
        return list(getattr(catalog, "skills", ()))
    if _loader is None:
        return []
    _loader.refresh_if_changed(reason="tool.skill_catalog")
    return list(_loader.snapshot().skills)


def _active_skill(name: str) -> Any | None:
    catalog = _active_catalog()
    if catalog is not None:
        get_by_name = getattr(catalog, "get_by_name", None)
        if callable(get_by_name):
            return get_by_name(name)
        return next(
            (skill for skill in getattr(catalog, "skills", ()) if skill.name == name),
            None,
        )
    return _loader.get_by_name(name) if _loader is not None else None


# Valid skill name pattern: lowercase alphanumeric + hyphens
_SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9\-]{0,62}$")
_INSTALL_OUTPUT_LIMIT = 4_000
_INSTALL_TIMEOUT_SECONDS = 120.0

_BREW_FORMULA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/_@.+-]*$")
_NODE_PACKAGE_RE = re.compile(r"^(?:@[A-Za-z0-9][A-Za-z0-9._-]*/)?[A-Za-z0-9][A-Za-z0-9._-]*$")
_GO_MODULE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~/-]*(?:@[A-Za-z0-9][A-Za-z0-9._~+-]*)?$")
_UV_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?$")


def _sanitize_yaml_value(value: str) -> str:
    """Strip characters that could inject YAML structure."""
    return value.replace("\n", " ").replace("\r", " ").strip()


def _render_skill_md(
    name: str,
    description: str,
    content: str,
    triggers: list[str] | None = None,
) -> str:
    """Render a SKILL.md file from parts.

    Frontmatter is serialized with the YAML library rather than hand-formatted,
    so punctuation the loader parses as YAML structure (``:``, ``#``, ``[``,
    ``{``, leading quotes, ...) is quoted correctly and round-trips through
    ``skills.loader._parse_frontmatter``. Hand-concatenating unquoted scalars
    silently corrupted or destroyed skills whose description/trigger contained
    such punctuation.
    """
    import yaml

    safe_desc = _sanitize_yaml_value(description)
    fm: dict[str, Any] = {"name": name, "description": safe_desc}
    if triggers:
        fm["triggers"] = [_sanitize_yaml_value(t) for t in triggers]
    frontmatter = yaml.safe_dump(
        fm, sort_keys=False, allow_unicode=True, width=100000
    ).strip()
    return f"---\n{frontmatter}\n---\n\n{content}"


def _cap_output(value: bytes | str, limit: int = _INSTALL_OUTPUT_LIMIT) -> str:
    if isinstance(value, bytes):
        text = value.decode(errors="replace")
    else:
        text = value
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}\n... truncated {omitted} characters"


def _validate_install_value(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not value:
        raise ToolError(f"Missing install value: {label}")
    if value.startswith("-") or not pattern.match(value):
        raise ToolError(f"Unsafe install value for {label}: {value}")
    return value


def _argv_for_install_spec(spec: SkillInstallSpec) -> list[str]:
    kind = spec.kind
    if kind == "download":
        raise ToolError("Install kind 'download' is deferred and cannot be executed")
    if kind == "brew":
        formula = _validate_install_value(
            spec.formula or spec.package,
            _BREW_FORMULA_RE,
            "formula",
        )
        return ["brew", "install", formula]
    if kind == "node":
        package = _validate_install_value(
            spec.package,
            _NODE_PACKAGE_RE,
            "package",
        )
        return ["npm", "install", "-g", "--ignore-scripts", package]
    if kind == "go":
        module = _validate_install_value(
            spec.module or spec.package,
            _GO_MODULE_RE,
            "module",
        )
        if "@" not in module:
            module = f"{module}@latest"
        return ["go", "install", module]
    if kind == "uv":
        package = _validate_install_value(
            spec.package or spec.module,
            _UV_PACKAGE_RE,
            "package",
        )
        return ["uv", "tool", "install", package]
    raise ToolError(f"Unsupported install kind: {kind}")


def _find_install_spec(skill_name: str, install_id: str) -> SkillInstallSpec:
    if install_id.startswith("-"):
        raise ToolError(f"Unsafe install value for install_id: {install_id}")
    if _loader is None:
        raise ToolError("Skill loader not available")

    skill = cast(SkillSpec | None, _active_skill(skill_name))
    if skill is None or not _skill_available(skill_name):
        # Coding-mode-gated skills are reported as not-found so deps cannot be
        # previewed or installed via install_skill_deps while OFF (codex review).
        raise ToolError(f"Skill not found: {skill_name}")
    if skill.metadata is None or not skill.metadata.install:
        raise ToolError(f"Skill has no install metadata: {skill_name}")

    for index, spec in enumerate(skill.metadata.install):
        fallback_id = f"{spec.kind}-{index}"
        if spec.id == install_id or (not spec.id and install_id == fallback_id):
            return spec
    raise ToolError(f"Install spec not found for skill '{skill_name}': {install_id}")


def _community_result_to_dict(row: Any, installed: set[str]) -> dict[str, Any]:
    identifier = getattr(row, "identifier", "") or getattr(row, "name", "")
    name = getattr(row, "name", "")
    return {
        "name": name,
        "description": getattr(row, "description", ""),
        "version": getattr(row, "version", ""),
        "author": getattr(row, "author", ""),
        "source": getattr(row, "source_id", ""),
        "trust_level": getattr(row, "trust_level", ""),
        "identifier": identifier,
        "installed": identifier in installed or name in installed,
    }


async def _run_install_argv(argv: list[str]) -> tuple[int, str, str, bool]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"Install command not found: {argv[0]}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=_INSTALL_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return -1, "", "Timed out", True
    return proc.returncode or 0, _cap_output(stdout), _cap_output(stderr), False


def create_skill_tools(
    loader: SkillLoader, skills_cfg_getter: Callable[[], object] | None = None
) -> None:
    """Register skill tools (list, view, create, edit, delete) with the global registry.

    ``skills_cfg_getter`` returns the live skills config so operator gating
    (coding mode / disabled) is honored at call time, not boot time.
    """
    from opensquilla.skills.eligibility import set_live_skills_config_getter

    global _loader
    _loader = loader
    set_live_skills_config_getter(skills_cfg_getter)

    @tool(
        name="skill_list",
        description="List all available skills with name, description, and eligibility.",
    )
    async def skill_list() -> str:
        if _loader is None:
            return "No skill loader available."
        skills = _active_skills()
        if not skills:
            return "No skills installed."

        from opensquilla.skills.eligibility import EligibilityContext, diagnose_eligibility

        # Hide operator-gated skills (coding mode off / disabled) so the list
        # does not reveal a skill the agent cannot use.
        skills = [s for s in skills if _skill_available(s.name)]
        if not skills:
            return "No skills installed."

        ctx = EligibilityContext.auto()
        lines = [f"Available skills ({len(skills)}):"]
        for s in sorted(skills, key=lambda x: x.name):
            report = diagnose_eligibility(s, ctx)
            lines.append(f"  - {s.name}: {s.description}")
            if not report.eligible:
                missing = []
                for b in report.missing_bins:
                    missing.append(f"{b} (binary)")
                for e in report.missing_env:
                    missing.append(f"{e} (env var)")
                for group in report.missing_env_any:
                    missing.append(f"{' or '.join(group)} (env var group)")
                if report.disabled:
                    missing.append("disabled")
                if report.wrong_os:
                    missing.append("wrong OS")
                if missing:
                    lines.append(f"      [unavailable] Missing: {', '.join(missing)}")
                for hint in report.install_hints:
                    lines.append(f"      Install: {hint.command}")
                for e in report.missing_env:
                    lines.append(f"      Hint: Set environment variable {e}")
                for group in report.missing_env_any:
                    lines.append(
                        "      Hint: Set one of environment variables " + " or ".join(group)
                    )
        return "\n".join(lines)

    @tool(
        name="skill_view",
        description=("Read a skill's SKILL.md content by name. Optionally read a supporting file."),
        params={
            "name": {
                "type": "string",
                "description": "Exact skill name to view",
            },
            "file_path": {
                "type": "string",
                "description": "Optional sub-file path (references/, scripts/)",
            },
        },
        required=["name"],
    )
    async def skill_view(name: str, file_path: str | None = None) -> str:
        if _loader is None:
            return "No skill loader available."
        # Gate operator-disabled / coding-mode skills here too: removing them
        # from <available_skills> is not enough if skill_view can fetch any
        # skill by name. Same message as not-found so it leaks no bypass hint.
        if not _skill_available(name):
            logger.info("skill_view.blocked_by_operator_config", skill=name)
            skill = None
        else:
            skill = _active_skill(name)
        if skill is None:
            return (
                f"Skill not found: {name}. This skill is not available in the "
                "current skill catalog. Do not search host filesystem paths to "
                "recover missing skills. Use skill_list to inspect available "
                "skills, continue with available tools, or tell the user the "
                "skill is not installed."
            )

        if file_path:
            normalized_path = file_path.strip().lstrip("./")
            if normalized_path in {"", "SKILL.md"}:
                return skill.content or f"(Skill '{name}' has no body content)"

            from pathlib import Path

            from opensquilla.skills.resources import SkillResources

            resources = SkillResources(Path(skill.base_dir))
            content = resources.read_resource(normalized_path)
            if content is None:
                return f"File not found in skill '{name}': {file_path}"
            return content

        return skill.content or f"(Skill '{name}' has no body content)"

    @tool(
        name="skill_search_community",
        description=(
            "Search Community skill sources such as ClawHub. Use this when the user asks to "
            "find, search, browse, or locate installable skills from the community marketplace."
        ),
        params={
            "query": {
                "type": "string",
                "description": "Search query for Community skills.",
            },
            "source": {
                "type": "string",
                "description": (
                    "Source id to search, usually 'clawhub'. Use 'all' to search all sources."
                ),
                "default": "clawhub",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return.",
                "default": 10,
            },
        },
        required=["query"],
    )
    async def skill_search_community(
        query: str,
        source: str = "clawhub",
        limit: int = 10,
    ) -> str:
        clean_query = str(query or "").strip()
        if not clean_query:
            raise ToolError("query must not be empty")
        try:
            result_limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            result_limit = 10

        source_id: str | None = str(source or "clawhub").strip() or "clawhub"
        if source_id in {"all", "*"}:
            source_id = None
        router = get_default_skill_router()
        results = await router.search(clean_query, limit=result_limit, source_id=source_id)
        installed = installed_skill_names()
        return json.dumps(
            {
                "status": "ok",
                "query": clean_query,
                "source": source_id or "all",
                "results": [_community_result_to_dict(row, installed) for row in results],
            }
        )

    @tool(
        name="skill_install_community",
        description=(
            "Install a Community skill from ClawHub or another configured source. "
            "Use only when the user clearly asked to install a specific skill identifier "
            "or chose one exact result from skill_search_community. Do not use skill_create "
            "for Community installs."
        ),
        params={
            "identifier": {
                "type": "string",
                "description": (
                    "Exact source identifier or slug returned by skill_search_community."
                ),
            },
            "source": {
                "type": "string",
                "description": "Source id, usually 'clawhub'.",
                "default": "clawhub",
            },
            "force": {
                "type": "boolean",
                "description": (
                    "Override a dangerous security scan only after the user explicitly asks."
                ),
                "default": False,
            },
        },
        required=["identifier"],
        owner_only=True,
    )
    async def skill_install_community(
        identifier: str,
        source: str = "clawhub",
        force: bool = False,
    ) -> str:
        if _loader is None:
            raise ToolError("Skill loader not available")
        clean_identifier = str(identifier or "").strip()
        if not clean_identifier:
            raise ToolError("identifier must not be empty")
        source_id = str(source or "clawhub").strip() or "clawhub"

        installer = build_default_skill_installer(managed_dir=_loader.managed_dir)
        try:
            with _loader.mutation_guard(reason="tool.skill_install_community"):
                result = await installer.install(
                    clean_identifier,
                    source_id,
                    force=bool(force),
                )
                if not result.success:
                    raise _NoCatalogMutationError(result)
        except _NoCatalogMutationError as exc:
            result = exc.result

        payload: dict[str, Any] = {
            "status": "installed" if result.success else "failed",
            "success": result.success,
            "name": result.name,
            "identifier": clean_identifier,
            "source": source_id,
            "message": result.message,
        }
        if result.path:
            payload["path"] = result.path
        if result.scan:
            payload["scan_verdict"] = result.scan.verdict
            payload["scan_findings"] = [finding.__dict__ for finding in result.scan.findings]
        return json.dumps(payload)

    @tool(
        name="install_skill_deps",
        description=(
            "Preview or install a skill dependency declared in skill metadata. "
            "Supports brew, node, go, and uv install specs. This does not install "
            "Community skills; use skill_install_community for ClawHub installs."
        ),
        params={
            "skill_name": {
                "type": "string",
                "description": "Exact skill name containing the install metadata.",
            },
            "install_id": {
                "type": "string",
                "description": "Install spec id from the skill metadata install list.",
            },
            "confirmed": {
                "type": "boolean",
                "description": "When false, return preview JSON. When true, execute argv.",
                "default": False,
            },
        },
        required=["skill_name", "install_id"],
        owner_only=True,
    )
    async def install_skill_deps(
        skill_name: str,
        install_id: str,
        confirmed: bool = False,
    ) -> str:
        spec = _find_install_spec(skill_name, install_id)
        argv = _argv_for_install_spec(spec)
        label = spec.label or spec.id or "Install dependency"

        if not confirmed:
            return json.dumps(
                {
                    "status": "preview",
                    "skill_name": skill_name,
                    "install_id": install_id,
                    "kind": spec.kind,
                    "label": label,
                    "argv": argv,
                }
            )

        exit_code, stdout, stderr, timed_out = await _run_install_argv(argv)
        if exit_code == 0 and not timed_out:
            from opensquilla.engine.steps.skills_filter import (
                invalidate_skill_eligibility_cache,
            )

            invalidate_skill_eligibility_cache()
        return json.dumps(
            {
                "status": "timeout" if timed_out else "executed",
                "skill_name": skill_name,
                "install_id": install_id,
                "kind": spec.kind,
                "label": label,
                "argv": argv,
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
            }
        )

    # ── Mutation tools (workspace layer only) ──────────────────────────

    @tool(
        name="skill_create",
        description=(
            "Create a new local authored skill in the workspace layer. "
            "Writes a SKILL.md file with frontmatter and body content. "
            "Do not use this for Community or ClawHub installs."
        ),
        params={
            "name": {
                "type": "string",
                "description": "Skill name (lowercase, hyphens allowed, e.g. 'my-helper').",
            },
            "description": {
                "type": "string",
                "description": "One-line description of what the skill does.",
            },
            "content": {
                "type": "string",
                "description": "Skill body content (markdown).",
            },
            "triggers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional trigger phrases for auto-activation.",
            },
        },
        required=["name", "description", "content"],
    )
    async def skill_create(
        name: str,
        description: str,
        content: str,
        triggers: list[str] | None = None,
    ) -> str:
        if _loader is None:
            raise ToolError("Skill loader not available")

        if not _SKILL_NAME_RE.match(name):
            raise ToolError(
                f"Invalid skill name: '{name}'. "
                "Use lowercase letters, digits, and hyphens (e.g. 'my-helper')."
            )

        if not description.strip():
            raise ToolError("Description must not be empty")

        if not content.strip():
            raise ToolError("Content must not be empty")

        # Check for name collision
        _loader.refresh_if_changed(reason="tool.skill_create.validate")
        existing = _loader.get_by_name(name)
        if existing is not None:
            raise ToolError(
                f"Skill '{name}' already exists in layer '{existing.layer.value}'. "
                "Use skill_edit to modify it, or choose a different name."
            )

        # Write to workspace layer
        workspace_dir = _loader.workspace_dir
        if workspace_dir is None:
            raise ToolError("No workspace skill directory configured")

        skill_dir = workspace_dir / name
        skill_file = skill_dir / "SKILL.md"
        with _loader.mutation_guard(reason="tool.skill_create"):
            if skill_file.exists():
                raise ToolError(f"Skill '{name}' already exists at {skill_file}")
            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_md = _render_skill_md(name, description, content, triggers)
            skill_file.write_text(skill_md, encoding="utf-8")

        logger.info("skill_create.success", name=name)
        return f"Skill '{name}' created at {skill_file}"

    @tool(
        name="skill_edit",
        description=(
            "Edit an existing skill's content or description. "
            "Only workspace-layer skills can be edited."
        ),
        params={
            "name": {
                "type": "string",
                "description": "Exact name of the skill to edit.",
            },
            "content": {
                "type": "string",
                "description": "New body content (replaces existing).",
            },
            "description": {
                "type": "string",
                "description": "New description (optional, keeps existing if omitted).",
            },
            "triggers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "New trigger list (optional, keeps existing if omitted).",
            },
        },
        required=["name"],
    )
    async def skill_edit(
        name: str,
        content: str | None = None,
        description: str | None = None,
        triggers: list[str] | None = None,
    ) -> str:
        if _loader is None:
            raise ToolError("Skill loader not available")

        _loader.refresh_if_changed(reason="tool.skill_edit.validate")
        existing = _loader.get_by_name(name)
        if existing is None:
            raise ToolError(f"Skill not found: {name}")

        if existing.layer not in _MUTABLE_LAYERS:
            raise ToolError(
                f"Skill '{name}' is in layer '{existing.layer.value}' and cannot be edited. "
                "Only workspace-layer skills can be modified. "
                "Create a workspace override with skill_create instead."
            )

        if content is None and description is None and triggers is None:
            raise ToolError("Nothing to edit — provide content, description, or triggers")

        # Build updated SKILL.md
        new_description = description if description is not None else existing.description
        new_content = content if content is not None else (existing.content or "")
        new_triggers = triggers if triggers is not None else existing.triggers

        skill_file = Path(existing.file_path)
        if not skill_file.exists():
            raise ToolError(f"Skill file missing: {skill_file}")

        with _loader.mutation_guard(reason="tool.skill_edit"):
            skill_md = _render_skill_md(name, new_description, new_content, new_triggers or None)
            skill_file.write_text(skill_md, encoding="utf-8")

        logger.info("skill_edit.success", name=name)
        return f"Skill '{name}' updated"

    @tool(
        name="skill_delete",
        description=(
            "Delete a skill from the workspace layer. Cannot delete bundled or managed skills."
        ),
        params={
            "name": {
                "type": "string",
                "description": "Exact name of the skill to delete.",
            },
        },
        required=["name"],
    )
    async def skill_delete(name: str) -> str:
        import shutil

        if _loader is None:
            raise ToolError("Skill loader not available")

        _loader.refresh_if_changed(reason="tool.skill_delete.validate")
        existing = _loader.get_by_name(name)
        if existing is None:
            raise ToolError(f"Skill not found: {name}")

        if existing.layer not in _MUTABLE_LAYERS:
            raise ToolError(
                f"Skill '{name}' is in layer '{existing.layer.value}' and cannot be deleted. "
                "Only workspace-layer skills can be removed."
            )

        skill_dir = Path(existing.base_dir)
        if not skill_dir.exists():
            raise ToolError(f"Skill directory missing: {skill_dir}")

        with _loader.mutation_guard(reason="tool.skill_delete"):
            shutil.rmtree(skill_dir)

        logger.info("skill_delete.success", name=name)
        return f"Skill '{name}' deleted from workspace layer"

    logger.info("skill_tools.registered")
