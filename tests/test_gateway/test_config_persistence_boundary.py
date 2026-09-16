from __future__ import annotations

from types import SimpleNamespace

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.config_persistence import persist_gateway_config


def test_persist_gateway_config_delegates_to_sparse_store(tmp_path, monkeypatch) -> None:
    import opensquilla.onboarding.config_store as config_store

    config = GatewayConfig()
    config.config_path = str(tmp_path / "config.toml")
    calls: list[tuple[GatewayConfig, str]] = []

    def record(candidate, *, path):
        calls.append((candidate, str(path)))
        return SimpleNamespace(path=path, backup_path=None)

    monkeypatch.setattr(config_store, "persist_config", record)

    persist_gateway_config(config)

    assert calls == [(config, config.config_path)]
