from __future__ import annotations

import pytest

from opensquilla.application.wizard import (
    WizardRegistry,
    get_wizard_registry,
    reset_wizard_registry,
)
from opensquilla.application.wizard_rpc import (
    wizard_cancel_rpc_payload,
    wizard_next_rpc_payload,
    wizard_start_rpc_payload,
    wizard_status_rpc_payload,
)
from opensquilla.gateway import wizard as gateway_wizard


def test_wizard_registry_advances_and_applies_schema_defaults() -> None:
    registry = WizardRegistry()

    wizard_id, first_step = registry.start("onboard_agent")

    assert len(wizard_id) == 8
    assert first_step.to_dict()["stepId"] == "agent_identity"

    first = registry.advance(wizard_id, {"agent_name": "cora"})
    assert first.completed is False
    assert first.next_step is not None
    assert first.next_step.step_id == "system_prompt"

    second = registry.advance(wizard_id, {"system_prompt": "Help with release work"})
    assert second.completed is False
    assert second.next_step is not None
    assert second.next_step.step_id == "defaults"

    final = registry.advance(wizard_id, {"default_model": "openai/gpt-4o-mini"})
    assert final.completed is True
    assert final.next_step is None
    assert final.result == {
        "wizardType": "onboard_agent",
        "answers": {
            "agent_name": "cora",
            "system_prompt": "Help with release work",
            "persona_tone": "professional",
            "default_model": "openai/gpt-4o-mini",
            "temperature": 7,
        },
    }


def test_onboard_agent_default_model_offers_current_anthropic_models() -> None:
    registry = WizardRegistry()
    wizard_id, _ = registry.start("onboard_agent")
    registry.advance(wizard_id, {"agent_name": "cora"})
    defaults = registry.advance(wizard_id, {"system_prompt": "Help with release work"})

    assert defaults.next_step is not None
    default_model = next(f for f in defaults.next_step.fields if f.name == "default_model")

    assert "anthropic/claude-sonnet-4-6" in default_model.choices
    assert "anthropic/claude-haiku-4-5-20251001" in default_model.choices
    assert not any("claude-3" in choice for choice in default_model.choices)

    final = registry.advance(wizard_id, {"default_model": "anthropic/claude-sonnet-4-6"})
    assert final.completed is True
    assert final.result["answers"]["default_model"] == "anthropic/claude-sonnet-4-6"


def test_wizard_registry_rejects_blank_required_answers() -> None:
    registry = WizardRegistry()
    wizard_id, _first_step = registry.start("onboard_agent")

    with pytest.raises(ValueError, match="missing required field"):
        registry.advance(wizard_id, {"agent_name": "  "})


def test_wizard_rpc_payload_helpers_own_wire_shapes() -> None:
    registry = WizardRegistry()
    wizard_id, first_step = registry.start("onboard_agent")

    started = wizard_start_rpc_payload(wizard_id, first_step)
    assert started["wizardId"] == wizard_id
    assert started["step"]["stepId"] == "agent_identity"

    outcome = registry.advance(wizard_id, {"agent_name": "cora"})
    assert outcome.next_step is not None
    advanced = wizard_next_rpc_payload(outcome)
    assert advanced == {
        "step": outcome.next_step.to_dict(),
        "completed": False,
        "result": None,
    }
    assert wizard_status_rpc_payload(registry.status(wizard_id), total_steps=3) == {
        "wizardId": wizard_id,
        "wizardType": "onboard_agent",
        "currentStepId": "system_prompt",
        "totalSteps": 3,
        "startedAt": registry.status(wizard_id).started_at,
        "completed": False,
    }
    assert wizard_cancel_rpc_payload(wizard_id) == {
        "wizardId": wizard_id,
        "cancelled": True,
    }


def test_gateway_wizard_imports_remain_compatible_with_application_singleton() -> None:
    reset_wizard_registry()

    assert gateway_wizard.get_wizard_registry() is get_wizard_registry()

    wizard_id, _first_step = gateway_wizard.get_wizard_registry().start("onboard_agent")
    assert get_wizard_registry().status(wizard_id).wizard_type == "onboard_agent"
