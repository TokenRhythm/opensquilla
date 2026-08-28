"""Skills domain RPC handlers (Tier 3 stubs)."""

from __future__ import annotations

import asyncio
import os
import shutil
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.paths import default_opensquilla_home
from opensquilla.skills.capability_runtime import trusted_capability_consumers_for_meta_plan
from opensquilla.skills.dependency_summary import build_dependency_summary
from opensquilla.skills.eligibility import (
    EligibilityContext,
    EligibilityReport,
    diagnose_eligibility,
    is_skill_available_live,
    live_eligibility_context,
)
from opensquilla.skills.hub.contracts import (
    SkillCompatibilityState,
    SkillInstallState,
    SkillInvocationCapabilities,
    SkillLifecycle,
    SkillLoadState,
    SkillReadinessState,
    SkillSelectionState,
)
from opensquilla.skills.hub.defaults import (
    build_default_skill_installer,
    get_default_skill_router,
    installed_skill_lockfile,
)
from opensquilla.skills.hub.deps import install_deps
from opensquilla.skills.hub.doctor import SkillDoctor
from opensquilla.skills.hub.identity import is_skill_meta_installed
from opensquilla.skills.hub.installer import (
    supported_keyword_arguments,
    supports_keyword_argument,
    unsupported_installer_result,
)
from opensquilla.skills.hub.management import (
    SkillManagementService,
    committed_store_read_guard,
    lifecycle_for_candidate,
)
from opensquilla.skills.hub.router import search_router_with_diagnostics
from opensquilla.skills.hub.transaction import journal_path_for_state
from opensquilla.skills.loader import PinnedSkillLoader, SkillLoader
from opensquilla.skills.meta.parser import MetaPlanError, parse_meta_plan

_d = get_dispatcher()

# Per-(name, install_id) install serialization. WeakValueDictionary prevents
# unbounded growth: once all coroutines release a lock it gets GC'd.
_deps_locks: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)

_ACTIVE_SKILL_INSTALLS_STATE_KEY = "_active_skill_installs"
_PENDING_SKILL_INSTALL_CANCELLATIONS_STATE_KEY = (
    "_pending_skill_install_cancellations"
)
_MAX_PENDING_SKILL_INSTALL_CANCELLATIONS_PER_CONNECTION = 8
_MAX_PENDING_SKILL_INSTALL_CANCELLATIONS = 256


def _deps_lock_for(name: str, install_id: str) -> asyncio.Lock:
    key = (name, install_id)
    lock = _deps_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _deps_locks[key] = lock
    return lock


def _skill_install_operation_id(params: dict[str, Any]) -> str:
    raw = _exact_identity_param(params, "operationId", "operation_id")
    if not raw:
        return ""
    try:
        return str(UUID(raw))
    except ValueError as exc:
        raise ValueError("params.operationId must be a UUID") from exc


def _skill_install_operation_key(ctx: RpcContext, operation_id: str) -> tuple[str, str]:
    return (ctx.conn_id, operation_id)


def _active_skill_installs(ctx: RpcContext) -> dict[tuple[str, str], asyncio.Task[Any]]:
    state = ctx.skill_management_state
    active = state.setdefault(_ACTIVE_SKILL_INSTALLS_STATE_KEY, {})
    if not isinstance(active, dict):
        raise RuntimeError("Invalid active skill-install state")
    return cast(dict[tuple[str, str], asyncio.Task[Any]], active)


def _pending_skill_install_cancellations(ctx: RpcContext) -> set[tuple[str, str]]:
    state = ctx.skill_management_state
    pending = state.setdefault(_PENDING_SKILL_INSTALL_CANCELLATIONS_STATE_KEY, set())
    if not isinstance(pending, set):
        raise RuntimeError("Invalid pending skill-install cancellation state")
    return cast(set[tuple[str, str]], pending)


def _skill_install_cancelled_payload() -> dict[str, Any]:
    return {
        "success": False,
        "cancelled": True,
        "message": "Skill installation cancelled",
    }


def _get_loader(ctx: RpcContext) -> SkillLoader | None:
    return getattr(ctx, "skill_loader", None)


def _eligibility_context(ctx: RpcContext) -> EligibilityContext:
    config = getattr(ctx, "config", None)
    return live_eligibility_context(getattr(config, "skills", None))


async def _catalog_snapshot(loader: SkillLoader, *, reason: str) -> Any:
    """Probe once at an RPC boundary and return one pinned generation."""

    await asyncio.to_thread(loader.refresh_if_changed, reason=reason)
    return loader.snapshot()


async def _catalog_skills(loader: SkillLoader, *, reason: str) -> tuple[Any, ...]:
    """Probe once at an RPC boundary, then pin all reads to one generation."""

    return tuple((await _catalog_snapshot(loader, reason=reason)).skills)


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
    if loader is not None:
        return getattr(loader, "managed_dir", None)
    state = getattr(ctx, "skill_management_state", None)
    raw = state.get("managed_dir") if isinstance(state, dict) else None
    return Path(raw) if raw else None


