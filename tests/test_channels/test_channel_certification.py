from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

import opensquilla.onboarding.channel_certification as certification
from opensquilla.cli.main import app
from opensquilla.onboarding.channel_certification import (
    CertificationUsageError,
    _delivery_message,
    certification_env_name,
    certification_environment,
    certify_channels,
    parse_targets,
    redact_evidence,
)


class _FakeAdapter:
    def __init__(self, probe_result: dict[str, Any] | None = None) -> None:
        self.probe_result = probe_result or {"authenticated": True, "bot_user_id": "bot-1"}
        self.probe_calls = 0
        self.sent = []
        self.stopped = False

    async def probe_connection(self) -> dict[str, Any]:
        self.probe_calls += 1
        return self.probe_result

    async def send(self, message) -> None:
        self.sent.append(message)

    async def stop(self) -> None:
        self.stopped = True


def _telegram_env(secret: str = "123456:secret-value") -> dict[str, str]:
    return {certification_env_name("telegram", "token"): secret}


def test_certification_environment_only_lists_provider_adapter_fields() -> None:
    environment = certification_environment("telegram")

    assert "token" in environment
    assert {
        "group_session_scope",
        "busy_input_mode",
        "dm_access",
        "allowed_senders",
    }.isdisjoint(environment)


@pytest.mark.asyncio
async def test_safe_probe_uses_environment_and_redacts_all_evidence(monkeypatch) -> None:
    secret = "123456:secret-value"
    adapter = _FakeAdapter(
        {
            "authenticated": True,
            "token": secret,
            "debug": f"authorization={secret}",
            "endpoint": f"https://api.telegram.org/bot{secret}/getMe",
        }
    )
    monkeypatch.setattr(certification, "build_managed_channel", lambda entry: adapter)

    evidence = await certify_channels(["telegram"], environ=_telegram_env(secret))

    assert evidence["summary"] == {"total": 1, "passed": 1, "failed": 0}
    row = evidence["providers"][0]
    assert row["status"] == "verified"
    assert row["operation"] == "safe_auth_probe"
    assert row["suppliedFields"] == ["token"]
    assert adapter.probe_calls == 1
    assert adapter.sent == []
    assert adapter.stopped is True
    rendered = json.dumps(evidence)
    assert secret not in rendered
    assert "[REDACTED]" in rendered


@pytest.mark.asyncio
async def test_missing_credentials_are_reported_without_building_adapter(monkeypatch) -> None:
    def _unexpected_build(entry):
        raise AssertionError("adapter must not be built")

    monkeypatch.setattr(certification, "build_managed_channel", _unexpected_build)

    evidence = await certify_channels(["telegram"], environ={})

    row = evidence["providers"][0]
    assert row["status"] == "missing_credentials"
    assert row["missingEnvironment"] == [
        "OPENSQUILLA_CHANNEL_CERT_TELEGRAM_TOKEN"
    ]
    assert "token" not in row


@pytest.mark.asyncio
async def test_adapter_construction_failure_is_redacted(monkeypatch) -> None:
    secret = "constructor-secret"

    def _failing_build(entry):
        raise RuntimeError(f"SDK rejected credential={secret}")

    monkeypatch.setattr(certification, "build_managed_channel", _failing_build)

    evidence = await certify_channels(["telegram"], environ=_telegram_env(secret))

    row = evidence["providers"][0]
    assert row["status"] == "invalid_config"
    assert secret not in json.dumps(evidence)


@pytest.mark.asyncio
async def test_adapter_without_safe_probe_is_unsupported_and_closed(monkeypatch) -> None:
    class AdapterWithoutProbe:
        def __init__(self) -> None:
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    adapter = AdapterWithoutProbe()
    monkeypatch.setattr(certification, "build_managed_channel", lambda entry: adapter)
    environ = {
        certification_env_name("dingtalk", "client_id"): "client-id",
        certification_env_name("dingtalk", "client_secret"): "client-secret",
    }

    evidence = await certify_channels(["dingtalk"], environ=environ)

    assert evidence["providers"][0]["status"] == "unsupported"
    assert adapter.stopped is True
    assert "client-secret" not in json.dumps(evidence)


@pytest.mark.asyncio
async def test_side_effecting_mode_requires_both_acknowledgement_and_target() -> None:
    with pytest.raises(CertificationUsageError, match="allow-side-effects"):
        await certify_channels(
            ["telegram"],
            environ=_telegram_env(),
            send_test_message=True,
        )

    with pytest.raises(CertificationUsageError, match="require --target"):
        await certify_channels(
            ["telegram"],
            environ=_telegram_env(),
            send_test_message=True,
            allow_side_effects=True,
        )


@pytest.mark.asyncio
async def test_explicit_delivery_target_is_used_but_not_emitted(monkeypatch) -> None:
    adapter = _FakeAdapter()
    monkeypatch.setattr(certification, "build_managed_channel", lambda entry: adapter)

    evidence = await certify_channels(
        ["telegram"],
        environ=_telegram_env(),
        send_test_message=True,
        allow_side_effects=True,
        targets={"telegram": "private-chat-123"},
    )

    row = evidence["providers"][0]
    assert row["status"] == "verified_with_delivery"
    assert row["targetConfigured"] is True
    assert row["deliveryAttempted"] is True
    assert len(adapter.sent) == 1
    assert adapter.sent[0].reply_to == "private-chat-123"
    assert "private-chat-123" not in json.dumps(evidence)


