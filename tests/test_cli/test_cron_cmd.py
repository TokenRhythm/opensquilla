"""CLI cron command coverage.

Verifies the new helpers (_parse_duration_seconds, _resolve_webhook_token,
_build_delivery_params) and that cron add/update build the right RPC params
when the new flags are exercised. Network is stubbed so we never actually
hit a gateway.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import typer

from opensquilla.cli import cron_cmd

# --- _parse_duration_seconds ---------------------------------------------


def test_parse_duration_none_returns_none() -> None:
    assert cron_cmd._parse_duration_seconds(None) is None
    assert cron_cmd._parse_duration_seconds("") is None


def test_parse_duration_seconds_suffix() -> None:
    assert cron_cmd._parse_duration_seconds("30s") == 30.0
    assert cron_cmd._parse_duration_seconds("5m") == 300.0
    assert cron_cmd._parse_duration_seconds("1h") == 3600.0
    assert cron_cmd._parse_duration_seconds("90sec") == 90.0
    assert cron_cmd._parse_duration_seconds("2hrs") == 7200.0


def test_parse_duration_plain_number() -> None:
    assert cron_cmd._parse_duration_seconds("45") == 45.0
    assert cron_cmd._parse_duration_seconds("0") == 0.0
    assert cron_cmd._parse_duration_seconds(60) == 60.0
    assert cron_cmd._parse_duration_seconds(60.5) == 60.5


def test_parse_duration_rejects_garbage() -> None:
    with pytest.raises(typer.BadParameter):
        cron_cmd._parse_duration_seconds("nope")
    with pytest.raises(typer.BadParameter):
        cron_cmd._parse_duration_seconds("5x")


# --- _resolve_webhook_token ----------------------------------------------


def test_webhook_token_none_when_no_source() -> None:
    assert cron_cmd._resolve_webhook_token(inline=None, env=None, path=None) is None


def test_webhook_token_from_env(monkeypatch) -> None:
    monkeypatch.setenv("MY_HOOK_TOKEN", "secret-bearer")
    assert (
        cron_cmd._resolve_webhook_token(inline=None, env="MY_HOOK_TOKEN", path=None)
        == "secret-bearer"
    )


def test_webhook_token_from_env_unset(monkeypatch) -> None:
    monkeypatch.delenv("UNSET_TOKEN", raising=False)
    with pytest.raises(typer.BadParameter, match="UNSET_TOKEN"):
        cron_cmd._resolve_webhook_token(inline=None, env="UNSET_TOKEN", path=None)


def test_webhook_token_from_file(tmp_path: Path) -> None:
    secret = tmp_path / "token.txt"
    secret.write_text("  file-token  \n", encoding="utf-8")
    assert (
        cron_cmd._resolve_webhook_token(inline=None, env=None, path=str(secret))
        == "file-token"
    )


def test_webhook_token_inline_emits_warning(capsys) -> None:
    out = cron_cmd._resolve_webhook_token(
        inline="raw-token", env=None, path=None
    )
    assert out == "raw-token"
    err = capsys.readouterr().err
    assert "shell history" in err


def test_webhook_token_rejects_multiple_sources(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("T", "x")
    secret = tmp_path / "t.txt"
    secret.write_text("y", encoding="utf-8")
    with pytest.raises(typer.BadParameter, match="at most one"):
        cron_cmd._resolve_webhook_token(inline=None, env="T", path=str(secret))


# --- _build_delivery_params ---------------------------------------------


def test_build_delivery_none_when_no_flags() -> None:
    assert (
        cron_cmd._build_delivery_params(
            announce=False,
            no_deliver=False,
            channel=None,
            to=None,
            account=None,
            best_effort=False,
            webhook_url=None,
            webhook_token=None,
        )
        is None
    )


def test_build_delivery_announce_with_target() -> None:
    d = cron_cmd._build_delivery_params(
        announce=True,
        no_deliver=False,
        channel="slack",
        to="C123",
        account=None,
        best_effort=False,
        webhook_url=None,
        webhook_token=None,
    )
    assert d == {"mode": "announce", "channelName": "slack", "to": "C123"}


def test_build_delivery_announce_with_last_omits_channel_name() -> None:
    """'last' is a CLI sentinel meaning 'let the backend infer'."""
    d = cron_cmd._build_delivery_params(
        announce=True,
        no_deliver=False,
        channel="last",
        to=None,
        account=None,
        best_effort=False,
        webhook_url=None,
        webhook_token=None,
    )
    assert d == {"mode": "announce"}
    assert "channelName" not in d


def test_build_delivery_no_deliver() -> None:
    d = cron_cmd._build_delivery_params(
        announce=False,
        no_deliver=True,
        channel=None,
        to=None,
        account=None,
        best_effort=False,
        webhook_url=None,
        webhook_token=None,
    )
    assert d == {"mode": "none"}


def test_build_delivery_webhook() -> None:
    d = cron_cmd._build_delivery_params(
        announce=False,
        no_deliver=False,
        channel=None,
        to=None,
        account=None,
        best_effort=True,
        webhook_url="https://hooks.example/cron",
        webhook_token="bearer-x",
    )
    assert d == {
        "mode": "webhook",
        "webhookUrl": "https://hooks.example/cron",
        "webhookToken": "bearer-x",
        "bestEffort": True,
    }


def test_build_delivery_mutex_webhook_vs_announce() -> None:
    with pytest.raises(typer.BadParameter, match="at most one"):
        cron_cmd._build_delivery_params(
            announce=True,
            no_deliver=False,
            channel=None,
            to=None,
            account=None,
            best_effort=False,
            webhook_url="https://hooks.example/x",
            webhook_token=None,
        )


def test_build_delivery_mutex_announce_vs_no_deliver() -> None:
    with pytest.raises(typer.BadParameter, match="at most one"):
        cron_cmd._build_delivery_params(
            announce=True,
            no_deliver=True,
            channel=None,
            to=None,
            account=None,
            best_effort=False,
            webhook_url=None,
            webhook_token=None,
        )


def test_build_delivery_token_without_url() -> None:
    with pytest.raises(typer.BadParameter, match="requires --webhook-url"):
        cron_cmd._build_delivery_params(
            announce=False,
            no_deliver=False,
            channel=None,
            to=None,
            account=None,
            best_effort=False,
            webhook_url=None,
            webhook_token="dangling-token",
        )


def test_build_delivery_with_account_implies_announce() -> None:
    d = cron_cmd._build_delivery_params(
        announce=False,
        no_deliver=False,
        channel="slack",
        to="C123",
        account="acct-1",
        best_effort=True,
        webhook_url=None,
        webhook_token=None,
    )
    assert d == {
        "mode": "announce",
        "channelName": "slack",
        "to": "C123",
        "accountId": "acct-1",
        "bestEffort": True,
    }


# --- _build_failure_destination_dict -------------------------------------


def test_failure_dest_none_when_no_flags() -> None:
    assert (
        cron_cmd._build_failure_destination_dict(
            mode=None,
            channel=None,
            to=None,
            account=None,
            webhook_url=None,
            webhook_token=None,
        )
        is None
    )


def test_failure_dest_webhook() -> None:
    fd = cron_cmd._build_failure_destination_dict(
        mode="webhook",
        channel=None,
        to=None,
        account=None,
        webhook_url="https://hooks.example/alert",
        webhook_token="bearer-x",
    )
    assert fd == {
        "mode": "webhook",
        "webhookUrl": "https://hooks.example/alert",
        "webhookToken": "bearer-x",
    }


def test_failure_dest_channel() -> None:
    fd = cron_cmd._build_failure_destination_dict(
        mode="channel",
        channel="Slack",
        to="C-ops",
        account="acct-1",
        webhook_url=None,
        webhook_token=None,
    )
    assert fd == {
        "mode": "channel",
        "channelName": "slack",
        "to": "C-ops",
        "accountId": "acct-1",
    }


def test_failure_dest_webhook_requires_url() -> None:
    with pytest.raises(typer.BadParameter, match="--failure-webhook-url"):
        cron_cmd._build_failure_destination_dict(
            mode="webhook",
            channel=None,
            to=None,
            account=None,
            webhook_url=None,
            webhook_token=None,
        )


def test_failure_dest_rejects_unknown_mode() -> None:
    with pytest.raises(typer.BadParameter, match="channel.*webhook"):
        cron_cmd._build_failure_destination_dict(
            mode="email",
            channel=None,
            to=None,
            account=None,
            webhook_url=None,
            webhook_token=None,
        )


def test_failure_dest_flag_without_mode() -> None:
    with pytest.raises(typer.BadParameter, match="--failure-mode"):
        cron_cmd._build_failure_destination_dict(
            mode=None,
            channel="slack",
            to="C-x",
            account=None,
            webhook_url=None,
            webhook_token=None,
        )


# --- cron_add / cron_update RPC param construction ------------------------


class _StubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        return {"id": "stub", "method": method}


@pytest.fixture
def stub_gateway(monkeypatch):
    client = _StubClient()

    def _runner(fn, *, json_output: bool = False):  # noqa: ARG001
        import asyncio
        return asyncio.run(fn(client))

    monkeypatch.setattr(cron_cmd, "run_gateway_sync", _runner)
    monkeypatch.setattr(cron_cmd, "confirm_or_exit", lambda *a, **kw: None)
    monkeypatch.setattr(cron_cmd, "_emit_success", lambda *a, **kw: None)
    return client


def test_cron_add_with_announce_and_channel(stub_gateway) -> None:
    cron_cmd.cron_add(
        expression="0 9 * * *",
        text="Daily brief",
        name=None,
        agent=None,
        session_target="isolated",
        timeout=None,
        tz=None,
        wake=None,
        exact=False,
        jitter=None,
        announce=True,
        no_deliver=False,
        channel="slack",
        to="C123",
        account=None,
        best_effort_deliver=False,
        webhook_url=None,
        webhook_token=None,
        webhook_token_env=None,
        webhook_token_file=None,
        failure_mode=None,
        failure_channel=None,
        failure_to=None,
        failure_account=None,
        failure_webhook_url=None,
        failure_webhook_token=None,
        failure_webhook_token_env=None,
        failure_webhook_token_file=None,
        json_output=False,
    )
    assert stub_gateway.calls, "no RPC call was issued"
    method, params = stub_gateway.calls[-1]
    assert method == "cron.create"
    assert params["delivery"] == {
        "mode": "announce",
        "channelName": "slack",
        "to": "C123",
    }


def test_cron_add_rejects_current_target_without_session_binding(stub_gateway) -> None:
    with pytest.raises(typer.BadParameter, match="current.*session-bound"):
        cron_cmd.cron_add(
            expression="0 9 * * *",
            text="Daily brief",
            name=None,
            agent=None,
            session_target="current",
            timeout=None,
            tz=None,
            wake=None,
            exact=False,
            jitter=None,
            announce=False,
            no_deliver=False,
            channel=None,
            to=None,
            account=None,
            best_effort_deliver=False,
            webhook_url=None,
            webhook_token=None,
            webhook_token_env=None,
            webhook_token_file=None,
            failure_mode=None,
            failure_channel=None,
            failure_to=None,
            failure_account=None,
            failure_webhook_url=None,
            failure_webhook_token=None,
            failure_webhook_token_env=None,
            failure_webhook_token_file=None,
            json_output=False,
        )

    assert stub_gateway.calls == []


def test_cron_add_with_webhook(stub_gateway, monkeypatch) -> None:
    monkeypatch.setenv("HOOK_TOKEN", "from-env")
    cron_cmd.cron_add(
        expression="*/5 * * * *",
        text="poke",
        name=None,
        agent=None,
        session_target="isolated",
        timeout=None,
        tz=None,
        wake=None,
        exact=False,
        jitter=None,
        announce=False,
        no_deliver=False,
        channel=None,
        to=None,
        account=None,
        best_effort_deliver=False,
        webhook_url="https://hooks.example/cron",
        webhook_token=None,
        webhook_token_env="HOOK_TOKEN",
        webhook_token_file=None,
        failure_mode=None,
        failure_channel=None,
        failure_to=None,
        failure_account=None,
        failure_webhook_url=None,
        failure_webhook_token=None,
        failure_webhook_token_env=None,
        failure_webhook_token_file=None,
        json_output=False,
    )
    method, params = stub_gateway.calls[-1]
    assert method == "cron.create"
    assert params["delivery"] == {
        "mode": "webhook",
        "webhookUrl": "https://hooks.example/cron",
        "webhookToken": "from-env",
    }


def test_cron_add_with_failure_destination(stub_gateway) -> None:
    cron_cmd.cron_add(
        expression="0 9 * * *",
        text="Daily brief",
        name=None,
        agent=None,
        session_target="isolated",
        timeout=None,
        tz=None,
        wake=None,
        exact=False,
        jitter=None,
        announce=True,
        no_deliver=False,
        channel="slack",
        to="C123",
        account=None,
        best_effort_deliver=False,
        webhook_url=None,
        webhook_token=None,
        webhook_token_env=None,
        webhook_token_file=None,
        failure_mode="webhook",
        failure_channel=None,
        failure_to=None,
        failure_account=None,
        failure_webhook_url="https://hooks.example/alert",
        failure_webhook_token=None,
        failure_webhook_token_env=None,
        failure_webhook_token_file=None,
        json_output=False,
    )
    method, params = stub_gateway.calls[-1]
    assert method == "cron.create"
    assert params["delivery"] == {
        "mode": "announce",
        "channelName": "slack",
        "to": "C123",
        "failureDestination": {
            "mode": "webhook",
            "webhookUrl": "https://hooks.example/alert",
        },
    }


def test_cron_add_with_wake_and_jitter_duration(stub_gateway) -> None:
    cron_cmd.cron_add(
        expression="*/5 * * * *",
        text="x",
        name=None,
        agent=None,
        session_target="main",
        timeout=None,
        tz=None,
        wake="next-heartbeat",
        exact=False,
        jitter="30s",
        announce=False,
        no_deliver=False,
        channel=None,
        to=None,
        account=None,
        best_effort_deliver=False,
        webhook_url=None,
        webhook_token=None,
        webhook_token_env=None,
        webhook_token_file=None,
        failure_mode=None,
        failure_channel=None,
        failure_to=None,
        failure_account=None,
        failure_webhook_url=None,
        failure_webhook_token=None,
        failure_webhook_token_env=None,
        failure_webhook_token_file=None,
        json_output=False,
    )
    method, params = stub_gateway.calls[-1]
    assert params["wakeMode"] == "next-heartbeat"
    assert params["jitterSeconds"] == 30.0
    assert "delivery" not in params  # no delivery flags → not sent


def test_cron_add_with_every_builds_canonical_schedule(stub_gateway) -> None:
    cron_cmd.cron_add(
        expression=None,
        cron=None,
        every="5m",
        at=None,
        text="Drink water",
        name=None,
        agent=None,
        session_target="isolated",
        timeout=None,
        tz=None,
        wake=None,
        exact=False,
        jitter=None,
        announce=False,
        no_deliver=False,
        channel=None,
        to=None,
        account=None,
        best_effort_deliver=False,
        webhook_url=None,
        webhook_token=None,
        webhook_token_env=None,
        webhook_token_file=None,
        failure_mode=None,
        failure_channel=None,
        failure_to=None,
        failure_account=None,
        failure_webhook_url=None,
        failure_webhook_token=None,
        failure_webhook_token_env=None,
        failure_webhook_token_file=None,
        json_output=False,
    )

    method, params = stub_gateway.calls[-1]
    assert method == "cron.create"
    assert params["schedule"] == {"kind": "every", "every_seconds": 300}
    assert params["payloadKind"] == "reminder"
    assert "expression" not in params


def test_cron_add_rejects_multiple_schedule_sources(stub_gateway) -> None:
    with pytest.raises(typer.BadParameter, match="exactly one"):
        cron_cmd.cron_add(
            expression="*/5 * * * *",
            cron="0 9 * * *",
            every=None,
            at=None,
            text="x",
            name=None,
            agent=None,
            session_target="isolated",
            timeout=None,
            tz=None,
            wake=None,
            exact=False,
            jitter=None,
            announce=False,
            no_deliver=False,
            channel=None,
            to=None,
            account=None,
            best_effort_deliver=False,
            webhook_url=None,
            webhook_token=None,
            webhook_token_env=None,
            webhook_token_file=None,
            failure_mode=None,
            failure_channel=None,
            failure_to=None,
            failure_account=None,
            failure_webhook_url=None,
            failure_webhook_token=None,
            failure_webhook_token_env=None,
            failure_webhook_token_file=None,
            json_output=False,
        )


def test_cron_add_rejects_fractional_every(stub_gateway) -> None:
    with pytest.raises(typer.BadParameter, match="whole seconds"):
        cron_cmd.cron_add(
            expression=None,
            cron=None,
            every="1.9s",
            at=None,
            text="x",
            name=None,
            agent=None,
            session_target="isolated",
            timeout=None,
            tz=None,
            wake=None,
            exact=False,
            jitter=None,
            announce=False,
            no_deliver=False,
            channel=None,
            to=None,
            account=None,
            best_effort_deliver=False,
            webhook_url=None,
            webhook_token=None,
            webhook_token_env=None,
            webhook_token_file=None,
            failure_mode=None,
            failure_channel=None,
            failure_to=None,
            failure_account=None,
            failure_webhook_url=None,
            failure_webhook_token=None,
            failure_webhook_token_env=None,
            failure_webhook_token_file=None,
            json_output=False,
        )

    assert stub_gateway.calls == []


def test_cron_add_rejects_invalid_wake(stub_gateway) -> None:
    with pytest.raises(typer.BadParameter, match="now or next-heartbeat"):
        cron_cmd.cron_add(
            expression="*/5 * * * *",
            text="x",
            name=None,
            agent=None,
            session_target="main",
            timeout=None,
            tz=None,
            wake="LATER",
            exact=False,
            jitter=None,
            announce=False,
            no_deliver=False,
            channel=None,
            to=None,
            account=None,
            best_effort_deliver=False,
            webhook_url=None,
            webhook_token=None,
            webhook_token_env=None,
            webhook_token_file=None,
            json_output=False,
        )


def test_cron_update_with_wake(stub_gateway) -> None:
    cron_cmd.cron_update(
        job_id="job-1",
        expression=None,
        cron=None,
        every=None,
        at=None,
        text=None,
        name=None,
        enabled=None,
        timeout=None,
        tz=None,
        wake="now",
        failure_mode=None,
        failure_channel=None,
        failure_to=None,
        failure_account=None,
        failure_webhook_url=None,
        failure_webhook_token=None,
        failure_webhook_token_env=None,
        failure_webhook_token_file=None,
        json_output=False,
    )
    method, params = stub_gateway.calls[-1]
    assert method == "cron.update"
    assert params["id"] == "job-1"
    assert params["wakeMode"] == "now"


def test_cron_update_with_every_builds_canonical_schedule(stub_gateway) -> None:
    cron_cmd.cron_update(
        job_id="job-1",
        expression=None,
        cron=None,
        every="10m",
        at=None,
        text=None,
        name=None,
        enabled=None,
        timeout=None,
        tz=None,
        wake=None,
        failure_mode=None,
        failure_channel=None,
        failure_to=None,
        failure_account=None,
        failure_webhook_url=None,
        failure_webhook_token=None,
        failure_webhook_token_env=None,
        failure_webhook_token_file=None,
        json_output=False,
    )

    method, params = stub_gateway.calls[-1]
    assert method == "cron.update"
    assert params["schedule"] == {"kind": "every", "every_seconds": 600}
    assert "expression" not in params


def test_cron_update_with_tz_only_sends_timezone_patch(stub_gateway) -> None:
    cron_cmd.cron_update(
        job_id="job-1",
        expression=None,
        cron=None,
        every=None,
        at=None,
        text=None,
        name=None,
        enabled=None,
        timeout=None,
        tz="Asia/Shanghai",
        wake=None,
        failure_mode=None,
        failure_channel=None,
        failure_to=None,
        failure_account=None,
        failure_webhook_url=None,
        failure_webhook_token=None,
        failure_webhook_token_env=None,
        failure_webhook_token_file=None,
        json_output=False,
    )

    method, params = stub_gateway.calls[-1]
    assert method == "cron.update"
    assert params == {"id": "job-1", "tz": "Asia/Shanghai"}


def test_cron_update_rejects_multiple_schedule_sources(stub_gateway) -> None:
    with pytest.raises(typer.BadParameter, match="at most one"):
        cron_cmd.cron_update(
            job_id="job-1",
            expression="*/5 * * * *",
            cron="0 9 * * *",
            every=None,
            at=None,
            text=None,
            name=None,
            enabled=None,
            timeout=None,
            tz=None,
            wake=None,
            failure_mode=None,
            failure_channel=None,
            failure_to=None,
            failure_account=None,
            failure_webhook_url=None,
            failure_webhook_token=None,
            failure_webhook_token_env=None,
            failure_webhook_token_file=None,
            json_output=False,
        )


def test_cron_update_requires_at_least_one_field(stub_gateway) -> None:
    with pytest.raises(typer.BadParameter, match="at least one"):
        cron_cmd.cron_update(
            job_id="job-1",
            expression=None,
            text=None,
            name=None,
            enabled=None,
            timeout=None,
            tz=None,
            wake=None,
            failure_mode=None,
            failure_channel=None,
            failure_to=None,
            failure_account=None,
            failure_webhook_url=None,
            failure_webhook_token=None,
            failure_webhook_token_env=None,
            failure_webhook_token_file=None,
            json_output=False,
        )


def test_cron_update_with_failure_destination_webhook(stub_gateway) -> None:
    cron_cmd.cron_update(
        job_id="job-1",
        expression=None,
        text=None,
        name=None,
        enabled=None,
        timeout=None,
        tz=None,
        wake=None,
        failure_mode="webhook",
        failure_channel=None,
        failure_to=None,
        failure_account=None,
        failure_webhook_url="https://hooks.example/alert",
        failure_webhook_token=None,
        failure_webhook_token_env=None,
        failure_webhook_token_file=None,
        json_output=False,
    )
    method, params = stub_gateway.calls[-1]
    assert method == "cron.update"
    assert params["delivery"] == {
        "failureDestination": {
            "mode": "webhook",
            "webhookUrl": "https://hooks.example/alert",
        }
    }


def test_cron_update_with_failure_destination_channel(stub_gateway) -> None:
    cron_cmd.cron_update(
        job_id="job-1",
        expression=None,
        text=None,
        name=None,
        enabled=None,
        timeout=None,
        tz=None,
        wake=None,
        failure_mode="channel",
        failure_channel="slack",
        failure_to="C-ops",
        failure_account=None,
        failure_webhook_url=None,
        failure_webhook_token=None,
        failure_webhook_token_env=None,
        failure_webhook_token_file=None,
        json_output=False,
    )
    method, params = stub_gateway.calls[-1]
    assert params["delivery"] == {
        "failureDestination": {
            "mode": "channel",
            "channelName": "slack",
            "to": "C-ops",
        }
    }


def test_cron_update_failure_webhook_missing_url(stub_gateway) -> None:
    with pytest.raises(typer.BadParameter, match="--failure-webhook-url"):
        cron_cmd.cron_update(
            job_id="job-1",
            expression=None,
            text=None,
            name=None,
            enabled=None,
            timeout=None,
            tz=None,
            wake=None,
            failure_mode="webhook",
            failure_channel=None,
            failure_to=None,
            failure_account=None,
            failure_webhook_url=None,
            failure_webhook_token=None,
            failure_webhook_token_env=None,
            failure_webhook_token_file=None,
            json_output=False,
        )