def _journal_path(ctx: RpcContext, managed_dir: Path) -> Path:
    configured = str(getattr(ctx.config, "state_dir", "") or "").strip()
    state_root = Path(configured) if configured else None
    return journal_path_for_state(managed_dir, state_root)


def _management_service(ctx: RpcContext) -> Any | None:
    injected = getattr(ctx, "skill_management_service", None)
    if injected is not None:
        return injected
    loader = _get_loader(ctx)
    managed_dir = _loader_managed_dir(ctx)
    if loader is None or managed_dir is None:
        return None
    return _get_default_installer(
        managed_dir=managed_dir,
        loader=loader,
        journal_path=_journal_path(ctx, managed_dir),
        offline=False,
    )


def _management_lockfile_path(ctx: RpcContext) -> Path:
    service = getattr(ctx, "skill_management_service", None)
    injected = getattr(service, "lockfile_path", None)
    return Path(injected) if injected else default_opensquilla_home() / "skills-lock.json"


@asynccontextmanager
async def _committed_lifecycle_read(ctx: RpcContext) -> AsyncIterator[None]:
    """Keep lifecycle catalog and store observations on one committed generation."""

    service = getattr(ctx, "skill_management_service", None)
    service_guard = getattr(service, "committed_store_read", None)
    if callable(service_guard):
        async with service_guard():
            yield
        return

    managed_dir = _loader_managed_dir(ctx)
    if managed_dir is None:
        yield
        return
    async with committed_store_read_guard(managed_dir):
        yield


def _recovery_required_payload(
    ctx: RpcContext,
    *,
    name: str = "",
) -> dict[str, Any] | None:
    """Return the fail-closed mutation result retained from Gateway startup."""

    # A live management service owns recovery state and can preserve the
    # current install path, install id, and loader lifecycle.  The synthetic
    # startup result is only the compatibility fallback for boots where that
    # service could not be constructed at all.
    if getattr(ctx, "skill_management_service", None) is not None:
        return None

    state = getattr(ctx, "skill_management_state", None)
    startup_diagnostics = (
        tuple(state.get("recovery_diagnostics", ()))
        if isinstance(state, dict)
        else ()
    )
    # A legacy/degraded startup may still have no management service. In that
    # case RPC must synthesize a fail-closed response from retained diagnostics.
    raw_diagnostics = startup_diagnostics
    diagnostics: list[dict[str, Any]] = []
    for item in raw_diagnostics:
        serializer = getattr(item, "to_dict", None)
        payload = serializer() if callable(serializer) else item
        if isinstance(payload, dict) and bool(payload.get("blocking")):
            diagnostics.append(dict(payload))
    if not diagnostics:
        return None
    lifecycle = SkillLifecycle(
        install_state=SkillInstallState.MISSING,
        load_state=SkillLoadState.NOT_DISCOVERED,
        selection_state=SkillSelectionState.SHADOWED,
        compatibility_state=SkillCompatibilityState.INSTRUCTION_ONLY,
        readiness_state=SkillReadinessState.UNKNOWN,
        invocation=SkillInvocationCapabilities(sandbox_execution="unknown"),
    )
    return {
        "success": False,
        "unchanged": False,
        "name": name,
        "message": "Managed Skill store requires recovery before mutation",
        "path": "",
        "scan": None,
        "installed": False,
        "active": False,
        "instruction_usable": False,
        "installId": "",
        "lifecycle": lifecycle.to_dict(),
        "resolution": None,
        "diagnostics": diagnostics,
        "reload": {},
        "rollbackPerformed": False,
        "catalogGeneration": 0,
        "effectiveFrom": "",
    }


def _install_result_to_dict(result: Any) -> dict[str, Any]:
    serializer = getattr(result, "to_dict", None)
    if callable(serializer):
        payload = dict(serializer())
    else:
        payload = {
            "success": bool(result.success),
            "name": str(result.name),
            "message": str(result.message),
        }
        if getattr(result, "path", ""):
            payload["path"] = result.path
    scan = getattr(result, "scan", None)
    if scan is not None:
        payload["scan_verdict"] = scan.verdict
        payload["scan_findings"] = [vars(finding) for finding in scan.findings]
    return payload


def _boolean_param(params: dict[str, Any], name: str, *, default: bool = False) -> bool:
    """Read an RPC boolean without accepting truthy JSON values."""

    if name not in params:
        return default
    value = params[name]
    if not isinstance(value, bool):
        raise ValueError(f"params.{name} must be a boolean")
    return value


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