@pytest.mark.asyncio
async def test_delivery_failure_preserves_successful_auth_evidence(monkeypatch) -> None:
    class DeliveryFailureAdapter(_FakeAdapter):
        async def send(self, message) -> None:
            raise RuntimeError(
                f"destination {message.reply_to} rejected the test message"
            )

    monkeypatch.setattr(
        certification,
        "build_managed_channel",
        lambda entry: DeliveryFailureAdapter(),
    )

    evidence = await certify_channels(
        ["telegram"],
        environ=_telegram_env(),
        send_test_message=True,
        allow_side_effects=True,
        targets={"telegram": "private-chat-123"},
    )

    row = evidence["providers"][0]
    assert row["status"] == "delivery_failed"
    assert row["authenticated"] is True
    assert row["deliveryAttempted"] is True
    assert "private-chat-123" not in json.dumps(evidence)
    assert "[REDACTED]" in row["detail"]


@pytest.mark.asyncio
async def test_probe_exception_is_redacted(monkeypatch) -> None:
    secret = "do-not-render"

    class FailingAdapter(_FakeAdapter):
        async def probe_connection(self) -> dict[str, Any]:
            raise RuntimeError(f"bot token={secret}")

    monkeypatch.setattr(
        certification,
        "build_managed_channel",
        lambda entry: FailingAdapter(),
    )

    evidence = await certify_channels(["telegram"], environ=_telegram_env(secret))

    assert evidence["providers"][0]["status"] == "failed"
    assert secret not in json.dumps(evidence)


def test_target_parser_rejects_ambiguous_or_duplicate_values() -> None:
    assert parse_targets(["telegram=123", "discord=456"]) == {
        "telegram": "123",
        "discord": "456",
    }
    with pytest.raises(CertificationUsageError, match="provider=destination"):
        parse_targets(["telegram"])
    with pytest.raises(CertificationUsageError, match="duplicate"):
        parse_targets(["telegram=1", "telegram=2"])


def test_redactor_handles_nested_sensitive_keys_and_inline_tokens() -> None:
    payload = {
        "access_token": "unknown-secret",
        "nested": [
            "password=hunter2",
            {"authorization": "provider-issued", "public": "okay"},
        ],
    }
    assert redact_evidence(payload, []) == {
        "access_token": "[REDACTED]",
        "nested": [
            "password=[REDACTED]",
            {"authorization": "[REDACTED]", "public": "okay"},
        ],
    }


@pytest.mark.parametrize(
    ("provider", "target", "expected_metadata"),
    [
        ("slack", "C123", {"channel": "C123"}),
        ("telegram", "-100123", {"chat_id": "-100123"}),
        ("discord", "123", {}),
        ("feishu", "oc_chat", {}),
        ("wecom", "user-1", {"touser": "user-1"}),
        ("matrix", "!room:example.org", {"room_id": "!room:example.org"}),
        (
            "qq",
            "group:group-openid",
            {"chat_type": "group", "group_openid": "group-openid"},
        ),
        (
            "dingtalk",
            "conversation-1",
            {"conversation_id": "conversation-1"},
        ),
    ],
)
def test_delivery_message_uses_only_provider_specific_target_metadata(
    provider: str,
    target: str,
    expected_metadata: dict[str, str],
) -> None:
    message = _delivery_message(provider, target)

    assert message.reply_to == target
    assert message.metadata == expected_metadata


def test_qq_delivery_target_requires_explicit_conversation_kind() -> None:
    with pytest.raises(CertificationUsageError, match="c2c:<openid>"):
        _delivery_message("qq", "ambiguous-openid")


@pytest.mark.asyncio
async def test_dingtalk_delivery_is_not_attempted_without_inbound_context(monkeypatch) -> None:
    adapter = _FakeAdapter()
    monkeypatch.setattr(certification, "build_managed_channel", lambda entry: adapter)
    environ = {
        certification_env_name("dingtalk", "client_id"): "client-id",
        certification_env_name("dingtalk", "client_secret"): "client-secret",
    }

    evidence = await certify_channels(
        ["dingtalk"],
        environ=environ,
        send_test_message=True,
        allow_side_effects=True,
        targets={"dingtalk": "conversation-1"},
    )

    row = evidence["providers"][0]
    assert row["status"] == "delivery_unsupported"
    assert row["authenticated"] is True
    assert row["deliveryAttempted"] is False
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_invalid_numeric_environment_never_echoes_supplied_value() -> None:
    invalid_value = "do-not-echo-this"

    evidence = await certify_channels(
        ["telegram"],
        environ={
            certification_env_name("telegram", "token"): "secret",
            certification_env_name("telegram", "poll_limit"): invalid_value,
        },
    )

    row = evidence["providers"][0]
    assert row["status"] == "invalid_environment"
    assert row["detail"] == "poll_limit must be an integer"
    assert invalid_value not in json.dumps(evidence)


def test_cli_json_evidence_never_prints_credentials(monkeypatch) -> None:
    secret = "cli-secret-value"
    adapter = _FakeAdapter({"authenticated": True, "token": secret})
    monkeypatch.setattr(certification, "build_managed_channel", lambda entry: adapter)
    monkeypatch.setenv(certification_env_name("telegram", "token"), secret)

    result = CliRunner().invoke(
        app,
        ["channels", "certify", "--provider", "telegram", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert secret not in result.output
    payload = json.loads(result.output)
    assert payload["providers"][0]["status"] == "verified"


def test_cli_refuses_delivery_without_explicit_ack_and_target(monkeypatch) -> None:
    monkeypatch.setenv(certification_env_name("telegram", "token"), "secret")

    result = CliRunner().invoke(
        app,
        [
            "channels",
            "certify",
            "--provider",
            "telegram",
            "--send-test-message",
            "--json",
        ],
    )

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["error"]["code"] == "invalid_certification_request"
