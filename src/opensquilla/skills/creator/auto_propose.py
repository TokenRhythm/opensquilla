"""Library function that drives meta-skill-creator unattended.

Used by:
  * the scheduler's ``auto_propose`` cron handler (Path 1)
  * the dream handler's post-completion hook (Path 2)

Behaviour: read the decision-log, aggregate top-K co-occurrence chains,
filter by frequency floor and existing-coverage, deduplicate against
already-pending proposals, then for each surviving pattern run the
meta-skill-creator DAG once and patch the resulting ``gates.json``
with provenance so the WebUI (Path 3) can distinguish auto-generated
proposals from user-invoked ones.

This function is intentionally **fault-tolerant**: it never raises,
because both callers (cron + dream) run in fire-and-forget contexts
where a single bad pattern must not kill the handler. All exceptions
are collected into ``AutoProposeResult.errors``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from opensquilla.observability.decision_log_aggregate import (
    aggregate_co_occurrences,
)
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.meta.orchestrator import MetaOrchestrator
from opensquilla.skills.meta.parser import parse_meta_plan
from opensquilla.skills.meta.types import MetaMatch
from opensquilla.skills.proposals_lib import accept_proposal

_log = logging.getLogger(__name__)

# Whichever home the gateway uses for state — also where proposals/ lives.
_DEFAULT_PROPOSALS_DIRNAME = "proposals"

# meta-skill-creator's name in the bundled skill catalog. The DAG that
# auto_propose drives.
_META_SKILL_CREATOR = "meta-skill-creator"

# Trigger phrases of meta-skill-creator. The synthesised user_message
# must avoid ALL of these as substrings so the substring-match in
# engine/steps/meta_resolution.py cannot accidentally re-fire the
# meta-resolution pipeline against our generated text (would only matter
# if the synthesised message were ever fed back into a turn, but cheap
# insurance).
_META_SKILL_CREATOR_TRIGGERS: tuple[str, ...] = (
    "新增 meta 技能",
    "组合现有 skill 成 meta-skill",
    "synthesize meta-skill",
    "compose meta-skill",
)

_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

# Compatibility fallback for skills that have not yet declared
# ``metadata.opensquilla.capabilities`` / ``risk`` in their manifests.
_HIGH_RISK_SKILLS = frozenset({
    "github",
    "tmux",
})

_MEDIUM_RISK_SKILLS = frozenset({
    "docx",
    "html-to-pdf",
    "latex-compile",
    "nano-pdf",
    "pdf-toolkit",
    "pptx",
    "xlsx",
})

_LOW_RISK_SKILLS = frozenset({
    "history-explorer",
    "summarize",
})

_CAPABILITY_RISK = {
    "artifact-write": "medium",
    "document-export": "medium",
    "filesystem-write": "medium",
    "network": "medium",
    "network-read": "medium",
    "credential-use": "high",
    "external-side-effect": "high",
    "network-write": "high",
    "process-control": "high",
    "shell": "high",
}

_SAFE_OUTPUT_TEMPLATE_FILTERS = frozenset({
    "truncate",
    "xml_escape",
    "slugify",
    "tojson",
})
_JINJA_EXPR_RE = re.compile(r"\{\{\s*(.*?)\s*\}\}", re.DOTALL)
_OUTPUT_REF_RE = re.compile(r"\boutputs\.[A-Za-z_][A-Za-z0-9_]*\b")
_USER_INPUT_REF_RE = re.compile(r"\binputs\.user_message\b")


@dataclass(frozen=True)
class _SkippedPattern:
    skills: list[str]
    freq: int
    reason: str


@dataclass(frozen=True)
class _PatternError:
    skills: list[str]
    freq: int
    error: str


@dataclass(frozen=True)
class AutoProposeResult:
    """Structured outcome — never the exception itself.

    ``proposals_created`` lists the 8-hex proposal_ids that landed
    under ``proposals_dir`` during this run. ``skipped`` and
    ``errors`` are diagnostic only — the caller logs them but does
    not act on them.
    """

    proposals_created: list[str] = field(default_factory=list)
    proposals_enabled: list[str] = field(default_factory=list)
    auto_enable: list[dict[str, object]] = field(default_factory=list)
    skipped: list[dict[str, object]] = field(default_factory=list)
    errors: list[dict[str, object]] = field(default_factory=list)
    triggered_by: str = "cron"

    def summary(self) -> str:
        return (
            f"auto_propose proposals={len(self.proposals_created)} "
            f"enabled={len(self.proposals_enabled)} "
            f"skipped={len(self.skipped)} errors={len(self.errors)} "
            f"via={self.triggered_by}"
        )


def _chain_signature(skills: list[str], intent_digest: str = "") -> str:
    """Stable identifier for a chain including order and intent context."""
    payload = {
        "skills": list(skills),
        "intent_digest": str(intent_digest or "").strip(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


def _chain_hash(skills: list[str]) -> str:
    """Backward-compatible ordered chain identifier without intent context."""
    return _chain_signature(skills, "")


def _existing_chain_hashes(proposals_dir: Path) -> set[str]:
    """Read every pending proposal's ``gates.json`` and collect chain hashes."""
    hashes: set[str] = set()
    if not proposals_dir.is_dir():
        return hashes
    for sub in proposals_dir.iterdir():
        if not sub.is_dir():
            continue
        gates_path = sub / "gates.json"
        if not gates_path.is_file():
            continue
        try:
            gates = json.loads(gates_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        prov = gates.get("provenance") or {}
        for key in ("proposal_signature", "chain_hash"):
            ch = prov.get(key)
            if isinstance(ch, str) and ch:
                hashes.add(ch)
    return hashes


def _meta_skill_coverage(skill_loader: SkillLoader) -> list[set[str]]:
    """Return one set per existing meta-skill — the skills it composes.

    Used to skip patterns whose every member is already covered by
    some existing meta-skill (no point synthesising a duplicate
    wrapper).
    """
    coverage: list[set[str]] = []
    for spec in skill_loader.list_meta_specs():
        composition = getattr(spec, "composition_raw", None) or {}
        steps = composition.get("steps") or []
        if not isinstance(steps, list):
            continue
        skills: set[str] = set()
        for step in steps:
            if not isinstance(step, dict):
                continue
            name = step.get("skill")
            if isinstance(name, str) and name:
                skills.add(name)
        if skills:
            coverage.append(skills)
    return coverage


def _meta_skill_names(skill_loader: SkillLoader) -> set[str]:
    """Names of all ``kind: meta`` skills currently loaded.

    Used to strip meta-skill members from candidate co-occurrence chains
    before they're shown to the synthesis LLM. The runtime forbids
    nested meta-skills (lint G1.2), so leaving them in the chain only
    invites the LLM to propose structurally-invalid SKILL.md files.
    """
    return {spec.name for spec in skill_loader.list_meta_specs()}


def _available_skill_names(skill_loader: SkillLoader) -> set[str]:
    """Names currently resolvable by the loader."""

    try:
        return {
            spec.name
            for spec in skill_loader.load_all()
            if getattr(spec, "name", None)
        }
    except Exception:
        return set()


def _pattern_already_covered(skills: list[str], coverage: list[set[str]]) -> bool:
    pattern_set = set(skills)
    return any(pattern_set <= covered for covered in coverage)


def _synthesise_user_message(
    skills: list[str],
    freq: int,
    window_days: int,
    *,
    intent_digest: str = "",
) -> str:
    """Build a user_message string for the DAG that does NOT contain any
    meta-skill-creator trigger phrase (regression-tested)."""
    skill_list = ", ".join(skills)
    msg = (
        f"auto-proposal: candidate skill chain {{{skill_list}}} observed "
        f"{freq} times in last {window_days}d. Wrap as a new bundled "
        f"meta-skill. This unattended proposal requires FULL_GATED validation: "
        f"run acceptance comparison, runtime E2E comparison against the "
        f"highest-tier no-meta baseline, lint, smoke, risk checks, and proposal "
        f"persistence before any auto-enable decision."
    )
    if intent_digest:
        msg += f" Observed user-intent digest: {intent_digest[:500]}"
    # Loop-safety check. The earlier ``assert`` form was a real safety
    # gate, but ``python -O`` strips assertions, so a production build
    # silently lost the recursion guard. ``raise`` keeps the check
    # active in every build and lets the caller see a structured error
    # instead of a silent recursion.
    lower = msg.lower()
    for trig in _META_SKILL_CREATOR_TRIGGERS:
        if trig.lower() in lower:
            raise RuntimeError(
                f"synthesised user_message contains meta-skill-creator "
                f"trigger {trig!r}; auto_propose would recursively trigger "
                f"itself if this message reached the resolver",
            )
    return msg


def _patch_gates_provenance(
    proposal_dir: Path,
    *,
    triggered_by: str,
    skills: list[str],
    freq: int,
    window_days: int,
    chain_hash: str,
    proposal_signature: str,
    intent_digest: str = "",
    source_context: str = "",
) -> None:
    """Add an additive ``provenance`` key to gates.json without touching
    the existing lint / smoke / auto_enable_eligible payload."""
    gates_path = proposal_dir / "gates.json"
    if not gates_path.is_file():
        return
    try:
        gates = json.loads(gates_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _log.warning("auto_propose.gates_read_failed: %s", exc)
        return
    gates["provenance"] = {
        "triggered_by": f"auto_{triggered_by}",
        "chain_hash": chain_hash,
        "proposal_signature": proposal_signature,
        "auto_propose_meta": {
            "skills": list(skills),
            "freq": freq,
            "window_days": window_days,
            "intent_digest": intent_digest,
            "source_context": source_context,
        },
    }
    try:
        gates_path.write_text(
            json.dumps(gates, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        _log.warning("auto_propose.gates_write_failed: %s", exc)


def _patch_gates_auto_enable(proposal_dir: Path, payload: dict[str, object]) -> None:
    """Record the auto-enable decision on the proposal/accepted gates file."""
    gates_path = proposal_dir / "gates.json"
    if not gates_path.is_file():
        return
    try:
        gates = json.loads(gates_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _log.warning("auto_propose.auto_enable_gates_read_failed: %s", exc)
        return
    gates["auto_enable"] = dict(payload)
    try:
        gates_path.write_text(
            json.dumps(gates, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        _log.warning("auto_propose.auto_enable_gates_write_failed: %s", exc)


def _load_proposal_spec(proposal_dir: Path):
    """Parse one proposal SKILL.md through the same loader path as runtime."""
    home = proposal_dir.parent.parent
    loader = SkillLoader(
        extra_dirs=[proposal_dir.parent],
        snapshot_path=home / "cache" / "auto_enable_snapshot.json",
    )
    loader.invalidate_cache()
    for spec in loader.load_all():
        if spec.path == proposal_dir:
            return spec
    raise ValueError(f"proposal {proposal_dir.name} did not parse as a skill")


def _normalise_max_risk(value: str) -> str:
    value = str(value or "low").strip().lower()
    return value if value in _RISK_ORDER else "low"


def _normalise_manifest_risk(value: str) -> str:
    value = str(value or "").strip().lower()
    return value if value in _RISK_ORDER else ""


def _iter_template_strings(value: Any, prefix: str) -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(prefix, value)]
    if isinstance(value, dict):
        found: list[tuple[str, str]] = []
        for key, item in value.items():
            found.extend(_iter_template_strings(item, f"{prefix}.{key}"))
        return found
    if isinstance(value, list):
        found = []
        for index, item in enumerate(value):
            found.extend(_iter_template_strings(item, f"{prefix}[{index}]"))
        return found
    return []


def _unsafe_output_template_reasons(plan) -> list[str]:
    """Return reasons for output templates that pass unbounded step output.

    This is a conservative auto-enable-only safety gate. Normal manually
    accepted meta-skills remain compatible, but unattended promotion requires
    each ``outputs.*`` interpolation to apply at least one bounding/escaping
    filter in the same Jinja expression.
    """
    reasons: list[str] = []
    for step in plan.steps:
        fields: list[tuple[str, str]] = []
        fields.extend(_iter_template_strings(step.with_args, f"{step.id}.with"))
        fields.extend(_iter_template_strings(step.tool_args, f"{step.id}.tool_args"))
        for path, text in fields:
            for match in _JINJA_EXPR_RE.finditer(text):
                expr = match.group(1)
                if not _OUTPUT_REF_RE.search(expr):
                    continue
                filters = {
                    part.split("(", 1)[0].strip()
                    for part in expr.split("|")[1:]
                    if part.strip()
                }
                if filters.isdisjoint(_SAFE_OUTPUT_TEMPLATE_FILTERS):
                    reasons.append(f"unbounded_output_template:{path}")
    return reasons


def _unsafe_user_input_template_reasons(plan) -> list[str]:
    """Return reasons for user_message templates without first-hop escaping."""
    reasons: list[str] = []
    for step in plan.steps:
        fields: list[tuple[str, str]] = []
        fields.extend(_iter_template_strings(step.with_args, f"{step.id}.with"))
        fields.extend(_iter_template_strings(step.tool_args, f"{step.id}.tool_args"))
        for path, text in fields:
            for match in _JINJA_EXPR_RE.finditer(text):
                expr = match.group(1)
                if not _USER_INPUT_REF_RE.search(expr):
                    continue
                filters = [
                    part.split("(", 1)[0].strip()
                    for part in expr.split("|")[1:]
                    if part.strip()
                ]
                if not filters or filters[0] not in {"xml_escape", "slugify"}:
                    reasons.append(f"unsafe_user_input_template:{path}")
    return reasons


def _evaluate_auto_enable_risk(
    proposal_dir: Path,
    *,
    skill_loader: SkillLoader,
    max_risk: str,
) -> dict[str, object]:
    """Return a deterministic risk decision for an eligible proposal.

    This is deliberately conservative: malformed meta plans, direct tool calls,
    unknown composed skills, and high-risk skills all prevent unattended accept.
    """
    try:
        spec = _load_proposal_spec(proposal_dir)
        plan = parse_meta_plan(spec)
    except Exception as exc:  # noqa: BLE001
        return {
            "allowed": False,
            "reason": "invalid_meta_plan",
            "risk_level": "high",
            "details": str(exc),
        }
    if plan is None:
        return {
            "allowed": False,
            "reason": "not_meta_skill",
            "risk_level": "high",
        }

    max_risk = _normalise_max_risk(max_risk)
    referenced_skills: set[str] = set()
    referenced_tools: set[str] = set()
    risk_level = "low"
    reasons: list[str] = []
    validation_profile = "static-safety-v2"

    def raise_risk(level: str, reason: str) -> None:
        nonlocal risk_level
        if _RISK_ORDER[level] > _RISK_ORDER[risk_level]:
            risk_level = level
        if reason not in reasons:
            reasons.append(reason)

    for step in plan.steps:
        if step.kind in ("agent", "skill_exec"):
            referenced_skills.add(step.skill)
        for route in step.route:
            referenced_skills.add(route.to)
        if step.kind == "skill_exec":
            spec = skill_loader.get_by_name(step.skill)
            if spec is not None and not getattr(spec, "entrypoint", None):
                raise_risk("high", f"skill_exec_without_entrypoint:{step.skill}")
        if step.kind == "tool_call":
            referenced_tools.add(step.tool)
            raise_risk("high", "direct_tool_call")
            if not step.tool_allowlist:
                raise_risk("high", f"tool_call_without_allowlist:{step.tool}")

    for reason in _unsafe_output_template_reasons(plan):
        raise_risk("high", reason)
    for reason in _unsafe_user_input_template_reasons(plan):
        raise_risk("high", reason)

    for skill in sorted(referenced_skills):
        spec = skill_loader.get_by_name(skill)
        if spec is None:
            raise_risk("high", f"unknown_skill:{skill}")
        elif getattr(spec, "kind", "skill") == "meta":
            raise_risk("high", f"nested_meta_skill:{skill}")
        else:
            metadata = getattr(spec, "metadata", None)
            manifest_risk = _normalise_manifest_risk(
                getattr(metadata, "risk_level", "") if metadata else ""
            )
            capabilities = {
                str(cap).strip().lower()
                for cap in (getattr(metadata, "capabilities", []) if metadata else [])
                if str(cap).strip()
            }
            has_manifest_risk = bool(manifest_risk or capabilities)
            if manifest_risk:
                raise_risk(manifest_risk, f"manifest_risk:{skill}:{manifest_risk}")
            for capability in sorted(capabilities):
                capability_risk = _CAPABILITY_RISK.get(capability, "medium")
                raise_risk(capability_risk, f"capability:{skill}:{capability}")
            if not has_manifest_risk:
                if skill in _HIGH_RISK_SKILLS:
                    raise_risk("high", f"legacy_high_risk_skill:{skill}")
                elif skill in _MEDIUM_RISK_SKILLS:
                    raise_risk("medium", f"legacy_medium_risk_skill:{skill}")
                elif skill in _LOW_RISK_SKILLS:
                    raise_risk("low", f"legacy_low_risk_skill:{skill}")
                else:
                    raise_risk("high", f"missing_risk_metadata:{skill}")

    allowed = _RISK_ORDER[risk_level] <= _RISK_ORDER[max_risk]
    return {
        "allowed": allowed,
        "reason": "ok" if allowed else "risk_too_high",
        "risk_level": risk_level,
        "max_risk": max_risk,
        "skills": sorted(referenced_skills),
        "tools": sorted(referenced_tools),
        "reasons": reasons,
        "validation_profile": validation_profile,
    }


def _try_auto_enable_proposal(
    *,
    proposals_dir: Path,
    proposal_id: str,
    skill_loader: SkillLoader,
    triggered_by: str,
    max_risk: str,
) -> dict[str, object]:
    """Attempt to promote one generated proposal into the managed layer."""
    home = proposals_dir.parent
    proposal_dir = proposals_dir / proposal_id
    gates_path = proposal_dir / "gates.json"
    gates: dict[str, object] = {}
    if gates_path.is_file():
        try:
            parsed = json.loads(gates_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                gates = dict(parsed)
        except (json.JSONDecodeError, OSError):
            gates = {}
    decision: dict[str, object]
    if not bool(gates.get("auto_enable_eligible", False)):
        decision = {
            "status": "skipped",
            "proposal_id": proposal_id,
            "reason": "gates_not_eligible",
            "triggered_by": triggered_by,
        }
        _patch_gates_auto_enable(proposal_dir, decision)
        return decision

    risk = _evaluate_auto_enable_risk(
        proposal_dir,
        skill_loader=skill_loader,
        max_risk=max_risk,
    )
    if not bool(risk.get("allowed", False)):
        decision = {
            "status": "skipped",
            "proposal_id": proposal_id,
            "reason": risk.get("reason", "risk_too_high"),
            "risk_level": risk.get("risk_level", "high"),
            "max_risk": risk.get("max_risk", _normalise_max_risk(max_risk)),
            "triggered_by": triggered_by,
            "details": risk,
        }
        _patch_gates_auto_enable(proposal_dir, decision)
        return decision

    decision = {
        "status": "enabled",
        "proposal_id": proposal_id,
        "risk_level": risk.get("risk_level", "low"),
        "max_risk": risk.get("max_risk", _normalise_max_risk(max_risk)),
        "triggered_by": triggered_by,
        "enabled_at_ms": int(time.time() * 1000),
        "details": risk,
    }
    _patch_gates_auto_enable(proposal_dir, decision)
    accepted = accept_proposal(home, proposal_id, force=False)
    if accepted.get("status") != "ok":
        failed: dict[str, object] = {
            "status": "error",
            "proposal_id": proposal_id,
            "reason": str(accepted.get("reason") or "accept_failed"),
            "triggered_by": triggered_by,
            "accept_result": accepted,
        }
        _patch_gates_auto_enable(proposal_dir, failed)
        return failed
    skill_loader.invalidate_cache()
    skill_loader.load_all()
    decision["skill_name"] = accepted.get("name")
    decision["skill_path"] = accepted.get("skill_path")
    return decision


def try_auto_enable_proposal(
    *,
    proposals_dir: Path,
    proposal_id: str,
    skill_loader: SkillLoader,
    triggered_by: str,
    max_risk: str,
) -> dict[str, object]:
    """Public wrapper used by cron/dream and manual creator persist paths.

    The operator kill switch is honoured at this boundary so that
    ``OPENSQUILLA_AUTO_PROPOSE_DISABLED=1`` halts auto-enable from
    every call site — cron, dream, and manual creator persist —
    without each caller having to remember to gate itself.
    """
    if is_auto_propose_disabled():
        _log.info(
            "auto_propose.kill_switch",
            extra={
                "proposal_id": proposal_id,
                "triggered_by": triggered_by,
                "path": "try_auto_enable_proposal",
            },
        )
        return {
            "decision": "refused",
            "reason": "kill_switch_disabled",
            "kill_switch": True,
        }
    return _try_auto_enable_proposal(
        proposals_dir=proposals_dir,
        proposal_id=proposal_id,
        skill_loader=skill_loader,
        triggered_by=triggered_by,
        max_risk=max_risk,
    )


# Centralised operator kill switch. Setting this env var to ``"1"``
# halts every auto-propose entry point (cron, dream, manual creator
# persist) so operators get a single point of control without each
# call site having to remember to inline the check.
_AUTO_PROPOSE_KILL_SWITCH_ENV = "OPENSQUILLA_AUTO_PROPOSE_DISABLED"


def is_auto_propose_disabled() -> bool:
    """Return True when the operator-controlled kill switch is set.

    Both the synthesis pipeline (``auto_propose``) and the auto-enable
    wrapper (``try_auto_enable_proposal``) consult this helper at
    their entry points. Callers (cron handler, dream callback) may
    pre-check it to skip building expensive context objects.
    """
    return os.getenv(_AUTO_PROPOSE_KILL_SWITCH_ENV) == "1"


def _resolve_proposals_dir(proposals_dir: Path | None) -> Path:
    if proposals_dir is not None:
        return proposals_dir
    env_home = os.environ.get("OPENSQUILLA_STATE_DIR")
    home = Path(env_home).expanduser() if env_home else Path.home() / ".opensquilla"
    return home / _DEFAULT_PROPOSALS_DIRNAME


async def auto_propose(
    *,
    orchestrator: MetaOrchestrator,
    skill_loader: SkillLoader,
    log_dir: Path,
    window_days: int = 30,
    min_freq: int = 3,
    top_k: int = 5,
    triggered_by: str = "cron",
    proposals_dir: Path | None = None,
    auto_enable: bool = False,
    auto_enable_max_risk: str = "low",
    source_context: str = "",
) -> AutoProposeResult:
    """Drive meta-skill-creator once per qualifying co-occurrence pattern.

    Args:
        orchestrator: pre-wired MetaOrchestrator. Fresh-per-fire; not
            reused across calls (orchestrator carries per-run state).
        skill_loader: the gateway's shared SkillLoader. Used to look up
            the meta-skill-creator plan + existing meta-skill coverage.
        log_dir: directory containing ``decisions-*.jsonl``. Usually
            ``~/.opensquilla/logs``.
        window_days: rolling window for co-occurrence aggregation.
        min_freq: drop patterns observed fewer than this many times.
        top_k: at most this many patterns are considered per call.
        triggered_by: ``"cron"`` or ``"dream"``. Recorded in provenance.
        proposals_dir: where the meta-skill-creator persist step writes
            proposals. Defaults to ``$OPENSQUILLA_STATE_DIR/proposals``
            or ``~/.opensquilla/proposals``.

    Returns:
        AutoProposeResult capturing proposals_created / skipped /
        errors. NEVER raises — every exception is collected.
    """
    # Honour the operator kill switch at the source so that every
    # entry point — cron, dream callback, manual creator
    # auto-enable, future call sites — observes the same disabled
    # state. Pre-checks in the cron handler stay as fast paths but
    # this guard is the load-bearing one.
    if is_auto_propose_disabled():
        _log.info(
            "auto_propose.kill_switch",
            extra={"triggered_by": triggered_by, "path": "auto_propose"},
        )
        return AutoProposeResult(
            skipped=[{"reason": "kill_switch_disabled"}],
            triggered_by=triggered_by,
        )
    proposals_dir = _resolve_proposals_dir(proposals_dir)
    proposals_created: list[str] = []
    proposals_enabled: list[str] = []
    auto_enable_decisions: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []

    creator_spec = skill_loader.get_by_name(_META_SKILL_CREATOR)
    if creator_spec is None:
        errors.append({
            "reason": "meta-skill-creator spec missing from loader",
        })
        return AutoProposeResult(
            proposals_created=proposals_created,
            proposals_enabled=proposals_enabled,
            auto_enable=auto_enable_decisions,
            skipped=skipped,
            errors=errors,
            triggered_by=triggered_by,
        )
    try:
        creator_plan = parse_meta_plan(creator_spec)
    except Exception as exc:  # noqa: BLE001 — fault-tolerant
        errors.append({
            "reason": f"meta-skill-creator plan parse failed: {exc}",
        })
        return AutoProposeResult(
            proposals_created=proposals_created,
            proposals_enabled=proposals_enabled,
            auto_enable=auto_enable_decisions,
            skipped=skipped,
            errors=errors,
            triggered_by=triggered_by,
        )
    if creator_plan is None:
        errors.append({"reason": "meta-skill-creator spec is not kind=meta"})
        return AutoProposeResult(
            proposals_created=proposals_created,
            proposals_enabled=proposals_enabled,
            auto_enable=auto_enable_decisions,
            skipped=skipped,
            errors=errors,
            triggered_by=triggered_by,
        )

    try:
        patterns = aggregate_co_occurrences(log_dir, window_days, top_k)
    except Exception as exc:  # noqa: BLE001
        errors.append({"reason": f"aggregate_co_occurrences failed: {exc}"})
        return AutoProposeResult(
            proposals_created=proposals_created,
            proposals_enabled=proposals_enabled,
            auto_enable=auto_enable_decisions,
            skipped=skipped,
            errors=errors,
            triggered_by=triggered_by,
        )

    coverage = _meta_skill_coverage(skill_loader)
    meta_names = _meta_skill_names(skill_loader)
    available_names = _available_skill_names(skill_loader)
    existing_hashes = _existing_chain_hashes(proposals_dir)

    for pattern in patterns:
        raw_skills = list(pattern.get("skills") or [])
        freq = int(pattern.get("freq") or 0)
        sample_intents = pattern.get("sample_intents") or []
        intent_digest = " | ".join(
            str(item).strip() for item in sample_intents if str(item).strip()
        )[:800]
        if not raw_skills:
            continue
        if freq < min_freq:
            skipped.append(asdict(_SkippedPattern(
                skills=raw_skills, freq=freq, reason="below_min_freq",
            )))
            continue
        # Strip meta-skill members from the chain — they cannot be
        # composed into another meta-skill (runtime forbids nesting),
        # so leaving them in the seed shown to the LLM only invites
        # invalid proposals that G1.2 lint will reject.
        skills = [s for s in raw_skills if s not in meta_names]
        if available_names:
            missing = [s for s in skills if s not in available_names]
            if missing:
                skipped.append(asdict(_SkippedPattern(
                    skills=raw_skills, freq=freq, reason="unknown_skill",
                )))
                continue
        if len(skills) < 2:
            skipped.append(asdict(_SkippedPattern(
                skills=raw_skills, freq=freq, reason="only_meta_after_filter",
            )))
            continue
        if _pattern_already_covered(skills, coverage):
            skipped.append(asdict(_SkippedPattern(
                skills=skills, freq=freq, reason="already_covered",
            )))
            continue
        chain_hash = _chain_hash(skills)
        proposal_signature = _chain_signature(skills, intent_digest)
        if chain_hash in existing_hashes or proposal_signature in existing_hashes:
            skipped.append(asdict(_SkippedPattern(
                skills=skills, freq=freq, reason="duplicate_pending",
            )))
            continue

        msg = _synthesise_user_message(
            skills, freq, window_days, intent_digest=intent_digest,
        )
        system_prompt = (
            "Unattended meta-skill auto-propose run. Synthesize a "
            "low-risk reusable meta-skill from observed skill co-occurrence "
            "evidence and preserve all creator gates."
        )
        if source_context:
            system_prompt += f"\n\nScheduler source context:\n{source_context[:1200]}"
        if intent_digest:
            system_prompt += f"\n\nObserved user-intent digest:\n{intent_digest}"
        match = MetaMatch(
            plan=creator_plan,
            inputs={
                "user_message": msg,
                "system_prompt": system_prompt,
            },
        )
        before = {p.name for p in proposals_dir.iterdir()} if proposals_dir.is_dir() else set()
        try:
            await orchestrator.run(match)
        except asyncio.CancelledError:
            # propagate cancellation — don't bury it
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(asdict(_PatternError(
                skills=skills, freq=freq, error=str(exc),
            )))
            continue

        after = {p.name for p in proposals_dir.iterdir()} if proposals_dir.is_dir() else set()
        new_ids = sorted(after - before)
        if not new_ids:
            # DAG completed but no proposal landed — usually means
            # lint/smoke gates failed; that is normal and not an error.
            skipped.append(asdict(_SkippedPattern(
                skills=skills, freq=freq, reason="dag_produced_no_proposal",
            )))
            continue
        for proposal_id in new_ids:
            proposals_created.append(proposal_id)
            existing_hashes.add(chain_hash)
            existing_hashes.add(proposal_signature)
            _patch_gates_provenance(
                proposals_dir / proposal_id,
                triggered_by=triggered_by,
                skills=skills,
                freq=freq,
                window_days=window_days,
                chain_hash=chain_hash,
                proposal_signature=proposal_signature,
                intent_digest=intent_digest,
                source_context=source_context,
            )
            if auto_enable:
                try:
                    decision = _try_auto_enable_proposal(
                        proposals_dir=proposals_dir,
                        proposal_id=proposal_id,
                        skill_loader=skill_loader,
                        triggered_by=triggered_by,
                        max_risk=auto_enable_max_risk,
                    )
                except Exception as exc:  # noqa: BLE001
                    decision = {
                        "status": "error",
                        "proposal_id": proposal_id,
                        "reason": str(exc),
                        "triggered_by": triggered_by,
                    }
                auto_enable_decisions.append(decision)
                if decision.get("status") == "enabled":
                    proposals_enabled.append(proposal_id)

    return AutoProposeResult(
        proposals_created=proposals_created,
        proposals_enabled=proposals_enabled,
        auto_enable=auto_enable_decisions,
        skipped=skipped,
        errors=errors,
        triggered_by=triggered_by,
    )


__all__ = [
    "auto_propose",
    "AutoProposeResult",
    "try_auto_enable_proposal",
    "_chain_signature",
]