def _provider_check_at_launch(
    spec: Any,
    *,
    skill_index: dict[str, Any] | None,
) -> bool:
    """Whether this exact trusted MetaSkill plan needs provider readiness later.

    Catalog eligibility intentionally remains an offline/local dependency view.
    The separate flag prevents that view from presenting a provider-backed
    MetaSkill as fully ready while keeping filtering and the existing tri-state
    wire contract stable.  Trust and consumer discovery stay code-owned in the
    capability registry; the Web UI does not need a provider or MetaSkill table.
    """

    if skill_index is None or getattr(spec, "kind", None) not in {"meta", "meta_sop"}:
        return False
    try:
        plan = parse_meta_plan(spec)
    except (MetaPlanError, TypeError, ValueError):
        return False
    if plan is None:
        return False
    return bool(
        trusted_capability_consumers_for_meta_plan(
            spec,
            plan,
            skill_resolver=skill_index,
        )
    )


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
        "description_zh": getattr(spec, "description_zh", "") or "",
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
        "user_invocable": bool(getattr(spec, "user_invocable", False)),
        "disable_model_invocation": bool(
            getattr(spec, "disable_model_invocation", False)
        ),
        "install": install_entries,
        "kind": kind,
        "sub_skills": sub_skills,
        "provider_check_at_launch": _provider_check_at_launch(
            spec,
            skill_index=skill_index,
        ),
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


def _path_key(value: str | Path) -> str:
    try:
        resolved = str(Path(value).resolve(strict=False))
    except (OSError, ValueError):
        resolved = str(value)
    return os.path.normcase(resolved)


def _candidate_by_path(candidates: tuple[Any, ...], path: str) -> Any | None:
    path_key = _path_key(path)
    return next(
        (
            candidate
            for candidate in candidates
            if _path_key(getattr(candidate, "base_dir", "")) == path_key
        ),
        None,
    )


def _doctor_item_by_install_id(items: Iterable[Any], install_id: str) -> Any | None:
    """Resolve an exact Doctor identity without choosing among corrupt duplicates."""

    matches = [item for item in items if item.install_id == install_id]
    if len(matches) > 1:
        raise KeyError(f"Skill install identity is ambiguous: {install_id}")
    return matches[0] if matches else None


def _exact_identity_param(params: dict[str, Any], camel: str, snake: str) -> str:
    """Read one optional exact-identity string from either wire spelling."""

    values = [params[key] for key in (camel, snake) if key in params]
    if not values:
        return ""
    if any(not isinstance(value, str) for value in values):
        raise ValueError(f"params.{camel} must be a string")
    normalized = [cast(str, value).strip() for value in values]
    if len(set(normalized)) > 1:
        raise ValueError(f"params.{camel} and params.{snake} must match")
    return normalized[0]


def _doctor_placeholder_row(item: Any) -> dict[str, Any]:
    """Serialize an exact Doctor item that the production loader rejected."""

    return {
        "name": item.name,
        "description": "",
        "description_zh": "",
        "layer": "managed",
        "eligible": False,
        "status": item.status,
        "status_detail": "Installed Skill is not loaded",
        "kind": "skill",
        "sub_skills": [],
        "requirements": {"summary": item.status, "items": []},
        "content": "",
        "file_path": str(Path(item.path) / "SKILL.md") if item.path else "",
        "base_dir": item.path,
        "instance_id": "",
        "install_id": item.install_id,
        "installed": item.installed,
        "active": item.active,
        "instruction_usable": item.instruction_usable,
        "lifecycle": item.lifecycle.to_dict(),
        "diagnostics": [diagnostic.to_dict() for diagnostic in item.diagnostics],
        "invocation": item.lifecycle.invocation.to_dict(),
    }


