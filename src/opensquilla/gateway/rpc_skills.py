"""Skills domain RPC handlers (Tier 3 stubs)."""

from __future__ import annotations

import asyncio
import shutil
import weakref
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.skills.dependency_summary import build_dependency_summary
from opensquilla.skills.eligibility import (
    EligibilityContext,
    EligibilityReport,
    diagnose_eligibility,
    is_skill_available_live,
)
from opensquilla.skills.hub.defaults import (
    build_default_skill_installer,
    get_default_skill_router,
    installed_skill_names,
)
from opensquilla.skills.hub.deps import install_deps
from opensquilla.skills.loader import SkillLoader

_d = get_dispatcher()

# Per-(name, install_id) install serialization. WeakValueDictionary prevents
# unbounded growth: once all coroutines release a lock it gets GC'd.
_deps_locks: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def _deps_lock_for(name: str, install_id: str) -> asyncio.Lock:
    key = (name, install_id)
    lock = _deps_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _deps_locks[key] = lock
    return lock


def _get_loader(ctx: RpcContext) -> SkillLoader | None:
    return getattr(ctx, "skill_loader", None)


async def _catalog_skills(loader: SkillLoader, *, reason: str) -> tuple[Any, ...]:
    """Probe once at an RPC boundary, then pin all reads to one generation."""
    await asyncio.to_thread(loader.refresh_if_changed, reason=reason)
    return loader.snapshot().skills


class _PinnedSkillLookup:
    """Minimal loader view used while serializing one catalog snapshot."""

    def __init__(self, skill_index: dict[str, Any]) -> None:
        self._skill_index = skill_index

    def get_by_name(self, name: str) -> Any | None:
        return self._skill_index.get(name)


def _reload_failure_payload(
    message: str,
    *,
    loader: SkillLoader | None,
) -> dict[str, Any]:
    snapshot = loader.snapshot() if loader is not None else None
    generation = snapshot.generation if snapshot is not None else 0
    kept_previous = bool(
        snapshot is not None and (snapshot.generation or snapshot.manifest or snapshot.skills)
    )
    return {
        "success": False,
        "changed": False,
        "partial": False,
        "generation": generation,
        "added": [],
        "removed": [],
        "modified": [],
        "errors": [
            {
                "name": "",
                "path": "",
                "message": message,
                "kept_previous": kept_previous,
            }
        ],
    }


class _NoCatalogMutationError(Exception):
    """Internal signal used to leave a mutation guard without dirtying it."""

    def __init__(self, result: Any) -> None:
        super().__init__()
        self.result = result


async def _run_catalog_mutation(
    loader: SkillLoader | None,
    *,
    reason: str,
    operation: Callable[[], Awaitable[Any]],
    did_change: Callable[[Any], bool],
) -> Any:
    """Keep readers on the old snapshot until a known mutation succeeds."""
    if loader is None:
        return await operation()
    try:
        with loader.mutation_guard(reason=reason):
            result = await operation()
            if not did_change(result):
                raise _NoCatalogMutationError(result)
            return result
    except _NoCatalogMutationError as exc:
        return exc.result


def _loader_managed_dir(ctx: RpcContext) -> Path | None:
    loader = _get_loader(ctx)
    return getattr(loader, "managed_dir", None) if loader is not None else None


def _status_from_report(report: EligibilityReport) -> str:
    """Map an EligibilityReport to a tri-state status string.

    Wire contract: one of ``"ready" | "needs_setup" | "not_declared"``.
    """
    if not report.eligible:
        return "needs_setup"
    if report.declared:
        return "ready"
    return "not_declared"


def _format_env_any_group(group: list[str]) -> str:
    return " or ".join(group)


def _status_detail(spec: Any, report: EligibilityReport) -> str:
    """Human-readable tooltip detail for the skill status dot/chip."""
    if not report.eligible:
        if report.disabled:
            return "Needs setup — disabled"
        if report.wrong_os:
            meta = getattr(spec, "metadata", None)
            os_list = list(meta.os) if meta and meta.os else []
            return f"Needs setup — wrong OS (requires: {', '.join(os_list)})"
        missing = (
            list(report.missing_bins)
            + list(report.missing_env)
            + [_format_env_any_group(group) for group in report.missing_env_any]
        )
        if missing:
            return f"Needs setup — missing: {', '.join(missing)}"
        return "Needs setup"
    if not report.declared:
        return "Ready — no dependencies declared"
    meta = getattr(spec, "metadata", None)
    requires = meta.requires if meta is not None else None
    if requires is None:
        total = 0
    else:
        total = (
            len(requires.bins)
            + (1 if requires.any_bins else 0)
            + len(requires.env)
            + (1 if requires.env_any else 0)
        )
    return f"Ready — {total}/{total} dependencies satisfied"


