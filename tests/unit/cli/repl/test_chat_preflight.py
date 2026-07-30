from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_explicit_gateway_is_never_replaced_by_local_start(monkeypatch) -> None:
    from opensquilla.cli.chat import preflight

    monkeypatch.setenv("OPENSQUILLA_GATEWAY_URL", "wss://gateway.example/ws")
    monkeypatch.setattr(
        "opensquilla.cli.gateway_rpc.default_gateway_url",
        lambda: "wss://gateway.example/ws",
    )
    monkeypatch.setattr(
        "opensquilla.cli.gateway_lifecycle.remote_gateway_status",
        lambda _url: SimpleNamespace(ok=False, code="REMOTE_DOWN"),
    )

    class ForbiddenManager:
        def __init__(self, **_kwargs):
            raise AssertionError("explicit target must not construct a local manager")

    monkeypatch.setattr(
        "opensquilla.cli.gateway_lifecycle.GatewayLifecycleManager",
        ForbiddenManager,
    )

    with pytest.raises(preflight.ChatGatewayPreflightError, match="will not start"):
        preflight.preflight_gateway_chat()


def test_explicit_gateway_probes_rpc_without_starting(monkeypatch) -> None:
    from opensquilla.cli.chat import preflight

    monkeypatch.setenv("OPENSQUILLA_GATEWAY_URL", "ws://127.0.0.1:19999/ws")
    monkeypatch.setattr(
        "opensquilla.cli.gateway_rpc.default_gateway_url",
        lambda: "ws://127.0.0.1:19999/ws",
    )
    monkeypatch.setattr(
        "opensquilla.cli.gateway_lifecycle.remote_gateway_status",
        lambda _url: SimpleNamespace(ok=True, state="running"),
    )
    probes: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        preflight,
        "_verify_rpc",
        lambda url, config_path: probes.append((url, config_path)),
    )

    result = preflight.preflight_gateway_chat()

    assert result.managed is False
    assert result.started is False
    assert probes == [("ws://127.0.0.1:19999/ws", None)]


def test_implicit_local_gateway_starts_once_and_probes_rpc(monkeypatch) -> None:
    from opensquilla.cli.chat import preflight

    monkeypatch.delenv("OPENSQUILLA_GATEWAY_URL", raising=False)
    monkeypatch.setattr(
        "opensquilla.cli.gateway_rpc.default_gateway_url",
        lambda: "ws://127.0.0.1:18791/ws",
    )
    monkeypatch.setattr(preflight, "_resolved_config_path", lambda: "/tmp/config.toml")
    monkeypatch.setattr(
        "opensquilla.gateway.config.GatewayConfig.load",
        lambda _path: SimpleNamespace(host="127.0.0.1", port=18791),
    )
    monkeypatch.setattr(
        "opensquilla.onboarding.status.get_onboarding_status",
        lambda _config: SimpleNamespace(needs_onboarding=False),
    )
    calls: list[str] = []

    class FakeManager:
        def __init__(self, **kwargs):
            assert kwargs["config_path"] == "/tmp/config.toml"

        def status(self):
            calls.append("status")
            return SimpleNamespace(state="not_started", ok=True)

        def start(self):
            calls.append("start")
            return SimpleNamespace(state="running", ok=True, managed=True)

    monkeypatch.setattr(
        "opensquilla.cli.gateway_lifecycle.GatewayLifecycleManager",
        FakeManager,
    )
    monkeypatch.setattr(
        preflight,
        "_verify_rpc",
        lambda url, config_path: calls.append(f"probe:{url}:{config_path}"),
    )

    result = preflight.preflight_gateway_chat()

    assert result.started is True
    assert calls == [
        "status",
        "start",
        "probe:ws://127.0.0.1:18791/ws:/tmp/config.toml",
    ]


def test_incomplete_local_onboarding_blocks_before_start(monkeypatch) -> None:
    from opensquilla.cli.chat import preflight

    monkeypatch.delenv("OPENSQUILLA_GATEWAY_URL", raising=False)
    monkeypatch.setattr(preflight, "_resolved_config_path", lambda: "/tmp/config.toml")
    monkeypatch.setattr(
        "opensquilla.gateway.config.GatewayConfig.load",
        lambda _path: SimpleNamespace(host="127.0.0.1", port=18791),
    )
    monkeypatch.setattr(
        "opensquilla.onboarding.status.get_onboarding_status",
        lambda _config: SimpleNamespace(
            needs_onboarding=True,
            section_details={"llm": {"blocking": True}},
        ),
    )

    with pytest.raises(preflight.ChatGatewayPreflightError, match="Provider|llm"):
        preflight.preflight_gateway_chat()


def test_rpc_system_exit_becomes_typed_preflight_error(monkeypatch) -> None:
    from opensquilla.cli.chat import preflight

    async def fail_probe(_url: str, _config_path: str | None) -> None:
        raise SystemExit("bad handshake")

    monkeypatch.setattr(preflight, "_probe_rpc", fail_probe)

    with pytest.raises(
        preflight.ChatGatewayPreflightError,
        match="authentication failed: bad handshake",
    ):
        preflight._verify_rpc("ws://127.0.0.1:18791/ws", None)