def _lifecycle_rows(
    *,
    loader: SkillLoader,
    snapshot: Any,
    base_skills: list[Any],
    skill_index: dict[str, Any],
    eligibility_ctx: EligibilityContext,
    lockfile_path: Path,
) -> list[dict[str, Any]]:
    """Serialize the opt-in lifecycle view without changing default list."""

    managed_dir = loader.managed_dir
    report = (
        SkillDoctor(
            managed_dir=managed_dir,
            lockfile_path=lockfile_path,
            loader=cast(SkillLoader, PinnedSkillLoader(snapshot, loader)),
            eligibility_context=eligibility_ctx,
        ).doctor()
        if managed_dir is not None
        else None
    )
    doctor_by_path = {
        _path_key(item.path): item
        for item in (report.skills if report is not None else ())
        if item.path
    }
    candidates = tuple(getattr(snapshot, "candidates", snapshot.skills))
    rows: list[dict[str, Any]] = []
    represented_paths: set[str] = set()

    def enrich(row: dict[str, Any], spec: Any, *, selected: bool) -> dict[str, Any]:
        base_path = _path_key(getattr(spec, "base_dir", ""))
        doctor_item = doctor_by_path.get(base_path)
        if doctor_item is not None:
            lifecycle = doctor_item.lifecycle
            diagnostics = list(doctor_item.diagnostics)
            install_id = doctor_item.install_id
            installed = doctor_item.installed
        else:
            diagnostics = []
            lifecycle = lifecycle_for_candidate(
                spec=spec,
                selected=selected,
                tracked=False,
                compatibility=SkillCompatibilityState.NATIVE,
                diagnostics=diagnostics,
            )
            install_id = ""
            installed = False
        row.update(
            {
                "instance_id": getattr(spec, "instance_id", ""),
                "install_id": install_id,
                "installed": installed,
                "active": (
                    doctor_item.active
                    if doctor_item is not None
                    else (
                        lifecycle.selection_state.value == "active"
                        and lifecycle.load_state.value == "loaded"
                    )
                ),
                "instruction_usable": (
                    doctor_item.instruction_usable
                    if doctor_item is not None
                    else lifecycle.usable is True
                ),
                "lifecycle": lifecycle.to_dict(),
                "diagnostics": [item.to_dict() for item in diagnostics],
                "invocation": lifecycle.invocation.to_dict(),
            }
        )
        represented_paths.add(base_path)
        return row

    for spec in base_skills:
        rows.append(
            enrich(
                _skill_to_dict(
                    spec,
                    diagnose_eligibility(spec, eligibility_ctx),
                    eligibility_ctx.os_name,
                    skill_index=skill_index,
                    loader=loader,
                    eligibility_ctx=eligibility_ctx,
                ),
                spec,
                selected=True,
            )
        )

    # Managed installs can be valid yet shadowed or model-hidden. They are
    # intentionally absent from the legacy winner-only list, but lifecycle-v2
    # callers need them to explain why an install is not instruction-usable.
    for doctor_item in report.skills if report is not None else ():
        path_key = _path_key(doctor_item.path)
        if path_key in represented_paths:
            continue
        spec = next(
            (
                item
                for item in candidates
                if _path_key(getattr(item, "base_dir", "")) == path_key
            ),
            None,
        )
        if spec is not None:
            row = _skill_to_dict(
                spec,
                diagnose_eligibility(spec, eligibility_ctx),
                eligibility_ctx.os_name,
                skill_index=skill_index,
                loader=loader,
                eligibility_ctx=eligibility_ctx,
            )
        else:
            row = {
                "name": doctor_item.name,
                "description": "",
                "description_zh": "",
                "layer": "managed",
                "eligible": False,
                "status": doctor_item.status,
                "status_detail": "Installed Skill is not loaded",
                "kind": "skill",
                "sub_skills": [],
                "requirements": {"summary": doctor_item.status, "items": []},
            }
        row.update(
            {
                "instance_id": getattr(spec, "instance_id", "") if spec is not None else "",
                "install_id": doctor_item.install_id,
                "installed": doctor_item.installed,
                "active": doctor_item.active,
                "instruction_usable": doctor_item.instruction_usable,
                "lifecycle": doctor_item.lifecycle.to_dict(),
                "diagnostics": [item.to_dict() for item in doctor_item.diagnostics],
                "invocation": doctor_item.lifecycle.invocation.to_dict(),
            }
        )
        rows.append(row)
        represented_paths.add(path_key)
    return rows


@_d.method("skills.status", scope="operator.read")
async def _handle_skills_status(params: dict | None, ctx: RpcContext) -> list[dict[str, Any]]:
    """Return all skills with their eligibility status."""
    loader = _get_loader(ctx)
    if loader is None:
        return []

    ctx_eligible = _eligibility_context(ctx)
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