def _requirements_item(
    name: str,
    source: str,
    spec: Any | None,
    report: EligibilityReport | None,
) -> dict[str, Any]:
    """Build a compact dependency-readiness row for the Skill dialog."""
    if spec is None or report is None:
        return {
            "name": name,
            "source": source,
            "status": "missing_skill",
            "requires_bins": [],
            "requires_any_bins": [],
            "requires_env": [],
            "missing_bins": [],
            "missing_env": [],
        }

    meta = getattr(spec, "metadata", None)
    requires = meta.requires if meta is not None else None
    return {
        "name": name,
        "source": source,
        "status": _status_from_report(report),
        "requires_bins": list(requires.bins) if requires else [],
        "requires_any_bins": list(requires.any_bins) if requires else [],
        "requires_env": list(requires.env) if requires else [],
        "missing_bins": list(report.missing_bins),
        "missing_env": list(report.missing_env),
    }


def _requirements_summary(items: list[dict[str, Any]]) -> str:
    if not items:
        return "not_declared"
    statuses = {str(item.get("status", "")) for item in items}
    if "needs_setup" in statuses or "missing_skill" in statuses:
        return "needs_setup"
    if "ready" in statuses:
        return "ready"
    return "not_declared"


def _requirements_payload(
    spec: Any,
    report: EligibilityReport,
    sub_skills: list[str],
    *,
    skill_index: dict[str, Any] | None = None,
    eligibility_ctx: EligibilityContext | None = None,
) -> dict[str, Any]:
    """Return current-skill requirements plus one-hop meta sub-skill rollup."""
    items: list[dict[str, Any]] = []
    if report.declared:
        items.append(_requirements_item(spec.name, "self", spec, report))

    kind = getattr(spec, "kind", "skill") or "skill"
    if kind in {"meta", "meta_sop"} and skill_index is not None and eligibility_ctx is not None:
        for sub_name in sub_skills:
            sub_spec = skill_index.get(sub_name)
            sub_report = (
                diagnose_eligibility(sub_spec, eligibility_ctx) if sub_spec is not None else None
            )
            items.append(_requirements_item(sub_name, "sub_skill", sub_spec, sub_report))

    return {"summary": _requirements_summary(items), "items": items}


