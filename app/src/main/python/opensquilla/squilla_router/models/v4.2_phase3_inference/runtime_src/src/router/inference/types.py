from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class InferenceRequest:
    current_user_text: str
    history_user_texts: list[str]
    prev_assistant_text: str | None
    prev_assistant_usage: dict | None
    prev_route_decisions: list
    flags_text_override: str | None = None
    context_metadata: dict = field(default_factory=dict)


@dataclass
class FeatureBundle:
    features_390: np.ndarray
    raw_bge_1536: np.ndarray
    bge_channels_used: list[str]
    asst_signal_present: bool
    history_user_text_compacted: str | None = None


@dataclass
class HeadOutputs:
    p_main_lgbm: np.ndarray
    p_aux_lgbm: np.ndarray | None
    logits_mlp: np.ndarray
    p_mlp_calibrated: np.ndarray


@dataclass
class FinalDecision:
    route_class: str
    margin: float
    difficulty_score: float
    flags: dict
    thinking_mode: str
    prompt_policy: str
    selected_model: str
    aux_downgrade_applied: bool
    sticky_applied: bool


@dataclass
class InferenceResult:
    decision: FinalDecision
    probabilities: dict[str, float]
    aux_decision_probs: dict[str, float] | None
    intermediates: dict | None = None
