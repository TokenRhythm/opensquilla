"""Unit tests for the code-task provider preflight + mid-run error mapping."""

from opensquilla.contrib.codetask import preflight as preflight_mod
from opensquilla.contrib.codetask.agent_config import build_per_run_agent_config
from opensquilla.contrib.codetask.preflight import provider_block_reason, provider_preflight
from opensquilla.onboarding.probe import ProviderProbeResult
from opensquilla.provider.failures import ProviderFailureKind

_TEMPLATE = {
    "workspace_strict": False,
    "sandbox": {"sandbox": False},
    "tools": {"deny": ["memory*"]},
    "meta_skill": {"enabled": False},
}


def _bundle(user):
    return build_per_run_agent_config(dict(_TEMPLATE), user)


def _probe(monkeypatch, result=None, *, calls=None):
    async def _fake(*, provider_id, model, **kw):
        if calls is not None:
            calls.append({"provider_id": provider_id, "model": model, **kw})
        return result or ProviderProbeResult(ok=True, provider_id=provider_id, model=model)

    monkeypatch.setattr(preflight_mod, "probe_llm_provider", _fake)


def test_preflight_passes_on_ok_probe(monkeypatch):
    calls = []
    _probe(monkeypatch, calls=calls)
    bundle = _bundle({"llm": {"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-x"}})
    ok, reason = provider_preflight(bundle)
    assert ok is True and reason == ""
    # Probes the operator's resolved provider/model with the transported key.
    assert calls[0]["provider_id"] == "deepseek"
    assert calls[0]["model"] == "deepseek-chat"
    assert calls[0]["api_key"] == "sk-x"


def test_preflight_blocks_on_missing_key(monkeypatch):
    _probe(
        monkeypatch,
        ProviderProbeResult(
            ok=False,
            provider_id="deepseek",
            model="deepseek-chat",
            failure_kind=ProviderFailureKind.AUTH_INVALID.value,
            message="No API key available (checked $DEEPSEEK_API_KEY).",
        ),
    )
    bundle = _bundle({"llm": {"provider": "deepseek", "model": "deepseek-chat"}})
    ok, reason = provider_preflight(bundle)
    assert ok is False
    assert "deepseek" in reason and "opensquilla onboard" in reason


def test_preflight_does_not_use_generic_key_when_explicit_env_is_missing(monkeypatch):
    monkeypatch.delenv("NEW_ENDPOINT_KEY", raising=False)
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "synthetic-generic-key")
    calls = []
    _probe(
        monkeypatch,
        ProviderProbeResult(
            ok=False,
            provider_id="openrouter",
            model="openai/gpt-test",
            failure_kind=ProviderFailureKind.AUTH_INVALID.value,
            message="No API key available (checked $NEW_ENDPOINT_KEY).",
        ),
        calls=calls,
    )
    bundle = _bundle(
        {
            "llm": {
                "provider": "openrouter",
                "model": "openai/gpt-test",
                "api_key_env": "NEW_ENDPOINT_KEY",
                "base_url": "https://openrouter.ai/api/v1",
            }
        }
    )

    ok, reason = provider_preflight(bundle)

    assert ok is False
    assert "openrouter" in reason and "opensquilla onboard" in reason
    assert calls[0]["api_key"] == ""


def test_preflight_blocks_on_insufficient_credits(monkeypatch):
    _probe(
        monkeypatch,
        ProviderProbeResult(
            ok=False,
            provider_id="deepseek",
            model="deepseek-chat",
            failure_kind=ProviderFailureKind.INSUFFICIENT_CREDITS.value,
            message="insufficient balance",
        ),
    )
    bundle = _bundle({"llm": {"provider": "deepseek", "model": "deepseek-chat", "api_key": "k"}})
    ok, _reason = provider_preflight(bundle)
    assert ok is False


def test_preflight_fails_open_on_transient(monkeypatch):
    # A transport blip must not block a run that might otherwise succeed.
    _probe(
        monkeypatch,
        ProviderProbeResult(
            ok=False,
            provider_id="deepseek",
            model="deepseek-chat",
            failure_kind=ProviderFailureKind.TRANSPORT_TRANSIENT.value,
            message="timeout",
        ),
    )
    bundle = _bundle({"llm": {"provider": "deepseek", "model": "deepseek-chat", "api_key": "k"}})
    ok, reason = provider_preflight(bundle)
    assert ok is True and reason == ""


def test_preflight_skips_keyless_provider(monkeypatch):
    calls = []
    _probe(monkeypatch, calls=calls)
    bundle = _bundle({"llm": {"provider": "ollama", "model": "llama3", "base_url": "http://x"}})
    ok, reason = provider_preflight(bundle)
    assert ok is True and reason == ""
    assert calls == []  # keyless: no network probe at all


def test_preflight_skips_cross_provider_tier_configs(monkeypatch):
    # Cross-provider tiers resolve credentials per tier from profiles/pools the
    # primary-provider probe can't see, so the preflight must not false-block
    # a keyless primary there — skip without probing.
    calls = []
    _probe(monkeypatch, calls=calls)
    bundle = _bundle(
        {
            "llm": {"provider": "deepseek", "model": "deepseek-chat"},
            "squilla_router": {"enabled": True, "cross_provider_tiers": True},
        }
    )
    ok, reason = provider_preflight(bundle)
    assert ok is True and reason == ""
    assert calls == []


def test_preflight_fails_open_on_probe_exception(monkeypatch):
    async def _boom(**_kw):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(preflight_mod, "probe_llm_provider", _boom)
    bundle = _bundle({"llm": {"provider": "deepseek", "model": "deepseek-chat", "api_key": "k"}})
    ok, _reason = provider_preflight(bundle)
    assert ok is True


def test_provider_block_reason_matches_credential_codes():
    # The engine emits code="no_provider" and str(status_code) for HTTP errors.
    assert provider_block_reason([{"code": "no_provider", "message": "none"}]) is not None
    assert provider_block_reason([{"code": "402", "message": "no credits"}]) is not None
    assert provider_block_reason([{"code": "401"}]) is not None
    assert provider_block_reason([{"code": "403"}]) is not None


def test_provider_block_reason_ignores_non_credential_codes():
    assert provider_block_reason([{"code": "429", "message": "rate limited"}]) is None
    assert provider_block_reason([{"code": "tool_error"}]) is None
    assert provider_block_reason([]) is None
    # Non-dict items are tolerated (defensive) rather than crashing.
    assert provider_block_reason(["oops", None]) is None
