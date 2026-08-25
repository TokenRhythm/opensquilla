"""Old-vs-new routing decision parity for the routing policy extraction.

The squilla-router step's post-classifier heuristics (confidence gate,
complaint upgrade, KV-cache anti-downgrade, large-context floor,
tier-provider mismatch flag, bind) live in
``opensquilla.engine.routing.policy``. This suite proves the extraction is
behavior-preserving: every corpus case must produce byte-identical routing
decisions and routing metadata before and after the extraction.

Capture method
--------------
Expected outputs live in ``goldens/routing_policy_parity_golden.json``. They
were captured by running this same harness (``uv run python
tests/test_engine/test_routing_policy_parity.py --capture``) against the
pre-extraction ``apply_squilla_router`` at the branch base
(staging/provider-overhaul @ 233af0c2), before any refactoring touched
``engine/steps/squilla_router.py``. The pytest run replays the identical
corpus through the current code and compares the canonical JSON
serialization (sorted keys, ``_ts`` monotonic timestamps scrubbed) of each
observation against that golden.

The large-context cases were intentionally recaptured for Issue #976 after
the capacity floor became a hard lower bound for physical fallback. Their
goldens therefore include the sanitized floor tier and omit fallback models
below it; the remaining corpus still pins the original extraction parity.

The image-bypass cases were intentionally recaptured for the #1195 reopen
after attachment capacity became mandatory for every attachment turn. Those
entries include the capacity-required marker, material estimate, thinking
reserve, and capacity-filtered fallback chain. Non-attachment cases retain
their prior byte-identical behavior.

The missing image-tier case was intentionally recaptured after Router image
admission began returning a structured rejection instead of raising a runtime
configuration exception. Its golden pins the stable rejection metadata while
the later provider admission layer owns the user-facing error event.

Classifier outputs are injected through a fake strategy: the corpus never
loads the LightGBM/ONNX bundle, touches the network, or needs credentials.
All tier/model/provider names and messages are synthetic dummy data; tier
model ids intentionally contain no ``/`` so pricing stays on the offline
static table.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.steps import squilla_router as squilla_router_step
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider import model_catalog as model_catalog_module
from opensquilla.provider.model_catalog import ModelCatalog

GOLDEN_PATH = Path(__file__).parent / "goldens" / "routing_policy_parity_golden.json"

# Every knob the corpus depends on is pinned explicitly so the golden is
# independent of GatewayConfig default drift and ambient environment.
BASE_ROUTER_KNOBS: dict[str, Any] = {
    "enabled": True,
    "auto_thinking": True,
    "rollout_phase": "full",
    "strategy": "v4_phase3",
    "tier_profile": None,
    "cross_provider_tiers": False,
    "default_tier": "c1",
    "confidence_threshold": 0.5,
    "confidence_high_tier_margin": 0.05,
    "kv_cache_anti_downgrade_enabled": True,
    "kv_cache_anti_downgrade_window_seconds": 600,
    "complaint_upgrade_enabled": True,
    "complaint_upgrade_steps": 1,
    "complaint_upgrade_max_chars": 160,
    "vision_history_lookback_turns": 8,
}


def synthetic_tiers() -> dict:
    return {
        "c0": {"model": "dummy-nano-1", "description": "tier c0"},
        "c1": {"model": "dummy-mini-1", "description": "tier c1"},
        "c2": {"model": "dummy-pro-1", "description": "tier c2"},
        "c3": {"model": "dummy-max-1", "description": "tier c3", "supports_thinking": True},
        "image_model": {
            "model": "dummy-vision-1",
            "supports_image": True,
            "image_only": True,
        },
    }


def tiers_with_provider(provider: str = "otherprov", tier: str = "c2") -> dict:
    tiers = synthetic_tiers()
    tiers[tier] = {**tiers[tier], "provider": provider}
    return tiers


def default_extra(
    route_class: str = "R1",
    thinking: str | None = "T1",
    prompt: str | None = "P1",
    **overrides: Any,
) -> dict:
    extra: dict[str, Any] = {
        "route_class": route_class,
        "top1_label": route_class,
        "probabilities": [0.05, 0.85, 0.07, 0.03],
        "margin": 0.78,
        "model_version": "parity-synthetic",
    }
    if thinking is not None:
        extra["thinking_mode"] = thinking
    if prompt is not None:
        extra["prompt_policy"] = prompt
    extra.update(overrides)
    return extra


def hist_entry(
    final_tier: str | None,
    route_class: str | None,
    ts_offset: float,
    turn_index: int = 0,
) -> dict:
    entry: dict[str, Any] = {
        "turn_index": turn_index,
        "text": "earlier turn",
        "_ts_offset": ts_offset,
    }
    if final_tier is not None:
        entry["final_tier"] = final_tier
        entry["final_route_class"] = route_class
    elif route_class is not None:
        entry["route_class"] = route_class
    return entry


DEFAULT_CLASSIFIER: tuple[str, float, str, dict] = ("c1", 0.9, "v4_phase3", default_extra())


class _FakeStrategy:
    """Injects a fixed classifier output — the seam the policy engine tests."""

    def __init__(self, tier: str, confidence: float, source: str, extra: dict) -> None:
        self.tier = tier
        self.confidence = confidence
        self.source = source
        self.extra = extra

    async def classify(
        self,
        message: str,
        valid_tiers: list[str],
        routing_history: list[dict] | None = None,
    ) -> tuple[str, float, str, dict]:
        return self.tier, self.confidence, self.source, copy.deepcopy(self.extra)


class _UnexpectedClassifyStrategy:
    """Sentinel for cases that must return before classification."""

    async def classify(
        self,
        message: str,
        valid_tiers: list[str],
        routing_history: list[dict] | None = None,
    ) -> tuple[str, float, str, dict]:
        raise AssertionError("classifier must not run for this corpus case")


@dataclass
class Case:
    name: str
    message: str = "please summarize this short note"
    raw_message: str | None = None
    session_key: str = "agent:parity:main"
    classifier: tuple[str, float, str, dict] | None = None  # None -> DEFAULT_CLASSIFIER
    classify_expected: bool = True
    tiers: dict | None = None  # None -> synthetic_tiers()
    router: dict = field(default_factory=dict)
    router_forced: dict = field(default_factory=dict)  # attrs outside the config schema
    llm_provider: str = "mainprov"
    llm_model: str = "dummy-base-model"
    attachments: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def build_corpus() -> list[Case]:
    cases: list[Case] = []

    # --- step gating / passthrough paths ---------------------------------
    cases.append(
        Case(
            name="router_disabled_passthrough",
            router={"enabled": False},
            classify_expected=False,
        )
    )
    cases.append(Case(name="no_tiers_passthrough", tiers={}, classify_expected=False))
    cases.append(Case(name="blank_message_passthrough", message="   ", classify_expected=False))
    cases.append(
        Case(
            name="subagent_session_passthrough",
            session_key="agent:parity:subagent:x1",
            classify_expected=False,
        )
    )
    cases.append(
        Case(
            name="only_image_only_tiers_passthrough",
            tiers={
                "image_model": {
                    "model": "dummy-vision-1",
                    "supports_image": True,
                    "image_only": True,
                }
            },
            classify_expected=False,
        )
    )

    # --- image bypass (pre-classifier, stays in the step) -----------------
    cases.append(
        Case(
            name="image_current_turn_bypass",
            attachments=[{"type": "image/png", "name": "synthetic.png"}],
            classify_expected=False,
        )
    )
    cases.append(
        Case(
            name="image_media_type_key_bypass",
            attachments=[{"media_type": "image/jpeg"}],
            classify_expected=False,
        )
    )
    cases.append(
        Case(
            name="image_gate_history_bypass",
            metadata={"router_vision_followup_needs_image": True},
            classify_expected=False,
        )
    )
    cases.append(
        Case(
            name="image_bypass_in_observe_phase",
            router={"rollout_phase": "observe"},
            attachments=[{"mime": "image/webp"}],
            classify_expected=False,
        )
    )
    cases.append(
        Case(
            name="image_without_image_tier_errors",
            tiers={k: v for k, v in synthetic_tiers().items() if k != "image_model"},
            attachments=[{"type": "image/png"}],
            classify_expected=False,
        )
    )

    # --- confidence gate ---------------------------------------------------
    cases.append(
        Case(
            name="confidence_gate_downgrades_low_confidence_high_tier",
            classifier=("c2", 0.30, "v4_phase3", default_extra("R2", "T2")),
        )
    )
    cases.append(
        Case(
            name="confidence_gate_margin_keeps_high_tier",
            classifier=("c2", 0.46, "v4_phase3", default_extra("R2", "T2")),
        )
    )
    cases.append(
        Case(
            name="confidence_gate_margin_boundary_downgrades",
            classifier=("c2", 0.44, "v4_phase3", default_extra("R2", "T2")),
        )
    )
    cases.append(
        Case(
            name="confidence_gate_lifts_low_confidence_low_tier",
            classifier=("c0", 0.40, "v4_phase3", default_extra("R0", "T0")),
        )
    )
    cases.append(
        Case(
            name="confidence_gate_threshold_boundary_keeps_tier",
            classifier=("c0", 0.50, "v4_phase3", default_extra("R0", "T0")),
        )
    )
    cases.append(
        Case(
            name="confidence_gate_disabled_without_default_tier",
            router={"default_tier": None},
            classifier=("c2", 0.10, "v4_phase3", default_extra("R2", "T2")),
        )
    )

    # --- complaint upgrade -------------------------------------------------
    cases.append(
        Case(
            name="complaint_short_english_upgrades",
            message="wrong, try again",
            classifier=("c0", 0.9, "v4_phase3", default_extra("R0", "T0", "P0")),
        )
    )
    cases.append(
        Case(
            name="complaint_chinese_upgrades",
            message="这个结论不对，请重写",
        )
    )
    cases.append(
        Case(
            name="complaint_over_max_chars_ignored",
            message="wrong " + "waffle " * 30,
        )
    )
    cases.append(
        Case(
            name="complaint_at_max_chars_boundary",
            message="rewrite " + "z" * 152,
        )
    )
    cases.append(
        Case(
            name="complaint_disabled_no_upgrade",
            message="wrong, try again",
            router={"complaint_upgrade_enabled": False},
        )
    )
    cases.append(
        Case(
            name="complaint_two_step_upgrade",
            message="redo this summary",
            router={"complaint_upgrade_steps": 2},
            classifier=("c0", 0.9, "v4_phase3", default_extra("R0", "T0")),
        )
    )
    cases.append(
        Case(
            name="complaint_restores_pre_confidence_tier",
            message="that's not right, try again",
            classifier=("c2", 0.30, "v4_phase3", default_extra("R2", "T2")),
        )
    )
    cases.append(
        Case(
            name="complaint_starts_from_previous_tier",
            message="you missed my point, redo",
            metadata={"routing_history": [hist_entry("c2", "R2", 30)]},
            classifier=("c0", 0.9, "v4_phase3", default_extra("R0", "T0")),
        )
    )
    cases.append(
        Case(
            name="complaint_at_highest_tier_no_change",
            message="wrong again",
            classifier=("c3", 0.9, "v4_phase3", default_extra("R3", "T3")),
        )
    )

    # --- KV-cache anti-downgrade --------------------------------------------
    cases.append(
        Case(
            name="anti_downgrade_holds_previous_tier",
            metadata={"routing_history": [hist_entry("c3", "R3", 30)]},
        )
    )
    cases.append(
        Case(
            name="anti_downgrade_window_expired",
            metadata={"routing_history": [hist_entry("c3", "R3", 700)]},
        )
    )
    cases.append(
        Case(
            name="anti_downgrade_disabled",
            router={"kv_cache_anti_downgrade_enabled": False},
            metadata={"routing_history": [hist_entry("c3", "R3", 30)]},
        )
    )
    cases.append(
        Case(
            name="anti_downgrade_previous_from_route_class",
            metadata={"routing_history": [hist_entry(None, "R2", 30)]},
        )
    )
    cases.append(
        Case(
            name="anti_downgrade_previous_lower_no_change",
            metadata={"routing_history": [hist_entry("c0", "R0", 30)]},
            classifier=("c2", 0.9, "v4_phase3", default_extra("R2", "T2")),
        )
    )
    cases.append(
        Case(
            name="history_pruned_beyond_session_window",
            metadata={"routing_history": [hist_entry("c3", "R3", 2000)]},
        )
    )
    cases.append(
        Case(
            name="anti_downgrade_custom_window_expired",
            router={"kv_cache_anti_downgrade_window_seconds": 60},
            metadata={"routing_history": [hist_entry("c3", "R3", 120)]},
        )
    )
    cases.append(
        Case(
            name="anti_downgrade_picks_last_entry_within_window",
            metadata={
                "routing_history": [
                    hist_entry("c3", "R3", 40),
                    hist_entry("c2", "R2", 20, turn_index=1),
                ]
            },
            classifier=("c0", 0.9, "v4_phase3", default_extra("R0", "T0")),
        )
    )

    # --- large-context floor -------------------------------------------------
    def floor_case(name: str, tokens: int, **kwargs: Any) -> Case:
        metadata = dict(kwargs.pop("metadata", {}))
        metadata.setdefault("material_estimated_tokens", tokens)
        kwargs.setdefault(
            "classifier", ("c0", 0.9, "v4_phase3", default_extra("R0", "T0"))
        )
        return Case(name=name, metadata=metadata, **kwargs)

    cases.append(floor_case("large_context_below_floor_boundary", 24_999))
    cases.append(floor_case("large_context_floor_to_c2_boundary", 25_000))
    cases.append(floor_case("large_context_floor_between_thresholds", 79_999))
    cases.append(floor_case("large_context_floor_to_c3_boundary", 80_000))
    cases.append(
        floor_case(
            "large_context_ratio_floor_to_c3",
            20_000,
            router_forced={"context_window_tokens": 50_000},
        )
    )
    cases.append(
        floor_case(
            "large_context_ratio_below_no_floor",
            19_999,
            router_forced={"context_window_tokens": 50_000},
        )
    )
    cases.append(
        floor_case(
            "large_context_no_floor_when_already_high",
            30_000,
            classifier=("c3", 0.9, "v4_phase3", default_extra("R3", "T3")),
        )
    )
    cases.append(
        Case(
            name="large_context_nested_normalization_tokens",
            metadata={"input_normalization": {"material_estimated_tokens": 26_000}},
            classifier=("c0", 0.9, "v4_phase3", default_extra("R0", "T0")),
        )
    )
    cases.append(
        floor_case(
            "large_context_floor_observe_phase",
            90_000,
            router={"rollout_phase": "observe"},
        )
    )
    cases.append(
        floor_case(
            "large_context_floor_reconciles_thinking",
            85_000,
            classifier=("c0", 0.9, "v4_phase3", default_extra("R0", "T0", "P0")),
        )
    )

    # --- tier-provider mismatch flag ------------------------------------------
    cases.append(
        Case(
            name="provider_mismatch_flagged",
            tiers=tiers_with_provider(),
            classifier=("c2", 0.9, "v4_phase3", default_extra("R2", "T2")),
        )
    )
    cases.append(
        Case(
            name="provider_mismatch_cross_provider_enabled",
            router={"cross_provider_tiers": True},
            tiers=tiers_with_provider(),
            classifier=("c2", 0.9, "v4_phase3", default_extra("R2", "T2")),
        )
    )
    cases.append(
        Case(
            name="provider_match_records_routed_provider",
            tiers=tiers_with_provider("MainProv"),
            classifier=("c2", 0.9, "v4_phase3", default_extra("R2", "T2")),
        )
    )
    cases.append(
        Case(
            name="provider_mismatch_skipped_in_observe",
            router={"rollout_phase": "observe"},
            tiers=tiers_with_provider(),
            classifier=("c2", 0.9, "v4_phase3", default_extra("R2", "T2")),
        )
    )
    cases.append(
        Case(
            name="no_tier_provider_no_flag",
            classifier=("c2", 0.9, "v4_phase3", default_extra("R2", "T2")),
        )
    )
    cases.append(
        Case(
            name="blank_active_provider_no_flag",
            llm_provider="",
            tiers=tiers_with_provider(),
            classifier=("c2", 0.9, "v4_phase3", default_extra("R2", "T2")),
        )
    )

    # --- rollout phases + controller application -------------------------------
    cases.append(
        Case(
            name="observe_phase_records_without_applying",
            router={"rollout_phase": "observe"},
            classifier=("c2", 0.9, "v4_phase3", default_extra("R2", "T2")),
        )
    )
    cases.append(
        Case(
            name="prompt_only_phase_injects_hint",
            router={"rollout_phase": "prompt_only"},
            classifier=("c0", 0.9, "v4_phase3", default_extra("R0", "T0", "P0")),
        )
    )
    cases.append(
        Case(
            name="full_phase_p0_hint_english",
            classifier=("c0", 0.9, "v4_phase3", default_extra("R0", "T0", "P0")),
        )
    )
    cases.append(
        Case(
            name="full_phase_p0_hint_chinese",
            message="总结一下这段话的要点",
            classifier=("c0", 0.9, "v4_phase3", default_extra("R0", "T0", "P0")),
        )
    )
    cases.append(
        Case(
            name="p2_policy_no_hint_injection",
            classifier=("c2", 0.9, "v4_phase3", default_extra("R2", "T2", "P2")),
        )
    )
    cases.append(
        Case(
            name="explicit_prompt_hint_from_extra",
            classifier=(
                "c1",
                0.9,
                "v4_phase3",
                default_extra(prompt_hint="Keep the answer brief."),
            ),
        )
    )
    cases.append(
        Case(
            name="auto_thinking_disabled_no_thinking_metadata",
            router={"auto_thinking": False},
            classifier=("c3", 0.9, "v4_phase3", default_extra("R3", "T3")),
        )
    )
    cases.append(
        Case(
            name="tier_thinking_level_when_no_controller_heads",
            tiers={
                **synthetic_tiers(),
                "c3": {"model": "dummy-max-1", "thinking_level": "xhigh"},
            },
            classifier=(
                "c3",
                0.9,
                "v4_phase3",
                {"route_class": "R3", "model_version": "parity-synthetic"},
            ),
        )
    )

    # --- default-tier fallback (classifier tier unknown) -------------------------
    cases.append(
        Case(
            name="classifier_unknown_tier_falls_back_default",
            classifier=("zz_unknown", 0.9, "v4_phase3", {}),
        )
    )
    cases.append(
        Case(
            name="default_route_promotes_p0_to_p1",
            router={"default_tier": "c0"},
            classifier=("zz_unknown", 0.9, "v4_phase3", {}),
        )
    )

    # --- semantic message + deferred history plumbing ----------------------------
    cases.append(
        Case(
            name="semantic_message_drives_complaint",
            message="[wrapped] channel text without cues",
            raw_message="try again please",
        )
    )
    cases.append(
        Case(
            name="deferred_history_pending_entry",
            metadata={"_defer_squilla_router_history": True},
        )
    )

    # --- stage interaction combos ---------------------------------------------
    cases.append(
        Case(
            name="combo_gate_complaint_previous_interplay",
            message="you are wrong, redo",
            metadata={"routing_history": [hist_entry("c2", "R2", 30)]},
            classifier=("c3", 0.20, "v4_phase3", default_extra("R3", "T3")),
        )
    )
    cases.append(
        Case(
            name="combo_anti_downgrade_then_floor",
            metadata={
                "routing_history": [hist_entry("c2", "R2", 30)],
                "material_estimated_tokens": 85_000,
            },
            classifier=("c0", 0.9, "v4_phase3", default_extra("R0", "T0")),
        )
    )
    cases.append(
        Case(
            name="combo_complaint_upgrade_hits_mismatched_tier",
            message="重新回答",
            tiers=tiers_with_provider(),
            metadata={"material_estimated_tokens": 26_000},
        )
    )

    return cases


CORPUS = build_corpus()


def _scrub(value: Any) -> Any:
    """Drop monotonic ``_ts`` timestamps; everything else must be deterministic."""
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k != "_ts"}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


def _canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def observation(ctx: TurnContext, error: str | None) -> dict:
    obs = {
        "error": error,
        "model": ctx.model,
        "message_len": len(ctx.message),
        "message_tail": ctx.message[-400:],
        "metadata": _scrub(ctx.metadata),
    }
    return json.loads(_canon(obs))


def run_case(case: Case) -> dict:
    sr = squilla_router_step
    sr._history_store.clear()
    sr._strategy = None
    sr._strategy_key = None

    config = GatewayConfig()
    config.llm.provider = case.llm_provider
    config.llm.model = case.llm_model
    for key, value in {**BASE_ROUTER_KNOBS, **case.router}.items():
        setattr(config.squilla_router, key, value)
    config.squilla_router.tiers = copy.deepcopy(
        case.tiers if case.tiers is not None else synthetic_tiers()
    )
    for key, value in case.router_forced.items():
        object.__setattr__(config.squilla_router, key, value)

    metadata = copy.deepcopy(case.metadata)
    now = time.monotonic()
    for entry in metadata.get("routing_history") or []:
        offset = entry.pop("_ts_offset", None)
        if offset is not None:
            entry["_ts"] = now - float(offset)

    ctx = TurnContext(
        message=case.message,
        session_key=case.session_key,
        config=config,
        provider=None,
        model=case.llm_model,
        tool_defs=[],
        system_prompt="system",
        attachments=copy.deepcopy(case.attachments),
        metadata=metadata,
        raw_message=case.raw_message,
    )

    strategy: object
    if case.classify_expected:
        strategy = _FakeStrategy(*(case.classifier or DEFAULT_CLASSIFIER))
    else:
        strategy = _UnexpectedClassifyStrategy()

    original_get_strategy = sr._get_strategy
    original_catalog = model_catalog_module._shared_catalog
    capacity_catalog = ModelCatalog()
    capacity_catalog.set_user_overrides(
        {
            model: {
                "context_window": 300_000,
                "max_output_tokens": 10_000,
            }
            for model in (
                "dummy-nano-1",
                "dummy-mini-1",
                "dummy-pro-1",
                "dummy-max-1",
                "dummy-vision-1",
            )
        }
    )
    model_catalog_module._shared_catalog = capacity_catalog
    sr._get_strategy = lambda _config: strategy  # type: ignore[assignment]
    error: str | None = None
    try:
        asyncio.run(sr.apply_squilla_router(ctx))
    except Exception as exc:  # noqa: BLE001 - the raised error is part of the contract
        error = f"{type(exc).__name__}: {exc}"
    finally:
        sr._get_strategy = original_get_strategy  # type: ignore[assignment]
        model_catalog_module._shared_catalog = original_catalog
        sr._history_store.clear()

    return observation(ctx, error)


def _load_golden() -> dict:
    assert GOLDEN_PATH.exists(), (
        f"missing golden {GOLDEN_PATH}; regenerate with "
        "`uv run python tests/test_engine/test_routing_policy_parity.py --capture` "
        "on the pre-extraction code only"
    )
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_corpus_names_are_unique_and_match_golden() -> None:
    names = [case.name for case in CORPUS]
    assert len(names) == len(set(names))
    assert set(_load_golden()) == set(names)


@pytest.mark.parametrize("case", CORPUS, ids=lambda case: case.name)
def test_routing_decision_parity(case: Case) -> None:
    observed = run_case(case)
    expected = _load_golden()[case.name]
    # Dict-level compare first for a readable pytest diff, then byte-identical
    # canonical JSON as the strict equality bar.
    assert observed == expected
    assert _canon(observed) == _canon(expected)


def _capture() -> None:
    golden = {case.name: run_case(case) for case in CORPUS}
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(
        json.dumps(golden, ensure_ascii=False, sort_keys=True, indent=1) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {GOLDEN_PATH} ({len(golden)} cases)")


if __name__ == "__main__":
    if "--capture" in sys.argv:
        _capture()
    else:
        raise SystemExit(
            "usage: uv run python tests/test_engine/test_routing_policy_parity.py --capture"
        )
