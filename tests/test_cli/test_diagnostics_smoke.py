"""Packaged runtime capability diagnostics remain available without task runners."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from opensquilla.cli import diagnostics_cmd
from opensquilla.cli.diagnostics_cmd import diagnostics_app

runner = CliRunner()


def test_smoke_imports_accepts_explicit_modules():
    result = runner.invoke(diagnostics_app, ["smoke-imports", "--module", "json"])
    assert result.exit_code == 0, result.output
    assert '"success": true' in result.stdout
    assert '"json"' in result.stdout


def test_smoke_imports_fails_on_missing_module():
    result = runner.invoke(
        diagnostics_app,
        ["smoke-imports", "--module", "opensquilla_missing_smoke_module_x"],
    )
    assert result.exit_code == 1
    assert '"success": false' in result.stdout
    assert "opensquilla_missing_smoke_module_x" in result.stdout


def test_smoke_router_reports_success(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        diagnostics_cmd,
        "_smoke_router_runtime",
        lambda: {
            "success": True,
            "available": True,
            "tier": "c1",
            "confidence": 0.88,
            "source": "v4_phase3",
            "route_class": "R1",
            "model_version": "test",
        },
    )

    result = runner.invoke(diagnostics_app, ["smoke-router"])

    assert result.exit_code == 0, result.output
    assert '"success": true' in result.stdout
    assert '"source": "v4_phase3"' in result.stdout


def test_smoke_router_fails_when_runtime_unavailable(monkeypatch: pytest.MonkeyPatch):
    def fail() -> dict[str, object]:
        raise RuntimeError("failed to initialize V4 Phase 3 router: No module named 'sklearn'")

    monkeypatch.setattr(diagnostics_cmd, "_smoke_router_runtime", fail)

    result = runner.invoke(diagnostics_app, ["smoke-router"])

    assert result.exit_code == 1
    assert '"success": false' in result.stdout
    assert "No module named" in result.stdout


def test_smoke_tokenizer_fails_offline_with_missing_encoding_asset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import tiktoken.registry

    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(tiktoken.registry, "ENCODINGS", {})

    result = runner.invoke(diagnostics_app, ["smoke-tokenizer"])

    assert result.exit_code == 1, result.output
    assert '"success": false' in result.stdout
    assert "offline tokenizer cache miss" in result.stdout


def test_smoke_tokenizer_does_not_accept_estimator_fallback(monkeypatch: pytest.MonkeyPatch):
    from types import SimpleNamespace

    import tiktoken

    from opensquilla import token_estimation

    text = "OpenSquilla offline tokenizer verification. 离线编码验证。"
    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda _name: SimpleNamespace(
            name="cl100k_base", encode=lambda *_args, **_kwargs: [1, 2], decode=lambda _: text
        ),
    )
    monkeypatch.setattr(
        token_estimation,
        "estimate_tokens_with_source",
        lambda _text: (2, "utf8_unicode_conservative"),
    )

    result = runner.invoke(diagnostics_app, ["smoke-tokenizer"])

    assert result.exit_code == 1, result.output
    assert '"success": false' in result.stdout
    assert "utf8_unicode_conservative" in result.stdout
