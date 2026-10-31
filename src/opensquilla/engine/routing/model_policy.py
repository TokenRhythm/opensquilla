"""Intent-led routing matching the frozen ``four-tier-intent-policy.v1``.

Classification owns the tier: continue retains the last committed tier, redo
advances one tier, and new_task uses the tier head. Controller rules only
derive thinking/prompt heads. Provider preflight and durable admission belong
to the caller, not this pure transition.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Any

import yaml

from opensquilla.squilla_router.controller import (
    compute_difficulty,
    compute_margin,
    derive_prompt_policy,
    normalize_decisions,
    select_localized_prompt_hint,
)
from opensquilla.squilla_router.v4_phase3 import default_bundle_dir

POLICY_VERSION = "four-tier-intent-policy.v1"
STATE_SCHEMA_VERSION = "model-tier-state.v1"
DECISION_SCHEMA_VERSION = "model-tier-decision.v1"
INTENTS = ("continue", "redo", "new_task")
TIERS = ("c0", "c1", "c2", "c3")
_NEW_CONTROLS = frozenset({"new", "new_task", "new-task", "create_task", "create-task"})
_REDO_CONTROLS = frozenset({"redo", "regenerate", "retry_response", "retry-response"})
_RULE_SECTIONS = ("flag_rules", "context_rules", "thinking_mode_rules", "prompt_policies")
_TRIVIAL_ACKS = frozenset(
    {"thanks", "thank you", "ok", "okay", "yes", "no", "收到", "好的", "谢谢", "是的", "不用了"}
)
_CODE_BLOCK_RE = re.compile(r"\x60{3}[\s\S]*?\x60{3}")
_LOG_BLOCK_RE = re.compile(
    r"(\d{4}[-/]\d{2}[-/]\d{2}[\sT]\d{2}:\d{2}.*\n){3,}"
    r"|(^\[?(INFO|WARN|ERROR|DEBUG)\]?\s.*\n){3,}",
    re.MULTILINE,
)
_FILE_PATH_RE = re.compile(r"""(?:^|[\s"'\x60(])([a-zA-Z_][\w.-]*/[\w./-]+\.[\w]+)""", re.MULTILINE)


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


@lru_cache(maxsize=1)
def _rules() -> dict[str, Any]:
    # These four sections and controller.py are identical to the frozen source;
    # the bundle's deployment registry and tier-changing thresholds are unused.
    data = yaml.safe_load(
        (default_bundle_dir() / "router.runtime.yaml").read_text(encoding="utf-8")
    )
    return {section: copy.deepcopy(data.get(section, {})) for section in _RULE_SECTIONS}


def _int(value: Any, default: int = 0) -> int:
    return (
        value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default
    )


def _tier(value: Any) -> str | None:
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in TIERS:
            return lowered
        if value in {"R0", "R1", "R2", "R3"}:
            return f"c{value[1]}"
    return None


def _flags(text: str, rules: Mapping[str, Any], context_tokens: int) -> dict[str, bool]:
    """Exact pure flag projection from frozen four_tier_squilla_control.py."""
    configured = rules.get("flag_rules", {})
    lowered = text.lower()

    def keywords(values: Any) -> bool:
        return any(str(value).lower() in lowered for value in values)

    risk = configured.get("high_risk", {})
    debug = configured.get("debug", {})
    long = configured.get("long_context", {})
    return {
        "high_risk": keywords(risk.get("keywords_zh", [])) or keywords(risk.get("keywords_en", [])),
        "debug": keywords(debug.get("keywords", []))
        or any(re.search(pattern, text) for pattern in debug.get("patterns", [])),
        "repo_arch": keywords(configured.get("repo_arch", {}).get("keywords", [])),
        "strict_format": keywords(configured.get("strict_format", {}).get("keywords", [])),
        "long_context": (
            len(text) >= int(long.get("char_threshold", 6_000))
            or sum(len(match.group()) for match in _CODE_BLOCK_RE.finditer(text))
            >= int(long.get("code_block_threshold", 1_500))
            or sum(len(match.group()) for match in _LOG_BLOCK_RE.finditer(text))
            >= int(long.get("log_block_threshold", 1_500))
            or len(_FILE_PATH_RE.findall(text)) >= int(long.get("file_ref_threshold", 2))
            or context_tokens
            > int(rules.get("context_rules", {}).get("heavy_context_tokens", 2_000))
        ),
    }


def _controller(
    tier: str,
    vector: list[float] | None,
    flags: Mapping[str, bool],
    rules: Mapping[str, Any],
    text: str,
) -> tuple[str, str]:
    margin = compute_margin(vector) if vector is not None else 0.0
    if tier == "c3":
        thinking = "T3"
    else:
        thinking_rules = rules.get("thinking_mode_rules", {})
        deep = thinking_rules.get("T3", {})
        minimum = _tier(deep.get("min_class", "R2")) or "c2"
        if TIERS.index(tier) >= TIERS.index(minimum) and any(
            flags.get(name, False)
            for name in deep.get("flags", ["debug", "long_context", "high_risk"])
        ):
            thinking = "T3"
        else:
            thinking = "T2"
            for mode, default_class, default_margin in (("T0", "R0", 0.5), ("T1", "R1", 0.4)):
                rule = thinking_rules.get(mode, {})
                maximum = _tier(rule.get("max_class", default_class)) or "c0"
                if TIERS.index(tier) <= TIERS.index(maximum) and margin >= float(
                    rule.get("min_margin", default_margin)
                ):
                    thinking = mode
                    break
    conditions = rules.get("prompt_policies", {}).get("P0", {}).get("conditions", {})
    if vector is not None:
        prompt = derive_prompt_policy(
            vector,
            dict(flags),
            max_difficulty=float(conditions.get("max_difficulty", 0.8)),
            min_margin=float(conditions.get("min_margin", 0.4)),
        )
    else:
        prompt = (
            "P2"
            if any(
                flags.get(name) for name in ("high_risk", "long_context", "debug", "strict_format")
            )
            else "P1"
        )
    ack = text.strip().lower().strip(" \t\r\n.!?。！？,，;；:：")
    if tier == "c0" and ack in _TRIVIAL_ACKS:
        return "T0", "P0"
    return normalize_decisions(thinking, prompt)


@dataclass(frozen=True)
class ModelPolicyState:
    tier: str
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    turn_count: int = 1
    version: int = 1
    task_start_input_message_id: str | None = None
    generation_start_input_message_id: str | None = None

    def __post_init__(self) -> None:
        if self.tier not in TIERS or not isinstance(self.task_id, str) or not self.task_id.strip():
            raise ValueError("invalid model routing state identity or tier")
        if any(type(value) is not int or value < 1 for value in (self.turn_count, self.version)):
            raise ValueError("invalid model routing state counters")
        for value in (self.task_start_input_message_id, self.generation_start_input_message_id):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError("invalid model routing state boundary")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "policy_version": POLICY_VERSION,
            **asdict(self),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ModelPolicyState:
        data = dict(value)
        if (
            data.pop("schema_version", None) != STATE_SCHEMA_VERSION
            or data.pop("policy_version", None) != POLICY_VERSION
        ):
            raise ValueError("incompatible model routing state")
        return cls(**data)


def _probability_winner(
    probabilities: Mapping[str, float],
    labels: tuple[str, ...],
) -> tuple[str, float]:
    """Validate one classifier boundary and apply its fixed-order argmax."""
    if (
        not isinstance(probabilities, Mapping)
        or set(probabilities) != set(labels)
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 1
            for value in probabilities.values()
        )
        or not math.isclose(sum(probabilities.values()), 1.0, rel_tol=1e-6, abs_tol=1e-6)
    ):
        raise ValueError("invalid classifier probability evidence")
    winner = max(labels, key=probabilities.__getitem__)
    return winner, probabilities[winner]


