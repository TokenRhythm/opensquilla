"""In-process structural and scheduler gates for MetaSkill proposals."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from opensquilla.skills.catalog_policy import (
    STABLE_META_DEPENDENCIES,
    project_creator_catalog,
)
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.manifest import compile_skill_manifest
from opensquilla.skills.meta.parser import MetaPlan, MetaPlanError, parse_meta_plan
from opensquilla.skills.meta.sop_compiler import SOPCompileError
from opensquilla.skills.meta.sop_compiler import compile as compile_sop
from opensquilla.skills.types import SkillLayer

_BUNDLED = Path(__file__).resolve().parents[1] / "bundled"
_UNESCAPED_USER_MESSAGE = __import__("re").compile(
    r"\{\{\s*inputs\.user_message(?=[\s|}])(?!\s*\|\s*(xml_escape|slugify)\b)"
)


def _run_g1(
    skill_md: str,
    *,
    loader: SkillLoader,
    catalog: dict[str, str],
) -> dict[str, Any]:
    diagnostics: list[str] = []
    try:
        spec = compile_skill_manifest(
            Path("virtual-meta-skill-candidate"),
            SkillLayer.EXTRA,
            skill_bytes=skill_md.encode("utf-8"),
        )
    except Exception as exc:  # noqa: BLE001 - linter returns diagnostics
        diagnostics.append(f"G1.1 (loader): {type(exc).__name__}: {exc}")
        return {"passed": False, "diagnostics": diagnostics, "spec": None, "plan": None}

    if spec.kind == "meta_sop":
        try:
            spec = compile_sop(spec, skill_loader=loader)
        except SOPCompileError as exc:
            diagnostics.append(f"G1.1 (sop_compile): {exc}")
            return {"passed": False, "diagnostics": diagnostics, "spec": spec, "plan": None}
    try:
        plan = parse_meta_plan(spec)
    except MetaPlanError as exc:
        diagnostics.append(f"G1.1 (parse_meta_plan): {exc}")
        return {"passed": False, "diagnostics": diagnostics, "spec": spec, "plan": None}

    if spec.kind == "meta" and _UNESCAPED_USER_MESSAGE.search(skill_md):
        diagnostics.append(
            "G1.6: every `{{ inputs.user_message ` must be immediately followed by "
            "`| xml_escape` or `| slugify`"
        )
    if plan is None:
        diagnostics.append("G1.1: parse_meta_plan returned None (kind != meta?)")
        return {"passed": False, "diagnostics": diagnostics, "spec": spec, "plan": None}

    for step in plan.steps:
        if step.kind not in {"agent", "skill_exec"}:
            continue
        if step.skill not in catalog:
            diagnostics.append(
                f"G1.2: step {step.id!r} references unknown skill {step.skill!r}"
            )
        elif catalog[step.skill] == "meta":
            diagnostics.append(
                f"G1.2: step {step.id!r} references {step.skill!r} which is kind: meta; "
                "nested MetaSkills are not supported"
            )
    return {
        "passed": not diagnostics,
        "diagnostics": diagnostics,
        "spec": spec,
        "plan": plan,
    }


async def _run_g2_async(plan: MetaPlan) -> dict[str, Any]:
    from opensquilla.skills.meta.events import _StepDone
    from opensquilla.skills.meta.scheduler import run_dag
    from opensquilla.skills.meta.types import MetaMatch

    async def dispatch(step: Any, _skill: str, _inputs: Any, _outputs: Any):
        yield _StepDone(text=f"<stub:{step.id}>")

    async def preface(_step_id: str, _skill: str):
        if False:  # pragma: no cover - establish async-generator shape
            yield None

    try:
        match = MetaMatch(plan=plan, inputs={"user_message": "<test>"})
        async for _event in run_dag(
            match,
            dispatch_step_stream=dispatch,
            yield_skill_view_preface=preface,
        ):
            pass
    except Exception as exc:  # noqa: BLE001 - linter returns diagnostics
        return {
            "passed": False,
            "diagnostics": [f"G2 (scheduler dry-run): {type(exc).__name__}: {exc}"],
            "steps_visited": 0,
        }
    return {"passed": True, "diagnostics": [], "steps_visited": len(plan.steps)}


def lint_meta_skill(skill_md: str, gates: str = "G1,G2") -> dict[str, Any]:
    """Run the creator's G1/G2 gates without a legacy bundled helper Skill."""

    selected = {gate.strip() for gate in gates.split(",") if gate.strip()}
    with tempfile.TemporaryDirectory(prefix="opensquilla-meta-lint-") as tmp:
        loader = SkillLoader(
            bundled_dir=_BUNDLED,
            snapshot_path=Path(tmp) / "skills-snapshot.json",
        )
        specs = loader.load_all()
        catalog_specs = project_creator_catalog(specs)
        candidate_name = ""
        try:
            candidate = compile_skill_manifest(
                Path("virtual-meta-skill-candidate"),
                SkillLayer.EXTRA,
                skill_bytes=skill_md.encode("utf-8"),
            )
            candidate_name = candidate.name
        except Exception:  # G1 reports the authoritative parse diagnostic.
            pass
        if candidate_name in STABLE_META_DEPENDENCIES:
            allowed = set(STABLE_META_DEPENDENCIES[candidate_name])
            catalog_specs.extend(spec for spec in specs if spec.name in allowed)
        catalog = {spec.name: spec.kind for spec in catalog_specs}
        out: dict[str, Any] = {}
        g1 = _run_g1(skill_md, loader=loader, catalog=catalog)
        if "G1" in selected:
            out["G1"] = {
                "passed": g1["passed"],
                "diagnostics": g1["diagnostics"],
            }
        if not g1["passed"]:
            return out
        if "G2" in selected:
            out["G2"] = asyncio.run(_run_g2_async(g1["plan"]))
        return out
