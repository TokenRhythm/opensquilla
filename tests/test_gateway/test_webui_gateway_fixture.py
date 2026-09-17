from __future__ import annotations

import runpy
from pathlib import Path
from unittest.mock import Mock

import pytest

from opensquilla import token_estimation
from opensquilla.gateway.config import GatewayConfig


async def test_real_webui_fixture_counts_tokens_without_loading_external_encoding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    fixture = Path(__file__).resolve().parents[2] / "opensquilla-webui/e2e/goal-mode-gateway.py"
    namespace = runpy.run_path(str(fixture))
    main = namespace["main"]
    loader = Mock(side_effect=AssertionError("The offline fixture must not fetch an encoding"))
    monkeypatch.setattr(token_estimation, "_encoding", None)
    monkeypatch.setattr(token_estimation, "_load_encoding", loader)
    settings = {
        "PORT": "18791",
        "STATE": str(tmp_path),
        "EVENT_LOG": str(tmp_path / "provider.jsonl"),
        "RELEASE_FIRST": str(tmp_path / "first"),
        "RELEASE": str(tmp_path / "second"),
        "ORIGIN": "http://127.0.0.1:18792",
        "AUTH_MODE": "token",
    }
    for key, value in settings.items():
        monkeypatch.setenv(f"OPENSQUILLA_WEBUI_GOAL_E2E_{key}", value)

    class StartupVerifiedError(Exception):
        pass

    async def verify_startup(**kwargs: object) -> None:
        assert token_estimation.estimate_tokens_with_source("synthetic " * 20) == (
            100, "utf8_unicode_conservative",
        )
        loader.assert_not_called()
        assert isinstance(kwargs["config"], GatewayConfig)
        assert kwargs["config"].auth.mode == "token"
        raise StartupVerifiedError

    monkeypatch.setitem(main.__globals__, "start_gateway_server", verify_startup)
    with pytest.raises(StartupVerifiedError):
        await main()