@dataclass(frozen=True)
class ModelPolicyAudit:
    source: str
    run_status: str
    final: str
    reason: str
    prediction: str | None = None
    probabilities: Mapping[str, float] | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        if self.source not in {
            "classifier",
            "rule",
            "fallback",
            "not_run",
        } or self.run_status not in {"ran", "error", "not_run"}:
            raise ValueError("invalid model routing classifier audit")
        if self.run_status == "ran":
            labels = INTENTS if self.final in INTENTS else TIERS
            if self.source != "classifier" or not isinstance(self.probabilities, Mapping):
                raise ValueError("classifier run is missing its probabilities")
            winner, confidence = _probability_winner(self.probabilities, labels)
            if (
                self.prediction != winner
                or self.final != winner
                or isinstance(self.confidence, bool)
                or self.confidence != confidence
            ):
                raise ValueError("classifier audit must preserve the fixed-order argmax")
        elif any(
            value is not None for value in (self.prediction, self.probabilities, self.confidence)
        ):
            raise ValueError("skipped or failed classifier cannot claim prediction evidence")
        elif self.run_status == "error" and self.source != "fallback":
            raise ValueError("classifier error requires an audited fallback")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ModelPolicyDecision:
    intent: str
    tier: str
    raw_tier: str
    intent_audit: ModelPolicyAudit
    tier_audit: ModelPolicyAudit
    next_state: ModelPolicyState
    context_reset: bool
    thinking_mode: str
    prompt_policy: str
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.intent not in INTENTS or self.tier not in TIERS or self.raw_tier not in TIERS:
            raise ValueError("invalid model routing decision labels")
        if self.intent_audit.final != self.intent or self.tier_audit.final != self.raw_tier:
            raise ValueError("model routing decision disagrees with classifier audits")
        if self.next_state.tier != self.tier or type(self.context_reset) is not bool:
            raise ValueError("model routing decision disagrees with next state")
        if self.thinking_mode not in {"T0", "T1", "T2", "T3"} or self.prompt_policy not in {
            "P0",
            "P1",
            "P2",
        }:
            raise ValueError("invalid model routing controller heads")
        previous = self.metadata.get("previous_tier")
        if self.intent in {"continue", "redo"}:
            if (
                previous not in TIERS
                or self.tier_audit.run_status != "not_run"
                or self.tier_audit.source != "not_run"
            ):
                raise ValueError("model reuse must skip tier classification")
            expected = TIERS[min(TIERS.index(previous) + (self.intent == "redo"), 3)]
            if self.raw_tier != expected:
                raise ValueError("model reuse conflicts with previous tier and intent")
        elif self.tier_audit.run_status not in {"ran", "error"}:
            raise ValueError("new task must attempt tier classification")
        held = self.metadata.get("held_tier")
        if held is not None and held not in TIERS:
            raise ValueError("invalid manual hold evidence")
        if self.tier != (held or self.raw_tier) or self.metadata.get(
            "router_control_hold_applied"
        ) is not (held is not None):
            raise ValueError("final model tier conflicts with the explicit hold")
        reset = self.intent == "new_task" and self.intent_audit.source == "rule"
        if self.context_reset != reset:
            raise ValueError("only explicit new-task control can reset context")
        if (
            self.metadata.get("quality_escalation_satisfied_by_prior_policy") is not True
            or self.metadata.get("quality_escalation_prior_policy_reason")
            != "model_tier_policy_resolved_explicit_correction"
        ):
            raise ValueError("model decision requires the explicit-correction recovery guard")

    def to_dict(self) -> dict[str, Any]:
        data = {
            "schema_version": DECISION_SCHEMA_VERSION,
            "policy_version": POLICY_VERSION,
            **asdict(self),
        }
        data["next_state"] = self.next_state.to_dict()
        data["trace_hash"] = _hash(data)
        return data

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ModelPolicyDecision:
        data = copy.deepcopy(dict(value))
        digest = data.pop("trace_hash", None)
        if digest != _hash(data):
            raise ValueError("model routing decision checksum mismatch")
        if (
            data.pop("schema_version", None) != DECISION_SCHEMA_VERSION
            or data.pop("policy_version", None) != POLICY_VERSION
        ):
            raise ValueError("incompatible model routing decision")
        data["next_state"] = ModelPolicyState.from_dict(data["next_state"])
        data["intent_audit"] = ModelPolicyAudit(**data["intent_audit"])
        data["tier_audit"] = ModelPolicyAudit(**data["tier_audit"])
        return cls(**data)


