"""Fail-closed loading of authenticated per-message Skill selections."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from opensquilla.engine.pipeline import TurnContext
from opensquilla.skills.body import emit_skill_load, expanded_skill_body
from opensquilla.skills.catalog_policy import is_user_invocable_ordinary
from opensquilla.skills.eligibility import (
    EligibilityContext,
    diagnose_eligibility,
    effective_disabled,
    is_skill_available_live,
)
from opensquilla.skills.tree import compute_tree_sha256
from opensquilla.token_estimation import estimate_tokens


class SelectedSkillError(ValueError):
    """The requested turn cannot run with its explicitly selected instructions."""


async def load_selected_skills(ctx: TurnContext, tool_context: Any) -> TurnContext:
    """Load all selected bodies before provider admission, outside fail-open steps.

    The snapshot is fixed at the provider/tools boundary. Identity binds the
    selection to its winning instance and digest, rather than a catalog version
    that unrelated installations may advance.
    """

    refs = ctx.metadata.get("selected_skills") or ()
    if not refs:
        return ctx
    cfg = getattr(ctx.config, "skills", None)
    eligibility = EligibilityContext.auto(
        disabled_set=set(effective_disabled(getattr(cfg, "disabled", []) or [])),
    )
    catalog = ctx.skill_catalog
    skills = {skill.name: skill for skill in getattr(catalog, "skills", ())}
    allowed = getattr(tool_context, "authorized_tool_names", None)
    if allowed is None:
        allowed = {tool.name for tool in ctx.tool_defs}
    bodies: list[str] = []
    selected: list[Any] = []
    seen: set[str] = set()
    seen_refs: set[tuple[str, str, str]] = set()
    started: list[Any] = []
    try:
        for ref in refs:
            name = str(ref.get("name", "")) if isinstance(ref, dict) else ""
            skill = skills.get(name)
            receipt_skill = SimpleNamespace(
                name=name,
                instance_id=str(ref.get("instanceId", "")) if isinstance(ref, dict) else "",
                tree_digest=str(ref.get("digest", "")) if isinstance(ref, dict) else "",
            )
            ref_key = (name, receipt_skill.instance_id, receipt_skill.tree_digest)
            if ref_key in seen_refs:
                continue
            started.append(receipt_skill)
            await emit_skill_load(tool_context, receipt_skill, source="user", status="loading")
            error = ""
            if not isinstance(ref, dict) or not name:
                error = "Invalid Skill selection. Remove it and select the Skill again."
            elif getattr(tool_context, "guest_safe", False) or "skill_view" not in allowed:
                error = "Skill loading is unavailable under this turn's tool permissions."
            elif skill is None:
                error = f"Selected Skill '{name}' is no longer installed. Select it again."
            elif (
                not ref.get("instanceId") or not ref.get("digest")
                or skill.instance_id != ref["instanceId"] or skill.tree_digest != ref["digest"]
            ):
                error = f"Selected Skill '{name}' changed. Remove it and select it again."
            elif not is_user_invocable_ordinary(skill):
                error = f"Skill '{name}' cannot be selected manually in the current mode."
            elif not is_skill_available_live(name):
                error = f"Skill '{name}' is disabled. Allow it in Skill settings before retrying."
            else:
                report = diagnose_eligibility(skill, eligibility)
                if not report.eligible:
                    error = f"Skill '{name}' is unavailable: {'; '.join(report.reasons)}"
                elif skill.requires_tools and not set(skill.requires_tools).issubset(allowed):
                    error = f"Skill '{name}' requires tools unavailable in this turn."
                else:
                    try:
                        digest = await asyncio.to_thread(compute_tree_sha256, Path(skill.base_dir))
                    except (OSError, ValueError):
                        digest = ""
                    if digest != skill.tree_digest:
                        error = f"Skill '{name}' changed on disk. Refresh and select it again."
            if error:
                await emit_skill_load(
                    tool_context, receipt_skill, source="user", status="failed", error=error,
                )
                raise SelectedSkillError(error)
            assert skill is not None
            body = expanded_skill_body(skill)
            if not body.strip():
                error = f"Skill '{name}' has no instruction body. Repair it before retrying."
                await emit_skill_load(
                    tool_context, skill, source="user", status="failed", error=error,
                )
                raise SelectedSkillError(error)
            seen.add(skill.instance_id)
            seen_refs.add(ref_key)
            bodies.append(f"## Selected Skill: {name}\n\n{body}")
            selected.append(skill)

        # Digest checks yield to the event loop. An operator can disable any
        # staged selection while a later tree is being verified; commit none
        # of the bodies if the live gate changed during that interval.
        for skill in selected:
            if not is_skill_available_live(skill.name):
                raise SelectedSkillError(
                    f"Skill '{skill.name}' is disabled. "
                    "Allow it in Skill settings before retrying."
                )
        base, suffix = (
            (ctx.system_prompt, "") if isinstance(ctx.system_prompt, str) else ctx.system_prompt
        )
        instructions = "\n\n".join(bodies)
        llm = getattr(ctx.config, "llm", None)
        window = int(getattr(llm, "context_window_tokens", 0) or 0)
        output = int(getattr(llm, "max_tokens", 0) or 0)
        # The finalized provider governor remains authoritative. This early
        # bound guarantees explicit bodies alone cannot consume its budget;
        # bodies are never truncated to the optional catalog-description cap.
        if window and estimate_tokens(f"{base}\n{suffix}\n{instructions}\n{ctx.message}") >= (
            window - output
        ):
            error = "Selected Skill instructions exceed the context budget. Select fewer Skills."
            raise SelectedSkillError(error)
        ctx.system_prompt = (base, f"{suffix}\n\n{instructions}".strip())
        tool_context.verified_skill_ids.update(seen)
        ctx.metadata["selected_skill_ids"] = [skill.instance_id for skill in selected]
        for skill in selected:
            await emit_skill_load(tool_context, skill, source="user", status="loaded")
        return ctx
    except asyncio.CancelledError:
        for pending in started:
            await emit_skill_load(
                tool_context, pending, source="user", status="failed",
                error="Skill loading was cancelled before completion.",
            )
        raise
    except SelectedSkillError as exc:
        for skill in selected:
            await emit_skill_load(
                tool_context, skill, source="user", status="failed", error=str(exc),
            )
        raise
    except Exception as exc:
        # No filesystem paths, credentials, or exception internals in receipts.
        error = "Selected Skills could not be loaded. Refresh the Skill catalog and retry."
        for pending in started:
            await emit_skill_load(
                tool_context, pending, source="user", status="failed", error=error,
            )
        raise SelectedSkillError(error) from exc
