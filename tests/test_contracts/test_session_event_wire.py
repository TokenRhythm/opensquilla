"""Wire-contract freeze for streamed session event payloads.

``session.event.router_decision`` drives the router HUD (tier pill,
tier-shift highlight, scanner popover) and ``session.event`` ensemble
progress frames reveal ensemble members mid-turn, on every client surface.
Their key names are a public protocol contract (see CLAUDE.md: public RPC
field names are stable).

- Renaming or removing any frozen key is a contract break and must fail here.
- Adding a key requires deliberately extending the frozen sets in this file —
  that friction is the point: wire additions should be a conscious decision.

The shapes are frozen at the pure payload builders in channel_dispatch
(``_router_decision_payload`` / ``_ensemble_progress_payload``) over fully
synthetic engine events — no gateway, channel, or network involved.
"""

from __future__ import annotations

from opensquilla.engine.types import EnsembleProgressEvent, RouterDecisionEvent
from opensquilla.gateway.channel_dispatch import (
    _ensemble_progress_payload,
    _router_decision_payload,
)

ROUTER_DECISION_KEYS = frozenset(
    {
        "tier",
        "tier_index",
        "model",
        "baseline_model",
        "source",
        "confidence",
        "probs",
        "savings_pct",
        "fallback",
        "thinking_mode",
        "prompt_policy",
        "routing_applied",
        "rollout_phase",
        "context_window",
    }
)

ENSEMBLE_PROGRESS_KEYS = frozenset(
    {
        "event_type",
        "proposer_index",
        "proposer_label",
        "proposer_model",
        "proposer_provider",
        "sample_index",
        "elapsed_ms",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "error",
    }
)


def _synthetic_router_decision() -> RouterDecisionEvent:
    return RouterDecisionEvent(
        tier="c1",
        tier_index=1,
        model="test-provider/test-model",
        baseline_model="test-provider/test-baseline",
        source="classifier",
        confidence=0.87,
        probs=[0.05, 0.87, 0.06, 0.02],
        savings_pct=0.42,
        fallback=False,
        thinking_mode="medium",
        prompt_policy="tiered",
        routing_applied=True,
        rollout_phase="full",
        context_window=128_000,
    )


def test_router_decision_payload_keys_are_frozen() -> None:
    payload = _router_decision_payload(_synthetic_router_decision())
    assert set(payload) == ROUTER_DECISION_KEYS


def test_router_decision_payload_values_pass_through() -> None:
    # Values must arrive verbatim; the HUD renders tier/model/probs raw and
    # keys tier styling off the canonical c0-c3 strings.
    payload = _router_decision_payload(_synthetic_router_decision())
    assert payload["tier"] == "c1"
    assert payload["tier_index"] == 1
    assert payload["model"] == "test-provider/test-model"
    assert payload["baseline_model"] == "test-provider/test-baseline"
    assert payload["probs"] == [0.05, 0.87, 0.06, 0.02]
    assert payload["context_window"] == 128_000


def test_router_decision_payload_passes_optional_tier_snapshot() -> None:
    event = _synthetic_router_decision()
    event.router_tier_snapshot = {
        "version": 1,
        "request_kind": "text",
        "tiers": [
            {
                "tier": "c1",
                "model": "test-provider/test-model",
                "execution_kind": "single_model",
            }
        ],
    }

    payload = _router_decision_payload(event)
    assert payload["router_tier_snapshot"] == event.router_tier_snapshot


def test_ensemble_progress_payload_keys_are_frozen() -> None:
    event = EnsembleProgressEvent(
        event_type="proposer_done",
        proposer_index=2,
        proposer_label="proposer-3",
        proposer_model="test-provider/test-model",
        proposer_provider="test-provider",
        sample_index=1,
        elapsed_ms=1500,
        input_tokens=1200,
        output_tokens=340,
        cost_usd=0.0021,
        error="",
    )
    payload = _ensemble_progress_payload(event)
    assert set(payload) == ENSEMBLE_PROGRESS_KEYS
    assert payload["event_type"] == "proposer_done"
    assert payload["proposer_model"] == "test-provider/test-model"