def _read(value: Any, field_name: str, default: Any = None) -> Any:
    return (
        value.get(field_name, default)
        if isinstance(value, Mapping)
        else getattr(value, field_name, default)
    )


def _prediction_audit(result: Any, *, head: str, fallback: str) -> ModelPolicyAudit:
    labels = INTENTS if head == "intent" else TIERS
    raw = _read(result, f"{head}_probabilities")
    probabilities: dict[str, float] = {}
    try:
        if not isinstance(raw, Mapping):
            raise ValueError("missing probabilities")
        for label, number in raw.items():
            name = str(label).lower()
            if name in probabilities:
                raise ValueError("invalid probability")
            probabilities[name] = number
        winner, confidence = _probability_winner(probabilities, labels)
        probabilities = {name: float(number) for name, number in probabilities.items()}
        confidence = float(confidence)
        reported = _read(result, f"{head}_decision")
        if reported is not None and probabilities.get(str(reported).lower()) != confidence:
            raise ValueError("reported decision is not an argmax")
        reported_confidence = _read(result, f"{head}_confidence", confidence)
        if (
            isinstance(reported_confidence, bool)
            or not isinstance(reported_confidence, (int, float))
            or not math.isclose(float(reported_confidence), confidence, rel_tol=1e-6, abs_tol=1e-6)
        ):
            raise ValueError("reported confidence differs from selected probability")
    except (TypeError, ValueError, OverflowError):
        return ModelPolicyAudit("fallback", "error", fallback, "invalid_classifier_result")
    return ModelPolicyAudit(
        "classifier", "ran", winner, "classifier_selected", winner, probabilities, confidence
    )


