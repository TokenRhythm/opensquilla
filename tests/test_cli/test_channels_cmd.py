"""CLI tests for `opensquilla channels`."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from opensquilla.cli.main import app

runner = CliRunner()


def _setenv(monkeypatch, tmp_path: Path) -> Path:
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    return target


def test_channels_list_empty(tmp_path, monkeypatch):
    _setenv(monkeypatch, tmp_path)
    result = runner.invoke(app, ["channels", "list"])
    assert result.exit_code == 0
    assert "0 channels" in result.stdout.lower()


def test_channels_add_telegram_polling_minimal(tmp_path, monkeypatch):
    target = _setenv(monkeypatch, tmp_path)
    result = runner.invoke(
        app,
        ["channels", "add", "telegram", "--name", "tg", "--token", "abc"],
    )
    assert result.exit_code == 0, result.stdout
    text = target.read_text()
    assert "tg" in text
    assert "telegram" in text
    assert "abc" not in result.stdout
    assert "restart" in result.stdout.lower()


def test_channels_add_slack_missing_token_fails(tmp_path, monkeypatch):
    _setenv(monkeypatch, tmp_path)
    result = runner.invoke(app, ["channels", "add", "slack", "--name", "w"])
    assert result.exit_code != 0
    combined = (result.stdout + (result.stderr or "")).lower()
    assert "token" in combined


def test_channels_add_slack_succeeds_with_token(tmp_path, monkeypatch):
    target = _setenv(monkeypatch, tmp_path)
    result = runner.invoke(
        app,
        [
            "channels", "add", "slack",
            "--name", "w", "--token", "xoxb-x",
            "--field", "signing_secret=ss",
            "--field", "slack_channel_id=C123",
        ],
    )
    assert result.exit_code == 0, result.stdout
    text = target.read_text()
    assert "C123" in text
    assert "xoxb-x" not in result.stdout


def test_channels_remove(tmp_path, monkeypatch):
    target = _setenv(monkeypatch, tmp_path)
    runner.invoke(app, ["channels", "add", "slack",
                        "--name", "w", "--token", "x",
                        "--field", "signing_secret=ss"])
    result = runner.invoke(app, ["channels", "remove", "w"])
    assert result.exit_code == 0
    # Either the channel is gone, or the [[channels.channels]] table is empty.
    text = target.read_text()
    assert 'name = "w"' not in text


def test_channels_disable_then_enable(tmp_path, monkeypatch):
    target = _setenv(monkeypatch, tmp_path)
    runner.invoke(app, ["channels", "add", "slack",
                        "--name", "w", "--token", "x",
                        "--field", "signing_secret=ss"])
    r1 = runner.invoke(app, ["channels", "disable", "w"])
    assert r1.exit_code == 0
    assert "enabled = false" in target.read_text()
    r2 = runner.invoke(app, ["channels", "enable", "w"])
    assert r2.exit_code == 0
    assert "enabled = true" in target.read_text()


def test_channels_list_redacts_secrets(tmp_path, monkeypatch):
    _setenv(monkeypatch, tmp_path)
    runner.invoke(app, ["channels", "add", "slack",
                        "--name", "w", "--token", "supersecret",
                        "--field", "signing_secret=ss"])
    result = runner.invoke(app, ["channels", "list"])
    assert "supersecret" not in result.stdout
    assert "***" in result.stdout


def test_channels_edit_updates_only_provided_fields(tmp_path, monkeypatch):
    target = _setenv(monkeypatch, tmp_path)
    runner.invoke(
        app,
        ["channels", "add", "slack", "--name", "w",
         "--token", "xoxb-original",
         "--field", "signing_secret=ss",
         "--field", "slack_channel_id=C111"],
    )
    result = runner.invoke(
        app,
        ["channels", "edit", "w", "--field", "slack_channel_id=C222"],
    )
    assert result.exit_code == 0, result.stdout
    text = target.read_text()
    assert "C222" in text
    assert "C111" not in text
    assert "xoxb-original" in text


def test_channels_edit_unknown_name_fails(tmp_path, monkeypatch):
    _setenv(monkeypatch, tmp_path)
    result = runner.invoke(app, ["channels", "edit", "missing"])
    assert result.exit_code != 0


def test_channels_edit_preserves_enabled_when_unchanged(tmp_path, monkeypatch):
    target = _setenv(monkeypatch, tmp_path)
    runner.invoke(
        app,
        ["channels", "add", "slack", "--name", "w",
         "--token", "xoxb-x", "--field", "signing_secret=ss", "--disabled"],
    )
    assert "enabled = false" in target.read_text()
    r = runner.invoke(
        app, ["channels", "edit", "w", "--field", "slack_channel_id=C9"]
    )
    assert r.exit_code == 0, r.stdout
    text = target.read_text()
    assert "enabled = false" in text
    assert "C9" in text


def test_channels_edit_preserves_agent_id_when_unchanged(tmp_path, monkeypatch):
    target = _setenv(monkeypatch, tmp_path)
    runner.invoke(
        app,
        ["channels", "add", "slack", "--name", "w",
         "--token", "xoxb-x", "--field", "signing_secret=ss", "--agent-id", "ops"],
    )
    assert 'agent_id = "ops"' in target.read_text()
    r = runner.invoke(
        app, ["channels", "edit", "w", "--field", "slack_channel_id=C9"]
    )
    assert r.exit_code == 0, r.stdout
    assert 'agent_id = "ops"' in target.read_text()


def test_channels_edit_preserves_non_secret_bool_when_unchanged(
    tmp_path, monkeypatch
):
    target = _setenv(monkeypatch, tmp_path)
    runner.invoke(
        app,
        ["channels", "add", "slack", "--name", "w",
         "--token", "xoxb-x",
         "--field", "signing_secret=ss",
         "--field", "reply_in_thread=true"],
    )
    assert "reply_in_thread = true" in target.read_text()
    r = runner.invoke(
        app, ["channels", "edit", "w", "--field", "slack_channel_id=C9"]
    )
    assert r.exit_code == 0, r.stdout
    assert "reply_in_thread = true" in target.read_text()


def test_channels_add_token_resolves_to_alias_order_not_field_order(
    tmp_path, monkeypatch
):
    """For wecom, --token should resolve to the literal 'token' field
    (alias index 0), not 'corp_secret' (alias index 5) which would happen
    under naive spec-field-order resolution.
    """
    _setenv(monkeypatch, tmp_path)
    result = runner.invoke(
        app,
        ["channels", "add", "wecom",
         "--name", "wc",
         "--token", "wecom-token",
         "--field", "corp_id=cid",
         "--field", "corp_secret=cs",
         "--field", "agent_id_int=1000",
         "--field", "encoding_aes_key=" + "a" * 43],
    )
    assert result.exit_code == 0, result.stdout
    assert "wecom.token" in result.stdout
    assert "wecom.corp_secret" not in result.stdout


def test_channels_add_token_flag_echoes_resolved_field(tmp_path, monkeypatch):
    _setenv(monkeypatch, tmp_path)
    result = runner.invoke(
        app, ["channels", "add", "matrix",
              "--name", "m",
              "--token", "syt-abc",
              "--field", "homeserver_url=https://matrix.org",
              "--field", "user_id=@me:matrix.org"]
    )
    assert result.exit_code == 0, result.stdout
    assert "--token" in result.stdout
    assert "matrix.access_token" in result.stdout


def test_channels_add_token_flag_for_slack_resolves_to_token(tmp_path, monkeypatch):
    _setenv(monkeypatch, tmp_path)
    result = runner.invoke(
        app,
        [
            "channels", "add", "slack",
            "--name", "w", "--token", "xoxb-x",
            "--field", "signing_secret=ss",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "slack.token" in result.stdout


def test_channels_describe_slack(tmp_path, monkeypatch):
    _setenv(monkeypatch, tmp_path)
    result = runner.invoke(app, ["channels", "describe", "slack"])
    assert result.exit_code == 0
    out = result.stdout
    assert "token" in out
    assert "signing_secret" in out
    assert "webhook" in out.lower()
    assert "api.slack.com" in out


def test_channels_describe_unknown_type_fails(tmp_path, monkeypatch):
    _setenv(monkeypatch, tmp_path)
    result = runner.invoke(app, ["channels", "describe", "no-such-type"])
    assert result.exit_code != 0


def test_channels_types_lists_all_supported(tmp_path, monkeypatch):
    _setenv(monkeypatch, tmp_path)
    result = runner.invoke(app, ["channels", "types"])
    assert result.exit_code == 0
    out = result.stdout
    for t in ("slack", "telegram", "discord", "feishu",
              "dingtalk", "wecom", "qq", "matrix"):
        assert t in out
    # msteams is intentionally hidden from the channel catalog CLI surface
    # until first-class support lands.
    assert "msteams" not in out
    assert "transport" in out.lower()


def test_channels_add_restart_notice_disambiguates(tmp_path, monkeypatch):
    _setenv(monkeypatch, tmp_path)
    result = runner.invoke(
        app,
        [
            "channels", "add", "slack",
            "--name", "w", "--token", "x",
            "--field", "signing_secret=ss",
        ],
    )
    assert result.exit_code == 0
    out = result.stdout.lower()
    assert "gateway process" in out
    assert "not the same as" in out
    assert "channels restart" in out


def test_channels_add_prints_status_verification_next_step(tmp_path, monkeypatch):
    _setenv(monkeypatch, tmp_path)
    result = runner.invoke(
        app,
        [
            "channels", "add", "slack",
            "--name", "w", "--token", "x",
            "--field", "signing_secret=ss",
        ],
    )
    assert result.exit_code == 0
    out = result.stdout.lower()
    assert "opensquilla gateway restart" in out
    assert "opensquilla channels status w --json" in out


def test_channels_add_echoes_resolved_path_and_source(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "opensquilla.toml").write_text("")
    monkeypatch.chdir(project)
    result = runner.invoke(
        app,
        [
            "channels", "add", "slack",
            "--name", "w", "--token", "x",
            "--field", "signing_secret=ss",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert str(project / "opensquilla.toml") in result.stdout
    assert "cwd" in result.stdout.lower()


class _FakePairingGatewayClient:
    """Records pairing RPC calls and replays canned payloads."""

    calls: list[tuple[str, dict]] = []
    payloads: dict[str, dict] = {}

    async def connect(self, url: str, *, token=None) -> None:
        return None

    async def close(self) -> None:
        return None

    async def call(self, method: str, params: dict | None = None) -> dict:
        type(self).calls.append((method, params or {}))
        return type(self).payloads.get(method, {})


def _install_fake_gateway(monkeypatch) -> type[_FakePairingGatewayClient]:
    _FakePairingGatewayClient.calls = []
    monkeypatch.setattr(
        "opensquilla.cli.gateway_client.GatewayClient", _FakePairingGatewayClient
    )
    return _FakePairingGatewayClient


def test_pairings_list_renders_codes_and_passes_status_filter(tmp_path, monkeypatch):
    _setenv(monkeypatch, tmp_path)
    fake = _install_fake_gateway(monkeypatch)
    fake.payloads = {
        "channels.pairings": {
            "pairings": [
                {
                    "pairingId": "abcdef1234567890",
                    "pairingCode": "abcdef12",
                    "channelName": "telegram-main",
                    "senderId": "user-42",
                    "senderName": "Alice",
                    "status": "pending",
                    "createdAt": "2026-07-16T09:00:00+00:00",
                    "approvedAt": None,
                }
            ]
        }
    }

    result = runner.invoke(
        app,
        ["channels", "pairings", "list", "telegram-main", "--status", "pending"],
    )

    assert result.exit_code == 0, result.output
    assert fake.calls == [
        ("channels.pairings", {"channelName": "telegram-main", "status": "pending"})
    ]
    assert "abcdef12" in result.stdout
    assert "Alice" in result.stdout


def test_pairings_approve_requires_confirmation_and_sends_code(tmp_path, monkeypatch):
    _setenv(monkeypatch, tmp_path)
    fake = _install_fake_gateway(monkeypatch)
    fake.payloads = {
        "channels.pairing.approve": {
            "pairing": {"pairingCode": "abcdef12", "senderId": "user-42", "status": "approved"}
        }
    }

    # --json without --yes must refuse before any RPC happens.
    refused = runner.invoke(
        app, ["channels", "pairings", "approve", "telegram-main", "abcdef12", "--json"]
    )
    assert refused.exit_code == 2
    assert fake.calls == []

    result = runner.invoke(
        app, ["channels", "pairings", "approve", "telegram-main", "abcdef12", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert fake.calls == [
        (
            "channels.pairing.approve",
            {"channelName": "telegram-main", "pairingCode": "abcdef12"},
        )
    ]
    assert "approved" in result.stdout.lower()


def test_pairings_revoke_uses_the_revoke_rpc(tmp_path, monkeypatch):
    _setenv(monkeypatch, tmp_path)
    fake = _install_fake_gateway(monkeypatch)
    fake.payloads = {
        "channels.pairing.revoke": {"pairing": {"pairingCode": "abcdef12", "status": "revoked"}}
    }

    result = runner.invoke(
        app, ["channels", "pairings", "revoke", "telegram-main", "abcdef12", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert fake.calls == [
        (
            "channels.pairing.revoke",
            {"channelName": "telegram-main", "pairingCode": "abcdef12"},
        )
    ]
    assert "revoked" in result.stdout.lower()


def test_pairings_approve_admin_flag_passes_as_admin(tmp_path, monkeypatch):
    _setenv(monkeypatch, tmp_path)
    fake = _install_fake_gateway(monkeypatch)
    fake.payloads = {
        "channels.pairing.approve": {
            "pairing": {"pairingCode": "abcdef12", "senderId": "user-42", "status": "approved"},
            "adminGranted": True,
        }
    }

    result = runner.invoke(
        app,
        [
            "channels", "pairings", "approve", "telegram-main", "abcdef12",
            "--admin", "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert fake.calls == [
        (
            "channels.pairing.approve",
            {"channelName": "telegram-main", "pairingCode": "abcdef12", "asAdmin": True},
        )
    ]
    assert "[admin]" in result.stdout
