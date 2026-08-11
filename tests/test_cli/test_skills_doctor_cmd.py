from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from opensquilla.cli.gateway_client import GatewayRPCError
from opensquilla.cli.main import app
from opensquilla.skills.hub.lockfile import LockEntry, Lockfile, compute_tree_sha256
from opensquilla.skills.loader import SkillLoader


class _OfflineGatewayClient:
    async def connect(self, url: str, *, token: str | None = None) -> None:
        raise ConnectionError("connection refused")

    async def close(self) -> None:
        return None


class _LegacyGatewayClient:
    closed = False

    async def connect(self, url: str, *, token: str | None = None) -> None:
        return None

    async def call(self, method: str, params: dict) -> dict:
        raise GatewayRPCError(
            method,
            code="METHOD_NOT_FOUND",
            message=f"Method not found: {method}",
        )

    async def close(self) -> None:
        type(self).closed = True


def test_reachable_legacy_gateway_requires_upgrade_without_offline_fallback(monkeypatch) -> None:
    offline_builds = 0
    _LegacyGatewayClient.closed = False

    def _offline_loader_must_not_be_built():
        nonlocal offline_builds
        offline_builds += 1
        raise AssertionError("a reachable Gateway must not trigger offline Doctor")

    monkeypatch.setattr(
        "opensquilla.cli.gateway_client.GatewayClient",
        _LegacyGatewayClient,
    )
    monkeypatch.setattr(
        "opensquilla.cli.skills_cmd._build_offline_skill_loader",
        _offline_loader_must_not_be_built,
    )

    result = CliRunner().invoke(app, ["skills", "doctor", "demo", "--json"])

    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "GATEWAY_UPGRADE_REQUIRED"
    assert payload["error"]["details"] == {
        "method": "skills.doctor",
        "gatewayCode": "METHOD_NOT_FOUND",
        "hint": (
            "Restart the Gateway from the same upgraded OpenSquilla installation, "
            "then run skills doctor again."
        ),
    }
    assert "Upgrade or restart the Gateway" in payload["error"]["message"]
    assert offline_builds == 0
    assert _LegacyGatewayClient.closed is True


def test_offline_skills_doctor_honors_name_based_disabled_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    managed = tmp_path / "managed"
    skill_dir = managed / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: disabled Doctor fixture\n---\nInstructions.\n",
        encoding="utf-8",
    )
    digest = compute_tree_sha256(skill_dir)
    Lockfile(
        installed={
            "demo": LockEntry(
                source="github",
                identifier="owner/repo:demo",
                relative_path="demo",
                directory_name="demo",
                manifest_name="demo",
                install_id="install-demo",
                tree_sha256=digest,
                sha256=digest,
            )
        }
    ).save(tmp_path / "skills-lock.json")
    loader = SkillLoader(
        managed_dir=managed,
        snapshot_path=tmp_path / "snapshot.json",
    )
    config = SimpleNamespace(
        skills=SimpleNamespace(disabled=["demo"], coding_mode=False),
    )
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "opensquilla.cli.gateway_client.GatewayClient",
        _OfflineGatewayClient,
    )
    monkeypatch.setattr(
        "opensquilla.cli.skills_cmd._build_offline_skill_loader",
        lambda: (config, loader),
    )

    result = CliRunner().invoke(app, ["skills", "doctor", "demo", "--json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    row = payload["skills"][0]
    assert row["lifecycle"]["selection_state"] == "disabled"
    assert row["active"] is False
    assert row["instruction_usable"] is False


def test_offline_skills_doctor_human_output_shows_degraded_compatibility(
    tmp_path: Path,
    monkeypatch,
) -> None:
    managed = tmp_path / "managed"
    skill_dir = managed / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: demo\n"
        "description: degraded Doctor fixture\n"
        "allowed-tools: Bash(npx example@latest *)\n"
        "---\n"
        "Instructions.\n",
        encoding="utf-8",
    )
    digest = compute_tree_sha256(skill_dir)
    Lockfile(
        installed={
            "demo": LockEntry(
                source="github",
                identifier="owner/repo:demo",
                relative_path="demo",
                directory_name="demo",
                manifest_name="demo",
                install_id="install-demo",
                tree_sha256=digest,
                sha256=digest,
                extra={"degraded_capabilities": ["scoped_tool_permissions"]},
            )
        }
    ).save(tmp_path / "skills-lock.json")
    loader = SkillLoader(
        managed_dir=managed,
        snapshot_path=tmp_path / "snapshot.json",
    )
    config = SimpleNamespace(
        skills=SimpleNamespace(disabled=[], coding_mode=False),
    )
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "opensquilla.cli.gateway_client.GatewayClient",
        _OfflineGatewayClient,
    )
    monkeypatch.setattr(
        "opensquilla.cli.skills_cmd._build_offline_skill_loader",
        lambda: (config, loader),
    )

    result = CliRunner().invoke(app, ["skills", "doctor", "demo"])

    assert result.exit_code == 0, result.output
    assert "Compatibility" in result.stdout
    assert "degraded" in result.stdout
