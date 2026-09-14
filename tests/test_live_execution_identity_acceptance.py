from types import SimpleNamespace

import pytest
import structlog

from opensquilla.provider.types import DoneEvent, TextDeltaEvent
from scripts import live_execution_identity_acceptance as live


@pytest.mark.parametrize("fallback", [False, True])
async def test_identity_probe_uses_real_agent_and_synthetic_provider(monkeypatch, fallback):
    class Provider:
        provider_name = "tokenrhythm"

        async def chat(self, messages, tools=None, config=None):
            yield TextDeltaEvent(text="synthetic-model")
            yield DoneEvent(model="synthetic-alias", input_tokens=42, output_tokens=3)

    monkeypatch.setattr(live, "_build_provider", lambda config: Provider())
    row = await live.run_case(
        model="synthetic-model", question="en", placeholder="synthetic-key", fallback=fallback,
    )
    assert row["completed"]
    assert row["answer"] == "synthetic-model"
    assert row["local_fault_injected"] is fallback
    assert len(row["physical_calls"]) == 1
    call = row["physical_calls"][0]
    assert call["facts_match_target"] is True
    assert '"model":"synthetic-model"' in call["facts"][0]
    assert "synthetic-failing-deployment" not in call["facts"][0]
    assert call["usage"]["reported_model"] == "synthetic-alias"
    assert call["usage"]["billed_usd"] is None
    assert call["usage"]["cache_read_observed"] is None
    assert row["execution"]["current_request"]["model"] == "synthetic-model"


@pytest.mark.parametrize(("question", "answer", "expected"), [
    ("en", "model-a", True),
    ("en", "Model A", True),
    ("en", "model-a or model-b", False),
    ("en", "tier c1", False),
    ("provider", "TokenRhythm", True),
    ("provider", "OpenAI", False),
    ("product", "OpenSquilla", True),
    ("task", "42", True),
    ("task", "[[reply_to_current]]\n42", True),
    ("task", "42; using model-a", False),
    ("json", '{"sum":42}', True),
    ("json", '{"sum":42,"model":"model-a"}', False),
])
def test_synthetic_answer_match_does_not_guess_tier_or_allow_unrelated_identity(
    question, answer, expected,
):
    assert live.answer_matches(
        answer, "model-a", question, {"model-a": ["model-a", "Model A"], "model-b": ["model-b"]},
    ) is expected


async def test_live_process_rejects_real_key_before_file_or_network_access(monkeypatch):
    monkeypatch.setenv("TOKENRHYTHM_API_KEY", "synthetic-real-key")
    with pytest.raises(ValueError, match="real_credential_must_stay_in_relay"):
        await live.run(SimpleNamespace())


def test_live_driver_is_disabled_by_default(monkeypatch, capsys):
    monkeypatch.setattr(live.sys, "argv", ["identity-probe"])
    was_configured = structlog.is_configured()
    previous_config = structlog.get_config()
    try:
        assert live.main() == 0
        assert capsys.readouterr().out == '{"enabled":false}\n'
        assert structlog.get_config() == previous_config
        assert structlog.is_configured() is was_configured
    finally:
        # Preserve the host test process even if the regression assertion fails.
        if was_configured:
            structlog.configure(**previous_config)
        else:
            structlog.reset_defaults()
