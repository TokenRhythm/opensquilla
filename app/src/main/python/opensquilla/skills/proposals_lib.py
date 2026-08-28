"""Library functions for the ``~/.opensquilla/proposals/`` directory.

Lifted out of ``skills/bundled/skill-creator-proposals/scripts/proposals.py``
so the gateway RPC layer (Path 3) can call them in-process — the
bundled script's hyphenated path is not importable.

The bundled script now delegates here so there's one source of truth.

Path layout::

    ~/.opensquilla/proposals/<8-hex>/SKILL.md
    ~/.opensquilla/proposals/<8-hex>/gates.json
    ~/.opensquilla/skills/<name>/                # MANAGED layer after accept
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path

PROPOSAL_ID_PATTERN = re.compile(r"[0-9a-f]{8}")
SKILL_NAME_PATTERN = re.compile(r"[\w\-]+")
RISK_LEVELS = frozenset({"low", "medium", "high"})
_NO_REQUIRED_IMPROVEMENTS = frozenset({"", "none", "no", "n/a", "not applicable"})


def proposals_dir(home: Path) -> Path:
    return home / "proposals"


def skills_dir(home: Path) -> Path:
    return home / "skills"


def is_valid_proposal_id(proposal_id: str | None) -> bool:
    if not proposal_id:
        return False
    return bool(PROPOSAL_ID_PATTERN.fullmatch(proposal_id))


def atomic_write_proposal(
    home: Path, skill_md: str, gates: dict,
) -> str:
    """Materialise a proposal directory atomically.

    Writes ``SKILL.md`` + ``gates.json`` under ``$home/.tmp/proposal-<id>``
    then renames into ``$home/proposals/<id>`` — readers never see a
    half-built dir. Returns the new 8-hex proposal_id.
    """
    proposals = proposals_dir(home)
    proposals.mkdir(parents=True, exist_ok=True)
    proposal_id = uuid.uuid4().hex[:8]

    tmp_parent = home / ".tmp"
    tmp_parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = tmp_parent / f"proposal-{proposal_id}"
    tmp_dir.mkdir()
    (tmp_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    (tmp_dir / "gates.json").write_text(
        json.dumps(gates, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    final_dir = proposals / proposal_id
    tmp_dir.rename(final_dir)
    return proposal_id


def _normalise_acceptance_result(acceptance_result: object) -> dict:
    if acceptance_result is None:
        return {}
    if isinstance(acceptance_result, dict):
        return dict(acceptance_result)
    if isinstance(acceptance_result, str):
        text = acceptance_result.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}
        if isinstance(parsed, dict):
            return parsed
        return {"raw": text}
    return {"raw": str(acceptance_result)}


def _first_section_item(raw: str, section: str) -> str:
    pattern = re.compile(
        rf"^{re.escape(section)}:\s*(.*?)(?=^[A-Z][A-Z _-]*:|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(raw)
    if not match:
        return ""
    body = match.group(1).strip()
    if not body:
        return ""
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        return ""
    first = lines[0]
    return first[1:].strip() if first.startswith("-") else first


def _evaluate_acceptance_compare(
    creator_mode: str,
    acceptance_result: object,
) -> dict:
    mode = (creator_mode or "").strip().upper()
    required = mode == "FULL_GATED"
    payload = _normalise_acceptance_result(acceptance_result)
    raw = str(payload.get("raw") or "").strip()
    winner = str(payload.get("winner") or "").strip().lower()
    quality_score_raw = payload.get("quality_score")
    required_improvements = str(
        payload.get("required_improvements") or payload.get("required_improvement") or ""
    ).strip()

    if raw:
        if not winner:
            match = re.search(r"^WINNER:\s*([^\n]+)", raw, re.MULTILINE | re.IGNORECASE)
            if match:
                winner = match.group(1).strip().lower()
        if not required_improvements:
            required_improvements = _first_section_item(raw, "REQUIRED_IMPROVEMENTS")
        if quality_score_raw is None:
            score_match = re.search(
                r"^QUALITY_SCORE:\s*([0-9]+(?:\.[0-9]+)?)",
                raw,
                re.MULTILINE | re.IGNORECASE,
            )
            if score_match:
                quality_score_raw = score_match.group(1)

    required_improvements_norm = required_improvements.strip().lower()
    has_required_improvements = required_improvements_norm not in _NO_REQUIRED_IMPROVEMENTS
    quality_score: float | None = None
    if quality_score_raw not in (None, ""):
        try:
            quality_score = float(str(quality_score_raw))
        except (TypeError, ValueError):
            quality_score = None
    quality_passed = quality_score is None or quality_score >= 0.80
    passed = (
        not required
        or (
            winner in {"orchestrated", "tie"}
            and not has_required_improvements
            and quality_passed
        )
    )
    diagnostics: list[str] = []
    if required and not winner:
        diagnostics.append("missing WINNER in acceptance comparison")
    if required and winner not in {"orchestrated", "tie"}:
        diagnostics.append(f"winner is not orchestrated/tie: {winner or 'missing'}")
    if required and has_required_improvements:
        diagnostics.append("required improvements are present")
    if required and not quality_passed:
        diagnostics.append("quality score below 0.80")

    return {
        "required": required,
        "passed": passed,
        "winner": winner,
        "quality_score": quality_score,
        "required_improvements": required_improvements,
        "diagnostics": diagnostics,
        "raw": raw,
    }


def _evaluate_collision_check(creator_mode: str, collision_result: object) -> dict:
    mode = (creator_mode or "").strip().upper()
    required = mode in {"FULL_GATED", "PERSISTED_PROPOSAL"}
    raw = str(collision_result or "").strip()
    lowered = raw.lower()
    failed = "revise_needed" in lowered or "fail" in lowered
    return {
        "required": required,
        "passed": (not required) or (bool(raw) and not failed),
        "reason": "ok" if ((not required) or (bool(raw) and not failed)) else (
            "collision_check_failed" if raw else "missing_collision_check"
        ),
        "raw": raw,
    }


def _evaluate_risk_classify(creator_mode: str, risk_result: object) -> dict:
    mode = (creator_mode or "").strip().upper()
    required = mode in {"FULL_GATED", "PERSISTED_PROPOSAL"}
    raw = str(risk_result or "").strip()
    match = re.search(r"^RISK:\s*(low|medium|high)\b", raw, re.MULTILINE | re.IGNORECASE)
    risk_level = match.group(1).lower() if match else ""
    passed = (not required) or (bool(raw) and risk_level in {"low", "medium"})
    return {
        "required": required,
        "passed": passed,
        "risk_level": risk_level,
        "reason": "ok" if passed else (
            "risk_too_high" if risk_level == "high" else "missing_risk_classification"
        ),
        "raw": raw,
    }


def _normalise_runtime_e2e_result(runtime_e2e_result: object) -> dict:
    if runtime_e2e_result is None:
        return {}
    if isinstance(runtime_e2e_result, dict):
        return dict(runtime_e2e_result)
    if isinstance(runtime_e2e_result, str):
        text = runtime_e2e_result.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}
        if isinstance(parsed, dict):
            return parsed
        return {"raw": text}
    return {"raw": str(runtime_e2e_result)}


def _evaluate_runtime_e2e(
    creator_mode: str,
    runtime_e2e_result: object,
) -> dict:
    mode = (creator_mode or "").strip().upper()
    required = mode == "FULL_GATED"
    payload = _normalise_runtime_e2e_result(runtime_e2e_result)
    if not payload:
        return {
            "required": required,
            "passed": not required,
            "reason": "missing_runtime_e2e_result" if required else "not_required",
            "winner": "",
            "cases": [],
        }
    winner = str(payload.get("winner") or "").strip().lower()
    cases = payload.get("cases")
    if not isinstance(cases, list):
        cases = []
    case_blockers: list[str] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            continue
        case_winner = str(case.get("winner") or "").strip().lower()
        regression = str(case.get("regression") or "").strip()
        if case_winner not in {"meta", "tie"}:
            case_blockers.append(f"case_{index}_winner:{case_winner or 'missing'}")
        if regression:
            case_blockers.append(f"case_{index}_regression")
    passed = (
        (not required)
        or (
            bool(payload.get("passed", False))
            and winner in {"meta", "tie"}
            and not case_blockers
        )
    )
    return {
        "required": required,
        "passed": passed,
        "reason": "ok" if passed else str(payload.get("reason") or "runtime_e2e_failed"),
        "winner": winner,
        "baseline_model": payload.get("baseline_model", ""),
        "cases": cases,
        "diagnostics": case_blockers,
        "raw": payload.get("raw", ""),
    }


def write_proposal(
    home: Path,
    skill_md: str,
    lint_result: dict,
    smoke_result: dict,
    *,
    creator_mode: str = "",
    acceptance_result: object = None,
    runtime_e2e_result: object = None,
    collision_result: object = None,
    risk_result: object = None,
) -> dict:
    """Atomic write + return the standard ``{status, proposal_id, ...}`` shape."""
    acceptance_gate = _evaluate_acceptance_compare(creator_mode, acceptance_result)
    runtime_gate = _evaluate_runtime_e2e(creator_mode, runtime_e2e_result)
    collision_gate = _evaluate_collision_check(creator_mode, collision_result)
    risk_gate = _evaluate_risk_classify(creator_mode, risk_result)
    # D1: ``degraded`` smoke (no fixture LLM available → deterministic
    # stub fixtures) flags G3/G4 as ``passed: True`` even though no
    # cross-vendor verification actually happened. Treating it as
    # eligible would let an unattended creator pipeline auto-enable a
    # candidate that has never been validated against a real model.
    # Refuse eligibility whenever the smoke result is degraded;
    # ``acceptance/runtime_e2e`` may still proceed so the proposal
    # lands for human review.
    smoke_degraded = bool(smoke_result.get("degraded", False))
    gate_eligible = (
        lint_result.get("G1", {}).get("passed", False)
        and lint_result.get("G2", {}).get("passed", False)
        and smoke_result.get("G3", {}).get("passed", False)
        and smoke_result.get("G4", {}).get("passed", False)
        and not smoke_degraded
    )
    eligible = (
        gate_eligible
        and bool(collision_gate.get("passed", False))
        and bool(risk_gate.get("passed", False))
        and bool(acceptance_gate.get("passed", False))
        and bool(runtime_gate.get("passed", False))
    )
    gates = {
        "lint": lint_result,
        "smoke": smoke_result,
        "collision_check": collision_gate,
        "risk_classify": risk_gate,
        "acceptance_compare": acceptance_gate,
        "runtime_e2e": runtime_gate,
        "auto_enable_eligible": eligible,
    }
    proposal_id = atomic_write_proposal(home, skill_md, gates)
    return {
        "status": "ok",
        "proposal_id": proposal_id,
        "auto_enable_eligible": eligible,
    }


def auto_enable_audit_from_gates(gates: dict) -> dict[str, object]:
    """Return a compact, UI-ready auto-enable audit summary."""
    auto_enable = gates.get("auto_enable")
    if not isinstance(auto_enable, dict):
        return {}
    details = auto_enable.get("details")
    if not isinstance(details, dict):
        details = {}
    reason = auto_enable.get("reason") or details.get("reason") or ""
    skills = details.get("skills")
    tools = details.get("tools")
    reasons = details.get("reasons")
    return {
        "status": auto_enable.get("status", "unknown"),
        "reason": reason,
        "risk_level": auto_enable.get("risk_level", details.get("risk_level", "unknown")),
        "max_risk": auto_enable.get("max_risk", details.get("max_risk", "unknown")),
        "validation_profile": details.get("validation_profile", "unknown"),
        "skills": skills if isinstance(skills, list) else [],
        "tools": tools if isinstance(tools, list) else [],
        "reasons": reasons if isinstance(reasons, list) else [],
    }


def list_proposals(home: Path) -> dict:
    """Snapshot of pending proposals (id + eligibility + provenance digest)."""
    proposals = proposals_dir(home)
    if not proposals.is_dir():
        return {"proposals": []}
    rows: list[dict] = []
    for sub in sorted(proposals.iterdir()):
        if not (sub / "SKILL.md").is_file():
            continue
        gates_path = sub / "gates.json"
        gates: dict = {}
        if gates_path.is_file():
            try:
                gates = json.loads(gates_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                gates = {}
        provenance = gates.get("provenance") or {}
        auto_enable_digest = auto_enable_audit_from_gates(gates)
        rows.append({
            "proposal_id": sub.name,
            "auto_enable_eligible": bool(gates.get("auto_enable_eligible", False)),
            "triggered_by": provenance.get("triggered_by", "manual"),
            "chain_hash": provenance.get("chain_hash"),
            "auto_enable": auto_enable_digest,
        })
    return {"proposals": rows}


def pending_count(home: Path) -> dict:
    """Number of pending proposals — cheap badge backend for the WebUI."""
    proposals = proposals_dir(home)
    if not proposals.is_dir():
        return {"count": 0}
    count = 0
    for sub in proposals.iterdir():
        if sub.is_dir() and (sub / "SKILL.md").is_file():
            count += 1
    return {"count": count}


def show_proposal(home: Path, proposal_id: str) -> dict:
    """Full payload for one proposal: SKILL.md text + gates.json."""
    if not is_valid_proposal_id(proposal_id):
        return {"status": "error", "reason": "invalid proposal_id format"}
    sub = proposals_dir(home) / proposal_id
    skill_path = sub / "SKILL.md"
    gates_path = sub / "gates.json"
    if not skill_path.is_file():
        return {"status": "error", "reason": f"proposal {proposal_id} not found"}
    skill_md = skill_path.read_text(encoding="utf-8")
    gates: dict = {}
    if gates_path.is_file():
        try:
            gates = json.loads(gates_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            gates = {}
    return {
        "status": "ok",
        "proposal_id": proposal_id,
        "skill_md": skill_md,
        "gates": gates,
        "auto_enable_audit": auto_enable_audit_from_gates(gates),
    }


def accept_proposal(home: Path, proposal_id: str, force: bool = False) -> dict:
    """Promote a proposal to the MANAGED skills layer."""
    if not is_valid_proposal_id(proposal_id):
        return {
            "status": "error",
            "reason": (
                f"invalid proposal_id format (expected 8 hex chars): {proposal_id!r}"
            ),
        }
    src = proposals_dir(home) / proposal_id
    if not (src / "SKILL.md").is_file():
        return {"status": "error", "reason": f"proposal {proposal_id} not found"}
    gates: dict = {}
    if (src / "gates.json").is_file():
        try:
            gates = json.loads((src / "gates.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            gates = {}
    if not gates.get("auto_enable_eligible") and not force:
        return {
            "status": "refused",
            "reason": "gates not all passed; use --force to override",
            "gates": gates,
        }
    skill_md = (src / "SKILL.md").read_text(encoding="utf-8")
    # Accept both `name: foo` and `name: "foo"` (creator's tojson emits quoted).
    name_match = re.search(r'^name:\s*"?([\w\-]+)"?\s*$', skill_md, re.MULTILINE)
    if not name_match:
        return {"status": "error", "reason": "cannot parse skill name from SKILL.md"}
    name = name_match.group(1)

    dst = skills_dir(home) / name
    if dst.exists():
        return {"status": "refused", "reason": f"skill {name} already exists at {dst}"}

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return {"status": "ok", "skill_path": str(dst), "name": name}


def list_auto_enabled_skills(home: Path) -> dict:
    """Return managed skills that were promoted by auto-enable."""
    managed = skills_dir(home)
    if not managed.is_dir():
        return {"skills": []}
    rows: list[dict] = []
    for sub in sorted(managed.iterdir()):
        if not (sub / "SKILL.md").is_file():
            continue
        gates_path = sub / "gates.json"
        if not gates_path.is_file():
            continue
        try:
            gates = json.loads(gates_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        auto_enable = gates.get("auto_enable")
        if not isinstance(auto_enable, dict):
            continue
        if auto_enable.get("status") != "enabled":
            continue
        audit = auto_enable_audit_from_gates(gates)
        rows.append({
            "name": sub.name,
            "proposal_id": auto_enable.get("proposal_id"),
            "risk_level": auto_enable.get("risk_level", "unknown"),
            "max_risk": auto_enable.get("max_risk", "unknown"),
            "triggered_by": auto_enable.get("triggered_by", "unknown"),
            "enabled_at_ms": auto_enable.get("enabled_at_ms"),
            "validation_profile": audit.get("validation_profile", "unknown"),
            "skills": audit.get("skills", []),
            "tools": audit.get("tools", []),
            "reasons": audit.get("reasons", []),
        })
    return {"skills": rows}


def disable_auto_enabled_skill(home: Path, name: str) -> dict:
    """Move an auto-enabled managed skill back to proposals for review."""
    if not isinstance(name, str) or not SKILL_NAME_PATTERN.fullmatch(name):
        return {"status": "error", "reason": "invalid skill name"}
    src = skills_dir(home) / name
    if not (src / "SKILL.md").is_file():
        return {"status": "error", "reason": f"skill {name} not found"}
    gates_path = src / "gates.json"
    gates: dict = {}
    if gates_path.is_file():
        try:
            parsed = json.loads(gates_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                gates = parsed
        except (json.JSONDecodeError, OSError):
            gates = {}
    auto_enable = gates.get("auto_enable")
    if not isinstance(auto_enable, dict) or auto_enable.get("status") != "enabled":
        return {"status": "refused", "reason": f"skill {name} is not auto-enabled"}

    proposal_id = str(auto_enable.get("proposal_id") or uuid.uuid4().hex[:8])
    if not is_valid_proposal_id(proposal_id) or (proposals_dir(home) / proposal_id).exists():
        proposal_id = uuid.uuid4().hex[:8]
    proposals_dir(home).mkdir(parents=True, exist_ok=True)
    dst = proposals_dir(home) / proposal_id

    disabled = dict(auto_enable)
    disabled["previous_status"] = auto_enable.get("status")
    disabled["status"] = "disabled"
    disabled["proposal_id"] = proposal_id
    gates["auto_enable"] = disabled
    gates_path.write_text(json.dumps(gates, indent=2, ensure_ascii=False), encoding="utf-8")
    shutil.move(str(src), str(dst))
    return {"status": "ok", "proposal_id": proposal_id, "name": name}


def reject_proposal(home: Path, proposal_id: str) -> dict:
    """Delete the proposal directory. Idempotent — re-deleting is fine."""
    if not is_valid_proposal_id(proposal_id):
        return {
            "status": "error",
            "reason": (
                f"invalid proposal_id format (expected 8 hex chars): {proposal_id!r}"
            ),
        }
    target = proposals_dir(home) / proposal_id
    if not target.is_dir():
        return {"status": "error", "reason": f"proposal {proposal_id} not found"}
    shutil.rmtree(target)
    return {"status": "ok", "proposal_id": proposal_id}


# ─── Auto-propose settings (Path 1/2 runtime toggle) ──────────────────

_AUTO_PROPOSE_BOOL_SETTINGS_KEYS = ("enabled", "on_dream_complete", "auto_enable")
_AUTO_PROPOSE_SETTINGS_KEYS = (*_AUTO_PROPOSE_BOOL_SETTINGS_KEYS, "auto_enable_max_risk")


def auto_propose_settings_path(home: Path) -> Path:
    """Path to the per-installation runtime overrides JSON."""
    return home / "state" / "auto_propose_settings.json"


def read_auto_propose_settings(home: Path) -> dict[str, object]:
    """Return the persisted runtime overrides, or {} when not present.

    The dict is keyed by ``enabled``, ``on_dream_complete``, and/or
    ``auto_enable``. Missing keys mean "no override" — the caller should fall
    back to the toml / pydantic-settings default.
    """
    path = auto_propose_settings_path(home)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, dict):
        return {}
    out: dict[str, object] = {
        k: bool(v) for k, v in payload.items()
        if k in _AUTO_PROPOSE_BOOL_SETTINGS_KEYS and isinstance(v, bool)
    }
    risk = payload.get("auto_enable_max_risk")
    if isinstance(risk, str) and risk in RISK_LEVELS:
        out["auto_enable_max_risk"] = risk
    return out


def write_auto_propose_settings(home: Path, settings: dict[str, object]) -> None:
    """Persist the runtime overrides atomically. Unknown keys are dropped."""
    path = auto_propose_settings_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    sanitised: dict[str, object] = {
        k: bool(settings.get(k))
        for k in _AUTO_PROPOSE_BOOL_SETTINGS_KEYS
        if k in settings
    }
    risk = settings.get("auto_enable_max_risk")
    if isinstance(risk, str) and risk in RISK_LEVELS:
        sanitised["auto_enable_max_risk"] = risk
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sanitised, indent=2), encoding="utf-8")
    tmp.replace(path)


__all__ = [
    "PROPOSAL_ID_PATTERN",
    "atomic_write_proposal",
    "accept_proposal",
    "auto_enable_audit_from_gates",
    "auto_propose_settings_path",
    "disable_auto_enabled_skill",
    "is_valid_proposal_id",
    "list_auto_enabled_skills",
    "list_proposals",
    "pending_count",
    "proposals_dir",
    "read_auto_propose_settings",
    "reject_proposal",
    "show_proposal",
    "skills_dir",
    "write_auto_propose_settings",
    "write_proposal",
]
