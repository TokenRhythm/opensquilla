from __future__ import annotations

from pathlib import Path

import pytest

import opensquilla.cli.main as cli_main
from opensquilla.gateway.config import GatewayConfig


@pytest.mark.parametrize(
    ("agent_id", "expected_suffix"),
    [
        ("main", ()),
        ("ops", ("agents", "ops")),
    ],
)
def test_cli_dream_uses_configured_agent_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_id: str,
    expected_suffix: tuple[str, ...],
) -> None:
    configured_workspace = tmp_path / "configured workspace"
    cwd = tmp_path / "launch cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    cfg = GatewayConfig(workspace_dir=str(configured_workspace))
    monkeypatch.setattr(GatewayConfig, "load", classmethod(lambda cls, _path=None: cfg))

    dream = cli_main._build_cli_dream(agent_id, need_provider=False)

    assert dream.workspace == configured_workspace.joinpath(*expected_suffix)
    assert not (cwd / ".opensquilla").exists()


def test_cli_dream_prewarms_install_id_before_building_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = GatewayConfig(
        workspace_dir=str(tmp_path / "workspace"),
        privacy={"disable_network_observability": True},
    )
    calls: list[tuple[str, GatewayConfig]] = []

    monkeypatch.setattr(GatewayConfig, "load", classmethod(lambda cls, _path=None: cfg))

    def _prewarm(*, config: GatewayConfig) -> None:
        calls.append(("prewarm", config))

    class _Dream:
        def __call__(self, _agent_id: str):
            return object()

    def _build(*, config: GatewayConfig, turn_runner, need_provider: bool):
        assert turn_runner is None
        assert need_provider is True
        calls.append(("build", config))
        return _Dream()

    monkeypatch.setattr(
        "opensquilla.provider.tokenrhythm_correlation.prewarm_tokenrhythm_install_id",
        _prewarm,
    )
    monkeypatch.setattr("opensquilla.memory.dream_factory.build_dream_factory", _build)

    cli_main._build_cli_dream("main")

    assert calls == [("prewarm", cfg), ("build", cfg)]
    assert cfg.privacy.disable_network_observability is True