async def _read_skills_list(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """List installed skills."""
    loader = _get_loader(ctx)
    if loader is None:
        return {"skills": []}

    ctx_eligible = _eligibility_context(ctx)
    snapshot = await _catalog_snapshot(loader, reason="rpc.skills.list")
    all_skills = snapshot.skills
    skill_index = {skill.name: skill for skill in all_skills}
    # Operator gate: coding-mode-gated skills (code-task when OFF) stay out.
    skills = [
        skill
        for skill in all_skills
        if skill.user_invocable and is_skill_available_live(skill.name)
    ]
    if isinstance(params, dict) and params.get("includeLifecycle") is True:
        return {
            "skills": _lifecycle_rows(
                loader=loader,
                snapshot=snapshot,
                base_skills=skills,
                skill_index=skill_index,
                eligibility_ctx=ctx_eligible,
                lockfile_path=_management_lockfile_path(ctx),
            )
        }
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


@_d.method("skills.list", scope="operator.read")
async def _handle_skills_list(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if isinstance(params, dict) and params.get("includeLifecycle") is True:
        async with _committed_lifecycle_read(ctx):
            return await _read_skills_list(params, ctx)
    # Preserve the non-blocking legacy winner-only catalog surface.
    return await _read_skills_list(params, ctx)


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


async def _read_skills_get(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Get a winner by name or an exact lifecycle-v2 candidate identity."""
    if not isinstance(params, dict):
        raise ValueError("params.name is required")

    instance_id = _exact_identity_param(params, "instanceId", "instance_id")
    install_id = _exact_identity_param(params, "installId", "install_id")
    if "name" not in params and not instance_id and not install_id:
        raise ValueError("params.name is required")
    requested_name = params.get("name")
    if requested_name is not None and not isinstance(requested_name, str):
        raise ValueError("params.name must be a string")

    loader = _get_loader(ctx)
    if loader is None:
        raise KeyError("No skill loader available")

    snapshot = await _catalog_snapshot(loader, reason="rpc.skills.get")
    skills = snapshot.skills
    skill_index = {item.name: item for item in skills}
    candidates = tuple(getattr(snapshot, "candidates", skills))
    doctor_report = None
    doctor_item = None
    skill = None

    if install_id:
        if loader.managed_dir is None:
            raise KeyError(f"Skill install not found: {install_id}")
        doctor_report = SkillDoctor(
            managed_dir=loader.managed_dir,
            lockfile_path=_management_lockfile_path(ctx),
            loader=cast(SkillLoader, PinnedSkillLoader(snapshot, loader)),
            eligibility_context=_eligibility_context(ctx),
        ).doctor(install_id)
        doctor_item = _doctor_item_by_install_id(doctor_report.skills, install_id)
        if doctor_item is None:
            raise KeyError(f"Skill install not found: {install_id}")
        skill = _candidate_by_path(candidates, doctor_item.path)

    if instance_id:
        instance_candidate = next(
            (
                candidate
                for candidate in candidates
                if getattr(candidate, "instance_id", "") == instance_id
            ),
            None,
        )
        if instance_candidate is None:
            raise KeyError(f"Skill instance not found: {instance_id}")
        if skill is not None and skill is not instance_candidate:
            raise KeyError("Skill installId and instanceId do not identify the same candidate")
        if doctor_item is not None and _path_key(doctor_item.path) != _path_key(
            getattr(instance_candidate, "base_dir", "")
        ):
            raise KeyError("Skill installId and instanceId do not identify the same candidate")
        skill = instance_candidate

    if skill is None and doctor_item is None:
        skill = skill_index.get(requested_name)
    if skill is None and doctor_item is None:
        raise KeyError(f"Skill not found: {requested_name}")

    if skill is not None:
        resolved_name = skill.name
    else:
        assert doctor_item is not None
        resolved_name = doctor_item.name
    if requested_name is not None and resolved_name != requested_name:
        raise KeyError(f"Skill identity does not match name: {requested_name}")
    if not is_skill_available_live(resolved_name):
        # Gated coding-mode skills are reported as not-found so their content is
        # never returned while the toggle is OFF (codex review).
        raise KeyError(f"Skill not found: {resolved_name}")

    # An install may be present in the managed store yet rejected by the
    # production loader. Exact lifecycle callers must see that Doctor item,
    # never an unrelated winner with the same manifest name.
    if skill is None:
        assert doctor_item is not None
        return _doctor_placeholder_row(doctor_item)

    ctx_eligible = _eligibility_context(ctx)
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
    exact_lookup = bool(instance_id or install_id)
    if exact_lookup:
        result["instance_id"] = getattr(skill, "instance_id", "")
        result["install_id"] = doctor_item.install_id if doctor_item is not None else ""
    if params.get("includeLifecycle") is True and loader.managed_dir is not None:
        if doctor_report is None:
            doctor_report = SkillDoctor(
                managed_dir=loader.managed_dir,
                lockfile_path=_management_lockfile_path(ctx),
                loader=cast(SkillLoader, PinnedSkillLoader(snapshot, loader)),
                eligibility_context=ctx_eligible,
            ).doctor(skill.name)
        if doctor_item is None:
            doctor_item = next(
                (
                    item
                    for item in doctor_report.skills
                    if _path_key(item.path) == _path_key(skill.base_dir)
                ),
                None,
            )
        if doctor_item is not None:
            result.update(
                {
                    "instance_id": getattr(skill, "instance_id", ""),
                    "install_id": doctor_item.install_id,
                    "installed": doctor_item.installed,
                    "active": doctor_item.active,
                    "instruction_usable": doctor_item.instruction_usable,
                    "lifecycle": doctor_item.lifecycle.to_dict(),
                    "diagnostics": [
                        item.to_dict() for item in doctor_item.diagnostics
                    ],
                    "invocation": doctor_item.lifecycle.invocation.to_dict(),
                }
            )
        else:
            lifecycle_diagnostics: list[Any] = []
            winner = skill_index.get(skill.name)
            selected = bool(
                winner is not None
                and getattr(winner, "instance_id", "") == getattr(skill, "instance_id", "")
            )
            lifecycle = lifecycle_for_candidate(
                spec=skill,
                selected=selected,
                tracked=False,
                compatibility=SkillCompatibilityState.NATIVE,
                diagnostics=lifecycle_diagnostics,
            )
            result.update(
                {
                    "instance_id": getattr(skill, "instance_id", ""),
                    "install_id": "",
                    "installed": False,
                    "active": (
                        lifecycle.selection_state.value == "active"
                        and lifecycle.load_state.value == "loaded"
                    ),
                    "instruction_usable": lifecycle.usable is True,
                    "lifecycle": lifecycle.to_dict(),
                    "diagnostics": [
                        item.to_dict() for item in lifecycle_diagnostics
                    ],
                    "invocation": lifecycle.invocation.to_dict(),
                }
            )
    return result


@_d.method("skills.get", scope="operator.read")
async def _handle_skills_get(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    lifecycle_read = bool(
        isinstance(params, dict)
        and (
            params.get("includeLifecycle") is True
            or params.get("installId")
            or params.get("install_id")
        )
    )
    if lifecycle_read:
        async with _committed_lifecycle_read(ctx):
            return await _read_skills_get(params, ctx)
    return await _read_skills_get(params, ctx)


@_d.method("skills.search", scope="operator.read")
async def _handle_skills_search(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Search for skills across Community sources."""
    if not isinstance(params, dict) or "query" not in params:
        raise ValueError("params.query is required")

    management_service = getattr(ctx, "skill_management_service", None)
    router = getattr(management_service, "router", None)
    if router is None:
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
    report = await search_router_with_diagnostics(
        router,
        query,
        limit=limit,
        source_id=source_id,
    )
    results = report.results
    search_diagnostics = [item.to_dict() for item in report.diagnostics]
    injected_lockfile = getattr(management_service, "lockfile_path", None)
    if injected_lockfile:
        from opensquilla.skills.hub.lockfile import Lockfile

        installed = Lockfile.load(Path(injected_lockfile))
    else:
        installed = installed_skill_lockfile()
    payload: dict[str, Any] = {
        "results": [
            {
                "name": r.name,
                "description": r.description,
                "version": r.version,
                "author": r.author,
                "source": r.source_id,
                "trust_level": r.trust_level,
                "identifier": r.identifier,
                "installReference": r.canonical_identifier or r.identifier,
                "installed": is_skill_meta_installed(r, installed),
            }
            for r in results
        ]
    }
    if search_diagnostics:
        payload["diagnostics"] = search_diagnostics
        payload["partial"] = report.partial
        payload["allSourcesUnavailable"] = report.all_sources_unavailable
    return payload


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
    operation_id = _skill_install_operation_id(params)
    operation_key = (
        _skill_install_operation_key(ctx, operation_id) if operation_id else None
    )
    active_installs = _active_skill_installs(ctx) if operation_key else None
    pending_cancellations = (
        _pending_skill_install_cancellations(ctx) if operation_key else None
    )
    if operation_key and pending_cancellations is not None:
        if operation_key in pending_cancellations:
            pending_cancellations.discard(operation_key)
            return _skill_install_cancelled_payload()
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Skill installation is not running in an asyncio task")
        assert active_installs is not None
        if operation_key in active_installs:
            raise ValueError("params.operationId is already active")
        active_installs[operation_key] = task

    try:
        return await _run_skill_install(params, ctx)
    except asyncio.CancelledError:
        if not operation_id:
            raise
        return _skill_install_cancelled_payload()
    finally:
        if operation_key and active_installs is not None:
            active_installs.pop(operation_key, None)


async def _run_skill_install(params: dict[str, Any], ctx: RpcContext) -> dict[str, Any]:
    recovery_failure = _recovery_required_payload(ctx)
    if recovery_failure is not None:
        return recovery_failure
    loader = _get_loader(ctx)
    if loader is None:
        return {"success": False, "message": "No skill loader configured"}

    installer = _management_service(ctx)
    if installer is None:
        return {"success": False, "message": "No skill installer configured"}

    identifier = params["identifier"]
    source_id = params.get("source", "clawhub")
    force = _boolean_param(params, "force")
    replace_source = _boolean_param(params, "replaceSource")
    risk_confirmation = _exact_identity_param(
        params,
        "riskConfirmation",
        "risk_confirmation",
    )
    install = installer.install
    if replace_source and not supports_keyword_argument(install, "replace_source"):
        return _install_result_to_dict(
            unsupported_installer_result(
                operation="install",
                capability="replaceSource",
                name=str(identifier),
            )
        )
    if force and not supports_keyword_argument(install, "force"):
        return _install_result_to_dict(
            unsupported_installer_result(
                operation="install",
                capability="force",
                name=str(identifier),
            )
        )
    if risk_confirmation and not supports_keyword_argument(install, "risk_confirmation"):
        return _install_result_to_dict(
            unsupported_installer_result(
                operation="install",
                capability="riskConfirmation",
                name=str(identifier),
            )
        )
    if not isinstance(installer, SkillManagementService) and force and (
        not risk_confirmation
        or not supports_keyword_argument(install, "risk_confirmation")
    ):
        return _install_result_to_dict(
            unsupported_installer_result(
                operation="install",
                capability="riskConfirmation",
                name=str(identifier),
            )
        )
    install_kwargs = supported_keyword_arguments(
        install,
        {
            "force": force,
            "replace_source": replace_source,
            "risk_confirmation": risk_confirmation,
        },
    )
    if isinstance(installer, SkillManagementService):
        result = await install(identifier, source_id, **install_kwargs)
    else:
        result = await _run_catalog_mutation(
            loader,
            reason="rpc.skills.install",
            operation=lambda: install(identifier, source_id, **install_kwargs),
            did_change=lambda value: bool(value.success),
        )
    return _install_result_to_dict(result)


@_d.method("skills.install.cancel", scope="operator.admin")
async def _handle_skills_install_cancel(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    """Cancel one install owned by this connection and await its cleanup."""
    if not isinstance(params, dict):
        raise ValueError("params.operationId is required")
    operation_id = _skill_install_operation_id(params)
    if not operation_id:
        raise ValueError("params.operationId is required")

    operation_key = _skill_install_operation_key(ctx, operation_id)
    active_installs = _active_skill_installs(ctx)
    task = active_installs.get(operation_key)
    if task is None:
        pending = _pending_skill_install_cancellations(ctx)
        connection_pending = [key for key in pending if key[0] == ctx.conn_id]
        if len(connection_pending) >= _MAX_PENDING_SKILL_INSTALL_CANCELLATIONS_PER_CONNECTION:
            pending.discard(connection_pending[0])
        if len(pending) >= _MAX_PENDING_SKILL_INSTALL_CANCELLATIONS:
            pending.pop()
        pending.add(operation_key)
        return {**_skill_install_cancelled_payload(), "pending": True}

    task.cancel()
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        if not task.cancelled():
            raise
    return {**_skill_install_cancelled_payload(), "pending": False}


@_d.method("skills.update", scope="operator.admin")
async def _handle_skills_update(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Update installed skills from lockfile."""
    recovery_failure = _recovery_required_payload(
        ctx,
        name=str((params or {}).get("name") or ""),
    )
    if recovery_failure is not None:
        return {**recovery_failure, "results": []}
    loader = _get_loader(ctx)
    if loader is None:
        return {
            "results": [],
            "success": False,
            "message": "No skill loader configured",
        }
    installer = _management_service(ctx)
    if installer is None:
        return {"success": False, "message": "No skill installer configured"}

    name = (params or {}).get("name")
    install_id = _exact_identity_param(params or {}, "installId", "install_id")
    force = _boolean_param(params or {}, "force")
    risk_confirmation = _exact_identity_param(
        params or {},
        "riskConfirmation",
        "risk_confirmation",
    )
    try:
        update = installer.update
        requested_capabilities = (
            ("install_id", "installId", bool(install_id)),
            ("force", "force", force),
            ("risk_confirmation", "riskConfirmation", bool(risk_confirmation)),
        )
        for keyword, capability, required in requested_capabilities:
            if not required or supports_keyword_argument(update, keyword):
                continue
            unsupported = unsupported_installer_result(
                operation="update",
                capability=capability,
                name=str(name or ""),
                install_id=install_id,
            )
            return {**_install_result_to_dict(unsupported), "results": []}
        if not isinstance(installer, SkillManagementService) and force and (
            not risk_confirmation
            or not supports_keyword_argument(update, "risk_confirmation")
        ):
            unsupported = unsupported_installer_result(
                operation="update",
                capability="riskConfirmation",
                name=str(name or ""),
                install_id=install_id,
            )
            return {**_install_result_to_dict(unsupported), "results": []}
        update_kwargs = supported_keyword_arguments(
            update,
            {
                "install_id": install_id,
                "force": force,
                "risk_confirmation": risk_confirmation,
            },
        )
        if isinstance(installer, SkillManagementService):
            results = await update(name, **update_kwargs)
        else:
            results = await _run_catalog_mutation(
                loader,
                reason="rpc.skills.update",
                operation=lambda: update(name, **update_kwargs),
                did_change=lambda values: any(value.success for value in values),
            )
    except OSError as exc:
        return {
            "results": [],
            "success": False,
            "message": f"Skill update unavailable: {exc}",
        }
    return {
        "results": [_install_result_to_dict(r) for r in results]
    }


@_d.method("skills.uninstall", scope="operator.admin")
async def _handle_skills_uninstall(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Uninstall a managed skill."""
    if not isinstance(params, dict):
        raise ValueError("params.name or params.installId is required")
    install_id = _exact_identity_param(params, "installId", "install_id")
    if "name" not in params and not install_id:
        raise ValueError("params.name or params.installId is required")
    name = str(params.get("name") or "")
    recovery_failure = _recovery_required_payload(ctx, name=name)
    if recovery_failure is not None:
        return recovery_failure

    installer = _management_service(ctx)
    if installer is None:
        return {"success": False, "message": "No skill installer configured"}

    loader = _get_loader(ctx)
    allow_drift = _boolean_param(params, "allowDrift")
    if isinstance(installer, SkillManagementService):
        result = await installer.uninstall(
            name,
            install_id=install_id,
            allow_drift=allow_drift,
        )
    else:
        uninstall = installer.uninstall
        if install_id and not supports_keyword_argument(uninstall, "install_id"):
            return _install_result_to_dict(
                unsupported_installer_result(
                    operation="uninstall",
                    capability="installId",
                    name=name,
                    install_id=install_id,
                )
            )
        if allow_drift and not supports_keyword_argument(uninstall, "allow_drift"):
            return _install_result_to_dict(
                unsupported_installer_result(
                    operation="uninstall",
                    capability="allowDrift",
                    name=name,
                    install_id=install_id,
                )
            )
        uninstall_kwargs = supported_keyword_arguments(
            uninstall,
            {"install_id": install_id, "allow_drift": allow_drift},
        )
        result = await _run_catalog_mutation(
            loader,
            reason="rpc.skills.uninstall",
            operation=lambda: uninstall(name, **uninstall_kwargs),
            did_change=lambda value: bool(value.success),
        )
    return _install_result_to_dict(result)


async def _read_skills_doctor(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Read-only local diagnostics for managed Community Skills."""

    loader = _get_loader(ctx)
    managed_dir = _loader_managed_dir(ctx)
    if managed_dir is None:
        return {
            "ok": False,
            "skills": [],
            "diagnostics": [
                {
                    "code": "MANAGED_ROOT_UNAVAILABLE",
                    "severity": "error",
                    "phase": "store",
                    "blocking": True,
                    "message": "No managed Skill directory is configured",
                    "hint": "Configure a managed Skill directory and restart the Gateway.",
                    "details": {},
                }
            ],
        }
    target = ""
    if isinstance(params, dict):
        target = str(params.get("name") or params.get("installId") or "").strip()
    report = SkillDoctor(
        managed_dir=managed_dir,
        lockfile_path=_management_lockfile_path(ctx),
        loader=loader,
        journal_path=Path(
            getattr(getattr(ctx, "skill_management_service", None), "journal_path", None)
            or _journal_path(ctx, managed_dir)
        ),
        eligibility_context=_eligibility_context(ctx),
        additional_diagnostics=(
            *(
                tuple(ctx.skill_management_state.get("recovery_diagnostics", ()))
                if isinstance(ctx.skill_management_state, dict)
                else ()
            ),
            *tuple(
                getattr(
                    getattr(ctx, "skill_management_service", None),
                    "recovery_diagnostics",
                    (),
                )
                or ()
            ),
        ),
    ).doctor(target or None)
    return report.to_dict()


@_d.method("skills.doctor", scope="operator.read")
async def _handle_skills_doctor(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    async with _committed_lifecycle_read(ctx):
        return await _read_skills_doctor(params, ctx)


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
    exact_install_id = _exact_identity_param(params, "installId", "skill_install_id")
    instance_id = _exact_identity_param(params, "instanceId", "instance_id")
    if "name" not in params and not exact_install_id and not instance_id:
        raise ValueError("params.name, params.installId, or params.instanceId is required")
    if "install_id" not in params:
        raise ValueError("params.install_id is required")

    name = str(params.get("name") or "")
    install_id = params["install_id"]
    loader = _get_loader(ctx)
    if loader is None:
        raise KeyError("No skill loader available")
    snapshot = await _catalog_snapshot(loader, reason="rpc.skills.deps.install")
    candidates = tuple(getattr(snapshot, "candidates", snapshot.skills))
    skill = None
    doctor_item = None
    if exact_install_id:
        if loader.managed_dir is None:
            raise KeyError(f"Skill install not found: {exact_install_id}")
        doctor_report = SkillDoctor(
            managed_dir=loader.managed_dir,
            lockfile_path=_management_lockfile_path(ctx),
            loader=cast(SkillLoader, PinnedSkillLoader(snapshot, loader)),
            eligibility_context=_eligibility_context(ctx),
        ).doctor(exact_install_id)
        doctor_item = _doctor_item_by_install_id(
            doctor_report.skills,
            exact_install_id,
        )
        if doctor_item is None:
            raise KeyError(f"Skill install not found: {exact_install_id}")
        skill = _candidate_by_path(candidates, doctor_item.path)
    if instance_id:
        instance_candidate = next(
            (
                candidate
                for candidate in candidates
                if getattr(candidate, "instance_id", "") == instance_id
            ),
            None,
        )
        if instance_candidate is None:
            raise KeyError(f"Skill instance not found: {instance_id}")
        if skill is not None and skill is not instance_candidate:
            raise KeyError("Skill installId and instanceId do not identify the same candidate")
        skill = instance_candidate
    if skill is None and not exact_install_id and not instance_id:
        skill = next((item for item in snapshot.skills if item.name == name), None)
    elif skill is None:
        raise KeyError("Exact Skill install is not loaded in the current catalog")
    if skill is not None and name and skill.name != name:
        raise KeyError(f"Skill identity does not match name: {name}")
    resolved_name = skill.name if skill is not None else name
    if skill is None or not is_skill_available_live(resolved_name):
        # Coding-mode-gated skills are reported as not-found so they cannot be
        # resolved or have deps installed while the toggle is OFF (codex review).
        raise KeyError(f"Skill not found: {resolved_name}")

    specs = skill.metadata.install if skill.metadata else []
    spec = next((s for s in specs if s.id == install_id), None)
    if spec is None:
        raise KeyError(f"Install spec not found: {install_id}")

    ctx_eligible = _eligibility_context(ctx)
    if spec.os and ctx_eligible.os_name and ctx_eligible.os_name not in spec.os:
        raise ValueError(
            f"Install spec {install_id!r} not supported on "
            f"{ctx_eligible.os_name} (requires: {', '.join(spec.os)})"
        )

    dependency_lock_identity = str(
        exact_install_id or getattr(skill, "instance_id", "") or resolved_name
    )
    async with _deps_lock_for(dependency_lock_identity, str(install_id)):
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


def _get_default_installer(*, managed_dir=None, loader=None, journal_path=None, offline=None):
    kwargs = {
        "managed_dir": managed_dir,
        **supported_keyword_arguments(
            build_default_skill_installer,
            {
                "loader": loader,
                "journal_path": journal_path,
                "offline": offline,
            },
        ),
    }
    return build_default_skill_installer(**kwargs)