def quality_retry_tier(
    current_tier: str,
    reason: str,
    remaining_budget: int,
    *,
    already_used: bool = False,
    held_tier: str | None = None,
) -> str | None:
    """Authorize only a bounded concrete-failure remedy; caller reserves budget.

    Preflight, current spend limits and durable CAS must pass before activation.
    Transport failures are retried separately. Explicit correction is already
    resolved by the intent policy and must never cause a second upgrade.
    """
    if current_tier not in TIERS:
        raise ValueError("invalid current tier")
    if (
        reason not in {"validation_failure", "no_progress"}
        or _int(remaining_budget) < 1
        or already_used
        or held_tier is not None
        or current_tier == "c3"
    ):
        return None
    return TIERS[TIERS.index(current_tier) + 1]


def decide_model_tier(
    router_input: Mapping[str, Any],
    state: ModelPolicyState | None,
    classifier: Any,
    explicit_control: str | None = None,
    held_tier: str | None = None,
    default_tier: str = "c1",
    input_message_id: str | None = None,
    control_context: Mapping[str, Any] | None = None,
    *,
    task_id_factory: Callable[[], str] | None = None,
) -> ModelPolicyDecision:
    if default_tier not in TIERS or (held_tier is not None and held_tier not in TIERS):
        raise ValueError("invalid model routing default or manual hold")
    canonical = copy.deepcopy(dict(router_input))
    if not isinstance(canonical.get("current_request"), str):
        raise ValueError("model routing requires current_request text")
    canonical["history_user"] = list(canonical.get("history_user", []))[-4:]
    canonical["route_history"] = list(canonical.get("route_history", []))[-5:]
    canonical["active_route_tier"] = state.tier.upper() if state is not None else None
    event = str(explicit_control or "").strip().casefold()
    explicit_intent = (
        "new_task" if event in _NEW_CONTROLS else "redo" if event in _REDO_CONTROLS else None
    )
    result: Any = None
    inference_error: Exception | None = None
    inference_ran = False

    def predict(head: str, fallback: str) -> ModelPolicyAudit:
        nonlocal result, inference_error, inference_ran
        if not inference_ran:
            inference_ran = True
            try:
                result = classifier.predict(copy.deepcopy(canonical))
            except Exception as exc:
                if getattr(exc, "fail_closed", False) is True:
                    raise
                inference_error = exc
        if inference_error is not None:
            return ModelPolicyAudit(
                "fallback",
                "error",
                fallback,
                f"classifier_error:{type(inference_error).__name__}",
            )
        return _prediction_audit(result, head=head, fallback=fallback)

    if explicit_intent == "redo" and state is None:
        intent_audit = ModelPolicyAudit(
            "fallback", "not_run", "new_task", "redo_without_active_task"
        )
    elif explicit_intent is not None:
        intent_audit = ModelPolicyAudit(
            "rule", "not_run", explicit_intent, f"explicit_{explicit_intent}_control"
        )
    elif state is None:
        intent_audit = ModelPolicyAudit("fallback", "not_run", "new_task", "no_active_task")
    else:
        intent_audit = predict("intent", "continue")
    intent = intent_audit.final
    if state is not None and intent in {"continue", "redo"}:
        raw_tier = TIERS[min(TIERS.index(state.tier) + (intent == "redo"), 3)]
        tier_audit = ModelPolicyAudit(
            "not_run",
            "not_run",
            raw_tier,
            "model_continue_keeps_previous_tier"
            if intent == "continue"
            else "model_redo_upgrades_previous_tier",
        )
    else:
        tier_audit = predict("tier", state.tier if state is not None else default_tier)
        raw_tier = tier_audit.final
    final_tier = held_tier or raw_tier
    reset = intent_audit.source == "rule" and intent == "new_task"
    if intent == "new_task":
        task_id = task_id_factory() if task_id_factory else uuid.uuid4().hex
        turn_count = 1
        task_start_input_message_id = input_message_id
    else:
        assert state is not None  # Reuse is only selected for an active task above.
        task_id = state.task_id
        turn_count = state.turn_count + 1
        task_start_input_message_id = state.task_start_input_message_id
    next_state = ModelPolicyState(
        tier=final_tier,
        task_id=task_id,
        turn_count=turn_count,
        version=state.version + 1 if state is not None else 1,
        task_start_input_message_id=task_start_input_message_id,
        generation_start_input_message_id=input_message_id
        if reset
        else state.generation_start_input_message_id
        if state is not None
        else None,
    )
    context = dict(control_context if control_context is not None else canonical.get("context", {}))
    message = canonical["current_request"]
    flags_text = context.get("flags_text_override")
    flags_text = flags_text if isinstance(flags_text, str) else message
    rules = _rules()
    flags = _flags(flags_text, rules, _int(context.get("context_tokens_est")))
    raw_vector = (
        [tier_audit.probabilities[tier] for tier in TIERS]
        if tier_audit.probabilities is not None
        else None
    )
    mass = sum(raw_vector) if raw_vector is not None else None
    vector = (
        [value / mass for value in raw_vector] if raw_vector is not None and mass else raw_vector
    )
    thinking, prompt = _controller(final_tier, vector, flags, rules, flags_text)
    metadata = {
        "tier_policy": "model",
        "tier_policy_version": POLICY_VERSION,
        "raw_base_tier": raw_tier,
        "final_tier": final_tier,
        "previous_tier": state.tier if state is not None else None,
        "raw_probabilities": dict(tier_audit.probabilities)
        if tier_audit.probabilities is not None
        else None,
        "raw_confidence": tier_audit.confidence,
        "confidence_filter_enabled": False,
        "probability_margin_filter_enabled": False,
        "aux_head_available": False,
        "aux_downgrade_applied": False,
        "flags": flags,
        "runtime_rules_sha256": _hash(rules),
        "margin": compute_margin(vector) if vector is not None else None,
        "difficulty_score": compute_difficulty(vector) if vector is not None else None,
        "thinking_mode": thinking,
        "prompt_policy": prompt,
        "prompt_hint": select_localized_prompt_hint(
            rules.get("prompt_policies", {}).get(prompt, {}), message
        )
        if prompt != "P2"
        else None,
        "router_control_hold_applied": held_tier is not None,
        "held_tier": held_tier,
        "quality_escalation_used": False,
        "quality_escalation_satisfied_by_prior_policy": True,
        "quality_escalation_prior_policy_reason": (
            "model_tier_policy_resolved_explicit_correction"
        ),
        "classifier_input_hash": _hash(canonical),
    }
    return ModelPolicyDecision(
        intent,
        final_tier,
        raw_tier,
        intent_audit,
        tier_audit,
        next_state,
        reset,
        thinking,
        prompt,
        metadata,
    )


def quality_retry_controls(
    decision: ModelPolicyDecision,
    target_tier: str,
) -> tuple[str, str]:
    """Reconcile the original heads exactly as the frozen execution retry.

    Retry controller policy promotes the prior heads, without reclassifying or
    rerunning text flags.
    """
    from opensquilla.engine.routing.policy import reconcile_controller_with_final_tier

    if target_tier not in TIERS or TIERS.index(target_tier) != TIERS.index(decision.tier) + 1:
        raise ValueError("quality recovery must advance exactly one tier")
    thinking, prompt = reconcile_controller_with_final_tier(
        decision.thinking_mode,
        decision.prompt_policy,
        {"base_tier": decision.tier, "final_tier": target_tier},
    )
    assert thinking is not None and prompt is not None
    return thinking, prompt
