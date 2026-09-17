"""CLI tests for `opensquilla providers`."""

from __future__ import annotations

import tomllib
from pathlib import Path

from typer.testing import CliRunner

from opensquilla.cli.main import app

runner = CliRunner()


def test_providers_list_shows_all_supported(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    result = runner.invoke(app, ["providers", "list"])
    assert result.exit_code == 0
    out = result.stdout
    for pid in ("openrouter", "openai", "ollama", "vllm", "azure"):
        assert pid in out


def test_providers_list_marks_unsupported(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    result = runner.invoke(app, ["providers", "list"])
    assert result.exit_code == 0
    assert "openai_codex" in result.stdout
    assert "unsupported" in result.stdout.lower() or "disabled" in result.stdout.lower()


def test_providers_configure_writes_config(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    result = runner.invoke(
        app,
        [
            "providers", "configure", "openrouter",
            "--model", "deepseek/deepseek-v4-flash",
            "--api-key", "sk-test",
        ],
    )
    assert result.exit_code == 0, result.stdout
    text = target.read_text()
    assert "openrouter" in text
    assert "deepseek/deepseek-v4-flash" in text
    assert "sk-test" not in result.stdout
    config = tomllib.loads(text)
    assert config["image_generation"] == {
        "enabled": True,
        "binding": "follow_llm",
        "primary": "openrouter/google/gemini-3.1-flash-image-preview",
    }


def test_providers_configure_unsupported_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    result = runner.invoke(
        app, ["providers", "configure", "github_copilot", "--model", "x"]
    )
    assert result.exit_code != 0
    assert (
        "not runtime-supported" in result.stdout.lower()
        or "not runtime-supported" in (result.stderr or "").lower()
    )


def test_providers_configure_ollama_no_key_required(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    result = runner.invoke(
        app, ["providers", "configure", "ollama", "--model", "llama3"]
    )
    assert result.exit_code == 0
    assert "ollama" in target.read_text()


def test_providers_configure_vllm_requires_base_url(tmp_path, monkeypatch):
    # vllm is experimental (registry-runnable, unverified): configurable, but
    # its explicit base_url requirement still validates.
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    result = runner.invoke(
        app,
        ["providers", "configure", "vllm", "--model", "x", "--api-key", "k"],
    )
    assert result.exit_code != 0
    combined = (result.stdout + (result.stderr or "")).lower()
    assert "base_url" in combined

    result = runner.invoke(
        app,
        [
            "providers",
            "configure",
            "vllm",
            "--model",
            "x",
            "--api-key",
            "k",
            "--base-url",
            "http://localhost:8000/v1",
        ],
    )
    assert result.exit_code == 0


def test_providers_status_probe_column_surfaces_failure_kind(monkeypatch):
    monkeypatch.setattr(
        "opensquilla.cli.providers_cmd.run_gateway_sync",
        lambda *_args, **_kwargs: {
            "providers": [
                {
                    "providerId": "openrouter",
                    "active": True,
                    "configured": True,
                    "buildable": True,
                    "model": "openrouter/model",
                    "error": "",
                    "modelProbe": {
                        "status": "error",
                        "count": 0,
                        "failureKind": "auth_invalid",
                        "error": "HTTP 401 invalid credential",
                    },
                }
            ]
        },
    )

    result = runner.invoke(app, ["providers", "status", "--probe-models"])

    assert result.exit_code == 0, result.stdout
    assert "probe" in result.stdout
    assert "auth_invalid" in result.stdout
    assert "HTTP 401" in result.stdout
