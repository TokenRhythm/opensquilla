"""Tests for secret redaction helpers."""

from opensquilla.onboarding.redaction import (
    REDACTED_PLACEHOLDER,
    redact_channel_entry,
    redact_memory_embedding_payload,
    redact_provider_payload,
)


def test_provider_api_key_is_redacted():
    out = redact_provider_payload({"api_key": "sk-secret", "model": "x"})
    assert out["api_key"] == REDACTED_PLACEHOLDER
    assert out["model"] == "x"


def test_empty_api_key_redacts_to_empty_string():
    out = redact_provider_payload({"api_key": "", "model": "x"})
    assert out["api_key"] == ""


def test_memory_embedding_remote_key_is_redacted():
    out = redact_memory_embedding_payload(
        {"provider": "openai", "remote": {"api_key": "mem-secret"}}
    )
    assert out["remote"]["api_key"] == REDACTED_PLACEHOLDER


def test_telegram_secrets_are_redacted():
    out = redact_channel_entry(
        "telegram",
        {"name": "tg", "token": "abcd", "webhook_secret_token": "wxyz"},
    )
    assert out["token"] == REDACTED_PLACEHOLDER
    assert out["webhook_secret_token"] == REDACTED_PLACEHOLDER
    assert out["name"] == "tg"


def test_unknown_channel_type_redacts_secret_shaped_keys():
    # No setup spec (entry-point plugin adapter): fail closed by masking
    # anything secret-shaped instead of returning credentials verbatim.
    payload = {"name": "x", "token": "y", "api_key": "z", "appToken": "t", "port": 8080}
    out = redact_channel_entry("not-a-type", payload)
    assert out["token"] == REDACTED_PLACEHOLDER
    assert out["api_key"] == REDACTED_PLACEHOLDER
    assert out["appToken"] == REDACTED_PLACEHOLDER
    assert out["name"] == "x"
    assert out["port"] == 8080


def test_input_dict_is_not_mutated():
    src = {"api_key": "s", "model": "m"}
    out = redact_provider_payload(src)
    assert src["api_key"] == "s"
    assert out["api_key"] == REDACTED_PLACEHOLDER
