from __future__ import annotations

import json
from typing import Any

from typer.testing import CliRunner

from opensquilla.cli.main import app

runner = CliRunner()


class FakeGatewayClient:
    calls: list[tuple[str, Any]] = []
    payload: dict[str, Any] = {}

    async def connect(self, url: str, *, token=None) -> None:
        type(self).calls.append(("connect", url))

    async def close(self) -> None:
        type(self).calls.append(("close", None))

    async def call(self, method: str, params: dict | None = None) -> Any:
        type(self).calls.append((method, params or {}))
        return type(self).payload


class OfflineGatewayClient(FakeGatewayClient):
    async def connect(self, url: str, *, token=None) -> None:
        raise ConnectionError("connection refused")


def _install(monkeypatch, payload: dict[str, Any]) -> type[FakeGatewayClient]:
    FakeGatewayClient.calls = []
    FakeGatewayClient.payload = payload
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", FakeGatewayClient)
    return FakeGatewayClient


def test_skills_reload_json_forwards_stable_rpc_result(monkeypatch) -> None:
    client = _install(
        monkeypatch,
        {
            "success": True,
            "changed": True,
            "partial": False,
            "generation": 12,
            "added": ["new-skill"],
            "removed": [],
            "modified": ["edited-skill"],
            "errors": [],
        },
    )

    result = runner.invoke(app, ["skills", "reload", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["generation"] == 12
    assert payload["added"] == ["new-skill"]
    assert payload["modified"] == ["edited-skill"]
    assert ("skills.reload", {}) in client.calls


def test_skills_reload_human_output_shows_generation_and_diff(monkeypatch) -> None:
    _install(
        monkeypatch,
        {
            "success": True,
            "changed": True,
            "partial": False,
            "generation": 7,
            "added": ["alpha"],
            "removed": ["beta"],
            "modified": ["gamma"],
            "errors": [],
        },
    )

    result = runner.invoke(app, ["skills", "reload"])

    assert result.exit_code == 0, result.output
    assert "generation 7" in result.stdout
    assert "Added: alpha" in result.stdout
    assert "Removed: beta" in result.stdout
    assert "Modified: gamma" in result.stdout


def test_skills_reload_partial_warns_but_succeeds(monkeypatch) -> None:
    _install(
        monkeypatch,
        {
            "success": True,
            "changed": True,
            "partial": True,
            "generation": 8,
            "added": [],
            "removed": [],
            "modified": ["valid-skill"],
            "errors": [
                {
                    "name": "broken-skill",
                    "path": "/skills/broken-skill/SKILL.md",
                    "message": "invalid frontmatter",
                    "kept_previous": True,
                }
            ],
        },
    )

    result = runner.invoke(app, ["skills", "reload"])

    assert result.exit_code == 0, result.output
    assert "Warning" in result.stderr
    assert "broken-skill" in result.stderr


def test_skills_reload_gateway_unavailable_is_nonzero_and_never_falls_back(
    monkeypatch,
) -> None:
    OfflineGatewayClient.calls = []
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", OfflineGatewayClient)

    result = runner.invoke(app, ["skills", "reload", "--json"])

    assert result.exit_code == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "GATEWAY_UNAVAILABLE"
    assert "running Skill catalog was not refreshed" in error["message"]


def test_skills_reload_catalog_failure_exits_nonzero(monkeypatch) -> None:
    _install(
        monkeypatch,
        {
            "success": False,
            "changed": False,
            "partial": False,
            "generation": 4,
            "added": [],
            "removed": [],
            "modified": [],
            "errors": [
                {
                    "name": "",
                    "path": "/skills",
                    "message": "directory scan failed",
                    "kept_previous": True,
                }
            ],
        },
    )

    result = runner.invoke(app, ["skills", "reload"])

    assert result.exit_code == 1
    assert "generation 4 remains active" in result.stderr
    assert "directory scan failed" in result.stderr