def _skill_to_dict(
    spec: Any,
    report: EligibilityReport,
    os_name: str = "",
    *,
    skill_index: dict[str, Any] | None = None,
    loader: SkillLoader | None = None,
    eligibility_ctx: EligibilityContext | None = None,
) -> dict[str, Any]:
    """Convert a SkillSpec to a dict with eligibility diagnostics.

    Install options are filtered against ``os_name`` before serialization.
    An install entry is kept when its ``os`` list is empty (treated as
    "any OS") or contains the current ``os_name``. This applies the two-layer
    OS filter (skill-level ``metadata.os`` + per-install ``os``), and keeps the
    wire payload narrow (no ``os`` field per entry).
    Passing an empty ``os_name`` disables per-entry filtering (backward compat).
    """
    meta = getattr(spec, "metadata", None)
    install_entries: list[dict[str, Any]] = []
    if meta is not None:
        for ispec in meta.install:
            spec_os = list(getattr(ispec, "os", []) or [])
            if spec_os and os_name and os_name not in spec_os:
                continue
            install_entries.append(
                {
                    "id": ispec.id,
                    "kind": ispec.kind,
                    "label": ispec.label,
                    "bins": list(ispec.bins),
                }
            )

    # Meta-skill metadata: expose kind + the list of sub-skills referenced
    # by the composition DAG so the WebUI can group meta-skills separately
    # and surface "uses: X, Y, Z" badges without a second round-trip.
    kind = getattr(spec, "kind", "skill") or "skill"
    sub_skills: list[str] = []
    composition_raw = getattr(spec, "composition_raw", None)
    if isinstance(composition_raw, dict):
        steps_raw = composition_raw.get("steps")
        if isinstance(steps_raw, list):
            seen: set[str] = set()
            for step in steps_raw:
                if not isinstance(step, dict):
                    continue
                sub = step.get("skill")
                if isinstance(sub, str) and sub and sub not in seen:
                    seen.add(sub)
                    sub_skills.append(sub)
                # routes (kind=llm_classify) may also reference sub-skills
                routes = step.get("routes")
                if isinstance(routes, list):
                    for route in routes:
                        if isinstance(route, dict):
                            rsub = route.get("skill")
                            if isinstance(rsub, str) and rsub and rsub not in seen:
                                seen.add(rsub)
                                sub_skills.append(rsub)

    # Coding-mode-gated sub-skills (code-task when OFF) are not surfaced in a
    # meta-skill's composition rollup either (codex review — every skill API).
    sub_skills = [name for name in sub_skills if is_skill_available_live(name)]

    d: dict[str, Any] = {
        "name": spec.name,
        "description": spec.description,
        "layer": str(spec.layer),
        "always": spec.always,
        "triggers": spec.triggers,
        "eligible": report.eligible,
        "emoji": meta.emoji if meta else "",
        "primary_env": meta.primary_env if meta else "",
        "homepage": meta.homepage if meta else getattr(spec, "homepage", ""),
        "file_path": getattr(spec, "file_path", ""),
        "os": list(meta.os) if meta else [],
        "disabled": report.disabled,
        "install": install_entries,
        "kind": kind,
        "sub_skills": sub_skills,
        "requirements": _requirements_payload(
            spec,
            report,
            sub_skills,
            skill_index=skill_index,
            eligibility_ctx=eligibility_ctx,
        ),
    }
    provenance = getattr(spec, "provenance", None)
    d["provenance"] = {
        "origin": provenance.origin if provenance else "unknown",
        "license": provenance.license if provenance else "unknown",
        "upstream_url": provenance.upstream_url if provenance else "",
        "maintained_by": provenance.maintained_by if provenance else "OpenSquilla",
    }
    d["declared"] = report.declared
    d["status"] = _status_from_report(report)
    d["status_detail"] = _status_detail(spec, report)
    dependency_loader = loader
    if skill_index is not None:
        # Dependency rollups recursively look up sub-skills. Pin those lookups
        # too, otherwise a concurrent reload could mix catalog generations in
        # one RPC response.
        dependency_loader = cast(SkillLoader, _PinnedSkillLookup(skill_index))
    d["dependency_summary"] = build_dependency_summary(
        spec,
        loader=dependency_loader,
        ctx=eligibility_ctx,
        report=report,
    )
    if not report.eligible:
        d["reasons"] = report.reasons
        d["missing_bins"] = report.missing_bins
        d["missing_env"] = report.missing_env
        d["missing_env_any"] = report.missing_env_any
    return d


@_d.method("skills.status", scope="operator.read")
async def _handle_skills_status(params: dict | None, ctx: RpcContext) -> list[dict[str, Any]]:
    """Return all skills with their eligibility status."""
    loader = _get_loader(ctx)
    if loader is None:
        return []

    ctx_eligible = EligibilityContext.auto()
    # Operator gate: skills governed by the coding-mode toggle (code-task) are
    # hidden from the skill manager when the toggle is OFF — unreachable through
    # every skill API, not just the agent prompt (codex review).
    skills = [
        s
        for s in await _catalog_skills(loader, reason="rpc.skills.status")
        if is_skill_available_live(s.name)
    ]
    skill_index = {skill.name: skill for skill in skills}
    return [
        _skill_to_dict(
            skill,
            diagnose_eligibility(skill, ctx_eligible),
            ctx_eligible.os_name,
            skill_index=skill_index,
            loader=loader,
            eligibility_ctx=ctx_eligible,
        )
        for skill in skills
    ]


