from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.gateway import boot
from opensquilla.gateway.adapters.setup_mutations import GatewaySetupRuntimePort


@pytest.mark.asyncio
async def test_setup_runtime_sync_search_passes_api_key_env(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_configure_search(**kwargs) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("opensquilla.tools.builtin.web.configure_search", fake_configure_search)

    await GatewaySetupRuntimePort(None, None).sync_search(
        SimpleNamespace(
            search_provider="exa",
            search_max_results=7,
            search_api_key="",
            search_api_key_env="CUSTOM_EXA_KEY",
            search_proxy="http://proxy.test",
            search_use_env_proxy=True,
            search_fallback_policy="network",
            search_diagnostics=True,
        )
    )

    assert calls == [
        {
            "provider_name": "exa",
            "max_results": 7,
            "api_key": "",
            "api_key_env": "CUSTOM_EXA_KEY",
            "proxy": "http://proxy.test",
            "use_env_proxy": True,
            "fallback_policy": "network",
            "diagnostics": True,
        }
    ]


def test_boot_search_provider_setup_no_longer_has_brave_only_promotion() -> None:
    source = boot.__loader__.get_source(boot.__name__)

    assert source is not None
    assert "Auto-select: use brave" not in source
    assert "os.environ.get(\"BRAVE_SEARCH_API_KEY\")" not in source
