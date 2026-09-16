"""`reset` and `mcp-server run` resolve their gateway, they do not assume it.

Both took the gateway as a `--gateway` option whose *default* was a literal
`localhost:18791`. Explicit flags and `OPENSQUILLA_GATEWAY_URL` worked; what the
literal swallowed was the step below them — the gateway the selected profile
actually configured. Follow-up to #1417, which fixed the four commands whose
literal was an internal fallback rather than an advertised option default.

`reset` is the one that matters: it flushes memory and rotates the session id,
so pointed at the wrong gateway it does not fail a lookup, it mutates whatever
matches on 127.0.0.1:18791.

The precedence these tests pin, in order: explicit `--gateway`, then
`OPENSQUILLA_GATEWAY_URL`, then the selected profile's config, then the release
default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from opensquilla.cli.main import app

runner = CliRunner()

PROFILE_PORT = 18823
DEFAULT_URL = "ws://localhost:18791/ws"


@pytest.fixture
def profile_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home whose gateway config binds a non-default port.

    Entered at the home rather than at `--profile`, because the suite sets
    `OPENSQUILLA_STATE_DIR` for isolation and that override outranks
    `OPENSQUILLA_HOME` inside `default_opensquilla_home()`. The profile ->
    home link is pinned separately in `test_profile_target_resolution.py`.
    """

    home = tmp_path / "qa-reset"
    home.mkdir(parents=True)
    (home / "config.toml").write_text(
        f'host = "127.0.0.1"\nport = {PROFILE_PORT}\n', encoding="utf-8"
    )
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_URL", raising=False)
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    return home


@pytest.fixture
def connected_urls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    urls: list[str] = []

    class RecordingClient:
        async def connect(self, url: str, *, token: str | None = None) -> None:
            urls.append(url)

        async def reset_session(self, key: str) -> dict[str, Any]:
            return {"session_id": "new", "previous_session_id": key, "flush_receipt": {}}

        async def close(self) -> None:
            return None

    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", RecordingClient)
    return urls


@pytest.fixture
def bridged_urls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`mcp-server run` hands its URL to the bridge instead of connecting."""

    urls: list[str] = []

    class RecordingBridge:
        def __init__(self, *, gateway_url: str) -> None:
            urls.append(gateway_url)

    monkeypatch.setattr(
        "opensquilla.mcp_server.bridge.OpenSquillaMCPBridge", RecordingBridge
    )
    monkeypatch.setattr(
        "opensquilla.mcp_server.server.create_mcp_server",
        lambda bridge: type("_Mcp", (), {"run": lambda self, *a, **k: None})(),
    )
    return urls


def test_reset_defaults_to_the_selected_profile(
    profile_home: Path, connected_urls: list[str]
) -> None:
    """The regression: a state-mutating command aimed at the wrong gateway."""

    runner.invoke(app, ["reset", "--key", "sess-1"])

    assert connected_urls
    for url in connected_urls:
        assert f":{PROFILE_PORT}/" in url, url
        assert url != DEFAULT_URL


def test_mcp_server_bridges_to_the_selected_profile(
    profile_home: Path, bridged_urls: list[str]
) -> None:
    runner.invoke(app, ["mcp-server", "run"])

    assert bridged_urls
    for url in bridged_urls:
        assert f":{PROFILE_PORT}/" in url, url


def test_an_explicit_gateway_flag_still_wins_over_the_profile(
    profile_home: Path, connected_urls: list[str]
) -> None:
    runner.invoke(app, ["reset", "--key", "sess-1", "--gateway", "http://127.0.0.1:19999"])

    assert connected_urls
    for url in connected_urls:
        assert ":19999/" in url, url


def test_the_env_override_still_wins_over_the_profile(
    profile_home: Path, connected_urls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_URL", "ws://127.0.0.1:19998/ws")

    runner.invoke(app, ["reset", "--key", "sess-1"])

    assert connected_urls
    for url in connected_urls:
        assert ":19998/" in url, url


def test_an_explicit_flag_outranks_the_env_override(
    profile_home: Path, connected_urls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full precedence chain, top two rungs — unchanged by this fix."""

    monkeypatch.setenv("OPENSQUILLA_GATEWAY_URL", "ws://127.0.0.1:19998/ws")

    runner.invoke(app, ["reset", "--key", "sess-1", "--gateway", "http://127.0.0.1:19999"])

    assert connected_urls
    for url in connected_urls:
        assert ":19999/" in url, url


def test_nothing_configured_still_means_the_release_default(
    tmp_path: Path, connected_urls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old literal was `http://localhost:18791`; it normalised to this."""

    monkeypatch.delenv("OPENSQUILLA_GATEWAY_URL", raising=False)
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("OPENSQUILLA_PROFILE", raising=False)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(empty))

    runner.invoke(app, ["reset", "--key", "sess-1"])

    assert connected_urls
    for url in connected_urls:
        assert url == DEFAULT_URL, url