@_d.method("skills.list", scope="operator.read")
async def _handle_skills_list(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """List installed skills."""
    loader = _get_loader(ctx)
    if loader is None:
        return {"skills": []}

    ctx_eligible = EligibilityContext.auto()
    all_skills = await _catalog_skills(loader, reason="rpc.skills.list")
    skill_index = {skill.name: skill for skill in all_skills}
    # Operator gate: coding-mode-gated skills (code-task when OFF) stay out.
    skills = [
        skill
        for skill in all_skills
        if skill.user_invocable and is_skill_available_live(skill.name)
    ]
    return {
        "skills": [
            _skill_to_dict(
                skill,
                diagnose_eligibility(skill, ctx_eligible),
                ctx_eligible.os_name,
                skill_index=skill_index,
                loader=loader,
                eligibility_ctx=ctx_eligible,
            )
            for skill in skills
        ]
    }


@_d.method("skills.bins", scope="node")
async def _handle_skills_bins(params: dict | None, ctx: RpcContext) -> dict[str, bool]:
    """Return the availability status of required bins across all skills."""
    loader = _get_loader(ctx)
    if loader is None:
        return {}

    bins_status: dict[str, bool] = {}
    skills = await _catalog_skills(loader, reason="rpc.skills.bins")

    for skill in skills:
        if skill.metadata and skill.metadata.requires:
            for bin_name in skill.metadata.requires.bins:
                if bin_name not in bins_status:
                    bins_status[bin_name] = shutil.which(bin_name) is not None
            for bin_name in skill.metadata.requires.any_bins:
                if bin_name not in bins_status:
                    bins_status[bin_name] = shutil.which(bin_name) is not None

    return bins_status


@_d.method("skills.get", scope="operator.read")
async def _handle_skills_get(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Get a single skill by name, including its full content."""
    if not isinstance(params, dict) or "name" not in params:
        raise ValueError("params.name is required")

    loader = _get_loader(ctx)
    if loader is None:
        raise KeyError("No skill loader available")

    skills = await _catalog_skills(loader, reason="rpc.skills.get")
    skill_index = {item.name: item for item in skills}
    skill = skill_index.get(params["name"])
    if skill is None or not is_skill_available_live(params["name"]):
        # Gated coding-mode skills are reported as not-found so their content is
        # never returned while the toggle is OFF (codex review).
        raise KeyError(f"Skill not found: {params['name']}")

    ctx_eligible = EligibilityContext.auto()
    result = _skill_to_dict(
        skill,
        diagnose_eligibility(skill, ctx_eligible),
        ctx_eligible.os_name,
        skill_index=skill_index,
        loader=loader,
        eligibility_ctx=ctx_eligible,
    )
    result["content"] = skill.content
    result["file_path"] = skill.file_path
    result["base_dir"] = skill.base_dir
    return result


def _installed_names() -> set[str]:
    """Return the set of skill names currently recorded in the lockfile.

    Lockfile is the authoritative "installed via Community source" record —
    bundled or workspace skills with colliding names won't be mis-flagged
    as installed-from-ClawHub. Missing/corrupt lockfile returns an empty
    set (treat everything as not-yet-installed).
    """
    return installed_skill_names()


@_d.method("skills.search", scope="operator.read")
async def _handle_skills_search(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Search for skills across Community sources."""
    if not isinstance(params, dict) or "query" not in params:
        raise ValueError("params.query is required")

    router = getattr(ctx, "_skill_router", None)
    if router is None:
        router = _get_default_router()
    if router is None:
        return {"results": [], "message": "No skill sources configured"}

    query = params["query"]
    try:
        limit = min(int(params.get("limit", 20)), 100)
    except (TypeError, ValueError):
        limit = 20
    source_id = params.get("source")
    if source_id is not None and not isinstance(source_id, str):
        source_id = None
    results = await router.search(query, limit=limit, source_id=source_id)
    installed = _installed_names()
    # Lockfile keys are the installer's name — which for ClawHub is the
    # slug (``identifier``), not the human-readable ``displayName`` a
    # source may return as ``SkillMeta.name``. Check both so we catch
    # either convention; a future source that matches on name directly
    # still works.
    return {
        "results": [
            {
                "name": r.name,
                "description": r.description,
                "version": r.version,
                "author": r.author,
                "source": r.source_id,
                "trust_level": r.trust_level,
                "identifier": r.identifier,
                "installed": r.identifier in installed or r.name in installed,
            }
            for r in results
        ]
    }


@_d.method("skills.reload", scope="operator.admin")
async def _handle_skills_reload(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Force a rescan of the running Gateway's Skill catalog."""
    loader = _get_loader(ctx)
    if loader is None:
        return _reload_failure_payload("No skill loader configured", loader=None)

    from opensquilla.engine.steps.skills_filter import (
        invalidate_skill_eligibility_cache,
    )

    try:
        result = await asyncio.to_thread(
            loader.reload,
            force=True,
            reason="rpc.skills.reload",
        )
    except Exception as exc:  # Keep the RPC response shape stable on unexpected failures.
        return _reload_failure_payload(str(exc) or type(exc).__name__, loader=loader)
    finally:
        # A force reload is also the operator's escape hatch after changing
        # binaries/environment outside the catalog writer paths.
        invalidate_skill_eligibility_cache()
    return result.to_dict()


@_d.method("skills.install", scope="operator.admin")
async def _handle_skills_install(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Install a skill from a Community source."""
    if not isinstance(params, dict) or "identifier" not in params:
        raise ValueError("params.identifier is required")
    loader = _get_loader(ctx)
    if loader is None:
        return {"success": False, "message": "No skill loader configured"}

    installer = _get_default_installer(managed_dir=loader.managed_dir)
    if installer is None:
        return {"success": False, "message": "No skill installer configured"}

    identifier = params["identifier"]
    source_id = params.get("source", "clawhub")
    force = params.get("force", False)
    result = await _run_catalog_mutation(
        loader,
        reason="rpc.skills.install",
        operation=lambda: installer.install(identifier, source_id, force=force),
        did_change=lambda value: bool(value.success),
    )
    resp: dict[str, Any] = {
        "success": result.success,
        "name": result.name,
        "message": result.message,
    }
    if result.path:
        resp["path"] = result.path
    if result.scan:
        resp["scan_verdict"] = result.scan.verdict
        resp["scan_findings"] = [finding.__dict__ for finding in result.scan.findings]
    return resp


@_d.method("skills.update", scope="operator.admin")
async def _handle_skills_update(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Update installed skills from lockfile."""
    loader = _get_loader(ctx)
    if loader is None:
        return {
            "results": [],
            "success": False,
            "message": "No skill loader configured",
        }
    installer = _get_default_installer(managed_dir=loader.managed_dir)
    if installer is None:
        return {"success": False, "message": "No skill installer configured"}

    name = (params or {}).get("name")
    try:
        results = await _run_catalog_mutation(
            loader,
            reason="rpc.skills.update",
            operation=lambda: installer.update(name),
            did_change=lambda values: any(value.success for value in values),
        )
    except OSError as exc:
        return {
            "results": [],
            "success": False,
            "message": f"Skill update unavailable: {exc}",
        }
    return {
        "results": [{"success": r.success, "name": r.name, "message": r.message} for r in results]
    }


@_d.method("skills.uninstall", scope="operator.admin")
async def _handle_skills_uninstall(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Uninstall a managed skill."""
    if not isinstance(params, dict) or "name" not in params:
        raise ValueError("params.name is required")

    installer = _get_default_installer(managed_dir=_loader_managed_dir(ctx))
    if installer is None:
        return {"success": False, "message": "No skill installer configured"}

    loader = _get_loader(ctx)
    result = await _run_catalog_mutation(
        loader,
        reason="rpc.skills.uninstall",
        operation=lambda: installer.uninstall(params["name"]),
        did_change=lambda value: bool(value.success),
    )
    return {"success": result.success, "name": result.name, "message": result.message}


@_d.method("skills.deps.install", scope="operator.admin")
async def _handle_skills_deps_install(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Install runtime dependencies for an already-loaded skill.

    Looks up the skill by name, finds the matching SkillInstallSpec by id in
    `metadata.install`, runs it via `install_deps`, then re-runs
    `diagnose_eligibility` and returns `missing_still` reflecting post-install state.

    Note: `kind == "download"` is non-idempotent — re-running re-downloads.
    Callers should consult `missing_still` before retrying.
    """
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")
    if "name" not in params:
        raise ValueError("params.name is required")
    if "install_id" not in params:
        raise ValueError("params.install_id is required")

    name = params["name"]
    install_id = params["install_id"]
    loader = _get_loader(ctx)
    if loader is None:
        raise KeyError("No skill loader available")
    skill = loader.get_by_name(name)
    if skill is None or not is_skill_available_live(name):
        # Coding-mode-gated skills are reported as not-found so they cannot be
        # resolved or have deps installed while the toggle is OFF (codex review).
        raise KeyError(f"Skill not found: {name}")

    specs = skill.metadata.install if skill.metadata else []
    spec = next((s for s in specs if s.id == install_id), None)
    if spec is None:
        raise KeyError(f"Install spec not found: {install_id}")

    ctx_eligible = EligibilityContext.auto()
    if spec.os and ctx_eligible.os_name and ctx_eligible.os_name not in spec.os:
        raise ValueError(
            f"Install spec {install_id!r} not supported on "
            f"{ctx_eligible.os_name} (requires: {', '.join(spec.os)})"
        )

    async with _deps_lock_for(name, install_id):
        results = await install_deps([spec])
        r = results[0]
        if r.success:
            from opensquilla.engine.steps.skills_filter import (
                invalidate_skill_eligibility_cache,
            )

            invalidate_skill_eligibility_cache()
        report = diagnose_eligibility(skill, ctx_eligible)

    return {
        "success": r.success,
        "kind": r.kind,
        "message": r.message,
        "missing_still": {
            "bins": list(report.missing_bins),
            "env": list(report.missing_env),
            "env_any": [list(group) for group in report.missing_env_any],
        },
    }


# ---------------------------------------------------------------------------
# Default router/installer (lazy init)
# ---------------------------------------------------------------------------

def _get_default_router():
    return get_default_skill_router()


def _get_default_installer(*, managed_dir=None):
    return build_default_skill_installer(managed_dir=managed_dir)
