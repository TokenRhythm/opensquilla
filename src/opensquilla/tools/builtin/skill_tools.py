"""Skill tools — agent-accessible skill discovery, viewing, and management.

Registered at boot time when a SkillLoader is available.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog

from opensquilla.process_tree import (
    capture_process_tree_owner,
    create_owned_subprocess_exec,
)
from opensquilla.skills.hub.defaults import (
    build_default_skill_installer,
    get_default_skill_router,
    installed_skill_lockfile,
)
from opensquilla.skills.hub.identity import is_skill_meta_installed
from opensquilla.skills.hub.installer import (
    supported_keyword_arguments,
    supports_keyword_argument,
    unsupported_installer_result,
)
from opensquilla.skills.hub.management import SkillManagementService
from opensquilla.skills.hub.router import search_router_with_diagnostics
from opensquilla.skills.types import SkillInstallSpec, SkillLayer, SkillSpec
from opensquilla.tools.registry import tool
from opensquilla.tools.types import PlanAccess, ToolError, current_tool_context

if TYPE_CHECKING:
    from opensquilla.skills.loader import SkillLoader

logger = structlog.get_logger(__name__)

# Module-level reference set at boot
_loader: SkillLoader | None = None
# Layers that user may mutate — workspace only
_MUTABLE_LAYERS = frozenset({SkillLayer.WORKSPACE})


def _reject_guest_skill_tool(tool_name: str) -> None:
    ctx = current_tool_context.get()
    if ctx is not None and ctx.guest_safe:
        raise ToolError(
            f"GUEST_TOOL_UNAVAILABLE: {tool_name} is unavailable to anonymous guests"
        )


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


def _expanded_skill_body(skill: Any) -> str:
    """Expand location placeholders only in an invoked SKILL.md body.

    The catalog prompt intentionally carries no host location. Supporting
    resources are returned byte-for-byte by ``SkillResources``; only the body
    selected through ``skill_view`` receives the runtime base directory.
    """

    body = str(getattr(skill, "content", "") or "")
    base_dir = str(getattr(skill, "base_dir", "") or "")
    if not body or not base_dir:
        return body
    return body.replace("{baseDir}", base_dir).replace("{base_dir}", base_dir)


async def _pinned_resource_tree_matches(skill: Any) -> bool:
    """Verify that a pinned spec still owns the bytes on disk.

    Unpinned standalone calls refresh through the live loader. A turn-pinned
    call must never combine instructions from one generation with supporting
    files from another generation, including during a concurrent directory
    swap.
    """

    if _active_catalog() is None:
        return True
    expected = str(getattr(skill, "tree_digest", "") or "")
    base_dir = str(getattr(skill, "base_dir", "") or "")
    if not expected or not base_dir:
        return False
    from opensquilla.skills.tree import compute_tree_sha256

    try:
        return await asyncio.to_thread(compute_tree_sha256, Path(base_dir)) == expected
    except (OSError, ValueError):
        return False


def _resource_generation_mismatch(name: str) -> str:
    return (
        f"Skill resources changed after the current catalog was pinned: {name}. "
        "Retry skill_view in the next turn."
    )


def _managed_resource_manifest(
    skill: Any,
    *,
    lockfile_path: Path | None = None,
) -> tuple[str, ...] | None:
    """Return lock-recorded v2 files for one exact managed candidate."""

    if getattr(skill, "layer", None) is not SkillLayer.MANAGED:
        return None
    from opensquilla.paths import default_opensquilla_home
    from opensquilla.skills.hub.lockfile import Lockfile

    selected_lockfile = lockfile_path or default_opensquilla_home() / "skills-lock.json"
    lockfile = Lockfile.load(selected_lockfile)
    if lockfile.mutation_blocked:
        return ()
    target = Path(str(getattr(skill, "base_dir", "") or ""))
    try:
        target_key = target.resolve(strict=False)
    except (OSError, ValueError):
        return ()
    matches = []
    for storage_key, entry in lockfile.installed.items():
        relative = entry.relative_path or entry.directory_name or storage_key
        recorded = target.parent / relative
        if not entry.relative_path and not entry.directory_name and entry.path:
            recorded = Path(entry.path)
            if not recorded.is_absolute():
                recorded = target.parent / recorded
        try:
            if recorded.resolve(strict=False) == target_key:
                matches.append(entry)
        except (OSError, ValueError):
            continue
    if len(matches) != 1:
        return () if matches else None
    entry = matches[0]
    if not entry.parser_version:
        return None
    raw_files = entry.extra.get("files", [])
    if not isinstance(raw_files, list):
        return ()
    return tuple(item for item in raw_files if isinstance(item, str))


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
    ``skills.manifest.parse_skill_frontmatter``. Hand-concatenating unquoted scalars
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


def _community_result_to_dict(row: Any, installed: Any) -> dict[str, Any]:
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
        "installReference": getattr(row, "canonical_identifier", "") or identifier,
        "installed": is_skill_meta_installed(row, installed),
    }


async def _run_install_argv(argv: list[str]) -> tuple[int, str, str, bool]:
    try:
        proc = await create_owned_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"Install command not found: {argv[0]}") from exc
    process_tree = capture_process_tree_owner(proc, isolated=True)
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=_INSTALL_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        from opensquilla.tools.builtin.shell import _terminate_exec_process_tree

        await asyncio.shield(_terminate_exec_process_tree(proc, process_tree))
        raise
    except TimeoutError:
        from opensquilla.tools.builtin.shell import _terminate_exec_process_tree

        await _terminate_exec_process_tree(proc, process_tree)
        return -1, "", "Timed out", True
    from opensquilla.tools.builtin.shell import _terminate_exec_process_tree

    await _terminate_exec_process_tree(proc, process_tree)
    return proc.returncode or 0, _cap_output(stdout), _cap_output(stderr), False


def create_skill_tools(
    loader: SkillLoader,
    skills_cfg_getter: Callable[[], object] | None = None,
    management_service: SkillManagementService | None = None,
) -> None:
    """Register skill tools (list, view, create, edit, delete) with the global registry.

    ``skills_cfg_getter`` returns the live skills config so operator gating
    (coding mode / disabled) is honored at call time, not boot time. The
    Gateway composition root supplies ``management_service`` so agent installs
    share its configured journal and managed-root transaction lock.
    """
    from opensquilla.skills.eligibility import set_live_skills_config_getter

    global _loader
    _loader = loader
    set_live_skills_config_getter(skills_cfg_getter)
    injected_lockfile_path = (
        getattr(management_service, "lockfile_path", None)
        if management_service is not None
        else None
    )
    resource_lockfile_path = (
        Path(injected_lockfile_path) if injected_lockfile_path else None
    )
    injected_skill_router = (
        getattr(management_service, "router", None)
        if management_service is not None
        else None
    )

    @tool(
        name="skill_list",
        description="List all available skills with name, description, and eligibility.",
        plan_access=PlanAccess.READ_ONLY,
    )
    async def skill_list() -> str:
        _reject_guest_skill_tool("skill_list")
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
        plan_access=PlanAccess.READ_ONLY,
    )
    async def skill_view(name: str, file_path: str | None = None) -> str:
        _reject_guest_skill_tool("skill_view")
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
                return _expanded_skill_body(skill) or (
                    f"(Skill '{name}' has no body content)"
                )

            from pathlib import Path

            from opensquilla.skills.resources import SkillResources

            if not await _pinned_resource_tree_matches(skill):
                return _resource_generation_mismatch(name)

            resources = SkillResources(
                Path(skill.base_dir),
                managed_manifest_files=_managed_resource_manifest(
                    skill,
                    lockfile_path=resource_lockfile_path,
                ),
            )
            content = await asyncio.to_thread(resources.read_resource, normalized_path)
            if content is None:
                return f"File not found in skill '{name}': {file_path}"
            # Close the check/read race: a concurrent publish after the first
            # digest must not leak its bytes into this pinned turn.
            if not await _pinned_resource_tree_matches(skill):
                return _resource_generation_mismatch(name)
            return content

        return _expanded_skill_body(skill) or f"(Skill '{name}' has no body content)"

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
        plan_access=PlanAccess.READ_ONLY,
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
        router = injected_skill_router or get_default_skill_router()
        report = await search_router_with_diagnostics(
            router,
            clean_query,
            limit=result_limit,
            source_id=source_id,
        )
        if resource_lockfile_path is not None:
            from opensquilla.skills.hub.lockfile import Lockfile

            installed = Lockfile.load(resource_lockfile_path)
        else:
            installed = installed_skill_lockfile()
        payload: dict[str, Any] = {
            "status": "ok",
            "query": clean_query,
            "source": source_id or "all",
            "results": [
                _community_result_to_dict(row, installed) for row in report.results
            ],
        }
        if report.diagnostics:
            payload["diagnostics"] = [item.to_dict() for item in report.diagnostics]
            payload["partial"] = report.partial
            payload["allSourcesUnavailable"] = report.all_sources_unavailable
            payload["status"] = "partial" if report.partial else "unavailable"
        return json.dumps(payload)

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
                    "Acknowledge a dangerous security scan only after the user explicitly asks."
                ),
                "default": False,
            },
            "risk_confirmation": {
                "type": "string",
                "description": (
                    "Exact confirmationToken returned by the prior "
                    "SCAN_CONFIRMATION_REQUIRED diagnostic. It is valid only for "
                    "that resolved artifact."
                ),
                "default": "",
            },
            "replace_source": {
                "type": "boolean",
                "description": (
                    "Replace a same-name install from another source only after "
                    "the user explicitly approves."
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
        risk_confirmation: str = "",
        replace_source: bool = False,
    ) -> str:
        if _loader is None:
            raise ToolError("Skill loader not available")
        if type(force) is not bool or type(replace_source) is not bool:
            raise ToolError("force and replace_source must be booleans")
        clean_identifier = str(identifier or "").strip()
        if not clean_identifier:
            raise ToolError("identifier must not be empty")
        if not isinstance(risk_confirmation, str):
            raise ToolError("risk_confirmation must be a string")
        clean_risk_confirmation = risk_confirmation.strip()
        if clean_risk_confirmation and not force:
            raise ToolError("risk_confirmation requires force=true")
        source_id = str(source or "clawhub").strip() or "clawhub"

        installer: Any = management_service
        if installer is None:
            builder_kwargs = {
                "managed_dir": _loader.managed_dir,
                **supported_keyword_arguments(
                    build_default_skill_installer,
                    {"loader": _loader, "offline": False},
                ),
            }
            installer = build_default_skill_installer(**builder_kwargs)
        if isinstance(installer, SkillManagementService):
            result = await installer.install(
                clean_identifier,
                source_id,
                force=force,
                replace_source=replace_source,
                risk_confirmation=clean_risk_confirmation,
            )
        else:
            install = installer.install
            if replace_source and not supports_keyword_argument(install, "replace_source"):
                result = unsupported_installer_result(
                    operation="install",
                    capability="replaceSource",
                    name=clean_identifier,
                )
            elif force and not supports_keyword_argument(install, "force"):
                result = unsupported_installer_result(
                    operation="install",
                    capability="force",
                    name=clean_identifier,
                )
            elif force and (
                not clean_risk_confirmation
                or not supports_keyword_argument(install, "risk_confirmation")
            ):
                result = unsupported_installer_result(
                    operation="install",
                    capability="riskConfirmation",
                    name=clean_identifier,
                )
            else:
                install_kwargs = supported_keyword_arguments(
                    install,
                    {
                        "force": force,
                        "replace_source": replace_source,
                        "risk_confirmation": clean_risk_confirmation,
                    },
                )
                try:
                    with _loader.mutation_guard(reason="tool.skill_install_community"):
                        result = await install(
                            clean_identifier,
                            source_id,
                            **install_kwargs,
                        )
                        if not result.success:
                            raise _NoCatalogMutationError(result)
                except _NoCatalogMutationError as exc:
                    result = exc.result

        serializer = getattr(result, "to_dict", None)
        payload: dict[str, Any] = dict(serializer()) if callable(serializer) else {}
        payload.update({
            "status": "installed" if result.success else "failed",
            "success": result.success,
            "name": result.name,
            "identifier": clean_identifier,
            "source": source_id,
            "message": result.message,
        })
        if result.path:
            payload["path"] = result.path
        if result.scan:
            payload["scan_verdict"] = result.scan.verdict
            payload["scan_findings"] = [finding.__dict__ for finding in result.scan.findings]
        if result.success:
            visibility = (
                "It can be used from the next turn."
                if bool(getattr(result, "instruction_usable", False))
                else (
                    "The committed catalog state becomes observable from the next turn; "
                    "the lifecycle result does not declare the Skill usable."
                )
            )
            payload["message"] = (
                f"{result.message} The current turn keeps its pinned Skill catalog; "
                f"{visibility}"
            )
            payload["effectiveFrom"] = "next_turn"
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
        _reject_guest_skill_tool("skill_create")
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
        _reject_guest_skill_tool("skill_edit")
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

        _reject_guest_skill_tool("skill_delete")
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
