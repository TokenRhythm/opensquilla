"""`sessions export`/`delete`/`resume` and `bundle` aim at the selected profile.

Those four, and no others. `--profile qa` puts the profile's home on
`OPENSQUILLA_PROFILE`, which moves the resolved config, which carries the port
that profile's gateway binds. Commands routed through `run_gateway_sync` follow
that chain. These four did not: they connected to a literal
`ws://localhost:18791/ws`, so a profile gateway on another port was invisible
to them — issues #1379 and #1374.

`sessions export` was the reported symptom, and `delete` is the one that
matters most among the four: sent to the wrong gateway it does not merely fail
to find the session, it operates on whatever gateway is listening on 18791.

Two commands are still profile-blind and are not covered here, because in both
the literal is the default of a documented `--gateway` option rather than an
internal fallback: `reset`, which mutates session state and so carries the same
risk as `delete`, and `mcp-server run`. Changing an option default changes
`--help` and the flag's advertised contract, so those move in their own
reviewed change rather than riding along with this one.
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

    `--profile qa-export` reaches the port through
    `OPENSQUILLA_PROFILE` -> `profile_home()` -> `default_opensquilla_home()` ->
    the resolved `config.toml` -> its `port`. This fixture enters that chain one
    link later, at the home, because the suite sets `OPENSQUILLA_STATE_DIR` for
    isolation and that override outranks `OPENSQUILLA_HOME` inside
    `default_opensquilla_home()`. The first link is pinned on its own in
    `test_the_profile_env_moves_the_resolved_url`; everything below it is where
    the defect was.
    """

    home = tmp_path / "qa-export"
    home.mkdir(parents=True)
    (home / "config.toml").write_text(
        f'host = "127.0.0.1"\nport = {PROFILE_PORT}\n', encoding="utf-8"
    )
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_URL", raising=False)
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    return home


def test_the_profile_env_moves_the_resolved_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first link: a selected profile really does change the target."""

    from opensquilla.cli.gateway_rpc import default_gateway_url

    homes = tmp_path / "homes"
    (homes / "qa-export").mkdir(parents=True)
    (homes / "qa-export" / "config.toml").write_text(
        f'host = "127.0.0.1"\nport = {PROFILE_PORT}\n', encoding="utf-8"
    )
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_URL", raising=False)
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("OPENSQUILLA_STATE_DIR", raising=False)
    monkeypatch.setenv("OPENSQUILLA_HOME", str(homes))

    monkeypatch.setenv("OPENSQUILLA_PROFILE", "qa-export")
    assert f":{PROFILE_PORT}/" in default_gateway_url()

    monkeypatch.delenv("OPENSQUILLA_PROFILE")
    assert default_gateway_url() == DEFAULT_URL


@pytest.fixture
def connected_urls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every URL a CLI command asks the gateway client to connect to."""

    urls: list[str] = []

    class RecordingClient:
        async def connect(self, url: str, *, token: str | None = None) -> None:
            urls.append(url)

        async def call(self, method: str, params: dict | None = None) -> Any:
            return {}

        async def list_sessions(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"sessions": [], "count": 0}

        async def close(self) -> None:
            return None

    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", RecordingClient)
    return urls


# The three that used the literal, and one that already resolved correctly and
# has to keep doing so.
@pytest.mark.parametrize(
    "argv",
    [
        ["sessions", "export", "some-session-key"],
        ["sessions", "delete", "some-session-key", "--yes"],
        ["sessions", "resume", "some-session-key"],
        ["sessions", "show", "some-session-key"],
    ],
    ids=["export", "delete", "resume", "show"],
)
def test_a_sessions_command_aims_at_the_selected_profile(
    argv: list[str], profile_home: Path, connected_urls: list[str]
) -> None:
    runner.invoke(app, argv)

    assert connected_urls, f"{argv[1]} never opened a gateway connection"
    for url in connected_urls:
        assert f":{PROFILE_PORT}/" in url, url
        assert url != DEFAULT_URL


def test_the_bundle_enriches_from_the_profile_it_is_collecting(
    profile_home: Path, connected_urls: list[str], tmp_path: Path
) -> None:
    """The rest of the bundle is read from the profile's home.

    Live `doctor`/`channels` sections taken from a different gateway would be
    wrong in a way the file does not disclose.
    """

    runner.invoke(app, ["bundle", "--output", str(tmp_path / "b.zip"), "--json"])

    assert connected_urls, "bundle never attempted live enrichment"
    for url in connected_urls:
        assert f":{PROFILE_PORT}/" in url, url


@pytest.mark.parametrize(
    "argv",
    [
        ["sessions", "export", "some-session-key"],
        ["sessions", "delete", "some-session-key", "--yes"],
        ["sessions", "show", "some-session-key"],
    ],
    ids=["export", "delete", "show"],
)
def test_an_explicit_gateway_url_still_wins(
    argv: list[str],
    profile_home: Path,
    connected_urls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The env override outranks the profile, as it did before."""

    monkeypatch.setenv("OPENSQUILLA_GATEWAY_URL", "ws://127.0.0.1:19999/ws")

    runner.invoke(app, argv)

    assert connected_urls
    for url in connected_urls:
        assert ":19999/" in url, url


def test_no_profile_still_means_the_release_default(
    tmp_path: Path, connected_urls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing configured must land exactly where it always did."""

    monkeypatch.delenv("OPENSQUILLA_GATEWAY_URL", raising=False)
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("OPENSQUILLA_PROFILE", raising=False)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(empty))

    runner.invoke(app, ["sessions", "export", "some-session-key"])

    assert connected_urls
    for url in connected_urls:
        assert url == DEFAULT_URL, url
