"""Offline decision-model constraints, signal gating, and bounded failures."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from opensquilla.gateway.config import GatewayConfig, JevRouterConfig
from opensquilla.jev_router import JevCandidate, JevRequest, collect_jev_signals, evaluate_jev

CURRENT = JevCandidate(
    id="current",
    model="synthetic-large",
    provider="tokenrhythm",
    tier="c3",
    context_window_tokens=128_000,
    vision_support="unsupported",
    supports_tools=True,
    input_cost_per_million=10,
    output_cost_per_million=30,
)
SMALL = JevCandidate(
    id="small",
    model="synthetic-small",
    provider="tokenrhythm",
    tier="c0",
    context_window_tokens=32_000,
    vision_support="unsupported",
    supports_tools=False,
    input_cost_per_million=1,
    output_cost_per_million=3,
)
VISION = JevCandidate(
    id="vision",
    model="synthetic-vision",
    provider="tokenrhythm",
    tier="c2",
    context_window_tokens=256_000,
    vision_support="supported",
    supports_tools=True,
)
FUSION = JevCandidate(
    id="ensemble:configured", kind="ensemble", model=CURRENT.model,
    provider=CURRENT.provider, tier="c4", context_window_tokens=128_000,
    supports_tools=True, vision_support="unsupported", ensemble={
        "tier": "c4", "label": "Multi-model fusion", "strategy": "custom_b5",
        "proposers": [
            {"provider": "tokenrhythm", "model": "synthetic-analyst-a"},
            {"provider": "tokenrhythm", "model": "synthetic-analyst-b"},
        ],
        "aggregator": {"provider": CURRENT.provider, "model": CURRENT.model},
        "configurationFingerprint": "synthetic-lineup-fingerprint",
    },
)


def request(**changes):
    return replace(
        JevRequest(
            message="Give a short greeting.",
            current_model=CURRENT.model,
            current_provider=CURRENT.provider,
            candidates=(CURRENT, SMALL, VISION),
        ),
        **changes,
    )


def decision(**changes):
    return {
        "action": "switch",
        "candidate_id": "small",
        "title": "A simpler model fits",
        "reason": "This brief task may use the less expensive configured model.",
        "confidence": 0.9,
        "signal": "cost_fit",
        "phase": "unknown",
        "evidence": ["The task is short; both configured prices are known."],
        **changes,
    }


class Provider:
    def __init__(self, result=None, *, fail=False, wait=False, done=True, refusal=False):
        self.result = decision() if result is None else result
        self.fail, self.wait, self.done, self.refusal = fail, wait, done, refusal
        self.calls = []
        self.closed = False

    async def decide(self, payload, *, timeout_seconds):
        self.calls.append(payload)
        try:
            if self.wait:
                await asyncio.sleep(5)
            if self.fail:
                raise RuntimeError("synthetic upstream detail must not be persisted")
            if not self.done or self.refusal:
                return {"model": "NeoHorse-Jev-4B"}
            if not isinstance(self.result, dict):
                raise ValueError("invalid native response")
            selected = {
                "target": self.result.get("single_candidate_id", (
                    CURRENT.id if self.result.get("candidate_id") == FUSION.id
                    else self.result.get("candidate_id")
                )),
                "fusion_benefit": self.result.get("fusion_benefit", (
                    "fusion" if self.result.get("candidate_id") == FUSION.id else "single"
                )),
                "session_continuity": self.result.get("session_continuity", "keep_context"),
                "action": self.result.get("action"),
                "signal": self.result.get("signal"),
                "phase": self.result.get("phase"),
            }
            answers = {}
            for key, question in payload["questions"].items():
                if question["type"] == "noul":
                    fusion_vote = selected["fusion_benefit"] == "fusion"
                    score = self.result.get("fusion_benefit_probability", 0.95)
                    value = self.result.get(
                        "verification_noul", score if fusion_vote else 1 - score,
                    )
                    answers[key] = {"type": "noul", "noul": value,
                                    "probabilities": {"false": 1 - value, "true": value}}
                    continue
                if question["type"] == "score":
                    high = self.result.get("reasoning_probability", 0.95)
                    distribution = {"0": 0, "1": 0, "2": 1 - high, "3": high}
                    answers[key] = {"type": "score", "score": 2 + high,
                                    "probabilities": distribution}
                    continue
                choices = question["criteria"]
                choice = selected[key]
                total = len(choices)
                selected_probability = self.result.get(
                    f"{key}_probability", self.result.get("probability", 0.95),
                )
                probabilities = {
                    candidate: (
                        selected_probability if candidate == choice
                        else (1 - selected_probability) / max(1, total - 1)
                    )
                    for candidate in choices
                }
                if total == 1 and choice in choices:
                    probabilities[choice] = 1.0
                answers[key] = {
                    "type": "choice",
                    "choice": choice,
                    "probabilities": probabilities,
                    "confidence": self.result.get("confidence"),
                }
            return {
                "model": "NeoHorse-Jev-4B",
                "answers": answers,
                "input_tokens": 12,
                "usage": {"output_tokens": 25},
            }
        finally:
            self.closed = True


async def test_native_jev_can_choose_fusion_above_the_single_model_tiers(settings):
    from opensquilla.router_tiers import TEXT_TIERS

    provider = Provider(decision(candidate_id=FUSION.id))
    result = await evaluate_jev(request(
        message="Analyze incompatible system designs independently and verify a synthesis.",
        candidates=(CURRENT, SMALL, FUSION), assessment_stage="input",
    ), settings, provider=provider)
    assert result.status == "decided"
    assert result.decision.action == "ensemble"
    assert result.decision.candidate_id == FUSION.id
    questions = provider.calls[0]["questions"]
    assert list(questions) == ["target", "independent_verification", "reasoning_demand"]
    assert FUSION.id not in questions["target"]["criteria"]
    assert questions["independent_verification"]["type"] == "noul"
    assert questions["reasoning_demand"]["type"] == "score"
    assert len(questions["reasoning_demand"]["criteria"]) == 4
    assert TEXT_TIERS == ("c0", "c1", "c2", "c3")


async def test_current_fusion_is_not_recommended_again(settings):
    provider = Provider(decision(candidate_id=FUSION.id))
    result = await evaluate_jev(request(
        candidates=(CURRENT, SMALL, FUSION), current_execution_mode="ensemble",
        assessment_stage="input",
    ), settings, provider=provider)
    assert result.decision.action == "keep"
    questions = provider.calls[0]["questions"]
    criteria = questions["target"]["criteria"]
    assert json.loads(provider.calls[0]["state"])["current_execution_mode"] == "ensemble"
    assert '"is_current": false' in criteria[CURRENT.id]


async def test_fusion_can_return_to_its_aggregator_as_a_single_model(settings):
    result = await evaluate_jev(request(
        candidates=(CURRENT, SMALL, FUSION), current_execution_mode="ensemble",
        assessment_stage="input",
    ), settings, provider=Provider(decision(candidate_id=CURRENT.id)))
    assert result.decision.action == "switch"
    assert result.decision.candidate_id == CURRENT.id


@pytest.mark.parametrize("modality", ["image", "audio", "video", "document"])
async def test_text_fusion_is_not_admitted_for_attachments(settings, modality):
    provider = Provider(decision(candidate_id=FUSION.id))
    result = await evaluate_jev(request(
        candidates=(CURRENT, FUSION), attachment_modalities=(modality,),
        assessment_stage="input",
    ), settings, provider=provider)
    assert result.status == "decided" and result.decision.action != "ensemble"
    assert "independent_verification" not in provider.calls[0]["questions"]


async def test_tool_fusion_requires_verified_aggregator_support(settings):
    provider = Provider(decision(candidate_id=FUSION.id))
    result = await evaluate_jev(request(
        candidates=(CURRENT, replace(FUSION, supports_tools=None)), requires_tools=True,
        assessment_stage="input",
    ), settings, provider=provider)
    assert result.status == "decided" and result.decision.action != "ensemble"
    assert "independent_verification" not in provider.calls[0]["questions"]


@pytest.mark.parametrize(
    ("reasoning_score", "fusion_score", "expected"),
    [(0.9, 0.82, "ensemble"), (0.82, 0.9, "ensemble"), (0.9, 0.6, "switch"),
     (0.6, 0.9, "switch")],
)
async def test_fusion_requires_two_independent_native_thresholds(
    settings, reasoning_score, fusion_score, expected,
):
    result = await evaluate_jev(request(
        current_model=SMALL.model, candidates=(SMALL, CURRENT, FUSION), assessment_stage="input",
    ), settings, provider=Provider(decision(
        candidate_id=FUSION.id, reasoning_probability=reasoning_score,
        fusion_benefit_probability=fusion_score,
    )))
    assert result.status == "decided" and result.decision.action == expected
    assert result.decision.confidence == (
        min(reasoning_score, fusion_score) if expected == "ensemble" else 0.95
    )


async def test_model_choice_posterior_is_not_used_as_fusion_confidence(settings):
    result = await evaluate_jev(request(
        current_model=SMALL.model, candidates=(SMALL, CURRENT, FUSION), assessment_stage="input",
    ), settings, provider=Provider(decision(
        candidate_id=FUSION.id, target_probability=0.55,
        reasoning_probability=0.9, verification_noul=0.99,
    )))
    assert result.status == "decided" and result.decision.action == "ensemble"
    assert result.decision.confidence == 0.9


@pytest.mark.parametrize(
    "change", [
        lambda value: value["answers"]["independent_verification"].update(noul=float("nan")),
        lambda value: value["answers"]["independent_verification"].update(confidence=True),
        lambda value: value["answers"]["independent_verification"].update(
            probabilities={"false": 0.9, "true": 0.1},
        ),
        lambda value: value["answers"]["reasoning_demand"].update(score=0.1),
        lambda value: value["answers"]["reasoning_demand"]["probabilities"].update({"4": 0.0}),
        lambda value: value["answers"]["reasoning_demand"]["probabilities"].update({"3": -0.1}),
        lambda value: value["answers"]["reasoning_demand"].update(type="choice"),
    ],
)
async def test_invalid_native_noul_or_score_cannot_authorize_fusion(settings, change):
    class Malformed(Provider):
        async def decide(self, payload, *, timeout_seconds):
            value = await super().decide(payload, timeout_seconds=timeout_seconds)
            change(value)
            return value

    result = await evaluate_jev(request(
        candidates=(CURRENT, SMALL, FUSION), assessment_stage="input",
    ), settings, provider=Malformed(decision(candidate_id=FUSION.id)))
    assert result.status == "invalid" and result.decision is None


async def test_highest_rating_probability_needs_a_consistent_ordinal_expectation(settings):
    class Dispersed(Provider):
        async def decide(self, payload, *, timeout_seconds):
            value = await super().decide(payload, timeout_seconds=timeout_seconds)
            value["answers"]["reasoning_demand"] = {
                "type": "score", "score": 2.1,
                "probabilities": {"0": 0.3, "1": 0, "2": 0, "3": 0.7},
            }
            return value

    result = await evaluate_jev(request(
        current_model=SMALL.model, candidates=(SMALL, CURRENT, FUSION), assessment_stage="input",
    ), settings, provider=Dispersed(decision(candidate_id=FUSION.id)))
    assert result.decision.action == "switch" and result.decision.candidate_id == CURRENT.id


@pytest.mark.parametrize("tier", ["c0", "c1", "c2", ""])
async def test_ordinal_demand_and_verification_determine_fusion_not_model_role(settings, tier):
    chosen = replace(CURRENT, tier=tier)
    result = await evaluate_jev(request(
        current_model=SMALL.model, candidates=(SMALL, chosen, FUSION), assessment_stage="input",
    ), settings, provider=Provider(decision(candidate_id=FUSION.id)))
    assert result.status == "decided"
    assert result.decision.action == "ensemble" and result.decision.candidate_id == FUSION.id


async def test_cheaper_model_choice_does_not_override_high_native_reasoning_demand(settings):
    result = await evaluate_jev(request(
        message="Independently verify a synthetic protocol under interacting failures.",
        candidates=(CURRENT, SMALL, FUSION), assessment_stage="input",
    ), settings, provider=Provider(decision(
        candidate_id=FUSION.id, single_candidate_id=SMALL.id, target_probability=0.55,
        reasoning_probability=0.87, verification_noul=0.98,
    )))
    assert result.status == "decided" and result.decision.action == "ensemble"
    assert result.decision.candidate_id == FUSION.id
    assert result.decision.confidence == 0.87


@pytest.mark.parametrize(
    "distribution", [
        {"0": 0.99, "1": 0, "2": 0, "3": 0.01},
        {"0": 0, "1": 0.01, "2": 0.98, "3": 0.01},
    ],
)
async def test_strong_single_model_vote_does_not_make_low_or_medium_demand_fusion(
    settings, distribution,
):
    class BoundedDemand(Provider):
        async def decide(self, payload, *, timeout_seconds):
            value = await super().decide(payload, timeout_seconds=timeout_seconds)
            value["answers"]["reasoning_demand"] = {
                "type": "score", "probabilities": distribution,
                "score": sum(int(key) * probability for key, probability in distribution.items()),
            }
            return value

    result = await evaluate_jev(request(
        current_model=SMALL.model, candidates=(SMALL, CURRENT, FUSION), assessment_stage="input",
    ), settings, provider=BoundedDemand(decision(candidate_id=FUSION.id)))
    assert result.status == "decided" and result.decision.action == "switch"
    assert result.decision.candidate_id == CURRENT.id


async def test_single_hard_problem_does_not_require_fusion(settings):
    result = await evaluate_jev(request(
        current_model=SMALL.model, candidates=(SMALL, CURRENT, FUSION), assessment_stage="input",
    ), settings, provider=Provider(decision(candidate_id=CURRENT.id, fusion_benefit="single")))
    assert result.decision.action == "switch" and result.decision.candidate_id == CURRENT.id


@pytest.mark.parametrize(("target_score", "strategy_score"), [(0.6, 0.95), (0.95, 0.6)])
async def test_fusion_downshift_requires_clear_model_and_strategy_votes(
    settings, target_score, strategy_score,
):
    result = await evaluate_jev(request(
        candidates=(CURRENT, SMALL, FUSION), current_execution_mode="ensemble",
        assessment_stage="input",
    ), settings, provider=Provider(decision(
        candidate_id=SMALL.id, target_probability=target_score,
        fusion_benefit="single", fusion_benefit_probability=strategy_score,
    )))
    assert result.status == "decided" and result.decision.action == "keep"


async def test_malformed_fusion_vote_cannot_authorize_any_model_change(settings):
    class MissingStrategy(Provider):
        async def decide(self, payload, *, timeout_seconds):
            value = await super().decide(payload, timeout_seconds=timeout_seconds)
            value["answers"].pop("independent_verification")
            return value

    result = await evaluate_jev(request(
        current_model=SMALL.model, candidates=(SMALL, CURRENT, FUSION), assessment_stage="input",
    ), settings, provider=MissingStrategy(decision(candidate_id=FUSION.id)))
    assert result.status == "invalid" and result.decision is None


async def test_continuity_and_fusion_are_separate_answers_in_the_same_native_call(settings):
    from opensquilla.jev_context import ContextRestartAssessment

    assessment = ContextRestartAssessment(
        completed_turns=40, history_tokens=110_000,
        context_window_tokens=128_000, threshold_tokens=102_400,
    )
    provider = Provider(decision(candidate_id=FUSION.id, session_continuity="new_session"))
    result = await evaluate_jev(request(
        candidates=(CURRENT, SMALL, FUSION), context_assessment=assessment,
    ), settings, provider=provider)
    assert len(provider.calls) == 1
    assert list(provider.calls[0]["questions"]) == [
        "target", "independent_verification", "reasoning_demand", "session_continuity",
    ]
    state = json.loads(provider.calls[0]["state"])
    assert state["session_continuity"]["completed_conversation_turns"] == 40
    assert state["session_continuity"]["history_tokens"] == 110_000
    assert result.decision.action == "ensemble"
    assert result.context_decision.action == "new_session"


@pytest.mark.parametrize("optional", [None, {}, {"type": "noul", "noul": 0.99}])
async def test_invalid_optional_continuity_does_not_invalidate_model_choice(settings, optional):
    from opensquilla.jev_context import ContextRestartAssessment

    class InvalidContinuity(Provider):
        async def decide(self, payload, *, timeout_seconds):
            value = await super().decide(payload, timeout_seconds=timeout_seconds)
            value["answers"]["session_continuity"] = optional
            return value

    result = await evaluate_jev(request(context_assessment=ContextRestartAssessment(
        completed_turns=40, history_tokens=110_000,
        context_window_tokens=128_000, threshold_tokens=102_400,
    )), settings, provider=InvalidContinuity())
    assert result.status == "decided" and result.decision.action == "switch"
    assert result.context_decision is None


async def test_model_choice_error_does_not_discard_valid_independent_continuity(settings):
    from opensquilla.jev_context import ContextRestartAssessment

    class InvalidModelChoice(Provider):
        async def decide(self, payload, *, timeout_seconds):
            value = await super().decide(payload, timeout_seconds=timeout_seconds)
            value["answers"]["target"]["choice"] = "unknown-deployment"
            return value

    result = await evaluate_jev(request(context_assessment=ContextRestartAssessment(
        completed_turns=40, history_tokens=110_000,
        context_window_tokens=128_000, threshold_tokens=102_400,
    )), settings, provider=InvalidModelChoice(decision(session_continuity="new_session")))
    assert result.status == "invalid" and result.decision is None
    assert result.context_decision.action == "new_session"


async def test_continuity_can_be_assessed_when_only_one_model_is_configured(settings):
    from opensquilla.jev_context import ContextRestartAssessment

    provider = Provider(decision(candidate_id=CURRENT.id, session_continuity="new_session"))
    result = await evaluate_jev(request(
        candidates=(CURRENT,), turn_count=40,
        recent_messages=({"role": "assistant", "content": "A coherent prior answer."},),
        context_assessment=ContextRestartAssessment(
            completed_turns=40, history_tokens=110_000,
            context_window_tokens=128_000, threshold_tokens=102_400,
        ),
    ), settings, provider=provider)
    assert len(provider.calls) == 1
    assert result.status == "decided" and result.decision.action == "keep"
    assert result.context_decision.action == "new_session"


async def test_unsolicited_continuity_answer_is_not_used_without_eligibility(settings):
    class UnrequestedContinuity(Provider):
        async def decide(self, payload, *, timeout_seconds):
            value = await super().decide(payload, timeout_seconds=timeout_seconds)
            value["answers"]["session_continuity"] = {
                "type": "choice", "choice": "new_session",
                "probabilities": {"keep_context": 0.01, "new_session": 0.99},
            }
            return value

    result = await evaluate_jev(request(), settings, provider=UnrequestedContinuity())
    assert result.status == "decided" and result.decision.action == "switch"
    assert result.context_decision is None


@pytest.fixture
def settings():
    return JevRouterConfig(enabled=True, timeout_seconds=0.2)


def test_old_configuration_keeps_jev_disabled():
    config = GatewayConfig()
    assert config.jev_router.enabled is False
    assert config.squilla_router.enabled is True
    assert config.jev_router.mode == "suggest"
    assert config.jev_router.loop_interval == 5


def test_settings_reject_invalid_mode_budget_and_blank_model():
    for value in ({"mode": "automatic"}, {"max_prompts_per_hour": -1}, {"model": " "}):
        with pytest.raises(ValueError):
            JevRouterConfig(**value)


def test_settings_can_be_enabled_through_environment(monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_JEV_ROUTER_ENABLED", "true")
    monkeypatch.setenv("OPENSQUILLA_JEV_ROUTER_MODE", "trust")
    config = JevRouterConfig()
    assert config.enabled and config.mode == "trust"


def test_long_conversation_does_not_mean_failure():
    signals = collect_jev_signals(
        request(
            message="Please continue with the next section of the approved analysis." * 8,
            turn_count=80,
            recent_messages=({"role": "assistant", "content": "Section complete."},),
        )
    )
    assert not set(signals) & {"unresolved_session", "dissatisfaction", "negative_feedback"}


def test_long_unresolved_conversation_requires_failure_evidence():
    signals = collect_jev_signals(
        request(
            turn_count=20,
            negative_feedback_count=2,
            message="Please continue.",
        )
    )
    assert "unresolved_session" in signals


@pytest.mark.parametrize(("count", "expected"), [(0, False), (1, False), (2, True), (4, True)])
def test_typed_consecutive_tool_failures_are_an_assessment_signal(count, expected):
    signals = collect_jev_signals(request(tool_failure_count=count))
    assert ("tool_failures" in signals) is expected
    assert "dissatisfaction" not in signals


async def test_environment_tool_failures_do_not_automatically_upgrade_the_model(settings):
    provider = Provider(decision(candidate_id=CURRENT.id))
    result = await evaluate_jev(request(
        message="Continue the existing synthetic task.", assessment_stage="loop",
        candidates=(CURRENT, SMALL, FUSION), tool_failure_count=2,
        recent_tool_results=("Permission denied while reading the synthetic fixture.",),
    ), settings, provider=provider)
    state = json.loads(provider.calls[0]["state"])
    assert state["tool_failure_count"] == 2
    assert "tool_failures" in state["signals"]
    assert result.status == "decided" and result.decision.action == "keep"
    instructions = provider.calls[0]["questions"]["target"]["instructions"]
    assert "environmental fix rather than a stronger model" in instructions
    assert "environmental fix rather than a stronger model" in instructions


@pytest.mark.parametrize("message", ["请用古文改写这个短段落", "Translate this into Dutch."])
def test_language_fit_can_prompt_evaluation(message):
    assert "language_fit" in collect_jev_signals(request(message=message))


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"message": "你没理解，重新来"}, "dissatisfaction"),
        ({"regeneration_count": 2}, "regeneration"),
        ({"negative_feedback_count": 1}, "negative_feedback"),
        ({"message": "按这个改，保留结尾"}, "manual_revision"),
        ({"message": "批量分类这些内容"}, "batch_task"),
        ({"context_tokens": 85_000, "context_window_tokens": 100_000}, "context_pressure"),
        ({"attachment_modalities": ("image",)}, "capability_gap"),
        (
            {
                "message": "Please debug this code",
                "candidates": (SMALL, VISION),
                "current_model": SMALL.model,
            },
            "capability_gap",
        ),
        ({"last_provider_error": "synthetic_error"}, "availability"),
        ({"budget_remaining_ratio": 0.05}, "availability"),
        ({"latency_sensitive": True}, "latency"),
        ({"message": "请开始执行", "previous_phase": "planning"}, "task_change"),
        ({"message": "Please review this proposal"}, "second_opinion"),
        ({"message": "I need medical advice"}, "high_stakes"),
        ({"message": "This is confidential"}, "privacy"),
        ({"preferred_candidate_id": "small"}, "preference"),
    ],
)
def test_specific_signals(changes, expected):
    assert expected in collect_jev_signals(request(**changes))


def test_rephrased_question_is_failure_evidence():
    signals = collect_jev_signals(
        request(
            message="Please explain why the synthetic import fails during startup again.",
            recent_messages=(
                {
                    "role": "user",
                    "content": "Please explain why the synthetic import fails during startup.",
                },
            ),
            turn_count=3,
        )
    )
    assert "repeated_request" in signals


async def test_disabled_never_calls_provider(settings):
    provider = Provider()
    result = await evaluate_jev(
        request(), settings.model_copy(update={"enabled": False}), provider=provider
    )
    assert result.status == "skipped" and not provider.calls


async def test_candidate_and_schema_bound_decision(settings):
    provider = Provider()
    result = await evaluate_jev(request(), settings, provider=provider)
    assert result.status == "decided"
    assert result.decision.candidate_id == "small"
    assert result.decision.estimated_savings_percent == 90.0
    assert result.decision.risk == "low"
    assert result.input_tokens == 12 and result.output_tokens == 25
    assert result.billed_cost == 0.0
    payload = provider.calls[0]
    assert payload["model"] == "NeoHorse-Jev-4B"
    assert set(payload) == {"model", "state", "questions"}
    assert list(payload["questions"]["target"]["criteria"]) == ["current", "small", "vision"]
    assert list(payload["questions"]) == ["target"]
    assert all(question["type"] == "choice" for question in payload["questions"].values())
    assert provider.closed


async def test_checkpoint_task_evidence_is_separately_bounded_and_redacted(settings):
    provider = Provider()
    secret = "sk-" + "a" * 48
    result = await evaluate_jev(request(
        message="Read the supplied task documents. " * 50,
        assessment_stage="loop", loop_iteration=6,
        recent_tool_results=(
            "Concurrent coordinators require a fencing proof. " + secret + " X" * 1000,
            "Earlier evidence. " * 100,
            "This third result must not be sent.",
        ),
    ), settings, provider=provider)
    assert result.status == "decided"
    payload = provider.calls[0]
    state = json.loads(payload["state"])
    assert state["assessment_stage"] == "loop"
    assert state["loop_iteration"] == 6
    assert state["recent_tool_results"][0].startswith("Concurrent coordinators")
    assert state["task"] == state["recent_tool_results"][0]
    assert state["original_user_goal"].startswith("Read the supplied task documents.")
    assert len(state["task"]) <= 500
    assert len(state["original_user_goal"]) <= 500
    assert sum(map(len, state["recent_tool_results"])) <= 700
    assert secret not in payload["state"]
    assert "third result" not in payload["state"]
    assert "remaining work" in payload["questions"]["target"]["instructions"]
    assert "first_turn" not in result.signals


async def test_confidential_tool_results_trigger_privacy_before_remote_payload(settings):
    provider = Provider(decision(candidate_id="current"))
    result = await evaluate_jev(request(
        assessment_stage="loop", recent_tool_results=("Confidential synthetic ledger.",),
    ), settings, provider=provider)
    assert result.status == "decided"
    assert "privacy" in result.signals
    state = json.loads(provider.calls[0]["state"])
    assert state["recent_tool_results"] == []
    assert state["recent_messages"] == []
    assert state["task"] == "[confidential input withheld]"
    assert state["original_user_goal"] == "[confidential input withheld]"


@pytest.mark.parametrize(
    "change",
    [
        {"candidate_id": "invented"},
        {"confidence": True},
        {"confidence": float("nan")},
        {"confidence": 2},
    ],
)
async def test_invalid_decision_cannot_route(settings, change):
    result = await evaluate_jev(request(), settings, provider=Provider(decision(**change)))
    assert result.status == "invalid" and result.decision is None
    assert result.error == "invalid_decision"


@pytest.mark.parametrize("result", ["not JSON", "[]", "null", "x" * 12_001])
async def test_malformed_or_excessive_output(settings, result):
    evaluation = await evaluate_jev(request(), settings, provider=Provider(result))
    assert evaluation.status == "invalid" and evaluation.decision is None


async def test_low_selected_probability_keeps_current_model(settings):
    result = await evaluate_jev(request(), settings, provider=Provider(decision(probability=0.5)))
    assert result.status == "decided" and result.decision.action == "keep"
    assert result.decision.candidate_id == ""
    assert result.decision.estimated_savings_percent is None
    assert result.decision.title == "Keep the current model"
    assert "keep the current model" in result.decision.reason
    assert "another model" not in result.decision.reason


async def test_timeout_is_bounded_and_closes_stream(settings):
    provider = Provider(wait=True)
    result = await evaluate_jev(request(), settings, provider=provider)
    assert result.status == "unavailable" and result.error == "timeout"
    assert result.latency_ms < 1500
    assert provider.closed


async def test_provider_error_does_not_persist_upstream_text(settings):
    result = await evaluate_jev(request(), settings, provider=Provider(fail=True))
    assert result.status == "unavailable" and result.error == "provider_unavailable"
    assert result.decision is None


@pytest.mark.parametrize("changes", [{"done": False}, {"refusal": True}])
async def test_incomplete_or_refused_stream_cannot_route(settings, changes):
    result = await evaluate_jev(request(), settings, provider=Provider(**changes))
    assert result.status == "invalid" and result.decision is None


async def test_missing_credentials_does_not_use_execution_provider(settings):
    result = await evaluate_jev(request(), settings)
    assert result.status == "unavailable" and result.error == "not_configured"


async def test_model_can_have_isolated_decision_and_execution_roles(settings):
    provider = Provider()
    result = await evaluate_jev(
        request(
            candidates=(
                CURRENT,
                replace(
                    SMALL,
                    model=settings.model,
                ),
            )
        ),
        settings,
        provider=provider,
    )
    assert result.status == "decided" and len(provider.calls) == 1
    assert result.decision.candidate_id == "small"
    assert "messages" not in provider.calls[0] and "tools" not in provider.calls[0]


@pytest.mark.parametrize(
    ("changes", "candidate"),
    [
        ({"attachment_modalities": ("image",)}, "small"),
        ({"requires_tools": True}, "small"),
        ({"context_tokens": 40_000}, "small"),
        ({"sensitive_data": True}, "small"),
    ],
)
async def test_known_incompatible_candidate_is_rejected(settings, changes, candidate):
    result = await evaluate_jev(
        request(**changes), settings, provider=Provider(decision(candidate_id=candidate))
    )
    assert result.status == "invalid" and result.decision is None


async def test_supported_image_candidate_can_be_suggested(settings):
    result = await evaluate_jev(
        request(attachment_modalities=("image",)),
        settings,
        provider=Provider(decision(candidate_id="vision")),
    )
    assert result.status == "decided" and result.decision.risk == "medium"
    assert result.decision.estimated_savings_percent is None


async def test_private_content_is_withheld_from_decision_provider(settings):
    provider = Provider(decision(candidate_id="local", signal="privacy"))
    local = replace(SMALL, id="local", provider="ollama", is_local=True)
    result = await evaluate_jev(
        request(
            message="synthetic confidential content",
            recent_messages=({"role": "user", "content": "synthetic private history"},),
            candidates=(CURRENT, local),
            sensitive_data=True,
        ),
        settings,
        provider=provider,
    )
    payload = provider.calls[0]["state"]
    assert "synthetic confidential content" not in payload
    assert "synthetic private history" not in payload
    assert result.status == "decided" and result.decision.risk == "high"


async def test_secret_looking_snippets_are_redacted(settings):
    provider = Provider()
    dummy_key = "sk_tr_" + "synthetic" * 4
    await evaluate_jev(
        request(message="Please explain this setting api_key=" + dummy_key),
        settings,
        provider=provider,
    )
    assert dummy_key not in provider.calls[0]["state"]


async def test_unknown_price_cannot_claim_savings_or_low_risk(settings):
    result = await evaluate_jev(
        request(
            candidates=(
                CURRENT,
                replace(
                    SMALL,
                    input_cost_per_million=None,
                ),
            )
        ),
        settings,
        provider=Provider(decision(signal="first_turn")),
    )
    assert result.decision.estimated_savings_percent is None
    assert result.decision.risk == "medium"


@pytest.mark.parametrize(("context_tokens", "risk"), [(2_000, "low"), (25_000, "medium")])
async def test_trusted_savings_requires_context_headroom(settings, context_tokens, risk):
    result = await evaluate_jev(
        request(context_tokens=context_tokens), settings, provider=Provider()
    )
    assert result.status == "decided" and result.decision.risk == risk


async def test_coding_task_cannot_auto_downgrade_based_only_on_price(settings):
    result = await evaluate_jev(
        request(message="Please debug this code."), settings, provider=Provider()
    )
    assert result.status == "decided" and result.decision.risk == "medium"


async def test_mixed_token_prices_cannot_promise_savings(settings):
    target = replace(SMALL, input_cost_per_million=0.1, output_cost_per_million=35)
    result = await evaluate_jev(
        request(candidates=(CURRENT, target)),
        settings,
        provider=Provider(decision(signal="first_turn")),
    )
    assert result.status == "decided"
    assert result.decision.risk == "medium"
    assert result.decision.estimated_savings_percent is None


async def test_savings_uses_less_favorable_token_price_improvement(settings):
    target = replace(SMALL, input_cost_per_million=2, output_cost_per_million=3)
    result = await evaluate_jev(
        request(candidates=(CURRENT, target)), settings, provider=Provider()
    )
    assert result.decision.estimated_savings_percent == 80


async def test_high_stakes_cannot_claim_low_risk(settings):
    result = await evaluate_jev(
        request(message="Please give medical advice."),
        settings,
        provider=Provider(decision(signal="high_stakes")),
    )
    assert result.decision.risk == "high"


async def test_native_client_receives_resolved_credentials_without_mutating_config(
    settings, monkeypatch
):
    from opensquilla.provider.selector import ProviderConfig

    provider = Provider()
    seen = []

    def build(config):
        seen.append(config)
        return provider

    monkeypatch.setattr("opensquilla.provider.jev_decision.NativeJevClient", build)
    configured = ProviderConfig(
        provider="tokenrhythm",
        model="synthetic-chat",
        api_key="synthetic-dummy-key",
        base_url="https://example.test/v1",
    )
    result = await evaluate_jev(request(), settings, provider_config=configured)
    assert result.status == "decided"
    assert seen[0].model == "synthetic-chat"
    assert provider.calls[0]["model"] == "NeoHorse-Jev-4B"
    assert seen[0].api_key == "synthetic-dummy-key"
    assert configured.model == "synthetic-chat"


async def test_callers_can_use_structural_settings_without_gateway_dependency():
    settings = SimpleNamespace(
        enabled=True, model="NeoHorse-Jev-4B", timeout_seconds=0.2, confidence_threshold=0.65
    )
    result = await evaluate_jev(request(), settings, provider=Provider())
    assert result.status == "decided"


@pytest.mark.parametrize("stage", ["input", "loop"])
async def test_explicit_assessment_runs_without_heuristic_signal(settings, stage):
    provider = Provider(
        decision(
            action="keep",
            candidate_id="current",
            signal="input_assessment" if stage == "input" else "agent_checkpoint",
        )
    )
    result = await evaluate_jev(
        request(
            message="Continue.",
            recent_messages=({"role": "assistant", "content": "Ready"},),
            turn_count=3,
            candidates=(CURRENT, VISION),
            assessment_stage=stage,
        ),
        settings,
        provider=provider,
    )
    assert result.status == "decided" and provider.calls


async def test_old_chat_model_never_falls_back_to_chat_inference(settings):
    provider = Provider()
    result = await evaluate_jev(
        request(), settings.model_copy(update={"model": "synthetic-chat"}), provider=provider
    )
    assert result.status == "unavailable" and result.error == "native_jev_required"
    assert not provider.calls


async def test_choosing_current_candidate_is_the_keep_decision(settings):
    result = await evaluate_jev(
        request(), settings, provider=Provider(decision(candidate_id="current"))
    )
    assert result.status == "decided" and result.decision.action == "keep"


async def test_model_selection_is_one_coherent_choice(settings):
    provider = Provider()
    result = await evaluate_jev(request(), settings, provider=provider)
    assert list(provider.calls[0]["questions"]) == ["target"]
    assert result.decision.action == "switch"
    assert result.decision.candidate_id == "small"


async def test_implicit_input_uses_configured_task_roles_in_one_native_choice(settings):
    provider = Provider()
    text = "Sort the supplied product labels alphabetically."
    result = await evaluate_jev(
        request(message=text, assessment_stage="input"), settings, provider=provider,
    )
    payload = provider.calls[0]
    choices = payload["questions"]["target"]["criteria"]
    assert choices["small"].startswith("Configured task role c0: Routine, mechanical work")
    assert choices["current"].startswith("Configured task role c3: Difficult reasoning")
    assert "not benchmark guarantees" in payload["questions"]["target"]["instructions"]
    assert "No request to switch models is needed" in payload["questions"]["target"]["instructions"]
    assert json.loads(payload["state"])["task"] == text
    assert result.decision.action == "switch"
    assert result.decision.signal == "cost_fit"
    assert "lower published token prices" in result.decision.reason
    assert len(provider.calls) == 1


async def test_implicit_upgrade_explains_reasoning_role_without_price_claim(settings):
    provider = Provider(decision(candidate_id="current"))
    text = "Find a counterexample under simultaneous partial failures and justify the invariant."
    result = await evaluate_jev(
        request(
            message=text, current_model=SMALL.model, current_provider=SMALL.provider,
            assessment_stage="input",
        ), settings, provider=provider,
    )
    assert result.decision.action == "switch"
    assert result.decision.candidate_id == "current"
    assert result.decision.signal == "reasoning_fit"
    assert "reasoning_fit" in result.signals
    assert "deep reasoning" in result.decision.reason
    assert "configured deployment" in result.decision.reason
    assert "lower" not in result.decision.reason
    assert result.decision.estimated_savings_percent is None


async def test_configured_role_aliases_are_normalized_without_assuming_unknown_roles(settings):
    provider = Provider()
    unknown = replace(VISION, tier="unranked", input_cost_per_million=None)
    await evaluate_jev(request(candidates=(
        replace(CURRENT, tier="t3"), replace(SMALL, tier="t0"), unknown,
    )), settings, provider=provider)
    choices = provider.calls[0]["questions"]["target"]["criteria"]
    current_facts = json.loads(choices["current"].split("Deployment facts: ", 1)[1])
    assert current_facts["tier"] == "c3"
    assert current_facts["is_current"] is True
    assert choices["vision"].startswith("No configured task role")
    unknown_facts = json.loads(choices["vision"].split("Deployment facts: ", 1)[1])
    assert unknown_facts["tier"] == ""
    assert unknown_facts["input_cost_per_million"] is None


def test_long_routine_input_can_expose_a_price_gap_without_claiming_failure():
    signals = collect_jev_signals(request(
        message="Normalize each synthetic record to lowercase. " + "Item, " * 200,
        turn_count=5,
    ))
    assert "cost_fit" in signals
    assert "dissatisfaction" not in signals
    assert "unresolved_session" not in signals


async def test_explicit_independent_review_uses_the_chosen_alternative(settings):
    result = await evaluate_jev(
        request(message="Please review this draft with another model."),
        settings, provider=Provider(),
    )
    assert result.decision.action == "review"
    assert result.decision.signal == "second_opinion"


async def test_simple_cost_fit_has_concrete_reason_instead_of_initial_input_label(settings):
    result = await evaluate_jev(
        request(assessment_stage="input"), settings, provider=Provider(),
    )
    assert result.decision.signal == "cost_fit"



@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update(model="synthetic-chat"),
        lambda value: value.update(answers={}),
        lambda value: value["answers"]["target"].update(probabilities={"small": 1.0}),
        lambda value: value["answers"]["target"]["probabilities"].update(current=0.8),
        lambda value: value["answers"]["target"]["probabilities"].update(small=float("nan")),
        lambda value: value["answers"]["target"].update(type="noul"),
        lambda value: value["answers"]["target"].update(choice="current"),
    ],
)
async def test_native_distribution_must_be_complete_finite_and_consistent(settings, change):
    class Malformed(Provider):
        async def decide(self, payload, *, timeout_seconds):
            value = await super().decide(payload, timeout_seconds=timeout_seconds)
            change(value)
            return value

    result = await evaluate_jev(request(), settings, provider=Malformed())
    assert result.status == "invalid" and result.decision is None


async def test_native_usage_does_not_add_expanded_and_packed_input_counts(settings):
    class Usage(Provider):
        async def decide(self, payload, *, timeout_seconds):
            value = await super().decide(payload, timeout_seconds=timeout_seconds)
            value["input_details"] = {"expanded_tokens": 48}
            return value

    result = await evaluate_jev(request(), settings, provider=Usage())
    assert result.input_tokens == 12
    assert result.billed_cost == 0


async def test_native_choice_probability_supports_responses_without_confidence(settings):
    class DistributionOnly(Provider):
        async def decide(self, payload, *, timeout_seconds):
            response = await super().decide(payload, timeout_seconds=timeout_seconds)
            for answer in response["answers"].values():
                answer.pop("confidence")
            return response

    result = await evaluate_jev(
        request(candidates=(CURRENT, SMALL)), settings,
        provider=DistributionOnly(decision(probability=0.7)),
    )
    assert result.status == "decided"
    assert result.decision.action == "switch"
    assert result.decision.confidence == 0.7


async def test_native_decimal_string_distribution_is_supported(settings):
    class DecimalDistribution(Provider):
        async def decide(self, payload, *, timeout_seconds):
            response = await super().decide(payload, timeout_seconds=timeout_seconds)
            answer = response["answers"]["target"]
            answer.pop("confidence")
            answer["probabilities"] = {"current": "0.30000000", "small": "0.70000000"}
            return response

    result = await evaluate_jev(
        request(candidates=(CURRENT, SMALL)), settings, provider=DecimalDistribution(),
    )
    assert result.status == "decided"
    assert result.decision.action == "switch"
    assert result.decision.candidate_id == "small"
    assert result.decision.confidence == 0.7


@pytest.mark.parametrize(
    "invalid", ["NaN", "Infinity", "true", "0x1", "1_0", "1e999", "-0.1", "1.1", " "],
)
async def test_native_probability_strings_must_be_bounded_finite_decimals(settings, invalid):
    class InvalidDecimal(Provider):
        async def decide(self, payload, *, timeout_seconds):
            response = await super().decide(payload, timeout_seconds=timeout_seconds)
            response["answers"]["target"]["probabilities"]["small"] = invalid
            return response

    result = await evaluate_jev(request(), settings, provider=InvalidDecimal())
    assert result.status == "invalid" and result.decision is None


async def test_optional_confidence_does_not_replace_routing_distribution_score(settings):
    result = await evaluate_jev(
        request(), settings, provider=Provider(decision(confidence=0.2)),
    )
    assert result.status == "decided"
    assert result.decision.action == "switch"
    assert result.decision.confidence == 0.95
