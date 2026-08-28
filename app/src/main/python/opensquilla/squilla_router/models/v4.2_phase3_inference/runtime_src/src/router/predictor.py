"""Squilla router inference orchestrator.

Loads a trained LightGBM model and feature pipeline, predicts R0-R3
route class, applies post-processing (margin upgrade, flag overrides),
and derives thinking mode, prompt policy, and model selection.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import yaml

from src.router.features import ContextMetadata
from src.router.flags import RoutingFlags, compute_flags

ROUTE_CLASSES = ["R0", "R1", "R2", "R3"]
_CLASS_TO_IDX = {c: i for i, c in enumerate(ROUTE_CLASSES)}


@dataclass
class RoutingResult:
    route_class: str
    probabilities: dict
    difficulty_score: float
    margin: float
    flags: RoutingFlags
    tier: str
    thinking_mode: str
    prompt_policy: str
    prompt_hint: str
    selected_model: str
    trajectory: str = "COLD_START"
    model_version: str = "v1"
    # v4 additions (optional, default no-op for v1/v2/v3 callers):
    aux_decision_probs: dict | None = None
    bge_channels_used: list = field(default_factory=list)
    asst_signal_present: bool = False
    aux_downgrade_applied: bool = False
    sticky_applied: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _apply_margin_upgrade(route_class: str, margin: float,
                          config: dict) -> str:
    threshold = config.get("thresholds", {}).get("margin_upgrade", 0.15)
    if margin < threshold:
        idx = _CLASS_TO_IDX[route_class]
        if idx < len(ROUTE_CLASSES) - 1:
            return ROUTE_CLASSES[idx + 1]
    return route_class


def _apply_r1_rescue(route_class: str, probs: np.ndarray,
                     config: dict) -> str:
    """Rescue R1 from R0 only (safe upward direction).

    Only promotes R0→R1 when R1 is a close second. Never demotes R2→R1
    because that would increase under-routing (complex task on weak model).
    """
    rescue = config.get("thresholds", {}).get("r1_rescue", {})
    r0_gap = rescue.get("from_r0_max_gap", 0.20)

    if route_class == "R0":
        r1_prob = float(probs[1])
        r0_prob = float(probs[0])
        if r0_prob - r1_prob < r0_gap:
            return "R1"
    return route_class


def _apply_flag_overrides(route_class: str, flags: RoutingFlags,
                          config: dict) -> str:
    idx = _CLASS_TO_IDX[route_class]
    if flags.high_risk:
        idx = max(idx, _CLASS_TO_IDX["R2"])
    if flags.debug and flags.long_context:
        idx = max(idx, _CLASS_TO_IDX["R2"])
    if flags.repo_arch:
        idx = max(idx, _CLASS_TO_IDX["R1"])
    return ROUTE_CLASSES[idx]


def _derive_thinking_mode(route_class: str, margin: float,
                          flags: RoutingFlags, config: dict) -> str:
    rules = config.get("thinking_mode_rules", {})
    if route_class == "R3":
        return "T3"
    t3_flags = rules.get("T3", {}).get("flags", ["debug", "long_context", "high_risk"])
    if _CLASS_TO_IDX[route_class] >= _CLASS_TO_IDX.get(
            rules.get("T3", {}).get("min_class", "R2"), 2):
        for flag_name in t3_flags:
            if getattr(flags, flag_name, False):
                return "T3"
    t0_rule = rules.get("T0", {})
    max_class_t0 = t0_rule.get("max_class", "R0")
    if (_CLASS_TO_IDX[route_class] <= _CLASS_TO_IDX.get(max_class_t0, 0)
            and margin >= t0_rule.get("min_margin", 0.5)):
        return "T0"
    t1_rule = rules.get("T1", {})
    max_class_t1 = t1_rule.get("max_class", "R1")
    if (_CLASS_TO_IDX[route_class] <= _CLASS_TO_IDX.get(max_class_t1, 1)
            and margin >= t1_rule.get("min_margin", 0.4)):
        return "T1"
    return "T2"


def _derive_prompt_policy(difficulty_score: float, margin: float,
                          flags: RoutingFlags, config: dict) -> str:
    policies = config.get("prompt_policies", {})
    p2_conds = policies.get("P2", {}).get("conditions", {})
    any_flags = p2_conds.get("any_flag", ["high_risk", "long_context", "debug", "strict_format"])
    for flag_name in any_flags:
        if getattr(flags, flag_name, False):
            return "P2"
    p0_conds = policies.get("P0", {}).get("conditions", {})
    max_diff = p0_conds.get("max_difficulty", 0.8)
    min_margin = p0_conds.get("min_margin", 0.4)
    no_flags = p0_conds.get("no_flags", ["high_risk", "strict_format", "debug"])
    has_blocking_flag = any(getattr(flags, f, False) for f in no_flags)
    if difficulty_score <= max_diff and margin >= min_margin and not has_blocking_flag:
        return "P0"
    return "P1"


def _select_model(route_class: str, config: dict) -> tuple[str, str]:
    tier_mapping = config.get("tier_mapping", {})
    tier_registry = config.get("tier_registry", {})
    tier = tier_mapping.get(route_class, "M")
    models = tier_registry.get(tier, ["unknown"])
    return tier, models[0]


def _prompt_hint_locale(text: str | None) -> str:
    if not text:
        return "en"
    cjk_count = 0
    latin_count = 0
    for char in text:
        if (
            "\u4e00" <= char <= "\u9fff"
            or "\u3400" <= char <= "\u4dbf"
            or "\uf900" <= char <= "\ufaff"
        ):
            cjk_count += 1
        elif char.isascii() and char.isalpha():
            latin_count += 1
    if cjk_count >= 2:
        return "zh"
    return "en"


def _get_prompt_hint(policy: str, config: dict, text: str | None = None) -> str:
    policies = config.get("prompt_policies", {})
    p = policies.get(policy, {})
    if _prompt_hint_locale(text) == "zh":
        return p.get("hint_zh", "") or p.get("hint_en", "")
    return p.get("hint_en", "") or p.get("hint_zh", "")


def _detect_model_version(model_dir: Path) -> tuple[str, dict]:
    """Read version.json from model directory; default to v1 if absent."""
    vjson = model_dir / "version.json"
    if vjson.exists():
        meta = json.loads(vjson.read_text(encoding="utf-8"))
        return meta.get("version", "v1"), meta
    return "v1", {}


def _reconcile_extractor_schema(extractor, booster) -> None:
    """Infer use_context / use_hist from booster dim when meta.json is missing."""
    num_feat = booster.num_feature()
    bge_offset = 64 if extractor.use_bge else 0
    core = num_feat - bge_offset
    known = {
        151: (False, False),
        161: (True, False),
        167: (False, True),
        177: (True, True),
    }
    if core not in known:
        raise ValueError(
            f"unexpected booster dim {num_feat} with use_bge={extractor.use_bge}"
        )
    extractor.use_context, extractor.use_hist = known[core]


def _apply_sticky_tier(pred_class: str, probs: np.ndarray,
                       history: list | None, cfg: dict) -> str:
    """Layer 6: KV-cache-aware sticky tier.

    When the current prediction is lower than the previous turn's class, stick
    with the previous class to avoid unnecessary KV-cache invalidation from
    tier downgrades.
    """
    if not history or not cfg.get("thresholds", {}).get("kv_cache_aware", False):
        return pred_class
    prev_idx = _CLASS_TO_IDX[history[-1].route_class]
    pred_idx = _CLASS_TO_IDX[pred_class]
    if prev_idx > pred_idx:
        return history[-1].route_class
    return pred_class


def apply_post_processing(
    probs: np.ndarray,
    text: str,
    config: dict,
    context: ContextMetadata | None = None,
    history: list | None = None,
) -> tuple[int, RoutingFlags]:
    """Apply the full 6-layer post-processing pipeline.

    Steps: argmax -> margin upgrade -> R1 rescue -> safety net -> flag overrides
           -> context-based routing rules -> sticky tier.
    Returns (final_class_idx, flags).
    """
    sorted_probs = sorted(probs, reverse=True)
    margin = float(sorted_probs[0] - sorted_probs[1])

    base_class = ROUTE_CLASSES[int(np.argmax(probs))]
    flags = compute_flags(text, config, context=context)

    route_class = _apply_margin_upgrade(base_class, margin, config)
    route_class = _apply_r1_rescue(route_class, probs, config)

    # Safety net
    safety_threshold = config.get("thresholds", {}).get(
        "under_routing_safety", 0.45)
    if _CLASS_TO_IDX[route_class] < 2:
        heavy_prob = float(probs[2] + probs[3])
        if heavy_prob > safety_threshold:
            route_class = "R2"

    route_class = _apply_flag_overrides(route_class, flags, config)

    # Context-based routing rules
    if context is not None:
        ctx_rules = config.get("context_rules", {})
        deep_threshold = ctx_rules.get("deep_conversation_threshold", 4)
        if context.turn_index >= deep_threshold:
            deep_min = ctx_rules.get("deep_conversation_min_class", "R1")
            idx = max(_CLASS_TO_IDX[route_class], _CLASS_TO_IDX[deep_min])
            route_class = ROUTE_CLASSES[idx]

    # Layer 6: sticky tier (KV-cache-aware)
    route_class = _apply_sticky_tier(route_class, probs, history, config)

    return _CLASS_TO_IDX[route_class], flags


class SquillaRouter:
    """Factory + standard 4-class router.

    Auto-dispatches to ``V4Router`` when ``<model_dir>/version.json`` declares
    ``version: "v4"``. v1/v2 callers see the original behavior.
    """

    def __new__(cls, model_dir: str = "models/", *args, **kwargs):
        version_path = Path(model_dir) / "version.json"
        if version_path.exists():
            try:
                ver = json.loads(version_path.read_text(encoding="utf-8")).get("version")
            except (json.JSONDecodeError, OSError):
                ver = None
            if ver == "v4":
                from src.router.v4_predictor import V4Router
                return V4Router(model_dir, *args, **kwargs)
        # Fall through to default v1/v2 initialization.
        # Returning a SquillaRouter instance causes Python to invoke
        # __init__ with the original args/kwargs.
        return super().__new__(cls)

    def __init__(self, model_dir: str = "models/",
                 config_path: str = "configs/router.yaml"):
        with open(config_path, encoding="utf-8") as f:
            self._config = yaml.safe_load(f)
        self._model_dir = Path(model_dir)
        self._version, self._meta = _detect_model_version(self._model_dir)
        import lightgbm as lgb
        self._model = lgb.Booster(model_file=str(self._model_dir / "lgbm_model.bin"))
        from src.router.features import FeatureExtractor
        self._extractor = FeatureExtractor.load(str(self._model_dir / "features"))
        if not (self._model_dir / "features" / "meta.json").exists():
            _reconcile_extractor_schema(self._extractor, self._model)

    def predict(self, text: str, context: ContextMetadata | None = None, *,
                history: list | None = None,
                trajectory=None) -> RoutingResult:
        from src.router.trajectory import classify as classify_trajectory
        if trajectory is None:
            trajectory = classify_trajectory(history or [])

        features = self._extractor.transform(
            text, context=context, history=history, trajectory=trajectory,
        ).reshape(1, -1)
        expected_dims = self._model.num_feature()
        actual_dims = features.shape[1]
        if actual_dims != expected_dims:
            raise ValueError(
                f"Feature dimension mismatch: model expects {expected_dims}, "
                f"got {actual_dims}. Check if model was trained with --context."
            )
        raw_probs = self._model.predict(features)[0]
        probs = {c: float(p) for c, p in zip(ROUTE_CLASSES, raw_probs)}
        sorted_probs = sorted(raw_probs, reverse=True)
        margin = float(sorted_probs[0] - sorted_probs[1])
        difficulty_score = float(sum(i * p for i, p in enumerate(raw_probs)))

        cls_idx, flags = apply_post_processing(
            raw_probs, text, self._config, context=context, history=history,
        )
        route_class = ROUTE_CLASSES[cls_idx]

        thinking_mode = _derive_thinking_mode(route_class, margin, flags, self._config)
        prompt_policy = _derive_prompt_policy(difficulty_score, margin, flags, self._config)
        prompt_hint = _get_prompt_hint(prompt_policy, self._config, text)
        tier, selected_model = _select_model(route_class, self._config)
        return RoutingResult(
            route_class=route_class, probabilities=probs,
            difficulty_score=difficulty_score, margin=margin,
            flags=flags, tier=tier, thinking_mode=thinking_mode,
            prompt_policy=prompt_policy, prompt_hint=prompt_hint,
            selected_model=selected_model,
            trajectory=trajectory.value if hasattr(trajectory, 'value') else str(trajectory),
            model_version=self._version,
        )

    def route(self, text: str, context: ContextMetadata | None = None, *,
              history: list | None = None, trajectory=None) -> RoutingResult:
        return self.predict(text, context=context, history=history, trajectory=trajectory)


class CascadeRouter:
    """Two-stage cascade router: Stage1 (light/heavy) → Stage2a/2b."""

    def __init__(self, model_dir: str = "models/",
                 config_path: str = "configs/router.yaml"):
        with open(config_path, encoding="utf-8") as f:
            self._config = yaml.safe_load(f)
        model_path = Path(model_dir)
        self._version, self._meta = _detect_model_version(model_path)
        import lightgbm as lgb
        self._stage1 = lgb.Booster(
            model_file=str(model_path / "cascade_stage1.bin"))
        self._stage2a = lgb.Booster(
            model_file=str(model_path / "cascade_stage2a.bin"))
        self._stage2b = lgb.Booster(
            model_file=str(model_path / "cascade_stage2b.bin"))
        from src.router.features import FeatureExtractor
        self._extractor = FeatureExtractor.load(str(model_path / "features"))

    def predict(self, text: str, context: ContextMetadata | None = None, *,
                history: list | None = None,
                trajectory=None) -> RoutingResult:
        features = self._extractor.transform(text, context=context).reshape(1, -1)

        # Stage 1: light(0) vs heavy(1)
        # Bias toward "heavy" to minimize under-routing: threshold 0.4
        # (uncertain samples go to R2/R3 rather than risk under-routing)
        s1_threshold = self._config.get("thresholds", {}).get(
            "cascade_stage1_threshold", 0.4)
        s1_prob = float(self._stage1.predict(features)[0])
        is_heavy = s1_prob > s1_threshold

        if is_heavy:
            # Stage 2b: R2(0) vs R3(1)
            s2b_prob = float(self._stage2b.predict(features)[0])
            if s2b_prob > 0.5:
                base_class = "R3"
                probs = {
                    "R0": 0.0, "R1": 0.0,
                    "R2": (1 - s2b_prob) * s1_prob,
                    "R3": s2b_prob * s1_prob,
                }
            else:
                base_class = "R2"
                probs = {
                    "R0": 0.0, "R1": 0.0,
                    "R2": (1 - s2b_prob) * s1_prob,
                    "R3": s2b_prob * s1_prob,
                }
        else:
            # Stage 2a: R0(0) vs R1(1)
            s2a_prob = float(self._stage2a.predict(features)[0])
            if s2a_prob > 0.5:
                base_class = "R1"
                probs = {
                    "R0": (1 - s2a_prob) * (1 - s1_prob),
                    "R1": s2a_prob * (1 - s1_prob),
                    "R2": 0.0, "R3": 0.0,
                }
            else:
                base_class = "R0"
                probs = {
                    "R0": (1 - s2a_prob) * (1 - s1_prob),
                    "R1": s2a_prob * (1 - s1_prob),
                    "R2": 0.0, "R3": 0.0,
                }

        # Normalize probabilities
        total = sum(probs.values()) or 1.0
        probs = {k: v / total for k, v in probs.items()}

        raw_probs = np.array([probs[c] for c in ROUTE_CLASSES])
        sorted_p = sorted(raw_probs, reverse=True)
        margin = float(sorted_p[0] - sorted_p[1])
        difficulty_score = float(
            sum(i * p for i, p in enumerate(raw_probs)))

        flags = compute_flags(text, self._config)
        route_class = _apply_flag_overrides(base_class, flags, self._config)
        thinking_mode = _derive_thinking_mode(
            route_class, margin, flags, self._config)
        prompt_policy = _derive_prompt_policy(
            difficulty_score, margin, flags, self._config)
        prompt_hint = _get_prompt_hint(prompt_policy, self._config, text)
        tier, selected_model = _select_model(route_class, self._config)

        # Derive trajectory for RoutingResult v2 fields
        from src.router.trajectory import classify as classify_trajectory
        if trajectory is None:
            trajectory = classify_trajectory(history or [])

        return RoutingResult(
            route_class=route_class, probabilities=probs,
            difficulty_score=difficulty_score, margin=margin,
            flags=flags, tier=tier, thinking_mode=thinking_mode,
            prompt_policy=prompt_policy, prompt_hint=prompt_hint,
            selected_model=selected_model,
            trajectory=trajectory.value if hasattr(trajectory, 'value') else str(trajectory),
            model_version=self._version,
        )

    def route(self, text: str, context: ContextMetadata | None = None, *,
              history: list | None = None, trajectory=None) -> RoutingResult:
        return self.predict(text, context=context, history=history, trajectory=trajectory)
